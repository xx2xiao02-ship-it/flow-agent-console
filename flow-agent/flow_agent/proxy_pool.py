"""Per-account proxy binding for Google Flow traffic.

Each account sticks to one dedicated proxy exit so distinct accounts never
share an IP and one account never jumps between IPs. The pool is user-supplied;
credentials stay in the local database and are never returned to the UI. With
an empty pool every caller behaves exactly as before.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

PROBE_URL = 'https://flow.google.com/robots.txt'
VALID_SCHEMES = ('http', 'https', 'socks5')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse(value: str) -> dict:
    """Validate a proxy URL and split credentials for Playwright/Chromium."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('代理地址不能为空')
    raw = value.strip()
    split = urlsplit(raw if '://' in raw else 'http://' + raw)
    if split.scheme not in VALID_SCHEMES:
        raise ValueError('代理协议仅支持 http / https / socks5')
    if not split.hostname or not split.port:
        raise ValueError('代理缺少主机或端口')
    if split.username and split.path:
        raise ValueError('代理地址格式不正确')
    return {'scheme': split.scheme, 'hostname': split.hostname,
            'port': split.port, 'username': split.username or '',
            'password': split.password or ''}


def masked(value: str) -> str:
    """Credentials never leave the backend; only a masked form is displayed."""
    try:
        parts = parse(value)
    except ValueError:
        return '(无效代理)'
    auth = f'{parts["username"]}:***@' if parts['username'] else ''
    return f'{parts["scheme"]}://{auth}{parts["hostname"]}:{parts["port"]}'


def playwright_proxy(url: str) -> dict | None:
    parts = parse(url)
    server = f'{parts["scheme"]}://{parts["hostname"]}:{parts["port"]}'
    result = {'server': server}
    if parts['username']:
        result['username'] = parts['username']
        result['password'] = parts['password']
    return result


def _rows(service) -> list[dict]:
    with service.connect() as db:
        return [dict(row) for row in db.execute(
            'select id, url, added_at, last_ok_at, last_error from proxy_pool order by added_at')]


def bindings(service) -> dict[str, str]:
    with service.connect() as db:
        rows = db.execute('select account_key, proxy_id from console_account_proxy').fetchall()
    urls = {row['id']: row['url'] for row in _rows(service)}
    return {account: urls.get(proxy_id, '') for account, proxy_id in rows}


def snapshot(service) -> dict:
    items = [{**{key: row[key] for key in ('id', 'added_at', 'last_ok_at', 'last_error')},
              'proxy': masked(row['url'])} for row in _rows(service)]
    bound = {account: masked(url) for account, url in bindings(service).items() if url}
    return {'proxies': items, 'bindings': bound,
            'accounts': sorted(service._cookie_files())}


def add(service, value: str) -> dict:
    try:
        parts = parse(value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    ident = hashlib.sha256((parts['scheme'] + parts['hostname'] + str(parts['port'])
                            + parts['username'] + parts['password']).encode()).hexdigest()[:16]
    # Store the scheme-normalized URL: httpx rejects schemeless proxies even
    # though parse() accepts them as a shorthand for http.
    url = value.strip()
    if '://' not in url:
        url = parts['scheme'] + '://' + url
    try:
        with service.connect() as db:
            db.execute('insert or replace into proxy_pool (id, url, added_at, last_ok_at, last_error)'
                       ' values (?, ?, ?, null, null)', (ident, url, utc_now()))
    except sqlite3.IntegrityError:
        raise HTTPException(400, '代理地址无法保存') from None
    return snapshot(service)


def remove(service, ident: str) -> dict:
    with service.connect() as db:
        db.execute('delete from proxy_pool where id=?', (ident,))
        db.execute('delete from console_account_proxy where proxy_id=?', (ident,))
    return snapshot(service)


def _bound_count(db) -> dict[str, int]:
    return dict(db.execute('select proxy_id, count(*) from console_account_proxy group by proxy_id').fetchall())


def proxy_for(service, account: str) -> dict | None:
    """The account's sticky proxy; auto-assigns the least-used exit once."""
    if not account:
        return None
    with service.connect() as db:
        row = db.execute('select proxy_id from console_account_proxy where account_key=?',
                         (account,)).fetchone()
        pool = db.execute('select id, url, added_at from proxy_pool order by added_at').fetchall()
        if not pool:
            return None
        if row:
            proxy_id = row[0]
            match = next((item for item in pool if item['id'] == proxy_id), None)
            if match is None:  # Proxy was removed; fall through to reassignment.
                db.execute('delete from console_account_proxy where account_key=?', (account,))
            else:
                return playwright_proxy(match['url'])
        used = _bound_count(db)
        chosen = min(pool, key=lambda item: (used.get(item['id'], 0), item['added_at']))
        db.execute('insert or replace into console_account_proxy values (?, ?, ?)',
                   (account, chosen['id'], utc_now()))
        return playwright_proxy(chosen['url'])


def raw_url_for(service, account: str | None) -> str | None:
    """The account's proxy URL including credentials, for httpx clients."""
    if not account:
        return None
    if proxy_for(service, account) is None:
        return None
    with service.connect() as db:
        row = db.execute('select p.url from proxy_pool p'
                         ' join console_account_proxy b on b.proxy_id = p.id'
                         ' where b.account_key=?', (account,)).fetchone()
    if not row:
        return None
    # Rows saved before scheme normalization existed.
    return row[0] if '://' in row[0] else 'http://' + row[0]


def rebind(service, account: str, ident: str | None = None) -> dict:
    """Manual exit change: pick a specific proxy, or the least-used other one."""
    with service.connect() as db:
        pool = db.execute('select id, url, added_at from proxy_pool order by added_at').fetchall()
        if not pool:
            raise HTTPException(409, '代理池为空，请先添加代理。')
        if ident:
            if not any(item['id'] == ident for item in pool):
                raise HTTPException(404, '代理不存在或已删除。')
        else:
            current = db.execute('select proxy_id from console_account_proxy where account_key=?',
                                 (account,)).fetchone()
            used = _bound_count(db)
            candidates = [item for item in pool if not current or item['id'] != current[0]]
            if not candidates:
                candidates = pool
            chosen = min(candidates, key=lambda item: (used.get(item['id'], 0), item['added_at']))
            ident = chosen['id']
        db.execute('insert or replace into console_account_proxy values (?, ?, ?)',
                   (account, ident, utc_now()))
    return snapshot(service)


def unbind(service, account: str) -> dict:
    with service.connect() as db:
        db.execute('delete from console_account_proxy where account_key=?', (account,))
    return snapshot(service)


def forget_account(service, account: str) -> None:
    with service.connect() as db:
        db.execute('delete from console_account_proxy where account_key=?', (account,))


def test(service, ident: str) -> dict:
    with service.connect() as db:
        row = db.execute('select url from proxy_pool where id=?', (ident,)).fetchone()
    if row is None:
        raise HTTPException(404, '代理不存在或已删除。')
    started = time.monotonic()
    error = None
    # Rows saved before scheme normalization existed need the same fix-up.
    url = row['url'] if '://' in row['url'] else 'http://' + row['url']
    try:
        with httpx.Client(proxy=url, timeout=12, follow_redirects=False) as client:
            response = client.get(PROBE_URL)
            response.raise_for_status()
    except Exception as exc:
        error = type(exc).__name__
    latency_ms = round((time.monotonic() - started) * 1000) if error is None else None
    with service.connect() as db:
        db.execute('update proxy_pool set last_ok_at=?, last_error=? where id=?',
                   (utc_now() if error is None else None, error, ident))
    return {'id': ident, 'ok': error is None, 'latency_ms': latency_ms,
            'error': error, **{'snapshot': snapshot(service)}}
