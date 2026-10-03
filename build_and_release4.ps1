# Build v1.2 with clean venv into fresh output dirs, then release + push.
# Uses dist-out/ and build-out/ to avoid directories left with broken ACLs.
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$venv = Join-Path $root ".venvpro"
$vpy = Join-Path $venv "Scripts\python.exe"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONIOENCODING = "utf-8"
$cnName = -join ([char]0x5FAE, [char]0x535A, [char]0x6536, [char]0x85CF, [char]0x4E0B, [char]0x8F7D, [char]0x5668)
$distOut = Join-Path $root "dist-out"
$buildOut = Join-Path $root "build-out"

if (-not (Test-Path $vpy)) { Write-Host "VENV MISSING: $vpy"; exit 1 }
& $vpy -V
& $vpy -c "import customtkinter, PIL, requests, PyInstaller; print('VENV DEPS OK')"

Write-Host "=== STEP 1: build into fresh dirs ==="
Remove-Item $distOut -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $buildOut -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root "WeiboFavoritesDownloader.spec") -Force -ErrorAction SilentlyContinue
& $vpy -m PyInstaller --noconfirm --onedir --windowed `
    --name "WeiboFavoritesDownloader" `
    --collect-all customtkinter `
    --distpath $distOut `
    --workpath $buildOut `
    (Join-Path $root "weibo_favorites_gui.py")
Write-Host "pyinstaller exit=$LASTEXITCODE"
if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED"; exit 1 }

$distDir = Join-Path $distOut "WeiboFavoritesDownloader"
Write-Host "=== STEP 2: size breakdown ==="
$new = Get-ChildItem $distDir -Recurse -File -ErrorAction SilentlyContinue
Write-Host ("TOTAL: {0:N1} MB / {1} files" -f (($new | Measure-Object Length -Sum).Sum / 1MB), $new.Count)
$new | Sort-Object Length -Descending | Select-Object -First 8 | ForEach-Object {
    Write-Host ("  {0,8:N2} MB  {1}" -f ($_.Length / 1MB), $_.FullName.Substring($distDir.Length))
}

Write-Host "=== STEP 3: assemble + zip ==="
$dst = Join-Path $distOut $cnName
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }
Copy-Item $distDir $dst -Recurse -Force
Move-Item (Join-Path $dst "WeiboFavoritesDownloader.exe") (Join-Path $dst ($cnName + ".exe")) -Force
$relDir = Join-Path $root "release"
if (-not (Test-Path $relDir)) { New-Item -ItemType Directory -Path $relDir -Force | Out-Null }
$zipNew = Join-Path $relDir ($cnName + "-v1.2.zip")
if (Test-Path $zipNew) { Remove-Item $zipNew -Force }
Compress-Archive -Path (Join-Path $dst "*") -DestinationPath $zipNew -Force
$sizeMB = [math]::Round((Get-Item $zipNew).Length / 1MB, 1)
Write-Host "zip size: $sizeMB MB"
Get-ChildItem $relDir | ForEach-Object { Write-Host "  release/ $($_.Name)" }

Write-Host "=== STEP 4: launch smoke test ==="
$p = Start-Process -FilePath (Join-Path $dst ($cnName + ".exe")) -PassThru
Start-Sleep -Seconds 15
if ($p.HasExited) {
    Write-Host "EXE EXITED EARLY code=$($p.ExitCode) - PROBLEM"
} else {
    Write-Host "EXE RUNNING OK"
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
}

Write-Host "=== STEP 5: commit + push ==="
$env:GIT_SSH = Join-Path $root "ssh-wrapper.cmd"
& git -C $root rm -r --cached deps -q 2>$null
& git -C $root rm -r --cached dist -q 2>$null
& git -C $root add -A
& git -C $root status --short | Select-Object -First 12
if ($sizeMB -lt 95) {
    & git -C $root commit -m "feat(v1.2): retweet favorites now save the original post only (no prefix); slim release built from a clean venv"
    & git -C $root push
    Write-Host "push exit=$LASTEXITCODE"
} else {
    Write-Host "SKIP PUSH: zip is $sizeMB MB"
}
Write-Host "=== ALL DONE ==="
