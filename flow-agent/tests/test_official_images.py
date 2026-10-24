"""Local real-browser regression. Never contacts a generation provider.

Set FLOW_TEST_BROWSER to an existing Chrome for Testing executable to include
browser tests. Every profile, database, HTTP endpoint and image is a test fixture.
"""
import asyncio
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from flow_agent.console import ConsoleService, create_console_router, safe_failure
from flow_agent.engine import FlowEngine
from flow_agent.official_images import OfficialImageError, image_suffix, result_ids, profile_in_use
from flow_agent import api

NEW_ID = '11111111-1111-4111-8111-111111111111'
OLD_ID = '22222222-2222-4222-8222-222222222222'
MEDIA_ID = '33333333-3333-4333-8333-333333333333'
HTML = '''<!doctype html><html lang="zh-CN"><body><script>window.WIZ_global_data={SNlM0e:"fresh-at",FdrFJe:"fresh-fsid"};</script>
<button aria-label="Google 账号：fixture (fixture@example.test)">账号</button>
<div role="group" aria-label="old"><img src="/image/OLD_ID.png"></div>
<div role="textbox" aria-label="您希望创作什么？" contenteditable="true"></div>
<button aria-label="设置触发器" onclick="document.querySelector('#settings').hidden=false">设置</button>
<section id="settings" hidden>
<input type="radio" aria-label="图片" name="kind" checked>
<input type="radio" aria-label="视频" name="kind">
<input type="radio" aria-label="16:9" name="aspect" checked>
<input type="radio" aria-label="9:16" name="aspect">
<input type="radio" aria-label="1:1" name="aspect">
<input type="radio" aria-label="4:3" name="aspect">
<input type="radio" aria-label="3:4" name="aspect">
<input type="radio" aria-label="x1" name="count">
<input type="radio" aria-label="x2" name="count" checked>
<button aria-label="选择模型系列">Nano Banana 2.1</button>
</section>
<button aria-label="开始生成" disabled>开始</button>
<button aria-label="下载媒体内容" hidden onclick="document.querySelector('#original').hidden=false">下载</button>
<a id="original" role="menuitem" hidden href="/download" download="fixture.png">1K 原始尺寸</a>
<script>
const mode='MODE';
if(mode==='recover') document.querySelector('[role=group] img').ondblclick=()=>{fetch('/data/batchexecute?rpcids=as29s',{method:'POST',body:'detail'});history.pushState({},'','/project/fixture/edit/NEW_ID');document.querySelector('[aria-label="下载媒体内容"]').hidden=false;};
const textbox=document.querySelector('[contenteditable]');
const submit=document.querySelector('[aria-label="开始生成"]');
textbox.addEventListener('input',()=>{
 if(mode==='disabled') return;
 if(mode==='delayed') {setTimeout(()=>submit.disabled=!textbox.innerText,700);return;}
 textbox.innerText && (submit.disabled=false);
});
submit.addEventListener('click',async()=>{
 const response=await fetch('/data/batchexecute?rpcids=ogiZ0b',{method:'POST',body:textbox.innerText});
 if(mode==='duplicate') fetch('/data/batchexecute?rpcids=ogiZ0b',{method:'POST',body:textbox.innerText}).catch(()=>{});
 if(!response.ok || mode==='timeout' || mode==='empty') return;
 const card=document.createElement('div');card.role='group';card.ariaLabel='new';
 const img=document.createElement('img');
 const bytes=await (await fetch('/image/NEW_ID.png')).blob(); img.src=URL.createObjectURL(bytes);
 img.ondblclick=()=>{fetch('/data/batchexecute?rpcids=as29s',{method:'POST',body:'detail'});history.pushState({},'', '/project/fixture/edit/'+(mode==='wrong_result'?'OLD_ID':'NEW_ID'));document.querySelector('[aria-label="下载媒体内容"]').hidden=false;};
 card.append(img);document.body.prepend(card);
});
</script></body></html>'''


@pytest.fixture
def local_flow():
    stream = BytesIO()
    Image.new('RGB', (64, 36), '#5783ba').save(stream, format='PNG')
    image = stream.getvalue()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path.startswith('/image/') or self.path == '/download':
                data = b'not-an-image' if self.path == '/download' and self.server.mode == 'invalid' else image
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                if self.path == '/download':
                    self.send_header('Content-Disposition', 'attachment; filename="fixture.png"')
            elif self.server.mode.startswith('login') and self.path != '/ready':
                data = b'<html><title>Sign in - Google Accounts</title><input type="email"><button onclick="location.href=\'/ready\'">Continue local sign-in</button></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
            else:
                html = HTML.replace('MODE', self.server.mode).replace('NEW_ID', NEW_ID).replace('OLD_ID', OLD_ID)
                if self.server.mode == 'recover' and '/edit/' in self.path:
                    html=html.replace('aria-label="下载媒体内容" hidden','aria-label="下载媒体内容"')
                    html=html.replace('<script>', '<script>fetch("/data/batchexecute?rpcids=as29s",{method:"POST",body:"detail"});')
                data=html.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if 'as29s' in self.path:
                data=json.dumps([['wrb.fr','as29s',json.dumps([MEDIA_ID,'fixture',NEW_ID])]]).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            self.server.submissions += 1
            payload = [None, 100, [] if self.server.mode == 'empty' else [[MEDIA_ID]]]
            data = json.dumps([['wrb.fr', 'ogiZ0b', json.dumps(payload)]]).encode()
            self.send_response(403 if self.server.mode == 'rejected' else 200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.mode = 'success'
    server.submissions = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, image
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def local_service(tmp_path, monkeypatch, local_flow):
    browser = os.environ.get('FLOW_TEST_BROWSER')
    if not browser or not Path(browser).is_file():
        pytest.skip('Set FLOW_TEST_BROWSER for local real-browser integration tests')
    service = ConsoleService(FlowEngine(root=tmp_path/'flow-agent'))
    service.initialize()
    service.cookies_dir.mkdir()
    # Legacy preferences: headless checks and the hidden generation window. The
    # new visible defaults are covered in test_humanize.py.
    with service.connect() as db:
        db.execute("insert or replace into console_meta values ('verification_headless', 'true')")
        db.execute("insert or replace into console_meta values ('generation_browser_visible', 'false')")
    cookie = service.cookies_dir/'account_fixture.json'
    cookie.write_text(json.dumps({'cookies':[{'name':'test','value':'fixture'}],
                                 'at':'fixture','fsid':'fixture','fingerprint':{'userAgent':'fixture'}}))
    profile = tmp_path/'profile'
    profile.mkdir()
    server, _ = local_flow
    monkeypatch.setattr(service.official_images, 'settings', lambda _: (
        profile, Path(browser), 'fixture@example.test', f'http://127.0.0.1:{server.server_port}/project/fixture'))
    return service, str(cookie), profile


def test_image_decode_rejects_truncated_and_error_pages():
    stream=BytesIO()
    Image.new('RGB',(64,36)).save(stream,format='PNG')
    data=stream.getvalue()
    assert image_suffix(data)=='.png'
    for invalid in (b'<html>error</html>', data[:-12], b'\xff\xd8\xff'+b'x'*100+b'\xff\xd9'):
        with pytest.raises(OfficialImageError):
            image_suffix(invalid)


def test_result_correlation_excludes_empty_and_arbitrary_strings():
    assert result_ids([[None, 100, [[NEW_ID]]]]) == {NEW_ID}
    assert result_ids([[None, 100, []]]) == set()
    assert result_ids([['ignored', OLD_ID]]) == set()
    assert result_ids([[None, 100, [[MEDIA_ID, OLD_ID, NEW_ID]]]]) == {MEDIA_ID, NEW_ID}
    assert OLD_ID not in result_ids([[None, 100, [[MEDIA_ID, OLD_ID, NEW_ID]]]])


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code', [('success',None),('duplicate',None),('rejected','provider_rejected'),
                                      ('empty','provider_empty'),('timeout','result_timeout'),('invalid','download_invalid'),('wrong_result','result_identity_mismatch')])
async def test_local_browser_outcomes_close_and_never_replay(local_service,local_flow,mode,code):
    service,cookie,profile=local_service
    server,image=local_flow
    server.mode=mode
    if code:
        with pytest.raises(OfficialImageError) as exc:
            await service.official_images.run(cookies=cookie,prompt='local fixture',timeout=2 if mode=='timeout' else 8)
        assert exc.value.code == (code if code == 'provider_rejected' else 'original_result_unknown')
        if code != 'provider_rejected':
            assert service.official_images.last_report['original_error_code'] == code
            assert exc.value.execution['outcome'] == 'pending_review'
            assert exc.value.execution['browser_closed']
        assert exc.value.submitted
        assert not list(service.engine.output_dir.glob('*'))
    else:
        result=await service.engine.generate_image(cookies=cookie,prompt='local fixture',model='beluga',timeout=8)
        assert result['status']=='succeeded'
        assert Path(result['files'][0]['path']).read_bytes()==image
        assert result['execution']['download_verified']
        assert result['execution']['browser_closed']
    assert server.submissions==1
    assert service.official_images.last_report['generation_requests']==1
    assert service.official_images.last_report['browser_closed']
    assert service.official_images.last_report['visibility']['hidden_confirmed']
    assert service.official_images.last_report['visibility']['visible_owned_windows']==0
    assert not profile_in_use(profile)
    assert not service.browser_execution.lock.locked()
    assert not service.browser_execution.handoff_active
    if mode=='duplicate':
        assert service.official_images.last_report['blocked_generation_requests']==1


@pytest.mark.asyncio
async def test_local_readiness_check_makes_no_generation(local_service,local_flow):
    service,cookie,profile=local_service
    result=await service.official_images.run(cookies=cookie,check_only=True)
    assert result['page_ready'] and result['browser_closed']
    assert not result['generation_submitted'] and result['generation_requests']==0
    assert local_flow[0].submissions==0
    assert not profile_in_use(profile)


@pytest.mark.asyncio
async def test_cancelled_call_closes_own_browser(local_service,monkeypatch):
    service,cookie,profile=local_service
    async def cancelled(*args,**kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(service.official_images,'submit_once',cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.official_images.run(cookies=cookie,prompt='local fixture')
    assert service.official_images.last_report['browser_closed']
    assert not profile_in_use(profile)
    assert not service.browser_execution.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'delayed'])
async def test_backend_http_queue_success_and_real_download(local_service,local_flow,monkeypatch,mode):
    service,cookie,profile=local_service
    local_flow[0].mode=mode
    if mode == 'success':
        service.media_storage.configure(str(service.engine.root.parent / '自选图片目录'))
    # Real service worker + local real browser. Provider is a local test fixture.
    app=FastAPI()
    app.include_router(create_console_router(service))
    app.add_api_route('/api/v1/media/{filename:path}', api.flow_media,methods=['GET'])
    monkeypatch.setattr(api,'MEDIA_DIR',service.engine.output_dir)
    monkeypatch.setattr(api,'engine',service.engine)
    service.worker=asyncio.create_task(service.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/console/api/jobs',json={'kind':'image','model':'beluga','prompt':'local fixture','account_id':'account_fixture'})
            assert response.status_code==202
            job=response.json()
            await asyncio.wait_for(service.queue.join(),30)
            done=(await client.get('/console/api/jobs/'+job['id'])).json()
            assert done['status']=='succeeded', done
            assert done['result']['execution']['browser_closed']
            download=await client.get(done['result']['files'][0]['url'])
            assert download.status_code==200
            assert download.content==local_flow[1]
            assert (service.engine.output_dir / done['result']['files'][0]['name']).read_bytes() == download.content
            public_report = dict(done['result'].get('execution',{}))
            assert public_report.pop('upload_project_id') == 'fixture'
            assert 'fixture' not in json.dumps(public_report).replace('account_fixture','')
            assert local_flow[0].submissions==1
            if mode=='delayed':
                assert done['result']['execution']['submit_ready_wait_seconds']>=.4
            assert not done['result'].get('recovery')
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_disabled_submit_waits_then_fails_without_generation(local_service,local_flow):
    service,cookie,profile=local_service
    local_flow[0].mode='disabled'
    service.official_images.submit_ready_timeout=.3
    with pytest.raises(OfficialImageError) as exc:
        await service.official_images.run(cookies=cookie,prompt='local fixture')
    assert exc.value.code=='submit_disabled'
    assert not exc.value.submitted
    assert local_flow[0].submissions==0
    report=service.official_images.last_report
    assert not report['generation_submitted'] and report['generation_requests']==0
    assert report['browser_closed'] and not profile_in_use(profile)
    assert not list(service.engine.output_dir.glob('*'))


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [True, False])
async def test_manual_login_reveals_then_closes_without_generation(local_service,local_flow,monkeypatch,complete):
    from flow_agent.browser_visibility import OwnedBrowserVisibility
    service,cookie,profile=local_service
    from flow_agent.verification_settings import configure
    configure(service, False)  # Explicitly retained visible/manual mode.
    local_flow[0].mode='login'
    service.official_images.manual_timeout=8 if complete else 1.5
    guards=[]
    def create_guard(*args, **kwargs):
        guard=OwnedBrowserVisibility(*args, **kwargs);guards.append(guard);return guard
    monkeypatch.setattr('flow_agent.official_images.OwnedBrowserVisibility',create_guard)
    pages=[]
    reason=service.official_images.attention_reason
    async def observe(page):
        if page not in pages:pages.append(page)
        return await reason(page)
    monkeypatch.setattr(service.official_images,'attention_reason',observe)
    task=asyncio.create_task(service.official_images.run(cookies=cookie,check_only=True))
    try:
        for _ in range(150):
            if guards and guards[0].info()['shown_for_user'] and any(w['visible'] for w in guards[0].snapshot()):
                break
            await asyncio.sleep(.1)
        else:
            pytest.fail('Login window did not become visible')
        assert guards[0].info()['waiting_for_user']
        assert service.official_images.active_report['stage']=='waiting_for_user'
        assert local_flow[0].submissions==0
        if complete:
            await next(p for p in pages if p.url.startswith('http://127.0.0.1')).get_by_role('button',name='Continue local sign-in').click()
            report=await asyncio.wait_for(task,20)
            assert report['page_ready'] and report['browser_closed']
            assert not report['visibility']['waiting_for_user']
        else:
            with pytest.raises(OfficialImageError) as exc:
                await asyncio.wait_for(task,10)
            assert exc.value.code=='manual_timeout'
        assert service.official_images.last_report['browser_closed']
        assert not profile_in_use(profile)
        assert local_flow[0].submissions==0
    finally:
        if not task.done():
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass


@pytest.mark.asyncio
async def test_visibility_does_not_adopt_or_close_other_profiles(local_service, local_flow, tmp_path):
    from flow_agent.browser_visibility import OwnedBrowserVisibility, BrowserVisibilityError
    from playwright.async_api import async_playwright
    _, _, profile = local_service
    browser = Path(os.environ['FLOW_TEST_BROWSER'])
    foreign_profile = tmp_path / 'unrelated-profile'
    observer = OwnedBrowserVisibility(foreign_profile, browser)
    guard = OwnedBrowserVisibility(profile, browser)
    await guard.start()
    async with async_playwright() as pw:
        foreign = await pw.chromium.launch_persistent_context(str(foreign_profile), executable_path=str(browser), headless=False)
        owned = None
        try:
            for _ in range(100):
                before = observer.snapshot()
                if any(w['visible'] for w in before): break
                await asyncio.sleep(.1)
            assert any(w['visible'] for w in before)
            with pytest.raises(BrowserVisibilityError):
                await OwnedBrowserVisibility(foreign_profile, browser).start()
            owned = await pw.chromium.launch_persistent_context(str(profile), executable_path=str(browser), headless=False, args=guard.launch_args())
            await guard.confirm_hidden()
            assert guard.info()['hidden_confirmed']
            assert not any(w['visible'] for w in guard.snapshot())
            assert any(w['visible'] for w in observer.snapshot())
            await owned.close()
            assert await guard.close_owned()
            assert profile_in_use(foreign_profile)
            assert any(w['visible'] for w in observer.snapshot())
        finally:
            if owned: await owned.close()
            await guard.close_owned()
            await foreign.close()


def test_detail_binding_requires_exact_media_project_and_scene():
    from flow_agent.official_images import detail_bindings
    row=[MEDIA_ID,'expected-project',NEW_ID]
    assert detail_bindings([row],{MEDIA_ID},'expected-project')=={(MEDIA_ID,NEW_ID)}
    assert not detail_bindings([row],{OLD_ID},'expected-project')
    assert not detail_bindings([row],{MEDIA_ID},'other-project')
    assert not detail_bindings([[row]],{MEDIA_ID},'expected-project')
    assert not detail_bindings([[MEDIA_ID,'expected-project',None]],{MEDIA_ID},'expected-project')


@pytest.mark.asyncio
@pytest.mark.parametrize('known_detail', [True, False])
async def test_recover_existing_bound_original_without_generation(local_service, local_flow, known_detail):
    service,cookie,profile=local_service
    local_flow[0].mode='recover'
    raw=await service.official_images.run(cookies=cookie,check_only=True,
        _recover={'media_ids':[MEDIA_ID], **({'detail_id':NEW_ID} if known_detail else {})})
    assert raw['status']=='succeeded' and raw['execution']['download_verified']
    assert raw['execution']['recovered_without_generation']
    assert raw['execution']['generation_requests']==0
    assert raw['execution']['browser_closed']
    assert Path(raw['files'][0]['path']).read_bytes()==local_flow[1]
    assert local_flow[0].submissions==0
    assert not profile_in_use(profile)


@pytest.mark.asyncio
async def test_reconciliation_preserves_failure_and_never_regenerates(local_service,local_flow):
    service,cookie,profile=local_service
    local_flow[0].mode='wrong_result'
    service.worker=asyncio.create_task(service.run())
    from flow_agent.console import JobInput
    try:
        job=service.enqueue(JobInput(kind='image',model='beluga',prompt='local fixture',account_id='account_fixture'))
        await asyncio.wait_for(service.queue.join(),40)
        original=service.get_job(job['id'])
        assert original['status']=='failed'
        assert local_flow[0].submissions==1
        local_flow[0].mode='recover'
        raw=await service.official_images.run(cookies=cookie,check_only=True,
            _recover={'media_ids':[MEDIA_ID],'detail_id':NEW_ID})
        assert original['result']['execution']['error_code'] == 'original_result_unknown'
        assert original['result']['execution']['original_error_code'] == 'result_identity_mismatch'
        project = raw['execution']['upload_project_id']
        raw['execution']['upload_project_id'] = 'wrong-project'
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            service.reconcile_official_download(job['id'],raw)
        assert service.get_job(job['id']) == original
        raw['execution']['upload_project_id'] = project
        done=service.reconcile_official_download(job['id'],raw)
        assert done['status']=='succeeded' and done['error'] is None
        assert done['result']['execution']['outcome']=='recovered'
        assert done['result']['recovery']['original_job']==original
        assert done['result']['execution']['generation_requests']==1
        assert done['result']['recovery']['read_only_execution']['generation_requests']==0
        assert len(service.list_jobs())==1 and local_flow[0].submissions==1
        assert not profile_in_use(profile)
        with pytest.raises(Exception): service.reconcile_official_download(job['id'],raw)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_agent_sidebar_is_closed_before_exact_image_parameter_check(local_service, local_flow):
    service, cookie, _ = local_service
    original = local_flow[0].mode
    # Fixture contains the real conflict: an agent prompt with a different
    # settings panel and the direct editor hidden behind the sidebar.
    global HTML
    saved = HTML
    try:
        split = HTML.index('<script>\nconst mode=')
        start = HTML.index('<div role="textbox"')
        HTML = HTML[:start] + '<section id="direct" hidden>' + HTML[start:split] + '</section>' + HTML[split:]
        HTML = HTML.replace('<script>\nconst mode=', '<aside id="agent"><div role="textbox" aria-label="您希望创作什么？"></div><button aria-label="智能体指令">instructions</button><button aria-label="发起新的会话">new</button><button aria-label="关闭" onclick="document.querySelector(\'#agent\').remove();document.querySelector(\'#direct\').hidden=false">close</button></aside><script>\nconst mode=')
        result = await service.official_images.run(cookies=str(cookie),check_only=True,model='beluga',aspect='1:1',count=1)
        assert result['agent_panel_closed'] is True
        assert result['parameters'] == {'model':'beluga','aspect':'1:1','count':1,'kind':'image'}
        assert result['browser_closed'] and result['generation_requests']==0
        assert local_flow[0].submissions==0
    finally:
        HTML = saved


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['enabled', 'already_off', 'stuck', 'missing_state', 'ambiguous'])
async def test_inline_agent_toggle_is_verified_before_direct_image_settings(local_service, local_flow, state):
    service, cookie, profile = local_service
    service.official_images.agent_ready_timeout = .4
    global HTML
    saved = HTML
    try:
        start = HTML.index('<div role="textbox"')
        end = HTML.index('<script>\nconst mode=')
        direct = HTML[start:end]
        if state != 'already_off':
            direct = direct.replace('<button aria-label="设置触发器"', '<button hidden aria-label="设置触发器"')
        changes = "this.setAttribute('aria-pressed','false');document.querySelector('[aria-label=设置触发器]').hidden=false;document.querySelector('#instructions').remove();document.querySelector('#agent-settings').remove();"
        handler = "this.dataset.clicked='yes';" + ("setTimeout(()=>{"+changes+"},150)" if state == 'enabled' else '')
        pressed = '' if state == 'missing_state' else ' aria-pressed="'+('false' if state == 'already_off' else 'true')+'"'
        toggle = '<button aria-label="智能体"'+pressed+' onclick="'+handler+'">mode</button>'
        if state == 'ambiguous': toggle += '<button aria-label="智能体" aria-pressed="true">other</button>'
        controls = '' if state == 'already_off' else '<button id="instructions" aria-label="智能体指令">instructions</button><button id="agent-settings" aria-label="设置">agent settings</button>'
        HTML = HTML[:start] + '<section>'+toggle+controls+direct+'</section>' + HTML[end:]
        if state in ('enabled','already_off'):
            result = await service.official_images.run(cookies=cookie,check_only=True,model='beluga',aspect='1:1',count=1)
            assert result['direct_composer_confirmed']
            assert result['parameters'] == {'model':'beluga','aspect':'1:1','count':1,'kind':'image'}
            if state == 'enabled':
                assert result['agent_mode_disabled'] and result['agent_mode_before']=='true' and result['agent_mode_after']=='false'
            else: assert not result.get('agent_mode_disabled')
        else:
            with pytest.raises(OfficialImageError) as error:
                await service.official_images.run(cookies=cookie,check_only=True,model='beluga',aspect='1:1',count=1)
            assert error.value.code == 'agent_panel_unconfirmed'
            assert service.official_images.last_report['agent_guard_reason']
        report = service.official_images.last_report
        assert report['browser_closed'] and report['generation_requests']==0
        assert local_flow[0].submissions == 0 and not profile_in_use(profile)
    finally:
        HTML = saved


@pytest.mark.asyncio
async def test_agent_mode_reenabled_during_prompt_input_cannot_submit(local_service, local_flow):
    service, cookie, profile = local_service
    global HTML
    saved = HTML
    try:
        HTML = HTML.replace('<div role="textbox"', '<button aria-label="智能体" aria-pressed="false">mode</button><div role="textbox"',1)
        HTML = HTML.replace("textbox.addEventListener('input',()=>{", "textbox.addEventListener('input',()=>{document.querySelector('[aria-label=智能体]').setAttribute('aria-pressed','true');",1)
        with pytest.raises(OfficialImageError) as error:
            await service.official_images.run(cookies=cookie,prompt='local fixture only',model='beluga',aspect='1:1',count=1)
        assert error.value.code == 'agent_panel_unconfirmed'
        report = service.official_images.last_report
        assert report['browser_closed'] and report['generation_requests']==0
        assert local_flow[0].submissions==0 and not profile_in_use(profile)
    finally:
        HTML = saved


@pytest.fixture(autouse=True)
def short_uncertain_window_for_local_regressions(monkeypatch):
    # Full production 180-second timing is exercised in test_uncertain_result.py.
    monkeypatch.setattr("flow_agent.uncertain_result.UNCERTAIN_WAIT_SECONDS", .1)
