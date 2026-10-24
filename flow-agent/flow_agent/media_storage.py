"""User-selected local output folders; historical folders stay readable."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class StorageInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    directory: str = Field(min_length=1, max_length=1024)


class MediaStorage:
    def __init__(self, service):
        self.service = service
        self.original = service.engine.output_dir.resolve()
        self.directories = [self.original]
        self.redirects = {}
        self.picker_lock = asyncio.Lock()
        from .media_migration import MediaMigration
        self.migration = MediaMigration(self)

    @staticmethod
    def key(directory: Path):
        return hashlib.sha256(os.path.normcase(str(directory.resolve())).encode()).hexdigest()[:24]

    def load(self):
        with self.service.connect() as db:
            row = db.execute("select value from console_meta where key='media_storage'").fetchone()
        if not row:
            return
        try:
            data = json.loads(row[0])
            paths = [Path(value) for value in data['directories']]
            active = Path(data['directory'])
            if data['version'] != 1 or not active.is_absolute() or any(not p.is_absolute() for p in paths):
                raise ValueError()
            self.directories = list(dict.fromkeys([*paths, active]))
            self.redirects = data.get('redirects', {})
            self.service.engine.output_dir = active
        except (ValueError, TypeError, KeyError):
            raise RuntimeError('素材保存目录配置无法读取，请检查本机 console.db 中的 media_storage 配置。') from None

    def snapshot(self):
        active = self.service.engine.output_dir
        return {'directory': str(active), 'default_directory': str(self.original),
                'directories': [{'id': self.key(p), 'directory': str(p), 'available': p.is_dir(),
                                 'active': p == active} for p in self.directories],
                'moves_existing_files': False}

    def require_idle(self, *, allow_migration=False):
        self.service.preparation.require_idle_queue(allow_migration=allow_migration)
        if self.service.preparation.running or self.service.scheduler.active:
            raise HTTPException(409, '生成或网页检查正在运行，请结束后再更改保存目录。')

    def configure(self, value: str):
        self.require_idle()
        path = self.validate_path(value)
        self.probe(path)
        directories = list(dict.fromkeys([*self.directories, path]))
        config = {'version': 1, 'directory': str(path), 'directories': [str(p) for p in directories],
                  'redirects': self.redirects}
        with self.service.connect() as db:
            db.execute("insert or replace into console_meta values ('media_storage', ?)", (json.dumps(config),))
        self.directories = directories
        self.service.engine.output_dir = path
        return self.snapshot()

    def validate_path(self, value: str):
        value = value.strip()
        path = Path(value)
        if (not value or '\x00' in value or not path.is_absolute() or str(path).startswith(('\\\\', '//'))
                or path == Path(path.anchor)):
            raise HTTPException(422, '请选择本地磁盘中的完整文件夹路径，例如 E:\\FlowMedia；不能使用磁盘根目录或网络路径。')
        if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in [path, *path.parents]):
            raise HTTPException(422, '保存目录不能使用符号链接或目录联接，请选择真实的本地文件夹。')
        path = path.resolve()
        private = [self.service.engine.root / name for name in ('cookies', 'data', 'bin')]
        if any(path == p.resolve() or p.resolve() in path.parents for p in private):
            raise HTTPException(422, '请选择独立的素材文件夹，不能使用程序的账号或内部数据目录。')
        return path

    @staticmethod
    def probe(path):
        try:
            path.mkdir(parents=True, exist_ok=True)
            # Existing foreign indexes are never overwritten by selecting a folder.
            index = path / 'flow-media-index.json'
            if index.is_symlink():
                raise ValueError()
            if index.exists():
                content = json.loads(index.read_text(encoding='utf-8'))
                if (content.get('schema_version') != 1 or content.get('generator') != 'flow-agent-console'
                        or not isinstance(content.get('items'), list)):
                    raise ValueError()
            with tempfile.TemporaryFile(dir=path) as probe:
                probe.write(b'flow-storage-write-check')
                probe.flush()
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(409, '该目录已有无法识别的素材索引，已保留原文件，请选择其他目录。') from None
        except OSError:
            raise HTTPException(422, '目录无法创建或写入，请检查磁盘是否连接、路径和写入权限。') from None

    def reference(self, name: str, storage: str | None = None):
        key = (storage or self.key(self.original)) + ':' + name
        redirected = self.redirects.get(key)
        if redirected:
            return Path(redirected['directory']) / redirected['name']
        scope = storage or self.key(self.original)
        while scope.startswith('d-'):
            scope = scope[2:]
        for directory in self.directories:
            if self.key(directory) == scope:
                return directory / name
        return None

    def file_scope(self, directory: Path, name: str):
        scope = self.key(directory)
        candidate = directory / name
        while candidate.is_file():
            alias = self.redirects.get(scope + ':' + name)
            if not alias or Path(alias['directory']) / alias['name'] == candidate:
                break
            scope = 'd-' + scope
        return scope

    def file_id(self, directory: Path, name: str):
        scope = self.file_scope(directory, name)
        return name if directory == self.original and scope == self.key(directory) else scope + ':' + name

    def url(self, directory: Path, name: str):
        scope = self.file_scope(directory, name)
        suffix = '' if directory == self.original and scope == self.key(directory) else '?storage=' + scope
        return '/api/v1/media/' + quote(name) + suffix

    def resolve(self, name: str, storage: str | None = None):
        if (not name or Path(name).name != name or '/' in name or '\\' in name or ':' in name
                or Path(name).suffix.lower() not in ('.mp4', '.png', '.jpg', '.jpeg', '.webp')):
            raise HTTPException(404, '素材不存在')
        redirected = self.redirects.get((storage or self.key(self.original)) + ':' + name)
        if redirected:
            target = Path(redirected['directory'])
            candidate = target / redirected['name']
            if (target in self.directories and candidate.is_file() and not candidate.is_symlink()
                    and candidate.resolve().parent == target.resolve()):
                return candidate
            raise HTTPException(404, '迁移后的素材不存在或所在磁盘未连接')
        physical_scope = storage
        while physical_scope and physical_scope.startswith('d-'):
            physical_scope = physical_scope[2:]
        directories = [p for p in self.directories if physical_scope is None or self.key(p) == physical_scope]
        found = []
        for directory in directories:
            candidate = directory / name
            if candidate.is_file() and not candidate.is_symlink() and candidate.resolve().parent == directory.resolve():
                if storage is None and directory == self.original:
                    return candidate  # Original URLs continue to identify the original folder.
                found.append(candidate)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise HTTPException(409, '多个素材目录有同名文件，请从素材库中选择对应文件。')
        raise HTTPException(404, '素材不存在或所在磁盘未连接')

    def resolve_id(self, value: str):
        if ':' in value:
            storage, name = value.split(':', 1)
            return self.resolve(name, storage)
        return self.resolve(value)

    async def choose_folder(self):
        if os.name != 'nt':
            raise HTTPException(503, '文件夹选择窗口仅适用于 Windows，可直接输入完整路径。')
        if self.picker_lock.locked():
            raise HTTPException(409, '文件夹选择窗口已经打开，请先选择或取消。')
        async with self.picker_lock:
            script = r'''Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择 Flow 图片和视频的本地保存目录'
$dialog.SelectedPath = $env:FLOW_MEDIA_PICKER_PATH
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
try {
  $result = $dialog.ShowDialog($owner)
  $chosen = if ($result -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath } else { $null }
  [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
  @{directory=$chosen;cancelled=($null -eq $chosen)} | ConvertTo-Json -Compress
} finally { $dialog.Dispose(); $owner.Dispose() }
'''
            env = dict(os.environ, FLOW_MEDIA_PICKER_PATH=str(self.service.engine.output_dir))
            executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            try:
                proc = await asyncio.create_subprocess_exec(str(executable), '-NoProfile', '-STA', '-WindowStyle', 'Hidden',
                    '-Command', script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                raise HTTPException(503, '无法打开文件夹选择窗口，请直接输入完整路径。') from None
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 300)
                if proc.returncode:
                    raise ValueError()
                return json.loads(out.decode('utf-8-sig'))
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise HTTPException(408, '文件夹选择已结束，请重试或直接输入路径。') from None
            except (ValueError, UnicodeError):
                raise HTTPException(503, '无法打开文件夹选择窗口，请直接输入完整路径。') from None
