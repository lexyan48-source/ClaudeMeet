$ErrorActionPreference = 'Continue'
$dir = $PSScriptRoot
$log = Join-Path $dir 'logs\watchdog_install.txt'
if (-not (Test-Path (Split-Path $log -Parent))) { New-Item -ItemType Directory -Path (Split-Path $log -Parent) -Force | Out-Null }
function W($m) { $l = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m; Write-Output $l; Add-Content -Path $log -Value $l -Encoding UTF8 }

$name = 'ClaudeMeet_Watchdog'
$vbs  = Join-Path $dir 'watchdog_hidden.vbs'
if (-not (Test-Path $vbs)) { W "FAILED: $vbs not found"; exit 1 }

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $vbs + '"')
$t1 = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $name -Action $action -Trigger @($t1, $t2) -Settings $settings -Principal $principal -Force | Out-Null
    W "$name registered: at logon + every 5 min, runs only in the logged-in user session"
} catch {
    W "$name FAILED: $($_.Exception.Message)"
    exit 1
}
try { Start-ScheduledTask -TaskName $name; Start-Sleep -Seconds 8 } catch { W "Start FAILED: $($_.Exception.Message)" }
$t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
$i = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
W "state=$($t.State) lastRun=$($i.LastRunTime) lastResult=$($i.LastTaskResult) nextRun=$($i.NextRunTime)"
W 'Check watchdog log: logs\watchdog.log'
