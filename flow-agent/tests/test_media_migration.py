"""Real local-disk migration only: no Google requests, no paid generation."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_api import create_lan_app
from flow_agent.media_migration import MigrationStart


@pytest.fixture
def service(tmp_path):
    svc = ConsoleService(FlowEngine(root=tmp_path / 'app'))
    svc.initialize()
    svc.engine.output_dir.mkdir()
    return svc


def material(service, name, prompt, content):
    path = service.engine.output_dir / name
    path.write_bytes(content)
    job = service.enqueue(JobInput(kind='video' if name.endswith('.mp4') else 'image', prompt=prompt))
    url = service.media_storage.url(path.parent, name)
    with service.connect() as db:
        db.execute("update console_jobs set status='succeeded',result_json=? where id=?",
                   (json.dumps({'files': [{'name': name, 'url': url}]}), job['id']))
    service.media_library.catalog()
    return path, url


async def migrate(service, target):
    migration = service.media_storage.migration
    plan = migration.prepare(str(target))
    await migration.start(MigrationStart(plan_id=plan['id'], confirmed=True))
    await asyncio.wait_for(migration.task, 10)
    return migration.snapshot()


@pytest.mark.asyncio
async def test_complete_move_prompt_search_old_urls_inputs_and_restart(service, tmp_path):
    original = service.engine.output_dir
    image, image_url = material(service, '猫.png', '猫在书架上', b'local image bytes')
    video, video_url = material(service, '猫.mp4', '猫缓慢行走', b'local video bytes')
    index = (original / 'flow-media-index.json').read_bytes()
    unrelated = original / 'keep.txt'; unrelated.write_bytes(b'not media; keep')
    jobs = service.list_jobs()
    target = tmp_path / '整体迁移'
    result = await migrate(service, target)
    assert result['state'] == 'succeeded', result
    assert result['copied'] == result['removed'] == 2
    assert not image.exists() and not video.exists()
    assert unrelated.read_bytes() == b'not media; keep'
    assert service.engine.output_dir == target and service.media_storage.directories == [target]
    assert service.list_jobs() == jobs
    backup = target / ('.flow-migration-' + result['id']) / 'source-index-0.json'
    assert backup.read_bytes() == index
    restarted = ConsoleService(FlowEngine(root=service.engine.root)); restarted.initialize()
    assert restarted.media_storage.directories == [target]
    assert restarted.media_storage.resolve('猫.png').read_bytes() == b'local image bytes'
    assert restarted.media_storage.resolve('猫.mp4').read_bytes() == b'local video bytes'
    items = restarted.media_library.snapshot(search='书架')['items']
    assert len(items) == 1 and items[0]['prompt'] == '猫在书架上'
    assert restarted._input_image('media:猫.png') == str(target / '猫.png')
    key = 'x' * 48
    app = create_lan_app(restarted, key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
        for url, data in [(image_url, b'local image bytes'), (video_url, b'local video bytes')]:
            assert (await client.get(url)).status_code == 401
            downloaded = await client.get(url, headers={'Authorization': 'Bearer ' + key})
            assert downloaded.status_code == 200 and downloaded.content == data


@pytest.mark.asyncio
async def test_merge_multiple_roots_name_conflicts_preserve_all_prompts_and_aliases(service, tmp_path):
    old, _ = material(service, 'same.png', 'old prompt', b'old')
    second = tmp_path / 'second'; service.media_storage.configure(str(second))
    other, other_url = material(service, 'same.png', 'second prompt', b'second')
    target = tmp_path / 'target'; target.mkdir()
    (target / 'same.png').write_bytes(b'preexisting target')
    result = await migrate(service, target)
    assert result['state'] == 'succeeded', result
    assert result['renamed'] == 2
    items = service.media_library.catalog()
    assert len(items) == 3 and len({item['id'] for item in items}) == 3
    assert service.media_storage.resolve('same.png').read_bytes() == b'old'
    assert service.media_storage.resolve('same.png', service.media_storage.key(second)).read_bytes() == b'second'
    assert (target / 'same.png').read_bytes() == b'preexisting target'
    assert {item['prompt'] for item in items} == {None, 'old prompt', 'second prompt'}
    # A second whole migration flattens old aliases; no task history rewrite.
    last = tmp_path / 'last'
    assert (await migrate(service, last))['state'] == 'succeeded'
    assert service.media_storage.resolve('same.png').read_bytes() == b'old'
    assert service.media_storage.resolve('same.png', service.media_storage.key(second)).read_bytes() == b'second'
    assert len(service.media_library.catalog()) == 3
    assert not old.exists() and not other.exists()


@pytest.mark.asyncio
async def test_move_back_to_original_directory(service, tmp_path):
    original = service.engine.output_dir
    material(service, 'same.png', 'original prompt', b'original')
    assert (await migrate(service, tmp_path / 'other'))['state'] == 'succeeded'
    assert (await migrate(service, original))['state'] == 'succeeded'
    assert service.media_storage.resolve('same.png').read_bytes() == b'original'
    assert service.media_library.catalog()[0]['prompt'] == 'original prompt'


@pytest.mark.asyncio
async def test_partial_copy_failure_preserves_sources_and_resumes_after_restart(service, tmp_path, monkeypatch):
    first, _ = material(service, 'a.png', 'A', b'A')
    second, _ = material(service, 'b.png', 'B', b'B')
    migration = service.media_storage.migration
    original_copy = migration._copy
    calls = []
    def fail_once(source, target):
        calls.append(source)
        if len(calls) == 2:
            raise OSError('fixture disk full')
        original_copy(source, target)
    monkeypatch.setattr(migration, '_copy', fail_once)
    target = tmp_path / 'target'
    result = await migrate(service, target)
    assert result['state'] == 'needs_attention' and not result['committed']
    assert first.read_bytes() == b'A' and second.read_bytes() == b'B'
    assert service.engine.output_dir == first.parent
    with pytest.raises(HTTPException):
        service.enqueue(JobInput(kind='image', prompt='blocked; never generate'))
    with pytest.raises(HTTPException):
        async with service.browser_execution.page_slot('fixture.json'):
            pytest.fail('must not open a browser while migration is pending')
    restarted = ConsoleService(FlowEngine(root=service.engine.root)); restarted.initialize()
    migration = restarted.media_storage.migration
    await migration.resume(); await asyncio.wait_for(migration.task, 10)
    assert migration.snapshot()['state'] == 'succeeded', migration.snapshot()
    assert not first.exists() and not second.exists()
    assert len(restarted.media_library.catalog()) == 2


@pytest.mark.asyncio
async def test_normal_shutdown_pauses_copy_and_resumes(service, tmp_path, monkeypatch):
    source, _ = material(service, 'a.png', 'A', b'A' * 100)
    migration = service.media_storage.migration
    original_copy = migration._copy
    def stop_after_copy(src, dst):
        original_copy(src, dst)
        migration.stop_event.set()
    monkeypatch.setattr(migration, '_copy', stop_after_copy)
    result = await migrate(service, tmp_path / 'target')
    assert result['state'] == 'interrupted' and source.exists()
    monkeypatch.setattr(migration, '_copy', original_copy)
    await migration.resume(); await migration.task
    assert migration.snapshot()['state'] == 'succeeded' and not source.exists()


@pytest.mark.asyncio
async def test_external_new_source_file_prevents_incomplete_whole_move(service, tmp_path, monkeypatch):
    source, _ = material(service, 'a.png', 'A', b'A')
    migration = service.media_storage.migration
    original_copy = migration._copy
    def external_write(src, dst):
        original_copy(src, dst)
        (source.parent / 'new.png').write_bytes(b'concurrent external output')
    monkeypatch.setattr(migration, '_copy', external_write)
    result = await migrate(service, tmp_path / 'target')
    assert result['state'] == 'needs_attention' and not result['committed']
    assert source.exists() and (source.parent / 'new.png').exists()
    assert service.engine.output_dir == source.parent


@pytest.mark.asyncio
async def test_reused_old_filename_has_separate_direct_url_and_survives_next_move(service, tmp_path):
    original = service.engine.output_dir
    material(service, 'same.png', 'first prompt', b'first')
    assert (await migrate(service, tmp_path / 'other'))['state'] == 'succeeded'
    service.media_storage.configure(str(original))
    new_source, new_url = material(service, 'same.png', 'new prompt', b'new')
    assert 'storage=d-' in new_url
    assert service.media_storage.resolve('same.png').read_bytes() == b'first'
    assert service.media_storage.resolve_id(service.media_storage.file_id(original, 'same.png')).read_bytes() == b'new'
    assert (await migrate(service, tmp_path / 'last'))['state'] == 'succeeded'
    assert service.media_storage.resolve('same.png').read_bytes() == b'first'
    assert service.media_storage.resolve('same.png', 'd-' + service.media_storage.key(original)).read_bytes() == b'new'
    assert {item['prompt'] for item in service.media_library.catalog()} == {'first prompt', 'new prompt'}


@pytest.mark.asyncio
async def test_cleanup_failure_resumes_without_recopies(service, tmp_path, monkeypatch):
    source, _ = material(service, 'a.png', 'A', b'A')
    original_unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError('fixture locked source')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(Path, 'unlink', denied)
        result = await migrate(service, tmp_path / 'target')
    assert result['state'] == 'needs_attention' and result['committed']
    assert source.exists()
    restarted = ConsoleService(FlowEngine(root=service.engine.root)); restarted.initialize()
    migration = restarted.media_storage.migration
    monkeypatch.setattr(migration, '_copy', lambda *_: pytest.fail('post-commit resume must never recopy'))
    await migration.resume(); await migration.task
    assert migration.snapshot()['state'] == 'succeeded'
    assert not source.exists()
    assert restarted.media_storage.resolve('a.png').read_bytes() == b'A'


@pytest.mark.asyncio
async def test_changed_target_never_deletes_original(service, tmp_path, monkeypatch):
    source, _ = material(service, 'a.png', 'A', b'paid original')
    migration = service.media_storage.migration
    original_hash = migration._hash
    reads = []
    def corrupt(path):
        if path.name == 'a.png' and path.parent.name == 'target':
            reads.append(path)
            if len(reads) == 1:
                path.write_bytes(b'changed copy')
        return original_hash(path)
    monkeypatch.setattr(migration, '_hash', corrupt)
    result = await migrate(service, tmp_path / 'target')
    assert result['state'] == 'needs_attention' and source.read_bytes() == b'paid original'
    assert not result['committed']
    migration.abandon()
    assert not migration.pending and service.engine.output_dir == source.parent


@pytest.mark.asyncio
async def test_preview_is_non_destructive_confirmation_and_stale_plan_required(service, tmp_path):
    source, _ = material(service, 'a.png', 'A', b'A')
    migration = service.media_storage.migration
    target = tmp_path / 'not-created'
    plan = migration.prepare(str(target))
    assert not target.exists() and source.exists()
    with pytest.raises(HTTPException) as exc:
        await migration.start(MigrationStart(plan_id=plan['id']))
    assert exc.value.status_code == 422
    source.write_bytes(b'changed')
    with pytest.raises(HTTPException) as exc:
        await migration.start(MigrationStart(plan_id=plan['id'], confirmed=True))
    assert exc.value.status_code == 409 and not target.exists()


@pytest.mark.parametrize('target', ['nested', 'parent', 'file', 'foreign', 'busy', 'offline'])
def test_unsafe_plans_do_not_move_files(service, tmp_path, target):
    source, _ = material(service, 'a.png', 'A', b'A')
    destination = tmp_path / 'target'
    if target == 'nested': destination = source.parent / 'nested'
    if target == 'parent': destination = source.parent.parent
    if target == 'file': destination.write_bytes(b'keep')
    if target == 'foreign':
        destination.mkdir(); (destination / 'flow-media-index.json').write_bytes(b'unrelated index')
    if target == 'busy': service.enqueue(JobInput(kind='image', prompt='queued only'))
    if target == 'offline': service.media_storage.directories.append(tmp_path / 'offline')
    with pytest.raises(HTTPException):
        service.media_storage.migration.prepare(str(destination))
    assert source.read_bytes() == b'A'


@pytest.mark.asyncio
async def test_local_http_migration_and_document_file_are_not_lan_management(service, tmp_path):
    material(service, 'a.png', 'A', b'A')
    app = FastAPI(); app.include_router(create_console_router(service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as c:
        document = await c.get('/console/api/integration/document.md')
        expected = Path(__file__).resolve().parents[2] / 'docs/INTEGRATION_API.md'
        assert document.status_code == 200 and document.content == expected.read_bytes()
        assert 'FlowAgent_API.md' in document.headers['content-disposition']
        plan = (await c.post('/console/api/media/storage/migration/plan', json={'directory': str(tmp_path / 'target')})).json()
        assert (await c.post('/console/api/media/storage/migration/start', json={'plan_id': plan['id'], 'confirmed': True})).status_code == 202
        await service.media_storage.migration.task
        assert (await c.get('/console/api/media/storage/migration')).json()['state'] == 'succeeded'
    key = 'x' * 48
    app = create_lan_app(service, key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://lan') as c:
        for path in ('/console/api/integration/document.md', '/console/api/media/storage/migration/plan', '/api/v1/media/storage/migration/plan'):
            assert (await c.post(path, headers={'Authorization': 'Bearer ' + key})).status_code == 404
