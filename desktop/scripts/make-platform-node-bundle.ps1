param(
    [string]$SidecarPath,
    [string]$OutputPath,
    [string]$ApiOrigin,
    [string]$NodeId,
    [ValidateSet("quiet", "balanced", "fast")]
    [string]$ResourceMode = "fast",
    [string]$BootstrapPath,
    [switch]$IncludeNodeToken,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$desktopRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $desktopRoot
$package = Get-Content -LiteralPath (Join-Path $desktopRoot "package.json") -Raw | ConvertFrom-Json
$version = [string]$package.version
if (-not $SidecarPath) {
    $SidecarPath = Join-Path $desktopRoot "src-tauri\binaries\mathagent-sidecar-x86_64-pc-windows-msvc.exe"
}
$SidecarPath = (Resolve-Path -LiteralPath $SidecarPath -ErrorAction Stop).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path $repoRoot ("releases\MathAgent-PlatformNode-$version.zip")
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
if ((Test-Path -LiteralPath $OutputPath) -and -not $Force) {
    throw "Output already exists. Use -Force to replace this exact archive."
}
$hasApiOrigin = -not [string]::IsNullOrWhiteSpace($ApiOrigin)
$hasNodeId = -not [string]::IsNullOrWhiteSpace($NodeId)
if ($hasApiOrigin -xor $hasNodeId) {
    throw "ApiOrigin and NodeId must be provided together for a personalized package."
}
if ($hasApiOrigin) {
    $parsedOrigin = $null
    if (
        -not [Uri]::TryCreate($ApiOrigin, [UriKind]::Absolute, [ref]$parsedOrigin) -or
        $parsedOrigin.Scheme -ne "https" -or
        [string]::IsNullOrWhiteSpace($parsedOrigin.Host) -or
        $parsedOrigin.UserInfo -or
        $parsedOrigin.AbsolutePath -ne "/" -or
        $parsedOrigin.Query -or
        $parsedOrigin.Fragment
    ) {
        throw "ApiOrigin must be a bare HTTPS origin without credentials, path, query, or fragment."
    }
    if ($NodeId -notmatch '^[A-Za-z0-9._:-]{1,64}$') {
        throw "NodeId is invalid."
    }
} elseif ($IncludeNodeToken) {
    throw "Credential embedding requires a personalized package with ApiOrigin and NodeId."
}
$includeCredentials = [bool]$IncludeNodeToken
$prebuiltBootstrap = $null
if (-not [string]::IsNullOrWhiteSpace($BootstrapPath)) {
    if (-not $hasApiOrigin) {
        throw "BootstrapPath requires a personalized package with ApiOrigin and NodeId."
    }
    try {
        $prebuiltBootstrap = Get-Content -LiteralPath $BootstrapPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "BootstrapPath is not valid JSON."
    }
    if (
        $null -eq $prebuiltBootstrap -or
        [string]$prebuiltBootstrap.apiOrigin -ne $parsedOrigin.AbsoluteUri.TrimEnd('/') -or
        [string]$prebuiltBootstrap.nodeId -ne $NodeId
    ) {
        throw "BootstrapPath does not match ApiOrigin and NodeId."
    }
    $allowedBootstrapFields = @("apiOrigin", "nodeId", "nodeToken", "resourceMode")
    $unknownBootstrapFields = @(
        $prebuiltBootstrap.PSObject.Properties.Name |
            Where-Object { $_ -notin $allowedBootstrapFields }
    )
    if (
        $unknownBootstrapFields.Count -gt 0 -or
        [string]::IsNullOrWhiteSpace([string]$prebuiltBootstrap.nodeToken)
    ) {
        throw "BootstrapPath must contain only one platform node credential."
    }
    $includeCredentials = $true
}

$plainNodeToken = $null
$secureNodeToken = $null
if ($IncludeNodeToken -and $null -eq $prebuiltBootstrap) {
    $secureNodeToken = Read-Host "为这个专属包输入 Node Token（不会显示）" -AsSecureString
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureNodeToken)
        $plainNodeToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        $secureNodeToken.Dispose()
    }
    if ([string]::IsNullOrWhiteSpace($plainNodeToken) -or $plainNodeToken.Length -lt 20 -or $plainNodeToken.Length -gt 256 -or $plainNodeToken -match '\s') {
        throw "NodeToken is invalid."
    }
}

$reportedVersion = (& $SidecarPath --version 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $reportedVersion -ne $version) {
    throw "Sidecar version $reportedVersion does not match package version $version."
}
$health = (& $SidecarPath --health 2>$null | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $health.status -ne "ok" -or $health.protocol_version -ne 5) {
    throw "Sidecar health contract failed."
}

$stage = Join-Path ([IO.Path]::GetTempPath()) ("mathagent-platform-node-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$files = @(
    "install-platform-node.ps1",
    "setup-platform-node.ps1",
    "start-platform-node.ps1",
    "stop-platform-node.ps1",
    "platform-node-status.ps1",
    "uninstall-platform-node.ps1",
    "control-platform-node.ps1",
    "control-platform-node.cmd",
    "easy-setup.cmd",
    "PLATFORM_NODE_README.txt"
)
try {
    Copy-Item -LiteralPath $SidecarPath -Destination (Join-Path $stage "mathagent-sidecar.exe")
    foreach ($file in $files) {
        $source = Join-Path $PSScriptRoot $file
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Bundle file is missing: $file"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $stage $file)
    }

    $bootstrapFiles = @()
    if ($prebuiltBootstrap) {
        Copy-Item -LiteralPath $BootstrapPath -Destination (Join-Path $stage "node-bootstrap.json")
        $bootstrapFiles = @("node-bootstrap.json")
    } elseif ($hasApiOrigin) {
        $bootstrapPayload = [ordered]@{
            apiOrigin = $parsedOrigin.AbsoluteUri.TrimEnd('/')
            nodeId = $NodeId
            resourceMode = $ResourceMode
        }
        if ($IncludeNodeToken) {
            $bootstrapPayload.nodeToken = $plainNodeToken
        }
        $bootstrapPayload | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $stage "node-bootstrap.json") -Encoding UTF8
        $bootstrapFiles = @("node-bootstrap.json")
    }

    $hash = (Get-FileHash -LiteralPath (Join-Path $stage "mathagent-sidecar.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
    [ordered]@{
        product = "MathAgent platform node"
        version = $version
        protocolVersion = 5
        sidecarSha256 = $hash
        containsCredentials = $includeCredentials
        personalized = $hasApiOrigin
        files = @("mathagent-sidecar.exe") + $files + $bootstrapFiles + @("manifest.json", "SHA256SUMS.txt")
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stage "manifest.json") -Encoding UTF8
    $sidecarHashLine = "$hash  mathagent-sidecar.exe"
    Set-Content -LiteralPath (Join-Path $stage "SHA256SUMS.txt") -Value $sidecarHashLine -Encoding ASCII

    $outputDirectory = Split-Path -Parent $OutputPath
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $OutputPath -CompressionLevel Optimal

    $verify = Join-Path ([IO.Path]::GetTempPath()) ("mathagent-platform-node-verify-" + [Guid]::NewGuid().ToString("N"))
    try {
        Expand-Archive -LiteralPath $OutputPath -DestinationPath $verify -Force
        foreach ($required in @("mathagent-sidecar.exe", "manifest.json", "SHA256SUMS.txt", "setup-platform-node.ps1", "easy-setup.cmd", "control-platform-node.cmd")) {
            if (-not (Test-Path -LiteralPath (Join-Path $verify $required) -PathType Leaf)) {
                throw "Archive verification failed: $required is missing."
            }
        }
        $protected = Get-ChildItem -LiteralPath $verify -Recurse -File | Where-Object {
            $_.Name -match '^\.env(\.local)?$|\.db$|\.sqlite3?$|\.pem$|\.key$'
        }
        if ($protected) {
            throw "Archive contains a protected credential or database file."
        }
    }
    finally {
        Remove-Item -LiteralPath $verify -Recurse -Force -ErrorAction SilentlyContinue
    }

    $archiveHash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $archiveHashPath = "$OutputPath.sha256"
    if ((Test-Path -LiteralPath $archiveHashPath) -and -not $Force) {
        throw "Archive checksum already exists. Use -Force to replace this exact file."
    }
    Set-Content -LiteralPath $archiveHashPath -Value "$archiveHash  $([IO.Path]::GetFileName($OutputPath))" -Encoding ASCII
    [PSCustomObject]@{
        Archive = $OutputPath
        Version = $version
        SidecarSha256 = $hash
        ArchiveSha256 = $archiveHash
        ArchiveSha256File = $archiveHashPath
        SizeBytes = (Get-Item -LiteralPath $OutputPath).Length
        CredentialFilesIncluded = $false
        CredentialsEmbedded = $includeCredentials
        Personalized = $hasApiOrigin
    }
}
finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
