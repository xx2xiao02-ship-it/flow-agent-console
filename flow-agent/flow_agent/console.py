"""Local console with bounded, account-isolated dispatch over FlowEngine.

The console owns only its queue and uploaded inputs. The engine remains the sole
owner of Google sessions, generation, credits and its existing SQLite schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import uuid
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile, WebSocket, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from .engine import EngineError, FlowEngine, extract_json
from .image_models import ImageModelError
from .account_details import AccountReadError, FlowAccountReader
from .account_preparation import AccountPreparation
from .account_monitor import AccountMonitor
from .account_removal import AccountRemoval
from .media_library import MediaLibrary
from .media_storage import MediaStorage, StorageInput
from .media_migration import MigrationStart
from .browser_pages import BrowserPages
from .browser_execution import BrowserExecution
from .task_scheduler import TaskScheduler, SchedulerInput
from .retry_policy import RETRY_DELAYS, retry_action
from .official_images import OfficialImages, OfficialImageError
from .official_videos import OfficialVideos, OfficialVideoError
from .browser_visibility import OwnedBrowserVisibility, BrowserVisibilityError
from .lan_keys import LanKeyStore
from .credit_pricing import OMNI_COSTS, VEO_COSTS, PRICING, video_estimates
from .video_models import VideoModel, VIDEO_MODELS, validate_video_model
from .mp4_input import inspect_mp4
from .dispatch_quotes import DispatchQuotes, QuoteInput


class JobInput(BaseModel):
    kind: Literal["image", "video"]
    prompt: str = Field(min_length=1, max_length=4000)
    aspect: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] = "16:9"
    count: int = Field(default=1, ge=1, le=4)
    model: Literal["narwhal", "beluga", "harbor_seal", "gem_pix_2"] = "narwhal"
    video_model: VideoModel = "omni_flash"
    account_strategy: Literal["round_robin", "highest_credits", "lowest_credits"] = "round_robin"
    duration: Literal["4s", "6s", "8s", "10s"] = "8s"
    quality: Literal["360p", "720p"] = "720p"
    account_id: str | None = Field(default=None, description='默认省略或填null，由后台自动调度。仅用户明确指定账号时填写；填写后不参与轮流选号，暂停或异常账号会在生成前拒绝。')
    start_image_id: str | None = None
    end_image_id: str | None = None
    reference_image_ids: list[str] = Field(default_factory=list)
    media_rights_confirmed: bool = False
    source_video_id: str | None = None
    video_mode: Literal["text", "frames", "references", "edit"] | None = None
    auto_retry_limit: int = Field(default=0, ge=0, le=3, strict=True)
    retry_uncertain: bool = Field(default=False, strict=True)
    retry_switch_accounts: bool = Field(default=False, strict=True)
    dispatch_quote_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def check_video_options(self):
        if self.dispatch_quote_id and (self.auto_retry_limit or self.retry_uncertain or self.retry_switch_accounts):
            raise ValueError('一次性报价不允许自动重试或切号')
        if self.retry_uncertain and not self.auto_retry_limit:
            raise ValueError("允许结果不确定时重试，必须同时启用自动重试")
        inferred = ("edit" if self.source_video_id else "references" if self.reference_image_ids
                    else "frames" if self.start_image_id or self.end_image_id else "text")
        if self.video_mode is None:
            self.video_mode = inferred
        if self.video_mode != inferred:
            raise ValueError("视频模式与已提交的参考素材不匹配")
        if self.kind == "video" and self.aspect not in ("16:9", "9:16"):
            raise ValueError("视频仅支持 16:9 或 9:16")
        if self.kind == "image" and self.count != 1:
            raise ValueError("当前 Windows 引擎每次图片请求仅生成 1 张")
        if self.kind == "image" and self.video_mode != "text":
            raise ValueError("参考素材仅用于视频任务")
        if self.end_image_id and not self.start_image_id:
            raise ValueError("使用尾帧时必须提供首帧")
        if self.reference_image_ids and (self.start_image_id or self.end_image_id or self.source_video_id):
            raise ValueError("参考图不能与首尾帧或原视频混用")
        if self.source_video_id and (self.start_image_id or self.end_image_id):
            raise ValueError("视频编辑不能同时使用首尾帧")
        if len(set(self.reference_image_ids)) != len(self.reference_image_ids):
            raise ValueError("参考图不能重复")
        if any(not reference.strip() for reference in self.reference_image_ids):
            raise ValueError("参考图 ID 不能为空")
        if self.video_mode in ("references", "edit") and self.count != 1:
            raise ValueError("参考图生视频和视频编辑每次仅支持 1 条")
        if self.kind == 'video':
            validate_video_model(self.video_model, self.duration, self.quality, self.video_mode)
        if self.video_model == 'omni_flash' and self.video_mode in ("references", "edit") and (self.quality != "720p" or self.aspect != "16:9"):
            raise ValueError("此模式不支持指定比例或画质，请保留默认参数；输出由 Flow 决定")
        if self.video_mode == "edit" and self.duration != "8s":
            raise ValueError("视频编辑不支持指定时长，请保留默认参数；长度由原视频与 Flow 决定")
        return self


class RetryInput(BaseModel):
    acknowledge_possible_charge: bool = False
    auto_retry_limit: int | None = Field(default=None, ge=0, le=3, strict=True)
    retry_uncertain: bool | None = Field(default=None, strict=True)
    retry_switch_accounts: bool | None = Field(default=None, strict=True)


class ImagePageCheckInput(BaseModel):
    model: Literal['beluga'] = 'beluga'
    aspect: Literal['16:9', '9:16', '1:1', '4:3', '3:4'] = '16:9'
    count: Literal[1] = 1


class VideoPageCheckInput(BaseModel):
    video_model: VideoModel = 'omni_flash'
    aspect: Literal['16:9', '9:16'] = '16:9'
    duration: Literal['4s', '6s', '8s', '10s'] = '8s'
    quality: Literal['360p', '720p'] = '720p'
    count: int = Field(default=1, ge=1, le=4)

    @model_validator(mode='after')
    def check_model(self):
        validate_video_model(self.video_model, self.duration, self.quality)
        return self


class RotateLanKeyInput(BaseModel):
    acknowledge_revoke: bool = False


class LocalProfileInput(BaseModel):
    profile_id: str = Field(min_length=16, max_length=64)


class PrepareAccountInput(BaseModel):
    account_id: str | None = None


class AccountMonitorInput(BaseModel):
    enabled: bool


class ProxyActionInput(BaseModel):
    action: str
    value: str | None = None
    account_id: str | None = None
    proxy_id: str | None = None


class GenerationSettingsInput(BaseModel):
    visible: bool | None = None
    isolated: bool | None = None


class AccountRemovalInput(BaseModel):
    confirmed: bool = Field(default=False, strict=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_failure(exc: BaseException) -> str:
    """Never send subprocess argv, prompts, cookie paths or provider bodies to JS."""
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, OfficialVideoError):
        messages = {
            'unsupported_video_options': '官方网页视频链路不支持这组参数，本次未选择替代模型。',
            'video_model_not_selected': '未能确认官方页面已选择请求的视频模型；未自动切换为其他模型。',
            'video_model_unavailable': '当前账号网页没有请求的视频模型，请检查模型权限；本次未提交生成。',
            'video_parameters_unconfirmed': '未能确认官方视频的比例、时长、画质或数量。',
            'video_input_missing': '找不到本次参考素材，请重新选择本地文件。',
            'video_input_unconfirmed': '官方素材选择器未能确认上传的参考文件。',
            'video_upload_rights_required': 'Google 要求本人确认图片使用权，本次等待超时；请在下次显示的专用浏览器处理，程序不会代替您同意。',
            'video_upload_rights_confirmation_failed': '已获授权，但未能确认 Google 图片使用权弹窗已关闭，已停止，不会重复点击。',
            'video_response_unrecognized': '官方响应中没有可确认的视频编号，生成及扣费状态未知；请只读核对原项目，不要直接重试。',
            'video_rpc_error': '官方服务返回了错误响应，未取得有效视频结果；扣费状态未知，请只读核对原项目。',
            'original_result_unknown': '已隐藏查询180秒，仍未取得可严格关联的结果：待核对。已关闭本次浏览器，未重复提交；请核对官方历史和积分。',
            'recovery_project_mismatch': '账号当前项目与原任务项目不一致，已停止查询，未重新生成。',
            'video_result_count_mismatch': '官方网页返回的视频结果数量或项目与本次任务不一致，请核对网页历史和积分。',
            'video_result_timeout': '等待官方网页本次视频超时，请核对网页历史和积分。',
            'video_result_identity_mismatch': '官方视频详情与本次任务编号不一致或尚未就绪，未将旧视频计作成功。',
            'video_download_control_unconfirmed': '未确认原始视频下载选项，已停止；不会点击需要额外积分的放大选项。',
            'video_download_failed': '本次原始视频下载未完成，请保留官方网页历史中的结果。',
            'video_download_invalid': '下载文件未通过 MP4 完整性和视频轨道检查，请保留官方网页历史中的结果。',
            'video_page_failed': '官方视频页面控件未就绪或发生变化，请使用“检查视频页面（不生成）”核对。',
        }
        if exc.code in messages:
            from .provider_diagnostics import public_diagnostic
            diagnostic = public_diagnostic((getattr(exc, 'execution', None) or {}).get('generation_response'))
            message = messages[exc.code] + (' ' + diagnostic + '。' if diagnostic else '')
            return message + (' 本次可能已提交，不会自动重试。' if exc.submitted else ' 本次未提交生成。')
    if isinstance(exc, OfficialImageError):
        messages = {
            'flow_access_denied': 'Google 官方页面提示此账号无权访问 Flow，已停止；请使用具有 Flow 访问权限的账号。',
            'visibility_failed': '无法确认专用浏览器已隐藏，已停止操作并关闭本次浏览器。',
            'manual_timeout': '等待本人登录或人工验证超时，本次浏览器已自动关闭；请准备账号后再提交。',
            'manual_verification_required': '该账号需要人工登录或完成验证，已立即从执行池撤下并集中通知；请到账号管理点击“打开 Flow”处理，后台自检通过后自动恢复调度。',
            'headless_needs_login': '无头检查发现需要登录或人工验证；浏览器已关闭。请手动打开 Flow 完成验证后再检测。',
            'identity_not_ready': '官方页面未确认所选账号，已关闭本次浏览器，请先准备账号。',
            'profile_busy': '此账号的专用浏览器已打开，请关闭该专用窗口后再检查。程序不会接管或关闭已有窗口。',
            'identity_missing': '无法确认账号与专用浏览器资料的对应关系，请先刷新账号信息。',
            'identity_mismatch': '官方页面当前账号与任务账号不一致，已停止。',
            'project_missing': '该账号未保存有效的 Flow 项目，请先打开该账号的项目并同步。',
            'profile_missing': '找不到该账号的专用浏览器资料。',
            'browser_missing': '缺少专用浏览器，请检查安装文件。',
            'runtime_missing': '缺少网页操作组件，请升级程序或重新安装依赖。',
            'browser_driver_missing': '浏览器自动化驱动文件缺失，请修复安装客户端；不是网页控件或账号问题，本次未提交生成。',
            'browser_driver_failed': '浏览器自动化驱动无法启动，请修复安装客户端；本次未提交生成。',
            'preparation_busy': '账号准备正在运行，请等待结束。',
            'unsupported_options': '当前官方网页图片链路仅支持 Nano Banana 2.1、单账号、单张图片。',
            'model_not_selected': '未能确认官方页面已选择 Nano Banana 2.1。',
            'agent_panel_unconfirmed': '未能确认已切回官方直接生成模式（智能体开关或侧栏状态未就绪）；已安全停止。',
            'composer_settings_missing': '未找到提示框内的生图设置；已停止，未点击全局设置。',
            'parameters_unconfirmed': '官方页面的图片类型、比例或单张数量未确认。',
            'submit_disabled': '提示词已填写，但官方页面的生成按钮在等待后仍不可用。请检查账号权限或网页提示。',
            'prompt_unconfirmed': '官方页面未能确认本次提示词，已停止提交。',
            'provider_rejected': '官方网页未完成生成或返回拒绝，请核对网页历史和积分。',
            'provider_empty': '官方网页请求未返回可确认的结果，请核对网页历史和积分。',
            'generation_response_unreadable': '生成请求已发出，但本地浏览器未能读取响应内容，无法确认结果编号；请只读核对原项目，勿重复提交。',
            'original_result_unknown': '已隐藏查询180秒，仍无法确认本次图片结果，待核对。未重复提交；请核对原项目历史和积分。',
            'result_identity_mismatch': '官方详情页结果编号与本次生成响应不一致，已停止下载，未将旧图片计作成功。',
            'result_ambiguous': '页面出现多个新结果，无法确认哪一张属于本次任务。',
            'result_timeout': '等待官方页面本次图片超时，请核对网页历史和积分。',
            'download_invalid': '下载内容不是完整的受支持图片，请保留网页历史中的结果。',
            'download_failed': '原图下载失败，请保留网页历史中的结果。',
        }
        message = messages.get(exc.code, '官方图片页面控件未就绪或发生变化，请使用“不生成检查”核对。')
        from .provider_diagnostics import public_diagnostic
        diagnostic = public_diagnostic((getattr(exc, 'execution', None) or {}).get('generation_response'))
        if diagnostic:
            message += ' ' + diagnostic + '。'
        return message + (' 本次可能已提交，不会自动重试。' if exc.submitted else ' 本次未提交生成。')
    if isinstance(exc, ImageModelError):
        if str(exc) == "narwhal_retired":
            return "该账号已切换至 Nano Banana 2.1（BELUGA），不再提供 Nano Banana 2（NARWHAL）。请重新选择 2.1 后提交；本次未调用生成。"
        if str(exc) == "model_unavailable":
            return "该账号的实时模型目录未提供所选图片模型（MODEL_UNAVAILABLE），本次未调用生成。请更换模型或选择支持该模型的账号。"
        return "无法读取该账号的实时图片模型目录，本次未调用生成。请检查网络或准备账号后再试。"
    if isinstance(exc, EngineError):
        if exc.returncode == 127:
            return "找不到 Flow 引擎，请检查 Windows 程序文件。"
        if exc.returncode == -1:
            return "引擎超时；上游结果可能仍在处理，请先核对历史与余额。"
        words = (exc.stderr + " " + exc.stdout).lower()
        for code, message in (
            ('browser_verification_protected', '当前 Flow 官方页面已启用外部验证调用保护（BROWSER_VERIFICATION_PROTECTED），程序无法使用该页面的验证接口。本次未获取验证令牌、未提交生成；刷新积分或重复登录不能解除该保护。请使用官方页面生成。'),
            ('browser_extension_update_required', 'Flow 扩展仍运行旧版本，本次未提交生成。请在对应 Chrome 的扩展管理页重新加载 Flow Bridge 后检查。'),
            ('browser_owned_tab_missing', '未确认本次新打开的 Flow 标签，本次未提交生成，原有网页已保留。'),
            ('browser_account_not_connected', '未连接到所选账号的浏览器扩展，本次未提交生成。请打开对应账号的 Flow；如需登录或人工验证，请本人完成。'),
            ('browser_page_not_ready', '所选账号的 Flow 页面尚未加载验证组件，本次未提交生成。请检查保留的窗口，完成登录或人工验证。'),
            ('browser_account_changed', '浏览器当前身份与所选账号不一致，本次未提交生成。请重新准备对应账号。'),
            ('browser_bridge_occupied', '浏览器桥接端口被其他进程占用，本次未提交生成。请检查原有 Flow 服务。'),
            ('browser_account_missing', '缺少所选账号的完整本地会话，本次未提交生成。'),
            ('browser_project_missing', '该账号尚未记录 Flow 项目，本次未提交生成。请先在对应账号的 Flow 打开一个项目。'),
            ('browser_preparation_busy', '正在准备账号，本次未提交生成。请等待准备结束。'),
            ('browser_single_account_required', '本地浏览器验证按单账号串行运行，本次未提交多账号生成。'),
            ('generation_replay_blocked', '已阻止引擎自动重复提交。首个请求的结果尚未确认，请先核对历史和积分。'),
        ):
            if code in words:
                return message
        if "public_error_unusual_activity" in words:
            return "Google Flow 检测到异常活动，已拒绝本次生成。请先核对历史和积分，再用“一键准备账号”检查会话；不要连续重试。"
        public_reason = re.search(r'\bpublic_error_[a-z0-9_]{1,80}\b', words)
        if public_reason:
            return f"Google Flow 已拒绝本次生成（{public_reason.group().upper()}）。请先核对历史与积分，按上游提示处理；不会自动重试。"
        if any(needle in words for needle in ("invalid model", "unknown model", "unsupported model")):
            return "Flow 拒绝了所选模型（MODEL_UNAVAILABLE）。请核对 Flow 当前可用型号；不要连续重试。"
        # 'captcha' also appears in normal strategy/probe logs. A mention is
        # not evidence that verification failed. Prefer the structured result.
        try:
            result = extract_json(exc.stdout)
        except ValueError:
            result = None
        if (isinstance(result, dict) and result.get("status") == "empty"
                or "returned no image" in words or "the retry came back empty too" in words):
            return "Flow 未返回生成结果（EMPTY_RESULT），引擎未提供具体原因。请先核对历史和积分，勿连续重试；这不等于已确认验证码失败。"
        if re.search(r"(?:re)?captcha[^\r\n]{0,100}(?:fail|timed?\s*out|unavailable|reject|denied|missing)", words):
            return "Google 验证未通过（CAPTCHA_FAILED）。请检查对应账号的 Flow 页面和扩展；如有登录或人工验证，请本人完成。"
        for needle, message in (
            ("insufficient", "积分不足。"),
            ("no account", "没有可用账号。"),
            ("no cookies", "账号会话缺失。"),
            ("rate limit", "上游限流，请稍后检查状态。"),
            ("unauthorized", "会话可能已失效，请刷新账号状态。"),
        ):
            if needle in words:
                return message
        return f"Flow 引擎返回错误（退出码 {exc.returncode}），请检查账号、浏览器桥接和上游状态。"
    return "本地处理失败，请检查服务端环境。"


class ConsoleService:
    def __init__(self, engine: FlowEngine):
        self.engine = engine
        self.db_path = engine.root / "data" / "console.db"
        self.upload_dir = engine.root / "data" / "console_uploads"
        self.cookies_dir = engine.root / "cookies"
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.account_cursor = 0
        self.scheduler = TaskScheduler(self)
        self.dispatch_quotes = DispatchQuotes(self)
        self.bridge_process: asyncio.subprocess.Process | None = None
        self.bridge_lock = asyncio.Lock()
        self.bridge_reader: asyncio.Task | None = None
        self.extension_refreshed_profiles: set[str] = set()
        self.browser_execution = BrowserExecution(self)
        engine.browser_execution = self.browser_execution
        self.official_images = OfficialImages(self)
        engine.official_images = self.official_images
        self.official_videos = OfficialVideos(self)
        engine.official_videos = self.official_videos
        self.preparation = AccountPreparation(self)
        self.account_sync_task: asyncio.Task | None = None
        self.balance_refresh_lock = asyncio.Lock()
        self.account_details_task: asyncio.Task | None = None
        self.details_attempts: dict[str, tuple[str, float]] = {}
        self.monitor = AccountMonitor(self)
        self.account_removal = AccountRemoval(self)
        self.media_storage = MediaStorage(self)
        engine.media_storage = self.media_storage
        self.media_library = MediaLibrary(self)
        self.browser_pages = BrowserPages(self)
        from .startup_check import StartupCheck
        self.startup = StartupCheck(self)
        engine.startup_readiness = self.startup

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""create table if not exists console_jobs (
                id text primary key, kind text not null, status text not null,
                request_json text not null, result_json text, error text,
                created_at text not null, started_at text, finished_at text,
                retry_of text)""")
            db.execute("""create table if not exists console_meta (
                key text primary key, value text not null)""")
            self.dispatch_quotes.initialize(db)
            db.execute("""create table if not exists console_account_profiles (
                account_key text primary key, profile_name text not null)""")
            db.execute("""create table if not exists console_account_bindings (
                account_key text primary key, account_id text not null,
                bundle_digest text not null, refreshed_at text)""")
            db.execute("""create table if not exists console_account_details (
                account_key text primary key, bundle_digest text not null,
                name text, email text, credits integer, checked_at text,
                error text)""")
            db.execute("""create table if not exists proxy_pool (
                id text primary key, url text not null, added_at text not null,
                last_ok_at text, last_error text)""")
            db.execute("""create table if not exists console_account_proxy (
                account_key text primary key, proxy_id text not null,
                assigned_at text not null)""")
            # A process crash leaves the upstream outcome unknown. Never resubmit.
            db.execute("""update console_jobs set status='interrupted',
                error='服务重启，结果未确认；请先检查素材和积分。', finished_at=?
                where status in ('queued','running')""", (utc_now(),))
        self.media_storage.load()
        self.media_storage.migration.load()

    async def start(self):
        self.startup.start()

    async def stop(self):
        await self.startup.stop()
        await self.media_storage.migration.stop()
        await self.preparation.stop()
        if self.account_details_task:
            self.account_details_task.cancel()
            try:
                await self.account_details_task
            except asyncio.CancelledError:
                pass
            self.account_details_task = None
        await self.monitor.stop()
        if self.account_sync_task:
            self.account_sync_task.cancel()
            try:
                await self.account_sync_task
            except asyncio.CancelledError:
                pass
            self.account_sync_task = None
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            self.worker = None
        if self.bridge_process and self.bridge_process.returncode is None:
            self.bridge_process.terminate()
            try:
                await asyncio.wait_for(self.bridge_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.bridge_process.kill()
                await self.bridge_process.wait()
        self.bridge_process = None
        if self.bridge_reader:
            self.bridge_reader.cancel()
            try:
                await self.bridge_reader
            except asyncio.CancelledError:
                pass
            self.bridge_reader = None

    async def _read_bridge_diagnostics(self, process):
        if not getattr(process, "stderr", None):
            return
        try:
            while line := await process.stderr.readline():
                self.preparation.observe_bridge_line(line.decode("utf-8", "replace"))
        except (OSError, ValueError):
            return

    async def restart_idle_bridge(self):
        self.preparation.require_idle_queue()
        async with self.bridge_lock:
            process = self.bridge_process
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                self.bridge_process = None
            if self.bridge_reader:
                self.bridge_reader.cancel()
                try:
                    await self.bridge_reader
                except asyncio.CancelledError:
                    pass
                self.bridge_reader = None
            self.preparation.synced.clear()
        await self.start_bridge()

    @staticmethod
    def _bridge_port_open() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 9222), timeout=0.2):
                return True
        except OSError:
            return False

    def bridge_status(self) -> dict:
        extension = self.engine.root.parent / "flow-extension"
        binary = self.engine.binary
        owned = self.bridge_process is not None and self.bridge_process.returncode is None
        occupied = self._bridge_port_open()
        state = ("running" if owned and occupied else "starting" if owned else
                 "occupied" if occupied else "missing" if not binary.is_file() else "stopped")
        return {"state": state, "extension_dir": str(extension),
                "extension_available": (extension / "manifest.json").is_file()}

    @staticmethod
    def _find_extension_browser() -> Path | None:
        """Chrome for Testing supports unpacked extension loading; branded Chrome does not."""
        explicit = os.environ.get("FLOW_AGENT_BROWSER")
        if explicit:
            candidate = Path(explicit)
            return candidate if candidate.is_file() else None
        engine_root = os.environ.get("FLOW_AGENT_ROOT")
        if engine_root:
            bundled = Path(engine_root).parent / "browser" / "chrome-win64" / "chrome.exe"
            if bundled.is_file():
                return bundled
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            return None
        browser_root = Path(local_app_data) / "ms-playwright"
        builds = [p for p in browser_root.glob("chromium-*")
                  if p.name.removeprefix("chromium-").isdigit()]
        builds.sort(key=lambda p: int(p.name.removeprefix("chromium-")), reverse=True)
        return next((p / "chrome-win64" / "chrome.exe" for p in builds
                     if (p / "chrome-win64" / "chrome.exe").is_file()), None)

    async def connect_account(self) -> dict:
        """Open a dedicated visible Chrome profile with the bundled extension."""
        chrome = self._find_extension_browser()
        extension = self.engine.root.parent / "flow-extension"
        if not chrome:
            raise HTTPException(503, "未找到可加载扩展的 Chrome for Testing，请重新运行 Windows 一键启动脚本。")
        if not (extension / "manifest.json").is_file():
            raise HTTPException(503, "未找到 Flow Bridge 扩展目录。")
        await self.start_bridge()
        # One dedicated browser profile per saved account keeps simultaneous
        # Google sign-ins separate. Repeated clicks before sync reuse the slot.
        initial_files = set(self._cookie_files())
        profile = self._new_browser_profile()
        try:
            subprocess.Popen(
                [str(chrome), f"--user-data-dir={profile}",
                 f"--load-extension={extension}", "--no-first-run", "--new-window",
                 "https://labs.google/fx/tools/flow"],
                cwd=str(self.engine.root),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise HTTPException(502, "无法打开专用浏览器，请检查安装文件和本机权限。") from exc
        if self.account_sync_task:
            self.account_sync_task.cancel()
        self.account_sync_task = asyncio.create_task(
            self._watch_account_sync(initial_files, profile.name),
            name="flow-account-sync",
        )
        return {"state": "awaiting_login"}

    async def _watch_account_sync(self, initial_files: set[str], profile_name: str) -> None:
        """Reopen the owned bridge until a newly signed-in profile is saved.

        The engine bootstraps an extension when it connects, but a sign-in that
        happens after that handshake may not trigger another full cookie pull.
        A bridge reconnect performs that pull without reading sessions here.
        """
        try:
            for _ in range(120):  # 30 minutes to finish interactive login.
                await asyncio.sleep(15)
                new_files = set(self._cookie_files()) - initial_files
                if new_files:
                    if len(new_files) == 1:
                        key = next(iter(new_files))
                        _, profile_email = self._profile_identity(profile_name)
                        detail = self._verified_detail(key) or {}
                        if profile_email and detail.get('email') and profile_email.casefold() != detail['email'].casefold():
                            return  # A different browser synced; do not associate it by arrival order.
                        with self.connect() as db:
                            owners = db.execute("select account_key from console_account_profiles where profile_name=? and account_key<>?",
                                                (profile_name, key)).fetchall()
                            if owners:
                                return  # A dedicated profile cannot be assigned to a second account.
                            db.execute("insert or replace into console_account_profiles values (?, ?)",
                                       (key, profile_name))
                        # The bridge can first save cookies before the page has
                        # finished loading its tokens. Keep waiting/reconnecting
                        # until that same account's session is complete.
                        if self._complete_session(self._cookie_files()[key]):
                            return
                    else:
                        return  # Ambiguous identity: never bind by file order.
                if not self.bridge_process or self.bridge_process.returncode is not None:
                    return  # Never restart somebody else's bridge.
                with self.connect() as db:
                    active = db.execute(
                        "select 1 from console_jobs where status in ('queued','running') limit 1"
                    ).fetchone()
                if active:
                    continue  # Do not interrupt generation or a queued task.
                async with self.bridge_lock:
                    process = self.bridge_process
                    if not process or process.returncode is not None:
                        return
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                    self.bridge_process = None
                await self.start_bridge()
        except asyncio.CancelledError:
            raise
        except (OSError, HTTPException):
            return  # The next user action can report bridge startup errors.

    async def start_bridge(self) -> dict:
        async with self.bridge_lock:
            if self.browser_execution.handoff_active:
                raise HTTPException(409, '生成进程正在使用浏览器验证连接，请等待当前操作结束。')
            status = self.bridge_status()
            if status["state"] in ("running", "starting"):
                return status
            if status["state"] == "occupied":
                raise HTTPException(409, "本机 9222 端口已被其他程序占用，请检查原有 bridge 窗口。")
            if status["state"] == "missing":
                raise HTTPException(503, "未找到 Flow Windows 引擎程序。")
            try:
                self.bridge_process = await asyncio.create_subprocess_exec(
                    str(self.engine.binary), "bridge", cwd=str(self.engine.root),
                    stdout=subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    env={**self.engine._process_env(), 'FLOW_BRIDGE_SYNC_ONLY': '1',
                         'FLOW_BLOCK_GENERATION': '1'},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.preparation.synced.clear()
                self.bridge_reader = asyncio.create_task(self._read_bridge_diagnostics(self.bridge_process))
            except OSError as exc:
                raise HTTPException(502, "Flow bridge 无法启动，请检查引擎文件和本机权限。") from exc
            for _ in range(20):
                if self.bridge_process.returncode is not None:
                    self.bridge_process = None
                    raise HTTPException(502, "Flow bridge 启动失败，请检查本机 9222 端口和引擎程序。")
                if self._bridge_port_open():
                    return self.bridge_status()
                await asyncio.sleep(0.15)
            self.bridge_process.terminate()
            await self.bridge_process.wait()
            self.bridge_process = None
            raise HTTPException(502, "Flow bridge 未在 3 秒内就绪，请检查本机环境。")

    def _cookie_files(self) -> dict[str, Path]:
        if not self.cookies_dir.is_dir():
            return {}
        removed = self.account_removal.records()
        return {p.stem: p for p in self.cookies_dir.glob("account_*.json") if p.is_file() and p.stem not in removed}

    async def _watch_account_details(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self.monitor.start_check()
            except (OSError, ValueError, sqlite3.Error):
                continue

    async def refresh_account_details(self, only_changed: bool = False, *, account_ids: set[str] | None = None,
                                      on_check=None, pause_when_busy: bool = False):
        """Serial read-only probes; never guess the identity from browser order."""
        from . import proxy_pool
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.connect() as db:
            known = {row["account_key"]: dict(row) for row in db.execute("select * from console_account_details")}
        default_proxy = self.engine._process_env().get("FLOW_PROXY")
        for key, path in self._cookie_files().items():
            if account_ids is not None and key not in account_ids:
                continue
            if pause_when_busy:
                with self.connect() as db:
                    active = db.execute("select 1 from console_jobs where status in ('queued','running') limit 1").fetchone()
                if active or self.preparation.running:
                    break
            if loop.time() - started > 45:
                break
            if not self._complete_session(path):
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            old = known.get(key, {})
            attempt = self.details_attempts.get(key)
            if only_changed and (old.get("bundle_digest") == digest and old.get("email") and old.get("credits") is not None
                                 or attempt and attempt[0] == digest and loop.time() - attempt[1] < 300):
                continue
            self.details_attempts[key] = (digest, loop.time())
            if on_check:
                on_check(key)
            try:
                # One client per account so its bound exit stays sticky.
                proxy_url = proxy_pool.raw_url_for(self, key) or default_proxy
                async with httpx.AsyncClient(proxy=proxy_url, timeout=8,
                                             follow_redirects=False) as client:
                    result = await FlowAccountReader(client, json.loads(raw)).read()
            except (AccountReadError, ValueError, TypeError, KeyError, AttributeError):
                result = {"name": None, "email": None, "credits": None, "error": "incomplete"}
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                continue  # A browser changed accounts/cookies during the probe.
            _, profile_email = self._profile_identity(self._account_profile_name(key))
            expected_email = old.get("email") or profile_email
            if result["email"] and expected_email and result["email"].casefold() != expected_email.casefold():
                result = {"name": old.get("name"), "email": expected_email,
                          "credits": None, "error": "identity"}
            elif not result["email"]:
                result["credits"] = None
                result["error"] = result["error"] or "unread"
            # Keep the last known identity when an expired session cannot
            # answer. Credits/error still reflect this failed live read.
            result["name"] = result["name"] or old.get("name")
            result["email"] = result["email"] or old.get("email")
            with self.connect() as db:
                db.execute("insert or replace into console_account_details values (?, ?, ?, ?, ?, ?, ?)",
                           (key, digest, result["name"], result["email"], result["credits"], utc_now(), result["error"]))
            # A clean live read (identity + credits) clears the manual-attention
            # flag: the human has handled it and the account re-enters the pool.
            if result.get("credits") is not None and not result.get("error"):
                from .account_scheduling import clear_manual_attention
                clear_manual_attention(self, key)
        current = [self._verified_detail(key) for key, path in self._cookie_files().items() if self._complete_session(path)]
        if current and all(item and item["credits"] is not None for item in current):
            checked = max(item["checked_at"] for item in current)
            with self.connect() as db:
                db.execute("insert or replace into console_meta values ('last_successful_balance_refresh', ?)", (checked,))
                db.execute("delete from console_meta where key='balance_refresh_error'")

    def _verified_detail(self, account_id: str) -> dict | None:
        path = self._cookie_files().get(account_id)
        if not path:
            return None
        with self.connect() as db:
            row = db.execute("select * from console_account_details where account_key=?", (account_id,)).fetchone()
        return dict(row) if row and row["bundle_digest"] == hashlib.sha256(path.read_bytes()).hexdigest() else None

    def _normal_profile_for_email(self, email: str | None) -> dict | None:
        if not email:
            return None
        matches = [p for p in self._local_profiles().values() if p["emails"] == [email]]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _local_chrome_root() -> Path | None:
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "Google" / "Chrome" / "User Data" if local else None

    @staticmethod
    def _find_local_chrome() -> Path | None:
        candidates = []
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(variable)
            if base:
                candidates.extend(Path(base) / "Google" / "Chrome" / folder / "chrome.exe"
                                  for folder in ("Application", "Bin"))
        return next((p for p in candidates if p.is_file()), None)

    def _local_profiles(self) -> dict[str, dict]:
        """Display metadata only. Never open Cookies, Login Data or Local State."""
        root = self._local_chrome_root()
        if root is None or not root.is_dir():
            return {}
        profiles = {}
        for folder in sorted(root.iterdir()):
            if not re.fullmatch(r"Default|Profile \d+", folder.name) or folder.is_symlink():
                continue
            try:
                info = json.loads((folder / "Preferences").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(info, dict):
                continue
            accounts = info.get("account_info", [])
            if not isinstance(accounts, list):
                continue
            emails = list(dict.fromkeys(a["email"].strip()[:320] for a in accounts
                          if isinstance(a, dict) and isinstance(a.get("email"), str)
                          and a["email"].strip()))
            if not emails:
                continue
            metadata = info.get("profile")
            label = metadata.get("name", folder.name) if isinstance(metadata, dict) else folder.name
            key = hashlib.sha256(str(folder).encode()).hexdigest()[:24]
            profiles[key] = {"id": key, "label": str(label)[:128],
                             "emails": emails, "directory": folder.name}
        return profiles

    def local_profiles(self) -> list[dict]:
        # The UI does not need filesystem paths or browser preference contents.
        return [{k: v for k, v in item.items() if k != "directory"}
                for item in self._local_profiles().values()]

    async def open_local_profile(self, profile_id: str, *, account_id: str | None = None,
                                 background: bool = False) -> dict:
        self.media_storage.migration.require_available()
        profile = self._local_profiles().get(profile_id)
        if profile is None:
            raise HTTPException(404, "本机 Chrome 账号资料不存在，请重新扫描。")
        chrome = self._find_local_chrome()
        root = self._local_chrome_root()
        if not chrome or not root:
            raise HTTPException(503, "未找到本机 Google Chrome。")
        await self.start_bridge()
        try:
            refresh_url = None
            if account_id:
                refresh_url = await self._refresh_account_extension(chrome, root / profile['directory'],
                        self.engine.root.parent / 'flow-extension',
                        user_data_dir=root, profile_directory=profile['directory'], background=background)
            # Existing accounts already have a bridge. Only the initial import
            # needs the extensions setup page; opening it on every preparation
            # leaves an extra tab/window behind after Flow itself has closed.
            page_url = self.browser_pages.launch_url(account_id) if account_id else "https://labs.google/fx/tools/flow"
            setup_pages = [] if account_id else ["chrome://extensions/"]
            process = subprocess.Popen([str(chrome), f"--user-data-dir={root}",
                              f"--profile-directory={profile['directory']}", "--new-window",
                              *(['--start-minimized'] if background else []), page_url,
                              *([refresh_url] if refresh_url else []), *setup_pages],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.browser_pages.record_process(page_url, process, browser=chrome, profile=root)
        except OSError as exc:
            raise HTTPException(502, "无法打开所选 Chrome 账号。") from exc
        # Launching Chrome proves neither authentication nor a credit balance.
        return {"state": "awaiting_extension", "profile_id": profile_id}

    def open_extension_folder(self) -> dict:
        extension = self.engine.root.parent / "flow-extension"
        if not (extension / "manifest.json").is_file():
            raise HTTPException(503, "Flow Bridge 扩展文件缺失，请重新安装。")
        try:
            os.startfile(str(extension))
        except (AttributeError, OSError) as exc:
            raise HTTPException(503, "无法打开扩展文件夹。") from exc
        return {"opened": True, "extension_dir": str(extension)}

    def _profile_identity(self, profile_name: str | None) -> tuple[str | None, str | None]:
        """Read only display metadata from our dedicated Chrome profile."""
        if not profile_name or not re.fullmatch(r"flow_browser_profile_\d+", profile_name):
            return None, None
        prefs = self.engine.root / "data" / profile_name / "Default" / "Preferences"
        try:
            accounts = json.loads(prefs.read_text(encoding="utf-8")).get("account_info", [])
        except (OSError, ValueError):
            return None, None
        accounts = [a for a in accounts if isinstance(a, dict) and
                    isinstance(a.get("email"), str) and a["email"].strip()]
        if len(accounts) != 1:
            return None, None  # A profile with several sign-ins is ambiguous.
        account = accounts[0]
        email = account["email"].strip()[:320]
        name = account.get("full_name")
        name = name.strip()[:128] if isinstance(name, str) else ""
        return name or email, email

    def _account_profile_name(self, account_id: str) -> str | None:
        from .account_identity import canonical
        account_id = canonical(self, account_id)
        with self.connect() as db:
            row = db.execute("select profile_name from console_account_profiles where account_key=?",
                             (account_id,)).fetchone()
        name = row[0] if row else None
        if name is None and len(self._cookie_files()) == 1:
            available = [p for p in (self.engine.root / "data").glob("flow_browser_profile_*")
                         if p.is_dir()]
            if len(available) == 1:
                name = available[0].name
        return name if name and re.fullmatch(r"flow_browser_profile_\d+", name) else None

    def _new_browser_profile(self) -> Path:
        # Account count shrinks after removals and cannot identify a free slot.
        with self.connect() as db:
            used = {r[0] for r in db.execute("select profile_name from console_account_profiles")}
        data = self.engine.root / "data"
        data.mkdir(parents=True, exist_ok=True)
        index = 1
        while True:
            profile = data / f"flow_browser_profile_{index}"
            index += 1
            if profile.name in used:
                continue
            try:
                profile.mkdir()  # Exclusive creation, including simultaneous clicks.
                return profile
            except FileExistsError:
                continue

    def _manual_account_profile(self, account_id: str, profile_name: str | None,
                                *, background: bool) -> str | None:
        detail = self._verified_detail(account_id) or {}
        email = (detail.get('email') or '').casefold()
        _, actual = self._profile_identity(profile_name)
        with self.connect() as db:
            others = [r[0] for r in db.execute(
                "select account_key from console_account_profiles where profile_name=? and account_key<>?",
                (profile_name, account_id))]
        conflict = bool(email and actual and email != actual.casefold())
        for key in others:
            other = self._verified_detail(key) or {}
            if not email or not other.get('email') or other['email'].casefold() != email:
                conflict = True
        if not conflict:
            return profile_name
        if background:
            raise HTTPException(409, '账号与浏览器登录身份不一致，请手动打开 Flow 修复连接；本次未生成。')
        # Preserve old browser data; never switch another account's login.
        fresh = self._new_browser_profile()
        with self.connect() as db:
            db.execute("insert or replace into console_meta values (?, ?)",
                       ('profile_binding_before_repair:' + account_id, profile_name or ''))
            db.execute("insert or replace into console_account_profiles values (?, ?)",
                       (account_id, fresh.name))
        return fresh.name

    async def open_account_browser(self, account_id: str, *, background: bool = False) -> dict:
        from .account_identity import canonical
        account_id = canonical(self, account_id)
        self.media_storage.migration.require_available()
        if account_id not in self._cookie_files():
            raise HTTPException(404, "本地账号不存在。")
        profile_name = self._manual_account_profile(account_id,
            self._account_profile_name(account_id), background=background)
        profile = self.engine.root / "data" / profile_name if profile_name else None
        if profile is None or not profile.is_dir():
            detail = self._verified_detail(account_id)
            normal = self._normal_profile_for_email(detail.get("email") if detail else None)
            if normal:
                if background:
                    raise HTTPException(409, '后台隐藏任务需要独立账号浏览器资料，请先通过“连接账号”建立专用资料。原有 Chrome 保留。')
                return await self.open_local_profile(normal["id"], account_id=account_id, background=background)
            raise HTTPException(409, "找不到这个账号的专用浏览器资料，请重新连接 Flow 账号。")
        chrome = self._find_extension_browser()
        extension = self.engine.root.parent / "flow-extension"
        if not chrome or not (extension / "manifest.json").is_file():
            raise HTTPException(503, "专用 Chrome 或 Flow 扩展缺失，请重新运行 Windows 一键启动脚本。")
        await self.start_bridge()
        guard = None
        lease = self.browser_pages.pending.get(account_id)
        if background and lease is not None:
            guard = OwnedBrowserVisibility(profile, Path(chrome))
            try:
                await guard.start()
            except BrowserVisibilityError:
                raise HTTPException(409, '此账号的专用浏览器已打开或无法确认独占使用，请先关闭该专用窗口。原有窗口不会被接管。') from None
            lease.visibility = guard
            self.browser_execution.active_visibility = guard
        try:
            # App windows avoid tabbed startup restoring older console pages.
            # Cold launches load the current unpacked worker. A reused, stale
            # worker is rejected by the native check before any submission; the
            # explicit prepare/open action remains available to reload it.
            refresh_url = (None if background else
                           await self._refresh_account_extension(chrome, profile, extension))
            page_url = self.browser_pages.launch_url(account_id)
            from . import proxy_pool
            bound = proxy_pool.proxy_for(self, account_id) if account_id else None
            process = subprocess.Popen([str(chrome), f"--user-data-dir={profile}",
                              f"--load-extension={extension}", "--no-first-run", '--disable-background-mode',
                              *([f"--proxy-server={bound['server']}"] if bound else []),
                              *([*guard.launch_args(), f'--app={page_url}'] if background else
                                ['--new-window', page_url, *([refresh_url] if refresh_url else [])])],
                             cwd=str(self.engine.root), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.browser_pages.record_process(page_url, process, browser=chrome, profile=profile)
        except OSError as exc:
            raise HTTPException(502, "无法打开这个账号的专用 Chrome 窗口。") from exc
        return {"state": "opened"}

    async def _refresh_account_extension(self, chrome: Path, profile: Path, extension: Path,
                                         *, user_data_dir: Path | None = None,
                                         profile_directory: str | None = None, background: bool = False) -> str | None:
        """Return the installed extension refresh URL for the SAME Flow window.

        An unpacked MV3 worker can keep the old imported modules after an app
        upgrade. Chrome's own runtime.reload loads the new manifest and modules.
        Never edit the browser's preferences or restart its whole process.
        A separate refresh-only window becomes a blank window on reload. The
        caller opens Flow first, then this URL in that single window, so reload
        removes only the extension page and leaves the owned Flow tab to close.
        """
        profile_key = str(profile.resolve())
        if profile_key in self.extension_refreshed_profiles or not (extension / 'refresh.html').is_file():
            return
        for name in ('Secure Preferences', 'Preferences', 'Default/Secure Preferences', 'Default/Preferences'):
            try:
                settings = json.loads((profile / name).read_text(encoding='utf-8')).get('extensions', {}).get('settings', {})
            except (OSError, ValueError, AttributeError):
                continue
            for extension_id, entry in settings.items():
                if (not re.fullmatch('[a-p]{32}', extension_id) or not isinstance(entry, dict)
                        or not entry.get('path') or Path(entry['path']).resolve() != extension.resolve()):
                    continue
                # This may be the FIRST Chrome process for a dedicated profile.
                # Chrome ignores --load-extension on later launches that reuse it.
                # Load the bridge before opening its refresh page on a cold start.
                options = ([f'--profile-directory={profile_directory}'] if profile_directory
                           else [f'--load-extension={extension}', '--disable-background-mode'])
                # Chromium's DISABLE_UNSUPPORTED_DEVELOPER_EXTENSION (1 << 24).
                # Explain the required manual action; never change protected
                # browser settings or remove a disable reason in preferences.
                reasons = entry.get('disable_reasons') or []
                if isinstance(reasons, int):
                    developer_mode_off = bool(reasons & (1 << 24))
                else:
                    developer_mode_off = isinstance(reasons, list) and (1 << 24) in reasons
                if developer_mode_off:
                    subprocess.Popen([str(chrome), f'--user-data-dir={user_data_dir or profile}',
                                      *options, '--no-first-run', '--new-window', 'chrome://extensions/'],
                                     cwd=str(self.engine.root), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    raise HTTPException(409, 'Chrome 已停用 Flow Bridge：请在刚打开的扩展管理页开启右上角“开发者模式”，确认 Flow Bridge 已启用，再点击“检查浏览器验证（不生成）”。本次未提交生成。')
                self.extension_refreshed_profiles.add(profile_key)
                return f'chrome-extension://{extension_id}/refresh.html'

    def _account_cookie(self, account_id: str | None) -> str | None:
        if not account_id:
            return None
        path = self._cookie_files().get(account_id)
        if path is None:
            raise HTTPException(422, "所选账号的本地会话文件不存在")
        return str(path)

    async def _scheduled_cookie(self, data: JobInput | None = None, *, exclude_accounts: set[str] | None = None) -> str | None:
        """Choose the initial session from cached balances; never submit blindly.

        Cost estimates mirror the bundled engine's config.VideoCosts. They are
        eligibility hints, not a spending cap or a promise of upstream pricing.
        The engine performs its own live affordability check.
        """
        minimum = 1
        if data and data.kind == "video":
            if data.video_model.startswith('veo_3_1_'):
                # Tier is unknown: use the published non-Ultra estimate; allow
                # explicit account selection for a different verified tier.
                minimum = VEO_COSTS[data.video_model.removeprefix('veo_3_1_')]['non_ultra'] * data.count
            else:
                minimum = 40 if data.video_mode == "edit" else OMNI_COSTS[data.duration][data.quality] * data.count
        try:
            balance = await self.engine.get_balance()
            eligible = [a for a in self.accounts(balance)["accounts"]
                              if a["selectable"] and a["status"] == "active"
                              and isinstance(a["credits"], (int, float))
                              and a["credits"] >= minimum]
        except (OSError, sqlite3.Error):
            eligible = []
        if not eligible:
            raise HTTPException(422, "没有缓存积分足够的可用账号，请先刷新积分或手动指定账号。")
        from .account_scheduling import candidates, browser_identity_ready
        if data and (data.kind == 'video' or data.model == 'beluga'):
            eligible = [a for a in eligible if browser_identity_ready(self, a['id'])]
            if not eligible:
                raise HTTPException(422, '没有身份和专用浏览器关联完整的自动调度账号，请先在账号管理中同步；未提交生成。')
        eligible.sort(key=lambda a: a['id'])
        eligible = candidates(self, eligible, exclude_accounts)
        if not eligible:
            if exclude_accounts:
                return None
            raise HTTPException(422, "没有未暂停且积分足够的自动调度账号，请检查账号调度策略。")
        strategy = data.account_strategy if data else "round_robin"
        if strategy == "highest_credits":
            chosen = min(eligible, key=lambda a: (-a["credits"], a["id"]))
        elif strategy == "lowest_credits":
            chosen = min(eligible, key=lambda a: (a["credits"], a["id"]))
        else:
            chosen = eligible[self.account_cursor % len(eligible)]
            self.account_cursor += 1
        return self._account_cookie(chosen["id"])

    def _input_asset(self, image_id: str | None, suffixes: tuple[str, ...]) -> str | None:
        if not image_id:
            return None
        if image_id.startswith("upload:"):
            name = image_id[7:]
            if not name or Path(name).name != name:
                raise HTTPException(422, "参考素材 ID 无效")
            path = self.upload_dir / name
        elif image_id.startswith("media:"):
            name = image_id[6:]
            try:
                path = self.media_storage.resolve_id(name)
            except HTTPException:
                raise HTTPException(422, '参考素材不存在或所在磁盘未连接') from None
        else:
            raise HTTPException(422, "参考素材 ID 无效")
        directory = self.upload_dir if image_id.startswith("upload:") else path.parent
        if path.resolve().parent != directory.resolve() or path.suffix.lower() not in suffixes or not path.is_file():
            raise HTTPException(422, "参考素材不存在或格式不支持")
        return str(path.resolve())

    def _input_image(self, image_id: str | None) -> str | None:
        return self._input_asset(image_id, (".jpg", ".jpeg", ".png", ".webp"))

    def authorize_media_images(self, paths):
        """Record the owner's explicit assertion for exact image contents only."""
        entries = [("media_rights:" + hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    json.dumps({"confirmed_at": utc_now(), "scope": "image_sha256"}))
                   for path in paths if path]
        with self.connect() as db:
            db.executemany("insert or replace into console_meta values (?, ?)", entries)

    def media_rights_authorized(self, path) -> bool:
        key = "media_rights:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self.connect() as db:
            return db.execute("select 1 from console_meta where key=?", (key,)).fetchone() is not None

    def uploaded_image_key(self, account, project, path):
        return "uploaded_image:" + str(account) + ":" + str(project) + ":" + hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def known_uploaded_image(self, account, project, path):
        with self.connect() as db:
            row = db.execute("select value from console_meta where key=?",
                (self.uploaded_image_key(account, project, path),)).fetchone()
        return row['value'] if row else None

    def remember_uploaded_image(self, account, project, path):
        with self.connect() as db:
            db.execute("insert or replace into console_meta values (?, ?)",
                (self.uploaded_image_key(account, project, path), Path(path).name))

    def _input_video(self, video_id: str | None) -> str | None:
        return self._input_asset(video_id, (".mp4",))

    def _probe_video(self, path: str | Path) -> dict:
        """Check local MP4 container, video track and duration before upload."""
        path = Path(path)
        if path.stat().st_size > 100 * 1024 * 1024:
            raise HTTPException(413, "视频超过本后台 100 MB 限制")
        with path.open("rb") as stream:
            header = stream.read(12)
        if len(header) < 12 or header[4:8] != b"ftyp":
            raise HTTPException(422, "视频内容不是有效的 MP4 文件")
        try:
            return inspect_mp4(path)
        except (OSError, ValueError, TypeError, OverflowError):
            raise HTTPException(422, "无法读取视频，或视频超过 10 秒；请选择 10 秒以内的 MP4") from None

    def input_response(self, name: str) -> FileResponse:
        if not re.fullmatch(r"[0-9a-f]{32}\.(?:jpg|jpeg|png|webp|mp4)", name):
            raise HTTPException(404, "上传素材不存在")
        path = self.upload_dir / name
        if path.resolve().parent != self.upload_dir.resolve() or not path.is_file():
            raise HTTPException(404, "上传素材不存在")
        return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})

    def validate(self, data: JobInput):
        self._account_cookie(data.account_id)
        from .account_scheduling import require_generation_account
        require_generation_account(self, data.account_id)
        self._input_image(data.start_image_id)
        self._input_image(data.end_image_id)
        for reference in data.reference_image_ids:
            self._input_image(reference)
        if data.source_video_id:
            self._probe_video(self._input_video(data.source_video_id))

    def enqueue(self, data: JobInput, retry_of: str | None = None) -> dict:
        self.startup.require_ready()
        self.media_storage.migration.require_available()
        if self.preparation.running:
            raise HTTPException(409, "正在准备账号，请等待检查结束后提交生成任务。")
        self.validate(data)
        if data.media_rights_confirmed:
            self.authorize_media_images([self._input_image(asset) for asset in
                [data.start_image_id, data.end_image_id, *data.reference_image_ids] if asset])
        job_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute('begin immediate')
            if data.dispatch_quote_id:
                self.dispatch_quotes.check(data, db)
                changed = db.execute('''update console_dispatch_quotes set job_id=?
                    where id=? and job_id is null''', (job_id,data.dispatch_quote_id)).rowcount
                if changed != 1:
                    raise HTTPException(409, '报价已被消费；只查询原任务，不重发。')
            db.execute("""insert into console_jobs
                (id,kind,status,request_json,created_at,retry_of)
                values (?,?,?,?,?,?)""", (job_id, data.kind, "queued",
                                   data.model_dump_json(), utc_now(), retry_of))
        self.queue.put_nowait(job_id)
        from .work_log import emit
        emit(self, job_id, '任务已入队', {'stage': 'queued', 'kind': data.kind, 'model': data.model,
            'video_model': data.video_model, 'aspect': data.aspect, 'count': data.count,
            'duration': data.duration, 'quality': data.quality, 'account_strategy': data.account_strategy})
        self.scheduler.wakeup.set()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("select * from console_jobs where id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "任务不存在")
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
        return item

    def reconcile_official_download(self, job_id: str, raw: dict) -> dict:
        """Reconcile an already-billed image only after exact read-only binding.

        Internal recovery operation, not a generation/retry endpoint. The original
        failed job is retained verbatim inside recovery.original_job for audit.
        """
        before = self.get_job(job_id)
        original = (before.get('result') or {}).get('execution') or {}
        recovered = raw.get('execution') or {}
        account = before['request'].get('selected_account_id') or before['request'].get('account_id')
        if not (before['status'] == 'failed' and before['kind'] == 'image'
                and before['request'].get('model') == 'beluga'
                and (original.get('error_code') == 'result_identity_mismatch'
                     or (original.get('error_code') == 'original_result_unknown'
                         and original.get('original_error_code') == 'result_identity_mismatch'))
                and original.get('upload_project_id')
                and recovered.get('upload_project_id') == original.get('upload_project_id')
                and original.get('generation_requests') == 1
                and raw.get('status') == 'succeeded' and raw.get('account_id') == account
                and recovered.get('result_media_id') in original.get('response_result_ids', [])
                and recovered.get('result_identity_verified') and recovered.get('download_verified')
                and recovered.get('browser_closed') and recovered.get('recovered_without_generation')
                and recovered.get('check_only') and recovered.get('generation_requests') == 0
                and not recovered.get('generation_submitted')):
            raise HTTPException(409, '原任务与恢复下载的身份或生成记录不一致，未修改任务。')
        from .official_images import image_suffix
        files = []
        for item in raw.get('files') or []:
            path = Path(item.get('path') or '')
            if (path.is_symlink() or path.resolve().parent != self.engine.output_dir.resolve()
                    or not path.is_file()):
                raise HTTPException(409, '恢复文件未通过本地路径校验，未修改任务。')
            image_suffix(path.read_bytes())
            files.append({'name': path.name, 'url': self.media_storage.url(path.parent, path.name)})
        if len(files) != 1:
            raise HTTPException(409, '恢复原图数量不正确，未修改任务。')
        execution = {**original, 'stage': 'completed', 'download_verified': True,
                     'result_identity_verified': True, 'result_media_id': recovered['result_media_id'],
                     'result_detail_id': recovered['result_detail_id'], 'browser_closed': True,
                     'recovered_without_generation': True, 'outcome': 'recovered'}
        execution.pop('error_code', None)
        result = {'engine_job_id': raw.get('job_id'), 'status': 'succeeded', 'account_id': account,
                  'files': files, 'elapsed_seconds': raw.get('elapsed_seconds'), 'execution': execution,
                  'recovery': {'recovered_at': utc_now(), 'original_job': before,
                               'read_only_execution': recovered}}
        with self.connect() as db:
            db.execute('begin immediate')
            current = db.execute('select status, result_json from console_jobs where id=?', (job_id,)).fetchone()
            if (not current or current['status'] != before['status']
                    or json.loads(current['result_json']) != before['result']):
                raise HTTPException(409, '任务在恢复过程中已改变，未修改任务。')
            db.execute("update console_jobs set status='succeeded', result_json=?, error=null, finished_at=? where id=?",
                       (json.dumps(result), utc_now(), job_id))
        return self.get_job(job_id)

    def reconcile_official_video_download(self, job_id: str, raw: dict) -> dict:
        """Attach a verified existing video, preserving the failed job for audit.

        Internal local operation only. It does not enqueue, generate or retry.
        """
        before = self.get_job(job_id)
        original = (before.get('result') or {}).get('execution') or {}
        recovered = raw.get('execution') or {}
        account = before['request'].get('selected_account_id') or before['request'].get('account_id')
        ids = original.get('response_result_ids')
        verified = recovered.get('verified_results') or []
        if not (before['status'] == 'failed' and before['kind'] == 'video'
                and before['request'].get('video_model', 'omni_flash') in VIDEO_MODELS
                and (original.get('error_code') == 'video_result_identity_mismatch'
                     or (original.get('error_code') == 'original_result_unknown'
                         and original.get('original_error_code') == 'video_result_identity_mismatch'))
                and original.get('upload_project_id')
                and recovered.get('upload_project_id') == original.get('upload_project_id')
                and original.get('generation_requests') == 1
                and isinstance(ids, list) and len(ids) == before['request']['count'] and len(set(ids)) == len(ids)
                and raw.get('status') == 'succeeded' and raw.get('account_id') == account
                and raw.get('media_ids') == ids and recovered.get('response_result_ids') == ids
                and [item.get('generated_id') for item in verified] == ids
                and all(item.get('detail_id') == item.get('generated_id') for item in verified)
                and recovered.get('result_identity_verified') and recovered.get('download_verified')
                and recovered.get('browser_closed') and recovered.get('recovered_without_generation')
                and recovered.get('check_only') and recovered.get('generation_requests') == 0
                and not recovered.get('generation_submitted')):
            raise HTTPException(409, '原视频任务与只读下载的编号或生成记录不一致，未修改任务。')
        files = []
        for index, item in enumerate(raw.get('files') or []):
            path = Path(item.get('path') or '')
            if (path.is_symlink() or path.resolve().parent != self.engine.output_dir.resolve()
                    or not path.is_file() or path.suffix.lower() != '.mp4' or index >= len(verified)):
                raise HTTPException(409, '恢复视频未通过本地路径校验，未修改任务。')
            try:
                metadata = inspect_mp4(path, max_duration_seconds=None)
            except (ValueError, OSError):
                raise HTTPException(409, '恢复的视频不是完整 MP4，未修改任务。') from None
            if (metadata['size'] != verified[index].get('bytes')
                    or metadata['duration_seconds'] != verified[index].get('duration_seconds')):
                raise HTTPException(409, '恢复视频的文件信息不一致，未修改任务。')
            files.append({'name': path.name, 'url': self.media_storage.url(path.parent, path.name)})
        if len(files) != len(ids):
            raise HTTPException(409, '恢复视频数量不正确，未修改任务。')
        execution = {**original, 'stage': 'completed', 'download_verified': True,
                     'result_identity_verified': True, 'verified_results': verified,
                     'downloaded_count': len(files), 'browser_closed': True, 'recovered_without_generation': True,
                     'outcome': 'recovered'}
        execution.pop('error_code', None)
        result = {'engine_job_id': raw.get('job_id'), 'status': 'succeeded', 'account_id': account,
                  'files': files, 'elapsed_seconds': raw.get('elapsed_seconds'), 'execution': execution,
                  'recovery': {'recovered_at': utc_now(), 'original_job': before, 'read_only_execution': recovered}}
        with self.connect() as db:
            db.execute('begin immediate')
            current = db.execute('select status, result_json, request_json from console_jobs where id=?', (job_id,)).fetchone()
            if (not current or current['status'] != before['status']
                    or json.loads(current['result_json']) != before['result']
                    or json.loads(current['request_json']) != before['request']):
                raise HTTPException(409, '任务在恢复过程中已改变，未修改任务。')
            db.execute("update console_jobs set status='succeeded', result_json=?, error=null, finished_at=? where id=?",
                       (json.dumps(result), utc_now(), job_id))
        return self.get_job(job_id)

    def list_jobs(self) -> list[dict]:
        with self.connect() as db:
            ids = [r[0] for r in db.execute(
                "select id from console_jobs order by created_at desc, rowid desc limit 100")]
        return [self.get_job(job_id) for job_id in ids]

    def cancel(self, job_id: str) -> dict:
        with self.connect() as db:
            changed = db.execute("""update console_jobs set status='canceled',
                finished_at=? where id=? and status='queued'""", (utc_now(), job_id)).rowcount
        if not changed:
            raise HTTPException(409, "只能取消尚未开始的排队任务")
        self.scheduler.wakeup.set()
        return self.get_job(job_id)

    def retry(self, job_id: str, acknowledge: bool, auto_retry_limit=None, retry_uncertain=None, retry_switch_accounts=None) -> dict:
        if not acknowledge:
            raise HTTPException(422, "需要确认重试可能再次消耗积分")
        prior = self.get_job(job_id)
        if prior["status"] not in ("failed", "interrupted"):
            raise HTTPException(409, "仅失败或中断任务可重试")
        request = dict(prior['request'])
        if auto_retry_limit is not None:
            request['auto_retry_limit'] = auto_retry_limit
        if retry_uncertain is not None:
            request['retry_uncertain'] = retry_uncertain
        if retry_switch_accounts is not None:
            request['retry_switch_accounts'] = retry_switch_accounts
        try:
            data = JobInput.model_validate(request)
        except ValueError:
            raise HTTPException(422, '重试参数无效，请核对自动重试次数和重复扣费授权。') from None
        return self.enqueue(data, retry_of=job_id)

    async def run(self):
        await self.scheduler.run()

    def _finish_attempt(self, job_id, data, previous, result, success, error=None, exc=None):
        from .work_log import emit
        emit(self, job_id, '尝试成功' if success else '尝试失败或待核对', result.get('execution') or {}, exc)
        """Persist each attempt and requeue the same task within its original limit."""
        state = dict(previous)
        if data.auto_retry_limit and error:
            error = error.replace('不会自动重试。', '')
        attempts = state.get('attempts_started', 1)
        execution = result.get('execution') or {}
        history = list(state.get('history', []))
        history.append({'attempt': attempts, 'action': state.get('action', 'generate'),
                        'status': 'succeeded' if success else 'failed', 'error': error,
                        'finished_at': utc_now(), 'execution': execution,
                        'account_id': state.get('current_account_id')})
        state.update(history=history, attempts_completed=attempts, next_retry_at=None)
        status, finished = ('succeeded' if success else 'failed'), utc_now()
        if not success and data.auto_retry_limit:
            action, reason, target = retry_action(data, execution, exc, partial_files=bool(result.get('files')))
            # Once an original result is known, every further attempt is read-only.
            if state.get('action') == 'inspect':
                action, target = 'stop', None
                reason = '已只读查询原项目，结果仍无法安全关联；已停止，未重新生成。'
            elif state.get('recovery_target'):
                if action != 'stop':
                    action, target = 'recover', state['recovery_target']
                elif execution.get('recovered_without_generation') and not execution.get('uncertainty_query_completed'):
                    action, target = 'recover', state['recovery_target']
            if action in ('recover', 'inspect'):
                state['recovery_account_id'] = state.get('recovery_account_id') or state.get('current_account_id')
            if data.retry_switch_accounts:
                # Read-only queries do not spend another generation attempt.
                # Even the final permitted submission must be queried if unknown.
                budget_available = (state.get('queries_started', 0) < 3 if action in ('recover', 'inspect')
                                    else state.get('generation_attempts', attempts) <= data.auto_retry_limit)
                delay_index = (state.get('queries_started', 0) if action in ('recover', 'inspect')
                               else state.get('generation_attempts', attempts) - 1)
            else:
                budget_available, delay_index = attempts <= data.auto_retry_limit, attempts - 1
            if budget_available and action != 'stop':
                delay = RETRY_DELAYS[min(delay_index, len(RETRY_DELAYS) - 1)]
                state.update(action=action, recovery_target=target, stop_reason=None,
                    next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec='milliseconds'))
                status, finished = 'queued', None
                notice = (' 已安排只读查询原结果，不生成。' if action in ('recover', 'inspect') else
                          f' 已安排第 {state.get("generation_attempts", attempts)}/{data.auto_retry_limit} 次自动重试（{delay}秒后）。')
                error = (error or '').replace('不会自动重试。', '') + notice
            else:
                state['stop_reason'] = ('已达到自动重试上限。' if not budget_available else reason)
        if data.auto_retry_limit:
            state['limit'] = data.auto_retry_limit
            state['retries_started'] = max(0, state.get('generation_attempts', attempts) - 1) if data.retry_switch_accounts else max(0, attempts - 1)
            result['auto_retry'] = state
        with self.connect() as db:
            db.execute("""update console_jobs set status=?, result_json=?, error=?,
                finished_at=? where id=? and status='running'""",
                (status, json.dumps(result), error, finished, job_id))
        if status == 'queued':
            self.queue.put_nowait(job_id)
            self.scheduler.wakeup.set()

    async def execute_job(self, job_id, selected_cookie=None, selection_error=None):
        with self.connect() as db:
            changed = db.execute("""update console_jobs set status='running',
                started_at=? where id=? and status='queued'""",
                (utc_now(), job_id)).rowcount
        if not changed:
            return
        job = self.get_job(job_id)
        data = JobInput.model_validate(job["request"])
        from .work_log import emit
        emit(self, job_id, '开始执行', {'stage': 'start'})
        retry_state = dict((job.get('result') or {}).get('auto_retry') or {})
        retry_state['attempts_started'] = retry_state.get('attempts_completed', 0) + 1
        retry_state['next_retry_at'] = None
        if data.retry_switch_accounts:
            counter = 'queries_started' if retry_state.get('action') in ('recover', 'inspect') else 'generation_attempts'
            retry_state[counter] = retry_state.get(counter, 0) + 1
        retry_state['current_account_id'] = None
        if data.auto_retry_limit:
            current = dict(job.get('result') or {'files': []})
            current['auto_retry'] = {**retry_state, 'limit': data.auto_retry_limit,
                                    'retries_started': max(0, retry_state.get('generation_attempts', retry_state['attempts_started']) - 1)}
            with self.connect() as db:
                db.execute('update console_jobs set result_json=? where id=?', (json.dumps(current), job_id))
        try:
            if selection_error is not None:
                raise selection_error
            from .account_identity import canonical
            cookies = selected_cookie or (self._account_cookie(canonical(self, data.account_id)) if data.account_id else await self._scheduled_cookie(data))
            # Persist the chosen public ID before submission, including
            # failed runs, without exposing session paths or contents.
            selected_id = Path(cookies).stem
            emit(self, job_id, '已选择账号', {'account_id': selected_id, 'stage': data.account_strategy})
            if retry_state.get('action') not in ('recover', 'inspect'):
                from .account_scheduling import require_generation_account
                require_generation_account(self, selected_id)
                if data.dispatch_quote_id:
                    await self.dispatch_quotes.before_execute(data, job_id, selected_id)
            retry_state['current_account_id'] = selected_id
            used = list(retry_state.get('attempted_accounts', []))
            if selected_id not in used:
                used.append(selected_id)
            retry_state['attempted_accounts'] = used
            with self.connect() as db:
                request = json.loads(data.model_dump_json())
                request["selected_account_id"] = selected_id
                db.execute("update console_jobs set request_json=? where id=?",
                           (json.dumps(request), job_id))
            if retry_state.get('action') in ('recover', 'inspect'):
                runner = self.official_videos if data.kind == 'video' else self.official_images
                recovery_options = {'duration': data.duration, 'quality': data.quality,
                                    'video_model': data.video_model} if data.kind == 'video' else {}
                raw = await runner.run(cookies=cookies, prompt=data.prompt, aspect=data.aspect,
                    count=data.count, check_only=True, _recover=retry_state['recovery_target'], **recovery_options)
            elif data.kind == "image":
                raw = await self.engine.generate_image(
                    prompt=data.prompt, aspect=data.aspect, count=data.count,
                    model=data.model, cookies=cookies)
            elif data.video_mode == "edit":
                source = self._input_video(data.source_video_id)
                await asyncio.to_thread(self._probe_video, source)
                raw = await self.engine.edit_video(prompt=data.prompt, source=source, cookies=cookies)
            else:
                conditioning = {}
                if data.end_image_id:
                    conditioning["end_image"] = self._input_image(data.end_image_id)
                if data.reference_image_ids:
                    conditioning["references"] = [self._input_image(ref) for ref in data.reference_image_ids]
                if data.video_model != 'omni_flash':
                    conditioning['video_model'] = data.video_model
                raw = await self.engine.generate_video(
                    prompt=data.prompt, aspect=data.aspect,
                    duration=data.duration, quality=data.quality,
                    count=data.count, start_image=self._input_image(data.start_image_id),
                    cookies=cookies, **conditioning)
            files = []
            for entry in raw.get("files") or []:
                name = Path(entry.get("path") or "").name
                saved = self.engine.output_dir / name
                if name and saved.is_file() and saved.stat().st_size > 0:
                    files.append({"name": name, "url": self.media_storage.url(saved.parent, name)})
            result = {"engine_job_id": raw.get("job_id"), "status": raw.get("status"),
                      "account_id": raw.get("account_id"), "files": files,
                      "elapsed_seconds": raw.get("elapsed_seconds")}
            if isinstance(raw.get('execution'), dict):
                result['execution'] = raw['execution']
            # Native batch video generation/edit/reference commands
            # return "ready" after polling and downloading. Preserve
            # that native status in the result, while the console job
            # uses its existing "succeeded" state for a saved product.
            completed = raw.get("status") == "succeeded" or (
                data.kind == "video" and raw.get("status") == "ready")
            success = completed and bool(files)
            self._finish_attempt(job_id, data, retry_state, result, success,
                None if success else "引擎未返回已保存的成品，请检查历史和积分。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            execution = getattr(exc, 'execution', None) if isinstance(exc, OfficialImageError) else None
            partial_files = []
            if isinstance(exc, OfficialVideoError) and execution:
                for name in execution.get('downloaded_files', []):
                    if isinstance(name, str) and Path(name).name == name:
                        path = self.engine.output_dir / name
                        if path.is_file() and path.stat().st_size > 0:
                            partial_files.append({'name': name, 'url': self.media_storage.url(path.parent, name)})
            self._finish_attempt(job_id, data, retry_state,
                {'files': partial_files, 'execution': execution}, False, safe_failure(exc), exc)

    def record_balance_bindings(self):
        evidence = self.engine.last_balance_refresh
        if not evidence:
            return
        files = self._cookie_files()
        with self.connect() as db:
            for key, detail in evidence.get("details", {}).items():
                path = files.get(key)
                digest = evidence["digests"].get(key)
                if not path or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    continue
                db.execute("insert or replace into console_account_details values (?, ?, ?, ?, ?, ?, ?)",
                           (key, digest, detail["name"], detail["email"], detail["credits"], utc_now(), detail["error"]))
            for key, account_id in evidence["bindings"].items():
                path = files.get(key)
                digest = evidence["digests"].get(key)
                if not path or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    continue
                refreshed_at = utc_now() if account_id in evidence["refreshed"] else None
                db.execute("""insert into console_account_bindings values (?, ?, ?, ?)
                    on conflict(account_key) do update set account_id=excluded.account_id,
                    bundle_digest=excluded.bundle_digest, refreshed_at=excluded.refreshed_at""",
                    (key, account_id, digest, refreshed_at))

    @staticmethod
    def _complete_session(path: Path) -> bool:
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            return bool(bundle.get("cookies") and bundle.get("at")
                        and bundle.get("fsid") and bundle.get("fingerprint"))
        except (OSError, ValueError, AttributeError):
            return False

    def accounts(self, balance: dict) -> dict:
        files = self._cookie_files()
        from .account_identity import aliases
        account_aliases = aliases(self)
        db_accounts = balance.get("accounts", [])
        with self.connect() as db:
            bindings = {row["account_key"]: dict(row) for row in
                        db.execute("select * from console_account_bindings")}
            details = {row["account_key"]: dict(row) for row in db.execute("select * from console_account_details")}
        items = []
        seen = set()
        for key, path in sorted(files.items()):
            if account_aliases.get(key,key) != key:
                continue
            suffix = key.removeprefix("account_").lower()
            match = next((a for a in db_accounts
                          if a["account_id"].lower().removeprefix("acct-") == suffix), None)
            binding = bindings.get(key)
            verified = bool(binding and binding["bundle_digest"] ==
                            hashlib.sha256(path.read_bytes()).hexdigest())
            if verified:
                match = next((a for a in db_accounts if a["account_id"] == binding["account_id"]), None)
                # A failed check must not present an earlier balance as fresh.
                if not binding["refreshed_at"]:
                    match = None
            complete = self._complete_session(path)
            if not complete:
                match = None
            seen.add(binding["account_id"] if verified else match["account_id"] if match else "")
            profile_name = self._account_profile_name(key)
            display_name, email = self._profile_identity(profile_name)
            detail = details.get(key)
            if detail and detail["bundle_digest"] != hashlib.sha256(path.read_bytes()).hexdigest():
                detail = None
            if detail and detail["email"]:
                display_name, email = detail["name"] or detail["email"], detail["email"]
            credit = detail["credits"] if complete and detail else match["credits"] if match else None
            normal = self._normal_profile_for_email(email) if not profile_name else None
            items.append({"id": key, "label": display_name or ("待识别账号" if complete else "未完成连接的账号"),
                          "email": email,
                          "credits": credit,
                          "credits_checked_at": detail["checked_at"] if detail and credit is not None else None,
                          "read_error": detail["error"] if detail else None,
                          "status": "active" if detail and credit is not None else match["status"] if match else "unknown",
                          "session": "local_file" if complete else "incomplete", "selectable": complete,
                          "can_open_browser": bool(profile_name and (self.engine.root / "data" / profile_name).is_dir() or normal),
                          "session_saved_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
        # Unmatched database identities are history, not additional sessions.
        stale_balance_rows = [row for row in db_accounts
                              if row["account_id"] not in seen]
        if not files and not self.account_removal.records():
            for row in stale_balance_rows:
                items.append({"id": row["account_id"], "label": row["account_id"],
                              "email": None,
                              "credits": row["credits"], "status": row["status"],
                              "session": "missing", "selectable": False,
                              "can_open_browser": False,
                              "session_saved_at": None})
        from .account_scheduling import provider_blocks, snapshot as scheduling_policy
        cooldowns = provider_blocks(self)
        cooldowns = {account_aliases.get(key,key): value for key,value in cooldowns.items()}
        paused = {account_aliases.get(key,key) for key in scheduling_policy(self)['paused_accounts']}
        generation_blocks = set(cooldowns) | paused
        for account in items:
            account['generation_blocked'] = account['id'] in generation_blocks
            account['generation_block_reason'] = ('本地调度暂停' if account['id'] in paused else ('近期生成响应被拒绝，程序冷却至 ' + cooldowns[account['id']]['until'] + '；不代表账号被封禁。' if account['id'] in cooldowns else None))
            account["video_estimates"] = video_estimates(account["credits"])
        unread = sum(a["credits"] is None for a in items)
        known = [a["credits"] for a in items if a["credits"] is not None]
        with self.connect() as db:
            row = db.execute("select value from console_meta where key='last_successful_balance_refresh'").fetchone()
            error = db.execute("select value from console_meta where key='balance_refresh_error'").fetchone()
        return {"accounts": items, "video_credit_pricing": PRICING,
                "total_credits": sum(known) if known else None,
                "unread": unread, "stale_balance_rows": len(stale_balance_rows),
                "refresh_error": error[0] if error else None,
                "last_refresh_at": row[0] if row else None}

    def media(self, search: str = "", kind: str = "all") -> list[dict]:
        return self.media_library.filtered(self.media_library.catalog(), search, kind)[:200]

    def history(self, search: str = "", kind: str = "all", status: str = "all",
                limit: int = 50, offset: int = 0) -> dict:
        if kind not in ("all", "image", "video") or status not in (
            "all", "queued", "running", "succeeded", "failed", "empty"
        ) or not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "历史查询参数无效")
        if not self.engine.db_path.exists():
            return {"generations": [], "total": 0, "limit": limit, "offset": offset}
        clauses = []
        params: list[str | int] = []
        if search:
            clauses.append("(job_id like ? or account_id like ?)")
            pattern = f"%{search[:100]}%"
            params.extend([pattern, pattern])
        if kind != "all":
            clauses.append("kind=?")
            params.append(kind)
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        where = " where " + " and ".join(clauses) if clauses else ""
        with self.engine._connect() as db:
            total = db.execute("select count(*) from generations" + where, params).fetchone()[0]
            rows = db.execute("""select job_id, account_id, kind, model, aspect,
                status, credits_spent, elapsed_ms, created_at from generations"""
                + where + " order by id desc limit ? offset ?", [*params, limit, offset]).fetchall()
        fields = ("job_id", "account_id", "kind", "model", "aspect", "status",
                  "credits_spent", "elapsed_ms", "created_at")
        return {"generations": [dict(zip(fields, row)) for row in rows],
                "total": total, "limit": limit, "offset": offset}

    async def upload(self, file: UploadFile) -> dict:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(422, "仅支持 JPG、PNG、WebP 图片")
        data = await file.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, "图片超过 20 MB")
        signatures = {".jpg": data.startswith(b"\xff\xd8\xff"), ".jpeg": data.startswith(b"\xff\xd8\xff"),
                      ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                      ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
        if not signatures[suffix]:
            raise HTTPException(422, "图片内容与格式不符")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{suffix}"
        (self.upload_dir / name).write_bytes(data)
        return {"image_id": f"upload:{name}", "name": file.filename,
                "url": f"/console/api/inputs/{name}"}

    async def upload_source_video(self, file: UploadFile) -> dict:
        if Path(file.filename or "").suffix.lower() != ".mp4":
            raise HTTPException(422, "视频编辑目前仅接收 MP4 文件")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.mp4"
        pending = self.upload_dir / (name + ".uploading")
        target = self.upload_dir / name
        size = 0
        try:
            with pending.open("xb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise HTTPException(413, "视频超过本后台 100 MB 限制")
                    stream.write(chunk)
            metadata = await asyncio.to_thread(self._probe_video, pending)
            pending.rename(target)
        finally:
            pending.unlink(missing_ok=True)  # Only this invocation's unfinished upload.
        return {"video_id": f"upload:{name}", "name": file.filename,
                "url": f"/console/api/inputs/{name}", **metadata}


def create_console_router(service: ConsoleService) -> APIRouter:
    router = APIRouter(prefix="/console/api", tags=["console"])
    lan_key_store = LanKeyStore(service.engine.root)

    @router.get('/startup')
    async def startup_status():
        return service.startup.snapshot()

    @router.get('/accounts/manual-attention')
    async def manual_attention_list():
        from .account_scheduling import manual_attention
        return {'accounts': [{'account_id': key, **(value or {})} for key, value in manual_attention(service).items()]}

    @router.get('/accounts/verification/settings')
    async def verification_settings():
        from .verification_settings import snapshot
        return snapshot(service)

    @router.post('/accounts/verification/settings')
    async def configure_verification_settings(data: AccountMonitorInput):
        from .verification_settings import configure
        return configure(service, data.enabled)

    @router.get('/generation/settings')
    async def generation_settings():
        from .verification_settings import snapshot
        prefs = snapshot(service)
        return {'generation_browser_visible': prefs['generation_browser_visible'],
                'isolated_desktop': prefs['isolated_desktop']}

    @router.post('/generation/settings')
    async def configure_generation_settings(data: GenerationSettingsInput):
        from .verification_settings import configure_generation, snapshot
        if data.visible is None:
            return {'generation_browser_visible': snapshot(service)['generation_browser_visible'],
                    'isolated_desktop': snapshot(service)['isolated_desktop']}
        return configure_generation(service, data.visible, data.isolated)

    @router.get('/proxy')
    async def proxy_snapshot():
        from . import proxy_pool
        return proxy_pool.snapshot(service)

    @router.post('/proxy')
    async def proxy_action(data: ProxyActionInput):
        from . import proxy_pool
        if data.action == 'add':
            return proxy_pool.add(service, data.value or '')
        if data.action == 'remove':
            if not data.proxy_id:
                raise HTTPException(400, '缺少代理编号。')
            return proxy_pool.remove(service, data.proxy_id)
        if data.action == 'test':
            if not data.proxy_id:
                raise HTTPException(400, '缺少代理编号。')
            return await asyncio.to_thread(proxy_pool.test, service, data.proxy_id)
        if data.action == 'rebind':
            if not data.account_id:
                raise HTTPException(400, '缺少账号。')
            return proxy_pool.rebind(service, data.account_id, data.proxy_id)
        if data.action == 'unbind':
            if not data.account_id:
                raise HTTPException(400, '缺少账号。')
            return proxy_pool.unbind(service, data.account_id)
        raise HTTPException(400, '未知操作。')

    @router.get('/desktop/startup')
    async def desktop_startup_status():
        from .startup import status
        return status()

    @router.post('/desktop/startup')
    async def desktop_startup_settings(data: AccountMonitorInput):
        from .startup import configure
        try:
            return configure(data.enabled)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('/integration/document.md')
    async def integration_document():
        base = Path(sys._MEIPASS) / 'payload' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
        document = base / 'docs' / 'INTEGRATION_API.md'
        if not document.is_file():
            raise HTTPException(503, '随程序提供的接口文档缺失，请更新客户端。')
        return FileResponse(document, media_type='text/markdown', filename='FlowAgent_API.md', headers={'Cache-Control': 'no-store'})

    @router.get('/integration/mcp')
    async def mcp_integration():
        if getattr(sys, 'frozen', False):
            command = str(Path(sys.executable).parent / 'FlowConsoleMCP.exe')
            args = []
        else:
            command = sys.executable
            args = ['-u', str(Path(__file__).resolve().parents[1] / 'console_mcp_entry.py')]
        return {'mcpServers': {'flow-console': {'command': command, 'args': args,
            'env': {'FLOW_MCP_URL': 'http://127.0.0.1:8001', 'FLOW_MCP_READ_ONLY': '1'}}}}

    @router.get('/integration/mcp/{artifact}')
    async def mcp_artifact(artifact: str):
        base = Path(sys._MEIPASS) / 'payload' if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
        allowed = {'guide.md': (base / 'docs' / 'MCP_SKILL.md', 'text/markdown', 'Flow_MCP_Skill_接入说明.md'),
                   'bundle.zip': (base / 'docs' / 'Flow-MCP-Skill.zip', 'application/zip', 'Flow-MCP-Skill.zip')}
        if artifact not in allowed:
            raise HTTPException(404, '接入文件不存在')
        path, media, filename = allowed[artifact]
        if not path.is_file():
            raise HTTPException(503, '接入文件缺失，请升级客户端')
        return FileResponse(path, media_type=media, filename=filename, headers={'Cache-Control': 'no-store'})

    @router.get("/integration")
    async def integration():
        lan_enabled = os.environ.get("FLOW_AGENT_LAN_ENABLED") == "1"
        lan_ipv4 = None
        if lan_enabled:
            try:
                # UDP connect chooses the default adapter without sending data.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect(("192.0.2.1", 9))
                    lan_ipv4 = probe.getsockname()[0]
            except OSError:
                pass
        return {"lan_enabled": lan_enabled,
                "lan_port": int(os.environ.get("FLOW_AGENT_LAN_PORT", "8002")),
                "lan_ipv4": lan_ipv4}

    @router.get("/lan-key")
    async def lan_key_status():
        return lan_key_store.status()

    @router.post("/lan-key/copy")
    async def copy_lan_key():
        try:
            lan_key_store.copy_to_windows_clipboard()
        except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            raise HTTPException(503, "无法复制 API 密钥；请在本机打开密钥文件。") from exc
        return {"copied": True}

    @router.post("/lan-key/rotate")
    async def rotate_lan_key(data: RotateLanKeyInput):
        if not data.acknowledge_revoke:
            raise HTTPException(422, "重置后旧密钥立即失效，请先确认。")
        try:
            return lan_key_store.rotate()
        except OSError as exc:
            raise HTTPException(503, "无法写入新的 API 密钥；请检查本机文件权限。") from exc

    @router.get("/overview")
    async def overview():
        balance = await service.engine.get_balance()
        return {"accounts": service.accounts(balance), "tasks": service.list_jobs()[:8],
                "media_count": len(service.media())}

    @router.get("/accounts")
    async def accounts():
        return service.accounts(await service.engine.get_balance())

    @router.get('/accounts/removed')
    async def removed_accounts():
        return service.account_removal.snapshot()

    @router.post('/accounts/{account_id}/remove')
    async def remove_account(account_id: str, data: AccountRemovalInput):
        return service.account_removal.remove(account_id, data.confirmed)

    @router.post('/accounts/{account_id}/restore')
    async def restore_account(account_id: str, data: AccountRemovalInput):
        return service.account_removal.restore(account_id, data.confirmed)

    @router.get("/accounts/connection")
    async def account_connection():
        return service.bridge_status()

    @router.post("/accounts/connection")
    async def start_account_connection():
        return await service.start_bridge()

    @router.post("/accounts/connect")
    async def connect_account():
        return await service.connect_account()

    @router.get("/accounts/preparation")
    async def account_preparation():
        return service.preparation.snapshot()

    @router.post("/accounts/preparation/settings")
    async def configure_preparation_pages(data: AccountMonitorInput):
        return service.browser_pages.configure(data.enabled)

    @router.websocket("/accounts/preparation/page/{token}")
    async def preparation_page(socket: WebSocket, token: str):
        await service.browser_pages.channel(socket, token)

    @router.get("/accounts/monitor")
    async def account_monitor():
        return service.monitor.snapshot()

    @router.post("/accounts/monitor/settings")
    async def configure_account_monitor(data: AccountMonitorInput):
        return service.monitor.configure(data.enabled)

    @router.post("/accounts/monitor/check", status_code=202)
    async def check_accounts_in_background():
        return await service.monitor.start_check(force=True)

    @router.post("/accounts/prepare", status_code=202)
    async def prepare_account(data: PrepareAccountInput):
        return await service.preparation.start(data.account_id)

    @router.post("/accounts/prepare-all", status_code=202)
    async def prepare_all_accounts():
        return await service.preparation.start_all()

    @router.post("/accounts/preparation/stop")
    async def stop_account_preparation():
        return service.preparation.stop_after_current()

    @router.get("/accounts/local-profiles")
    async def local_profiles():
        return await asyncio.to_thread(service.local_profiles)

    @router.post("/accounts/local-profiles/open")
    async def open_local_profile(data: LocalProfileInput):
        return await service.open_local_profile(data.profile_id)

    @router.post("/accounts/extension-folder")
    async def extension_folder():
        return service.open_extension_folder()

    @router.post("/accounts/refresh")
    async def refresh_accounts():
        async with service.balance_refresh_lock:
            return await refresh_balance()

    async def refresh_balance():
        message = None
        try:
            balance = await service.engine.get_balance(refresh=True)
        except EngineError as exc:
            if exc.returncode == -1:
                message = "积分查询超过 45 秒，Flow 尚未返回余额。请检查代理网络，并在该账号的专用浏览器中重新打开 Flow 后重试。"
            else:
                message = safe_failure(exc)
        if message is None:
            service.record_balance_bindings()
            report = service.accounts(balance)
            current = [a for a in report["accounts"] if a["selectable"]]
            evidence = service.engine.last_balance_refresh
            verified = bool(evidence) and all(
                (evidence["bindings"].get(a["id"]) in evidence["refreshed"]
                 or evidence.get("details", {}).get(a["id"], {}).get("credits") is not None)
                and evidence["digests"].get(a["id"]) == hashlib.sha256(
                    service._cookie_files()[a["id"]].read_bytes()).hexdigest()
                for a in current)
            if not current or not verified or any(a["credits"] is None for a in current):
                reason = evidence.get("reason") if evidence else None
                message = ("Flow 尚未返回完整积分：查询网络失败，请检查系统代理连接后重试。" if reason == "network" else
                           "Flow 尚未返回完整积分：账号登录会话已失效，请打开该账号的 Flow 页面重新登录。" if reason == "session" else
                           "Flow 尚未返回完整积分；请在专用 Chrome 中打开 Flow 项目，等待账号会话同步完整后重试。")
        if message:
            with service.connect() as db:
                db.execute("insert or replace into console_meta values ('balance_refresh_error', ?)", (message,))
            raise HTTPException(502, message)
        with service.connect() as db:
            db.execute("insert or replace into console_meta values ('last_successful_balance_refresh', ?)", (utc_now(),))
            db.execute("delete from console_meta where key='balance_refresh_error'")
        return service.accounts(balance)

    @router.post("/accounts/{account_id}/open")
    async def open_account_browser(account_id: str):
        return await service.open_account_browser(account_id)

    @router.post('/accounts/{account_id}/browser-check')
    async def browser_verification_check(account_id: str):
        service.preparation.require_idle_queue()
        cookie = service._account_cookie(account_id)
        from .verification_settings import snapshot
        if snapshot(service)['headless']:
            try:
                result = await service.official_images.run(cookies=cookie, check_only=True, _identity_only=True)
                return {'browser_connected': True, 'page_ready': result.get('page_ready', False),
                    'token_minted': False, 'generation_submitted': False,
                    'message': '已通过无头浏览器核对账号身份，未提交生成；不代表生成风控已解除。',
                    'browser_cleanup': {'state': 'closed' if result['browser_closed'] else 'unconfirmed',
                                        'message': '无头浏览器检查结束，已释放浏览器。' if result['browser_closed'] else '浏览器退出未确认'},
                    'visibility': result.get('visibility')}
            except OfficialImageError as exc:
                raise HTTPException(409, safe_failure(exc)) from None
        try:
            out, _ = await service.engine._run('browser-check', '--cookies', cookie, timeout=120)
            result = extract_json(out)
        except EngineError as exc:
            raise HTTPException(409, safe_failure(exc)) from None
        # Deliberately fixed fields. No native raw response/session data in JS.
        if (not isinstance(result, dict) or result.get('generation_submitted') is not False
                or result.get('token_minted') is not False
                or result.get('browser_connected') is not True
                or result.get('verification_client_loaded') is not True
                or result.get('captcha_provider') != 'flow.captcha'):
            raise HTTPException(502, '浏览器检查未返回可确认的结果。')
        return {**{key: result.get(key) for key in ('browser_connected', 'verification_client_loaded',
                'captcha_provider', 'token_minted', 'generation_submitted')},
                'browser_cleanup': service.browser_execution.last_cleanup}

    @router.get('/scheduler')
    async def scheduler_status():
        return service.scheduler.snapshot()

    @router.post('/scheduler')
    async def configure_scheduler(data: SchedulerInput):
        return service.scheduler.configure(data)

    @router.get("/jobs")
    async def jobs():
        return service.list_jobs()

    @router.get('/browser-activity')
    async def browser_activity():
        reports = [*service.official_videos.active_reports.values(), *service.official_images.active_reports.values()]
        guard = getattr(service.browser_execution, 'active_visibility', None)
        first = reports[0] if reports else None
        visibility = first.get('visibility', {}) if first else guard.info() if guard else None
        return {'active': bool(reports or guard), 'stage': first.get('stage') if first else None,
                'visibility': visibility, 'activities': [
                    {key: report.get(key) for key in ('job_id', 'account_id', 'kind', 'stage', 'visibility')}
                    for report in reports]}

    @router.post('/accounts/{account_id}/image-page-check')
    async def image_page_check(account_id: str, data: ImagePageCheckInput = ImagePageCheckInput()):
        service.preparation.require_idle_queue()
        cookie = service._account_cookie(account_id)
        try:
            return await service.official_images.run(cookies=cookie, check_only=True, **data.model_dump())
        except OfficialImageError as exc:
            raise HTTPException(409, safe_failure(exc)) from None

    @router.post('/accounts/{account_id}/video-page-check')
    async def video_page_check(account_id: str, data: VideoPageCheckInput = VideoPageCheckInput()):
        service.preparation.require_idle_queue()
        cookie = service._account_cookie(account_id)
        try:
            return await service.official_videos.run(cookies=cookie, check_only=True,
                prompt='视频页面就绪检查（不提交）', **data.model_dump())
        except OfficialImageError as exc:
            raise HTTPException(409, safe_failure(exc)) from None

    @router.post("/jobs", status_code=202)
    async def submit(data: JobInput, x_flow_client: str | None = Header(default=None)):
        job = service.enqueue(data)
        from .work_log import emit
        emit(service, job['id'], '调用入口：' + ('MCP' if x_flow_client == 'mcp' else '客户端' if x_flow_client == 'desktop' else '本地HTTP接口'))
        return job

    @router.post('/dispatch-quotes')
    async def prepare_dispatch_quote(data: QuoteInput):
        return await service.dispatch_quotes.prepare(data)

    @router.get('/dispatch-quotes/{quote_id}')
    async def dispatch_quote(quote_id: str):
        return service.dispatch_quotes.get(quote_id)

    @router.get("/jobs/{job_id}")
    async def job(job_id: str):
        return service.get_job(job_id)

    @router.get('/jobs/{job_id}/work-log')
    async def work_log(job_id: str):
        from .work_log import read
        return read(service, job_id)

    @router.get('/jobs/{job_id}/work-log/export')
    async def export_work_log(job_id: str):
        from .work_log import export
        from fastapi.responses import Response
        return Response(export(service, job_id), media_type='application/zip',
            headers={'Content-Disposition': f'attachment; filename="flow-diagnostic-{job_id}.zip"'})

    @router.get('/jobs/{job_id}/work-log/screenshot')
    async def work_log_screenshot(job_id: str):
        from .work_log import folder
        service.get_job(job_id)
        path = folder(service, job_id)/'failure.png'
        if not path.is_file(): raise HTTPException(404, '此任务没有安全截图')
        return FileResponse(path, media_type='image/png')

    @router.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        return service.cancel(job_id)

    @router.post("/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str, data: RetryInput):
        return service.retry(job_id, data.acknowledge_possible_charge, data.auto_retry_limit, data.retry_uncertain, data.retry_switch_accounts)

    @router.get("/history")
    async def history(search: str = "", kind: str = "all", status: str = "all",
                      limit: int = 50, offset: int = 0):
        return service.history(search, kind, status, limit, offset)

    @router.get("/media")
    async def media(search: str = "", kind: str = "all"):
        return service.media(search, kind)

    @router.get('/media/library')
    async def media_library(search: str = '', kind: str = 'all', limit: int = 60, offset: int = 0):
        return service.media_library.snapshot(search, kind, limit, offset)

    @router.get('/media/storage')
    async def media_storage():
        return service.media_storage.snapshot()

    @router.post('/media/storage')
    async def save_media_storage(data: StorageInput):
        return service.media_storage.configure(data.directory)

    @router.post('/media/storage/choose')
    async def choose_media_storage():
        return await service.media_storage.choose_folder()

    @router.get('/media/storage/migration')
    async def migration_status():
        return service.media_storage.migration.snapshot()

    @router.post('/media/storage/migration/plan')
    async def migration_plan(data: StorageInput):
        return service.media_storage.migration.prepare(data.directory)

    @router.post('/media/storage/migration/start', status_code=202)
    async def start_migration(data: MigrationStart):
        return await service.media_storage.migration.start(data)

    @router.post('/media/storage/migration/resume', status_code=202)
    async def resume_migration():
        return await service.media_storage.migration.resume()

    @router.post('/media/storage/migration/abandon')
    async def abandon_migration():
        return service.media_storage.migration.abandon()

    @router.get('/media/index')
    async def media_index():
        from fastapi.responses import JSONResponse
        return JSONResponse({'schema_version': 1, 'generator': 'flow-agent-console',
                             'items': service.media_library.catalog()},
                            headers={'Content-Disposition': 'attachment; filename="flow-media-index.json"'})

    @router.post('/media/open-folder')
    async def open_media_folder():
        return service.media_library.open_folder()

    @router.post("/uploads/image")
    async def upload_image(file: UploadFile = File(...)):
        return await service.upload(file)

    @router.post("/uploads/video")
    async def upload_video(file: UploadFile = File(...)):
        return await service.upload_source_video(file)

    @router.get("/inputs/{name}")
    async def input_file(name: str):
        return service.input_response(name)

    return router
