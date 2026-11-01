param(
    [string]$SidecarPath,
    [string]$TaskName = "MathAgent Platform Accelerator",
    [int]$WaitSeconds = 45
)

$ErrorActionPreference = "Stop"
if (-not $SidecarPath) {
    $SidecarPath = Join-Path $env:LOCALAPPDATA "MathAgent\PlatformNode\mathagent-sidecar.exe"
}
$SidecarPath = (Resolve-Path -LiteralPath $SidecarPath -ErrorAction Stop).Path
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "The platform node task is not installed. Run setup-platform-node.ps1 first."
}

$previousPid = 0
try {
    $previous = (& $SidecarPath --platform-node-status | ConvertFrom-Json).platformNode
    if ($previous) {
        [void][int]::TryParse([string]$previous.pid, [ref]$previousPid)
    }
}
catch {
    # A missing or malformed prior status cannot prevent a fresh start.
}

Enable-ScheduledTask -TaskName $TaskName | Out-Null
Start-ScheduledTask -TaskName $TaskName

$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(5, $WaitSeconds))
$runtime = $null
$runtimeHasFreshProcess = $false
do {
    Start-Sleep -Milliseconds 500
    try {
        $runtime = (& $SidecarPath --platform-node-status | ConvertFrom-Json).platformNode
        $runtimePid = 0
        if (
            $runtime -and
            [int]::TryParse([string]$runtime.pid, [ref]$runtimePid) -and
            $runtimePid -gt 0 -and
            ($previousPid -le 0 -or $runtimePid -ne $previousPid)
        ) {
            $runtimeHasFreshProcess = $true
        }
    }
    catch {
        $runtime = $null
        $runtimeHasFreshProcess = $false
    }
} while (
    [DateTime]::UtcNow -lt $deadline -and
    (
        $null -eq $runtime -or
        -not $runtimeHasFreshProcess -or
        $runtime.state -in @("starting", "stale")
    )
)

if ($null -eq $runtime -or -not $runtimeHasFreshProcess) {
    throw "The platform node did not publish a lifecycle status."
}
if ($runtime.state -eq "registration_unverified" -and -not [string]::IsNullOrWhiteSpace([string]$runtime.lastErrorCode)) {
    throw "The platform node could not verify its server registration: $($runtime.lastErrorCode)"
}
if ($runtime.state -in @("credential_missing", "credential_error", "credential_rejected", "registration_unverified", "fault", "stopped")) {
    throw "The platform node failed to start: $($runtime.state)"
}

[PSCustomObject]@{
    State = $runtime.state
    Connected = [bool]$runtime.connected
    ApiReady = [bool]$runtime.apiReady
    ApiLeaseSlots = $runtime.apiLeaseSlots
    HardwareReady = [bool]$runtime.hardwareReady
    LastErrorCode = $runtime.lastErrorCode
    Version = $runtime.version
}
