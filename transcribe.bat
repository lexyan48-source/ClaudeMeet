@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
set "LOG=%~dp0transcribe_log.txt"

REM Расшифровка одной папки записи. Весь вывод дублируется в лог, иначе при
REM закрытии консоли ошибку не увидеть.
REM   transcribe.bat                         последняя запись, модель small
REM   transcribe.bat 2026-08-05_1742         конкретная папка
REM   transcribe.bat 2026-08-05_1742 medium  модель medium

set "FOLDER=%~1"
set "MODEL=%~2"
if "%MODEL%"=="" set "MODEL=small"

echo ==== transcribe ==== > "%LOG%"
echo data: %DATE% %TIME% >> "%LOG%"
echo PY: %PY% >> "%LOG%"
echo FOLDER: [%FOLDER%] MODEL: %MODEL% >> "%LOG%"
echo. >> "%LOG%"

"%PY%" -c "" >nul 2>&1
if errorlevel 1 (
  echo NET PYTHON: %PY% >> "%LOG%"
  goto :end
)
if not exist "%~dp0transcribe.py" (
  echo NET transcribe.py >> "%LOG%"
  goto :end
)

"%PY%" "%~dp0transcribe.py" %FOLDER% --model %MODEL% >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo EXITCODE: %ERRORLEVEL% >> "%LOG%"

:end
type "%LOG%"
