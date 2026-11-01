param(
    [string]$SidecarPath,
    [string]$TaskName = "MathAgent Platform Accelerator"
)

$ErrorActionPreference = "Stop"
$DesktopRoot = Split-Path -Parent $PSScriptRoot
if (-not $SidecarPath) {
    $SidecarPath = Join-Path $env:LOCALAPPDATA "MathAgent\PlatformNode\mathagent-sidecar.exe"
}
$SidecarPath = [IO.Path]::GetFullPath($SidecarPath)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$runtime = $null
if (Test-Path -LiteralPath $SidecarPath -PathType Leaf) {
    try {
        $runtime = & $SidecarPath --platform-node-status 2>$null | ConvertFrom-Json
    }
    catch {
        $runtime = $null
    }
}
[PSCustomObject]@{
    TaskInstalled = $null -ne $task
    TaskState = if ($task) { [string]$task.State } else { "NotInstalled" }
    TaskEnabled = [bool]($task -and $task.State -ne "Disabled")
    TaskRunning = [bool]($task -and $task.State -eq "Running")
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    LastRunTime = if ($taskInfo) { $taskInfo.LastRunTime } else { $null }
    NextRunTime = if ($taskInfo) { $taskInfo.NextRunTime } else { $null }
    Runtime = if ($runtime) { $runtime.platformNode } else { $null }
}
