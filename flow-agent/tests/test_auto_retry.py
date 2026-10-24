"""Offline retries through the real queue, database and HTTP contracts. No Google calls."""
import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine, EngineError
from flow_agent.lan_api import create_lan_app
from flow_agent.official_videos import OfficialVideoError
from flow_agent.task_scheduler import SchedulerInput


def page_error(code='video_page_failed', submitted=False, **extra):
    error = OfficialVideoError(code, submitted=submitted)
    error.execution = dict(route='official_page', kind='video', error_code=code,
        generation_submitted=submitted, generation_requests=int(submitted), browser_closed=True, **extra)
    return error


class RetryEngine(FlowEngine):
    def __init__(self, root):
        super().__init__(root=root)
        self.calls = []
        self.failures = 0
        self.failure = page_error()

    async def generate_video(self, **data):
        self.calls.append(data)
        if len(self.calls) <= self.failures:
            raise self.failure
        path = self.output_dir / f'local-{len(self.calls)}.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'LOCAL FIXTURE ONLY')
        return {'status': 'succeeded', 'files': [{'path': str(path)}]}


@pytest.fixture
def service(tmp_path, monkeypatch):
    import flow_agent.console as console
    monkeypatch.setattr(console, 'RETRY_DELAYS', (.03, .04, .05))
    s = ConsoleService(RetryEngine(tmp_path / 'flow-agent'))
    s.initialize()
    s.cookies_dir.mkdir()
    for a in ('a', 'b'):
        (s.cookies_dir / f'account_{a}.json').write_text(json.dumps({
            'cookies': [{'name': 'fixture', 'value': 'PRIVATE'}], 'at': 'PRIVATE',
            'fsid': 'PRIVATE', 'fingerprint': {'userAgent': 'fixture'}}))
    return s


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.005)


def queue(s, **options):
    return s.enqueue(JobInput(kind='video', prompt='local fixture', account_id='account_a', **options))['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('failures,expected_calls,status', [(0, 1, 'succeeded'), (2, 3, 'succeeded'), (99, 4, 'failed')])
async def test_bounded_retry_success_stop_and_history(service, failures, expected_calls, status):
    s = service
    s.engine.failures = failures
    job = queue(s, auto_retry_limit=3)
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] in ('failed', 'succeeded'))
        result = s.get_job(job)
        assert result['status'] == status
        assert len(s.engine.calls) == expected_calls
        state = result['result']['auto_retry']
        assert len(state['history']) == expected_calls
        assert state['retries_started'] == expected_calls - 1
        assert len(s.list_jobs()) == 1
        assert {Path(c['cookies']).name for c in s.engine.calls} == {'account_a.json'}
        assert all(h['execution'].get('browser_closed') for h in state['history'] if h['status'] == 'failed')
        assert 'PRIVATE' not in json.dumps(result)
    finally:
        await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain,expected', [(False, 1), (True, 4)])
async def test_submitted_mismatch_requires_explicit_consent(service, uncertain, expected):
    s = service
    s.engine.failures = 99
    s.engine.failure = page_error('video_result_count_mismatch', submitted=True)
    job = queue(s, auto_retry_limit=3, retry_uncertain=uncertain)
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert len(s.engine.calls) == expected
        assert s.get_job(job)['result']['auto_retry']['attempts_started'] == expected
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_retry_off_old_job_and_restart_never_replay(service):
    s = service
    s.engine.failures = 99
    job = queue(s)
    s.worker = asyncio.create_task(s.run())
    await until(lambda: s.get_job(job)['status'] == 'failed')
    await s.stop()
    queued = queue(s, auto_retry_limit=3, retry_uncertain=True)
    restarted = ConsoleService(s.engine)
    restarted.initialize()
    assert restarted.get_job(queued)['status'] == 'interrupted'
    assert restarted.get_job(job)['status'] == 'failed'
    assert len(s.engine.calls) == 1


@pytest.mark.asyncio
async def test_backoff_releases_slot_and_cancel_stops_retry(service, monkeypatch):
    import flow_agent.console as console
    monkeypatch.setattr(console, 'RETRY_DELAYS', (10, 30, 60))
    s = service
    s.engine.failures = 1
    first = queue(s, auto_retry_limit=3)
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: bool((s.get_job(first).get('result') or {}).get('auto_retry', {}).get('next_retry_at')))
        assert s.get_job(first)['status'] == 'queued'
        second = queue(s)
        await until(lambda: s.get_job(second)['status'] == 'succeeded')
        s.cancel(first)
        await until(lambda: not s.scheduler.active)
        assert s.get_job(first)['status'] == 'canceled'
        assert len(s.engine.calls) == 2
        state = s.get_job(first)['result']['auto_retry']
        delay = (datetime.fromisoformat(state['next_retry_at']) - datetime.fromisoformat(state['history'][0]['finished_at'])).total_seconds()
        assert 9 <= delay <= 11
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_known_result_recovered_without_generation_or_account_switch(service, monkeypatch):
    s = service
    s.engine.failures = 99
    mid = '11111111-1111-4111-8111-111111111111'
    s.engine.failure = page_error('video_download_failed', submitted=True, response_result_ids=[mid])
    recovered = []
    async def recover(**options):
        recovered.append(options)
        assert options['check_only'] is True and options['_recover']['media_ids'] == [mid]
        path = s.engine.output_dir / 'recovered.mp4'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'RECOVERED LOCAL FIXTURE')
        return {'status': 'succeeded', 'files': [{'path': str(path)}],
            'execution': {'recovered_without_generation': True, 'browser_closed': True}}
    monkeypatch.setattr(s.official_videos, 'run', recover)
    async def choose(data, **options): return s.cookies_dir / 'account_a.json'
    monkeypatch.setattr(s, '_scheduled_cookie', choose)
    job = s.enqueue(JobInput(kind='video', prompt='fixture', auto_retry_limit=3, retry_uncertain=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'succeeded')
        assert len(s.engine.calls) == 1 and len(recovered) == 1
        assert Path(recovered[0]['cookies']).name == 'account_a.json'
        assert s.get_job(job)['result']['files'][0]['name'] == 'recovered.mp4'
    finally:
        await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['flow_access_denied', 'manual_timeout', 'identity_mismatch', 'video_input_missing'])
async def test_permanent_failure_does_not_retry_even_with_charge_consent(service, code):
    s = service
    s.engine.failures = 99
    s.engine.failure = page_error(code)
    job = queue(s, auto_retry_limit=3, retry_uncertain=True)
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert len(s.engine.calls) == 1
    finally:
        await s.stop()


@pytest.mark.parametrize('data', [dict(auto_retry_limit=4), dict(auto_retry_limit=-1),
    dict(auto_retry_limit=True), dict(auto_retry_limit='3'), dict(auto_retry_limit=1.5),
    dict(auto_retry_limit=0, retry_uncertain=True), dict(auto_retry_limit=3, retry_uncertain='true')])
def test_retry_input_validation(data):
    with pytest.raises(ValidationError):
        JobInput(kind='video', prompt='fixture', **data)


@pytest.mark.asyncio
async def test_local_and_lan_retry_contracts_without_running_worker(service):
    s = service
    app = FastAPI()
    app.include_router(create_console_router(s))
    lan = create_lan_app(s, 'k' * 48)
    body = dict(kind='video', prompt='fixture', account_id='account_a', auto_retry_limit=3, retry_uncertain=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
        r = await c.post('/console/api/jobs', json=body)
        assert r.status_code == 202
        assert r.json()['request']['auto_retry_limit'] == 3
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lan), base_url='http://lan') as c:
        assert (await c.post('/api/v1/jobs', json=body)).status_code == 401
        auth = {'Authorization': 'Bearer ' + 'k' * 48}
        assert (await c.post('/api/v1/jobs', headers=auth, json=body)).status_code == 422
        r = await c.post('/api/v1/jobs', headers={**auth, 'X-Flow-Allow-Charge': 'yes'}, json=body)
        assert r.status_code == 202 and r.json()['request']['retry_uncertain'] is True
    assert not s.engine.calls


@pytest.mark.asyncio
async def test_manual_retry_can_enable_three_retries_without_touching_original(service):
    s = service
    s.engine.failures = 99
    original = queue(s)
    await s.execute_job(original)
    before = s.get_job(original)
    app = FastAPI()
    app.include_router(create_console_router(s))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
        r = await c.post(f'/console/api/jobs/{original}/retry', json={
            'acknowledge_possible_charge': True, 'auto_retry_limit': 3, 'retry_uncertain': True})
        assert r.status_code == 202
        assert r.json()['retry_of'] == original
        assert r.json()['request']['auto_retry_limit'] == 3
        assert r.json()['request']['retry_uncertain'] is True
        assert r.json()['result'] is None
        invalid = await c.post(f'/console/api/jobs/{original}/retry', json={
            'acknowledge_possible_charge': True, 'auto_retry_limit': 0, 'retry_uncertain': True})
        assert invalid.status_code == 422
    assert s.get_job(original) == before
    assert len(s.engine.calls) == 1  # API only queues; no real provider call.


@pytest.mark.asyncio
async def test_failed_recovery_never_falls_back_to_regeneration(service, monkeypatch):
    s = service
    s.engine.failures = 99
    mid = '11111111-1111-4111-8111-111111111111'
    s.engine.failure = page_error('video_download_failed', submitted=True, response_result_ids=[mid])
    recovered = []
    async def recover(**options):
        recovered.append(options)
        raise page_error('video_result_timeout', recovered_without_generation=True)
    monkeypatch.setattr(s.official_videos, 'run', recover)
    job = queue(s, auto_retry_limit=3, retry_uncertain=True)
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert len(s.engine.calls) == 1 and len(recovered) == 3
        assert all(c['check_only'] and c['_recover']['media_ids'] == [mid] for c in recovered)
    finally:
        await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_message', ['PUBLIC_ERROR_UNUSUAL_ACTIVITY', 'insufficient credits', 'rate limit'])
async def test_platform_rejection_never_retries(service, provider_message):
    s = service
    s.engine.failures = 99
    s.engine.failure = EngineError(['fixture'], 1, '', provider_message)
    job = queue(s, auto_retry_limit=3, retry_uncertain=True)
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert len(s.engine.calls) == 1
    finally:
        await s.stop()


def test_normal_captcha_strategy_logs_are_not_treated_as_verification_failure():
    from flow_agent.retry_policy import retry_action
    data = JobInput(kind='video', prompt='fixture', auto_retry_limit=3, retry_uncertain=True)
    exc = EngineError(['fixture'], 1, '', 'recaptcha strategy: auto; network disconnected')
    assert retry_action(data, None, exc)[0] == 'generate'
    exc = EngineError(['fixture'], 1, '', 'recaptcha token rejected')
    assert retry_action(data, None, exc)[0] == 'stop'
