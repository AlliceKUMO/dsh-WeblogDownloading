# Build script: generates the folder package + zip (ASCII-only file)
# Usage: powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# Dependencies live in the project-local "deps" folder (installed with:
#   python -m pip install --target deps customtkinter pyinstaller )
# The system Python (3.14) already provides requests / Pillow.
$ErrorActionPreference = "Stop"

$deps = Join-Path $PSScriptRoot "deps"
if (-not (Test-Path $deps)) {
    Write-Host "deps folder not found. Install it first:"
    Write-Host "  python -m pip install --target deps customtkinter pyinstaller"
    exit 1
}
$env:PYTHONPATH = $deps
$env:PYTHONDONTWRITEBYTECODE = "1"

Write-Host "==> Building with PyInstaller (onedir mode: no runtime extraction, avoids fatal-error dialog)"
# Clean caches first: otherwise PyInstaller may skip rebuilding and leave dist/ stale
Remove-Item (Join-Path $PSScriptRoot "build") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $PSScriptRoot "dist\WeiboFavoritesDownloader") -Recurse -Force -ErrorAction SilentlyContinue
& python -m PyInstaller --noconfirm --onedir --windowed `
    --name "WeiboFavoritesDownloader" `
    --collect-all customtkinter `
    --paths $deps `
    weibo_favorites_gui.py
if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED"; exit 1 }

$src = Join-Path $PSScriptRoot "dist\WeiboFavoritesDownloader"
$dst = Join-Path $PSScriptRoot "dist\WeiBoFavoritesDownloader"
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-Item $src $dst -Recurse -Force
Move-Item (Join-Path $dst "WeiboFavoritesDownloader.exe") (Join-Path $dst "WeiBoFavoritesDownloader.exe") -Force

$zip = Join-Path $PSScriptRoot "dist\WeiBoFavoritesDownloader.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $dst "*") -DestinationPath $zip -Force

Write-Host ""
Write-Host "DONE: folder $dst, zip $zip"
Write-Host "Keep the whole folder together; double-click the exe inside."
