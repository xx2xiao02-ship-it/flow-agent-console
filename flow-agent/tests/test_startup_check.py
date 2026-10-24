"""Readiness gates and local startup failures; never contact Google."""
import asyncio
import json
import socket
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_api import create_lan_app

@pytest.fixture
def service(tmp_path, monkeypatch):
    root = tmp_path/'flow-agent'
    root.mkdir()
    binary = root/'flow.exe'
    binary.write_bytes(b'fixture')
    s = ConsoleService(FlowEngine(root=root, binary=str(binary)))
    dist = tmp_path/'web'/'dist'
    (dist/'assets').mkdir(parents=True)
    (dist/'index.html').write_text('<html>local fixture</html>')
    (dist/'assets'/'main.js').write_text('// fixture')
    ext = tmp_path/'flow-extension'
    ext.mkdir()
    (ext/'manifest.json').write_text(json.dumps({'background':{'service_worker':'worker.js'}}))
    (ext/'worker.js').write_text('// fixture')
    browser = tmp_path/'chrome.exe'
    browser.write_bytes(b'fixture')
    (tmp_path/'chrome.dll').write_bytes(b'fixture')
    monkeypatch.setattr(s, '_find_extension_browser', lambda: browser)
    monkeypatch.setattr(s.startup, 'engine_runtime', lambda: s.startup.require_file(binary))
    return s

@pytest.mark.asyncio
async def test_startup_opens_generation_only_after_all_basic_checks(service):
    await service.start()
    with pytest.raises(HTTPException) as denied:
        service.enqueue(JobInput(kind='image',prompt='never submit'))
    assert denied.value.status_code == 503
    assert service.worker is None and service.account_details_task is None
    await service.startup.task
    status = service.startup.snapshot()
    assert status['ready'] and all(row['state']=='passed' for row in status['checks'])
    assert service.worker and service.account_details_task
    assert not service.list_jobs()  # No accounts is not a startup blocker.
    assert not list(service.engine.output_dir.glob('.flow-startup-*'))
    await service.stop()
    assert service.worker is None and service.account_details_task is None

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['browser','browser_driver','engine','database','storage','resources'])
async def test_critical_failure_blocks_every_generation_surface_but_keeps_diagnostics(service, monkeypatch, failure):
    def fail(*args): raise OSError('PRIVATE_COOKIE_TOKEN')
    if failure == 'browser': monkeypatch.setattr(service.startup, 'browser', fail)
    if failure == 'browser_driver': monkeypatch.setattr(service.startup, 'browser_driver', fail)
    if failure == 'engine': monkeypatch.setattr(service.startup, 'engine_runtime', fail)
    if failure == 'database': monkeypatch.setattr(service.startup, 'database', fail)
    if failure == 'storage': monkeypatch.setattr(service.startup, 'writable', fail)
    if failure == 'resources': monkeypatch.setattr(service.startup, 'resources', fail)
    await service.start()
    await service.startup.task
    assert service.startup.state == 'blocked' and service.worker is None
    assert service.account_details_task is None
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        status = client.get('/console/api/startup')
        assert status.status_code == 200 and not status.json()['ready']
        assert 'PRIVATE' not in status.text
        assert client.post('/console/api/jobs',json={'kind':'image','prompt':'never submit'}).status_code == 503
    lan = create_lan_app(service,'x'*32)
    with TestClient(lan) as client:
        assert client.post('/api/v1/jobs',headers={'Authorization':'Bearer '+'x'*32,'X-Flow-Allow-Charge':'yes'},json={'kind':'image','prompt':'never submit'}).status_code == 503
    for operation, options in [(service.engine.generate_image,{'prompt':'never submit'}),(service.engine.generate_video,{'prompt':'never submit'}),(service.engine.edit_video,{'prompt':'never submit','source':'never-upload.mp4'})]:
        with pytest.raises(HTTPException) as denied:
            await operation(**options)
        assert denied.value.status_code == 503
    assert not service.list_jobs()
    await service.stop()

@pytest.mark.asyncio
async def test_initialization_failure_still_exposes_safe_startup_diagnostics(service, monkeypatch):
    def fail(): raise OSError('PRIVATE_STORAGE_PATH')
    monkeypatch.setattr(service,'initialize',fail)
    await service.start()
    await service.startup.task
    assert service.startup.state == 'blocked'
    assert 'PRIVATE' not in json.dumps(service.startup.snapshot())
    await service.stop()

def test_occupied_port_is_reported_without_stopping_existing_listener():
    from desktop_launcher import check_listen_ports
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match=str(port)):
            check_listen_ports(port)
        assert listener.fileno() >= 0
    check_listen_ports(port)

@pytest.mark.asyncio
async def test_account_checks_hold_readiness_and_preserve_per_account_warnings(service, monkeypatch):
    service.initialize()
    service.cookies_dir.mkdir(exist_ok=True)
    for key in ('a', 'b'):
        (service.cookies_dir / f'account_{key}.json').write_text('{}')
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def refresh(*, account_ids):
        calls.extend(sorted(account_ids))
        entered.set()
        await release.wait()
    monkeypatch.setattr(service, 'refresh_account_details', refresh)
    monkeypatch.setattr(service, 'local_profiles', lambda: [{'id':'local', 'label':'本机账号', 'emails':[]}])
    await service.start()
    await entered.wait()
    assert not service.startup.snapshot()['ready'] and service.worker is None
    assert service.startup.snapshot()['completed'] == 9
    assert service.startup.snapshot()['total'] == 11
    with pytest.raises(HTTPException):
        service.startup.require_ready()
    release.set()
    await service.startup.task
    status = service.startup.snapshot()
    assert status['ready'] and status['completed'] == status['total'] == 11
    assert calls == ['account_a', 'account_b']
    assert len(service.monitor.startup_issues) == 2
    assert all(a['requires_user_action'] for a in service.monitor.snapshot()['accounts'])
    assert status['local_profiles'][0]['id'] == 'local'
    assert not service.list_jobs()
    await service.stop()
