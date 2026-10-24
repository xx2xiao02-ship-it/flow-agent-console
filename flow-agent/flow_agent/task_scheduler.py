"""Bounded local dispatch. Reserve accounts and profiles before starting jobs.

Never resubmit a job, replace its account after submission, or interrupt running
jobs when lowering the configured limit. Legacy bridge operations stay exclusive.
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from contextvars import ContextVar
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from .account_scheduling import snapshot as account_policy_snapshot


CURRENT_JOB: ContextVar[str | None] = ContextVar('flow_console_job', default=None)
MAX_CONCURRENCY = 16
# 0 = no forced spacing between dispatches; user opt-in via the scheduler UI.
DEFAULT_MIN_INTERVAL = 0


class SchedulerInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_concurrency: int = Field(ge=1, le=MAX_CONCURRENCY, strict=True)
    # Resolved at instantiation so the offline suites can zero the default.
    min_job_interval_seconds: int = Field(
        default_factory=lambda: DEFAULT_MIN_INTERVAL, ge=0, le=600, strict=True)


class TaskScheduler:
    def __init__(self, service):
        self.service = service
        self.max_concurrency = MAX_CONCURRENCY
        self.min_job_interval = DEFAULT_MIN_INTERVAL
        self._next_dispatch_at = 0.0
        self.wakeup = asyncio.Event()
        self.active: dict[str, asyncio.Task] = {}
        self.reservations: dict[str, tuple[str, str]] = {}

    def load(self):
        with self.service.connect() as db:
            row = db.execute("select value from console_meta where key='max_concurrency'").fetchone()
            interval = db.execute("select value from console_meta where key='min_job_interval'").fetchone()
        try:
            self.max_concurrency = SchedulerInput(max_concurrency=int(row[0])).max_concurrency if row else MAX_CONCURRENCY
        except (ValueError, TypeError):
            self.max_concurrency = MAX_CONCURRENCY
        try:
            self.min_job_interval = SchedulerInput(
                max_concurrency=self.max_concurrency,
                min_job_interval_seconds=int(interval[0])).min_job_interval_seconds if interval else DEFAULT_MIN_INTERVAL
        except (ValueError, TypeError):
            self.min_job_interval = DEFAULT_MIN_INTERVAL

    def snapshot(self):
        with self.service.connect() as db:
            counts = dict(db.execute('select status,count(*) from console_jobs group by status').fetchall())
        return {'automatic_account_policy': account_policy_snapshot(self.service),
                'max_concurrency': self.max_concurrency, 'min': 1, 'max': MAX_CONCURRENCY,
                'min_job_interval_seconds': self.min_job_interval,
                'min_job_interval_min': 0, 'min_job_interval_max': 600,
                'running': counts.get('running', 0), 'queued': counts.get('queued', 0),
                'per_account_limit': 1, 'per_profile_limit': 1,
                'legacy_bridge_limit': 1, 'running_tasks': [
                    {'job_id': job, 'account_id': account}
                    for job, (account, _) in self.reservations.items()]}

    def configure(self, data: SchedulerInput):
        with self.service.connect() as db:
            db.execute("insert or replace into console_meta values ('max_concurrency', ?)",
                       (str(data.max_concurrency),))
            db.execute("insert or replace into console_meta values ('min_job_interval', ?)",
                       (str(data.min_job_interval_seconds),))
        self.max_concurrency = data.max_concurrency
        self.min_job_interval = data.min_job_interval_seconds
        self.wakeup.set()
        return self.snapshot()

    def profile_key(self, account: str):
        name = self.service._account_profile_name(account)
        return str((self.service.engine.root / 'data' / name).resolve()).casefold() if name else 'account:' + account

    def identity_key(self, account: str):
        detail = self.service._verified_detail(account) or {}
        return (detail.get('email') or account).casefold()

    def busy_accounts(self):
        accounts = {a for a, _ in self.reservations.values()}
        profiles = {p for _, p in self.reservations.values()}
        identities = {self.identity_key(a) for a in accounts}
        accounts.update(a for a in self.service._cookie_files()
                        if self.profile_key(a) in profiles or self.identity_key(a) in identities)
        return accounts

    async def _execute(self, job_id, cookie, error):
        token = CURRENT_JOB.set(job_id)
        try:
            await self.service.execute_job(job_id, cookie, error)
        finally:
            CURRENT_JOB.reset(token)
            self.reservations.pop(job_id, None)
            self.active.pop(job_id, None)
            self.service.queue.task_done()
            self.wakeup.set()

    async def run(self):
        from .console import JobInput
        self.load()
        pending = []
        try:
            while True:
                self.wakeup.clear()
                while not self.service.queue.empty():
                    pending.append(self.service.queue.get_nowait())
                for job_id in pending[:]:
                    if len(self.active) >= self.max_concurrency:
                        break
                    if self.min_job_interval and time.monotonic() < self._next_dispatch_at:
                        break  # Spacing window between dispatches is still open.
                    job = self.service.get_job(job_id)
                    if job['status'] != 'queued':
                        pending.remove(job_id)
                        self.service.queue.task_done()
                        continue
                    retry = (job.get('result') or {}).get('auto_retry') or {}
                    if retry.get('next_retry_at'):
                        if datetime.fromisoformat(retry['next_retry_at']) > datetime.now(timezone.utc):
                            continue
                    cookie, error = None, None
                    try:
                        data = JobInput.model_validate(job['request'])
                        busy = self.busy_accounts()
                        readonly = retry.get('action') in ('recover', 'inspect')
                        rotate = data.retry_switch_accounts and retry.get('attempts_completed', 0) > 0 and not readonly
                        account = (retry.get('recovery_account_id') or job['request'].get('selected_account_id')) if readonly else (None if rotate else data.account_id or (job['request'].get('selected_account_id') if retry else None))
                        if data.dispatch_quote_id and not readonly:
                            with self.service.connect() as db:
                                bound = self.service.dispatch_quotes.check(data, db, job_id=job_id)
                            account = bound['accountId']
                        if readonly and not account:
                            from fastapi import HTTPException
                            raise HTTPException(409, '缺少原账号，不能安全查询；未重新生成。')
                        used = set(retry.get('attempted_accounts', [])) if rotate else set()
                        # Aliases of the same email or browser profile are not different accounts.
                        used_identities = {self.identity_key(a) for a in used}
                        used_profiles = {self.profile_key(a) for a in used}
                        used.update(a for a in self.service._cookie_files()
                                    if self.identity_key(a) in used_identities or self.profile_key(a) in used_profiles)
                        if account:
                            if not readonly:
                                from .account_scheduling import require_generation_account
                                require_generation_account(self.service, account)
                            if account in busy:
                                continue
                            cookie = self.service._account_cookie(account)
                        else:
                            cookie = await self.service._scheduled_cookie(data, exclude_accounts=busy | used)
                            if cookie is None:
                                if rotate and await self.service._scheduled_cookie(data, exclude_accounts=used) is None:
                                    from fastapi import HTTPException
                                    raise HTTPException(409, '没有未尝试的可用账号，已停止换号重试。')
                                continue  # Eligible accounts are busy, keep the job cancellable.
                    except Exception as exc:
                        error = exc
                    # Selection may await: a client can cancel during that await.
                    if self.service.get_job(job_id)['status'] != 'queued':
                        pending.remove(job_id)
                        self.service.queue.task_done()
                        continue
                    if len(self.active) >= self.max_concurrency:
                        break  # The setting may have been lowered while selection awaited.
                    pending.remove(job_id)
                    if cookie:
                        account = Path(cookie).stem
                        self.reservations[job_id] = (account, self.profile_key(account))
                    if self.min_job_interval:
                        self._next_dispatch_at = time.monotonic() + self.min_job_interval * random.uniform(1.0, 1.5)
                    self.active[job_id] = asyncio.create_task(
                        self._execute(job_id, cookie, error), name='flow-job-' + job_id)
                deadlines = []
                if self.min_job_interval:
                    remaining = self._next_dispatch_at - time.monotonic()
                    if remaining > 0:
                        deadlines.append(remaining)
                for job_id in pending:
                    job = self.service.get_job(job_id)
                    retry = (job.get('result') or {}).get('auto_retry') or {}
                    if job['status'] == 'queued' and retry.get('next_retry_at'):
                        seconds = (datetime.fromisoformat(retry['next_retry_at']) - datetime.now(timezone.utc)).total_seconds()
                        if seconds > 0:
                            deadlines.append(seconds)
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=min(deadlines) if deadlines else None)
                except asyncio.TimeoutError:
                    pass
        finally:
            tasks = list(self.active.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
