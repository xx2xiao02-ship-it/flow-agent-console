"""Generate through visible Flow controls, never by minting tokens or replaying RPCs.

Only a cold, program-owned account profile is opened. No connection to a user's
existing browser, stealth flags, private client functions, or CAPTCHA solving.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from .account_details import payloads
from .browser_visibility import OwnedBrowserVisibility, BrowserVisibilityError, HeadlessCheckVisibility
from . import humanize


def image_suffix(data: bytes) -> str:
    from PIL import Image
    try:
        if not 32 <= len(data) <= 50 * 1024 * 1024:
            raise ValueError()
        with Image.open(BytesIO(data)) as image:
            suffix = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(image.format)
            if not suffix or min(image.size) < 1:
                raise ValueError()
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
        return suffix
    except Exception:
        raise OfficialImageError('download_invalid', submitted=True) from None


def generation_rpc(request):
    return ('/data/batchexecute' in request.url and
            'ogiZ0b' in request.url + (request.post_data_buffer or b'').decode('utf-8', 'replace'))


async def read_generation_body(response, report):
    """Read the existing response only; never resend a provider request."""
    report['generation_response_http_status'] = response.status
    report['stage'] = 'read_generation_response'
    try:
        text = await asyncio.wait_for(response.text(), 30)
    except Exception as exc:
        # Playwright errors can contain request URLs or tokens. Store only a
        # fixed classification, never the original exception or response body.
        message = str(exc).lower()
        reason = ('body_missing' if 'no data found for resource with given identifier' in message else
                  'body_evicted' if 'no resource with given identifier' in message else
                  'browser_closed' if 'closed' in message else
                  'read_timeout' if isinstance(exc, TimeoutError) else
                  'decode_error' if isinstance(exc, UnicodeError) else
                  'body_unavailable')
        report['response_read_exception_type'] = type(exc).__name__
        report['response_read_api'] = ('network_get_response_body' if 'network.getresponsebody' in message else 'response_text')
        report['response_read_error'] = reason
        raise OfficialImageError('generation_response_unreadable', submitted=True) from None
    report['response_body_read'] = True
    return text


def result_ids(docs):
    ids = set()
    for doc in docs:
        if isinstance(doc, list) and len(doc) > 2 and isinstance(doc[2], list):
            for row in doc[2]:
                if not (isinstance(row, list) and row and isinstance(row[0], str)
                        and re.fullmatch(r'[a-fA-F0-9-]{36}', row[0])):
                    continue
                ids.add(row[0])  # Media ID used by the original image URL.
                # Official media rows bind the distinct editor/scene ID in field 3.
                # The public /edit/ URL uses this ID, not the media UUID.
                if len(row) > 2 and isinstance(row[2], str) and re.fullmatch(r'[a-fA-F0-9-]{36}', row[2]):
                    ids.add(row[2])
    ids.update(re.findall(r'https://flow-content\.google/image/([a-fA-F0-9-]{36})',
                         json.dumps(docs)))
    return ids


def detail_bindings(docs, media_ids, project):
    """Only an official detail media row binds media -> project -> scene.

    Never accept a UUID found somewhere in a response or a different project.
    """
    rows = set()
    for doc in docs:
        if (isinstance(doc, list) and len(doc) > 2 and isinstance(doc[0], str)
                and doc[0] in media_ids and doc[1] == project
                and isinstance(doc[2], str) and re.fullmatch(r'[a-fA-F0-9-]{36}', doc[2])):
            rows.add((doc[0], doc[2]))
    return rows


def result_urls(docs):
    urls = []
    def walk(value):
        if isinstance(value, str) and value.startswith('https://flow-content.google/image/'):
            urls.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(docs)
    return list(dict.fromkeys(urls))


class OfficialImageError(RuntimeError):
    def __init__(self, code: str, *, submitted: bool = False):
        self.code = code
        self.submitted = submitted
        self.execution = None
        super().__init__(code)


def profile_in_use(profile: Path) -> bool:
    for process in psutil.process_iter(['name']):
        try:
            if (process.info.get('name') or '').lower() not in ('chrome', 'chrome.exe', 'chromium'):
                continue
            args = process.cmdline()
            for index, arg in enumerate(args):
                candidate = (arg.split('=', 1)[1] if arg.startswith('--user-data-dir=') else
                             args[index + 1] if arg == '--user-data-dir' and index + 1 < len(args) else None)
                if candidate and Path(candidate.strip('"')).resolve() == profile.resolve():
                    return True
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # Cannot safely establish exclusive ownership.
            return True
    return False


class OfficialImages:
    def __init__(self, service):
        self.service = service
        self.timeout = 180
        self.last_report: dict = {}
        self.active_reports: dict[int, dict] = {}
        self.manual_timeout = 180
        self.submit_ready_timeout = 15
        self.agent_ready_timeout = 8

    @property
    def active_report(self):
        return next(iter(self.active_reports.values()), None)

    @active_report.setter
    def active_report(self, report):
        task_key = id(asyncio.current_task())
        if report is None:
            self.active_reports.pop(task_key, None)
        else:
            from .task_scheduler import CURRENT_JOB
            report['job_id'] = CURRENT_JOB.get()
            self.active_reports[task_key] = report

    def settings(self, account: str, *, allow_identity_home=False):
        service = self.service
        cookie = service._cookie_files().get(account)
        profile_name = service._account_profile_name(account)
        detail = service._verified_detail(account)
        if not cookie or not profile_name or not detail or not detail.get('email'):
            raise OfficialImageError('identity_missing')
        profile = service.engine.root / 'data' / profile_name
        if not profile.is_dir() or profile.is_symlink():
            raise OfficialImageError('profile_missing')
        bundle = json.loads(cookie.read_text(encoding='utf-8'))
        project = bundle.get('project_id')
        if not isinstance(project, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', project):
            if not allow_identity_home:
                raise OfficialImageError('project_missing')
            project = None
        chrome = service._find_extension_browser()
        if not chrome:
            raise OfficialImageError('browser_missing')
        if profile_in_use(profile):
            raise OfficialImageError('profile_busy')
        return profile, chrome, detail['email'], f'https://flow.google.com/project/{project}' if project else 'https://flow.google.com/'

    async def run(self, **options):
        runner = copy.copy(self)
        try:
            return await runner._run(**options)
        finally:
            self.last_report = runner.last_report

    async def _run(self, *, cookies=None, prompt='', aspect='16:9', count=1,
                  model='beluga', all_accounts=False, download=True, timeout=None,
                  check_only=False, _recover=None, _preparation=False, _identity_only=False, _account_reader=None, _repair_visible=False):
        if model != 'beluga' or count != 1 or all_accounts or aspect not in ('16:9', '9:16', '1:1', '4:3', '3:4'):
            raise OfficialImageError('unsupported_options')
        cookie = cookies or await self.service._scheduled_cookie()
        async with self.service.browser_execution.page_slot(cookie):
            if self.service.preparation.running and not (_preparation and check_only):
                raise OfficialImageError('preparation_busy')
            account = Path(cookie).stem
            if self.service._cookie_files().get(account) != Path(cookie):
                raise OfficialImageError('identity_missing')
            from . import proxy_pool
            proxy = proxy_pool.proxy_for(self.service, account)
            try:
                profile, chrome, email, url = self.settings(account)
            except OfficialImageError as exc:
                if not (check_only and _preparation and exc.code == 'project_missing'):
                    raise
                profile, chrome, email, url = self.settings(account, allow_identity_home=True)
            report = {'route': 'official_page', 'account_id': account,
                      'upload_project_id': urlsplit(url).path.rsplit('/', 1)[-1],
                      'check_only': check_only, 'generation_submitted': False,
                      'generation_requests': 0, 'blocked_generation_requests': 0,
                      'download_verified': False, 'browser_closed': False, 'stage': 'launch'}
            from .work_log import Report
            report = Report(self.service, report)
            context = None
            spawned = None
            from .verification_settings import snapshot
            preferences = snapshot(self.service)
            headless = bool(check_only and (_recover is None or _recover.get('inspect_only')) and not _repair_visible and preferences['headless'])
            if _repair_visible and not (check_only and _preparation):
                raise OfficialImageError('unsupported_options')
            visible_mode = not headless and (preferences['generation_browser_visible']
                                             or _repair_visible)
            # Generation tasks can run on a private Win32 desktop: fully visible
            # to the page, never shown on the user's desktop. Check-only and
            # preparation flows stay on the current desktop for human handling.
            isolated = bool(visible_mode and not check_only and preferences.get('isolated_desktop'))
            guard = (HeadlessCheckVisibility if headless else OwnedBrowserVisibility)(
                profile, Path(chrome),
                mode='isolated' if isolated else 'visible' if visible_mode else 'hidden')
            watcher = None
            self.active_report = report
            try:
                from .browser_runtime import require_playwright_driver, BrowserRuntimeError
                try:
                    require_playwright_driver()
                except BrowserRuntimeError as exc:
                    report['runtime_component'] = exc.component
                    raise OfficialImageError(exc.code) from None
                await guard.start()
                from playwright.async_api import async_playwright
                async with async_playwright() as pw:
                    try:
                        if isolated:
                            from . import isolated_desktop
                            report['isolated_desktop'] = True
                            extra_args = guard.launch_args()
                            forwarder = None
                            if proxy:
                                if proxy.get('username'):
                                    # Chromium cannot take proxy credentials on
                                    # the command line and its auth prompt
                                    # would hang on the private desktop; a
                                    # local relay injects the credentials.
                                    raw_proxy = proxy_pool.raw_url_for(self.service, account)
                                    forwarder = isolated_desktop.ProxyForwarder(raw_proxy)
                                    relay_port = await forwarder.start()
                                    extra_args.append(f'--proxy-server=http://127.0.0.1:{relay_port}')
                                else:
                                    extra_args.append(f'--proxy-server={proxy["server"]}')
                                report['proxy'] = proxy['server']
                            spawned = None
                            try:
                                spawned = isolated_desktop.spawn(str(profile), str(chrome), extra_args)
                                if forwarder:
                                    spawned.forwarder = forwarder
                                browser_cdp, context = await isolated_desktop.attach(pw.chromium, spawned, str(profile))
                            except Exception:
                                if spawned:
                                    spawned.terminate()  # Closes the forwarder too.
                                elif forwarder:
                                    forwarder.close()
                                raise
                        else:
                            spawned = None
                            context = await pw.chromium.launch_persistent_context(
                                str(profile), executable_path=str(chrome), headless=headless,
                                accept_downloads=True, locale='zh-CN', viewport={'width': 1280, 'height': 900},
                                args=[*guard.launch_args(), '--disable-background-mode'],
                                **({'proxy': proxy} if proxy else {}), timeout=30000)
                        if check_only and _preparation:
                            from .account_repair import seed_new_profile
                            await seed_new_profile(self.service, account, profile, context, Path(cookie))
                        page = await context.new_page()
                        # Retain completed responses across renderer changes.
                        # This only configures capture; it never replays an RPC.
                        network = await context.new_cdp_session(page)
                        await network.send('Network.enable', {
                            'maxTotalBufferSize': 64 * 1024 * 1024,
                            'maxResourceBufferSize': 16 * 1024 * 1024,
                            'enableDurableMessages': True,
                        })
                        # Flow preloads detail RPCs before the user opens a card.
                        # Keep only exact project/media/editor UUID bindings from
                        # this browser session, never response bodies or tokens.
                        self._detail_evidence = set()
                        async def remember_detail(response):
                            request = response.request
                            if (request.method != 'POST' or '/data/batchexecute' not in request.url
                                    or urlsplit(response.url).netloc != urlsplit(url).netloc
                                    or 'as29s' not in request.url + (request.post_data_buffer or b'').decode('utf-8', 'replace')):
                                return
                            try:
                                for doc in payloads(await response.text(), 'as29s'):
                                    if (isinstance(doc, list) and len(doc) > 2
                                            and all(isinstance(x, str) and re.fullmatch(r'[a-fA-F0-9-]{36}', x) for x in doc[:3])
                                            and len(self._detail_evidence) < 512):
                                        self._detail_evidence.add(tuple(doc[:3]))
                            except Exception:
                                pass
                        page.on('response', remember_detail)
                        await guard.confirm_hidden()
                        report['visibility'] = guard.info()
                        watcher = asyncio.create_task(self.watch_attention(context, guard, report))
                        # No generation before the one explicit click, including
                        # requests from a restored draft or page-side retries.
                        async def gate(route):
                            request = route.request
                            text = request.url + (request.post_data_buffer or b'').decode('utf-8', 'replace')
                            if request.method == 'POST' and re.search(r'ogiZ0b|YhhmEf|nprQif|eb1hJf|MZZa6b|jIps6|batchGenerate|generateImages|generateVideo|upsample', text, re.I):
                                if (check_only or report.get('query_only') or not report['generation_submitted'] or
                                        report['generation_requests'] >= 1 or not generation_rpc(request)):
                                    report['blocked_generation_requests'] += 1
                                    await route.abort()
                                    return
                                report['generation_requests'] += 1
                            await route.continue_()
                        await context.route('**/*', gate)
                        report['stage'] = 'load_page'
                        await page.goto(url, wait_until='domcontentloaded', timeout=45000)
                        page.set_default_timeout(15000)
                        report['stage'] = 'verify_parameters'
                        await self.wait_for_identity(page, email, guard, report)
                        if check_only and (_preparation or _identity_only):
                            if _preparation and _account_reader is not None:
                                fresh = json.loads(Path(cookie).read_bytes())
                                tokens = await page.evaluate('''() => {
                                    const w = window.WIZ_global_data || {};
                                    return {at: w.SNlM0e || '', fsid: w.FdrFJe || ''};
                                }''')
                                if not tokens.get('at') or not tokens.get('fsid'):
                                    raise OfficialImageError('identity_not_ready')
                                fresh.update(tokens)
                                fresh['cookies'] = await context.cookies()
                                fresh['fingerprint'] = dict(fresh.get('fingerprint') or {})
                                fresh['fingerprint']['user_agent'] = await page.evaluate('navigator.userAgent')
                                await _account_reader(fresh)
                            report.update(stage='identity_check_completed', page_ready=True)
                            return report
                        if _recover and _recover.get('project_id') and _recover['project_id'] != urlsplit(url).path.rsplit('/', 1)[-1]:
                            raise OfficialImageError('recovery_project_mismatch')
                        if _recover and _recover.get('inspect_only'):
                            if _recover.get('prompt'):
                                report['project_prompt_check'] = {
                                    'exact_text_matches': await page.get_by_text(_recover['prompt'], exact=True).count(),
                                    'image_elements': await page.get_by_role('group').locator('img').count(),
                                    'result_binding_verified': False,
                                }
                            return await self.inspect_unknown_result(page, report, snapshot_only=bool(_recover.get('snapshot_only')))
                        await self.prepare(page, email=email, aspect=aspect)
                        report['page_ready'] = True
                        sources = await self.image_sources(page)
                        report['existing_media_count'] = len(sources)
                        report['correlatable_media_count'] = sum(bool(re.search(r'[a-fA-F0-9-]{36}', src)) for src in sources)
                        if _recover is not None:
                            if not check_only:
                                raise OfficialImageError('unsupported_options')
                            return await self.recover_download(page, account, report, url, _recover)
                        if check_only:
                            report['stage'] = 'check_completed'
                            return report
                        report['stage'] = 'generate'
                        from .uncertain_result import observe_uncertain
                        before = await self.image_sources(page)
                        async def late_image_response(response):
                            if generation_rpc(response.request) and response.ok and urlsplit(response.url).netloc == urlsplit(url).netloc:
                                try:
                                    ids = result_ids(payloads(await response.text(), 'ogiZ0b'))
                                    if ids:
                                        report['response_result_ids'] = sorted(ids)
                                except Exception:
                                    pass
                        async def recover_original(deadline):
                            ids = report.get('response_result_ids')
                            if not ids:
                                await page.get_by_role('group').count()
                                return None
                            if report.get('download_verified'):
                                return None
                            return await self.collect_image_results(page, account, report, time.monotonic(),
                                deadline, before, set(ids), True, urlsplit(url).path.rsplit('/', 1)[-1])
                        page.on('response', late_image_response)
                        try:
                            try:
                                return await self.submit_once(page, prompt, account, report, email,
                                    timeout or self.timeout, download, urlsplit(url).path.rsplit('/', 1)[-1])
                            except Exception as exc:
                                from .work_log import emit
                                from .task_scheduler import CURRENT_JOB
                                emit(self.service, CURRENT_JOB.get(), '生成或结果读取异常', report, exc)
                                if not report['generation_submitted'] or getattr(exc, 'code', None) == 'provider_rejected':
                                    raise
                                report['original_error_code'] = getattr(exc, 'code', type(exc).__name__)
                                report['original_error_stage'] = report.get('stage')
                                return await observe_uncertain(report, recover_original)
                        finally:
                            page.remove_listener('response', late_image_response)
                    finally:
                        import sys
                        if sys.exc_info()[0] is not None and 'page' in locals():
                            from .work_log import failure_snapshot
                            await failure_snapshot(self.service, page, report)
                        if watcher:
                            watcher.cancel()
                            try:
                                await watcher
                            except asyncio.CancelledError:
                                pass
                        if context is not None:
                            try:
                                await asyncio.wait_for(context.close(), 15)
                            except Exception:
                                report['cleanup_warning'] = True
                            finally:
                                if spawned is not None:
                                    spawned.terminate()  # CDP detach leaves Chrome alive.
                                report['browser_closed'] = not profile_in_use(profile)
                                if not report['browser_closed']:
                                    report['browser_closed'] = await guard.close_owned()
            except asyncio.CancelledError:
                raise
            except OfficialImageError as exc:
                report['error_code'] = exc.code
                report['visibility'] = guard.info()
                exc.execution = report
                raise
            except BrowserVisibilityError:
                error = OfficialImageError('visibility_failed', submitted=report['generation_submitted'])
                report['error_code'] = error.code
                error.execution = report
                raise error from None
            except ImportError:
                raise OfficialImageError('runtime_missing') from None
            except Exception as exc:
                report['exception_type'] = type(exc).__name__
                report['error_code'] = 'page_failed'
                error = OfficialImageError('page_failed', submitted=report['generation_submitted'])
                error.execution = report
                raise error from None
            finally:
                if headless:
                    try:
                        report['browser_closed'] = await guard.close_owned()
                    except Exception:
                        report['browser_closed'] = False
                await guard.stop()
                report['visibility'] = guard.info()
                self.active_report = None
                self.last_report = dict(report)
                try:
                    with self.service.connect() as db:
                        db.execute('insert or replace into console_meta values (?, ?)',
                                   ('last_official_image_execution', json.dumps(report)))
                except Exception:
                    pass  # A diagnostic write cannot hide the operation outcome.

    async def inspect_unknown_result(self, page, report, snapshot_only=False):
        from .uncertain_result import observe_uncertain
        report['recovered_without_generation'] = True
        async def inspect(deadline):
            report['original_project_query'] = {
                'completed': True, 'project_id': report.get('upload_project_id'),
                'video_elements': await page.locator('video').count(),
                'gallery_groups': await page.get_by_role('group').count(),
                'empty_state_visible': await page.get_by_text('开始创建或拖放媒体', exact=True).is_visible(),
                'result_binding_verified': False,
            }
            return None
        if snapshot_only:
            await inspect(None)
            report.update(stage='inspection_completed', query_only=True, outcome='pending_review')
            return report
        return await observe_uncertain(report, inspect)

    @staticmethod
    async def attention_reason(page):
        if urlsplit(page.url).hostname in ('accounts.google.com', 'accounts.google.com.hk'):
            return 'login'
        for selector in ('input[type="password"]', 'input[type="email"]',
                         'iframe[src*="recaptcha"][src*="bframe"]', 'iframe[title*="challenge"]'):
            for item in await page.locator(selector).all():
                if await item.is_visible():
                    return 'login_or_verification'
        warning = page.get_by_text(re.compile(
            r'验证您是本人|验证您的身份|确认您不是机器人|完成.*人机验证|Verify it.s you|Verify you are human', re.I))
        for item in await warning.all():
            if await item.is_visible():
                return 'login_or_verification'
        return None

    async def watch_attention(self, context, guard, report):
        try:
            while True:
                for page in context.pages:
                    if page.is_closed():
                        continue
                    try:
                        reason = await self.attention_reason(page)
                        if reason:
                            if report.get('check_only'):
                                guard.reveal(reason)
                            else:
                                # Generation run: never pop the window. Pull the
                                # account from the pool and notify; the run keeps
                                # its normal deadline and lands on 待核对 if the
                                # challenge blocks result reading.
                                self._mark_manual_attention(report, reason)
                                report['manual_attention'] = reason
                    except Exception:
                        continue  # Navigation can replace the DOM during this read.
                report['visibility'] = guard.info()
                await asyncio.sleep(.3)
        except asyncio.CancelledError:
            raise

    def _mark_manual_attention(self, report, reason):
        from .account_scheduling import mark_manual_attention
        mark_manual_attention(self.service, report.get('account_id'), reason,
                              job_id=report.get('job_id'))

    async def wait_for_identity(self, page, email, guard, report):
        identity = page.get_by_role('button', name=re.compile(
            r'(Google 账号|Google Account).*' + re.escape(email), re.I))
        started = time.monotonic()
        manual_started = None
        entered_home = False
        while True:
            if guard.error:
                raise BrowserVisibilityError(guard.error)
            location = urlsplit(page.url)
            if not entered_home and location.hostname == 'flow.google.com' and location.path.rstrip('/') == '/about':
                entry = page.get_by_role('button', name=re.compile(r'^\s*(?:使用 Google Flow 创建|使用 Google Flow 创作|Create with Google Flow|Get started|开始使用)\s*$', re.I))
                if await entry.count() and await entry.first.is_visible():
                    entered_home = True
                    report['identity_entry'] = 'flow_about'
                    await entry.first.click(timeout=10000)
                    await asyncio.sleep(.5)
                    continue
            denied = page.get_by_role('heading', name=re.compile(
                r'您似乎无权访问\s*Flow|You (?:don.t|do not|seem not to) have access to Flow', re.I))
            if await denied.count() and await denied.is_visible():
                raise OfficialImageError('flow_access_denied')
            reason = await self.attention_reason(page)
            if reason:
                if not report.get('check_only') and await identity.count() != 1:
                    # Generation run: pull the account from the pool and fail
                    # fast instead of popping a window and waiting for a human.
                    # Nothing has been submitted yet, so nothing can be charged.
                    # A transient overlay with the identity button still visible
                    # keeps waiting instead of failing.
                    self._mark_manual_attention(report, reason)
                    report['manual_attention'] = reason
                    raise OfficialImageError('manual_verification_required')
                guard.reveal(reason)
            if guard.shown:
                manual_started = manual_started or time.monotonic()
                report['stage'] = 'waiting_for_user'
            if await identity.count() == 1 and not reason:
                guard.hide_after_verification()
                if guard.enabled:
                    await guard.confirm_hidden()
                report['visibility'] = guard.info()
                report['stage'] = 'verify_parameters'
                return
            if manual_started is not None and time.monotonic() - manual_started >= self.manual_timeout:
                raise OfficialImageError('manual_timeout')
            if manual_started is None and time.monotonic() - started >= 40:
                raise OfficialImageError('identity_not_ready')
            await asyncio.sleep(.2)

    def agent_guard_failure(self, reason):
        if self.active_report is not None:
            self.active_report['agent_guard_reason'] = reason
        raise OfficialImageError('agent_panel_unconfirmed')

    async def assert_direct_mode(self, page):
        instructions = page.get_by_role('button', name='智能体指令', exact=True).filter(visible=True)
        toggle = page.get_by_role('button', name='智能体', exact=True).filter(visible=True)
        if await instructions.count():
            self.agent_guard_failure('instructions_still_visible')
        if await toggle.count() > 1:
            self.agent_guard_failure('ambiguous_agent_toggle')
        if await toggle.count() == 1 and await toggle.get_attribute('aria-pressed') == 'true':
            self.agent_guard_failure('agent_mode_still_enabled')

    async def ensure_direct_composer(self, page):
        report = self.active_report
        if report is not None:
            report['parameter_step'] = 'verify_direct_composer'
        instructions = page.get_by_role('button', name='智能体指令', exact=True).filter(visible=True)
        new_chat = page.get_by_role('button', name='发起新的会话', exact=True).filter(visible=True)
        # Older UI: close only the smallest verified sidebar containing its
        # instructions, new-chat and close controls, not a global close button.
        if await new_chat.count() and await instructions.count():
            if await new_chat.count() != 1:
                self.agent_guard_failure('ambiguous_sidebar')
            panel = new_chat
            closed = False
            for _ in range(12):
                panel = panel.locator('xpath=..')
                close = panel.get_by_role('button', name='关闭', exact=True).filter(visible=True)
                if (await close.count() == 1
                        and await panel.get_by_role('button', name='发起新的会话', exact=True).filter(visible=True).count() == 1
                        and await panel.get_by_role('button', name='智能体指令', exact=True).filter(visible=True).count() == 1):
                    if report is not None:
                        report['parameter_step'] = 'close_agent_sidebar'
                    await close.click()
                    try:
                        await close.wait_for(state='hidden', timeout=int(self.agent_ready_timeout*1000))
                        await new_chat.wait_for(state='hidden', timeout=int(self.agent_ready_timeout*1000))
                    except Exception:
                        self.agent_guard_failure('sidebar_close_not_confirmed')
                    closed = True
                    if report is not None:
                        report['agent_panel_closed'] = True
                    break
            if not closed:
                self.agent_guard_failure('sidebar_close_control_missing')
        textbox = page.get_by_role('textbox', name='您希望创作什么？', exact=True).filter(visible=True)
        await textbox.wait_for()
        # Current UI: agent mode is an aria-pressed toggle inside the SAME
        # composer. There is no sidebar and therefore no close/new-chat button.
        container = textbox
        toggle = None
        for _ in range(8):
            container = container.locator('xpath=..')
            candidate = container.get_by_role('button', name='智能体', exact=True).filter(visible=True)
            submit = container.get_by_role('button', name='开始生成', exact=True).filter(visible=True)
            if await candidate.count() == 1 and await submit.count() == 1:
                toggle = candidate
                break
        if toggle is not None:
            pressed = await toggle.get_attribute('aria-pressed')
            if report is not None:
                report['agent_mode_before'] = pressed
            if pressed == 'true':
                if report is not None:
                    report['parameter_step'] = 'disable_agent_mode'
                await toggle.click()  # One UI mode switch; never a generate click.
                deadline = time.monotonic() + self.agent_ready_timeout
                while True:
                    state = await toggle.get_attribute('aria-pressed')
                    direct = page.get_by_role('button', name='设置触发器', exact=True).filter(visible=True)
                    if state == 'false' and await instructions.count() == 0 and await direct.count() == 1:
                        if report is not None:
                            report.update(agent_mode_disabled=True, agent_mode_after=state)
                        break
                    if time.monotonic() >= deadline:
                        self.agent_guard_failure('agent_mode_switch_not_confirmed')
                    await asyncio.sleep(.1)
            elif pressed not in ('false', None) or await instructions.count():
                self.agent_guard_failure('agent_toggle_state_unconfirmed')
        await self.assert_direct_mode(page)
        if report is not None:
            report['direct_composer_confirmed'] = True

    async def composer_settings(self, page):
        textbox = page.get_by_role('textbox', name='您希望创作什么？', exact=True).filter(visible=True)
        trigger = page.get_by_role('button', name='设置触发器', exact=True).filter(visible=True)
        if await trigger.count() != 1:
            if await page.get_by_role('button', name='智能体指令', exact=True).filter(visible=True).count():
                raise OfficialImageError('agent_panel_unconfirmed')
            # The new official editor calls both app and composer controls 设置.
            # Anchor to the nearest prompt ancestor containing its submit button;
            # never click the global settings based on page order.
            container = textbox
            trigger = None
            for _ in range(8):
                container = container.locator('xpath=..')
                candidate = container.get_by_role('button', name='设置', exact=True).filter(visible=True)
                submit = container.get_by_role('button', name='开始生成', exact=True).filter(visible=True)
                if await candidate.count() == 1 and await submit.count() == 1:
                    trigger = candidate
                    break
            if trigger is None:
                raise OfficialImageError('composer_settings_missing')
        return trigger

    async def prepare(self, page, *, email, aspect):
        identity = page.get_by_role('button', name=re.compile(r'(Google 账号|Google Account).*' + re.escape(email), re.I))
        await identity.wait_for(state='attached', timeout=40000)
        if await identity.count() != 1:
            raise OfficialImageError('identity_mismatch')
        await self.ensure_direct_composer(page)
        textbox = page.get_by_role('textbox', name='您希望创作什么？', exact=True).filter(visible=True)
        await textbox.wait_for()
        await humanize.pause('read')
        await humanize.wander(page)
        if self.active_report is not None:
            self.active_report['parameter_step'] = 'open_composer_settings'
        trigger = await self.composer_settings(page)
        await trigger.click()
        await humanize.pause('action')
        if self.active_report is not None:
            self.active_report['parameter_step'] = 'select_image_mode'
        await page.get_by_role('radio', name='图片', exact=True).click()
        await humanize.pause('action')
        if self.active_report is not None:
            self.active_report['parameter_step'] = 'select_aspect'
        await page.get_by_role('radio', name=aspect, exact=True).click()
        await humanize.pause('action')
        if self.active_report is not None:
            self.active_report['parameter_step'] = 'select_count'
        await page.get_by_role('radio', name='x1', exact=True).click()
        await humanize.pause('action')
        if self.active_report is not None:
            self.active_report['parameter_step'] = 'select_model'
        model = page.get_by_role('button', name='选择模型系列', exact=True)
        if not re.search(r'Nano Banana\s*2\.1', await model.inner_text()):
            await model.click()
            await page.get_by_role('menuitem', name=re.compile(r'Nano Banana\s*2\.1$')).click()
            if not re.search(r'Nano Banana\s*2\.1', await model.inner_text()):
                raise OfficialImageError('model_not_selected')
        for name in (aspect, 'x1', '图片'):
            if not await page.get_by_role('radio', name=name, exact=True).is_checked():
                raise OfficialImageError('parameters_unconfirmed')
        await page.keyboard.press('Escape')
        if self.active_report is not None:
            self.active_report['parameters'] = {'model': 'beluga', 'aspect': aspect, 'count': 1, 'kind': 'image'}
            self.active_report['parameter_step'] = 'confirmed'

    @staticmethod
    async def image_sources(page):
        # Only images inside rendered media groups, excluding avatars and icons.
        return set(await page.get_by_role('group').locator('img').evaluate_all(
            '(images) => images.map(i => i.currentSrc || i.src).filter(Boolean)'))

    async def ready_to_submit(self, page, prompt, email, report):
        await self.assert_direct_mode(page)
        textbox = page.get_by_role('textbox', name='您希望创作什么？', exact=True).filter(visible=True)
        # Trusted keyboard events only (paste + typed tail); never JS value writes.
        await humanize.enter_prompt(page, textbox, prompt)
        if (await self.editor_prompt(textbox)).strip() != prompt.replace('\r\n', '\n').strip():
            raise OfficialImageError('prompt_unconfirmed')
        # The official editor enables submit asynchronously after its input event.
        # Waiting for readiness must never become a generation or click retry.
        button = page.get_by_role('button', name='开始生成', exact=True)
        started = time.monotonic()
        report['stage'] = 'wait_submit_ready'
        while not await button.is_enabled():
            if time.monotonic() - started >= self.submit_ready_timeout:
                raise OfficialImageError('submit_disabled')
            await asyncio.sleep(.1)
        report['submit_ready_wait_seconds'] = round(time.monotonic() - started, 2)
        if (await self.editor_prompt(textbox)).strip() != prompt.replace('\r\n', '\n').strip():
            raise OfficialImageError('prompt_unconfirmed')
        # Recheck visible identity after parameter changes, immediately before submit.
        identity = page.get_by_role('button', name=re.compile('Google 账号.*' + re.escape(email), re.I))
        if await identity.count() != 1:
            raise OfficialImageError('identity_mismatch')
        await self.assert_direct_mode(page)
        return button

    @staticmethod
    async def editor_prompt(textbox):
        """Read editor content without the layout gaps between Flow paragraphs.

        Flow turns input lines into direct <p> children. innerText introduces
        an extra newline between those paragraphs; textContent loses all line
        breaks. Reconstruct just that observed representation, retaining empty
        paragraphs, explicit <br> breaks, spaces and every prompt character.
        Other editor shapes keep their normal rendered text (or form value).
        """
        return await textbox.evaluate('''element => {
            if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement)
                return element.value.replace(/\\r\\n/g, '\\n');
            const nodes = Array.from(element.childNodes);
            if (nodes.length && nodes.every(n => n.nodeType === 1 && n.tagName === 'P') &&
                    !element.querySelector('p div, p p, p ul, p ol, p table')) {
                const text = node => node.nodeType === 3 ? node.textContent :
                    node.nodeType === 1 && node.tagName === 'BR' ? '\\n' :
                    Array.from(node.childNodes).map(text).join('');
                return nodes.map(p => p.childNodes.length === 1 &&
                    p.firstChild.nodeName === 'BR' ? '' : text(p)).join('\\n');
            }
            return element.innerText.replace(/\\r\\n/g, '\\n');
        }''')

    async def submit_once(self, page, prompt, account, report, email, timeout, download=True, project=None):
        button = await self.ready_to_submit(page, prompt, email, report)
        before = await self.image_sources(page)
        started = time.monotonic()
        report['stage'] = 'generate'
        # One longer human pause before the only paid click; the click itself
        # stays single-shot and is never retried.
        await humanize.pause('pre_submit')
        # Mark uncertainty BEFORE clicking. Never retry a click after a timeout.
        report['generation_submitted'] = True
        async with page.expect_response(lambda response: generation_rpc(response.request) and urlsplit(response.url).netloc == urlsplit(page.url).netloc,
                                        timeout=max(timeout, 15) * 1000) as pending:
            await button.click(timeout=10000)
        response = await pending.value
        report['generation_response_http_status'] = response.status
        if not response.ok:
            raise OfficialImageError('provider_rejected', submitted=True)
        text = await read_generation_body(response, report)
        docs = payloads(text, 'ogiZ0b')
        from .official_videos import video_response_summary, origin_only
        report['generation_response'] = {'http_status': response.status, 'response_origin': origin_only(response.url),
            **video_response_summary(text, 'ogiZ0b', docs)}
        if report['generation_response']['rpc_error_codes']:
            raise OfficialImageError('provider_rejected', submitted=True)
        ids = result_ids(docs)
        if not ids:
            raise OfficialImageError('provider_empty', submitted=True)
        report['response_result_ids'] = sorted(ids)
        return await self.collect_image_results(page, account, report, started,
            started + timeout, before, ids, download, project, docs)

    async def collect_image_results(self, page, account, report, started, deadline, before,
                                    ids, download=True, project=None, docs=None):
        candidate = None
        while time.monotonic() < deadline:
            new_sources = await self.image_sources(page) - before
            matched = [source for source in new_sources if any(key in source for key in ids)]
            if not matched and len(new_sources) == 1:
                # Flow renders blob: thumbnails. A unique new card is only a
                # candidate; its public detail URL must confirm the response ID.
                matched = list(new_sources)
            if len(matched) == 1:
                source = matched[0]
                images = page.get_by_role('group').locator('img')
                for img in await images.all():
                    if await img.evaluate('(i) => i.currentSrc || i.src') == source:
                        candidate = img
                        break
            if candidate:
                break
            if len(matched) > 1:
                raise OfficialImageError('result_ambiguous', submitted=True)
            if await page.get_by_text(re.compile('异常活动|无法生成|生成失败|unusual activity', re.I)).count():
                raise OfficialImageError('provider_rejected', submitted=True)
            await asyncio.sleep(.5)
        if candidate is None:
            raise OfficialImageError('result_timeout', submitted=True)
        report['stage'] = 'verify_result_identity'
        binding = await self.verify_detail(page, ids, project, candidate.dblclick)
        report['result_identity_verified'] = True
        report['result_detail_id'] = binding[1]
        report['result_media_id'] = binding[0]
        if not download:
            return {'job_id': uuid.uuid4().hex, 'status': 'succeeded', 'account_id': account,
                    'media_ids': sorted(ids), 'media': [{'url': url, 'kind': 'image'} for url in result_urls(docs)],
                    'elapsed_seconds': round(time.monotonic() - started, 2), 'execution': report}
        return await self.save_download(page, account, report, started)

    async def verify_detail(self, page, media_ids, project, open_detail):
        rows = set()
        received = asyncio.Event()
        async def capture(response):
            request = response.request
            text = request.url + (request.post_data_buffer or b'').decode('utf-8', 'replace')
            if '/data/batchexecute' not in request.url or 'as29s' not in text:
                return
            try:
                found = detail_bindings(payloads(await response.text(), 'as29s'), media_ids, project)
                if found:
                    rows.update(found)
                    received.set()
            except Exception:
                pass  # Missing/malformed evidence cannot authorize a download.
        page.on('response', capture)
        try:
            await open_detail()
            await page.get_by_role('button', name='下载媒体内容', exact=True).wait_for()
            await page.wait_for_url(re.compile(r'/edit/[a-fA-F0-9-]{36}(?:[/?#]|$)'))
            match = re.fullmatch(r'/project/([^/]+)/edit/([a-fA-F0-9-]{36})/?', urlsplit(page.url).path)
            if not match or match.group(1) != project:
                raise OfficialImageError('result_identity_mismatch', submitted=True)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                cached = {(media, detail) for media, owner, detail in getattr(self, '_detail_evidence', set())
                          if media in media_ids and owner == project and detail == match.group(2)}
                matched = cached | {row for row in rows if row[1] == match.group(2)}
                if len(matched) == 1:
                    return next(iter(matched))
                if len(matched) > 1:
                    break
                await asyncio.sleep(.1)
            raise OfficialImageError('result_identity_mismatch', submitted=True)
        finally:
            page.remove_listener('response', capture)

    async def recover_download(self, page, account, report, project_url, target):
        ids, detail = target.get('media_ids'), target.get('detail_id')
        if (not isinstance(ids, list) or len(ids) != 1
                or any(not isinstance(x, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', x) for x in [*ids, *([detail] if detail is not None else [])])):
            raise OfficialImageError('unsupported_options')
        project = urlsplit(project_url).path.rsplit('/', 1)[-1]
        report['stage'] = 'recover_existing_result'
        deadline = time.monotonic() + 120
        # Resolve the editor ID from the original project's preloaded details.
        # The media UUID is not itself an editor URL.
        if detail is None:
            until = time.monotonic() + 10
            while time.monotonic() < until:
                matches = {editor for media, owner, editor in getattr(self, '_detail_evidence', set())
                           if media in ids and owner == project}
                if len(matches) == 1:
                    detail = next(iter(matches))
                    break
                await asyncio.sleep(.1)
        async def open_exact():
            await page.goto(project_url + '/edit/' + (detail or ids[0]), wait_until='domcontentloaded', timeout=15000)
        binding = None
        try:
            binding = await asyncio.wait_for(self.verify_detail(page, set(ids), project, open_exact), 35)
        except (OfficialImageError, TimeoutError):
            pass
        if binding is None and detail is None:
            # Some image responses provide only the media ID, not the editor ID.
            # Inspect existing cards read-only, accepting only exact media/project
            # evidence from the detail response. Never use prompt/order as identity.
            await page.goto(project_url, wait_until='domcontentloaded', timeout=15000)
            await page.get_by_role('group').locator('img').first.wait_for(state='visible', timeout=15000)
            count = min(await page.get_by_role('group').locator('img').count(), 48)
            for index in range(count):
                if time.monotonic() >= deadline:
                    break
                if index:
                    await page.goto(project_url, wait_until='domcontentloaded', timeout=15000)
                    await page.get_by_role('group').locator('img').nth(index).wait_for(state='visible', timeout=15000)
                report['recovery_cards_checked'] = index + 1
                try:
                    binding = await asyncio.wait_for(self.verify_detail(page, set(ids), project,
                        page.get_by_role('group').locator('img').nth(index).dblclick), min(6, max(.1, deadline-time.monotonic())))
                    break
                except (OfficialImageError, TimeoutError):
                    continue
        if binding is None or (detail is not None and binding[1] != detail):
            raise OfficialImageError('result_identity_mismatch')
        report.update(result_identity_verified=True, result_media_id=binding[0], result_detail_id=binding[1],
                      recovered_without_generation=True)
        return await self.save_download(page, account, report, time.monotonic())

    async def save_download(self, page, account, report, started):
        report['stage'] = 'download_original'
        data, suffix = await self.download_original(page)
        job_id = uuid.uuid4().hex
        output = self.service.engine.output_dir
        output.mkdir(parents=True, exist_ok=True)
        path = output / f'flow-page-{job_id}{suffix}'
        # Exclusive create; no existing paid asset can be overwritten.
        with path.open('xb') as stream:
            stream.write(data)
        report['download_verified'] = True
        report['stage'] = 'completed'
        return {'job_id': job_id, 'status': 'succeeded', 'account_id': account,
                'files': [{'path': str(path)}], 'elapsed_seconds': round(time.monotonic() - started, 2),
                'execution': report}

    @staticmethod
    async def download_original(page):
        await page.get_by_role('button', name='下载媒体内容', exact=True).click()
        async with page.expect_download(timeout=30000) as pending_download:
            await page.get_by_role('menuitem', name=re.compile(r'1K.*原始')).click()
        item = await pending_download.value
        temporary = await item.path()
        if not temporary or await item.failure():
            raise OfficialImageError('download_failed', submitted=True)
        data = Path(temporary).read_bytes()
        suffix = image_suffix(data)
        return data, suffix
