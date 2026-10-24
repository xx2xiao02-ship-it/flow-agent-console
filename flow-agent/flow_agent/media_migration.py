"""Confirmed, journalled local-media migration. Never invokes a provider."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class MigrationStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str = Field(min_length=1, max_length=64)
    confirmed: bool = False


class MigrationPaused(Exception):
    pass


class MediaMigration:
    def __init__(self, storage):
        self.storage = storage
        self.service = storage.service
        self.record = None
        self.plan = None
        self.task = None
        self.stop_event = threading.Event()

    @property
    def pending(self):
        return bool(self.record and self.record['state'] not in ('succeeded', 'abandoned'))

    def require_available(self):
        if self.pending:
            raise HTTPException(409, '素材目录迁移尚未结束，请到素材库查看进度、继续迁移或保留两边文件后结束。')

    def load(self):
        with self.service.connect() as db:
            row = db.execute("select value from console_meta where key='media_migration'").fetchone()
        self.record = json.loads(row[0]) if row else None
        if self.record and self.record['state'] == 'running':
            self.record.update(state='interrupted', message='程序已重启，迁移已暂停。原文件仅在复制校验并切换目录后清理；点击继续迁移。')
            self._save()

    def snapshot(self):
        if not self.record:
            return {'state': 'idle', 'pending': False}
        r = self.record
        return {key: r.get(key) for key in ('id', 'state', 'phase', 'target', 'sources', 'total', 'total_bytes',
                'copied', 'removed', 'message', 'renamed', 'committed')} | {'pending': self.pending}

    def _save(self):
        with self.service.connect() as db:
            db.execute("insert or replace into console_meta values ('media_migration', ?)", (json.dumps(self.record, ensure_ascii=False),))

    @staticmethod
    def _signature(path):
        s = path.stat()
        return f'{s.st_size}:{s.st_mtime_ns}'

    @staticmethod
    def _index_hash(directory):
        path = directory / 'flow-media-index.json'
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def _check_stop(self):
        if self.stop_event.is_set():
            raise MigrationPaused()

    def _hash(self, path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                self._check_stop()
                digest.update(chunk)
        return digest.hexdigest()

    def _build(self, value):
        target = self.storage.validate_path(value)
        sources = list(self.storage.directories)
        if target.exists() and not target.is_dir():
            raise HTTPException(422, '目标位置是文件，请选择文件夹。')
        for root in sources:
            if not root.is_dir():
                raise HTTPException(409, '素材目录未连接，不能完整迁移：' + str(root))
            if root != target and (root in target.parents or target in root.parents):
                raise HTTPException(422, '迁移目标不能是素材源目录的上级或子目录，请选择独立文件夹。')
        library = self.service.media_library
        existing = library._catalog_directory(target, persist=False) if target.is_dir() else []
        occupied = {p.name.casefold() for p in target.iterdir()} if target.is_dir() else set()
        files, indexes, watch = [], [], []
        for root in sources:
            entries = library._catalog_directory(root, persist=False)
            watch.extend((str(root / item['name']), item['file_signature']) for item in entries)
            index = root / 'flow-media-index.json'
            indexes.append({'directory': str(root), 'sha256': self._index_hash(root),
                            'content': index.read_bytes().decode('utf-8') if index.is_file() else None})
            if root == target:
                continue
            for item in entries:
                name = item['name']
                stem, suffix = Path(name).stem[:160], Path(name).suffix
                number = 0
                while name.casefold() in occupied:
                    number += 1
                    name = f'{stem}-migrated-{self.storage.key(root)[:8]}-{number}{suffix}'
                occupied.add(name.casefold())
                files.append({'source': str(root / item['name']), 'name': name, 'item': item,
                              'signature': item['file_signature'], 'size': item['size'],
                              'sha256': None, 'published': False, 'publish_started': False, 'removed': False})
        target_watch = [(str(target / item['name']), item['file_signature']) for item in existing]
        signature = hashlib.sha256(json.dumps([watch, target_watch, indexes, self.storage.redirects,
            [str(p) for p in sources], self._index_hash(target)], sort_keys=True).encode()).hexdigest()
        parent = target
        while not parent.exists():
            parent = parent.parent
        total_bytes = sum(f['size'] for f in files)
        free = shutil.disk_usage(parent).free
        if free < total_bytes + 1024 * 1024:
            raise HTTPException(422, '目标磁盘剩余空间不足，需能容纳全部迁移素材及索引。')
        return {'target': str(target), 'sources': [str(p) for p in sources], 'files': files,
                'source_watch': sorted((f['source'], f['signature']) for f in files),
                'indexes': indexes, 'target_items': existing, 'signature': signature,
                'target_index_hash': self._index_hash(target), 'total_bytes': total_bytes,
                'free_bytes': free, 'renamed': sum(f['name'] != f['item']['name'] for f in files)}

    def prepare(self, directory):
        self.storage.require_idle()
        plan = self._build(directory)
        self.plan = plan | {'id': uuid.uuid4().hex, 'expires': time.monotonic() + 900}
        return {key: self.plan[key] for key in ('id', 'target', 'sources', 'total_bytes', 'free_bytes', 'renamed')} | {
            'total': len(plan['files']), 'delete_originals_after_verification': True,
            'scope': '全部已登记素材目录中的图片、视频和提示词索引；其他文件及账号不移动。',
            'conflicts': [{'original': f['item']['name'], 'renamed': f['name']} for f in plan['files'] if f['name'] != f['item']['name']]}

    async def start(self, data):
        if not data.confirmed:
            raise HTTPException(422, '请先确认迁移范围和校验后清理原素材的影响。')
        self.storage.require_idle()
        plan = self.plan
        if not plan or plan['id'] != data.plan_id or time.monotonic() > plan['expires']:
            raise HTTPException(409, '迁移预览已过期，请重新检查目标目录。')
        fresh = self._build(plan['target'])
        if fresh['signature'] != plan['signature']:
            raise HTTPException(409, '素材或目录已变化，请重新预览迁移范围。')
        if not fresh['files']:
            raise HTTPException(422, '没有需要迁移的素材；只改后续保存位置请使用“仅更改保存位置”。')
        self.record = copy.deepcopy(fresh) | {'id': plan['id'], 'state': 'running', 'phase': 'copy',
            'total': len(fresh['files']), 'copied': 0, 'removed': 0, 'committed': False,
            'message': '正在复制并逐个校验，原素材暂不清理。'}
        self._save()
        self.plan = None
        self._launch()
        return self.snapshot()

    def _launch(self):
        self.stop_event.clear()
        self.task = asyncio.create_task(asyncio.to_thread(self._run), name='flow-media-migration')

    async def resume(self):
        if self.task and not self.task.done():
            raise HTTPException(409, '迁移仍在执行，请等待当前步骤。')
        if not self.pending:
            raise HTTPException(409, '没有待恢复的迁移。')
        self.storage.require_idle(allow_migration=True)
        self.record.update(state='running', message='正在继续迁移，已校验的素材不会重复覆盖。')
        self._save()
        self._launch()
        return self.snapshot()

    def abandon(self):
        if self.task and not self.task.done():
            raise HTTPException(409, '迁移仍在执行，不能中断文件操作。请正常退出程序后再处理。')
        if not self.pending:
            raise HTTPException(409, '没有待处理的迁移。')
        self.record.update(state='abandoned', message='迁移已结束，保留两边现有文件。' +
            ('新目录已生效；未清理的原文件仍在原处。' if self.record['committed'] else '原保存位置保持不变；已复制文件保留在目标目录。'))
        self._save()
        return self.snapshot()

    async def stop(self):
        if self.task and not self.task.done():
            self.stop_event.set()
            await asyncio.shield(self.task)

    def _copy(self, source, destination):
        with source.open('rb') as src, destination.open('xb') as dst:
            while chunk := src.read(1024 * 1024):
                self._check_stop()
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, destination)

    def _run(self):
        try:
            self._perform()
        except MigrationPaused:
            self.record.update(state='interrupted', message='迁移已安全暂停，重新启动后可继续；未验证的原文件未清理。')
            self._save()
        except Exception as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc) if isinstance(exc, ValueError) else '磁盘读取、写入或权限检查失败。请连接磁盘、释放空间或恢复权限后继续。'
            self.record.update(state='needs_attention', message=message)
            self._save()

    def _perform(self):
        r = self.record
        target = self.storage.validate_path(r['target'])
        stage = target / ('.flow-migration-' + r['id'])
        self.storage.validate_path(str(stage))
        if not r['committed']:
            self.storage.probe(target)
            stage.mkdir(exist_ok=True)
            # Preserve the original sidecars for recovery and audit, including unknown prompts.
            for i, index in enumerate(r['indexes']):
                if index['content'] is not None:
                    backup = stage / f'source-index-{i}.json'
                    if backup.is_symlink():
                        raise ValueError('迁移索引备份位置异常。')
                    if not backup.exists():
                        with backup.open('xb') as stream:
                            stream.write(index['content'].encode('utf-8'))
            for f in r['files']:
                self._check_stop()
                source = Path(f['source'])
                self.storage.validate_path(str(source.parent))
                if source.is_symlink() or not source.is_file() or self._signature(source) != f['signature']:
                    raise ValueError('原素材已改变或不可用，未清理原文件；请恢复原文件后继续：' + source.name)
                digest = self._hash(source)
                if f['sha256'] and f['sha256'] != digest:
                    raise ValueError('原素材内容已改变，已停止迁移：' + source.name)
                f['sha256'] = digest
                destination, temporary = target / f['name'], stage / f['name']
                if not destination.exists():
                    if temporary.exists():
                        if temporary.is_symlink():
                            raise ValueError('迁移临时文件位置异常。')
                        # Only this journal's own incomplete copy is replaced.
                        temporary.unlink()
                    self._copy(source, temporary)
                    if self._hash(temporary) != digest or self._signature(source) != f['signature']:
                        raise ValueError('复制校验未通过，原素材未清理：' + source.name)
                    f['publish_started'] = True
                    self._save()
                    if os.name == 'nt':
                        temporary.rename(destination)  # Windows refuses an existing destination.
                    else:
                        os.link(temporary, destination)
                        temporary.unlink()
                elif not f['publish_started']:
                    raise ValueError('目标出现同名文件，未覆盖：' + f['name'])
                if destination.is_symlink() or self._hash(destination) != digest:
                    raise ValueError('目标素材校验未通过，原素材未清理：' + f['name'])
                f['published'] = True
                r['copied'] = sum(bool(item['published']) for item in r['files'])
                self._save()
            self._check_stop()
            current_watch = sorted((str(root / item['name']), item['file_signature'])
                for root in (Path(value) for value in r['sources']) if root != target
                for item in self.service.media_library._catalog_directory(root, persist=False))
            if current_watch != [tuple(item) for item in r['source_watch']]:
                raise ValueError('原目录新增或改变了素材，已停止切换目录并保留两边文件；请结束后重新预览迁移。')
            # Build a merged sidecar with exact source prompt bindings and renamed filenames.
            entries = copy.deepcopy(r['target_items'])
            for f in r['files']:
                item = copy.deepcopy(f['item'])
                item.update(name=f['name'], id=self.storage.file_id(target, f['name']),
                    url=self.storage.url(target, f['name']), file_signature=self._signature(target / f['name']))
                entries.append(item)
            entries.sort(key=lambda item: (item['created_at'], item['name']), reverse=True)
            content = json.dumps({'schema_version': 1, 'generator': 'flow-agent-console', 'items': entries}, ensure_ascii=False, indent=2)
            new_hash = hashlib.sha256(content.encode()).hexdigest()
            current_hash = self._index_hash(target)
            if current_hash not in (r['target_index_hash'], new_hash):
                raise ValueError('目标素材索引已变化，已保留两边文件，请先检查。')
            for f in r['files']:
                if self._signature(Path(f['source'])) != f['signature'] or self._hash(target / f['name']) != f['sha256']:
                    raise ValueError('切换目录前复核失败，原素材未清理。')
            index_path = target / 'flow-media-index.json'
            backup = stage / 'previous-target-index.json'
            if backup.is_symlink():
                raise ValueError('目标索引备份位置异常。')
            if index_path.is_file() and current_hash != new_hash and not backup.exists():
                shutil.copy2(index_path, backup)
            pending_index = stage / ('merged-index-' + uuid.uuid4().hex + '.json')
            with pending_index.open('xb') as stream:
                stream.write(content.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            pending_index.replace(index_path)
            redirects = copy.deepcopy(self.storage.redirects)
            source_scopes = {f['source']: self.storage.file_scope(Path(f['source']).parent, Path(f['source']).name) for f in r['files']}
            moves = {(str(Path(f['source']).parent), Path(f['source']).name): f['name'] for f in r['files']}
            for alias, value in redirects.items():
                name = moves.get((value['directory'], value['name']))
                if name is not None:
                    redirects[alias] = {'directory': str(target), 'name': name}
            for f in r['files']:
                source = Path(f['source'])
                redirects[source_scopes[f['source']] + ':' + source.name] = {'directory': str(target), 'name': f['name']}
            config = {'version': 1, 'directory': str(target), 'directories': [str(target)], 'redirects': redirects}
            committed = copy.deepcopy(r)
            committed.update(committed=True, phase='cleanup', message='复制和校验完成，新目录已生效；正在核验后清理原素材。')
            with self.service.connect() as db:
                db.execute("insert or replace into console_meta values ('media_storage', ?)", (json.dumps(config),))
                db.execute("insert or replace into console_meta values ('media_migration', ?)", (json.dumps(committed, ensure_ascii=False),))
            r.update(committed)
            self.storage.load()
        # A restarted post-commit operation only verifies/cleans originals; never generates or recopies.
        for f in r['files']:
            self._check_stop()
            source, destination = Path(f['source']), target / f['name']
            self.storage.validate_path(str(source.parent))
            if destination.is_symlink() or not destination.is_file() or self._hash(destination) != f['sha256']:
                raise ValueError('新目录文件不可用或已变化，已停止清理原素材：' + f['name'])
            if f['removed']:
                continue
            if source.exists():
                if source.is_symlink() or self._signature(source) != f['signature'] or self._hash(source) != f['sha256']:
                    raise ValueError('原目录有变化，已保留该文件；请检查后继续或结束并保留两边文件：' + source.name)
                self._check_stop()
                source.unlink()
            f['removed'] = True
            r['removed'] = sum(bool(item['removed']) for item in r['files'])
            self._save()
        for index in r['indexes']:
            root = Path(index['directory'])
            old_index = root / 'flow-media-index.json'
            if root != target and index['sha256'] and old_index.is_file() and self._index_hash(root) == index['sha256']:
                old_index.unlink()  # Exact original sidecar is archived in the target migration journal folder.
        r.update(state='succeeded', phase='complete', message='整体迁移完成，已校验全部素材并清理原文件；旧任务下载链接继续可用。')
        self._save()
