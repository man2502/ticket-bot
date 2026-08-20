"""CreateBooking через API T5 iOS + уведомление с URL оплаты.

Бронь — единственная неидемпотентная операция модуля, поэтому результат здесь
трёхзначный, а не «получилось / не получилось»:

  ok=True                  бронь точно создана;
  ok=False, unknown=False  сервер достоверно отказал, брони НЕТ — можно
                           пробовать другой рейс;
  ok=False, unknown=True   ответ потерян или не разобран — бронь МОГЛА быть
                           создана. Повторять нельзя: второй CreateBooking даст
                           вторую настоящую бронь на тех же пассажиров и второй
                           счёт в банке.

Каждая попытка пишется в журнал (`api_bookings.json`) ДО отправки запроса —
чтобы падение процесса между POST и ответом не стёрло факт попытки.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API_ROOT = Path(__file__).resolve().parent
REPO_ROOT = API_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.booking_guard import BookingGuard, booking_key
from core.term import green
from api_client import RequestFailed, ResultUnknown, api_call
from api_env import env, to_api_date
from flight_index import (
    flight_array_index,
    flight_prefer_time,
    flight_selection_label as _selection_label,
    flight_strict,
    flight_want_indexes,
    format_flight_indexes,
)
from notify_helper import notify
from passenger_loader import get_passengers

# Журнал попыток брони. Отдельно от api_state.json: обычное сохранение состояния
# не должно иметь возможности затереть защиту от повторной брони.
BOOKINGS_JOURNAL = API_ROOT / "api_bookings.json"

SKIP_FLIGHT_RE = re.compile(
    r"мест\s*нет|транзит|сквозн|transit|connecting|пересадк|via\b|no\s*seat|sold\s*out",
    re.I,
)

CITIZEN_IDS = {
    "TM": 1,
    "TURKMENISTAN": 1,
    "TURKMEN": 1,
}

PAX_PREFIX = {
    "adult": "adult",
    "child": "child",
    "infant": "infant",
}

_green = green  # общая палитра, см. core.term


@dataclass
class FlightOffer:
    raw: dict[str, Any]
    summary: str
    search_id: int
    booking_class_id: int
    departure_time: str
    flight_number: str
    seat: int = 0
    list_index: int = 0
    skipped: bool = False


@dataclass
class AvailabilityInfo:
    search_key: str
    flights: list[FlightOffer]
    picked: FlightOffer | None = None


@dataclass
class BookingResult:
    ok: bool
    checkout_url: str = ""
    pnr: str = ""
    bank_order_id: str = ""
    error: str = ""
    flight: FlightOffer | None = None
    unknown: bool = False
    warning: str = ""
    booking_key: str = ""

    @property
    def certain_failure(self) -> bool:
        """Брони на сервере точно нет — безопасно пробовать следующий рейс."""
        return not self.ok and not self.unknown


def auto_book_enabled() -> bool:
    return (env("API_AUTO_BOOK") or "0").strip().lower() not in ("0", "false", "no")


def payment_type() -> str:
    return (env("API_PAYMENT_TYPE") or "halkbank").strip() or "halkbank"


def flight_selection_label() -> str:
    """Режим выбора рейса для стартового лога (разбор настроек — в flight_index)."""
    return _selection_label(fallback_hint="другие прямые с местами")


def _flight_skipped(flight: dict[str, Any], summary: str) -> bool:
    if SKIP_FLIGHT_RE.search(summary):
        return True
    stops = str(flight.get("stops") or "").lower()
    if stops and stops not in ("non-stop", "nonstop", "0", "direct"):
        if re.search(r"transit|connect|stop|пересад|транзит|сквозн", stops, re.I):
            return True
    return False


def _flight_has_time(offer: FlightOffer, prefer_time: str) -> bool:
    if not prefer_time:
        return False
    hay = f"{offer.departure_time} {offer.summary}".replace(".", ":")
    return prefer_time in hay


def _flight_selectable(offer: FlightOffer) -> bool:
    return offer.seat > 0 and not offer.skipped


def offers_from_selected(selected: list[Any]) -> list[FlightOffer]:
    offers: list[FlightOffer] = []
    for idx, flight in enumerate(selected):
        if not isinstance(flight, dict):
            continue
        seat = _flight_seat(flight) or 0
        summary = _flight_summary(flight)
        search_id, booking_class_id = _extract_flight_ids(flight, index=idx)
        offers.append(
            FlightOffer(
                raw=flight,
                summary=summary,
                search_id=search_id,
                booking_class_id=booking_class_id,
                departure_time=_extract_departure_time(flight),
                flight_number=_extract_flight_number(flight),
                seat=int(seat),
                list_index=idx,
                skipped=_flight_skipped(flight, summary),
            )
        )
    return offers


def _no_flights_on_date_msg(detail: str = "") -> str:
    date = env("DATE") or ""
    if date:
        text = f"на {date} пока нет рейсов, рейсы появятся не ранее чем за 30 дней"
    else:
        text = "на эту дату пока нет рейсов, рейсы появятся не ранее чем за 30 дней"
    if detail:
        return f"{text} ({detail})"
    return text


def pick_flight_offer(offers: list[FlightOffer]) -> tuple[FlightOffer | None, str | None]:
    """
    Выбор рейса по FLIGHT_INDEX / FLIGHT_TIME / FLIGHT_STRICT — как в steps.py.
    """
    if not offers:
        return None, _no_flights_on_date_msg("selectedDate пуст")

    strict = flight_strict()
    want_idxs = flight_want_indexes()
    prefer_time = flight_prefer_time()
    selectable = [o for o in offers if _flight_selectable(o)]
    idx_label = format_flight_indexes(want_idxs)

    if prefer_time:
        search_pool = offers if strict else selectable
        matches = [o for o in search_pool if _flight_has_time(o, prefer_time)]
        if matches:
            pick = matches[0]
            if not _flight_selectable(pick):
                return None, f"strict: рейс {prefer_time} без мест или транзит"
            return pick, None
        if strict:
            return None, f"strict: время {prefer_time} не найдено среди {len(offers)} рейсов"
        # не strict — fallback на index ниже

    preferred: list[FlightOffer] = []
    preferred_errors: list[str] = []
    for want_idx in want_idxs:
        array_idx = flight_array_index(want_idx)
        if array_idx >= len(offers):
            preferred_errors.append(f"[{want_idx}] вне списка")
            continue
        pick = offers[array_idx]
        if not _flight_selectable(pick):
            flight_no = pick.flight_number or "?"
            why = "транзит" if pick.skipped else "нет мест"
            preferred_errors.append(f"[{want_idx}] (flight={flight_no}) {why}")
            continue
        preferred.append(pick)

    if preferred:
        return preferred[0], None

    if strict:
        detail = " ".join(preferred_errors) or "нет совпадений"
        return None, f"strict: FLIGHT_INDEX={idx_label} — {detail}"

    if not selectable:
        return None, "нет доступных рейсов (места=0 или транзит)"
    return selectable[0], None


def count_direct_flights(body: dict[str, Any] | str) -> tuple[int, list[str]] | None:
    """Уникальные прямые рейсы (есть номер, не транзит)."""
    info = parse_availability(body)
    if info is None:
        if isinstance(body, dict) and body.get("success"):
            return 0, []
        return None
    labels: list[str] = []
    seen: set[str] = set()
    for offer in info.flights:
        num = (offer.flight_number or "").strip()
        if not num or offer.skipped or num in seen:
            continue
        seen.add(num)
        time_part = (offer.departure_time or "").strip()
        labels.append(f"{num} {time_part}".strip() if time_part else num)
    return len(labels), labels


def evaluate_api_availability(
    body: dict[str, Any] | str,
) -> tuple[bool, list[str], AvailabilityInfo | None]:
    """Есть ли места на выбранном рейсе + разобранный AvailabilityInfo."""
    info = parse_availability(body)
    if not info or not info.picked:
        if info and info.flights:
            _, err = pick_flight_offer(info.flights)
            return False, [err or "рейс не выбран"], info
        if not isinstance(body, dict) or not body.get("success"):
            msg = body.get("message") if isinstance(body, dict) else str(body)
            return False, [str(msg or "success=false")], None
        return False, [_no_flights_on_date_msg("нет selectedDate или searchKey")], None
    label = f"#{info.picked.list_index + 1}"
    if info.picked.flight_number:
        label += f" flight={info.picked.flight_number}"
    if info.picked.departure_time:
        label += f" time={info.picked.departure_time}"
    return True, [f"{label} | {info.picked.summary}"], info


def _parse_json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def normalize_availability_data(data: Any) -> dict[str, Any] | None:
    data = _parse_json_maybe(data)
    if not isinstance(data, dict):
        return None
    if "searchKey" in data:
        return data
    inner = _parse_json_maybe(data.get("data"))
    if isinstance(inner, dict) and "searchKey" in inner:
        return inner
    if "result" in data:
        return data
    return None


def _find_selected_date(data: Any) -> list[Any] | None:
    if isinstance(data, dict):
        raw = data.get("selectedDate")
        if isinstance(raw, list):
            return raw
        for val in data.values():
            found = _find_selected_date(val)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_selected_date(item)
            if found is not None:
                return found
    return None


def _first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        val = item.get(key)
        if val not in (None, "", []):
            return val
    return None


def _flight_seat(flight: Any) -> int | None:
    if not isinstance(flight, dict):
        return None
    raw = flight.get("seat")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        try:
            return int(float(str(raw).strip()))
        except ValueError:
            return None


def _flight_summary(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "flightNo",
        "flightNumber",
        "flight_number",
        "flightSegmentFlightNumber",
        "departureTime",
        "depTime",
        "time",
        "arrivalTime",
        "arrTime",
        "class",
        "cabin",
        "price",
        "amount",
        "seat",
        "searchId",
        "bookingClassId",
    ):
        val = item.get(key)
        if val not in (None, "", []):
            parts.append(f"{key}={val}")
    if parts:
        return " | ".join(parts)
    return json.dumps(item, ensure_ascii=False)[:200]


def _to_int_id(raw: Any, *, fallback: int | None = None) -> int | None:
    if raw in (None, ""):
        return fallback
    try:
        return int(str(raw).strip())
    except ValueError:
        return fallback


def _extract_flight_ids(flight: dict[str, Any], *, index: int) -> tuple[int, int]:
    search_id = _to_int_id(
        _first_value(flight, "searchId", "search_id", "id", "flightSearchId"),
        fallback=index + 1,
    )
    booking_class_id = _to_int_id(
        _first_value(
            flight,
            "bookingClassId",
            "booking_class_id",
            "classId",
            "class_id",
            "bookingClass",
        ),
        fallback=1,
    )
    return int(search_id or index + 1), int(booking_class_id or 1)


def _extract_departure_time(flight: dict[str, Any]) -> str:
    raw = _first_value(
        flight,
        "departureTime",
        "depTime",
        "time",
        "departure_time",
        "flightDepartureTime",
    )
    text = str(raw or "").strip().replace(".", ":")
    m = re.search(r"\b(\d{1,2}:\d{2})\b", text)
    return m.group(1) if m else text


def _extract_flight_number(flight: dict[str, Any]) -> str:
    raw = _first_value(
        flight,
        "flightSegmentFlightNumber",
        "flightNo",
        "flightNumber",
        "flight_number",
    )
    return str(raw or "").strip()


def parse_availability(body: dict[str, Any] | str) -> AvailabilityInfo | None:
    if not isinstance(body, dict):
        return None
    if not body.get("success"):
        return None

    inner = normalize_availability_data(body.get("data"))
    if not inner:
        return None

    search_key = str(inner.get("searchKey") or "").strip()
    if not search_key:
        return None

    result = inner.get("result") if isinstance(inner.get("result"), dict) else inner
    depart = result.get("depart") if isinstance(result, dict) else None
    selected = None
    if isinstance(depart, dict) and isinstance(depart.get("selectedDate"), list):
        selected = depart["selectedDate"]
    else:
        selected = _find_selected_date(inner)
    if not selected:
        return None

    offers = offers_from_selected(selected)
    if not offers:
        return None

    picked, err = pick_flight_offer(offers)
    if not picked:
        return AvailabilityInfo(search_key=search_key, flights=offers, picked=None)

    return AvailabilityInfo(search_key=search_key, flights=offers, picked=picked)


def doc_expiry_default(*, on: dt.date | None = None) -> str:
    """Срок документа для API: сегодня + 1 год (MM/DD/YYYY)."""
    today = on or dt.date.today()
    try:
        exp = today.replace(year=today.year + 1)
    except ValueError:
        exp = today.replace(year=today.year + 1, day=28)
    return f"{exp.month:02d}/{exp.day:02d}/{exp.year}"


def gender_api(raw: str) -> str:
    g = (raw or "M").strip().upper()
    if g in ("F", "FEMALE", "W", "WOMAN", "Ж"):
        return "female"
    return "male"


def citizen_id(citizenship: str) -> int:
    key = (citizenship or "TM").strip().upper()
    return CITIZEN_IDS.get(key, 1)


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("993") and len(digits) > 8:
        digits = digits[3:]
    if digits.startswith("8") and len(digits) == 9:
        digits = digits[1:]
    return f"+993{digits}" if digits else ""


def _contact_person(passengers: list[dict]) -> tuple[str, str, str, str]:
    contact_name = (env("API_CONTACT_NAME") or "").strip()
    contact_surname = (env("API_CONTACT_SURNAME") or "").strip()
    contact_email = (env("API_CONTACT_EMAIL") or "").strip()
    contact_phone = (env("API_CONTACT_PHONE") or env("API_CUSTOMER_PHONE") or "").strip()

    first_adult = next((p for p in passengers if p["type"] == "adult"), passengers[0])
    if not contact_name:
        contact_name = first_adult["first_name"]
    if not contact_surname:
        contact_surname = first_adult["last_name"]
    if not contact_email:
        contact_email = first_adult.get("email") or ""
    if not contact_phone:
        contact_phone = first_adult.get("phone") or ""
    return contact_name, contact_surname, contact_email, normalize_phone(contact_phone)


def build_create_booking_payload(
    info: AvailabilityInfo,
    flight: FlightOffer,
    *,
    passengers: list[dict] | None = None,
) -> dict[str, Any]:
    passengers = passengers or get_passengers(verbose=False)
    grouped: dict[str, list[dict]] = {"adult": [], "child": [], "infant": []}
    for pax in passengers:
        grouped[pax["type"]].append(pax)
    payload: dict[str, Any] = {
        "searchKey": info.search_key,
        "tripType": "ow",
        "resident": 1,
        "searchId": flight.search_id,
        "bookingClassId": flight.booking_class_id,
        "paymentType": payment_type(),
    }

    expdt = doc_expiry_default()
    for pax_type in ("adult", "child", "infant"):
        prefix = PAX_PREFIX[pax_type]
        for idx, pax in enumerate(grouped[pax_type], start=1):
            payload[f"{prefix}gender{idx}"] = gender_api(pax.get("gender", "M"))
            payload[f"{prefix}name{idx}"] = pax["first_name"]
            payload[f"{prefix}surname{idx}"] = pax["last_name"]
            payload[f"{prefix}dob{idx}"] = to_api_date(pax["dob"])
            payload[f"{prefix}citizenid{idx}"] = citizen_id(pax.get("citizenship", "TM"))
            payload[f"{prefix}docser{idx}"] = pax["doc_number"]
            payload[f"{prefix}expdt{idx}"] = expdt

    contact_name, contact_surname, contact_email, contact_phone = _contact_person(passengers)
    payload["contactName"] = contact_name
    payload["contactSurname"] = contact_surname
    payload["contactEmail"] = contact_email
    payload["subscriberNumber"] = contact_phone
    return payload


def _short(value: Any, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}… (+{len(text) - limit} симв.)"


def parse_booking_response(
    body: dict[str, Any] | str,
    *,
    status: int = 200,
) -> BookingResult:
    """
    Разобрать ответ CreateBooking.

    Ключевое отличие от прежней версии: отсутствие checkoutUrl больше НЕ
    считается автоматически провалом. Ответ мог не разобраться по десятку
    причин, а бронь на сервере при этом уже существует. Всё, в чём мы не
    уверены, помечается unknown=True — и монитор останавливается, вместо того
    чтобы идти бронировать следующий рейс.
    """
    if status >= 500:
        return BookingResult(
            ok=False,
            unknown=True,
            error=f"HTTP {status}, состояние брони неизвестно: {_short(body)}",
        )

    if not isinstance(body, dict):
        return BookingResult(ok=False, unknown=True, error=f"ответ не JSON: {_short(body)}")

    if not body.get("success"):
        message = str(body.get("message") or body.get("data") or "").strip()
        if message:
            # Сервер назвал причину — это достоверный отказ, брони нет.
            return BookingResult(ok=False, error=message)
        return BookingResult(
            ok=False, unknown=True, error=f"success=false без message: {_short(body)}"
        )

    data = _parse_json_maybe(body.get("data"))
    if not isinstance(data, dict):
        return BookingResult(ok=False, unknown=True, error=f"пустой data: {_short(body)}")

    err = data.get("error")
    if err not in (0, "0", None, ""):
        detail = str(data.get("message") or f"error={err}")
        return BookingResult(ok=False, error=detail)

    message = data.get("message")
    if isinstance(message, str):
        message = _parse_json_maybe(message)
    if not isinstance(message, dict):
        return BookingResult(
            ok=False, unknown=True, error=f"неожиданный message: {_short(message)}"
        )

    checkout_url = str(message.get("checkoutUrl") or message.get("checkout_url") or "").strip()
    pnr = str(message.get("PNRNumber") or message.get("pnr") or "").strip()
    bank_order_id = str(message.get("bankOrderId") or message.get("bank_order_id") or "").strip()

    if not checkout_url and not pnr:
        return BookingResult(
            ok=False, unknown=True, error=f"нет checkoutUrl и PNR: {_short(message)}"
        )

    # PNR есть — бронь существует, даже если ссылку на оплату не прислали.
    return BookingResult(
        ok=True,
        checkout_url=checkout_url,
        pnr=pnr,
        bank_order_id=bank_order_id,
        warning="" if checkout_url else "нет checkoutUrl — ссылку на оплату ищи в приложении",
    )


def compute_booking_key(info: AvailabilityInfo, flight: FlightOffer, passengers: list[dict]) -> str:
    """
    Один и тот же рейс + пассажиры → один и тот же ключ между запусками.

    searchKey сюда НЕ входит: он меняется на каждый поиск, и по нему повторную
    попытку после перезапуска опознать было бы невозможно.
    """
    docs = ",".join(sorted(str(p.get("doc_number") or "") for p in passengers))
    return booking_key(
        env("DATE"),
        env("DEPART_PORT_CODE"),
        env("ARRIVAL_PORT_CODE"),
        flight.flight_number or flight.list_index,
        flight.departure_time,
        flight.booking_class_id,
        docs,
    )


def booking_notify_body(
    *,
    route_line: str,
    flight: FlightOffer,
    passengers: list[dict],
    checkout_url: str,
    pnr: str,
) -> str:
    names = ", ".join(f"{p['first_name']} {p['last_name']}".strip() for p in passengers)
    time_part = f", {flight.departure_time}" if flight.departure_time else ""
    flight_no = f" (рейс {flight.flight_number})" if flight.flight_number else ""
    lines = [
        route_line + time_part + flight_no,
        f"Пассажир: {names}",
    ]
    if pnr:
        lines.append(f"Бронь (PNR): {pnr}")
    lines.append("")
    lines.append("Ссылка для оплаты:")
    lines.append(checkout_url)
    return "\n".join(lines)


def create_booking(
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    idempotency_key: str,
    verbose: bool = True,
) -> tuple[int, dict[str, Any] | str]:
    """
    POST CreateBooking. Бросает ResultUnknown, если ответ не получен.

    `idempotent=False` запрещает транспорту молча повторить запрос: повтор
    CreateBooking — это вторая бронь и второй счёт, а не «ещё одна попытка».
    """
    if verbose:
        print(
            f"[book-api] CreateBooking searchKey={payload.get('searchKey', '')} "
            f"searchId={payload.get('searchId')}"
        )
    response = api_call(
        "POST",
        "CreateBooking",
        headers={**headers, "idempotency-key": idempotency_key},
        body=payload,
        idempotent=False,
    )
    if verbose:
        print(f"[book-api] ответ за {response.elapsed_ms:.0f} мс")
    return response.status, response.body  # type: ignore[return-value]


def try_auto_book(
    availability_body: dict[str, Any] | str,
    *,
    headers: dict[str, str],
    route_line: str,
    guard: BookingGuard | None = None,
    verbose: bool = True,
) -> BookingResult:
    """
    Полный цикл автоброни: выбрать рейс → собрать payload → POST под защитой журнала.

    Возврат читать через `result.ok` / `result.unknown`, а НЕ через `not ok`:
    неопределённый результат означает «остановись», а не «пробуй дальше».
    """
    info = parse_availability(availability_body)
    if not info or not info.picked:
        err = "не удалось разобрать searchKey/рейсы"
        if info and info.flights:
            _, pick_err = pick_flight_offer(info.flights)
            if pick_err:
                err = pick_err
        # Ничего не отправляли — брони точно нет.
        return BookingResult(ok=False, error=err)

    flight = info.picked
    passengers = get_passengers(verbose=False)
    payload = build_create_booking_payload(info, flight, passengers=passengers)

    key = compute_booking_key(info, flight, passengers)
    guard = guard or BookingGuard(BOOKINGS_JOURNAL)

    previous = guard.get(key)
    if previous is not None and guard.is_settled(key):
        return BookingResult(
            ok=False,
            unknown=True,
            flight=flight,
            booking_key=key,
            error=(
                f"по этому рейсу и пассажирам уже была попытка брони "
                f"({previous.status}, {previous.started_at}) — повтор запрещён"
            ),
        )

    # Пишем pending НА ДИСК до отправки: если процесс умрёт между POST и ответом,
    # следующий запуск увидит незакрытую попытку и не станет бронировать вслепую.
    idempotency_key = guard.begin(key)

    try:
        status, body = create_booking(
            payload,
            headers=headers,
            idempotency_key=idempotency_key,
            verbose=verbose,
        )
    except ResultUnknown as exc:
        guard.mark_unknown(key, str(exc))
        return BookingResult(
            ok=False,
            unknown=True,
            flight=flight,
            booking_key=key,
            error=f"ответ не получен, бронь могла быть создана: {exc}",
        )
    except RequestFailed as exc:
        # Запрос до сервера не дошёл — брони точно нет, можно пробовать дальше.
        guard.settle(key, ok=False, detail=str(exc))
        return BookingResult(
            ok=False, flight=flight, booking_key=key, error=f"запрос не ушёл: {exc}"
        )

    if verbose:
        print(f"[book-api] CreateBooking → HTTP {status}")

    result = parse_booking_response(body, status=status)
    result.flight = flight
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
        booking_number=result.pnr,
        form_url=result.checkout_url,
        detail=result.warning,
    )

    subject = f"T5 API: бронь {result.pnr or 'OK'} — {route_line}"
    body_text = booking_notify_body(
        route_line=route_line,
        flight=flight,
        passengers=passengers,
        checkout_url=result.checkout_url,
        pnr=result.pnr,
    )
    if verbose:
        print(_green(f"[book-api] PNR={result.pnr} checkout={result.checkout_url}"))
    if result.warning:
        print(f"[book-api] {result.warning}")
    # Письмо уходит в фоне: бронь уже создана и записана в журнал, держать
    # процесс на SMTP-коннекте (до 30 с при затыке сети) больше незачем.
    # Перед выходом монитор зовёт flush_notifications().
    notify(subject, body_text, wait_gmail=False)
    return result
