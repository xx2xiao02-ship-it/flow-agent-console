"""Local automatic-dispatch overrides; never mutate sessions or explicit accounts."""
import json
from datetime import datetime, timedelta, timezone
KEY='automatic_account_policy'
COOLDOWN_SECONDS = 1800
ATTENTION_PREFIX = 'manual_attention:'


def snapshot(service):
    with service.connect() as db:
        row=db.execute('select value from console_meta where key=?',(KEY,)).fetchone()
    if not row:return {'paused_accounts':[], 'preferred_accounts':[]}
    try:
        data=json.loads(row[0])
        if not isinstance(data,dict):raise ValueError('invalid policy')
        result={}
        for key in ('paused_accounts','preferred_accounts'):
            values=data.get(key,[])
            if not isinstance(values,list) or not all(isinstance(v,str) for v in values):raise ValueError('invalid policy')
            result[key]=list(dict.fromkeys(values))
        return result
    except (ValueError,TypeError):
        from fastapi import HTTPException
        raise HTTPException(409,'自动调度策略无法读取，请检查设置；未提交生成。') from None


def mark_manual_attention(service, account, reason, job_id=None):
    """Pull the account out of the dispatch pool until a fresh check passes."""
    if not account:
        return
    with service.connect() as db:
        db.execute('insert or replace into console_meta values (?,?)',
                   (ATTENTION_PREFIX + account, json.dumps(
                       {'reason': reason, 'job_id': job_id,
                        'at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False)))


def clear_manual_attention(service, account):
    if not account:
        return
    with service.connect() as db:
        db.execute('delete from console_meta where key=?', (ATTENTION_PREFIX + account,))


def manual_attention(service):
    with service.connect() as db:
        rows = db.execute('select key, value from console_meta where key glob ?',
                          (ATTENTION_PREFIX + '*',)).fetchall()
    result = {}
    for row in rows:
        try:
            data = json.loads(row[1])
        except ValueError:
            continue
        result[row[0][len(ATTENTION_PREFIX):]] = data
    return result


def candidates(service,eligible,excluded=None):
    from .account_identity import aliases
    mapping=aliases(service)
    policy=snapshot(service)
    blocked=set(policy['paused_accounts'])|set(excluded or ())
    blocked.update(provider_blocks(service))
    blocked.update(manual_attention(service))
    blocked.update(key for key in getattr(getattr(service, 'monitor', None), 'startup_issues', {})
                   if mapping.get(key,key)==key)
    blocked={mapping.get(key,key) for key in blocked}
    eligible=[a for a in eligible if a['id'] not in blocked]
    preferred = {mapping.get(account, account): index for index, account in enumerate(policy['preferred_accounts'])}
    if preferred:
        eligible.sort(key=lambda a: (preferred.get(a['id'], len(preferred)), a['id']))
    return eligible

def provider_blocks(service):
    """Short local cooldown, only with exact provider marker and verified origin.

    Legacy classifications are insufficient evidence for an account-wide block.
    Expiry permits scheduling; it does not claim provider restrictions cleared.
    """
    with service.connect() as db:
        rows = db.execute("select id,created_at,finished_at,request_json,result_json from console_jobs where status in ('failed','succeeded') order by created_at desc,rowid desc").fetchall()
    seen, blocked = set(), {}
    for row in rows:
        request, result = json.loads(row['request_json']), json.loads(row['result_json'] or '{}')
        execution = (result or {}).get('execution') or {}
        account = execution.get('account_id') or request.get('selected_account_id')
        if not account or account in seen or not execution.get('generation_requests'):
            continue
        errors = (execution.get('generation_response') or {}).get('rpc_errors') or []
        response = execution.get('generation_response') or {}
        if (response.get('response_origin') == 'https://labs.google' and
                any('PUBLIC_ERROR_UNUSUAL_ACTIVITY' in e.get('upstream_markers', []) for e in errors if isinstance(e, dict))):
            try:
                at = datetime.fromisoformat(row['finished_at'] or row['created_at'])
                if at.tzinfo is None:
                    continue
                until = at + timedelta(seconds=COOLDOWN_SECONDS)
            except (TypeError, ValueError):
                continue
            if until > datetime.now(timezone.utc):
                blocked[account] = {'reason': 'recent_request_rejected', 'job_id': row['id'], 'until': until.isoformat()}
            seen.add(account)
        elif (result or {}).get('status') == 'succeeded' and execution.get('download_verified'):
            seen.add(account)
    return blocked


def require_generation_account(service, account):
    if not account:
        return
    from .account_identity import aliases
    mapping=aliases(service)
    attention = manual_attention(service)
    if mapping.get(account,account) in {mapping.get(key,key) for key in attention}:
        from fastapi import HTTPException
        reason = (attention.get(account) or {}).get('reason') or '人工验证'
        raise HTTPException(409, f'账号 {account} 需要人工处理（{reason}），已暂停调度；请到账号管理完成登录或验证后再试。本次未提交。')
    blocked={mapping.get(key,key) for key in [*snapshot(service)['paused_accounts'],*provider_blocks(service)]}
    if mapping.get(account,account) in blocked:
        from fastapi import HTTPException
        raise HTTPException(409, f'账号 {account} 处于本地调度暂停或30分钟请求冷却期；本次未提交。这不是账号封禁判定；请使用自动调度或等待冷却结束，原任务仍可只读查询。')


def browser_identity_ready(service, account):
    """Check saved identity/profile binding only; never launch or generate."""
    profile_name = service._account_profile_name(account)
    detail = service._verified_detail(account)
    if not profile_name or not detail or not detail.get('email'):
        return False
    profile = service.engine.root / 'data' / profile_name
    return profile.is_dir() and not profile.is_symlink()
