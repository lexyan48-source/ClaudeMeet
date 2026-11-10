@echo off
chcp 65001 >nul
REM Автозапуск наблюдателя через папку автозагрузки — без прав администратора.
REM Наблюдателя НЕ стартует — только прописывает автозапуск при входе в систему.

set "LOG=%~dp0setup_startup_log.txt"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SRC=%~dp0watcher_hidden.vbs"

echo ==== setup_startup ==== > "%LOG%"
echo data: %DATE% %TIME% >> "%LOG%"
echo STARTUP: %STARTUP% >> "%LOG%"
echo SRC: %SRC% >> "%LOG%"

if not exist "%SRC%" (
  echo FAILED: net faila watcher_hidden.vbs >> "%LOG%"
  goto :end
)

REM В автозагрузку кладётся не копия watcher_hidden.vbs (он ищет .bat рядом с собой),
REM а короткий .vbs с полным путём к этой папке.
> "%STARTUP%\ClaudeMeet.vbs" echo CreateObject("WScript.Shell").Run """%~dp0run_meet_recorder.bat""", 0, False

if exist "%STARTUP%\ClaudeMeet.vbs" (
  echo OK: ClaudeMeet.vbs v avtozagruzke >> "%LOG%"
) else (
  echo FAILED: kopirovanie ne udalos >> "%LOG%"
)

echo. >> "%LOG%"
echo --- soderzhimoe papki avtozagruzki --- >> "%LOG%"
dir /b "%STARTUP%" >> "%LOG%" 2>&1

:end
type "%LOG%"
