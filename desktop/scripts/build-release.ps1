param(
    [Parameter(Mandatory = $true)]
    [string]$ApiOrigin,
    [Parameter(Mandatory = $true)]
    [string]$WebOrigin
)

$ErrorActionPreference = "Stop"
$DesktopRoot = Split-Path -Parent $PSScriptRoot

function Find-Cargo {
    $command = Get-Command cargo.exe -ErrorAction SilentlyContinue
    $candidates = @()
    if ($command) { $candidates += $command.Source }
    $candidates += (Join-Path $HOME ".cargo\bin\cargo.exe")
    $toolchains = Get-ChildItem `
        "D:\DevTools\Rust\rustup\toolchains\*-x86_64-pc-windows-msvc\bin\cargo.exe" `
        -ErrorAction SilentlyContinue
    $candidates += $toolchains.FullName
    $compatible = @()
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $versionText = & $candidate --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $versionText -match '^cargo\s+(\d+\.\d+\.\d+)') {
                $version = [Version]$Matches[1]
                $rustc = Join-Path (Split-Path -Parent $candidate) "rustc.exe"
                if ($version -ge [Version]'1.85.0' -and (Test-Path -LiteralPath $rustc -PathType Leaf)) {
                    $compatible += [PSCustomObject]@{
                        Cargo = $candidate
                        Rustc = $rustc
                        Version = $version
                    }
                }
            }
        }
    }
    $selected = $compatible | Sort-Object Version -Descending | Select-Object -First 1
    if ($selected) { return $selected }
    throw "A complete Rust MSVC toolchain with Cargo 1.85.0 or newer and rustc.exe was not found."
}

function Assert-HttpsOrigin {
    param([string]$Name, [string]$Value)
    $parsed = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed)) {
        throw "$Name must be an absolute HTTPS origin."
    }
    if (
        $parsed.Scheme -ne "https" -or
        [string]::IsNullOrWhiteSpace($parsed.Host) -or
        $parsed.UserInfo -or
        $parsed.AbsolutePath -ne "/" -or
        $parsed.Query -or
        $parsed.Fragment
    ) {
        throw "$Name must be a bare HTTPS origin without credentials, path, query, or fragment."
    }
    return $parsed.AbsoluteUri.TrimEnd('/')
}

$ValidatedApiOrigin = Assert-HttpsOrigin "ApiOrigin" $ApiOrigin
$ValidatedWebOrigin = Assert-HttpsOrigin "WebOrigin" $WebOrigin
$RustToolchain = Find-Cargo
$PreviousApiOrigin = $env:MATHAGENT_API_ORIGIN
$PreviousViteApiOrigin = $env:VITE_API_ORIGIN
$PreviousWebOrigin = $env:MATHAGENT_WEB_FALLBACK_URL
$PreviousPath = $env:Path
$PreviousCargo = $env:CARGO
$PreviousRustc = $env:RUSTC
try {
    $env:MATHAGENT_API_ORIGIN = $ValidatedApiOrigin
    $env:VITE_API_ORIGIN = $ValidatedApiOrigin
    $env:MATHAGENT_WEB_FALLBACK_URL = $ValidatedWebOrigin
    $env:CARGO = $RustToolchain.Cargo
    $env:RUSTC = $RustToolchain.Rustc
    $env:Path = "$(Split-Path -Parent $RustToolchain.Cargo);$env:Path"
    Push-Location $DesktopRoot
    try {
        & npm run build:prepare
        if ($LASTEXITCODE -ne 0) { throw "Preparing the desktop release failed." }
        & npx tauri build
        if ($LASTEXITCODE -ne 0) { throw "Building the desktop release failed." }
    }
    finally { Pop-Location }
}
finally {
    $env:Path = $PreviousPath
    if ($null -eq $PreviousCargo) { Remove-Item Env:\CARGO -ErrorAction SilentlyContinue }
    else { $env:CARGO = $PreviousCargo }
    if ($null -eq $PreviousRustc) { Remove-Item Env:\RUSTC -ErrorAction SilentlyContinue }
    else { $env:RUSTC = $PreviousRustc }
    if ($null -eq $PreviousApiOrigin) { Remove-Item Env:\MATHAGENT_API_ORIGIN -ErrorAction SilentlyContinue }
    else { $env:MATHAGENT_API_ORIGIN = $PreviousApiOrigin }
    if ($null -eq $PreviousViteApiOrigin) { Remove-Item Env:\VITE_API_ORIGIN -ErrorAction SilentlyContinue }
    else { $env:VITE_API_ORIGIN = $PreviousViteApiOrigin }
    if ($null -eq $PreviousWebOrigin) { Remove-Item Env:\MATHAGENT_WEB_FALLBACK_URL -ErrorAction SilentlyContinue }
    else { $env:MATHAGENT_WEB_FALLBACK_URL = $PreviousWebOrigin }
}