"""Тесты WAGON_TYPE_ID и fallback по типам вагона."""


import pytest

from railway_trips import (
    trip_first_available_wagon_id,
    trip_has_seats,
    wagon_type_ids,
    wagon_type_label,
)


def _trip(*wagon_specs: tuple[int, bool]) -> dict:
    return {
        "wagon_types": [
            {"wagon_type_id": wid, "wagon_type_title": wagon_type_label(wid), "has_seats": has_seats}
            for wid, has_seats in wagon_specs
        ]
    }


@pytest.fixture(autouse=True)
def _clear_wagon_env(monkeypatch):
    monkeypatch.delenv("WAGON_TYPE_ID", raising=False)


def test_wagon_type_ids_single(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3")
    assert wagon_type_ids() == [3]


def test_wagon_type_ids_fallback_list(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3, 2")
    assert wagon_type_ids() == [3, 2]


def test_trip_has_seats_any_configured_type(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    trip = _trip((3, False), (2, True))
    assert trip_has_seats(trip) is True


def test_trip_has_seats_false_when_none_available(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    trip = _trip((3, False), (2, False))
    assert trip_has_seats(trip) is False


def test_trip_first_available_wagon_id_respects_priority(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    trip = _trip((3, True), (2, True))
    assert trip_first_available_wagon_id(trip) == 3


def test_format_trip_stops_at_first_available(monkeypatch):
    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    from railway_trips import format_trip

    trip = _trip((3, True), (2, True))
    line = format_trip(trip)
    assert "Plaskart" in line
    assert "Kupe" not in line

    trip_fb = _trip((3, False), (2, True))
    line_fb = format_trip(trip_fb)
    assert "Plaskart" in line_fb and "нет мест" in line_fb
    assert "Kupe" in line_fb


def test_trip_resolved_wagon_id_alias(monkeypatch):
    from railway_trips import trip_first_available_wagon_id, trip_resolved_wagon_id

    monkeypatch.setenv("WAGON_TYPE_ID", "3,2")
    trip = _trip((3, False), (2, True))
    assert trip_resolved_wagon_id(trip) == 2
    assert trip_first_available_wagon_id(trip) == 2
