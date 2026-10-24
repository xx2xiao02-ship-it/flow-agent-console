"""Offline queue/HTTP and local Chrome checks; never connect to Google."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from flow_agent.console import JobInput, create_console_router
from flow_agent.retry_policy import retry_action
from flow_agent.lan_api import create_lan_app
from flow_agent.official_images import OfficialImageError, profile_in_use
from tests.test_auto_retry import service, page_error, until
from tests.test_official_videos import video_service, local_video_page


def add_accounts(s):
    for a in ('c', 'd'):
        (s.cookies_dir / f'account_{a}.json').write_text((s.cookies_dir / 'account_a.json').read_text())


def select_from_cookies(s, monkeypatch):
    async def choose(data=None, *, exclude_accounts=None):
        for a in sorted(s._cookie_files()):
            if a not in (exclude_accounts or set()):
                return s._account_cookie(a)
        return None
    monkeypatch.setattr(s, '_scheduled_cookie', choose)


@pytest.mark.asyncio
@pytest.mark.parametrize('failures,count,status', [(3, 4, 'succeeded'), (99, 4, 'failed'), (1, 2, 'succeeded')])
async def test_switches_each_retry_and_never_exceeds_four(service, monkeypatch, failures, count, status):
    s = service
    add_accounts(s)
    select_from_cookies(s, monkeypatch)
    s.engine.failures = failures
    job = s.enqueue(JobInput(kind='video', prompt='fixture', account_id='account_a',
                            auto_retry_limit=3, retry_switch_accounts=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] in ('failed', 'succeeded'))
        final = s.get_job(job)
        assert final['status'] == status
        accounts = [Path(c['cookies']).stem for c in s.engine.calls]
        assert len(accounts) == len(set(accounts)) == count
        assert accounts[0] == 'account_a'
        state = final['result']['auto_retry']
        assert state['attempted_accounts'] == accounts
        assert [h['account_id'] for h in state['history']] == accounts
        assert len(s.list_jobs()) == 1
        assert all(c['prompt'] == 'fixture' and c['count'] == 1 for c in s.engine.calls)
        assert 'PRIVATE' not in json.dumps(final)
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_exhausted_accounts_stop_instead_of_reusing_or_waiting_forever(service, monkeypatch):
    s = service
    select_from_cookies(s, monkeypatch)
    s.engine.failures = 99
    job = s.enqueue(JobInput(kind='video', prompt='fixture', auto_retry_limit=3,
                            retry_switch_accounts=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert len(s.engine.calls) == 2
        assert len({Path(c['cookies']).stem for c in s.engine.calls}) == 2
        assert '没有未尝试' in s.get_job(job)['error']
    finally:
        await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', ['email', 'profile'])
async def test_aliases_are_not_treated_as_different_accounts(service, monkeypatch, alias):
    s = service
    select_from_cookies(s, monkeypatch)
    monkeypatch.setattr(s.scheduler, 'identity_key' if alias == 'email' else 'profile_key', lambda _: 'same')
    s.engine.failures = 99
    job = s.enqueue(JobInput(kind='video', prompt='fixture', account_id='account_a',
                            auto_retry_limit=3, retry_switch_accounts=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert len(s.engine.calls) == 1
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_busy_fresh_account_waits_and_is_not_reused(service, monkeypatch):
    s = service
    select_from_cookies(s, monkeypatch)
    s.engine.failures = 1
    s.scheduler.reservations['other-task'] = ('account_b', s.scheduler.profile_key('account_b'))
    job = s.enqueue(JobInput(kind='video', prompt='fixture', account_id='account_a',
                            auto_retry_limit=3, retry_switch_accounts=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: bool((s.get_job(job).get('result') or {}).get('auto_retry', {}).get('history')))
        await asyncio.sleep(.12)
        assert s.get_job(job)['status'] == 'queued' and len(s.engine.calls) == 1
        s.scheduler.reservations.pop('other-task')
        s.scheduler.wakeup.set()
        await until(lambda: s.get_job(job)['status'] == 'succeeded')
        assert [Path(c['cookies']).stem for c in s.engine.calls] == ['account_a', 'account_b']
    finally:
        await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['video_rpc_error', 'video_response_unrecognized'])
async def test_unknown_submission_queries_original_once_and_never_regenerates(service, monkeypatch, code):
    s = service
    select_from_cookies(s, monkeypatch)
    s.engine.failures = 99
    project = '11111111-1111-4111-8111-111111111111'
    s.engine.failure = page_error(code, submitted=True, upload_project_id=project)
    queries = []
    async def inspect(**options):
        queries.append(options)
        assert options['check_only'] is True
        assert options['_recover'] == {'inspect_only': True, 'project_id': project}
        assert Path(options['cookies']).stem == 'account_a'
        raise page_error('original_result_unknown', recovered_without_generation=True)
    monkeypatch.setattr(s.official_videos, 'run', inspect)
    job = s.enqueue(JobInput(kind='video', prompt='fixture', auto_retry_limit=3,
                            retry_switch_accounts=True, retry_uncertain=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert len(s.engine.calls) == len(queries) == 1
        assert s.get_job(job)['result']['auto_retry']['history'][1]['action'] == 'inspect'
    finally:
        await s.stop()


def test_rotation_never_guesses_unsubmitted_or_bypasses_permission_errors():
    data = JobInput(kind='video', prompt='fixture', auto_retry_limit=3,
                    retry_switch_accounts=True, retry_uncertain=True)
    assert retry_action(data, None)[0] == 'stop'
    for code in ('flow_access_denied', 'manual_timeout', 'identity_mismatch', 'provider_rejected'):
        e = page_error(code)
        assert retry_action(data, e.execution, e)[0] == 'stop'
    e = page_error('video_rpc_error', submitted=True)
    assert retry_action(data, e.execution, e)[0] == 'stop'  # Missing original project.


@pytest.mark.asyncio
async def test_last_allowed_submission_still_queries_without_extra_paid_attempt(service, monkeypatch):
    s = service
    add_accounts(s)
    select_from_cookies(s, monkeypatch)
    original_generate = s.engine.generate_video
    s.engine.failures = 3
    calls = 0
    async def generate(**options):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise page_error('video_rpc_error', submitted=True,
                             upload_project_id='11111111-1111-4111-8111-111111111111')
        return await original_generate(**options)
    queries = []
    async def inspect(**options):
        queries.append(options)
        assert Path(options['cookies']).stem == 'account_d'
        raise page_error('original_result_unknown', recovered_without_generation=True)
    monkeypatch.setattr(s.engine, 'generate_video', generate)
    monkeypatch.setattr(s.official_videos, 'run', inspect)
    job = s.enqueue(JobInput(kind='video', prompt='fixture', account_id='account_a',
                            auto_retry_limit=3, retry_switch_accounts=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'failed')
        assert calls == 4 and len(queries) == 1
        state = s.get_job(job)['result']['auto_retry']
        assert state['retries_started'] == 3 and state['queries_started'] == 1
        assert state['attempts_started'] == 5
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_known_ids_recover_on_original_account_and_keep_download(service, monkeypatch):
    s = service
    select_from_cookies(s, monkeypatch)
    s.engine.failures = 99
    project = '11111111-1111-4111-8111-111111111111'
    media = '22222222-2222-4222-8222-222222222222'
    s.engine.failure = page_error('video_download_failed', submitted=True,
                                 upload_project_id=project, response_result_ids=[media])
    queries = []
    async def recover(**options):
        queries.append(options)
        assert options['check_only'] is True
        assert options['_recover'] == {'media_ids': [media], 'project_id': project}
        assert Path(options['cookies']).stem == 'account_a'
        path = s.engine.output_dir / 'recovered-fixture.mp4'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'LOCAL FILE FIXTURE ONLY')
        return {'status': 'succeeded', 'files': [{'path': str(path)}],
                'execution': {'recovered_without_generation': True, 'browser_closed': True}}
    monkeypatch.setattr(s.official_videos, 'run', recover)
    job = s.enqueue(JobInput(kind='video', prompt='fixture', account_id='account_a',
                            auto_retry_limit=3, retry_switch_accounts=True))['id']
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: s.get_job(job)['status'] == 'succeeded')
        assert len(s.engine.calls) == len(queries) == 1
        assert s.get_job(job)['result']['files'][0]['name'] == 'recovered-fixture.mp4'
        assert s.get_job(job)['result']['auto_retry']['attempted_accounts'] == ['account_a']
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_rotation_http_contract_and_manual_retry(service):
    s = service
    app = FastAPI()
    app.include_router(create_console_router(s))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        body = {'kind': 'video', 'prompt': 'fixture', 'account_id': 'account_a',
                'auto_retry_limit': 3, 'retry_switch_accounts': True}
        response = await client.post('/console/api/jobs', json=body)
        assert response.status_code == 202
        assert response.json()['request']['retry_switch_accounts'] is True
        job = response.json()['id']
        s.engine.failures = 99
        await s.execute_job(job)
        # Make a separate failed job with retry disabled for the manual retry route.
        old = s.enqueue(JobInput(kind='video', prompt='fixture', account_id='account_a'))['id']
        await s.execute_job(old)
        before = s.get_job(old)
        response = await client.post(f'/console/api/jobs/{old}/retry', json={
            'acknowledge_possible_charge': True, 'auto_retry_limit': 3, 'retry_switch_accounts': True})
        assert response.status_code == 202
        assert response.json()['request']['retry_switch_accounts'] is True
        assert s.get_job(old) == before
    with pytest.raises(ValidationError):
        JobInput(kind='video', prompt='fixture', retry_switch_accounts='true')


@pytest.mark.asyncio
async def test_lan_rotation_requires_key_and_charge_header_and_only_enqueues(service):
    s = service
    app = create_lan_app(s, 'k' * 48)
    body = {'kind': 'video', 'prompt': 'fixture', 'auto_retry_limit': 3,
            'retry_switch_accounts': True, 'retry_uncertain': False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://lan') as client:
        assert (await client.post('/api/v1/jobs', json=body)).status_code == 401
        headers = {'Authorization': 'Bearer ' + 'k' * 48}
        assert (await client.post('/api/v1/jobs', json=body, headers=headers)).status_code == 422
        response = await client.post('/api/v1/jobs', json=body,
                                     headers={**headers, 'X-Flow-Allow-Charge': 'yes'})
        assert response.status_code == 202
        assert response.json()['request']['retry_switch_accounts'] is True
    assert not s.engine.calls


@pytest.mark.asyncio
async def test_real_local_browser_unknown_query_blocks_generation_and_closes(video_service, local_video_page):
    s, cookie, profile = video_service
    with pytest.raises(OfficialImageError) as error:
        await s.official_videos.run(cookies=str(cookie), check_only=True, prompt='fixture',
            _recover={'inspect_only': True, 'project_id': 'fixture'})
    report = s.official_videos.last_report
    assert error.value.code == 'original_result_unknown'
    assert report['original_project_query']['completed'] is True
    assert report['generation_requests'] == 0 and not report['generation_submitted']
    assert report['browser_closed'] is True and not profile_in_use(profile)
    assert local_video_page.submissions == 0


@pytest.mark.asyncio
async def test_real_local_browser_rejects_wrong_original_project(video_service, local_video_page):
    s, cookie, profile = video_service
    with pytest.raises(OfficialImageError) as error:
        await s.official_videos.run(cookies=str(cookie), check_only=True,
            _recover={'inspect_only': True, 'project_id': 'other-project'})
    assert error.value.code == 'recovery_project_mismatch'
    assert s.official_videos.last_report['browser_closed'] is True
    assert local_video_page.submissions == 0 and not profile_in_use(profile)
