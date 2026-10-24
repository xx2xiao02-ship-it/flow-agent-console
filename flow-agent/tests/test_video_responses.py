"""Explicit public response fields; no provider calls or paid operations."""
import json
import pytest
from flow_agent.account_details import payloads
from flow_agent.official_videos import video_result_ids, video_response_summary, OfficialVideoError
from flow_agent.console import safe_failure

PROJECT='11111111-1111-4111-8111-111111111111'
SCENE='22222222-2222-4222-8222-222222222222'
MEDIA='33333333-3333-4333-8333-333333333333'
OTHER='44444444-4444-4444-8444-444444444444'

@pytest.mark.parametrize('doc,expected', [
    ([None,100,[[SCENE,None,None,[],PROJECT]]],[SCENE]),
    ([None,100,[],[[MEDIA,PROJECT]]],[MEDIA]),
    ([None,100,None,[[MEDIA,PROJECT]]],[MEDIA]),
    ([None,100,[[SCENE,None,None,[],PROJECT]],[[MEDIA,PROJECT]]],[SCENE]),
    ([None,100,[],[[MEDIA,OTHER]]],[]),
    ([None,100,[],[[MEDIA]]],[]),
    ([None,100,[],[['not-an-id',PROJECT]]],[]),
    ([None,100,[[SCENE,None,None,[],OTHER]],[[MEDIA,PROJECT]]],[]),
    ([None,100,[],[[MEDIA,PROJECT],[MEDIA,PROJECT]]],[MEDIA]),
    ([None,100,[],[],[MEDIA,PROJECT]],[]),
])
def test_explicit_project_scoped_response_fields(doc,expected):
    assert video_result_ids([doc],PROJECT)==expected

def test_envelope_and_safe_diagnostics_do_not_expose_body():
    secret='PRIVATE-COOKIE-TOKEN-URL-PROMPT'
    doc=[None,100,[],[[MEDIA,PROJECT]],secret]
    body=")]}'\n123\n"+json.dumps([['wrb.fr','YhhmEf',json.dumps(doc)]])
    docs=payloads(body,'YhhmEf')
    assert video_result_ids(docs,PROJECT)==[MEDIA]
    summary=video_response_summary(body,'YhhmEf',docs)
    assert summary['matching_frame_count']==summary['payload_count']==1
    assert secret not in json.dumps(summary) and MEDIA not in json.dumps(summary)
    assert '不要直接重试' in safe_failure(OfficialVideoError('video_response_unrecognized',submitted=True))

def test_malformed_or_missing_payload_remains_unknown():
    body=json.dumps([['wrb.fr','YhhmEf',None,None,None,[7,'PRIVATE']]])
    docs=payloads(body,'YhhmEf')
    assert not docs and not video_result_ids(docs,PROJECT)
    summary=video_response_summary(body,'YhhmEf',docs)
    assert summary['matching_frame_count']==1 and summary['payload_count']==0
    assert 'PRIVATE' not in json.dumps(summary)
    assert summary['rpc_error_codes'] == [7]


@pytest.mark.parametrize('status,expected', [(7,[7]),(13,[13]),(0,[]),('7',[]),(True,[])])
def test_rpc_error_status_is_numeric_only_and_scoped(status, expected):
    body=json.dumps([['wrb.fr','other',None,None,None,[99,'SECRET']],
                     ['wrb.fr','YhhmEf',None,None,None,[status,None,['PRIVATE']]]])
    result=video_response_summary(body,'YhhmEf',[])
    assert result['rpc_error_codes']==expected
    assert 'PRIVATE' not in json.dumps(result) and 'SECRET' not in json.dumps(result)


def test_rpc_error_never_auto_generates_again_even_with_uncertain_consent():
    from types import SimpleNamespace
    from flow_agent.retry_policy import retry_action
    data=SimpleNamespace(kind='video', count=1, retry_uncertain=True)
    execution={'route':'official_page','error_code':'video_rpc_error',
               'generation_submitted':True,'generation_requests':1}
    assert retry_action(data, execution)[0]=='stop'
    assert '错误响应' in safe_failure(OfficialVideoError('video_rpc_error',submitted=True))
