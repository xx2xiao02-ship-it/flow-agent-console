"""Prompt indexing/search/download contracts; all assets are local fixtures."""
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_api import create_lan_app


@pytest.fixture
def service(tmp_path):
    svc = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    svc.initialize()
    svc.engine.output_dir.mkdir()
    svc.engine.get_balance = AsyncMock(return_value={'accounts': []})
    return svc


def save_asset(service, name='cat.png', prompt='一只猫坐在金色窗台上', kind='image'):
    path = service.engine.output_dir / name
    if not path.exists():
        path.write_bytes(b'fixture image or video bytes')
    job = service.enqueue(JobInput(kind=kind, model='beluga', prompt=prompt))
    with service.connect() as db:
        request = job['request']
        request['cookies'] = 'PRIVATE_COOKIE'
        request['token'] = 'PRIVATE_TOKEN'
        db.execute("update console_jobs set status='succeeded',request_json=?,result_json=? where id=?",
                   (json.dumps(request), json.dumps({'files': [{'name': name, 'url': '/api/v1/media/' + name}],
                     'account_session': 'PRIVATE_SESSION'}), job['id']))
    return path, job['id']


def test_prompt_index_is_portable_and_preserves_original_file_and_job(service):
    path, job_id = save_asset(service)
    before = (path.read_bytes(), path.stat().st_mtime_ns, service.get_job(job_id))
    item = service.media()[0]
    assert item['name'] == 'cat.png' and item['url'] == '/api/v1/media/cat.png'
    assert item['prompt'] == '一只猫坐在金色窗台上' and item['task_id'] == job_id
    assert item['model'] == 'beluga' and item['index_status'] == 'indexed'
    assert item['parameters'] == {'aspect': '16:9', 'count': 1}
    document = json.loads(service.media_library.index_path.read_text(encoding='utf-8'))
    assert document['schema_version'] == 1 and len(document['items']) == 1
    exported = json.dumps(document)
    for secret in ('PRIVATE_COOKIE', 'PRIVATE_TOKEN', 'PRIVATE_SESSION', str(service.engine.root)):
        assert secret not in exported
    assert before == (path.read_bytes(), path.stat().st_mtime_ns, service.get_job(job_id))
    indexed_mtime = service.media_library.index_path.stat().st_mtime_ns
    assert service.media()[0] == item
    assert service.media_library.index_path.stat().st_mtime_ns == indexed_mtime


def test_multi_keyword_search_covers_prompt_model_task_and_filename(service):
    _, job_id = save_asset(service)
    save_asset(service, 'dog.mp4', 'A dog in BLUE light', kind='video')
    assert len(service.media('猫 金色')) == 1
    assert len(service.media('a DOG blue', 'video')) == 1
    assert service.media('猫 海洋') == []
    assert service.media('cat.png', 'video') == []
    assert len(service.media('BELUGA')) == 1
    assert len(service.media(job_id)) == 1
    with pytest.raises(HTTPException):
        service.media('', 'unsupported')


def test_orphans_ambiguous_names_and_changed_files_are_not_guessed(service):
    orphan = service.engine.output_dir / 'orphan.png'
    orphan.write_bytes(b'unknown origin')
    assert service.media()[0]['prompt'] is None
    save_asset(service, 'collision.png', 'first prompt')
    save_asset(service, 'collision.png', 'different prompt')
    collision = next(x for x in service.media() if x['name'] == 'collision.png')
    assert collision['prompt'] is None and collision['index_status'] == 'ambiguous'
    path, _ = save_asset(service, 'changed.png')
    service.media()
    path.write_bytes(b'replaced with another image contents')
    for _ in range(2):
        changed = next(x for x in service.media() if x['name'] == path.name)
        assert changed['prompt'] is None and changed['index_status'] == 'file_changed'


def test_catalog_keeps_known_prompt_if_task_history_is_no_longer_available(service):
    _, job_id = save_asset(service)
    service.media()
    with service.connect() as db:
        db.execute('delete from console_jobs where id=?', (job_id,))
    restarted = ConsoleService(service.engine)
    assert restarted.media()[0]['prompt'] == '一只猫坐在金色窗台上'


def test_search_and_pagination_can_find_assets_beyond_legacy_200_limit(service):
    for i in range(205):
        (service.engine.output_dir / f'asset-{i:03}.png').write_bytes(b'fixture')
    snapshot = service.media_library.snapshot(limit=60, offset=180)
    assert snapshot['total'] == 205 and len(snapshot['items']) == 25
    assert len(service.media()) == 200
    assert len(service.media('asset-000')) == 1
    assert service.media_library.snapshot(search='asset-000')['matching'] == 1


def test_foreign_index_is_preserved_without_overwriting(service):
    service.media_library.index_path.write_text('an existing unrelated index', encoding='utf-8')
    with pytest.raises(HTTPException) as error:
        service.media()
    assert error.value.status_code == 409
    assert service.media_library.index_path.read_text(encoding='utf-8') == 'an existing unrelated index'


def test_native_media_requires_exact_path_not_matching_timestamp(service):
    path = service.engine.output_dir / 'native.png'
    path.write_bytes(b'fixture')
    with sqlite3.connect(service.engine.db_path) as db:
        db.execute('create table media(file_name text,file_path text,prompt text)')
        db.execute('insert into media values(?,?,?)', (path.name, str(path), 'native prompt'))
        db.execute('insert into media values(?,?,?)', ('outside.png', '/another/folder/outside.png', 'not the same file'))
    (service.engine.output_dir / 'outside.png').write_bytes(b'fixture')
    items = {item['name']: item for item in service.media()}
    assert items[path.name]['prompt'] == 'native prompt'
    assert items[path.name]['source'] == 'engine_media'
    assert items['outside.png']['prompt'] is None


def test_conflicting_native_records_are_not_selected_by_row_order(service):
    path = service.engine.output_dir / 'duplicate.png'
    path.write_bytes(b'fixture')
    with sqlite3.connect(service.engine.db_path) as db:
        db.execute('create table media(file_name text,file_path text,prompt text)')
        db.executemany('insert into media values(?,?,?)', [(path.name, str(path), prompt) for prompt in ('one', 'two')])
    item = service.media()[0]
    assert item['prompt'] is None and item['index_status'] == 'ambiguous'


def test_http_library_export_search_and_folder_are_local_and_scoped(service, monkeypatch):
    save_asset(service)
    app = FastAPI()
    app.include_router(create_console_router(service))
    launches = []
    monkeypatch.setattr('flow_agent.media_library.subprocess.Popen', lambda *args, **kw: launches.append(args[0]))
    with TestClient(app) as client:
        report = client.get('/console/api/media/library', params={'search': '猫', 'limit': 1})
        assert report.status_code == 200 and report.json()['matching'] == 1
        assert report.json()['directory'] == str(service.engine.output_dir.resolve())
        assert client.get('/console/api/media/library?limit=0').status_code == 422
        assert client.get('/console/api/media/library?offset=-1').status_code == 422
        exported = client.get('/console/api/media/index')
        assert exported.status_code == 200
        assert 'attachment;' in exported.headers['content-disposition']
        assert exported.json()['items'][0]['prompt'] == '一只猫坐在金色窗台上'
        assert client.post('/console/api/media/open-folder', json={'path': '../outside'}).status_code == 200
        assert launches == [['explorer.exe', str(service.engine.output_dir.resolve())]]
    lan = TestClient(create_lan_app(service, 'fixture-key-' * 4))
    headers = {'Authorization': 'Bearer ' + 'fixture-key-' * 4}
    assert lan.get('/api/v1/media', params={'search': '猫'}, headers=headers).json()[0]['prompt']
    assert lan.post('/console/api/media/open-folder', headers=headers).status_code == 404
