# Build script: generates dist\WeiBoFavoritesDownloader\ folder package + zip
# Usage: powershell -ExecutionPolicy Bypass -File build_exe.ps1
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 parses non-BOM UTF-8 as GBK)
$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "venv not found. Create it first: python -m venv .venv"
    exit 1
}

Write-Host "==> Installing/checking dependencies"
& $venvPython -m pip install --quiet --disable-pip-version-check customtkinter pillow pyinstaller requests "urllib3<2" "requests<2.32"

Write-Host "==> Building with PyInstaller (onedir mode: no runtime extraction, avoids fatal-error dialog)"
# Clean PyInstaller caches first: otherwise it may skip rebuilding and leave dist/ stale
Remove-Item (Join-Path $PSScriptRoot "build") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $PSScriptRoot "dist\WeiboFavoritesDownloader") -Recurse -Force -ErrorAction SilentlyContinue
& $venvPython -m PyInstaller --noconfirm --onedir --windowed `
    --name "WeiboFavoritesDownloader" `
    --collect-all customtkinter `
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
