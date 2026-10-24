"""Isolated-desktop launch path and generation settings defaults."""
import os

import pytest

from flow_agent import isolated_desktop
from flow_agent.browser_visibility import OwnedBrowserVisibility


def test_settings_default_isolated_desktop_on(tmp_path):
    from flow_agent.console import ConsoleService
    from flow_agent.engine import FlowEngine
    from flow_agent.verification_settings import snapshot, configure_generation
    service = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    service.initialize()
    prefs = snapshot(service)
    assert prefs['isolated_desktop'] is True
    configure_generation(service, True, isolated=False)
    assert snapshot(service)['isolated_desktop'] is False


def test_isolated_mode_visibility_info(tmp_path):
    guard = OwnedBrowserVisibility(tmp_path / 'profile', tmp_path / 'chrome.exe', mode='isolated')
    assert guard.info()['mode'] == 'isolated_desktop'
    assert guard.requires_hidden is False
    guard.hide_after_verification()  # no-op, no exception


@pytest.mark.skipif(os.name != 'nt', reason='Win32 desktop API')
def test_spawn_and_attach_on_private_desktop(tmp_path):
    """Real launch on the private desktop: page visible, focused, interactive."""
    chrome = os.environ.get('FLOW_TEST_BROWSER')
    if not chrome or not os.path.isfile(chrome):
        pytest.skip('Set FLOW_TEST_BROWSER for local real-browser integration tests')
    import asyncio

    async def run():
        from playwright.async_api import async_playwright
        spawned = isolated_desktop.spawn(str(tmp_path / 'profile'), chrome,
                                         ['--disable-features=CalculateNativeWinOcclusion'])
        try:
            async with async_playwright() as pw:
                browser, context = await isolated_desktop.attach(pw.chromium, spawned, str(tmp_path / 'profile'))
                page = context.pages[0] if context.pages else await context.new_page()
                await page.evaluate('''() => {
                    const b = document.createElement('button');
                    b.id = 'b';
                    b.onclick = () => { b.innerText = 'ok'; };
                    document.body.appendChild(b);
                }''')
                await page.click('#b')
                assert await page.inner_text('#b') == 'ok'
                state = await page.evaluate('() => document.visibilityState')
                assert state == 'visible'
                await browser.close()
        finally:
            spawned.terminate()
        assert spawned.terminated

    asyncio.run(run())


# --- ProxyForwarder: credential injection, socks5 upstream, full teardown --- #

import asyncio
import base64
import httpx

from flow_agent.isolated_desktop import ProxyForwarder


async def _fake_http_proxy(reader, writer, expect_auth, seen=None):
    line = await reader.readline()
    auth_ok = False
    auth_leaked = None
    expected = b'proxy-authorization: basic ' + base64.b64encode(b'user:pass').lower()
    while True:
        header = await reader.readline()
        if header in (b'\r\n', b'\n', b''):
            break
        if expect_auth and header.lower().startswith(expected):
            auth_ok = True  # base64('user:pass'), case-insensitive
        if header.lower().startswith(b'proxy-authorization:'):
            auth_leaked = header
    if expect_auth:
        assert auth_ok, 'credentials missing'
    else:
        # Through a SOCKS5 tunnel the bytes reach the target server: proxy
        # credentials must never be forwarded there.
        assert auth_leaked is None, f'proxy credentials leaked to target: {auth_leaked!r}'
        if seen is not None:
            seen['auth_header'] = auth_leaked
    body = b'fake-body'
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
    await writer.drain()


async def _fake_socks5_proxy(reader, writer, seen=None):
    greeting = await reader.readexactly(3)
    assert greeting[0] == 5 and greeting[2] == 2  # user/pass method offered
    writer.write(b'\x05\x02')
    await writer.drain()
    ver = await reader.readexactly(1)  # subnegotiation version 0x01
    ulen = (await reader.readexactly(1))[0]
    user = await reader.readexactly(ulen)
    plen = (await reader.readexactly(1))[0]
    pwd = await reader.readexactly(plen)
    assert ver == b'\x01'
    assert (user, pwd) == (b'user', b'pass')
    writer.write(b'\x05\x00')
    await writer.drain()
    head = await reader.readexactly(4)
    assert head[:2] == b'\x05\x01'
    dlen = (await reader.readexactly(1))[0]
    await reader.readexactly(dlen)
    await reader.readexactly(2)
    writer.write(b'\x05\x00\x00\x01' + b'\x00' * 6)
    await writer.drain()
    await _fake_http_proxy(reader, writer, expect_auth=False)


def test_forwarder_injects_credentials_and_teardown_kills_tunnels():
    asyncio.run(_forwarder_injects_credentials_and_teardown_kills_tunnels())


async def _forwarder_injects_credentials_and_teardown_kills_tunnels():
    for scheme in ('http', 'socks5'):
        events = []

        async def upstream(reader, writer, scheme=scheme):
            if scheme == 'socks5':
                await _fake_socks5_proxy(reader, writer)
            else:
                await _fake_http_proxy(reader, writer, expect_auth=True)
            events.append('served')

        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        raw = {'http': f'http://user:pass@127.0.0.1:{port}',
               'socks5': f'socks5://user:pass@127.0.0.1:{port}'}[scheme]
        forwarder = ProxyForwarder(raw)
        relay_port = await forwarder.start()
        try:
            async with httpx.AsyncClient(proxy=f'http://127.0.0.1:{relay_port}',
                                         trust_env=False, timeout=8) as client:
                response = await client.get('http://target.test/hello')
                assert response.text == 'fake-body'
                assert events == ['served']
                # close() must tear down active tunnels: after close the relay
                # listener is gone and any held connection is terminated.
                forwarder.close()
                with pytest.raises((httpx.ConnectError, httpx.ProxyError, httpx.TransportError)):
                    await client.get('http://target.test/hello')
        finally:
            forwarder.close()
            server.close()


def test_socks5_tunnel_strips_browser_proxy_auth():
    """A browser-sent Proxy-Authorization header must be stripped in the tunnel."""
    asyncio.run(_socks5_tunnel_strips_browser_proxy_auth())


async def _socks5_tunnel_strips_browser_proxy_auth():
    seen = {}

    async def upstream(reader, writer):
        await _fake_socks5_proxy(reader, writer, seen)

    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    forwarder = ProxyForwarder(f'socks5://user:pass@127.0.0.1:{port}')
    relay_port = await forwarder.start()
    reader, writer = await asyncio.open_connection('127.0.0.1', relay_port)
    try:
        # The browser carries a stale proxy credential; the tunnel must strip it.
        writer.write(b'GET http://target.test/hello HTTP/1.1\r\n'
                     b'Host: target.test\r\n'
                     b'Proxy-Authorization: Basic c3RhbGxlLWNyZWRlbnRpYWw=\r\n'
                     b'\r\n')
        await writer.drain()
        response = await asyncio.wait_for(reader.read(500), 5)
        assert b'fake-body' in response
        assert seen.get('auth_header') is None
    finally:
        forwarder.close()
        writer.close()
        server.close()


def test_forwarder_close_breaks_active_tunnel():
    asyncio.run(_forwarder_close_breaks_active_tunnel())


def test_close_reaches_stalled_handshake_connection():
    asyncio.run(_close_reaches_stalled_handshake_connection())


async def _close_reaches_stalled_handshake_connection():
    """An upstream connection stuck in its handshake must be reachable by close()."""
    upstream_connected = asyncio.Event()
    upstream_eof = asyncio.Event()

    async def stalled_upstream(reader, writer):
        upstream_connected.set()
        try:
            await reader.readexactly(2)  # greeting, deliberately never answered
            while True:
                chunk = await reader.read(10)
                if chunk == b'':
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        upstream_eof.set()  # EOF (or protocol abort) means the relay released us

    server = await asyncio.start_server(stalled_upstream, '127.0.0.1', 0)
    forwarder = ProxyForwarder(f'socks5://user:pass@127.0.0.1:{server.sockets[0].getsockname()[1]}')
    relay_port = await forwarder.start()
    reader, writer = await asyncio.open_connection('127.0.0.1', relay_port)
    try:
        writer.write(b'CONNECT slow.test:443 HTTP/1.1\r\nHost: slow.test\r\n\r\n')
        await writer.drain()
        await asyncio.wait_for(upstream_connected.wait(), 5)
        forwarder.close()  # must reach the not-yet-registered-looking connection
        await asyncio.wait_for(upstream_eof.wait(), 5)
    finally:
        forwarder.close()
        writer.close()
        server.close()


def test_handshake_timeout_closes_stalled_upstream(monkeypatch):
    asyncio.run(_handshake_timeout_closes_stalled_upstream(monkeypatch))


async def _handshake_timeout_closes_stalled_upstream(monkeypatch):
    monkeypatch.setattr(ProxyForwarder, 'HANDSHAKE_TIMEOUT', 0.5)
    upstream_eof = asyncio.Event()

    async def stalled_upstream(reader, writer):
        try:
            await reader.readexactly(2)  # greeting, deliberately never answered
            while True:
                chunk = await reader.read(10)
                if chunk == b'':
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        upstream_eof.set()  # EOF (or protocol abort) means the relay released us

    server = await asyncio.start_server(stalled_upstream, '127.0.0.1', 0)
    forwarder = ProxyForwarder(f'socks5://user:pass@127.0.0.1:{server.sockets[0].getsockname()[1]}')
    relay_port = await forwarder.start()
    reader, writer = await asyncio.open_connection('127.0.0.1', relay_port)
    try:
        writer.write(b'CONNECT slow.test:443 HTTP/1.1\r\nHost: slow.test\r\n\r\n')
        await writer.drain()
        # The handshake timeout ends the stalled attempt quickly: the browser
        # connection closes and the upstream is released.
        await asyncio.wait_for(upstream_eof.wait(), 5)
        data = await asyncio.wait_for(reader.read(100), 3)
        assert data == b''
    finally:
        forwarder.close()
        writer.close()
        server.close()


async def _forwarder_close_breaks_active_tunnel():
    """close() must terminate a held tunnel, not just stop the listener."""
    held = asyncio.Event()

    async def upstream_hold(reader, writer):
        await reader.readline()
        while True:
            header = await reader.readline()
            if header in (b'\r\n', b'\n', b''):
                break
        writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        await writer.drain()
        held.set()
        await asyncio.Event().wait()  # hold the tunnel open

    server = await asyncio.start_server(upstream_hold, '127.0.0.1', 0)
    forwarder = ProxyForwarder(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}')
    relay_port = await forwarder.start()
    reader, writer = await asyncio.open_connection('127.0.0.1', relay_port)
    try:
        writer.write(b'CONNECT slow.test:443 HTTP/1.1\r\nHost: slow.test\r\n\r\n')
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 5)
        assert b'200' in status
        await asyncio.wait_for(held.wait(), 5)
        forwarder.close()
        # EOF: the active tunnel was torn down (a buffered header byte may
        # still flush first, so drain until EOF).
        while True:
            data = await asyncio.wait_for(reader.read(100), 3)
            if data == b'':
                break
    finally:
        forwarder.close()
        writer.close()
        server.close()
