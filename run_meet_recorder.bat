@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
"%PY%" meet_recorder.py >> "%~dp0logs\_runner.log" 2>&1
