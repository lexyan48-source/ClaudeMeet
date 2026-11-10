@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
set "LOG=%~dp0notify_test_log.txt"

REM Проверка всех каналов уведомлений. Смотри на экран и в Telegram.
echo ==== notify_test ==== > "%LOG%"
echo data: %DATE% %TIME% >> "%LOG%"
echo. >> "%LOG%"

"%PY%" -c "import sys,json; sys.path.insert(0,r'%~dp0.'); import notify; cfg=json.load(open(r'%~dp0meet_config.json',encoding='utf-8')); notify.self_test(cfg)" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo EXITCODE: %ERRORLEVEL% >> "%LOG%"
type "%LOG%"
