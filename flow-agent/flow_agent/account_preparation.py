"""Local account setup orchestration. Never submits generation or CAPTCHA calls."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException

from .account_details import FlowAccountReader

if TYPE_CHECKING:
    from .console import ConsoleService


class AccountPreparation:
    def __init__(self, service: ConsoleService):
        self.service = service
        self.task: asyncio.Task | None = None
        self.synced: set[str] = set()
        self.wait_seconds = 90
        self.settle_seconds = 1
        self.start_lock = asyncio.Lock()
        self.report: dict = {"state": "idle", "account_id": None, "steps": []}
        self.batch: dict | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def snapshot(self) -> dict:
        report = copy.deepcopy(self.report)
        report["auto_close_pages"] = self.service.browser_pages.enabled()
        with self.service.connect() as db:
            row = db.execute("select value from console_meta where key='last_browser_execution_cleanup'").fetchone()
        if row:
            try:
                report['last_execution_cleanup'] = json.loads(row[0])
            except (TypeError, ValueError):
                pass
        report["scope"] = "all" if self.batch else "single"
        if self.batch:
            batch = copy.deepcopy(self.batch)
            for account in batch["accounts"]:
                if account["state"] == "running" and account["account_id"] == report["account_id"]:
                    account.update(report)
            report.update(state=batch["state"], message=batch["message"], batch=batch)
        return report

    def observe_bridge_line(self, line: str):
        # Parse fixed evidence only. Never persist or return the raw diagnostics.
        match = re.search(r"bridge: synced .* to .*[/\\](account_[a-zA-Z0-9]+)\.json ", line)
        if match and "page tokens true, fingerprint true" in line:
            self.synced.add(match.group(1))

    def require_idle_queue(self, *, allow_migration=False):
        if not allow_migration:
            self.service.media_storage.migration.require_available()
        if (self.service.browser_execution.lock.locked()
                or self.service.browser_execution.handoff_active):
            raise HTTPException(409, '浏览器验证连接正在使用，请等待当前操作结束。')
        with self.service.connect() as db:
            active = db.execute("select 1 from console_jobs where status in ('queued','running') limit 1").fetchone()
        if active:
            raise HTTPException(409, "有生成任务进行中，请等待任务结束后再准备账号。")

    async def start(self, account_id: str | None) -> dict:
        async with self.start_lock:
            return await self._start(account_id)

    async def _start(self, account_id: str | None) -> dict:
        from .account_identity import canonical
        if account_id:
            account_id = canonical(self.service, account_id)
        if self.running:
            if self.batch or account_id and account_id != self.report["account_id"]:
                raise HTTPException(409, "正在准备另一个账号，请等待检查结束。")
            return self.snapshot()
        self.require_idle_queue()
        if not account_id:
            accounts = self.service.accounts(await self.service.engine.get_balance())["accounts"]
            candidates = [a for a in accounts if a["can_open_browser"] and a["selectable"]]
            if not candidates:
                raise HTTPException(409, "没有可直接打开的账号，请先到账号管理连接 Flow 账号。")
            account_id = candidates[0]["id"]
        self.require_idle_queue()
        self.service._account_cookie(account_id)
        self.batch = None
        self.report = self._new_report(account_id)
        self.task = asyncio.create_task(self._run(account_id), name="flow-account-prepare")
        return self.snapshot()

    @staticmethod
    def _new_report(account_id: str | None) -> dict:
        return {
            "state": "running", "account_id": account_id, "checked_at": None,
            "message": "正在自动准备账号，无需分别打开页面和刷新积分。",
            "generation_verified": False,
            "steps": [{"id": key, "label": label, "state": "pending"} for key, label in (
                ("browser", "打开对应账号的 Flow"), ("extension", "确认扩展与会话同步"),
                ("identity", "验证账号身份"), ("credits", "读取最新积分"))],
        }

    async def start_all(self) -> dict:
        async with self.start_lock:
            if self.running:
                if self.batch:
                    return self.snapshot()
                raise HTTPException(409, "正在准备所选账号，请等待检查结束后再准备全部账号。")
            self.require_idle_queue()
            accounts = self.service.accounts(await self.service.engine.get_balance())["accounts"]
            self.require_idle_queue()
            if not any(a["can_open_browser"] for a in accounts):
                raise HTTPException(409, "没有可直接打开的账号，请先到账号管理连接 Flow 账号。")
            self.batch = {
                "state": "running", "message": "正在按顺序检查全部账号，不会同时切换多个账号。",
                "stop_requested": False,
                "accounts": [{"account_id": a["id"], "label": a["label"], "email": a.get("email"),
                              "state": "queued" if a["can_open_browser"] else "skipped",
                              "steps": [], "credits": None, "checked_at": None,
                              "message": "等待检查" if a["can_open_browser"] else "未检查：缺少可复用浏览器，请先完成账号连接。"}
                             for a in accounts],
            }
            self.report = self._new_report(None)
            self._batch_counts()
            self.task = asyncio.create_task(self._run_all(), name="flow-all-accounts-prepare")
            return self.snapshot()

    def stop_after_current(self) -> dict:
        if self.batch and self.running:
            self.batch.update(stop_requested=True, message="将在当前账号检查结束后停止，后续账号不会打开。")
        return self.snapshot()

    def _batch_counts(self):
        accounts = self.batch["accounts"]
        counts = {state: sum(a["state"] == state for a in accounts)
                  for state in ("checked", "needs_attention", "skipped")}
        self.batch.update(total=len(accounts), completed=sum(counts.values()),
                          succeeded=counts["checked"], attention=counts["needs_attention"], skipped=counts["skipped"])

    async def _run_all(self):
        from .console import utc_now
        for account in self.batch["accounts"]:
            if account["state"] != "queued":
                continue
            if self.batch["stop_requested"]:
                account.update(state="skipped", message="未检查：已停止后续检查。")
                self._batch_counts()
                continue
            self.report = self._new_report(account["account_id"])
            account["state"] = "running"
            await self._run(account["account_id"])
            account.update(copy.deepcopy(self.report))
            self._batch_counts()
        self.batch.update(state="needs_attention" if self.batch["attention"] or self.batch["skipped"] else "checked",
                          checked_at=utc_now(),
                          message=f"批量检查结束：{self.batch['succeeded']} 个通过，{self.batch['attention']} 个需处理，{self.batch['skipped']} 个未检查。")

    def step(self, key: str, state: str):
        next(item for item in self.report["steps"] if item["id"] == key)["state"] = state

    async def _read_account(self, raw: bytes) -> dict:
        from . import proxy_pool
        account = (self.report or {}).get('account_id')
        proxy = (proxy_pool.raw_url_for(self.service, account)
                 or self.service.engine._process_env().get("FLOW_PROXY"))
        async with httpx.AsyncClient(proxy=proxy, timeout=8, follow_redirects=False) as client:
            return await FlowAccountReader(client, json.loads(raw)).read()

    async def _run(self, account_id: str):
        from .console import utc_now
        from .verification_settings import snapshot
        if snapshot(self.service)['headless']:
            return await self._run_headless(account_id, repair_login=True)
        current = "browser"
        lease = None
        try:
            async with self.service.balance_refresh_lock:
                old = self.service._verified_detail(account_id)
                _, profile_email = self.service._profile_identity(self.service._account_profile_name(account_id))
                expected_email = (old or {}).get("email") or profile_email
                self.step(current, "running")
                # Ordinary 'open Flow' buttons never receive a close lease.
                # The same explicit ownership applies to both dedicated and
                # normal Chrome launches, without closing any pre-existing tab.
                lease = self.service.browser_pages.begin(account_id)
                await self.service.open_account_browser(account_id)
                self.step(current, "passed")
                current = "extension"
                self.step(current, "running")
                # A reconnect causes the real engine to pull the current page's
                # session. Only our owned bridge may be restarted, and never
                # while a task is running. An open port alone is not success.
                await self.service.restart_idle_bridge()
                deadline = asyncio.get_running_loop().time() + self.wait_seconds
                while account_id not in self.synced:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise HTTPException(409, "尚未收到此账号的扩展同步。请在已打开的窗口完成登录或人工验证，并进入 Flow 项目，再点一次“一键准备账号”。")
                    await asyncio.sleep(0.5)
                path = self.service._cookie_files().get(account_id)
                if not path or not self.service._complete_session(path):
                    raise HTTPException(409, "账号会话仍不完整，请在打开的 Flow 窗口完成登录并进入项目。")
                self.step(current, "passed")
                current = "identity"
                self.step(current, "running")
                # The engine may finish its initial project discovery after its
                # first sync. Re-read automatically if that updates the bundle;
                # never associate an old response with a newer session snapshot.
                for _ in range(3):
                    await asyncio.sleep(self.settle_seconds)
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    result = await self._read_account(raw)
                    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                        break
                else:
                    raise HTTPException(409, "检查期间账号会话发生变化，请重新准备账号。")
                email = result.get("email")
                if not email or expected_email and email.casefold() != expected_email.casefold():
                    raise HTTPException(409, "未能确认是所选账号，请在 Flow 窗口核对登录账号后重新准备。")
                self.step(current, "passed")
                current = "credits"
                self.step(current, "running")
                checked = utc_now()
                with self.service.connect() as db:
                    db.execute("insert or replace into console_account_details values (?, ?, ?, ?, ?, ?, ?)",
                               (account_id, digest, result.get("name"), email, result.get("credits"), checked, result.get("error")))
                if result.get("credits") is None:
                    messages = {"network": "积分查询网络失败，请检查网络后重新准备。",
                                "session": "Flow 登录已失效，请在打开的窗口重新登录。",
                                "refused": "Flow 拒绝读取积分，请在打开的窗口检查是否需要人工验证。"}
                    raise HTTPException(409, messages.get(result.get("error"), "Flow 未返回可识别积分，请检查页面后重新准备。"))
                self.step(current, "passed")
                self.report.update(state="checked", checked_at=checked, credits=result["credits"],
                                   message="账号检查通过，最新积分已读取。可返回提交任务；Google 仍会独立审核每次生成请求。")
        except asyncio.CancelledError:
            self.report.update(state="needs_attention", message="账号准备已停止，浏览器页面已保留。")
            raise
        except HTTPException as exc:
            self.step(current, "blocked")
            self.report.update(state="needs_attention", message=str(exc.detail))
        except Exception:
            self.step(current, "blocked")
            self.report.update(state="needs_attention", message="本机账号准备失败，请检查 Flow 窗口后重试。")
        finally:
            checked = self.report["state"] == "checked"
            message = self.report.get("message")
            if checked and lease:
                self.report.update(state="running", message="账号检查通过，正在确认自动关闭本次页面。")
            cleanup = await self.service.browser_pages.finish(lease, checked)
            if checked:
                self.report.update(state="checked", message=message)
            self.report["browser_cleanup"] = cleanup

    async def _run_headless(self, account_id, *, repair_login=False):
        from .console import utc_now, safe_failure
        from .official_images import OfficialImageError
        current = 'browser'
        browser_report = None
        fresh_result = None
        fresh_bundle = None
        initial_path = self.service._cookie_files()[account_id]
        initial_digest = hashlib.sha256(initial_path.read_bytes()).hexdigest()
        async def read_fresh(bundle):
            nonlocal fresh_result, fresh_bundle
            fresh_result = await self._read_account(json.dumps(bundle).encode('utf-8'))
            fresh_bundle = bundle
        try:
            self.step(current, 'running')
            from .account_repair import ensure_profile
            self.service.official_images.last_report = {}
            self.report['repair_stage'] = 'browser_binding'
            self.report['message'] = ensure_profile(self.service, account_id)
            self.report['repair_stage'] = 'headless_sync'
            cookie = self.service._account_cookie(account_id)
            try:
                browser_report = await self.service.official_images.run(
                    cookies=cookie, check_only=True, _preparation=True, _account_reader=read_fresh)
            except OfficialImageError as exc:
                # A 401 alone is insufficient evidence to open a window. The
                # real page must explicitly require login or human verification.
                if not repair_login or exc.code != 'headless_needs_login':
                    raise
                if not self.service.official_images.last_report.get('browser_closed'):
                    raise
                self.report['login_confirmed_by_page'] = True
                self.report['repair_stage'] = 'waiting_for_login'
                self.report.update(message='检测到需要登录或人工验证，正在显示对应账号窗口；完成后自动同步并关闭。')
                browser_report = await self.service.official_images.run(
                    cookies=cookie, check_only=True, _preparation=True,
                    _account_reader=read_fresh, _repair_visible=True)
            if not browser_report.get('browser_closed'):
                raise HTTPException(409, '无头浏览器退出未确认，请检查后再继续。')
            self.step(current, 'passed')
            current = 'extension'
            self.step(current, 'passed')
            current = 'identity'
            self.report['repair_stage'] = 'identity_and_credits'
            self.step(current, 'running')
            async with self.service.balance_refresh_lock:
                path = self.service._cookie_files()[account_id]
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                expected = self.service._verified_detail(account_id)
                result = fresh_result or await self._read_account(raw)
                if digest != initial_digest:
                    raise HTTPException(409, '会话已变化，请重新检测。')
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise HTTPException(409, '会话已变化，请重新检测。')
                email = result.get('email')
                if not email:
                    reason = result.get('error')
                    messages = {'network': '账号接口网络连接失败，请检查网络后重试。',
                                'session': '浏览器会话仍被身份接口判为失效，请手动打开 Flow 重新登录。',
                                'refused': '账号身份接口拒绝访问，请手动打开 Flow 检查。'}
                    raise HTTPException(409, messages.get(reason, '身份接口未返回可核对的邮箱，请手动打开 Flow 检查。'))
                if not expected or email.casefold() != expected.get('email', '').casefold():
                    raise HTTPException(409, '需要本人重新登录或同步身份，请手动打开 Flow；无头浏览器已关闭。')
                if fresh_bundle is not None:
                    updated = json.dumps(fresh_bundle, ensure_ascii=False).encode('utf-8')
                    temp = path.with_name(path.name + '.sync.tmp')
                    try:
                        temp.write_bytes(updated)
                        temp.replace(path)
                    finally:
                        temp.unlink(missing_ok=True)
                    digest = hashlib.sha256(updated).hexdigest()
                self.step(current, 'passed')
                current = 'credits'
                self.step(current, 'running')
                checked = utc_now()
                with self.service.connect() as db:
                    db.execute('insert or replace into console_account_details values (?, ?, ?, ?, ?, ?, ?)',
                        (account_id, digest, result.get('name'), email, result.get('credits'), checked, result.get('error')))
                if result.get('credits') is None:
                    raise HTTPException(409, '未读到积分，请检查账号登录或网络；无头浏览器已关闭。')
                self.step(current, 'passed')
                self.report.update(state='checked', checked_at=checked, credits=result['credits'],
                    repair_stage='completed', message='自动修复完成，身份和积分已核对，专用浏览器已关闭。')
        except asyncio.CancelledError:
            self.report.update(state='needs_attention', message='检查已取消，本次无头浏览器已执行退出清理。')
            raise
        except (OfficialImageError, HTTPException) as exc:
            self.step(current, 'blocked')
            self.report.update(state='needs_attention', message=str(exc.detail) if isinstance(exc, HTTPException) else safe_failure(exc))
            if isinstance(exc, OfficialImageError) and exc.code == 'profile_busy':
                self.service.monitor.waiting_profiles.add(account_id)
                self.report.update(state='pending', message='当前账号窗口正在使用，已安排自动继续；窗口关闭后自动同步身份和积分，无需再次点击。')
        except Exception:
            self.step(current, 'blocked')
            self.report.update(state='needs_attention', message='无头检查失败；请检查网络或手动打开 Flow 完成登录。')
        finally:
            browser_report = browser_report or self.service.official_images.last_report
            if repair_login and account_id not in self.service.monitor.waiting_profiles and initial_path.is_file():
                self.service.monitor.repair_attempts[account_id] = hashlib.sha256(initial_path.read_bytes()).hexdigest()
            self.report['checked_at'] = utc_now()
            self.report['generation_requests'] = browser_report.get('generation_requests',0)
            self.report['browser_cleanup'] = {'state': 'closed' if browser_report.get('browser_closed') else 'unconfirmed',
                'message': '本次无头浏览器已关闭。' if browser_report.get('browser_closed') else '未启动浏览器或退出未确认，请查看检查提示。'}
            with self.service.connect() as db:
                db.execute('insert or replace into console_meta values (?,?)',
                    ('account_repair:'+account_id,json.dumps(self.report,ensure_ascii=False)))

    async def stop(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
