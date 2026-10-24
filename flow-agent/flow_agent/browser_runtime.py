"""Validate the actual Playwright driver before opening any browser/session."""
from pathlib import Path
import subprocess


class BrowserRuntimeError(RuntimeError):
    def __init__(self, code, component):
        super().__init__(code)
        self.code, self.component = code, component


def require_playwright_driver(*, probe=False):
    try:
        from playwright._impl._driver import compute_driver_executable
        node, cli = compute_driver_executable()
    except ImportError:
        raise BrowserRuntimeError('browser_driver_missing', 'playwright') from None
    for component, name in [('node', node), ('cli', cli)]:
        path = Path(name)
        if not path.is_file() or path.stat().st_size == 0:
            raise BrowserRuntimeError('browser_driver_missing', component)
    if probe:
        try:
            result = subprocess.run([node, cli, '--version'], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise BrowserRuntimeError('browser_driver_failed', 'driver') from None
        if result.returncode:
            raise BrowserRuntimeError('browser_driver_failed', 'driver')
