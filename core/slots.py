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

__all__ = [
    "now_second",
    "seconds_until_second",
    "precise_sleep",
    "sleep_until_second",
    "CHECK_PERIODS_SEC",
    "DEFAULT_CHECK_PERIOD_SEC",
    "normalize_check_period",
    "slot_anchors",
    "slot_minute_stride",
    "is_slot_minute",
]

# Как часто разрешено проверять. Список закрытый намеренно: произвольное число
# вроде 47 секунд — это ровная сетка, которой у живого человека не бывает,
# и она читается на стороне сайта быстрее всего.
#
# 10 секунд — режим «очень надо». Шесть заходов в минуту заметно повышают шанс
# словить 429 и бан; держать его сутками не стоит.
CHECK_PERIODS_SEC = (10, 30, 60, 120)
DEFAULT_CHECK_PERIOD_SEC = 30

_SPIN_WINDOW_SEC = 0.02

# Максимальный кусок сна. Влияет не на точность, а на то, как быстро
# бот заметит команду остановки: обработчик сигнала выполняется
# только между вызовами time.sleep.
_MAX_SLEEP_CHUNK_SEC = 0.5


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
    """
    Спать `delay` секунд по монотонным часам, с точностью до пары миллисекунд.

    Долгое ожидание разбито на короткие куски. Причина не в точности, а в
    остановке: на Windows `time.sleep` не прерывается сигналом Ctrl+Break,
    и обработчик выполняется только между вызовами. Одним куском на 60 секунд
    бот переставал реагировать на команду «остановить» почти на минуту — и его
    приходилось убивать, не дав дописать журнал броней.
    """
    if delay <= 0:
        return
    deadline = time.monotonic() + delay

    while True:
        coarse = deadline - _SPIN_WINDOW_SEC - time.monotonic()
        if coarse <= 0:
            break
        time.sleep(min(_MAX_SLEEP_CHUNK_SEC, coarse))

    # Последние миллисекунды — коротким спином: sleep регулярно просыпается
    # на 5-20 мс позже, а слот этого не прощает.
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


def normalize_check_period(value: object, default: int = DEFAULT_CHECK_PERIOD_SEC) -> int:
    """
    Привести период к одному из разрешённых. Мусор и пустое — это default.

    Ближайший подходящий не подбираем: тихо превратить 45 в 30 хуже, чем
    вернуться к значению по умолчанию, — человек хотя бы заметит несовпадение.
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number in CHECK_PERIODS_SEC else default


def slot_minute_stride(period_sec: int) -> int:
    """
    Через сколько минут повторяется расписание.

    Для 10/30/60 — каждую минуту, для 120 — через одну.
    """
    return max(1, normalize_check_period(period_sec) // 60)


def slot_anchors(period_sec: int, at_second: int = 0) -> list[int]:
    """
    Секунды минуты, в которые открывается окно проверки.

    Отсчёт идёт от `at_second`, а не от нуля: две копии панели, настроенные
    на :00, ходили бы в сайт синхронно, и это видно в его логах. Сдвиг решает.

        (30, 0)  → [0, 30]
        (10, 7)  → [7, 17, 27, 37, 47, 57]
        (120, 5) → [5]           # плюс пропуск каждой второй минуты
    """
    period = normalize_check_period(period_sec)
    start = max(0, min(59, int(at_second)))
    if period > 60:
        return [start]
    return [(start + offset) % 60 for offset in range(0, 60, period)]


def is_slot_minute(period_sec: int, now: dt.datetime | None = None) -> bool:
    """
    Рабочая ли это минута для периода реже раза в минуту (120 с).

    Считаем по минутам эпохи, а не по `now.minute`: так шаг не сбивается
    на границе часа и одинаков в любом часовом поясе.
    """
    stride = slot_minute_stride(period_sec)
    if stride <= 1:
        return True
    moment = now or dt.datetime.now()
    return int(moment.timestamp()) // 60 % stride == 0
