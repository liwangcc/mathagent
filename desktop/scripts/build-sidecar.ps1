param(
    [ValidateSet("x86_64-pc-windows-msvc", "x86_64-pc-windows-gnu")]
    [string]$TargetTriple = "x86_64-pc-windows-msvc"
)

$ErrorActionPreference = "Stop"
# Hermes and other launchers may inject their own Python environment into the
# parent process. A build venv must resolve only its pinned dependencies.
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue
$env:PYTHONNOUSERSITE = "1"
$DesktopRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $DesktopRoot
$DesktopPackage = Get-Content -LiteralPath (Join-Path $DesktopRoot "package.json") -Raw | ConvertFrom-Json
$ExpectedVersion = [string]$DesktopPackage.version
if ($ExpectedVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "The desktop package version is invalid."
}
$BuildEnvironment = Join-Path $DesktopRoot ".venv-build"
$PyInstallerConfig = Join-Path $DesktopRoot ".pyinstaller-cache"
$WorkerRoot = Join-Path $DesktopRoot "worker"
$GpuRuntime = Join-Path $WorkerRoot "mathagent_worker\native\windows_nvidia_gpu"
$BinaryDirectory = Join-Path $DesktopRoot "src-tauri\binaries"
$Icon = Join-Path $DesktopRoot "src-tauri\icons\icon.ico"
$OutputName = "mathagent-sidecar-$TargetTriple.exe"
$Python = Join-Path $RepositoryRoot "venv_local\Scripts\python.exe"
$BuildPython = Join-Path $BuildEnvironment "Scripts\python.exe"

function Assert-NativeSuccess {
    param([string]$Action)
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python 3.11 build interpreter not found at the repository venv path."
}
if (-not (Test-Path -LiteralPath $Icon)) {
    throw "Desktop icon not found at the expected path."
}
& (Join-Path $PSScriptRoot "build-vksift-resident.ps1")
Assert-NativeSuccess "Building the resident VulkanSift helper"
$PackagedGpuRuntimeFiles = @(
    "vksift_resident.exe",
    "vulkansift.dll",
    "vcruntime140.dll",
    "known_answer_1536x2048.pgm",
    "VulkanSift-LICENSE.txt",
    "volk-LICENSE.txt",
    "Microsoft-VC-Runtime-NOTICE.txt"
)
foreach ($RuntimeFile in $PackagedGpuRuntimeFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $GpuRuntime $RuntimeFile))) {
        throw "Windows NVIDIA runtime file is missing: $RuntimeFile"
    }
}

if (-not (Test-Path -LiteralPath $BuildPython)) {
    & $Python -m venv $BuildEnvironment
    Assert-NativeSuccess "Creating the sidecar build environment"
}

& $BuildPython -m pip install --disable-pip-version-check --quiet `
    --no-cache-dir `
    "pyinstaller==6.21.0"
Assert-NativeSuccess "Installing the sidecar build dependencies"
& $BuildPython -m pip install --disable-pip-version-check --quiet `
    --no-cache-dir `
    --no-build-isolation `
    --force-reinstall --no-deps $WorkerRoot
Assert-NativeSuccess "Installing the sidecar package"
& $BuildPython -m pip install --disable-pip-version-check --quiet `
    --no-cache-dir `
    "numpy==2.4.2" `
    "opencv-python-headless==4.13.0.92" `
    "Pillow==11.1.0" `
    "psutil==7.2.2"
Assert-NativeSuccess "Installing the sidecar runtime dependencies"
& $BuildPython -m pip check
Assert-NativeSuccess "Checking the sidecar runtime dependencies"

New-Item -ItemType Directory -Force -Path $BinaryDirectory | Out-Null
Push-Location $WorkerRoot
$PreviousPyInstallerConfig = $env:PYINSTALLER_CONFIG_DIR
try {
    $env:PYINSTALLER_CONFIG_DIR = $PyInstallerConfig
    & $BuildPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --add-data "$(Join-Path $GpuRuntime 'vksift_resident.exe');mathagent_worker/native/windows_nvidia_gpu" `
        --add-data "$(Join-Path $GpuRuntime 'vulkansift.dll');mathagent_worker/native/windows_nvidia_gpu" `
        --add-data "$(Join-Path $GpuRuntime 'vcruntime140.dll');mathagent_worker/native/windows_nvidia_gpu" `
        --add-data "$(Join-Path $GpuRuntime 'known_answer_1536x2048.pgm');mathagent_worker/native/windows_nvidia_gpu" `
        --add-data "$(Join-Path $GpuRuntime 'VulkanSift-LICENSE.txt');mathagent_worker/native/windows_nvidia_gpu" `
        --add-data "$(Join-Path $GpuRuntime 'volk-LICENSE.txt');mathagent_worker/native/windows_nvidia_gpu" `
        --add-data "$(Join-Path $GpuRuntime 'Microsoft-VC-Runtime-NOTICE.txt');mathagent_worker/native/windows_nvidia_gpu" `
        --paths $RepositoryRoot `
        --collect-submodules services `
        --hidden-import "PIL.ImageDraw" `
        --hidden-import "PIL.ImageFont" `
        --hidden-import "PIL.ImageOps" `
        --icon $Icon `
        --name "mathagent-sidecar-$TargetTriple" `
        --distpath $BinaryDirectory `
        --workpath (Join-Path $WorkerRoot "build") `
        --specpath (Join-Path $WorkerRoot "build") `
        (Join-Path $WorkerRoot "sidecar_entry.py")
    Assert-NativeSuccess "Packaging the sidecar"
}
finally {
    if ($null -eq $PreviousPyInstallerConfig) {
        Remove-Item Env:\PYINSTALLER_CONFIG_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:PYINSTALLER_CONFIG_DIR = $PreviousPyInstallerConfig
    }
    Pop-Location
}

$Binary = Join-Path $BinaryDirectory $OutputName
if (-not (Test-Path -LiteralPath $Binary)) {
    throw "Expected sidecar binary was not produced: $Binary"
}

$PreviousPath = $env:Path
try {
    $env:Path = "$env:SystemRoot\System32;$env:SystemRoot"
    $Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $HealthJson = & $Binary --health
    $Stopwatch.Stop()
    Assert-NativeSuccess "Running the packaged sidecar health check"
    $PackagedVersion = & $Binary --version
    Assert-NativeSuccess "Running the packaged sidecar version check"
    $CapacityJson = & $Binary --capacity
    Assert-NativeSuccess "Running the packaged sidecar capacity check"
}
finally {
    $env:Path = $PreviousPath
}

$Health = $HealthJson | ConvertFrom-Json
if (
    $Health.status -ne "ok" -or
    $Health.service -ne "mathagent-desktop-sidecar" -or
    $Health.version -ne $ExpectedVersion -or
    $Health.protocol_version -ne 5
) {
    throw "The packaged sidecar returned an invalid health payload."
}
if ($PackagedVersion.Trim() -ne $ExpectedVersion) {
    throw "The packaged sidecar returned an unexpected version."
}
$Capacity = $CapacityJson | ConvertFrom-Json
if (
    $Capacity.status -ne "ok" -or
    $Capacity.workerStatus.route -ne "server_required" -or
    $Capacity.workerStatus.workerCount -ne 0 -or
    $Capacity.workerStatus.minimumWorkerCount -ne 2 -or
    $Capacity.workerStatus.handoffState -ne "not_integrated"
) {
    throw "The packaged sidecar returned an unsafe initial capacity decision."
}

$StartInfo = New-Object System.Diagnostics.ProcessStartInfo
$StartInfo.FileName = $Binary
$StartInfo.Arguments = "--serve"
$StartInfo.UseShellExecute = $false
$StartInfo.RedirectStandardInput = $true
$StartInfo.RedirectStandardOutput = $true
$StartInfo.RedirectStandardError = $true
$Process = New-Object System.Diagnostics.Process
$Process.StartInfo = $StartInfo
$ProcessStarted = $false
try {
    [void]$Process.Start()
    $ProcessStarted = $true
    $HealthLine = $Process.StandardOutput.ReadLineAsync()
    if (-not $HealthLine.Wait(5000)) {
        throw "The packaged sidecar did not report serve health within five seconds."
    }
    $ServeHealth = $HealthLine.Result | ConvertFrom-Json
    $Process.StandardInput.WriteLine('{"command":"get_worker_status","requestId":"build-check-1"}')
    $WorkerStatusLine = $Process.StandardOutput.ReadLineAsync()
    if (-not $WorkerStatusLine.Wait(15000)) {
        throw "The packaged sidecar did not report worker status within fifteen seconds."
    }
    $WorkerResponse = $WorkerStatusLine.Result | ConvertFrom-Json
    $Process.StandardInput.WriteLine('{"command":"shutdown"}')
    $StoppingLine = $Process.StandardOutput.ReadLineAsync()
    if (-not $StoppingLine.Wait(5000)) {
        throw "The packaged sidecar did not acknowledge shutdown within five seconds."
    }
    $Stopping = $StoppingLine.Result | ConvertFrom-Json
    if (-not $Process.WaitForExit(5000)) {
        throw "The packaged sidecar did not exit within five seconds."
    }

    if (
        $Process.ExitCode -ne 0 -or
        $ServeHealth.status -ne "ok" -or
        $WorkerResponse.status -ne "ok" -or
        $WorkerResponse.requestId -ne "build-check-1" -or
        $WorkerResponse.workerStatus.workerCount -ne 0 -or
        $Stopping.status -ne "stopping"
    ) {
        throw "The packaged sidecar did not complete its serve/shutdown lifecycle."
    }
    $LifecycleExitCode = $Process.ExitCode
}
finally {
    if ($ProcessStarted -and -not $Process.HasExited) {
        $Process.Kill()
        $Process.WaitForExit()
    }
}

$BinaryInfo = Get-Item -LiteralPath $Binary
[PSCustomObject]@{
    FullName = $BinaryInfo.FullName
    Length = $BinaryInfo.Length
    ColdHealthStartMs = [Math]::Round($Stopwatch.Elapsed.TotalMilliseconds, 2)
    ProtocolVersion = $Health.protocol_version
    LifecycleExitCode = $LifecycleExitCode
}
