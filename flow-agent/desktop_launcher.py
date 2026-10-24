"""Windows desktop entry point; works from source and PyInstaller onefile."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

import uvicorn
from desktop_tray import WindowsTray


class ShutdownSignal:
    """Per-install Windows event, shared with the upgrade helper (no HTTP API)."""

    def __init__(self):
        self.handle = None
        self.show_handle = None
        self.api = None
        self.already_running = False
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                         wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateEventW.restype = wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.SetEvent.argtypes = [wintypes.HANDLE]
        self.api.SetEvent.restype = wintypes.BOOL
        executable = str(Path(sys.executable).resolve()).lower()
        name = "Local\\FlowAgentConsole-" + hashlib.sha256(executable.encode("utf-8")).hexdigest()[:24]
        self.handle = self.api.CreateEventW(None, True, False, name)
        self.already_running = ctypes.get_last_error() == 183
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.show_handle = self.api.CreateEventW(None, False, False, name + "-Show")
        if not self.show_handle:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def requested(self) -> bool:
        return bool(self.handle and self.api.WaitForSingleObject(self.handle, 0) == 0)

    def show_requested(self) -> bool:
        return bool(self.show_handle and self.api.WaitForSingleObject(self.show_handle, 0) == 0)

    def request_show(self):
        if self.show_handle:
            self.api.SetEvent(self.show_handle)

    def close(self):
        if self.show_handle:
            self.api.CloseHandle(self.show_handle)
            self.show_handle = None
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _watch_shutdown(signal: ShutdownSignal, servers, window=None, on_exit=None, on_show=None):
    while not all(server.should_exit for server in servers):
        if signal.requested():
            if on_exit:
                on_exit()
                return
            for server in servers:
                server.should_exit = True
            if window is not None:
                window.destroy()
            return
        if on_show and signal.show_requested():
            on_show()
        time.sleep(0.1)


def _exit_watchdog():
    # Native WebView/CLR threads can outlive the closed UI. Allow cleanup first,
    # then release the EXE rather than leave an invisible installation blocker.
    time.sleep(12)
    os._exit(0)


def _log(message: str) -> None:
    """Write diagnostics when a console exists; frozen windowed EXE has no stdout."""
    try:
        print(message, flush=True)
    except (AttributeError, OSError):
        pass


def check_listen_ports(port: int, lan_port: int | None = None):
    """Probe only our requested bind addresses. Never stop the port owner."""
    import socket
    sockets = []
    try:
        for host, value in [('127.0.0.1', port)] + ([('0.0.0.0', lan_port)] if lan_port else []):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(probe)
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, value))
            except OSError:
                raise RuntimeError(f'启动自查失败：端口 {value} 被占用或无权监听。请关闭占用该端口的旧服务，或更换启动端口；原服务未被终止。') from None
    finally:
        for probe in sockets:
            probe.close()


def _workspace_root() -> Path:
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates = (executable_dir, executable_dir.parent)
        for candidate in candidates:
            if ((candidate / "flow-agent" / "bin" / "flow-windows.exe").is_file()
                    and (candidate / "web" / "dist" / "index.html").is_file()):
                return candidate
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FlowAgentConsole"
        payload = Path(sys._MEIPASS) / "payload"
        for relative in ("flow-agent/bin/flow-windows.exe", "flow-extension/manifest.json"):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(payload / relative, target)
        shutil.copytree(payload / "flow-extension", root / "flow-extension", dirs_exist_ok=True)
        shutil.copytree(payload / "web" / "dist", root / "web" / "dist", dirs_exist_ok=True)
        return root
    return Path(__file__).resolve().parent.parent


async def _open_console(port: int, server: uvicorn.Server):
    while not server.started and not server.should_exit:
        await asyncio.sleep(0.1)
    if server.started:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/console/")


class DesktopLifetime:
    """Closing hides the UI; only an explicit exit shuts down the API."""

    def __init__(self, window, servers):
        self.window, self.servers = window, servers
        self.exiting = threading.Event()
        self.tray = WindowsTray(self.show, self.exit)
        self._notified = False

    def show(self):
        if not self.exiting.is_set():
            self.window.show()
            self.window.restore()

    def closing(self):
        if not self.exiting.is_set() and self.tray.available:
            self.window.hide()
            if not self._notified:
                self.tray.notify_hidden()
                self._notified = True
            return False  # pywebview: cancel destruction, keep the API alive.
        return True

    def closed(self):
        self.exiting.set()
        for server in self.servers:
            server.should_exit = True

    def exit(self):
        if self.exiting.is_set():
            return
        self.closed()
        self.window.destroy()


def _native_window(local_app, lan_app, port: int, lan_port: int, signal=None,
                   hidden=False) -> None:
    """Run the API behind a native WebView2 window instead of an external browser."""
    import webview

    local_server = uvicorn.Server(uvicorn.Config(
        local_app, host="127.0.0.1", port=port, log_level="info", access_log=False,
        log_config=None, timeout_graceful_shutdown=5,
    ))
    servers = [local_server]
    if lan_app is not None:
        servers.append(uvicorn.Server(uvicorn.Config(
            lan_app, host="0.0.0.0", port=lan_port, log_level="info", access_log=False,
            log_config=None, timeout_graceful_shutdown=5,
        )))
    threads = [threading.Thread(target=server.run, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        while not local_server.started and not local_server.should_exit:
            if not threads[0].is_alive():
                raise RuntimeError("本机服务未能启动，请检查 8001 端口是否已被旧程序占用。")
            time.sleep(0.05)
        window = webview.create_window(
            "Flow Agent 中文管理后台",
            f"http://127.0.0.1:{port}/console/",
            width=1440,
            height=960,
            min_size=(1100, 700),
            confirm_close=False,
            hidden=hidden,
        )
        lifetime = DesktopLifetime(window, servers)
        window.events.closing += lifetime.closing
        def closed():
            lifetime.closed()
            if getattr(sys, "frozen", False):
                threading.Thread(target=_exit_watchdog, daemon=True).start()
        window.events.closed += closed
        tray_ready = lifetime.tray.start()
        if hidden and not tray_ready:
            # Never leave a hidden window without a usable way to restore it.
            window.hidden = False
        if signal:
            threading.Thread(target=_watch_shutdown,
                             args=(signal, servers),
                             kwargs={"on_exit": lifetime.exit, "on_show": lifetime.show},
                             daemon=True).start()
        webview.start(gui="edgechromium", debug=False)
    finally:
        if 'lifetime' in locals():
            lifetime.tray.stop()
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=5)


async def _serve(local_app, lan_app, port: int, lan_port: int, open_browser: bool, signal=None):
    local_server = uvicorn.Server(uvicorn.Config(local_app, host="127.0.0.1", port=port,
                                               log_level="info", access_log=False,
                                               log_config=None, timeout_graceful_shutdown=5))
    servers = [local_server]
    if lan_app is not None:
        servers.append(uvicorn.Server(uvicorn.Config(lan_app, host="0.0.0.0", port=lan_port,
                                                    log_level="info", access_log=False,
                                                    log_config=None, timeout_graceful_shutdown=5)))
    def show():
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/console/")
    def exit_servers():
        for server in servers:
            server.should_exit = True
    tray = WindowsTray(show, exit_servers)
    tray.start()
    if signal:
        threading.Thread(target=_watch_shutdown, args=(signal, servers),
                         kwargs={"on_show": show}, daemon=True).start()
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    if open_browser:
        tasks.append(asyncio.create_task(_open_console(port, local_server)))
    server_tasks = tasks[:len(servers)]
    try:
        await asyncio.wait(server_tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        tray.stop()
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*server_tasks, return_exceptions=True)
        for task in tasks[len(servers):]:
            if not task.done():
                task.cancel()


def main() -> int:
    parser = argparse.ArgumentParser(description="Flow Agent Windows 中文管理后台")
    parser.add_argument("--port", type=int, default=8001, help="本机控制台端口，默认 8001")
    parser.add_argument("--lan", action="store_true", help="另开带密钥的局域网 API")
    parser.add_argument("--lan-port", type=int, default=8002, help="局域网 API 端口，默认 8002")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--browser", action="store_true", help="使用系统浏览器打开管理页面（兼容模式）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.lan_port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    if args.lan and args.port == args.lan_port:
        parser.error("本机控制台和局域网 API 端口不能相同")

    signal = ShutdownSignal()
    if getattr(sys, "frozen", False) and signal.already_running:
        if not args.no_browser:
            signal.request_show()
        signal.close()
        return 0  # Do not overwrite the active engine or create an invisible duplicate.
    try:
        check_listen_ports(args.port, args.lan_port if args.lan else None)
    except RuntimeError as exc:
        _log(str(exc))
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Flow Agent 启动自查", 0x10)
        signal.close()
        return 2
    root = _workspace_root()
    if getattr(sys, "frozen", False) and not os.environ.get("FLOW_AGENT_BROWSER"):
        installed_browser = Path(sys.executable).resolve().parent / "browser" / "chrome-win64" / "chrome.exe"
        if installed_browser.is_file():
            os.environ["FLOW_AGENT_BROWSER"] = str(installed_browser)
    os.environ["FLOW_AGENT_ROOT"] = str(root / "flow-agent")
    os.environ["FLOW_AGENT_LAN_ENABLED"] = "1" if args.lan else "0"
    os.environ["FLOW_AGENT_LAN_PORT"] = str(args.lan_port)
    from flow_agent.api import app, console_service
    console_service.browser_pages.port = args.port
    from flow_agent.lan_api import create_lan_app
    from flow_agent.lan_keys import LanKeyStore

    lan_app = None
    if args.lan:
        key_store = LanKeyStore(root / "flow-agent")
        key_store.ensure()
        lan_app = create_lan_app(console_service, key_store)
        _log(f"局域网 API 已启用，端口 {args.lan_port}；密钥文件：{key_store.path}")
    _log(f"本机管理页面：http://127.0.0.1:{args.port}/console/")
    try:
        if args.no_browser and sys.platform != "win32":
            asyncio.run(_serve(app, lan_app, args.port, args.lan_port, False, signal))
        elif args.browser:
            asyncio.run(_serve(app, lan_app, args.port, args.lan_port, not args.no_browser, signal))
        else:
            _native_window(app, lan_app, args.port, args.lan_port, signal,
                           hidden=args.no_browser)
    except ImportError as exc:
        if "webview" not in str(exc):
            raise
        _log("缺少 pywebview，无法打开独立窗口；请安装桌面依赖，或使用 --browser。")
        asyncio.run(_serve(app, lan_app, args.port, args.lan_port, not args.no_browser, signal))
    except KeyboardInterrupt:
        pass
    finally:
        signal.close()
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        try:
            log_root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FlowAgentConsole"
            log_root.mkdir(parents=True, exist_ok=True)
            (log_root / "launcher-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
    else:
        if getattr(sys, "frozen", False):
            os._exit(result)  # CLR/WebView threads must not retain the onefile EXE.
        raise SystemExit(result)
