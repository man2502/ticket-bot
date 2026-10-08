#!/usr/bin/env python3
"""
Монитор рейсов T5 через API.

Каждый цикл как в ticket-parser:
1. GET  /api/v1/customers/info — ping auth (тело 200 не читаем)
2. GET  /api/v1/captcha/finding — картинка + key
3. POST /api/v1/GetAirAvailability — с key и captcha_api

401 на info: token_type=client|customer → login только этого токена.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_ROOT = ROOT
REPO_ROOT = ROOT.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from core.bootstrap import bootstrap

# Перейти на python из .venv и включить UTF-8 в консоли — до первого print.
bootstrap(__file__)

import argparse
import base64
import datetime as dt
import json
import os
import random
import re
import time
from typing import Any

from core.open_file import open_in_viewer
from core.term import cyan, green, purple, red
from core.slots import (
    CHECK_PERIODS_SEC,
    is_slot_minute,
    normalize_check_period,
    now_second,
    precise_sleep,
    slot_anchors,
    slot_minute_stride,
)
from core.booking_guard import BookingGuard
from core.state import load_json, save_json
from api_client import RequestFailed, ResultUnknown, api_request, warmup_connection
from api_auth import (
    AuthFatalError,
    AuthTransientError,
    auto_refresh_tokens_enabled,
    ensure_api_session,
    ensure_live_auth,
    refresh_api_tokens,
    refresh_credentials_configured,
    refresh_customer_token,
    tokens_configured,
)
from api_env import (
    api_build_version,
    api_secrets_env_path,
    build_api_headers,
    current_ios_user_agent,
    env,
    ios_user_agent_rotate_in_min,
    load_api_env,
    to_api_date,
)
from api_session import is_auth_forbidden, print_auth_help
from api_rate_limit import RATE_LIMIT_EXIT_CODE, RateLimitStop, wait_if_paused
from api_errors import classify_error_text, report_error
from api_booking import (
    bookings_journal,
    BookingResult,
    auto_book_enabled,
    count_direct_flights,
    evaluate_api_availability,
    flight_selection_label,
    try_auto_book,
)
from notify_helper import (
    flush_notifications,
    gmail_configured,
    notifications_delegated,
    notify,
)
from passenger_loader import format_passenger_names, get_passenger_counts_by_type

STATE_FILE = API_ROOT / "api_state.json"
CAPTCHA_IMAGE_PATH = API_ROOT / "api_captcha_last.png"


def state_file() -> Path:
    """Состояние монитора: общий файл или свой (API_STATE_FILE) для аккаунта."""
    own = env("API_STATE_FILE")
    return Path(own) if own else STATE_FILE


def captcha_image_path() -> Path:
    """
    Куда класть последнюю капчу. Панель даёт каждому запуску свой файл:
    иначе один процесс открывал бы и решал картинку другого.
    """
    own = env("API_CAPTCHA_IMAGE")
    return Path(own) if own else CAPTCHA_IMAGE_PATH


# Палитра общая (core.term): раньше пять модулей держали свои копии ANSI-кодов,
# и оттенки успели разъехаться. Плюс core.term не красит вывод, если он идёт
# не в терминал, а в файл — иначе лог наполняется escape-мусором.
_purple, _cyan, _green, _red = purple, cyan, green, red


def cnn_use_enabled() -> bool:
    return (env("CAPTCHA_API_USE_CNN") or "1").strip().lower() not in ("0", "false", "no")


def compare_captcha_enabled() -> bool:
    return (env("CAPTCHA_API_COMPARE", "1") or "1").strip().lower() not in ("0", "false", "no")


def api_delay_enabled() -> bool:
    """API_DELAY=1 — jitter и паузы капчи. API_DELAY=0 — всё сразу в слот."""
    return (env("API_DELAY") or "0").strip().lower() not in ("0", "false", "no", "")


def request_jitter_sec() -> float:
    """Случайная пауза между API-запросами: 0 / 0.1 / 0.2 сек."""
    if not api_delay_enabled():
        return 0.0
    return random.choice((0.0, 0.1, 0.2))


def sleep_jitter() -> None:
    delay = request_jitter_sec()
    if delay > 0:
        time.sleep(delay)


def captcha_wrong_delay_sec() -> float:
    """Пауза перед повтором после неверной капчи: 2 / 3 / 4 сек."""
    if not api_delay_enabled():
        return 0.0
    return float(random.choice((2, 3, 4)))


def captcha_search_delay_sec() -> float:
    """Пауза после решения капчи перед GetAirAvailability: 1.5–2 сек."""
    if not api_delay_enabled():
        return 0.0
    return random.uniform(1.5, 2.0)


def sleep_before_captcha_search() -> None:
    delay = captcha_search_delay_sec()
    if delay > 0:
        time.sleep(delay)


def captcha_max_attempts() -> int:
    if manual_captcha_enabled():
        try:
            return max(1, int(env("CAPTCHA_API_MAX_ATTEMPTS", "5") or "5"))
        except ValueError:
            return 5
    try:
        return max(1, int(env("CAPTCHA_MAX_ATTEMPTS", "5") or "5"))
    except ValueError:
        return 5


def interval_sec() -> int:
    try:
        return max(1, int(env("INTERVAL_SEC", "60") or "60"))
    except ValueError:
        return 60


SLOT_TICK_SEC = 0.1


def slot_offsets(lo: int, hi: int) -> list[float]:
    """0.0, 0.1, …, 1.0 или 30.0, 30.1, …, 31.0."""
    start = float(lo)
    end = float(hi)
    steps = int(round((end - start) / SLOT_TICK_SEC))
    return [round(start + i * SLOT_TICK_SEC, 1) for i in range(steps + 1)]


def format_slot_time(offset: float) -> str:
    return f":{offset:04.1f}"


def env_on(name: str, default: str = "1") -> bool:
    return (env(name, default) or default).strip().lower() not in ("0", "false", "no", "")


def first_slot_second() -> int:
    """Секунда первого окна: API_CHECK_AT_SEC, 0–59."""
    try:
        return max(0, min(59, int(env("API_CHECK_AT_SEC", "0") or "0")))
    except ValueError:
        return 0


def check_period_sec() -> int:
    """
    Как часто проверять: 10 / 30 / 60 / 120 секунд (API_CHECK_PERIOD_SEC).

    Если период не задан, читаем старые флаги API_CHECK_EVERY_00/30 — они
    лежат в готовых .env.local и означают ровно «дважды в минуту» либо
    «раз в минуту». Ломать чужие файлы ради нового имени незачем.
    """
    raw = env("API_CHECK_PERIOD_SEC")
    if raw:
        return normalize_check_period(raw)

    first = env_on("API_CHECK_EVERY_00", "1")
    second = env_on("API_CHECK_EVERY_30", "1")
    if first and second:
        return 30
    if first or second:
        return 60
    raise SystemExit(
        "Нет слотов: задай API_CHECK_PERIOD_SEC "
        f"({'/'.join(str(p) for p in CHECK_PERIODS_SEC)}) в .env.local"
    )


def check_at_second_ranges() -> list[tuple[int, int]]:
    """
    Окна проверки: по секунде на каждую отметку периода.

    Раньше окна были зашиты (:00 и :30) и включались двумя флагами. Теперь
    отметки считает `slot_anchors`, поэтому появились 10 и 120 секунд,
    а первое окно можно сдвинуть с нуля.

    Окно шириной в секунду оставлено как было: внутри него бот стреляет
    в случайную десятую долю, чтобы запросы не ложились в одну и ту же долю.
    """
    period = check_period_sec()
    # Второй флаг раньше выключал именно окно :30. Если период взят из старых
    # флагов и включено только одно окно — уважаем, какое именно.
    if not env("API_CHECK_PERIOD_SEC") and period == 60 and not env_on("API_CHECK_EVERY_00", "1"):
        return [(30, 31)]
    return [(anchor, anchor + 1) for anchor in slot_anchors(period, first_slot_second())]


def check_at_second_range() -> tuple[int, int]:
    return check_at_second_ranges()[0]


def format_slot_ranges() -> str:
    parts: list[str] = []
    for lo, hi in check_at_second_ranges():
        if lo == hi:
            parts.append(format_slot_time(float(lo)))
        else:
            parts.append(f"{format_slot_time(float(lo))}–{format_slot_time(float(hi))}")
    return ", ".join(parts)


def check_at_second() -> int:
    """Секунда слота (середина первого диапазона)."""
    lo, hi = check_at_second_range()
    return (lo + hi) // 2


def _window_ticks(lo: int, hi: int) -> list[float]:
    """Отметки внутри окна: 0.0, 0.1, … 1.0 — точки, в которые можно стрельнуть."""
    return slot_offsets(lo, hi)


def next_check_slot() -> tuple[float, tuple[int, int], float]:
    """
    Ближайшее окно и случайная отметка внутри него: (offset, окно, секунд ждать).

    Секунда берётся с долями (`now_second`), а не через `datetime.now().second`.
    Старый расчёт выбрасывал микросекунды, поэтому запрос стабильно уходил
    в среднем на полсекунды позже намеченной отметки — в гонке это проигрыш.

    Перебор идёт на `stride` минут вперёд. Для периодов до минуты stride=1
    и поведение прежнее — эта минута и следующая. Для 120 секунд нерабочие
    минуты пропускаются, иначе бот стрелял бы каждую минуту вместо каждой второй.
    """
    now = dt.datetime.now()
    current = now_second(now)
    period = check_period_sec()
    stride = slot_minute_stride(period)
    windows = check_at_second_ranges()
    best: tuple[float, float, tuple[int, int]] | None = None  # (delay, offset, окно)

    for minutes_ahead in range(stride + 1):
        if not is_slot_minute(period, now + dt.timedelta(minutes=minutes_ahead)):
            continue
        base = minutes_ahead * 60.0
        for lo, hi in windows:
            ticks = [t + base for t in _window_ticks(lo, hi) if t + base > current]
            if not ticks:
                continue
            # Сравниваем окна по ближайшей доступной отметке, а стреляем
            # в случайную из оставшихся — чтобы не бить в одну и ту же долю секунды.
            earliest = ticks[0] - current
            chosen = random.choice(ticks)
            if best is None or earliest < best[0]:
                best = (earliest, chosen, (lo, hi))

    if best is None:
        # Сюда попасть нельзя: перебрано stride+1 минут, а рабочая среди них есть
        # всегда. Оставлено, чтобы опечатка в расписании не роняла монитор.
        return 0.0, check_at_second_range(), max(0.0, 60.0 - current)

    _earliest, offset, window = best
    return offset % 60.0, window, max(0.0, offset - current)


# За сколько секунд до слота поднимать TCP+TLS, чтобы не платить handshake в момент запроса.
WARMUP_LEAD_SEC = 1.0


def wait_for_check_slot() -> float:
    """
    Ждём ближайший слот — случайно :00.0…:01.0 или :30.0…:31.0 с шагом 0.1.

    За секунду до отметки поднимаем соединение: handshake это 2-3 RTT, и делать
    его в момент слота значит дарить конкурентам сотни миллисекунд.
    """
    offset, (lo, hi), until = next_check_slot()
    range_label = (
        format_slot_time(float(lo))
        if lo == hi
        else f"{format_slot_time(float(lo))}–{format_slot_time(float(hi))}"
    )
    print(
        f"[slot] жду {format_slot_time(offset)} ({range_label}) "
        f"(≈{until:.2f}с до запроса)…"
    )
    deadline = time.monotonic() + until
    if until > WARMUP_LEAD_SEC:
        precise_sleep(until - WARMUP_LEAD_SEC)
    warmup_connection()
    precise_sleep(max(0.0, deadline - time.monotonic()))
    return until


def load_state() -> dict:
    """Состояние монитора. Битый файл не роняет запуск — уезжает в .corrupt."""
    data = load_json(state_file(), {})
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    """Атомарная запись: truncate+write мог оставить обрезанный файл после Ctrl-C."""
    save_json(state_file(), state)


def route_line(date_str: str | None = None) -> str:
    depart = env("DEPART_PORT_CODE")
    arrival = env("ARRIVAL_PORT_CODE")
    date = (date_str or env("DATE") or "").strip()
    return f"{depart} → {arrival}, {date}"


def _retry_after_token_refresh(headers: dict[str, str]) -> bool:
    if not auto_refresh_tokens_enabled():
        return False
    ensure_live_auth(headers, env_path=api_secrets_env_path())
    print("[auth] токены обновлены — повтор запроса")
    return True


def _token_preview(raw: str, *, n: int = 12) -> str:
    text = (raw or "").replace("Bearer ", "").replace("bearer ", "").strip()
    if not text:
        return "(empty)"
    return f"{text[:n]}…"


def _retry_after_search_error(headers: dict[str, str]) -> bool:
    """Error Search / hitit — обновить customer token и повторить поиск."""
    if not auto_refresh_tokens_enabled() or not refresh_credentials_configured():
        print("[auth] Error Search — refresh выключен или нет credentials")
        return False
    old_customer = env("API_CUSTOMER_AUTHORIZATION")
    print(f"[auth] customer token до refresh: {_token_preview(old_customer)}")
    try:
        refresh_customer_token(env_path=api_secrets_env_path())
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] customer refresh не удался ({exc}) — полный re-login")
        refresh_api_tokens(env_path=api_secrets_env_path())
    headers.clear()
    headers.update(build_api_headers())
    new_customer = env("API_CUSTOMER_AUTHORIZATION")
    if old_customer == new_customer:
        print(
            "[auth] customer token не изменился после refresh — "
            "ошибка может быть не в токене (дата, маршрут, сессия)"
        )
    else:
        print(f"[auth] customer token обновлён: {_token_preview(new_customer)}")
    print(
        "[auth] следующий запрос с x-customer-authorization: "
        f"{_token_preview(headers.get('x-customer-authorization', ''))}"
    )
    print("[auth] повтор поиска")
    return True


def _handle_auth_forbidden(
    headers: dict[str, str],
    depart_date: str,
    *,
    verbose: bool,
    allow_token_refresh: bool,
    error_tracker: RepeatedErrorTracker | None = None,
) -> tuple[bool, list[str], dict[str, Any] | str | None]:
    # Раньше подсказку печатал транспорт на каждый 401/403, в том числе на те,
    # которые тут же чинились refresh-ом. Теперь — один раз, по месту обработки.
    print_auth_help()
    if allow_token_refresh:
        try:
            if _retry_after_token_refresh(headers):
                return run_check_once(
                    headers,
                    depart_date,
                    verbose=verbose,
                    allow_token_refresh=False,
                    error_tracker=error_tracker,
                )
        except AuthTransientError as exc:
            return False, [str(exc)], None
        except AuthFatalError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuthFatalError(f"не удалось обновить токены: {exc}") from exc
    raise AuthFatalError("403/forbidden после попытки обновить токены — монитор остановлен")


def build_search_payload(*, depart_date: str, captcha_key: str = "", captcha_api: str = "") -> dict[str, str]:
    depart = env("DEPART_PORT_CODE")
    arrival = env("ARRIVAL_PORT_CODE")
    if not depart or not arrival:
        raise RuntimeError("DEPART_PORT_CODE и ARRIVAL_PORT_CODE обязательны в check.env")

    date_raw = env("DATE")
    if date_raw:
        os.environ.setdefault("DATES", date_raw.split(",")[0].strip())

    adult, child, infant = get_passenger_counts_by_type(verbose=False)

    payload = {
        "tripType": "ow",
        "resident": "1",
        "departPort": depart.upper(),
        "arrivalPort": arrival.upper(),
        "departDate": depart_date,
        "adult": str(adult),
        "child": str(child),
        "infant": str(infant),
        "typeFlight": "1",
    }
    if captcha_key:
        payload["key"] = captcha_key
    if captcha_api:
        payload["captcha_api"] = captcha_api
    return payload


def captcha_accepted(status: int, body: dict[str, Any] | str) -> bool:
    """Капча принята сервером (есть данные или явный success)."""
    if not isinstance(body, dict):
        return False
    if body.get("success"):
        return True
    msg = str(body.get("message") or "").lower()
    if "successfully" in msg:
        return True
    # иногда data — строка успеха
    data = body.get("data")
    if isinstance(data, str) and "successfully" in data.lower():
        return True
    return False


def is_captcha_required(status: int, body: dict[str, Any] | str) -> bool:
    """Нужна (новая) капча — не отправлена или неверная."""
    if captcha_accepted(status, body):
        return False
    if is_hitit_search_error(status, body):
        return False
    if status == 402:
        return True
    if isinstance(body, dict):
        msg = str(body.get("message") or "").lower()
        data = str(body.get("data") or "").lower()
        return "captcha" in msg or "captcha" in data or "invalid" in msg
    return False


def is_maintenance_error(status: int, body: dict[str, Any] | str) -> bool:
    """Сервис на профилактике — refresh токена не поможет."""
    if not isinstance(body, dict):
        return False
    text = f"{body.get('message') or ''} {body.get('data') or ''}".lower()
    return "профилакт" in text or "временно не работает" in text


def is_rate_limited(status: int, body: dict[str, Any] | str) -> bool:
    """HTTP 429 / HTML Too Many Requests — лимит запросов на сервере."""
    if status == 429:
        return True
    if isinstance(body, str):
        return "too many requests" in body.lower()
    return False


def handle_rate_limited(
    *,
    status: int,
    body: dict[str, Any] | str,
    retry_after: float | None,
    label: str,
) -> bool:
    """
    True — был 429, нужен новый цикл (свежая капча).

    Пауза здесь больше не выдерживается: её уже выдержал api_call по общей
    лестнице (core.rate_limit). Вторая пауза поверх подняла бы ступень дважды.
    """
    if not is_rate_limited(status, body):
        return False
    _log_api_response(label=label, status=status, body=body)
    return True


def api_request_resilient(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    label: str,
) -> tuple[int, dict[str, Any] | str]:
    """api_request с паузой при 429; не съедает попытки капчи."""
    while True:
        status, parsed, retry_after = api_request(
            method,
            path,
            headers=headers,
            body=body,
        )
        if not handle_rate_limited(
            status=status,
            body=parsed,
            retry_after=retry_after,
            label=label,
        ):
            return status, parsed


def is_hitit_search_error(status: int, body: dict[str, Any] | str) -> bool:
    """
    Error Search / hitit_error.Internal Error — протухший customer token.
    HTTP 400 после капчи или GetAirAvailability.
    """
    if not isinstance(body, dict) or body.get("success"):
        return False
    msg = str(body.get("message") or "").strip().lower()
    data = str(body.get("data") or "").strip().lower()
    if "error search" in msg:
        return True
    if "hitit_error" in data:
        return True
    if status == 400 and "internal error" in data:
        return True
    return False


def search_error_max_retries() -> int:
    try:
        return max(1, int(env("API_SEARCH_ERROR_MAX_RETRIES", "3") or "3"))
    except ValueError:
        return 3


class RepeatedErrorTracker:
    """Подряд идущие одинаковые ошибки (сброс при успешном цикле или другой ошибке)."""

    def __init__(self) -> None:
        self._last_key: str | None = None
        self._streak: int = 0

    def note(self, key: str) -> int:
        if key == self._last_key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1
        return self._streak

    def clear(self) -> None:
        self._last_key = None
        self._streak = 0

    def reset_streak(self) -> None:
        self.clear()


def repeated_error_pause_after() -> int:
    try:
        return max(1, int(env("API_REPEATED_ERROR_PAUSE_AFTER", "3") or "3"))
    except ValueError:
        return 3


def maintenance_pause_sec() -> float:
    try:
        lo = float(env("API_MAINTENANCE_PAUSE_MIN", "120") or "120")
        hi = float(env("API_MAINTENANCE_PAUSE_MAX", "240") or "240")
    except ValueError:
        lo, hi = 120.0, 240.0
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def pause_for_maintenance(body: dict[str, Any] | str | None = None) -> None:
    delay = maintenance_pause_sec()
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("data") or body.get("message") or "").strip()
    elif isinstance(body, str):
        detail = body.strip()
    mins = delay / 60.0
    now = time.strftime("%H:%M:%S")
    print(
        f"[maintenance] {now} {detail or 'сервис недоступен'} — "
        f"пауза {mins:.1f} мин ({delay:.0f}с)"
    )
    time.sleep(delay)


def repeated_error_pause_sec() -> float:
    try:
        lo = float(env("API_REPEATED_ERROR_PAUSE_MIN", "60") or "60")
        hi = float(env("API_REPEATED_ERROR_PAUSE_MAX", "90") or "90")
    except ValueError:
        lo, hi = 60.0, 90.0
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def error_key_from_response(status: int, body: dict[str, Any] | str) -> str | None:
    if is_rate_limited(status, body):
        return "rate_limit"
    if is_maintenance_error(status, body):
        return "maintenance"
    if is_hitit_search_error(status, body):
        return "error_search"
    if isinstance(body, dict) and not body.get("success"):
        msg = str(body.get("message") or "").strip().lower()
        data = str(body.get("data") or "").strip().lower()
        if msg or data:
            return f"http{status}:{msg}:{data}"[:160]
    return None


def _is_normal_no_seats_hint(text: str) -> bool:
    """Успешный ответ API без мест — не ошибка для backoff."""
    raw = (text or "").strip().lower()
    if not raw:
        return True
    if "error search" in raw or "hitit_error" in raw:
        return False
    if "captcha failed" in raw or "forbidden" in raw:
        return False
    if "seat=0" in raw or "нет доступных" in raw or "strict:" in raw:
        return True
    if "нет рейсов" in raw:
        return True
    if "мест нет" in raw and "error" not in raw:
        return True
    if raw.startswith("success"):
        return True
    return False


def error_key_from_text(text: str) -> str | None:
    raw = (text or "").strip().lower()
    if not raw or _is_normal_no_seats_hint(text):
        return None
    if "too many requests" in raw or "429" in raw:
        return "rate_limit"
    if "профилакт" in raw or "временно не работает" in raw:
        return "maintenance"
    if "error search" in raw or "hitit_error" in raw:
        return "error_search"
    if "captcha failed" in raw:
        return "captcha_failed"
    if "forbidden" in raw or "403" in raw:
        return "auth_forbidden"
    return None


def maybe_pause_repeated_error(tracker: RepeatedErrorTracker, key: str) -> None:
    """Пауза 60–90 с, если одна и та же ошибка подряд >3 раз."""
    n = tracker.note(key)
    limit = repeated_error_pause_after()
    if n <= limit:
        return
    delay = repeated_error_pause_sec()
    print(
        f"[backoff] «{key}» подряд {n} раз (в т.ч. после refresh) — "
        f"пауза {delay:.0f}с"
    )
    time.sleep(delay)
    tracker.reset_streak()


def _handle_search_error_retry(
    *,
    status: int,
    body: dict[str, Any] | str,
    headers: dict[str, str],
    error_tracker: RepeatedErrorTracker,
    search_error_retries: int,
    max_search_retries: int,
    allow_token_refresh: bool,
) -> tuple[str, int]:
    """
    Error Search / hitit: backoff при повторах, refresh token, retry.
    Возвращает action: done | retry | maintenance.
    """
    if not is_hitit_search_error(status, body):
        return "done", search_error_retries
    if is_maintenance_error(status, body):
        pause_for_maintenance(body)
        error_tracker.reset_streak()
        return "maintenance", search_error_retries
    key = error_key_from_response(status, body) or "error_search"
    maybe_pause_repeated_error(error_tracker, key)
    if not allow_token_refresh or search_error_retries >= max_search_retries:
        return "done", search_error_retries
    if not _retry_after_search_error(headers):
        return "done", search_error_retries
    return "retry", search_error_retries + 1


def decode_captcha_image(img_field: str) -> bytes:
    raw = (img_field or "").strip()
    if not raw:
        raise RuntimeError("captcha/finding: пустое поле img")

    if raw.startswith("data:"):
        _, _, b64 = raw.partition(",")
        raw = b64

    return base64.b64decode(raw)


def cnn_api_ready() -> bool:
    try:
        from captcha_cnn_api import is_ready

        return is_ready()
    except ImportError as exc:
        print(f"[captcha-api] CNN недоступна ({exc})")
        return False


def manual_captcha_enabled() -> bool:
    """Пока нет обученной модели — ручной ввод в терминале (по умолчанию да)."""
    return (env("CAPTCHA_API_MANUAL", "1") or "1").strip().lower() not in ("0", "false", "no")


def api_captcha_n_digits() -> int:
    return 5


def show_captcha_image(image_bytes: bytes, *, open_viewer: bool | None = None) -> Path:
    image_path = captcha_image_path()
    image_path.write_bytes(image_bytes)
    if open_viewer is None:
        open_viewer = manual_captcha_enabled() and sys.stdin.isatty()
    if open_viewer:
        print(f"[captcha-api] картинка: {image_path}")
        open_in_viewer(image_path)
    return image_path


def prompt_captcha_manual() -> str:
    want = api_captcha_n_digits()
    while True:
        try:
            raw = input(f"Введи {want} цифр с картинки капчи: ").strip()
        except EOFError as exc:
            raise RuntimeError("Нет ввода капчи (терминал закрыт?)") from exc
        code = re.sub(r"\D", "", raw)
        if len(code) == want:
            return code
        print(f"Нужно ровно {want} цифр, получено: {code!r}")


def try_cnn_predict(image_bytes: bytes) -> tuple[str, float] | None:
    """CNN-ответ для API-капчи (5 цифр), если модель есть."""
    if not cnn_use_enabled() or not cnn_api_ready():
        return None
    from captcha_cnn_api import N_DIGITS, predict

    try:
        code, conf = predict(image_bytes)
    except Exception as exc:  # noqa: BLE001
        print(f"[cnn-api] predict: {exc}")
        return None
    if len(code) != N_DIGITS:
        print(f"[cnn-api] predict: не {N_DIGITS} цифр ({code!r})")
        return None
    return code, conf


def print_cnn_suggestion(cnn_guess: tuple[str, float] | None) -> None:
    if cnn_guess is None:
        return
    cnn_code, conf = cnn_guess
    print(_cyan(f"[cnn-api] предложение: {cnn_code} (conf={conf:.2f})"))


def print_cnn_auto_answer(cnn_guess: tuple[str, float] | None) -> None:
    if cnn_guess is None:
        return
    cnn_code, conf = cnn_guess
    print(_cyan(f"[cnn-api] авто-ответ: {cnn_code} (conf={conf:.2f})"))


def compare_manual_with_cnn(
    image_bytes: bytes,
    manual_code: str,
    cnn_guess: tuple[str, float] | None,
) -> None:
    """Сравнить ручной ввод с CNN; при расхождении — rejected для CNN-ответа."""
    if not compare_captcha_enabled() or cnn_guess is None:
        return

    from captcha_dataset_api import save_rejected

    cnn_code, _conf = cnn_guess
    if cnn_code == manual_code:
        return

    print(_red(f"[compare] ✗ CNN {cnn_code} ≠ ручной {manual_code}"))
    save_rejected(image_bytes, cnn_code, source="cnn", reason="mismatch_manual")


def _format_api_response(body: dict[str, Any] | str) -> str:
    """Полный ответ API для лога (без обрезки)."""
    if isinstance(body, dict):
        return json.dumps(body, ensure_ascii=False, indent=2)
    return str(body)


def _log_api_response(
    *,
    label: str,
    status: int,
    body: dict[str, Any] | str,
    error_only: bool = True,
) -> None:
    """Печать полного тела ответа при ошибке HTTP или success=false."""
    if error_only:
        if status < 400 and isinstance(body, dict) and body.get("success"):
            return
    print(_red(f"[api] {label} HTTP {status} — полный ответ:"))
    print(_format_api_response(body))


def _server_captcha_message(body: dict[str, Any] | str) -> str:
    if not isinstance(body, dict):
        return str(body)[:200]
    for key in ("message", "data", "error"):
        val = body.get(key)
        if val not in (None, "", {}):
            return str(val)[:200]
    return ""


def report_captcha_server_result(
    manual_code: str,
    status: int,
    body: dict[str, Any] | str,
    *,
    cnn_guess: tuple[str, float] | None = None,
    image_bytes: bytes | None = None,
) -> bool:
    """Сравнить ручной ввод с ответом сервера. True — капча принята."""
    if captcha_accepted(status, body):
        msg = _server_captcha_message(body)
        extra = f" ({msg})" if msg else ""
        conf_suffix = ""
        if cnn_guess is not None:
            _cnn_code, conf = cnn_guess
            conf_suffix = f" (conf={conf:.2f})"
        print(_green(f"[compare] ✓ сервер принял капчу: {manual_code}{extra}{conf_suffix}"))
        if compare_captcha_enabled() and cnn_guess is not None and image_bytes:
            cnn_code, _conf = cnn_guess
            if cnn_code != manual_code:
                from captcha_dataset_api import save_rejected

                save_rejected(image_bytes, cnn_code, source="cnn", reason="server_mismatch")
        return True

    if is_captcha_required(status, body):
        msg = _server_captcha_message(body)
        print(_red(f"[compare] ✗ сервер отклонил капчу: ввод={manual_code}"))
        if msg:
            print(_red(f"[compare] ответ сервера: {msg}"))
        return False

    msg = _server_captcha_message(body) or str(body)[:200]
    print(_red(f"[compare] ✗ сервер не принял (HTTP {status}): ввод={manual_code}"))
    if msg:
        print(_red(f"[compare] ответ: {msg}"))
    return False


def solve_captcha_api(image_bytes: bytes) -> tuple[str, tuple[str, float] | None]:
    """API-капча: CNN подсказка + ручной ввод. (код, cnn_guess)."""
    cnn_guess = try_cnn_predict(image_bytes) if compare_captcha_enabled() else None

    manual_env = re.sub(r"\D", "", env("CAPTCHA_API"))
    if manual_env:
        print(f"[captcha-api] CAPTCHA_API из env: {manual_env}")
        compare_manual_with_cnn(image_bytes, manual_env, cnn_guess)
        return manual_env, cnn_guess

    manual = manual_captcha_enabled() and sys.stdin.isatty()
    show_captcha_image(image_bytes, open_viewer=manual)
    if manual:
        print_cnn_suggestion(cnn_guess)

    if manual:
        code = prompt_captcha_manual()
        print(f"[captcha-api] введено вручную: {code}")
        compare_manual_with_cnn(image_bytes, code, cnn_guess)
        return code, cnn_guess

    if cnn_guess is not None:
        code, conf = cnn_guess
        from captcha_cnn_api import min_confidence

        print_cnn_auto_answer(cnn_guess)
        if conf < min_confidence():
            print(f"[cnn-api] conf={conf:.2f} < {min_confidence():.2f} — всё равно отправляю")
        return code, cnn_guess

    if cnn_api_ready() and cnn_use_enabled():
        guess = try_cnn_predict(image_bytes)
        if guess:
            code, conf = guess
            from captcha_cnn_api import min_confidence

            print_cnn_auto_answer(guess)
            if conf < min_confidence():
                print(f"[cnn-api] conf={conf:.2f} < {min_confidence():.2f} — всё равно отправляю")
            return code, guess

    raise RuntimeError(
        "Капча: запусти в терминале (ручной ввод) или обучи captcha_model_api.pt. "
        f"Картинка: {captcha_image_path()}"
    )


def parse_api_availability(body: dict[str, Any] | str) -> tuple[bool, list[str]]:
    available, items, _info = evaluate_api_availability(body)
    return available, items


def check_version(
    headers: dict[str, str],
    *,
    verbose: bool = True,
) -> tuple[int, dict[str, Any] | str, float | None]:
    """POST checkVersion — как при старте iOS-приложения."""
    status, body, retry_after = api_request(
        "POST",
        "checkVersion",
        headers=headers,
        body={
            "system": (env("API_SYSTEM", "ios") or "ios").lower(),
            "build_version": api_build_version(),
        },
    )
    if verbose:
        ok = isinstance(body, dict) and body.get("success")
        mark = "ok" if ok else "warn"
        print(f"[0] checkVersion → HTTP {status} ({mark})")
        if not ok:
            preview = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            if len(preview) > 500:
                preview = preview[:500] + "…"
            print(f"[0] checkVersion body: {preview}")
    return status, body, retry_after


def skip_availability_probe() -> bool:
    """Пропустить probe GetAirAvailability (402) — сразу captcha/finding."""
    return (env("API_SKIP_PROBE", "1") or "1").strip().lower() not in ("0", "false", "no")


def run_check_once(
    headers: dict[str, str],
    depart_date: str,
    *,
    verbose: bool = True,
    allow_token_refresh: bool = True,
    error_tracker: RepeatedErrorTracker | None = None,
) -> tuple[bool, list[str], dict[str, Any] | str | None]:
    from captcha_dataset_api import save_confirmed, save_rejected

    tracker = error_tracker or RepeatedErrorTracker()
    max_attempts = captcha_max_attempts()
    max_search_retries = search_error_max_retries()
    search_error_retries = 0

    try:
        ensure_live_auth(headers, env_path=api_secrets_env_path(), verbose=verbose)
    except AuthTransientError as exc:
        print(f"[auth] {exc} — этот цикл без поиска")
        return False, [str(exc)], None

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(f"[captcha] повтор {attempt}/{max_attempts}")

        skip_probe = skip_availability_probe()

        if not skip_probe:
            payload_probe = build_search_payload(depart_date=depart_date)
            status1, body1 = api_request_resilient(
                "POST",
                "GetAirAvailability",
                headers=headers,
                body=payload_probe,
                label="GetAirAvailability",
            )
            if verbose:
                print(f"[1] GetAirAvailability → HTTP {status1}")
            if is_auth_forbidden(status1, body1):
                return _handle_auth_forbidden(
                    headers,
                    depart_date,
                    verbose=verbose,
                    allow_token_refresh=allow_token_refresh,
                    error_tracker=tracker,
                )
            sleep_jitter()

            if isinstance(body1, dict) and body1.get("success"):
                available, items = parse_api_availability(body1)
                if verbose and available:
                    print(f"[ok] без капчи: {len(items)} рейс(ов)")
                return available, items, body1

            if not is_captcha_required(status1, body1):
                action, search_error_retries = _handle_search_error_retry(
                    status=status1,
                    body=body1,
                    headers=headers,
                    error_tracker=tracker,
                    search_error_retries=search_error_retries,
                    max_search_retries=max_search_retries,
                    allow_token_refresh=allow_token_refresh,
                )
                if action in ("retry", "maintenance"):
                    continue
                if verbose:
                    print(f"[warn] не captcha и не success: {body1}")
                return False, [str(body1)], None

        captcha_step = 1 if skip_probe else 2
        search_step = 2 if skip_probe else 3

        status2, body2 = api_request_resilient(
            "GET",
            "captcha/finding",
            headers=headers,
            label="captcha/finding",
        )
        if verbose:
            print(f"[{captcha_step}] captcha/finding → HTTP {status2}")
        if is_auth_forbidden(status2, body2):
            return _handle_auth_forbidden(
                headers,
                depart_date,
                verbose=verbose,
                allow_token_refresh=allow_token_refresh,
                error_tracker=tracker,
            )
        sleep_jitter()

        if status2 == 0:
            # Транспорт вернул 0 — ответа не было (обрыв, таймаут, DNS).
            # Капчу без картинки не решить, ждём следующий слот.
            return False, [f"сеть: {body2}"], None

        if not isinstance(body2, dict) or not body2.get("key") or not body2.get("img"):
            raise RuntimeError(f"нет key/img капчи: {body2}")

        captcha_key = str(body2["key"])
        image_bytes = decode_captcha_image(str(body2["img"]))
        captcha_code, cnn_guess = solve_captcha_api(image_bytes)

        payload_final = build_search_payload(
            depart_date=depart_date,
            captcha_key=captcha_key,
            captcha_api=captcha_code,
        )
        sleep_before_captcha_search()
        status3, body3, retry_after3 = api_request(
            "POST",
            "GetAirAvailability",
            headers=headers,
            body=payload_final,
        )
        if verbose:
            print(f"[{search_step}] GetAirAvailability+captcha → HTTP {status3}")
        if is_auth_forbidden(status3, body3):
            return _handle_auth_forbidden(
                headers,
                depart_date,
                verbose=verbose,
                allow_token_refresh=allow_token_refresh,
                error_tracker=tracker,
            )
        if handle_rate_limited(
            status=status3,
            body=body3,
            retry_after=retry_after3,
            label="GetAirAvailability+captcha",
        ):
            continue
        if status3 >= 400 or (isinstance(body3, dict) and not body3.get("success")):
            _log_api_response(label="GetAirAvailability+captcha", status=status3, body=body3)
        sleep_jitter()

        captcha_ok = report_captcha_server_result(
            captcha_code,
            status3,
            body3,
            cnn_guess=cnn_guess,
            image_bytes=image_bytes,
        )

        if captcha_ok:
            save_confirmed(image_bytes, captcha_code)
            available, items = parse_api_availability(body3)
            if verbose and available:
                print("[ok] success, места=да")
            return available, items, body3

        if is_captcha_required(status3, body3):
            save_rejected(image_bytes, captcha_code, reason="invalid")
            delay = captcha_wrong_delay_sec()
            if delay > 0:
                print(f"[captcha-api] неверная капча — повтор через {delay:.0f}с…")
                time.sleep(delay)
            else:
                print("[captcha-api] неверная капча — повтор сразу")
            continue

        action, search_error_retries = _handle_search_error_retry(
            status=status3,
            body=body3,
            headers=headers,
            error_tracker=tracker,
            search_error_retries=search_error_retries,
            max_search_retries=max_search_retries,
            allow_token_refresh=allow_token_refresh,
        )
        if action in ("retry", "maintenance"):
            continue

        if verbose:
            print(f"[error] ответ: {body3}")
        return False, [str(body3)], None

    print("[error] капча не пройдена после всех попыток", file=sys.stderr)
    return False, ["captcha failed"], None


def check_booking_guard(guard: BookingGuard, *, force_rebook: bool) -> int | None:
    """
    Не дать забронировать второй раз после перезапуска.

    Раньше монитор ничего не помнил между запусками: упал после успешного
    CreateBooking — при следующем старте спокойно бронировал заново, на тех же
    пассажиров и с новым счётом в банке.

    Возвращает код выхода, если стартовать нельзя, иначе None.
    """
    blocker = guard.blocking_attempt()
    if blocker is None:
        return None

    if force_rebook:
        print(_red(f"[book-api] --force-rebook: снимаю блокировку ({blocker.status})"))
        guard.clear_all()
        return None

    print(_red("[book-api] автобронь заблокирована незакрытой попыткой:"))
    print(_red(f"  статус:  {blocker.status}"))
    print(_red(f"  начата:  {blocker.started_at}"))
    if blocker.booking_number:
        print(_red(f"  PNR:     {blocker.booking_number}"))
    if blocker.form_url:
        print(_red(f"  оплата:  {blocker.form_url}"))
    if blocker.detail:
        print(_red(f"  детали:  {blocker.detail}"))
    print(f"  журнал:  {bookings_journal()}")
    print("Проверь брони в приложении T5, затем запусти с --force-rebook")
    return 2


def stop_on_unknown_booking(result: BookingResult, route: str) -> int:
    """Неопределённый результат брони: остановиться и позвать человека."""
    text = "\n".join(
        [
            route,
            "Бронь могла быть создана, но подтверждения нет:",
            result.error,
            "",
            "Проверь список броней в приложении T5 ПЕРЕД повторным запуском.",
            f"Журнал попыток: {bookings_journal()}",
            "Снять блокировку после проверки: --force-rebook",
        ]
    )
    print(_red("[book-api] СТОП — результат брони неизвестен."))
    notify("T5 API: бронь под вопросом", text)
    return 2


def auth_debug_enabled() -> bool:
    return (env("API_DEBUG_AUTH") or "0").strip().lower() not in ("0", "false", "no")


def run_watch_loop(*, once: bool = False, force_rebook: bool = False) -> int:
    secrets_file = api_secrets_env_path()
    headers: dict[str, str] | None = None

    for attempt in range(1, 6):
        try:
            if ensure_api_session(env_path=secrets_file):
                headers = build_api_headers()
            break
        except RateLimitStop as exc:
            report_error("rate_limit_stop", detail=exc, endpoint="auth/login")
            return RATE_LIMIT_EXIT_CODE
        except AuthTransientError as exc:
            # 429 или 5xx на входе. После 429 пауза уже выдержана в api_call,
            # после 5xx ждём сами — и пробуем снова, а не падаем.
            print(f"[auth] {exc} — повтор входа {attempt}/5")
            wait_if_paused(label="auth/login")
            time.sleep(30)
        except Exception as exc:  # noqa: BLE001
            if not tokens_configured():
                raise SystemExit(f"[auth] не удалось получить токены: {exc}") from exc
            print(f"[auth] предупреждение: {exc} — использую токены из env")
            break

    if not headers:
        headers = build_api_headers()
    if auth_debug_enabled():
        print(
            "[auth-debug] старт: customer="
            f"{_token_preview(headers.get('x-customer-authorization', ''))}"
        )
    depart_date = to_api_date(env("DATE"))
    date_label = env("DATE")
    state_key = f"{env('DEPART_PORT_CODE')}-{env('ARRIVAL_PORT_CODE')}-{date_label}"

    period = check_period_sec()
    slot_label = format_slot_ranges()
    n_slots = len(check_at_second_ranges())
    print("=== T5 API watch ===")
    print(f"Маршрут: {route_line(date_label)}")
    print(f"Рейс: {flight_selection_label()}")
    print(f"Пассажиры: {env('PASSENGER_IDS') or '1'}")
    req_per_cycle = (2 if skip_availability_probe() else 3) + 1
    rate = (
        f"{n_slots}/мин" if period <= 60 else f"раз в {period // 60} мин"
    )
    print(
        f"Слот: {slot_label} шаг 0.1с "
        f"(каждые {period}с — {rate}, {req_per_cycle} HTTP за цикл)"
    )
    ua = current_ios_user_agent()
    print(f"[ua] {ua}  (смена через {ios_user_agent_rotate_in_min():.0f} мин)")
    if api_delay_enabled():
        print("[delay] API_DELAY=1 — jitter и паузы капчи включены")
    else:
        print("[delay] API_DELAY=0 — без пауз капчи, капча сразу")
    if cnn_api_ready() and not manual_captcha_enabled():
        print("[cnn-api] модель captcha_model_api.pt (5 цифр)")
    else:
        print("[captcha-api] ручной ввод в терминале (картинка откроется автоматически)")
        if cnn_api_ready():
            print("[info] CNN есть — включи CAPTCHA_API_MANUAL=0 для авто-режима")
    if auto_book_enabled():
        print("[book-api] API_AUTO_BOOK=1 — при местах: CreateBooking + ссылка на оплату")
    if notifications_delegated():
        # Запуск из панели: она сама читает журнал броней и шлёт письмо и SMS.
        print("[notify] уведомления рассылает панель")
    elif gmail_configured():
        to = env("NOTIFY_EMAIL") or env("GMAIL_USER") or ""
        print(f"[notify] Gmail настроен → {to}")
    else:
        print("[notify] Gmail не настроен — только macOS + консоль")
        print("         нужны GMAIL_USER + GMAIL_APP_PASSWORD в .env.local и NOTIFY_EMAIL в check.env")
    print()

    try:
        status, body, retry_after = check_version(headers)
        while handle_rate_limited(
            status=status,
            body=body,
            retry_after=retry_after,
            label="checkVersion",
        ):
            status, body, retry_after = check_version(headers, verbose=False)
        sleep_jitter()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] checkVersion: {exc}")

    guard = BookingGuard(bookings_journal())
    if auto_book_enabled():
        blocked = check_booking_guard(guard, force_rebook=force_rebook)
        if blocked is not None:
            return blocked

    state = load_state()
    error_tracker = RepeatedErrorTracker()

    try:
        while True:
            wait_for_check_slot()
            started = time.strftime("%H:%M:%S")
            pax_names = format_passenger_names()
            print(f"[check] {started} {route_line(date_label)}, passenger: {pax_names}")
            if auth_debug_enabled():
                print(
                    "[auth-debug] запрос: customer="
                    f"{_token_preview(headers.get('x-customer-authorization', ''))}"
                )
            # Ошибка, уже записанная в журнал, ниже приходит ещё раз как подсказка
            # цикла — без флага каждая попадала в «Ошибки» дважды.
            error_reported = False
            try:
                available, items, avail_body = run_check_once(
                    headers,
                    depart_date,
                    error_tracker=error_tracker,
                )
            except AuthFatalError as exc:
                print(f"\033[91m[auth] {exc}\033[0m")
                text = str(exc)
                report_error(
                    "auth_login" if "login" in text.lower() else "auth_forbidden",
                    detail=text,
                )
                return 1
            except RateLimitStop:
                raise
            except (Exception, KeyboardInterrupt) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                print(f"\033[91m[error] {exc}\033[0m — следующий слот {format_slot_ranges()}")
                # У сетевых ошибок текст и так говорит всё; имя класса только
                # путает: ResultUnknown на поиске — не «бронь под вопросом».
                detail = str(exc) if isinstance(exc, RequestFailed) else f"{type(exc).__name__}: {exc}"
                report_error(classify_error_text(str(exc)) or "other", detail=detail)
                error_reported = True
                available, items, avail_body = False, [str(exc)], None

            counted = count_direct_flights(avail_body) if avail_body is not None else None
            if counted is not None:
                n_direct, labels = counted
                extra = f" ({', '.join(labels)})" if labels else ""
                print(f"[flights] прямых: {n_direct}{extra}")

            prev = state.get(state_key, {}).get("available")
            if available:
                error_tracker.clear()
                summary = " | ".join(items[:3]) if items else "есть рейсы"
                print(f"[ok] ЕСТЬ места — {summary}")
                route = route_line(date_label)
                booked = False
                if auto_book_enabled() and avail_body:
                    # Бронь ПЕРВЫМ делом. Раньше в ветке неудачи письмо уходило
                    # синхронно (SMTP до 30 с), и цикл стоял с занятым слотом.
                    try:
                        book_result = try_auto_book(
                            avail_body,
                            headers=headers,
                            route_line=route,
                            guard=guard,
                        )
                    except ResultUnknown as exc:
                        book_result = BookingResult(
                            ok=False, unknown=True, error=f"ответ потерян: {exc}"
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Неизвестно, ушёл ли POST — считаем результат неопределённым.
                        book_result = BookingResult(
                            ok=False, unknown=True, error=f"{type(exc).__name__}: {exc}"
                        )

                    if book_result.ok:
                        print(_green(f"[book-api] PNR={book_result.pnr} — бронь создана"))
                        if notifications_delegated():
                            print("[book-api] ссылку на оплату разошлёт панель")
                        else:
                            print(
                                "[book-api] письмо с checkoutUrl должно быть в почте "
                                f"({env('NOTIFY_EMAIL') or env('GMAIL_USER') or '—'})"
                            )
                        booked = True
                    elif book_result.unknown:
                        # Повторять нельзя: вторая попытка = вторая настоящая бронь.
                        report_error("booking_unknown", detail=book_result.error,
                                     endpoint="CreateBooking")
                        return stop_on_unknown_booking(book_result, route)
                    else:
                        print(
                            _red(f"[book-api] бронь не удалась, брони нет: {book_result.error}")
                        )
                        report_error("booking_failed", detail=book_result.error,
                                     endpoint="CreateBooking")
                        if prev is not True:
                            notify(
                                "T5 API: места есть, бронь не удалась",
                                f"{route}\n{summary}\n\n{book_result.error}",
                            )
                elif not auto_book_enabled() and prev is not True:
                    notify("T5 API: появились билеты", f"{route}\n{summary}")
                state[state_key] = {
                    "available": True,
                    "items": items[:5],
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_state(state)
                if auto_book_enabled() and not booked:
                    print("[book-api] бронь не удалась — поиск продолжается")
                else:
                    stop_msg = (
                        "[ok] Бронь создана — монитор остановлен."
                        if booked
                        else "[ok] Билеты найдены — монитор остановлен."
                    )
                    print(stop_msg)
                    return 0
            else:
                hint = items[0] if items else "свободных мест нет"
                print(_purple(f"[--] {hint}"))
                err_key = error_key_from_text(hint)
                if not error_reported and not _is_normal_no_seats_hint(hint):
                    # 429 уже записан там, где пойман, — с запросом и Retry-After.
                    journal_key = classify_error_text(hint)
                    if journal_key and journal_key != "rate_limit":
                        report_error(journal_key, detail=hint)
                if err_key == "rate_limit":
                    # Пауза по общей лестнице уже выдержана в api_call.
                    error_tracker.reset_streak()
                elif err_key == "maintenance":
                    pause_for_maintenance(hint)
                    error_tracker.reset_streak()
                elif err_key:
                    maybe_pause_repeated_error(error_tracker, err_key)
                else:
                    error_tracker.reset_streak()

            state[state_key] = {
                "available": available,
                "items": items[:5],
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_state(state)

            if once:
                return 0
    except KeyboardInterrupt:
        print("\n[stop] Остановлено пользователем.")
        return 0
    except RateLimitStop as exc:
        report_error("rate_limit_stop", detail=exc)
        print("[stop] Повторные 429 — все процессы самолётов остановлены. "
              "Не перезапускайте сразу: это и есть путь к блокировке аккаунта.")
        return RATE_LIMIT_EXIT_CODE
    finally:
        # Письма уходят в фоне — дождаться их до выхода из процесса, иначе
        # демон-поток умрёт вместе с ним и ссылка на оплату потеряется.
        flush_notifications()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Монитор рейсов T5 через API")
    parser.add_argument(
        "--env",
        dest="env_path",
        default="check.env",
        help="Путь к env-файлу (по умолчанию check.env)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Один цикл без бесконечного цикла",
    )
    parser.add_argument(
        "--force-rebook",
        action="store_true",
        help="снять блокировку журнала броней (только после ручной проверки броней)",
    )
    args = parser.parse_args(argv)

    load_api_env(args.env_path)
    return run_watch_loop(once=args.once, force_rebook=args.force_rebook)


if __name__ == "__main__":
    raise SystemExit(main())
