<#
.SYNOPSIS
  Run the full application test suite and the bridge smoke test. Exit code is non-zero if either fails.
.DESCRIPTION
  Tests use temporary folders and in-memory databases; they do not touch your real task queue, chat history or
  settings, and they never start the scheduler or approve anything.
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'No .venv found. Run .\scripts\setup.ps1 first.' }

Write-Host '== application tests'
& $py -m unittest discover -s tests -t .
$unit = $LASTEXITCODE

Write-Host "`n== bridge smoke test"
& $py -I bridge\smoke_test.py
$smoke = $LASTEXITCODE

Write-Host ("`nunit tests: {0}   bridge smoke test: {1}" -f $(if ($unit -eq 0) { 'PASS' } else { 'FAIL' }), $(if ($smoke -eq 0) { 'PASS' } else { 'FAIL' }))
exit ([int]($unit -ne 0 -or $smoke -ne 0))
