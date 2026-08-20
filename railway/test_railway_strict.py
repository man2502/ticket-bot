"""Тесты RAILWAY_STRICT: выбранный рейс vs запасные."""

import pytest

from railway_trips import resolve_booking_trips, wagon_type_label


def _trip(
    trip_id: int,
    *,
    dep: str = "2026-03-01T13:50:00",
    train: str = "95",
    wagon_specs: tuple[tuple[int, bool], ...] = ((3, True),),
) -> dict:
    return {
        "id": trip_id,
        "departure_time": dep,
        "source": "Ashgabat",
        "destination": "Mary",
        "journeys": [{"train_run_number": train}],
        "wagon_types": [
            {
                "wagon_type_id": wid,
                "wagon_type_title": wagon_type_label(wid),
                "has_seats": has_seats,
            }
            for wid, has_seats in wagon_specs
        ],
    }


@pytest.fixture(autouse=True)
def _wagon_env(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    monkeypatch.delenv("RAILWAY_STRICT", raising=False)


def test_prefers_watched_when_it_has_seats(monkeypatch):
    monkeypatch.setenv("RAILWAY_STRICT", "0")
    watched = [_trip(1, wagon_specs=((3, True),))]
    other = _trip(2, dep="2026-03-01T19:50:00", train="03", wagon_specs=((3, True),))
    trips, fallback = resolve_booking_trips(watched, watched + [other])
    assert fallback is False
    assert [t["id"] for t in trips] == [1]


def test_fallback_to_other_when_not_strict(monkeypatch):
    monkeypatch.setenv("RAILWAY_STRICT", "0")
    watched = [_trip(1, wagon_specs=((3, False), (2, False)))]
    other = _trip(2, dep="2026-03-01T19:50:00", train="03", wagon_specs=((3, True),))
    trips, fallback = resolve_booking_trips(watched, watched + [other])
    assert fallback is True
    assert [t["id"] for t in trips] == [2]


def test_strict_does_not_fallback(monkeypatch):
    monkeypatch.setenv("RAILWAY_STRICT", "1")
    watched = [_trip(1, wagon_specs=((3, False), (2, False)))]
    other = _trip(2, dep="2026-03-01T19:50:00", train="03", wagon_specs=((3, True),))
    trips, fallback = resolve_booking_trips(watched, watched + [other])
    assert fallback is False
    assert trips == []


def test_fallback_ignores_other_without_wanted_wagon(monkeypatch):
    monkeypatch.setenv("RAILWAY_STRICT", "0")
    monkeypatch.setenv("WAGON_TYPE_ID", "3")
    watched = [_trip(1, wagon_specs=((3, False),))]
    other = _trip(2, dep="2026-03-01T19:50:00", wagon_specs=((2, True), (5, True)))
    trips, fallback = resolve_booking_trips(watched, watched + [other])
    assert fallback is False
    assert trips == []
