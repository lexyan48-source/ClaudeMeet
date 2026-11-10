#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check.py - самодиагностика рекордера созвонов. Запускается через check.bat.

Проверяет по пунктам: Python, numpy, PyAudioWPatch, ffmpeg, аудиоустройства,
loopback, реестровый детект микрофона, права на запись, свободное место,
конфиг и задачу Планировщика. По каждому пункту - прямой вердикт по-русски.

Пишет одновременно на экран и в check_log.txt.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(SCRIPT_DIR, "check_log.txt")

_log_fh = None
_results = []  # (пункт, "ok"/"warn"/"fail", пояснение)


def out(text=""):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))
    except OSError as exc:
        sys.stderr.write("Консоль недоступна: %r\n" % (exc,))
    if _log_fh is not None:
        try:
            _log_fh.write(text + "\n")
            _log_fh.flush()
        except OSError as exc:
            sys.stderr.write("Не пишется check_log.txt: %r\n" % (exc,))


def verdict(name, status, text):
    mark = {"ok": "[ОК]  ", "warn": "[!]   ", "fail": "[СБОЙ]"}.get(status, "[?]   ")
    _results.append((name, status, text))
    out("%s %s: %s" % (mark, name, text))


def header(text):
    out("")
    out("=" * 74)
    out(text)
    out("=" * 74)


# --------------------------------------------------------------------------

def check_python():
    header("1. Python")
    out("Версия: %s" % sys.version.replace("\n", " "))
    out("Интерпретатор: %s" % sys.executable)
    major, minor = sys.version_info[:2]
    if major < 3 or (major == 3 and minor < 8):
        verdict("Python", "fail", "нужен Python 3.8 и новее, найден %d.%d" % (major, minor))
        return False
    if (major, minor) >= (3, 13):
        out("Python 3.13+: модуля audioop в стандартной библиотеке больше нет.")
        out("Рекордер это учитывает - весь ресемплинг сделан на numpy.")
    verdict("Python", "ok", "%d.%d, путь корректный" % (major, minor))
    return True


def check_numpy():
    header("2. numpy")
    try:
        import numpy
    except Exception as exc:
        verdict("numpy", "fail",
                "не импортируется (%r). Без него не будет ни ресемплинга, ни сведения. "
                "Лечится: python -m pip install numpy" % (exc,))
        return False
    out("Версия: %s" % numpy.__version__)
    try:
        a = numpy.interp([0.0, 0.5, 1.0], [0, 1], [0.0, 10.0])
        out("Проверка np.interp: %s (ожидалось [0. 5. 10.])" % (a,))
    except Exception as exc:
        verdict("numpy", "fail", "установлен, но не работает: %r" % (exc,))
        return False
    verdict("numpy", "ok", "установлен и считает")
    return True


def check_pyaudio():
    header("3. PyAudioWPatch (запись звука)")
    try:
        import pyaudiowpatch as pyaudio
    except Exception as exc:
        verdict("PyAudioWPatch", "fail",
                "не импортируется (%r). Без него звук не пишется вообще. "
                "Лечится: install.bat или python -m pip install PyAudioWPatch" % (exc,))
        return None
    ver = getattr(pyaudio, "__version__", "?")
    out("Версия: %s" % ver)
    verdict("PyAudioWPatch", "ok", "импортируется, версия %s" % ver)
    return pyaudio


def check_ffmpeg(cfg):
    header("4. ffmpeg (видео экрана)")
    path = cfg.get("ffmpeg_path", "")
    ok = False
    if path and os.path.exists(path):
        out("Найден: %s" % path)
        try:
            proc = subprocess.run([path, "-version"], capture_output=True, timeout=20)
            first = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
            out("  %s" % (first[0] if first else "(пустой вывод)"))
            ok = True
        except (OSError, subprocess.TimeoutExpired) as exc:
            out("  но не запускается: %r" % (exc,))
    else:
        out("НЕ найден по пути из конфига: %s" % path)
        try:
            proc = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=20)
            first = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
            out("Зато ffmpeg есть в PATH: %s" % (first[0] if first else ""))
            out("Впишите его полный путь в ffmpeg_path в meet_config.json.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            out("В PATH его тоже нет (%r)" % (exc,))

    probe = cfg.get("ffprobe_path", "")
    out("ffprobe: %s" % ("найден %s" % probe if probe and os.path.exists(probe)
                         else "НЕ найден по пути %s" % probe))

    if ok:
        verdict("ffmpeg", "ok", "готов, видео экрана будет писаться")
    else:
        verdict("ffmpeg", "warn",
                "недоступен. Звук всё равно запишется, а screen.mp4 - нет. "
                "Поправьте ffmpeg_path в meet_config.json")
    return ok


def check_devices(pyaudio, cfg, mr):
    header("5. Аудиоустройства")
    if pyaudio is None:
        verdict("Устройства", "fail", "проверять нечем, PyAudioWPatch не установлен")
        return
    try:
        pa = pyaudio.PyAudio()
    except Exception as exc:
        verdict("Устройства", "fail",
                "аудиоподсистема не поднимается: %r. Обычно это значит, что нет ни "
                "одного звукового устройства или сломан аудиодрайвер" % (exc,))
        return
    try:
        devices = mr.all_devices(pa)
        out("%-4s %-6s %-4s %-9s %s" % ("IDX", "LOOP", "IN", "RATE", "NAME"))
        out("-" * 74)
        for d in devices:
            if d.get("maxInputChannels", 0) <= 0 and not d["is_loopback"]:
                continue
            out("%-4s %-6s %-4s %-9s %s" % (
                d["index"], "да" if d["is_loopback"] else "-",
                int(d.get("maxInputChannels", 0)),
                int(d.get("defaultSampleRate", 0)), d.get("name", "")))
        out("-" * 74)

        mic = mr.default_mic(pa, cfg)
        if mic:
            verdict("Микрофон", "ok", "'%s' (index=%s)" % (mic.get("name"), mic.get("index")))
        else:
            verdict("Микрофон", "fail",
                    "не найден ни один вход. Вашего голоса в записи не будет. "
                    "Проверьте Параметры -> Система -> Звук -> Ввод")

        lbs = mr.loopback_devices(pa)
        out("Loopback-устройств: %d" % len(lbs))
        for d in lbs:
            out("   - %s (index=%s)" % (d.get("name"), d.get("index")))
        if not lbs:
            verdict("Loopback", "fail",
                    "не найдено ни одного. Речь собеседников записана НЕ БУДЕТ. "
                    "Обычно это значит, что стоит обычный PyAudio вместо PyAudioWPatch")
        else:
            dl = mr.default_loopback(pa)
            verdict("Loopback", "ok",
                    "найдено %d, по умолчанию будет '%s'"
                    % (len(lbs), dl.get("name") if dl else "?"))
            out("ВАЖНО: то, что устройство найдено, ещё не значит, что на нём слышен")
            out("созвон. Windows часто отдаёт звук конференций в устройство роли 'Связь'.")
            out("Реальную проверку делает test_audio.bat.")
    finally:
        try:
            pa.terminate()
        except Exception as exc:
            out("PyAudio не завершился штатно: %r (не критично)" % (exc,))


def check_registry(cfg, mr):
    header("6. Детект микрофона через реестр")
    if mr.winreg is None:
        verdict("Реестр", "fail", "модуль winreg недоступен - это не Windows")
        return
    try:
        entries = mr.read_mic_consent()
    except Exception as exc:
        verdict("Реестр", "fail",
                "ветка ConsentStore\\microphone не читается: %r. Детект созвонов "
                "работать не будет" % (exc,))
        out(traceback.format_exc())
        return

    out("Всего записей в ConsentStore\\microphone: %d" % len(entries))
    active = [e for e in entries if e["active"]]
    watched = [e for e in active
               if mr.name_matches(e["app"], cfg["watch_processes"])
               and not mr.name_matches(e["app"], cfg["ignore_processes"])]

    recent = sorted([e for e in entries if e["start"]], key=lambda e: -e["start"])[:8]
    if recent:
        out("Последние приложения, бравшие микрофон:")
        for e in recent:
            when = mr.filetime_to_dt(e["start"])
            state = "ДЕРЖИТ СЕЙЧАС" if e["active"] else "отпустил"
            out("   %-34s %-14s %s" % (e["app"][:34], state,
                                       when.strftime("%Y-%m-%d %H:%M") if when else "?"))

    if not entries:
        verdict("Реестр", "warn",
                "ветка пуста. Так бывает на системе, где микрофоном ещё никто не "
                "пользовался. Рекордер уйдёт на резерв через tasklist и будет ловить "
                "созвоны хуже. Один раз включите микрофон в любом приложении и "
                "перезапустите эту проверку")
        return

    if active:
        out("Прямо сейчас микрофон держат: %s"
            % ", ".join("%s%s" % ("+" if e in watched else "-", e["app"]) for e in active))
    else:
        out("Прямо сейчас микрофон свободен - это нормально, если созвона нет.")
    verdict("Реестр", "ok",
            "ветка читается, записей %d, активных %d, из них в белом списке %d"
            % (len(entries), len(active), len(watched)))


def check_tasklist(cfg, mr):
    header("7. tasklist (заголовки окон и резервный детект)")
    rows = mr.run_tasklist(verbose=True)
    if not rows:
        verdict("tasklist", "warn",
                "не отвечает. Заголовки окон в meta.json не попадут, темы в имени "
                "папки не будет. На саму запись не влияет")
        return
    out("Процессов в системе: %d" % len(rows))
    titles = mr.collect_window_titles(cfg)
    if titles:
        out("Окна процессов из белого списка (из них берётся тема):")
        for t in titles[:10]:
            out("   [%s] %s" % (t["process"], t["title"][:80]))
    else:
        out("Сейчас ни одно окно из белого списка не открыто - это нормально.")
    verdict("tasklist", "ok", "отвечает, окон из белого списка сейчас %d" % len(titles))


def check_write_and_space(cfg):
    header("8. Права на запись и свободное место")
    ok = True
    for key in ("recordings_dir", "logs_dir"):
        path = cfg.get(key, "")
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, "_check_write.tmp")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("check %s" % dt.datetime.now().isoformat())
            os.remove(probe)
            out("Запись в %s: получилось" % path)
        except OSError as exc:
            out("Запись в %s: НЕ получилось (%r)" % (path, exc))
            ok = False
    if ok:
        verdict("Права на запись", "ok", "папки записей и логов доступны на запись")
    else:
        verdict("Права на запись", "fail",
                "в рабочие папки писать нельзя. Записи сохраняться не будут. "
                "Проверьте права и путь в meet_config.json")

    import shutil
    try:
        usage = shutil.disk_usage(cfg.get("recordings_dir") or SCRIPT_DIR)
        free = usage.free / (1024.0 ** 3)
        total = usage.total / (1024.0 ** 3)
        out("Свободно: %.1f ГБ из %.1f ГБ" % (free, total))
        out("Ориентир: час созвона это примерно 110 МБ звука + 30-80 МБ видео экрана.")
        need = float(cfg.get("min_free_gb", 2.0))
        if free < need:
            verdict("Свободное место", "fail",
                    "%.1f ГБ - меньше порога %.1f ГБ, запись НЕ начнётся" % (free, need))
        elif free < need * 5:
            verdict("Свободное место", "warn",
                    "%.1f ГБ - хватит на несколько созвонов, но лучше почистить диск" % free)
        else:
            verdict("Свободное место", "ok", "%.1f ГБ - с запасом" % free)
    except OSError as exc:
        verdict("Свободное место", "warn", "не удалось узнать: %r" % (exc,))


def check_task():
    header("9. Задача Планировщика ClaudeMeet_Watcher")
    try:
        proc = subprocess.run(["schtasks", "/query", "/tn", "ClaudeMeet_Watcher", "/fo", "list"],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        verdict("Планировщик", "warn", "schtasks не ответил: %r" % (exc,))
        return
    text = (proc.stdout or b"").decode("cp866", "replace")
    if proc.returncode != 0:
        verdict("Планировщик", "warn",
                "задача ClaudeMeet_Watcher не заведена. Автозапуска при входе не будет. "
                "Лечится запуском setup_task.bat")
        return
    for line in text.splitlines():
        if line.strip():
            out("   %s" % line.strip())
    verdict("Планировщик", "ok", "задача ClaudeMeet_Watcher существует")


def check_config():
    header("10. Конфиг meet_config.json")
    path = os.path.join(SCRIPT_DIR, "meet_config.json")
    if not os.path.exists(path):
        verdict("Конфиг", "warn",
                "файла нет, работают значения по умолчанию. Это допустимо")
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        verdict("Конфиг", "fail",
                "файл не читается: %r. Рекордер не запустится. Обычно виновата "
                "лишняя запятая или кавычки-ёлочки" % (exc,))
        return None
    out("Параметров в файле: %d" % len(data))
    for key in ("min_hold_seconds", "stop_grace_seconds", "min_meeting_seconds",
                "record_screen", "record_audio", "loopback_device_name"):
        out("   %-22s = %r" % (key, data.get(key)))
    verdict("Конфиг", "ok", "читается, JSON корректный")
    return data


def summary():
    header("ИТОГ")
    fails = [r for r in _results if r[1] == "fail"]
    warns = [r for r in _results if r[1] == "warn"]
    for name, status, text in _results:
        mark = {"ok": "ОК  ", "warn": "!   ", "fail": "СБОЙ"}.get(status, "?   ")
        out("%s  %-20s %s" % (mark, name, text[:90]))
    out("")
    if fails:
        out("ВЕРДИКТ: рекордер в таком виде работать не будет.")
        out("Сначала разберитесь с пунктами, помеченными СБОЙ:")
        for name, _s, text in fails:
            out("   - %s: %s" % (name, text))
        return 1
    if warns:
        out("ВЕРДИКТ: рекордер запустится, но есть замечания:")
        for name, _s, text in warns:
            out("   - %s: %s" % (name, text))
        out("")
        out("Дальше: test_audio.bat - убедиться, что реально слышно обе стороны.")
        return 0
    out("ВЕРДИКТ: всё в порядке, можно работать.")
    out("Дальше: test_audio.bat, потом setup_task.bat.")
    return 0


def main():
    global _log_fh
    try:
        _log_fh = open(LOG_PATH, "w", encoding="utf-8")
    except OSError as exc:
        sys.stderr.write("Не смог создать %s: %r. Пишу только на экран.\n" % (LOG_PATH, exc))
        _log_fh = None

    try:
        out("Самодиагностика ClaudeMeet")
        out("Дата: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        out("Папка: %s" % SCRIPT_DIR)

        ok_py = check_python()
        check_numpy()
        pa_mod = check_pyaudio()

        # Сам рекордер импортируем как модуль - так проверяем заодно, что он цел.
        mr = None
        header("Импорт meet_recorder.py")
        try:
            sys.path.insert(0, SCRIPT_DIR)
            import meet_recorder as mr
            out("Модуль импортирован, версия скрипта %s" % mr.SCRIPT_VERSION)
            verdict("meet_recorder.py", "ok", "файл цел и импортируется")
        except Exception as exc:
            verdict("meet_recorder.py", "fail",
                    "не импортируется: %r. Файл повреждён - без него ничего не работает"
                    % (exc,))
            out(traceback.format_exc())

        raw_cfg = check_config()

        if mr is not None:
            try:
                cfg = mr.load_config()
            except Exception as exc:
                out("Конфиг не загрузился через сам рекордер: %r" % (exc,))
                cfg = dict(mr.DEFAULT_CONFIG)
            check_ffmpeg(cfg)
            check_devices(pa_mod, cfg, mr)
            check_registry(cfg, mr)
            check_tasklist(cfg, mr)
            check_write_and_space(cfg)
        else:
            out("Дальнейшие проверки пропущены: не импортировался meet_recorder.py")
        check_task()

        if not ok_py:
            out("")
            out("Отдельно: версия Python не подходит, всё остальное вторично.")
        return summary()
    except Exception as exc:
        out("")
        out("САМА ПРОВЕРКА УПАЛА: %r" % (exc,))
        out(traceback.format_exc())
        return 1
    finally:
        if _log_fh is not None:
            try:
                _log_fh.close()
            except OSError:
                sys.stderr.write("check_log.txt не закрылся штатно\n")


if __name__ == "__main__":
    sys.exit(main())
