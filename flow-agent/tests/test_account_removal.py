"""Local account lifecycle tests. No browser launches or upstream requests."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_api import create_lan_app


@pytest.fixture
def service(tmp_path):
    svc = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    svc.initialize()
    svc.cookies_dir.mkdir()
    for key in ('a', 'b'):
        (svc.cookies_dir / f'account_{key}.json').write_text(json.dumps({
            'cookies': [{'name': 'fixture', 'value': 'PRIVATE_COOKIE'}],
            'at': 'PRIVATE_AT', 'fsid': 'PRIVATE_FSID', 'fingerprint': {'userAgent': 'test'}}))
    return svc


def test_remove_restore_preserves_other_accounts_jobs_media_and_browser(service):
    original = (service.cookies_dir / 'account_a.json').read_bytes()
    other = (service.cookies_dir / 'account_b.json').read_bytes()
    profile = service.engine.root / 'data' / 'chrome-profile' / 'Preferences'
    profile.parent.mkdir()
    profile.write_text('browser login fixture')
    service.engine.output_dir.mkdir()
    media = service.engine.output_dir / 'paid-result.png'
    media.write_bytes(b'paid result fixture')
    job = service.enqueue(JobInput(kind='image', prompt='fixture'))
    service.cancel(job['id'])
    jobs = service.list_jobs()
    with service.connect() as db:
        db.execute('insert into console_account_details values (?,?,?,?,?,?,?)',
                   ('account_a', 'digest', 'fixture', 'a@example.test', 25, None, None))
    service.account_removal.remove('account_a', True)
    assert not (service.cookies_dir / 'account_a.json').exists()
    assert list(service._cookie_files()) == ['account_b']
    assert service.account_removal.snapshot()[0]['id'] == 'account_a'
    assert next(service.account_removal.archive_dir.iterdir()).read_bytes() == original
    with pytest.raises(HTTPException) as absent:
        service._account_cookie('account_a')
    assert absent.value.status_code == 422
    assert [a['account_id'] for a in service.monitor.snapshot()['accounts']] == ['account_b']
    restarted = ConsoleService(service.engine)
    assert list(restarted._cookie_files()) == ['account_b']
    service.account_removal.restore('account_a', True)
    assert (service.cookies_dir / 'account_a.json').read_bytes() == original
    assert service.account_removal.snapshot() == []
    assert set(service._cookie_files()) == {'account_a', 'account_b'}
    assert (service.cookies_dir / 'account_b.json').read_bytes() == other
    assert service.list_jobs() == jobs
    assert media.read_bytes() == b'paid result fixture'
    assert profile.read_text() == 'browser login fixture'
    with service.connect() as db:
        assert db.execute('select count(*) from console_account_details').fetchone()[0] == 1


def test_resync_does_not_resurrect_removed_account_or_overwrite_new_session_on_restore(service):
    service.account_removal.remove('account_a', True)
    new_session = b'new browser sync'
    (service.cookies_dir / 'account_a.json').write_bytes(new_session)
    assert 'account_a' not in service._cookie_files()
    assert 'account_a' not in {a['id'] for a in service.accounts({'accounts': []})['accounts']}
    service.account_removal.restore('account_a', True)
    assert (service.cookies_dir / 'account_a.json').read_bytes() == new_session


def test_remove_last_session_does_not_reintroduce_engine_cached_accounts(service):
    service.account_removal.remove('account_a', True)
    service.account_removal.remove('account_b', True)
    result = service.accounts({'accounts': [{'account_id': 'acct-a', 'credits': 100, 'status': 'active'}]})
    assert result['accounts'] == []
    assert result['total_credits'] is None


def test_confirmation_and_path_validation_do_not_mutate_records(service):
    before = {p.name: p.read_bytes() for p in service.cookies_dir.iterdir()}
    for key, confirmed, status in [('account_a', False, 422), ('../outside', True, 404), ('account_missing', True, 404)]:
        with pytest.raises(HTTPException) as error:
            service.account_removal.remove(key, confirmed)
        assert error.value.status_code == status
    assert {p.name: p.read_bytes() for p in service.cookies_dir.iterdir()} == before
    assert not service.account_removal.snapshot()


@pytest.mark.parametrize('state', ['queued', 'running', 'refresh', 'browser'])
def test_busy_work_blocks_removal(service, state):
    if state in ('queued', 'running'):
        job = service.enqueue(JobInput(kind='image', prompt='fixture'))
        with service.connect() as db:
            db.execute('update console_jobs set status=? where id=?', (state, job['id']))
    elif state == 'browser':
        service.browser_execution.handoff_active = True
    else:
        asyncio.run(service.balance_refresh_lock.acquire())
    with pytest.raises(HTTPException) as error:
        service.account_removal.remove('account_a', True)
    assert error.value.status_code == 409
    assert (service.cookies_dir / 'account_a.json').is_file()
    assert service.account_removal.snapshot() == []


def test_archive_failure_keeps_original_and_clears_exclusion(service, monkeypatch):
    from pathlib import Path
    def fail_move(*args):
        raise PermissionError('fixture access denied')
    monkeypatch.setattr(Path, 'replace', fail_move)
    with pytest.raises(HTTPException) as error:
        service.account_removal.remove('account_a', True)
    assert error.value.status_code == 503
    assert 'account_a' in service._cookie_files()
    assert service.account_removal.snapshot() == []


def test_http_confirm_delete_restore_and_no_secret_exposure(service):
    service.engine.get_balance = AsyncMock(return_value={'accounts': []})
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        url = '/console/api/accounts/account_a'
        assert client.post(url + '/remove', json={}).status_code == 422
        assert client.post(url + '/remove', json={'confirmed': 'true'}).status_code == 422
        response = client.post(url + '/remove', json={'confirmed': True})
        assert response.status_code == 200
        assert response.json()['recoverable'] is True
        removed = client.get('/console/api/accounts/removed')
        assert len(removed.json()) == 1
        for secret in ('PRIVATE_COOKIE', 'PRIVATE_AT', 'PRIVATE_FSID', 'archive', str(service.engine.root)):
            assert secret not in removed.text + response.text
        assert client.post(url + '/remove', json={'confirmed': True}).status_code == 404
        assert client.post(url + '/restore', json={}).status_code == 422
        assert client.post(url + '/restore', json={'confirmed': True}).status_code == 200
        assert len(client.get('/console/api/accounts').json()['accounts']) == 2
        assert client.get('/console/api/accounts/removed').json() == []
    lan = TestClient(create_lan_app(service, 'fixture-key-' * 4))
    assert lan.post(url + '/remove', headers={'Authorization': 'Bearer ' + 'fixture-key-' * 4},
                    json={'confirmed': True}).status_code == 404


def test_missing_archive_keeps_exclusion_and_reports_clear_error(service):
    service.account_removal.remove('account_a', True)
    next(service.account_removal.archive_dir.iterdir()).unlink()
    with pytest.raises(HTTPException) as error:
        service.account_removal.restore('account_a', True)
    assert error.value.status_code == 404
    assert 'account_a' not in service._cookie_files()
    assert len(service.account_removal.snapshot()) == 1


def test_restore_write_failure_cannot_publish_partial_session(service, monkeypatch):
    service.account_removal.remove('account_a', True)
    def fail_link(*args):
        raise PermissionError('fixture access denied')
    monkeypatch.setattr('flow_agent.account_removal.os.link', fail_link)
    with pytest.raises(HTTPException) as error:
        service.account_removal.restore('account_a', True)
    assert error.value.status_code == 503
    assert not (service.cookies_dir / 'account_a.json').exists()
    assert not list(service.cookies_dir.glob('.restore-*.tmp'))
    assert len(service.account_removal.snapshot()) == 1
    assert next(service.account_removal.archive_dir.iterdir()).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['monitor', 'sync', 'preparation'])
async def test_background_operation_blocks_remove_and_restore(service, operation):
    service.account_removal.remove('account_b', True)
    task = asyncio.create_task(asyncio.sleep(30))
    if operation == 'monitor':
        service.monitor.task = task
    elif operation == 'sync':
        service.account_sync_task = task
    else:
        service.preparation.task = task
    try:
        for action, key in [(service.account_removal.remove, 'account_a'), (service.account_removal.restore, 'account_b')]:
            with pytest.raises(HTTPException) as error:
                action(key, True)
            assert error.value.status_code == 409
        assert 'account_a' in service._cookie_files()
        assert 'account_b' not in service._cookie_files()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
