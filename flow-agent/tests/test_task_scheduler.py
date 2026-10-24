"""Isolated scheduling and API contracts, never contacts Google."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_api import create_lan_app
from flow_agent.task_scheduler import SchedulerInput
from flow_agent.official_images import OfficialImageError


class ControlledEngine(FlowEngine):
    def __init__(self, root):
        super().__init__(root=root)
        self.calls = []
        self.releases = {}

    async def generate_video(self, **data):
        self.calls.append(data)
        await self.releases.setdefault(data['prompt'], asyncio.Event()).wait()
        if data['prompt'] == 'fail':
            raise RuntimeError('local controlled failure')
        path = self.output_dir / (data['prompt'] + '.mp4')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'LOCAL TEST ONLY')
        return {'status': 'ready', 'files': [{'path': str(path)}]}


@pytest.fixture
def scheduled_service(tmp_path):
    service = ConsoleService(ControlledEngine(tmp_path / 'flow-agent'))
    service.initialize()
    service.cookies_dir.mkdir()
    for index, account in enumerate(('a', 'b', 'c'), 1):
        (service.cookies_dir / f'account_{account}.json').write_text(json.dumps({
            'cookies': [{'name': 'fixture', 'value': 'local'}], 'at': 'local',
            'fsid': 'local', 'fingerprint': {'userAgent': 'fixture'}}))
        with service.connect() as db:
            db.execute('insert into console_account_profiles values (?, ?)',
                       ('account_' + account, 'flow_browser_profile_' + str(index)))
    return service


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


def queue(service, prompt, account):
    return service.enqueue(JobInput(kind='video', prompt=prompt, account_id='account_' + account))['id']


@pytest.mark.asyncio
async def test_custom_limit_skip_busy_account_lower_without_interrupt_and_persist(scheduled_service):
    s = scheduled_service
    s.scheduler.configure(SchedulerInput(max_concurrency=2))
    ids = [queue(s, p, a) for p, a in [('a1', 'a'), ('a2', 'a'), ('b1', 'b'), ('c1', 'c')]]
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: len(s.engine.calls) == 2)
        assert [c['prompt'] for c in s.engine.calls] == ['a1', 'b1']
        assert s.get_job(ids[1])['status'] == 'queued'
        s.scheduler.configure(SchedulerInput(max_concurrency=1))
        assert s.scheduler.snapshot()['running'] == 2
        s.engine.releases['a1'].set()
        await until(lambda: s.get_job(ids[0])['status'] == 'succeeded')
        await asyncio.sleep(.05)
        assert len(s.engine.calls) == 2
        s.engine.releases['b1'].set()
        await until(lambda: len(s.engine.calls) == 3)
        assert s.engine.calls[2]['prompt'] == 'a2'
        s.engine.releases['a2'].set()
        await until(lambda: len(s.engine.calls) == 4)
        s.engine.releases['c1'].set()
        await asyncio.wait_for(s.queue.join(), 3)
        assert all(s.get_job(i)['status'] == 'succeeded' for i in ids)
        assert not s.scheduler.reservations
        another = ConsoleService(FlowEngine(s.engine.root))
        another.scheduler.load()
        assert another.scheduler.max_concurrency == 1
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_increase_limit_failure_releases_and_queued_cancel(scheduled_service):
    s = scheduled_service
    s.scheduler.configure(SchedulerInput(max_concurrency=1))
    one, canceled, two = queue(s, 'fail', 'a'), queue(s, 'cancel', 'a'), queue(s, 'b1', 'b')
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: len(s.engine.calls) == 1)
        s.cancel(canceled)
        s.scheduler.configure(SchedulerInput(max_concurrency=2))
        await until(lambda: len(s.engine.calls) == 2)
        s.engine.releases['fail'].set()
        s.engine.releases['b1'].set()
        await asyncio.wait_for(s.queue.join(), 3)
        assert s.get_job(one)['status'] == 'failed'
        assert s.get_job(two)['status'] == 'succeeded'
        assert s.get_job(canceled)['status'] == 'canceled'
        assert len(s.engine.calls) == 2 and not s.scheduler.reservations
    finally:
        await s.stop()


def test_new_install_defaults_to_maximum_without_creating_jobs(scheduled_service):
    s = scheduled_service
    s.scheduler.load()
    assert s.scheduler.snapshot()['max_concurrency'] == s.scheduler.snapshot()['max'] == 16
    assert not s.list_jobs() and not s.engine.calls


@pytest.mark.parametrize('value', [1, 4, 16])
def test_saved_custom_limit_survives_reload(scheduled_service, value):
    s = scheduled_service
    s.scheduler.configure(SchedulerInput(max_concurrency=value))
    reloaded = ConsoleService(FlowEngine(s.engine.root))
    reloaded.scheduler.load()
    assert reloaded.scheduler.max_concurrency == value
    assert not reloaded.list_jobs()


@pytest.mark.parametrize('value', ['broken', '0', '17'])
def test_invalid_saved_limit_falls_back_to_maximum(scheduled_service, value):
    s = scheduled_service
    with s.connect() as db:
        db.execute("insert or replace into console_meta values ('max_concurrency', ?)", (value,))
    s.scheduler.load()
    assert s.scheduler.max_concurrency == 16


@pytest.mark.asyncio
@pytest.mark.parametrize('shared', ['profile', 'email'])
async def test_two_account_ids_sharing_profile_remain_serial(scheduled_service, monkeypatch, shared):
    s = scheduled_service
    if shared == 'profile':
        with s.connect() as db:
            db.execute("update console_account_profiles set profile_name='flow_browser_profile_1' where account_key='account_b'")
    else:
        monkeypatch.setattr(s, '_verified_detail', lambda key: {'email': 'same@example.test' if key != 'account_c' else 'other@example.test'})
    s.scheduler.configure(SchedulerInput(max_concurrency=3))
    queue(s, 'a1', 'a'); queue(s, 'b1', 'b'); queue(s, 'c1', 'c')
    s.worker = asyncio.create_task(s.run())
    try:
        await until(lambda: len(s.engine.calls) == 2)
        assert [c['prompt'] for c in s.engine.calls] == ['a1', 'c1']
        s.engine.releases['a1'].set()
        await until(lambda: len(s.engine.calls) == 3)
        for event in s.engine.releases.values():
            event.set()
        await asyncio.wait_for(s.queue.join(), 3)
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_page_profile_lock_native_exclusion_and_capacity(scheduled_service):
    s = scheduled_service
    s.scheduler.configure(SchedulerInput(max_concurrency=2))
    execution = s.browser_execution
    first = str(s.cookies_dir / 'account_a.json')
    second = str(s.cookies_dir / 'account_b.json')
    entered = asyncio.Event()
    async def another():
        async with execution.page_slot(second):
            entered.set()
    async with execution.page_slot(first):
        assert execution.handoff_active
        task = asyncio.create_task(another())
        await asyncio.wait_for(entered.wait(), 1)
        await task
    assert not execution.handoff_active
    async with execution.lock:
        entered.clear()
        task = asyncio.create_task(another())
        await asyncio.sleep(.15)
        assert not entered.is_set()
    await asyncio.wait_for(task, 1)
    assert not execution.page_accounts


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['image', 'video'])
async def test_automatic_account_is_selected_once_and_locked_before_settings(scheduled_service, monkeypatch, kind):
    s = scheduled_service
    selections = []
    async def choose():
        selections.append('chosen')
        return str(s.cookies_dir / ('account_a.json' if len(selections) == 1 else 'account_b.json'))
    monkeypatch.setattr(s, '_scheduled_cookie', choose)
    runner = s.official_images if kind == 'image' else s.official_videos
    def settings(account):
        assert account == 'account_a' and set(s.browser_execution.page_accounts) == {account}
        raise OfficialImageError('profile_missing')
    monkeypatch.setattr(runner, 'settings', settings)
    with pytest.raises(OfficialImageError):
        await runner.run(check_only=True)
    assert len(selections) == 1 and not s.browser_execution.page_accounts


@pytest.mark.asyncio
async def test_local_and_authenticated_api_share_setting_and_validate(scheduled_service):
    s = scheduled_service
    local = FastAPI(); local.include_router(create_console_router(s))
    lan = create_lan_app(s, 'x' * 48)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=local), base_url='http://local') as c, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=lan), base_url='http://lan') as remote:
        auth = {'Authorization': 'Bearer ' + 'x' * 48}
        assert (await remote.post('/api/v1/scheduler', json={'max_concurrency': 2})).status_code == 401
        for value in (0, 17, 1.5, True, '2'):
            assert (await c.post('/console/api/scheduler', json={'max_concurrency': value})).status_code == 422
        assert (await remote.post('/api/v1/scheduler', headers=auth, json={'max_concurrency': 3})).status_code == 200
        assert (await c.get('/console/api/scheduler')).json()['max_concurrency'] == 3
        assert not s.list_jobs()
        paths = (await remote.get('/api/v1/openapi.json', headers=auth)).json()['paths']
        for path in ('/api/v1/scheduler', '/api/v1/accounts/prepare-all', '/api/v1/accounts/preparation', '/api/v1/accounts/preparation/stop'):
            assert path in paths


@pytest.mark.asyncio
async def test_batch_prepare_via_lan_auth_opt_in_progress_idempotence_and_busy(scheduled_service, monkeypatch):
    s = scheduled_service
    monkeypatch.setattr(s, 'accounts', lambda _: {'accounts': [
        {'id': 'account_' + a, 'label': a, 'email': None, 'can_open_browser': True}
        for a in ('a', 'b', 'c')]})
    async def balance(): return {}
    monkeypatch.setattr(s.engine, 'get_balance', balance)
    release = asyncio.Event(); visited = []
    async def prepare(account):
        visited.append(account)
        await release.wait()
        s.preparation.report.update(state='needs_attention' if account == 'account_b' else 'checked',
                                    message='local fixture', credits=None if account == 'account_b' else 100)
    monkeypatch.setattr(s.preparation, '_run', prepare)
    lan = create_lan_app(s, 'k' * 48)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lan), base_url='http://lan') as c:
        auth = {'Authorization': 'Bearer ' + 'k' * 48}
        assert (await c.post('/api/v1/accounts/prepare-all')).status_code == 401
        assert (await c.post('/api/v1/accounts/prepare-all', headers=auth)).status_code == 422
        enabled = {**auth, 'X-Flow-Allow-Interaction': 'yes'}
        result = await c.post('/api/v1/accounts/prepare-all', headers=enabled)
        assert result.status_code == 202 and result.json()['scope'] == 'all'
        await until(lambda: len(visited) == 1)
        assert (await c.post('/api/v1/accounts/prepare-all', headers=enabled)).status_code == 202
        assert len(visited) == 1
        release.set()
        await s.preparation.task
        report = (await c.get('/api/v1/accounts/preparation', headers=auth)).json()
        assert report['batch']['completed'] == 3 and report['batch']['attention'] == 1
        assert report['batch']['succeeded'] == 2 and not report['generation_verified']
        assert not s.list_jobs()
        queue(s, 'busy', 'a')
        assert (await c.post('/api/v1/accounts/prepare-all', headers=enabled)).status_code == 409
