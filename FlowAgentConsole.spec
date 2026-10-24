# PyInstaller Windows onefile build; runtime account data is never bundled.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

workspace = Path(SPECPATH)

a = Analysis(
    [str(workspace / "flow-agent" / "desktop_launcher.py")],
    pathex=[str(workspace / "flow-agent")],
    binaries=[(str(workspace / "flow-agent" / "bin" / "flow-windows.exe"),
               "payload/flow-agent/bin")],
    datas=[(str(workspace / "web" / "dist"), "payload/web/dist"),
           (str(workspace / "flow-agent" / "assets" / "flow-console.ico"), "payload/assets"),
           (str(workspace / "docs" / "INTEGRATION_API.md"), "payload/docs"),
           (str(workspace / "docs" / "MCP_SKILL.md"), "payload/docs"),
           (str(workspace / "docs" / "Flow-MCP-Skill.zip"), "payload/docs"),
           (str(workspace / "flow-extension"), "payload/flow-extension")],
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("webview") + collect_submodules("websockets"),
    hookspath=[],
    hooksconfig={},
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FlowAgentConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(workspace / "flow-agent" / "assets" / "flow-console.ico"),
)

# Installed service uses persistent runtime files, not an unpacked TEMP/_MEI
# directory which a cleanup utility can remove while the server stays alive.
# Keep the original onefile artifact for compatibility.
installed_exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="FlowAgentConsole", debug=False, console=False, upx=False,
    contents_directory="app-runtime",
    icon=str(workspace / "flow-agent" / "assets" / "flow-console.ico"),
)
installed = COLLECT(
    installed_exe, a.binaries, a.datas, strip=False, upx=False,
    name="FlowAgentConsoleInstalled",
)
