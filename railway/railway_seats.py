"""Выбор мест по правилам plaskart / kupe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Plaskart: 6 мест в секции, последние 2 секции у туалета.
PLASKART_WAGON_TYPE_ID = 3
PLASKART_SECTION_SIZE = 6
PLASKART_BAD_SECTIONS = 2

# Kupe / VIP Kupe: 4 места в купе, последние 8 мест в вагоне — у туалета.
KUPE_WAGON_TYPE_ID = 2
VIP_KUPE_WAGON_TYPE_ID = 5
KUPE_COMPARTMENT_SIZE = 4
KUPE_BAD_TAIL_SEATS = 8


@dataclass(frozen=True)
class SelectedSeat:
    seat_id: int
    seat_label: str
    level: int
    train_wagon_id: int
    wagon_number: int
    score: int
    tier: int


def _seat_label_num(seat: dict[str, Any]) -> int:
    try:
        return int(str(seat.get("label") or "0"))
    except ValueError:
        return 0


def _seat_level(seat: dict[str, Any]) -> int:
    try:
        return int(str(seat.get("level") or "3"))
    except ValueError:
        return 3


def _wagon_seat_count(wagon: dict[str, Any]) -> int:
    seats = wagon.get("seats")
    if not isinstance(seats, list):
        return 0
    labels = [_seat_label_num(s) for s in seats if isinstance(s, dict)]
    return max(labels) if labels else len(seats)


def _plaskart_bad_section(label_num: int, total_seats: int) -> bool:
    if label_num < 1 or total_seats < 1:
        return False
    total_sections = (total_seats + PLASKART_SECTION_SIZE - 1) // PLASKART_SECTION_SIZE
    section_idx = (label_num - 1) // PLASKART_SECTION_SIZE
    return section_idx >= total_sections - PLASKART_BAD_SECTIONS


def _kupe_bad_seat(label_num: int, total_seats: int) -> bool:
    if label_num < 1 or total_seats < 1:
        return False
    return label_num > total_seats - KUPE_BAD_TAIL_SEATS


def seat_is_bad(seat: dict[str, Any], *, wagon: dict[str, Any], wagon_type_id: int) -> bool:
    label_num = _seat_label_num(seat)
    total = _wagon_seat_count(wagon)
    if wagon_type_id == PLASKART_WAGON_TYPE_ID:
        return _plaskart_bad_section(label_num, total)
    if wagon_type_id in (KUPE_WAGON_TYPE_ID, VIP_KUPE_WAGON_TYPE_ID):
        return _kupe_bad_seat(label_num, total)
    return False


def seat_tier(seat: dict[str, Any], *, wagon: dict[str, Any], wagon_type_id: int) -> int:
    """
    0 — хороший этаж (1–2), не у туалета
    1 — этаж 3, не у туалета
    2 — у туалета, этаж 1–2
    3 — у туалета, этаж 3
    """
    level = _seat_level(seat)
    bad = seat_is_bad(seat, wagon=wagon, wagon_type_id=wagon_type_id)
    high = level >= 3
    if not bad and not high:
        return 0
    if not bad and high:
        return 1
    if bad and not high:
        return 2
    return 3


def seat_score(seat: dict[str, Any], *, wagon: dict[str, Any], wagon_type_id: int) -> int:
    tier = seat_tier(seat, wagon=wagon, wagon_type_id=wagon_type_id)
    level = _seat_level(seat)
    label_num = _seat_label_num(seat)
    wagon_no = int(wagon.get("number") or 0)
    # tier важнее всего; внутри tier — ниже этаж и номер места.
    return tier * 1000 + level * 10 + label_num + wagon_no * 0.001


def _available_seats(wagon: dict[str, Any]) -> list[dict[str, Any]]:
    seats = wagon.get("seats")
    if not isinstance(seats, list):
        return []
    return [s for s in seats if isinstance(s, dict) and s.get("available")]


def select_seats(
    wagons: list[dict[str, Any]],
    count: int,
    *,
    wagon_type_id: int,
) -> list[SelectedSeat] | None:
    if count < 1:
        return []

    ranked: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for wagon in wagons:
        wagon_id = int(wagon.get("id") or 0)
        wagon_no = int(wagon.get("number") or 0)
        for seat in _available_seats(wagon):
            score = seat_score(seat, wagon=wagon, wagon_type_id=wagon_type_id)
            ranked.append((score, wagon_no, wagon, seat))

    if not ranked:
        return None

    ranked.sort(key=lambda item: (item[0], item[1]))

    by_wagon: dict[int, list[tuple[int, dict[str, Any], dict[str, Any]]]] = {}
    for score, _wagon_no, wagon, seat in ranked:
        wagon_id = int(wagon.get("id") or 0)
        by_wagon.setdefault(wagon_id, []).append((score, wagon, seat))

    best_group: tuple[float, list[tuple[int, dict[str, Any], dict[str, Any]]]] | None = None
    for items in by_wagon.values():
        if len(items) < count:
            continue
        picked = items[:count]
        avg = sum(s for s, _, _ in picked) / count
        if best_group is None or avg < best_group[0]:
            best_group = (avg, picked)

    if best_group is None:
        if len(ranked) < count:
            return None
        picked = [(score, wagon, seat) for score, _no, wagon, seat in ranked[:count]]
    else:
        picked = best_group[1]

    result: list[SelectedSeat] = []
    for score, wagon, seat in picked:
        wagon_id = int(wagon.get("id") or 0)
        result.append(
            SelectedSeat(
                seat_id=int(seat["id"]),
                seat_label=str(seat.get("label") or ""),
                level=_seat_level(seat),
                train_wagon_id=wagon_id,
                wagon_number=int(wagon.get("number") or 0),
                score=int(score),
                tier=seat_tier(seat, wagon=wagon, wagon_type_id=wagon_type_id),
            )
        )
    return result


def format_selected_seats(seats: list[SelectedSeat]) -> str:
    parts = []
    for seat in seats:
        parts.append(f"вагон {seat.wagon_number} место {seat.seat_label} (этаж {seat.level})")
    return "; ".join(parts)
