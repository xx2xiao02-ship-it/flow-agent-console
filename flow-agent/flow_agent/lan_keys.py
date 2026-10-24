"""Local storage and Windows clipboard access for the optional LAN API key.

The key is never returned by a web endpoint. The browser receives only
non-secret status metadata; the actual value stays in this server-side file.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


class LanKeyStore:
    def __init__(self, engine_root: Path):
        self.path = engine_root / "data" / "lan_api_key.txt"

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                with self.path.open("x", encoding="ascii") as stream:
                    stream.write(secrets.token_urlsafe(48))
            except FileExistsError:
                pass
        self.read()

    def read(self) -> str:
        key = self.path.read_text(encoding="ascii").strip()
        if len(key) < 32:
            raise ValueError("局域网 API 密钥文件无效；请在本机检查该文件")
        return key

    def status(self) -> dict:
        exists = self.path.is_file()
        valid = False
        if exists:
            try:
                self.read()
                valid = True
            except (OSError, UnicodeError, ValueError):
                pass
        return {
            "configured": valid,
            "key_file": str(self.path),
            "updated_at": datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds") if exists else None,
        }

    def rotate(self) -> dict:
        """Replace the key atomically; LAN requests read the current file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(48))
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.status()

    def copy_to_windows_clipboard(self) -> None:
        if os.name != "nt":
            raise RuntimeError("仅支持 Windows 剪贴板")
        # Pass the key over stdin, never on the command line or in HTTP output.
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$value = [Console]::In.ReadToEnd(); Set-Clipboard -Value $value"],
            input=self.read(), text=True, capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            raise RuntimeError("复制失败，请检查本机剪贴板或打开密钥文件")
