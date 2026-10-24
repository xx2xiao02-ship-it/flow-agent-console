import pytest
from flow_agent.official_images import read_generation_body, OfficialImageError


@pytest.mark.asyncio
@pytest.mark.parametrize('error,reason', [
    ('Protocol error (Network.getResponseBody): No data found for resource with given identifier SECRET_TOKEN', 'body_missing'),
    ('Protocol error: No resource with given identifier found SECRET_TOKEN', 'body_evicted'),
    ('Target page closed SECRET_COOKIE', 'browser_closed'),
    ('unknown https://example.test/?token=SECRET', 'body_unavailable'),
])
async def test_response_read_failure_is_safe_and_never_replays(error, reason):
    class Response:
        status = 200
        calls = 0
        async def text(self):
            self.calls += 1
            raise RuntimeError(error)
    response = Response()
    report = {}
    with pytest.raises(OfficialImageError) as failure:
        await read_generation_body(response, report)
    assert failure.value.code == 'generation_response_unreadable'
    assert failure.value.submitted
    assert response.calls == 1
    assert report['response_read_error'] == reason
    assert 'SECRET' not in str(report)
    assert report['generation_response_http_status'] == 200
    assert report['response_read_exception_type'] == 'RuntimeError'
    from flow_agent.work_log import safe
    persisted = safe(report)
    assert persisted['response_read_exception_type'] == 'RuntimeError'
    assert 'SECRET' not in str(persisted)
    if reason == 'body_missing':
        assert persisted['response_read_api'] == 'network_get_response_body'


@pytest.mark.asyncio
async def test_response_read_success_keeps_body_out_of_report():
    class Response:
        status = 200
        async def text(self):
            return 'fixture payload'
    report = {}
    assert await read_generation_body(Response(), report) == 'fixture payload'
    assert report['response_body_read']
    assert 'fixture payload' not in str(report)
