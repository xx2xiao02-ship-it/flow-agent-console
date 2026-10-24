param([int]$Port = 8001)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'flow-agent'
$web = Join-Path $root 'web'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if ($Port -lt 1 -or $Port -gt 65535) { throw '端口必须在 1 到 65535 之间。' }
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host '首次创建 Python 虚拟环境…'
    python -m venv (Join-Path $backend '.venv')
    if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败，请安装 Python 3.10+。' }
}
& $python -c 'import fastapi, uvicorn, pydantic, multipart, webview, playwright.async_api'
if ($LASTEXITCODE -ne 0) {
    Write-Host '正在安装 Python 依赖…'
    Push-Location $backend
    try {
        & $python -m pip install -e '.[test,desktop]'
        if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $web 'dist\index.html'))) {
    Write-Host '首次构建中文管理页面…'
    Push-Location $web
    try {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败，请安装 Node.js 20.19+。' }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally { Pop-Location }
}

$browserReady = $false
if ($env:FLOW_AGENT_BROWSER -and (Test-Path -LiteralPath $env:FLOW_AGENT_BROWSER -PathType Leaf)) {
    $browserReady = $true
} elseif ($env:LOCALAPPDATA) {
    $browserRoot = Join-Path $env:LOCALAPPDATA 'ms-playwright'
    if (Test-Path -LiteralPath $browserRoot) {
        $browserReady = [bool](Get-ChildItem -LiteralPath $browserRoot -Directory -Filter 'chromium-*' |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'chrome-win64\chrome.exe') } |
            Select-Object -First 1)
    }
}
if (-not $browserReady) {
    Write-Host '首次安装账号连接专用浏览器（Chrome for Testing），请稍候…'
    & $python -m pip install 'playwright>=1.56,<2'
    if ($LASTEXITCODE -ne 0) { throw '专用浏览器工具安装失败，请检查网络。' }
    & $python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw '专用浏览器下载失败，请检查网络。' }
}

$url = "http://127.0.0.1:$Port/console/"
Write-Host "启动独立管理窗口：$url"
Write-Host '关闭管理窗口会隐藏到右下角系统托盘；右键托盘图标选择“彻底退出”，也可按 Ctrl+C 停止。'
Push-Location $backend
try { & $python desktop_launcher.py --port $Port }
finally { Pop-Location }
