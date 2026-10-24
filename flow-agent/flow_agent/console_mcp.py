"""MCP stdio adapter for the running Windows console; never starts an engine."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, quote
import httpx
from pydantic import ValidationError
from .console import JobInput
from .dispatch_quotes import QuoteInput

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')

def schema(properties=None, required=()):
    return {'type':'object','properties':properties or {},'required':list(required),'additionalProperties':False}

def tools():
    items=[]
    def add(name, description, inputs, readonly=True):
        items.append({'name':name,'description':description,'inputSchema':inputs,
            'annotations':{'readOnlyHint':readonly,'destructiveHint':False,'idempotentHint':readonly,'openWorldHint':True}})
    add('flow_capabilities','查询接入能力、模型参数及收费边界，不生成。',schema())
    add('flow_accounts','查询账号与缓存积分，不打开浏览器、不生成。',schema())
    add('flow_jobs','查询最近任务；不能代替单个任务的最终状态。',schema())
    add('flow_get_job','查询指定任务状态及成功后的文件信息，不重试。',schema({'job_id':{'type':'string','minLength':1}},['job_id']))
    add('flow_prepare_dispatch_quote','预选账号并无生成检查精确模型参数、实时积分，签发15分钟一次性估算报价；可能打开专用浏览器，不扣生成积分。',QuoteInput.model_json_schema(),False)
    add('flow_get_dispatch_quote','查询原报价及其唯一任务ID，提交超时后只读恢复，不重发。',schema({'quote_id':{'type':'string','minLength':1}},['quote_id']))
    for kind in ('image','video'):
        props=JobInput.model_json_schema()['properties'].copy()
        for key in ('kind','auto_retry_limit','retry_uncertain','retry_switch_accounts'):props.pop(key,None)
        props['model']['default']='beluga'
        add('flow_generate_'+kind,'提交一个'+('图片' if kind=='image' else '视频')+'后台任务，可能扣积分。仅在用户授权后调用；立即返回任务 ID；不自动重试。',
            schema({'job':schema(props,['prompt']), 'acknowledge_possible_charge':{'type':'boolean','const':True}},['job','acknowledge_possible_charge']),False)
    add('flow_upload_reference','上传当前 MCP 所在电脑的本地参考图片/视频；不生成，返回 input ID。',
        schema({'file_path':{'type':'string'},'kind':{'enum':['image','video']}},['file_path','kind']),False)
    add('flow_download_result','将已成功任务中的指定结果保存到当前 MCP 所在电脑；必须提供绝对路径，拒绝覆盖。',
        schema({'job_id':{'type':'string'},'file_index':{'type':'integer','minimum':0,'default':0},'destination':{'type':'string'}},['job_id','destination']),False)
    return items

class AdapterError(Exception):pass

class ConsoleMCP:
    def __init__(self, base_url=None, api_key=None, client=None, read_only=None):
        self.base=(base_url or os.getenv('FLOW_MCP_URL','http://127.0.0.1:8001')).rstrip('/')
        u=urlsplit(self.base)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
            raise ValueError('FLOW_MCP_URL 必须是服务器根地址，不能包含路径或凭据')
        self.key=api_key if api_key is not None else os.getenv('FLOW_MCP_API_KEY','')
        self.local=u.hostname in ('127.0.0.1','localhost','::1') and not self.key
        if not self.local and not self.key:raise ValueError('远程 API 必须配置 FLOW_MCP_API_KEY')
        if self.key != self.key.strip():raise ValueError('API 密钥首尾不能有空白')
        self.prefix='/console/api' if self.local else '/api/v1'
        self.client=client
        self.read_only=read_only if read_only is not None else os.getenv('FLOW_MCP_READ_ONLY','0')=='1'

    async def request(self,method,path,*,media=False,**kwargs):
        prefix='/api/v1' if media else self.prefix
        headers={'Authorization':'Bearer '+self.key} if self.key else {}
        headers['X-Flow-Client'] = 'mcp'
        headers.update(kwargs.pop('headers',{}))
        try:
            if self.client:
                r=await self.client.request(method,self.base+prefix+path,headers=headers,**kwargs)
            else:
                async with httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False) as c:
                    r=await c.request(method,self.base+prefix+path,headers=headers,**kwargs)
        except httpx.HTTPError:
            raise AdapterError('后台连接失败或响应超时。若为提交请求，结果可能不确定；先查询任务，不要重新提交。') from None
        if not r.is_success:
            if r.status_code == 409:
                try:
                    detail = r.json().get('detail')
                    if isinstance(detail, str) and detail.startswith('账号 ') and '本次未提交' in detail:
                        raise AdapterError(detail)
                except (ValueError, AttributeError):
                    pass
            raise AdapterError(f'后台返回 HTTP {r.status_code}。未自动重试；提交状态不确定时先查询任务。')
        return r

    async def call(self,name,args):
        spec=next((t for t in tools() if t['name']==name),None)
        if not spec:raise AdapterError('未知工具')
        if not isinstance(args,dict) or set(args)-set(spec['inputSchema']['properties']):raise AdapterError('工具参数格式错误或含未知字段')
        if any(k not in args for k in spec['inputSchema']['required']):raise AdapterError('缺少必填参数')
        if name=='flow_capabilities':
            return {'transport':'stdio','backend':self.base,'read_only':self.read_only,
                'image_default':'beluga','image_count':1,'job_schema':JobInput.model_json_schema(),
                'automatic_retries':0,'generation_requires_explicit_authorization':True,
                'note':'任务共享后台队列。真实生成沿用后台浏览器策略；不绕过登录或人工验证。'}
        if name=='flow_accounts':return (await self.request('GET','/accounts' if self.local else '/balance')).json()
        if name=='flow_jobs':return (await self.request('GET','/jobs')).json()
        if name=='flow_get_job':return await self.job(args['job_id'])
        if name=='flow_prepare_dispatch_quote':
            if self.read_only:raise AdapterError('只读模式不允许预选账号或打开检查浏览器')
            data=QuoteInput.model_validate(args)
            return (await self.request('POST','/dispatch-quotes',json=data.model_dump())).json()
        if name=='flow_get_dispatch_quote':
            return (await self.request('GET','/dispatch-quotes/'+quote(args['quote_id'],safe=''))).json()
        if name.startswith('flow_generate_'):
            if self.read_only or args.get('acknowledge_possible_charge') is not True:raise AdapterError('生成被阻止：需要用户明确授权扣积分，且不能处于只读模式')
            raw=args['job']
            allowed=set(JobInput.model_fields)-{'kind','auto_retry_limit','retry_uncertain','retry_switch_accounts'}
            if not isinstance(raw,dict) or set(raw)-allowed:raise AdapterError('任务参数含不支持字段；禁止自动重试')
            data=JobInput.model_validate({**raw,'kind':name.removeprefix('flow_generate_'),'model':raw.get('model','beluga'),'auto_retry_limit':0,'retry_uncertain':False})
            return (await self.request('POST','/jobs',json=data.model_dump(),headers={'X-Flow-Allow-Charge':'yes'})).json()
        if name=='flow_upload_reference':
            if self.read_only:raise AdapterError('只读模式不允许上传')
            kind=args['kind']
            if kind not in ('image','video'):raise AdapterError('素材类型必须为 image 或 video')
            p=Path(args['file_path'])
            if not p.is_absolute() or not p.is_file() or p.stat().st_size>250*1024*1024:raise AdapterError('素材必须为存在的绝对文件路径，且不超过250MB')
            with p.open('rb') as f:r=await self.request('POST','/uploads/'+kind,files={'file':(p.name,f)})
            return r.json()
        if name=='flow_download_result':
            if self.read_only:raise AdapterError('只读模式不允许写入下载文件')
            j=await self.job(args['job_id'])
            if j.get('status')!='succeeded':raise AdapterError('仅可下载 succeeded 任务，不会重试生成')
            files=(j.get('result') or {}).get('files') or []
            i=args.get('file_index',0)
            if type(i) is not int or not 0<=i<len(files):raise AdapterError('结果文件序号不存在')
            url=files[i].get('url','');u=urlsplit(url)
            prefix=next((x for x in ('/console/api','/api/v1') if u.path.startswith(x+'/media/')),None)
            if u.scheme or u.netloc or prefix is None or '..' in u.path.split('/'):
                raise AdapterError('结果文件地址不属于后台素材接口')
            p=Path(args['destination'])
            if not p.is_absolute() or not p.parent.is_dir() or p.exists():raise AdapterError('下载需要父目录已存在的绝对路径，且文件不能已存在')
            r=await self.request('GET',url[len(prefix):],media=True)
            if not r.content:raise AdapterError('结果文件为空')
            with p.open('xb') as f:f.write(r.content)
            return {'path':str(p),'bytes':len(r.content),'job_id':args['job_id']}
        raise AdapterError('未实现的工具')

    async def job(self,job_id):
        if not isinstance(job_id,str) or not job_id or len(job_id)>128:raise AdapterError('任务ID无效')
        return (await self.request('GET','/jobs/'+quote(job_id,safe=''))).json()

    async def handle(self,raw):
        try:m=json.loads(raw)
        except ValueError:return json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'JSON解析失败'}})
        if not isinstance(m,dict) or m.get('jsonrpc')!='2.0':return json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'请求格式错误'}})
        if 'id' not in m:return None
        rid=m['id']; method=m.get('method');p=m.get('params',{})
        def ok(r):return json.dumps({'jsonrpc':'2.0','id':rid,'result':r},ensure_ascii=False)
        def fail(code,msg):return json.dumps({'jsonrpc':'2.0','id':rid,'error':{'code':code,'message':msg}},ensure_ascii=False)
        if not isinstance(p,dict):return fail(-32602,'params必须是对象')
        if method=='initialize':return ok({'protocolVersion':p.get('protocolVersion') if p.get('protocolVersion') in VERSIONS else VERSIONS[0], 'capabilities':{'tools':{}},'serverInfo':{'name':'flow-console','version':'1.0.1'},'instructions':'先查询能力和账号。生成前取得用户收费授权；提交一次后只查询原任务，失败或不确定时不要自动重提。'})
        if method=='ping':return ok({})
        if method=='tools/list':return ok({'tools':tools()})
        if method!='tools/call':return fail(-32601,'未知方法')
        try:
            result=await self.call(p.get('name'),p.get('arguments',{}))
            return ok({'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'isError':False})
        except (AdapterError,ValidationError,ValueError,TypeError,OSError) as e:
            msg=str(e) if isinstance(e,AdapterError) else '参数无效或本地文件无法访问；未自动重试。'
            return ok({'content':[{'type':'text','text':msg}],'isError':True})

async def run_stdio():
    # MCP stdio is UTF-8 even under a Chinese Windows console code page.
    if hasattr(sys.stdin, 'reconfigure'):sys.stdin.reconfigure(encoding='utf-8')
    if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    adapter=ConsoleMCP();loop=asyncio.get_running_loop()
    while True:
        line=await loop.run_in_executor(None,sys.stdin.readline)
        if not line:break
        response=await adapter.handle(line)
        if response is not None:
            sys.stdout.write(response+'\n');sys.stdout.flush()

def main():
    try:
        asyncio.run(run_stdio())
    except ValueError:
        print('[FLOW_MCP_CONFIG_INVALID] MCP配置无效，请检查服务地址、密钥空白和环境变量；查询不受只读开关限制。', file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    except (BrokenPipeError, ConnectionResetError):
        print('[FLOW_MCP_PIPE_CLOSED] AI客户端已关闭标准输入输出连接；请在该客户端重新加载此MCP连接。', file=sys.stderr, flush=True)
        raise SystemExit(3) from None

if __name__=='__main__':main()
