"""Local, portable prompt index. Never renames media or calls a provider."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException


class MediaLibrary:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()

    @property
    def directory(self):
        return self.service.engine.output_dir

    @property
    def index_path(self):
        return self.directory / 'flow-media-index.json'

    @staticmethod
    def category(path: Path):
        return 'video' if path.suffix.lower() == '.mp4' else 'image' if path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') else None

    def _job_links(self, directory):
        links = {}
        with self.service.connect() as db:
            rows = db.execute('select id,kind,status,request_json,result_json,created_at from console_jobs').fetchall()
        for row in rows:
            request = json.loads(row['request_json'])
            result = json.loads(row['result_json']) if row['result_json'] else {}
            for file in result.get('files') or []:
                recorded_storage = parse_qs(urlsplit(file.get('url') or '').query).get('storage', [None])[0]
                name = file.get('name')
                if not isinstance(name, str) or Path(name).name != name:
                    continue
                reference = self.service.media_storage.reference(name, recorded_storage)
                if reference is None or reference.parent != directory:
                    continue
                name = reference.name
                prompt = request.get('prompt')
                if not isinstance(prompt, str) or not prompt.strip():
                    continue
                entry = {'prompt': prompt, 'task_id': row['id'], 'task_status': row['status'],
                         'generation_created_at': row['created_at'], 'source': 'console_job',
                         'model': request.get('video_model') if row['kind'] == 'video' else request.get('model'),
                         'parameters': {key: request[key] for key in ('aspect', 'count', 'duration', 'quality', 'video_mode')
                                        if key in request and (row['kind'] == 'video' or key in ('aspect', 'count'))}}
                links.setdefault(name, {})[row['id']] = entry
        # Native CLI results are linked only by an explicit media filename/path.
        # Never infer ownership from a timestamp, prompt similarity or row order.
        db_path = self.service.engine.db_path
        if db_path.is_file():
            with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                columns = {r[1] for r in db.execute('pragma table_info(media)')}
                if {'file_name', 'file_path', 'prompt'} <= columns:
                    for index, row in enumerate(db.execute('select file_name,file_path,prompt from media')):
                        name, prompt = row['file_name'], row['prompt']
                        if not name or Path(name).name != name or not prompt:
                            continue
                        source = Path(row['file_path']).resolve().parent if row['file_path'] else self.service.media_storage.original
                        reference = self.service.media_storage.reference(name, self.service.media_storage.key(source))
                        if reference is None or reference.parent != directory:
                            continue
                        name = reference.name
                        # An unambiguous console link already has richer metadata.
                        if not any(value['source'] == 'console_job' for value in links.get(name, {}).values()):
                            links.setdefault(name, {})[f'native:{index}'] = {'prompt': prompt, 'task_id': None,
                                'task_status': None, 'generation_created_at': None, 'source': 'engine_media',
                                'model': None, 'parameters': {}}
        return links

    def catalog(self, *, persist=True) -> list[dict]:
        with self.lock:
            items = []
            for directory in self.service.media_storage.directories:
                items.extend(self._catalog_directory(directory, persist=persist and not self.service.media_storage.migration.pending))
            items.sort(key=lambda item: (item['created_at'], item['id']), reverse=True)
            return items

    def _catalog_directory(self, directory, *, persist=True):
        index_path = directory / 'flow-media-index.json'
        old = {}
        if index_path.is_symlink():
            raise HTTPException(409, '素材索引文件位置异常，未写入。')
        if index_path.exists():
            try:
                document = json.loads(index_path.read_text(encoding='utf-8'))
                if document.get('schema_version') != 1 or document.get('generator') != 'flow-agent-console':
                    raise ValueError('unrecognized index')
                old = {item['name']: item for item in document['items']}
            except (ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(409, '现有素材索引格式无法识别，已保留原文件；请先备份并检查 flow-media-index.json。') from None
        links = self._job_links(directory)
        items = []
        for path in directory.iterdir() if directory.is_dir() else []:
            kind = self.category(path)
            if not kind or not path.is_file() or path.is_symlink() or path.resolve().parent != directory.resolve():
                continue
            stat = path.stat()
            signature = f'{stat.st_size}:{stat.st_mtime_ns}'
            prior = old.get(path.name, {})
            candidates = links.get(path.name, {})
            metadata = {'prompt': None, 'task_id': None, 'model': None, 'parameters': {},
                        'generation_created_at': None, 'task_status': None, 'source': None}
            status = 'missing_prompt'
            if prior.get('index_status') == 'file_changed' or prior.get('prompt') and prior.get('file_signature') != signature:
                status = 'file_changed'
            elif len(candidates) == 1:
                metadata.update(next(iter(candidates.values())))
                status = 'indexed'
            elif len(candidates) > 1:
                status = 'ambiguous'
            elif prior.get('prompt') and prior.get('file_signature') == signature:
                metadata.update({key: prior.get(key) for key in metadata})
                status = 'indexed'
            storage = self.service.media_storage
            item_id = storage.file_id(directory, path.name)
            item = {'id': item_id, 'name': path.name, 'kind': kind, 'size': stat.st_size,
                    'created_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec='seconds'),
                    'url': storage.url(directory, path.name), **metadata,
                    'title': ' '.join((metadata['prompt'] or path.name).split())[:100],
                    'index_status': status, 'file_signature': signature}
            items.append(item)
        items.sort(key=lambda item: (item['created_at'], item['name']), reverse=True)
        document = {'schema_version': 1, 'generator': 'flow-agent-console', 'items': items}
        # Atomic sidecar update only; the image/video bytes and names stay put.
        if persist and directory.is_dir():
            content = json.dumps(document, ensure_ascii=False, indent=2)
            if not index_path.is_file() or index_path.read_text(encoding='utf-8') != content:
                temporary = directory / ('.flow-media-index-' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_text(content, encoding='utf-8')
                    temporary.replace(index_path)
                finally:
                    temporary.unlink(missing_ok=True)
        return items

    @staticmethod
    def filtered(items, search, kind):
        if kind not in ('all', 'image', 'video'):
            raise HTTPException(422, '素材分类无效')
        normalize = lambda value: unicodedata.normalize('NFKC', value).casefold()
        words = normalize(search[:500]).split()
        return [item for item in items if (kind == 'all' or item['kind'] == kind) and all(
            word in normalize(' '.join(str(item.get(key) or '') for key in ('name', 'prompt', 'task_id', 'model')))
            for word in words)]

    def snapshot(self, search='', kind='all', limit=60, offset=0):
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(422, '素材分页参数无效')
        items = self.catalog()
        matched = self.filtered(items, search, kind)
        return {'directory': str(self.directory.resolve()), 'index_path': str(self.index_path.resolve()),
                'directories': self.service.media_storage.snapshot()['directories'],
                'total': len(items), 'indexed': sum(item['index_status'] == 'indexed' for item in items),
                'matching': len(matched), 'items': matched[offset:offset + limit], 'offset': offset, 'limit': limit}

    def open_folder(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            raise HTTPException(503, '打开本地文件夹仅适用于 Windows。')
        try:
            subprocess.Popen(['explorer.exe', str(self.directory.resolve())],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError:
            raise HTTPException(503, '无法打开素材文件夹，请复制页面中的本地路径。') from None
        return {'opened': True}
