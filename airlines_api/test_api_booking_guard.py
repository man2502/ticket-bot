"""Тесты защиты от повторной брони в airlines_api.

Главный инвариант: результат CreateBooking трёхзначный. «Не получилось» и
«непонятно, что получилось» — разные вещи, и вторая из них обязана остановить
монитор, а не отправить его бронировать следующий рейс.

Запуск (из корня репозитория):
    python run_tests.py airlines_api
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import api_booking
from api_booking import (
    AvailabilityInfo,
    BookingResult,
    FlightOffer,
    compute_booking_key,
    parse_booking_response,
    try_auto_book,
)
from core.booking_guard import DONE, UNKNOWN, BookingGuard
from core.http import RequestFailed, ResultUnknown


def _offer(**over) -> FlightOffer:
    base = dict(
        raw={},
        summary="flightNo=T5-101",
        search_id=7,
        booking_class_id=3,
        departure_time="09:40",
        flight_number="T5-101",
        seat=5,
        list_index=0,
    )
    base.update(over)
    return FlightOffer(**base)


def _info(offer: FlightOffer | None = None) -> AvailabilityInfo:
    offer = offer or _offer()
    return AvailabilityInfo(search_key="SK-1", flights=[offer], picked=offer)


PAX = [
    {
        "id": "1",
        "type": "adult",
        "first_name": "Anvar",
        "last_name": "Test",
        "dob": "01.01.1990",
        "gender": "M",
        "doc_number": "AA1234567",
        "citizenship": "TM",
    }
]


# --------------------------------------------------------------------------
# Разбор ответа
# --------------------------------------------------------------------------


def test_success_with_checkout_url() -> None:
    body = {
        "success": True,
        "data": {"error": 0, "message": {"checkoutUrl": "https://bank/pay", "PNRNumber": "ABC123"}},
    }
    result = parse_booking_response(body)
    assert result.ok is True
    assert result.checkout_url == "https://bank/pay"
    assert result.pnr == "ABC123"
    assert result.warning == ""


def test_pnr_without_checkout_url_is_still_a_booking() -> None:
    """Раньше отсутствие ссылки считалось провалом — и бот шёл бронировать снова."""
    body = {"success": True, "data": {"error": 0, "message": {"PNRNumber": "ABC123"}}}
    result = parse_booking_response(body)
    assert result.ok is True
    assert result.pnr == "ABC123"
    assert result.warning  # человеку сказано, где искать оплату


def test_server_refusal_is_certain_failure() -> None:
    body = {"success": False, "message": "нет мест в этом классе"}
    result = parse_booking_response(body)
    assert result.certain_failure is True
    assert "нет мест" in result.error


def test_5xx_is_unknown_not_failure() -> None:
    result = parse_booking_response("<html>502</html>", status=502)
    assert result.ok is False
    assert result.unknown is True


def test_non_json_body_is_unknown() -> None:
    result = parse_booking_response("вообще не json", status=200)
    assert result.unknown is True


def test_success_false_without_message_is_unknown() -> None:
    result = parse_booking_response({"success": False})
    assert result.unknown is True


def test_empty_message_dict_is_unknown() -> None:
    body = {"success": True, "data": {"error": 0, "message": {}}}
    assert parse_booking_response(body).unknown is True


# --------------------------------------------------------------------------
# Ключ брони
# --------------------------------------------------------------------------


def test_booking_key_ignores_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """searchKey меняется на каждый поиск — по нему повтор не опознать."""
    monkeypatch.setenv("DATE", "10.08.2026")
    monkeypatch.setenv("DEPART_PORT_CODE", "ASB")
    monkeypatch.setenv("ARRIVAL_PORT_CODE", "MYP")

    offer = _offer()
    first = compute_booking_key(AvailabilityInfo("SK-1", [offer], offer), offer, PAX)
    second = compute_booking_key(AvailabilityInfo("SK-2", [offer], offer), offer, PAX)
    assert first == second


def test_booking_key_differs_for_other_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATE", "10.08.2026")
    a = _offer(flight_number="T5-101")
    b = _offer(flight_number="T5-777", departure_time="19:20")
    assert compute_booking_key(_info(a), a, PAX) != compute_booking_key(_info(b), b, PAX)


# --------------------------------------------------------------------------
# Полный цикл try_auto_book под защитой журнала
# --------------------------------------------------------------------------


@pytest.fixture()
def guard(tmp_path: Path) -> BookingGuard:
    return BookingGuard(tmp_path / "bookings.json")


@pytest.fixture(autouse=True)
def _stub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ни сети, ни passengers.json, ни писем — только логика брони."""
    monkeypatch.setenv("DATE", "10.08.2026")
    monkeypatch.setenv("DEPART_PORT_CODE", "ASB")
    monkeypatch.setenv("ARRIVAL_PORT_CODE", "MYP")
    monkeypatch.setattr(api_booking, "get_passengers", lambda **_k: list(PAX))
    monkeypatch.setattr(api_booking, "parse_availability", lambda _body: _info())
    monkeypatch.setattr(api_booking, "build_create_booking_payload", lambda *a, **k: {"searchKey": "SK-1"})
    monkeypatch.setattr(api_booking, "notify", lambda *a, **k: True)


def _book(guard: BookingGuard) -> BookingResult:
    return try_auto_book({}, headers={}, route_line="ASB → MYP", guard=guard, verbose=False)


def test_lost_response_marks_unknown_and_blocks_retry(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    def boom(*_a, **_k):
        raise ResultUnknown("read timeout")

    monkeypatch.setattr(api_booking, "create_booking", boom)

    first = _book(guard)
    assert first.unknown is True
    assert guard.get(first.booking_key).status == UNKNOWN

    # Повторная попытка по тем же данным запрещена — даже если сеть починилась.
    monkeypatch.setattr(
        api_booking,
        "create_booking",
        lambda *a, **k: (200, {"success": True, "data": {"error": 0, "message": {"PNRNumber": "X"}}}),
    )
    second = _book(guard)
    assert second.ok is False
    assert second.unknown is True
    assert "повтор запрещён" in second.error


def test_request_not_sent_is_certain_failure(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    """Запрос не ушёл — брони нет, следующий рейс пробовать можно."""

    def boom(*_a, **_k):
        raise RequestFailed("connect timeout")

    monkeypatch.setattr(api_booking, "create_booking", boom)
    result = _book(guard)
    assert result.certain_failure is True
    assert guard.is_settled(result.booking_key) is False


def test_success_is_recorded_as_done(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    monkeypatch.setattr(
        api_booking,
        "create_booking",
        lambda *a, **k: (
            200,
            {"success": True, "data": {"error": 0, "message": {"checkoutUrl": "https://bank/1", "PNRNumber": "P1"}}},
        ),
    )
    result = _book(guard)
    assert result.ok is True
    assert guard.get(result.booking_key).status == DONE
    assert guard.blocking_attempt() is not None  # второй запуск будет заблокирован


def test_server_refusal_does_not_block_next_attempt(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    monkeypatch.setattr(
        api_booking,
        "create_booking",
        lambda *a, **k: (400, {"success": False, "message": "мест нет"}),
    )
    result = _book(guard)
    assert result.certain_failure is True
    assert guard.blocking_attempt() is None


def test_pending_survives_process_death(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Запись pending уходит на диск ДО запроса — иначе перезапуск бронирует вслепую."""
    journal = tmp_path / "bookings.json"

    def die_after_send(*_a, **_k):
        # Имитация: POST ушёл, ответа нет, процесс падает.
        raise ResultUnknown("соединение оборвано")

    monkeypatch.setattr(api_booking, "create_booking", die_after_send)
    _book(BookingGuard(journal))

    # Новый запуск читает журнал с нуля.
    fresh = BookingGuard(journal)
    blocker = fresh.blocking_attempt()
    assert blocker is not None and blocker.status == UNKNOWN
