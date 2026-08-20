"""Тесты выбора мест Railway."""

from railway_seats import PLASKART_WAGON_TYPE_ID, KUPE_WAGON_TYPE_ID, select_seats, seat_tier


def _plaskart_wagon(number: int, available_labels: list[int], total: int = 60) -> dict:
    seats = []
    for i in range(1, total + 1):
        level = ((i - 1) % 3) + 1
        seats.append({"id": i, "label": str(i), "level": str(level), "available": i in available_labels})
    return {"id": number * 100, "number": number, "wagon_type_id": PLASKART_WAGON_TYPE_ID, "seats": seats}


def _kupe_wagon(number: int, available_labels: list[int], total: int = 36) -> dict:
    seats = []
    for i in range(1, total + 1):
        seats.append({"id": i, "label": str(i), "level": "1", "available": i in available_labels})
    return {"id": number * 100, "number": number, "wagon_type_id": KUPE_WAGON_TYPE_ID, "seats": seats}


def test_plaskart_prefers_low_level_and_avoids_tail_sections():
    wagon = _plaskart_wagon(1, available_labels=[1, 2, 49, 50])
    picked = select_seats([wagon], 1, wagon_type_id=PLASKART_WAGON_TYPE_ID)
    assert picked is not None
    assert picked[0].seat_label in ("1", "2")
    assert picked[0].tier == 0


def test_plaskart_falls_back_to_tail_when_only_there():
    wagon = _plaskart_wagon(1, available_labels=[59, 60])
    picked = select_seats([wagon], 1, wagon_type_id=PLASKART_WAGON_TYPE_ID)
    assert picked is not None
    assert picked[0].seat_label in ("59", "60")
    assert picked[0].tier >= 2


def test_kupe_avoids_last_eight():
    wagon = _kupe_wagon(1, available_labels=[1, 29, 30])
    picked = select_seats([wagon], 1, wagon_type_id=KUPE_WAGON_TYPE_ID)
    assert picked is not None
    assert picked[0].seat_label == "1"
    assert seat_tier(
        {"label": "29", "level": "1"},
        wagon={"seats": [{"label": str(i)} for i in range(1, 37)]},
        wagon_type_id=KUPE_WAGON_TYPE_ID,
    ) >= 2


def test_picks_same_wagon_for_two_passengers():
    w1 = _plaskart_wagon(1, available_labels=[1])
    w2 = _plaskart_wagon(2, available_labels=[7, 8])
    picked = select_seats([w1, w2], 2, wagon_type_id=PLASKART_WAGON_TYPE_ID)
    assert picked is not None
    assert len(picked) == 2
    assert picked[0].wagon_number == picked[1].wagon_number == 2
