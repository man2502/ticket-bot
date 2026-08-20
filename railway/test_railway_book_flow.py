"""Тесты сценария автоброни: когда можно идти к следующему рейсу, а когда — стоп.

Главный инвариант: неопределённый результат (таймаут, неразобранный ответ,
необработанное исключение) ОСТАНАВЛИВАЕТ перебор рейсов. Иначе бот заводит
вторую настоящую бронь на тех же пассажиров.

Запуск (из корня репозитория):
    .venv/bin/python -m pytest railway/test_railway_book_flow.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "railway")):
    if path not in sys.path:
        sys.path.insert(0, path)

import railway_check
from core.booking_guard import UNKNOWN, BookingGuard
from core.http import ResultUnknown
from railway_booking import BookingResult

TRIPS = [{"id": 1}, {"id": 2}, {"id": 3}]


@pytest.fixture()
def guard(tmp_path: Path) -> BookingGuard:
    return BookingGuard(tmp_path / "bookings.json")


def _patch(monkeypatch: pytest.MonkeyPatch, outcomes: list[object]) -> list[int]:
    """Подменяет попытку брони: по одному исходу на рейс. Возвращает журнал вызовов."""
    seen: list[int] = []

    def fake(trip, *, guard=None, verbose=True):  # noqa: ANN001, ARG001
        seen.append(trip["id"])
        outcome = outcomes[len(seen) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(railway_check, "try_book_available_trip", fake)
    return seen


def test_stops_after_unknown_result(monkeypatch: pytest.MonkeyPatch, guard: BookingGuard) -> None:
    seen = _patch(
        monkeypatch,
        [
            BookingResult(ok=False, unknown=True, error="ответ потерян"),
            BookingResult(ok=True, booking_number="ДОЛЖЕН БЫТЬ НЕДОСТИЖИМ"),
        ],
    )
    result = railway_check.attempt_bookings(TRIPS, guard, verbose=False)
    assert result is not None and result.unknown is True
    assert seen == [1], "после неопределённого ответа перебор рейсов обязан остановиться"


def test_stops_when_post_raises_result_unknown(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    seen = _patch(monkeypatch, [ResultUnknown("read timeout"), BookingResult(ok=True)])
    result = railway_check.attempt_bookings(TRIPS, guard, verbose=False)
    assert result is not None and result.unknown is True
    assert seen == [1]


def test_unexpected_exception_is_treated_as_unknown(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    """Незнакомая ошибка на пути брони — не повод пробовать следующий рейс."""
    seen = _patch(monkeypatch, [KeyError("seat_id"), BookingResult(ok=True)])
    result = railway_check.attempt_bookings(TRIPS, guard, verbose=False)
    assert result is not None and result.unknown is True
    assert seen == [1]


def test_certain_failure_advances_to_next_trip(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    seen = _patch(
        monkeypatch,
        [
            BookingResult(ok=False, error="мест нет"),
            BookingResult(ok=False, error="мест нет"),
            BookingResult(ok=True, booking_number="RW-3"),
        ],
    )
    result = railway_check.attempt_bookings(TRIPS, guard, verbose=False)
    assert result is not None and result.ok is True
    assert seen == [1, 2, 3]


def test_stops_on_first_success(monkeypatch: pytest.MonkeyPatch, guard: BookingGuard) -> None:
    seen = _patch(monkeypatch, [BookingResult(ok=True, booking_number="RW-1")])
    result = railway_check.attempt_bookings(TRIPS, guard, verbose=False)
    assert result is not None and result.ok
    assert seen == [1]


def test_guard_blocks_next_run_after_unknown(tmp_path: Path) -> None:
    """Перезапуск после неопределённой попытки не должен бронировать заново."""
    path = tmp_path / "bookings.json"
    first = BookingGuard(path)
    first.begin("k1")
    first.mark_unknown("k1", "read timeout")

    fresh = BookingGuard(path)
    blocker = fresh.blocking_attempt()
    assert blocker is not None and blocker.status == UNKNOWN
    assert railway_check.check_booking_guard(fresh, force_rebook=False) == 2


def test_force_rebook_unblocks(tmp_path: Path) -> None:
    path = tmp_path / "bookings.json"
    guard = BookingGuard(path)
    guard.begin("k1")
    guard.mark_unknown("k1", "read timeout")
    assert railway_check.check_booking_guard(guard, force_rebook=True) is None
    assert guard.blocking_attempt() is None
