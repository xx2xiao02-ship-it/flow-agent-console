"""Local startup readiness; never invokes generation or account RPCs."""
from __future__ import annotations
import asyncio
import copy
import json
import tempfile
import subprocess
import sys
from pathlib import Path
from fastapi import HTTPException

class StartupCheck:
    def __init__(self, service):
        self.service = service
        self.active = False
        self.state = 'checking'
        self.items = []
        self.finished_at = None
        self.task = None
        self.total = 9
        self.local_profiles = []

    def snapshot(self):
        return {'state': self.state, 'ready': self.state == 'ready',
                'message': {'checking':'正在启动自查，暂不接受生成任务。',
                    'ready':'启动检查完成；需要处理的账号请到账号管理查看。',
                    'blocked':'启动自查未通过，请处理下方故障后重新启动程序。'}[self.state],
                'checks': copy.deepcopy(self.items), 'finished_at': self.finished_at,
                'completed': sum(i['state'] != 'checking' for i in self.items),
                'total': self.total, 'local_profiles': self.local_profiles,
                'generation_submitted': False}

    async def check_accounts(self, keys):
        """Fresh reads before dispatch starts; never submit or show login windows."""
        from .account_scheduling import browser_identity_ready
        for index, key in enumerate(keys, 1):
            row = {'id': 'account:' + key, 'label': f'账号核验 {index}/{len(keys)}',
                   'state': 'checking', 'message': ''}
            self.items.append(row)
            try:
                async with self.service.balance_refresh_lock:
                    await asyncio.wait_for(self.service.refresh_account_details(account_ids={key}), 60)
                detail = self.service._verified_detail(key) or {}
                if browser_identity_ready(self.service, key) and detail.get('error') in ('session', 'refused'):
                    preparation = self.service.preparation
                    preparation.report = preparation._new_report(key)
                    await asyncio.wait_for(preparation._run_headless(key, repair_login=False), 150)
                    detail = self.service._verified_detail(key) or {}
                if not browser_identity_ready(self.service, key):
                    message = '浏览器关联不完整，请在账号管理重新连接。'
                elif detail.get('error') or detail.get('credits') is None:
                    message = '身份或积分未通过核验，请在账号管理处理。'
                else:
                    message = ''
                row.update(state='warning' if message else 'passed', message=message or '身份、会话、浏览器关联和积分已核对')
                if message:
                    self.service.monitor.startup_issues[key] = message
            except asyncio.CancelledError:
                raise
            except Exception:
                message = '本次启动核验未完成，请在账号管理重新检测。'
                self.service.monitor.startup_issues[key] = message
                row.update(state='warning', message=message)

    def require_ready(self):
        if self.active and self.state != 'ready':
            raise HTTPException(503, self.snapshot()['message'])

    async def run_check(self, key, label, action, remedy):
        row = {'id':key, 'label':label, 'state':'checking', 'message':''}
        self.items.append(row)
        try:
            await asyncio.to_thread(action)
            row.update(state='passed', message='检查通过')
        except Exception:
            # Never expose exception strings, account data or credentials.
            row.update(state='failed', message=remedy)

    @staticmethod
    def writable(path):
        path.mkdir(parents=True, exist_ok=True)
        # Exclusive disposable probe: never overwrite an existing user file.
        with tempfile.NamedTemporaryFile(prefix='.flow-startup-', dir=path):
            pass

    def database(self):
        with self.service.connect() as db:
            if db.execute('pragma quick_check(1)').fetchone()[0] != 'ok':
                raise ValueError('database check failed')
            db.execute('begin immediate')
            db.rollback()

    def resources(self):
        root = self.service.engine.root.parent
        dist = root / 'web' / 'dist'
        if not (dist / 'index.html').is_file() or not any((dist / 'assets').glob('*.js')):
            raise ValueError('web assets missing')
        extension = root / 'flow-extension'
        manifest = json.loads((extension / 'manifest.json').read_text('utf-8'))
        worker = (manifest.get('background') or {}).get('service_worker')
        if not worker or not (extension / worker).is_file():
            raise ValueError('extension worker missing')

    def browser(self):
        path = self.service._find_extension_browser()
        if not path or not Path(path).is_file():
            raise ValueError('browser missing')
        if Path(path).name.lower() == 'chrome.exe' and not (Path(path).parent/'chrome.dll').is_file():
            raise ValueError('browser components missing')

    def browser_driver(self):
        from .browser_runtime import require_playwright_driver
        require_playwright_driver(probe=True)

    def engine_runtime(self):
        self.require_file(self.service.engine.binary)
        result = subprocess.run([str(self.service.engine.binary), '--help'],
            cwd=self.service.engine.root, env=self.service.engine._process_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('engine cannot start')

    def mcp_runtime(self):
        if getattr(sys, 'frozen', False):
            root = Path(sys.executable).resolve().parent
            self.require_file(root/'FlowConsoleMCP.exe')
            self.require_file(root/'mcp-runtime'/'base_library.zip')
        else:
            self.require_file(Path(__file__).resolve().parent.parent/'console_mcp_entry.py')

    async def run(self):
        from .console import utc_now
        try:
            try:
                await asyncio.to_thread(self.service.initialize)
            except Exception:
                self.items.append({'id':'initialize','label':'本地数据初始化','state':'failed',
                    'message':'数据目录或配置无法打开。请确认磁盘已连接、目录可写；不要删除原数据库。'})
                self.state = 'blocked'
                return
            checks = [
                ('database','数据库完整性与访问',self.database,'数据库无法正常读写或完整性检查未通过。请保留数据库并检查磁盘、权限或备份。'),
                ('data','本地数据目录',lambda:self.writable(self.service.db_path.parent),'本地数据目录不可写，请检查磁盘和文件夹权限。'),
                ('storage','素材保存目录',lambda:self.writable(self.service.engine.output_dir),'素材保存目录不可写或磁盘不可用，请恢复磁盘或调整保存目录。'),
                ('engine','Flow 运行引擎',self.engine_runtime,'Flow 引擎缺失或无法运行，请重新安装客户端。'),
                ('mcp','MCP 接入组件',self.mcp_runtime,'MCP 接入组件缺失，请重新安装客户端。'),
                ('browser','专用浏览器组件',self.browser,'专用浏览器组件缺失，请重新安装客户端。'),
                ('browser_driver','浏览器自动化驱动',self.browser_driver,'浏览器驱动缺失或无法启动，请修复安装客户端；此问题不是账号或网页故障。'),
                ('resources','管理界面与扩展文件',self.resources,'管理界面或扩展文件缺失，请重新安装客户端。'),
            ]
            keys = sorted(self.service._cookie_files())
            self.total = len(checks) + 1 + len(keys)
            for args in checks:
                await self.run_check(*args)
            if any(i['state'] == 'failed' for i in self.items):
                self.state = 'blocked'
                return
            self.service.monitor.load()
            def scan_local():
                self.local_profiles = self.service.local_profiles()
            await self.run_check('local_accounts', '扫描本机可复用账号', scan_local,
                                 '本机账号扫描失败，可在账号管理重新扫描。')
            # A scan failure does not invalidate usable dedicated accounts.
            if self.items[-1]['state'] == 'failed':
                self.items[-1]['state'] = 'warning'
            await self.check_accounts(keys)
            self.service.queue = asyncio.Queue()
            self.service.scheduler.wakeup = asyncio.Event()
            self.service.worker = asyncio.create_task(self.service.run(), name='flow-console-worker')
            self.service.account_details_task = asyncio.create_task(self.service._watch_account_details(), name='flow-account-details')
            self.state = 'ready'
        except asyncio.CancelledError:
            raise
        except Exception:
            self.items.append({'id':'services','label':'后台服务','state':'failed',
                'message':'后台服务启动失败，请检查本地日志并重新启动。'})
            self.state = 'blocked'
        finally:
            self.finished_at = utc_now()

    @staticmethod
    def require_file(path):
        if not Path(path).is_file() or Path(path).stat().st_size == 0:
            raise ValueError('runtime missing')

    def start(self):
        self.active = True
        self.state = 'checking'
        self.items = []
        self.total = 9
        self.local_profiles = []
        self.service.monitor.startup_issues.clear()
        self.finished_at = None
        self.task = asyncio.create_task(self.run(), name='flow-startup-check')

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
