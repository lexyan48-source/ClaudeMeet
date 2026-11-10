$ErrorActionPreference = 'Continue'
$out = Join-Path $PSScriptRoot 'logs\power_events.txt'
if (-not (Test-Path (Split-Path $out -Parent))) { New-Item -ItemType Directory -Path (Split-Path $out -Parent) -Force | Out-Null }
$end   = Get-Date
$start = $end.AddDays(-3)
"Power and crash events $start - $end (collected $(Get-Date))" | Out-File $out -Encoding UTF8
"--- System: boot 12/6005, shutdown 13/6006/109/1074, crash 41/6008, sleep 42/506, resume 107/507/1 ---" | Out-File $out -Append -Encoding UTF8
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$start; EndTime=$end; Id=1,12,13,41,42,107,109,506,507,1074,1076,6005,6006,6008} -ErrorAction SilentlyContinue |
  Sort-Object TimeCreated |
  ForEach-Object { "{0:yyyy-MM-dd HH:mm:ss}  id={1,-5} {2}  | {3}" -f $_.TimeCreated, $_.Id, $_.ProviderName, (($_.Message -split "`r?`n")[0]) } |
  Out-File $out -Append -Encoding UTF8
"--- Application: crashes and hangs 1000/1001/1002 (python, ffmpeg, Telemost, chrome) ---" | Out-File $out -Append -Encoding UTF8
Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=$start; EndTime=$end; Id=1000,1001,1002} -ErrorAction SilentlyContinue |
  Sort-Object TimeCreated |
  ForEach-Object { "{0:yyyy-MM-dd HH:mm:ss}  id={1,-5} {2}  | {3}" -f $_.TimeCreated, $_.Id, $_.ProviderName, (($_.Message -split "`r?`n" | Select-Object -First 2) -join ' ') } |
  Out-File $out -Append -Encoding UTF8
"--- Logon/lock sessions: 4624 type 2/7/10, 4800 lock, 4801 unlock (if auditing is on) ---" | Out-File $out -Append -Encoding UTF8
Get-WinEvent -FilterHashtable @{LogName='Security'; StartTime=$start; EndTime=$end; Id=4800,4801} -ErrorAction SilentlyContinue |
  Sort-Object TimeCreated |
  ForEach-Object { "{0:yyyy-MM-dd HH:mm:ss}  id={1}" -f $_.TimeCreated, $_.Id } |
  Out-File $out -Append -Encoding UTF8
Write-Output "Saved: $out"
