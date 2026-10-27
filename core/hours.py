"""Интервалы часов суток: «01:00-06:00, 19:00-20:30».

Нужны боту для двух настроек:

* ночная пауза — в эти часы бот не делает ни одного запроса. Живой человек
  ночью билеты не ищет, а поиск каждые две минуты в три часа ночи — самый
  заметный признак робота;
* часы точных слотов — в эти часы бот бьёт в точную секунду, как раньше,
  ради гонки за открытием продаж. В остальное время он ходит со случайным
  интервалом: ровная сетка «каждые 120 с на :10» видна на стороне сайта сразу.

Интервал может переходить через полночь: «23:00-06:00».
"""

from __future__ import annotations

import datetime as dt
import re

__all__ = ["HoursError", "parse_hours", "format_hours", "in_hours", "seconds_until_end"]

_PART = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?$")

# Отрезок суток в минутах от полуночи: (начало, конец). Конец не включается.
Range = tuple[int, int]


class HoursError(ValueError):
    """Строку интервалов не разобрать — с объяснением для человека."""


def _minutes(hours: str, minutes: str | None) -> int:
    h, m = int(hours), int(minutes or 0)
    if h > 24 or m > 59 or (h == 24 and m):
        raise HoursError(f"нет такого времени: {hours}:{minutes or '00'}")
    return h * 60 + m


def parse_hours(text: str) -> list[Range]:
    """«01:00-06:00, 19-20» → [(60, 360), (1140, 1200)]. Пусто — пустой список."""
    ranges: list[Range] = []
    for raw in re.split(r"[,;\n]+", text or ""):
        part = raw.strip()
        if not part:
            continue
        match = _PART.match(part)
        if not match:
            raise HoursError(f"«{part}» — нужно как «01:00-06:00»")
        start = _minutes(match.group(1), match.group(2))
        end = _minutes(match.group(3), match.group(4))
        if start == end:
            raise HoursError(f"«{part}» — начало и конец совпадают")
        ranges.append((start % 1440, end % 1440 or 1440))
    return ranges


def format_hours(ranges: list[Range]) -> str:
    """Обратно в строку для формы: [(60, 360)] → «01:00-06:00»."""
    def clock(minutes: int) -> str:
        return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"

    return ", ".join(f"{clock(start)}-{clock(end)}" for start, end in ranges)


def _inside(minute: float, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # через полночь


def in_hours(ranges: list[Range], now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    minute = now.hour * 60 + now.minute + now.second / 60
    return any(_inside(minute, start, end) for start, end in ranges)


def seconds_until_end(ranges: list[Range], now: dt.datetime | None = None) -> float:
    """
    Сколько секунд до выхода из интервалов. 0 — сейчас вне их.

    Смежные интервалы («01-03, 03-06») считаются одним: выход — в 06:00.
    """
    now = now or dt.datetime.now()
    moment = now
    total = 0.0
    # Каждый шаг выводит за конец одного интервала; больше шагов, чем
    # интервалов, не бывает — счётчик только страхует от вечного цикла.
    for _ in range(len(ranges) + 1):
        minute = moment.hour * 60 + moment.minute + (moment.second + moment.microsecond / 1e6) / 60
        current = [(s, e) for s, e in ranges if _inside(minute, s, e)]
        if not current:
            return total
        start, end = current[0]
        left_min = (end - minute) % 1440 or 1440
        total += left_min * 60
        moment = now + dt.timedelta(seconds=total)
    return total
