"""Serial handoff of the local extension bridge to a generation subprocess.

The idle bridge only syncs sessions. It cannot relay browser operations to a
separate CLI process. Release its listener, let the command own the connection,
then restore syncing. Never terminate a foreign listener or browser process.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

from .engine import EngineError


class BrowserExecution:
    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()
        self._handoff_active = False
        self.page_accounts: dict[str, tuple[str, str]] = {}
        self.page_condition = asyncio.Condition()
        self.wait_seconds = 90
        self.last_cleanup: dict = {}
        self.active_visibility = None

    @property
    def handoff_active(self):
        return self._handoff_active or bool(self.page_accounts)

    @handoff_active.setter
    def handoff_active(self, value):
        self._handoff_active = value

    @asynccontextmanager
    async def page_slot(self, cookie):
        self.service.media_storage.migration.require_available()
        account = Path(cookie).stem
        profile = self.service.scheduler.profile_key(account)
        identity = self.service.scheduler.identity_key(account)
        async with self.page_condition:
            while (self.lock.locked() or account in self.page_accounts
                   or any(profile == p or identity == i for p, i in self.page_accounts.values())
                   or len(self.page_accounts) >= self.service.scheduler.max_concurrency):
                try:
                    await asyncio.wait_for(self.page_condition.wait(), .1)
                except TimeoutError:
                    pass
            self.service.media_storage.migration.require_available()
            self.page_accounts[account] = (profile, identity)
        try:
            yield
        finally:
            async with self.page_condition:
                self.page_accounts.pop(account, None)
                self.page_condition.notify_all()

    async def _stop_owned_bridge(self):
        service = self.service
        async with service.bridge_lock:
            process = service.bridge_process
            if process is None or process.returncode is not None:
                if service._bridge_port_open():
                    raise EngineError([], 1, '', 'BROWSER_BRIDGE_OCCUPIED')
                return
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
            service.bridge_process = None
            if service.bridge_reader:
                service.bridge_reader.cancel()
                try:
                    await service.bridge_reader
                except asyncio.CancelledError:
                    pass
                service.bridge_reader = None
            for _ in range(30):
                if not service._bridge_port_open():
                    return
                await asyncio.sleep(.1)
            raise EngineError([], 1, '', 'BROWSER_BRIDGE_OCCUPIED')

    async def run(self, args, spawn):
        async with self.lock:
            self.service.media_storage.migration.require_available()
            # The native extension bridge is shared. It must never overlap any
            # official-page browser, even when the queue allows several jobs.
            async with self.page_condition:
                while self.page_accounts:
                    await self.page_condition.wait()
            service = self.service
            args = list(args)
            if '--all' in args:
                raise EngineError([], 1, '', 'BROWSER_SINGLE_ACCOUNT_REQUIRED')
            if service.preparation.running:
                raise EngineError([], 1, '', 'BROWSER_PREPARATION_BUSY')
            if '--cookies' not in args:
                # Apply the existing console scheduler, then freeze that account.
                cookie = await service._scheduled_cookie()
                args += ['--cookies', cookie]
            else:
                cookie = str(args[args.index('--cookies') + 1])
            account = Path(cookie).stem
            if service._cookie_files().get(account) != Path(cookie):
                raise EngineError([], 1, '', 'BROWSER_ACCOUNT_MISSING')
            lease = service.browser_pages.begin(account, force=True)
            connected = False
            handoff_started = False
            try:
                await service.start_bridge()
                service.preparation.synced.discard(account)
                await service.open_account_browser(account, background=True)
                deadline = asyncio.get_running_loop().time() + self.wait_seconds
                # An already-connected idle bridge may not emit another cookie
                # sync line just because a fresh tab opened. Its nonce-bound
                # page channel proves THIS launch is connected. The native
                # strict bootstrap still pins and verifies the selected Google
                # identity before any token or generation can be requested.
                while ((account not in service.preparation.synced and not lease.ever_connected)
                       or (lease.visibility and lease.visibility.shown)):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise EngineError([], 1, '', 'BROWSER_ACCOUNT_NOT_CONNECTED')
                    await asyncio.sleep(.25)
                connected = True
                if lease.visibility:
                    await lease.visibility.confirm_hidden()
                with service.connect() as db:
                    db.execute('delete from console_meta where key=?',
                               ('preparation_page_closed:' + account,))
                # Block background preparation from taking this port mid-run.
                self.handoff_active = handoff_started = True
                await self._stop_owned_bridge()
                # The native strict mode pins the matching browser cookie identity;
                # it refuses HTTP fallback, missing pages and account changes.
                args += ['--captcha', 'browser']
                result = await spawn(*args)
                return result
            finally:
                try:
                    # Generation outcome is unrelated to page ownership. A failed
                    # or timed-out command still releases its tabs. The extension
                    # independently retains login/challenge/user-navigated pages.
                    self.last_cleanup = await service.browser_pages.finish(lease, connected)
                except Exception:
                    self.last_cleanup = {'state': 'unconfirmed', 'message': '未确认关页结果，请检查保留的窗口。'}
                finally:
                    if lease and lease.visibility:
                        guard = lease.visibility
                        try:
                            closed = await guard.close_owned()
                            self.last_cleanup = {'state': 'closed' if closed else 'unconfirmed',
                                'message': '本次专用浏览器已自动关闭。' if closed else '未确认专用浏览器退出，请检查保留窗口。',
                                'browser_closed': closed, 'visibility': guard.info()}
                        except Exception:
                            self.last_cleanup = {'state': 'unconfirmed', 'browser_closed': False,
                                'message': '未确认专用浏览器退出，请检查保留窗口。'}
                        finally:
                            self.active_visibility = None
                    # Keep the public acknowledgement for real job audits as
                    # well as browser-check responses. Never persist a nonce,
                    # session, URL, provider response or subprocess argv here.
                    if isinstance(self.last_cleanup, dict):
                        record = {'account_id': account, 'operation': args[0], 'checked_at': datetime.now(timezone.utc).isoformat(),
                                  'cleanup': self.last_cleanup}
                        try:
                            with service.connect() as db:
                                db.execute('insert or replace into console_meta values (?, ?)',
                                           ('last_browser_execution_cleanup', json.dumps(record)))
                        except Exception:
                            pass  # Recording cannot hide the generation result.
                    if handoff_started:
                        self.handoff_active = False
                        # Restore only after cleanup; bootstrapping the idle bridge
                        # here used to race with removal and reopen a Flow tab.
                        try:
                            await service.start_bridge()
                        except Exception:
                            pass
