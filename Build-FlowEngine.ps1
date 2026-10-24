param([string]$GoPath, [string]$SourceRepository)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$commit = '70ec73861091267dd1f57184bcf809a922bea85b'
$patch = Join-Path $root 'native\browser-required.patch'
$patchHash = (Get-FileHash -LiteralPath $patch -Algorithm SHA256).Hash.ToLowerInvariant()
$source = Join-Path $root ('build\native-browser-' + $patchHash.Substring(0, 12))
if (-not $GoPath) {
    $bundled = Join-Path $root 'build\go-toolchain-1264\go\bin\go.exe'
    $GoPath = if (Test-Path -LiteralPath $bundled) { $bundled } else { (Get-Command go -ErrorAction Stop).Source }
}
if (-not $SourceRepository) {
    $cache = Join-Path $root 'build\upstream-flow-go'
    $SourceRepository = if (Test-Path -LiteralPath (Join-Path $cache '.git')) { $cache } else { 'https://github.com/kodelyx/flow-go.git' }
}
if (-not (Test-Path -LiteralPath $source)) {
    & git clone --no-hardlinks --no-checkout $SourceRepository $source
    if ($LASTEXITCODE -ne 0) { throw '无法取得原生引擎源码。' }
    & git -C $source -c core.autocrlf=false checkout --detach $commit
    if ($LASTEXITCODE -ne 0) { throw '固定源码提交不可用。' }
}
$head = & git -C $source rev-parse HEAD
if ($head -ne $commit) { throw '构建目录的源码提交不匹配，拒绝覆盖。' }
& git -C $source apply --reverse --check $patch 2>$null
if ($LASTEXITCODE -ne 0) {
    & git -C $source apply --check $patch
    if ($LASTEXITCODE -ne 0) { throw '浏览器验证补丁与源码不匹配。' }
    & git -C $source apply $patch
    if ($LASTEXITCODE -ne 0) { throw '补丁应用失败。' }
}
$savedEnv = @{}
foreach ($key in @('GOCACHE','GOMODCACHE','GOPATH','GOTOOLCHAIN','CGO_ENABLED')) { $savedEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }
try {
    $env:GOCACHE = Join-Path $root 'build\go-cache'
    $env:GOMODCACHE = Join-Path $root 'build\go-mod-cache'
    $env:GOPATH = Join-Path $root 'build\go-path'
    $env:GOTOOLCHAIN = 'local'
    $env:CGO_ENABLED = '0'
    Push-Location (Join-Path $source 'flow-go')
    try {
        & $GoPath test ./internal/bridge ./internal/recaptcha ./internal/batchexecute ./internal/engine ./internal/cli ./internal/app -count=1
        if ($LASTEXITCODE -ne 0) { throw '原生引擎离线回归失败。' }
        $output = Join-Path $root 'build\flow-windows-browser.exe'
        & $GoPath build -o $output .
        if ($LASTEXITCODE -ne 0) { throw '原生引擎构建失败。' }
    } finally { Pop-Location }
} finally {
    foreach ($key in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($key, $savedEnv[$key], 'Process') }
}
$binary = Join-Path $root 'flow-agent\bin\flow-windows.exe'
if (Test-Path -LiteralPath $binary) {
    $oldHash = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant()
    $backup = Join-Path $root ('build\flow-windows-before-browser-' + $oldHash.Substring(0, 12) + '.exe')
    if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $binary -Destination $backup }
}
Copy-Item -LiteralPath $output -Destination $binary -Force
@{ upstream_commit=$commit; patch_sha256=$patchHash; binary_sha256=(Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'build\browser-engine-build.json') -Encoding utf8
Write-Host "浏览器验证引擎已构建：$binary"
