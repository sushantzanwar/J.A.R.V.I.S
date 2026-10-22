<#
.SYNOPSIS
  Install, inspect, verify, pause or remove the Jarvis background worker and coordinator (Windows Task Scheduler).

.DESCRIPTION
  Registers two current-user-only tasks, "\JarvisWorker" and "\JarvisCoordinator":
    - runs:      .venv\Scripts\pythonw.exe -m jarvis.worker --allow-file-changes   (no console window)
    - when:      at logon, and every -IntervalMinutes while logged on (missed runs start when available)
    - as:        current user, interactive logon (no stored password), RunLevel Limited (never elevated)
    - limits:    one instance at a time (IgnoreNew), each run stopped after 1 hour
  The coordinator performs one persistent result-event pass after scheduling; it does not run a worker itself.
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

$WorkerTaskName = 'JarvisWorker'
$CoordinatorTaskName = 'JarvisCoordinator'
$Root       = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $Root '.venv\Scripts\pythonw.exe'
$WorkerArgs = '-m jarvis.worker --allow-file-changes --allow-commands'
$CoordinatorArgs = 'bridge\coordinator.py'
$User       = "$env:USERDOMAIN\$env:USERNAME"
$BackupDir  = Join-Path $Root '.jarvis\backups'
$AuditLog   = Join-Path $Root 'logs\scheduler-audit.jsonl'
$WorkerLog  = Join-Path $Root 'logs\worker.jsonl'
$CoordinatorLog = Join-Path $Root 'logs\coordinator.jsonl'
$PauseFile  = Join-Path $Root 'bridge\PAUSE'
$Utf8NoBom  = New-Object System.Text.UTF8Encoding($false)

function Write-Audit([string]$What, [hashtable]$Details) {
    New-Item -ItemType Directory -Force (Split-Path $AuditLog) | Out-Null
    $rec = [ordered]@{ ts = (Get-Date -Format s); action = $What; user = $User }
    foreach ($k in $Details.Keys) { $rec[$k] = $Details[$k] }
    [System.IO.File]::AppendAllText($AuditLog, ((New-Object psobject -Property $rec) | ConvertTo-Json -Compress) + "`n", $Utf8NoBom)
}

function Get-JarvisTask([string]$Name) { Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue }

function Backup-Task([string]$Name) {
    if (-not (Get-JarvisTask $Name)) { return $null }
    New-Item -ItemType Directory -Force $BackupDir | Out-Null
    $file = Join-Path $BackupDir ("{0}-{1}.xml" -f $Name, (Get-Date -Format 'yyyyMMdd-HHmmss'))
    [System.IO.File]::WriteAllText($file, (Export-ScheduledTask -TaskName $Name), $Utf8NoBom)
    return $file
}

function Show-Plan {
    Write-Host "Jarvis scheduled tasks (nothing is changed by this preview):"
    Write-Host "  Worker:    \$WorkerTaskName -> `"$Python`" $WorkerArgs"
    Write-Host "  Coordinator:\$CoordinatorTaskName -> `"$Python`" $CoordinatorArgs"
    Write-Host "  Folder:    $Root"
    Write-Host "  Triggers:  at logon of $User; then every $IntervalMinutes min while logged on"
    Write-Host "  Account:   $User, interactive logon (no password stored), RunLevel Limited (not elevated)"
    Write-Host "  Limits:    one instance of each task at a time; stopped after 1 hour; missed runs start when available"
    Write-Host "  Never:     task approval, unrestricted commands, network listeners"
}

function Show-TaskStatus([string]$Name, [string]$Label) {
    $t = Get-JarvisTask $Name
    if (-not $t) { Write-Host "$Label scheduled task: NOT registered"; }
    else {
        $i = Get-ScheduledTaskInfo -TaskName $Name
        Write-Host "$Label scheduled task: registered, state = $($t.State)"
        Write-Host "  Action:      $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
        Write-Host "  Run as:      $($t.Principal.UserId), $($t.Principal.LogonType), RunLevel $($t.Principal.RunLevel)"
        Write-Host "  Triggers:    $(($t.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace 'MSFT_Task','' -replace 'Trigger','' }) -join ', ')"
        Write-Host "  Last run:    $($i.LastRunTime)  result: $($i.LastTaskResult)"
        Write-Host "  Next run:    $($i.NextRunTime)"
    }
}

function Show-Status {
    Show-TaskStatus $WorkerTaskName 'Worker'
    Show-TaskStatus $CoordinatorTaskName 'Coordinator'
    Write-Host "Worker paused (bridge\PAUSE): $(Test-Path $PauseFile)"
    if (Test-Path $WorkerLog) {
        Write-Host "Last worker log events:"
        Get-Content $WorkerLog -Tail 5 | ForEach-Object { Write-Host "  $_" }
    }
    if (Test-Path $CoordinatorLog) {
        Write-Host "Last coordinator log events:"
        Get-Content $CoordinatorLog -Tail 5 | ForEach-Object { Write-Host "  $_" }
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
        $workerBackup = Backup-Task $WorkerTaskName
        $coordinatorBackup = Backup-Task $CoordinatorTaskName
        $act = New-ScheduledTaskAction -Execute $Python -Argument $WorkerArgs -WorkingDirectory $Root
        $coordinatorAct = New-ScheduledTaskAction -Execute $Python -Argument $CoordinatorArgs -WorkingDirectory $Root
        $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
        $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $WorkerTaskName -Action $act -Trigger @($atLogon, $repeat) -Principal $principal `
            -Settings $settings -Force `
            -Description 'Jarvis: auto-runs policy-approved read/file tasks and human-approved allowlisted commands tasks. Never auto-approves commands.' | Out-Null
        # Create independent trigger/settings objects: scheduled-task CIM objects are not safely reusable.
        $coordAtLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
        $coordRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
        $coordSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $CoordinatorTaskName -Action $coordinatorAct -Trigger @($coordAtLogon, $coordRepeat) -Principal $principal `
            -Settings $coordSettings -Force `
            -Description 'Jarvis: processes persistent task result events and submits only policy-permitted bounded follow-ups. Never wakes Codex.' | Out-Null
        Write-Audit 'install' @{ interval_minutes = $IntervalMinutes; worker_command = "$Python $WorkerArgs"; coordinator_command = "$Python $CoordinatorArgs"; worker_backup = "$workerBackup"; coordinator_backup = "$coordinatorBackup" }
        Write-Host "`nRegistered. Previous definitions backed up to: worker=$(if ($workerBackup) { $workerBackup } else { '(none existed)' }), coordinator=$(if ($coordinatorBackup) { $coordinatorBackup } else { '(none existed)' })"
        Show-Status
    }

    'Verify' {
        $t = Get-JarvisTask $WorkerTaskName
        $c = Get-JarvisTask $CoordinatorTaskName
        if (-not $t -or -not $c) { throw 'Worker and coordinator tasks must both be registered.' }
        $problems = @()
        if ($t.Principal.RunLevel -ne 'Limited') { $problems += 'task would run elevated' }
        if ($t.Principal.LogonType -ne 'Interactive') { $problems += 'unexpected logon type' }
        if ($c.Principal.RunLevel -ne 'Limited' -or $c.Principal.LogonType -ne 'Interactive') { $problems += 'coordinator has unexpected principal' }
        if ($t.State -eq 'Disabled' -or $c.State -eq 'Disabled') { $problems += 'task is disabled' }
        if ($problems) { throw ("Verification failed: " + ($problems -join '; ')) }
        $start = Get-Date
        Start-ScheduledTask -TaskName $WorkerTaskName
        Start-ScheduledTask -TaskName $CoordinatorTaskName
        $deadline = (Get-Date).AddMinutes(5)
        do {
            Start-Sleep -Seconds 2
            $info = Get-ScheduledTaskInfo -TaskName $WorkerTaskName
            $coordInfo = Get-ScheduledTaskInfo -TaskName $CoordinatorTaskName
            $state = (Get-JarvisTask $WorkerTaskName).State
            $coordState = (Get-JarvisTask $CoordinatorTaskName).State
        } while ((Get-Date) -lt $deadline -and ($state -eq 'Running' -or $coordState -eq 'Running' -or $info.LastRunTime -lt $start.AddSeconds(-1) -or $coordInfo.LastRunTime -lt $start.AddSeconds(-1)))
        $ran = @()
        if (Test-Path $WorkerLog) {
            # @() matters: in Windows PowerShell 5.1 a single PSCustomObject has no .Count
            $ran = @(Get-Content $WorkerLog | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } } |
                Where-Object { $_.event -eq 'worker_start' -and ([datetime]$_.ts) -ge $start.AddSeconds(-1) })
        }
        $coordinatorRan = @()
        if (Test-Path $CoordinatorLog) {
            $coordinatorRan = @(Get-Content $CoordinatorLog | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } } |
                Where-Object { $_.event -eq 'coordinator_start' -and ([datetime]$_.ts) -ge $start.AddSeconds(-1) })
        }
        $ok = ($info.LastTaskResult -eq 0) -and ($ran.Count -ge 1) -and ($coordInfo.LastTaskResult -eq 0) -and ($coordinatorRan.Count -ge 1)
        Write-Audit 'verify' @{ ok = $ok; worker_result = $info.LastTaskResult; worker_started = $ran.Count; coordinator_result = $coordInfo.LastTaskResult; coordinator_started = $coordinatorRan.Count }
        Write-Host "Worker: last run $($info.LastRunTime) result $($info.LastTaskResult); coordinator: last run $($coordInfo.LastRunTime) result $($coordInfo.LastTaskResult)"
        Write-Host "Worker log shows $($ran.Count) start(s); coordinator log shows $($coordinatorRan.Count) start(s) since $start"
        if (-not $ok) { throw 'Verification FAILED: the scheduler did not complete both background passes.' }
        Write-Host 'Verification PASSED: the scheduler started both passes and they completed.'
    }

    'Disable'   { Disable-ScheduledTask -TaskName $WorkerTaskName,$CoordinatorTaskName | Out-Null; Write-Audit 'disable' @{}; Show-Status }
    'Enable'    { Enable-ScheduledTask -TaskName $WorkerTaskName,$CoordinatorTaskName | Out-Null; Write-Audit 'enable' @{}; Show-Status }

    'Uninstall' {
        $workerBackup = Backup-Task $WorkerTaskName
        $coordinatorBackup = Backup-Task $CoordinatorTaskName
        if ($workerBackup) { Unregister-ScheduledTask -TaskName $WorkerTaskName -Confirm:$false }
        if ($coordinatorBackup) { Unregister-ScheduledTask -TaskName $CoordinatorTaskName -Confirm:$false }
        Write-Audit 'uninstall' @{ worker_backup = "$workerBackup"; coordinator_backup = "$coordinatorBackup" }
        if ($workerBackup -or $coordinatorBackup) { Write-Host "Removed. Definitions backed up to: worker=$workerBackup coordinator=$coordinatorBackup" }
        else { Write-Host 'Nothing to remove.' }
    }
}
