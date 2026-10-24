"""Periodic read-only account checks. Repairs expired sessions without generating."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from .console import ConsoleService


class AccountMonitor:
    interval_seconds = 300

    def __init__(self, service: ConsoleService):
        self.service = service
        self.enabled = True
        self.task: asyncio.Task | None = None
        self.current_account_id: str | None = None
        self.last_cycle_at: str | None = None
        self.last_error: str | None = None
        self.closing = False
        self.repair_attempts: dict[str, str] = {}
        self.startup_issues: dict[str, str] = {}
        self.waiting_profiles: set[str] = set()

    def profile_busy(self, key):
        from .official_images import profile_in_use
        name = self.service._account_profile_name(key)
        path = self.service.engine.root / 'data' / name if name else None
        return bool(path and path.is_dir() and profile_in_use(path))

    def load(self):
        self.closing = False
        with self.service.connect() as db:
            row = db.execute("select value from console_meta where key='account_monitor_enabled'").fetchone()
        self.enabled = row is None or row[0] != 'false'

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def busy(self) -> bool:
        with self.service.connect() as db:
            active = db.execute("select 1 from console_jobs where status in ('queued','running') limit 1").fetchone()
        return bool(active or self.service.preparation.running or self.service.balance_refresh_lock.locked())

    @staticmethod
    def _checked_time(value: str | None) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    def _accounts(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        with self.service.connect() as db:
            known = {row['account_key']: dict(row) for row in db.execute('select * from console_account_details')}
        accounts = []
        from .account_identity import aliases
        account_aliases = aliases(self.service)
        for key, path in sorted(self.service._cookie_files().items()):
            if account_aliases.get(key,key) != key:
                continue
            old = known.get(key, {})
            checked = self._checked_time(old.get('checked_at'))
            due = checked + timedelta(seconds=self.interval_seconds) if checked else now
            complete = self.service._complete_session(path)
            matches = old.get('bundle_digest') == hashlib.sha256(path.read_bytes()).hexdigest()
            needs_check = complete and (not matches or due <= now)
            waiting = key in self.waiting_profiles
            if waiting and complete and not self.profile_busy(key):
                needs_check = True
            state, message = 'pending', '等待后台检测。'
            if not complete:
                state, message = 'incomplete', '会话未连接完整，请先连接账号。'
            elif self.current_account_id == key:
                state, message = 'checking', '正在后台核对身份和积分，无需打开网页。'
            elif not matches:
                message = '会话已更新，等待后台重新核对。'
            elif old.get('error') in ('session', 'refused'):
                state, message = 'needs_attention', '会话读取失败，等待自动修复；尚未确认需要重新登录。'
            elif old.get('error') == 'network':
                state, message = 'network_error', '网络查询失败，将在下一轮后台检测时重试读取。'
            elif old.get('error') == 'identity':
                state, message = 'identity_error', '实际登录账号与保存的账号不符，请打开 Flow 核对。'
            elif old.get('error') or not old.get('email') or old.get('credits') is None:
                state, message = 'unread', '尚未成功读取身份或积分，将在下一轮重新检测。'
            elif due <= now:
                state, message = 'stale', '上次检测已到期，等待后台更新。'
            else:
                state, message = 'ready', '后台身份和积分检测通过。'
            from .account_scheduling import browser_identity_ready
            if state in ('ready', 'stale') and not browser_identity_ready(self.service, key):
                state, message = 'binding_missing', '浏览器关联不完整，请重新连接此账号；未进入自动调度。'
            if key in self.startup_issues and state != 'checking':
                state, message = 'needs_attention', self.startup_issues[key]
            with self.service.connect() as db:
                repair_row = db.execute('select value from console_meta where key=?',('account_repair:'+key,)).fetchone()
            repair = json.loads(repair_row[0]) if repair_row else {}
            if state not in ('checking','ready','stale') and repair.get('state') == 'needs_attention':
                state = 'needs_login' if repair.get('login_confirmed_by_page') else 'needs_attention'
                message = repair.get('message') or message
            if waiting and state != 'checking':
                state, message = 'pending', '等待当前账号窗口释放；关闭后自动继续同步，无需再次点击重新连接。'
            accounts.append({'account_id': key, 'state': state, 'message': message,
                             'checked_at': old.get('checked_at') if matches else None,
                             'next_check_at': due.isoformat(timespec='seconds') if complete and matches else None,
                             'credits': old.get('credits') if matches and state in ('ready', 'stale') else None,
                             'requires_user_action': state in ('incomplete', 'needs_login', 'identity_error', 'binding_missing', 'needs_attention'),
                             '_due': needs_check})
        return accounts

    def snapshot(self) -> dict:
        accounts = self._accounts()
        if self.running:
            state, message = 'checking', '后台正在逐个核对账号，切换页面不会中断。'
        elif not self.enabled:
            state, message = 'disabled', '自动检测已关闭，可手动立即检测。'
        elif self.busy():
            state, message = 'paused', '有任务或账号准备进行中，结束后自动继续后台检测。'
        else:
            state, message = 'watching', '后台检测已开启；会话变化自动检测，身份和积分每 5 分钟核验。'
        for account in accounts:
            account.pop('_due')
        return {'enabled': self.enabled, 'state': state, 'message': self.last_error or message,
                'interval_seconds': self.interval_seconds, 'current_account_id': self.current_account_id,
                'last_cycle_at': self.last_cycle_at, 'accounts': accounts, 'generation_verified': False}

    def configure(self, enabled: bool) -> dict:
        with self.service.connect() as db:
            db.execute("insert or replace into console_meta values ('account_monitor_enabled', ?)",
                       (json.dumps(enabled),))
        self.enabled = enabled
        return self.snapshot()

    async def start_check(self, *, force: bool = False) -> dict:
        if self.closing:
            raise HTTPException(409, '程序正在退出，请稍后重新启动。')
        if self.running:
            return self.snapshot()
        if self.busy():
            if force:
                raise HTTPException(409, '有任务或账号准备进行中，结束后再检查；后台会自动恢复。')
            return self.snapshot()
        if not force and not self.enabled:
            return self.snapshot()
        accounts = self._accounts()
        keys = {a['account_id'] for a in accounts if a['state'] != 'incomplete' and (force or a['_due'])}
        if keys:
            self.last_error = None
            self.task = asyncio.create_task(self._check(keys), name='flow-background-account-check')
        return self.snapshot()

    async def _check(self, keys: set[str]):
        from .console import utc_now
        try:
            async with self.service.balance_refresh_lock:
                await self.service.refresh_account_details(account_ids=keys, on_check=self._checking,
                                                           pause_when_busy=True)
            # HTTP reads are cheap. Open the dedicated profile only for an
            # authentication failure, never for network/unknown/identity errors.
            for key in sorted(keys):
                if self.closing or self.busy():
                    break
                from .account_identity import canonical
                if canonical(self.service,key) != key:
                    continue  # A freshly synced session was just identified as an alias.
                detail = self.service._verified_detail(key) or {}
                if detail.get('error') not in ('session', 'refused'):
                    self.waiting_profiles.discard(key)
                    continue
                if self.profile_busy(key):
                    self.waiting_profiles.add(key)
                    continue
                if key in self.waiting_profiles:
                    self.repair_attempts.pop(key, None)
                self.waiting_profiles.discard(key)
                path = self.service._cookie_files().get(key)
                if not path:
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if self.repair_attempts.get(key) == digest:
                    continue
                self.repair_attempts[key] = digest
                self._checking(key)
                preparation = self.service.preparation
                async with preparation.start_lock:
                    if preparation.running or self.busy():
                        continue
                    preparation.require_idle_queue()
                    preparation.batch = None
                    preparation.report = preparation._new_report(key)
                    preparation.task = asyncio.create_task(
                        preparation._run_headless(key, repair_login=True), name='flow-session-repair')
                await preparation.task
                # A refreshed bundle may still be rejected; do not continually
                # repeat an interactive repair for that same resulting snapshot.
                if path.is_file():
                    self.repair_attempts[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_error = '本轮后台检测暂未成功，将在下一轮重查；可检查本机网络。'
        finally:
            self.current_account_id = None
            self.last_cycle_at = utc_now()

    def _checking(self, account_id: str):
        self.startup_issues.pop(account_id, None)
        self.current_account_id = account_id

    async def stop(self):
        self.closing = True
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
