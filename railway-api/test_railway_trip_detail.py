"""Тесты выбора типа вагона перед бронью.

Случай из жизни: WAGON_TYPE_ID=2,5, в Kupe мест нет, в VIP Kupe есть. Бот
писал «ЕСТЬ места», а потом спрашивал деталь рейса по Kupe, получал
`roundtrip.boardingNotFound` и бросал бронь, не дойдя до VIP Kupe.

Запуск (из корня репозитория):
    python run_tests.py railway-api/test_railway_trip_detail.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "railway-api")):
    if path not in sys.path:
        sys.path.insert(0, path)

import passenger_loader
import railway_check
import railway_seats
import railway_trip_detail
from railway_trip_detail import TripDetailError, find_seats_for_trip
from railway_trips import trip_wagon_ids_with_seats

KUPE, VIP = 2, 5
BOARDING_NOT_FOUND = TripDetailError(
    "trip/8927494: success=false: {'success': False, 'error': "
    "{'id': 'roundtrip.boardingNotFound'}}"
)

TRIP = {
    "id": 8927494,
    "wagon_types": [
        {"wagon_type_id": KUPE, "has_seats": False},
        {"wagon_type_id": VIP, "has_seats": True, "price": 90.2},
    ],
}


@pytest.fixture()
def kupe_then_vip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAGON_TYPE_ID", f"{KUPE},{VIP}")


def _patch_detail(monkeypatch: pytest.MonkeyPatch, by_wagon: dict[int, object]) -> list[int]:
    """Подменяет GET /v2/trips/{id}: по типу вагона — ответ или ошибка."""
    asked: list[int] = []

    def fake(trip_id, *, wagon_type_id_param=None, **_):  # noqa: ANN001, ARG001
        asked.append(wagon_type_id_param)
        outcome = by_wagon[wagon_type_id_param]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(railway_trip_detail, "fetch_trip_detail", fake)
    monkeypatch.setattr(
        railway_seats,
        "select_seats",
        lambda wagons, count, wagon_type_id=None: [f"seat-{wagon_type_id}"] if wagons else [],
    )
    return asked


def _detail(wagon_id: int) -> dict:
    return {"journeys": [{"train_wagons": [{"wagon_type_id": wagon_id}]}]}


def test_types_without_seats_are_skipped(kupe_then_vip) -> None:
    assert trip_wagon_ids_with_seats(TRIP) == [VIP]


def test_trip_without_wagon_info_keeps_all_types(kupe_then_vip) -> None:
    assert trip_wagon_ids_with_seats({"id": 1}) == [KUPE, VIP]


def test_error_on_one_type_moves_on_to_the_next(monkeypatch, kupe_then_vip) -> None:
    asked = _patch_detail(monkeypatch, {KUPE: BOARDING_NOT_FOUND, VIP: _detail(VIP)})

    wagon_id, _journey, seats = find_seats_for_trip(1, 2, wagon_ids=[KUPE, VIP])

    assert asked == [KUPE, VIP]
    assert wagon_id == VIP
    assert seats == [f"seat-{VIP}"]


def test_error_is_kept_when_no_type_had_seats(monkeypatch, kupe_then_vip) -> None:
    """Без мест и с ошибкой — показываем ошибку, а не безликое «мест нет»."""
    _patch_detail(monkeypatch, {KUPE: BOARDING_NOT_FOUND, VIP: {"journeys": []}})

    with pytest.raises(TripDetailError, match="boardingNotFound"):
        find_seats_for_trip(1, 2, wagon_ids=[KUPE, VIP])


def test_booking_asks_only_types_with_seats(monkeypatch, kupe_then_vip) -> None:
    seen: dict[str, object] = {}

    def fake_find(trip_id, count, *, wagon_ids=None, verbose=False):  # noqa: ANN001, ARG001
        seen["wagon_ids"] = wagon_ids
        return None

    monkeypatch.setattr(railway_check, "find_seats_for_trip", fake_find)
    monkeypatch.setattr(passenger_loader, "get_passengers", lambda verbose=False: [{}, {}])

    result = railway_check.try_book_available_trip(TRIP, verbose=False)

    assert seen["wagon_ids"] == [VIP]
    assert result.ok is False
