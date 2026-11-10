@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "LOG=%~dp0restart_log.txt"
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

echo ==== restart_watcher ==== > "%LOG%"
echo data: %DATE% %TIME% >> "%LOG%"

REM 1. Гасим только наблюдателя ClaudeMeet, а не все python-процессы вообще:
REM    отбираем по командной строке, чтобы не задеть другие python-скрипты.
echo --- gashu staryy nablyudatel --- >> "%LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*meet_recorder.py*' }; if ($p) { $p | ForEach-Object { Write-Output ('killing PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Output 'nablyudatel ne byl zapushchen' }" >> "%LOG%" 2>&1

timeout /t 3 /nobreak >nul

REM 2. Проверяем всплывашку прямо сейчас — чтобы было видно, что сигнал работает.
echo --- proverka vsplyvashki --- >> "%LOG%"
"%PY%" -c "import sys; sys.path.insert(0,r'%~dp0.'); import notify; print('toast:', notify.toast('ClaudeMeet: проверка сигнала', 'Если ты это видишь - всплывашки работают. Наблюдатель перезапущен.'))" >> "%LOG%" 2>&1

REM 3. Поднимаем наблюдателя заново, уже с новым кодом.
echo --- podnimayu nablyudatel --- >> "%LOG%"
start "" wscript.exe "%~dp0watcher_hidden.vbs"
timeout /t 5 /nobreak >nul

echo --- kto zapushchen sechas --- >> "%LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*meet_recorder.py*' } | ForEach-Object { Write-Output ('PID ' + $_.ProcessId + ' : ' + $_.CommandLine) }" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo gotovo >> "%LOG%"
type "%LOG%"
