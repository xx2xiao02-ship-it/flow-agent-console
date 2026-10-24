"""The frozen launcher must select persistent data paths before importing the app."""

import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
import pytest


def test_launcher_import_does_not_initialize_flow_agent_before_root_is_set():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import desktop_launcher, sys; assert 'flow_agent' not in sys.modules"],
        cwd=root, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('hidden', [False, True])
def test_native_close_hides_then_explicit_exit_stops_both_servers(monkeypatch, hidden):
    import desktop_launcher as launcher
    servers = []
    class Server:
        def __init__(self, config):
            self.started = False
            self.should_exit = False
            servers.append(self)
        def run(self):
            self.started = True
    class Event:
        def __iadd__(self, handler):
            self.handler = handler
            return self
    calls = []
    window = SimpleNamespace(events=SimpleNamespace(closing=Event(), closed=Event()),
        hide=lambda: calls.append('hide'), show=lambda: calls.append('show'),
        restore=lambda: calls.append('restore'))
    def destroy():
        assert window.events.closing.handler() is True
        window.events.closed.handler()
    window.destroy = destroy
    class Tray:
        def __init__(self, on_open, on_exit):
            self.on_open, self.on_exit = on_open, on_exit
            self.available = True
        def start(self):
            return True
        def stop(self):
            calls.append('tray stopped')
        def notify_hidden(self):
            calls.append('notified')
    trays = []
    def tray(*args):
        t = Tray(*args)
        trays.append(t)
        return t
    options = {}
    def create_window(*args, **kwargs):
        options.update(kwargs)
        return window
    def start(**kwargs):
        assert window.events.closing.handler() is False
        assert not any(s.should_exit for s in servers)
        assert window.events.closing.handler() is False
        assert calls.count('notified') == 1
        trays[0].on_open()
        assert calls[-2:] == ['show', 'restore']
        trays[0].on_exit()
        assert all(s.should_exit for s in servers)
    monkeypatch.setattr(launcher.uvicorn, "Server", Server)
    monkeypatch.setattr(launcher, "WindowsTray", tray)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start))
    launcher._native_window(object(), object(), 8001, 8002, hidden=hidden)
    assert len(servers) == 2
    assert all(s.should_exit for s in servers)
    assert options["confirm_close"] is False
    assert options['hidden'] is hidden
    assert calls[-1] == 'tray stopped'


def test_installer_shutdown_notifies_server_and_closes_window():
    import desktop_launcher as launcher
    servers = [SimpleNamespace(should_exit=False), SimpleNamespace(should_exit=False)]
    calls = []
    signal = SimpleNamespace(requested=lambda: True)
    launcher._watch_shutdown(signal, servers, SimpleNamespace(destroy=lambda: calls.append("closed")))
    assert calls == ["closed"]
    assert all(s.should_exit for s in servers)


def test_installer_exit_bypasses_close_to_tray(monkeypatch):
    import desktop_launcher as launcher
    servers = [SimpleNamespace(should_exit=False)]
    calls = []
    window = SimpleNamespace(destroy=lambda: calls.append('destroy'), hide=lambda: calls.append('hide'))
    life = launcher.DesktopLifetime(window, servers)
    life.tray.available = True
    launcher._watch_shutdown(SimpleNamespace(requested=lambda: True), servers,
                             on_exit=life.exit, on_show=life.show)
    assert calls == ['destroy']
    assert life.closing() is True
    life.exit()
    assert calls == ['destroy']  # Exit callback is idempotent.


def test_tray_failure_allows_normal_exit_instead_of_hiding_forever():
    import desktop_launcher as launcher
    life = launcher.DesktopLifetime(SimpleNamespace(hide=lambda: (_ for _ in ()).throw(AssertionError())), [])
    life.tray.available = False
    assert life.closing() is True


def test_repeat_launch_restores_existing_window_without_stopping_server(monkeypatch):
    import desktop_launcher as launcher
    servers = [SimpleNamespace(should_exit=False)]
    calls = []
    def show():
        calls.append('show')
    ticks = iter([False, True])
    signal = SimpleNamespace(requested=lambda: next(ticks), show_requested=lambda: True)
    launcher._watch_shutdown(signal, servers, on_show=show)
    assert calls == ['show']
    assert servers[0].should_exit


def test_windows_named_show_event_is_per_executable_and_auto_reset(monkeypatch, tmp_path):
    if sys.platform != 'win32':
        return
    import desktop_launcher as launcher
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'client.exe'))
    one = launcher.ShutdownSignal()
    two = launcher.ShutdownSignal()
    try:
        assert not one.already_running
        assert two.already_running
        assert not one.requested()
        two.request_show()
        assert one.show_requested()
        assert not one.show_requested()
        assert not one.requested()
    finally:
        two.close()
        one.close()


def test_tray_action_dispatch_avoids_overlapping_callbacks():
    import desktop_tray
    import threading
    done, release = threading.Event(), threading.Event()
    calls = []
    def blocked():
        calls.append('open')
        done.set()
        release.wait(2)
    tray = desktop_tray.WindowsTray(lambda: None, lambda: None)
    tray._dispatch(blocked)
    assert done.wait(2)
    tray._dispatch(lambda: calls.append('overlap'))
    release.set()
    assert calls == ['open']
