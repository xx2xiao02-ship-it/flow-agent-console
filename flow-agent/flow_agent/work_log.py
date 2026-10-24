"""Durable allowlisted task diagnostics. Never serialize requests or exceptions."""
import io
import json
import re
import threading
import traceback
import uuid
import zipfile
from .build_info import VERSION
from datetime import datetime, timezone
from pathlib import Path

LOCK = threading.Lock()

RESPONSE_DIAGNOSTICS = frozenset(('response_read_exception_type', 'response_read_api'))
FIELDS = frozenset('kind model video_model aspect count duration quality account_strategy stage route outcome error_code original_error_code original_error_stage response_read_error exception_type account_id upload_project_id result_media_id result_detail_id generation_requests blocked_generation_requests generation_submitted browser_closed download_verified result_identity_verified response_body_read generation_response_http_status uncertainty_wait_seconds uncertainty_wait_elapsed page_ready check_only query_only existing_media_count correlatable_media_count'.split())

def safe(data):
    result = {}
    for key in FIELDS | RESPONSE_DIAGNOSTICS:
        value = data.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            if key in data: result[key] = value
        elif isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', value):
            result[key] = value
    ids = data.get('response_result_ids')
    if isinstance(ids, list):
        result['response_result_ids'] = [x for x in ids[:16] if isinstance(x, str) and re.fullmatch(r'[a-fA-F0-9-]{36}', x)]
    visibility = data.get('visibility')
    if isinstance(visibility, dict):
        result['visibility'] = {k: v for k, v in visibility.items()
            if k in ('hidden_confirmed', 'shown_for_user', 'waiting_for_user') and type(v) is bool}
        if visibility.get('mode') in ('headless', 'background_hidden', 'visible', 'isolated_desktop'):
            result['visibility']['mode'] = visibility['mode']
    summary = data.get('generation_response') or {}
    if isinstance(summary, dict):
        result['rpc_error_codes'] = [x for x in summary.get('rpc_error_codes', []) if type(x) is int][:8]
        from .provider_diagnostics import REASONS
        result['provider_reasons'] = sorted({x for row in summary.get('rpc_errors', []) if isinstance(row, dict)
            for x in row.get('reason_codes', []) if isinstance(x, str) and x in REASONS})
    return result

def folder(service, job_id):
    if str(uuid.UUID(job_id)) != job_id: raise ValueError('Invalid job ID')
    return service.engine.root / 'data' / 'work-logs' / job_id

def emit(service, job_id, event, data=None, exc=None):
    if not job_id: return
    try:
        path = folder(service, job_id)
        record = {'time': datetime.now(timezone.utc).isoformat(), 'version': VERSION, 'event': event, 'data': safe(data or {})}
        if exc is not None:
            record['stack'] = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                               for f in traceback.extract_tb(exc.__traceback__)[-12:]]
            record['exception_type'] = type(exc).__name__
        with LOCK:
            path.mkdir(parents=True, exist_ok=True)
            log = path / 'events.jsonl'
            # Bounded per-task disk usage; preserve the last terminal event.
            if log.exists() and log.stat().st_size > 1024 * 1024:
                rows = log.read_text('utf-8').splitlines()[-500:]
                log.write_text('\n'.join(rows) + '\n', 'utf-8')
            with log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                stream.flush()
    except Exception:
        pass  # Logging must not change task/billing behavior.

class Report(dict):
    def __init__(self, service, values):
        super().__init__(values)
        from .task_scheduler import CURRENT_JOB
        self.service, self.job_id = service, CURRENT_JOB.get()
        self.previous = None
        self.persist()
    def persist(self):
        try:
            value = safe(self)
            if value != self.previous:
                emit(self.service, self.job_id, '执行步骤', self)
                self.previous = value
        except Exception:
            pass
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.persist()
    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self.persist()

async def failure_snapshot(service, page, report):
    """Capture only an exact known error label, never a whole account page."""
    from .task_scheduler import CURRENT_JOB
    job_id = CURRENT_JOB.get()
    if not job_id or page is None: return
    try:
        for label in ('生成失败', '无法生成', '异常活动', 'Something went wrong', 'Generation failed'):
            locator = page.get_by_text(label, exact=True)
            if await locator.count() == 1 and await locator.is_visible():
                path = folder(service, job_id)
                path.mkdir(parents=True, exist_ok=True)
                await locator.screenshot(path=str(path/'failure.png'), timeout=3000)
                emit(service, job_id, '已保存错误提示截图', report)
                return
        emit(service, job_id, '未发现可安全截取的错误提示，已省略截图', report)
    except Exception:
        emit(service, job_id, '截图不可用，保留文字诊断', report)

def read(service, job_id):
    job = service.get_job(job_id)
    path = folder(service, job_id)
    rows = []
    if (path/'events.jsonl').is_file():
        for line in (path/'events.jsonl').read_text('utf-8').splitlines():
            try: rows.append(json.loads(line))
            except ValueError: pass
    if not rows:
        rows = [{'time': job['created_at'], 'event': '历史任务摘要（当时未记录过程日志）',
                 'data': safe((job.get('result') or {}).get('execution') or {})}]
    return {'job_id': job_id, 'status': job['status'], 'events': rows,
            'screenshot': (path/'failure.png').is_file(),
            'privacy': '不包含提示词、Cookie、Token、原始响应或异常消息；截图仅限明确错误提示区域。'}

def export(service, job_id):
    content = read(service, job_id)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('work-log.json', json.dumps(content, ensure_ascii=False, indent=2))
        if content['screenshot']: archive.write(folder(service, job_id)/'failure.png', 'failure.png')
    return stream.getvalue()
