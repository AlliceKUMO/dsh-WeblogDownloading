# Build script: generates the folder package + zip (ASCII-only file)
# Usage: powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# Dependencies live in the project-local ".venvpro" virtual environment:
#   python -m venv .venvpro
#   .venvpro\Scripts\python -m pip install customtkinter pillow requests pyinstaller
# A clean venv matters: building with the Anaconda interpreter bundles its MKL
# libraries and inflates the package from ~17 MB to ~470 MB.
$ErrorActionPreference = "Stop"

$py = Join-Path $PSScriptRoot ".venvpro\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv not found. Create it first:"
    Write-Host "  python -m venv .venvpro"
    Write-Host "  .venvpro\Scripts\python -m pip install customtkinter pillow requests pyinstaller"
    exit 1
}
$env:PYTHONDONTWRITEBYTECODE = "1"

Write-Host "==> Building with PyInstaller (onedir mode: no runtime extraction, avoids fatal-error dialog)"
# Fresh output dirs keep stale/undeletable files from breaking the build
$distOut = Join-Path $PSScriptRoot "dist-out"
$buildOut = Join-Path $PSScriptRoot "build-out"
Remove-Item $buildOut -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $distOut "WeiboFavoritesDownloader") -Recurse -Force -ErrorAction SilentlyContinue

& $py -m PyInstaller --noconfirm --onedir --windowed `
    --name "WeiboFavoritesDownloader" `
    --collect-all customtkinter `
    --distpath $distOut `
    --workpath $buildOut `
    (Join-Path $PSScriptRoot "weibo_favorites_gui.py")
if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED"; exit 1 }

# "微博收藏下载器" from code points, keeps this file ASCII-only
$cnName = -join ([char]0x5FAE, [char]0x535A, [char]0x6536, [char]0x85CF, [char]0x4E0B, [char]0x8F7D, [char]0x5668)
$src = Join-Path $distOut "WeiboFavoritesDownloader"
$dst = Join-Path $distOut $cnName
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }
Copy-Item $src $dst -Recurse -Force
Move-Item (Join-Path $dst "WeiboFavoritesDownloader.exe") (Join-Path $dst ($cnName + ".exe")) -Force

$zip = Join-Path $distOut ($cnName + ".zip")
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $dst "*") -DestinationPath $zip -Force

Write-Host ""
Write-Host ("DONE: {0} ({1:N1} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
Write-Host "Keep the whole folder together; double-click the exe inside."
