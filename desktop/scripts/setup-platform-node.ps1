param(
    [string]$ApiOrigin,
    [string]$NodeId,
    [ValidateSet("quiet", "balanced", "fast")]
    [string]$ResourceMode,
    [string]$TaskName = "MathAgent Platform Accelerator",
    [Security.SecureString]$NodeToken
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($null -ne $NodeToken) {
        throw "Run setup-platform-node.ps1 from an elevated PowerShell window when NodeToken is supplied explicitly."
    }

    $elevatedArguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        ('"{0}"' -f $PSCommandPath.Replace('"', '\"'))
    )
    foreach ($parameter in @(
        @{ Name = "ApiOrigin"; Value = $ApiOrigin },
        @{ Name = "NodeId"; Value = $NodeId },
        @{ Name = "ResourceMode"; Value = $ResourceMode },
        @{ Name = "TaskName"; Value = $TaskName }
    )) {
        if (-not [string]::IsNullOrWhiteSpace([string]$parameter.Value)) {
            $elevatedArguments += $parameter.Name
            $elevatedArguments += ('"{0}"' -f ([string]$parameter.Value).Replace('"', '\"'))
        }
    }
    Write-Host "Requesting administrator permission to install the startup task..."
    $elevated = Start-Process `
        -FilePath "powershell.exe" `
        -Verb RunAs `
        -WorkingDirectory $PSScriptRoot `
        -ArgumentList $elevatedArguments `
        -Wait `
        -PassThru
    exit $elevated.ExitCode
}

function Get-NodeBootstrap {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $payload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        throw "node-bootstrap.json is invalid. Please request a new package from the administrator."
    }
    if ($null -eq $payload) {
        throw "node-bootstrap.json is invalid. Please request a new package from the administrator."
    }
    $allowedFields = @("apiOrigin", "nodeId", "nodeToken", "resourceMode")
    $unknownFields = @($payload.PSObject.Properties.Name | Where-Object { $_ -notin $allowedFields })
    if ($unknownFields.Count -gt 0) {
        throw "node-bootstrap.json contains unsupported fields. Please request a new package from the administrator."
    }
    if (
        [string]::IsNullOrWhiteSpace([string]$payload.apiOrigin) -or
        [string]::IsNullOrWhiteSpace([string]$payload.nodeId) -or
        [string]::IsNullOrWhiteSpace([string]$payload.resourceMode)
    ) {
        throw "node-bootstrap.json is incomplete. Please request a new package from the administrator."
    }
    if ([string]$payload.resourceMode -notin @("quiet", "balanced", "fast")) {
        throw "node-bootstrap.json contains an unsupported resource mode."
    }
    if ($null -ne $payload.nodeToken -and -not [string]::IsNullOrWhiteSpace([string]$payload.nodeToken)) {
        $token = [string]$payload.nodeToken
        if ($token.Length -lt 20 -or $token.Length -gt 256 -or $token -match '\s') {
            throw "node-bootstrap.json contains an invalid Node Token."
        }
    }
    return $payload
}

$bootstrapPath = Join-Path $PSScriptRoot "node-bootstrap.json"
$bootstrap = Get-NodeBootstrap $bootstrapPath
if ([string]::IsNullOrWhiteSpace($ApiOrigin) -and $bootstrap) {
    $ApiOrigin = [string]$bootstrap.apiOrigin
}
if ([string]::IsNullOrWhiteSpace($NodeId) -and $bootstrap) {
    $NodeId = [string]$bootstrap.nodeId
}
if ([string]::IsNullOrWhiteSpace($ResourceMode) -and $bootstrap) {
    $ResourceMode = [string]$bootstrap.resourceMode
}
if ([string]::IsNullOrWhiteSpace($ApiOrigin)) {
    $ApiOrigin = Read-Host "管理员提供的 HTTPS 服务地址"
}
if ([string]::IsNullOrWhiteSpace($NodeId)) {
    $NodeId = Read-Host "管理员提供的 Node ID"
}
if ([string]::IsNullOrWhiteSpace($ResourceMode)) {
    $ResourceMode = "fast"
}
if ($null -eq $NodeToken -and $bootstrap -and -not [string]::IsNullOrWhiteSpace([string]$bootstrap.nodeToken)) {
    $NodeToken = ConvertTo-SecureString ([string]$bootstrap.nodeToken) -AsPlainText -Force
}
$bootstrapContainsCredentials = [bool](
    $bootstrap -and
    -not [string]::IsNullOrWhiteSpace([string]$bootstrap.nodeToken)
)

$SidecarPath = Join-Path $PSScriptRoot "mathagent-sidecar.exe"
if (-not (Test-Path -LiteralPath $SidecarPath -PathType Leaf)) {
    throw "mathagent-sidecar.exe is missing from this package."
}

try {
    & (Join-Path $PSScriptRoot "install-platform-node.ps1") `
        -ApiOrigin $ApiOrigin `
        -NodeId $NodeId `
        -TaskName $TaskName `
        -ResourceMode $ResourceMode `
        -NodeToken $NodeToken `
        -NoStart `
        -SidecarPath $SidecarPath
    if ($LASTEXITCODE -ne 0) {
        throw "platform node installation failed"
    }

    & (Join-Path $PSScriptRoot "start-platform-node.ps1") `
        -TaskName $TaskName
    if ($bootstrapContainsCredentials) {
        Remove-Item -LiteralPath $bootstrapPath -Force
    }
    Write-Host ""
    Write-Host "安装完成。此节点已启动并开始等待任务。" -ForegroundColor Green
    Write-Host "以后请双击 control-platform-node.cmd 开启、关闭或查看节点状态。"
}
catch {
    & (Join-Path $PSScriptRoot "stop-platform-node.ps1") -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    throw
}
