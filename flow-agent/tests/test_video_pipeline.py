"""Local video queue/process/download/real Windows browser regressions.

The native generator is a deterministic subprocess fixture. This does not
contact Google, mint a token or prove provider generation acceptance.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import psutil
import pytest
from fastapi import FastAPI

from flow_agent import api
from flow_agent.browser_visibility import OwnedBrowserVisibility
from flow_agent.console import ConsoleService, create_console_router
from flow_agent.engine import FlowEngine

ASSET = Path(__file__).parent / 'assets/source-1s.mp4'
SESSION = json.dumps({'cookies': [{'name': 'test', 'value': 'PRIVATE'}],
                      'at': 'PRIVATE', 'fsid': 'PRIVATE',
                      'fingerprint': {'userAgent': 'local-fixture'}})


def make_service(tmp_path):
    service = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent', binary=sys.executable))
    # These tests retain coverage of the standalone native video path. The
    # default console page route is exercised in test_official_videos.py.
    service.engine.official_videos = None
    service.initialize()
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_fixture.json').write_text(SESSION)
    return service


def make_app(service, monkeypatch):
    app = FastAPI()
    app.include_router(create_console_router(service))
    app.add_api_route('/api/v1/media/{filename:path}', api.flow_media, methods=['GET'])
    monkeypatch.setattr(api, 'MEDIA_DIR', service.engine.output_dir)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,status,saved,expected', [
    ('video', 'ready', 'video', 'succeeded'),
    ('video', 'succeeded', 'video', 'succeeded'),
    ('video', 'ready', 'missing', 'failed'),
    ('video', 'ready', 'empty', 'failed'),
    ('video', 'submitted', 'video', 'failed'),
    ('video', 'failed', 'video', 'failed'),
    ('image', 'ready', 'video', 'failed'),
])
async def test_native_video_ready_requires_saved_product(tmp_path, monkeypatch, kind, status, saved, expected):
    service = make_service(tmp_path)
    service.engine.output_dir.mkdir()
    output = service.engine.output_dir / 'local-video.mp4'
    if saved != 'missing':
        output.write_bytes(ASSET.read_bytes() if saved == 'video' else b'')
    async def local_result(**kwargs):
        return {'status': status, 'job_id': 'local-native', 'files': [{'path': str(output)}]}
    monkeypatch.setattr(service.engine, 'generate_video', local_result)
    monkeypatch.setattr(service.engine, 'generate_image', local_result)
    service.worker = asyncio.create_task(service.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(service, monkeypatch)), base_url='http://test') as client:
            submitted = await client.post('/console/api/jobs', json={
                'kind': kind, 'prompt': 'local test only', 'account_id': 'account_fixture'})
            assert submitted.status_code == 202
            await asyncio.wait_for(service.queue.join(), 5)
            done = (await client.get('/console/api/jobs/' + submitted.json()['id'])).json()
            assert done['status'] == expected
            assert done['result']['status'] == status  # Keep the native contract.
            if expected == 'succeeded':
                downloaded = await client.get(done['result']['files'][0]['url'])
                assert downloaded.status_code == 200
                assert downloaded.content == ASSET.read_bytes()
                assert service._probe_video(output)['duration_seconds'] == 1
            if saved in ('missing', 'empty'):
                assert done['result']['files'] == []
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['ready', 'rejected', 'timeout', 'empty'])
async def test_video_queue_real_process_hidden_browser_and_cleanup(tmp_path, monkeypatch, mode):
    browser = Path(os.environ.get('FLOW_TEST_BROWSER', 'missing'))
    if os.name != 'nt' or not browser.is_file():
        pytest.skip('Set FLOW_TEST_BROWSER for real Windows browser tests')
    service = make_service(tmp_path)
    profile = tmp_path / 'exclusive-fixture-profile'
    guards, calls, process_ids = [], [], []
    runner = tmp_path / 'native_fixture.py'
    # A subprocess response in the native CLI's exact video shape; every input
    # and output stays local. Assert replay guards inside the child process.
    runner.write_text('''import json,os,sys,time
from pathlib import Path
assert os.environ['FLOW_AUTO_RETRY']=='0'
assert os.environ['FLOW_NO_SUBMISSION_REPLAY']=='1'
mode,asset,output=sys.argv[1:4]
assert sys.argv[4]=='generate'
assert sys.argv[-2:]==['--captcha','browser']
if mode=='timeout': time.sleep(10)
if mode=='rejected':
    print('PUBLIC_ERROR_UNUSUAL_ACTIVITY',file=sys.stderr);sys.exit(1)
path=Path(output);path.parent.mkdir(parents=True,exist_ok=True)
if mode!='empty': path.write_bytes(Path(asset).read_bytes())
print(json.dumps({'status':'ready','job_id':'local-native','files':[{'path':str(path)}]}))
''', encoding='utf-8')
    async def no_bridge():
        return {'state': 'local-fixture'}
    async def open_local(account, *, background=False):
        assert account == 'account_fixture' and background
        lease = next(iter(service.browser_pages.leases.values()))
        guard = OwnedBrowserVisibility(profile, browser)
        guards.append(guard)
        await guard.start()
        lease.visibility = service.browser_execution.active_visibility = guard
        subprocess.Popen([str(browser), '--user-data-dir=' + str(profile),
                          '--no-first-run', '--no-default-browser-check', '--disable-background-mode',
                          *guard.launch_args(), '--app=data:text/html,<title>Local video test</title>'],
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        lease.ever_connected = True  # Test page owns no Google extension.
    async def finish_without_extension(*args):
        # No extension acknowledgement on a local page; finally must still
        # close the owned browser even if page cleanup itself fails.
        if mode == 'rejected':
            raise OSError('local close channel unavailable')
        return {'state': 'unconfirmed'}
    def command(*args):
        calls.append(args)
        guard = guards[-1]
        assert guard.info()['hidden_confirmed']
        assert guard.info()['visible_owned_windows'] == 0
        for root in guard.roots.values():
            process_ids.extend([root.pid, *[p.pid for p in root.children(recursive=True)]])
        return [sys.executable, str(runner), mode, str(ASSET),
                str(service.engine.output_dir / 'local-video.mp4'), *map(str, args)]
    monkeypatch.setattr(service, 'start_bridge', no_bridge)
    monkeypatch.setattr(service, 'open_account_browser', open_local)
    monkeypatch.setattr(service.browser_execution, '_stop_owned_bridge', no_bridge)
    monkeypatch.setattr(service.browser_pages, 'finish', finish_without_extension)
    monkeypatch.setattr(service.engine, '_command', command)
    service.engine.timeout = 1 if mode == 'timeout' else 10
    service.worker = asyncio.create_task(service.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(service, monkeypatch)), base_url='http://test') as client:
            response = await client.post('/console/api/jobs', json={
                'kind': 'video', 'prompt': 'local test only', 'aspect': '9:16',
                'duration': '6s', 'quality': '720p', 'account_id': 'account_fixture'})
            assert response.status_code == 202
            await asyncio.wait_for(service.queue.join(), 45)
            job = (await client.get('/console/api/jobs/' + response.json()['id'])).json()
            assert job['status'] == ('succeeded' if mode == 'ready' else 'failed'), job
            assert len(calls) == 1  # No automatic replay after errors/timeouts.
            assert calls[0][:5] == ('generate', 'local test only', '9:16', '6s', '720p')
            cleanup = service.browser_execution.last_cleanup
            assert cleanup['browser_closed']
            assert cleanup['visibility']['hidden_confirmed']
            assert not cleanup['visibility']['shown_for_user']
            assert cleanup['visibility']['visible_owned_windows'] == 0
            assert not service.browser_execution.active_visibility
            for _ in range(30):
                if not any(psutil.pid_exists(pid) for pid in process_ids):
                    break
                await asyncio.sleep(.1)
            assert process_ids and not any(psutil.pid_exists(pid) for pid in process_ids)
            assert 'PRIVATE' not in json.dumps(job)
            if mode == 'ready':
                media = await client.get(job['result']['files'][0]['url'])
                assert media.status_code == 200 and media.content == ASSET.read_bytes()
                assert service._probe_video(service.engine.output_dir / 'local-video.mp4')['duration_seconds'] == 1
            if mode == 'empty':
                assert not job['result']['files']
    finally:
        await service.stop()
        for guard in guards:
            await guard.close_owned()
