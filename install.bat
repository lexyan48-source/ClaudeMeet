@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
set OUT=install_log.txt
set FFMPEG=%~dp0ffmpeg\bin\ffmpeg.exe
set FFPROBE=%~dp0ffmpeg\bin\ffprobe.exe

echo ==== ustanovka ClaudeMeet ==== > %OUT%
echo data: %DATE% %TIME% >> %OUT%
echo python: %PY% >> %OUT%
echo. >> %OUT%

echo ==== versiya python ==== >> %OUT%
"%PY%" -c "import sys; print(sys.version); print(sys.executable)" >> %OUT% 2>&1
echo. >> %OUT%

echo ==== ustanovka PyAudioWPatch ==== >> %OUT%
"%PY%" -m pip install PyAudioWPatch >> %OUT% 2>&1
echo. >> %OUT%

echo ==== proverka PyAudioWPatch ==== >> %OUT%
"%PY%" -c "import pyaudiowpatch as p; print('PyAudioWPatch OK'); a=p.PyAudio(); print('ustroystv:', a.get_device_count()); n=sum(1 for i in range(a.get_device_count()) if a.get_device_info_by_index(i).get('isLoopbackDevice')); print('loopback ustroystv:', n); a.terminate()" >> %OUT% 2>&1
echo. >> %OUT%

echo ==== proverka numpy ==== >> %OUT%
"%PY%" -c "import numpy; print('numpy', numpy.__version__)" >> %OUT% 2>&1
echo. >> %OUT%

echo ==== proverka ffmpeg ==== >> %OUT%
where ffmpeg >nul 2>&1 && set "FFMPEG=ffmpeg"
where ffprobe >nul 2>&1 && set "FFPROBE=ffprobe"
if "%FFMPEG%"=="ffmpeg" (
  echo naiden v PATH: ffmpeg >> %OUT%
  ffmpeg -version >> %OUT% 2>&1
) else if exist "%FFMPEG%" (
  echo naiden: %FFMPEG% >> %OUT%
  "%FFMPEG%" -version >> %OUT% 2>&1
) else (
  echo NE NAIDEN: %FFMPEG% >> %OUT%
  echo probuyu ffmpeg iz PATH >> %OUT%
  ffmpeg -version >> %OUT% 2>&1
)
echo. >> %OUT%

echo ==== proverka ffprobe ==== >> %OUT%
if "%FFPROBE%"=="ffprobe" (
  echo naiden v PATH: ffprobe >> %OUT%
) else if exist "%FFPROBE%" (
  echo naiden: %FFPROBE% >> %OUT%
) else (
  echo NE NAIDEN: %FFPROBE% >> %OUT%
)
echo. >> %OUT%

echo ==== kompilyaciya skripta ==== >> %OUT%
"%PY%" -m py_compile meet_recorder.py >> %OUT% 2>&1
if errorlevel 1 (echo SKRIPT NE KOMPILIRUETSYA >> %OUT%) else (echo skript kompiliruetsya OK >> %OUT%)
echo. >> %OUT%

echo ==== spisok audioustroystv ==== >> %OUT%
"%PY%" meet_recorder.py --list-devices >> %OUT% 2>&1

type %OUT%
echo.
echo ------------------------------------------------------------
echo Log sohranen: %CD%\%OUT%
echo Sleduyushchiy shag: zapustite check.bat
echo ------------------------------------------------------------
pause
