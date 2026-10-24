"""Windows notification-area icon. No browser, network, or account access."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from pathlib import Path
from ctypes import wintypes as W

log = logging.getLogger(__name__)


def icon_path() -> Path:
    base = Path(sys._MEIPASS) / "payload" if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / "assets" / "flow-console.ico"


class WindowsTray:
    """Own the Win32 icon and its message loop on one dedicated thread."""

    def __init__(self, on_open, on_exit):
        self.on_open, self.on_exit = on_open, on_exit
        self.available = False
        self.hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._callback_lock = threading.Lock()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="flow-system-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
        return self.available

    def stop(self):
        self._stop.set()
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, 0x10, 0, 0)  # WM_CLOSE
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def notify_hidden(self):
        if self.available:
            self.user.PostMessageW(self.hwnd, 0x8002, 0, 0)

    def _dispatch(self, callback):
        # Native window.show/destroy may wait for WebView's UI thread. Never
        # block the tray message loop or launch unbounded callback threads.
        if not self._callback_lock.acquire(blocking=False):
            return
        def invoke():
            try:
                callback()
            except Exception:
                log.exception("托盘操作失败")
            finally:
                self._callback_lock.release()
        threading.Thread(target=invoke, name="flow-tray-action", daemon=True).start()

    def _run(self):
        user, shell, kernel = (ctypes.WinDLL(name, use_last_error=True)
                               for name in ("user32", "shell32", "kernel32"))
        self.user = user
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", W.UINT), ("proc", WNDPROC), ("clsExtra", ctypes.c_int),
                        ("wndExtra", ctypes.c_int), ("instance", W.HINSTANCE), ("icon", W.HICON),
                        ("cursor", W.HANDLE), ("background", W.HBRUSH),
                        ("menu", W.LPCWSTR), ("name", W.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("a", W.DWORD), ("b", W.WORD), ("c", W.WORD), ("d", ctypes.c_ubyte * 8)]

        class ICONDATA(ctypes.Structure):
            _fields_ = [("size", W.DWORD), ("hwnd", W.HWND), ("id", W.UINT),
                        ("flags", W.UINT), ("callback", W.UINT), ("icon", W.HICON),
                        ("tip", W.WCHAR * 128), ("state", W.DWORD), ("stateMask", W.DWORD),
                        ("info", W.WCHAR * 256), ("version", W.UINT), ("infoTitle", W.WCHAR * 64),
                        ("infoFlags", W.DWORD), ("guid", GUID), ("balloonIcon", W.HICON)]

        def api(lib, name, args, result):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
            return fn
        api(kernel, "GetModuleHandleW", [W.LPCWSTR], W.HMODULE)
        api(user, "RegisterClassW", [ctypes.POINTER(WNDCLASS)], W.WORD)
        api(user, "UnregisterClassW", [W.LPCWSTR, W.HINSTANCE], W.BOOL)
        api(user, "CreateWindowExW", [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU,
            W.HINSTANCE, W.LPVOID], W.HWND)
        api(user, "DefWindowProcW", [W.HWND, W.UINT, W.WPARAM, W.LPARAM], LRESULT)
        api(user, "DestroyWindow", [W.HWND], W.BOOL)
        api(user, "PostMessageW", [W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL)
        api(user, "GetMessageW", [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT], W.BOOL)
        api(user, "TranslateMessage", [ctypes.POINTER(W.MSG)], W.BOOL)
        api(user, "DispatchMessageW", [ctypes.POINTER(W.MSG)], LRESULT)
        api(user, "PostQuitMessage", [ctypes.c_int], None)
        api(user, "RegisterWindowMessageW", [W.LPCWSTR], W.UINT)
        api(user, "LoadImageW", [W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int,
                                ctypes.c_int, W.UINT], W.HANDLE)
        api(user, "DestroyIcon", [W.HICON], W.BOOL)
        api(user, "CreatePopupMenu", [], W.HMENU)
        api(user, "AppendMenuW", [W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR], W.BOOL)
        api(user, "DestroyMenu", [W.HMENU], W.BOOL)
        api(user, "GetCursorPos", [ctypes.POINTER(W.POINT)], W.BOOL)
        api(user, "SetForegroundWindow", [W.HWND], W.BOOL)
        api(user, "TrackPopupMenu", [W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, W.HWND, W.LPVOID], W.UINT)
        api(shell, "Shell_NotifyIconW", [W.DWORD, ctypes.POINTER(ICONDATA)], W.BOOL)
        taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")
        instance = kernel.GetModuleHandleW(None)
        name = f"FlowAgentTray-{id(self)}"
        icon = None
        registered = False
        data = ICONDATA()

        def add_icon():
            data.flags = 1 | 2 | 4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
            self.available = bool(shell.Shell_NotifyIconW(0, ctypes.byref(data)))
            return self.available

        def menu(hwnd):
            popup = user.CreatePopupMenu()
            if not popup:
                return
            try:
                user.AppendMenuW(popup, 0, 1, "打开管理界面")
                user.AppendMenuW(popup, 0x800, 0, None)
                user.AppendMenuW(popup, 0, 2, "彻底退出")
                point = W.POINT()
                user.GetCursorPos(ctypes.byref(point))
                user.SetForegroundWindow(hwnd)
                action = user.TrackPopupMenu(popup, 0x100 | 0x2, point.x, point.y, 0, hwnd, None)
                user.PostMessageW(hwnd, 0, 0, 0)
                if action == 1:
                    self._dispatch(self.on_open)
                elif action == 2:
                    self._dispatch(self.on_exit)
            finally:
                user.DestroyMenu(popup)

        @WNDPROC
        def wndproc(hwnd, message, wparam, lparam):
            try:
                if message == taskbar_created:
                    add_icon()  # Explorer restarted: restore our icon.
                elif message == 0x8001:
                    if lparam == 0x203:  # WM_LBUTTONDBLCLK
                        self._dispatch(self.on_open)
                    elif lparam in (0x205, 0x7B):  # WM_RBUTTONUP / WM_CONTEXTMENU
                        menu(hwnd)
                elif message == 0x8002:
                    data.flags = 0x10  # NIF_INFO; do not change callback or icon.
                    data.infoTitle = "Flow Agent 仍在后台运行"
                    data.info = "任务和 API 继续运行。右键托盘图标可打开管理界面或彻底退出。"
                    data.infoFlags = 1
                    shell.Shell_NotifyIconW(1, ctypes.byref(data))
                elif message == 0x10:
                    user.DestroyWindow(hwnd)
                elif message == 2:  # WM_DESTROY
                    user.PostQuitMessage(0)
                else:
                    return user.DefWindowProcW(hwnd, message, wparam, lparam)
            except Exception:
                log.exception("托盘消息处理失败")
            return 0

        try:
            wc = WNDCLASS(proc=wndproc, instance=instance, name=name)
            if not user.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            self.hwnd = user.CreateWindowExW(0, name, "Flow Agent 后台托盘", 0,
                                             0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            icon = user.LoadImageW(None, str(icon_path()), 1, 32, 32, 0x10)  # LR_LOADFROMFILE
            if not icon:
                raise ctypes.WinError(ctypes.get_last_error())
            data.size, data.hwnd, data.id = ctypes.sizeof(data), self.hwnd, 1
            data.callback, data.icon, data.tip = 0x8001, icon, "Flow Agent · 后台运行（右键退出）"
            if not add_icon():
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            if self._stop.is_set():
                user.PostMessageW(self.hwnd, 0x10, 0, 0)
            message = W.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception:
            log.exception("无法创建系统托盘图标；关闭窗口将正常退出")
        finally:
            self.available = False
            if data.hwnd:
                shell.Shell_NotifyIconW(2, ctypes.byref(data))  # NIM_DELETE
            if self.hwnd:
                user.DestroyWindow(self.hwnd)
                self.hwnd = None
            if icon:
                user.DestroyIcon(icon)
            if registered:
                user.UnregisterClassW(name, instance)
            self._ready.set()
