<#
.SYNOPSIS
  Create the project-local virtual environment and install dependencies. Safe to re-run.
.DESCRIPTION
  Everything stays inside the project folder (.venv). Nothing is installed globally, no service is started,
  and no setting outside the project is changed.
.EXAMPLE
  .\scripts\setup.ps1          # app dependencies (PySide6)
  .\scripts\setup.ps1 -Dev     # also the packaging tool (PyInstaller)
#>
param([switch]$Dev)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    foreach ($candidate in @(@('py', '-3.12'), @('python'))) {
        $exe = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $extra = @($candidate | Select-Object -Skip 1)
        $ok = & $exe.Source @extra -c "import sys; print(sys.version_info >= (3, 12))" 2>$null
        if ($ok -eq 'True') { return @($exe.Source) + $extra }
    }
    throw 'Python 3.12 or newer was not found. Install it from python.org, then re-run.'
}

$venvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    $py = Find-Python
    Write-Host "Creating .venv with $($py -join ' ')"
    & $py[0] @($py | Select-Object -Skip 1) -m venv .venv
} else {
    Write-Host '.venv already exists; keeping it.'
}

$requirements = if ($Dev) { 'requirements-dev.txt' } else { 'requirements.txt' }
Write-Host "Installing from $requirements into .venv"
& $venvPython -m pip install --disable-pip-version-check --default-timeout 60 --retries 5 -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

& $venvPython -c "import sys, PySide6; print('Python', sys.version.split()[0], '| PySide6', PySide6.__version__)"
Write-Host "`nNext:  .\scripts\run-tests.ps1   then   .\.venv\Scripts\python.exe -m jarvis"
