"""Two actual hidden Chrome profiles against local pages only. Zero Google calls."""
import asyncio
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from flow_agent.console import ConsoleService, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_api import create_lan_app
from flow_agent.official_images import profile_in_use
from flow_agent.task_scheduler import SchedulerInput
from tests.test_official_videos import HTML, VIDEO


@contextmanager
def pages(second_rejected):
    release = threading.Event()
    submitted = threading.Barrier(3)  # Two HTTP generation requests and the test.
    servers, threads = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == '/download':
                data = VIDEO.read_bytes(); mime = 'video/mp4'
            elif self.path.startswith('/poster/'):
                data = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="36"><rect width="64" height="36"/></svg>'
                mime = 'image/svg+xml'
            else:
                server = self.server
                html = HTML.replace('OLD', '99999999-9999-4999-8999-999999999999')
                html = html.replace('MODE', 'prefetched').replace('IDS', json.dumps([server.generated]))
                html = html.replace('CONTENTS', json.dumps([server.content])).replace('HAD_RESULTS', '0')
                html = html.replace('/project/fixture', '/project/' + server.project)
                html = html.replace('fixture@example.test', server.email)
                html = html.replace('DOWNLOAD_LABEL', '720p（原始）')
                html = html.replace('DURATION_RADIOS', '<input type="radio" name="duration" aria-label="4 秒" value="4s" checked>')
                html = html.replace('COUNT_RADIOS', '<input type="radio" name="count" aria-label="x1" value="1" checked>')
                data = html.encode(); mime = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            if self.path == '/download':
                self.send_header('Content-Disposition', 'attachment; filename="local.mp4"')
            self.send_header('Content-Length', str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            server = self.server
            if 'as29s' in self.path:
                rpc = 'as29s'; payload = [server.content, server.project, json.loads(body)['id']]
                code = 200
            else:
                server.submissions += 1
                try:
                    submitted.wait(60)
                    release.wait(60)
                except threading.BrokenBarrierError:
                    pass
                rpc = 'YhhmEf'; payload = [None, 100, [[server.generated, None, None, [], server.project]]]
                code = 403 if server.rejected else 200
            data = json.dumps([['wrb.fr', rpc, json.dumps(payload)]]).encode()
            self.send_response(code);self.send_header('Content-Length', str(len(data)))
            self.end_headers();self.wfile.write(data)
    try:
        for i in (1, 2):
            s = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            s.generated = f'{i:08d}-1111-4111-8111-111111111111'
            s.content = f'{i:08d}-2222-4222-8222-222222222222'
            s.project = f'{i:08d}-3333-4333-8333-333333333333'
            s.email = f'local{i}@example.test'
            s.submissions = 0; s.rejected = i == 2 and second_rejected
            thread = threading.Thread(target=s.serve_forever, daemon=True);thread.start()
            servers.append(s);threads.append(thread)
        yield servers, submitted, release
    finally:
        release.set();submitted.abort()
        for s in servers: s.shutdown();s.server_close()
        for t in threads: t.join(2)


@pytest.mark.asyncio
@pytest.mark.parametrize('second_rejected', [False, True])
async def test_two_hidden_video_jobs_api_download_identity_and_independent_cleanup(tmp_path, monkeypatch, second_rejected):
    chrome = os.environ.get('FLOW_TEST_BROWSER')
    if not chrome or not Path(chrome).is_file():
        pytest.skip('Set FLOW_TEST_BROWSER to run real local browser checks')
    service = ConsoleService(FlowEngine(tmp_path / 'flow-agent'))
    service.initialize();service.cookies_dir.mkdir()
    service.scheduler.configure(SchedulerInput(max_concurrency=2))
    profiles = {}
    with pages(second_rejected) as (servers, barrier, release):
        settings = {}
        for i, server in enumerate(servers, 1):
            account = f'account_{i}'
            cookie = service.cookies_dir / (account + '.json')
            cookie.write_text(json.dumps({'cookies': [{'name': 'fixture', 'value': 'local'}],
                'at': 'local', 'fsid': 'local', 'fingerprint': {'userAgent': 'fixture'}}))
            profile = service.engine.root / 'data' / f'flow_browser_profile_{i}';profile.mkdir()
            profiles[account] = profile
            with service.connect() as db:
                db.execute('insert into console_account_profiles values (?,?)', (account, profile.name))
            settings[account] = (profile, Path(chrome), server.email,
                f'http://127.0.0.1:{server.server_port}/project/{server.project}')
        monkeypatch.setattr(service.official_videos, 'settings', lambda account: settings[account])
        service.official_videos.timeout = 90
        app = create_lan_app(service, 'x' * 48)
        service.worker = asyncio.create_task(service.run())
        try:
            auth = {'Authorization': 'Bearer ' + 'x' * 48}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local-api') as client:
                jobs = []
                for i in (1, 2):
                    response = await client.post('/api/v1/jobs', headers={**auth, 'X-Flow-Allow-Charge': 'yes'},
                        json={'kind': 'video', 'prompt': f'local video fixture {i}', 'account_id': f'account_{i}', 'duration': '4s'})
                    assert response.status_code == 202
                    jobs.append(response.json()['id'])
                await asyncio.to_thread(barrier.wait, 60)
                assert len(service.browser_execution.page_accounts) == 2
                reports = list(service.official_videos.active_reports.values())
                assert len(reports) == 2 and {r['job_id'] for r in reports} == set(jobs)
                assert all(r['visibility']['hidden_confirmed'] and not r['visibility']['shown_for_user'] for r in reports)
                assert (await client.get('/api/v1/scheduler', headers=auth)).json()['running'] == 2
                release.set()
                await asyncio.wait_for(service.queue.join(), 150)
                for i, job in enumerate(jobs):
                    result = (await client.get('/api/v1/jobs/' + job, headers=auth)).json()
                    execution = result['result']['execution']
                    assert execution['generation_requests'] == 1 and execution['browser_closed']
                    assert execution['visibility']['hidden_confirmed'] and not execution['visibility']['shown_for_user']
                    if i == 1 and second_rejected:
                        assert result['status'] == 'failed'
                        assert not result['result']['files']
                    else:
                        assert result['status'] == 'succeeded'
                        assert execution['verified_results'][0]['generated_id'] == servers[i].generated
                        assert execution['verified_results'][0]['content_id'] == servers[i].content
                        media = await client.get(result['result']['files'][0]['url'], headers=auth)
                        assert media.status_code == 200 and media.content == VIDEO.read_bytes()
                assert [s.submissions for s in servers] == [1, 1]
                assert not service.official_videos.active_reports
                assert not service.browser_execution.handoff_active
                assert all(not profile_in_use(p) for p in profiles.values())
        finally:
            release.set()
            await service.stop()
