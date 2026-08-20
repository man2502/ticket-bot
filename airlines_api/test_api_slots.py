"""Тесты тайминга слотов и выбора рейса в airlines_api.

Слоты — это про доли секунды. Старый расчёт брал `datetime.now().second`
и терял микросекунды: в 12:00:00.700 «ждать до :00» давало 0, но фактически
запрос уходил на 700 мс позже намеченной отметки. Здесь проверяется, что
расчёт ведётся с долями и что окно текущей минуты не пропускается зря.

Запуск (из корня репозитория):
    python run_tests.py airlines_api
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import api_booking
import api_check
from core.slots import now_second, seconds_until_second


def _at(second: int, micro: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 20, 12, 0, second, micro)


# --------------------------------------------------------------------------
# Точность секунд
# --------------------------------------------------------------------------


def test_now_second_keeps_fraction() -> None:
    assert now_second(_at(57, 900_000)) == pytest.approx(57.9)


def test_seconds_until_second_uses_fraction() -> None:
    # 12:00:57.900 → до :00 следующей минуты ровно 2.1 с, а не 3.0.
    assert seconds_until_second(0.0, now=_at(57, 900_000)) == pytest.approx(2.1, abs=1e-6)


def test_seconds_until_second_with_jitter() -> None:
    assert seconds_until_second(0.0, jitter=0.2, now=_at(59, 500_000)) == pytest.approx(0.7, abs=1e-6)


# --------------------------------------------------------------------------
# Окна проверки
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _both_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CHECK_EVERY_00", "1")
    monkeypatch.setenv("API_CHECK_EVERY_30", "1")


def test_two_windows_configured() -> None:
    assert api_check.check_at_second_ranges() == [(0, 1), (30, 31)]


def test_window_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CHECK_EVERY_00", "0")
    assert api_check.check_at_second_ranges() == [(30, 31)]


def test_no_windows_is_a_hard_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CHECK_EVERY_00", "0")
    monkeypatch.setenv("API_CHECK_EVERY_30", "0")
    with pytest.raises(SystemExit):
        api_check.check_at_second_range()


def test_ticks_cover_whole_window() -> None:
    ticks = api_check.slot_offsets(30, 31)
    assert ticks[0] == 30.0
    assert ticks[-1] == 31.0
    assert len(ticks) == 11  # шаг 0.1 с


def test_next_slot_is_in_the_future(monkeypatch: pytest.MonkeyPatch) -> None:
    """В середине окна :30 стреляем в оставшийся тик этой же минуты, не ждём минуту."""
    monkeypatch.setattr(api_check, "now_second", lambda: 30.45)
    offset, window, until = api_check.next_check_slot()
    assert window == (30, 31)
    assert 0 < until <= 0.6
    assert 30.4 < offset <= 31.0


def test_passed_window_rolls_to_next_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_check, "now_second", lambda: 45.0)
    _offset, window, until = api_check.next_check_slot()
    assert window == (0, 1)
    assert 15.0 <= until <= 16.0


def test_nearest_window_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """В :05 ближе окно :30, а не :00 следующей минуты."""
    monkeypatch.setattr(api_check, "now_second", lambda: 5.0)
    _offset, window, until = api_check.next_check_slot()
    assert window == (30, 31)
    assert 25.0 <= until <= 26.0


# --------------------------------------------------------------------------
# Выбор рейса
# --------------------------------------------------------------------------


def _offer(idx: int, *, seat: int = 4, time: str = "09:40", number: str = "T5-1") -> api_booking.FlightOffer:
    return api_booking.FlightOffer(
        raw={},
        summary=f"flightNo={number}",
        search_id=idx + 1,
        booking_class_id=1,
        departure_time=time,
        flight_number=number,
        seat=seat,
        list_index=idx,
    )


def test_index_picks_requested_position(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "2")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    offers = [_offer(0, number="T5-1"), _offer(1, number="T5-2")]
    picked, err = api_booking.pick_flight_offer(offers)
    assert err is None
    assert picked.flight_number == "T5-2"


def test_strict_refuses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """FLIGHT_STRICT=1: на выбранном мест нет — бронировать соседний нельзя."""
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "1")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    offers = [_offer(0, seat=0), _offer(1, seat=9)]
    picked, err = api_booking.pick_flight_offer(offers)
    assert picked is None
    assert "strict" in err


def test_non_strict_falls_back_to_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    offers = [_offer(0, seat=0, number="T5-1"), _offer(1, seat=9, number="T5-2")]
    picked, err = api_booking.pick_flight_offer(offers)
    assert err is None
    assert picked.flight_number == "T5-2"


def test_time_beats_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.setenv("FLIGHT_TIME", "19:20")
    offers = [_offer(0, time="09:40", number="T5-1"), _offer(1, time="19:20", number="T5-2")]
    picked, err = api_booking.pick_flight_offer(offers)
    assert err is None
    assert picked.flight_number == "T5-2"


def test_transit_flight_is_never_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    transit = _offer(0, number="T5-1")
    transit.skipped = True
    offers = [transit, _offer(1, number="T5-2")]
    picked, _err = api_booking.pick_flight_offer(offers)
    assert picked.flight_number == "T5-2"
