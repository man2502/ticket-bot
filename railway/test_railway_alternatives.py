"""Тесты уведомлений об альтернативных вагонах и рейсах."""


import pytest

from railway_trips import (
    alternative_trains_with_seats,
    collect_alternatives,
    format_alternatives_notice,
    trip_has_any_seats,
    watched_trip_wagon_notes,
    wagon_type_label,
)


def _trip(
    trip_id: int,
    *,
    dep: str = "2026-03-01T13:50:00",
    train: str = "03",
    wagon_specs: tuple[tuple[int, bool, int | None], ...] = ((3, True, 120),),
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
                **({"price": price} if price is not None else {}),
            }
            for wid, has_seats, price in wagon_specs
        ],
    }


@pytest.fixture(autouse=True)
def _clear_wagon_env(monkeypatch):
    monkeypatch.delenv("WAGON_TYPE_ID", raising=False)


def test_watched_trip_wagon_notes_fallback(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    trip = _trip(1, wagon_specs=((3, False, 120), (2, True, 150)))
    notes = watched_trip_wagon_notes(trip)
    assert len(notes) == 1
    assert "Plaskart нет мест" in notes[0]
    assert "Kupe" in notes[0]


def test_watched_trip_wagon_notes_other_type_only_configured(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3")
    trip = _trip(1, wagon_specs=((3, False, 120), (2, True, 150)))
    notes = watched_trip_wagon_notes(trip)
    assert len(notes) == 1
    assert "доступен Kupe" in notes[0]


def test_watched_trip_wagon_notes_empty_when_primary_has_seats(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    trip = _trip(1, wagon_specs=((3, True, 120), (2, True, 150)))
    assert watched_trip_wagon_notes(trip) == []


def test_alternative_trains_with_seats(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3")
    watched = [_trip(1, dep="2026-03-01T13:50:00", wagon_specs=((3, False, 120),))]
    other = _trip(2, dep="2026-03-01T19:50:00", train="05", wagon_specs=((2, True, 150),))
    lines = alternative_trains_with_seats(watched, watched + [other])
    assert len(lines) == 1
    assert "19:50" in lines[0]
    assert "Kupe" in lines[0]


def test_alternative_trains_empty_when_all_watched():
    trips = [_trip(1), _trip(2)]
    assert alternative_trains_with_seats(trips, trips) == []


def test_trip_has_any_seats():
    assert trip_has_any_seats(_trip(1, wagon_specs=((3, False, 120), (2, True, 150)))) is True
    assert trip_has_any_seats(_trip(1, wagon_specs=((3, False, 120), (2, False, 150)))) is False


def test_collect_and_format_alternatives(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3")
    watched = [_trip(1, wagon_specs=((3, False, 120), (2, True, 150)))]
    other = _trip(2, dep="2026-03-01T19:50:00", wagon_specs=((3, True, 120),))
    wagon_notes, other_trains = collect_alternatives(watched, watched + [other])
    notice = format_alternatives_notice(wagon_notes, other_trains)
    assert "Другие типы вагонов" in notice
    assert "Другие рейсы с местами" in notice
    assert "19:50" in notice
