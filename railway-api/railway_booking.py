"""POST /v2/bookings — бронирование и уведомление с URL оплаты.

Бронь — единственная неидемпотентная операция в проекте, поэтому здесь
максимально строгая семантика результата:

  ok=True                  бронь точно создана;
  ok=False, unknown=False  сервер достоверно отказал, брони НЕТ — можно
                           пробовать другой рейс;
  ok=False, unknown=True   ответ потерян или не разобран — бронь МОГЛА быть
                           создана. Повторять нельзя, монитор обязан
                           остановиться и показать это человеку.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.booking_guard import AlreadySettled, BookingGuard, booking_key
from notify_helper import notify
from passenger_loader import get_passengers
from railway_client import RequestFailed, ResultUnknown, api_call
from railway_env import RAILWAY_ROOT, env
from railway_seats import SelectedSeat
from railway_term import green, red
from railway_trips import (
    TripsError,
    normalize_train_date,
    normalize_train_time,
    wagon_type_label,
)

BOOKINGS_JOURNAL = RAILWAY_ROOT / "railway_bookings.json"


class BookingError(RuntimeError):
    pass


@dataclass
class BookingResult:
    ok: bool
    booking_number: str = ""
    order_number: str = ""
    form_url: str = ""
    expire_time: str = ""
    error: str = ""
    seats: list[SelectedSeat] | None = None
    unknown: bool = False
    warning: str = ""
    booking_key: str = ""

    @property
    def certain_failure(self) -> bool:
        """Брони на сервере точно нет — безопасно пробовать следующий рейс."""
        return not self.ok and not self.unknown


def auto_book_enabled() -> bool:
    return (env("RAILWAY_AUTO_BOOK") or "0").strip().lower() not in ("0", "false", "no")


def multimedia_enabled() -> bool:
    """Мультимедиа / Wi‑Fi в POST /v2/bookings (has_media_wifi). По умолчанию выкл."""
    return (env("RAILWAY_MULTIMEDIA") or "0").strip().lower() not in ("0", "false", "no")


def _to_railway_dob(raw: str) -> str:
    """passengers.json → DD-MM-YYYY для API."""
    text = (raw or "").strip()
    for sep in (".", "/", "-"):
        if sep in text:
            parts = text.split(sep)
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    year, month, day = parts[0], parts[1], parts[2]
                else:
                    day, month, year = parts[0], parts[1], parts[2]
                return f"{int(day):02d}-{int(month):02d}-{year}"
    raise BookingError(f"Не разобрал dob: {raw!r}")


def _gender_api(raw: str) -> str:
    key = (raw or "").strip().upper()
    if key in ("F", "FEMALE", "Ж", "W"):
        return "female"
    return "male"


def _normalize_phone(raw: str) -> str:
    text = re.sub(r"\D", "", raw or "")
    if not text:
        return ""
    if text.startswith("993"):
        return f"+{text}"
    if len(text) == 8:
        return f"+993{text}"
    return f"+{text}" if raw.startswith("+") else text


def _tariff_api(pax_type: str) -> str:
    if pax_type == "child":
        return "child"
    return "adult"


def build_booking_payload(
    *,
    journey_id: int,
    seats: list[SelectedSeat],
    passengers: list[dict] | None = None,
) -> dict[str, Any]:
    passengers = passengers or get_passengers(verbose=False)
    if len(passengers) != len(seats):
        raise BookingError(
            f"Пассажиров {len(passengers)}, мест {len(seats)} — числа должны совпадать"
        )

    first = passengers[0]
    contact_name = f"{first['first_name']} {first['last_name']}".strip()
    contact_email = first.get("email") or env("NOTIFY_EMAIL") or ""
    contact_phone = _normalize_phone(first.get("phone") or "")

    payload: dict[str, Any] = {
        "contact": {
            "email": contact_email,
            "main_contact": contact_name,
            "mobile": contact_phone,
        },
        "api_client": "ios",
        "bedding_type": "bedding",
        "passengers": [],
        "outbound": {
            "selected_journeys": [
                {
                    "seats": [
                        {"id": seat.seat_id, "train_wagon_id": seat.train_wagon_id}
                        for seat in seats
                    ],
                    "id": journey_id,
                }
            ]
        },
        "has_hygienic_supplies": "false",
        "has_media_wifi": "true" if multimedia_enabled() else "false",
    }

    for pax in passengers:
        payload["passengers"].append(
            {
                "gender": _gender_api(pax.get("gender", "M")),
                "tariff": _tariff_api(pax["type"]),
                "identity_number": pax["doc_number"],
                "surname": pax["last_name"],
                "dob": _to_railway_dob(pax["dob"]),
                "identity_type": "passport",
                "name": pax["first_name"],
            }
        )

    return payload


def _short(value: Any, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}… (+{len(text) - limit} симв.)"


def parse_booking_response(body: dict[str, Any] | str, *, status: int = 200) -> BookingResult:
    """
    Разбор ответа с явным различием «отказ» и «непонятно».

    Раньше отсутствие formUrl трактовалось как провал, и цикл шёл бронировать
    следующий рейс — при том что бронь уже могла быть создана.
    """
    if status >= 500:
        return BookingResult(
            ok=False, unknown=True, error=f"HTTP {status}, состояние брони неизвестно: {_short(body)}"
        )

    if not isinstance(body, dict):
        return BookingResult(ok=False, unknown=True, error=f"ответ не JSON: {_short(body)}")

    if not body.get("success"):
        message = str(body.get("message") or "").strip()
        if message:
            # Сервер явно отказал — брони нет.
            return BookingResult(ok=False, error=message)
        return BookingResult(ok=False, unknown=True, error=f"success=false без message: {_short(body)}")

    data = body.get("data")
    if not isinstance(data, dict):
        return BookingResult(ok=False, unknown=True, error=f"нет data: {_short(body)}")

    booking = data.get("booking")
    if not isinstance(booking, dict):
        return BookingResult(ok=False, unknown=True, error=f"нет data.booking: {_short(body)}")

    form_url = str(booking.get("formUrl") or "").strip()
    booking_number = str(booking.get("booking_number") or "").strip()

    if not form_url and not booking_number:
        return BookingResult(
            ok=False, unknown=True, error=f"нет formUrl и booking_number: {_short(booking)}"
        )

    # Есть номер брони — бронь существует, даже если ссылку на оплату не прислали.
    return BookingResult(
        ok=True,
        booking_number=booking_number,
        order_number=str(booking.get("orderNumber") or ""),
        form_url=form_url,
        expire_time=str(booking.get("expire_time") or ""),
        warning="" if form_url else "нет formUrl — ссылку на оплату придётся искать в приложении",
    )


def create_booking(
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    verbose: bool = True,
) -> tuple[int, dict[str, Any] | str]:
    """POST /v2/bookings. Бросает ResultUnknown, если ответ не получен."""
    if verbose:
        journey = payload.get("outbound", {}).get("selected_journeys", [{}])[0]
        seat_count = len(journey.get("seats") or [])
        print(f"[book-railway] POST /v2/bookings journey={journey.get('id')} seats={seat_count}")
    response = api_call(
        "POST",
        "/v2/bookings",
        body=payload,
        headers={"idempotency-key": idempotency_key},
        idempotent=False,
    )
    if verbose:
        print(f"[book-railway] ответ за {response.elapsed_ms:.0f} мс")
    return response.status, response.body  # type: ignore[return-value]


def compute_booking_key(
    *,
    trip_id: int | str,
    journey_id: int,
    seats: list[SelectedSeat],
    passengers: list[dict],
) -> str:
    """Один и тот же рейс + места + пассажиры → один и тот же ключ между запусками."""
    seat_ids = ",".join(str(s.seat_id) for s in sorted(seats, key=lambda s: s.seat_id))
    docs = ",".join(sorted(str(p.get("doc_number") or "") for p in passengers))
    return booking_key(env("DATE"), trip_id, journey_id, seat_ids, docs)


def booking_notify_body(
    *,
    source: str,
    destination: str,
    departure_time: str,
    passengers: list[dict],
    form_url: str,
    booking_number: str,
    seats: list[SelectedSeat] | None = None,
    wagon_type_id: int | None = None,
) -> str:
    names = ", ".join(f"{p['first_name']} {p['last_name']}".strip() for p in passengers)
    try:
        time_text = normalize_train_time(departure_time)
    except TripsError:
        time_text = (departure_time or "").strip()
    try:
        date_text = normalize_train_date(departure_time)
    except TripsError:
        try:
            date_text = normalize_train_date(env("DATE"))
        except TripsError:
            date_text = env("DATE")
    route = f"{source} → {destination}"
    if date_text:
        route += f" {date_text}"
    lines = [
        route,
        f"Отправление: {time_text}",
        f"Пассажир: {names}",
    ]
    if booking_number:
        lines.append(f"Бронь: {booking_number}")
    if seats:
        wagon_label = wagon_type_label(wagon_type_id) if wagon_type_id is not None else ""
        prev_wagon: int | None = None
        for seat in seats:
            if seat.wagon_number != prev_wagon:
                wagon_line = f"Вагон: {seat.wagon_number}"
                if wagon_label:
                    wagon_line += f" ({wagon_label})"
                lines.append(wagon_line)
                prev_wagon = seat.wagon_number
            lines.append(f"Место: {seat.seat_label} (этаж: {seat.level})")
    lines.append("")
    lines.append("Ссылка для оплаты:")
    lines.append(form_url)
    return "\n".join(lines)


def try_auto_book(
    *,
    journey: dict[str, Any],
    trip_id: int | str,
    seats: list[SelectedSeat],
    wagon_type_id: int | None = None,
    guard: BookingGuard | None = None,
    verbose: bool = True,
) -> BookingResult:
    journey_id = int(journey.get("id") or 0)
    if not journey_id:
        return BookingResult(ok=False, error="нет journey.id")

    passengers = get_passengers(verbose=False)
    try:
        payload = build_booking_payload(
            journey_id=journey_id,
            seats=seats,
            passengers=passengers,
        )
    except BookingError as exc:
        return BookingResult(ok=False, error=str(exc))

    key = compute_booking_key(
        trip_id=trip_id, journey_id=journey_id, seats=seats, passengers=passengers
    )
    guard = guard or BookingGuard(BOOKINGS_JOURNAL)

    previous = guard.get(key)
    if previous is not None and guard.is_settled(key):
        return BookingResult(
            ok=False,
            unknown=True,
            booking_key=key,
            seats=seats,
            error=(
                f"по этим местам уже была попытка брони "
                f"({previous.status}, {previous.started_at}) — повтор запрещён"
            ),
        )

    # Запись на диск ДО отправки: если процесс умрёт между POST и ответом,
    # следующий запуск увидит pending и не станет бронировать вслепую.
    try:
        idempotency_key = guard.begin(key)
    except AlreadySettled as exc:
        # Журнал знает про эту бронь больше, чем проверка выше. Запрос не шлём.
        return BookingResult(ok=False, unknown=True, booking_key=key, error=str(exc))

    try:
        status, body = create_booking(
            payload, idempotency_key=idempotency_key, verbose=verbose
        )
    except ResultUnknown as exc:
        guard.mark_unknown(key, str(exc))
        return BookingResult(
            ok=False,
            unknown=True,
            booking_key=key,
            seats=seats,
            error=f"ответ не получен, бронь могла быть создана: {exc}",
        )
    except RequestFailed as exc:
        # Запрос до сервера не дошёл — брони точно нет.
        guard.settle(key, ok=False, detail=str(exc))
        return BookingResult(
            ok=False, booking_key=key, seats=seats, error=f"запрос не ушёл: {exc}"
        )

    if verbose:
        print(f"[book-railway] → HTTP {status}")

    result = parse_booking_response(body, status=status)
    result.seats = seats
    result.booking_key = key

    if not result.ok:
        result.error = f"{result.error} (HTTP {status})"
        if result.unknown:
            guard.mark_unknown(key, result.error)
        else:
            guard.settle(key, ok=False, detail=result.error)
        return result

    guard.settle(
        key,
        ok=True,
        booking_number=result.booking_number,
        form_url=result.form_url,
        detail=result.warning,
    )

    source = str(journey.get("source") or "")
    destination = str(journey.get("destination") or "")
    departure = str(journey.get("departure_time") or "")
    wagon_label = wagon_type_label(wagon_type_id) if wagon_type_id is not None else ""
    subject = f"Railway: бронь {result.booking_number or 'OK'}"
    if wagon_label:
        subject += f" — {wagon_label}"
    subject += f" — {source}→{destination}"
    body_text = booking_notify_body(
        source=source,
        destination=destination,
        departure_time=departure,
        passengers=passengers,
        form_url=result.form_url,
        booking_number=result.booking_number,
        seats=seats,
        wagon_type_id=wagon_type_id,
    )
    print(green("\n=== URL БАНКА ==="))
    print(green(body_text))
    if result.warning:
        print(red(f"[book-railway] {result.warning}"))
    # Письмо уходит в фоне: бронь уже создана, держать процесс на SMTP незачем.
    notify(subject, body_text, wait_gmail=False)
    if verbose:
        print(green(f"[book-railway] booking={result.booking_number}"))
    return result
