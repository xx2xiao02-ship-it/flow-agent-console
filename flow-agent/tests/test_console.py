"""Console contract tests. No upstream calls or credits are used."""

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from flow_agent import api as base_api
from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine

COMPLETE_SESSION = json.dumps({"cookies": [{"name": "test", "value": "PRIVATE"}],
                               "at": "PRIVATE", "fsid": "PRIVATE", "fingerprint": {"userAgent": "test"}})


class FakeEngine(FlowEngine):
    def __init__(self, root):
        super().__init__(root=root)
        self.calls = []
        self.balance_accounts = []

    async def generate_video(self, **kwargs):
        self.calls.append(kwargs)
        path = self.output_dir / "finished.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return {"status": "succeeded", "job_id": "engine-1", "files": [{"path": str(path)}]}

    async def edit_video(self, **kwargs):
        return await self.generate_video(**kwargs)

    async def get_balance(self, refresh=False):
        return {"accounts": self.balance_accounts, "total_credits": 0, "unread": 0}

    async def get_stats(self):
        return {"generations": [], "totals": {}}


@pytest.fixture
def service(tmp_path):
    # The extension lives next to the engine root. Keep that sibling inside
    # this test's directory rather than sharing it with every pytest case.
    engine = FakeEngine(tmp_path / 'flow-agent')
    svc = ConsoleService(engine)
    svc.initialize()
    return svc


def test_media_rights_are_content_scoped_persistent_and_explicit(service, tmp_path):
    service.upload_dir.mkdir(parents=True, exist_ok=True)
    first = service.upload_dir / 'original.png'
    first.write_bytes(b'original owner image')
    renamed = tmp_path / 'renamed.png'
    renamed.write_bytes(first.read_bytes())
    assert not service.media_rights_authorized(first)
    # Enqueue only: no worker or provider submission is started by this test.
    service.cookies_dir.mkdir(exist_ok=True)
    (service.cookies_dir / 'account_a.json').write_text(COMPLETE_SESSION)
    data = JobInput(kind='video', prompt='fixture', account_id='account_a',
        start_image_id='upload:original.png')
    service.enqueue(data)
    assert not service.media_rights_authorized(first)
    confirmed = data.model_copy(update={'media_rights_confirmed': True})
    job = service.enqueue(confirmed)
    assert job['request']['media_rights_confirmed']
    restarted = ConsoleService(service.engine)
    assert restarted.media_rights_authorized(first)
    assert restarted.media_rights_authorized(renamed)
    service.remember_uploaded_image('account_a', 'project_a', first)
    assert restarted.known_uploaded_image('account_a', 'project_a', renamed) == first.name
    assert restarted.known_uploaded_image('other_account', 'project_a', first) is None
    assert restarted.known_uploaded_image('account_a', 'other_project', first) is None
    first.write_bytes(b'different contents at the same path')
    assert not restarted.media_rights_authorized(first)
    assert restarted.known_uploaded_image('account_a', 'project_a', first) is None


@pytest.mark.asyncio
async def test_queue_invokes_real_engine_interface_serially(service,monkeypatch):
    # Queue contract fixture; production identity checks are covered separately.
    monkeypatch.setattr('flow_agent.account_scheduling.browser_identity_ready',lambda *args:True)
    service.cookies_dir.mkdir()
    (service.cookies_dir / "account_a.json").write_text(COMPLETE_SESSION)
    service.engine.balance_accounts = [{"account_id": "acct-a", "credits": 100, "status": "active"}]
    job = service.enqueue(JobInput(kind="video", prompt="clouds", aspect="9:16"))
    assert job["status"] == "queued"
    service.worker = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(service.queue.join(), 1)
        done = service.get_job(job["id"])
        assert done["status"] == "succeeded"
        assert done["result"]["files"] == [{"name": "finished.mp4", "url": "/api/v1/media/finished.mp4"}]
        assert service.engine.calls[0]["aspect"] == "9:16"
        assert service.engine.calls[0]["cookies"].endswith("account_a.json")
        assert done["request"]["selected_account_id"] == "account_a"
        assert "cookies" not in json.dumps(done)
    finally:
        await service.stop()


def test_cancel_and_explicit_retry(service):
    job = service.enqueue(JobInput(kind="image", prompt="tree"))
    assert service.cancel(job["id"])["status"] == "canceled"
    with pytest.raises(Exception) as denied:
        service.retry(job["id"], True)
    assert getattr(denied.value, "status_code", None) == 409
    with service.connect() as db:
        db.execute("update console_jobs set status='failed' where id=?", (job["id"],))
    with pytest.raises(Exception) as denied:
        service.retry(job["id"], False)
    assert getattr(denied.value, "status_code", None) == 422
    retry = service.retry(job["id"], True)
    assert retry["retry_of"] == job["id"]
    assert retry["status"] == "queued"


def test_installer_browser_is_used_without_playwright_cache(service, monkeypatch):
    monkeypatch.delenv("FLOW_AGENT_BROWSER", raising=False)
    monkeypatch.setenv("FLOW_AGENT_ROOT", str(service.engine.root))
    browser = service.engine.root.parent / "browser" / "chrome-win64" / "chrome.exe"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"browser")
    assert service._find_extension_browser() == browser


def test_restart_never_auto_resubmits(service):
    job = service.enqueue(JobInput(kind="video", prompt="clouds"))
    service.initialize()
    assert service.get_job(job["id"])["status"] == "interrupted"


@pytest.mark.asyncio
async def test_auto_account_scheduler_is_serial_and_uses_cached_credits(service):
    service.cookies_dir.mkdir()
    for key in ("a", "b", "empty"):
        (service.cookies_dir / f"account_{key}.json").write_text(COMPLETE_SESSION)
    service.engine.balance_accounts = [
        {"account_id": "acct-a", "credits": 2, "status": "active"},
        {"account_id": "acct-b", "credits": 8, "status": "active"},
        {"account_id": "acct-empty", "credits": 0, "status": "active"},
    ]
    first = await service._scheduled_cookie()
    second = await service._scheduled_cookie()
    assert first.endswith("account_a.json")
    assert second.endswith("account_b.json")
    assert await service._scheduled_cookie() == first


def test_account_cookie_path_stays_on_server_and_media_is_scoped(service):
    cookie = service.cookies_dir / "account_demo.json"
    cookie.parent.mkdir(parents=True)
    cookie.write_text("SECRET_COOKIE", encoding="utf-8")
    service.engine.output_dir.mkdir()
    (service.engine.output_dir / "picture.png").write_bytes(b"png")
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        response = client.get("/console/api/accounts")
        assert response.status_code == 200
        assert "SECRET_COOKIE" not in response.text
        assert "cookies" not in response.text
        assert response.json()["accounts"][0]["id"] == "account_demo"
        assert client.post("/console/api/jobs", json={"kind": "video", "prompt": "x", "account_id": "../../other"}).status_code == 422
        assert client.get("/console/api/media").json()[0]["url"] == "/api/v1/media/picture.png"


@pytest.mark.asyncio
async def test_upload_validates_image_signature_and_path(service):
    bad = UploadFile(file=BytesIO(b"not an image"), filename="bad.png")
    with pytest.raises(Exception) as denied:
        await service.upload(bad)
    assert getattr(denied.value, "status_code", None) == 422
    good = UploadFile(file=BytesIO(b"\x89PNG\r\n\x1a\nrest"), filename="frame.png")
    image = await service.upload(good)
    assert service._input_image(image["image_id"]).endswith(".png")
    with pytest.raises(Exception):
        service._input_image("media:../private.png")


def test_history_search_and_pagination(service):
    service.engine.db_path.parent.mkdir(exist_ok=True)
    with sqlite3.connect(service.engine.db_path) as db:
        db.execute("""create table generations (id integer primary key, job_id text,
            account_id text, kind text, model text, aspect text, status text,
            credits_spent integer, elapsed_ms integer, created_at text)""")
        db.executemany("""insert into generations values (?,?,?,?,?,?,?,?,?,?)""", [
            (1, "job-a", "acct-a", "video", "v", "16:9", "succeeded", 10, 1000, "2026-01-01"),
            (2, "job-b", "acct-b", "image", "i", "1:1", "failed", None, 2000, "2026-01-02"),
        ])
    result = service.history(search="acct-a", kind="video", limit=1)
    assert result["total"] == 1
    assert result["generations"][0]["job_id"] == "job-a"
    assert service.history(limit=1, offset=1)["generations"][0]["job_id"] == "job-a"
    assert service.history(search="%' OR 1=1 --")["total"] == 0


def test_local_browser_origin_guard_keeps_direct_rest_calls():
    client = TestClient(base_api.app)
    assert client.get("/health", headers={"host": "example.com"}).status_code == 200
    assert client.get("/health", headers={"origin": "https://example.com"}).status_code == 403


@pytest.mark.asyncio
async def test_account_connection_starts_only_local_bridge_and_stops_owned_process(service, monkeypatch):
    service.engine.binary.parent.mkdir(parents=True, exist_ok=True)
    service.engine.binary.write_bytes(b"test binary placeholder")
    port_open = False
    monkeypatch.setattr(service, "_bridge_port_open", lambda: port_open)
    spawned = []

    class FakeProcess:
        returncode = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def fake_spawn(*args, **kwargs):
        nonlocal port_open
        spawned.append((args, kwargs))
        port_open = True
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    try:
        result = await service.start_bridge()
        assert result["state"] == "running"
        assert result["extension_available"] is False
        assert "cookie" not in str(result).lower()
        assert spawned[0][0] == (str(service.engine.binary), "bridge")
        assert spawned[0][1]["cwd"] == str(service.engine.root)
    finally:
        await service.stop()
    assert service.bridge_process is None


@pytest.mark.asyncio
async def test_connect_account_opens_isolated_chrome_with_extension(tmp_path, monkeypatch):
    svc = ConsoleService(FakeEngine(tmp_path / "flow-agent"))
    svc.initialize()
    extension = tmp_path / "flow-extension"
    extension.mkdir()
    (extension / "manifest.json").write_text("{}")
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"placeholder")
    monkeypatch.setattr(svc, "_find_extension_browser", lambda: chrome)
    bridge_calls = []

    async def fake_bridge():
        bridge_calls.append(True)

    launches = []
    monkeypatch.setattr(svc, "start_bridge", fake_bridge)
    monkeypatch.setattr("flow_agent.console.subprocess.Popen",
                        lambda args, **kwargs: launches.append((args, kwargs)))
    result = await svc.connect_account()
    assert result == {"state": "awaiting_login"}
    assert len(bridge_calls) == 1
    args, options = launches[0]
    assert args[0] == str(chrome)
    assert f"--load-extension={extension}" in args
    assert f"--user-data-dir={svc.engine.root / 'data' / 'flow_browser_profile_1'}" in args
    assert args[-1] == "https://labs.google/fx/tools/flow"
    assert options["cwd"] == str(svc.engine.root)


def test_local_profile_scan_returns_metadata_only(service, tmp_path, monkeypatch):
    root = tmp_path / "Chrome" / "User Data"
    profile = root / "Profile 1"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text(json.dumps({
        "account_info": [{"email": "test@gmail.com", "full_name": "测试", "token": "SECRET"}],
        "profile": {"name": "本机账号"}, "private": "SECRET"}), encoding="utf-8")
    (profile / "Cookies").write_text("SECRET_COOKIE")
    (root / "Default").mkdir()
    (root / "Default" / "Preferences").write_text("[]")
    (root / "Invalid").mkdir()
    (root / "Invalid" / "Preferences").write_text('{"account_info":[{"email":"other@test.com"}]}')
    monkeypatch.setattr(service, "_local_chrome_root", lambda: root)
    items = service.local_profiles()
    assert len(items) == 1
    assert items[0]["emails"] == ["test@gmail.com"]
    assert items[0]["label"] == "本机账号"
    assert len(items[0]["id"]) == 24
    assert all(s not in str(items) for s in ("SECRET", str(root), "directory", "Cookie"))


@pytest.mark.asyncio
async def test_local_profile_uses_original_chrome_without_copying_sessions(service, tmp_path, monkeypatch):
    root = tmp_path / "User Data"
    profile = root / "Default"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text('{"account_info":[{"email":"test@gmail.com"}]}')
    monkeypatch.setattr(service, "_local_chrome_root", lambda: root)
    monkeypatch.setattr(service, "_find_local_chrome", lambda: tmp_path / "chrome.exe")
    async def fake_bridge():
        return None
    monkeypatch.setattr(service, "start_bridge", fake_bridge)
    launches = []
    monkeypatch.setattr("flow_agent.console.subprocess.Popen", lambda args, **kw: launches.append(args))
    result = await service.open_local_profile(service.local_profiles()[0]["id"])
    assert result["state"] == "awaiting_extension"
    assert f"--user-data-dir={root}" in launches[0]
    assert "--profile-directory=Default" in launches[0]
    assert "chrome://extensions/" in launches[0]
    assert not any("load-extension" in a for a in launches[0])
    assert not service._cookie_files()
    with pytest.raises(Exception) as error:
        await service.open_local_profile("../invalid-profile")
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('auto_close', [True, False])
async def test_existing_normal_chrome_preparation_tracks_only_its_launch_without_setup_tab(service, tmp_path, monkeypatch, auto_close):
    root = tmp_path / 'User Data'
    (root / 'Profile 6').mkdir(parents=True)
    (root / 'Profile 6' / 'Preferences').write_text('{"account_info":[{"email":"test@gmail.com"}]}')
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_a.json').write_text(COMPLETE_SESSION)
    monkeypatch.setattr(service, '_local_chrome_root', lambda: root)
    monkeypatch.setattr(service, '_find_local_chrome', lambda: tmp_path / 'chrome.exe')
    monkeypatch.setattr(service, '_verified_detail', lambda _: {'email': 'test@gmail.com'})
    async def bridge(): pass
    monkeypatch.setattr(service, 'start_bridge', bridge)
    launches = []
    handle = object()
    def launch(args, **kwargs):
        launches.append(args)
        return handle
    monkeypatch.setattr('flow_agent.console.subprocess.Popen', launch)
    service.browser_pages.configure(auto_close)
    lease = service.browser_pages.begin('account_a')
    await service.open_account_browser('account_a')
    assert '--profile-directory=Profile 6' in launches[0]
    assert 'chrome://extensions/' not in launches[0]
    assert len([a for a in launches[0] if a.startswith('https://')]) == 1
    if auto_close:
        assert lease.token in launches[0][-1] and lease.process is handle
        assert not service.browser_pages.pending
    else:
        assert launches[0][-1] == 'https://labs.google/fx/tools/flow'
    # Manual open after preparation must not reuse the preparation's close lease.
    await service.open_account_browser('account_a')
    assert launches[1][-1] == 'https://labs.google/fx/tools/flow'
    assert not service.list_jobs()


def test_local_profile_routes_and_extension_folder(service, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_local_chrome_root", lambda: tmp_path / "missing")
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        assert client.get("/console/api/accounts/local-profiles").json() == []
        assert client.post("/console/api/accounts/local-profiles/open", json={"profile_id": "../"}).status_code == 422
        assert client.post("/console/api/accounts/extension-folder").status_code == 503


@pytest.mark.asyncio
async def test_account_sync_reconnects_bridge_after_login_without_jobs(service, monkeypatch):
    class FakeProcess:
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        async def wait(self):
            return self.returncode

    process = FakeProcess()
    service.bridge_process = process
    async def no_delay(_seconds):
        return None
    async def fake_start_bridge():
        service.cookies_dir.mkdir()
        (service.cookies_dir / "account_new.json").write_text(COMPLETE_SESSION)

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    monkeypatch.setattr(service, "start_bridge", fake_start_bridge)
    await service._watch_account_sync(set(), "flow_browser_profile_1")
    assert process.terminated
    assert len(service._cookie_files()) == 1
    with service.connect() as db:
        assert db.execute("select profile_name from console_account_profiles where account_key='account_new'").fetchone()[0] == "flow_browser_profile_1"


@pytest.mark.asyncio
async def test_new_account_waits_for_complete_sync_and_keeps_old_records(service, monkeypatch):
    service.cookies_dir.mkdir()
    historic = service.cookies_dir / 'account_old.json'
    historic.write_text('{"cookies":[{"name":"old","value":"PRIVATE"}]}')
    original = historic.read_bytes()
    current = service.cookies_dir / 'account_new.json'
    ticks = []
    restarts = []

    class Process:
        returncode = None
        def terminate(self): self.returncode = 0
        async def wait(self): return self.returncode

    service.bridge_process = Process()
    async def tick(_seconds):
        ticks.append(1)
        # The bridge first saves a partial record while the page is loading.
        if len(ticks) == 1:
            current.write_text('{"cookies":[{"name":"new","value":"PRIVATE"}]}')
        else:
            current.write_text(COMPLETE_SESSION)
    async def reconnect():
        restarts.append(1)
        service.bridge_process = Process()
    monkeypatch.setattr(asyncio, 'sleep', tick)
    monkeypatch.setattr(service, 'start_bridge', reconnect)
    await service._watch_account_sync({'account_old'}, 'flow_browser_profile_9')
    assert len(ticks) == 2 and len(restarts) == 1
    assert historic.read_bytes() == original
    assert service._complete_session(current)
    with service.connect() as db:
        rows = list(db.execute('select * from console_account_profiles'))
        assert len(rows) == 1
        assert rows[0]['account_key'] == 'account_new'
        assert rows[0]['profile_name'] == 'flow_browser_profile_9'
    assert not service.list_jobs() and not service.engine.calls


@pytest.mark.asyncio
async def test_account_sync_does_not_restart_during_queued_job(service, monkeypatch):
    class FakeProcess:
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True

    process = FakeProcess()
    service.bridge_process = process
    service.enqueue(JobInput(kind="image", prompt="tree"))
    async def no_delay(_seconds):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await service._watch_account_sync(set(), "flow_browser_profile_1")
    assert not process.terminated


def test_account_display_name_uses_only_unambiguous_browser_profile(service):
    service.cookies_dir.mkdir()
    (service.cookies_dir / "account_demo.json").write_text("SECRET_COOKIE")
    prefs = service.engine.root / "data" / "flow_browser_profile_1" / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text('{"account_info":[{"full_name":"示例账号","email":"example@gmail.com"}]}', encoding="utf-8")
    account = service.accounts({"accounts": [], "total_credits": 0, "unread": 0})["accounts"][0]
    assert account["label"] == "示例账号"
    assert account["email"] == "example@gmail.com"
    assert "SECRET_COOKIE" not in str(account)
    prefs.write_text('{"account_info":[{"email":"a@gmail.com"},{"email":"b@gmail.com"}]}', encoding="utf-8")
    assert service.accounts({"accounts": []})["accounts"][0]["label"] == "未完成连接的账号"


def test_rpc_metadata_is_bound_to_exact_session_and_survives_restart(service):
    service.cookies_dir.mkdir()
    path = service.cookies_dir / "account_normal.json"
    path.write_text(COMPLETE_SESSION)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    service.engine.last_balance_refresh = {
        "bindings": {}, "refreshed": [], "digests": {path.stem: digest},
        "details": {path.stem: {"name": "孙镜怡", "email": "user@gmail.com", "credits": 0, "error": None}}}
    service.record_balance_bindings()
    report = ConsoleService(service.engine).accounts({"accounts": []})
    assert report["accounts"][0]["label"] == "孙镜怡"
    assert report["accounts"][0]["email"] == "user@gmail.com"
    assert report["accounts"][0]["credits"] == 0
    assert "PRIVATE" not in str(report)
    path.write_text(COMPLETE_SESSION + " ")
    replaced = service.accounts({"accounts": []})["accounts"][0]
    assert replaced["credits"] is None
    assert replaced["email"] is None


def test_rpc_balance_failure_clears_old_snapshot_instead_of_reusing_it(service):
    service.cookies_dir.mkdir()
    path = service.cookies_dir / "account_a.json"
    path.write_text(COMPLETE_SESSION)
    evidence = {"bindings": {path.stem: "acct-a"}, "refreshed": [],
                "digests": {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()},
                "details": {path.stem: {"name": "本人", "email": "me@gmail.com", "credits": 1050, "error": None}}}
    service.engine.last_balance_refresh = evidence
    service.record_balance_bindings()
    evidence["details"][path.stem]["credits"] = None
    evidence["details"][path.stem]["error"] = "network"
    service.record_balance_bindings()
    account = service.accounts({"accounts": [{"account_id": "acct-a", "credits": 1050, "status": "active"}]})["accounts"][0]
    assert account["credits"] is None
    assert account["read_error"] == "network"


def test_single_saved_session_matches_engine_account_without_misreporting_unknown_balance(service):
    service.cookies_dir.mkdir()
    (service.cookies_dir / "account_filekey.json").write_text(COMPLETE_SESSION)
    report = service.accounts({"accounts": [
        {"account_id": "acct-differentkey", "credits": None, "status": "active"}
    ], "total_credits": 0, "unread": 1})
    assert len(report["accounts"]) == 1
    assert report["accounts"][0]["selectable"] is True
    assert report["accounts"][0]["credits"] is None
    assert report["total_credits"] is None
    assert report["unread"] == 1
    assert report["last_refresh_at"] is None


def test_unmatched_historical_balance_rows_are_not_rendered_as_extra_accounts(service):
    service.cookies_dir.mkdir()
    (service.cookies_dir / "account_current_a.json").write_text("{}")
    (service.cookies_dir / "account_current_b.json").write_text("{}")
    report = service.accounts({"accounts": [
        {"account_id": "acct-old-a", "credits": 1050, "status": "active"},
        {"account_id": "acct-old-b", "credits": 50, "status": "active"},
    ]})
    assert [a["id"] for a in report["accounts"]] == [
        "account_current_a", "account_current_b"
    ]
    assert all(a["credits"] is None for a in report["accounts"])
    assert report["total_credits"] is None
    assert report["stale_balance_rows"] == 2


def test_refresh_does_not_claim_success_when_engine_balance_is_unread(service):
    service.engine.balance_accounts = [
        {"account_id": "acct-a", "credits": None, "status": "active"}
    ]
    async def unread_balance(refresh=False):
        return {"accounts": service.engine.balance_accounts, "total_credits": 0, "unread": 1}
    service.engine.get_balance = unread_balance
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        response = client.post("/console/api/accounts/refresh")
        assert response.status_code == 502
        assert "尚未返回完整积分" in response.json()["detail"]
    with service.connect() as db:
        assert db.execute("select value from console_meta where key='last_successful_balance_refresh'").fetchone() is None


def test_refresh_rejects_old_balance_without_current_account_mapping(service):
    service.cookies_dir.mkdir()
    (service.cookies_dir / "account_new.json").write_text(COMPLETE_SESSION)
    service.engine.balance_accounts = [{"account_id": "acct-old", "credits": 1050, "status": "active"}]
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        assert client.post("/console/api/accounts/refresh").status_code == 502
        report = client.get("/console/api/accounts").json()
        assert report["last_refresh_at"] is None
        assert report["refresh_error"]
        assert report["accounts"][0]["credits"] is None


def test_engine_confirmed_mapping_survives_restart_but_not_session_replacement(service):
    service.cookies_dir.mkdir()
    path = service.cookies_dir / "account_filehash.json"
    path.write_text(COMPLETE_SESSION)
    service.engine.balance_accounts = [{"account_id": "acct-corehash", "credits": 0, "status": "active"},
                                       {"account_id": "acct-history", "credits": 999, "status": "active"}]
    service.engine.last_balance_refresh = {
        "bindings": {path.stem: "acct-corehash"}, "refreshed": ["acct-corehash"],
        "digests": {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()}, "reason": "unread"}
    service.record_balance_bindings()
    restarted = ConsoleService(service.engine)
    report = restarted.accounts({"accounts": service.engine.balance_accounts})
    assert report["accounts"][0]["credits"] == 0
    assert report["total_credits"] == 0
    assert report["stale_balance_rows"] == 1
    assert "PRIVATE" not in str(report)
    path.write_text(COMPLETE_SESSION + " ")
    assert restarted.accounts({"accounts": service.engine.balance_accounts})["accounts"][0]["credits"] is None


def test_successful_current_refresh_ignores_incomplete_session_and_clears_previous_error(service):
    service.cookies_dir.mkdir()
    path = service.cookies_dir / "account_filehash.json"
    path.write_text(COMPLETE_SESSION)
    (service.cookies_dir / "account_partial.json").write_text('{}')
    service.engine.balance_accounts = [{"account_id": "acct-corehash", "credits": 50, "status": "active"}]
    service.engine.last_balance_refresh = {
        "bindings": {path.stem: "acct-corehash"}, "refreshed": ["acct-corehash"],
        "digests": {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()}, "reason": "unread"}
    with service.connect() as db:
        db.execute("insert into console_meta values ('balance_refresh_error', 'old error')")
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        response = client.post("/console/api/accounts/refresh")
        assert response.status_code == 200
        report = response.json()
        assert report["total_credits"] == 50
        assert report["last_refresh_at"]
        assert report["refresh_error"] is None
        partial = next(a for a in report["accounts"] if a["id"] == "account_partial")
        assert partial["selectable"] is False
        assert partial["session"] == "incomplete"


def test_failed_refresh_does_not_show_old_credits_as_new(service):
    service.cookies_dir.mkdir()
    path = service.cookies_dir / "account_filehash.json"
    path.write_text(COMPLETE_SESSION)
    service.engine.balance_accounts = [{"account_id": "acct-corehash", "credits": 1050, "status": "active"}]
    service.engine.last_balance_refresh = {
        "bindings": {path.stem: "acct-corehash"}, "refreshed": [],
        "digests": {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()}, "reason": "network"}
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        response = client.post("/console/api/accounts/refresh")
        assert response.status_code == 502
        assert "网络失败" in response.json()["detail"]
        assert client.get("/console/api/accounts").json()["total_credits"] is None


@pytest.mark.asyncio
async def test_open_saved_account_reuses_its_profile(service, tmp_path, monkeypatch):
    service.cookies_dir.mkdir()
    (service.cookies_dir / "account_saved.json").write_text("{}")
    profile = service.engine.root / "data" / "flow_browser_profile_1"
    profile.mkdir(parents=True)
    with service.connect() as db:
        db.execute("insert into console_account_profiles values (?, ?)",
                   ("account_saved", profile.name))
    extension = service.engine.root.parent / "flow-extension"
    extension.mkdir()
    (extension / "manifest.json").write_text("{}")
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"browser")
    monkeypatch.setattr(service, "_find_extension_browser", lambda: chrome)
    async def fake_bridge():
        return {"state": "running"}
    monkeypatch.setattr(service, "start_bridge", fake_bridge)
    launches = []
    monkeypatch.setattr("flow_agent.console.subprocess.Popen",
                        lambda args, **kwargs: launches.append(args))
    assert (await service.open_account_browser("account_saved"))["state"] == "opened"
    assert f"--user-data-dir={profile}" in launches[0]
    assert launches[0][-1] == "https://labs.google/fx/tools/flow"
    with pytest.raises(Exception) as denied:
        await service.open_account_browser("account_other")
    assert getattr(denied.value, "status_code", None) == 404


def test_extension_browser_discovery_uses_chrome_for_testing(tmp_path, monkeypatch):
    browser = tmp_path / "ms-playwright" / "chromium-1234" / "chrome-win64" / "chrome.exe"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"placeholder")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("FLOW_AGENT_BROWSER", raising=False)
    assert ConsoleService._find_extension_browser() == browser


@pytest.mark.asyncio
async def test_credit_strategies_filter_cost_unknown_and_incomplete_sessions(service, monkeypatch):
    # Identity/profile eligibility is covered by test_account_scheduling_policy.
    monkeypatch.setattr('flow_agent.account_scheduling.browser_identity_ready', lambda service, account: True)
    service.cookies_dir.mkdir()
    for key in ('a', 'b', 'short', 'unknown'):
        (service.cookies_dir / f'account_{key}.json').write_text(COMPLETE_SESSION)
    (service.cookies_dir / 'account_incomplete.json').write_text('{}')
    service.engine.balance_accounts = [
        {'account_id': 'acct-a', 'credits': 30, 'status': 'active'},
        {'account_id': 'acct-b', 'credits': 100, 'status': 'active'},
        {'account_id': 'acct-short', 'credits': 23, 'status': 'active'},
        {'account_id': 'acct-incomplete', 'credits': 999, 'status': 'active'},
    ]
    # Two 8s/720p renders need an estimated 24 credits, not merely > 0.
    data = JobInput(kind='video', prompt='x', count=2, account_strategy='lowest_credits')
    assert (await service._scheduled_cookie(data)).endswith('account_a.json')
    data.account_strategy = 'highest_credits'
    assert (await service._scheduled_cookie(data)).endswith('account_b.json')
    data.account_strategy = 'round_robin'
    assert (await service._scheduled_cookie(data)).endswith('account_a.json')
    assert (await service._scheduled_cookie(data)).endswith('account_b.json')
    data.count = 4
    data.duration = '10s'
    assert (await service._scheduled_cookie(data)).endswith('account_b.json')
    service.engine.balance_accounts = []
    with pytest.raises(Exception, match='刷新积分'):
        await service._scheduled_cookie(data)


@pytest.mark.asyncio
async def test_manual_account_overrides_policy_and_retry_keeps_request(service):
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_manual.json').write_text(COMPLETE_SESSION)
    data = JobInput(kind='video', prompt='x', account_id='account_manual',
                    video_model='omni_flash', account_strategy='highest_credits')
    job = service.enqueue(data)
    worker = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(service.queue.join(), 1)
        done = service.get_job(job['id'])
        assert done['status'] == 'succeeded'
        assert service.engine.calls[0]['cookies'].endswith('account_manual.json')
        assert 'model' not in service.engine.calls[0]  # CLI has no video model flag.
        assert done['request']['video_model'] == 'omni_flash'
        assert done['request']['selected_account_id'] == 'account_manual'
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    with service.connect() as db:
        db.execute("update console_jobs set status='failed' where id=?", (job['id'],))
    retry = service.retry(job['id'], True)
    assert retry['request']['account_strategy'] == 'highest_credits'
    assert retry['request']['account_id'] == 'account_manual'
    assert 'selected_account_id' not in retry['request']


@pytest.mark.asyncio
async def test_unread_balance_fails_before_generation_with_actionable_reason(service):
    job = service.enqueue(JobInput(kind='video', prompt='x'))
    worker = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(service.queue.join(), 1)
        done = service.get_job(job['id'])
        assert done['status'] == 'failed'
        assert '刷新积分' in done['error']
        assert service.engine.calls == []
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def test_api_validates_model_and_strategy_without_silent_fallback(service):
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        for extra in ({'video_model': 'veo'}, {'account_strategy': 'free_first'}):
            assert client.post('/console/api/jobs', json={'kind': 'video', 'prompt': 'x', **extra}).status_code == 422
        response = client.post('/console/api/jobs', json={'kind': 'video', 'prompt': 'x',
                               'video_model': 'omni_flash', 'account_strategy': 'lowest_credits'})
        assert response.status_code == 202
        request = response.json()['request']
        assert request['account_strategy'] == 'lowest_credits'
        assert request['video_model'] == 'omni_flash'


@pytest.mark.asyncio
async def test_account_response_includes_safe_video_estimates(service):
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_a.json').write_text(COMPLETE_SESSION)
    service.engine.balance_accounts = [{'account_id': 'acct-a', 'credits': 1050, 'status': 'active'}]
    result = service.accounts(await service.engine.get_balance())
    assert result['accounts'][0]['video_estimates']['omni_flash']['8s']['720p'] == 87
    assert result['accounts'][0]['video_estimates']['veo_3_1']['fast']['non_ultra'] == 52
    assert result['video_credit_pricing']['checked_on'] == '2026-10-15'
    assert 'PRIVATE' not in json.dumps(result)


ASSETS = Path(__file__).parent / 'assets'


@pytest.mark.parametrize('payload', [
    {'video_mode': 'references'},
    {'video_mode': 'frames'},
    {'video_mode': 'edit'},
    {'end_image_id': 'upload:end.png'},
    {'reference_image_ids': ['a'], 'start_image_id': 'b'},
    {'reference_image_ids': ['a'], 'source_video_id': 'b'},
    {'reference_image_ids': ['a', 'a']},
    {'reference_image_ids': ['']},
    {'reference_image_ids': ['a'], 'count': 2},
    {'reference_image_ids': ['a'], 'quality': '360p'},
    {'reference_image_ids': ['a'], 'aspect': '9:16'},
    {'source_video_id': 'a', 'duration': '10s'},
    {'source_video_id': 'a', 'start_image_id': 'b'},
    {'source_video_id': 'a', 'count': 2},
])
def test_invalid_material_combinations_refused_before_submission(payload):
    with pytest.raises(ValueError):
        JobInput(kind='video', prompt='x', **payload)


@pytest.mark.asyncio
async def test_material_upload_preview_and_invalid_video_cleanup(service):
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        image_bytes = (ASSETS / 'reference-purple.png').read_bytes()
        image = client.post('/console/api/uploads/image', files={'file': ('purple.png', image_bytes, 'image/png')})
        assert image.status_code == 200
        assert client.get(image.json()['url']).content == image_bytes
        video_bytes = (ASSETS / 'source-1s.mp4').read_bytes()
        video = client.post('/console/api/uploads/video', files={'file': ('source.mp4', video_bytes, 'video/mp4')})
        assert video.status_code == 200
        assert video.json()['duration_seconds'] == 1
        assert client.get(video.json()['url']).content == video_bytes
        before = set(service.upload_dir.iterdir())
        for name, body in [('bad.mp4', b'fake'), ('source.mp4', (ASSETS / 'source-11s.mp4').read_bytes()), ('source.webm', video_bytes)]:
            assert client.post('/console/api/uploads/video', files={'file': (name, body, 'video/mp4')}).status_code == 422
        assert set(service.upload_dir.iterdir()) == before
        assert client.get('/console/api/inputs/console.db').status_code == 404
        assert client.post('/console/api/jobs', json={'kind': 'video', 'prompt': 'x', 'reference_image_ids': ['upload:../secret.png']}).status_code == 422
        assert service.engine.calls == []  # Local uploads never invoke Google.


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['frames', 'references', 'edit'])
async def test_material_queue_preserves_order_account_and_correct_engine_path(service, mode):
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_a.json').write_text(COMPLETE_SESSION)
    first = await service.upload(UploadFile(file=BytesIO((ASSETS / 'reference-purple.png').read_bytes()), filename='first.png'))
    second = await service.upload(UploadFile(file=BytesIO((ASSETS / 'reference-blue.png').read_bytes()), filename='second.png'))
    options = {'start_image_id': first['image_id'], 'end_image_id': second['image_id']} if mode == 'frames' else {'reference_image_ids': [second['image_id'], first['image_id']]}
    if mode == 'edit':
        video = await service.upload_source_video(UploadFile(file=BytesIO((ASSETS / 'source-1s.mp4').read_bytes()), filename='source.mp4'))
        options = {'source_video_id': video['video_id']}
    job = service.enqueue(JobInput(kind='video', prompt='change the scene', account_id='account_a', **options))
    worker = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(service.queue.join(), 2)
        done = service.get_job(job['id'])
        assert done['status'] == 'succeeded'
        call = service.engine.calls[0]
        assert call['cookies'].endswith('account_a.json')
        assert done['request']['video_mode'] == mode
        if mode == 'frames':
            assert call['start_image'] == service._input_image(first['image_id'])
            assert call['end_image'] == service._input_image(second['image_id'])
        elif mode == 'references':
            assert call['references'] == [service._input_image(second['image_id']), service._input_image(first['image_id'])]
        else:
            assert call['source'] == service._input_video(video['video_id'])
            assert 'duration' not in call and 'quality' not in call
        assert 'PRIVATE' not in json.dumps(done)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_video_edit_scheduler_requires_estimated_40_credits(service, monkeypatch):
    monkeypatch.setattr('flow_agent.account_scheduling.browser_identity_ready', lambda service, account: True)
    service.cookies_dir.mkdir()
    for key in ('a', 'b'):
        (service.cookies_dir / f'account_{key}.json').write_text(COMPLETE_SESSION)
    service.engine.balance_accounts = [{'account_id': 'acct-a', 'credits': 39, 'status': 'active'}, {'account_id': 'acct-b', 'credits': 40, 'status': 'active'}]
    data = JobInput(kind='video', prompt='x', source_video_id='upload:demo.mp4', account_strategy='lowest_credits')
    assert (await service._scheduled_cookie(data)).endswith('account_b.json')


@pytest.mark.asyncio
@pytest.mark.parametrize('reference_count', [4, 8])
async def test_more_than_three_reference_images_reach_engine_in_order(service, reference_count):
    """Use local fixtures and a fake engine; never submit a Google task."""
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_a.json').write_text(COMPLETE_SESSION)
    image_ids = []
    for index in range(reference_count):
        asset = await service.upload(UploadFile(
            file=BytesIO((ASSETS / 'reference-purple.png').read_bytes()),
            filename=f'reference-{index}.png'))
        image_ids.append(asset['image_id'])
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        schema = JobInput.model_json_schema()['properties']['reference_image_ids']
        assert 'maxItems' not in schema
        result = client.post('/console/api/jobs', json={
            'kind': 'video', 'prompt': 'offline multi image test',
            'account_id': 'account_a', 'video_mode': 'references',
            'reference_image_ids': image_ids,
        })
        assert result.status_code == 202
    worker = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(service.queue.join(), 2)
        job = service.get_job(result.json()['id'])
        assert job['status'] == 'succeeded'
        assert job['request']['reference_image_ids'] == image_ids
        assert service.engine.calls[0]['references'] == [service._input_image(ref) for ref in image_ids]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_veo_account_selection_uses_requested_model_cost(service,monkeypatch):
    # Pricing selection fixture is not an account-access verification.
    monkeypatch.setattr('flow_agent.account_scheduling.browser_identity_ready',lambda *args:True)
    service.cookies_dir.mkdir()
    for key in ('small','large'):
        (service.cookies_dir/f'account_{key}.json').write_text(COMPLETE_SESSION)
    service.engine.balance_accounts=[{'account_id':'acct-small','credits':30,'status':'active'},
                                   {'account_id':'acct-large','credits':210,'status':'active'}]
    data=JobInput(kind='video',prompt='fixture',video_model='veo_3_1_quality',count=2,account_strategy='lowest_credits')
    assert (await service._scheduled_cookie(data)).endswith('account_large.json')
    data.video_model='veo_3_1_lite';data.count=1
    assert (await service._scheduled_cookie(data)).endswith('account_small.json')

def test_mcp_integration_config_and_downloads(service):
    app=FastAPI();app.include_router(create_console_router(service))
    with TestClient(app) as client:
        config=client.get('/console/api/integration/mcp').json()['mcpServers']['flow-console']
        assert config['env']['FLOW_MCP_READ_ONLY']=='1'
        assert config['args'][-1].endswith('console_mcp_entry.py')
        doc=client.get('/console/api/integration/mcp/guide.md')
        assert doc.status_code==200 and 'FlowConsoleMCP.exe' in doc.text
        bundle=client.get('/console/api/integration/mcp/bundle.zip')
        assert bundle.status_code==200 and bundle.content[:2]==b'PK'
        assert client.get('/console/api/integration/mcp/secret').status_code==404
    assert service.engine.calls==[]

def test_profile_allocator_never_reuses_existing_or_mapped_directory(service):
    data = service.engine.root / 'data'
    (data / 'flow_browser_profile_1').mkdir(exist_ok=True)
    with service.connect() as db:
        db.execute('insert into console_account_profiles values (?, ?)', ('old', 'flow_browser_profile_2'))
    assert service._new_browser_profile().name == 'flow_browser_profile_3'
    assert service._new_browser_profile().name == 'flow_browser_profile_4'


def test_manual_open_repairs_conflicting_profile_without_touching_old_login(service, monkeypatch):
    data = service.engine.root / 'data'
    prefs = data / 'flow_browser_profile_8' / 'Default' / 'Preferences'
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({'account_info': [{'email': 'old@example.com'}]}))
    original = prefs.read_bytes()
    with service.connect() as db:
        db.executemany('insert into console_account_profiles values (?, ?)', [('old', 'flow_browser_profile_8'), ('new', 'flow_browser_profile_8')])
    monkeypatch.setattr(service, '_verified_detail', lambda key: {'email': key + '@example.com'})
    with pytest.raises(Exception) as error:
        service._manual_account_profile('new', 'flow_browser_profile_8', background=True)
    assert error.value.status_code == 409
    fresh = service._manual_account_profile('new', 'flow_browser_profile_8', background=False)
    assert fresh != 'flow_browser_profile_8'
    assert prefs.read_bytes() == original
    with service.connect() as db:
        assert db.execute("select profile_name from console_account_profiles where account_key='old'").fetchone()[0] == 'flow_browser_profile_8'
        assert db.execute("select profile_name from console_account_profiles where account_key='new'").fetchone()[0] == fresh
    assert service._manual_account_profile('new', fresh, background=False) == fresh
    assert not service.engine.calls

def test_image_page_check_accepts_exact_parameters_without_queue_or_generation(service, monkeypatch):
    service.cookies_dir.mkdir()
    (service.cookies_dir / 'account_fixture.json').write_text(COMPLETE_SESSION)
    seen=[]
    async def check(**kwargs):
        seen.append(kwargs)
        return {'stage':'check_completed','parameters':{'model':'beluga','aspect':kwargs['aspect'],'count':1},'generation_submitted':False,'generation_requests':0,'browser_closed':True}
    monkeypatch.setattr(service.official_images,'run',check)
    app=FastAPI();app.include_router(create_console_router(service))
    with TestClient(app) as client:
        result=client.post('/console/api/accounts/account_fixture/image-page-check',json={'model':'beluga','aspect':'1:1','count':1})
        assert result.status_code==200
        assert result.json()['parameters']['aspect']=='1:1'
        assert seen[0]['check_only'] is True
        assert client.post('/console/api/accounts/account_fixture/image-page-check',json={'count':2}).status_code==422
        assert len(seen)==1
    assert not service.list_jobs() and not service.engine.calls
