"""Тесты интервалов часов: ночная пауза и часы точных слотов (core.hours).

Запуск (из корня репозитория):
    python run_tests.py test_core_hours.py
"""

from __future__ import annotations

import datetime as dt

import pytest

from core.hours import HoursError, format_hours, in_hours, parse_hours, seconds_until_end


def _at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, 27, hour, minute, second)


def test_parse_and_format_round_trip() -> None:
    ranges = parse_hours("1:00-6:00, 19-20:30")
    assert ranges == [(60, 360), (1140, 1230)]
    assert format_hours(ranges) == "01:00-06:00, 19:00-20:30"


def test_empty_means_no_ranges() -> None:
    assert parse_hours("") == []
    assert parse_hours(" , ") == []
    assert not in_hours([], _at(3))
    assert seconds_until_end([], _at(3)) == 0


@pytest.mark.parametrize("text", ["ночью", "25:00-06:00", "01:00-01:00", "01:70-02:00", "1-"])
def test_garbage_is_explained(text: str) -> None:
    with pytest.raises(HoursError):
        parse_hours(text)


def test_inside_and_outside() -> None:
    night = parse_hours("01:00-06:00")
    assert in_hours(night, _at(1))
    assert in_hours(night, _at(5, 59, 59))
    assert not in_hours(night, _at(6))
    assert not in_hours(night, _at(0, 59))


def test_range_over_midnight() -> None:
    night = parse_hours("23:00-06:00")
    assert in_hours(night, _at(23, 30))
    assert in_hours(night, _at(2))
    assert not in_hours(night, _at(12))
    assert seconds_until_end(night, _at(23)) == pytest.approx(7 * 3600)


def test_until_end_counts_from_now() -> None:
    night = parse_hours("01:00-06:00")
    assert seconds_until_end(night, _at(5, 30)) == pytest.approx(1800)
    assert seconds_until_end(night, _at(7)) == 0


def test_adjacent_ranges_are_one_pause() -> None:
    assert seconds_until_end(parse_hours("01:00-03:00, 03:00-06:00"), _at(2)) == pytest.approx(4 * 3600)


def test_until_midnight_end() -> None:
    assert seconds_until_end(parse_hours("22:00-24:00"), _at(23)) == pytest.approx(3600)
