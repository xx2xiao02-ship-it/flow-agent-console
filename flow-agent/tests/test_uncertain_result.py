"""No provider access: timing, no replay and successful download boundary."""
import asyncio
from types import SimpleNamespace
import pytest
from flow_agent import uncertain_result as u
from flow_agent.official_images import OfficialImageError
from flow_agent.retry_policy import retry_action

@pytest.mark.asyncio
async def test_full_180_seconds_virtual_clock_and_no_retry(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(u.time, 'monotonic', lambda: clock[0])
    async def sleep(seconds): clock[0] += seconds
    monkeypatch.setattr(u.asyncio, 'sleep', sleep)
    report = {'route': 'official_page', 'generation_submitted': True, 'generation_requests': 1}
    reads = []
    async def read(deadline): reads.append(deadline)
    with pytest.raises(OfficialImageError) as error:
        await u.observe_uncertain(report, read)
    assert clock[0] == 180 and len(reads) == 90
    assert error.value.code == 'original_result_unknown'
    assert report['outcome'] == 'pending_review' and report['query_only']
    assert report['generation_requests'] == 1
    data = SimpleNamespace(kind='video', count=1, retry_switch_accounts=True, retry_uncertain=True)
    assert retry_action(data, report, error.value)[0] == 'stop'

@pytest.mark.asyncio
async def test_verified_download_returns_without_waiting_full_window():
    report = {'generation_requests': 1}
    async def read(deadline):
        report['download_verified'] = True
        return {'status': 'succeeded', 'files': [{'path': 'local-fixture.mp4'}]}
    result = await u.observe_uncertain(report, read)
    assert result['status'] == 'succeeded' and report['uncertainty_resolved']
    assert report['uncertainty_wait_elapsed'] < 1
    assert report['generation_requests'] == 1 and 'outcome' not in report

@pytest.mark.asyncio
async def test_failed_reads_bounded_and_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(u, 'UNCERTAIN_WAIT_SECONDS', .04)
    monkeypatch.setattr(u, 'POLL_SECONDS', .01)
    report = {}
    async def broken(deadline): raise RuntimeError('read failed')
    with pytest.raises(OfficialImageError): await u.observe_uncertain(report, broken)
    assert report['uncertainty_last_error'] == 'RuntimeError'
    async def canceled(deadline): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await u.observe_uncertain({}, canceled)

@pytest.mark.asyncio
async def test_unresponsive_read_honors_deadline(monkeypatch):
    monkeypatch.setattr(u, 'UNCERTAIN_WAIT_SECONDS', .04)
    async def hang(deadline): await asyncio.sleep(30)
    report = {}
    with pytest.raises(OfficialImageError): await u.observe_uncertain(report, hang)
    assert report['outcome'] == 'pending_review' and report['uncertainty_wait_elapsed'] < 1

from tests.test_official_videos import video_service, local_video_page
from flow_agent.official_images import profile_in_use
from flow_agent.official_videos import OfficialVideoError

@pytest.mark.asyncio
async def test_real_browser_same_window_waits_full_180_and_closes(video_service, local_video_page):
    service, cookie, profile = video_service
    local_video_page.mode = 'rpc_error'
    task = asyncio.create_task(service.official_videos.run(cookies=str(cookie), prompt='LOCAL ONLY'))
    for _ in range(600):
        report = service.official_videos.active_report or {}
        if report.get('uncertainty_query_started'):
            break
        if task.done():
            await task
        await asyncio.sleep(.1)
    assert report.get('uncertainty_query_started')
    assert profile_in_use(profile) and not task.done()
    assert report['query_only'] and report['generation_requests'] == 1
    with pytest.raises(OfficialVideoError) as error:
        await task
    report = service.official_videos.last_report
    assert error.value.code == 'original_result_unknown'
    assert report['uncertainty_wait_seconds'] == 180
    assert report['uncertainty_wait_elapsed'] >= 179.9
    assert report['outcome'] == 'pending_review'
    assert report['generation_response']['rpc_error_codes'] == [7]
    assert report['browser_closed'] and not profile_in_use(profile)
    assert report['visibility']['hidden_confirmed'] and not report['visibility']['shown_for_user']
    assert local_video_page.submissions == 1 and local_video_page.downloads == 0

@pytest.mark.asyncio
async def test_real_browser_recovers_verified_download_without_resubmitting(video_service, local_video_page, monkeypatch):
    service, cookie, profile = video_service
    async def interrupted(*args, **kwargs):
        raise OfficialVideoError('video_result_timeout', submitted=True)
    monkeypatch.setattr(service.official_videos, 'wait_video_candidate', interrupted)
    result = await service.official_videos.run(cookies=str(cookie), prompt='LOCAL ONLY')
    report = service.official_videos.last_report
    assert result['status'] == 'succeeded' and report['download_verified']
    assert report['uncertainty_resolved'] and report['uncertainty_wait_elapsed'] < 180
    assert report['browser_closed'] and not profile_in_use(profile)
    assert report['visibility']['hidden_confirmed'] and not report['visibility']['shown_for_user']
    assert local_video_page.submissions == 1 and local_video_page.downloads == 1
    assert len(result['files']) == 1
    assert __import__('pathlib').Path(result['files'][0]['path']).is_file()

from tests.test_auto_retry import service, page_error
from flow_agent.console import JobInput

@pytest.mark.asyncio
@pytest.mark.parametrize('rotation', [False, True])
async def test_expired_uncertainty_never_queues_generation_even_with_consent(service, rotation):
    service.engine.failures = 99
    service.engine.failure = page_error('original_result_unknown', submitted=True,
        uncertainty_query_completed=True, outcome='pending_review',
        response_result_ids=['11111111-1111-4111-8111-111111111111'])
    job = service.enqueue(JobInput(kind='video', prompt='LOCAL ONLY', account_id='account_a',
        auto_retry_limit=3, retry_switch_accounts=rotation, retry_uncertain=True))['id']
    await service.execute_job(job)
    result = service.get_job(job)
    assert result['status'] == 'failed' and result['result']['execution']['outcome'] == 'pending_review'
    assert result['result']['auto_retry']['next_retry_at'] is None
    assert len(service.engine.calls) == 1

from tests.test_official_images import local_service, local_flow

@pytest.mark.asyncio
async def test_image_unknown_report_includes_final_browser_cleanup(local_service, local_flow, monkeypatch):
    monkeypatch.setattr(u, 'UNCERTAIN_WAIT_SECONDS', .05)
    service, cookie, profile = local_service
    local_flow[0].mode = 'empty'
    with pytest.raises(OfficialImageError) as error:
        await service.official_images.run(cookies=cookie, prompt='LOCAL ONLY')
    report = error.value.execution
    assert error.value.code == 'original_result_unknown'
    assert report['outcome'] == 'pending_review' and report['browser_closed']
    assert local_flow[0].submissions == 1 and not profile_in_use(profile)
