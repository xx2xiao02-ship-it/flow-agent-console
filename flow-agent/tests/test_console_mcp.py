import json
from pathlib import Path
import httpx
import pytest
from flow_agent.console_mcp import ConsoleMCP, AdapterError, tools

@pytest.mark.asyncio
async def test_protocol_and_no_charge_gate():
    seen=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200,json={}))) as c:
        m=ConsoleMCP(client=c)
        assert await m.handle('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
        init=json.loads(await m.handle('{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"invented"}}'))
        assert init['id']==0 and init['result']['protocolVersion']=='2025-11-25'
        for args in ({'job':{'prompt':'cat'},'acknowledge_possible_charge':False},{'job':{'prompt':'cat'},'acknowledge_possible_charge':'true'}):
            with pytest.raises(AdapterError):await m.call('flow_generate_image',args)
        m.read_only=True
        with pytest.raises(AdapterError):await m.call('flow_generate_video',{'job':{'prompt':'cat'},'acknowledge_possible_charge':True})
    assert seen==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('kind,model', [('image','beluga'),('video','veo_3_1_fast')])
async def test_one_submission_uses_shared_jobs_without_retries(kind,model):
    seen=[]
    def handle(r):
        seen.append(r);data=json.loads(r.content)
        assert data['kind']==kind and data['auto_retry_limit']==0 and not data['retry_uncertain']
        assert r.headers['x-flow-allow-charge']=='yes'
        assert data['model']=='beluga'
        return httpx.Response(202,json={'id':'job-1','status':'queued'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        m=ConsoleMCP(base_url='http://192.168.1.2:8002',api_key='x'*32,client=c)
        job={'prompt':'cat'}
        if kind=='video':job['video_model']=model
        r=await m.call('flow_generate_'+kind,{'job':job,'acknowledge_possible_charge':True})
        assert r['id']=='job-1'
    assert len(seen)==1 and seen[0].url.path=='/api/v1/jobs'
    assert seen[0].headers['authorization']=='Bearer '+'x'*32

@pytest.mark.asyncio
async def test_timeout_never_resubmits_or_leaks_key():
    seen=[]
    def handle(r):seen.append(r);raise httpx.ReadTimeout('secret-token')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        m=ConsoleMCP(client=c)
        r=json.loads(await m.handle(json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'flow_generate_image','arguments':{'job':{'prompt':'cat'},'acknowledge_possible_charge':True}}})))
    assert r['result']['isError'] and 'secret-token' not in str(r) and len(seen)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('media_prefix', ['/console/api', '/api/v1'])
async def test_download_from_successful_job_and_no_overwrite(tmp_path, media_prefix):
    seen=[]
    def handle(r):
        seen.append(r)
        if '/jobs/' in r.url.path:return httpx.Response(200,json={'status':'succeeded','result':{'files':[{'url':media_prefix+'/media/test.mp4?storage=abc'}]}})
        assert r.url.path=='/api/v1/media/test.mp4' and r.url.params['storage']=='abc'
        return httpx.Response(200,content=b'video-fixture')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
        m=ConsoleMCP(base_url='http://192.168.1.2:8002',api_key='key',client=c)
        args={'job_id':'job-1','destination':str(tmp_path/'out.mp4')}
        await m.call('flow_download_result',args)
        assert (tmp_path/'out.mp4').read_bytes()==b'video-fixture'
        with pytest.raises(AdapterError):await m.call('flow_download_result',args)
    assert len(seen)==3

@pytest.mark.asyncio
async def test_validation_and_wrong_endpoint_prevent_submission():
    seen=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:seen.append(r) or httpx.Response(200,json={}))) as c:
        m=ConsoleMCP(client=c)
        for job in ({'prompt':'cat','auto_retry_limit':3},{'prompt':'cat','model':'unknown'},{'prompt':'cat','video_model':'veo_3_1_fast','duration':'4s'}):
            r=json.loads(await m.handle(json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'flow_generate_video','arguments':{'job':job,'acknowledge_possible_charge':True}}})))
            assert r['result']['isError']
    assert not seen
    with pytest.raises(ValueError):ConsoleMCP(base_url='http://192.168.1.2:8002',api_key='')
    with pytest.raises(ValueError):ConsoleMCP(base_url='http://u:password@localhost:8001')

def test_tool_schemas():
    names=[t['name'] for t in tools()]
    assert len(names)==len(set(names))==10
    assert {'flow_prepare_dispatch_quote','flow_get_dispatch_quote'} <= set(names)
    for t in tools(): assert t['inputSchema']['additionalProperties'] is False
