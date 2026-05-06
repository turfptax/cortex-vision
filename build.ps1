# cortex-vision build helper — clean rebuild via PyInstaller and smoke-test.
#
# Usage:
#     .\build.ps1            # full clean rebuild
#     .\build.ps1 -SkipClean # incremental, much faster after first build
#     .\build.ps1 -Test      # rebuild then smoke-test the bundle
#
# Output: dist\cortex-vision\cortex-vision.exe

param(
    [switch]$SkipClean,
    [switch]$Test
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
Set-Location $projectRoot

# --- Verify venv is activated -----------------------------------------------
if (-not $env:VIRTUAL_ENV) {
    Write-Host "ERROR: No virtualenv active. Run:" -ForegroundColor Red
    Write-Host "    .\.venv\Scripts\activate" -ForegroundColor Yellow
    exit 1
}

# --- Verify pyinstaller is installed ---------------------------------------
$pyinstallerCmd = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyinstallerCmd) {
    Write-Host "ERROR: pyinstaller not found. Install build deps:" -ForegroundColor Red
    Write-Host "    pip install -e .[dev,build]" -ForegroundColor Yellow
    exit 1
}

# --- Clean previous artifacts ----------------------------------------------
if (-not $SkipClean) {
    Write-Host "[1/3] Cleaning previous build artifacts..." -ForegroundColor Cyan
    if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
    if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist" }
}

# --- Build ------------------------------------------------------------------
Write-Host "[2/3] Building cortex-vision.exe (this takes 1-3 minutes)..." -ForegroundColor Cyan

$pyiArgs = @("cortex-vision.spec", "--noconfirm")
if (-not $SkipClean) { $pyiArgs += "--clean" }

& pyinstaller @pyiArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed. See above for details." -ForegroundColor Red
    exit $LASTEXITCODE
}

# --- Verify output ---------------------------------------------------------
$exePath = Join-Path $projectRoot "dist\cortex-vision\cortex-vision.exe"
if (-not (Test-Path $exePath)) {
    Write-Host "Build completed but $exePath was not produced." -ForegroundColor Red
    exit 1
}

$bundleSize = (Get-ChildItem -Recurse "dist\cortex-vision" | Measure-Object -Property Length -Sum).Sum
$bundleMB = [math]::Round($bundleSize / 1MB, 1)

Write-Host ""
Write-Host "[3/3] Build complete." -ForegroundColor Green
Write-Host "  Bundle:  dist\cortex-vision\"
Write-Host "  Binary:  $exePath"
Write-Host "  Size:    $bundleMB MB"
Write-Host ""

# --- Optional smoke test ----------------------------------------------------
if ($Test) {
    Write-Host "Running smoke test..." -ForegroundColor Cyan
    Write-Host "  Starting bundled .exe on port 8005 (avoids dev sidecar conflict)..."

    $env:CORTEX_VISION_PORT = "8005"
    $proc = Start-Process -FilePath $exePath -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput "build\smoke-stdout.log" `
        -RedirectStandardError "build\smoke-stderr.log"

    Start-Sleep -Seconds 4

    try {
        $health = Invoke-RestMethod -Uri "http://localhost:8005/api/video/health" -TimeoutSec 5
        Write-Host "  PASS  /api/video/health -> $($health.status) (version $($health.version))" -ForegroundColor Green

        $diag = Invoke-RestMethod -Uri "http://localhost:8005/api/video/diagnostics" -TimeoutSec 5
        Write-Host "  PASS  /api/video/diagnostics returned $($diag.PSObject.Properties.Count) top-level keys" -ForegroundColor Green
    } catch {
        Write-Host "  FAIL  Bundle smoke test: $_" -ForegroundColor Red
        Write-Host "  See logs:  build\smoke-stderr.log" -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        exit 1
    } finally {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Remove-Item Env:\CORTEX_VISION_PORT -ErrorAction SilentlyContinue
    }

    Write-Host ""
    Write-Host "Smoke test passed." -ForegroundColor Green
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  Manual test:    .\dist\cortex-vision\cortex-vision.exe"
Write-Host "  Then in another shell:  curl http://localhost:8004/api/video/diagnostics"
Write-Host "  Zip for release:  Compress-Archive -Path dist\cortex-vision\* -DestinationPath cortex-vision-0.1.0-windows-cpu.zip"
