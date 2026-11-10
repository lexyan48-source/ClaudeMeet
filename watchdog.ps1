$ErrorActionPreference = 'Continue'
$dir = $PSScriptRoot
$log = Join-Path $dir 'logs\watchdog.log'
$stateFile = Join-Path $dir 'logs\watchdog_state.txt'
function W($m) { try { Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8 } catch {} }

function Get-Recorder {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*meet_recorder.py*') -or
        ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*run_meet_recorder.bat*') })
}

$boot = $null
try { $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime } catch {}
$prev = ''
if (Test-Path $stateFile) { $prev = [string](Get-Content $stateFile -ErrorAction SilentlyContinue | Select-Object -First 1) }

$procs = Get-Recorder

# Hang check: the recorder (patched 29.10) touches logs\heartbeat.txt every poll. A process that is alive
# but whose heartbeat is older than 5 min (and newer than the process itself) is hung - kill and restart it.
$hb = Join-Path $dir 'logs\heartbeat.txt'
$pyProcs = @($procs | Where-Object { $_.Name -eq 'python.exe' })
if ($pyProcs.Count -gt 0 -and (Test-Path $hb)) {
    $hbTime = (Get-Item $hb).LastWriteTime
    $started = ($pyProcs | Sort-Object CreationDate | Select-Object -First 1).CreationDate
    $age = ((Get-Date) - $hbTime).TotalSeconds
    if ($hbTime -gt $started -and $age -gt 300) {
        W ("HUNG: recorder process alive but heartbeat is " + [int]$age + " s old - killing and restarting")
        foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
        Start-Sleep -Seconds 3
        $procs = Get-Recorder
    }
}

if ($procs.Count -gt 0) {
    if ($prev -ne 'up' -or (Get-Date).Minute -lt 5) {
        W ("OK: recorder running, pids " + (($procs | ForEach-Object { $_.ProcessId }) -join ',') + ", boot=$boot")
    }
    Set-Content -Path $stateFile -Value 'up'
    exit 0
}

# Recorder is not running. Give the Startup folder time right after logon, to avoid two copies.
$exp = Get-Process explorer -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
if ($exp -and ((Get-Date) - $exp.StartTime).TotalSeconds -lt 120) {
    W 'recorder not running, logon was less than 2 min ago - waiting for Startup folder'
    exit 0
}

W ("DOWN: recorder not running (previous state: '$prev', boot=$boot) - starting")
Set-Content -Path $stateFile -Value 'down'
Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + (Join-Path $dir 'watcher_hidden.vbs') + '"')
Start-Sleep -Seconds 20
$after = Get-Recorder
if ($after.Count -gt 0) {
    W 'started OK'
    Set-Content -Path $stateFile -Value 'up'
} else {
    W 'FAILED to start - see logs\_runner.log'
}
