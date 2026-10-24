"""Preparation never generates, retries or spends credits. All probes are offline."""
import asyncio
import hashlib
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from flow_agent.console import ConsoleService, JobInput, create_console_router, safe_failure
from flow_agent.engine import EngineError, FlowEngine


@pytest.fixture
def prepared_service(tmp_path, monkeypatch):
    service = ConsoleService(FlowEngine(root=tmp_path))
    service.initialize()
    from flow_agent.verification_settings import configure
    configure(service, False)  # These tests exercise the retained visible extension-sync option.
    service.preparation.settle_seconds = 0
    service.browser_pages.close_timeout = 0.03
    service.cookies_dir.mkdir()
    path = service.cookies_dir / 'account_a.json'
    path.write_text(json.dumps({'cookies': [{'name': 'SID', 'value': 'SECRET'}],
                                'at': 'SECRET', 'fsid': 'SECRET', 'fingerprint': {'userAgent': 'test'}}))
    operations = []
    async def open_browser(account_id):
        operations.append(('open', account_id))
    async def restart():
        operations.append(('restart', None))
        service.preparation.observe_bridge_line(
            f'bridge: synced 15 cookies (9 credential cookies, 0 unrelated dropped) from 127.0.0.1:12 to {path} '
            '(project "p", page tokens true, fingerprint true)')
    async def read(raw):
        operations.append(('read', None))
        return {'name': '测试账号', 'email': 'test@example.com', 'credits': 123, 'error': None}
    monkeypatch.setattr(service, 'open_account_browser', open_browser)
    monkeypatch.setattr(service, 'restart_idle_bridge', restart)
    monkeypatch.setattr(service.preparation, '_read_account', read)
    return service, operations, path


@pytest.mark.asyncio
@pytest.mark.parametrize('profile_name', ['flow_browser_profile_1', None])
async def test_preparation_finishes_cleanup_without_frontend_polling(prepared_service, monkeypatch, profile_name):
    service, _, _ = prepared_service
    monkeypatch.setattr(service, '_account_profile_name', lambda _: profile_name)
    async def acknowledge():
        while not service.browser_pages.leases:
            await asyncio.sleep(0)
        lease = next(iter(service.browser_pages.leases.values()))
        while lease.action != 'close':
            await asyncio.sleep(0)
        lease.result = 'closed'
        lease.done.set()
    ack = asyncio.create_task(acknowledge())
    await service.preparation.start('account_a')
    await service.preparation.task
    await ack
    result = service.preparation.snapshot()
    assert result['state'] == 'checked' and result['browser_cleanup']['state'] == 'closed'
    assert result['credits'] == 123 and not service.list_jobs()


@pytest.mark.asyncio
async def test_one_click_checks_exact_account_and_persists_balance_without_generation(prepared_service):
    service, operations, path = prepared_service
    report = await service.preparation.start('account_a')
    assert report['state'] == 'running'
    await service.preparation.task
    result = service.preparation.snapshot()
    assert result['state'] == 'checked'
    assert all(step['state'] == 'passed' for step in result['steps'])
    assert operations == [('open', 'account_a'), ('restart', None), ('read', None)]
    assert result['credits'] == 123 and result['generation_verified'] is False
    assert not service.list_jobs()
    assert service._verified_detail('account_a')['email'] == 'test@example.com'
    assert all(word not in json.dumps(result) for word in ('SECRET', str(path), 'fingerprint', 'fsid'))


@pytest.mark.asyncio
async def test_duplicate_click_reuses_operation_and_blocks_other_accounts(prepared_service, monkeypatch):
    service, operations, path = prepared_service
    gate = asyncio.Event()
    original = service.preparation._read_account
    async def hold_read(raw):
        await gate.wait()
        return await original(raw)
    monkeypatch.setattr(service.preparation, '_read_account', hold_read)
    first, second = await asyncio.gather(service.preparation.start('account_a'), service.preparation.start('account_a'))
    assert first['account_id'] == second['account_id']
    with pytest.raises(HTTPException) as denied:
        await service.preparation.start('account_b')
    assert denied.value.status_code == 409
    with pytest.raises(HTTPException) as denied:
        service.enqueue(JobInput(kind='video', prompt='never submit'))
    assert denied.value.status_code == 409
    gate.set()
    await service.preparation.task
    assert len([x for x in operations if x[0] == 'open']) == 1


@pytest.mark.asyncio
async def test_preparation_does_not_interrupt_existing_jobs(prepared_service):
    service, operations, _ = prepared_service
    service.enqueue(JobInput(kind='image', prompt='existing task'))
    with pytest.raises(HTTPException) as denied:
        await service.preparation.start('account_a')
    assert denied.value.status_code == 409
    assert not operations and service.preparation.task is None


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['port_only', 'wrong_account', 'incomplete'])
async def test_open_port_or_another_account_cannot_pass_as_synced(prepared_service, monkeypatch, evidence):
    service, operations, path = prepared_service
    async def restart():
        if evidence == 'wrong_account':
            service.preparation.synced.add('account_other')
        elif evidence == 'incomplete':
            service.preparation.observe_bridge_line(f'bridge: synced 2 cookies from host to {path} (project "", page tokens false, fingerprint true)')
    monkeypatch.setattr(service, 'restart_idle_bridge', restart)
    service.preparation.wait_seconds = 0
    await service.preparation.start('account_a')
    await service.preparation.task
    result = service.preparation.snapshot()
    assert result['state'] == 'needs_attention'
    assert result['steps'][1]['state'] == 'blocked'
    assert ('read', None) not in operations
    assert not service.list_jobs()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['identity', 'session_changed', 'credits'])
async def test_identity_session_and_balance_failures_remain_blocked(prepared_service, monkeypatch, failure):
    service, _, path = prepared_service
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with service.connect() as db:
        db.execute('insert into console_account_details values (?,?,?,?,?,?,?)',
                   ('account_a', digest, '本人', 'test@example.com', 123, 'yesterday', None))
    async def read(raw):
        if failure == 'session_changed':
            path.write_bytes(raw + b' ')
        return {'name': '测试', 'email': 'other@example.com' if failure == 'identity' else 'test@example.com',
                'credits': None if failure == 'credits' else 123, 'error': 'network' if failure == 'credits' else None}
    monkeypatch.setattr(service.preparation, '_read_account', read)
    await service.preparation.start('account_a')
    await service.preparation.task
    assert service.preparation.snapshot()['state'] == 'needs_attention'
    assert not service.list_jobs()
    if failure == 'credits':
        assert service._verified_detail('account_a')['credits'] is None


def test_risk_error_is_not_overwritten_by_successful_recaptcha_diagnostics():
    exc = EngineError(['PRIVATE'], 1, '', 'recaptcha: token acquired\ncli: error: PUBLIC_ERROR_UNUSUAL_ACTIVITY')
    message = safe_failure(exc)
    assert '异常活动' in message and '验证未通过' not in message and 'PRIVATE' not in message


@pytest.mark.asyncio
async def test_initial_session_update_is_rechecked_automatically(prepared_service, monkeypatch):
    service, operations, path = prepared_service
    original = service.preparation._read_account
    calls = 0
    async def read(raw):
        nonlocal calls
        calls += 1
        if calls == 1:
            path.write_bytes(raw + b' ')
        return await original(raw)
    monkeypatch.setattr(service.preparation, '_read_account', read)
    await service.preparation.start('account_a')
    await service.preparation.task
    assert calls == 2
    assert service.preparation.snapshot()['state'] == 'checked'
    assert not service.list_jobs()


def test_preparation_status_route_is_additive_and_keeps_secrets_server_side(prepared_service):
    service, _, _ = prepared_service
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        response = client.get('/console/api/accounts/preparation')
        assert response.status_code == 200 and response.json()['state'] == 'idle'
        assert client.post('/console/api/accounts/prepare', json={'account_id': '../missing'}).status_code == 422
        assert 'SECRET' not in response.text


@pytest.fixture
def batch_service(prepared_service, monkeypatch):
    service, operations, first_path = prepared_service
    for key in ('account_b', 'account_c', 'account_missing_browser'):
        (service.cookies_dir / f'{key}.json').write_bytes(first_path.read_bytes())
    accounts = [{'id': key, 'label': f'账号 {key[-1]}', 'email': None,
                 'can_open_browser': key != 'account_missing_browser', 'selectable': True}
                for key in ('account_a', 'account_b', 'account_c', 'account_missing_browser')]
    async def get_balance():
        return {}
    monkeypatch.setattr(service.engine, 'get_balance', get_balance)
    monkeypatch.setattr(service, 'accounts', lambda balance: {'accounts': accounts})
    async def restart():
        operations.append(('restart', None))
        service.preparation.synced.clear()
        current = next(value for op, value in reversed(operations) if op == 'open')
        service.preparation.observe_bridge_line(
            f'bridge: synced 15 cookies from host to {service.cookies_dir / (current + ".json")} '
            '(project "p", page tokens true, fingerprint true)')
    async def read(raw):
        current = next(value for op, value in reversed(operations) if op == 'open')
        operations.append(('read', current))
        return {'name': current, 'email': f'{current}@example.com', 'credits': 10, 'error': None}
    monkeypatch.setattr(service, 'restart_idle_bridge', restart)
    monkeypatch.setattr(service.preparation, '_read_account', read)
    return service, operations, accounts


@pytest.mark.asyncio
async def test_batch_prepares_every_openable_account_serially_and_reports_skips(batch_service):
    service, operations, _ = batch_service
    initial = await service.preparation.start_all()
    assert initial['scope'] == 'all' and initial['batch']['total'] == 4
    await service.preparation.task
    result = service.preparation.snapshot()
    assert operations == [(op, key if op != 'restart' else None)
                          for key in ('account_a', 'account_b', 'account_c') for op in ('open', 'restart', 'read')]
    assert result['batch']['succeeded'] == 3 and result['batch']['skipped'] == 1
    assert result['batch']['completed'] == 4
    assert [a['state'] for a in result['batch']['accounts']] == ['checked', 'checked', 'checked', 'skipped']
    assert result['state'] == 'needs_attention'  # A skipped account must not count as verified.
    assert service._verified_detail('account_c')['email'] == 'account_c@example.com'
    assert not service.list_jobs()
    assert all(word not in json.dumps(result) for word in ('SECRET', str(service.cookies_dir), 'fingerprint', 'fsid'))


@pytest.mark.asyncio
async def test_failed_account_does_not_abort_remaining_accounts(batch_service, monkeypatch):
    service, operations, accounts = batch_service
    # A cached identity cannot be assigned to a different signed-in account.
    path = service.cookies_dir / 'account_b.json'
    with service.connect() as db:
        db.execute('insert into console_account_details values (?,?,?,?,?,?,?)',
                   ('account_b', hashlib.sha256(path.read_bytes()).hexdigest(), '本人', 'expected@example.com', 99, 'yesterday', None))
    await service.preparation.start_all()
    await service.preparation.task
    result = service.preparation.snapshot()['batch']
    assert result['succeeded'] == 2 and result['attention'] == 1 and result['skipped'] == 1
    assert result['accounts'][1]['state'] == 'needs_attention'
    assert result['accounts'][1]['credits'] is None
    assert '未能确认' in result['accounts'][1]['message']
    assert result['accounts'][2]['state'] == 'checked'
    assert not service.list_jobs()


@pytest.mark.asyncio
async def test_batch_duplicate_clicks_do_not_open_twice_and_stop_leaves_remaining_unopened(batch_service, monkeypatch):
    service, operations, _ = batch_service
    started = asyncio.Event()
    gate = asyncio.Event()
    original = service.preparation._read_account
    async def hold(raw):
        started.set()
        await gate.wait()
        return await original(raw)
    monkeypatch.setattr(service.preparation, '_read_account', hold)
    await service.preparation.start_all()
    await started.wait()
    duplicate = await service.preparation.start_all()
    assert duplicate['batch']['accounts'][0]['state'] == 'running'
    assert duplicate['batch']['accounts'][0]['steps'][2]['state'] == 'running'
    with pytest.raises(HTTPException) as denied:
        await service.preparation.start('account_b')
    assert denied.value.status_code == 409
    with pytest.raises(HTTPException) as denied:
        service.enqueue(JobInput(kind='video', prompt='never submit'))
    assert denied.value.status_code == 409
    stopped = service.preparation.stop_after_current()
    assert stopped['batch']['stop_requested'] and service.preparation.running
    gate.set()
    await service.preparation.task
    report = service.preparation.snapshot()
    assert report['batch']['succeeded'] == 1 and report['batch']['skipped'] == 3
    assert [a for op, a in operations if op == 'open'] == ['account_a']
    assert '已停止' in report['batch']['accounts'][1]['message']
    assert not service.list_jobs()


@pytest.mark.asyncio
async def test_batch_rejects_active_jobs_or_an_existing_single_check(batch_service, monkeypatch):
    service, operations, _ = batch_service
    service.enqueue(JobInput(kind='image', prompt='existing task'))
    with pytest.raises(HTTPException) as denied:
        await service.preparation.start_all()
    assert denied.value.status_code == 409 and not operations
    with service.connect() as db:
        db.execute("update console_jobs set status='canceled'")
    await service.preparation.start('account_a')
    with pytest.raises(HTTPException) as denied:
        await service.preparation.start_all()
    assert denied.value.status_code == 409
    await service.preparation.task
    assert service.preparation.snapshot()['scope'] == 'single'


@pytest.mark.asyncio
async def test_all_success_is_checked_and_single_check_remains_usable(batch_service):
    service, operations, accounts = batch_service
    accounts.pop()
    await service.preparation.start_all()
    await service.preparation.task
    report = service.preparation.snapshot()
    assert report['state'] == 'checked' and report['batch']['succeeded'] == 3
    assert report['generation_verified'] is False
    await service.preparation.start('account_b')
    await service.preparation.task
    assert service.preparation.snapshot()['scope'] == 'single'
    assert 'batch' not in service.preparation.snapshot()
    assert not service.list_jobs()


@pytest.mark.asyncio
async def test_batch_without_openable_accounts_does_not_claim_success(batch_service):
    service, operations, accounts = batch_service
    for account in accounts:
        account['can_open_browser'] = False
    with pytest.raises(HTTPException) as denied:
        await service.preparation.start_all()
    assert denied.value.status_code == 409 and not operations
    assert service.preparation.task is None


def test_batch_routes_are_additive_and_return_safe_reports(prepared_service, monkeypatch):
    service, _, _ = prepared_service
    calls = []
    async def start_all():
        calls.append('all')
        return {'state': 'running', 'scope': 'all', 'account_id': None, 'steps': []}
    monkeypatch.setattr(service.preparation, 'start_all', start_all)
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        response = client.post('/console/api/accounts/prepare-all')
        assert response.status_code == 202 and response.json()['scope'] == 'all'
        assert calls == ['all']
        assert client.post('/console/api/accounts/preparation/stop').status_code == 200
        assert client.get('/console/api/accounts/preparation').json()['state'] == 'idle'
