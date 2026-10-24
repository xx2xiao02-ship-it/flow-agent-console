"""Additive, persisted one-shot dispatch quotes; never a generation retry API.

Credits are published estimates, not a provider-enforced billing cap. A quote
proves the exact model/options were checked on one account without generation.
Legacy jobs and recovery requests do not use this table.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .account_details import FlowAccountReader, AccountReadError
from .account_scheduling import require_generation_account
from .credit_pricing import OMNI_COSTS, VEO_COSTS, SOURCE_URL, PRICING


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request: dict
    max_credits: int = Field(ge=0, strict=True)
    asset_id: str = Field(min_length=1, max_length=128)


def normalized(data):
    body=data.model_dump(exclude={'dispatch_quote_id'})
    if data.kind=='video':
        body.pop('model',None)  # MCP's image default is immaterial to video.
    return body


def estimate(data):
    if data.kind != 'video':
        raise HTTPException(422, '图片尚无本接口可核验的实时积分报价，不能默认免费或填写估算数字；未生成。')
    if data.video_mode == 'edit':
        raise HTTPException(422, '编辑模式尚无无生成的精确参数检查，不签发报价；旧任务仍可查询。')
    if data.video_model == 'omni_flash':
        cost = OMNI_COSTS[data.duration][data.quality]
    else:
        cost = VEO_COSTS[data.video_model.removeprefix('veo_3_1_')]['non_ultra']
    return cost * data.count


class DispatchQuotes:
    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()

    def initialize(self, db):
        db.execute('''create table if not exists console_dispatch_quotes (
            id text primary key, body_json text not null, normalized_digest text not null,
            session_digest text not null, job_id text unique, attempt_started integer not null default 0)''')

    async def browser_balance(self, page, account, email):
        """Use the *current verified browser*, not a cached file balance."""
        tokens = await page.evaluate('''() => {const w=window.WIZ_global_data||{};
            return {at:w.SNlM0e||'',fsid:w.FdrFJe||''};}''')
        bundle = {**tokens, 'cookies':await page.context.cookies(),
            'fingerprint':{'user_agent':await page.evaluate('navigator.userAgent')}}
        try:
            from . import proxy_pool
            proxy = proxy_pool.raw_url_for(self.service, account)
            async with httpx.AsyncClient(proxy=proxy, timeout=30,
                                         trust_env=proxy is None) as client:
                detail = await FlowAccountReader(client,bundle).read()
        except (AccountReadError, OSError, ValueError, TypeError):
            raise HTTPException(409,'当前浏览器身份/积分核验失败；未生成。') from None
        if (detail.get('error') or not email or not detail.get('email')
                or email.casefold()!=detail['email'].casefold()
                or type(detail.get('credits')) is not int or detail['credits']<0):
            raise HTTPException(409,'当前浏览器身份或实时积分不一致；未生成。')
        return detail['credits']

    def get(self, quote_id, db=None):
        if db is None:
            with self.service.connect() as connection:
                return self.get(quote_id, connection)
        row = db.execute('select * from console_dispatch_quotes where id=?', (quote_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '报价不存在；未重新提交。')
        return {**json.loads(row['body_json']), 'jobId': row['job_id']}

    def _session_digest(self, account):
        return hashlib.sha256(Path(self.service._account_cookie(account)).read_bytes()).hexdigest()

    async def live_balance(self, account):
        expected = self.service._verified_detail(account) or {}
        cookie = Path(self.service._account_cookie(account))
        before = self._session_digest(account)
        try:
            from . import proxy_pool
            proxy = proxy_pool.raw_url_for(self.service, account)
            async with httpx.AsyncClient(proxy=proxy, timeout=30,
                                         trust_env=proxy is None) as client:
                detail = await FlowAccountReader(client, json.loads(cookie.read_bytes())).read()
        except (AccountReadError, OSError, ValueError, TypeError):
            raise HTTPException(409, '无法核验当前账号身份与积分；未提交生成。') from None
        if (before != self._session_digest(account) or detail.get('error')
                or not expected.get('email') or not detail.get('email')
                or detail['email'].casefold() != expected['email'].casefold()
                or type(detail.get('credits')) is not int or detail['credits'] < 0):
            raise HTTPException(409, '账号身份、会话或实时积分未通过核验；未提交生成。')
        return detail['credits']

    def _reserved(self, db, account, exclude=None):
        now = datetime.now(timezone.utc)
        total = 0
        for row in db.execute('''select q.id,q.body_json,q.job_id,q.attempt_started,j.status
                from console_dispatch_quotes q left join console_jobs j on j.id=q.job_id'''):
            body = json.loads(row['body_json'])
            if body['accountId'] != account or row['id'] == exclude:
                continue
            # A previously submitted/unknown outcome keeps its reservation. A
            # timeout or failure is never evidence that the provider refunded it.
            active = row['job_id'] and (row['status'] not in ('succeeded', 'canceled') or row['attempt_started'] and row['status']=='canceled')
            if active or (not row['job_id'] and datetime.fromisoformat(body['expiresAt']) > now):
                total += body['credits']
        return total

    async def prepare(self, spec):
        from .console import JobInput
        self.service.startup.require_ready()
        self.service.media_storage.migration.require_available()
        raw = spec.request
        if set(raw) - (set(JobInput.model_fields) - {'dispatch_quote_id'}):
            raise HTTPException(422, '报价请求包含未知或提交专用字段。')
        data = JobInput.model_validate(raw)
        if data.count != 1 or data.auto_retry_limit or data.retry_uncertain or data.retry_switch_accounts:
            raise HTTPException(422, '一次性报价仅允许单份产物且禁用所有重试和切号。')
        credits = estimate(data)
        if credits > spec.max_credits:
            raise HTTPException(409, '本次估算超过批准积分额度；未生成。')
        async with self.lock:
            self.service.preparation.require_idle_queue()
            self.service.validate(data)
            cookie = (self.service._account_cookie(data.account_id) if data.account_id else
                      await self.service._scheduled_cookie(data, exclude_accounts=self.service.scheduler.busy_accounts()))
            if not cookie:
                raise HTTPException(409, '当前无空闲账号；未签发报价，未生成。')
            account = Path(cookie).stem
            require_generation_account(self.service, account)
            session = self._session_digest(account)
            balance = await self.live_balance(account)
            with self.service.connect() as db:
                if balance - self._reserved(db, account) < credits:
                    raise HTTPException(409, '扣除已保留任务后可用积分不足；未生成。')
            options = dict(cookies=cookie, prompt=data.prompt, aspect=data.aspect,
                           count=data.count, check_only=True, _dispatch_probe=True)
            options.update(duration=data.duration, quality=data.quality, video_model=data.video_model,
                start_image=self.service._input_image(data.start_image_id),
                end_image=self.service._input_image(data.end_image_id),
                references=[self.service._input_image(ref) for ref in data.reference_image_ids])
            report = await self.service.official_videos.run(**options)
            if not (report.get('check_only') is True and report.get('generation_submitted') is False
                    and report.get('generation_requests') == 0 and report.get('page_ready') is True
                    and report.get('browser_closed') is True and report.get('account_id') == account
                    and report.get('submit_control_actionable') is True):
                raise HTTPException(409, '无生成的模型参数检查未通过；不签发报价。')
            balance = report.get('dispatch_balance')
            if type(balance) is not int or balance<0:
                raise HTTPException(409,'没有当前浏览器的实时积分证据；未生成。')
            params = report.get('parameters') or {}
            if any(params.get(key) != value for key, value in {
                    'model':data.video_model, 'aspect':data.aspect, 'duration':data.duration,
                    'quality':data.quality, 'count':data.count}.items()):
                raise HTTPException(409, '页面实际模型参数与报价请求不一致；未生成。')
            if session != self._session_digest(account):
                raise HTTPException(409, '检查过程中账号会话变化；报价失效。')
            now = datetime.now(timezone.utc)
            body = dict(version=2, quoteId=str(uuid.uuid4()), requestDigest=digest(raw),
                assetId=spec.asset_id,
                capturedAt=now.isoformat(), expiresAt=(now+timedelta(minutes=15)).isoformat(),
                accountId=account, accountStrategy=data.account_strategy,
                accountModelAccessVerified=True, credits=credits, maxCredits=spec.max_credits,
                source=SOURCE_URL, priceBasis='published_conservative_estimate',
                providerEnforcedCap=False, pricingDigest=digest(PRICING),
                accessEvidence={'method':'official_page_check_only', 'generationRequests':0,
                    'parameters':{k:params[k] for k in ('model','aspect','duration','quality','count')}},
                request=raw)
            with self.service.connect() as db:
                # No await inside this write transaction. Old quotes/jobs are untouched.
                db.execute('begin immediate')
                if balance-self._reserved(db,account) < credits:
                    raise HTTPException(409, '积分已被其它报价保留；未生成。')
                db.execute('''insert into console_dispatch_quotes
                    (id,body_json,normalized_digest,session_digest,job_id) values (?,?,?,?,null)''',
                    (body['quoteId'],json.dumps(body,ensure_ascii=False),digest(normalized(data)),session))
            return body

    def check(self, data, db, *, job_id=None, account=None):
        row = db.execute('select * from console_dispatch_quotes where id=?', (data.dispatch_quote_id,)).fetchone()
        if row is None:
            raise HTTPException(409, '报价不存在；未生成。')
        q = json.loads(row['body_json'])
        if row['job_id'] != job_id:
            raise HTTPException(409, '报价已被消费；只查询报价关联的原任务，不可重发。')
        now = datetime.now(timezone.utc)
        if (not datetime.fromisoformat(q['capturedAt']) <= now < datetime.fromisoformat(q['expiresAt'])
                or row['normalized_digest'] != digest(normalized(data))
                or row['session_digest'] != self._session_digest(q['accountId'])
                or q['pricingDigest'] != digest(PRICING) or q['credits'] != estimate(data)
                or q['credits'] > q['maxCredits'] or account not in (None,q['accountId'])):
            raise HTTPException(409, '报价过期、请求/价格/会话或账号发生变化；未生成。')
        require_generation_account(self.service, q['accountId'])
        return q

    async def before_execute(self, data, job_id, account):
        with self.service.connect() as db:
            q = self.check(data, db, job_id=job_id, account=account)
        balance = await self.live_balance(account)
        with self.service.connect() as db:
            self.check(data, db, job_id=job_id, account=account)
            if balance-self._reserved(db,account,exclude=q['quoteId']) < q['credits']:
                raise HTTPException(409, '执行前实时可用积分不足；未生成，不自动切号。')

    async def before_provider_click(self, page, account, email):
        from .task_scheduler import CURRENT_JOB
        from .console import JobInput
        job_id = CURRENT_JOB.get()
        if not job_id:
            return  # Preserve the pre-existing non-quoted entry points.
        job = self.service.get_job(job_id)
        data = JobInput.model_validate(job['request'])
        if not data.dispatch_quote_id:
            return
        balance = await self.browser_balance(page,account,email)
        with self.service.connect() as db:
            db.execute('begin immediate')
            q = self.check(data,db,job_id=job_id,account=account)
            status = db.execute('select status from console_jobs where id=?',(job_id,)).fetchone()
            if not status or status[0]!='running' or balance-self._reserved(db,account,exclude=q['quoteId'])<q['credits']:
                raise HTTPException(409,'提交前任务状态或积分变化；未生成。')
            changed=db.execute('''update console_dispatch_quotes set attempt_started=1
                where id=? and attempt_started=0''',(q['quoteId'],)).rowcount
            if changed!=1:
                raise HTTPException(409,'原报价已经开始过生成；只查询原任务，不重新生成。')
