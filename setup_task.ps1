$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'logs\setup_task_log.txt'
$logDir = Split-Path $log -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function W($m) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

W '=== setup ClaudeMeet task start ==='

$name = 'ClaudeMeet_Watcher'
$bat  = Join-Path $PSScriptRoot 'run_meet_recorder.bat'

if (-not (Test-Path $bat)) {
    W "FAILED: не найден $bat - задача не создана"
    exit 1
}

# Наблюдатель работает постоянно, поэтому лимит времени выполнения снят (Zero),
# а MultipleInstances IgnoreNew не даёт запустить второй экземпляр поверх первого.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$action  = New-ScheduledTaskAction -Execute $bat
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = 'PT1M'

try {
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    W "$name registered: at logon (delay 1 min), StartWhenAvailable, работает от батареи, IgnoreNew"
}
catch {
    W "$name FAILED: $($_.Exception.Message)"
    exit 1
}

W '--- verify ---'
$t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($null -eq $t) {
    W "$name NOT FOUND после регистрации - что-то пошло не так"
    exit 1
}
$i = Get-ScheduledTaskInfo -TaskName $name
$trig = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', '
W "$name | state=$($t.State) | triggers=$trig | last=$($i.LastRunTime) | lastResult=$($i.LastTaskResult) | StartWhenAvailable=$($t.Settings.StartWhenAvailable) | Battery=$($t.Settings.DisallowStartIfOnBatteries) | MultipleInstances=$($t.Settings.MultipleInstances)"

W 'Запускаю задачу прямо сейчас, чтобы не ждать следующего входа в систему'
try {
    Start-ScheduledTask -TaskName $name
    Start-Sleep -Seconds 5
    $i2 = Get-ScheduledTaskInfo -TaskName $name
    W "после запуска: state=$((Get-ScheduledTask -TaskName $name).State) lastResult=$($i2.LastTaskResult)"
}
catch {
    W "Start-ScheduledTask FAILED: $($_.Exception.Message)"
}

W ('Проверьте лог наблюдателя: ' + (Join-Path $PSScriptRoot 'logs\meet_recorder.log'))
W '=== setup done ==='
