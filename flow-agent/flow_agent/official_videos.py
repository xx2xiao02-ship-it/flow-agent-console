"""Video generation using normal Flow page controls and browser downloads.

No private verification calls, token extraction, request replay or hidden RPC
submission. Response listeners only correlate the user's one UI submission.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .account_details import payloads
from .browser_visibility import OwnedBrowserVisibility, BrowserVisibilityError, HeadlessCheckVisibility
from . import humanize
from .mp4_input import inspect_mp4
from .video_models import VIDEO_MODELS, validate_video_model
from .official_images import OfficialImages, OfficialImageError

VIDEO_RPCS = frozenset({'YhhmEf', 'nprQif', 'eb1hJf', 'MZZa6b'})
COMPOSER_PREFERENCE_RPCS = frozenset({'DA4VGb', 'Kcr7Ub'})
GENERATION_RPCS = VIDEO_RPCS | {'ogiZ0b', 'jIps6'}
READ_RPCS = frozenset({'UpteDb', 'o30O0e', 'HTrJv', 'yBhWQ', 'WuwhI', 'jwpduf',
    'as29s', 'nzlxg', 'Zzl0ze', 'ngNC2', 'mrlkwd', 'DTaVef', 'tRARke', 'NfrxTb',
    'KV2T2d', 'LPzVkd', 'cPZSdc', 'Yizz8d', 'qJcgMc', 've2Lsc'})
UUID = re.compile(r'[a-fA-F0-9-]{36}')


def rpc_ids(request):
    """Read only RPC names, never expose request bodies or tokens in reports."""
    ids = set(filter(None, parse_qs(urlsplit(request.url).query).get('rpcids', [''])[0].split(',')))
    data = request.post_data_buffer or b''
    try:
        form = parse_qs(data.decode('utf-8'))
        frames = json.loads(form['f.req'][0]) if 'f.req' in form else []
        for batch in frames:
            for row in batch:
                if isinstance(row, list) and row and isinstance(row[0], str):
                    ids.add(row[0])
    except (ValueError, KeyError, TypeError, UnicodeError):
        pass
    return ids


def video_result_ids(docs, project):
    """Read the explicit scene field, or the media field when scenes are absent.

    Public XS response schema: field 3 = Zy scenes (project field 5),
    field 4 = Xy media (project field 2). Do not recursively scan UUIDs,
    merge the two namespaces, or accept a media row from another project.
    """
    scenes, media = [], []
    saw_scene_rows = False
    for doc in docs:
        if not isinstance(doc, list):
            continue
        if len(doc) > 2 and isinstance(doc[2], list):
            saw_scene_rows |= bool(doc[2])
            for row in doc[2]:
                if (isinstance(row, list) and row and isinstance(row[0], str)
                        and UUID.fullmatch(row[0])
                        and (len(row) <= 4 or row[4] is None or row[4] == project)):
                    if row[0] not in scenes:
                        scenes.append(row[0])
        if len(doc) > 3 and isinstance(doc[3], list):
            for row in doc[3]:
                if (isinstance(row, list) and len(row) > 1
                        and isinstance(row[0], str) and UUID.fullmatch(row[0])
                        and row[1] == project and row[0] not in media):
                    media.append(row[0])
    # Conflicting/nonempty scenes never get silently replaced by media.
    return scenes if saw_scene_rows else media


def origin_only(url):
    parsed = urlsplit(url)
    # Retain only a known origin; no path, query, user info or arbitrary host.
    return 'https://labs.google' if parsed.scheme == 'https' and parsed.netloc == 'labs.google' else 'other'


def video_response_summary(text, rpc, docs):
    """Bounded structural evidence, without prompts, URLs, tokens or body text."""
    def shape(value, depth=0):
        if isinstance(value, list):
            return {'type': 'list', 'length': len(value),
                    'items': [shape(v, depth + 1) for v in value[:6]] if depth < 2 else []}
        if value is None:
            return {'type': 'null'}
        if isinstance(value, str):
            return {'type': 'string', 'length': len(value)}
        return {'type': 'boolean' if isinstance(value, bool) else 'number' if isinstance(value, (int, float)) else 'object'}
    from .provider_diagnostics import error_record
    frames, rpc_errors, error_records = [], [], []
    for line in text.splitlines():
        if not line.startswith('['):
            continue
        try:
            batch = json.loads(line)
        except ValueError:
            continue
        if not isinstance(batch, list):
            continue
        for frame in batch:
            if isinstance(frame, list) and frame[:2] == ['wrb.fr', rpc]:
                frames.append(shape(frame))
                # HTTP 200 can carry an RPC error instead of a result payload.
                # Retain only the numeric status, never provider text/details.
                if (len(frame) > 5 and frame[2] is None
                        and isinstance(frame[5], list) and frame[5]
                        and type(frame[5][0]) is int and frame[5][0] != 0):
                    rpc_errors.append(frame[5][0])
                    error_records.append(error_record(frame[5]))
    return {'rpc': rpc, 'payload_count': len(docs),
            'matching_frame_count': len(frames), 'frames': frames[:4],
            'payload_shapes': [shape(d) for d in docs[:4]],
            'rpc_error_codes': sorted(set(rpc_errors)), 'rpc_errors': error_records[:4],
            'diagnostic_format': 'allowlisted_reasons_v2'}


def video_detail_bindings(docs, requested, project):
    rows = set()
    for doc in docs:
        if (isinstance(doc, list) and len(doc) > 2 and doc[1] == project
                and isinstance(doc[0], str) and UUID.fullmatch(doc[0])
                and isinstance(doc[2], str) and UUID.fullmatch(doc[2])):
            # The flat public detail row binds content -> project -> editor ID.
            # Video generation returns the editor/media ID; image generation
            # can instead return the distinct content ID. Never conflate them.
            generated = doc[2] if doc[2] in requested else doc[0] if doc[0] in requested else None
            if generated:
                rows.add((generated, doc[0], doc[2]))
    return rows


class OfficialVideoError(OfficialImageError):
    pass


class OfficialVideos(OfficialImages):
    def __init__(self, service):
        super().__init__(service)
        self.timeout = 600
        self.upload_timeout = 60
        self.candidate_verify_wait = 20
        self._detail_rows = set()

    async def run(self, **options):
        runner = copy.copy(self)
        try:
            return await runner._run(**options)
        finally:
            self.last_report = runner.last_report

    async def _run(self, *, cookies=None, prompt='', aspect='16:9', duration='8s',
                  quality='720p', count=1, start_image=None, end_image=None,
                  references=None, all_accounts=False, download=True, timeout=None,
                  check_only=False, _recover=None, video_model='omni_flash', _dispatch_probe=False):
        references = list(references or [])
        try:
            validate_video_model(video_model, duration, quality,
                                 'references' if references else 'frames' if start_image else 'text')
        except ValueError:
            raise OfficialVideoError('unsupported_video_options') from None
        aspect = {'landscape': '16:9', 'portrait': '9:16'}.get(aspect, aspect)
        if (all_accounts or aspect not in ('16:9', '9:16') or duration not in ('4s', '6s', '8s', '10s')
                or quality not in ('360p', '720p') or count not in (1, 2, 3, 4)
                or (end_image and not start_image)
                or (references and (start_image or end_image or count != 1))
                or (_recover is not None and not check_only)):
            raise OfficialVideoError('unsupported_video_options')
        options = dict(aspect=aspect, duration=duration, quality=quality, count=count,
                       start_image=start_image, end_image=end_image, references=references,
                       video_model=video_model)
        cookie = cookies or await self.service._scheduled_cookie()
        async with self.service.browser_execution.page_slot(cookie):
            if self.service.preparation.running:
                raise OfficialVideoError('preparation_busy')
            account = Path(cookie).stem
            if self.service._cookie_files().get(account) != Path(cookie):
                raise OfficialVideoError('identity_missing')
            from . import proxy_pool
            proxy = proxy_pool.proxy_for(self.service, account)
            try:
                profile, chrome, email, url = self.settings(account)
            except OfficialImageError as exc:
                raise OfficialVideoError(exc.code) from None
            from .verification_settings import snapshot
            preferences = snapshot(self.service)
            headless = bool(check_only and (_recover is None or _recover.get('inspect_only')) and preferences['headless'])
            visible_mode = not headless and preferences['generation_browser_visible']
            isolated = bool(visible_mode and not check_only and preferences.get('isolated_desktop'))
            guard = (HeadlessCheckVisibility if headless else OwnedBrowserVisibility)(
                profile, Path(chrome),
                mode='isolated' if isolated else 'visible' if visible_mode else 'hidden')
            report = {'route': 'official_page', 'kind': 'video', 'account_id': account,
                'requested_video_model': video_model,
                'check_only': check_only, 'generation_submitted': False, 'generation_requests': 0,
                'blocked_generation_requests': 0, 'download_verified': False,
                'browser_closed': False, 'stage': 'launch', 'downloaded_count': 0}
            from .work_log import Report
            report = Report(self.service, report)
            context = watcher = spawned = None
            guard_started = False
            self.active_report = report
            self._detail_rows = set()
            try:
                from .browser_runtime import require_playwright_driver, BrowserRuntimeError
                try:
                    require_playwright_driver()
                except BrowserRuntimeError as exc:
                    report['runtime_component'] = exc.component
                    raise OfficialVideoError(exc.code) from None
                await guard.start()
                guard_started = True
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
                                    # Local relay injects proxy credentials; the
                                    # auth prompt would hang on the private desktop.
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
                            context = await pw.chromium.launch_persistent_context(str(profile),
                                executable_path=str(chrome), headless=headless, accept_downloads=True,
                                locale='zh-CN', viewport={'width': 1280, 'height': 900},
                                args=[*guard.launch_args(), '--disable-background-mode'],
                                **({'proxy': proxy} if proxy else {}), timeout=30000)
                        async def gate(route):
                            await self.gate(route, report, check_only)
                        await context.route('**/*', gate)
                        page = await context.new_page()
                        # Chromium otherwise discards some media-detail bodies
                        # across navigation. Keep read evidence before any page
                        # navigation or gallery prefetch; never replay requests.
                        cdp = await context.new_cdp_session(page)
                        await cdp.send('Network.enable', {
                            'maxTotalBufferSize': 64 * 1024 * 1024,
                            'maxResourceBufferSize': 32 * 1024 * 1024,
                            'enableDurableMessages': True})
                        project = urlsplit(url).path.rsplit('/', 1)[-1]
                        report['upload_project_id'] = project
                        async def capture_detail(response):
                            if ('as29s' not in rpc_ids(response.request) or not response.ok
                                    or urlsplit(response.url).netloc != urlsplit(url).netloc):
                                return
                            try:
                                docs = payloads(await asyncio.wait_for(response.text(), 20), 'as29s')
                                for doc in docs:
                                    if (isinstance(doc, list) and len(doc) > 2 and doc[1] == project
                                            and all(isinstance(doc[i], str) and UUID.fullmatch(doc[i]) for i in (0, 2))):
                                        self._detail_rows.add((doc[0], doc[1], doc[2]))
                            except Exception:
                                report['detail_read_errors'] = report.get('detail_read_errors', 0) + 1
                        page.on('response', capture_detail)
                        report['detail_capture'] = 'durable_before_navigation'
                        await guard.confirm_hidden()
                        watcher = asyncio.create_task(self.watch_attention(context, guard, report))
                        page.set_default_timeout(15000)
                        report['stage'] = 'load_page'
                        await page.goto(url, wait_until='domcontentloaded', timeout=45000)
                        await self.wait_for_identity(page, email, guard, report)
                        if _recover is not None:
                            if _recover.get('project_id') and _recover['project_id'] != project:
                                raise OfficialVideoError('recovery_project_mismatch')
                            if _recover.get('inspect_only'):
                                return await self.inspect_unknown_result(page, report)
                            ids = _recover.get('media_ids')
                            if (not isinstance(ids, list) or len(ids) != count
                                    or any(not isinstance(mid, str) or not UUID.fullmatch(mid) for mid in ids)
                                    or len(set(ids)) != count):
                                raise OfficialVideoError('unsupported_video_options')
                            report['parameters'] = {**options, 'model': video_model}
                            report.update(recovered_without_generation=True, response_result_ids=ids)
                            return await self.collect_video_results(page, account, report, url, ids,
                                time.monotonic() + (timeout or self.timeout), {}, recover=True)
                        report['stage'] = 'verify_parameters'
                        report['composer_preferences_pending'] = True
                        try:
                            await self.prepare_video(page, email=email, report=report, guard=guard, **options)
                        finally:
                            report.pop('composer_preferences_pending', None)
                        report['page_ready'] = True
                        if check_only:
                            if _dispatch_probe:
                                report['dispatch_balance'] = await self.service.dispatch_quotes.browser_balance(page,account,email)
                            # Exercise prompt readiness, but never call submit_once.
                            if prompt:
                                button = await self.ready_to_submit(page, prompt, email, report)
                                await button.click(trial=True, timeout=5000)
                                report['submit_control_actionable'] = True
                                await page.get_by_role('textbox', name='您希望创作什么？', exact=True).fill('')
                            report['stage'] = 'check_completed'
                            return report
                        from .uncertain_result import observe_uncertain
                        async def late_video_response(response):
                            ids = rpc_ids(response.request) & VIDEO_RPCS
                            if ids and urlsplit(response.url).netloc == urlsplit(url).netloc:
                                try:
                                    rpc = next(iter(ids))
                                    text = await response.text()
                                    docs = payloads(text, rpc)
                                    report['generation_response'] = {'http_status': response.status, 'response_origin': origin_only(response.url), **video_response_summary(text, rpc, docs)}
                                    report['generation_response_http_status'] = response.status
                                    requested = video_result_ids(docs, project)
                                    report['response_id_kind'] = 'scene' if any(isinstance(d, list) and len(d) > 2 and d[2] for d in docs) else 'media'
                                    if len(requested) == count and len(set(requested)) == count:
                                        report['response_result_ids'] = requested
                                except Exception:
                                    pass
                        async def recover_original(deadline):
                            requested = report.get('response_result_ids')
                            if not requested or len(requested) != count or report.get('downloaded_count', 0):
                                await page.locator('video').count()
                                return None
                            return await self.collect_video_results(page, account, report, url,
                                requested, deadline, {}, recover=True)
                        page.on('response', late_video_response)
                        try:
                            try:
                                return await self.submit_video_once(page, prompt, account, report, email,
                                    timeout or self.timeout, download, url, count)
                            except Exception as exc:
                                from .work_log import emit
                                from .task_scheduler import CURRENT_JOB
                                emit(self.service, CURRENT_JOB.get(), '生成或结果读取异常', report, exc)
                                if not report['generation_submitted'] or getattr(exc, 'code', None) == 'provider_rejected':
                                    raise
                                report['original_error_code'] = getattr(exc, 'code', type(exc).__name__)
                                return await observe_uncertain(report, recover_original)
                        finally:
                            page.remove_listener('response', late_video_response)
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
                            if 'page' in locals() and 'capture_detail' in locals():
                                page.remove_listener('response', capture_detail)
                            try:
                                await asyncio.wait_for(context.close(), 15)
                            except Exception:
                                report['cleanup_warning'] = True
                            finally:
                                if spawned is not None:
                                    spawned.terminate()  # CDP detach leaves Chrome alive.
            except asyncio.CancelledError:
                report['error_code'] = 'canceled'
                raise
            except OfficialImageError as exc:
                report['error_code'] = exc.code
                error = OfficialVideoError(exc.code, submitted=report['generation_submitted'])
                error.execution = report
                raise error from None
            except Exception as exc:
                code = 'visibility_failed' if isinstance(exc, BrowserVisibilityError) else 'video_page_failed'
                report['error_code'] = code
                report['exception_type'] = type(exc).__name__
                error = OfficialVideoError(code, submitted=report['generation_submitted'])
                error.execution = report
                raise error from None
            finally:
                try:
                    report['browser_closed'] = await guard.close_owned() if guard_started else False
                except Exception:
                    report['browser_closed'] = False
                report['visibility'] = guard.info()
                self.active_report = None
                self.last_report = dict(report)
                try:
                    with self.service.connect() as db:
                        db.execute('insert or replace into console_meta values (?, ?)',
                                   ('last_official_video_execution', json.dumps(report)))
                except Exception:
                    pass

    async def gate(self, route, report, check_only):
        request = route.request
        ids = rpc_ids(request)
        is_submission = bool(ids & GENERATION_RPCS) or bool(re.search(
            r'batchGenerate|generateImages|generateVideo|upsample', urlsplit(request.url).path, re.I))
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if is_submission:
                if (check_only or report.get('query_only') or not report['generation_submitted'] or report['generation_requests'] >= 1
                        or len(ids) != 1 or not ids <= VIDEO_RPCS):
                    report['blocked_generation_requests'] += 1
                    await route.abort()
                    return
                report['generation_requests'] += 1
            elif urlsplit(request.url).hostname in ('flow.google.com', 'labs.google', 'aisandbox-pa.googleapis.com'):
                # Unknown Flow write RPCs fail closed. Only normal reads and the
                # explicit file chooser's upload RPC may leave this page.
                rights_confirmation = ids == {'JJH6Ub'} and report.get('upload_rights_confirmation_pending', False)
                if rights_confirmation:
                    # Observed only from the normal ownership-dialog confirmation.
                    # Permit one request per visible dialog, never replay it.
                    report['upload_rights_confirmation_pending'] = False
                    report['rights_confirmation_requests'] = report.get('rights_confirmation_requests', 0) + 1
                # Observed only from the ordinary close-agent / composer-mode
                # controls. Permit these preferences during parameter preparation
                # only; generation RPCs above remain blocked in check-only mode.
                composer_preferences = (report.get('composer_preferences_pending', False)
                    and bool(ids & COMPOSER_PREFERENCE_RPCS)
                    and ids <= (READ_RPCS | COMPOSER_PREFERENCE_RPCS))
                if composer_preferences:
                    report['composer_preference_requests'] = report.get('composer_preference_requests', 0) + 1
                if not rights_confirmation and not composer_preferences and (not ids or not ids <= (READ_RPCS | {'maseQ'})):
                    report['blocked_write_rpc_names'] = sorted(set(report.get('blocked_write_rpc_names', [])) |
                        {rpc for rpc in ids if re.fullmatch(r'[A-Za-z0-9]{1,20}', rpc)})
                    await route.abort()
                    return
        await route.continue_()

    async def select_video_model(self, page, video_model, report):
        label = VIDEO_MODELS[video_model]
        body = r'\s*[-–—]?\s*'.join(map(re.escape, label.split()))
        pattern = re.compile(r'^(?:volume_up\s*)?' + body + r'(?:\s*arrow_drop_down)?$', re.I)
        button = page.get_by_role('button', name='选择模型系列', exact=True)
        if not pattern.fullmatch((await button.inner_text()).strip()):
            await button.click()
            item = page.get_by_role('menuitem', name=pattern)
            if await item.count() != 1 or not await item.is_visible():
                raise OfficialVideoError('video_model_unavailable')
            await item.click()
        # inner_text is the rendered model label; text_content also contains
        # accessibility-only icon nodes that differ between Flow revisions.
        deadline = time.monotonic() + 5
        actual = ''
        while time.monotonic() < deadline:
            actual = (await button.inner_text()).strip()
            if pattern.fullmatch(actual):
                break
            await asyncio.sleep(.1)
        else:
            report['observed_video_model'] = actual
            raise OfficialVideoError('video_model_not_selected')
        report['verified_video_model'] = video_model
        return actual

    async def prepare_video(self, page, *, email, report, aspect, duration, quality,
                            count, start_image=None, end_image=None, references=(), guard=None,
                            video_model='omni_flash'):
        await self.ensure_direct_composer(page)
        textbox = page.get_by_role('textbox', name='您希望创作什么？', exact=True).filter(visible=True)
        await textbox.wait_for()
        await humanize.pause('read')
        await humanize.wander(page)
        clear = page.get_by_role('button', name='清除提示', exact=True)
        if await clear.count() and await clear.is_visible():
            await clear.click()
        await textbox.fill('')
        report['parameter_step'] = 'open_composer_settings'
        trigger = await self.composer_settings(page)
        await trigger.click()
        await self.ui_buffer()
        report['parameter_step'] = 'select_video_mode'
        from playwright.async_api import expect
        video_kind = page.get_by_role('radio', name='视频', exact=True).filter(visible=True)
        # Angular updates aria-checked asynchronously after a normal click.
        # check() checks immediately and can fail although the switch succeeded.
        await video_kind.click()
        await expect(video_kind).to_be_checked(timeout=5000)
        await self.ui_buffer(transition=True)
        report['parameter_step'] = 'select_input_mode'
        mode = '帧' if start_image else '素材'
        input_mode = page.get_by_role('radio', name=mode, exact=True).filter(visible=True)
        await input_mode.click()
        await expect(input_mode).to_be_checked(timeout=5000)
        await self.ui_buffer(transition=True)
        report['parameter_step'] = 'select_video_model'
        page_model = await self.select_video_model(page, video_model, report)
        legacy_references = bool(references) and video_model == 'omni_flash'
        selected = [aspect] if not legacy_references else []
        if video_model == 'omni_flash':
            selected.append(f'{duration[:-1]} 秒')
        selected.append(f'x{count}')
        for name in selected:
            report['parameter_step'] = 'select_' + name
            await page.get_by_role('radio', name=name, exact=True).filter(visible=True).click()
            await expect(page.get_by_role('radio', name=name, exact=True).filter(visible=True)).to_be_checked(timeout=5000)
            await self.ui_buffer()
        if legacy_references:
            # Existing reference-mode API fields are placeholders. Preserve
            # their semantics; read the page's effective ratio and resolution.
            aspect = None
            for name in ('16:9', '9:16'):
                if await page.get_by_role('radio', name=name, exact=True).is_checked():
                    aspect = name
            for name in ('360p', '720p'):
                if await page.get_by_role('radio', name=re.compile('^'+name+r'(?:\s|$)')).is_checked():
                    quality = name
            if aspect is None:
                raise OfficialVideoError('video_parameters_unconfirmed')
        quality_radio = page.get_by_role('radio', name=re.compile('^' + re.escape(quality) + r'(?:\s|$)')).filter(visible=True)
        if not legacy_references and video_model == 'omni_flash':
            report['parameter_step'] = 'select_quality'
            await quality_radio.click()
        from playwright.async_api import expect
        for name in ['视频', mode, *selected]:
            try:
                await expect(page.get_by_role('radio', name=name, exact=True).filter(visible=True)).to_be_checked(timeout=5000)
            except AssertionError:
                report['unconfirmed_parameter'] = name
                raise OfficialVideoError('video_parameters_unconfirmed') from None
        if video_model == 'omni_flash':
            try:
                await expect(quality_radio).to_be_checked(timeout=5000)
            except AssertionError:
                report['unconfirmed_parameter'] = quality
                raise OfficialVideoError('video_parameters_unconfirmed') from None
        await self.dismiss_settings(page)
        await self.ui_buffer(transition=True)
        if video_model != 'omni_flash':
            try:
                await expect(trigger).to_contain_text('720p', timeout=5000)
                await expect(trigger).to_contain_text(re.compile(r'8\s*秒'), timeout=5000)
            except AssertionError:
                report['unconfirmed_parameter'] = 'Veo 8s / 720p'
                raise OfficialVideoError('video_parameters_unconfirmed') from None
        if start_image:
            await self.attach_file(page, start_image, '开始', report=report, guard=guard)
            if end_image:
                await self.attach_file(page, end_image, '结束', report=report, guard=guard)
        for reference in references:
            await self.attach_file(page, reference, '在提示框中添加素材', report=report, guard=guard)
        report['parameter_step'] = 'confirmed'
        report['parameters'] = {'model': video_model, 'page_model': page_model,
            'aspect': aspect, 'duration': duration, 'quality': quality, 'count': count,
            'mode': 'frames' if start_image else 'references' if references else 'text',
            'reference_count': len(references), 'start_frame': bool(start_image), 'end_frame': bool(end_image)}

    @staticmethod
    async def ui_buffer(*, transition=False):
        """Small rendering buffer after readiness checks; never replaces them."""
        await humanize.pause('transition' if transition else 'action')

    async def dismiss_settings(self, page):
        # Toggle the same visible control used to open the official popover.
        # Do not click an overlapping title/footer or force events through it.
        await (await self.composer_settings(page)).click()

    async def attach_file(self, page, path, button_name, *, report, guard=None):
        path = Path(path)
        if not path.is_file():
            raise OfficialVideoError('video_input_missing')
        report['upload_slot'] = {'开始': 'start_frame', '结束': 'end_frame'}.get(button_name, 'reference')
        report['stage'] = 'open_upload_picker'
        before_count = await page.get_by_role('button', name=re.compile(r'^(?:图片素材|素材)$')).count()
        await page.get_by_role('button', name=button_name, exact=True).click()
        await page.get_by_role('listbox', name='资源列表', exact=True).wait_for(state='visible', timeout=30000)
        await self.ui_buffer()
        known = self.service.known_uploaded_image(report['account_id'], report['upload_project_id'], path)
        if known:
            existing = page.get_by_role('option', name=re.compile(re.escape(known)))
            try:
                await existing.first.wait_for(state='visible', timeout=5000)
            except Exception:
                pass  # A different project library may not contain the cached asset.
            count = await existing.count()
            immutable_upload = Path(path).resolve().parent == self.service.upload_dir.resolve() and bool(
                re.fullmatch(r'[0-9a-f]{32}\.(?:png|jpg|jpeg|webp)', known))
            if count == 1 or (count > 1 and immutable_upload):
                choice = await self.wait_uploaded_choice(page, existing, report, guard,
                    allow_identical=immutable_upload)
                await self.finish_attachment(page, choice, before_count)
                report['reused_uploaded_images'] = report.get('reused_uploaded_images', 0) + 1
                report['stage'] = 'verify_parameters'
                return
        async with page.expect_file_chooser(timeout=15000) as pending:
            await page.get_by_role('button', name='上传媒体内容', exact=True).click()
        report['stage'] = 'upload_file'
        await (await pending.value).set_files(str(path))
        option = page.get_by_role('option', name=re.compile(re.escape(path.name)))
        option = await self.wait_uploaded_choice(page, option, report, guard,
            authorized=self.service.media_rights_authorized(path), before_count=before_count)
        report['stage'] = 'attach_uploaded_file'
        await self.ui_buffer(transition=True)
        await self.finish_attachment(page, option, before_count)
        self.service.remember_uploaded_image(report['account_id'], report['upload_project_id'], path)
        report['stage'] = 'verify_parameters'

    @staticmethod
    async def finish_attachment(page, option, before_count):
        """Accept either explicit Add or the site's automatic picker dismissal."""
        if option is not None:
            await option.click()
        picker = page.get_by_role('listbox', name='资源列表', exact=True)
        thumbnails = page.get_by_role('button', name=re.compile(r'^(?:图片素材|素材)$'))
        add = page.get_by_role('button', name='添加到提示', exact=True)
        if await picker.is_visible():
            try:
                await add.click(timeout=3000)
            except Exception:
                # Google can attach and detach this button while click waits.
                # Verify the resulting composer instead of repeating the click.
                pass
        try:
            await picker.wait_for(state='hidden', timeout=10000)
            from playwright.async_api import expect
            await expect(thumbnails).to_have_count(before_count + 1, timeout=10000)
            deadline = time.monotonic() + 30
            while not await thumbnails.last.locator('img').evaluate_all(
                    'els => els.length > 0 && els.every(i => i.complete && i.naturalWidth > 0)'):
                if time.monotonic() >= deadline:
                    raise OfficialVideoError('video_input_unconfirmed')
                await asyncio.sleep(.2)
            await OfficialVideos.ui_buffer(transition=True)
        except Exception:
            raise OfficialVideoError('video_input_unconfirmed') from None

    async def wait_uploaded_choice(self, page, option, report, guard, *, authorized=False, allow_identical=False, before_count=None):
        """Confirm rights only for exact images explicitly authorized by the owner."""
        rights = page.get_by_role('dialog', name=re.compile(r'此图片的使用权|Rights to this image', re.I))
        deadline = time.monotonic() + self.upload_timeout
        manual_started = None
        auto_clicked = False
        while True:
            if guard and guard.error:
                raise BrowserVisibilityError(guard.error)
            visible = await rights.count() > 0 and await rights.is_visible()
            if visible:
                if authorized:
                    if auto_clicked:
                        raise OfficialVideoError('video_upload_rights_confirmation_failed')
                    agree = rights.get_by_role('button', name=re.compile(r'^(?:我同意|I agree)$', re.I))
                    if await agree.count() != 1 or not await agree.is_enabled():
                        raise OfficialVideoError('video_upload_rights_confirmation_failed')
                    auto_clicked = True  # Never replay an uncertain confirmation click.
                    report['stage'] = 'confirm_authorized_upload_rights'
                    report['upload_rights_confirmation_pending'] = True
                    try:
                        await agree.click(timeout=5000)
                        await rights.wait_for(state='hidden', timeout=10000)
                    except Exception:
                        raise OfficialVideoError('video_upload_rights_confirmation_failed') from None
                    report['upload_rights_auto_confirmations'] = report.get('upload_rights_auto_confirmations', 0) + 1
                    deadline = time.monotonic() + self.upload_timeout
                    continue
                if manual_started is None:
                    manual_started = time.monotonic()
                    report['upload_rights_confirmation_required'] = True
                    report['upload_rights_confirmation_pending'] = True
                    if guard:
                        guard.reveal('media_rights_confirmation')
                report['stage'] = 'waiting_for_upload_rights'
                report['attention_message'] = '请在本次浏览器确认图片使用权；程序不会代替您同意。'
                if time.monotonic() - manual_started >= self.manual_timeout:
                    raise OfficialVideoError('video_upload_rights_required')
            else:
                if manual_started is not None:
                    report['upload_rights_dialog_dismissed'] = True
                    report.pop('attention_message', None)
                    manual_started = None
                    deadline = time.monotonic() + self.upload_timeout
                    if guard:
                        guard.hide_after_verification()
                        await guard.confirm_hidden()
                report['stage'] = 'wait_uploaded_file'
                # Reference uploads can mount directly and dismiss the picker.
                # Confirm the composer change instead of waiting for a removed option.
                if before_count is not None:
                    picker = page.get_by_role('listbox', name='资源列表', exact=True)
                    thumbnails = page.get_by_role('button', name=re.compile(r'^(?:图片素材|素材)$'))
                    if (not await picker.is_visible()
                            and await thumbnails.count() == before_count + 1
                            and await thumbnails.last.is_visible()
                            and await thumbnails.last.locator('img').evaluate_all(
                                'els => els.length > 0 && els.every(i => i.complete && i.naturalWidth > 0)')):
                        return None
                # Flow keeps the uploading placeholder beside the existing asset.
                # Presence (or duplicate filename) alone does not mean readiness.
                states = await option.evaluate_all("""els => els.map(e => ({
                    busy: /正在上传|Uploading/i.test(e.innerText) ||
                        e.getAttribute('aria-busy') === 'true' ||
                        e.getAttribute('aria-disabled') === 'true',
                    visible: e.getBoundingClientRect().width > 0,
                    loaded: [...e.querySelectorAll('img')].every(i => i.complete && i.naturalWidth > 0)
                }))""")
                if states and not any(s['busy'] for s in states):
                    ready = [i for i, s in enumerate(states) if s['visible'] and s['loaded']]
                    if len(ready) == 1 or (ready and allow_identical):
                        return option.nth(ready[0])
                if time.monotonic() >= deadline:
                    raise OfficialVideoError('video_input_unconfirmed')
            if guard:
                report['visibility'] = guard.info()
            await asyncio.sleep(.2)

    @staticmethod
    async def gallery(page):
        items = []
        for group in await page.get_by_role('group').all():
            sources = await group.locator('img, video').evaluate_all('els=>els.flatMap(e=>[e.currentSrc,e.src,e.poster]).filter(Boolean)')
            if sources:
                items.append((tuple(sorted(set(sources))), group))
        return items

    async def submit_video_once(self, page, prompt, account, report, email, timeout,
                                download, project_url, count):
        before = {sources for sources, _ in await self.gallery(page)}
        button = await self.ready_to_submit(page, prompt, email, report)
        if getattr(self.service,'dispatch_quotes',None) is not None:
            await self.service.dispatch_quotes.before_provider_click(page,account,email)
        started = time.monotonic()
        report['stage'] = 'generate'
        # One longer human pause before the only paid click; the click itself
        # stays single-shot and is never retried.
        await humanize.pause('pre_submit')
        report['generation_submitted'] = True  # No replay after any uncertainty.
        async with page.expect_response(lambda response: bool(rpc_ids(response.request) & VIDEO_RPCS) and urlsplit(response.url).netloc == urlsplit(project_url).netloc and urlsplit(response.url).scheme == urlsplit(project_url).scheme,
                                        timeout=max(15, timeout) * 1000) as pending:
            await button.click(timeout=10000)
        response = await pending.value
        report['generation_response_http_status'] = response.status
        ids = rpc_ids(response.request)
        rpc = next(iter(ids & VIDEO_RPCS))
        text = await response.text()
        docs = payloads(text, rpc)
        report['generation_response'] = {'http_status': response.status, 'response_origin': origin_only(response.url), **video_response_summary(text, rpc, docs)}
        if not response.ok:
            raise OfficialVideoError('provider_rejected', submitted=True)
        project = urlsplit(project_url).path.rsplit('/', 1)[-1]
        requested = video_result_ids(docs, project)
        report['response_id_kind'] = 'scene' if any(isinstance(d, list) and len(d) > 2 and d[2] for d in docs) else 'media'
        report['response_result_ids'] = requested
        report['expected_result_count'] = count
        report['observed_result_count'] = len(requested)
        if report['generation_response']['rpc_error_codes']:
            raise OfficialVideoError('video_rpc_error', submitted=True)
        if not requested:
            raise OfficialVideoError('video_response_unrecognized', submitted=True)
        if len(requested) != count:
            raise OfficialVideoError('video_result_count_mismatch', submitted=True)
        report['response_result_ids'] = requested
        if not download:
            report['stage'] = 'submitted'
            return {'job_id': uuid.uuid4().hex, 'status': 'submitted', 'account_id': account,
                    'media_ids': requested, 'files': [], 'execution': report}
        deadline = started + timeout
        return await self.collect_video_results(page, account, report, project_url, requested, deadline, before, started=started)

    async def collect_video_results(self, page, account, report, project_url, requested, deadline, before, *, recover=False, started=None):
        project = urlsplit(project_url).path.rsplit('/', 1)[-1]
        started = time.monotonic() if started is None else started
        remaining, files_by_id, verified_by_id = set(requested), {}, {}
        while remaining:
            if recover:
                # IDs originate from the original official generation response.
                # This is a normal read-only navigation, with the generation gate
                # permanently closed by check_only=True.
                target = next(mid for mid in requested if mid in remaining)
                report['stage'] = 'verify_result_identity'
                async def open_detail():
                    await page.goto(project_url + '/edit/' + target, wait_until='domcontentloaded', timeout=45000)
                binding = await self.verify_video_detail(page, {target}, project, open_detail, deadline)
                candidate = None
            else:
                candidate = await self.wait_video_candidate(page, remaining, before, report, deadline)
                report['stage'] = 'verify_result_identity'
                # Gallery order can change when the user submits another clip.
                # A new thumbnail is readiness evidence, never task identity.
                target = next(mid for mid in requested if mid in remaining)
                async def open_requested_detail():
                    await page.goto(project_url + '/edit/' + target, wait_until='domcontentloaded', timeout=45000)
                try:
                    if report.get('response_id_kind') == 'media':
                        raise OfficialVideoError('video_result_identity_mismatch', submitted=True)
                    binding = await self.verify_video_detail(page, {target}, project, open_requested_detail,
                                                             min(deadline, time.monotonic() + 5))
                except OfficialVideoError as error:
                    if error.code != 'video_result_identity_mismatch' or time.monotonic() >= deadline:
                        raise
                    # Media IDs and editor IDs are distinct in some official
                    # response variants. Resolve through the page, but still
                    # require the exact submitted ID and project binding.
                    await page.goto(project_url, wait_until='domcontentloaded', timeout=45000)
                    # A wrong readiness candidate is marked tried and never
                    # re-selected; each attempt gets its own short verify cap,
                    # then relocates to the next new item until the deadline —
                    # it never parks on one page for the whole window.
                    while True:
                        candidate = await self.wait_video_candidate(page, {target}, before, report, deadline)
                        before.add(candidate[0])
                        async def open_candidate_detail():
                            await candidate[1].dblclick()
                        try:
                            verify_deadline = min(deadline, time.monotonic() + self.candidate_verify_wait)
                            binding = await self.verify_video_detail(page, {target}, project, open_candidate_detail, verify_deadline)
                            break
                        except OfficialVideoError as retry_error:
                            if retry_error.code != 'video_result_identity_mismatch' or time.monotonic() >= deadline:
                                raise
                            await page.goto(project_url, wait_until='domcontentloaded', timeout=45000)
            report['last_verified_binding'] = {'generated_id': binding[0],
                'content_id': binding[1], 'detail_id': binding[2], 'project_id': project}
            report['stage'] = 'download_video'
            path, metadata = await self.download_video(page, report['parameters']['quality'])
            files_by_id[binding[0]] = {'path': str(path)}
            verified_by_id[binding[0]] = {'generated_id': binding[0], 'content_id': binding[1], 'detail_id': binding[2],
                                        'duration_seconds': metadata['duration_seconds'], 'bytes': metadata['size']}
            if candidate:
                before.add(candidate[0])
            remaining.remove(binding[0])
            report.update(downloaded_count=len(files_by_id),
                verified_results=[verified_by_id[mid] for mid in requested if mid in verified_by_id],
                downloaded_files=[Path(files_by_id[mid]['path']).name for mid in requested if mid in files_by_id])
            if remaining and not recover:
                await page.goto(project_url, wait_until='domcontentloaded', timeout=45000)
        report.update(stage='completed', result_identity_verified=True, download_verified=True)
        return {'job_id': uuid.uuid4().hex, 'status': 'succeeded', 'account_id': account,
                'media_ids': requested, 'files': [files_by_id[mid] for mid in requested],
                'elapsed_seconds': round(time.monotonic() - started, 2), 'execution': report}

    async def wait_video_candidate(self, page, remaining, before, report, deadline):
        report['stage'] = 'wait_video'
        project = report.get('upload_project_id')
        candidate = None
        while time.monotonic() < deadline:
            # The candidate is readiness evidence only: identity is enforced
            # afterwards through the project-scoped detail binding. Prefer a
            # gallery item carrying one of this job's IDs — gallery sources can
            # carry the content ID while `remaining` holds editor IDs, so accept
            # either via the captured content->project->editor bindings. A new
            # item without any of them may still be this run's clip waiting for
            # its binding, so it may serve as evidence; claiming is impossible
            # without the exact detail match.
            bindings = video_detail_bindings(
                [list(row) for row in self._detail_rows], set(remaining), project) if project else set()
            accepted = set(remaining) | {row[1] for row in bindings}
            gallery_items = await self.gallery(page)
            matched = [(sources, group) for sources, group in gallery_items
                       if any(mid in ' '.join(sources) for mid in accepted)]
            if matched:
                candidate = matched[0]
                break
            new = [(sources, group) for sources, group in gallery_items if sources not in before]
            if new:
                candidate = new[0]
                break
            if await page.get_by_role('alert').filter(has_text=re.compile('无法生成|异常活动|生成失败|unusual activity', re.I)).count():
                raise OfficialVideoError('provider_rejected', submitted=True)
            await asyncio.sleep(.5)
        if not candidate:
            raise OfficialVideoError('video_result_timeout', submitted=True)
        return candidate

    async def verify_video_detail(self, page, remaining, project, open_detail, deadline):
        await open_detail()
        while time.monotonic() < deadline:
            if page.is_closed():
                raise OfficialVideoError('video_result_identity_mismatch', submitted=True)
            match = re.fullmatch(r'/project/([^/]+)/edit/([a-fA-F0-9-]{36})/?', urlsplit(page.url).path)
            if match and match.group(1) != project:
                raise OfficialVideoError('video_result_identity_mismatch', submitted=True)
            bindings = video_detail_bindings([list(row) for row in self._detail_rows], remaining, project)
            rows = [row for row in bindings if match and row[2] == match.group(2)]
            if len(rows) == 1 and await page.get_by_role('button', name='下载媒体内容', exact=True).is_visible():
                return rows[0]
            if match and bindings and match.group(2) not in {row[2] for row in bindings}:
                # The editor ID of this detail page is definitively bound to a
                # different clip. Failing now allows the caller to relocate
                # instead of waiting out the deadline on the wrong video.
                raise OfficialVideoError('video_result_identity_mismatch', submitted=True)
            await asyncio.sleep(.2)
        raise OfficialVideoError('video_result_identity_mismatch', submitted=True)

    async def download_video(self, page, quality):
        await page.get_by_role('button', name='下载媒体内容', exact=True).click()
        # Only original/non-upscale downloads. Never click 1080p/4K/upscale,
        # which can create another billable operation.
        original = page.get_by_role('menuitem', name=re.compile(
            r'^(?:' + re.escape(quality) + r'(?:\s*[（(]?原始[^）)]*[）)]?)?|原始(?:视频|尺寸|分辨率)?(?:\s*' + re.escape(quality) + r')?)$', re.I))
        try:
            await original.first.wait_for(state='visible', timeout=15000)
        except Exception:
            raise OfficialVideoError('video_download_control_unconfirmed', submitted=True) from None
        if await original.count() != 1:
            raise OfficialVideoError('video_download_control_unconfirmed', submitted=True)
        async with page.expect_download(timeout=60000) as pending:
            await original.click()
        item = await pending.value
        temporary = await item.path()
        if not temporary or await item.failure():
            raise OfficialVideoError('video_download_failed', submitted=True)
        try:
            # The 10-second limit is for uploaded reference clips, not official
            # output containers (whose audio/video tracks may end later).
            metadata = await asyncio.to_thread(inspect_mp4, temporary, max_duration_seconds=None)
            if metadata['size'] > 500 * 1024 * 1024:
                raise ValueError()
        except (ValueError, OSError):
            raise OfficialVideoError('video_download_invalid', submitted=True) from None
        output = self.service.engine.output_dir
        output.mkdir(parents=True, exist_ok=True)
        path = output / ('flow-video-' + uuid.uuid4().hex + '.mp4')
        with Path(temporary).open('rb') as source, path.open('xb') as destination:
            await asyncio.to_thread(shutil.copyfileobj, source, destination)
        return path, metadata

