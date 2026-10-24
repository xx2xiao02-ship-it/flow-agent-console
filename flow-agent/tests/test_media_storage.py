"""Folder persistence, real local disk I/O and isolated task/download tests.

No Google calls, browser logins, or paid generation.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_api import create_lan_app


@pytest.fixture
def service(tmp_path):
    svc = ConsoleService(FlowEngine(root=tmp_path / 'app'))
    svc.initialize()
    svc.engine.output_dir.mkdir()
    svc.engine.get_balance = AsyncMock(return_value={'accounts': []})
    return svc


def record(service, name, prompt):
    job = service.enqueue(JobInput(kind='image', prompt=prompt, model='beluga'))
    with service.connect() as db:
        result = {'files': [{'name': name, 'url': service.media_storage.url(service.engine.output_dir, name)}]}
        db.execute("update console_jobs set status='succeeded',result_json=? where id=?", (json.dumps(result), job['id']))


def test_switch_keeps_old_bytes_urls_prompts_and_reloads_configuration(service, tmp_path):
    old = service.engine.output_dir / 'old.png'
    old.write_bytes(b'old paid output')
    record(service, old.name, 'old prompt')
    before = old.stat().st_mtime_ns
    service.media_library.catalog()
    index_before = service.media_library.index_path.read_bytes()
    new = tmp_path / '自定义素材目录'
    configured = service.media_storage.configure(str(new))
    assert configured['directory'] == str(new)
    assert service.engine.root.name == 'app'
    assert service.media_library.directory == new
    assert service.media_library.index_path == new / 'flow-media-index.json'
    (new / 'new.png').write_bytes(b'new local output')
    record(service, 'new.png', 'new prompt')
    items = {x['name']: x for x in service.media_library.catalog()}
    assert items['old.png']['url'] == '/api/v1/media/old.png'
    assert items['old.png']['prompt'] == 'old prompt'
    assert items['new.png']['prompt'] == 'new prompt'
    assert '?storage=' in items['new.png']['url']
    assert old.read_bytes() == b'old paid output' and old.stat().st_mtime_ns == before
    assert (old.parent / 'flow-media-index.json').read_bytes() == index_before
    assert service._input_image('media:' + items['old.png']['id']) == str(old)
    assert service._input_image('media:' + items['new.png']['id']) == str(new / 'new.png')
    restarted = ConsoleService(FlowEngine(root=service.engine.root))
    restarted.initialize()
    assert restarted.engine.output_dir == new
    assert len(restarted.media_library.catalog()) == 2
    assert restarted.media_storage.resolve('old.png') == old


def test_identical_names_in_different_folders_never_cross_bind(service, tmp_path):
    old = service.engine.output_dir / 'same.png'
    old.write_bytes(b'original')
    record(service, 'same.png', 'original prompt')
    new = tmp_path / 'new'
    service.media_storage.configure(str(new))
    (new / 'same.png').write_bytes(b'new image')
    record(service, 'same.png', 'different prompt')
    items = service.media_library.catalog()
    assert len({x['id'] for x in items}) == len({x['url'] for x in items}) == 2
    assert {x['prompt'] for x in items} == {'original prompt', 'different prompt'}
    assert service.media_storage.resolve('same.png').read_bytes() == b'original'
    assert service.media_storage.resolve('same.png', service.media_storage.key(new)).read_bytes() == b'new image'


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_busy_task_blocks_switch_without_creating_destination(service, tmp_path, status):
    job = service.enqueue(JobInput(kind='image', prompt='never generate'))
    with service.connect() as db:
        db.execute('update console_jobs set status=? where id=?', (status, job['id']))
    target = tmp_path / 'must-not-be-created'
    with pytest.raises(HTTPException) as exc:
        service.media_storage.configure(str(target))
    assert exc.value.status_code == 409 and not target.exists()


@pytest.mark.parametrize('value', ['relative/path', '', '../out', '\\\\server\\share', 'C:\\'])
def test_invalid_paths_are_rejected_without_changing_current(service, value):
    original = service.engine.output_dir
    with pytest.raises(HTTPException):
        service.media_storage.configure(value)
    assert service.engine.output_dir == original


def test_private_directory_foreign_index_and_file_target_are_preserved(service, tmp_path):
    for name in ('data', 'cookies', 'bin'):
        with pytest.raises(HTTPException):
            service.media_storage.configure(str(service.engine.root / name / 'nested'))
    foreign = tmp_path / 'foreign'; foreign.mkdir()
    (foreign / 'flow-media-index.json').write_text('unrelated index')
    with pytest.raises(HTTPException):
        service.media_storage.configure(str(foreign))
    assert (foreign / 'flow-media-index.json').read_text() == 'unrelated index'
    existing = tmp_path / 'file'; existing.write_bytes(b'keep')
    with pytest.raises(HTTPException):
        service.media_storage.configure(str(existing))
    assert existing.read_bytes() == b'keep'


def test_unavailable_drive_does_not_silently_revert_output(service, tmp_path):
    target = tmp_path / 'offline'
    service.media_storage.configure(str(target))
    target.rmdir()  # Empty test directory only, never a real user folder.
    restarted = ConsoleService(FlowEngine(root=service.engine.root))
    restarted.initialize()
    assert restarted.engine.output_dir == target
    assert restarted.media_storage.snapshot()['directories'][-1]['available'] is False


def test_resolver_rejects_traversal_unknown_storage_and_non_media(service):
    (service.engine.output_dir / 'secret.json').write_text('private')
    for name, storage in [('secret.json', None), ('../secret.png', None), ('image.png:stream', None), ('x.png', 'unregistered')]:
        with pytest.raises(HTTPException):
            service.media_storage.resolve(name, storage)


@pytest.mark.asyncio
async def test_http_configuration_local_only_and_native_picker_cancellation(service, tmp_path, monkeypatch):
    app = FastAPI(); app.include_router(create_console_router(service))
    monkeypatch.setattr(service.media_storage, 'choose_folder', AsyncMock(return_value={'directory': None, 'cancelled': True}))
    lan = create_lan_app(service, 'x' * 48)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as c:
        assert (await c.post('/console/api/media/storage/choose')).json()['cancelled']
        assert service.engine.output_dir == service.media_storage.original
        assert (await c.post('/console/api/media/storage', json={'directory': str(tmp_path / 'new')})).status_code == 200
        assert (await c.get('/console/api/media/storage')).json()['directory'] == str(tmp_path / 'new')
        assert (await c.post('/console/api/media/storage', json={'directory': str(tmp_path), 'extra': True})).status_code == 422
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lan), base_url='http://lan') as c:
        for path in ('/console/api/media/storage', '/console/api/media/storage/choose', '/api/v1/media/storage/choose'):
            assert (await c.post(path, headers={'Authorization': 'Bearer ' + 'x' * 48}, json={'directory': str(tmp_path)})).status_code == 404


@pytest.mark.asyncio
async def test_local_queue_success_download_and_prompt_index_in_selected_folder(service, tmp_path, monkeypatch):
    target = tmp_path / 'new'
    service.media_storage.configure(str(target))
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_fixture.json').write_text(json.dumps({'cookies': [{'name': 'local', 'value': 'fixture'}], 'at': 'local', 'fsid': 'local', 'fingerprint': {'userAgent': 'local'}}))
    async def generate(**kwargs):
        path = service.engine.output_dir / 'fixture.png'
        path.write_bytes(b'local fixture output; not provider generated')
        return {'status': 'succeeded', 'files': [{'path': str(path)}]}
    monkeypatch.setattr(service.engine, 'generate_image', generate)
    app = FastAPI(); app.include_router(create_console_router(service))
    lan = create_lan_app(service, 'x' * 48)
    service.worker = asyncio.create_task(service.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as c:
            submitted = await c.post('/console/api/jobs', json={'kind': 'image', 'model': 'beluga', 'prompt': 'local prompt', 'account_id': 'account_fixture'})
            assert submitted.status_code == 202
            await asyncio.wait_for(service.queue.join(), 3)
            job = (await c.get('/console/api/jobs/' + submitted.json()['id'])).json()
            assert job['status'] == 'succeeded', job
            assert (target / 'fixture.png').is_file()
            item = (await c.get('/console/api/media/library')).json()['items'][0]
            assert item['prompt'] == 'local prompt'
            assert item['url'] == job['result']['files'][0]['url']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lan), base_url='http://lan') as c:
            result = await c.get(item['url'], headers={'Authorization': 'Bearer ' + 'x' * 48})
            assert result.status_code == 200 and result.content == (target / 'fixture.png').read_bytes()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_native_process_receives_selected_output_without_changing_cwd(service, tmp_path, monkeypatch):
    target = tmp_path / 'new'; service.media_storage.configure(str(target))
    service.engine.binary = tmp_path / 'fixture.exe'; service.engine.binary.write_bytes(b'not executed')
    captured = {}
    class Process:
        returncode = 0
        async def communicate(self): return b'{}', b''
    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    await service.engine._spawn_run('image', 'local only')
    assert captured['env']['FLOW_OUTPUT_DIR'] == str(target)
    assert captured['cwd'] == str(service.engine.root)


@pytest.mark.asyncio
async def test_missing_folder_picker_returns_input_fallback(service, monkeypatch):
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=FileNotFoundError()))
    with pytest.raises(HTTPException) as exc:
        await service.media_storage.choose_folder()
    assert exc.value.status_code == 503
    assert '输入完整路径' in exc.value.detail
    assert not service.media_storage.picker_lock.locked()


@pytest.mark.asyncio
async def test_cancelled_folder_picker_closes_only_its_helper(service, monkeypatch):
    class Process:
        returncode = None
        killed = False
        communicate = AsyncMock(side_effect=asyncio.CancelledError())
        wait = AsyncMock(return_value=0)
        def kill(self):
            self.killed = True
    proc = Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    with pytest.raises(asyncio.CancelledError):
        await service.media_storage.choose_folder()
    assert proc.killed
    proc.wait.assert_awaited_once()
    assert not service.media_storage.picker_lock.locked()
