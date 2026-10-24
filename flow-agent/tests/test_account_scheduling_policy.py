import json
import pytest
from fastapi import HTTPException
from flow_agent.console import JobInput
from flow_agent.account_scheduling import KEY,candidates,snapshot
from flow_agent.account_scheduling import provider_blocks, require_generation_account
from tests.test_console import service

def policy(service,value):
    with service.connect() as db:db.execute('insert or replace into console_meta values (?,?)',(KEY,json.dumps(value)))

def rows():
    return [{'id':a,'credits':n,'selectable':True,'status':'active'} for a,n in [('bad',1050),('good',809),('other',500)]]

@pytest.mark.asyncio
@pytest.mark.parametrize('strategy',['round_robin','highest_credits','lowest_credits'])
async def test_actual_dispatch_prefers_good_and_never_paused(service,monkeypatch,strategy):
    monkeypatch.setattr('flow_agent.account_scheduling.browser_identity_ready',lambda service,account:True)
    policy(service,{'paused_accounts':['bad'],'preferred_accounts':['good']})
    monkeypatch.setattr(service,'accounts',lambda balance:{'accounts':rows()})
    monkeypatch.setattr(service,'_account_cookie',lambda account:account)
    expected = {'round_robin': ['good', 'other', 'good', 'other'],
                'highest_credits': ['good'] * 4, 'lowest_credits': ['other'] * 4}[strategy]
    actual = [await service._scheduled_cookie(JobInput(kind='video',prompt='local',account_strategy=strategy)) for _ in range(4)]
    assert actual == expected
    assert await service._scheduled_cookie(JobInput(kind='video',prompt='local'),exclude_accounts={'good'})=='other'

def test_pause_and_busy_before_preference(service):
    policy(service,{'paused_accounts':['bad','good'],'preferred_accounts':['good']})
    assert [a['id'] for a in candidates(service,rows())]==['other']
    assert candidates(service,rows(),{'other'})==[]

def test_default_policy_keeps_original_candidates(service):
    assert candidates(service,rows())==rows()

def test_startup_warning_excludes_even_preferred_account(service):
    policy(service, {'paused_accounts': ['bad'], 'preferred_accounts': ['good']})
    service.monitor.startup_issues['good'] = '核验未完成'
    assert [a['id'] for a in candidates(service, rows())] == ['other']

def test_unknown_preferred_account_falls_back(service):
    policy(service,{'paused_accounts':['bad'],'preferred_accounts':['missing']})
    assert [a['id'] for a in candidates(service,rows())]==['good','other']

def test_invalid_policy_blocks_dispatch(service):
    policy(service,{'paused_accounts':'not-a-list'})
    with pytest.raises(HTTPException):snapshot(service)

def test_snapshot_survives_restart_and_exposes_no_credentials(service):
    value={'paused_accounts':['bad'],'preferred_accounts':['good']};policy(service,value)
    assert service.scheduler.snapshot()['automatic_account_policy']==value

@pytest.mark.asyncio
async def test_incomplete_preferred_identity_is_skipped_before_dispatch(service,monkeypatch):
    policy(service,{'paused_accounts':['bad'],'preferred_accounts':['good']})
    monkeypatch.setattr(service,'accounts',lambda balance:{'accounts':rows()})
    monkeypatch.setattr(service,'_account_cookie',lambda account:account)
    monkeypatch.setattr('flow_agent.account_scheduling.browser_identity_ready',lambda service,account:account=='other')
    assert await service._scheduled_cookie(JobInput(kind='video',prompt='local'))=='other'

def test_saved_identity_requires_existing_profile(service,monkeypatch,tmp_path):
    from flow_agent.account_scheduling import browser_identity_ready
    monkeypatch.setattr(service,'_account_profile_name',lambda account:'test-profile')
    monkeypatch.setattr(service,'_verified_detail',lambda account:{'email':'test@example.com'})
    assert not browser_identity_ready(service,'test')

    (service.engine.root/'data'/'test-profile').mkdir(parents=True)
    assert browser_identity_ready(service,'test')
    monkeypatch.setattr(service,'_verified_detail',lambda account:None)
    assert not browser_identity_ready(service,'test')


def rejection(service, account='bad', job='rejected', created=None, code='unusual_activity', submitted=1, verified=True):
    from datetime import datetime, timezone
    created = created or datetime.now(timezone.utc).isoformat()
    request={'selected_account_id':account}
    result={'execution':{'account_id':account,'generation_requests':submitted,
        'generation_response':{'response_origin':'https://labs.google' if verified else 'other', 'rpc_errors':[{'code':7,'reason_codes':[code], 'upstream_markers':['PUBLIC_ERROR_UNUSUAL_ACTIVITY'] if verified and code=='unusual_activity' else []}]}}}
    with service.connect() as db:
        db.execute('insert into console_jobs (id,kind,status,request_json,result_json,created_at) values (?,?,?,?,?,?)',
            (job,'video','failed',json.dumps(request),json.dumps(result),created))


def test_persisted_provider_rejection_blocks_auto_and_explicit(service,monkeypatch):
    rejection(service)
    assert [a['id'] for a in candidates(service,rows())]==['good','other']
    monkeypatch.setattr(service,'_account_cookie',lambda account:account)
    with pytest.raises(HTTPException) as error:
        service.enqueue(JobInput(kind='video',prompt='no generation',account_id='bad'))
    assert error.value.status_code==409 and '本次未提交' in error.value.detail
    assert service.queue.empty() and service.engine.calls==[]
    assert provider_blocks(service)['bad']['job_id']=='rejected'


def test_explicit_paused_account_rejected_but_cookie_read_remains_available(service,monkeypatch):
    policy(service,{'paused_accounts':['bad'],'preferred_accounts':[]})
    monkeypatch.setattr(service,'_account_cookie',lambda account:account)
    with pytest.raises(HTTPException):require_generation_account(service,'bad')
    assert service._account_cookie('bad')=='bad'  # recovery/login remain usable


def test_unrecognized_error7_does_not_quarantine(service):
    rejection(service,code='unknown')
    assert not provider_blocks(service)


def test_legacy_classification_does_not_disable_account(service):
    rejection(service, verified=False)
    assert not provider_blocks(service)
    require_generation_account(service, 'bad')
    assert candidates(service, rows()) == rows()


def test_expired_rejection_does_not_disable_account(service):
    rejection(service, created='2020-01-01T00:00:00+00:00')
    assert not provider_blocks(service)
    require_generation_account(service, 'bad')


def test_later_unsubmitted_failure_does_not_clear_rejection(service):
    rejection(service)
    rejection(service,job='guard',created='2026-10-22T21:00:00+08:00',code='guard',submitted=0)
    assert 'bad' in provider_blocks(service)


def test_later_unknown_result_does_not_clear_rejection(service):
    rejection(service)
    rejection(service,job='unknown',created='2026-10-22T21:00:00+08:00',code='unknown')
    assert 'bad' in provider_blocks(service)


def test_older_result_recovered_later_does_not_clear_rejection(service):
    rejection(service)
    with service.connect() as db:
        db.execute('insert into console_jobs (id,kind,status,request_json,result_json,created_at,finished_at) values (?,?,?,?,?,?,?)',
            ('old','video','succeeded',json.dumps({'selected_account_id':'bad'}),
             json.dumps({'status':'succeeded','execution':{'generation_requests':1,'download_verified':True}}),
             '2026-10-22T10:00:00+08:00','2026-10-23T12:00:00+08:00'))
    assert 'bad' in provider_blocks(service)


@pytest.mark.asyncio
async def test_queued_batch_is_blocked_before_engine_when_account_becomes_abnormal(service,monkeypatch):
    import asyncio
    from pathlib import Path
    monkeypatch.setattr(service,'_account_cookie',lambda account:str(service.cookies_dir/(account+'.json')) if account else None)
    jobs=[service.enqueue(JobInput(kind='video',prompt='fixture',account_id='bad')) for _ in range(3)]
    rejection(service)
    worker=asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(service.queue.join(),5)
        assert all(service.get_job(j['id'])['status']=='failed' for j in jobs)
        assert service.engine.calls==[]
        assert all('本次未提交' in service.get_job(j['id'])['error'] for j in jobs)
    finally:
        worker.cancel()
        await asyncio.gather(worker,return_exceptions=True)
