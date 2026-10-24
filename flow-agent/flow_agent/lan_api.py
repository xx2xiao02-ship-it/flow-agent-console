"""Optional, authenticated API for trusted local networks.

This app deliberately has no console, session, browser, or legacy REST routes.
It shares the local console's single job queue in the same process.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .console import ConsoleService, JobInput
from .lan_keys import LanKeyStore
from .task_scheduler import SchedulerInput
from .dispatch_quotes import QuoteInput


def create_lan_app(service: ConsoleService, api_key: str | LanKeyStore) -> FastAPI:
    initial_key = api_key.read() if isinstance(api_key, LanKeyStore) else api_key
    if not initial_key or len(initial_key) < 32:
        raise ValueError("局域网 API 密钥长度不足")
    app = FastAPI(title="Flow Agent LAN API", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def require_key(request: Request, call_next):
        authorization = request.headers.get("authorization", "")
        current_key = api_key.read() if isinstance(api_key, LanKeyStore) else api_key
        expected = f"Bearer {current_key}"
        if not secrets.compare_digest(authorization, expected):
            return JSONResponse({"detail": "需要有效的 API 密钥"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok" if service.startup.snapshot()["ready"] else service.startup.state, "engine": service.engine.available(), "startup": service.startup.snapshot()}

    @app.get("/api/v1/openapi.json", include_in_schema=False)
    async def openapi_schema():
        return app.openapi()

    @app.get("/api/v1/balance")
    async def balance():
        # Cached local balance only. A LAN caller cannot trigger an upstream probe.
        return service.accounts(await service.engine.get_balance())

    @app.get("/api/v1/jobs")
    async def jobs():
        return service.list_jobs()

    @app.get('/api/v1/scheduler')
    async def scheduler_status():
        return service.scheduler.snapshot()

    @app.post('/api/v1/scheduler')
    async def configure_scheduler(data: SchedulerInput):
        return service.scheduler.configure(data)

    @app.get('/api/v1/accounts/preparation')
    async def account_preparation():
        return service.preparation.snapshot()

    @app.post('/api/v1/accounts/prepare-all', status_code=202)
    async def prepare_all_accounts(x_flow_allow_interaction: str | None = Header(default=None)):
        if x_flow_allow_interaction != 'yes':
            raise HTTPException(422, '账号准备可能在本机打开登录或验证页面；请设置 X-Flow-Allow-Interaction: yes')
        return await service.preparation.start_all()

    @app.post('/api/v1/accounts/preparation/stop')
    async def stop_account_preparation():
        return service.preparation.stop_after_current()

    @app.get("/api/v1/jobs/{job_id}")
    async def job(job_id: str):
        return service.get_job(job_id)

    @app.post('/api/v1/dispatch-quotes')
    async def prepare_dispatch_quote(data: QuoteInput):
        return await service.dispatch_quotes.prepare(data)

    @app.get('/api/v1/dispatch-quotes/{quote_id}')
    async def dispatch_quote(quote_id: str):
        return service.dispatch_quotes.get(quote_id)

    @app.post("/api/v1/jobs", status_code=202)
    async def submit(data: JobInput, x_flow_allow_charge: str | None = Header(default=None), x_flow_client: str | None = Header(default=None)):
        if x_flow_allow_charge != "yes":
            raise HTTPException(422, "生成可能消耗 Flow 积分；请设置 X-Flow-Allow-Charge: yes")
        job = service.enqueue(data)
        from .work_log import emit
        emit(service, job['id'], '调用入口：' + ('MCP' if x_flow_client == 'mcp' else 'API'))
        return job

    @app.get("/api/v1/media")
    async def media(search: str = "", kind: str = "all"):
        return service.media(search, kind)

    @app.get("/api/v1/media/{filename}")
    async def media_file(filename: str, storage: str | None = None):
        return FileResponse(service.media_storage.resolve(filename, storage))

    @app.post("/api/v1/uploads/image")
    async def upload_image(file: UploadFile = File(...)):
        result = await service.upload(file)
        result["url"] = result["url"].replace("/console/api/", "/api/v1/")
        return result

    @app.post("/api/v1/uploads/video")
    async def upload_video(file: UploadFile = File(...)):
        result = await service.upload_source_video(file)
        result["url"] = result["url"].replace("/console/api/", "/api/v1/")
        return result

    @app.get("/api/v1/inputs/{name}")
    async def input_file(name: str):
        return service.input_response(name)

    return app
