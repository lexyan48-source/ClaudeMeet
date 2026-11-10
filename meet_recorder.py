#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meet_recorder.py - локальный рекордер созвонов.

Запускается НА МАШИНЕ ПОЛЬЗОВАТЕЛЯ (Windows). Ловит не конкретную платформу,
а сам факт использования микрофона любым приложением: поэтому пишет и Телемост,
и Zoom, и Google Meet, и МТС Линк, и звонок в Telegram.

Как понимает, что созвон идёт:
    реестр Windows, ветка CapabilityAccessManager\\ConsentStore\\microphone.
    У каждого приложения там лежат LastUsedTimeStart и LastUsedTimeStop (FILETIME).
    LastUsedTimeStop == 0 при ненулевом Start = приложение держит микрофон ПРЯМО СЕЙЧАС.

Что пишет:
    recordings\\ГГГГ-ММ-ДД_ЧЧММ[_Тема]\\
        mic.wav      - микрофон (16000 Гц, моно, PCM 16 бит)
        system.wav   - системный звук через WASAPI loopback (то же самое)
        audio.wav    - сведённые обе дорожки
        screen.mp4   - экран, 2 кадра в секунду, дешёвое видео
        meta.json    - когда, сколько, кто держал микрофон, заголовки окон, устройства
        _recorder.log

Режимы:
    python meet_recorder.py                - демон: следить и писать
    python meet_recorder.py --test-audio   - 10 секунд, показать уровни обеих дорожек
    python meet_recorder.py --test-detect  - 30 секунд печатать, кто держит микрофон
    python meet_recorder.py --record-now   - ручной старт (через файл-флаг)
    python meet_recorder.py --stop         - ручной стоп
    python meet_recorder.py --list-devices - список аудиоустройств

Требует: PyAudioWPatch (pip install PyAudioWPatch), numpy, ffmpeg в системе.
Python 3.13: модуль audioop удалён из стандартной библиотеки, поэтому весь
ресемплинг и сведение сделаны на numpy.
"""

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import wave

# Видимые сигналы: всплывашка и файл-маркер. Отдельным модулем, чтобы отказ
# уведомлений никогда не касался самой записи.
try:
    import notify
except Exception:  # noqa: BLE001 - модуля может не быть, запись важнее
    notify = None

SCRIPT_VERSION = "1.4.0"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "meet_config.json")

MSK = dt.timezone(dt.timedelta(hours=3))

# --------------------------------------------------------------------------
# Импорты, которые есть только на Windows / только после pip install.
# Обёрнуты, чтобы файл компилировался где угодно, а падал с внятным текстом
# только при реальном запуске.
# --------------------------------------------------------------------------

WINREG_IMPORT_ERROR = None
try:
    import winreg
except Exception as _exc:  # pragma: no cover - на не-Windows
    WINREG_IMPORT_ERROR = _exc
    winreg = None

NUMPY_IMPORT_ERROR = None
try:
    import numpy as np
except Exception as _exc:  # pragma: no cover
    NUMPY_IMPORT_ERROR = _exc
    np = None

PYAUDIO_IMPORT_ERROR = None
try:
    import pyaudiowpatch as pyaudio
except Exception as _exc:  # pragma: no cover
    PYAUDIO_IMPORT_ERROR = _exc
    pyaudio = None


DEFAULT_CONFIG = {
    # --- пути ---
    # По умолчанию всё лежит рядом со скриптом. ffmpeg ищется в PATH, а если
    # его там нет - в подпапке ffmpeg\bin рядом со скриптом.
    "base_dir": SCRIPT_DIR,
    "recordings_dir": os.path.join(SCRIPT_DIR, "recordings"),
    "logs_dir": os.path.join(SCRIPT_DIR, "logs"),
    "ffmpeg_path": shutil.which("ffmpeg") or os.path.join(SCRIPT_DIR, "ffmpeg", "bin", "ffmpeg.exe"),
    "ffprobe_path": shutil.which("ffprobe") or os.path.join(SCRIPT_DIR, "ffmpeg", "bin", "ffprobe.exe"),

    # --- детект созвона ---
    "poll_seconds": 10,
    "min_hold_seconds": 30,
    "stop_grace_seconds": 90,
    "max_meeting_minutes": 180,
    "min_meeting_seconds": 120,
    "watch_processes": [
        "Telemost", "Zoom", "Teams", "chrome", "msedge",
        "Telegram", "ktalk", "discord", "webinar", "TrueConf", "Skype"
    ],
    "ignore_processes": ["obs64", "obs32", "Camera", "VoiceRecorder", "SoundRecorder"],
    "use_tasklist_fallback": True,

    # --- автоматическая расшифровка ---
    # Наблюдатель сам расшифровывает закрытые записи фоновой очередью:
    # по одной, только когда запись не идёт, от старых к новым. Накопленное
    # после перезагрузки разгребается само.
    # --- уведомления ---
    # Три независимых канала. toast - родное уведомление Windows, balloon -
    # старый NotifyIcon как резерв, telegram - в ваш бот, единственный канал,
    # видимый с телефона. Telegram необязателен: пока не заданы токен и чат
    # (прямо здесь или через отдельный JSON по tg_config_path), канал молча
    # пропускается.
    "notify_toast": True,
    "notify_balloon": True,
    "notify_telegram": True,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tg_config_path": "",

    "auto_transcribe": True,
    # small - кешированная и быстрая. medium точнее на суммах, артикулах и
    # именах, но качается ~1.5 ГБ и идёт втрое дольше.
    "transcribe_model": "small",
    "transcribe_scan_seconds": 120,
    "transcribe_start_delay_seconds": 90,

    # --- звук ---
    "record_audio": True,
    "target_sample_rate": 16000,
    "loopback_device_name": "",
    "mic_device_name": "",
    "silence_rms_threshold": 12,
    "probe_seconds": 3,
    "loopback_recheck_seconds": [60, 150],
    "keep_separate_tracks": True,
    # --- очистка старых записей ---
    # audio.wav и screen.mp4 удаляются через N дней, но ТОЛЬКО у записей,
    # у которых уже есть расшифровка (transcribe_info.json + непустой
    # transcript_raw.txt или transcript.md). 0 = не удалять этот файл.
    "retention_enabled": True,
    "retention_audio_days": 14,
    "retention_screen_days": 30,
    "mix_mic_gain": 1.0,
    "mix_system_gain": 1.0,
    "audio_chunk_frames": 2048,

    # --- экран ---
    "record_screen": True,
    "screen_framerate": 2,
    "screen_scale_width": 1600,
    "screen_crf": 32,
    "screen_preset": "veryfast",
    "screen_offset_x": 0,
    "screen_offset_y": 0,
    "screen_width": 0,
    "screen_height": 0,

    # --- прочее ---
    "title_poll_seconds": 30,
    "topic_from_window": True,
    "min_free_gb": 2.0,
    "max_log_mb": 5,
}


# ==========================================================================
# Логирование: метки времени, ротация по размеру, дубль в лог сессии.
# ==========================================================================

_LOG_FILE = None
_SESSION_LOG_FILE = None
_MAX_LOG_BYTES = 5 * 1024 * 1024
_LOG_LOCK = threading.Lock()


def set_log_file(path, max_mb=5):
    global _LOG_FILE, _MAX_LOG_BYTES
    _LOG_FILE = path
    _MAX_LOG_BYTES = int(max_mb * 1024 * 1024)


def set_session_log_file(path):
    global _SESSION_LOG_FILE
    _SESSION_LOG_FILE = path


def _rotate(path):
    """Ротация по размеру: файл переезжает в .1, новый начинается с нуля."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > _MAX_LOG_BYTES:
            old = path + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.replace(path, old)
    except OSError as exc:
        # Ротация не критична: не смогли - продолжаем писать в тот же файл.
        sys.stderr.write("Не удалось повернуть лог %s: %r\n" % (path, exc))


def _write_line(path, line):
    try:
        _rotate(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        sys.stderr.write("Не удалось записать в лог %s: %r\n" % (path, exc))


_CONSOLE_BROKEN = False


def _console_broken():
    """Отмечаем, что stdout недоступен. Один раз пишем об этом в файл лога."""
    global _CONSOLE_BROKEN
    if _CONSOLE_BROKEN:
        return
    _CONSOLE_BROKEN = True
    if _LOG_FILE:
        _write_line(_LOG_FILE, "%s [WARN] Консоль недоступна (нет stdout), "
                               "весь вывод идёт только в файл лога"
                    % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def log(msg, level="INFO"):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "%s [%s] %s" % (stamp, level, msg)
    with _LOG_LOCK:
        try:
            print(line)
        except UnicodeEncodeError:
            # Кривая кодировка консоли под Планировщиком - не повод молчать.
            sys.stdout.write(line.encode("utf-8", "replace").decode("ascii", "replace") + "\n")
        except OSError:
            # Консоли нет вообще (запуск из Планировщика без окна) - не беда,
            # в файл строка всё равно уйдёт ниже. Специально ничего не печатаем,
            # иначе получится бесконечная рекурсия внутри логгера.
            _console_broken()
        if _LOG_FILE:
            _write_line(_LOG_FILE, line)
        if _SESSION_LOG_FILE:
            _write_line(_SESSION_LOG_FILE, line)


def log_exc(msg, exc):
    """Любое исключение попадает в лог с трейсбеком и человеческим пояснением."""
    log("%s: %r" % (msg, exc), "ERROR")
    log(traceback.format_exc(), "ERROR")


# ==========================================================================
# Конфиг
# ==========================================================================

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
                user_cfg = json.load(fh)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "Не читается meet_config.json (%s): %r\n"
                "Скорее всего сломан JSON - лишняя запятая или кавычки. "
                "Проверьте файл или удалите его, тогда возьмутся значения по умолчанию."
                % (CONFIG_PATH, exc)
            )
        if not isinstance(user_cfg, dict):
            raise RuntimeError("meet_config.json должен содержать JSON-объект {...}")
        cfg.update(user_cfg)

    # Пустой путь в конфиге = значение по умолчанию (рядом со скриптом / из PATH).
    for key in ("base_dir", "recordings_dir", "logs_dir", "ffmpeg_path", "ffprobe_path"):
        if not cfg.get(key):
            cfg[key] = DEFAULT_CONFIG[key]

    # Числовые поля приводим аккуратно, с падением обратно на дефолт.
    for key in ("poll_seconds", "min_hold_seconds", "stop_grace_seconds",
                "max_meeting_minutes", "min_meeting_seconds", "title_poll_seconds",
                "screen_framerate", "screen_scale_width", "screen_crf",
                "screen_offset_x", "screen_offset_y", "screen_width", "screen_height",
                "target_sample_rate", "silence_rms_threshold", "probe_seconds",
                "audio_chunk_frames", "max_log_mb",
                "transcribe_scan_seconds", "transcribe_start_delay_seconds",
                "retention_audio_days", "retention_screen_days"):
        try:
            cfg[key] = int(cfg.get(key, DEFAULT_CONFIG[key]))
        except (TypeError, ValueError):
            log("Параметр %s в конфиге не число, беру значение по умолчанию %s"
                % (key, DEFAULT_CONFIG[key]), "WARN")
            cfg[key] = DEFAULT_CONFIG[key]

    for key in ("min_free_gb", "mix_mic_gain", "mix_system_gain"):
        try:
            cfg[key] = float(cfg.get(key, DEFAULT_CONFIG[key]))
        except (TypeError, ValueError):
            log("Параметр %s в конфиге не число, беру %s" % (key, DEFAULT_CONFIG[key]), "WARN")
            cfg[key] = DEFAULT_CONFIG[key]

    for key in ("record_audio", "record_screen", "keep_separate_tracks",
                "use_tasklist_fallback", "topic_from_window", "auto_transcribe",
                "notify_toast", "notify_balloon", "notify_telegram",
                "retention_enabled"):
        cfg[key] = bool(cfg.get(key, DEFAULT_CONFIG[key]))

    model = str(cfg.get("transcribe_model", DEFAULT_CONFIG["transcribe_model"]))
    if model not in ("tiny", "base", "small", "medium", "large-v2", "large-v3"):
        log("transcribe_model=%r неизвестна, беру small" % model, "WARN")
        model = "small"
    cfg["transcribe_model"] = model

    cfg["watch_processes"] = [str(x) for x in (cfg.get("watch_processes") or [])]
    cfg["ignore_processes"] = [str(x) for x in (cfg.get("ignore_processes") or [])]
    checks = cfg.get("loopback_recheck_seconds") or []
    out = []
    for v in checks:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            log("loopback_recheck_seconds: элемент %r не число, пропускаю" % (v,), "WARN")
    cfg["loopback_recheck_seconds"] = sorted(out)

    if cfg["poll_seconds"] < 2:
        cfg["poll_seconds"] = 2
    if cfg["target_sample_rate"] < 8000:
        cfg["target_sample_rate"] = 16000
    return cfg


def ensure_dirs(cfg):
    for key in ("base_dir", "recordings_dir", "logs_dir"):
        path = cfg.get(key)
        if path:
            os.makedirs(path, exist_ok=True)
    os.makedirs(state_dir(cfg), exist_ok=True)


def state_dir(cfg):
    return os.path.join(cfg["base_dir"], "state")


def manual_flag_path(cfg):
    return os.path.join(state_dir(cfg), "record_now.flag")


def stop_flag_path(cfg):
    return os.path.join(state_dir(cfg), "stop_now.flag")


# ==========================================================================
# Свободное место
# ==========================================================================

def free_gb(path):
    """Свободно гигабайт на диске, где лежит path. -1 если не смогли узнать."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe or ".")
        return usage.free / (1024.0 ** 3)
    except OSError as exc:
        log("Не смог посмотреть свободное место на %s: %r" % (probe, exc), "WARN")
        return -1.0


# ==========================================================================
# Детект: кто держит микрофон (реестр Windows)
# ==========================================================================

CONSENT_BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def filetime_to_dt(value):
    """FILETIME (100-нс интервалы с 1601-01-01 UTC) -> локальный datetime. None если 0."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        base = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
        return (base + dt.timedelta(microseconds=value / 10.0)).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def _decode_nonpackaged_name(key_name):
    """В NonPackaged имя подключа - путь к exe с '#' вместо обратного слэша."""
    path = key_name.replace("#", "\\")
    base = os.path.basename(path)
    return path, base


def _read_consent_key(root, subpath, packaged):
    """Читает один список подключей ConsentStore. Возвращает список записей."""
    entries = []
    try:
        key = winreg.OpenKey(root, subpath, 0, winreg.KEY_READ)
    except FileNotFoundError:
        # Ветки может просто не быть - это нормально, не ошибка.
        return entries
    except PermissionError as exc:
        log("Нет прав на ветку реестра %s: %r (обычно это HKLM, не страшно)" % (subpath, exc), "WARN")
        return entries
    except OSError as exc:
        log("Не открылась ветка реестра %s: %r" % (subpath, exc), "WARN")
        return entries

    try:
        idx = 0
        while True:
            try:
                name = winreg.EnumKey(key, idx)
            except OSError:
                break  # подключи кончились
            idx += 1
            if name.lower() == "nonpackaged":
                continue
            try:
                sub = winreg.OpenKey(key, name, 0, winreg.KEY_READ)
            except OSError as exc:
                log("Подключ %s\\%s не открылся: %r" % (subpath, name, exc), "WARN")
                continue
            try:
                start = stop = 0
                try:
                    start = winreg.QueryValueEx(sub, "LastUsedTimeStart")[0]
                except FileNotFoundError:
                    start = 0
                except OSError as exc:
                    log("LastUsedTimeStart не прочитан у %s: %r" % (name, exc), "WARN")
                try:
                    stop = winreg.QueryValueEx(sub, "LastUsedTimeStop")[0]
                except FileNotFoundError:
                    stop = 0
                except OSError as exc:
                    log("LastUsedTimeStop не прочитан у %s: %r" % (name, exc), "WARN")

                if packaged:
                    full, base = name, name
                else:
                    full, base = _decode_nonpackaged_name(name)

                entries.append({
                    "key": name,
                    "path": full,
                    "app": base,
                    "start": int(start or 0),
                    "stop": int(stop or 0),
                    "active": bool(start) and not int(stop or 0),
                    "hive": subpath,
                })
            finally:
                sub.Close()
    finally:
        key.Close()
    return entries


def read_mic_consent():
    """Все записи ConsentStore\\microphone из HKCU и HKLM, packaged и NonPackaged."""
    if winreg is None:
        raise RuntimeError(
            "Модуль winreg недоступен (%r). Детект по реестру работает только на Windows."
            % (WINREG_IMPORT_ERROR,)
        )
    entries = []
    for root, root_name in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                            (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for subpath, packaged in ((CONSENT_BASE, True),
                                  (CONSENT_BASE + r"\NonPackaged", False)):
            try:
                part = _read_consent_key(root, subpath, packaged)
            except OSError as exc:
                log("Сбой чтения %s\\%s: %r" % (root_name, subpath, exc), "WARN")
                part = []
            for e in part:
                e["hive"] = root_name
            entries.extend(part)
    return entries


def name_matches(name, patterns):
    """'*' = любой. Иначе - подстрока без учёта регистра."""
    if not patterns:
        return False
    low = (name or "").lower()
    stem = low[:-4] if low.endswith(".exe") else low
    for pat in patterns:
        p = str(pat).strip().lower()
        if not p:
            continue
        if p == "*":
            return True
        if p in low or p in stem:
            return True
    return False


def active_mic_apps(cfg):
    """Список приложений, которые держат микрофон прямо сейчас и проходят фильтры."""
    result = []
    for e in read_mic_consent():
        if not e["active"]:
            continue
        app = e["app"]
        if name_matches(app, cfg["ignore_processes"]):
            continue
        if not name_matches(app, cfg["watch_processes"]):
            continue
        result.append(e)
    return result


# --- Резервный детект через tasklist -------------------------------------

def _decode_console(raw):
    """Вывод tasklist на русской Windows - обычно cp866. Пробуем по порядку."""
    for enc in ("cp866", "cp1251", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# Предохранитель: на некоторых машинах `tasklist /v` стабильно висит и падает по
# таймауту (проверено 05.08.2026 - 10 подряд таймаутов за одну запись). Каждая
# такая попытка блокировала поток на 30 секунд впустую. После нескольких отказов
# перестаём звать его вовсе: заголовки окон - украшение, а детект живёт на
# реестре и без tasklist.
_TASKLIST_FAILS = {"verbose": 0, "plain": 0}
_TASKLIST_GIVE_UP_AFTER = 3


def tasklist_dead(verbose=False):
    key = "verbose" if verbose else "plain"
    return _TASKLIST_FAILS[key] >= _TASKLIST_GIVE_UP_AFTER


def run_tasklist(verbose=False):
    """Возвращает список строк tasklist в виде списков полей CSV."""
    key = "verbose" if verbose else "plain"
    if tasklist_dead(verbose):
        return []
    cmd = ["tasklist", "/fo", "csv", "/nh"]
    if verbose:
        cmd = ["tasklist", "/v", "/fo", "csv", "/nh"]
    # 8 секунд вместо 30: живой tasklist отвечает за десятые доли секунды,
    # а мёртвый не ответит и за минуту - ждать его нечего.
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=8,
                              creationflags=_no_window_flag())
    except FileNotFoundError as exc:
        _TASKLIST_FAILS[key] = _TASKLIST_GIVE_UP_AFTER
        log("tasklist не найден в PATH: %r. Резервный детект и сбор заголовков окон "
            "работать не будут." % (exc,), "WARN")
        return []
    except subprocess.TimeoutExpired:
        _TASKLIST_FAILS[key] += 1
        if tasklist_dead(verbose):
            log("tasklist ('%s') не ответил %d раз подряд - больше не зову его. "
                "Заголовков окон в meta.json не будет и темы в имени папки тоже, "
                "на запись это не влияет. Детект работает на реестре."
                % (" ".join(cmd), _TASKLIST_FAILS[key]), "WARN")
        else:
            log("tasklist не ответил за 8 секунд (попытка %d из %d)"
                % (_TASKLIST_FAILS[key], _TASKLIST_GIVE_UP_AFTER), "WARN")
        return []
    except OSError as exc:
        _TASKLIST_FAILS[key] += 1
        log("Не смог запустить tasklist: %r" % (exc,), "WARN")
        return []

    _TASKLIST_FAILS[key] = 0
    text = _decode_console(proc.stdout or b"")
    rows = []
    try:
        for row in csv.reader(io.StringIO(text)):
            if row:
                rows.append(row)
    except csv.Error as exc:
        log("Не разобрал CSV от tasklist: %r" % (exc,), "WARN")
    return rows


def _no_window_flag():
    """Чтобы служебные вызовы не мигали чёрными окнами консоли."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def active_apps_by_tasklist(cfg):
    """Резерв: просто ищем процессы из белого списка. Хуже реестра - Chrome
    запущен всегда, поэтому используется только когда реестровая ветка пуста."""
    found = []
    for row in run_tasklist(verbose=False):
        name = row[0] if row else ""
        if name_matches(name, cfg["ignore_processes"]):
            continue
        if name_matches(name, cfg["watch_processes"]):
            found.append({"key": name, "path": name, "app": name,
                          "start": 0, "stop": 0, "active": True, "hive": "tasklist"})
    return found


def collect_window_titles(cfg):
    """Заголовки окон процессов из белого списка. Заголовок окна Телемоста
    часто содержит название встречи - из него потом берётся тема."""
    titles = []
    for row in run_tasklist(verbose=True):
        if len(row) < 9:
            continue
        image = row[0]
        title = (row[8] or "").strip()
        if not title or title in ("N/A", "Н/Д"):
            continue
        if name_matches(image, cfg["ignore_processes"]):
            continue
        if not name_matches(image, cfg["watch_processes"]):
            continue
        titles.append({"process": image, "title": title})
    return titles


# ==========================================================================
# Транслитерация для имени папки (в .bat кириллица ломается)
# ==========================================================================

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(text):
    out = []
    for ch in text or "":
        low = ch.lower()
        if low in _TRANSLIT:
            rep = _TRANSLIT[low]
            out.append(rep.capitalize() if (ch.isupper() and rep) else rep)
        else:
            out.append(ch)
    return "".join(out)


# Куски заголовков, которые темой быть не могут: это названия самих программ.
_APP_NOISE = (
    "google chrome", "chrome", "microsoft edge", "edge", "mozilla firefox", "firefox",
    "яндекс браузер", "яндекс телемост", "телемост", "telemost", "yandex",
    "zoom", "zoom meeting", "zoom workplace", "microsoft teams", "teams",
    "telegram", "telegram desktop", "discord", "мтс линк", "mts link", "ktalk",
    "контур.толк", "skype", "trueconf", "webinar", "вебинар",
)


def sanitize_topic(title, limit=40):
    """Заголовок окна -> кусок имени папки: транслит, без спецсимволов.

    Из 'Планёрка отдела — Яндекс Телемост' нужна 'Планёрка отдела', а не название
    программы, поэтому режем заголовок по разделителям и выкидываем куски,
    которые совпали с названиями приложений.
    """
    if not title:
        return ""
    parts = [p.strip() for p in re.split(r"\s+[-—–|]\s+|\s+[|]\s+", title) if p.strip()]
    if not parts:
        parts = [title.strip()]
    useful = [p for p in parts if p.lower() not in _APP_NOISE
              and not any(p.lower() == n for n in _APP_NOISE)]
    if not useful:
        useful = parts
    # Берём самый содержательный кусок.
    useful.sort(key=lambda p: -len(p))
    text = translit(useful[0])
    text = re.sub(r"[^A-Za-z0-9 _.-]+", " ", text)
    text = re.sub(r"\s+", "_", text.strip())
    text = text.strip("._-")
    return text[:limit]


# ==========================================================================
# Аудио: устройства
# ==========================================================================

def require_audio_libs():
    if np is None:
        raise RuntimeError(
            "numpy не импортируется (%r). Без него нет ресемплинга и сведения. "
            "Установите: python -m pip install numpy" % (NUMPY_IMPORT_ERROR,)
        )
    if pyaudio is None:
        raise RuntimeError(
            "PyAudioWPatch не импортируется (%r). Установите: "
            "python -m pip install PyAudioWPatch" % (PYAUDIO_IMPORT_ERROR,)
        )


def open_pyaudio():
    require_audio_libs()
    return pyaudio.PyAudio()


def all_devices(pa):
    """Список device_info по всем устройствам, с пометкой loopback."""
    devices = []
    try:
        count = pa.get_device_count()
    except Exception as exc:
        log_exc("Не смог получить число аудиоустройств", exc)
        return devices
    for i in range(count):
        try:
            info = dict(pa.get_device_info_by_index(i))
        except Exception as exc:
            log("Устройство №%d не читается: %r - пропускаю" % (i, exc), "WARN")
            continue
        info["index"] = i
        info["is_loopback"] = bool(info.get("isLoopbackDevice"))
        devices.append(info)
    return devices


def loopback_devices(pa):
    return [d for d in all_devices(pa) if d["is_loopback"] and d.get("maxInputChannels", 0) > 0]


def default_loopback(pa):
    """Loopback дефолтного устройства вывода. Именно оно чаще всего оказывается
    НЕ тем, куда конференция шлёт звук (Windows роутит созвоны в eCommunications)."""
    try:
        info = pa.get_default_wasapi_loopback()
        if info:
            info = dict(info)
            info["index"] = info.get("index", info.get("deviceIndex"))
            info["is_loopback"] = True
            return info
    except Exception as exc:
        log("get_default_wasapi_loopback не сработал (%r), ищу вручную по имени "
            "устройства вывода по умолчанию" % (exc,), "WARN")

    # Ручной путь: имя дефолтного вывода WASAPI -> loopback с таким же именем.
    try:
        host = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        out_idx = host.get("defaultOutputDevice")
        out_info = pa.get_device_info_by_index(out_idx)
        target = str(out_info.get("name", "")).lower()
    except Exception as exc:
        log("Не определил устройство вывода по умолчанию: %r" % (exc,), "WARN")
        target = ""
    lbs = loopback_devices(pa)
    if target:
        for d in lbs:
            if str(d.get("name", "")).lower().startswith(target[:28]):
                return d
    return lbs[0] if lbs else None


def find_device_by_name(pa, name, want_loopback=None, want_input=True):
    """Поиск устройства по подстроке имени. Регистр не важен."""
    if not name:
        return None
    low = name.lower()
    for d in all_devices(pa):
        if want_input and d.get("maxInputChannels", 0) <= 0:
            continue
        if want_loopback is True and not d["is_loopback"]:
            continue
        if want_loopback is False and d["is_loopback"]:
            continue
        if low in str(d.get("name", "")).lower():
            return d
    return None


def default_mic(pa, cfg):
    named = find_device_by_name(pa, cfg.get("mic_device_name"), want_loopback=False)
    if named:
        return named
    if cfg.get("mic_device_name"):
        log("Микрофон с именем '%s' из конфига не найден, беру устройство ввода "
            "по умолчанию" % cfg["mic_device_name"], "WARN")
    try:
        info = dict(pa.get_default_input_device_info())
        info["index"] = info.get("index", info.get("deviceIndex"))
        info["is_loopback"] = bool(info.get("isLoopbackDevice"))
        return info
    except Exception as exc:
        log("Устройство ввода по умолчанию не определилось: %r" % (exc,), "WARN")
    for d in all_devices(pa):
        if d.get("maxInputChannels", 0) > 0 and not d["is_loopback"]:
            return d
    return None


def device_open_params(info):
    """Частота и число каналов, с которыми имеет смысл открывать устройство.
    WASAPI в общем режиме принимает только родную частоту устройства."""
    rate = int(info.get("defaultSampleRate") or 48000)
    ch = int(info.get("maxInputChannels") or 1)
    if ch < 1:
        ch = 1
    if ch > 2 and not info.get("is_loopback"):
        ch = 2  # у микрофонов больше двух каналов брать незачем
    return rate, ch


def open_input_stream(pa, info, chunk):
    """Открывает поток на чтение. Пробует родные параметры, потом типовые."""
    rate, ch = device_open_params(info)
    attempts = [(rate, ch)]
    for r in (48000, 44100, 16000):
        for c in (ch, 2, 1):
            if (r, c) not in attempts:
                attempts.append((r, c))
    last_exc = None
    for r, c in attempts:
        try:
            stream = pa.open(format=pyaudio.paInt16, channels=c, rate=r, input=True,
                             input_device_index=int(info["index"]),
                             frames_per_buffer=chunk)
            return stream, r, c
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(
        "Не удалось открыть устройство '%s' (index=%s) ни с одной комбинацией "
        "частоты и каналов. Последняя ошибка: %r" % (info.get("name"), info.get("index"), last_exc)
    )


# ==========================================================================
# Аудио: ресемплинг и WAV
# ==========================================================================

class LinearResampler(object):
    """Линейная интерполяция на numpy с сохранением фазы между чанками.

    Почему это важно: частоты микрофона и loopback обычно разные (48000 и 44100).
    Если сначала свести, а потом ресемплировать - получится ускоренная речь.
    Поэтому ресемплим КАЖДУЮ дорожку до 16000 Гц ДО сведения.
    Без переноса фазы между чанками длина дорожки уплывала бы на секунды за час.
    """

    def __init__(self, src_rate, dst_rate):
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._tail = None   # хвост предыдущего чанка
        self._pos = 0.0     # дробная позиция чтения внутри буфера

    def process(self, x):
        if self.src_rate == self.dst_rate:
            return x
        ratio = float(self.src_rate) / float(self.dst_rate)
        if self._tail is not None and len(self._tail):
            buf = np.concatenate((self._tail, x))
        else:
            buf = x
        n = len(buf)
        if n < 2:
            self._tail = buf
            return np.zeros(0, dtype=np.float32)
        count = int(np.floor((n - 1 - self._pos) / ratio)) + 1
        if count <= 0:
            self._tail = buf
            return np.zeros(0, dtype=np.float32)
        idx = self._pos + ratio * np.arange(count, dtype=np.float64)
        out = np.interp(idx, np.arange(n, dtype=np.float64), buf.astype(np.float64))
        last = self._pos + ratio * count
        keep = int(np.floor(last))
        if keep > n:
            keep = n
        self._tail = buf[keep:]
        self._pos = last - keep
        return out.astype(np.float32)


class IncrementalWav(object):
    """WAV-писатель, который держит корректный заголовок ПРЯМО ВО ВРЕМЯ записи.

    Стандартный wave.py проставляет размеры только в close(). Если процесс упал
    или машина ушла в перезагрузку, файл выглядит пустым, хотя данные на диске
    есть. Здесь заголовок переписывается каждые несколько секунд, поэтому
    оборванная запись всё равно открывается плеером.
    """

    def __init__(self, path, rate=16000, channels=1, sampwidth=2, header_interval=5.0):
        self.path = path
        self.rate = int(rate)
        self.channels = int(channels)
        self.sampwidth = int(sampwidth)
        self.data_bytes = 0
        self.header_interval = float(header_interval)
        self._fh = open(path, "wb")
        self._write_header()
        self._last_header = time.time()
        self._closed = False

    def _write_header(self):
        byte_rate = self.rate * self.channels * self.sampwidth
        block_align = self.channels * self.sampwidth
        hdr = b"RIFF" + struct.pack("<I", 36 + self.data_bytes) + b"WAVE"
        hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, self.channels, self.rate,
                                     byte_rate, block_align, self.sampwidth * 8)
        hdr += b"data" + struct.pack("<I", self.data_bytes)
        pos = self._fh.tell()
        self._fh.seek(0)
        self._fh.write(hdr)
        if pos > 44:
            self._fh.seek(pos)

    def write(self, raw):
        if self._closed:
            return
        self._fh.write(raw)
        self.data_bytes += len(raw)
        now = time.time()
        if now - self._last_header >= self.header_interval:
            self._write_header()
            self._fh.flush()
            self._last_header = now

    def seconds(self):
        denom = self.rate * self.channels * self.sampwidth
        return (self.data_bytes / float(denom)) if denom else 0.0

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._write_header()
            self._fh.flush()
        except OSError as exc:
            log("Не смог дописать заголовок %s: %r" % (self.path, exc), "ERROR")
        finally:
            try:
                self._fh.close()
            except OSError as exc:
                log("Не смог закрыть %s: %r" % (self.path, exc), "ERROR")


def rms_int16(arr):
    """RMS для массива int16/float. 0 = абсолютная тишина."""
    if arr is None or len(arr) == 0:
        return 0.0
    a = arr.astype(np.float64)
    return float(np.sqrt(np.mean(a * a)))


def to_mono_float(raw, channels):
    """bytes int16 -> моно float32 (в шкале int16)."""
    data = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        usable = (len(data) // channels) * channels
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32)


# ==========================================================================
# Аудио: поток записи одной дорожки
# ==========================================================================

class TrackRecorder(threading.Thread):
    """Пишет одну дорожку (микрофон или loopback) в WAV инкрементально.

    Всё сводится к 16000 Гц моно PCM 16 бит - ровно тот формат, который
    ест faster-whisper.
    """

    def __init__(self, pa, label, candidates, out_path, cfg, recheck_at=()):
        threading.Thread.__init__(self, name="track-" + label, daemon=True)
        self.pa = pa
        self.label = label
        self.candidates = list(candidates)
        self.out_path = out_path
        self.cfg = cfg
        self.recheck_at = list(recheck_at)
        self.dst_rate = int(cfg["target_sample_rate"])
        self.chunk = int(cfg["audio_chunk_frames"])

        self._stop = threading.Event()
        self._stream = None
        self._wav = None
        self._resampler = None
        self._src_rate = 0
        self._src_channels = 0
        self._cand_idx = -1

        self.device_info = None
        self.device_history = []
        self.sum_sq = 0.0
        self.sample_count = 0
        self.peak = 0
        self.started_ok = False
        self.error = None
        self._t0 = None

    # --- служебное ---

    def rms(self):
        if not self.sample_count:
            return 0.0
        return float((self.sum_sq / self.sample_count) ** 0.5)

    def seconds_written(self):
        return self._wav.seconds() if self._wav else 0.0

    def stop(self):
        self._stop.set()

    def _open_candidate(self, idx):
        """Открывает поток на кандидате idx. True если получилось."""
        info = self.candidates[idx]
        try:
            stream, rate, ch = open_input_stream(self.pa, info, self.chunk)
        except Exception as exc:
            log("[%s] устройство '%s' не открылось: %r" % (self.label, info.get("name"), exc), "WARN")
            return False
        self._close_stream()
        self._stream = stream
        self._src_rate = rate
        self._src_channels = ch
        self._resampler = LinearResampler(rate, self.dst_rate)
        self._cand_idx = idx
        self.device_info = info
        self.device_history.append({
            "name": str(info.get("name", "")),
            "index": int(info.get("index", -1)),
            "rate": rate,
            "channels": ch,
            "is_loopback": bool(info.get("is_loopback")),
            "at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        log("[%s] пишу с устройства '%s' (index=%s, %d Гц, %d кан.) -> %d Гц моно"
            % (self.label, info.get("name"), info.get("index"), rate, ch, self.dst_rate))
        return True

    def _close_stream(self):
        if self._stream is None:
            return
        try:
            self._stream.stop_stream()
        except Exception as exc:
            log("[%s] stop_stream: %r (не критично)" % (self.label, exc), "WARN")
        try:
            self._stream.close()
        except Exception as exc:
            log("[%s] close stream: %r (не критично)" % (self.label, exc), "WARN")
        self._stream = None

    # --- основной цикл ---

    def run(self):
        if not self.candidates:
            self.error = "нет ни одного подходящего устройства"
            log("[%s] нет ни одного подходящего устройства - дорожка не пишется"
                % self.label, "ERROR")
            return
        opened = False
        for i in range(len(self.candidates)):
            if self._open_candidate(i):
                opened = True
                break
        if not opened:
            self.error = "ни одно устройство не открылось"
            log("[%s] ни одно устройство не открылось - дорожка не пишется" % self.label, "ERROR")
            return

        try:
            self._wav = IncrementalWav(self.out_path, rate=self.dst_rate, channels=1)
        except OSError as exc:
            self.error = "не создался файл %s: %r" % (self.out_path, exc)
            log_exc("[%s] не создался WAV %s" % (self.label, self.out_path), exc)
            self._close_stream()
            return

        self.started_ok = True
        self._t0 = time.time()
        pending_rechecks = list(self.recheck_at)

        try:
            while not self._stop.is_set():
                try:
                    raw = self._stream.read(self.chunk, exception_on_overflow=False)
                except Exception as exc:
                    # Устройство могло исчезнуть (выдернули наушники) - пробуем
                    # переоткрыть тот же кандидат, а если нет - следующий.
                    log("[%s] сбой чтения потока: %r - пробую переоткрыть устройство"
                        % (self.label, exc), "WARN")
                    if not self._reopen_after_failure():
                        self.error = "поток оборвался и не переоткрылся: %r" % (exc,)
                        break
                    continue

                if not raw:
                    time.sleep(0.01)
                    continue

                try:
                    mono = to_mono_float(raw, self._src_channels)
                    res = self._resampler.process(mono)
                    if len(res):
                        clipped = np.clip(res, -32768.0, 32767.0)
                        self.sum_sq += float(np.sum(clipped.astype(np.float64) ** 2))
                        self.sample_count += len(clipped)
                        p = int(np.max(np.abs(clipped))) if len(clipped) else 0
                        if p > self.peak:
                            self.peak = p
                        self._wav.write(clipped.astype(np.int16).tobytes())
                except Exception as exc:
                    log_exc("[%s] сбой обработки чанка (ресемплинг/запись)" % self.label, exc)

                # Проверка на тишину: если через N секунд дорожка ровно молчит,
                # значит Windows льёт звук созвона в другое устройство - переезжаем.
                if pending_rechecks and self._t0 is not None:
                    elapsed = time.time() - self._t0
                    if elapsed >= pending_rechecks[0]:
                        pending_rechecks.pop(0)
                        self._maybe_switch_silent()
        finally:
            self._close_stream()
            if self._wav:
                self._wav.close()
            log("[%s] дорожка закрыта: %.1f сек, RMS %.1f, пик %d"
                % (self.label, self.seconds_written(), self.rms(), self.peak))

    def _reopen_after_failure(self):
        for i in range(len(self.candidates)):
            idx = (self._cand_idx + i) % len(self.candidates)
            if self._open_candidate(idx):
                return True
        return False

    def _maybe_switch_silent(self):
        """Если дорожка молчит - перебираемся на следующее loopback-устройство."""
        cur_rms = self.rms()
        thr = float(self.cfg["silence_rms_threshold"])
        if cur_rms >= thr:
            log("[%s] проверка тишины: RMS %.1f - звук идёт, устройство менять не нужно"
                % (self.label, cur_rms))
            return
        if len(self.candidates) <= 1:
            log("[%s] ТИШИНА (RMS %.1f) и переключаться не на что: доступно одно "
                "устройство. Системный звук в этой записи, вероятно, потерян."
                % (self.label, cur_rms), "WARN")
            return
        nxt = (self._cand_idx + 1) % len(self.candidates)
        log("[%s] ТИШИНА (RMS %.1f < %.1f) - Windows, скорее всего, льёт звук созвона "
            "в другое устройство (роль eCommunications). Переключаюсь на '%s'"
            % (self.label, cur_rms, thr, self.candidates[nxt].get("name")), "WARN")
        if self._open_candidate(nxt):
            # Счётчики обнуляем, иначе следующая проверка увидит старую тишину.
            self.sum_sq = 0.0
            self.sample_count = 0
        else:
            log("[%s] переключиться не вышло, остаюсь на прежнем устройстве" % self.label, "WARN")


# ==========================================================================
# Выбор loopback-устройства с пробой уровня
# ==========================================================================

def probe_device_rms(pa, info, seconds, chunk):
    """Открывает устройство на N секунд и меряет RMS. -1 если не открылось."""
    try:
        stream, rate, ch = open_input_stream(pa, info, chunk)
    except Exception as exc:
        log("Проба '%s': не открылось (%r)" % (info.get("name"), exc), "WARN")
        return -1.0
    total_sq = 0.0
    total_n = 0
    t_end = time.time() + max(1, int(seconds))
    try:
        while time.time() < t_end:
            try:
                raw = stream.read(chunk, exception_on_overflow=False)
            except Exception as exc:
                log("Проба '%s': сбой чтения (%r)" % (info.get("name"), exc), "WARN")
                break
            mono = to_mono_float(raw, ch)
            if len(mono):
                total_sq += float(np.sum(mono.astype(np.float64) ** 2))
                total_n += len(mono)
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception as exc:
            log("Проба '%s': поток не закрылся штатно (%r)" % (info.get("name"), exc), "WARN")
    if not total_n:
        return 0.0
    return float((total_sq / total_n) ** 0.5)


def build_loopback_candidates(pa, cfg):
    """Порядок кандидатов: имя из конфига -> loopback дефолтного вывода -> остальные."""
    cands = []
    seen = set()

    def add(d, why):
        if not d:
            return
        idx = int(d.get("index", -1))
        if idx in seen:
            return
        seen.add(idx)
        cands.append(d)
        log("Кандидат на системный звук [%s]: '%s' (index=%s)" % (why, d.get("name"), idx))

    named = find_device_by_name(pa, cfg.get("loopback_device_name"), want_loopback=True)
    if cfg.get("loopback_device_name") and not named:
        log("Loopback с именем '%s' из конфига не найден - беру устройство по умолчанию"
            % cfg["loopback_device_name"], "WARN")
    add(named, "из конфига")
    add(default_loopback(pa), "loopback устройства вывода по умолчанию")
    for d in loopback_devices(pa):
        add(d, "прочие loopback")
    return cands


def pick_loopback(pa, cfg):
    """Выбор loopback с пробой RMS. Возвращает (упорядоченный список кандидатов).

    Порядок важен: первым идёт тот, на котором проба услышала звук.
    Если молчали все - оставляем исходный порядок и честно пишем это в лог,
    а TrackRecorder потом ещё раз попробует переключиться уже во время записи.
    """
    cands = build_loopback_candidates(pa, cfg)
    if not cands:
        log("LOOPBACK НЕ НАЙДЕН ВООБЩЕ. Системный звук (речь собеседников) записан "
            "НЕ БУДЕТ - останется только микрофон. Проверьте, что установлен "
            "PyAudioWPatch, а не обычный PyAudio.", "ERROR")
        return []

    thr = float(cfg["silence_rms_threshold"])
    probe_s = int(cfg["probe_seconds"])
    chunk = int(cfg["audio_chunk_frames"])
    for i, d in enumerate(cands):
        val = probe_device_rms(pa, d, probe_s, chunk)
        log("Проба %d сек на '%s': RMS %.1f (порог тишины %.1f)"
            % (probe_s, d.get("name"), val, thr))
        if val >= thr:
            log("Беру для системного звука: '%s' - на нём слышен звук" % d.get("name"))
            return [d] + [c for c in cands if c is not d]
    log("Все loopback-устройства при пробе молчали. Беру первый по порядку ('%s'). "
        "Если в момент пробы просто никто не говорил - это нормально, дорожка "
        "проверится ещё раз уже во время записи." % cands[0].get("name"), "WARN")
    return cands


# ==========================================================================
# Экран: ffmpeg gdigrab
# ==========================================================================

class ScreenRecorder(object):
    """Дешёвое видео экрана: 2 кадра в секунду, h264 crf 32. Час - десятки мегабайт.

    Останавливаем корректно: посылаем 'q' в stdin. Если убить процесс, mp4
    останется без индекса и не откроется.
    """

    def __init__(self, cfg, out_path):
        self.cfg = cfg
        self.out_path = out_path
        self.proc = None
        self.cmd = None
        self.error = None

    def build_cmd(self):
        cfg = self.cfg
        cmd = [cfg["ffmpeg_path"], "-y", "-f", "gdigrab",
               "-framerate", str(cfg["screen_framerate"])]
        # Захват конкретного монитора: смещение + размер области.
        if cfg["screen_width"] > 0 and cfg["screen_height"] > 0:
            cmd += ["-offset_x", str(cfg["screen_offset_x"]),
                    "-offset_y", str(cfg["screen_offset_y"]),
                    "-video_size", "%dx%d" % (cfg["screen_width"], cfg["screen_height"])]
        cmd += ["-i", "desktop",
                "-vf", "scale=%d:-2" % cfg["screen_scale_width"],
                "-c:v", "libx264", "-preset", str(cfg["screen_preset"]),
                "-crf", str(cfg["screen_crf"]), "-pix_fmt", "yuv420p",
                "-vsync", "vfr",
                # Фрагментированный MP4. Обычный MP4 дописывает индекс (атом moov)
                # только при штатном завершении, поэтому если ffmpeg не успел
                # закрыться по 'q' и его сняли terminate - файл нечитаем целиком
                # (`moov atom not found`), теряется вся запись экрана.
                # Проверено 05.08.2026 на записи 2026-08-05_1742.
                # С этими флагами индекс пишется по ходу, и файл открывается
                # даже если процесс убили на середине.
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                # Ключевой кадр раз в 10 секунд: без этого при 2 кадрах/с
                # фрагменты становятся минутными, и обрывается больше.
                "-g", str(max(2, int(cfg["screen_framerate"]) * 10)),
                self.out_path]
        return cmd

    def start(self):
        if not os.path.exists(self.cfg["ffmpeg_path"]):
            self.error = "ffmpeg не найден по пути %s" % self.cfg["ffmpeg_path"]
            log("Видео экрана НЕ пишется: %s. Поправьте ffmpeg_path в meet_config.json."
                % self.error, "ERROR")
            return False
        self.cmd = self.build_cmd()
        log("Запускаю запись экрана: %s" % " ".join(self.cmd))
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_no_window_flag(),
            )
        except OSError as exc:
            self.error = "ffmpeg не запустился: %r" % (exc,)
            log_exc("Видео экрана НЕ пишется, ffmpeg не запустился", exc)
            self.proc = None
            return False
        time.sleep(1.5)
        if self.proc.poll() is not None:
            err = b""
            try:
                err = self.proc.stderr.read() or b""
            except OSError as exc:
                log("Не смог прочитать stderr ffmpeg: %r" % (exc,), "WARN")
            self.error = "ffmpeg завершился сразу, код %s" % self.proc.returncode
            log("Видео экрана НЕ пишется: %s. Вывод ffmpeg:\n%s"
                % (self.error, _decode_console(err)[-2000:]), "ERROR")
            self.proc = None
            return False
        return True

    def stop(self):
        if self.proc is None:
            return
        log("Останавливаю запись экрана (посылаю 'q' в ffmpeg)")
        try:
            # Перевод строки после 'q': без него ffmpeg на Windows может держать
            # символ в буфере и не увидеть команду. 05.08.2026 запись экрана
            # погибла именно так - ffmpeg не отреагировал и был снят terminate.
            self.proc.stdin.write(b"q\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            log("Не смог послать 'q' в ffmpeg: %r - придётся завершать жёстко" % (exc,), "WARN")
        try:
            # 45 секунд, а не 20: на длинной записи ffmpeg дописывает индекс
            # и на медленном диске не успевает за 20 секунд.
            self.proc.wait(timeout=45)
            log("ffmpeg завершился штатно, код %s" % self.proc.returncode)
        except subprocess.TimeoutExpired:
            log("ffmpeg не закрылся за 45 секунд, посылаю terminate. Запись "
                "фрагментированная, поэтому кадры по таймкодам достать всё равно "
                "получится, но сплошной проход по файлу может падать.", "WARN")
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError) as exc:
                log("terminate не помог (%r), убиваю процесс. screen.mp4 почти "
                    "наверняка не откроется." % (exc,), "ERROR")
                try:
                    self.proc.kill()
                except OSError as exc2:
                    log("kill тоже не сработал: %r" % (exc2,), "ERROR")
        finally:
            for pipe in (self.proc.stdin, self.proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError as exc:
                    log("Труба ffmpeg не закрылась: %r (на файл записи не влияет)"
                        % (exc,), "WARN")
            self.proc = None


# ==========================================================================
# Сведение дорожек
# ==========================================================================

def read_wav_int16(path):
    """Читает моно WAV в numpy int16. Возвращает (данные, частота)."""
    with wave.open(path, "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sw != 2:
        raise RuntimeError("Ожидался PCM 16 бит, а в %s ширина отсчёта %d байт" % (path, sw))
    data = np.frombuffer(frames, dtype=np.int16)
    if ch > 1:
        usable = (len(data) // ch) * ch
        data = data[:usable].reshape(-1, ch).mean(axis=1).astype(np.int16)
    return data, rate


def mix_tracks(mic_path, sys_path, out_path, cfg):
    """Сводит mic.wav и system.wav в audio.wav: выравнивание длин + клиппинг."""
    parts = []
    rate = int(cfg["target_sample_rate"])
    for path, gain, label in ((mic_path, float(cfg["mix_mic_gain"]), "микрофон"),
                              (sys_path, float(cfg["mix_system_gain"]), "системный звук")):
        if not path or not os.path.exists(path) or os.path.getsize(path) <= 44:
            log("Сведение: дорожки '%s' нет или она пустая - свожу без неё" % label, "WARN")
            continue
        try:
            data, r = read_wav_int16(path)
        except (OSError, RuntimeError, wave.Error, ValueError) as exc:
            log_exc("Сведение: не прочитал %s" % path, exc)
            continue
        if r != rate:
            log("Сведение: у %s частота %d вместо %d - привожу интерполяцией"
                % (os.path.basename(path), r, rate), "WARN")
            res = LinearResampler(r, rate)
            data = res.process(data.astype(np.float32)).astype(np.int16)
        if len(data) == 0:
            log("Сведение: дорожка '%s' пустая" % label, "WARN")
            continue
        parts.append(data.astype(np.float32) * gain)
        log("Сведение: '%s' - %.1f сек, усиление %.2f" % (label, len(data) / float(rate), gain))

    if not parts:
        log("Сводить нечего: обе дорожки пустые. audio.wav не создан.", "ERROR")
        return False

    n = max(len(p) for p in parts)
    acc = np.zeros(n, dtype=np.float32)
    for p in parts:
        if len(p) < n:
            p = np.pad(p, (0, n - len(p)))  # короткую дорожку добиваем тишиной
        acc += p
    acc = np.clip(acc, -32768.0, 32767.0).astype(np.int16)

    try:
        with wave.open(out_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(acc.tobytes())
    except (OSError, wave.Error) as exc:
        log_exc("Не смог записать сведённый %s" % out_path, exc)
        return False
    log("Сведено: %s, %.1f сек, RMS %.1f" % (os.path.basename(out_path),
                                             n / float(rate), rms_int16(acc)))
    return True


# ==========================================================================
# Сессия записи
# ==========================================================================

class RecordingSession(object):
    def __init__(self, cfg, trigger_app, trigger_kind):
        self.cfg = cfg
        self.trigger_app = trigger_app or "?"
        self.trigger_kind = trigger_kind  # "registry" / "tasklist" / "manual"
        self.started_at = dt.datetime.now()
        self.started_mono = time.time()
        self.finished_at = None
        self.folder = None
        self.pa = None
        self.mic_track = None
        self.sys_track = None
        self.screen = None
        self.titles = []
        self._titles_seen = set()
        self._last_title_poll = 0.0
        self.topic = ""
        self.notes = []

    # --- запуск ---

    def _make_folder(self):
        base = self.started_at.strftime("%Y-%m-%d_%H%M")
        path = os.path.join(self.cfg["recordings_dir"], base)
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = os.path.join(self.cfg["recordings_dir"], "%s_%d" % (base, suffix))
        os.makedirs(path, exist_ok=True)
        return path

    def start(self):
        free = free_gb(self.cfg["recordings_dir"])
        if 0 <= free < float(self.cfg["min_free_gb"]):
            log("НЕ НАЧИНАЮ ЗАПИСЬ: свободно %.2f ГБ, а нужно минимум %.2f ГБ. "
                "Освободите место на диске." % (free, self.cfg["min_free_gb"]), "ERROR")
            return False
        if free < 0:
            log("Свободное место определить не удалось - начинаю запись на свой страх "
                "и риск.", "WARN")

        try:
            self.folder = self._make_folder()
        except OSError as exc:
            log_exc("Не смог создать папку записи в %s" % self.cfg["recordings_dir"], exc)
            return False

        set_session_log_file(os.path.join(self.folder, "_recorder.log"))
        log("=== СТАРТ ЗАПИСИ ===")
        log("Папка: %s" % self.folder)
        log("Триггер: %s (%s), свободно на диске %.1f ГБ"
            % (self.trigger_app, self.trigger_kind, free))

        self._poll_titles(force=True)

        if self.cfg["record_audio"]:
            self._start_audio()
        else:
            log("record_audio=false - звук не пишется (так задано в конфиге)", "WARN")

        if self.cfg["record_screen"]:
            self.screen = ScreenRecorder(self.cfg, os.path.join(self.folder, "screen.mp4"))
            if not self.screen.start():
                self.notes.append("видео экрана не пишется: %s" % self.screen.error)
                self.screen = None
        else:
            log("record_screen=false - экран не пишется (так задано в конфиге)")

        # Видимые сигналы — последним делом, когда запись уже реально идёт.
        # Ни маркер, ни всплывашка не могут отменить запись: их отказ только
        # пишется в лог.
        if notify is not None:
            notify.marker_on(self.cfg["recordings_dir"], self.folder,
                             self.started_at, self.trigger_app, log=log)
            what = []
            if self.mic_track is not None:
                what.append("микрофон")
            if self.sys_track is not None:
                what.append("звук собеседников")
            if self.screen is not None:
                what.append("экран")
            notify.signal(
                self.cfg,
                "ClaudeMeet: запись началась",
                "%s · пишу %s"
                % (self.trigger_app, ", ".join(what) if what else "ничего - смотри лог"),
                log=log)

        return True

    def _start_audio(self):
        try:
            self.pa = open_pyaudio()
        except Exception as exc:
            log_exc("Звук не пишется: не поднялся PyAudio", exc)
            self.notes.append("звук не пишется: %r" % (exc,))
            self.pa = None
            return

        # Микрофон
        mic = default_mic(self.pa, self.cfg)
        if mic is None:
            log("МИКРОФОН НЕ НАЙДЕН. Дорожка mic.wav писаться не будет - в записи "
                "не будет вашего голоса.", "ERROR")
            self.notes.append("микрофон не найден")
        else:
            self.mic_track = TrackRecorder(self.pa, "mic", [mic],
                                           os.path.join(self.folder, "mic.wav"), self.cfg)
            self.mic_track.start()

        # Системный звук (loopback) с пробой уровня
        try:
            cands = pick_loopback(self.pa, self.cfg)
        except Exception as exc:
            log_exc("Сбой выбора loopback-устройства", exc)
            cands = []
        if not cands:
            log("СИСТЕМНЫЙ ЗВУК ПОТЕРЯН: loopback-устройство не найдено. Пишу только "
                "микрофон - собеседников в записи не будет.", "ERROR")
            self.notes.append("системный звук потерян: loopback не найден")
        else:
            self.sys_track = TrackRecorder(
                self.pa, "system", cands,
                os.path.join(self.folder, "system.wav"), self.cfg,
                recheck_at=self.cfg["loopback_recheck_seconds"])
            self.sys_track.start()

    # --- во время записи ---

    def tick(self):
        now = time.time()
        if now - self._last_title_poll >= self.cfg["title_poll_seconds"]:
            self._poll_titles()

    def _poll_titles(self, force=False):
        self._last_title_poll = time.time()
        try:
            titles = collect_window_titles(self.cfg)
        except Exception as exc:
            log_exc("Не смог собрать заголовки окон через tasklist", exc)
            return
        for t in titles:
            key = (t["process"], t["title"])
            if key in self._titles_seen:
                continue
            self._titles_seen.add(key)
            rec = dict(t)
            rec["at"] = dt.datetime.now().isoformat(timespec="seconds")
            self.titles.append(rec)
            log("Заголовок окна: [%s] %s" % (t["process"], t["title"]))

    def duration(self):
        return time.time() - self.started_mono

    # --- завершение ---

    def _guess_topic(self):
        """Тема из заголовка окна процесса, который держал микрофон."""
        if not self.cfg["topic_from_window"]:
            return ""
        trigger_stem = os.path.splitext(os.path.basename(self.trigger_app))[0].lower()
        generic = ("новая вкладка", "new tab", "chrome", "microsoft edge",
                   "telegram", "settings", "параметры")
        ranked = []
        for t in self.titles:
            proc = os.path.splitext(t["process"])[0].lower()
            score = 2 if (trigger_stem and trigger_stem in proc) else 0
            title_low = t["title"].strip().lower()
            if title_low in generic:
                score -= 3
            if len(t["title"].strip()) < 4:
                score -= 2
            ranked.append((score, t["title"]))
        ranked.sort(key=lambda x: -x[0])
        for score, title in ranked:
            if score <= 0:
                continue
            topic = sanitize_topic(title)
            if topic:
                return topic
        for score, title in ranked:
            topic = sanitize_topic(title)
            if topic:
                return topic
        return ""

    def finish(self, reason):
        log("Останавливаю запись, причина: %s" % reason)
        self.finished_at = dt.datetime.now()
        duration = self.duration()

        for track in (self.mic_track, self.sys_track):
            if track is not None:
                track.stop()
        for track in (self.mic_track, self.sys_track):
            if track is not None:
                track.join(timeout=20)
                if track.is_alive():
                    log("Дорожка %s не остановилась за 20 секунд - файл может быть "
                        "обрезан на последних секундах" % track.label, "WARN")

        if self.screen is not None:
            self.screen.stop()

        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception as exc:
                log("PyAudio не завершился штатно: %r (не критично)" % (exc,), "WARN")
            self.pa = None

        mic_path = os.path.join(self.folder, "mic.wav")
        sys_path = os.path.join(self.folder, "system.wav")
        mix_path = os.path.join(self.folder, "audio.wav")

        # Слишком короткая запись - ложное срабатывание, выбрасываем.
        if duration < self.cfg["min_meeting_seconds"] and reason != "max_duration":
            log("Запись длилась %.0f сек, а порог min_meeting_seconds = %d. "
                "Считаю это ложным срабатыванием и удаляю папку."
                % (duration, self.cfg["min_meeting_seconds"]), "WARN")
            set_session_log_file(None)
            try:
                shutil.rmtree(self.folder, ignore_errors=False)
                log("Удалена короткая запись: %s" % self.folder)
            except OSError as exc:
                log_exc("Не смог удалить короткую запись %s (удалите вручную)" % self.folder, exc)
            if notify is not None:
                notify.marker_off(self.cfg["recordings_dir"], log=log)
                notify.signal(
                    self.cfg,
                    "ClaudeMeet: запись отброшена",
                    "всего %.0f сек - короче порога %d сек, считаю ложным срабатыванием"
                    % (duration, self.cfg["min_meeting_seconds"]),
                    log=log)
            return None

        mixed = False
        if os.path.exists(mic_path) or os.path.exists(sys_path):
            try:
                mixed = mix_tracks(mic_path, sys_path, mix_path, self.cfg)
            except Exception as exc:
                log_exc("Сбой сведения дорожек - отдельные дорожки на месте, "
                        "audio.wav не собран", exc)
        else:
            log("Ни mic.wav, ни system.wav не появились - сводить нечего", "ERROR")

        if mixed and not self.cfg["keep_separate_tracks"]:
            for p in (mic_path, sys_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        log("Удалена отдельная дорожка %s (keep_separate_tracks=false)"
                            % os.path.basename(p))
                except OSError as exc:
                    log("Не смог удалить %s: %r" % (p, exc), "WARN")

        self.topic = self._guess_topic()
        meta = self._build_meta(reason, duration, mixed)
        meta_path = os.path.join(self.folder, "meta.json")
        try:
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)
            log("Записан meta.json")
        except OSError as exc:
            log_exc("Не смог записать meta.json", exc)

        final_folder = self._rename_with_topic()
        log("=== ЗАПИСЬ ЗАВЕРШЕНА: %s (%.1f мин) ===" % (final_folder, duration / 60.0))

        if notify is not None:
            notify.marker_off(self.cfg["recordings_dir"], log=log)
            notify.signal(
                self.cfg,
                "ClaudeMeet: запись сохранена",
                "%.0f мин · %s"
                % (duration / 60.0, os.path.basename(final_folder)),
                log=log)

        set_session_log_file(None)
        return final_folder

    def _build_meta(self, reason, duration, mixed):
        def track_meta(track, name):
            if track is None:
                return {"recorded": False, "reason": "дорожка не запускалась"}
            return {
                "recorded": bool(track.started_ok),
                "file": name,
                "seconds": round(track.seconds_written(), 1),
                "rms": round(track.rms(), 1),
                "peak": int(track.peak),
                "silent": track.rms() < float(self.cfg["silence_rms_threshold"]),
                "devices": track.device_history,
                "error": track.error,
            }

        return {
            "script_version": SCRIPT_VERSION,
            "started_at": self.started_at.astimezone().isoformat(timespec="seconds"),
            "finished_at": (self.finished_at or dt.datetime.now()).astimezone().isoformat(timespec="seconds"),
            "duration_seconds": int(duration),
            "duration_human": "%d:%02d" % (int(duration) // 60, int(duration) % 60),
            "stop_reason": reason,
            "trigger": {
                "app": self.trigger_app,
                "kind": self.trigger_kind,
            },
            "topic": self.topic,
            "window_titles": self.titles,
            "audio": {
                "sample_rate": int(self.cfg["target_sample_rate"]),
                "channels": 1,
                "mixed": bool(mixed),
                "mix_file": "audio.wav" if mixed else None,
                "mic": track_meta(self.mic_track, "mic.wav"),
                "system": track_meta(self.sys_track, "system.wav"),
            },
            "screen": {
                "recorded": bool(self.screen is not None) or os.path.exists(
                    os.path.join(self.folder, "screen.mp4")),
                "file": "screen.mp4" if os.path.exists(
                    os.path.join(self.folder, "screen.mp4")) else None,
                "framerate": int(self.cfg["screen_framerate"]),
                "scale_width": int(self.cfg["screen_scale_width"]),
            },
            "notes": self.notes,
            "ready_for_digest": True,
        }

    def _rename_with_topic(self):
        """Добавляет тему в имя папки. Кириллица транслитерируется."""
        if not self.topic:
            return self.folder
        parent = os.path.dirname(self.folder)
        base = os.path.basename(self.folder)
        new_name = "%s_%s" % (base, self.topic)
        new_path = os.path.join(parent, new_name)
        if os.path.exists(new_path):
            log("Папка %s уже есть, оставляю имя без темы" % new_path, "WARN")
            return self.folder
        try:
            os.rename(self.folder, new_path)
            self.folder = new_path
            # Лог сессии переехал вместе с папкой: без этого последние строки
            # ушли бы по старому пути, которого уже нет, и потерялись бы.
            set_session_log_file(os.path.join(new_path, "_recorder.log"))
            log("Папка переименована с темой: %s" % new_name)
        except OSError as exc:
            log("Не смог добавить тему в имя папки (%r) - файл может быть занят. "
                "Запись цела, просто имя без темы." % (exc,), "WARN")
        return self.folder


# ==========================================================================
# Режим демона
# ==========================================================================

def detect_holders(cfg, fallback_state):
    """Кто держит микрофон. Возвращает (список, вид_детекта)."""
    try:
        entries = read_mic_consent()
    except Exception as exc:
        log_exc("Реестровый детект сломался", exc)
        entries = None

    if entries is None:
        registry_empty = True
        active = []
    else:
        registry_empty = (len(entries) == 0)
        active = [e for e in entries
                  if e["active"]
                  and not name_matches(e["app"], cfg["ignore_processes"])
                  and name_matches(e["app"], cfg["watch_processes"])]

    if not registry_empty:
        if fallback_state.get("on"):
            fallback_state["on"] = False
            log("Реестровая ветка снова отвечает - возвращаюсь с резервного детекта "
                "на нормальный")
        return active, "registry"

    if not cfg["use_tasklist_fallback"]:
        if not fallback_state.get("warned"):
            fallback_state["warned"] = True
            log("Ветка реестра ConsentStore\\microphone пуста, а резерв через tasklist "
                "выключен в конфиге. Созвоны обнаруживаться НЕ БУДУТ.", "ERROR")
        return [], "registry"

    if not fallback_state.get("on"):
        fallback_state["on"] = True
        log("РАБОТАЮ НА РЕЗЕРВЕ: ветка реестра ConsentStore\\microphone пуста, "
            "детект идёт по списку процессов через tasklist. Это заметно хуже - "
            "Chrome запущен почти всегда, будут ложные старты.", "WARN")
    return active_apps_by_tasklist(cfg), "tasklist"


def pending_transcriptions(cfg):
    """
    Папки, где запись закрыта, но расшифровки ещё нет.

    Признак «закрыта» — наличие meta.json: рекордер пишет его только штатным
    завершением. Признак «расшифрована» — transcribe_info.json, его пишет
    transcribe.py в самом конце. Оборванный на середине прогон такого файла
    не оставляет, поэтому папка снова попадёт в очередь и досчитается с
    последнего таймкода.
    """
    todo = []
    rec_dir = cfg["recordings_dir"]
    try:
        names = sorted(os.listdir(rec_dir))
    except OSError as exc:
        log("Не смог прочитать %s: %r" % (rec_dir, exc), "WARN")
        return todo
    for name in names:
        folder = os.path.join(rec_dir, name)
        if not os.path.isdir(folder):
            continue
        if not os.path.exists(os.path.join(folder, "meta.json")):
            continue  # запись идёт прямо сейчас либо оборвана
        if os.path.exists(os.path.join(folder, "transcribe_info.json")):
            continue  # уже расшифрована
        has_audio = any(
            os.path.exists(os.path.join(folder, f)) and
            os.path.getsize(os.path.join(folder, f)) > 44
            for f in ("audio.wav", "mic.wav", "system.wav"))
        if has_audio:
            todo.append(folder)
    return todo


class TranscribeWorker(threading.Thread):
    """
    Фоновая очередь расшифровки.

    Зачем отдельным потоком, а не вызовом из finish(): расшифровка идёт минуты
    и десятки минут, и её нельзя вешать на путь остановки записи - иначе
    наблюдатель ослепнет на это время и пропустит следующий созвон.

    Правила:
      - пока идёт запись, расшифровка не запускается: процессорное время нужнее
        записи, а Whisper грузит все ядра;
      - за раз обрабатывается ровно одна папка, по возрастанию имени, то есть
        от старых к новым;
      - очередь пересматривается заново на каждом круге, поэтому накопленное
        после перезагрузки или после нескольких созвонов подряд разгребается
        само, без участия человека.
    """

    daemon = True

    def __init__(self, cfg, is_recording):
        super().__init__(name="transcriber")
        self.cfg = cfg
        self.is_recording = is_recording  # callable -> bool
        self._stop = threading.Event()
        self.current = None

    def stop(self):
        self._stop.set()

    def run(self):
        if not self.cfg.get("auto_transcribe", True):
            log("Автоматическая расшифровка выключена в конфиге "
                "(auto_transcribe=false) - записи будут копиться нерасшифрованными")
            return
        script = os.path.join(SCRIPT_DIR, "transcribe.py")
        if not os.path.exists(script):
            log("Нет %s - автоматическая расшифровка не будет работать. "
                "Записи сохранятся, расшифровать можно позже." % script, "ERROR")
            return
        log("Очередь расшифровки запущена: модель %s, обрабатываю по одной записи, "
            "во время записи не работаю"
            % self.cfg.get("transcribe_model", "small"))

        # Небольшая пауза на старте, чтобы не грузить машину сразу после входа
        # в систему, когда Windows и без нас занят автозагрузкой.
        self._stop.wait(self.cfg.get("transcribe_start_delay_seconds", 90))

        while not self._stop.is_set():
            try:
                if self.is_recording():
                    self._stop.wait(30)
                    continue
                todo = pending_transcriptions(self.cfg)
                if not todo:
                    self._stop.wait(self.cfg.get("transcribe_scan_seconds", 120))
                    continue
                log("В очереди на расшифровку: %d (%s)"
                    % (len(todo), ", ".join(os.path.basename(t) for t in todo[:5])))
                self._transcribe(script, todo[0])
            except Exception as exc:  # noqa: BLE001 - поток не должен умирать
                log_exc("Сбой в очереди расшифровки (продолжаю через минуту)", exc)
                self._stop.wait(60)

    def _transcribe(self, script, folder):
        name = os.path.basename(folder)
        model = self.cfg.get("transcribe_model", "small")
        log("Расшифровываю %s моделью %s. Это минуты; наблюдатель продолжает "
            "следить за микрофоном." % (name, model))
        self.current = name
        started = time.time()
        cmd = [sys.executable, script, name, "--model", model]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=SCRIPT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=_no_window_flag(),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            log_exc("Не смог запустить расшифровку %s" % name, exc)
            self.current = None
            self._stop.wait(300)
            return

        # Ждём, но не намертво: если началась запись, расшифровку надо снять -
        # она отбирает процессор у того, что важнее. Папка останется в очереди
        # и досчитается позже с последнего таймкода.
        while True:
            try:
                out = proc.communicate(timeout=20)[0]
                break
            except subprocess.TimeoutExpired:
                if self.is_recording():
                    log("Началась запись - снимаю расшифровку %s, вернусь к ней "
                        "после созвона" % name, "WARN")
                    try:
                        proc.terminate()
                        proc.wait(timeout=15)
                    except (subprocess.TimeoutExpired, OSError):
                        try:
                            proc.kill()
                        except OSError as exc:
                            log("Не смог снять процесс расшифровки: %r" % (exc,), "WARN")
                    self.current = None
                    return
                if self._stop.is_set():
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    self.current = None
                    return
                continue

        took = time.time() - started
        self.current = None
        tail = "\n".join((out or "").strip().splitlines()[-6:])
        if proc.returncode == 0:
            log("Расшифровка %s готова за %.0f сек" % (name, took))
            if tail:
                log("Хвост вывода расшифровки:\n%s" % tail)
            if notify is not None:
                notify.signal(self.cfg, "ClaudeMeet: расшифровка готова",
                             "%s · transcript_raw.txt" % name, log=log)
        else:
            log("Расшифровка %s упала, код %s. Вывод:\n%s"
                % (name, proc.returncode, tail or "(пусто)"), "ERROR")
            # Не долбить одну и ту же сломанную папку в цикле.
            self._stop.wait(600)


def _record_started(folder, meta):
    """Время старта записи: из meta.json, запасной вариант - из имени папки."""
    try:
        return dt.datetime.fromisoformat(meta["started_at"]).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return dt.datetime.strptime(os.path.basename(folder)[:15], "%Y-%m-%d_%H%M")
    except ValueError:
        return None


def _has_transcript(folder):
    """Расшифровка готова: есть transcribe_info.json и непустой текст."""
    if not os.path.exists(os.path.join(folder, "transcribe_info.json")):
        return False
    for name in ("transcript.md", "transcript_raw.txt"):
        p = os.path.join(folder, name)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return True
        except OSError:
            pass
    return False


def cleanup_old_recordings(cfg, dry_run=False):
    """
    Удаляет audio.wav (старше retention_audio_days) и screen.mp4 (старше
    retention_screen_days) у записей, у которых есть расшифровка.
    Папки без meta.json (идёт запись / оборвана), без расшифровки или с
    нечитаемой датой не трогает. Возвращает (файлов, байт).
    """
    if not cfg.get("retention_enabled", True):
        return 0, 0
    rules = (("audio.wav", int(cfg["retention_audio_days"])),
             ("screen.mp4", int(cfg["retention_screen_days"])))
    rec_dir = cfg["recordings_dir"]
    now = dt.datetime.now()
    files = freed = 0
    try:
        names = sorted(os.listdir(rec_dir))
    except OSError as exc:
        log("Очистка: не прочитал %s: %r" % (rec_dir, exc), "WARN")
        return 0, 0
    for name in names:
        folder = os.path.join(rec_dir, name)
        meta_path = os.path.join(folder, "meta.json")
        if not os.path.isdir(folder) or not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        started = _record_started(folder, meta)
        if started is None or not _has_transcript(folder):
            continue
        age_days = (now - started).total_seconds() / 86400.0
        for fname, days in rules:
            if days <= 0 or age_days < days:
                continue
            path = os.path.join(folder, fname)
            if not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                if dry_run:
                    log("Очистка (пробный прогон): удалил бы %s/%s, %.0f МБ, возраст %.0f дн."
                        % (name, fname, size / 1048576.0, age_days))
                else:
                    os.remove(path)
                    log("Очистка: удалён %s/%s, %.0f МБ, возраст %.0f дн., расшифровка есть"
                        % (name, fname, size / 1048576.0, age_days))
                files += 1
                freed += size
            except OSError as exc:
                log("Очистка: не смог удалить %s: %r" % (path, exc), "WARN")
    if files:
        log("Очистка: %s %d файл(ов), %.1f ГБ"
            % ("нашла бы" if dry_run else "освобождено", files, freed / 1073741824.0))
    return files, freed


def watch_loop(cfg):
    log("=== Старт наблюдателя ClaudeMeet v%s ===" % SCRIPT_VERSION)
    # Маркер, оставшийся с прошлого раза, означает, что процесс умер в середине
    # записи. Это надо назвать, а не молча подчистить.
    if notify is not None:
        notify.clear_stale_marker(cfg["recordings_dir"], log=log)
    log("Опрос раз в %d сек | старт после %d сек удержания микрофона | "
        "стоп через %d сек после освобождения"
        % (cfg["poll_seconds"], cfg["min_hold_seconds"], cfg["stop_grace_seconds"]))
    log("Слежу за процессами: %s" % ", ".join(cfg["watch_processes"]))
    free = free_gb(cfg["recordings_dir"])
    log("Свободно на диске: %.1f ГБ (минимум для старта %.1f ГБ)"
        % (free, cfg["min_free_gb"]))

    session = None
    hold_since = None
    released_at = None
    manual_mode = False
    fallback_state = {"on": False, "warned": False}
    last_holder = None
    cooldown_until = 0.0

    # Очередь расшифровки. Замыкание на локальную переменную session, поэтому
    # воркер всегда видит актуальное состояние: идёт запись или нет.
    state = {"session": None}
    transcriber = TranscribeWorker(cfg, lambda: state["session"] is not None)
    transcriber.start()

    # Очистка старых файлов: через 10 минут после старта, дальше раз в 6 часов,
    # и только когда запись не идёт.
    next_cleanup = time.time() + 600

    while True:
        state["session"] = session
        try:
            now = time.time()
            if session is None and now >= next_cleanup:
                next_cleanup = now + 6 * 3600
                try:
                    cleanup_old_recordings(cfg)
                except Exception as exc:
                    log_exc("Сбой очистки старых записей (не критично)", exc)
            manual = os.path.exists(manual_flag_path(cfg))
            stop_requested = os.path.exists(stop_flag_path(cfg))

            holders, kind = ([], "registry")
            if session is None or not manual_mode:
                holders, kind = detect_holders(cfg, fallback_state)

            if session is None:
                if stop_requested:
                    # Флаг стопа без активной записи - просто убираем.
                    _remove_flag(stop_flag_path(cfg))
                elif manual:
                    log("Найден флаг ручного старта - начинаю запись")
                    session = RecordingSession(cfg, "manual", "manual")
                    if session.start():
                        manual_mode = True
                    else:
                        session = None
                        _remove_flag(manual_flag_path(cfg))
                elif now < cooldown_until:
                    pass  # пауза после принудительной остановки по max_meeting
                elif holders:
                    app = holders[0]["app"]
                    if hold_since is None or app != last_holder:
                        hold_since = now
                        last_holder = app
                        log("Микрофон занял '%s' - жду %d сек, чтобы отсечь голосовые "
                            "и короткие звонки" % (app, cfg["min_hold_seconds"]))
                    elif now - hold_since >= cfg["min_hold_seconds"]:
                        session = RecordingSession(cfg, app, kind)
                        if not session.start():
                            session = None
                            hold_since = None
                            cooldown_until = now + 60
                        else:
                            manual_mode = False
                            released_at = None
                else:
                    if hold_since is not None:
                        log("Микрофон освобождён раньше порога - записи не будет")
                    hold_since = None
                    last_holder = None
            else:
                session.tick()
                if stop_requested:
                    _remove_flag(stop_flag_path(cfg))
                    _remove_flag(manual_flag_path(cfg))
                    session.finish("ручной стоп")
                    session = None
                    manual_mode = False
                    hold_since = None
                    released_at = None
                    cooldown_until = now + cfg["stop_grace_seconds"]
                elif manual_mode and not manual:
                    session.finish("снят флаг ручной записи")
                    session = None
                    manual_mode = False
                    hold_since = None
                    cooldown_until = now + 30
                elif session.duration() > cfg["max_meeting_minutes"] * 60:
                    log("Достигнут предохранитель max_meeting_minutes = %d - закрываю "
                        "запись. Если созвон продолжается, начнётся новая."
                        % cfg["max_meeting_minutes"], "WARN")
                    session.finish("max_duration")
                    session = None
                    manual_mode = False
                    hold_since = None
                    cooldown_until = now + 30
                elif not manual_mode:
                    if holders:
                        if released_at is not None:
                            log("Микрофон снова занят - продолжаю запись "
                                "(это была пауза/переподключение)")
                        released_at = None
                    else:
                        if released_at is None:
                            released_at = now
                            log("Микрофон освобождён - жду %d сек, вдруг это "
                                "переподключение" % cfg["stop_grace_seconds"])
                        elif now - released_at >= cfg["stop_grace_seconds"]:
                            session.finish("микрофон освобождён")
                            session = None
                            hold_since = None
                            released_at = None
                            last_holder = None

            # heartbeat: the watchdog treats a stale file as a hung recorder
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "heartbeat.txt"), "w") as _hb:
                    _hb.write(dt.datetime.now().isoformat())
            except Exception:
                pass
            time.sleep(cfg["poll_seconds"])

        except KeyboardInterrupt:
            log("Прервано с клавиатуры - аккуратно закрываю текущую запись", "WARN")
            if session is not None:
                try:
                    session.finish("прервано пользователем")
                except Exception as exc:
                    log_exc("Сбой при закрытии записи после Ctrl+C", exc)
            return 130
        except Exception as exc:
            log_exc("Сбой в главном цикле наблюдателя - продолжаю работать", exc)
            time.sleep(max(cfg["poll_seconds"], 5))


def _remove_flag(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log("Не смог удалить файл-флаг %s: %r. Уберите его вручную, иначе логика "
            "старта/стопа собьётся." % (path, exc), "ERROR")


# ==========================================================================
# Режимы диагностики
# ==========================================================================

def mode_list_devices(cfg):
    try:
        pa = open_pyaudio()
    except Exception as exc:
        print("Не удалось поднять аудиоподсистему: %s" % exc)
        return 3
    try:
        devices = all_devices(pa)
        print("")
        print("%-4s %-6s %-4s %-9s %s" % ("IDX", "LOOP", "IN", "RATE", "NAME"))
        print("-" * 90)
        for d in devices:
            if d.get("maxInputChannels", 0) <= 0 and not d["is_loopback"]:
                continue
            line = "%-4s %-6s %-4s %-9s %s" % (
                d["index"],
                "да" if d["is_loopback"] else "-",
                int(d.get("maxInputChannels", 0)),
                int(d.get("defaultSampleRate", 0)),
                d.get("name", ""))
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"))
        print("-" * 90)
        lbs = loopback_devices(pa)
        print("Loopback-устройств найдено: %d" % len(lbs))
        if not lbs:
            print("ЭТО ПРОБЛЕМА: без loopback не будет системного звука (собеседников).")
            print("Проверьте, что стоит именно PyAudioWPatch, а не обычный PyAudio.")
        dl = default_loopback(pa)
        if dl:
            print("Loopback устройства вывода по умолчанию: %s" % dl.get("name"))
        mic = default_mic(pa, cfg)
        if mic:
            print("Микрофон по умолчанию: %s" % mic.get("name"))
        else:
            print("МИКРОФОН НЕ НАЙДЕН.")
        print("")
        print("Чтобы закрепить устройство, впишите кусок его имени в meet_config.json:")
        print('  "loopback_device_name": "Realtek"   -  системный звук')
        print('  "mic_device_name": "Микрофон"       -  микрофон')
    finally:
        try:
            pa.terminate()
        except Exception as exc:
            log("PyAudio не завершился: %r" % (exc,), "WARN")
    return 0


def mode_test_audio(cfg, seconds=10):
    """10 секунд пишем обе дорожки и прямо говорим, слышно ли что-то."""
    try:
        pa = open_pyaudio()
    except Exception as exc:
        print("Не удалось поднять аудиоподсистему: %s" % exc)
        return 3

    out_dir = cfg["logs_dir"]
    os.makedirs(out_dir, exist_ok=True)
    mic_path = os.path.join(out_dir, "test_mic.wav")
    sys_path = os.path.join(out_dir, "test_system.wav")

    print("")
    print("=== Проверка звука, %d секунд ===" % seconds)
    print("СЕЙЧАС: скажите что-нибудь в микрофон И включите любой звук на компьютере")
    print("(музыку, ролик - что угодно), иначе проверять будет нечего.")
    print("")

    mic = default_mic(pa, cfg)
    cands = pick_loopback(pa, cfg)

    tracks = []
    if mic is not None:
        t = TrackRecorder(pa, "mic", [mic], mic_path, cfg)
        t.start()
        tracks.append(t)
    else:
        print("МИКРОФОН НЕ НАЙДЕН - дорожку микрофона проверить нечем.")
    if cands:
        t = TrackRecorder(pa, "system", cands, sys_path, cfg)
        t.start()
        tracks.append(t)
    else:
        print("LOOPBACK НЕ НАЙДЕН - системный звук записать нечем.")

    for i in range(seconds, 0, -1):
        sys.stdout.write("\r  осталось %2d сек... " % i)
        sys.stdout.flush()
        time.sleep(1)
    print("")

    for t in tracks:
        t.stop()
    for t in tracks:
        t.join(timeout=15)
    try:
        pa.terminate()
    except Exception as exc:
        log("PyAudio не завершился: %r" % (exc,), "WARN")

    thr = float(cfg["silence_rms_threshold"])
    print("")
    print("=== Результат ===")
    verdict_ok = True
    for t in tracks:
        label = "МИКРОФОН   " if t.label == "mic" else "СИСТЕМНЫЙ  "
        dev = (t.device_info or {}).get("name", "?")
        if not t.started_ok:
            print("%s: НЕ ПИШЕТСЯ. %s" % (label, t.error or "устройство не открылось"))
            verdict_ok = False
            continue
        bars = int(min(40, t.rms() / 50.0))
        print("%s: RMS %7.1f  пик %6d  %s" % (label, t.rms(), t.peak, "#" * bars))
        print("             устройство: %s" % dev)
        print("             файл: %s (%.1f сек)"
              % (t.out_path, t.seconds_written()))
        if t.rms() < thr:
            print("             ВЕРДИКТ: ТИШИНА. Ничего не слышно.")
            verdict_ok = False
        elif t.peak > 32000:
            print("             ВЕРДИКТ: слышно, но есть перегруз (пик у потолка).")
        else:
            print("             ВЕРДИКТ: слышно нормально.")
    print("")
    if verdict_ok and tracks:
        print("ИТОГ: обе дорожки слышны, можно запускать наблюдатель.")
    else:
        print("ИТОГ: есть проблема (см. выше).")
        print("Если молчит СИСТЕМНЫЙ звук: Windows часто отдаёт звук созвонов в другое")
        print("устройство (роль 'Связь'). Посмотрите --list-devices и впишите нужное")
        print("имя в loopback_device_name в meet_config.json.")
        print("Если молчит МИКРОФОН: проверьте Параметры -> Конфиденциальность ->")
        print("Микрофон, и что выбран правильный вход по умолчанию.")
    print("")
    print("Файлы проверки можно послушать: %s и %s" % (mic_path, sys_path))
    return 0 if verdict_ok else 1


def mode_test_detect(cfg, seconds=30):
    print("")
    print("=== Проверка детекта микрофона, %d секунд ===" % seconds)
    print("Включите созвон (или хотя бы нажмите 'проверить микрофон' в Zoom/Телемосте).")
    print("")
    try:
        entries = read_mic_consent()
    except Exception as exc:
        print("Реестр не читается: %s" % exc)
        return 3
    print("Всего записей в ConsentStore\\microphone: %d" % len(entries))
    if not entries:
        print("ВЕТКА ПУСТА. Такое бывает на свежей системе, где микрофоном ещё никто")
        print("не пользовался. Дайте любому приложению доступ к микрофону и повторите.")
        print("Пока ветка пуста, рекордер будет работать на резерве через tasklist.")
    print("")

    t_end = time.time() + seconds
    prev = None
    while time.time() < t_end:
        try:
            entries = read_mic_consent()
        except Exception as exc:
            print("Сбой чтения реестра: %r" % (exc,))
            time.sleep(2)
            continue
        active = [e for e in entries if e["active"]]
        watched = [e for e in active if name_matches(e["app"], cfg["watch_processes"])
                   and not name_matches(e["app"], cfg["ignore_processes"])]
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        if not active:
            line = "%s  микрофон свободен" % stamp
        else:
            parts = []
            for e in active:
                mark = "+" if e in watched else "-"
                parts.append("%s%s [%s]" % (mark, e["app"], e["hive"]))
            line = "%s  держат микрофон: %s" % (stamp, ", ".join(parts))
        if line[9:] != (prev or ""):
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"))
            prev = line[9:]
        time.sleep(2)

    print("")
    print("Знак '+' - процесс из белого списка watch_processes, по нему запись начнётся.")
    print("Знак '-' - микрофон держит кто-то посторонний, запись не начнётся.")
    print("Белый список сейчас: %s" % ", ".join(cfg["watch_processes"]))
    return 0


def mode_record_now(cfg):
    path = manual_flag_path(cfg)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dt.datetime.now().isoformat())
    except OSError as exc:
        print("Не смог создать файл-флаг %s: %r" % (path, exc))
        return 1
    _remove_flag(stop_flag_path(cfg))
    print("Флаг ручной записи поставлен: %s" % path)
    print("Наблюдатель подхватит его в течение %d секунд." % cfg["poll_seconds"])
    print("Если наблюдатель НЕ запущен - запись не начнётся. Запустите")
    print("run_meet_recorder.bat или задачу ClaudeMeet_Watcher.")
    print("Остановить: meet_recorder.py --stop")
    return 0


def mode_stop(cfg):
    _remove_flag(manual_flag_path(cfg))
    path = stop_flag_path(cfg)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dt.datetime.now().isoformat())
    except OSError as exc:
        print("Не смог создать файл-флаг остановки %s: %r" % (path, exc))
        return 1
    print("Флаг остановки поставлен: %s" % path)
    print("Текущая запись закроется в течение %d секунд." % cfg["poll_seconds"])
    return 0


# ==========================================================================
# main
# ==========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Рекордер созвонов: ловит использование микрофона и пишет звук + экран."
    )
    parser.add_argument("--test-audio", action="store_true",
                        help="10 секунд записи, показать уровни обеих дорожек")
    parser.add_argument("--test-detect", action="store_true",
                        help="30 секунд печатать, кто держит микрофон")
    parser.add_argument("--record-now", action="store_true",
                        help="ручной старт записи (через файл-флаг)")
    parser.add_argument("--stop", action="store_true",
                        help="ручная остановка записи")
    parser.add_argument("--list-devices", action="store_true",
                        help="показать аудиоустройства")
    parser.add_argument("--cleanup", action="store_true",
                        help="один раз прогнать очистку старых audio.wav/screen.mp4 и выйти")
    parser.add_argument("--dry-run", action="store_true",
                        help="с --cleanup: только показать, что было бы удалено")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except RuntimeError as exc:
        set_log_file(os.path.join(SCRIPT_DIR, "logs", "meet_recorder.log"))
        print("\n%s\n" % exc)
        return 2

    try:
        ensure_dirs(cfg)
    except OSError as exc:
        print("Не удалось создать рабочие папки (%r). Проверьте пути в meet_config.json "
              "и права на запись." % (exc,))
        return 2

    set_log_file(os.path.join(cfg["logs_dir"], "meet_recorder.log"), cfg["max_log_mb"])

    if WINREG_IMPORT_ERROR is not None and not (args.list_devices or args.test_audio):
        msg = ("Модуль winreg недоступен (%r). Этот скрипт рассчитан на Windows: "
               "детект созвона делается через реестр." % (WINREG_IMPORT_ERROR,))
        log(msg, "ERROR")
        print("\n%s\n" % msg)
        return 3

    try:
        if args.cleanup:
            n, b = cleanup_old_recordings(cfg, dry_run=args.dry_run)
            print("%s: %d файл(ов), %.1f ГБ" % ("Нашла бы" if args.dry_run else "Удалено", n, b / 1073741824.0))
            return 0
        if args.list_devices:
            return mode_list_devices(cfg)
        if args.test_audio:
            return mode_test_audio(cfg)
        if args.test_detect:
            return mode_test_detect(cfg)
        if args.record_now:
            return mode_record_now(cfg)
        if args.stop:
            return mode_stop(cfg)
        return watch_loop(cfg)
    except KeyboardInterrupt:
        log("Прервано пользователем", "WARN")
        return 130
    except Exception as exc:
        log_exc("НЕОБРАБОТАННЫЙ СБОЙ - рекордер остановлен", exc)
        print("\nСбой: %r\nПодробности в логе: %s\n"
              % (exc, os.path.join(cfg["logs_dir"], "meet_recorder.log")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
