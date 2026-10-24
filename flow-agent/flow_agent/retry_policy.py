"""Bounded queue retries. Uncertain submissions never switch accounts."""
from __future__ import annotations
import re

RETRY_DELAYS = (10, 30, 60)  # Seconds after the first, second and third failure.

# These require changed inputs or human intervention; repeating cannot fix them.
STOP_CODES = frozenset({
    'flow_access_denied', 'manual_timeout', 'identity_not_ready', 'identity_missing',
    'identity_mismatch', 'project_missing', 'profile_missing', 'browser_missing',
    'runtime_missing', 'browser_driver_missing', 'browser_driver_failed', 'unsupported_options', 'unsupported_video_options',
    'model_not_selected', 'video_model_not_selected', 'video_model_unavailable', 'parameters_unconfirmed',
    'video_parameters_unconfirmed', 'prompt_unconfirmed', 'video_input_missing',
    'video_upload_rights_required', 'video_upload_rights_confirmation_failed',
    'provider_rejected', 'video_rpc_error',
})


def retry_action(data, execution, exc=None, *, partial_files=False):
    """Return (generate|recover|stop, public reason, optional read-only target).

    Submitted results with known IDs are only recovered, never regenerated.
    Unknown outcomes may generate again only with explicit per-job consent.
    """
    from fastapi import HTTPException
    from .engine import EngineError
    from .image_models import ImageModelError

    if partial_files:
        return 'stop', '已保存部分成品，请核对后手动处理，避免重复生成。', None
    if isinstance(exc, (HTTPException, ImageModelError)):
        return 'stop', '账号、素材或参数需要处理，已停止自动重试。', None
    execution = execution or {}
    code = execution.get('error_code') or getattr(exc, 'code', None)
    if execution.get('uncertainty_query_completed'):
        return 'stop', '已隐藏查询180秒，结果仍不明，待核对；未重复提交。', None
    # Opt-in account rotation must never honor legacy duplicate-charge consent.
    if getattr(data, 'retry_switch_accounts', False):
        if execution.get('recovered_without_generation'):
            return 'stop', '已只读查询原项目，结果仍无法确认；已停止，未重新生成。', None
        ids = execution.get('response_result_ids')
        if execution.get('route') == 'official_page' and ids:
            if data.kind == 'video' and len(ids) == data.count:
                return 'recover', '只查询并下载原结果，不换号生成。', {'media_ids': ids, 'project_id': execution.get('upload_project_id')}
            if data.kind == 'image' and execution.get('result_media_id') and execution.get('result_detail_id'):
                return 'recover', '只查询并下载原结果，不换号生成。', {'media_ids': [execution['result_media_id']], 'detail_id': execution['result_detail_id'], 'project_id': execution.get('upload_project_id')}
        submitted = (execution.get('generation_submitted') is True
                     or execution.get('generation_requests', 0) > 0 or getattr(exc, 'submitted', False))
        if submitted and execution.get('route') == 'official_page':
            project = execution.get('upload_project_id')
            if not isinstance(project, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', project):
                return 'stop', '缺少原项目标识，不能安全查询；未重新生成。', None
            return 'inspect', '结果不明，先只读查询原账号原项目，不重新生成。', {'inspect_only': True, 'project_id': project}
    if code in STOP_CODES:
        return 'stop', '需要处理账号、参数或人工验证，已停止自动重试。', None
    if isinstance(exc, EngineError):
        words = (exc.stdout + ' ' + exc.stderr).lower()
        if exc.returncode == 127 or any(word in words for word in (
            'public_error_', 'unauthorized', 'insufficient', 'rate limit',
            'invalid model', 'unknown model', 'unsupported model', 'browser_',
            'no account', 'no cookies', 'generation_replay_blocked',
        )):
            return 'stop', '引擎或平台要求处理账号、权限、配额或验证，已停止自动重试。', None
        if re.search(r'(?:re)?captcha[^\r\n]{0,100}(?:fail|timed?\s*out|unavailable|reject|denied|missing)', words):
            return 'stop', '人工验证尚未通过，已停止自动重试。', None
    # Recover only exact IDs from the original response. Never guess from gallery.
    ids = execution.get('response_result_ids')
    if execution.get('route') == 'official_page' and ids:
        if data.kind == 'video' and len(ids) == data.count:
            return 'recover', '先查询并下载原生成结果，不重新生成。', {'media_ids': ids}
        if data.kind == 'image' and execution.get('result_media_id') and execution.get('result_detail_id'):
            return 'recover', '先查询并下载原生成结果，不重新生成。', {
                'media_ids': [execution['result_media_id']], 'detail_id': execution['result_detail_id']}
    if execution.get('recovered_without_generation'):
        return 'stop', '原结果恢复未完成，已停止重新生成以免重复扣费。', None
    # Only an explicit official report establishes that no request was submitted.
    unsubmitted = (execution.get('route') == 'official_page'
                   and execution.get('generation_submitted') is False
                   and execution.get('generation_requests', 0) == 0
                   and not getattr(exc, 'submitted', False))
    if unsubmitted:
        return 'generate', ('本次未提交生成，稍后换一个未尝试账号重试。' if getattr(data, 'retry_switch_accounts', False) else '本次未提交生成，稍后重试。'), None
    if data.retry_uncertain and not getattr(data, 'retry_switch_accounts', False):
        return 'generate', '已允许在结果不确定时重新生成，可能重复消耗积分。', None
    return 'stop', '结果可能已提交；未授权重复扣费，已停止自动重试。', None
