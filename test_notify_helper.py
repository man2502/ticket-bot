"""Тесты уведомлений бота: когда письмо шлёт он сам, а когда — панель.

Запуск (из корня репозитория):
    python run_tests.py test_notify_helper.py
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import notify_helper


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Убрать из окружения всё, что влияет на решение «слать или нет»."""
    for name in (
        "NOTIFY_EXTERNAL",
        "GMAIL_USER",
        "SMTP_USER",
        "GMAIL_APP_PASSWORD",
        "SMTP_PASSWORD",
        "NOTIFY_EMAIL",
        "GMAIL_TO",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _configure_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_USER", "bot@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "aaaa bbbb cccc dddd")
    monkeypatch.setenv("NOTIFY_EMAIL", "me@gmail.com")


def test_flag_is_off_by_default(clean_env) -> None:
    """Запуск из командной строки: почтой занимается сам бот."""
    assert notify_helper.notifications_delegated() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_flag_recognises_true_values(clean_env, value: str) -> None:
    clean_env.setenv("NOTIFY_EXTERNAL", value)
    assert notify_helper.notifications_delegated() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_flag_recognises_false_values(clean_env, value: str) -> None:
    clean_env.setenv("NOTIFY_EXTERNAL", value)
    assert notify_helper.notifications_delegated() is False


def test_configured_gmail_is_ignored_when_delegated(clean_env) -> None:
    """
    Даже полностью настроенная почта молчит под флагом.

    Иначе при запуске из панели человек получил бы два письма об одной броне:
    одно от бота, другое от панели.
    """
    _configure_gmail(clean_env)
    assert notify_helper.gmail_configured() is True

    clean_env.setenv("NOTIFY_EXTERNAL", "1")
    assert notify_helper.gmail_configured() is False


def test_delegated_send_is_silent(clean_env) -> None:
    """
    Ради этого всё и затевалось: под флагом в лог не должно попасть ни слова
    про ненастроенный Gmail. Раньше эта строка пугала при каждом запуске.
    """
    clean_env.setenv("NOTIFY_EXTERNAL", "1")

    err, out = io.StringIO(), io.StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        sent = notify_helper.notify_gmail("бронь", "ссылка на оплату")

    assert sent is False
    assert err.getvalue() == ""
    assert out.getvalue() == ""


def test_warning_still_shown_without_flag(clean_env) -> None:
    """Без флага ботом всё-таки занимается человек — молчать нельзя."""
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        sent = notify_helper.notify_gmail("бронь", "ссылка на оплату")

    assert sent is False
    assert "Gmail не настроен" in err.getvalue()


def test_notify_does_not_open_smtp_when_delegated(clean_env) -> None:
    """
    `notify()` под флагом печатает в консоль, но SMTP не трогает.

    Печать нужна: именно её строку панель ловит из stdout и показывает в логе.
    """
    _configure_gmail(clean_env)
    clean_env.setenv("NOTIFY_EXTERNAL", "1")

    def _fail(*args, **kwargs):  # pragma: no cover — тест падает, если вызовут
        raise AssertionError("SMTP не должен подниматься при NOTIFY_EXTERNAL=1")

    clean_env.setattr(notify_helper.smtplib, "SMTP", _fail)

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        notify_helper.notify("бронь", "ссылка на оплату", wait_gmail=True)
        notify_helper.flush_notifications(timeout=5.0)

    assert "ссылка на оплату" in out.getvalue()
