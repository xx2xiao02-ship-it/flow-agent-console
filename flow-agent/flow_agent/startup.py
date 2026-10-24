"""Current-user Windows startup; no administrator rights or secrets required."""
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "FlowAgentConsole"

def status():
    supported = sys.platform == "win32" and bool(getattr(sys, "frozen", False))
    command = f'"{Path(sys.executable).resolve()}" --no-browser'
    enabled = False
    if supported:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, VALUE_NAME)
                enabled = value == command
        except FileNotFoundError:
            pass
    return {"enabled": enabled, "supported": supported}

def configure(enabled: bool):
    if not status()["supported"]:
        raise RuntimeError("请在安装后的 Windows 客户端中设置开机启动")
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                             f'"{Path(sys.executable).resolve()}" --no-browser')
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    return status()
