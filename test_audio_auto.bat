@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
set "FF=%~dp0ffmpeg\bin\ffmpeg.exe"
for /f "delims=" %%F in ('where ffmpeg 2^>nul') do if not defined FF_PATH set "FF_PATH=%%F"
if defined FF_PATH set "FF=%FF_PATH%"
set "TONE=%TEMP%\claudemeet_tone.wav"
set "OUT=%~dp0test_audio_log.txt"

echo ==== avtoproverka zvuka ==== > "%OUT%"
echo data: %DATE% %TIME% >> "%OUT%"

REM 1. Генерируем 20-секундный тон 440 Гц — он пойдёт на колонки и должен попасть в loopback.
if exist "%FF%" (
  "%FF%" -y -f lavfi -i "sine=frequency=440:duration=20" -ac 2 -ar 44100 "%TONE%" >nul 2>&1
  if exist "%TONE%" ( echo ton sgenerirovan: %TONE% >> "%OUT%" ) else ( echo FAILED: ton ne sgenerirovan >> "%OUT%" )
) else (
  echo FAILED: net ffmpeg po puti %FF% >> "%OUT%"
)

REM 2. Запускаем воспроизведение тона в фоне через SoundPlayer — колонки заиграют.
if exist "%TONE%" (
  start "" /b powershell -NoProfile -Command "$p=New-Object Media.SoundPlayer '%TONE%'; $p.Load(); $p.PlaySync()"
  echo vosproizvedenie zapushcheno >> "%OUT%"
)

REM 3. Даём звуку раскрутиться и запускаем проверку записи.
timeout /t 2 /nobreak >nul
echo. >> "%OUT%"
echo ---- vyvod --test-audio ---- >> "%OUT%"
"%PY%" "%~dp0meet_recorder.py" --test-audio >> "%OUT%" 2>&1

echo. >> "%OUT%"
echo gotovo >> "%OUT%"
type "%OUT%"
