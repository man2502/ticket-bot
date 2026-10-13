"""GET /v2/trips — поиск поездов."""

from __future__ import annotations

import re
from typing import Any

from railway_client import api_request
from railway_env import env, to_railway_date


class TripsError(RuntimeError):
    pass


def railway_pax_counts(*, verbose: bool = False) -> tuple[int, int]:
    """(adult, child) для query — из PASSENGER_IDS / passengers.json."""
    from passenger_loader import get_passenger_counts_by_type

    adult, child, infant = get_passenger_counts_by_type(verbose=verbose)
    return adult, child + infant


def fetch_trips(
    *,
    source: str | None = None,
    destination: str | None = None,
    date: str | None = None,
    adult: int | None = None,
    child: int | None = None,
) -> list[dict[str, Any]]:
    source_id = (source or env("SOURCE_STATION_ID")).strip()
    destination_id = (destination or env("DESTINATION_STATION_ID")).strip()
    date_raw = date or env("DATE")
    if not source_id or not destination_id:
        raise TripsError("SOURCE_STATION_ID и DESTINATION_STATION_ID обязательны")

    if adult is None or child is None:
        pax_adult, pax_child = railway_pax_counts(verbose=False)
        adult = pax_adult if adult is None else adult
        child = pax_child if child is None else child

    status, body = api_request(
        "GET",
        "/v2/trips",
        query={
            "source": source_id,
            "destination": destination_id,
            "date": to_railway_date(date_raw),
            "adult": adult,
            "child": child,
        },
    )
    if status != 200:
        raise TripsError(f"trips → HTTP {status}: {body}")

    if not isinstance(body, dict):
        raise TripsError(f"trips: не JSON-объект: {body!r}")

    if not body.get("success"):
        raise TripsError(f"trips: success=false: {body!r}")

    data = body.get("data")
    if not isinstance(data, dict):
        raise TripsError(f"trips: нет data: {body!r}")

    trips = data.get("trips")
    if not isinstance(trips, list):
        raise TripsError(f"trips: нет data.trips: {body!r}")

    return trips


WAGON_TYPE_LABELS: dict[int, str] = {
    2: "Kupe",
    3: "Plaskart",
    5: "VIP Kupe",
}


def wagon_type_ids() -> list[int]:
    """WAGON_TYPE_ID=3 или 3,2 — приоритет слева направо (fallback)."""
    raw = env("WAGON_TYPE_ID").strip()
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError as exc:
            raise TripsError(f"WAGON_TYPE_ID: не число: {part!r} (из {raw!r})") from exc
    return ids


def wagon_type_id() -> int | None:
    """Первый тип из WAGON_TYPE_ID (для совместимости)."""
    ids = wagon_type_ids()
    return ids[0] if ids else None


def wagon_type_label(wagon_id: int) -> str:
    return WAGON_TYPE_LABELS.get(wagon_id, f"type{wagon_id}")


def wagon_type_selector_label() -> str | None:
    ids = wagon_type_ids()
    if not ids:
        return None
    if len(ids) == 1:
        return f"wagon{ids[0]}"
    return "wagon" + "-".join(str(wid) for wid in ids)


def train_index() -> int | None:
    """TRAIN_INDEX: 1 = первый рейс в списке ./run.sh --trips ([1], [2], …)."""
    raw = env("TRAIN_INDEX").strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError as exc:
        raise TripsError(f"TRAIN_INDEX: не число: {raw!r}") from exc
    if idx < 1:
        raise TripsError(
            f"TRAIN_INDEX: нумерация с 1, как в ./run.sh --trips — [1] первый, [2] второй; получено {idx}"
        )
    return idx


def train_time() -> str | None:
    raw = env("TRAIN_TIME").strip()
    return raw or None


def train_run_number() -> str | None:
    raw = env("TRAIN_RUN_NUMBER").strip()
    return raw or None


def railway_strict() -> bool:
    """RAILWAY_STRICT=1 → только выбранный рейс, без запасных."""
    return (env("RAILWAY_STRICT") or "0").strip().lower() not in ("0", "false", "no", "")


def railway_strict_label() -> str:
    if railway_strict():
        return "строго (только выбранный рейс)"
    return "с запасным выбором (если на выбранном нет мест)"


def train_selector_label() -> str | None:
    idx = train_index()
    if idx is not None:
        return f"idx{idx}"
    time_wanted = train_time()
    if time_wanted:
        return f"time{normalize_train_time(time_wanted)}"
    run_no = train_run_number()
    if run_no:
        return f"train{run_no}"
    return None


def trip_train_number(trip: dict[str, Any]) -> str:
    journeys = trip.get("journeys")
    if isinstance(journeys, list) and journeys:
        return str((journeys[0] or {}).get("train_run_number") or "").strip()
    return ""


def normalize_train_time(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise TripsError("TRAIN_TIME пуст")
    if "T" in text:
        clock = text.split("+")[0].split("T", 1)[-1]
    elif " " in text:
        clock = text.split(" ", 1)[-1]
    else:
        clock = text
    parts = clock.split(":")
    if len(parts) < 2:
        raise TripsError(f"TRAIN_TIME: нужен HH:MM, получено {raw!r}")
    hour, minute = int(parts[0]), int(parts[1])
    return f"{hour:02d}:{minute:02d}"


def normalize_train_date(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise TripsError("дата отправления пуста")
    if "T" in text:
        date_part = text.split("T", 1)[0]
    elif " " in text:
        date_part = text.split(" ", 1)[0]
    else:
        date_part = text
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_part):
        year, month, day = date_part.split("-")
        return f"{day}.{month}.{year[-2:]}"
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", date_part):
        day, month, year = date_part.split(".")
        return f"{day}.{month}.{year[-2:]}"
    if re.match(r"^\d{2}\.\d{2}\.\d{2}$", date_part):
        return date_part
    raise TripsError(f"дата отправления: не разобрал {raw!r}")


def trip_departure_hm(trip: dict[str, Any]) -> str:
    return normalize_train_time(str(trip.get("departure_time") or ""))


def sort_trips(trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(trips, key=lambda trip: str(trip.get("departure_time") or ""))


def format_trip_selector(trip: dict[str, Any], *, index: int | None = None) -> str:
    dep = _format_time(str(trip.get("departure_time") or ""))
    src = trip.get("source") or "?"
    dst = trip.get("destination") or "?"
    train = trip_train_number(trip)
    train_part = f" №{train}" if train else ""
    prefix = f"[{index}] " if index is not None else ""
    return f"{prefix}{dep} {src}→{dst}{train_part}"


def print_trip_catalog(trips: list[dict[str, Any]]) -> None:
    ordered = sort_trips(trips)
    if not ordered:
        return
    print("[railway] Рейсы (TRAIN_INDEX: 1 = [1], 2 = [2], …):")
    for index, trip in enumerate(ordered, start=1):
        print(f"  {format_trip_selector(trip, index=index)}")


def filter_watched_trips(trips: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    ordered = sort_trips(trips)

    idx = train_index()
    if idx is not None:
        if idx > len(ordered):
            return [], f"рейс #{idx} не найден ({len(ordered)} поезд. в ответе)"
        trip = ordered[idx - 1]
        return [trip], None

    time_wanted = train_time()
    if time_wanted:
        wanted_hm = normalize_train_time(time_wanted)
        filtered = [trip for trip in ordered if trip_departure_hm(trip) == wanted_hm]
        if filtered:
            return filtered, None
        available = ", ".join(trip_departure_hm(trip) for trip in ordered) or "—"
        return [], f"рейс на {wanted_hm} не найден (есть: {available})"

    run_no = train_run_number()
    if run_no:
        filtered = [trip for trip in ordered if trip_train_number(trip) == run_no]
        if filtered:
            return filtered, None
        return [], f"рейс №{run_no} не найден ({len(ordered)} поезд. в ответе)"

    return ordered, None


def resolve_booking_trips(
    watched_trips: list[dict[str, Any]],
    all_trips: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """
    Рейсы для «есть места» и брони.

    Сначала выбранный (TRAIN_INDEX / TRAIN_TIME).
    RAILWAY_STRICT=0: если на нём нет мест — другие рейсы с нужным типом вагона.
    RAILWAY_STRICT=1: только выбранный, без запасных.

    Возвращает (trips, used_fallback).
    """
    preferred = available_trips(watched_trips)
    if preferred:
        return preferred, False
    if railway_strict():
        return [], False
    watched_ids = {trip.get("id") for trip in watched_trips}
    others = [
        trip
        for trip in sort_trips(all_trips)
        if trip.get("id") not in watched_ids and trip_has_seats(trip)
    ]
    return others, bool(others)


def _wagon_matches(wagon: dict[str, Any], *, wanted_id: int | None) -> bool:
    if wanted_id is None:
        return True
    return wagon.get("wagon_type_id") == wanted_id


def _trip_wagon_has_seats(trip: dict[str, Any], wanted_id: int | None) -> bool:
    wagon_types = trip.get("wagon_types")
    if not isinstance(wagon_types, list):
        return False
    return any(
        bool(w.get("has_seats"))
        for w in wagon_types
        if isinstance(w, dict) and _wagon_matches(w, wanted_id=wanted_id)
    )


def trip_resolved_wagon_id(trip: dict[str, Any]) -> int | None:
    """Первый тип из WAGON_TYPE_ID по порядку списка, где есть места (3 → потом 2)."""
    for wid in wagon_type_ids():
        if _trip_wagon_has_seats(trip, wid):
            return wid
    return None


def trip_wagon_ids_with_seats(trip: dict[str, Any]) -> list[int]:
    """
    Типы из WAGON_TYPE_ID (в их порядке), где, по списку рейсов, есть места.

    Деталь рейса по типу без мест сервер не отдаёт вовсе — отвечает ошибкой
    `roundtrip.boardingNotFound`. Спрашивать его о таких типах незачем.
    Если в рейсе нет сведений о вагонах — все настроенные типы, как раньше.
    """
    configured = wagon_type_ids()
    if not isinstance(trip.get("wagon_types"), list):
        return configured
    return [wid for wid in configured if _trip_wagon_has_seats(trip, wid)]


def trip_has_seats(trip: dict[str, Any], *, wanted_wagon_id: int | None = None) -> bool:
    if wanted_wagon_id is not None:
        return _trip_wagon_has_seats(trip, wanted_wagon_id)
    configured = wagon_type_ids()
    if configured:
        return trip_resolved_wagon_id(trip) is not None
    return _trip_wagon_has_seats(trip, None)


def trip_first_available_wagon_id(trip: dict[str, Any]) -> int | None:
    """Alias — см. trip_resolved_wagon_id."""
    return trip_resolved_wagon_id(trip)


def available_trips(trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [trip for trip in trips if trip_has_seats(trip)]


def _format_time(iso: str) -> str:
    text = (iso or "").strip()
    if not text:
        return ""
    try:
        return normalize_train_time(text)
    except TripsError:
        return text


def _format_wagon_seat_line(wagon: dict[str, Any]) -> str:
    title = wagon.get("wagon_type_title") or wagon_type_label(int(wagon.get("wagon_type_id") or 0))
    price = wagon.get("price")
    if wagon.get("has_seats"):
        return f"{title} {price} TMT" if price is not None else title
    return f"{title} — нет мест"


def _trip_wagon_entry(trip: dict[str, Any], wanted_id: int) -> dict[str, Any] | None:
    wagon_types = trip.get("wagon_types")
    if not isinstance(wagon_types, list):
        return None
    for wagon in wagon_types:
        if isinstance(wagon, dict) and _wagon_matches(wagon, wanted_id=wanted_id):
            return wagon
    return None


def format_trip(trip: dict[str, Any]) -> str:
    dep = _format_time(str(trip.get("departure_time") or ""))
    src = trip.get("source") or "?"
    dst = trip.get("destination") or "?"
    train = trip_train_number(trip)
    configured = wagon_type_ids()
    seats: list[str] = []
    if configured:
        resolved = trip_resolved_wagon_id(trip)
        for wanted_id in configured:
            wagon = _trip_wagon_entry(trip, wanted_id)
            if wagon is not None:
                seats.append(_format_wagon_seat_line(wagon))
            else:
                seats.append(f"{wagon_type_label(wanted_id)} — нет в ответе")
            if resolved is not None and wanted_id == resolved:
                break
    else:
        wagon_types = trip.get("wagon_types")
        if isinstance(wagon_types, list):
            for wagon in wagon_types:
                if isinstance(wagon, dict):
                    seats.append(_format_wagon_seat_line(wagon))
    seat_text = ", ".join(seats) if seats else "нет мест"
    train_part = f" №{train}" if train else ""
    return f"{dep} {src}→{dst}{train_part} — {seat_text}"


def format_watched_train_log(trip: dict[str, Any]) -> str:
    """Краткая строка для лога: «  19:50  поезд №03»."""
    dep = trip_departure_hm(trip)
    train = trip_train_number(trip)
    if train:
        return f"  {dep}  поезд №{train}"
    return f"  {dep}"


def trip_has_any_seats(trip: dict[str, Any]) -> bool:
    """Есть ли места в любом типе вагона (без учёта WAGON_TYPE_ID)."""
    wagon_types = trip.get("wagon_types")
    if not isinstance(wagon_types, list):
        return False
    return any(isinstance(w, dict) and w.get("has_seats") for w in wagon_types)


def _trip_available_wagon_lines(trip: dict[str, Any]) -> list[str]:
    wagon_types = trip.get("wagon_types")
    if not isinstance(wagon_types, list):
        return []
    lines: list[str] = []
    for wagon in wagon_types:
        if isinstance(wagon, dict) and wagon.get("has_seats"):
            lines.append(_format_wagon_seat_line(wagon))
    return lines


def format_trip_any_seats(trip: dict[str, Any]) -> str:
    """Строка рейса — все типы вагонов с местами (для альтернативных рейсов)."""
    dep = _format_time(str(trip.get("departure_time") or ""))
    src = trip.get("source") or "?"
    dst = trip.get("destination") or "?"
    train = trip_train_number(trip)
    seats = _trip_available_wagon_lines(trip)
    seat_text = ", ".join(seats) if seats else "нет мест"
    train_part = f" №{train}" if train else ""
    return f"{dep} {src}→{dst}{train_part} — {seat_text}"


def watched_trip_wagon_notes(trip: dict[str, Any]) -> list[str]:
    """
    Заметки о других типах вагонов на отслеживаемом рейсе.
    Например: Plaskart нет, доступен Kupe 150 TMT.
    """
    configured = wagon_type_ids()
    if not configured:
        return []

    primary = configured[0]
    primary_has = _trip_wagon_has_seats(trip, primary)
    resolved = trip_resolved_wagon_id(trip)

    if not primary_has and resolved is not None and resolved != primary:
        wagon = _trip_wagon_entry(trip, resolved)
        alt_line = _format_wagon_seat_line(wagon) if wagon else wagon_type_label(resolved)
        return [f"{wagon_type_label(primary)} нет мест, доступен {alt_line}"]

    if len(configured) == 1 and not primary_has:
        notes: list[str] = []
        for wagon in trip.get("wagon_types") or []:
            if not isinstance(wagon, dict) or not wagon.get("has_seats"):
                continue
            if wagon.get("wagon_type_id") != primary:
                notes.append(f"доступен {_format_wagon_seat_line(wagon)}")
        return notes

    return []


def alternative_trains_with_seats(
    watched_trips: list[dict[str, Any]],
    all_trips: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[str]:
    """Другие рейсы на дату с местами (любой тип вагона)."""
    if not watched_trips or len(all_trips) <= len(watched_trips):
        return []
    watched_ids = {trip.get("id") for trip in watched_trips}
    others = [
        trip
        for trip in sort_trips(all_trips)
        if trip.get("id") not in watched_ids and trip_has_any_seats(trip)
    ]
    return [format_trip_any_seats(trip) for trip in others[:limit]]


def collect_alternatives(
    watched_trips: list[dict[str, Any]],
    all_trips: list[dict[str, Any]] | None,
) -> tuple[list[str], list[str]]:
    """(заметки по вагонам на watched-рейсе, другие рейсы с местами)."""
    wagon_notes: list[str] = []
    for trip in watched_trips:
        wagon_notes.extend(watched_trip_wagon_notes(trip))
    other_trains: list[str] = []
    if all_trips is not None:
        other_trains = alternative_trains_with_seats(watched_trips, all_trips)
    return wagon_notes, other_trains


def format_alternatives_notice(
    wagon_notes: list[str],
    other_trains: list[str],
) -> str:
    parts: list[str] = []
    if wagon_notes:
        parts.append("Другие типы вагонов:\n" + "\n".join(f"  • {note}" for note in wagon_notes))
    if other_trains:
        parts.append("Другие рейсы с местами:\n" + "\n".join(f"  • {line}" for line in other_trains))
    return "\n\n".join(parts)


def alternatives_signature(wagon_notes: list[str], other_trains: list[str]) -> str:
    return "|".join(wagon_notes + other_trains)


def summarize_trips(
    trips: list[dict[str, Any]],
    *,
    all_trips: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    if not trips:
        return False, ["поездов нет"]

    with_seats = available_trips(trips)
    if with_seats:
        return True, [format_trip(trip) for trip in with_seats]

    if len(trips) == 1 and all_trips and len(all_trips) > 1:
        trip = trips[0]
        head = f"мест нет —{format_watched_train_log(trip)}"
    else:
        head = f"мест нет ({len(trips)} поезд.)"

    samples = [format_trip(trip).replace(" — нет мест", "") for trip in trips[:3]]
    return False, [head] + samples
