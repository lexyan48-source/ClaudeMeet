# -*- coding: utf-8 -*-
"""
Видимые сигналы ClaudeMeet: уведомление и файл-маркер «идёт запись».

История: первая версия стреляла всплывашкой через NotifyIcon и не проверяла
результат — Popen с DEVNULL и без ожидания. 06.08.2026 выяснилось, что за два
созвона не появилось ни одного уведомления, а в логе не было ни одной ошибки,
потому что проверять было нечем. Отсюда два вывода, заложенных в этот модуль:

  1. Каналов должно быть несколько, и они независимы.
  2. Результат каждого канала пишется в лог. Молчание — не доказательство успеха.

Порядок каналов:
  toast  — родное уведомление Windows через WinRT. Работает надёжнее балуна,
           но требует зарегистрированного AppUserModelID: берём готовый AUMID
           самого PowerShell, он есть в системе всегда.
  balloon — NotifyIcon, старый способ. Оставлен резервом: на части систем
           WinRT недоступен, а балун показывается.
  telegram — сообщение в ваш Telegram-бот. Единственный канал, который виден
           с телефона и работает, даже когда компьютер заблокирован или
           уведомления Windows заглушены. Необязателен: пока токен и чат не
           заданы в meet_config.json, канал молча пропускается.

Ни один сигнал не имеет права задержать запись: всё уходит в фоновый поток,
любая ошибка глушится в лог.
"""

import json
import os
import subprocess
import threading
import urllib.parse
import urllib.request

MARKER_NAME = "ЗАПИСЬ_ИДЁТ.txt"

# AUMID PowerShell. Без зарегистрированного AUMID WinRT молча ничего не покажет —
# это самая частая причина «toast не работает, ошибок нет».
_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $tpl.GetElementsByTagName('text')
$texts.Item(0).AppendChild($tpl.CreateTextNode($env:CM_TITLE)) | Out-Null
$texts.Item(1).AppendChild($tpl.CreateTextNode($env:CM_TEXT)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CM_AUMID).Show($toast)
Write-Output 'TOAST_OK'
"""

_PS_BALLOON = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
    "$n = New-Object System.Windows.Forms.NotifyIcon; "
    "$n.Icon = [System.Drawing.SystemIcons]::Information; "
    "$n.Visible = $true; "
    "Start-Sleep -Milliseconds 300; "
    "$n.ShowBalloonTip(8000, $env:CM_TITLE, $env:CM_TEXT, "
    "[System.Windows.Forms.ToolTipIcon]::Info); "
    "Start-Sleep -Seconds 9; $n.Dispose(); "
    "Write-Output 'BALLOON_OK'"
)


def _no_window_flag():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _run_ps(script, title, text, timeout, log=None, label=""):
    """
    Запускает PowerShell и ЖДЁТ результат. Именно ожидание отличает эту версию
    от прошлой: без него не отличить «показалось» от «молча упало».
    """
    env = dict(os.environ)
    env["CM_TITLE"] = str(title)
    env["CM_TEXT"] = str(text)
    env["CM_AUMID"] = _AUMID
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_no_window_flag(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if log:
            log("Уведомление (%s): не запустилось - %r" % (label, exc), "WARN")
        return False
    if proc.returncode == 0 and "_OK" in (proc.stdout or ""):
        return True
    if log:
        err = ((proc.stderr or "") + (proc.stdout or "")).strip().replace("\n", " ")
        log("Уведомление (%s): не показалось, код %s, ответ: %s"
            % (label, proc.returncode, err[:400] or "(пусто)"), "WARN")
    return False


def _telegram_credentials(cfg, log=None):
    """
    Токен и чат для Telegram. Сначала ключи telegram_bot_token /
    telegram_chat_id из meet_config.json; если пусто и задан tg_config_path -
    берём report_bot_token / report_chat_id из того JSON. Ничего не задано -
    (None, None), канал просто не используется.
    """
    token = str(cfg.get("telegram_bot_token") or "").strip()
    chat = cfg.get("telegram_chat_id")
    if token and chat:
        return token, chat
    tg_conf = str(cfg.get("tg_config_path") or "").strip()
    if not tg_conf:
        return None, None
    try:
        with open(tg_conf, encoding="utf-8") as fh:
            tg = json.load(fh)
    except (OSError, ValueError) as exc:
        if log:
            log("Уведомление (telegram): не прочитал %s - %r" % (tg_conf, exc), "WARN")
        return None, None
    token = (tg.get("report_bot_token") or "").strip()
    chat = tg.get("report_chat_id")
    if not token or not chat:
        if log:
            log("Уведомление (telegram): в %s нет report_bot_token "
                "или report_chat_id" % tg_conf, "WARN")
        return None, None
    return token, chat


def _telegram_configured(cfg):
    return bool(str(cfg.get("telegram_bot_token") or "").strip()
                and cfg.get("telegram_chat_id")) \
        or bool(str(cfg.get("tg_config_path") or "").strip())


def _telegram(cfg, title, text, log=None):
    """Сообщение в Telegram-бот. Токен и чат - см. _telegram_credentials."""
    token, chat = _telegram_credentials(cfg, log)
    if not token or not chat:
        return False
    body = urllib.parse.urlencode({
        "chat_id": str(chat),
        "text": "%s\n%s" % (title, text),
        "disable_notification": "false",
    }).encode()
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    try:
        with urllib.request.urlopen(url, data=body, timeout=20) as resp:
            ok = json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception as exc:  # noqa: BLE001 - сеть, VPN, что угодно
        if log:
            log("Уведомление (telegram): не отправилось - %r" % (exc,), "WARN")
        return False
    if not ok and log:
        log("Уведомление (telegram): Telegram ответил ok=false", "WARN")
    return ok


def _signal_sync(cfg, title, text, log):
    channels = []
    if cfg.get("notify_toast", True):
        channels.append(("toast", lambda: _run_ps(_PS_TOAST, title, text, 25, log, "toast")))
    if cfg.get("notify_balloon", True):
        channels.append(("balloon", lambda: _run_ps(_PS_BALLOON, title, text, 30, log, "balloon")))
    if cfg.get("notify_telegram", True) and _telegram_configured(cfg):
        channels.append(("telegram", lambda: _telegram(cfg, title, text, log)))

    worked = []
    for name, fn in channels:
        try:
            if fn():
                worked.append(name)
        except Exception as exc:  # noqa: BLE001 - канал не должен ронять остальные
            if log:
                log("Уведомление (%s): неожиданный сбой %r" % (name, exc), "WARN")
    if log:
        if worked:
            log("Уведомление доставлено через: %s" % ", ".join(worked))
        else:
            log("Уведомление НЕ доставлено ни одним каналом. Запись это никак "
                "не затронуло, смотри строки выше - там причина по каждому "
                "каналу.", "ERROR")
    return worked


def signal(cfg, title, text, log=None):
    """
    Разослать уведомление по всем каналам. Возвращается сразу: работа уходит
    в фоновый поток, чтобы остановка записи не ждала ни PowerShell, ни сеть.
    """
    t = threading.Thread(target=_signal_sync, args=(cfg, title, text, log),
                         name="notify", daemon=True)
    t.start()
    return t


# Обратная совместимость: старое имя toast() продолжает работать, но теперь
# это полноценная рассылка по каналам.
def toast(title, text, log=None, cfg=None):
    signal(cfg or {}, title, text, log=log)
    return True


# --- файл-маркер ---------------------------------------------------------

def marker_path(recordings_dir):
    return os.path.join(recordings_dir, MARKER_NAME)


def marker_on(recordings_dir, folder, started_at, trigger_app, log=None):
    path = marker_path(recordings_dir)
    body = (
        "ИДЁТ ЗАПИСЬ СОЗВОНА\r\n"
        "\r\n"
        "Начата:   %s\r\n"
        "Триггер:  %s\r\n"
        "Папка:    %s\r\n"
        "\r\n"
        "Этот файл существует, только пока запись идёт, и исчезает сам,\r\n"
        "когда запись закрыта. Удалять его руками не нужно: он не управляет\r\n"
        "записью, а только показывает её состояние.\r\n"
        "\r\n"
        "Остановить запись досрочно: stop_now.bat\r\n"
    ) % (started_at.strftime("%Y-%m-%d %H:%M:%S"), trigger_app, folder)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return True
    except OSError as exc:
        if log:
            log("Маркер %s не создался: %r (на запись не влияет)"
                % (MARKER_NAME, exc), "WARN")
        return False


def marker_off(recordings_dir, log=None):
    path = marker_path(recordings_dir)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError as exc:
        if log:
            log("Маркер %s не удалился: %r - удалите вручную, он ни на что "
                "не влияет" % (MARKER_NAME, exc), "WARN")
    return False


def clear_stale_marker(recordings_dir, log=None):
    path = marker_path(recordings_dir)
    if not os.path.exists(path):
        return False
    if log:
        log("Найден маркер %s от предыдущего запуска: прошлый процесс завершился "
            "в середине записи. Проверьте последнюю папку в recordings - в ней "
            "может не быть meta.json." % MARKER_NAME, "WARN")
    marker_off(recordings_dir, log=log)
    return True


# --- самопроверка --------------------------------------------------------

def self_test(cfg=None, log=None):
    """
    Прогнать все каналы и сказать, какие реально работают.
    Запускается из check.bat и из notify_test.bat.
    """
    cfg = cfg or {}
    printer = log or (lambda m, lvl="INFO": print("[%s] %s" % (lvl, m)))
    printer("Проверка каналов уведомлений. Смотри на экран и в Telegram.")
    worked = _signal_sync(cfg, "ClaudeMeet: проверка сигнала",
                          "Если ты это видишь - канал работает.", printer)
    printer("Работают каналы: %s" % (", ".join(worked) if worked else "НИ ОДИН"))
    return worked


if __name__ == "__main__":
    self_test()
