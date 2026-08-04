<#
.SYNOPSIS
    Installs service dependencies into the bundled Python distribution
    from the offline wheel cache under deps\.

    The bundled Python is a full CPython (from python-build-standalone,
    astral-sh), so pip is already present and no ._pth patching is required.

.PARAMETER InstallDir
    The directory where the installer extracted all files — Python lives
    at  $InstallDir\python\python.exe.
#>
param([Parameter(Mandatory)][string]$InstallDir)

$ErrorActionPreference = "Stop"

$python  = Join-Path $InstallDir "python\python.exe"
$depsDir = Join-Path $InstallDir "deps"

if (-not (Test-Path $python)) {
    Write-Error "Python runtime not found at $python"
    exit 1
}

# Install every wheel in deps\ directly, with --no-deps, bypassing pip's
# resolver. The CI step already resolved the full transitive closure when
# it ran `pip download`, so every required wheel is present on disk. Using
# `pip install -r requirements.txt --find-links` re-runs the resolver on
# the target, which can fail with modern pip even when the offline cache
# is complete — so we skip it and just unzip each wheel into site-packages.
#
# pip/setuptools/wheel/packaging are excluded: they already ship with the
# bundled Python distribution and are not needed at runtime by the service.
Write-Host "Installing service dependencies from local wheels..."
$wheels = @(
    Get-ChildItem $depsDir -Filter "*.whl" |
        Where-Object { $_.Name -notmatch '^(pip|setuptools|wheel|packaging)-' } |
        ForEach-Object { $_.FullName }
)

if ($wheels.Count -eq 0) {
    Write-Error "No wheel files found in $depsDir"
    exit 1
}

# --no-warn-script-location: wheels drop console scripts (uvicorn.exe,
# httpx.exe, ...) into python\Scripts, which is deliberately NOT on PATH —
# the service invokes python.exe directly and never uses those .exe shims.
# Without the flag pip prints a PATH warning per script into the NSIS
# install log, which reads like something went wrong.
& $python -m pip install `
    --no-deps `
    --no-index `
    --disable-pip-version-check `
    --no-warn-script-location `
    --quiet `
    $wheels
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

# Byte-compile the stdlib + service code + installed wheels. Without this,
# the first `python main.py` after install spends 30-90 seconds writing
# .pyc files for the entire dependency graph (uvicorn, fastapi, lxml,
# pydantic_core, the bundled stdlib) — and on Windows 11 with Defender
# touching every freshly-written file, that cold-start can balloon to
# minutes and trip the scheduled task's restart loop. Pre-compiling at
# install time pays the cost once, under the installer's UAC elevation,
# so service boot is just module loading.
#
# -q quiet mode (one summary line per directory)
# -f force, even if a .pyc exists, in case the bundled distribution
#    shipped stale ones for a different Python version
# -x skips paths that are never imported at runtime but contain files
#    that cannot byte-compile, so their errors don't pollute the install
#    log: the Tcl/Tix support tree (tix8.4.3\pref\WmDefault.py is
#    Python-2-era and mixes tabs/spaces -> TabError) and stdlib/wheel
#    test dirs (intentionally-broken syntax fixtures).
Write-Host "Pre-compiling Python bytecode (one-time, speeds first boot)..."
& $python -m compileall -q -f -x '[\\/](?:tcl|tests?|idle_test)[\\/]' "$InstallDir" 2>$null
Write-Host "Python environment ready."
