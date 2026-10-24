"""Offline bridge ownership, account binding and generation replay guards."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from flow_agent.console import ConsoleService, create_console_router, safe_failure
from flow_agent.engine import FlowEngine, EngineError


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = FlowEngine(root=tmp_path / 'flow-agent')
    service = ConsoleService(engine)
    service.initialize()
    service.cookies_dir.mkdir()
    cookie = service.cookies_dir / 'account_a.json'
    cookie.write_text('{}')
    events = []
    async def start():
        assert not service.browser_execution.handoff_active
        events.append('sync-start')
        return {'state': 'running'}
    async def open_browser(account, *, background=False):
        assert account == 'account_a'
        assert background is True
        events.append('open-selected')
        service.preparation.synced.add(account)
    async def stop():
        assert service.browser_execution.handoff_active
        events.append('sync-stop')
    async def finish(lease, success):
        assert lease and lease.account_id == 'account_a'
        events.append('close-owned' if success else 'retain-owned')
    monkeypatch.setattr(service, 'start_bridge', start)
    monkeypatch.setattr(service, 'open_account_browser', open_browser)
    monkeypatch.setattr(service.browser_execution, '_stop_owned_bridge', stop)
    monkeypatch.setattr(service.browser_pages, 'finish', finish)
    return service, cookie, events


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_handoff_binds_account_and_restores_sync_after_one_spawn(runtime, fail):
    service, cookie, events = runtime
    async def spawn(*args):
        events.append('spawn')
        assert service.browser_execution.handoff_active
        assert args[-2:] == ('--captcha', 'browser')
        assert args[args.index('--cookies') + 1] == str(cookie)
        if fail:
            raise EngineError([], 1, '', 'PUBLIC_ERROR_UNUSUAL_ACTIVITY')
        return '{}', ''
    if fail:
        with pytest.raises(EngineError):
            await service.browser_execution.run(['image', 'cat', '--cookies', str(cookie)], spawn)
    else:
        assert await service.browser_execution.run(['image', 'cat', '--cookies', str(cookie)], spawn) == ('{}', '')
    assert events == ['sync-start', 'open-selected', 'sync-stop', 'spawn', 'close-owned',
                      'sync-start']
    assert not service.browser_execution.lock.locked()


@pytest.mark.asyncio
async def test_existing_extension_needs_no_duplicate_cookie_sync_log(runtime, monkeypatch):
    service, cookie, events = runtime
    async def open_existing(account, **kwargs):
        lease = next(p for p in service.browser_pages.leases.values() if p.account_id == account)
        lease.ever_connected = True
        events.append('owned-page-connected')
    monkeypatch.setattr(service, 'open_account_browser', open_existing)
    service.browser_execution.wait_seconds = 0
    async def spawn(*args):
        assert args[-2:] == ('--captcha', 'browser')
        assert args[args.index('--cookies') + 1] == str(cookie)
        events.append('native-strict-account-check')
        return '{}', ''
    await service.browser_execution.run(['browser-check', '--cookies', str(cookie)], spawn)
    assert 'account_a' not in service.preparation.synced
    assert events[-3:] == ['native-strict-account-check', 'close-owned', 'sync-start']


@pytest.mark.asyncio
async def test_missing_sync_stops_before_generation(runtime, monkeypatch):
    service, cookie, events = runtime
    async def open_without_sync(_, **kwargs):
        pass
    monkeypatch.setattr(service, 'open_account_browser', open_without_sync)
    service.browser_execution.wait_seconds = 0
    async def never_spawn(*args):
        pytest.fail('generation must not be spawned without selected browser sync')
    with pytest.raises(EngineError) as caught:
        await service.browser_execution.run(['image', 'cat', '--cookies', str(cookie)], never_spawn)
    assert '未提交生成' in safe_failure(caught.value)
    assert events == ['sync-start', 'retain-owned']


@pytest.mark.asyncio
@pytest.mark.parametrize('exc', [EngineError([], -1, '', 'timeout'), asyncio.CancelledError()])
async def test_timeout_and_cancel_release_owned_pages_before_restoring_bridge(runtime, exc):
    service, cookie, events = runtime
    async def spawn(*args):
        raise exc
    with pytest.raises(type(exc)):
        await service.browser_execution.run(['image', 'cat', '--cookies', str(cookie)], spawn)
    assert events[-2:] == ['close-owned', 'sync-start']
    assert not service.browser_execution.handoff_active
    assert not service.browser_execution.lock.locked()


@pytest.mark.asyncio
async def test_real_job_cleanup_ack_is_persisted_without_private_launch_data(runtime, monkeypatch):
    service, cookie, _ = runtime
    async def finish(lease, checked):
        assert checked
        return {'state': 'closed', 'remaining_windows': 3, 'remaining_tabs': 3}
    monkeypatch.setattr(service.browser_pages, 'finish', finish)
    async def spawn(*args):
        raise EngineError([], 1, '', 'PUBLIC_ERROR_UNUSUAL_ACTIVITY')
    with pytest.raises(EngineError):
        await service.browser_execution.run(['image', 'cat', '--cookies', str(cookie)], spawn)
    with service.connect() as db:
        value = db.execute("select value from console_meta where key='last_browser_execution_cleanup'").fetchone()[0]
    record = json.loads(value)
    assert record['account_id'] == 'account_a' and record['checked_at']
    assert record['cleanup'] == {'state': 'closed', 'remaining_windows': 3, 'remaining_tabs': 3}
    assert all(lease.token not in value for lease in service.browser_pages.leases.values())
    assert str(cookie) not in value and 'PRIVATE' not in value


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_hide_generation_failure(runtime, monkeypatch):
    service, cookie, events = runtime
    async def broken_finish(*args):
        raise OSError('close channel interrupted')
    monkeypatch.setattr(service.browser_pages, 'finish', broken_finish)
    async def spawn(*args):
        raise EngineError([], 1, '', 'PUBLIC_ERROR_UNUSUAL_ACTIVITY')
    with pytest.raises(EngineError, match='PUBLIC_ERROR_UNUSUAL_ACTIVITY'):
        await service.browser_execution.run(['image', 'cat', '--cookies', str(cookie)], spawn)
    assert events[-1] == 'sync-start'
    assert service.browser_execution.last_cleanup['state'] == 'unconfirmed'


@pytest.mark.asyncio
async def test_no_auto_close_still_scopes_owned_generation_tabs(runtime):
    service, cookie, events = runtime
    service.browser_pages.configure(False)
    async def spawn(*args):
        assert service.browser_pages.leases
        return '{}', ''
    await service.browser_execution.run(['browser-check', '--cookies', str(cookie)], spawn)


@pytest.mark.asyncio
async def test_generation_and_browser_check_are_serialized(runtime):
    service, cookie, events = runtime
    in_flight = 0
    async def spawn(*args):
        nonlocal in_flight
        in_flight += 1
        assert in_flight == 1
        await asyncio.sleep(.01)
        in_flight -= 1
        return '{}', ''
    await asyncio.gather(*(service.browser_execution.run(['browser-check', '--cookies', str(cookie)], spawn) for _ in range(2)))


@pytest.mark.asyncio
async def test_foreign_bridge_listener_is_not_terminated(tmp_path, monkeypatch):
    service = ConsoleService(FlowEngine(root=tmp_path))
    monkeypatch.setattr(service, '_bridge_port_open', lambda: True)
    with pytest.raises(EngineError) as caught:
        await service.browser_execution._stop_owned_bridge()
    assert caught.value.stderr == 'BROWSER_BRIDGE_OCCUPIED'


@pytest.mark.asyncio
async def test_readonly_probe_sets_network_generation_block_and_no_replay(tmp_path, monkeypatch):
    engine = FlowEngine(root=tmp_path, binary=Path(__file__))
    recorded = {}
    class Child:
        returncode = 0
        async def communicate(self):
            return b'{}', b''
    async def spawn(*args, **kwargs):
        recorded.update(kwargs)
        return Child()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    await engine._spawn_run('browser-check', browser=True)
    assert recorded['env']['FLOW_BLOCK_GENERATION'] == '1'
    assert recorded['env']['FLOW_NO_SUBMISSION_REPLAY'] == '1'
    assert recorded['env']['FLOW_AUTO_RETRY'] == '0'


def test_browser_check_endpoint_returns_only_public_probe_fields(runtime, monkeypatch):
    service, cookie, events = runtime
    async def check(*args, **kwargs):
        assert args[0] == 'browser-check'
        return json.dumps({'generation_submitted': False, 'token_minted': False,
                           'captcha_provider': 'flow.captcha', 'browser_connected': True,
                           'verification_client_loaded': True, 'token': 'PRIVATE'}), ''
    monkeypatch.setattr(service.engine, '_run', check)
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        result = client.post('/console/api/accounts/account_a/browser-check')
    assert result.status_code == 200
    assert 'PRIVATE' not in result.text and 'token"' not in result.text
    assert result.json()['generation_submitted'] is False


@pytest.mark.asyncio
async def test_extension_refresh_targets_only_exact_installed_path_once(runtime, monkeypatch, tmp_path):
    service, _, _ = runtime
    profile = tmp_path / 'profile'
    (profile / 'Default').mkdir(parents=True)
    extension = tmp_path / 'flow-extension'
    extension.mkdir()
    (extension / 'refresh.html').write_text('refresh')
    valid = 'a' * 32
    (profile / 'Default/Secure Preferences').write_text(json.dumps({'extensions': {'settings': {
        valid: {'path': str(extension)}, 'b'*32: {'path': str(tmp_path/'other-extension')}
    }}}))
    calls = []
    monkeypatch.setattr('flow_agent.console.subprocess.Popen', lambda args, **kwargs: calls.append(args))
    async def no_delay(_): pass
    monkeypatch.setattr('flow_agent.console.asyncio.sleep', no_delay)
    refresh = await service._refresh_account_extension(Path('chrome.exe'), profile, extension)
    assert await service._refresh_account_extension(Path('chrome.exe'), profile, extension) is None
    assert not calls  # Never create an independent refresh-only window.
    assert refresh == f'chrome-extension://{valid}/refresh.html'


@pytest.mark.asyncio
async def test_normal_chrome_refresh_keeps_profile_without_load_switch(runtime, monkeypatch, tmp_path):
    service, _, _ = runtime
    root = tmp_path / 'User Data'
    profile = root / 'Profile 2'
    profile.mkdir(parents=True)
    extension = tmp_path / 'flow-extension'
    extension.mkdir()
    (extension / 'refresh.html').write_text('refresh')
    (profile / 'Secure Preferences').write_text(json.dumps({'extensions': {'settings': {
        'a'*32: {'path': str(extension)}
    }}}))
    calls = []
    monkeypatch.setattr('flow_agent.console.subprocess.Popen', lambda args, **kwargs: calls.append(args))
    async def no_delay(_): pass
    monkeypatch.setattr('flow_agent.console.asyncio.sleep', no_delay)
    refresh = await service._refresh_account_extension(Path('chrome.exe'), profile, extension,
                                            user_data_dir=root, profile_directory='Profile 2')
    assert not calls
    assert refresh == f"chrome-extension://{'a'*32}/refresh.html"


@pytest.mark.asyncio
async def test_background_generation_uses_single_app_window_without_restoring_tabbed_startup(runtime, monkeypatch):
    service, _, _ = runtime
    profile = service.engine.root / 'data/flow_browser_profile_1'
    profile.mkdir(parents=True, exist_ok=True)
    extension = service.engine.root.parent / 'flow-extension'
    extension.mkdir(exist_ok=True)
    (extension / 'manifest.json').write_text('{}')
    (extension / 'refresh.html').write_text('refresh')
    (profile / 'Preferences').write_text(json.dumps({'extensions': {'settings': {
        'a'*32: {'path': str(extension)}
    }}}))
    monkeypatch.setattr(service, '_account_profile_name', lambda _: profile.name)
    monkeypatch.setattr(service, '_find_extension_browser', lambda: Path('chrome.exe'))
    calls = []
    child = SimpleNamespace(poll=lambda: None)
    def launch(args, **kwargs):
        calls.append(args)
        return child
    monkeypatch.setattr('flow_agent.console.subprocess.Popen', launch)
    lease = service.browser_pages.begin('account_a', force=True)
    await ConsoleService.open_account_browser(service, 'account_a', background=True)
    assert len(calls) == 1
    assert calls[0][-1].startswith('--app=https://labs.google/fx/tools/flow#flow-console-page=')
    assert '--new-window' not in calls[0]
    assert not any(a.startswith('chrome-extension://') for a in calls[0])
    assert '--window-position=-32000,-32000' in calls[0] and '--disable-background-mode' in calls[0]
    await lease.visibility.stop()
    assert lease.process is child


@pytest.mark.asyncio
@pytest.mark.parametrize('reasons', [[1 << 24], 1 << 24])
async def test_disabled_developer_extension_explains_manual_enable_without_pref_edits(runtime, monkeypatch, tmp_path, reasons):
    service, _, _ = runtime
    profile = tmp_path / 'profile'
    (profile / 'Default').mkdir(parents=True)
    extension = tmp_path / 'flow-extension'
    extension.mkdir()
    (extension / 'refresh.html').write_text('refresh')
    preferences = profile / 'Default/Secure Preferences'
    preferences.write_text(json.dumps({'extensions': {'settings': {
        'a'*32: {'path': str(extension), 'disable_reasons': reasons}
    }}}))
    original = preferences.read_bytes()
    calls = []
    monkeypatch.setattr('flow_agent.console.subprocess.Popen', lambda args, **kwargs: calls.append(args))
    with pytest.raises(HTTPException) as error:
        await service._refresh_account_extension(Path('chrome.exe'), profile, extension)
    assert error.value.status_code == 409
    assert '开发者模式' in error.value.detail and '未提交生成' in error.value.detail
    assert calls[0][-1] == 'chrome://extensions/'
    assert preferences.read_bytes() == original
    assert not service.extension_refreshed_profiles
