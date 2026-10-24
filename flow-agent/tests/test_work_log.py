import io
import json
import uuid
import zipfile
from types import SimpleNamespace
import pytest
from flow_agent.work_log import emit, read, export, Report, folder, failure_snapshot
from flow_agent.task_scheduler import CURRENT_JOB
from tests.test_official_images import local_service, local_flow

@pytest.fixture
def service(tmp_path):
    return SimpleNamespace(engine=SimpleNamespace(root=tmp_path), get_job=lambda _: {
        'status':'failed', 'created_at':'2026-10-24T00:00:00Z', 'result':{'execution':{'stage':'generate'}}})

def test_durable_log_export_excludes_credentials_and_raw_exception(service):
    job = str(uuid.uuid4())
    token = CURRENT_JOB.set(job)
    try:
        report = Report(service, {'stage':'launch','cookies':'SECRET_COOKIE','token':'SECRET_TOKEN'})
        report['stage'] = 'generate'
        report.update(generation_requests=1, generation_response={'rpc_error_codes':[7],
            'rpc_errors':[{'reason_codes':['unusual_activity','SECRET_TOKEN']}], 'body':'SECRET_BODY'})
        try: raise RuntimeError('Bearer SECRET_TOKEN Cookie SECRET_COOKIE')
        except RuntimeError as exc: emit(service,job,'失败',report,exc)
        content = read(service,job)
        assert any(r['data'].get('stage')=='launch' for r in content['events'])
        assert content['events'][-1]['stack']
        raw = export(service,job)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = archive.read('work-log.json').decode()
        assert 'SECRET' not in text
        assert 'unusual_activity' in text
        assert folder(service,job).joinpath('events.jsonl').is_file()
    finally: CURRENT_JOB.reset(token)

def test_history_is_labeled_and_paths_are_validated(service):
    assert '历史任务摘要' in read(service,str(uuid.uuid4()))['events'][0]['event']
    with pytest.raises(ValueError): folder(service,'../../cookies')

def test_work_log_http_export_and_missing_job(tmp_path):
    from flow_agent.console import ConsoleService, create_console_router
    from flow_agent.engine import FlowEngine
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    s=ConsoleService(FlowEngine(root=tmp_path/'engine')); s.initialize()
    job=str(uuid.uuid4())
    with s.connect() as db:
        db.execute('insert into console_jobs (id,kind,status,request_json,created_at) values (?,?,?,?,?)',
            (job,'image','failed','{"kind":"image","prompt":"SECRET_PROMPT"}','2026-10-24T00:00:00Z'))
    emit(s,job,'测试失败',{'stage':'generate','token':'SECRET_TOKEN'})
    app=FastAPI(); app.include_router(create_console_router(s))
    client=TestClient(app)
    response=client.get(f'/console/api/jobs/{job}/work-log')
    assert response.status_code==200 and response.json()['events'][0]['event']=='测试失败'
    response=client.get(f'/console/api/jobs/{job}/work-log/export')
    assert response.status_code==200 and response.headers['content-type']=='application/zip'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert 'SECRET' not in archive.read('work-log.json').decode()
    assert client.get(f'/console/api/jobs/{uuid.uuid4()}/work-log').status_code==404

@pytest.mark.asyncio
async def test_safe_screenshot_is_cropped_to_fixed_error(service):
    import os
    from playwright.async_api import async_playwright
    browser_path=os.environ.get('FLOW_TEST_BROWSER')
    if not browser_path: pytest.skip('Local browser required')
    job=str(uuid.uuid4()); token=CURRENT_JOB.set(job)
    try:
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(executable_path=browser_path,headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('<p>Cookie SECRET_COOKIE Token SECRET_TOKEN</p><div style="width:100px">生成失败</div>')
                await failure_snapshot(service,page,{'stage':'generate'})
                from PIL import Image
                with Image.open(folder(service,job)/'failure.png') as image:
                    assert image.width == 100 and image.height < 50
                assert read(service,job)['screenshot']
            finally: await browser.close()
    finally: CURRENT_JOB.reset(token)

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'rejected'])
async def test_real_local_pipeline_writes_stages_and_cleanup(local_service, local_flow, mode):
    from flow_agent.official_images import OfficialImageError
    s,cookie,profile=local_service
    local_flow[0].mode=mode
    job=str(uuid.uuid4()); token=CURRENT_JOB.set(job)
    try:
        if mode=='success':
            await s.official_images.run(cookies=cookie,prompt='fixture',timeout=8)
        else:
            with pytest.raises(OfficialImageError):
                await s.official_images.run(cookies=cookie,prompt='fixture',timeout=8)
        rows=[json.loads(line) for line in (folder(s,job)/'events.jsonl').read_text('utf-8').splitlines()]
        assert any(r['data'].get('stage')=='launch' for r in rows)
        assert any(r['data'].get('generation_requests')==1 for r in rows)
        assert any(r['data'].get('browser_closed') is True for r in rows)
        if mode=='success':
            assert any(r['data'].get('response_result_ids') for r in rows)
            assert any(r['data'].get('download_verified') is True for r in rows)
        assert local_flow[0].submissions==1  # Local fixture only.
    finally: CURRENT_JOB.reset(token)
