$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$releaseVersion = [regex]::Match((Get-Content (Join-Path $root 'FlowAgentConsoleInstaller.iss') -Raw), '#define AppVersion "([^"]+)"').Groups[1].Value
if (-not $releaseVersion) { throw '缺少发布版本号' }
Set-Content (Join-Path $root 'flow-agent\flow_agent\build_info.py') "VERSION = '$releaseVersion'" -Encoding utf8
$python = Join-Path $root 'flow-agent\.venv\Scripts\python.exe'
$webDist = Join-Path $root 'web\dist\index.html'
$binary = Join-Path $root 'flow-agent\bin\flow-windows.exe'
$env:UV_CACHE_DIR = Join-Path $root '.uv-cache'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $root '.pyinstaller-cache'

# Include the strict account-bound engine, with offline regression tests.
& (Join-Path $root 'Build-FlowEngine.ps1')
if ($LASTEXITCODE -ne 0) { throw '浏览器验证引擎构建失败。' }

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw '请先运行 Start-FlowConsole.ps1，完成 Python 环境安装。'
}
if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
    throw '缺少 Flow Windows 引擎：flow-agent\bin\flow-windows.exe'
}
if ($true) { # Always rebuild versioned frontend before packaging.
    Push-Location (Join-Path $root 'web')
    try {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally { Pop-Location }
}
& $python -c 'import PyInstaller, webview, playwright.async_api, PIL'
if ($LASTEXITCODE -ne 0) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv pip install --python $python 'pyinstaller>=6.0,<7' 'pywebview>=6.0,<7' 'playwright>=1.56,<2' 'pillow>=11,<13'
    } else {
        & $python -m ensurepip --upgrade
        if ($LASTEXITCODE -eq 0) { & $python -m pip install 'pyinstaller>=6.0,<7' 'pywebview>=6.0,<7' 'playwright>=1.56,<2' 'pillow>=11,<13' }
    }
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败。' }
}
Push-Location $root
try {
    & $python -m PyInstaller .\FlowAgentConsole.spec --noconfirm --distpath .\dist --workpath .\build\pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'EXE 打包失败。' }
    & $python -m PyInstaller --onedir --contents-directory mcp-runtime --console --noconfirm --name FlowConsoleMCP --paths (Join-Path $root 'flow-agent') --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build\mcp-exe') --specpath (Join-Path $root 'build') (Join-Path $root 'flow-agent\console_mcp_entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'MCP 程序打包失败。' }
} finally { Pop-Location }
Write-Host "已生成：$(Join-Path $root 'dist\FlowAgentConsole.exe')"
Write-Host "安装版：$(Join-Path $root 'dist\FlowAgentConsoleInstalled\FlowAgentConsole.exe')（必须与 app-runtime 一起分发）"
