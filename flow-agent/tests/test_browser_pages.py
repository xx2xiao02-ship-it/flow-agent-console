import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from flow_agent.console import ConsoleService, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.browser_pages import BrowserLifetime
import psutil


@pytest.fixture
def service(tmp_path):
    result = ConsoleService(FlowEngine(root=tmp_path))
    result.initialize()
    result.browser_pages.close_timeout = 0.03
    return result


@pytest.mark.parametrize('count', [0, 1, 2])
def test_actual_browser_requires_unique_exact_binary_and_profile(service, monkeypatch, tmp_path, count):
    lease = service.browser_pages.begin('account_a')
    launcher = object()
    lease.process = launcher
    lease.browser_path = (tmp_path / 'browser/chrome.exe').resolve()
    lease.browser_profile = (tmp_path / 'profile').resolve()

    class Process:
        info = {'name': 'chrome.exe'}
        def __init__(self, exe, args): self.binary, self.args = exe, args
        def exe(self): return str(self.binary)
        def cmdline(self): return self.args
        def create_time(self): return 1
        def is_running(self): return True

    matches = [Process(lease.browser_path, ['--user-data-dir', str(lease.browser_profile)]) for _ in range(count)]
    others = [
        Process(tmp_path / 'other/chrome.exe', ['--user-data-dir=' + str(lease.browser_profile)]),
        Process(lease.browser_path, ['--user-data-dir=' + str(tmp_path / 'other-profile')]),
        Process(lease.browser_path, ['--user-data-dir=' + str(lease.browser_profile), '--type=renderer']),
    ]
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: others + matches)
    actual = service.browser_pages._actual_process(lease)
    if count == 1:
        assert isinstance(actual, BrowserLifetime) and actual.process is matches[0]
        assert actual.poll() is None
    else:
        assert actual is None  # Never use a delegated launcher to prove exit.


@pytest.mark.parametrize('outcome,expected', [('alive', None), ('exited', 0), ('reused', 0), ('denied', None)])
def test_actual_browser_lifetime_fails_closed_on_access_denied(outcome, expected):
    class Process:
        def create_time(self): return 1
        def is_running(self):
            if outcome == 'denied': raise psutil.AccessDenied(pid=123)
            # psutil.is_running compares creation time for a reused PID.
            return outcome == 'alive'
    assert BrowserLifetime(Process()).poll() == expected


def test_setting_defaults_on_and_persists_without_changing_jobs(service):
    pages = service.browser_pages
    assert pages.enabled()
    pages.configure(False)
    assert pages.begin('account_a') is None
    assert pages.launch_url('account_a') == 'https://labs.google/fx/tools/flow'
    assert not ConsoleService(service.engine).browser_pages.enabled()
    assert not service.list_jobs()


def test_only_explicit_preparation_launch_receives_a_unique_one_use_marker(service):
    pages = service.browser_pages
    a, b = pages.begin('account_a'), pages.begin('account_b')
    assert a.token != b.token
    assert a.token not in str(service.preparation.snapshot())
    assert a.token not in pages.launch_url('account_b')
    assert a.token in pages.launch_url('account_a')
    assert a.token not in pages.launch_url('account_a')
    assert a.token not in pages.launch_url('account_c')


@pytest.mark.asyncio
async def test_failed_check_never_authorizes_close(service):
    pages = service.browser_pages
    lease = pages.begin('account_a')
    result = await pages.finish(lease, False)
    assert result['state'] == 'retained' and lease.action == 'retain'
    assert '登录' in result['message']


@pytest.mark.asyncio
async def test_unconfirmed_close_revokes_late_permission(service):
    pages = service.browser_pages
    lease = pages.begin('account_a')
    result = await pages.finish(lease, True)
    assert result['state'] == 'unconfirmed'
    assert lease.action == 'retain'


@pytest.mark.asyncio
async def test_setting_disabled_during_check_retains_page(service):
    pages = service.browser_pages
    lease = pages.begin('account_a')
    pages.configure(False)
    result = await pages.finish(lease, True)
    assert result['state'] == 'retained' and lease.action == 'retain'


@pytest.mark.parametrize('ack', ['closed', 'retained'])
def test_channel_requires_actual_tab_acknowledgement(service, ack):
    app = FastAPI()
    app.include_router(create_console_router(service))
    lease = service.browser_pages.begin('account_a')
    lease.action = 'close'
    with TestClient(app) as client:
        with client.websocket_connect('/console/api/accounts/preparation/page/' + lease.token,
                                      headers={'origin': 'chrome-extension://' + 'a' * 32}) as ws:
            assert ws.receive_json() == {'action': 'close'}
            ws.send_text('ready')
            assert ws.receive_json() == {'action': 'commit_close'}
            ws.send_text(ack)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert lease.result == ack and lease.done.is_set()


@pytest.mark.parametrize('handshake,initially_alive,expected', [(True, True, 'closed'),
                                                              (True, False, None),
                                                              (False, True, None)])
def test_last_tab_exit_requires_handshake_and_own_live_launch_handle(service, handshake, initially_alive, expected):
    class Launch:
        calls = 0
        def poll(self):
            self.calls += 1
            return None if initially_alive and self.calls == 1 else 0
    app = FastAPI()
    app.include_router(create_console_router(service))
    lease = service.browser_pages.begin('account_a')
    lease.action = 'close'
    lease.process = Launch()
    with TestClient(app) as client:
        with client.websocket_connect('/console/api/accounts/preparation/page/' + lease.token,
                                      headers={'origin': 'chrome-extension://' + 'a' * 32}) as ws:
            assert ws.receive_json() == {'action': 'close'}
            if handshake:
                ws.send_text('ready')
                assert ws.receive_json() == {'action': 'commit_close'}
            ws.close()
    assert lease.result == expected
    assert lease.done.is_set() is (expected == 'closed')


@pytest.mark.parametrize('windows,tabs', [(0, 0), (1, 3)])
def test_close_ack_preserves_only_public_remaining_window_counts(service, windows, tabs):
    import json
    app = FastAPI()
    app.include_router(create_console_router(service))
    lease = service.browser_pages.begin('account_a')
    lease.action = 'close'
    with TestClient(app) as client:
        with client.websocket_connect('/console/api/accounts/preparation/page/' + lease.token,
                                      headers={'origin': 'chrome-extension://' + 'a' * 32}) as ws:
            assert ws.receive_json() == {'action': 'close'}
            ws.send_text('ready')
            assert ws.receive_json() == {'action': 'commit_close'}
            ws.send_text(json.dumps({'state': 'closed', 'remaining_windows': windows, 'remaining_tabs': tabs}))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert lease.result == 'closed'
    assert lease.remaining_windows == windows and lease.remaining_tabs == tabs


def test_last_app_tab_teardown_requires_exact_owned_process_exit(service):
    class Launch:
        calls = 0
        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 0
    app = FastAPI()
    app.include_router(create_console_router(service))
    lease = service.browser_pages.begin('account_a')
    lease.action = 'close'
    lease.process = Launch()
    with TestClient(app) as client:
        with client.websocket_connect('/console/api/accounts/preparation/page/' + lease.token,
                                      headers={'origin':'chrome-extension://'+'a'*32}) as ws:
            assert ws.receive_json() == {'action':'close'}
            ws.send_text('ready')
            assert ws.receive_json() == {'action':'commit_close'}
            ws.send_text('retained')
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert lease.result == 'closed'
    assert lease.remaining_windows == lease.remaining_tabs == 0


@pytest.mark.asyncio
async def test_remaining_windows_are_not_described_as_confirmed_browser_exit(service):
    lease = service.browser_pages.begin('account_a')
    lease.result = 'closed'
    lease.remaining_windows, lease.remaining_tabs = 3, 3
    lease.done.set()
    result = await service.browser_pages.finish(lease, True)
    assert '仍有 3 个窗口' in result['message']
    assert '未确认整个浏览器退出' in result['message']


@pytest.mark.asyncio
async def test_disconnect_while_sending_final_close_is_not_an_application_error(service):
    from types import SimpleNamespace
    lease = service.browser_pages.begin('account_a')
    lease.action = 'close'
    class Socket:
        client = SimpleNamespace(host='testclient')
        headers = {'origin':'chrome-extension://'+'a'*32}
        async def accept(self): pass
        async def send_json(self, value): pass
        async def receive_text(self): return 'retained'
        async def close(self, **kwargs): raise WebSocketDisconnect(code=1006)
    await service.browser_pages.channel(Socket(),lease.token)
    assert lease.done.is_set() and lease.result == 'retained'


@pytest.mark.parametrize('origin,valid', [('https://evil.example', True), ('null', True),
                                       ('chrome-extension://' + 'a' * 32, False)])
def test_foreign_origin_or_unknown_nonce_is_rejected(service, origin, valid):
    app = FastAPI()
    app.include_router(create_console_router(service))
    lease = service.browser_pages.begin('account_a')
    token = lease.token if valid else 'b' * 64
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/console/api/accounts/preparation/page/' + token,
                                      headers={'origin': origin}):
            pass
    assert lease.result is None and not lease.done.is_set()


@pytest.mark.asyncio
async def test_generation_reopens_only_preparation_closed_accounts(service, monkeypatch):
    opened = []
    async def open_browser(account_id):
        opened.append(account_id)
        service.preparation.synced.add(account_id)
    monkeypatch.setattr(service, 'open_account_browser', open_browser)
    await service.browser_pages.restore_for_generation('account_b')
    assert not opened
    with service.connect() as db:
        db.execute("insert into console_meta values ('preparation_page_closed:account_a', '1')")
    await service.browser_pages.restore_for_generation('account_a')
    await service.browser_pages.restore_for_generation('account_a')
    assert opened == ['account_a'] and not service.list_jobs()
