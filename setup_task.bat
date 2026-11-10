@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_task.ps1"
echo.
echo ------------------------------------------------------------
echo Zadacha ClaudeMeet_Watcher nastroena na avtozapusk pri vhode.
echo Log: %~dp0logs\setup_task_log.txt
echo Proverit: schtasks /query /tn ClaudeMeet_Watcher
echo ------------------------------------------------------------
pause
