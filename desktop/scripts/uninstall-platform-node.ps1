param(
    [string]$SidecarPath,
    [string]$TaskName = "MathAgent Platform Accelerator",
    [switch]$KeepCredential
)

$ErrorActionPreference = "Stop"
if (-not $SidecarPath) {
    $SidecarPath = Join-Path $env:LOCALAPPDATA "MathAgent\PlatformNode\mathagent-sidecar.exe"
}
$SidecarPath = [IO.Path]::GetFullPath($SidecarPath)
$InstallRoot = Join-Path $env:LOCALAPPDATA "MathAgent\PlatformNode"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
& (Join-Path $PSScriptRoot "stop-platform-node.ps1") -SidecarPath $SidecarPath -TaskName $TaskName | Out-Null
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$commandSidecar = $null
if (Test-Path -LiteralPath $SidecarPath -PathType Leaf) {
    $commandSidecar = $SidecarPath
}
else {
    $packageSidecar = Join-Path $PSScriptRoot "mathagent-sidecar.exe"
    if (Test-Path -LiteralPath $packageSidecar -PathType Leaf) {
        $commandSidecar = (Resolve-Path -LiteralPath $packageSidecar).Path
    }
}
if (-not $KeepCredential) {
    if (-not $commandSidecar) {
        throw "The sidecar executable is missing; cannot clear protected credentials."
    }
    foreach ($profile in @("primary", "failover", "accelerator", "retry")) {
        $profileResponse = & $commandSidecar --clear-provider-profile $profile | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or $profileResponse.status -ne "ok") {
            throw "The protected $profile provider profile could not be removed."
        }
    }
    $response = & $commandSidecar --clear-platform-node | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $response.status -ne "ok") {
        throw "The protected platform credential could not be removed."
    }
}

if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
    Get-ChildItem -LiteralPath $InstallRoot -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @("mathagent-sidecar.exe", "mathagent-sidecar.new.exe", "status.json") } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}
[PSCustomObject]@{
    TaskInstalled = $false
    CredentialRetained = [bool]$KeepCredential
    ProviderCredentialsRetained = [bool]$KeepCredential
    InstallationRemoved = -not (Test-Path -LiteralPath (Join-Path $InstallRoot "mathagent-sidecar.exe") -PathType Leaf)
}
