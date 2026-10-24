"""Offline contract tests. Browser/account fixtures are NOT provider evidence."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from flow_agent.console import ConsoleService,JobInput,create_console_router
from flow_agent.dispatch_quotes import QuoteInput,digest,normalized,PRICING
from flow_agent.task_scheduler import CURRENT_JOB
from flow_agent.engine import FlowEngine


class FakeEngine(FlowEngine):
    def __init__(self,root):
        super().__init__(root=root)
        self.calls=[]

    async def get_balance(self,refresh=False):
        return {'accounts':[],'total_credits':0,'unread':0}


@pytest.fixture
def svc(tmp_path,monkeypatch):
    s=ConsoleService(FakeEngine(tmp_path/'engine'))
    s.initialize()
    s.cookies_dir.mkdir(exist_ok=True)
    (s.cookies_dir/'account_fixture.json').write_text('{}')
    monkeypatch.setattr(s.preparation,'require_idle_queue',lambda:None)
    async def select(*args,**kwargs):return str(s.cookies_dir/'account_fixture.json')
    monkeypatch.setattr(s,'_scheduled_cookie',select)
    async def balance(account):return 100
    monkeypatch.setattr(s.dispatch_quotes,'live_balance',balance)
    async def browser_balance(*args):return 100
    monkeypatch.setattr(s.dispatch_quotes,'browser_balance',browser_balance)
    async def page(**args):
        assert args['check_only'] is True and args['_dispatch_probe'] is True
        return dict(check_only=True,generation_submitted=False,generation_requests=0,
            page_ready=True,browser_closed=True,account_id='account_fixture',
            submit_control_actionable=True,dispatch_balance=100,
            parameters={k:args[k] for k in ('aspect','duration','quality','count')} | {'model':args['video_model']})
    monkeypatch.setattr(s.official_videos,'run',page)
    return s


def spec(**changes):
    return QuoteInput(request=dict(kind='video',prompt='OFFLINE FIXTURE; never provider submitted',
        video_model='omni_flash',duration='10s',quality='720p',account_id=None,
        account_strategy='round_robin',**changes),max_credits=15,asset_id='fixture-1')


def prepared(s):
    inp=spec()
    q=asyncio.run(s.dispatch_quotes.prepare(inp))
    return q,JobInput.model_validate({**inp.request,'dispatch_quote_id':q['quoteId']})


def mutate(s,q,**updates):
    body=s.dispatch_quotes.get(q['quoteId'])
    body.pop('jobId')
    body.update(updates)
    with s.connect() as db:
        db.execute('update console_dispatch_quotes set body_json=? where id=?',(json.dumps(body),q['quoteId']))


def test_quote_binds_exact_request_and_creates_no_job(svc):
    q,data=prepared(svc)
    assert q['credits']==15 and q['providerEnforcedCap'] is False
    assert q['requestDigest']==digest(spec().request)
    assert data.account_id is None and q['accountId']=='account_fixture'
    assert svc.list_jobs()==[]
    assert svc.engine.calls==[]


def test_atomic_consume_and_original_job_lookup_after_response_loss(svc):
    q,data=prepared(svc)
    def consume(_):
        try:return svc.enqueue(data)['id']
        except HTTPException as e:return e.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(consume,range(2)))
    ids=[x for x in results if isinstance(x,str)]
    assert len(ids)==1 and 409 in results
    assert svc.dispatch_quotes.get(q['quoteId'])['jobId']==ids[0]
    assert len(svc.list_jobs())==1 and svc.engine.calls==[]


@pytest.mark.parametrize('change',[
    {'prompt':'different'}, {'duration':'8s'}, {'account_id':'account_other'},
    {'video_model':'veo_3_1_fast','duration':'8s'}, {'quality':'360p'},
    {'account_strategy':'highest_credits'},
])
def test_request_changes_refused_before_generation(svc,change):
    q,data=prepared(svc)
    changed=JobInput.model_validate({**data.model_dump(),**change})
    with pytest.raises(HTTPException):svc.enqueue(changed)
    assert svc.engine.calls==[] and svc.dispatch_quotes.get(q['quoteId'])['jobId'] is None


@pytest.mark.parametrize('reason',['expired','price','session','paused'])
def test_stale_or_changed_quote_fails_closed(svc,reason):
    q,data=prepared(svc)
    if reason=='expired':mutate(svc,q,expiresAt=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    if reason=='price':mutate(svc,q,pricingDigest='changed')
    if reason=='session':(svc.cookies_dir/'account_fixture.json').write_text('{"changed":true}')
    if reason=='paused':
        with svc.connect() as db:db.execute('insert or replace into console_meta values (?,?)',('automatic_account_policy',json.dumps({'paused_accounts':['account_fixture']})))
    with pytest.raises(HTTPException):svc.enqueue(data)
    assert svc.engine.calls==[]


def test_execute_refuses_wrong_account_or_low_balance(svc,monkeypatch):
    q,data=prepared(svc)
    job=svc.enqueue(data)
    with pytest.raises(HTTPException):asyncio.run(svc.dispatch_quotes.before_execute(data,job['id'],'account_wrong'))
    async def low(*args):return 0
    monkeypatch.setattr(svc.dispatch_quotes,'live_balance',low)
    with pytest.raises(HTTPException):asyncio.run(svc.dispatch_quotes.before_execute(data,job['id'],'account_fixture'))
    assert svc.engine.calls==[]


def test_provider_click_guard_is_same_context_and_one_shot(svc):
    q,data=prepared(svc)
    job=svc.enqueue(data)
    with svc.connect() as db:db.execute("update console_jobs set status='running' where id=?",(job['id'],))
    async def guarded():
        token=CURRENT_JOB.set(job['id'])
        try:
            await svc.dispatch_quotes.before_provider_click(object(),'account_fixture','fixture.invalid')
            with pytest.raises(HTTPException):
                await svc.dispatch_quotes.before_provider_click(object(),'account_fixture','fixture.invalid')
        finally:CURRENT_JOB.reset(token)
    asyncio.run(guarded())
    assert svc.engine.calls==[]


def test_unsupported_image_or_edit_never_invents_quote(svc):
    for request in (dict(kind='image',prompt='fixture',model='beluga'),
                    dict(kind='video',prompt='fixture',source_video_id='upload:fixture.mp4')):
        with pytest.raises(HTTPException):
            asyncio.run(svc.dispatch_quotes.prepare(QuoteInput(request=request,max_credits=100,asset_id='fixture')))
    assert svc.engine.calls==[]


def test_reservations_prevent_overbooking(svc):
    for _ in range(6):prepared(svc)
    with pytest.raises(HTTPException):prepared(svc)
    assert svc.engine.calls==[]


def test_legacy_and_restart_never_resubmit(svc):
    q,data=prepared(svc)
    job=svc.enqueue(data)
    old=svc.enqueue(JobInput(kind='image',prompt='legacy fixture',model='beluga'))
    restarted=ConsoleService(svc.engine)
    restarted.initialize()
    assert restarted.get_job(job['id'])['status']=='interrupted'
    assert restarted.get_job(old['id'])['status']=='interrupted'
    with pytest.raises(HTTPException):restarted.enqueue(data)
    assert restarted.dispatch_quotes.get(q['quoteId'])['jobId']==job['id']
    assert svc.engine.calls==[]


def test_real_router_contract_no_worker_no_charge(svc):
    app=FastAPI();app.include_router(create_console_router(svc))
    with TestClient(app) as c:
        q=c.post('/console/api/dispatch-quotes',json=spec().model_dump())
        assert q.status_code==200,q.text
        body={**spec().request,'dispatch_quote_id':q.json()['quoteId']}
        job=c.post('/console/api/jobs',json=body)
        assert job.status_code==202,job.text
        assert c.post('/console/api/jobs',json=body).status_code==409
        recovered=c.get('/console/api/dispatch-quotes/'+q.json()['quoteId']).json()
        assert recovered['jobId']==job.json()['id']
        assert c.get('/console/api/jobs/'+recovered['jobId']).status_code==200
    assert svc.engine.calls==[]


def test_mcp_video_without_irrelevant_image_model_uses_same_quote(svc):
    import httpx
    from flow_agent.console_mcp import ConsoleMCP
    app=FastAPI();app.include_router(create_console_router(svc))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            m=ConsoleMCP(client=client)
            q=await m.call('flow_prepare_dispatch_quote',spec().model_dump())
            job=await m.call('flow_generate_video',{'job':{**spec().request,
                'dispatch_quote_id':q['quoteId']},'acknowledge_possible_charge':True})
            assert job['status']=='queued'
            recovered=await m.call('flow_get_dispatch_quote',{'quote_id':q['quoteId']})
            assert recovered['jobId']==job['id']
    asyncio.run(exercise())
    assert svc.engine.calls==[]


def test_quote_then_expired_queue_never_reaches_provider(svc,monkeypatch):
    q,data=prepared(svc)
    job=svc.enqueue(data)
    mutate(svc,q,expiresAt=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    async def run():
        worker=asyncio.create_task(svc.run())
        try:await asyncio.wait_for(svc.queue.join(),2)
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(run())
    assert svc.get_job(job['id'])['status']=='failed'
    assert svc.engine.calls==[]


def test_provider_guard_refuses_canceled_or_underfunded_job(svc,monkeypatch):
    q,data=prepared(svc)
    job=svc.enqueue(data)
    async def run():
        token=CURRENT_JOB.set(job['id'])
        try:
            with pytest.raises(HTTPException):
                await svc.dispatch_quotes.before_provider_click(object(),'account_fixture','fixture.invalid')
            with svc.connect() as db:db.execute("update console_jobs set status='running' where id=?",(job['id'],))
            async def low(*args):return 0
            monkeypatch.setattr(svc.dispatch_quotes,'browser_balance',low)
            with pytest.raises(HTTPException):
                await svc.dispatch_quotes.before_provider_click(object(),'account_fixture','fixture.invalid')
        finally:CURRENT_JOB.reset(token)
    asyncio.run(run())
    assert svc.engine.calls==[]
