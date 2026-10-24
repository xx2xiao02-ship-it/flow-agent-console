"""Offline provider reasons and strict no-credential output."""
import json
import pytest
from flow_agent.official_videos import video_response_summary, OfficialVideoError
from flow_agent.provider_diagnostics import public_diagnostic
from flow_agent.console import safe_failure

def envelope(error, rpc='YhhmEf'):
    return json.dumps([['wrb.fr',rpc,None,None,None,error,'generic']])

@pytest.mark.parametrize('text,expected', [
 ('PUBLIC_ERROR_UNUSUAL_ACTIVITY','unusual_activity'),
 ('Permission denied','permission_denied'),('CAPTCHA_FAILED','verification_required'),
 ('insufficient credits','quota_exhausted'),('RATE_LIMIT_EXCEEDED','rate_limited'),
 ('INVALID_ARGUMENT','invalid_argument'),('content policy violation','policy_rejected'),
 ('MODEL_UNAVAILABLE','model_unavailable'),('SERVICE_UNAVAILABLE','service_unavailable'),
 ('异常活动','unusual_activity')])
def test_explicit_reason_only(text,expected):
    summary=video_response_summary(envelope([7,None,[text]]),'YhhmEf',[])
    assert summary['rpc_errors'][0]['reason_codes']==[expected]
    assert public_diagnostic(summary)

@pytest.mark.parametrize('error',[[7,None,[]],[7,'PRIVATE'],[7,None,['unrecognized secret']]])
def test_numeric_seven_is_not_a_cause(error):
    summary=video_response_summary(envelope(error),'YhhmEf',[])
    assert summary['rpc_errors'][0]['reason_codes']==[]
    assert summary['rpc_errors'][0]['reason_status']=='not_provided_or_unrecognized'
    assert '原因尚未确定' in public_diagnostic(summary)

def test_only_allowlisted_reason_survives_not_credentials_or_other_rpc():
    secret='PRIVATE-CREDENTIAL-8d662b09'
    error=[7,{'message':'Permission denied; Cookie='+secret+'; bearer '+secret,
        'cookie':secret,'token':secret,'url':'https://example.test/?token='+secret,
        'email':'private@example.test','prompt':secret,
        'details':[{'reason':'PUBLIC_ERROR_UNUSUAL_ACTIVITY','authorization':secret}]}]
    body=envelope(error)+'\n'+envelope([7,'INSUFFICIENT_CREDITS'],'OTHER')
    summary=video_response_summary(body,'YhhmEf',[])
    result=json.dumps(summary)
    assert secret not in result and 'example.test' not in result and 'private@' not in result
    assert summary['rpc_errors'][0]['reason_codes']==['permission_denied','unusual_activity']
    assert 'quota_exhausted' not in result
    exc=OfficialVideoError('video_rpc_error',submitted=True);exc.execution={'generation_response':summary}
    assert '异常活动' in safe_failure(exc) and secret not in safe_failure(exc)

def test_credential_keys_and_success_payload_do_not_supply_causes():
    summary=video_response_summary(envelope([7,{'token':'PUBLIC_ERROR_UNUSUAL_ACTIVITY','cookie':'PERMISSION_DENIED'}]),'YhhmEf',[])
    assert summary['rpc_errors'][0]['reason_codes']==[]
    body=json.dumps([['wrb.fr','YhhmEf',json.dumps(['PERMISSION_DENIED'])]])
    assert not video_response_summary(body,'YhhmEf',[['PERMISSION_DENIED']])['rpc_errors']


def test_exact_marker_is_separate_from_local_category():
    exact=video_response_summary(envelope([7,None,['PUBLIC_ERROR_UNUSUAL_ACTIVITY']]),'YhhmEf',[])['rpc_errors'][0]
    phrase=video_response_summary(envelope([7,None,['unusual activity']]),'YhhmEf',[])['rpc_errors'][0]
    assert exact['upstream_markers']==['PUBLIC_ERROR_UNUSUAL_ACTIVITY']
    assert exact['classification_source']=='local_allowlist_match_on_rpc_error'
    assert phrase['reason_codes']==['unusual_activity'] and phrase['upstream_markers']==[]

def test_unknown_or_deep_long_values_are_not_logged():
    value='PRIVATE'*2000
    summary=video_response_summary(envelope([7,value,[value]]),'YhhmEf',[])
    assert value not in json.dumps(summary) and len(json.dumps(summary))<3000

from tests.test_official_videos import video_service, local_video_page

@pytest.mark.asyncio
async def test_local_real_browser_reason_persisted_and_closed(video_service,local_video_page,monkeypatch):
    monkeypatch.setattr('flow_agent.uncertain_result.UNCERTAIN_WAIT_SECONDS',.05)
    service,cookie,profile=video_service
    local_video_page.mode='rpc_error_with_reason'
    with pytest.raises(OfficialVideoError) as error:
        await service.official_videos.run(cookies=str(cookie),prompt='LOCAL ONLY')
    report=error.value.execution
    assert report['generation_response']['rpc_errors'][0]['reason_codes']==['unusual_activity']
    assert '异常活动' in safe_failure(error.value)
    assert 'PRIVATE' not in json.dumps(report)
    assert report['generation_requests']==local_video_page.submissions==1
    assert report['browser_closed'] and report['outcome']=='pending_review'
