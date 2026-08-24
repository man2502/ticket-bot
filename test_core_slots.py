"""Тесты расписания проверок: период, отметки минуты, пропуск нерабочих минут.

Расписание общее для обоих ботов, поэтому и тесты общие. Проверяется главным
образом то, что новые периоды не сломали старое поведение: 30 секунд обязаны
дать ровно прежние отметки :00 и :30.

Запуск (из корня репозитория):
    python run_tests.py test_core_slots.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.slots import (
    CHECK_PERIODS_SEC,
    DEFAULT_CHECK_PERIOD_SEC,
    is_slot_minute,
    normalize_check_period,
    slot_anchors,
    slot_minute_stride,
)


# --------------------------------------------------------------------------- #
# Разбор значения периода
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("period", list(CHECK_PERIODS_SEC))
def test_allowed_periods_pass_through(period: int) -> None:
    assert normalize_check_period(period) == period
    assert normalize_check_period(str(period)) == period


@pytest.mark.parametrize("raw", ["", "  ", None, "abc", "45", "0", "-30", "3600"])
def test_unknown_period_falls_back_to_default(raw: object) -> None:
    """
    Не подгоняем к ближайшему разрешённому.

    Тихо превратить 45 в 30 хуже, чем вернуться к значению по умолчанию:
    в первом случае человек уверен, что настроил 45, и никогда не узнает,
    что бот ходит иначе.
    """
    assert normalize_check_period(raw) == DEFAULT_CHECK_PERIOD_SEC


def test_default_can_be_overridden() -> None:
    assert normalize_check_period("мусор", default=120) == 120


# --------------------------------------------------------------------------- #
# Отметки минуты
# --------------------------------------------------------------------------- #


def test_thirty_seconds_keeps_old_marks() -> None:
    """Прежнее поведение обоих ботов — окна :00 и :30 — должно сохраниться."""
    assert slot_anchors(30, 0) == [0, 30]


def test_ten_seconds_gives_six_marks() -> None:
    assert slot_anchors(10, 0) == [0, 10, 20, 30, 40, 50]


def test_minute_and_slower_give_single_mark() -> None:
    assert slot_anchors(60, 0) == [0]
    assert slot_anchors(120, 0) == [0]


def test_marks_are_shifted_by_anchor() -> None:
    """Сдвиг нужен, чтобы две панели с одинаковой настройкой не били синхронно."""
    assert slot_anchors(30, 7) == [7, 37]
    assert slot_anchors(10, 7) == [7, 17, 27, 37, 47, 57]


def test_shift_wraps_around_the_minute() -> None:
    assert slot_anchors(30, 45) == [45, 15]


@pytest.mark.parametrize("raw_anchor,expected", [(-5, 0), (60, 59), (999, 59)])
def test_anchor_is_clamped(raw_anchor: int, expected: int) -> None:
    assert slot_anchors(60, raw_anchor) == [expected]


def test_unknown_period_uses_default_marks() -> None:
    assert slot_anchors(45, 0) == slot_anchors(DEFAULT_CHECK_PERIOD_SEC, 0)


# --------------------------------------------------------------------------- #
# Периоды реже минуты
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("period,stride", [(10, 1), (30, 1), (60, 1), (120, 2)])
def test_stride_counts_minutes(period: int, stride: int) -> None:
    assert slot_minute_stride(period) == stride


@pytest.mark.parametrize("period", [10, 30, 60])
def test_every_minute_works_for_fast_periods(period: int) -> None:
    for minute in range(60):
        assert is_slot_minute(period, dt.datetime(2026, 8, 20, 12, minute, 0))


def test_two_minute_period_skips_every_other_minute() -> None:
    """
    Без этого 120 секунд превратились бы в 60: бот стрелял бы каждую минуту.
    """
    active = [
        minute
        for minute in range(10)
        if is_slot_minute(120, dt.datetime(2026, 8, 20, 12, minute, 0))
    ]
    assert len(active) == 5
    # Рабочие минуты идут через одну — какая именно первая, зависит от эпохи.
    assert all(second - first == 2 for first, second in zip(active, active[1:]))


def test_two_minute_period_does_not_stumble_on_hour_edge() -> None:
    """
    Считаем по минутам эпохи, а не по `now.minute`. Час — это 60 минут, то есть
    чётное число, но проверка на границе дешевле, чем разбор жалобы «ночью бот
    почему-то удвоил темп».
    """
    before = dt.datetime(2026, 8, 20, 12, 59, 30)
    after = before + dt.timedelta(minutes=1)
    assert is_slot_minute(120, before) != is_slot_minute(120, after)
