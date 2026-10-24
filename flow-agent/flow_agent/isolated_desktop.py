"""Alternate Win32 desktop for generation browsers.

The dedicated Chrome for a generation task is started on a private desktop
(CreateDesktopW + CreateProcess lpDesktop), so it never appears on the user's
current desktop and never takes focus there, while the page itself still
reports a fully visible, focused state. Playwright attaches over CDP; request
gating, downloads and cleanup behave exactly as before (verified by probe).
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
from ctypes import wintypes
import time
from pathlib import Path

DESKTOP_NAME = 'FlowAgentAuto'
LAUNCH_TIMEOUT = 40

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
                                    wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
                                    wintypes.LPVOID, wintypes.LPVOID]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR), ('lpDesktop', wintypes.LPWSTR),
                ('lpTitle', wintypes.LPWSTR), ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
                ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD), ('dwXCountChars', wintypes.DWORD),
                ('dwYCountChars', wintypes.DWORD), ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD), ('lpReserved2', ctypes.c_void_p),
                ('hStdInput', wintypes.HANDLE), ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
                ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]


_desktop_handle: int | None = None
_desktop_failed = False


def _ensure_desktop() -> int:
    """Create the private desktop once per process; a stale handle is fine."""
    global _desktop_handle, _desktop_failed
    if _desktop_failed:
        raise OSError('independent desktop unavailable')
    if _desktop_handle:
        return _desktop_handle
    handle = user32.CreateDesktopW(DESKTOP_NAME, None, None, 0, 0x000F01FF, None)
    if not handle:
        _desktop_failed = True
        raise OSError(f'CreateDesktopW failed: {kernel32.GetLastError()}')
    _desktop_handle = handle
    return handle


class IsolatedBrowser:
    """The spawned Chrome process; terminate() is the hard cleanup fallback."""

    def __init__(self, process_handle: int, thread_handle: int, pid: int):
        self.process_handle, self.thread_handle, self.pid = process_handle, thread_handle, pid
        self.terminated = False
        self.forwarder: 'ProxyForwarder | None' = None

    def terminate(self):
        if not self.terminated and self.process_handle:
            kernel32.TerminateProcess(self.process_handle, 1)
            self.terminated = True
        if self.forwarder:
            self.forwarder.close()
            self.forwarder = None


class ProxyForwarder:
    """Local relay that injects proxy credentials towards one fixed upstream proxy.

    Chromium cannot take proxy credentials from --proxy-server, and on the
    private desktop its auth prompt would hang forever. The browser talks to
    127.0.0.1 without auth; this relay forwards to the configured upstream with
    credentials applied. Upstream schemes: http, https (TLS to the proxy) and
    socks5 (with optional user/pass auth). close() also tears down every
    active tunnel, not just the listener.
    """

    def __init__(self, raw_url: str):
        import ssl as _ssl  # noqa: F401  (kept alongside the scheme handling)
        from urllib.parse import urlsplit
        parts = urlsplit(raw_url if '://' in raw_url else 'http://' + raw_url)
        self.scheme = parts.scheme if parts.scheme in ('http', 'https', 'socks5') else 'http'
        self.host = parts.hostname
        self.port = parts.port or {'https': 443, 'socks5': 1080}.get(self.scheme, 80)
        self.username, self.password = parts.username or '', parts.password or ''
        self.server = None
        self._writers: set = set()

    def _auth_header(self) -> str:
        if not self.username:
            return ''
        token = base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
        return f'Proxy-Authorization: Basic {token}\r\n'

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, '127.0.0.1', 0)
        return self.server.sockets[0].getsockname()[1]

    def close(self):
        if self.server:
            self.server.close()
            self.server = None
        # Tear down active tunnels too: closing the writers ends the relays.
        for writer in list(self._writers):
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass
        self._writers.clear()

    def _track(self, *writers):
        for writer in writers:
            self._writers.add(writer)

    def _untrack(self, *writers):
        for writer in writers:
            self._writers.discard(writer)

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError):
            pass

    CONNECT_TIMEOUT = 15
    HANDSHAKE_TIMEOUT = 15

    async def _open_upstream(self):
        """TCP/TLS connect to the upstream proxy only — no protocol I/O.

        The caller registers the writer for teardown BEFORE any handshake runs,
        so a stalled proxy can never leave an untracked connection behind.
        """
        if self.scheme in ('http', 'https'):
            import ssl
            ssl_ctx = ssl.create_default_context() if self.scheme == 'https' else None
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=ssl_ctx), self.CONNECT_TIMEOUT)
            return reader, writer
        if self.scheme == 'socks5':
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.CONNECT_TIMEOUT)
            return reader, writer
        raise OSError(f'unsupported proxy scheme: {self.scheme}')

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         target_host: str, target_port: int):
        """Protocol negotiation; proxy credentials never reach the target."""
        if self.scheme == 'socks5':
            greeting = b'\x05\x01\x02' if self.username else b'\x05\x01\x00'
            writer.write(greeting)
            await writer.drain()
            chosen = await reader.readexactly(2)
            if chosen[0] != 5:
                raise OSError('socks5 greeting rejected')
            if chosen[1] == 2:
                if not self.username:
                    raise OSError('socks5 proxy requires credentials')
                reply = (bytes([1, len(self.username)]) + self.username.encode() +
                         bytes([len(self.password)]) + self.password.encode())
                writer.write(reply)
                await writer.drain()
                auth = await reader.readexactly(2)
                if auth[1] != 0:
                    raise OSError('socks5 credential check failed')
            elif chosen[1] != 0:
                raise OSError('socks5 method rejected')
            addr = target_host.encode()
            writer.write(b'\x05\x01\x00\x03' + bytes([len(addr)]) + addr + int(target_port).to_bytes(2, 'big'))
            await writer.drain()
            head = await reader.readexactly(4)
            if head[1] != 0:
                raise OSError(f'socks5 CONNECT failed: {head[1]}')
            atyp = head[3]
            if atyp == 1:
                await reader.readexactly(4)
            elif atyp == 3:
                await reader.readexactly((await reader.readexactly(1))[0])
            elif atyp == 4:
                await reader.readexactly(16)
            await reader.readexactly(2)
        else:
            writer.write(f'CONNECT {target_host}:{target_port} HTTP/1.1\r\n'
                         f'Host: {target_host}:{target_port}\r\n{self._auth_header()}\r\n'.encode())
            await writer.drain()
            status = await reader.readline()
            while True:
                header = await reader.readline()
                if header in (b'\r\n', b'\n', b''):
                    break
            if b' 200' not in status:
                raise OSError('proxy CONNECT refused')

    async def _tunnel(self, browser_reader, browser_writer, upstream_reader, upstream_writer):
        self._track(browser_writer, upstream_writer)
        try:
            await asyncio.gather(self._relay(browser_reader, upstream_writer),
                                 self._relay(upstream_reader, browser_writer))
        finally:
            self._untrack(browser_writer, upstream_writer)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._track(writer)
        upstream_writer = None
        try:
            line = await reader.readline()
            if not line:
                return
            if line.startswith(b'CONNECT'):
                target = line.split()[1].decode('latin1') if len(line.split()) > 1 else ''
                while (header := await reader.readline()) not in (b'\r\n', b'\n', b''):
                    pass
                host, _, port = target.partition(':')
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    self._open_upstream(), self.CONNECT_TIMEOUT)
                # Registered before any handshake I/O so close() can always
                # reach a stalled connection.
                self._track(upstream_writer)
                try:
                    await asyncio.wait_for(
                        self._handshake(upstream_reader, upstream_writer,
                                        host, int(port or 443)), self.HANDSHAKE_TIMEOUT)
                except BaseException:
                    self._untrack(upstream_writer)
                    upstream_writer.close()
                    raise
                writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
                await writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer)
            else:
                # Plain HTTP: the browser already sends the absolute URI for a
                # proxied request. For an HTTP/HTTPS upstream the credentials
                # are injected here (they belong to the proxy). Through a SOCKS5
                # tunnel the bytes reach the TARGET server instead, so proxy
                # credentials must never be added — strip anything the browser
                # might have carried.
                headers = []
                while (header := await reader.readline()) not in (b'\r\n', b'\n', b''):
                    headers.append(header)
                if self.scheme == 'socks5':
                    headers = [h for h in headers
                               if not h.lower().startswith(b'proxy-authorization:')]
                body_hint = next((h for h in headers if h.lower().startswith(b'content-length:')), None)
                length = int(body_hint.split(b':')[1]) if body_hint else 0
                body = await reader.read(length) if length else b''
                method, _, first_url = line.decode('latin1').partition(' ')
                first_url = first_url.split(' ')[0]
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    self._open_upstream(), self.CONNECT_TIMEOUT)
                self._track(upstream_writer)
                try:
                    if self.scheme == 'socks5':
                        from urllib.parse import urlsplit
                        target = urlsplit(first_url)
                        path = target.path or '/'
                        if target.query:
                            path += '?' + target.query
                        await asyncio.wait_for(
                            self._handshake(upstream_reader, upstream_writer,
                                            target.hostname, target.port or 80), self.HANDSHAKE_TIMEOUT)
                        request_line = f'{method} {path} HTTP/1.1\r\n'.encode()
                    else:
                        request_line = line  # absolute URI is what an HTTP proxy expects
                except BaseException:
                    self._untrack(upstream_writer)
                    upstream_writer.close()
                    raise
                auth_header = self._auth_header().encode() if self.scheme != 'socks5' else b''
                upstream_writer.write(request_line + b''.join(headers) +
                                      auth_header + b'\r\n' + body)
                await upstream_writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer)
        except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            self._untrack(writer)
            for close_target in (upstream_writer, writer):
                if close_target is not None:
                    try:
                        close_target.close()
                    except (ConnectionError, OSError):
                        pass


def spawn(profile: str, chrome: str, extra_args: list[str]) -> IsolatedBrowser:
    """Start Chrome on the private desktop with CDP over a local port."""
    desktop = _ensure_desktop()
    args = [str(chrome), f'--user-data-dir="{profile}"', '--remote-debugging-port=0', '--no-first-run',
            '--disable-background-mode', '--window-position=100,80', '--window-size=1280,900',
            *extra_args]
    cmd = ' '.join(a if ' ' not in a else f'"{a}"' for a in args)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = DESKTOP_NAME
    pi = PROCESS_INFORMATION()
    if not kernel32.CreateProcessW(str(chrome), cmd, None, None, False, 0, None, None,
                                   ctypes.byref(si), ctypes.byref(pi)):
        raise OSError(f'CreateProcessW failed: {kernel32.GetLastError()}')
    return IsolatedBrowser(pi.hProcess, pi.hThread, pi.dwProcessId)


async def attach(pw_chromium, spawned: IsolatedBrowser, profile: str) -> tuple[object, object]:
    """Wait for DevToolsActivePort and attach Playwright over CDP."""
    portfile = Path(profile) / 'DevToolsActivePort'
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        if portfile.is_file():
            break
        if spawned.terminated:
            raise RuntimeError('browser exited before CDP was ready')
        await asyncio.sleep(0.2)
    if not portfile.is_file():
        spawned.terminate()
        raise TimeoutError('DevToolsActivePort did not appear')
    port = int(portfile.read_text(encoding='utf-8').splitlines()[0])
    browser = await pw_chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return browser, context
