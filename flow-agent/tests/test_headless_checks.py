import asyncio
import pytest
from tests.test_official_images import local_flow, local_service
from flow_agent.official_images import OfficialImageError, profile_in_use
from flow_agent.verification_settings import snapshot, configure

@pytest.mark.asyncio
async def test_noninteractive_check_does_not_consume_login_repair(local_service,monkeypatch):
    from pathlib import Path
    service,cookie,profile=local_service
    account=Path(cookie).stem
    def blocked(*args): raise OfficialImageError('headless_needs_login')
    monkeypatch.setattr('flow_agent.account_repair.ensure_profile',blocked)
    service.preparation.report=service.preparation._new_report(account)
    await service.preparation._run_headless(account,repair_login=False)
    assert account not in service.monitor.repair_attempts
    assert service.preparation.report['state']=='needs_attention'

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
async def test_real_headless_checks_close_without_generation(local_service, local_flow, monkeypatch, outcome):
    service, cookie, profile = local_service
    configure(service, True)  # Opt in; the default is now a visible window.
    assert snapshot(service)['headless'] is True
    entered = asyncio.Event()
    async def prepare(*args, **kwargs):
        entered.set()
        if outcome == 'failure': raise OfficialImageError('identity_mismatch')
        if outcome == 'cancel': await asyncio.Event().wait()
    monkeypatch.setattr(service.official_images, 'prepare', prepare)
    task = asyncio.create_task(service.official_images.run(cookies=cookie, check_only=True))
    if outcome == 'cancel':
        await asyncio.wait_for(entered.wait(), 20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    elif outcome == 'failure':
        with pytest.raises(OfficialImageError): await task
    else:
        result = await task
        assert result['page_ready']
    report = service.official_images.last_report
    assert report['visibility']['mode'] == 'headless'
    assert report['browser_closed']
    assert not profile_in_use(profile)
    assert local_flow[0].submissions == 0
    configure(service, False)
    assert not snapshot(service)['headless']



@pytest.mark.asyncio
@pytest.mark.parametrize('identity_outcome', ['ok', 'wrong_account', 'network'])
async def test_preparation_headless_opt_in_without_open_or_bridge(local_service, local_flow, monkeypatch, identity_outcome):
    import hashlib
    from pathlib import Path
    service, cookie, profile = local_service
    configure(service, True)  # Opt in; the default is now a visible window.
    account = Path(cookie).stem
    digest = hashlib.sha256(Path(cookie).read_bytes()).hexdigest()
    with service.connect() as db:
        db.execute('insert or replace into console_account_details values (?, ?, ?, ?, ?, ?, ?)',
            (account, digest, 'Fixture', 'fixture@example.test', 100, '2026-10-21T00:00:00+00:00', None))
    async def forbidden(*args, **kwargs):
        raise AssertionError('headless preparation must not open a visible browser or extension bridge')
    async def read(raw):
        import json
        fresh = json.loads(raw)
        assert fresh['at'] == 'fresh-at'
        assert fresh['fsid'] == 'fresh-fsid'
        return {'name':'Fixture', 'email': ('fixture@example.test' if identity_outcome == 'ok' else 'other@example.test' if identity_outcome == 'wrong_account' else None), 'credits':100, 'error': 'network' if identity_outcome == 'network' else None}
    monkeypatch.setattr(service, 'open_account_browser', forbidden)
    monkeypatch.setattr(service, 'restart_idle_bridge', forbidden)
    monkeypatch.setattr(service.preparation, '_read_account', read)
    monkeypatch.setattr(service.official_images, 'prepare', forbidden)
    await service.preparation.start(account)
    await service.preparation.task
    report = service.preparation.snapshot()
    assert report['state'] == ('checked' if identity_outcome == 'ok' else 'needs_attention'), report
    import json
    assert json.loads(Path(cookie).read_bytes())['at'] == ('fresh-at' if identity_outcome == 'ok' else 'fixture')
    if identity_outcome == 'network':
        assert '网络' in report['message']
        assert '重新登录' not in report['message']
    assert report['browser_cleanup']['state'] == 'closed'
    assert not profile_in_use(profile)
    assert local_flow[0].submissions == 0


@pytest.mark.asyncio
async def test_headless_login_stops_without_showing_window(local_service, local_flow):
    service, cookie, profile = local_service
    configure(service, True)  # Opt in; the default is now a visible window.
    local_flow[0].mode = 'login'
    with pytest.raises(OfficialImageError) as exc:
        await service.official_images.run(cookies=cookie, check_only=True)
    assert exc.value.code == 'headless_needs_login'
    report = service.official_images.last_report
    assert report['browser_closed']
    assert report['visibility']['mode'] == 'headless'
    assert not report['visibility']['shown_for_user']
    assert local_flow[0].submissions == 0
    assert not profile_in_use(profile)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['headless_needs_login', 'identity_not_ready', 'page_failed'])
async def test_session_repair_only_shows_for_confirmed_login(local_service, monkeypatch, reason):
    service, cookie, profile = local_service
    from pathlib import Path
    account = Path(cookie).stem
    monkeypatch.setattr('flow_agent.account_repair.ensure_profile', lambda service, account: 'fixture binding')
    attempts = []
    async def run(**options):
        attempts.append(options)
        service.official_images.last_report = {'browser_closed': True}
        raise OfficialImageError(reason if len(attempts) == 1 else 'manual_timeout')
    monkeypatch.setattr(service.official_images, 'run', run)
    service.preparation.report = service.preparation._new_report(account)
    await service.preparation._run_headless(account, repair_login=True)
    assert len(attempts) == (2 if reason == 'headless_needs_login' else 1)
    assert all(o['check_only'] and o['_preparation'] for o in attempts)
    if len(attempts) == 2:
        assert attempts[1]['_repair_visible'] is True
    assert service.preparation.report['browser_cleanup']['state'] == 'closed'
    assert not service.list_jobs()
