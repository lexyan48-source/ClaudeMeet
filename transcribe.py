# -*- coding: utf-8 -*-
"""
Расшифровка записи созвона локальным faster-whisper.

Пишет результат в файл ПО МЕРЕ СЧЁТА, а не в конце: генератор segments ленивый,
и при обрыве на диске останется всё, что успело посчитаться. Повторный запуск
дорасшифрует остаток от последнего таймкода.

    python transcribe.py                        последняя запись, модель small
    python transcribe.py 2026-08-05_1742        конкретная папка
    python transcribe.py 2026-08-05_1742 --model medium
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

REC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


def say(msg):
    print(msg, flush=True)


def pick_folder(name):
    if name:
        path = os.path.join(REC_DIR, name)
        if not os.path.isdir(path):
            say("Папки нет: %s" % path)
            sys.exit(1)
        return path
    # Самая свежая по ИМЕНИ среди закрытых записей: имя - это время старта,
    # а mtime врёт при копировании.
    closed = []
    for d in sorted(os.listdir(REC_DIR)):
        full = os.path.join(REC_DIR, d)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, "meta.json")):
            closed.append(full)
    if not closed:
        say("В %s нет ни одной закрытой записи (папки с meta.json)" % REC_DIR)
        sys.exit(1)
    return closed[-1]


def pick_audio(folder):
    """audio.wav основной; нет - работаем по той дорожке, что есть."""
    for name in ("audio.wav", "mic.wav", "system.wav"):
        path = os.path.join(folder, name)
        if os.path.exists(path) and os.path.getsize(path) > 44:
            return path, name
    return None, None


def last_done_second(out_path):
    """Последний посчитанный таймкод, чтобы продолжить, а не начинать с нуля."""
    if not os.path.exists(out_path):
        return 0.0
    last = 0.0
    pattern = re.compile(r"^\[(\d\d):(\d\d)-(\d\d):(\d\d)\]")
    with open(out_path, encoding="utf-8") as fh:
        for line in fh:
            m = pattern.match(line)
            if m:
                last = int(m.group(3)) * 60 + int(m.group(4))
    return float(last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default=None)
    ap.add_argument("--model", default="small",
                    help="small - кешированная и быстрая, medium - точнее на "
                         "цифрах и именах, но качается ~1.5 ГБ и идёт втрое дольше")
    args = ap.parse_args()

    folder = pick_folder(args.folder)
    audio, audio_name = pick_audio(folder)
    if audio is None:
        say("В папке нет ни одной звуковой дорожки: %s" % folder)
        sys.exit(1)

    out_path = os.path.join(folder, "transcript_raw.txt")
    log_path = os.path.join(folder, "whisper.log")
    resume_from = last_done_second(out_path)

    say("=" * 70)
    say("Папка:    %s" % folder)
    say("Дорожка:  %s" % audio_name)
    say("Модель:   %s" % args.model)
    if resume_from > 0:
        say("Продолжаю с %d:%02d - в файле уже есть результат"
            % (resume_from // 60, resume_from % 60))
    say("=" * 70)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        say("faster-whisper не установлен: %r" % (exc,))
        say("Лечится: install.bat")
        sys.exit(1)

    say("Поднимаю модель (первый раз для medium - качается ~1.5 ГБ, это минуты)...")
    started = dt.datetime.now()
    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    say("Модель поднята за %.0f сек. Считаю..." % (dt.datetime.now() - started).total_seconds())

    kwargs = dict(language="ru", vad_filter=True, beam_size=5)
    if resume_from > 0:
        kwargs["clip_timestamps"] = [resume_from]

    segments, info = model.transcribe(audio, **kwargs)

    mode = "a" if resume_from > 0 else "w"
    n = 0
    t0 = dt.datetime.now()
    with open(out_path, mode, encoding="utf-8") as out, \
         open(log_path, "a", encoding="utf-8") as log:
        log.write("\n=== %s | модель %s | дорожка %s ===\n"
                  % (dt.datetime.now().isoformat(timespec="seconds"),
                     args.model, audio_name))
        for s in segments:
            m1, s1 = divmod(int(s.start), 60)
            m2, s2 = divmod(int(s.end), 60)
            line = "[%02d:%02d-%02d:%02d] %s" % (m1, s1, m2, s2, s.text.strip())
            out.write(line + "\n")
            out.flush()
            n += 1
            if n % 10 == 0:
                say("  ... %d реплик, дошёл до %02d:%02d" % (n, m2, s2))

    took = (dt.datetime.now() - t0).total_seconds()
    say("=" * 70)
    say("Готово: %d реплик за %.0f сек" % (n, took))
    say("Результат: %s" % out_path)

    marker = {
        "model": args.model,
        "audio_file": audio_name,
        "segments": n,
        "seconds_spent": round(took, 1),
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    with open(os.path.join(folder, "transcribe_info.json"), "w", encoding="utf-8") as fh:
        json.dump(marker, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
