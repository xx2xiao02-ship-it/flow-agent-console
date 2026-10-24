"""Offline contract audit: no connection to Flow or production queue."""
import json
import httpx
import pytest
from flow_agent.console import JobInput
from flow_agent.console_mcp import ConsoleMCP

@pytest.mark.asyncio
@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('video_model,duration', [('omni_flash','6s'),('veo_3_1_fast','8s')])
async def test_mcp_video_matches_direct_api_normalized_parameters(remote,video_model,duration):
    captured=[]
    raw={'prompt':'LOCAL CONTRACT ONLY','account_id':'local-test-account',
         'video_model':video_model,'duration':duration,'quality':'720p','aspect':'16:9','count':1}
    direct=JobInput.model_validate({**raw,'kind':'video','model':'beluga'}).model_dump()
    def receive(request):
        captured.append(request)
        assert json.loads(request.content)==direct
        return httpx.Response(202,json={'id':'offline-only','status':'queued'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        adapter=ConsoleMCP(base_url='http://192.0.2.1:8002' if remote else 'http://127.0.0.1:8001',
                           api_key='local-test-key' if remote else '',client=client)
        await adapter.call('flow_generate_video',{'job':raw,'acknowledge_possible_charge':True})
    assert len(captured)==1
    assert captured[0].url.path==('/api/v1/jobs' if remote else '/console/api/jobs')
    assert direct['auto_retry_limit']==0 and direct['retry_uncertain'] is False
