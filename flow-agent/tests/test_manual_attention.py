"""Verification-pull mode: challenge marks the account, blocks dispatch, and
a clean background read re-enters the pool."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flow_agent import account_scheduling
from flow_agent.console import ConsoleService, create_console_router
from flow_agent.engine import FlowEngine
from flow_agent.official_images import OfficialImageError


class FakeGuard:
    def __init__(self):
        self.revealed = []
        self.shown = False
        self.error = None
        self.enabled = False

    def reveal(self, reason):
        self.revealed.append(reason)
        self.shown = True

    async def confirm_hidden(self):
        return None

    def hide_after_verification(self):
        pass


class FakeCount:
    def __init__(self, value):
        self.value = value

    async def count(self):
        return self.value

    async def is_visible(self):
        return bool(self.value)


class FakePage:
    url = 'https://flow.google.com/project/x'

    def __init__(self, identity_visible=False):
        self._identity = FakeCount(1 if identity_visible else 0)

    def get_by_role(self, *args, **kwargs):
        if args and args[0] == 'button':
            return self._identity
        return FakeCount(0)

    def get_by_text(self, *args, **kwargs):
        return FakeCount(0)


@pytest.fixture
def service(tmp_path):
    svc = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    svc.initialize()
    svc.cookies_dir.mkdir()
    (svc.cookies_dir / 'account_a.json').write_text('{"cookies": [], "at": "x", "fsid": "y"}')
    return svc


def test_mark_blocks_dispatch_until_cleared(service):
    account_scheduling.mark_manual_attention(service, 'account_a', 'login_or_verification', job_id='j1')
    assert 'account_a' in account_scheduling.manual_attention(service)
    # Excluded from automatic candidates.
    eligible = [{'id': 'account_a'}]
    assert account_scheduling.candidates(service, eligible) == []
    # Explicit generation submission is refused with a clear message.
    with pytest.raises(Exception) as exc:
        account_scheduling.require_generation_account(service, 'account_a')
    assert '人工处理' in str(exc.value)
    # A clean background read re-enters the pool.
    account_scheduling.clear_manual_attention(service, 'account_a')
    assert account_scheduling.candidates(service, eligible) == [{'id': 'account_a'}]


@pytest.mark.asyncio
async def test_generation_run_fails_fast_without_reveal_on_attention(service, monkeypatch):
    """Pre-submit challenge: mark the account and raise; never reveal or wait."""
    runner = service.official_images
    guard = FakeGuard()
    report = {'account_id': 'account_a', 'check_only': False, 'job_id': 'j2'}

    async def attention_reason(page):
        return 'login_or_verification'

    monkeypatch.setattr(runner, 'attention_reason', attention_reason)
    with pytest.raises(OfficialImageError) as exc:
        await runner.wait_for_identity(FakePage(identity_visible=False), 'a@example.test', guard, report)
    assert exc.value.code == 'manual_verification_required'
    assert exc.value.submitted is False
    assert 'account_a' in account_scheduling.manual_attention(service)
    assert report['manual_attention'] == 'login_or_verification'
    assert guard.revealed == []  # no window popup


@pytest.mark.asyncio
async def test_transient_overlay_with_valid_identity_keeps_waiting(service, monkeypatch):
    """Identity already visible + attention flag: transient state, keep waiting."""
    runner = service.official_images
    guard = FakeGuard()
    report = {'account_id': 'account_a', 'check_only': False}

    async def attention_reason(page):
        return 'login_or_verification'

    monkeypatch.setattr(runner, 'attention_reason', attention_reason)
    task = asyncio.create_task(runner.wait_for_identity(
        FakePage(identity_visible=True), 'a@example.test', guard, report))
    await asyncio.sleep(0.8)
    assert 'account_a' not in account_scheduling.manual_attention(service)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, OfficialImageError):
        pass


@pytest.mark.asyncio
async def test_check_only_still_reveals_and_waits(service, monkeypatch):
    """Preparation/check runs keep the existing visible human flow."""
    runner = service.official_images
    guard = FakeGuard()
    report = {'account_id': 'account_a', 'check_only': True}

    async def attention_reason(page):
        return 'login_or_verification'

    monkeypatch.setattr(runner, 'attention_reason', attention_reason)
    task = asyncio.create_task(runner.wait_for_identity(
        FakePage(identity_visible=True), 'a@example.test', guard, report))
    await asyncio.sleep(0.6)
    assert guard.revealed and guard.revealed[0] == 'login_or_verification'
    assert 'account_a' not in account_scheduling.manual_attention(service)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, OfficialImageError):
        pass


def test_manual_attention_api_roundtrip(service):
    app = FastAPI()
    app.include_router(create_console_router(service))
    with TestClient(app) as client:
        assert client.get('/console/api/accounts/manual-attention').json() == {'accounts': []}
        account_scheduling.mark_manual_attention(service, 'account_a', 'login_or_verification')
        items = client.get('/console/api/accounts/manual-attention').json()['accounts']
        assert items and items[0]['account_id'] == 'account_a'
        assert items[0]['reason'] == 'login_or_verification'
