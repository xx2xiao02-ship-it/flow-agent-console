"""Remove console registrations with a local recovery archive; never log out Chrome."""
from __future__ import annotations

import json
import os
import re
import uuid

from fastapi import HTTPException


PREFIX = 'removed_account:'


class AccountRemoval:
    def __init__(self, service):
        self.service = service
        self.archive_dir = service.engine.root / 'data' / 'removed_accounts'

    def records(self) -> dict[str, dict]:
        with self.service.connect() as db:
            rows = db.execute('select key,value from console_meta where key glob ?', (PREFIX + '*',)).fetchall()
        return {row['key'][len(PREFIX):]: json.loads(row['value']) for row in rows}

    def snapshot(self) -> list[dict]:
        return [{'id': key, 'label': row['label'], 'email': row['email'], 'removed_at': row['removed_at']}
                for key, row in self.records().items()]

    def require_idle(self):
        svc = self.service
        with svc.connect() as db:
            active = db.execute("select 1 from console_jobs where status in ('queued','running') limit 1").fetchone()
        if (active or svc.preparation.running or svc.balance_refresh_lock.locked() or svc.monitor.running
                or svc.browser_execution.handoff_active
                or svc.account_sync_task and not svc.account_sync_task.done()):
            raise HTTPException(409, '有任务、账号检测或连接正在进行，请结束后再删除或恢复账号。')

    def remove(self, account_id: str, confirmed: bool):
        from .console import utc_now
        if not confirmed:
            raise HTTPException(422, '请先确认删除本地账号记录，素材、任务历史和浏览器登录将保留。')
        self.require_idle()
        svc = self.service
        path = svc._cookie_files().get(account_id)
        if path is None:
            raise HTTPException(404, '该本地账号记录不存在或已删除。')
        if path.is_symlink() or path.resolve().parent != svc.cookies_dir.resolve():
            raise HTTPException(422, '账号文件位置异常，未执行删除。')
        account = next(a for a in svc.accounts({'accounts': []})['accounts'] if a['id'] == account_id)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        name = uuid.uuid4().hex + '.json'
        archive = self.archive_dir / name
        record = {'label': account['label'], 'email': account['email'], 'removed_at': utc_now(), 'archive': name}
        # Commit the exclusion before moving. On a failed move, roll back the
        # exclusion; a process interruption still cannot re-enable this account.
        with svc.connect() as db:
            db.execute('insert into console_meta values (?,?)', (PREFIX + account_id, json.dumps(record)))
        try:
            path.replace(archive)
        except OSError as exc:
            with svc.connect() as db:
                db.execute('delete from console_meta where key=?', (PREFIX + account_id,))
            raise HTTPException(503, '无法归档账号记录，未删除；请检查本地文件权限。') from exc
        svc.details_attempts.pop(account_id, None)
        from . import proxy_pool
        proxy_pool.forget_account(svc, account_id)
        return {'removed': True, 'account_id': account_id, 'recoverable': True}

    def restore(self, account_id: str, confirmed: bool):
        if not confirmed:
            raise HTTPException(422, '请先确认恢复此账号到本地账号列表和调度。')
        self.require_idle()
        record = self.records().get(account_id)
        if record is None:
            raise HTTPException(404, '找不到已删除的账号记录。')
        if not re.fullmatch(r'account_[A-Za-z0-9_-]+', account_id):
            raise HTTPException(422, '账号记录编号无效。')
        name = record['archive']
        if not re.fullmatch(r'[0-9a-f]{32}\.json', name):
            raise HTTPException(422, '恢复文件编号无效。')
        archive = self.archive_dir / name
        target = self.service.cookies_dir / (account_id + '.json')
        if target.is_symlink() or archive.is_symlink():
            raise HTTPException(422, '账号文件位置异常，未执行恢复。')
        # Prefer a newly synchronized session over the archived one. Never
        # overwrite it, nor remove browser profiles, jobs, media or engine rows.
        try:
            if not target.exists():
                if not archive.is_file():
                    raise HTTPException(404, '本地恢复文件缺失，请重新连接账号。')
                self.service.cookies_dir.mkdir(parents=True, exist_ok=True)
                raw = archive.read_bytes()
                temporary = self.service.cookies_dir / ('.restore-' + uuid.uuid4().hex + '.tmp')
                try:
                    with temporary.open('xb') as output:
                        output.write(raw)
                    # Atomic creation without overwriting a session concurrently
                    # synchronized by Chrome. A partial write is never visible.
                    os.link(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(503, '无法恢复本地账号文件，请检查文件权限后重试；归档仍保留。') from exc
        with self.service.connect() as db:
            db.execute('delete from console_meta where key=?', (PREFIX + account_id,))
        self.service.details_attempts.pop(account_id, None)
        return {'restored': True, 'account_id': account_id}
