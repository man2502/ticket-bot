"""429 в самолётном боте: общая лестница пауз на все процессы с одного адреса.

Сама лестница и состояние — в `core.rate_limit`. Здесь то, что нужно боту:
где лежит файл, как ждать так, чтобы кнопка «Остановить» срабатывала сразу,
и чем сообщить наверх, что пора остановиться совсем.

Пауза включается в `api_client.api_call` — через него идут ВСЕ запросы бота:
вход, `customers/info`, капча, поиск, бронь. Раньше 429 обрабатывался в трёх
местах из пяти, и именно на первом запросе цикла пауза не включалась вовсе.
"""

from __future__ import annotations

import time
from pathlib import Path

from core.rate_limit import (
    RateLimitDecision,
    active_pause,
    is_stopped,
    rate_limit_state_path,
    register_429,
)

__all__ = [
    "RateLimitStop",
    "RATE_LIMIT_EXIT_CODE",
    "state_file",
    "is_rate_limited_response",
    "on_429",
    "wait_if_paused",
]

API_ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = API_ROOT / "api_rate_limit.json"

# Код выхода «остановлен из-за 429». Отдельный, чтобы панель отличала его
# от обычной ошибки: здесь нельзя просто перезапустить — это и есть путь к бану.
RATE_LIMIT_EXIT_CODE = 3

# Как часто во время паузы перечитывать общий файл: другой процесс мог
# продлить паузу или объявить стоп.
_RECHECK_SEC = 5.0


class RateLimitStop(BaseException):
    """
    Пятый 429 — все процессы самолётов останавливаются.

    Наследник BaseException, а не Exception, намеренно: в боте много мест
    с `except Exception`, которые превратили бы стоп в «ошибку цикла, идём
    дальше» или в ошибку авторизации. Стоп обязан дойти до главного цикла.
    """


def state_file() -> Path:
    return rate_limit_state_path(DEFAULT_STATE_FILE)


def is_rate_limited_response(status: int, text: str) -> bool:
    """HTTP 429 или HTML-страница «Too Many Requests» с другим статусом."""
    if status == 429:
        return True
    return "too many requests" in (text or "").lower()


def _sleep_chunked(seconds: float) -> None:
    """
    Спать короткими кусками: на Windows длинный `time.sleep` не прерывается
    Ctrl+Break, и кнопка «Остановить» в панели ждала бы конца паузы.
    """
    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.5, left))


def wait_if_paused(*, label: str = "") -> None:
    """
    Дождаться конца общей паузы. Бросает RateLimitStop, если объявлен стоп.

    Зовётся перед каждым запросом: так остальные процессы останавливаются
    на следующем же шаге, а не досылают свой цикл до конца.
    """
    path = state_file()
    announced = False
    while True:
        stopped = is_stopped(path)
        if stopped > 0:
            raise RateLimitStop(
                f"стоп после повторных 429, ещё {stopped / 60:.0f} мин — "
                f"все процессы самолётов остановлены"
            )
        left = active_pause(path)
        if left <= 0:
            if announced:
                print("[rate-limit] общая пауза закончилась — продолжаю")
            return
        if not announced:
            until = time.strftime("%H:%M:%S", time.localtime(time.time() + left))
            where = f" перед {label}" if label else ""
            print(f"[rate-limit] общая пауза{where} до {until} ({left / 60:.1f} мин)")
            announced = True
        _sleep_chunked(min(_RECHECK_SEC, left))


def on_429(
    *,
    endpoint: str,
    status: int,
    retry_after: float | None,
    source: str = "",
    wait: bool = True,
) -> RateLimitDecision:
    """
    Учесть 429: поднять общую ступень, напечатать строку журнала ошибок и ждать.

    `wait=False` — только учесть. Нужно CreateBooking: внутри брони ждать
    нельзя, а 429 на ней означает «запрос не принят», и перебор рейсов
    сам дождётся паузы перед следующей попыткой.
    """
    decision = register_429(
        state_file(), retry_after=retry_after, source=source, endpoint=endpoint
    )
    ra = f"Retry-After={retry_after:.0f}с" if retry_after is not None else "Retry-After: нет"
    print(f"[err] rate_limit | HTTP {status} | {endpoint} | {ra} | {decision.describe()}")
    if not wait:
        # Внутри брони стоп не бросаем: попытка уже записана в журнал как
        # pending, и прерванная на ней бронь заблокировала бы аккаунт.
        # Стоп сработает на следующем wait_if_paused — до записи следующей попытки.
        return decision
    if decision.stop:
        raise RateLimitStop(decision.describe())
    wait_if_paused(label=endpoint)
    return decision
