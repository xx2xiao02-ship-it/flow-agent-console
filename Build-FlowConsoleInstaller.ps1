param(
    [string]$AppExePath,
    [string]$InstallerOutputDir,
    [string]$CompilerPath,
    [string]$AppRuntimePath
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$exe = if ($AppExePath) { [IO.Path]::GetFullPath($AppExePath) } else { Join-Path $root 'dist\FlowAgentConsoleInstalled\FlowAgentConsole.exe' }
$appRuntime = if ($AppRuntimePath) { [IO.Path]::GetFullPath($AppRuntimePath) } else { Join-Path $root 'dist\FlowAgentConsoleInstalled\app-runtime' }
foreach ($required in @('playwright\driver\node.exe','playwright\driver\package\cli.js','python314.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $appRuntime $required) -PathType Leaf)) {
        throw "安装版运行组件缺失：$required，请重新构建客户端。"
    }
}
$recipe = Join-Path $root 'FlowAgentConsoleInstaller.iss'
$versionMatch = [regex]::Match((Get-Content -LiteralPath $recipe -Raw), '(?m)^#define AppVersion "(\d+\.\d+\.\d+)"\s*$')
if (-not $versionMatch.Success) { throw '安装配方中缺少有效的 AppVersion。' }
$appVersion = $versionMatch.Groups[1].Value
$compilerCandidates = @(
    $CompilerPath,
    (Join-Path $root 'build\tools\InnoSetup7\ISCC.exe'),
    'C:\Program Files\Inno Setup 7\ISCC.exe',
    'C:\Program Files (x86)\Inno Setup 7\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe',
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
)
$compiler = $compilerCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
if (-not $compiler) { throw '未找到 Inno Setup 编译器 ISCC.exe，请先安装 Inno Setup。' }
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw '缺少便携版 EXE。请先运行 Build-FlowConsoleExe.ps1。'
}
$browserCache = Join-Path $env:LOCALAPPDATA 'ms-playwright'
$browser = Get-ChildItem -LiteralPath $browserCache -Directory -Filter 'chromium-*' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^chromium-(\d+)$' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'chrome-win64\chrome.exe') -PathType Leaf) } |
    Sort-Object { [int]($_.Name -replace '^chromium-', '') } -Descending |
    Select-Object -First 1
if (-not $browser) {
    throw '缺少可打包的专用浏览器。请先运行 Start-FlowConsole.ps1 安装 Chrome for Testing。'
}
$browserRoot = Join-Path $browser.FullName 'chrome-win64'
$output = if ($InstallerOutputDir) { [IO.Path]::GetFullPath($InstallerOutputDir) } else { Join-Path $root 'dist\installer' }
New-Item -ItemType Directory -Path $output -Force | Out-Null
Push-Location $root
try {
    & $compiler "/DBrowserRoot=$browserRoot" "/DAppExePath=$exe" "/DAppRuntimeRoot=$appRuntime" "/DInstallerOutputDir=$output" $recipe
    if ($LASTEXITCODE -ne 0) { throw "安装包编译失败（退出码 $LASTEXITCODE）。" }
} finally { Pop-Location }
$setup = Join-Path $output "FlowAgentConsole_Setup_$appVersion.exe"
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw '编译器未生成预期的 Setup EXE。' }
$item = Get-Item -LiteralPath $setup
$sha = Get-FileHash -LiteralPath $setup -Algorithm SHA256
Write-Host "安装包：$($item.FullName)"
Write-Host "大小：$([math]::Round($item.Length / 1MB, 1)) MB"
Write-Host "SHA-256：$($sha.Hash)"
