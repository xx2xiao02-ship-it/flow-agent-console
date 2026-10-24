"""Close only a preparation tab identified by a fresh, private launch nonce.

The extension remembers the concrete Chrome tab ID. No process killing, remote
debugging port, Google credentials or arbitrary browser commands are involved.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from fastapi import HTTPException, WebSocket, WebSocketDisconnect


class BrowserLifetime:
    """Read-only identity of the actual browser root, including PID reuse guard."""
    def __init__(self, process):
        self.process = process
        process.create_time()

    def poll(self):
        try:
            return None if self.process.is_running() else 0
        except psutil.NoSuchProcess:
            return 0
        except psutil.AccessDenied:
            return None  # An unreadable process cannot prove browser exit.


@dataclass
class PageLease:
    token: str
    account_id: str
    created: float = field(default_factory=time.monotonic)
    action: str = "wait"
    result: str | None = None
    connected: bool = False
    ever_connected: bool = False
    ready_received: bool = False
    connection_error: str | None = None
    remaining_windows: int | None = None
    remaining_tabs: int | None = None
    process: subprocess.Popen | BrowserLifetime | None = None
    browser_path: Path | None = None
    browser_profile: Path | None = None
    visibility: object | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


class BrowserPages:
    def __init__(self, service):
        self.service = service
        self.port = 8001
        self.leases: dict[str, PageLease] = {}
        self.pending: dict[str, PageLease] = {}
        self.close_timeout = 15

    def enabled(self) -> bool:
        with self.service.connect() as db:
            row = db.execute("select value from console_meta where key='auto_close_preparation_pages'").fetchone()
        return not row or row[0] != "0"

    def configure(self, enabled: bool) -> dict:
        with self.service.connect() as db:
            db.execute("insert or replace into console_meta values ('auto_close_preparation_pages', ?)",
                       ("1" if enabled else "0",))
        return {"auto_close_pages": enabled}

    def begin(self, account_id: str, *, force: bool = False) -> PageLease | None:
        self.leases = {k: v for k, v in self.leases.items() if time.monotonic() - v.created < 1800}
        if not self.enabled() and not force:
            return None
        lease = PageLease(secrets.token_hex(32), account_id)
        self.leases[lease.token] = lease
        self.pending[account_id] = lease
        return lease

    def launch_url(self, account_id: str) -> str:
        lease = self.pending.pop(account_id, None)
        url = "https://labs.google/fx/tools/flow"
        if lease:
            url += f"#flow-console-page={self.port}.{lease.token}"
        return url

    def record_process(self, url: str, process: subprocess.Popen, *, browser=None, profile=None):
        if "#flow-console-page=" not in url:
            return
        lease = self.leases.get(url.rsplit(".", 1)[-1])
        if lease:
            lease.process = process
            lease.browser_path = Path(browser).resolve() if browser else None
            lease.browser_profile = Path(profile).resolve() if profile else None

    @staticmethod
    def _actual_process(lease):
        # Chrome can delegate to a root already shutting down. The short-lived
        # launcher is not the window's lifetime. Inspect ONLY the exact binary
        # and user-data directory of this launch; never stop any process.
        if not lease.browser_path or not lease.browser_profile:
            return lease.process
        matches = []
        for process in psutil.process_iter(['name']):
            try:
                if (process.info.get('name') or '').lower() not in ('chrome.exe', 'chrome', 'chromium'):
                    continue
                if Path(process.exe()).resolve() != lease.browser_path:
                    continue
                args = process.cmdline()
                if any(arg.startswith('--type=') for arg in args):
                    continue
                profile = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--user-data-dir=')), None)
                if profile is None and '--user-data-dir' in args:
                    index = args.index('--user-data-dir')
                    profile = args[index + 1] if index + 1 < len(args) else None
                if profile and Path(profile.strip('"')).resolve() == lease.browser_profile:
                    matches.append(BrowserLifetime(process))
            except (psutil.Error, OSError, ValueError):
                continue
        # Once launch identity is available, ambiguity cannot establish exit.
        # A delegated launcher may already have exited while Chrome is alive.
        return matches[0] if len(matches) == 1 else None

    async def finish(self, lease: PageLease | None, checked: bool) -> dict:
        if not lease:
            return {"state": "retained", "message": "已关闭自动关页，浏览器页面已保留。"}
        self.pending.pop(lease.account_id, None)
        if not checked or not self.enabled():
            lease.action = "retain"
            return {"state": "retained", "message": "页面已保留，请完成登录或人工验证。" if not checked
                    else "已关闭自动关页，浏览器页面已保留。"}
        lease.action = "close"
        # A generation needs the live extension again if closing the final tab
        # causes Chrome to exit. Remember this across a console restart too.
        with self.service.connect() as db:
            db.execute("insert or replace into console_meta values (?, '1')",
                       ("preparation_page_closed:" + lease.account_id,))
        try:
            await asyncio.wait_for(lease.done.wait(), self.close_timeout)
        except TimeoutError:
            # Revoke the permission: a late extension must never close a page
            # after the user has been told that it was retained.
            lease.action = "retain"
            return {"state": "unconfirmed", "message": "未收到关页确认，已停止自动关闭；请检查保留的窗口。",
                    "page_connected": lease.ever_connected, "close_ready": lease.ready_received,
                    "connection_error": lease.connection_error}
        message = "页面正在等待人工操作、已被切换或无法确认归属，已安全保留。"
        if lease.result == 'closed':
            message = ("本次打开的页面和浏览器窗口已自动关闭。" if lease.remaining_windows == 0
                       else f"本次标签已关闭，但此浏览器仍有 {lease.remaining_windows} 个窗口、{lease.remaining_tabs} 个标签；未确认整个浏览器退出。" if lease.remaining_windows
                       else "本次打开的页面已自动关闭。")
        return {"state": lease.result, "message": message,
                "remaining_windows": lease.remaining_windows, "remaining_tabs": lease.remaining_tabs}

    async def restore_for_generation(self, account_id: str):
        key = "preparation_page_closed:" + account_id
        with self.service.connect() as db:
            row = db.execute("select value from console_meta where key=?", (key,)).fetchone()
        if not row:
            return
        self.service.preparation.synced.discard(account_id)
        await self.service.open_account_browser(account_id)
        deadline = asyncio.get_running_loop().time() + 90
        while account_id not in self.service.preparation.synced:
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(409, "重新打开账号后尚未收到扩展同步，本次未提交生成。请检查 Flow 窗口。")
            await asyncio.sleep(0.5)
        with self.service.connect() as db:
            db.execute("delete from console_meta where key=?", (key,))

    async def channel(self, socket: WebSocket, token: str):
        # This endpoint is absent from the LAN application. Browser websites
        # cannot use it; an extension still needs an unguessable launch nonce.
        origin = socket.headers.get("origin", "")
        lease = self.leases.get(token)
        if lease:
            if not socket.client or socket.client.host not in {"127.0.0.1", "::1", "testclient"}:
                lease.connection_error = "peer"
            elif not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                lease.connection_error = "origin"
            elif lease.connected:
                lease.connection_error = "occupied"
        if (not socket.client or socket.client.host not in {"127.0.0.1", "::1", "testclient"}
                or not re.fullmatch(r"chrome-extension://[a-p]{32}", origin)
                or not lease or lease.connected or time.monotonic() - lease.created >= 1800):
            await socket.close(code=1008)
            return
        lease.connected = True
        lease.connection_error = None
        lease.ever_connected = True
        await socket.accept()
        committed = False
        owned_process_alive = False
        async def confirm_owned_exit():
            if not (committed and owned_process_alive and lease.action == 'close'):
                return False
            for _ in range(30):
                if lease.process.poll() is not None:
                    lease.remaining_windows = 0
                    lease.remaining_tabs = 0
                    return True
                await asyncio.sleep(0.1)
            return False
        try:
            while time.monotonic() - lease.created < 1800:
                action = "commit_close" if committed and lease.action == "close" else lease.action
                await socket.send_json({"action": action})
                reply = await asyncio.wait_for(socket.receive_text(), 8)
                if action == 'commit_close' and reply.startswith('{'):
                    try:
                        info = json.loads(reply)
                        if info.get('state') == 'closed' and all(type(info.get(k)) is int and
                                0 <= info[k] <= 10000 for k in ('remaining_windows', 'remaining_tabs')):
                            lease.remaining_windows = info['remaining_windows']
                            lease.remaining_tabs = info['remaining_tabs']
                            reply = 'closed'
                    except (ValueError, AttributeError):
                        pass
                if action == 'wait' and reply == 'attention' and lease.visibility:
                    lease.visibility.reveal('login_or_verification')
                elif action == 'wait' and reply == 'waiting' and lease.visibility and lease.visibility.shown:
                    lease.visibility.hide_after_verification()
                if action == "close" and reply == "ready":
                    lease.ready_received = True
                    # Capture a live launch handle BEFORE authorizing removal.
                    # Resolve a delegated launch to its exact browser/profile
                    # root. No process is terminated by this observation.
                    lease.process = await asyncio.to_thread(self._actual_process, lease)
                    owned_process_alive = bool(lease.process and lease.process.poll() is None)
                    committed = True
                if (action in {"close", "commit_close"} and reply == "retained"
                        or action == "commit_close" and reply == "closed"):
                    # Closing the last app tab can tear down extension APIs
                    # during the post-removal check. A conservative retained
                    # reply is not final until this exact owned process exits.
                    if reply == 'retained' and await confirm_owned_exit():
                        reply = 'closed'
                    lease.result = reply
                    lease.done.set()
                    break
                if action == "retain":
                    break
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, TimeoutError, RuntimeError):
            # Last-tab closure can exit Chrome before it can send the final
            # acknowledgement. Require BOTH the precise-tab handshake and the
            # observed exit of the still-live process we launched ourselves.
            if await confirm_owned_exit():
                lease.result = "closed"
                lease.done.set()
        finally:
            lease.connected = False
            try:
                await socket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
