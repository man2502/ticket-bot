"""GET /v2/trips/{id} — схема вагонов и мест."""

from __future__ import annotations

from typing import Any

from railway_client import api_request
from railway_trips import railway_pax_counts, wagon_type_id


class TripDetailError(RuntimeError):
    pass


def fetch_trip_detail(
    trip_id: int | str,
    *,
    wagon_type_id_param: int | None = None,
    adult: int | None = None,
    child: int | None = None,
) -> dict[str, Any]:
    wanted_wagon = wagon_type_id() if wagon_type_id_param is None else wagon_type_id_param
    if wanted_wagon is None:
        raise TripDetailError("WAGON_TYPE_ID обязателен для GET /v2/trips/{id}")

    if adult is None or child is None:
        pax_adult, pax_child = railway_pax_counts(verbose=False)
        adult = pax_adult if adult is None else adult
        child = pax_child if child is None else child

    status, body = api_request(
        "GET",
        f"/v2/trips/{trip_id}",
        query={
            "adult": adult,
            "child": child,
            "outbound_wagon_type_id": wanted_wagon,
        },
    )
    if status != 200:
        raise TripDetailError(f"trip/{trip_id} → HTTP {status}: {body}")

    if not isinstance(body, dict) or not body.get("success"):
        raise TripDetailError(f"trip/{trip_id}: success=false: {body!r}")

    data = body.get("data")
    if not isinstance(data, dict):
        raise TripDetailError(f"trip/{trip_id}: нет data: {body!r}")

    outbound = data.get("outbound")
    if not isinstance(outbound, dict):
        raise TripDetailError(f"trip/{trip_id}: нет data.outbound: {body!r}")

    return outbound


def trip_detail_journeys(detail: dict[str, Any]) -> list[dict[str, Any]]:
    journeys = detail.get("journeys")
    if not isinstance(journeys, list):
        return []
    return [j for j in journeys if isinstance(j, dict)]


def trip_detail_wagons(journey: dict[str, Any], *, wanted_wagon_id: int | None = None) -> list[dict[str, Any]]:
    wagons = journey.get("train_wagons")
    if not isinstance(wagons, list):
        return []
    result = [w for w in wagons if isinstance(w, dict)]
    if wanted_wagon_id is not None:
        result = [w for w in result if w.get("wagon_type_id") == wanted_wagon_id]
    return result


def find_seats_for_trip(
    trip_id: int | str,
    passenger_count: int,
    *,
    wagon_ids: list[int] | None = None,
    verbose: bool = False,
) -> tuple[int, dict[str, Any], list[Any]] | None:
    """
    Перебирает WAGON_TYPE_ID по приоритету (3,2 → сначала plaskart, потом kupe).
    Возвращает (wagon_id, journey, seats) или None.
    """
    from railway_seats import select_seats
    from railway_trips import wagon_type_ids, wagon_type_label

    ids = wagon_ids if wagon_ids is not None else wagon_type_ids()
    if not ids:
        return None

    last_error: TripDetailError | None = None
    for wid in ids:
        try:
            detail = fetch_trip_detail(trip_id, wagon_type_id_param=wid)
        except TripDetailError as exc:
            # Ошибка по одному типу (чаще всего boardingNotFound — мест в нём
            # уже нет) не повод бросать остальные: Kupe пуст, а VIP Kupe есть.
            last_error = exc
            if verbose and len(ids) > 1:
                print(f"[railway] {wagon_type_label(wid)} ({wid}) — {exc}, дальше…")
            continue
        journeys = trip_detail_journeys(detail)
        if not journeys:
            if verbose and len(ids) > 1:
                print(f"[railway] {wagon_type_label(wid)} ({wid}) — нет journeys, дальше…")
            continue
        journey = journeys[0]
        wagons = trip_detail_wagons(journey, wanted_wagon_id=wid)
        seats = select_seats(wagons, passenger_count, wagon_type_id=wid)
        if seats:
            if verbose and len(ids) > 1:
                print(f"[railway] {wagon_type_label(wid)} ({wid}) — места найдены")
            return wid, journey, seats
        if verbose and len(ids) > 1:
            print(f"[railway] {wagon_type_label(wid)} ({wid}) — нет подходящих мест, дальше…")
    if last_error is not None:
        # Ни один тип не дал мест, и хотя бы один — ошибку: её и показываем,
        # иначе в логе было бы безликое «нет свободных мест».
        raise last_error
    return None
