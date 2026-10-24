import hashlib,json
import pytest
from fastapi import HTTPException
from tests.test_console import service
from flow_agent.account_repair import ensure_profile,seed_new_profile

def known(service,key='account_test',email='owner@example.test'):
    service.cookies_dir.mkdir(exist_ok=True)
    p=service.cookies_dir/(key+'.json');p.write_text('{}')
    with service.connect() as db:
        db.execute('insert into console_account_details values (?,?,?,?,?,?,?)',(key,hashlib.sha256(p.read_bytes()).hexdigest(),'Owner',email,None,'2026-10-23T00:00:00Z','session'))
    return p

def profile(service,name,email):
    p=service.engine.root/'data'/name/'Default';p.mkdir(parents=True)
    (p/'Preferences').write_text(json.dumps({'account_info':[{'email':email}]}))
    return p.parent

def test_missing_binding_creates_isolated_profile_without_changing_others(service):
    known(service)
    profile(service,'flow_browser_profile_1','other@example.test')
    with service.connect() as db:db.execute('insert into console_account_profiles values (?,?)',('account_other','flow_browser_profile_1'))
    assert '建立独立' in ensure_profile(service,'account_test')
    assert service._account_profile_name('account_test')!='flow_browser_profile_1'
    assert service._account_profile_name('account_other')=='flow_browser_profile_1'
    assert '现有' in ensure_profile(service,'account_test')

def test_unique_matching_email_can_restore_unowned_profile(service):
    known(service);profile(service,'flow_browser_profile_4','owner@example.test')
    assert '按邮箱恢复' in ensure_profile(service,'account_test')
    assert service._account_profile_name('account_test')=='flow_browser_profile_4'

def test_ambiguous_matches_stop_without_guessing(service):
    known(service)
    for n in (1,2):profile(service,'flow_browser_profile_'+str(n),'owner@example.test')
    with pytest.raises(HTTPException,match='多个'):ensure_profile(service,'account_test')

def test_missing_identity_is_not_guessed(service):
    known(service,email=None)
    with pytest.raises(HTTPException):ensure_profile(service,'account_test')

def test_identity_check_can_use_home_but_generation_still_requires_project(service,monkeypatch):
    from flow_agent.official_images import OfficialImageError
    known(service);ensure_profile(service,'account_test')
    monkeypatch.setattr(service,'_find_extension_browser',lambda: 'fixture-chrome.exe')
    monkeypatch.setattr('flow_agent.official_images.profile_in_use',lambda p:False)
    with pytest.raises(OfficialImageError) as exc:
        service.official_images.settings('account_test')
    assert exc.value.code == 'project_missing'
    assert service.official_images.settings('account_test',allow_identity_home=True)[3]=='https://flow.google.com/'

@pytest.mark.asyncio
async def test_cookie_seed_only_once_and_only_owned_google_domains(service):
    p=known(service);ensure_profile(service,'account_test')
    target=service.engine.root/'data'/service._account_profile_name('account_test')
    p.write_text(json.dumps({'cookies':[{'name':'TEST','value':'PRIVATE','domain':'.google.com'}, {'name':'OTHER','value':'PRIVATE','domain':'example.com'}]}))
    class Context:
        calls=[]
        async def add_cookies(self,values):self.calls.append(values)
    ctx=Context()
    await seed_new_profile(service,'account_test',target,ctx,p)
    await seed_new_profile(service,'account_test',target,ctx,p)
    assert len(ctx.calls)==1 and len(ctx.calls[0])==1
    assert not service.list_jobs()
