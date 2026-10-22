<#
.SYNOPSIS
  Build the native Windows app folder (dist\Jarvis\Jarvis.exe) with PyInstaller. No paid service is involved.
.DESCRIPTION
  Needs the packaging tool: run .\scripts\setup.ps1 -Dev once. Output goes to dist\ and build\, both ignored by
  Git. The packaged app bundles only the desktop UI. The bridge and worker stay in the project folder; set
  JARVIS_HOME to that folder to see the task queue and worker status in a packaged build.
.EXAMPLE
  .\scripts\build.ps1            # build
  .\scripts\build.ps1 -Smoke     # build, then launch it for a few seconds and check that a window opened
#>
param([switch]$Smoke, [switch]$NoBuild)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'No .venv found. Run .\scripts\setup.ps1 -Dev first.' }
if (-not $NoBuild) {
    & $py -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller is not installed. Run .\scripts\setup.ps1 -Dev first.' }
    & $py -m PyInstaller packaging\jarvis.spec --noconfirm --clean --distpath dist --workpath build
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
}

$exe = Join-Path $Root 'dist\Jarvis\Jarvis.exe'
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing." }
$size = (Get-ChildItem (Split-Path $exe) -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Built {0}  ({1:N0} MB folder)" -f $exe, $size)

if ($Smoke) {
    # throwaway data folder so the check never touches your real history or settings
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $env:JARVIS_DATA_DIR = $tmp
    $p = Start-Process -FilePath $exe -PassThru
    try {
        $title = ''
        for ($i = 0; $i -lt 20 -and -not $title; $i++) { Start-Sleep -Milliseconds 500; $p.Refresh(); $title = $p.MainWindowTitle }
        $alive = -not $p.HasExited
        Write-Host "Launch check: process alive = $alive, window title = '$title'"
        if (-not $alive -or $title -ne 'J.A.R.V.I.S') { throw 'Smoke check FAILED: the packaged app did not open its window.' }
        Write-Host 'Smoke check PASSED.'
    } finally {
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
        $null = $p.WaitForExit(10000)   # let Windows release the database file before cleaning up
        Remove-Item Env:JARVIS_DATA_DIR -ErrorAction SilentlyContinue
        try { if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction Stop } }
        catch { Write-Warning "Could not remove temporary folder $tmp ($($_.Exception.Message)); it holds no personal data." }
    }
}
