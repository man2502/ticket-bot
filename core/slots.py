"""Точный тайминг проверочных слотов.

Старый код брал `datetime.now().second` и выбрасывал микросекунды:
в 12:00:57.900 расчёт «до :00» давал ровно 3.0 с, и запрос уходил в :00.900.
Ошибка равномерна на [0, 1) с, то есть в среднем бот опаздывал на пол-секунды —
в гонке за место это много.

Здесь:
  * доли секунды учитываются;
  * ожидание идёт по `time.monotonic()`, а не по настенным часам
    (перевод времени / NTP-скачок не сдвигает слот);
  * последние миллисекунды дожидаются коротким спином, потому что
    `time.sleep` регулярно просыпается на 5-20 мс позже.
"""

from __future__ import annotations

import datetime as dt
import time

__all__ = ["now_second", "seconds_until_second", "precise_sleep", "sleep_until_second"]

_SPIN_WINDOW_SEC = 0.02


def now_second(now: dt.datetime | None = None) -> float:
    """Текущая секунда минуты с долями: 57.9 вместо 57."""
    moment = now or dt.datetime.now()
    return moment.second + moment.microsecond / 1_000_000


def seconds_until_second(
    target: float,
    *,
    jitter: float = 0.0,
    now: dt.datetime | None = None,
    allow_past_window: float = 0.0,
) -> float:
    """
    Сколько ждать до отметки :target (+jitter) в этой или следующей минуте.

    allow_past_window — насколько секунд «опоздание» ещё считается попаданием
    в слот (тогда возвращается 0.0, и запрос уходит сразу).
    """
    current = now_second(now)
    wanted = target + jitter
    delta = wanted - current
    if delta < 0:
        if -delta <= allow_past_window:
            return 0.0
        delta += 60.0
    return delta


def precise_sleep(delay: float) -> None:
    """Спать `delay` секунд по монотонным часам, с точностью до пары миллисекунд."""
    if delay <= 0:
        return
    deadline = time.monotonic() + delay
    coarse = delay - _SPIN_WINDOW_SEC
    if coarse > 0:
        time.sleep(coarse)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining / 2)


def sleep_until_second(
    target: float,
    *,
    jitter: float = 0.0,
    allow_past_window: float = 0.0,
) -> float:
    """Дождаться отметки :target. Возвращает фактически прожданное время."""
    delay = seconds_until_second(
        target, jitter=jitter, allow_past_window=allow_past_window
    )
    precise_sleep(delay)
    return delay
