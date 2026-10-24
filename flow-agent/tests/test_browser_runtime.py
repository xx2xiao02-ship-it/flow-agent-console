import subprocess
import pytest
from flow_agent.browser_runtime import require_playwright_driver, BrowserRuntimeError
from flow_agent.official_videos import OfficialVideoError
from flow_agent.console import safe_failure
from tests.test_official_videos import local_video_page, video_service

@pytest.mark.parametrize('missing',['node','cli'])
def test_missing_runtime_is_specific_not_page_failure(tmp_path, monkeypatch, missing):
    from playwright._impl import _driver
    node,cli=tmp_path/'node.exe',tmp_path/'cli.js'
    if missing!='node':node.write_bytes(b'fixture')
    if missing!='cli':cli.write_bytes(b'fixture')
    monkeypatch.setattr(_driver,'compute_driver_executable',lambda:(str(node),str(cli)))
    with pytest.raises(BrowserRuntimeError) as e:require_playwright_driver()
    assert e.value.component==missing
    message=safe_failure(OfficialVideoError(e.value.code))
    assert '驱动文件缺失' in message and str(tmp_path) not in message

def test_real_local_driver_can_start_without_browser_or_network():
    require_playwright_driver(probe=True)

def test_broken_driver_runtime_is_detected(tmp_path, monkeypatch):
    from playwright._impl import _driver
    node,cli=tmp_path/'node.exe',tmp_path/'cli.js'
    node.write_bytes(b'fixture');cli.write_bytes(b'fixture')
    monkeypatch.setattr(_driver,'compute_driver_executable',lambda:(str(node),str(cli)))
    with pytest.raises(BrowserRuntimeError) as e:require_playwright_driver(probe=True)
    assert e.value.code=='browser_driver_failed'


@pytest.mark.asyncio
async def test_video_launch_reports_missing_driver_without_submission(video_service, local_video_page, monkeypatch):
    import flow_agent.browser_runtime as runtime
    service,cookie,profile=video_service
    def missing(**kwargs):
        raise BrowserRuntimeError('browser_driver_missing','node')
    monkeypatch.setattr(runtime,'require_playwright_driver',missing)
    with pytest.raises(OfficialVideoError) as error:
        await service.official_videos.run(cookies=str(cookie),check_only=True,duration='6s')
    assert error.value.code=='browser_driver_missing'
    report=service.official_videos.last_report
    assert report['runtime_component']=='node' and report['stage']=='launch'
    assert report['generation_requests']==local_video_page.submissions==0
    assert not report['generation_submitted']
