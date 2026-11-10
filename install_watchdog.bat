@echo off
chcp 65001 >nul
echo [1/2] Collecting Windows power and crash log for the last 3 days ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diag_power.ps1"
echo.
echo [2/2] Installing the recorder watchdog (task ClaudeMeet_Watchdog) ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_watchdog.ps1"
echo.
echo Done: watchdog installed. Log: logs\watchdog_install.txt
pause
