param(
    [Parameter(Mandatory = $true)]
    [string]$ApiOrigin,
    [Parameter(Mandatory = $true)]
    [string]$NodeId,
    [Security.SecureString]$NodeToken,
    [string]$TaskName = "MathAgent Platform Accelerator",
    [ValidateSet("quiet", "balanced", "fast")]
    [string]$ResourceMode = "fast",
    [string]$SidecarPath,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$DesktopRoot = Split-Path -Parent $PSScriptRoot
if (-not $SidecarPath) {
    $SidecarPath = Join-Path $DesktopRoot "src-tauri\binaries\mathagent-sidecar-x86_64-pc-windows-msvc.exe"
}
$SourceSidecar = (Resolve-Path -LiteralPath $SidecarPath -ErrorAction Stop).Path
if ([IO.Path]::GetExtension($SourceSidecar) -ne ".exe") {
    throw "SidecarPath must resolve to an executable."
}
$parsed = $null
if (-not [Uri]::TryCreate($ApiOrigin, [UriKind]::Absolute, [ref]$parsed)) {
    throw "ApiOrigin must be an absolute HTTPS origin."
}
if (
    $parsed.Scheme -ne "https" -or
    [string]::IsNullOrWhiteSpace($parsed.Host) -or
    $parsed.UserInfo -or
    $parsed.AbsolutePath -ne "/" -or
    $parsed.Query -or
    $parsed.Fragment
) {
    throw "ApiOrigin must be a bare HTTPS origin without credentials, path, query, or fragment."
}
if ($NodeId -notmatch '^[A-Za-z0-9._:-]{1,64}$') {
    throw "NodeId is invalid."
}
if ($null -eq $NodeToken) {
    $NodeToken = Read-Host "One-time platform node token" -AsSecureString
}

$health = & $SourceSidecar --health | ConvertFrom-Json
if (
    $LASTEXITCODE -ne 0 -or
    $health.status -ne "ok" -or
    $health.service -ne "mathagent-desktop-sidecar" -or
    $health.protocol_version -ne 5
) {
    throw "The source sidecar failed its health contract."
}
$InstallRoot = Join-Path $env:LOCALAPPDATA "MathAgent\PlatformNode"
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$InstalledSidecar = Join-Path $InstallRoot "mathagent-sidecar.exe"
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
& (Join-Path $PSScriptRoot "stop-platform-node.ps1") -SidecarPath $InstalledSidecar -TaskName $TaskName | Out-Null
$StagedSidecar = Join-Path $InstallRoot "mathagent-sidecar.new.exe"
Copy-Item -LiteralPath $SourceSidecar -Destination $StagedSidecar -Force
Move-Item -LiteralPath $StagedSidecar -Destination $InstalledSidecar -Force
$SidecarPath = $InstalledSidecar

$tokenPointer = [IntPtr]::Zero
$plainToken = $null
$credential = $null
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $SidecarPath
$startInfo.Arguments = "--configure-platform-node"
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
$processStarted = $false
try {
    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($NodeToken)
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if (
        [string]::IsNullOrWhiteSpace($plainToken) -or
        $plainToken.Length -lt 20 -or
        $plainToken.Length -gt 256 -or
        $plainToken -match '\s'
    ) {
        throw "NodeToken is invalid."
    }
    $credential = @{
        server = $parsed.AbsoluteUri.TrimEnd('/')
        nodeId = $NodeId
        token = $plainToken
        accessScope = "platform"
    } | ConvertTo-Json -Compress
    [void]$process.Start()
    $processStarted = $true
    $process.StandardInput.Write($credential)
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(10000)) {
        $process.Kill()
        throw "The sidecar credential operation timed out."
    }
    $responseText = $process.StandardOutput.ReadToEnd()
    if ($process.ExitCode -ne 0) {
        throw "The sidecar rejected the protected platform credential."
    }
    $response = $responseText | ConvertFrom-Json
    if (
        $response.status -ne "ok" -or
        $response.configured -ne $true -or
        $response.accessScope -ne "platform"
    ) {
        throw "The sidecar returned an invalid credential response."
    }
}
finally {
    $credential = $null
    $plainToken = $null
    if ($tokenPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
    $NodeToken.Dispose()
    if ($processStarted -and -not $process.HasExited) {
        $process.Kill()
        $process.WaitForExit()
    }
    $process.Dispose()
}

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction `
    -Execute $SidecarPath `
    -Argument "--platform-node --resource-mode $ResourceMode" `
    -WorkingDirectory (Split-Path -Parent $SidecarPath)
$triggers = @(
    # Start as soon as Windows starts when the interactive user token is
    # available, and keep the logon trigger as a recovery path.
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn -User $currentUser
)
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval ([TimeSpan]::FromMinutes(1))
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $triggers `
    -Principal $principal `
    -Settings $settings `
    -Description "MathAgent outbound-only platform image acceleration node"
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
if ($NoStart) {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    [PSCustomObject]@{
        status = "ok"
        taskName = $TaskName
        taskEnabled = $false
        started = $false
    }
}
else {
    & (Join-Path $PSScriptRoot "start-platform-node.ps1") -SidecarPath $SidecarPath -TaskName $TaskName
}
