param(
    [Parameter(Mandatory = $true)][string]$AppPath,
    [string]$LogPath = (Join-Path $env:LOCALAPPDATA 'FlowAgentConsole\upgrade-close.log')
)

$ErrorActionPreference = 'Stop'
try {
    $targetExe = [IO.Path]::GetFullPath($AppPath)
    if ([IO.Path]::GetFileName($targetExe) -ine 'FlowAgentConsole.exe') {
        throw '目标必须是 FlowAgentConsole.exe。'
    }
    $installRoot = [IO.Path]::GetDirectoryName($targetExe)
    $mcpExe = Join-Path $installRoot 'FlowConsoleMCP.exe'
    $browserExe = Join-Path $installRoot 'browser\chrome-win64\chrome.exe'
    $snapshot = @(Get-CimInstance Win32_Process)
    if (@($snapshot | Where-Object { $_.Name -in @('FlowAgentConsole.exe', 'FlowConsoleMCP.exe') -and -not $_.ExecutablePath }).Count) {
        throw [UnauthorizedAccessException]::new('无法读取后台进程路径，需要与旧程序相同的权限。')
    }
    $roots = @($snapshot | Where-Object { $_.ExecutablePath -ieq $targetExe })
    $ownedIds = [Collections.Generic.HashSet[uint32]]::new()
    foreach ($process in $roots) { [void]$ownedIds.Add([uint32]$process.ProcessId) }
    do {
        $changed = $false
        foreach ($process in $snapshot) {
            if ($ownedIds.Contains([uint32]$process.ParentProcessId)) {
                if ($ownedIds.Add([uint32]$process.ProcessId)) { $changed = $true }
            }
        }
    } while ($changed)

    # Refuse to interrupt a known generation. No retry/resubmission is performed.
    if ($roots.Count) {
        try {
            $jobs = Invoke-RestMethod 'http://127.0.0.1:8001/console/api/jobs' -TimeoutSec 2
        } catch { $jobs = @() }
        if (@($jobs | Where-Object { $_.status -in @('queued', 'running') }).Count) {
            [IO.File]::WriteAllText($LogPath, '后台有排队或生成任务，请等待任务结束后再升级。')
            exit 3
        }
    }

    # The new launcher watches this per-install local Windows event.
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($targetExe.ToLowerInvariant()))
        $name = 'Local\FlowAgentConsole-' + ([BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()).Substring(0, 24)
    } finally { $sha.Dispose() }
    $signal = $null
    try {
        $signal = [Threading.EventWaitHandle]::OpenExisting($name)
        [void]$signal.Set()
    } catch [Threading.WaitHandleCannotBeOpenedException] {
        # Legacy releases have no event. Try closing their visible main window.
        foreach ($process in $roots) {
            $instance = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
            if ($instance) { [void]$instance.CloseMainWindow() }
        }
    } finally { if ($signal) { $signal.Dispose() } }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        # A 32-bit installer can launch 32-bit PowerShell; Process.Path is then
        # empty for a 64-bit app. WMI works across bitness on Windows.
        $remaining = @(Get-CimInstance Win32_Process -Filter "Name='FlowAgentConsole.exe'" |
                       Where-Object { $_.ExecutablePath -ieq $targetExe })
        if (-not $remaining.Count) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    # Compatibility cleanup: only the exact installed EXE and its captured
    # engine/WebView children. Never Stop-Process by a broad browser/app name.
    $selected = @($snapshot | Where-Object {
        ($_.ExecutablePath -ieq $targetExe) -or ($_.ExecutablePath -ieq $mcpExe) -or
        ($ownedIds.Contains([uint32]$_.ProcessId) -and
         $_.Name -in @('flow-windows.exe', 'msedgewebview2.exe')) -or
        ($_.ExecutablePath -ieq $browserExe)
    })
    foreach ($process in ($selected | Sort-Object ProcessId -Descending)) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ProcessId)"
        # Verify path and creation time again, including across 32/64-bit hosts.
        if ($current -and $current.ExecutablePath -ieq $process.ExecutablePath -and
            $current.CreationDate -eq $process.CreationDate) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        }
    }
    foreach ($process in $selected) {
        Wait-Process -Id $process.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    }
    $remaining = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -ieq $targetExe -or $_.ExecutablePath -ieq $mcpExe -or $_.ExecutablePath -ieq $browserExe -or
        ($ownedIds.Contains([uint32]$_.ProcessId) -and $_.Name -in @('flow-windows.exe', 'msedgewebview2.exe'))
    })
    if ($remaining.Count) { throw '应用仍被占用，请使用与旧程序相同的 Windows 权限运行安装包。' }
    Write-Output 'Flow Agent 已关闭，可以安装升级。'
    exit 0
} catch {
    try { [IO.File]::WriteAllText($LogPath, $_.Exception.Message) } catch {}
    [Console]::Error.WriteLine($_.Exception.Message)
    if ($_.Exception -is [UnauthorizedAccessException] -or
        $_.Exception.Message -match '拒绝|权限|访问|access|denied|unauthorized|CIM') { exit 5 }
    exit 1
}

