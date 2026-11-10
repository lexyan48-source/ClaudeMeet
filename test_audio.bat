@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo Sejchas budet 10 sekund zapisi.
echo GOVORITE v mikrofon I vklyuchite lyuboj zvuk na kompyutere (muzyku, rolik).
echo Inache proveryat budet nechego.
echo.
pause

"%PY%" meet_recorder.py --test-audio
echo.
pause
