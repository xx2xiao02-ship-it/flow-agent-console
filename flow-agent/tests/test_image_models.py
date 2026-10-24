"""No paid calls: catalog parsing, compatibility, and stopping before spawn."""
from unittest.mock import AsyncMock

import pytest

from flow_agent.account_details import AccountReadError
from flow_agent.console import JobInput, safe_failure
from flow_agent.engine import FlowEngine
from flow_agent.image_models import ImageModelError, catalog_image_models, check_image_model


def test_catalog_requires_exact_model_ids_not_descriptions():
    assert catalog_image_models([['BELUGA', 'old NARWHAL retired'], ['GEM_PIX_2']]) == {'BELUGA', 'GEM_PIX_2'}
    assert catalog_image_models(['NARWHAL is unsupported']) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize('model,catalog', [('beluga', ['BELUGA']), ('narwhal', ['NARWHAL']), ('pro', ['GEM_PIX_2'])])
async def test_models_are_not_substituted(model, catalog):
    reader = AsyncMock()
    reader.call.return_value = [catalog]
    await check_image_model(reader, model)
    reader.call.assert_awaited_once_with('HTrJv', [])


@pytest.mark.asyncio
async def test_retired_model_stops_before_native_generation(tmp_path, monkeypatch):
    cookie = tmp_path / 'session.json'
    cookie.write_text('{}')
    reader = AsyncMock()
    reader.call.return_value = [['BELUGA', 'HARBOR_SEAL', 'GEM_PIX_2']]
    monkeypatch.setattr('flow_agent.engine.FlowAccountReader', lambda *_: reader)
    engine = FlowEngine(root=tmp_path)
    native = AsyncMock()
    monkeypatch.setattr(engine, '_run_json', native)
    with pytest.raises(ImageModelError, match='narwhal_retired') as error:
        await engine.generate_image('cat', model='narwhal', cookies=str(cookie))
    native.assert_not_awaited()
    assert '本次未调用生成' in safe_failure(error.value)


@pytest.mark.asyncio
async def test_beluga_passed_unchanged_to_native_engine(tmp_path, monkeypatch):
    cookie = tmp_path / 'session.json'
    cookie.write_text('{}')
    reader = AsyncMock()
    reader.call.return_value = [['BELUGA']]
    monkeypatch.setattr('flow_agent.engine.FlowAccountReader', lambda *_: reader)
    engine = FlowEngine(root=tmp_path)
    native = AsyncMock(return_value={'status': 'succeeded'})
    monkeypatch.setattr(engine, '_run_json', native)
    await engine.generate_image('cat', aspect='16:9', model='beluga', cookies=str(cookie))
    native.assert_awaited_once_with('image', 'cat', '16:9', '--model', 'beluga', '--cookies', str(cookie), timeout=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('catalog', [[], [['unknown_future_model']]])
async def test_unknown_catalog_does_not_claim_support(catalog):
    reader = AsyncMock()
    reader.call.return_value = catalog
    with pytest.raises(ImageModelError, match='catalog_unread'):
        await check_image_model(reader, 'beluga')


@pytest.mark.asyncio
async def test_catalog_network_failure_does_not_submit(tmp_path, monkeypatch):
    cookie = tmp_path / 'session.json'
    cookie.write_text('{}')
    reader = AsyncMock()
    reader.call.side_effect = AccountReadError('network')
    monkeypatch.setattr('flow_agent.engine.FlowAccountReader', lambda *_: reader)
    engine = FlowEngine(root=tmp_path)
    native = AsyncMock()
    monkeypatch.setattr(engine, '_run_json', native)
    with pytest.raises(ImageModelError, match='catalog_unread'):
        await engine.generate_image('cat', model='beluga', cookies=str(cookie))
    native.assert_not_awaited()


def test_console_accepts_new_model_and_preserves_legacy_api_default():
    assert JobInput(kind='image', prompt='cat', model='beluga').model == 'beluga'
    assert JobInput(kind='image', prompt='cat').model == 'narwhal'


def test_rest_api_returns_safe_preflight_error_instead_of_500(monkeypatch):
    from fastapi.testclient import TestClient
    from flow_agent import api
    monkeypatch.setattr(api.engine, 'generate_image', AsyncMock(side_effect=ImageModelError('narwhal_retired')))
    # No lifespan/background monitor is started and no real engine is used.
    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.post('/api/v1/image', json={'prompt': 'cat', 'model': 'narwhal'})
    assert response.status_code == 422
    assert '本次未调用生成' in response.json()['detail']
