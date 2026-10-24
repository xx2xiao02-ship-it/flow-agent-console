"""Humanize pacing, prompt entry and visibility preference defaults."""
import asyncio
import os
import time

import pytest

from flow_agent import humanize
from flow_agent.browser_visibility import OwnedBrowserVisibility
from flow_agent.task_scheduler import SchedulerInput


@pytest.mark.parametrize('kind', sorted(humanize.KINDS))
def test_delay_stays_within_bounds(kind, monkeypatch):
    monkeypatch.setattr(humanize, 'PACE', 1)
    median, sigma, low, high = humanize.KINDS[kind]
    for _ in range(400):
        value = humanize.delay(kind)
        assert low <= value <= high


def test_delay_scales_with_pace(monkeypatch):
    monkeypatch.setattr(humanize, 'PACE', 0.5)
    median, sigma, low, high = humanize.KINDS['action']
    for _ in range(100):
        assert humanize.delay('action') <= high * 0.5 + 1e-9


@pytest.mark.asyncio
async def test_zero_pace_disables_waits(monkeypatch):
    monkeypatch.setattr(humanize, 'PACE', 0)
    started = time.monotonic()
    await humanize.pause('pre_submit')
    await humanize.wander(object())
    assert time.monotonic() - started < 0.2


class FakeKeyboard:
    def __init__(self):
        self.events = []

    async def press(self, key):
        self.events.append(('press', key))

    async def insert_text(self, text):
        self.events.append(('insert', text))

    async def type(self, text):
        self.events.append(('type', text))


class FakeTextbox:
    def __init__(self):
        self.clicks = 0

    async def click(self):
        self.clicks += 1


class FakePage:
    def __init__(self):
        self.keyboard = FakeKeyboard()
        self.viewport_size = {'width': 1280, 'height': 900}


@pytest.mark.asyncio
async def test_enter_prompt_pastes_bulk_and_types_tail(monkeypatch):
    page = FakePage()
    textbox = FakeTextbox()
    await humanize.enter_prompt(page, textbox, 'a tiny red paper collage apple')
    presses = [key for kind_, key in page.keyboard.events if kind_ == 'press']
    assert presses == ['ControlOrMeta+a', 'Delete']
    assert textbox.clicks == 1
    inserted = ''.join(t for kind_, t in page.keyboard.events if kind_ == 'insert')
    typed = ''.join(t for kind_, t in page.keyboard.events if kind_ == 'type')
    assert inserted + typed == 'a tiny red paper collage apple'
    assert typed  # the tail is actually typed, not pasted


@pytest.mark.asyncio
async def test_enter_prompt_inserts_multiline_without_enter_keys(monkeypatch):
    page = FakePage()
    prompt = 'line one\n\nline two'
    await humanize.enter_prompt(page, FakeTextbox(), prompt)
    typed = ''.join(t for kind_, t in page.keyboard.events if kind_ == 'type')
    inserted = ''.join(t for kind_, t in page.keyboard.events if kind_ == 'insert')
    assert typed == ''
    assert inserted == prompt  # a real paste keeps newlines as text, never Enter


def test_visible_owned_visibility_reports_and_args(tmp_path):
    guard = OwnedBrowserVisibility(tmp_path / 'profile', tmp_path / 'chrome.exe', mode='visible')
    args = guard.launch_args() if os.name == 'nt' else []
    assert any('CalculateNativeWinOcclusion' in arg for arg in args) or os.name != 'nt'
    info = guard.info()
    assert info['mode'] == 'visible'
    assert info['hidden_confirmed'] is False
    assert guard.requires_hidden is False


def test_hidden_owned_visibility_is_default(tmp_path):
    guard = OwnedBrowserVisibility(tmp_path / 'profile', tmp_path / 'chrome.exe')
    assert guard.mode == 'hidden'
    assert guard.requires_hidden is (os.name == 'nt')


def test_preferences_default_to_visible_windows(tmp_path):
    from flow_agent.console import ConsoleService
    from flow_agent.engine import FlowEngine
    from flow_agent.verification_settings import snapshot, configure_generation
    service = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    service.initialize()
    preferences = snapshot(service)
    assert preferences['headless'] is False
    assert preferences['generation_browser_visible'] is True
    configure_generation(service, False)
    assert snapshot(service)['generation_browser_visible'] is False


def test_scheduler_interval_roundtrip_and_bounds(tmp_path):
    from flow_agent.console import ConsoleService
    from flow_agent.engine import FlowEngine
    service = ConsoleService(FlowEngine(root=tmp_path / 'flow-agent'))
    service.initialize()
    snapshot_ = service.scheduler.configure(SchedulerInput(max_concurrency=2, min_job_interval_seconds=45))
    assert snapshot_['min_job_interval_seconds'] == 45
    service.scheduler.load()
    assert service.scheduler.min_job_interval == 45
    with pytest.raises(Exception):
        SchedulerInput(max_concurrency=1, min_job_interval_seconds=601)
    with pytest.raises(Exception):
        SchedulerInput(max_concurrency=1, min_job_interval_seconds=-1)
