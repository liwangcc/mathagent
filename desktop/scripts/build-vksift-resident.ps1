param(
    [string]$VcVars64 = $env:MATHAGENT_VCVARS64
)

$ErrorActionPreference = "Stop"
$DesktopRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $DesktopRoot
$Source = Join-Path $DesktopRoot "worker\native\vksift_resident.cpp"
$RuntimeRoot = Join-Path $DesktopRoot "worker\mathagent_worker\native\windows_nvidia_gpu"
$Output = Join-Path $RuntimeRoot "vksift_resident.exe"
$BuildRoot = Join-Path $DesktopRoot "worker\build\native"
$Object = Join-Path $BuildRoot "vksift_resident.obj"
$ExpectedSha256 = "1a1e83c9bb8650a6fb782468fc9fcaee9c0d595b60e42aeee29be86e89523fdc"

if (-not (Test-Path -LiteralPath $Source)) {
    throw "Resident VulkanSift source is missing: $Source"
}
if (-not $VcVars64) {
    $VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $VsWhere) {
        $VcVars64 = & $VsWhere `
            -latest `
            -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -find "VC\Auxiliary\Build\vcvars64.bat" |
            Select-Object -First 1
    }
}
if (-not $VcVars64) {
    $LocalBuildTools = "D:\DevTools\VSBuildTools\VC\Auxiliary\Build\vcvars64.bat"
    if (Test-Path -LiteralPath $LocalBuildTools) {
        $VcVars64 = $LocalBuildTools
    }
}
if (-not $VcVars64 -or -not (Test-Path -LiteralPath $VcVars64)) {
    throw "Visual C++ x64 build environment was not found. Set MATHAGENT_VCVARS64."
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot, $BuildRoot | Out-Null
$Compile = @(
    "call `"$VcVars64`" >nul",
    "cl /nologo /std:c++17 /EHsc /O2 /Brepro /Fo:`"$Object`" /Fe:`"$Output`" `"$Source`""
) -join " && "
& cmd.exe /d /c $Compile
if ($LASTEXITCODE -ne 0) {
    throw "Building the resident VulkanSift helper failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $Output)) {
    throw "Resident VulkanSift helper was not produced: $Output"
}
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Output).Hash.ToLowerInvariant()
if ($Hash -ne $ExpectedSha256) {
    throw "Resident VulkanSift helper hash mismatch: $Hash"
}

[PSCustomObject]@{
    FullName = (Get-Item -LiteralPath $Output).FullName
    Length = (Get-Item -LiteralPath $Output).Length
    Sha256 = $Hash
    VulkanSiftCommit = "04245a5e194c9c1aa2ac07c2d257eedad3c8699d"
}
