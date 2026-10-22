<#
.SYNOPSIS
  Install, inspect, verify, pause or remove the Jarvis background worker (Windows Task Scheduler).

.DESCRIPTION
  Registers ONE task, "\JarvisWorker", for the current user only:
    - runs:      .venv\Scripts\pythonw.exe -m jarvis.worker --allow-file-changes   (no console window)
    - when:      at logon, and every -IntervalMinutes while logged on (missed runs start when available)
    - as:        current user, interactive logon (no stored password), RunLevel Limited (never elevated)
    - limits:    one instance at a time (IgnoreNew), each run stopped after 1 hour
  It enables commands-tier tasks only when they are human-approved and match the local command allowlist.
  It never auto-approves tasks or opens a network port.
  Install requires the explicit authorization phrase. Existing task definitions are exported to
  .jarvis\backups before they are replaced or removed, and every change is appended to logs\scheduler-audit.jsonl.

.EXAMPLE
  .\scripts\jarvis-scheduler.ps1                                   # Status (read-only)
  .\scripts\jarvis-scheduler.ps1 -Action Install -Authorize REGISTER
  .\scripts\jarvis-scheduler.ps1 -Action Verify                    # runs one pass now and checks it really ran
  .\scripts\jarvis-scheduler.ps1 -Action Disable | Enable | Uninstall
#>
param(
    [ValidateSet('Status', 'Install', 'Verify', 'Disable', 'Enable', 'Uninstall')]
    [string]$Action = 'Status',
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 10,
    [string]$Authorize = ''
)
$ErrorActionPreference = 'Stop'

$TaskName   = 'JarvisWorker'
$Root       = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $Root '.venv\Scripts\pythonw.exe'
$WorkerArgs = '-m jarvis.worker --allow-file-changes --allow-commands'
$User       = "$env:USERDOMAIN\$env:USERNAME"
$BackupDir  = Join-Path $Root '.jarvis\backups'
$AuditLog   = Join-Path $Root 'logs\scheduler-audit.jsonl'
$WorkerLog  = Join-Path $Root 'logs\worker.jsonl'
$PauseFile  = Join-Path $Root 'bridge\PAUSE'
$Utf8NoBom  = New-Object System.Text.UTF8Encoding($false)

function Write-Audit([string]$What, [hashtable]$Details) {
    New-Item -ItemType Directory -Force (Split-Path $AuditLog) | Out-Null
    $rec = [ordered]@{ ts = (Get-Date -Format s); action = $What; user = $User }
    foreach ($k in $Details.Keys) { $rec[$k] = $Details[$k] }
    [System.IO.File]::AppendAllText($AuditLog, ((New-Object psobject -Property $rec) | ConvertTo-Json -Compress) + "`n", $Utf8NoBom)
}

function Get-JarvisTask { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

function Backup-Task {
    if (-not (Get-JarvisTask)) { return $null }
    New-Item -ItemType Directory -Force $BackupDir | Out-Null
    $file = Join-Path $BackupDir ("JarvisWorker-{0}.xml" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    [System.IO.File]::WriteAllText($file, (Export-ScheduledTask -TaskName $TaskName), $Utf8NoBom)
    return $file
}

function Show-Plan {
    Write-Host "Jarvis worker scheduled task (nothing is changed by this preview):"
    Write-Host "  Task:      \$TaskName"
    Write-Host "  Runs:      `"$Python`" $WorkerArgs"
    Write-Host "  Folder:    $Root"
    Write-Host "  Triggers:  at logon of $User; then every $IntervalMinutes min while logged on"
    Write-Host "  Account:   $User, interactive logon (no password stored), RunLevel Limited (not elevated)"
    Write-Host "  Limits:    one instance at a time; stopped after 1 hour; missed runs start when available"
    Write-Host "  Never:     task approval, unrestricted commands, network listeners"
}

function Show-Status {
    $t = Get-JarvisTask
    if (-not $t) { Write-Host "Scheduled task: NOT registered"; }
    else {
        $i = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Scheduled task: registered, state = $($t.State)"
        Write-Host "  Action:      $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
        Write-Host "  Run as:      $($t.Principal.UserId), $($t.Principal.LogonType), RunLevel $($t.Principal.RunLevel)"
        Write-Host "  Triggers:    $(($t.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace 'MSFT_Task','' -replace 'Trigger','' }) -join ', ')"
        Write-Host "  Last run:    $($i.LastRunTime)  result: $($i.LastTaskResult)"
        Write-Host "  Next run:    $($i.NextRunTime)"
    }
    Write-Host "Worker paused (bridge\PAUSE): $(Test-Path $PauseFile)"
    if (Test-Path $WorkerLog) {
        Write-Host "Last worker log events:"
        Get-Content $WorkerLog -Tail 5 | ForEach-Object { Write-Host "  $_" }
    }
}

switch ($Action) {
    'Status' { Show-Status }

    'Install' {
        Show-Plan
        if ($Authorize -cne 'REGISTER') {
            Write-Host "`nNot registered. To authorize this one-time change, re-run with:  -Action Install -Authorize REGISTER"
            exit 2
        }
        if (-not (Test-Path $Python)) { throw "Python not found: $Python (create the venv first)" }
        $backup = Backup-Task
        $act = New-ScheduledTaskAction -Execute $Python -Argument $WorkerArgs -WorkingDirectory $Root
        $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
        $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger @($atLogon, $repeat) -Principal $principal `
            -Settings $settings -Force `
            -Description 'Jarvis: auto-runs policy-approved read/file tasks and human-approved allowlisted commands tasks. Never auto-approves commands.' | Out-Null
        Write-Audit 'install' @{ interval_minutes = $IntervalMinutes; command = "$Python $WorkerArgs"; backup = "$backup" }
        Write-Host "`nRegistered. Previous definition backed up to: $(if ($backup) { $backup } else { '(none existed)' })"
        Show-Status
    }

    'Verify' {
        $t = Get-JarvisTask
        if (-not $t) { throw 'Not registered.' }
        $problems = @()
        if ($t.Principal.RunLevel -ne 'Limited') { $problems += 'task would run elevated' }
        if ($t.Principal.LogonType -ne 'Interactive') { $problems += 'unexpected logon type' }
        if ($t.State -eq 'Disabled') { $problems += 'task is disabled' }
        if ($problems) { throw ("Verification failed: " + ($problems -join '; ')) }
        $start = Get-Date
        Start-ScheduledTask -TaskName $TaskName
        $deadline = (Get-Date).AddMinutes(5)
        do {
            Start-Sleep -Seconds 2
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            $state = (Get-JarvisTask).State
        } while ((Get-Date) -lt $deadline -and ($state -eq 'Running' -or $info.LastRunTime -lt $start.AddSeconds(-1)))
        $ran = @()
        if (Test-Path $WorkerLog) {
            # @() matters: in Windows PowerShell 5.1 a single PSCustomObject has no .Count
            $ran = @(Get-Content $WorkerLog | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } } |
                Where-Object { $_.event -eq 'worker_start' -and ([datetime]$_.ts) -ge $start.AddSeconds(-1) })
        }
        $ok = ($info.LastTaskResult -eq 0) -and ($ran.Count -ge 1)
        Write-Audit 'verify' @{ ok = $ok; last_result = $info.LastTaskResult; worker_started = $ran.Count }
        Write-Host "Last run: $($info.LastRunTime)  result: $($info.LastTaskResult)  next: $($info.NextRunTime)"
        Write-Host "Worker log shows $($ran.Count) worker start(s) since $start"
        if (-not $ok) { throw 'Verification FAILED: the scheduler did not complete a worker pass.' }
        Write-Host 'Verification PASSED: the scheduler started the worker and the pass completed.'
    }

    'Disable'   { Disable-ScheduledTask -TaskName $TaskName | Out-Null; Write-Audit 'disable' @{}; Show-Status }
    'Enable'    { Enable-ScheduledTask -TaskName $TaskName | Out-Null; Write-Audit 'enable' @{}; Show-Status }

    'Uninstall' {
        $backup = Backup-Task
        if ($backup) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Audit 'uninstall' @{ backup = "$backup" }
            Write-Host "Removed. Definition backed up to: $backup"
        } else { Write-Host 'Nothing to remove.' }
    }
}
