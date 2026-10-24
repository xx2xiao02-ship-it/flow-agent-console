"""Windows visibility for a cold, exclusively program-owned Chrome profile.

No headless mode, no safety bypass, no access to an existing personal browser.
"""
from __future__ import annotations
import asyncio
import ctypes
import os
import re
import time
from pathlib import Path
import psutil


class BrowserVisibilityError(RuntimeError):
    pass


class OwnedBrowserVisibility:
    def __init__(self, profile: Path, browser: Path, mode: str = 'hidden'):
        self.profile, self.browser = profile.resolve(), browser.resolve()
        self.mode = mode if mode in ('hidden', 'visible', 'isolated') else 'hidden'
        self.roots = {}
        self.task = None
        self.shown = False
        self.reason = None
        self.hidden_handles = set()
        self.shown_handles = set()
        self.last_windows = []
        self.error = None
        self._started = time.time()
        self._stopping = False
        self.enabled = os.name == 'nt'
        if self.enabled:
            from ctypes import wintypes as w
            self.user = ctypes.WinDLL('user32', use_last_error=True)
            self.callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
            self.user.EnumWindows.argtypes = [self.callback_type, w.LPARAM]
            self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
            self.user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
            self.user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
            self.user.IsWindowVisible.argtypes = [w.HWND]
            self.user.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
            self.user.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
            self.user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]

    def launch_args(self):
        if os.name != 'nt':
            return []
        if self.mode in ('visible', 'isolated'):
            # Normal desktop geometry. CalculateNativeWinOcclusion is disabled so
            # the page stays "visible" even when covered by the user's windows or
            # while living on the private automation desktop.
            return ['--window-position=120,80', '--window-size=1280,900',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-features=CalculateNativeWinOcclusion']
        # Initial geometry stays away from the desktop before SW_HIDE is applied.
        return ['--window-position=-32000,-32000', '--window-size=1280,900', '--disable-backgrounding-occluded-windows']

    @property
    def requires_hidden(self):
        return self.enabled and self.mode == 'hidden'

    def _matching_roots(self):
        found = []
        for p in psutil.process_iter(['name']):
            try:
                if (p.info['name'] or '').lower() not in ('chrome.exe', 'chrome', 'chromium'):
                    continue
                args = p.cmdline()
                if any(a.startswith('--type=') for a in args) or Path(p.exe()).resolve() != self.browser:
                    continue
                value = next((a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir=')), None)
                if value is None and '--user-data-dir' in args:
                    value = args[args.index('--user-data-dir') + 1]
                if value and Path(value.strip('"')).resolve() == self.profile:
                    found.append(p)
            except (psutil.NoSuchProcess, OSError, IndexError):
                continue
            except psutil.AccessDenied:
                raise BrowserVisibilityError('cannot establish exclusive browser ownership') from None
        return found

    async def start(self):
        if not self.enabled:
            return
        if await asyncio.to_thread(self._matching_roots):
            raise BrowserVisibilityError('browser profile is already open')
        self.task = asyncio.create_task(self._watch())

    def snapshot(self):
        if not self.enabled:
            return []
        from ctypes import wintypes as w
        for p in self._matching_roots():
            if p.create_time() >= self._started - 1:
                self.roots[p.pid] = p
        owned = {}
        for pid, p in list(self.roots.items()):
            try:
                if not p.is_running():
                    continue
                owned[pid] = p
                owned.update({child.pid: child for child in p.children(recursive=True)})
            except psutil.NoSuchProcess:
                continue
        rows = []
        @self.callback_type
        def visit(hwnd, _):
            pid = w.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in owned:
                name = ctypes.create_unicode_buffer(128)
                self.user.GetClassNameW(hwnd, name, 128)
                if name.value.startswith('Chrome_WidgetWin'):
                    title = ctypes.create_unicode_buffer(512)
                    self.user.GetWindowTextW(hwnd, title, 512)
                    rows.append({'handle': hwnd, 'pid': pid.value,
                                 'visible': bool(self.user.IsWindowVisible(hwnd)), 'title': title.value})
            return True
        if not self.user.EnumWindows(visit, 0):
            raise BrowserVisibilityError('window enumeration failed')
        return rows

    def _tick(self):
        windows = self.snapshot()
        if self.mode == 'isolated':
            # The windows live on the private automation desktop; window-manager
            # calls from this desktop would fail, and nothing needs moving.
            self.last_windows = windows
            return
        if self.mode == 'visible':
            # Keep owned windows on the desktop without stealing focus.
            for row in windows:
                hwnd = row['handle']
                if not row['visible'] and hwnd not in self.shown_handles:
                    self.user.SetWindowPos(hwnd, None, 120, 80, 1280, 900, 0x0014)  # no z-order/focus changes
                    self.user.ShowWindow(hwnd, 8)  # SW_SHOWNA
                    self.shown_handles.add(hwnd)
            self.last_windows = windows
            return
        for row in windows:
            hwnd = row['handle']
            if re.search(r'Sign in.*Google|登录.*Google|Verify.*(you|identity)|验证.*(身份|本人)|人机身份验证', row['title'], re.I):
                self.reveal('login_or_verification')
            if self.shown:
                if hwnd not in self.shown_handles:
                    self.user.SetWindowPos(hwnd, None, 100, 80, 1280, 900, 0x0014)  # no z-order/focus changes
                    self.user.ShowWindow(hwnd, 9)
                    self.shown_handles.add(hwnd)
            else:
                self.user.ShowWindow(hwnd, 0)
                self.hidden_handles.add(hwnd)
        self.last_windows = self.snapshot()

    async def _watch(self):
        try:
            while not self._stopping:
                await asyncio.to_thread(self._tick)
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = 'visibility_control_failed'

    async def confirm_hidden(self, timeout=10):
        if not self.enabled or self.mode != 'hidden':
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error:
                raise BrowserVisibilityError(self.error)
            if self.last_windows and not any(w['visible'] for w in self.last_windows) and self.hidden_handles:
                return
            await asyncio.sleep(.1)
        raise BrowserVisibilityError('hidden state was not confirmed')

    def reveal(self, reason='login_or_verification'):
        self.shown, self.reason = True, reason

    def hide_after_verification(self):
        if self.mode != 'hidden':
            return
        self.shown, self.reason = False, None
        self.shown_handles.clear()

    def info(self):
        if self.mode == 'isolated':
            # The window lives on the private desktop and cannot be brought to
            # the user's; attention is surfaced through the manual-attention
            # notification instead. The state stays truthful either way.
            return {'mode': 'isolated_desktop', 'hidden_confirmed': False,
                    'waiting_for_user': bool(self.shown), 'shown_for_user': False,
                    'reason': self.reason,
                    'visible_owned_windows': sum(w['visible'] for w in self.last_windows),
                    'visibility_error': self.error}
        if self.mode == 'visible':
            return {'mode': 'visible', 'hidden_confirmed': False, 'waiting_for_user': False,
                    'shown_for_user': bool(self.shown_handles), 'reason': self.reason,
                    'visible_owned_windows': sum(w['visible'] for w in self.last_windows),
                    'visibility_error': self.error}
        return {'mode': 'background_hidden' if self.enabled else 'visible_platform_fallback',
                'hidden_confirmed': bool(self.hidden_handles), 'waiting_for_user': self.shown,
                'shown_for_user': bool(self.shown_handles), 'reason': self.reason,
                'visible_owned_windows': sum(w['visible'] for w in self.last_windows),
                'visibility_error': self.error}

    async def stop(self):
        if self.task:
            self._stopping = True
            await self.task
            self.task = None

    async def close_owned(self):
        # Only roots accepted after the cold-profile check, protected by psutil's
        # cached creation time. Never enumerate-and-kill by executable name.
        await self.stop()
        if not self.enabled:
            return False
        for row in await asyncio.to_thread(self.snapshot):
            self.user.PostMessageW(row['handle'], 0x0010, 0, 0)  # WM_CLOSE
        for _ in range(30):
            if not any(p.is_running() for p in self.roots.values()):
                self.last_windows = []
                return True
            await asyncio.sleep(.1)
        for p in self.roots.values():
            try:
                if p.is_running():
                    p.terminate()
            except psutil.NoSuchProcess:
                pass
        for _ in range(30):
            if not any(p.is_running() for p in self.roots.values()):
                self.last_windows = []
                return True
            await asyncio.sleep(.1)
        return False


class HeadlessCheckVisibility(OwnedBrowserVisibility):
    """Short-lived headless checks; never reveal a window or solve verification."""
    @staticmethod
    def launch_args():
        return []

    async def start(self):
        # Exclusive cold-profile ownership is checked by the caller's settings().
        async def track():
            while not self._stopping:
                for process in self._matching_roots():
                    if process.create_time() >= self._started - 1:
                        self.roots[process.pid] = process
                await asyncio.sleep(.1)
        self.task = asyncio.create_task(track())

    async def confirm_hidden(self):
        return None

    def reveal(self, reason):
        from .official_images import OfficialImageError
        self.reason = reason
        raise OfficialImageError('headless_needs_login')

    def hide_after_verification(self):
        pass

    def info(self):
        return {'mode': 'headless', 'hidden_confirmed': True, 'waiting_for_user': False,
                'shown_for_user': False, 'reason': self.reason,
                'visible_owned_windows': 0, 'visibility_error': self.error}

    async def close_owned(self):
        await self.stop()
        for process in self._matching_roots():
            if process.create_time() >= self._started - 1:
                self.roots[process.pid] = process
        owned = []
        for process in self.roots.values():
            try:
                if process.is_running():
                    owned.extend(process.children(recursive=True))
                    owned.append(process)
            except psutil.NoSuchProcess:
                pass
        for process in reversed(owned):
            try: process.terminate()
            except psutil.NoSuchProcess: pass
        _, alive = await asyncio.to_thread(psutil.wait_procs, owned, timeout=5)
        for process in alive:
            try: process.kill()
            except psutil.NoSuchProcess: pass
        if alive: await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)
        return not self._matching_roots()
