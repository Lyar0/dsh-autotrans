#Requires -Version 5.1
<#
    get-runtime.ps1 -- fetch the prebuilt self-contained runtime for AutoTrans
    from a GitHub Release and drop it where the plugin auto-detects it.

    The plugin (lib/index.js, config.python = "auto") looks for the runtime at
    <plugin>/runtime/autotrans.exe. By default this script extracts into the
    `runtime/` folder next to this file (i.e. <plugin>/runtime/ when run from an
    installed plugin, or <repo>/runtime/ in a source checkout), so after running
    it once no config edit is needed.

    Asset published by scripts/build-runtime.ps1: autotrans-win-x64.zip

    Usage:
      powershell -ExecutionPolicy Bypass -File scripts/get-runtime.ps1
      powershell -ExecutionPolicy Bypass -File scripts/get-runtime.ps1 -Owner Lyar0 -Repo dsh-autotrans -Tag latest
      powershell -ExecutionPolicy Bypass -File scripts/get-runtime.ps1 -OutDir D:/runtimes/autotrans
#>
param(
    [string]$Owner = "Lyar0",
    [string]$Repo = "dsh-autotrans",
    [string]$Tag = "latest",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$Asset = "autotrans-win-x64.zip"

# Default output: <repo>/runtime (this script lives in <repo>/scripts)
if (-not $OutDir) {
    $OutDir = Join-Path (Split-Path -Parent $PSScriptRoot) "runtime"
}

if ($Tag -eq "latest") {
    $Url = "https://github.com/$Owner/$Repo/releases/latest/download/$Asset"
} else {
    $Url = "https://github.com/$Owner/$Repo/releases/download/$Tag/$Asset"
}

Write-Host "==> Downloading $Url"
$tmpZip = Join-Path $env:TEMP $Asset
Invoke-WebRequest -Uri $Url -OutFile $tmpZip

Write-Host "==> Extracting to $OutDir"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Expand-Archive -Path $tmpZip -DestinationPath $OutDir -Force
Remove-Item -Force $tmpZip

$exe = Join-Path $OutDir "autotrans.exe"
if (-not (Test-Path $exe)) { throw "Expected $exe not found after extraction" }

Write-Host ""
Write-Host "==> Runtime ready: $exe"
Write-Host "Try it:  $exe --help"
Write-Host ""
Write-Host "The plugin auto-detects this runtime (config.python = 'auto'). If you"
Write-Host "extracted elsewhere, set the profile's tool-autotrans config.python to"
Write-Host "the path of this autotrans.exe."
