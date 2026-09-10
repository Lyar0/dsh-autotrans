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
    [string]$Name = "autotrans",
    [string]$OpenSSLBin = ""
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

# --- OpenSSL DLL pinning -------------------------------------------------------
# PyInstaller sometimes resolves libssl/libcrypto to a DIFFERENT OpenSSL than the
# one $Python's own _ssl.pyd links against (e.g. a stale conda pair), which makes
# the frozen runtime throw:
#     ImportError: DLL load failed while importing _ssl: <procedure not found>
# and downstream urllib reports  "unknown url type: https".
# Fix: resolve the OpenSSL version $Python reports, locate those exact DLLs inside
# the interpreter's environment, and force PyInstaller to ship them. If auto-detection
# fails we keep building (no pin) and just warn; the author can pass -OpenSSLBin.
# -------------------------------------------------------------------------------
$pinArgs = @()

function Resolve-OpenSslBin {
    param([string]$PythonExe)
    # _ssl sits in the interpreter's env; for conda it is <env>/Library/bin.
    # NOTE: PyInstaller otherwise resolves a stale OpenSSL 3.0.x pair that does not
    # match the interpreter's _ssl.pyd, which breaks every https call in the frozen
    # runtime ("unknown url type: https"), so the pin matters.
    $candRoots = @()
    $exeDir = Split-Path $PythonExe -Parent          # <env> or <env>/Scripts
    $candRoots += $exeDir
    $envRoot = Split-Path $exeDir -Parent            # <env> from <env>/python or <env>/Scripts
    if (Test-Path (Join-Path $envRoot "Library\bin")) { $candRoots += (Join-Path $envRoot "Library\bin") }
    if (Test-Path (Join-Path $exeDir "Library\bin")) { $candRoots += (Join-Path $exeDir "Library\bin") }
    foreach ($r in $candRoots) {
        $ssl = Get-Item (Join-Path $r "libssl-3-x64.dll") -ErrorAction SilentlyContinue
        $crp = Get-Item (Join-Path $r "libcrypto-3-x64.dll") -ErrorAction SilentlyContinue
        if ($ssl -and $crp) {
            $pySsl = & $PythonExe -c "import ssl;print(ssl.OPENSSL_VERSION)" 2>$null
            if ($pySsl) { Write-Host "  interpreter $pySsl" }
            Write-Host "  pin OpenSSL @ $r (libssl $($ssl.VersionInfo.FileVersion))"
            return $r
        }
    }
    return $null
}

if ($OpenSSLBin -and (Test-Path $OpenSSLBin)) {
    Write-Host "==> Pinning OpenSSL from explicit -OpenSSLBin: $OpenSSLBin"
    $pinArgs = @(
        "--add-binary", ((Join-Path $OpenSSLBin "libssl-3-x64.dll") + ";."),
        "--add-binary", ((Join-Path $OpenSSLBin "libcrypto-3-x64.dll") + ";.")
    )
} else {
    $match = Resolve-OpenSslBin -PythonExe $Python
    if ($match) {
        $pinArgs = @(
            "--add-binary", ((Join-Path $match "libssl-3-x64.dll") + ";."),
            "--add-binary", ((Join-Path $match "libcrypto-3-x64.dll") + ";.")
        )
    } else {
        Write-Host "  WARNING: could not auto-locate OpenSSL DLLs next to $Python; building WITHOUT a pin."
        Write-Host "  If the frozen exe throws 'DLL load failed while importing _ssl', build with:"
        Write-Host "    -OpenSSLBin D:/Projects/CondaEnvs/<env>/Library/bin"
    }
}

$dataSpec = (Join-Path $root "python\glossaries") + ";glossaries"
$args = @(
    "--noconfirm", "--clean", "--onedir", "--name", $Name,
    "--paths", (Join-Path $root "python"),
    "--add-data", $dataSpec,
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build")
) + $pinArgs + @( (Join-Path $pythonDir "autotrans.py") )
& $Python -m PyInstaller @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Post-build assertion: the produced onedir must include a libssl whose version
# matches the pinned one (guards against shipping the broken 3.0.7-style pair).
$builtSsl = Get-Item (Join-Path $bundle "libssl-3-x64.dll") -ErrorAction SilentlyContinue
if ($builtSsl) { Write-Host "  -> bundled libssl version: $($builtSsl.VersionInfo.FileVersion)" }

# Thrown if an OpenSSL pin was requested but the output lacks our DLL (config/ABI drift).
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
Write-Host "SSL check built-in: the frozen exe now bundles a self-consistent OpenSSL"
Write-Host "(see above 'bundled libssl version'), so https translation works."
Write-Host "After uploading the zip to a GitHub Release, new devices pull it with"
Write-Host "scripts/get-runtime.ps1 and point the profile's tool-autotrans config.python"
Write-Host "at the extracted autotrans.exe."
