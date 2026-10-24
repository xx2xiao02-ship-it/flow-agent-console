param(
    [Parameter(Mandatory = $true)]
    [string]$CertificateThumbprint,
    [ValidateSet('CurrentUser', 'LocalMachine')]
    [string]$CertificateStore = 'CurrentUser',
    [string]$TimestampServer = 'http://timestamp.digicert.com',
    [string]$SignToolPath
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$thumbprint = ($CertificateThumbprint -replace '\s', '').ToUpperInvariant()
if ($thumbprint -notmatch '^[0-9A-F]{40}$') { throw '证书指纹必须是 40 位十六进制字符。' }
if ($TimestampServer -notmatch '^https?://') { throw '时间戳地址必须是 HTTP 或 HTTPS URL。' }

$certPath = "Cert:\$CertificateStore\My\$thumbprint"
$cert = Get-Item -LiteralPath $certPath -ErrorAction SilentlyContinue
if (-not $cert -or -not $cert.HasPrivateKey) {
    throw '未找到带私钥的代码签名证书。请先取得受信任 CA 签发的证书，并导入所选 Windows 证书库。'
}
if ($cert.NotBefore -gt (Get-Date) -or $cert.NotAfter -le (Get-Date)) {
    throw '代码签名证书尚未生效或已经过期。'
}
$eku = @($cert.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId.Value })
if ($eku.Count -gt 0 -and '1.3.6.1.5.5.7.3.3' -notin $eku) {
    throw '所选证书不具备代码签名用途。'
}

if (-not $SignToolPath) {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { $SignToolPath = $command.Source }
}
if (-not $SignToolPath) {
    $bundled = Join-Path $root 'build\signing-tools\sdk-buildtools'
    $SignToolPath = Get-ChildItem -LiteralPath $bundled -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $SignToolPath -or -not (Test-Path -LiteralPath $SignToolPath -PathType Leaf)) {
    throw '未找到 Microsoft SignTool。请安装 Windows SDK，或通过 -SignToolPath 指定 signtool.exe。'
}

$source = Join-Path $root 'dist\FlowAgentConsoleInstalled\FlowAgentConsole.exe'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw '缺少安装版 EXE，请先运行 Build-FlowConsoleExe.ps1。' }
$stage = Join-Path $root 'build\signing-release'
$output = Join-Path $root 'dist\signed'
New-Item -ItemType Directory -Path $stage, $output -Force | Out-Null
$signedApp = Join-Path $stage 'FlowAgentConsole.exe'
Copy-Item -LiteralPath $source -Destination $signedApp -Force

function Sign-And-Verify([string]$filePath) {
    $signArgs = @('sign', '/fd', 'SHA256', '/tr', $TimestampServer, '/td', 'SHA256',
                  '/sha1', $thumbprint, '/s', 'My')
    if ($CertificateStore -eq 'LocalMachine') { $signArgs += '/sm' }
    $signArgs += $filePath
    & $SignToolPath @signArgs
    if ($LASTEXITCODE -ne 0) { throw "签名失败：$filePath" }
    & $SignToolPath verify /pa /v $filePath
    if ($LASTEXITCODE -ne 0) { throw "签名验证失败：$filePath" }
    $signature = Get-AuthenticodeSignature -LiteralPath $filePath
    if ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate) {
        throw "签名不受当前 Windows 信任，或缺少时间戳：$filePath；状态：$($signature.Status)"
    }
}

Sign-And-Verify $signedApp
& (Join-Path $root 'Build-FlowConsoleInstaller.ps1') -AppExePath $signedApp -InstallerOutputDir $output
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw '安装包重新构建失败。' }
$recipe = Join-Path $root 'FlowAgentConsoleInstaller.iss'
$versionMatch = [regex]::Match((Get-Content -LiteralPath $recipe -Raw), '(?m)^#define AppVersion "(\d+\.\d+\.\d+)"\s*$')
if (-not $versionMatch.Success) { throw '安装配方中缺少有效的 AppVersion。' }
$setup = Join-Path $output "FlowAgentConsole_Setup_$($versionMatch.Groups[1].Value).exe"
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw '未生成待签名的安装包。' }
Sign-And-Verify $setup

$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash
Write-Host "受信任的签名安装包：$setup"
Write-Host "SHA-256：$hash"
