"""LAN API security and queue contract; never calls Google."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path

from flow_agent.lan_api import create_lan_app
from flow_agent.console import ConsoleService, JobInput, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.lan_keys import LanKeyStore


class FakeEngine(FlowEngine):
    async def get_balance(self, refresh=False):
        return {"accounts": [], "total_credits": 0, "unread": 0}


def test_lan_api_requires_key_and_exposes_only_selected_routes(tmp_path):
    service = ConsoleService(FakeEngine(tmp_path))
    service.initialize()
    key = "a" * 48
    with TestClient(create_lan_app(service, key)) as client:
        assert client.get("/api/v1/balance").status_code == 401
        assert client.get("/api/v1/balance", headers={"Authorization": "Bearer wrong"}).status_code == 401
        auth = {"Authorization": f"Bearer {key}"}
        assert client.get("/api/v1/balance", headers=auth).status_code == 200
        assert client.get("/console/", headers=auth).status_code == 404
        assert client.get("/console/api/accounts", headers=auth).status_code == 404
        assert client.get("/docs", headers=auth).status_code == 404
        assert client.get("/api/v1/openapi.json").status_code == 401
        schema = client.get("/api/v1/openapi.json", headers=auth)
        assert schema.status_code == 200
        assert "/api/v1/jobs" in schema.json()["paths"]
        payload = {"kind": "video", "prompt": "clouds", "aspect": "9:16"}
        assert client.post("/api/v1/jobs", json=payload, headers=auth).status_code == 422
        response = client.post("/api/v1/jobs", json=payload,
                               headers={**auth, "X-Flow-Allow-Charge": "yes"})
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert client.get(f"/api/v1/jobs/{job_id}", headers=auth).status_code == 200
        assert client.get("/api/v1/media/../lan_api_key.txt", headers=auth).status_code == 404


def test_lan_api_rejects_short_key(tmp_path):
    service = ConsoleService(FakeEngine(tmp_path))
    try:
        create_lan_app(service, "short")
    except ValueError:
        pass
    else:
        assert False, "短密钥必须拒绝"


def test_lan_material_upload_and_preview_require_auth_and_preserve_modes(tmp_path):
    service = ConsoleService(FakeEngine(tmp_path))
    service.initialize()  # No worker: enqueue contracts never call Google.
    assets = Path(__file__).parent / 'assets'
    auth = {'Authorization': 'Bearer ' + 'a' * 48}
    with TestClient(create_lan_app(service, 'a' * 48)) as client:
        uploaded_images = []
        for name in ('reference-purple.png', 'reference-blue.png'):
            payload = (assets / name).read_bytes()
            assert client.post('/api/v1/uploads/image', files={'file': (name, payload)}).status_code == 401
            result = client.post('/api/v1/uploads/image', headers=auth, files={'file': (name, payload)})
            assert result.status_code == 200
            uploaded_images.append(result.json()['image_id'])
            assert client.get(result.json()['url']).status_code == 401
            assert client.get(result.json()['url'], headers=auth).content == payload
        payload = (assets / 'source-1s.mp4').read_bytes()
        assert client.post('/api/v1/uploads/video', files={'file': ('source.mp4', payload)}).status_code == 401
        result = client.post('/api/v1/uploads/video', headers=auth, files={'file': ('source.mp4', payload)})
        assert result.status_code == 200
        video = result.json()
        assert video['duration_seconds'] == 1
        assert video['url'].startswith('/api/v1/inputs/')
        assert client.get(video['url']).status_code == 401
        assert client.get(video['url'], headers=auth).content == payload
        for mode, fields in [('references', {'reference_image_ids': uploaded_images}),
                             ('edit', {'source_video_id': video['video_id']})]:
            request = {'kind': 'video', 'prompt': 'offline contract test', 'video_mode': mode, **fields}
            assert client.post('/api/v1/jobs', headers=auth, json=request).status_code == 422
            response = client.post('/api/v1/jobs', headers={**auth, 'X-Flow-Allow-Charge': 'yes'}, json=request)
            assert response.status_code == 202
            assert response.json()['request']['video_mode'] == mode
            for key, value in fields.items():
                assert response.json()['request'][key] == value
        assert client.get('/api/v1/inputs/console.db', headers=auth).status_code == 404
        assert service.worker is None


def test_local_integration_info_and_job_lookup(tmp_path, monkeypatch):
    service = ConsoleService(FakeEngine(tmp_path))
    service.initialize()
    app = FastAPI()
    app.include_router(create_console_router(service))
    monkeypatch.setenv("FLOW_AGENT_LAN_ENABLED", "0")
    with TestClient(app) as client:
        info = client.get("/console/api/integration").json()
        assert info == {"lan_enabled": False, "lan_port": 8002, "lan_ipv4": None}
        job_id = service.enqueue(JobInput(kind="image", prompt="test"))["id"]
        assert client.get(f"/console/api/jobs/{job_id}").json()["status"] == "queued"
        assert client.get("/console/api/jobs/missing").status_code == 404


def test_local_key_management_never_returns_secret_and_rotation_revokes_old_key(tmp_path, monkeypatch):
    service = ConsoleService(FakeEngine(tmp_path))
    service.initialize()
    store = LanKeyStore(tmp_path)
    store.ensure()
    first_key = store.read()
    local_app = FastAPI()
    local_app.include_router(create_console_router(service))
    copied = []
    monkeypatch.setattr(LanKeyStore, "copy_to_windows_clipboard", lambda self: copied.append(self.read()))
    with TestClient(local_app) as local, TestClient(create_lan_app(service, store)) as lan:
        status = local.get("/console/api/lan-key")
        assert status.status_code == 200
        assert status.json()["configured"] is True
        assert first_key not in status.text
        assert local.post("/console/api/lan-key/copy").json() == {"copied": True}
        assert copied == [first_key]
        assert first_key not in local.post("/console/api/lan-key/copy").text
        assert local.post("/console/api/lan-key/rotate", json={}).status_code == 422
        assert store.read() == first_key
        assert lan.get("/api/v1/health", headers={"Authorization": f"Bearer {first_key}"}).status_code == 200
        rotated = local.post("/console/api/lan-key/rotate", json={"acknowledge_revoke": True})
        assert rotated.status_code == 200
        second_key = store.read()
        assert second_key != first_key
        assert second_key not in rotated.text
        assert lan.get("/api/v1/health", headers={"Authorization": f"Bearer {first_key}"}).status_code == 401
        assert lan.get("/api/v1/health", headers={"Authorization": f"Bearer {second_key}"}).status_code == 200



def test_lan_accepts_supported_strategy_and_rejects_veo(tmp_path):
    service = ConsoleService(FakeEngine(tmp_path))
    service.initialize()
    headers = {'Authorization': 'Bearer ' + 'a' * 48, 'X-Flow-Allow-Charge': 'yes'}
    with TestClient(create_lan_app(service, 'a' * 48)) as client:
        for strategy in ('round_robin', 'highest_credits', 'lowest_credits'):
            r = client.post('/api/v1/jobs', headers=headers,
                            json={'kind': 'video', 'prompt': 'x', 'video_model': 'omni_flash',
                                  'account_strategy': strategy})
            assert r.status_code == 202
            assert r.json()['request']['account_strategy'] == strategy
        assert client.post('/api/v1/jobs', headers=headers,
                           json={'kind': 'video', 'prompt': 'x', 'video_model': 'veo'}).status_code == 422
        schema = client.get('/api/v1/openapi.json', headers=headers).json()
        fields = schema['components']['schemas']['JobInput']['properties']
        assert fields['video_model']['enum'] == ['omni_flash','veo_3_1_lite','veo_3_1_fast','veo_3_1_quality']
        assert fields['account_strategy']['enum'] == ['round_robin', 'highest_credits', 'lowest_credits']


def test_veo_lan_model_and_validation_contract(tmp_path):
    service=ConsoleService(FakeEngine(tmp_path));service.initialize()
    headers={'Authorization':'Bearer '+'a'*48,'X-Flow-Allow-Charge':'yes'}
    with TestClient(create_lan_app(service,'a'*48)) as client:
        for model in ('veo_3_1_lite','veo_3_1_fast','veo_3_1_quality'):
            body={'kind':'video','prompt':'contract only','video_model':model}
            result=client.post('/api/v1/jobs',json=body,headers=headers)
            assert result.status_code==202
            assert result.json()['request']['video_model']==model
            assert client.post('/api/v1/jobs',json={**body,'duration':'4s'},headers=headers).status_code==422
            assert client.post('/api/v1/jobs',json=body).status_code==401
