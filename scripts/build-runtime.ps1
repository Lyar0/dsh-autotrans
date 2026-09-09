#Requires -Version 5.1
<#
    build-runtime.ps1 -- package the AutoTrans Python core into a self-contained
    Windows x64 runtime (a "runtime that wraps the python core").

    Outputs:
      dist/autotrans/             onedir runtime folder (autotrans.exe + bundled
                                  python + deps + glossaries; runnable with no
                                  system Python installed)
      dist/autotrans-win-x64.zip  zipped runtime (upload to a GitHub Release)

    Prereq: a Python (3.10+) that already has  pymupdf, ebooklib, pyinstaller.
    Point -Python at it (e.g. a conda env); defaults to `python`.

    Usage:
      powershell -ExecutionPolicy Bypass -File scripts/build-runtime.ps1
      powershell -ExecutionPolicy Bypass -File scripts/build-runtime.ps1 -Python D:/Projects/CondaEnvs/booktrans/python.exe
#>
param(
    [string]$Python = "python",
    [string]$Name = "autotrans"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot        # repo root
$pythonDir = Join-Path $root "python"
$dist = Join-Path $root "dist"
$bundle = Join-Path $dist $Name
$zipOut = Join-Path $dist "$Name-win-x64.zip"

Write-Host "==> Python: $Python"
Write-Host "==> Root:   $root"

function Test-Mod([string]$mod) {
    & $Python -c "import $mod" 2>$null
    return $LASTEXITCODE -eq 0
}
foreach ($m in @("PyInstaller", "pymupdf", "ebooklib")) {
    if (-not (Test-Mod $m)) { throw "Missing build dependency $m - install into $Python first: pip install $m" }
}

if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }

Write-Host "==> Running PyInstaller (onedir)..."
$dataSpec = (Join-Path $root "python\glossaries") + ";glossaries"
$args = @(
    "--noconfirm", "--clean", "--onedir", "--name", $Name,
    "--paths", (Join-Path $root "python"),
    "--add-data", $dataSpec,
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build"),
    (Join-Path $pythonDir "autotrans.py")
)
& $Python -m PyInstaller @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "==> Zipping $bundle -> $zipOut"
if (Test-Path $zipOut) { Remove-Item -Force $zipOut }
Compress-Archive -Path (Join-Path $bundle "*") -DestinationPath $zipOut -Force

Write-Host ""
Write-Host "==> Done."
Write-Host "    onedir : $bundle\autotrans.exe"
$sizeMb = [math]::Round((Get-Item $zipOut).Length / 1MB, 1)
Write-Host "    zip    : $zipOut  ($sizeMb MB)"
Write-Host ""
Write-Host "Try it:  $bundle\autotrans.exe --help"
Write-Host "After uploading the zip to a GitHub Release, new devices pull it with"
Write-Host "scripts/get-runtime.ps1 and point the profile's tool-autotrans config.python"
Write-Host "at the extracted autotrans.exe."
