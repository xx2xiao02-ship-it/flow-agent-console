"""Real Chrome with local video pages; no Google generation or credits."""
import asyncio
import json
import os
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from flow_agent.official_images import profile_in_use
from flow_agent import api
from flow_agent.console import ConsoleService, create_console_router, safe_failure
from flow_agent.engine import FlowEngine
from flow_agent.official_videos import OfficialVideoError, video_result_ids, video_detail_bindings, rpc_ids

PROJECT = 'fixture'
OLD = '99999999-9999-4999-8999-999999999999'
IDS = [f'{i:08d}-1111-4111-8111-111111111111' for i in range(1, 5)]
CONTENTS = [f'{i:08d}-2222-4222-8222-222222222222' for i in range(1, 5)]
VIDEO = Path(__file__).parent / 'assets/video-page-4s.mp4'

HTML = '''<!doctype html><html lang="zh-CN"><title>Local video fixture</title><style>[role=listbox]{min-height:40px}</style><body>
<h1 onclick="document.querySelector('#settings').hidden=true">Local fixture project</h1>
<p onclick="document.querySelector('#settings').hidden=true">Google Flow 可能会出错，请注意核查</p>
<button aria-label="Google 账号：fixture (fixture@example.test)">账号</button>
<section id="gallery"><div role="group" aria-label="old"><img src="/poster/OLD"></div></section>
<div role="textbox" aria-label="您希望创作什么？" contenteditable="true"></div>
<button aria-label="清除提示" onclick="document.querySelector('[contenteditable]').innerText='';document.querySelector('#attached').innerText=''">清除提示</button>
<div id="attached"></div>
<button aria-label="开始" onclick="openPicker()">开始</button><button aria-label="结束" onclick="openPicker()">结束</button>
<button aria-label="在提示框中添加素材" onclick="openPicker()">添加素材</button>
<section id="picker" hidden>
<button aria-label="上传媒体内容" onclick="document.querySelector('#upload').click()">上传</button>
<input id="upload" type="file" accept="image/png,image/jpeg,image/webp" hidden>
<div role="listbox" aria-label="资源列表"></div>
<button aria-label="添加到提示" onclick="attach()">添加到提示</button>
</section>
<dialog id="rights" aria-label="此图片的使用权"><p>请确认图片使用权</p>
<button onclick="confirmRights()">我同意</button>
<button onclick="document.querySelector('#rights').close()">取消</button></dialog>
<button aria-label="设置触发器" onclick="document.querySelector('#settings').hidden=!document.querySelector('#settings').hidden">视频 · 720p · 8 秒 x1</button>
<section id="settings" hidden>
<input type="radio" aria-label="图片" name="kind" checked><input type="radio" aria-label="视频" name="kind">
<input type="radio" aria-label="帧" name="mode"><input type="radio" aria-label="素材" name="mode" checked>
<input type="radio" aria-label="16:9" name="aspect" checked><input type="radio" aria-label="9:16" name="aspect">
<button aria-label="选择模型系列" onclick="document.querySelector('#models').hidden=!document.querySelector('#models').hidden">Omni 1.1 Flash</button>
<section id="models" hidden></section>
<input type="radio" aria-label="360p 360p 分辨率的清晰度较低，生成速度较快" name="quality" value="360p">
<input type="radio" aria-label="720p" name="quality" value="720p" checked>
DURATION_RADIOS
COUNT_RADIOS
</section>
<button aria-label="开始生成" disabled>开始生成</button>
<button aria-label="下载媒体内容" hidden onclick="document.querySelector('#download-menu').hidden=false">下载</button>
<section id="download-menu" hidden><a role="menuitem" download="video.mp4" href="/download">DOWNLOAD_LABEL</a></section>
<script>
const mode='MODE', ids=IDS, contents=CONTENTS;
if(mode==='delayed_radios'){
 for(const label of ['视频','素材']){
  const old=document.querySelector('input[aria-label="'+label+'"]');
  const button=document.createElement('button');button.type='button';button.role='radio';
  button.ariaLabel=label;button.setAttribute('aria-checked','false');
  button.onclick=()=>setTimeout(()=>button.setAttribute('aria-checked','true'),350);
  old.replaceWith(button);
 }
}
const modelButton=document.querySelector('[aria-label="选择模型系列"]');
for(const label of ['Omni 1.1 Flash','Veo 3.1 - Lite','Veo 3.1 - Fast','Veo 3.1 - Quality']){
 if(mode==='missing_veo' && label.includes('Veo'))continue;
 const item=document.createElement('button');item.role='menuitem';item.innerText='volume_up'+label;
 item.onclick=()=>{if(mode!=='sticky_model')modelButton.innerText=label+' arrow_drop_down';document.querySelector('#models').hidden=true;};document.querySelector('#models').append(item);
}

const textbox=document.querySelector('[contenteditable]'), submit=document.querySelector('[aria-label="开始生成"]');
textbox.addEventListener('input',()=>{submit.disabled=mode==='disabled'||!textbox.innerText;});
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelector('#settings').hidden=true;});
function openPicker(){document.querySelector('#picker').hidden=false;}
let chosen;
async function confirmRights(){
 const response=await fetch('/data/batchexecute?rpcids=JJH6Ub',{method:'POST',body:'{}'});
 if(response.ok){document.querySelector('#rights').close();publishUpload();}
}
async function publishUpload(){
 const name=document.querySelector('#upload').files[0].name;
 await fetch('/data/batchexecute?rpcids=maseQ',{method:'POST',body:name});
 const option=document.createElement('div');option.role='option';option.innerText=name+' 图片';
 option.onclick=()=>{chosen=name;if(mode==='auto_attach')attach();};document.querySelector('[role=listbox]').append(option);
 if(mode==='upload_auto_attach'){chosen=name;attach();document.querySelector('#attached button:last-child').ariaLabel='素材';}
 if(mode==='upload_pending'){
  const pending=document.createElement('div');pending.role='option';pending.innerText='正在上传 '+name;
  document.querySelector('[role=listbox]').prepend(pending);
  const add=document.querySelector('[aria-label="添加到提示"]');add.disabled=true;
  setTimeout(()=>{pending.remove();add.disabled=false;},900);
  option.onclick=()=>{if(document.querySelector('[role=listbox]').innerText.includes('正在上传'))throw Error('premature selection');chosen=name;};
 }
}
document.querySelector('#upload').addEventListener('change',()=>{
 if(mode==='rights')document.querySelector('#rights').showModal();else publishUpload();
});
function attach(){const button=document.createElement('button');button.ariaLabel='图片素材';button.innerText=chosen;const image=document.createElement('img');image.src='/poster/attached';button.append(image);document.querySelector('#attached').append(button);document.querySelector('#picker').hidden=true;}
function draw(count){
 for(let i=0;i<count;i++){
  const group=document.createElement('div');group.role='group';group.ariaLabel='new '+ids[i];
  const img=document.createElement('img');img.src='/poster/'+ids[i];group.append(img);
  group.ondblclick=async()=>{
   if(mode!=='prefetched')await fetch('/data/batchexecute?rpcids=as29s',{method:'POST',body:JSON.stringify({id:mode==='wrong_result'?'OLD':ids[i]})});
   history.pushState({},'', '/project/fixture/edit/'+(mode==='wrong_result'?'OLD':ids[i]));
   document.querySelector('[aria-label="下载媒体内容"]').hidden=false;
  };
  if(mode==='prefetched'){
   fetch('/data/batchexecute?rpcids=as29s',{method:'POST',body:JSON.stringify({id:ids[i]})})
    .then(r=>r.text()).then(()=>document.querySelector('#gallery').prepend(group));
  }else document.querySelector('#gallery').prepend(group);
 }
}
if(HAD_RESULTS && mode!=='timeout')draw(HAD_RESULTS);
if(location.pathname.includes('/edit/')){
 fetch('/data/batchexecute?rpcids=as29s',{method:'POST',body:JSON.stringify({id:location.pathname.split('/').pop()})});
 document.querySelector('[aria-label="下载媒体内容"]').hidden=false;
}
submit.onclick=async()=>{
 const count=Number(document.querySelector('[name=count]:checked').value);
 const payload={prompt:textbox.innerText,count,aspect:document.querySelector('[name=aspect]:checked').ariaLabel,
  quality:document.querySelector('[name=quality]:checked').value,duration:document.querySelector('[name=duration]:checked').value,
  attached:document.querySelector('#attached').innerText};
 if(modelButton.innerText.includes('Veo'))payload.video_model=modelButton.innerText.split(' arrow_drop_down')[0];
 const request=()=>fetch('/data/batchexecute?rpcids=YhhmEf',{method:'POST',body:JSON.stringify(payload)});
 const response=await request();if(mode==='duplicate')request().catch(()=>{});
 if(!response.ok||mode==='timeout'||mode==='empty')return;
 if(mode!=='upscale_only')document.querySelector('#download-menu a').innerText=payload.quality+'（原始）';
 draw(count);
};
if(mode==='auto')fetch('/data/batchexecute?rpcids=YhhmEf',{method:'POST',body:'auto'}).catch(()=>{});
</script></body></html>'''


@pytest.fixture
def local_video_page():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            if self.path == '/download':
                self.server.downloads += 1
                invalid = self.server.mode == 'invalid' or (self.server.mode == 'partial_invalid' and self.server.downloads > 1)
                data = b'<html>invalid download</html>' if invalid else VIDEO.read_bytes()
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Disposition', 'attachment; filename="video.mp4"')
            elif self.path.startswith('/poster/'):
                data = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="36"><rect width="64" height="36" fill="navy"/></svg>'
                self.send_header('Content-Type', 'image/svg+xml')
            else:
                html = HTML.replace('OLD', OLD).replace('MODE', self.server.mode)
                html = html.replace('IDS', json.dumps(IDS)).replace('CONTENTS', json.dumps(CONTENTS))
                html = html.replace('HAD_RESULTS', str(self.server.result_count))
                options = ''.join('<div role="option" data-name="'+escape(name, quote=True)+
                    '" onclick="chosen=this.dataset.name">'+escape(name)+' 图片</div>' for name in self.server.uploads)
                html = html.replace('<div role="listbox" aria-label="资源列表"></div>',
                    '<div role="listbox" aria-label="资源列表">'+options+'</div>')
                html = html.replace('DOWNLOAD_LABEL', '1080p 放大' if self.server.mode == 'upscale_only' else self.server.result_quality+'（原始）')
                html = html.replace('DURATION_RADIOS', ''.join(f'<input type="radio" name="duration" aria-label="{i} 秒" value="{i}s" {"checked" if i==8 else ""}>' for i in (4,6,8,10)))
                html = html.replace('COUNT_RADIOS', ''.join(f'<input type="radio" name="count" aria-label="x{i}" value="{i}" {"checked" if i==1 else ""}>' for i in range(1,5)))
                if self.server.mode == 'access_denied':
                    html = '<!doctype html><h2>您似乎无权访问 Flow。请点击此处查看推出范围</h2>'
                data = html.encode()
                self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers();self.wfile.write(data)
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            code = 200
            if 'as29s' in self.path:
                key = json.loads(body)['id']
                if self.server.mode == 'wrong_detail':
                    key = OLD
                content = CONTENTS[IDS.index(key)] if key in IDS else OLD
                rpc, payload = 'as29s', [content, PROJECT, key, 'video', []]
            elif 'DA4VGb' in self.path or 'Kcr7Ub' in self.path:
                rpc, payload = ('DA4VGb' if 'DA4VGb' in self.path else 'Kcr7Ub'), []
            elif 'JJH6Ub' in self.path:
                self.server.rights_confirmations += 1
                rpc, payload = 'JJH6Ub', []
            elif 'maseQ' in self.path:
                self.server.uploads.append(body.decode())
                rpc, payload = 'maseQ', []
            else:
                self.server.submissions += 1
                payload = json.loads(body)
                self.server.requests.append(payload)
                self.server.result_quality = payload['quality']
                count = payload['count']
                self.server.result_count = 0 if self.server.mode == 'empty' else count
                rpc, payload = 'YhhmEf', [None, 100, [[key,None,None,[],PROJECT] for key in IDS[:self.server.result_count]]]
                if self.server.mode in ('media_response', 'media_content_response'):
                    payload = [None, 100, [], [[key, PROJECT] for key in (CONTENTS if self.server.mode == 'media_content_response' else IDS)[:self.server.result_count]]]
                code = 403 if self.server.mode == 'rejected' else 200
            data = json.dumps([['wrb.fr', rpc, json.dumps(payload)]]).encode()
            if self.server.mode in ('rpc_error','rpc_error_with_reason') and rpc == 'YhhmEf':
                reason = 'PUBLIC_ERROR_UNUSUAL_ACTIVITY Cookie=PRIVATE' if self.server.mode == 'rpc_error_with_reason' else 'PRIVATE'
                data = json.dumps([['wrb.fr',rpc,None,None,None,[7,None,[reason]],'generic']]).encode()
            self.send_response(code);self.send_header('Content-Length', str(len(data)))
            self.end_headers();self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.mode, server.submissions, server.downloads, server.result_count = 'success', 0, 0, 0
    server.requests, server.uploads = [], []
    server.rights_confirmations = 0
    server.result_quality = '720p'
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        yield server
    finally:
        server.shutdown();server.server_close();thread.join(2)


@pytest.fixture
def video_service(tmp_path, monkeypatch, local_video_page):
    chrome = os.environ.get('FLOW_TEST_BROWSER')
    if not chrome or not Path(chrome).is_file():
        pytest.skip('Set FLOW_TEST_BROWSER for local real-browser tests')
    service = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    service.initialize();service.cookies_dir.mkdir()
    # Legacy preferences: headless checks and the hidden generation window. The
    # new visible defaults are covered in test_humanize.py.
    with service.connect() as db:
        db.execute("insert or replace into console_meta values ('verification_headless', 'true')")
        db.execute("insert or replace into console_meta values ('generation_browser_visible', 'false')")
    cookie = service.cookies_dir / 'account_fixture.json'
    cookie.write_text(json.dumps({'cookies':[{'name':'test','value':'PRIVATE'}], 'at':'PRIVATE',
        'fsid':'PRIVATE','fingerprint':{'userAgent':'local fixture'}}))
    profile = tmp_path / 'exclusive-profile';profile.mkdir()
    monkeypatch.setattr(service.official_videos, 'settings', lambda _: (
        profile, Path(chrome), 'fixture@example.test', f'http://127.0.0.1:{local_video_page.server_port}/project/fixture'))
    return service, cookie, profile


def test_video_binding_uses_project_and_distinct_ids():
    docs = [[None,100,[[IDS[0],None,None,[],PROJECT],[IDS[1],None,None,[],'other']]]]
    assert video_result_ids(docs, PROJECT) == [IDS[0]]
    detail = [[CONTENTS[0],PROJECT,IDS[0]], [CONTENTS[1],'other',IDS[1]]]
    assert video_detail_bindings(detail, set(IDS), PROJECT) == {(IDS[0],CONTENTS[0],IDS[0])}
    assert not video_detail_bindings(detail, {OLD}, PROJECT)
    request = SimpleNamespace(url='http://local/data/batchexecute?rpcids=YhhmEf',post_data_buffer=b'f.req=%5B%5B%5B%22ogiZ0b%22%5D%5D%5D')
    assert rpc_ids(request) == {'YhhmEf','ogiZ0b'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,count,expected', [
    ('success',1,'succeeded'), ('success',2,'succeeded'), ('success',4,'succeeded'),
    ('duplicate',1,'succeeded'), ('rejected',1,'failed'), ('empty',1,'failed'),
    ('timeout',1,'failed'), ('wrong_result',1,'succeeded'), ('wrong_detail',1,'failed'), ('invalid',1,'failed'),
    ('upscale_only',1,'failed'),
    ('partial_invalid',2,'failed'),
    ('prefetched',1,'succeeded'),
])
async def test_backend_video_page_queue_download_and_cleanup(video_service, local_video_page, monkeypatch, mode, count, expected):
    service,cookie,profile = video_service
    if mode == 'success':
        service.media_storage.configure(str(service.engine.root.parent / '自选视频目录'))
    server = local_video_page;server.mode = mode
    service.official_videos.timeout = 3 if mode in ('timeout','wrong_detail') else 45
    app = FastAPI();app.include_router(create_console_router(service))
    app.add_api_route('/api/v1/media/{filename:path}', api.flow_media, methods=['GET'])
    monkeypatch.setattr(api, 'MEDIA_DIR', service.engine.output_dir)
    monkeypatch.setattr(api, 'engine', service.engine)
    service.worker = asyncio.create_task(service.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            submitted = await client.post('/console/api/jobs', json={
                'kind':'video','prompt':'local video only','aspect':'9:16','duration':'4s',
                'quality':'360p','count':count,'account_id':'account_fixture'})
            assert submitted.status_code == 202
            await asyncio.wait_for(service.queue.join(), 120)
            done = (await client.get('/console/api/jobs/'+submitted.json()['id'])).json()
            assert done['status'] == expected, done
            report = service.official_videos.last_report
            assert report['browser_closed'] and report['visibility']['hidden_confirmed']
            assert report['visibility']['visible_owned_windows'] == 0 and not report['visibility']['shown_for_user']
            assert server.submissions == 1 and report['generation_requests'] == 1
            assert server.requests[0] == dict(prompt='local video only',count=count,aspect='9:16',duration='4s',quality='360p',attached='')
            assert done['result']['execution']['browser_closed']
            assert 'PRIVATE' not in json.dumps(done) and str(cookie) not in json.dumps(done)
            if expected == 'succeeded':
                assert len(done['result']['files']) == count
                assert len({r['generated_id'] for r in report['verified_results']}) == count
                assert [r['generated_id'] for r in report['verified_results']] == IDS[:count]
                for file in done['result']['files']:
                    download = await client.get(file['url'])
                    assert download.status_code == 200 and download.content == VIDEO.read_bytes()
                    assert (service.engine.output_dir / file['name']).read_bytes() == download.content
            if mode == 'duplicate':
                assert report['blocked_generation_requests'] == 1
            if mode == 'upscale_only':
                assert server.downloads == 0
            if mode == 'partial_invalid':
                assert len(done['result']['files']) == 1
                saved = await client.get(done['result']['files'][0]['url'])
                assert saved.status_code == 200 and saved.content == VIDEO.read_bytes()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_check_only_blocks_auto_submission_and_closes(video_service, local_video_page):
    service,cookie,profile = video_service
    local_video_page.mode = 'auto'
    report = await service.official_videos.run(cookies=str(cookie),check_only=True,prompt='local readiness only')
    assert report['page_ready'] and report['browser_closed']
    assert not report['generation_submitted'] and report['generation_requests'] == 0
    assert report['blocked_generation_requests'] == 1 and local_video_page.submissions == 0


@pytest.mark.asyncio
async def test_async_aria_radios_wait_for_state_without_generation(video_service, local_video_page):
    service,cookie,profile=video_service
    local_video_page.mode='delayed_radios'
    report=await service.official_videos.run(cookies=str(cookie),check_only=True,
        duration='4s',quality='720p',count=1)
    assert report['parameter_step']=='confirmed' and report['browser_closed']
    assert report['generation_requests']==local_video_page.submissions==0


@pytest.mark.asyncio
async def test_http200_rpc_error_is_not_result_parser_failure(video_service, local_video_page):
    service,cookie,profile=video_service
    local_video_page.mode='rpc_error'
    with pytest.raises(OfficialVideoError) as caught:
        await service.official_videos.run(cookies=str(cookie),prompt='local only')
    assert caught.value.code == 'original_result_unknown'
    assert caught.value.execution['original_error_code'] == 'video_rpc_error'
    assert caught.value.execution['outcome'] == 'pending_review'
    report=service.official_videos.last_report
    assert report['generation_response']['rpc_error_codes']==[7]
    assert report['browser_closed'] and local_video_page.submissions==1
    assert local_video_page.downloads==0
    assert 'PRIVATE' not in json.dumps(report)


@pytest.mark.asyncio
async def test_official_access_denied_is_specific_and_closes_without_generation(video_service, local_video_page):
    service, cookie, profile = video_service
    local_video_page.mode = 'access_denied'
    with pytest.raises(OfficialVideoError) as error:
        await service.official_videos.run(cookies=str(cookie), check_only=True)
    assert error.value.code == 'flow_access_denied'
    assert '无权访问 Flow' in safe_failure(error.value)
    report = service.official_videos.last_report
    assert report['browser_closed'] and report['generation_requests'] == local_video_page.submissions == 0
    assert not report['generation_submitted'] and not report['visibility']['shown_for_user']


@pytest.mark.asyncio
@pytest.mark.parametrize('upload_mode', ['normal', 'upload_pending'])
async def test_video_frames_upload_order_and_check_only_never_generate(video_service, local_video_page, tmp_path, upload_mode):
    service,cookie,profile = video_service
    local_video_page.mode = upload_mode
    files = []
    for name in ('first.png','last.png'):
        file = tmp_path/name;file.write_bytes(b'local file chooser fixture '+name.encode());files.append(str(file))
    report = await service.official_videos.run(cookies=str(cookie),check_only=True,
        start_image=files[0],end_image=files[1])
    assert local_video_page.uploads == ['first.png','last.png']
    assert local_video_page.submissions == 0 and report['browser_closed']
    assert report['parameters']['start_frame'] and report['parameters']['end_frame']


@pytest.mark.asyncio
@pytest.mark.parametrize('user_action', ['agree', 'timeout', 'cancel'])
async def test_upload_rights_reveals_waits_for_user_and_never_agrees_automatically(
        video_service, local_video_page, tmp_path, monkeypatch, user_action):
    service, cookie, profile = video_service
    local_video_page.mode = 'rights'
    # This test exercises an explicit visible manual-confirmation flow.
    # Automatic checks default to headless and cannot present this dialog.
    from flow_agent.verification_settings import configure
    configure(service, False)
    runner = service.official_videos
    runner.manual_timeout = .8 if user_action == 'timeout' else 15
    runner.upload_timeout = 1
    captured = {}
    original = type(runner).prepare_video
    async def capture(self, page, **kwargs):
        captured['page'] = page
        return await original(self, page, **kwargs)
    monkeypatch.setattr(type(runner), 'prepare_video', capture)
    first = tmp_path / 'rights-frame.png';first.write_bytes(b'local chooser fixture')
    task = asyncio.create_task(runner.run(cookies=str(cookie), check_only=True,
        start_image=str(first), duration='4s', quality='720p'))
    try:
        async with asyncio.timeout(30):
            while not runner.active_report or runner.active_report.get('stage') != 'waiting_for_upload_rights':
                await asyncio.sleep(.05)
        # No upload has been acknowledged and the adapter never clicks agreement.
        assert local_video_page.uploads == []
        async with asyncio.timeout(5):
            while not runner.active_report['visibility'].get('shown_for_user'):
                await asyncio.sleep(.05)
        assert runner.active_report['visibility']['reason'] == 'media_rights_confirmation'
        dialog = captured['page'].get_by_role('dialog', name='此图片的使用权')
        if user_action != 'timeout':
            # This is a simulated human on a localhost fixture, not adapter code.
            await dialog.get_by_role('button', name='我同意' if user_action == 'agree' else '取消', exact=True).click()
        if user_action == 'agree':
            report = await task
            assert report['page_ready'] and report['upload_rights_dialog_dismissed']
            assert local_video_page.uploads == ['rights-frame.png']
            assert report['parameters']['start_frame'] and report['parameters']['duration'] == '4s'
        else:
            with pytest.raises(OfficialVideoError) as error:
                await task
            expected = 'video_upload_rights_required' if user_action == 'timeout' else 'video_input_unconfirmed'
            assert error.value.code == expected
            assert not local_video_page.uploads
        report = runner.last_report
        assert report['generation_requests'] == local_video_page.submissions == 0
        assert not report['generation_submitted'] and report['browser_closed']
        assert report['upload_rights_confirmation_required']
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('attachment_mode', ['auto_attach', 'upload_auto_attach'])
async def test_automatically_attached_frame_does_not_wait_for_detached_add_button(
        video_service, local_video_page, tmp_path, attachment_mode):
    service, cookie, profile = video_service
    local_video_page.mode = attachment_mode
    first = tmp_path / 'auto-attached.png';first.write_bytes(b'unique local fixture')
    report = await service.official_videos.run(cookies=str(cookie), check_only=True,
        start_image=str(first), duration='4s')
    assert report['page_ready'] and report['parameters']['start_frame'] and report['browser_closed']
    assert report['generation_requests'] == local_video_page.submissions == 0


@pytest.mark.asyncio
async def test_rights_write_gate_is_single_use_and_does_not_allow_generation(tmp_path):
    service = ConsoleService(FlowEngine(root=tmp_path))
    decisions = []
    class Route:
        def __init__(self, name):
            self.request = SimpleNamespace(method='POST',
                url='https://flow.google.com/data/batchexecute?rpcids='+name,
                post_data_buffer=b'')
        async def abort(self): decisions.append('abort')
        async def continue_(self): decisions.append('continue')
    report = {'upload_rights_confirmation_pending':True,'generation_submitted':False,
        'generation_requests':0,'blocked_generation_requests':0}
    await service.official_videos.gate(Route('JJH6Ub'), report, True)
    await service.official_videos.gate(Route('JJH6Ub'), report, True)
    report['upload_rights_confirmation_pending'] = True
    await service.official_videos.gate(Route('YhhmEf'), report, True)
    await service.official_videos.gate(Route('UNKNOWN'), report, True)
    assert decisions == ['continue','abort','abort','abort']
    assert report['rights_confirmation_requests'] == 1
    assert report['generation_requests'] == 0 and report['blocked_generation_requests'] == 1


@pytest.mark.asyncio
async def test_authorized_upload_confirms_once_without_showing_or_generating(
        video_service, local_video_page, tmp_path):
    service, cookie, profile = video_service
    local_video_page.mode = 'rights'
    first = tmp_path / 'authorized-frame.png'
    first.write_bytes(b'owner authorized fixture')
    service.authorize_media_images([first])
    report = await service.official_videos.run(cookies=str(cookie), check_only=True,
        start_image=str(first), duration='4s', quality='720p')
    assert report['page_ready'] and report['upload_rights_auto_confirmations'] == 1
    assert not report['visibility']['shown_for_user']
    assert report['browser_closed'] and report['parameters']['start_frame']
    assert report['generation_requests'] == local_video_page.submissions == 0
    assert not report['generation_submitted']
    assert local_video_page.uploads == ['authorized-frame.png']
    assert local_video_page.rights_confirmations == 1
    # A second task may reuse only this account/project's verified same-content upload.
    again = await service.official_videos.run(cookies=str(cookie), check_only=True,
        start_image=str(first), duration='4s', quality='720p')
    assert again['page_ready'] and again['reused_uploaded_images'] == 1
    assert again['browser_closed'] and not again['visibility']['shown_for_user']
    assert again['generation_requests'] == local_video_page.submissions == 0
    assert local_video_page.uploads == ['authorized-frame.png']
    assert local_video_page.rights_confirmations == 1


@pytest.mark.asyncio
async def test_video_references_preserve_order(video_service, local_video_page, tmp_path):
    service,cookie,profile = video_service
    files = []
    for name in ('second.png','first.png','third.png','fourth.png'):
        file = tmp_path/name;file.write_bytes(b'local fixture '+name.encode());files.append(str(file))
    report = await service.official_videos.run(cookies=str(cookie),check_only=True,references=files)
    assert local_video_page.uploads == [Path(p).name for p in files]
    assert report['parameters']['reference_count'] == 4
    assert local_video_page.submissions == 0 and report['browser_closed']


@pytest.mark.asyncio
async def test_unsupported_video_options_do_not_open_browser(video_service):
    service,cookie,profile = video_service
    with pytest.raises(OfficialVideoError) as caught:
        await service.official_videos.run(cookies=str(cookie),all_accounts=True)
    assert '未提交' in safe_failure(caught.value) and not service.official_videos.last_report


@pytest.mark.asyncio
async def test_video_cancel_closes_own_browser(video_service, monkeypatch):
    service,cookie,profile = video_service
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(service.official_videos, 'submit_video_once', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.official_videos.run(cookies=str(cookie),prompt='local fixture')
    assert service.official_videos.last_report['browser_closed']
    assert service.official_videos.last_report['error_code'] == 'canceled'
    assert not service.browser_execution.lock.locked()
    assert not service.browser_execution.handoff_active


@pytest.mark.asyncio
async def test_readonly_video_recovery_exact_id_and_original_download(video_service, local_video_page):
    service,cookie,profile=video_service
    local_video_page.result_count=1
    raw=await service.official_videos.run(cookies=str(cookie),check_only=True,
        _recover={'media_ids':[IDS[0]]},timeout=30)
    assert raw['status']=='succeeded' and raw['media_ids']==[IDS[0]]
    assert Path(raw['files'][0]['path']).read_bytes()==VIDEO.read_bytes()
    assert raw['execution']['verified_results'][0]['content_id']==CONTENTS[0]
    assert raw['execution']['generation_requests']==0 and local_video_page.submissions==0
    assert raw['execution']['browser_closed'] and raw['execution']['visibility']['hidden_confirmed']


@pytest.mark.parametrize('invalid', ['none','account','id','generation','closed','invalid_file','project','wrapped'])
def test_video_reconciliation_requires_exact_billed_result(video_service, invalid):
    from fastapi import HTTPException
    from flow_agent.console import JobInput
    from flow_agent.mp4_input import inspect_mp4
    service,cookie,profile=video_service
    job=service.enqueue(JobInput(kind='video',prompt='preserved original',account_id='account_fixture'))
    original={'generation_requests':1,'response_result_ids':[IDS[0]],'error_code':'video_result_identity_mismatch','upload_project_id':'original-project'}
    if invalid == 'wrapped':
        original.update(error_code='original_result_unknown',original_error_code='video_result_identity_mismatch',outcome='pending_review')
    with service.connect() as db:
        db.execute("update console_jobs set status='failed',result_json=?,error='preserved failure' where id=?",
            (json.dumps({'files':[],'execution':original}),job['id']))
    before=service.get_job(job['id'])
    service.engine.output_dir.mkdir(exist_ok=True)
    path=service.engine.output_dir/'existing.mp4';path.write_bytes(VIDEO.read_bytes())
    meta=inspect_mp4(path)
    raw={'status':'succeeded','account_id':'account_fixture','media_ids':[IDS[0]],'files':[{'path':str(path)}],
        'execution':{'upload_project_id':'original-project','response_result_ids':[IDS[0]],'verified_results':[{'generated_id':IDS[0],
            'detail_id':IDS[0],'content_id':CONTENTS[0],'bytes':meta['size'],'duration_seconds':meta['duration_seconds']}],
            'result_identity_verified':True,'download_verified':True,'browser_closed':True,
            'recovered_without_generation':True,'check_only':True,'generation_requests':0,'generation_submitted':False}}
    if invalid=='account':raw['account_id']='other'
    if invalid=='id':raw['media_ids']=[OLD]
    if invalid=='generation':raw['execution']['generation_requests']=1
    if invalid=='closed':raw['execution']['browser_closed']=False
    if invalid=='invalid_file':path.write_bytes(b'not video')
    if invalid=='project':raw['execution']['upload_project_id']='other-project'
    if invalid not in ('none','wrapped'):
        with pytest.raises(HTTPException):service.reconcile_official_video_download(job['id'],raw)
        assert service.get_job(job['id'])==before
    else:
        result=service.reconcile_official_video_download(job['id'],raw)
        assert result['status']=='succeeded' and result['result']['files'][0]['name']==path.name
        assert result['result']['recovery']['original_job']==before
        assert result['result']['execution']['generation_requests']==1
        assert result['result']['execution']['outcome']=='recovered'
        assert result['result']['recovery']['read_only_execution']['generation_requests']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['veo_3_1_lite','veo_3_1_fast','veo_3_1_quality'])
async def test_veo_queue_model_download_cleanup(video_service,local_video_page,monkeypatch,model):
    service,cookie,profile=video_service
    app=FastAPI();app.include_router(create_console_router(service))
    app.add_api_route('/api/v1/media/{filename:path}',api.flow_media,methods=['GET'])
    monkeypatch.setattr(api,'engine',service.engine)
    monkeypatch.setattr(api,'MEDIA_DIR',service.engine.output_dir)
    service.worker=asyncio.create_task(service.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            r=await client.post('/console/api/jobs',json={'kind':'video','prompt':'local Veo fixture','video_model':model,'account_id':'account_fixture'})
            assert r.status_code==202,r.text
            await asyncio.wait_for(service.queue.join(),120)
            done=(await client.get('/console/api/jobs/'+r.json()['id'])).json()
            assert done['status']=='succeeded',done
            assert done['request']['video_model']==model
            report=service.official_videos.last_report
            assert report['verified_video_model']==model
            assert report['browser_closed'] and report['visibility']['hidden_confirmed']
            assert report['generation_requests']==1 and local_video_page.submissions==1
            assert local_video_page.requests[0]['video_model'].endswith(model.rsplit('_',1)[-1].title())
            for file in done['result']['files']:
                download=await client.get(file['url'])
                assert download.status_code==200 and download.content==VIDEO.read_bytes()
    finally:
        await service.stop()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code',[('missing_veo','video_model_unavailable'),('sticky_model','video_model_not_selected')])
async def test_veo_model_mismatch_never_generates(video_service,local_video_page,mode,code):
    service,cookie,profile=video_service;local_video_page.mode=mode
    with pytest.raises(OfficialVideoError) as err:
        await service.official_videos.run(cookies=str(cookie),prompt='local only',video_model='veo_3_1_fast')
    assert err.value.code==code
    assert local_video_page.submissions==0
    assert service.official_videos.last_report['browser_closed']

@pytest.mark.asyncio
@pytest.mark.parametrize('model',['veo_3_1_lite','veo_3_1_fast','veo_3_1_quality'])
async def test_veo_check_only_no_generation(video_service,local_video_page,model):
    service,cookie,profile=video_service
    report=await service.official_videos.run(cookies=str(cookie),prompt='local only',video_model=model,check_only=True)
    assert report['verified_video_model']==model
    assert report['browser_closed'] and report['generation_requests']==0
    assert local_video_page.submissions==0


@pytest.mark.asyncio
@pytest.mark.parametrize('model,mode',[('veo_3_1_fast','references'),('veo_3_1_quality','frames')])
async def test_veo_materials_preserve_order(video_service,local_video_page,tmp_path,model,mode):
    service,cookie,profile=video_service
    images=[]
    for i in range(4 if mode=='references' else 2):
        path=tmp_path/f'veo-reference-{i}.png'
        path.write_bytes((Path(__file__).parent/'assets/reference-purple.png').read_bytes()+bytes([i]))
        images.append(str(path))
    options={'references':images} if mode=='references' else {'start_image':images[0],'end_image':images[1]}
    await service.official_videos.run(cookies=str(cookie),prompt='local materials only',video_model=model,aspect='9:16',**options)
    report=service.official_videos.last_report
    assert report['parameters']['mode']==mode and report['parameters']['aspect']=='9:16'
    assert report['verified_video_model']==model and report['browser_closed']
    assert local_video_page.submissions==1
    attached=local_video_page.requests[0]['attached']
    assert [attached.index(Path(path).name) for path in images]==sorted(attached.index(Path(path).name) for path in images)


@pytest.mark.asyncio
@pytest.mark.parametrize('layout', ['sidebar','inline'])
async def test_agent_mode_is_closed_before_video_parameters(video_service, local_video_page, layout):
    service,cookie,profile=video_service
    global HTML
    saved=HTML
    try:
        start=HTML.index('<div role="textbox"')
        end=HTML.index('<script>')
        direct=HTML[start:end]
        if layout=='sidebar':
            agent='<aside id="agent"><button aria-label="智能体指令">instructions</button><button aria-label="发起新的会话">new</button><button aria-label="关闭" onclick="fetch(\'/data/batchexecute?rpcids=DA4VGb\',{method:\'POST\'}).then(r=>{if(r.ok){document.querySelector(\'#agent\').remove();document.querySelector(\'#direct\').hidden=false}})">close</button></aside>'
            HTML=HTML[:start]+agent+'<section id="direct" hidden>'+direct+'</section>'+HTML[end:]
        else:
            direct=direct.replace('<button aria-label="设置触发器"','<button hidden aria-label="设置触发器"')
            toggle='<button aria-label="智能体" aria-pressed="true" onclick="this.setAttribute(\'aria-pressed\',\'false\');document.querySelector(\'#instructions\').remove();document.querySelector(\'[aria-label=设置触发器]\').hidden=false">mode</button><button id="instructions" aria-label="智能体指令">instructions</button>'
            HTML=HTML[:start]+'<section>'+toggle+direct+'</section>'+HTML[end:]
        result=await service.official_videos.run(cookies=cookie,check_only=True,prompt='fixture only',duration='4s')
        assert result['parameters']['duration']=='4s' and result['verified_video_model']=='omni_flash'
        assert result['direct_composer_confirmed'] and result['submit_control_actionable']
        assert result['generation_requests']==0 and result['browser_closed']
        assert local_video_page.submissions==0 and not profile_in_use(profile)
    finally:HTML=saved


@pytest.mark.asyncio
async def test_composer_preferences_are_scoped_and_never_allow_generation(tmp_path):
    from types import SimpleNamespace
    service=ConsoleService(FlowEngine(root=tmp_path))
    class Route:
        def __init__(self,rpc):
            self.request=SimpleNamespace(url='https://flow.google.com/data/batchexecute?rpcids='+rpc,method='POST',post_data_buffer=b'')
            self.allowed=False;self.blocked=False
        async def continue_(self):self.allowed=True
        async def abort(self):self.blocked=True
    report={'generation_submitted':False,'generation_requests':0,'blocked_generation_requests':0}
    outside=Route('DA4VGb');await service.official_videos.gate(outside,report,True);assert outside.blocked
    report['composer_preferences_pending']=True
    for rpc in ('DA4VGb','Kcr7Ub'):
        route=Route(rpc);await service.official_videos.gate(route,report,True);assert route.allowed
    for rpc in ('YhhmEf','ogiZ0b','unknown','Kcr7Ub,unknown'):
        route=Route(rpc);await service.official_videos.gate(route,report,True);assert route.blocked
    assert report['generation_requests']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['media_response', 'media_content_response'])
async def test_media_field_response_queue_download_and_hidden_cleanup(video_service, local_video_page, monkeypatch, mode):
    """Real localhost browser + HTTP download; no Google generation."""
    service, cookie, profile = video_service
    local_video_page.mode = mode
    app = FastAPI();app.include_router(create_console_router(service))
    app.add_api_route('/api/v1/media/{filename:path}', api.flow_media, methods=['GET'])
    monkeypatch.setattr(api, 'engine', service.engine)
    service.worker = asyncio.create_task(service.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post('/console/api/jobs', json={
                'kind':'video', 'prompt':'local media response only', 'aspect':'16:9',
                'duration':'4s', 'quality':'720p', 'count':1, 'account_id':'account_fixture'})
            assert result.status_code == 202
            await asyncio.wait_for(service.queue.join(), 120)
            job = (await client.get('/console/api/jobs/' + result.json()['id'])).json()
            assert job['status'] == 'succeeded', job
            report = job['result']['execution']
            expected = CONTENTS[0] if mode == 'media_content_response' else IDS[0]
            assert report['response_result_ids'] == [expected]
            assert report['verified_results'][0]['content_id'] == CONTENTS[0]
            assert report['verified_results'][0]['detail_id'] == IDS[0]
            assert report['download_verified'] and report['result_identity_verified']
            assert report['browser_closed'] and report['visibility']['hidden_confirmed']
            assert not report['visibility']['shown_for_user']
            assert report['generation_requests'] == local_video_page.submissions == 1
            assert report['generation_response']['payload_count'] == 1
            assert local_video_page.downloads == 1
            media = await client.get(job['result']['files'][0]['url'])
            assert media.status_code == 200 and media.content == VIDEO.read_bytes()
    finally:
        await service.stop()


@pytest.fixture(autouse=True)
def short_uncertain_window_for_local_regressions(monkeypatch):
    # Full production 180-second timing is exercised in test_uncertain_result.py.
    monkeypatch.setattr("flow_agent.uncertain_result.UNCERTAIN_WAIT_SECONDS", .1)

