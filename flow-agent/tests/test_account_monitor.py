"""Background checks are read-only and independent of browser/UI activity."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine


@pytest.fixture
def monitored_service(tmp_path, monkeypatch):
    service = ConsoleService(FlowEngine(root=tmp_path))
    service.initialize()
    service.monitor.load()
    service.cookies_dir.mkdir()
    for key in ('a', 'b'):
        (service.cookies_dir / f'account_{key}.json').write_text(json.dumps({
            'account': key, 'cookies': [{'name': 'SID', 'value': 'SECRET'}], 'at': 'SECRET',
            'fsid': 'SECRET', 'fingerprint': {'userAgent': 'test'},
        }))
    calls = []
    class Reader:
        def __init__(self, client, bundle):
            self.bundle = bundle
        async def read(self):
            calls.append(self.bundle['account'])
            return {'name': '测试' + self.bundle['account'], 'email': self.bundle['account'] + '@example.com',
                    'credits': 100, 'error': None}
    monkeypatch.setattr('flow_agent.console.FlowAccountReader', Reader)
    async def forbidden(*args, **kwargs):
        pytest.fail('Background monitoring must never open/restart browsers or generate')
    for name in ('open_account_browser', 'connect_account', 'start_bridge', 'restart_idle_bridge'):
        monkeypatch.setattr(service, name, forbidden)
    monkeypatch.setattr(service.engine, 'generate_video', forbidden)
    monkeypatch.setattr(service.engine, 'generate_image', forbidden)
    return service, calls, Reader


@pytest.mark.asyncio
async def test_background_reads_all_accounts_without_browser_or_generation(monitored_service):
    service, calls, _ = monitored_service
    for index, key in enumerate(('account_a', 'account_b')):
        name = f'flow_browser_profile_{index}'
        (service.engine.root / 'data' / name).mkdir()
        with service.connect() as db:
            db.execute('insert or replace into console_account_profiles values (?, ?)', (key, name))
    await service.monitor.start_check()
    await service.monitor.task
    report = service.monitor.snapshot()
    assert calls == ['a', 'b']
    assert report['enabled'] and report['state'] == 'watching'
    assert all(a['state'] == 'ready' and a['credits'] == 100 for a in report['accounts'])
    assert report['generation_verified'] is False and report['last_cycle_at']
    assert not service.list_jobs()
    assert all(secret not in json.dumps(report) for secret in ('SECRET', str(service.cookies_dir), 'fingerprint', 'fsid'))


@pytest.mark.asyncio
async def test_periodic_check_refreshes_unchanged_sessions_when_due(monitored_service):
    service, calls, _ = monitored_service
    for index, key in enumerate(('account_a', 'account_b')):
        name = f'flow_browser_profile_{index}'
        (service.engine.root / 'data' / name).mkdir()
        with service.connect() as db:
            db.execute('insert or replace into console_account_profiles values (?, ?)', (key, name))
    await service.monitor.start_check()
    await service.monitor.task
    await service.monitor.start_check()
    assert calls == ['a', 'b']  # Five-second ticks do not query Google repeatedly.
    expired = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    with service.connect() as db:
        db.execute('update console_account_details set checked_at=?', (expired,))
    assert all(a['state'] == 'stale' for a in service.monitor.snapshot()['accounts'])
    await service.monitor.start_check()
    await service.monitor.task
    assert calls == ['a', 'b', 'a', 'b']


@pytest.mark.asyncio
async def test_server_scheduler_rechecks_without_any_page_request(monitored_service, monkeypatch):
    service, calls, _ = monitored_service
    ticks = 0
    async def tick(seconds):
        nonlocal ticks
        assert seconds == 5
        ticks += 1
        if ticks > 1:
            await service.monitor.task
        if ticks == 2:
            expired = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
            with service.connect() as db:
                db.execute('update console_account_details set checked_at=?', (expired,))
        if ticks == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr('flow_agent.console.asyncio.sleep', tick)
    with pytest.raises(asyncio.CancelledError):
        await service._watch_account_details()
    assert calls == ['a', 'b', 'a', 'b']
    assert not service.list_jobs()


@pytest.mark.asyncio
async def test_changed_session_is_detected_before_periodic_deadline(monitored_service):
    service, calls, _ = monitored_service
    await service.monitor.start_check()
    await service.monitor.task
    path = service.cookies_dir / 'account_a.json'
    path.write_bytes(path.read_bytes() + b' ')
    assert service.monitor.snapshot()['accounts'][0]['state'] == 'pending'
    await service.monitor.start_check()
    await service.monitor.task
    assert calls == ['a', 'b', 'a']


@pytest.mark.asyncio
@pytest.mark.parametrize('error,expected', [('session', 'needs_attention'), ('refused', 'needs_attention'), ('network', 'network_error')])
async def test_failure_status_clears_balance_and_waits_for_next_round(monitored_service, monkeypatch, error, expected):
    service, calls, Reader = monitored_service
    async def no_browser(*args, **kwargs):
        pass  # Browser recovery is covered separately by the headless tests.
    monkeypatch.setattr(service.preparation, '_run_headless', no_browser)
    await service.monitor.start_check()
    await service.monitor.task
    async def read(self):
        calls.append(self.bundle['account'])
        return {'name': None, 'email': None, 'credits': None, 'error': error}
    monkeypatch.setattr(Reader, 'read', read)
    await service.monitor.start_check(force=True)
    await service.monitor.task
    report = service.monitor.snapshot()
    assert all(a['state'] == expected and a['credits'] is None for a in report['accounts'])
    await service.monitor.start_check()
    assert len(calls) == 4
    assert service._verified_detail('account_a')['credits'] is None


@pytest.mark.asyncio
async def test_identity_mismatch_cannot_replace_saved_account_or_balance(monitored_service, monkeypatch):
    service, _, Reader = monitored_service
    await service.monitor.start_check()
    await service.monitor.task
    async def read(self):
        return {'name': '别的账号', 'email': 'other@example.com', 'credits': 9999, 'error': None}
    monkeypatch.setattr(Reader, 'read', read)
    await service.monitor.start_check(force=True)
    await service.monitor.task
    assert all(a['state'] == 'identity_error' for a in service.monitor.snapshot()['accounts'])
    detail = service._verified_detail('account_a')
    assert detail['email'] == 'a@example.com' and detail['credits'] is None


@pytest.mark.asyncio
async def test_session_change_during_read_does_not_bind_stale_result(monitored_service, monkeypatch):
    service, _, Reader = monitored_service
    original = Reader.read
    async def read(self):
        path = service.cookies_dir / f"account_{self.bundle['account']}.json"
        path.write_bytes(path.read_bytes() + b' ')
        return await original(self)
    monkeypatch.setattr(Reader, 'read', read)
    await service.monitor.start_check()
    await service.monitor.task
    assert all(a['state'] == 'pending' and a['credits'] is None for a in service.monitor.snapshot()['accounts'])
    assert service._verified_detail('account_a') is None


@pytest.mark.asyncio
async def test_disabled_setting_persists_and_manual_check_remains_available(monitored_service):
    service, calls, _ = monitored_service
    service.monitor.configure(False)
    service.monitor.load()
    assert not service.monitor.enabled
    await service.monitor.start_check()
    assert not calls and service.monitor.task is None
    await service.monitor.start_check(force=True)
    await service.monitor.task
    assert calls == ['a', 'b']
    assert service.monitor.snapshot()['state'] == 'disabled'


@pytest.mark.asyncio
async def test_generation_takes_priority_and_monitor_does_not_touch_queue(monitored_service, monkeypatch):
    service, calls, Reader = monitored_service
    original = Reader.read
    async def read(self):
        result = await original(self)
        service.enqueue(JobInput(kind='image', prompt='existing task'))
        return result
    monkeypatch.setattr(Reader, 'read', read)
    await service.monitor.start_check()
    await service.monitor.task
    assert calls == ['a']
    assert service.list_jobs()[0]['status'] == 'queued'
    assert service.monitor.snapshot()['state'] == 'paused'
    with pytest.raises(HTTPException) as denied:
        await service.monitor.start_check(force=True)
    assert denied.value.status_code == 409 and calls == ['a']


@pytest.mark.asyncio
async def test_incomplete_session_is_reported_without_query(monitored_service):
    service, calls, _ = monitored_service
    (service.cookies_dir / 'account_b.json').write_text('{}')
    await service.monitor.start_check()
    await service.monitor.task
    assert calls == ['a']
    incomplete = service.monitor.snapshot()['accounts'][1]
    assert incomplete['state'] == 'incomplete' and incomplete['requires_user_action']


@pytest.mark.asyncio
async def test_duplicate_requests_and_shutdown_do_not_leak_background_tasks(monitored_service, monkeypatch):
    service, calls, Reader = monitored_service
    started = asyncio.Event()
    gate = asyncio.Event()
    original = Reader.read
    async def read(self):
        started.set()
        await gate.wait()
        return await original(self)
    monkeypatch.setattr(Reader, 'read', read)
    await service.start()
    await service.monitor.start_check(force=True)
    await started.wait()
    task = service.monitor.task
    duplicate = await service.monitor.start_check(force=True)
    assert service.monitor.task is task and duplicate['state'] == 'checking'
    await service.stop()
    assert task.cancelled() and service.monitor.task is None
    assert service.account_details_task is None and service.worker is None and not calls


def test_monitor_routes_validate_settings_and_do_not_expose_secrets(monitored_service):
    service, _, _ = monitored_service
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        report = client.get('/console/api/accounts/monitor')
        assert report.status_code == 200 and report.json()['enabled']
        assert 'SECRET' not in report.text
        assert client.post('/console/api/accounts/monitor/settings', json={}).status_code == 422
        result = client.post('/console/api/accounts/monitor/settings', json={'enabled': False})
        assert result.status_code == 200 and not result.json()['enabled']
        assert client.post('/console/api/accounts/monitor/check').status_code == 202


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['session', 'refused', 'network', 'identity'])
async def test_auth_failure_repairs_once_per_session_without_generation(monitored_service, monkeypatch, error):
    service, calls, Reader = monitored_service
    await service.monitor.start_check()
    await service.monitor.task
    async def failed(self):
        return {'name': None, 'email': None, 'credits': None, 'error': error}
    monkeypatch.setattr(Reader, 'read', failed)
    monkeypatch.setattr(service, '_account_profile_name', lambda key: 'dedicated')
    repaired = []
    async def repair(key, *, repair_login):
        assert repair_login is True
        assert not service.balance_refresh_lock.locked()
        repaired.append(key)
    monkeypatch.setattr(service.preparation, '_run_headless', repair)
    await service.monitor.start_check(force=True)
    await service.monitor.task
    assert repaired == (['account_a', 'account_b'] if error in ('session', 'refused') else [])
    await service.monitor.start_check(force=True)
    await service.monitor.task
    assert len(repaired) == (2 if error in ('session', 'refused') else 0)
    assert not service.list_jobs()

@pytest.mark.asyncio
async def test_busy_profile_resumes_without_second_click(monitored_service, monkeypatch):
    service, calls, Reader = monitored_service
    await service.monitor.start_check(); await service.monitor.task
    async def failed(self):
        return {'name':None,'email':None,'credits':None,'error':'session'}
    monkeypatch.setattr(Reader,'read',failed)
    busy = True
    monkeypatch.setattr(service.monitor,'profile_busy',lambda key:busy)
    repaired=[]
    async def repair(key, **kwargs): repaired.append(key)
    monkeypatch.setattr(service.preparation,'_run_headless',repair)
    await service.monitor.start_check(force=True); await service.monitor.task
    assert service.monitor.waiting_profiles == {'account_a','account_b'}
    assert not repaired
    busy = False
    # No force: closing the browser makes the waiting accounts immediately due.
    await service.monitor.start_check(); await service.monitor.task
    assert repaired == ['account_a','account_b']
    assert not service.monitor.waiting_profiles
    assert not service.list_jobs()
