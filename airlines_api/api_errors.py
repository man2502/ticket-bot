"""Строка журнала ошибок — один формат на весь бот.

    [err] <тип> | HTTP <статус> | <запрос> | <подробности>

Панель вытаскивает такие строки из вывода процесса в отдельную таблицу и строит
по ним сводку «какая ошибка, у какого аккаунта, сколько раз». Раньше ошибки
были разбросаны по десятку форматов (`[error]`, `[auth]`, `[--] …`), и понять,
что именно происходило с аккаунтом перед блокировкой, можно было только
читая лог глазами.

Типы — закрытый список: по свободному тексту сводку не построить.
"""

from __future__ import annotations

import re

__all__ = ["ERROR_KEYS", "report_error", "classify_error_text"]

ERROR_KEYS = {
    "rate_limit": "лимит запросов (429)",
    "rate_limit_stop": "стоп после повторных 429",
    "auth_login": "вход не удался",
    "auth_forbidden": "доступ запрещён (401/403)",
    "error_search": "Error Search / hitit",
    "captcha_failed": "капча не принята",
    "maintenance": "профилактика на сайте",
    "network": "сеть / таймаут",
    "http_5xx": "ошибка сервера (5xx)",
    "booking_failed": "бронь не удалась",
    "booking_unknown": "бронь под вопросом",
    "account_blocked": "аккаунт заблокирован (450)",
    "unknown_stop": "стоп: неопознанная ошибка подряд",
    "other": "прочее",
}


def _clean(text: object, limit: int = 300) -> str:
    """Одна строка без разделителя формата: «|» внутри подробностей сломал бы разбор."""
    line = " ".join(str(text or "").split()).replace("|", "/")
    return line if len(line) <= limit else line[:limit] + "…"


def report_error(key: str, *, detail: object = "", status: int | None = None,
                 endpoint: str = "") -> None:
    """Напечатать строку журнала ошибок. Неизвестный тип становится `other`."""
    key = key if key in ERROR_KEYS else "other"
    endpoint = endpoint or endpoint_from_text(detail)
    status = status or status_from_text(detail)
    status_part = f"HTTP {status}" if status else "—"
    print(f"[err] {key} | {status_part} | {_clean(endpoint) or '—'} | {_clean(detail)}")


_ENDPOINT_RE = re.compile(r"/api/v1/([\w/.-]+)")
# Итог цикла: «GetAirAvailability → HTTP 450: {…}» (api_check.failure_hint).
_ARROW_RE = re.compile(r"^([\w/.+-]+) → HTTP \d{3}\b")


def endpoint_from_text(text: object) -> str:
    """Запрос из текста ошибки: «POST /api/v1/GetAirAvailability (…)» → GetAirAvailability."""
    raw = str(text or "")
    match = _ENDPOINT_RE.search(raw) or _ARROW_RE.match(raw)
    return match.group(1) if match else ""


_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def status_from_text(text: object) -> int | None:
    """Код ответа из подсказки цикла: «Error Search (HTTP 400)» → 400."""
    match = _STATUS_RE.search(str(text or ""))
    return int(match.group(1)) if match else None


def classify_error_text(text: str) -> str | None:
    """
    Тип ошибки по тексту неудачного цикла. None — это не ошибка (мест просто нет).

    Обычный исход «мест нет» сюда не попадает: вызывающий код отсекает его
    раньше. Всё прочее, что не опознано, — `other`, а не молчание: неизвестная
    ошибка в сводке полезнее, чем никакой.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return None
    if "too many requests" in raw or "http 429" in raw or " 429" in raw:
        return "rate_limit"
    if "профилакт" in raw or "временно не работает" in raw:
        return "maintenance"
    if "error search" in raw or "hitit_error" in raw:
        return "error_search"
    if "captcha failed" in raw or "капча не" in raw:
        return "captcha_failed"
    if "forbidden" in raw or "403" in raw or "401" in raw:
        return "auth_forbidden"
    if any(word in raw for word in ("сеть", "timeout", "timed out", "таймаут", "connection")):
        return "network"
    if any(code in raw for code in ("http 500", "http 502", "http 503", "http 504", " 5xx")):
        return "http_5xx"
    return "other"
