import hashlib,json
from tests.test_console import service
from tests.test_account_repair import known,profile
from flow_agent.account_identity import aliases

def ready(service,key,email='same@example.test',credits=1050):
    path=known(service,key,email)
    path.write_text(json.dumps({'cookies':[{'name':'SID','value':'fixture'}],'at':'fixture','fsid':'fixture','project_id':'12345678-1234-1234-1234-123456789012','fingerprint':{'user_agent':'fixture'}}))
    with service.connect() as db:
        db.execute('update console_account_details set bundle_digest=?,credits=?,error=NULL where account_key=?',(hashlib.sha256(path.read_bytes()).hexdigest(),credits,key))
    return path

def test_duplicate_email_is_one_account_and_balance_without_deleting_sessions(service):
    a=ready(service,'account_old'); b=ready(service,'account_new')
    profile(service,'flow_browser_profile_1','same@example.test')
    with service.connect() as db:
        db.execute('insert into console_account_profiles values (?,?)',('account_old','flow_browser_profile_1'))
    before={p:p.read_bytes() for p in (a,b)}
    assert aliases(service)['account_new']=='account_old'
    assert service._account_profile_name('account_new')=='flow_browser_profile_1'
    result=service.accounts({'accounts':[]})
    assert [a['id'] for a in result['accounts']]==['account_old']
    assert result['total_credits']==1050
    assert [a['account_id'] for a in service.monitor.snapshot()['accounts']]==['account_old']
    assert service._account_cookie('account_new')==str(b) # historical lookups retain original session
    assert all(p.read_bytes()==raw for p,raw in before.items())
    ready(service,'account_resynced')
    assert len(service.accounts({'accounts':[]})['accounts'])==1

def test_unverified_or_other_email_never_merged(service):
    a=ready(service,'account_a'); b=ready(service,'account_b',email='other@example.test')
    assert aliases(service)=={'account_a':'account_a','account_b':'account_b'}
    with service.connect() as db: db.execute("update console_account_details set email='same@example.test',error='identity' where account_key='account_b'")
    assert 'account_b' not in aliases(service)
    b.write_bytes(b.read_bytes()+b' ')
    assert 'account_b' not in aliases(service)
