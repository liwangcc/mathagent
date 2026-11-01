param(
    [string]$SidecarPath,
    [string]$TaskName = "MathAgent Platform Accelerator",
    [switch]$LeaveTaskEnabled,
    [int]$WaitSeconds = 30
)

$ErrorActionPreference = "Stop"
if (-not $SidecarPath) {
    $SidecarPath = Join-Path $env:LOCALAPPDATA "MathAgent\PlatformNode\mathagent-sidecar.exe"
}
$SidecarPath = [IO.Path]::GetFullPath($SidecarPath)

function Test-PlatformRootProcess {
    param([object]$Process)
    if ($null -eq $Process) { return $false }
    $path = if ($Process.PSObject.Properties.Name -contains "ExecutablePath") {
        [string]$Process.ExecutablePath
    }
    else {
        [string]$Process.Path
    }
    if (-not [String]::Equals($path, $SidecarPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    # CIM exposes the command line; Get-Process is the fallback for a normal
    # non-elevated user session where Win32_Process is access-denied. The
    # status PID has already been read from this exact sidecar executable.
    if ($Process.PSObject.Properties.Name -contains "CommandLine") {
        return [string]$Process.CommandLine -match '--platform-node(?:\s|$)'
    }
    return $true
}

function Get-PlatformRootProcesses {
    $cimProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    if ($cimProcesses.Count -gt 0) {
        return @($cimProcesses | Where-Object { Test-PlatformRootProcess $_ })
    }
    return @(Get-Process -Name "mathagent-sidecar" -ErrorAction SilentlyContinue | Where-Object {
        Test-PlatformRootProcess $_
    })
}

function Get-StatusProcessId {
    if (-not (Test-Path -LiteralPath $SidecarPath -PathType Leaf)) {
        return $null
    }
    try {
        $payload = (& $SidecarPath --platform-node-status 2>$null | ConvertFrom-Json)
        $candidate = 0
        if ($payload.platformNode -and [int]::TryParse([string]$payload.platformNode.pid, [ref]$candidate) -and $candidate -gt 0) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $candidate" -ErrorAction SilentlyContinue
            if ($null -eq $process) {
                $process = Get-Process -Id $candidate -ErrorAction SilentlyContinue
            }
            if (Test-PlatformRootProcess $process) {
                return $candidate
            }
        }
    }
    catch {
        # A stopped node may leave an unreadable or stale status file. The
        # process scan below is the safe fallback.
    }
    return $null
}

function Invoke-TreeKill {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return }
    # /T is intentional: the PyInstaller child and vksift_resident.exe are
    # descendants of this exact sidecar process and must stop together.
    try {
        & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    }
    catch {
        # The final process check below turns permission failures into one
        # actionable error instead of exposing native taskkill noise.
    }
}

$knownRootIds = @()
$statusPid = Get-StatusProcessId
if ($statusPid) { $knownRootIds += $statusPid }
$knownRootIds += @(Get-PlatformRootProcesses | ForEach-Object { [int]$_.ProcessId })
$knownRootIds = @($knownRootIds | Sort-Object -Unique)

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and -not $LeaveTaskEnabled) {
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
}
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(5, $WaitSeconds))
do {
    $currentRootIds = @(Get-PlatformRootProcesses | ForEach-Object { [int]$_.ProcessId })
    $allRootIds = @($knownRootIds + $currentRootIds | Sort-Object -Unique)
    if ($allRootIds.Count -eq 0) {
        break
    }
    foreach ($rootId in $allRootIds) {
        Invoke-TreeKill -ProcessId $rootId
    }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)

$remaining = @(Get-PlatformRootProcesses)
if ($remaining.Count -gt 0) {
    $remainingIds = ($remaining | ForEach-Object { [int]$_.ProcessId }) -join ","
    throw "The platform sidecar did not stop cleanly (remaining PID: $remainingIds). Run this control script as the same Windows user that installed the node."
}

$taskAfter = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($taskAfter -and $taskAfter.State -eq "Running") {
    throw "The platform task is still running."
}

[PSCustomObject]@{
    TaskName = $TaskName
    TaskDisabled = [bool]($task -and -not $LeaveTaskEnabled)
    TaskState = if ($taskAfter) { [string]$taskAfter.State } else { "NotInstalled" }
    RootPids = $knownRootIds
    Running = $false
}
