"""Уведомления: macOS + Gmail (SMTP)."""

from __future__ import annotations

import smtplib
import subprocess
import sys
import threading
import time
from email.mime.text import MIMEText
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.env import env  # noqa: E402


# Реализация общая — core.env (там же описано, почему пустое значение = default).


_PENDING: list[threading.Thread] = []
_PENDING_LOCK = threading.Lock()


def notify_macos(title: str, body: str) -> None:
    if sys.platform != "darwin":
        return
    # argv надёжнее, чем -e со строкой: URL/кавычки ломают AppleScript
    script = (
        "on run argv\n"
        "  set bodyText to item 1 of argv\n"
        "  set titleText to item 2 of argv\n"
        '  display notification bodyText with title titleText sound name "Glass"\n'
        "end run"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, "--", body or title, title],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] macOS notify: {exc}", file=sys.stderr)


def _notify_emails() -> list[str]:
    """Список получателей из NOTIFY_EMAIL / GMAIL_TO (через запятую или ;)."""
    raw = env("NOTIFY_EMAIL") or env("GMAIL_TO") or env("GMAIL_USER") or env("SMTP_USER")
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(";", ",").split(","):
        addr = chunk.strip()
        if addr and "@" in addr and addr not in parts:
            parts.append(addr)
    return parts


def _gmail_password() -> str:
    """App password: пробелы из Google UI убираем (16 символов)."""
    raw = env("GMAIL_APP_PASSWORD") or env("SMTP_PASSWORD")
    return raw.replace(" ", "").replace("\t", "")


def gmail_configured() -> bool:
    user = env("GMAIL_USER") or env("SMTP_USER")
    password = _gmail_password()
    return bool(user and password and _notify_emails())


def notify_gmail(subject: str, body: str) -> bool:
    """
    Gmail через SMTP — сразу на все адреса из NOTIFY_EMAIL.
    Нужны в .env:
      GMAIL_USER=you@gmail.com
      GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
      NOTIFY_EMAIL=a@gmail.com, b@gmail.com, c@gmail.com
    """
    user = env("GMAIL_USER") or env("SMTP_USER")
    password = _gmail_password()
    recipients = _notify_emails()
    host = env("SMTP_HOST") or "smtp.gmail.com"
    port = int(env("SMTP_PORT") or "587")

    if not user or not password or not recipients:
        print(
            "[notify] Gmail не настроен: нужны GMAIL_USER + GMAIL_APP_PASSWORD + NOTIFY_EMAIL",
            file=sys.stderr,
        )
        return False

    msg = MIMEText(body or subject, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(user, password)
            smtp.sendmail(user, recipients, msg.as_string())
        print(f"[notify] Gmail: отправлено → {', '.join(recipients)}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Gmail НЕ отправлен: {exc}", file=sys.stderr)
        print(
            "[notify] Проверь: App Password в .env.local, 2FA в Google, сеть до smtp.gmail.com",
            file=sys.stderr,
        )
        return False


def notify(title: str, body: str = "", *, wait_gmail: bool = False) -> bool:
    """
    Печать + macOS сразу.

    wait_gmail=True — письмо синхронно. Использовать ТОЛЬКО вне гонки: SMTP-коннект
    к Gmail это 1-3 секунды в норме и до 30 при затыке сети, и раньше эти секунды
    тратились ровно в момент появления мест, до попытки брони.

    По умолчанию письмо уходит в фоне; перед выходом из процесса позови
    `flush_notifications()`, иначе демон-поток умрёт вместе с процессом.
    """
    text = f"{title}\n{body}".strip() if body else title
    print(f"\n*** {text}\n")
    notify_macos(title, body or title)

    if wait_gmail:
        return notify_gmail(title, body or title)

    def _bg() -> None:
        notify_gmail(title, body or title)

    thread = threading.Thread(target=_bg, daemon=True, name="notify-gmail")
    with _PENDING_LOCK:
        _PENDING[:] = [t for t in _PENDING if t.is_alive()]
        _PENDING.append(thread)
    thread.start()
    return gmail_configured()


def flush_notifications(timeout: float = 30.0) -> None:
    """Дождаться фоновых писем перед выходом (демон-потоки умирают с процессом)."""
    with _PENDING_LOCK:
        threads = [t for t in _PENDING if t.is_alive()]
    if not threads:
        return
    deadline = time.monotonic() + timeout
    print(f"[notify] жду отправку писем ({len(threads)})…")
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)
    still = [t for t in threads if t.is_alive()]
    if still:
        print(f"[warn] {len(still)} письмо(писем) не успело уйти за {timeout:.0f}с", file=sys.stderr)
