"""Repair dedicated profile ownership without guessing another account's identity."""
import json
from fastapi import HTTPException


def ensure_profile(service, account):
    detail = service._verified_detail(account) or {}
    email = (detail.get('email') or '').casefold()
    if not email:
        raise HTTPException(409, '没有可核对的账号邮箱，无法自动关联浏览器；请先完成账号连接。')
    current = service._account_profile_name(account)
    root = service.engine.root / 'data'
    with service.connect() as db:
        owners = dict(db.execute('select account_key,profile_name from console_account_profiles'))
    def available(name):
        path = root / name
        if not path.is_dir() or path.is_symlink():
            return False
        return not any(key != account and profile == name for key,profile in owners.items())
    if current and available(current):
        _, actual = service._profile_identity(current)
        if (not actual and owners.get(account) == current) or actual and actual.casefold() == email:
            if owners.get(account) != current:
                with service.connect() as db:
                    db.execute('insert or replace into console_account_profiles values (?,?)',(account,current))
                return '已按邮箱恢复专用浏览器关联'
            return '已确认现有专用浏览器关联'
    matches = []
    for path in root.glob('flow_browser_profile_*'):
        if not available(path.name):
            continue
        _, actual = service._profile_identity(path.name)
        if actual and actual.casefold() == email:
            matches.append(path.name)
    if len(matches) > 1:
        raise HTTPException(409, '存在多个同邮箱的浏览器资料，无法确定应使用哪一份；请手动选择连接，不会借用其他账号。')
    name = matches[0] if matches else service._new_browser_profile().name
    with service.connect() as db:
        db.execute('insert or replace into console_meta values (?,?)', ('profile_binding_before_repair:'+account,current or ''))
        db.execute('insert or replace into console_account_profiles values (?,?)', (account,name))
        if not matches:
            db.execute('insert or replace into console_meta values (?,?)', ('profile_seed_pending:'+account,name))
    return '已按邮箱恢复专用浏览器关联' if matches else '已建立独立浏览器资料，准备使用已保存会话核验'


async def seed_new_profile(service, account, profile, context, cookie):
    """Seed once into a new owned profile, never overwrite an existing login."""
    key = 'profile_seed_pending:'+account
    with service.connect() as db:
        row = db.execute('select value from console_meta where key=?',(key,)).fetchone()
    if not row or row[0] != profile.name:
        return
    bundle = json.loads(cookie.read_bytes())
    cookies=[]
    for item in bundle.get('cookies',[]):
        if not isinstance(item,dict) or item.get('domain','').lstrip('.') not in ('google.com','flow.google.com','labs.google','accounts.google.com'):
            continue
        if not isinstance(item.get('name'),str) or not isinstance(item.get('value'),str):
            continue
        c={k:item[k] for k in ('name','value','domain')}
        c['path']=item.get('path') or '/'
        for k in ('secure','httpOnly'):
            if isinstance(item.get(k),bool): c[k]=item[k]
        if item.get('sameSite') in ('Strict','Lax','None'): c['sameSite']=item['sameSite']
        expiry=item.get('expires',item.get('expirationDate'))
        if isinstance(expiry,(int,float)) and expiry>0: c['expires']=expiry
        cookies.append(c)
    if cookies:
        await context.add_cookies(cookies)
    with service.connect() as db:
        db.execute('delete from console_meta where key=? and value=?',(key,profile.name))
