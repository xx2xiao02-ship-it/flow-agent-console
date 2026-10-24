"""Proxy pool parsing, sticky binding, API surface and cleanup on removal."""
import pytest
from fastapi.testclient import TestClient

from flow_agent import proxy_pool
from flow_agent.console import ConsoleService, safe_failure
from flow_agent.engine import FlowEngine


@pytest.fixture
def service(tmp_path):
    svc = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    svc.initialize()
    svc.cookies_dir.mkdir()
    (svc.cookies_dir / 'account_a.json').write_text('{"cookies": [], "at": "x", "fsid": "y"}')
    (svc.cookies_dir / 'account_b.json').write_text('{"cookies": [], "at": "x", "fsid": "y"}')
    return svc


def test_parse_accepts_supported_schemes_and_splits_credentials():
    parts = proxy_pool.parse('http://user:secret@10.0.0.1:8080')
    assert parts['hostname'] == '10.0.0.1' and parts['port'] == 8080
    assert parts['username'] == 'user' and parts['password'] == 'secret'
    assert proxy_pool.parse('socks5://10.0.0.2:1080')['scheme'] == 'socks5'
    assert proxy_pool.parse('10.0.0.3:3128')['scheme'] == 'http'  # scheme optional


def test_parse_rejects_unknown_scheme_and_missing_host():
    for bad in ('ftp://1.2.3.4:21', 'http://', 'http://host'):
        with pytest.raises(ValueError):
            proxy_pool.parse(bad)


def test_masked_output_never_contains_credentials():
    assert proxy_pool.masked('http://user:secret@10.0.0.1:8080') == 'http://user:***@10.0.0.1:8080'
    assert 'secret' not in proxy_pool.masked('http://user:secret@10.0.0.1:8080')


def test_playwright_proxy_splits_credentials_out_of_server_url():
    result = proxy_pool.playwright_proxy('http://user:secret@10.0.0.1:8080')
    assert result == {'server': 'http://10.0.0.1:8080', 'username': 'user', 'password': 'secret'}
    assert proxy_pool.playwright_proxy('socks5://10.0.0.2:1080') == {'server': 'socks5://10.0.0.2:1080'}


def test_empty_pool_returns_none_and_never_assigns(service):
    assert proxy_pool.proxy_for(service, 'account_a') is None
    assert proxy_pool.raw_url_for(service, 'account_a') is None
    assert proxy_pool.snapshot(service)['bindings'] == {}


def test_schemeless_proxy_is_normalized_on_save_and_read(service):
    proxy_pool.add(service, '10.0.0.3:3128')
    assert proxy_pool.snapshot(service)['proxies'][0]['proxy'] == 'http://10.0.0.3:3128'
    bound = proxy_pool.raw_url_for(service, 'account_a')
    import httpx
    httpx.Client(proxy=bound, timeout=2).close()  # must be accepted now
    assert bound == 'http://10.0.0.3:3128'


def test_legacy_schemeless_row_still_tests(service):
    """Rows saved before normalization: the tester must not misreport them."""
    proxy_pool.add(service, 'http://127.0.0.1:1')
    ident = proxy_pool.snapshot(service)['proxies'][0]['id']
    with service.connect() as db:
        db.execute('update proxy_pool set url=? where id=?', ('127.0.0.1:1', ident))
    result = proxy_pool.test(service, ident)
    assert result['ok'] is False
    assert result['error'] != 'ValueError'  # a connect failure, not a scheme error


def test_sticky_binding_and_least_used_assignment(service):
    proxy_pool.add(service, 'http://10.0.0.1:8080')
    proxy_pool.add(service, 'http://10.0.0.2:8080')
    first = proxy_pool.playwright_proxy(proxy_pool.raw_url_for(service, 'account_a'))
    assert first['server'] == 'http://10.0.0.1:8080'
    # Sticky: same account keeps the same exit.
    again = proxy_pool.proxy_for(service, 'account_a')
    assert again == first
    # The next account gets the least-used exit.
    second = proxy_pool.playwright_proxy(proxy_pool.raw_url_for(service, 'account_b'))
    assert second['server'] == 'http://10.0.0.2:8080'
    snapshot_ = proxy_pool.snapshot(service)
    assert set(snapshot_['bindings']) == {'account_a', 'account_b'}


def test_rebind_moves_account_to_least_used_other_exit(service):
    proxy_pool.add(service, 'http://10.0.0.1:8080')
    proxy_pool.add(service, 'http://10.0.0.2:8080')
    proxy_pool.raw_url_for(service, 'account_a')
    proxy_pool.rebind(service, 'account_a')
    assert proxy_pool.snapshot(service)['bindings']['account_a'] == 'http://10.0.0.2:8080'


def test_removing_proxy_clears_its_bindings(service):
    proxy_pool.add(service, 'http://10.0.0.1:8080')
    proxy_pool.raw_url_for(service, 'account_a')
    ident = proxy_pool.snapshot(service)['proxies'][0]['id']
    proxy_pool.remove(service, ident)
    assert proxy_pool.snapshot(service)['bindings'] == {}
    assert proxy_pool.snapshot(service)['proxies'] == []


def test_forget_account_on_removal(service):
    proxy_pool.add(service, 'http://10.0.0.1:8080')
    proxy_pool.raw_url_for(service, 'account_a')
    proxy_pool.forget_account(service, 'account_a')
    assert 'account_a' not in proxy_pool.snapshot(service)['bindings']


def test_proxy_api_roundtrip(service):
    from fastapi import FastAPI
    from flow_agent.console import create_console_router
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        added = client.post('/console/api/proxy', json={'action': 'add', 'value': 'http://u:p@127.0.0.1:8888'})
        assert added.status_code == 200
        body = added.json()
        assert body['proxies'][0]['proxy'] == 'http://u:***@127.0.0.1:8888'
        ident = body['proxies'][0]['id']
        bound = client.post('/console/api/proxy', json={'action': 'rebind', 'account_id': 'account_a', 'proxy_id': ident})
        assert bound.status_code == 200
        assert list(bound.json()['bindings'].values()) == ['http://u:***@127.0.0.1:8888']
        assert client.post('/console/api/proxy', json={'action': 'add', 'value': 'ftp://x'}).status_code == 400
        assert client.post('/console/api/proxy', json={'action': 'nope'}).status_code == 400
        removed = client.post('/console/api/proxy', json={'action': 'remove', 'proxy_id': ident})
        assert removed.status_code == 200 and removed.json()['bindings'] == {}


def test_safe_failure_masks_proxy_errors(service):
    try:
        proxy_pool.add(service, 'http://u:topsecret@127.0.0.1:1')
        proxy_pool.test(service, proxy_pool.snapshot(service)['proxies'][0]['id'])
    except Exception as exc:
        assert 'topsecret' not in safe_failure(exc)
