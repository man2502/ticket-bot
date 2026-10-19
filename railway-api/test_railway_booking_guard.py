"""Тесты защиты от двойной брони и разбора ответа /v2/bookings.

Запуск (из корня репозитория):
    .venv/bin/python -m pytest railway/test_railway_booking_guard.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "railway")):
    if path not in sys.path:
        sys.path.insert(0, path)

from core.booking_guard import (
    DONE,
    FAILED,
    PENDING,
    UNKNOWN,
    AlreadySettled,
    BookingGuard,
    booking_key,
)
from railway_booking import parse_booking_response


# --------------------------------------------------------------------------- #
# Журнал попыток
# --------------------------------------------------------------------------- #


@pytest.fixture()
def guard(tmp_path: Path) -> BookingGuard:
    return BookingGuard(tmp_path / "bookings.json")


def test_begin_persists_before_response(tmp_path: Path) -> None:
    """pending должен быть на диске ДО ответа — иначе падение = слепой повтор."""
    path = tmp_path / "bookings.json"
    guard = BookingGuard(path)
    guard.begin("k1")

    # Новый процесс читает тот же файл.
    reloaded = BookingGuard(path)
    assert reloaded.get("k1") is not None
    assert reloaded.get("k1").status == PENDING
    assert reloaded.blocking_attempt() is not None


def test_idempotency_key_stable_between_attempts(guard: BookingGuard) -> None:
    """
    Повтор после ДОСТОВЕРНОГО отказа несёт тот же idempotency-key.

    Достоверный отказ означает, что брони нет, и попробовать ещё раз можно —
    но если сервер всё-таки что-то создал, ключ не даст ему создать второе.
    """
    first = guard.begin("k1")
    guard.settle("k1", ok=False, detail="мест нет")
    second = guard.begin("k1")
    assert first == second, "повтор по тому же ключу должен нести тот же idempotency-key"


@pytest.mark.parametrize("close", ["unknown", "done", "pending"])
def test_begin_refuses_to_overwrite_a_closed_attempt(
    guard: BookingGuard, close: str
) -> None:
    """
    `begin` не должен затирать закрытую запись.

    Так была потеряна настоящая бронь: успешная запись со статусом `done`
    превратилась в `failed`, и номер с ссылкой на оплату исчезли из журнала,
    хотя бронь у перевозчика осталась. Проверка есть и выше по коду, но
    в операции, которая стоит второго счёта, одной проверки мало.
    """
    guard.begin("k1")
    if close == "unknown":
        guard.mark_unknown("k1", "таймаут")
    elif close == "done":
        guard.settle("k1", ok=True, booking_number="RW-1", form_url="https://bank.tm/1")
    # pending остаётся как есть после begin()

    with pytest.raises(AlreadySettled):
        guard.begin("k1")


def test_cleared_attempt_is_archived_not_erased(guard: BookingGuard) -> None:
    """
    Снятие блокировки не должно стирать след настоящей брони.

    `--force-rebook` удалял запись целиком — вместе с номером и ссылкой
    на оплату. Бронь у перевозчика при этом никуда не девалась, а узнать
    про неё было уже неоткуда.
    """
    guard.begin("k1")
    guard.settle("k1", ok=True, booking_number="RW-1", form_url="https://bank.tm/1")

    guard.clear_all()

    assert guard.get("k1") is None, "блокировка должна быть снята"
    assert guard.blocking_attempt() is None
    archived = list(guard.archived())
    assert len(archived) == 1
    assert archived[0].booking_number == "RW-1"
    assert archived[0].form_url == "https://bank.tm/1"


def test_archive_survives_reload(guard: BookingGuard) -> None:
    guard.begin("k1")
    guard.settle("k1", ok=True, booking_number="RW-1", form_url="https://bank.tm/1")
    guard.clear_all()

    reloaded = BookingGuard(guard.path)
    assert [r.booking_number for r in reloaded.archived()] == ["RW-1"]


def test_single_clear_also_archives(guard: BookingGuard) -> None:
    guard.begin("k1")
    guard.settle("k1", ok=True, booking_number="RW-2", form_url="https://bank.tm/2")

    guard.clear("k1")

    assert guard.get("k1") is None
    assert [r.booking_number for r in guard.archived()] == ["RW-2"]


def test_cleared_key_can_be_booked_again(guard: BookingGuard) -> None:
    """Архив — это память, а не блокировка: после снятия бронировать можно."""
    guard.begin("k1")
    guard.settle("k1", ok=True, booking_number="RW-1")
    guard.clear_all()

    guard.begin("k1")  # не должно бросить
    assert guard.get("k1").status == PENDING


def test_failed_does_not_block(guard: BookingGuard) -> None:
    guard.begin("k1")
    guard.settle("k1", ok=False, detail="мест нет")
    assert guard.get("k1").status == FAILED
    assert guard.is_settled("k1") is False
    assert guard.blocking_attempt() is None


@pytest.mark.parametrize("finish, status", [("done", DONE), ("unknown", UNKNOWN)])
def test_done_and_unknown_block(guard: BookingGuard, finish: str, status: str) -> None:
    guard.begin("k1")
    if finish == "done":
        guard.settle("k1", ok=True, booking_number="RW1")
    else:
        guard.mark_unknown("k1", "ответ потерян")
    assert guard.get("k1").status == status
    assert guard.is_settled("k1") is True
    assert guard.blocking_attempt() is not None


def test_force_rebook_clears(guard: BookingGuard) -> None:
    guard.begin("k1")
    guard.settle("k1", ok=True, booking_number="RW1")
    guard.clear_all()
    assert guard.blocking_attempt() is None


def test_booking_key_is_deterministic() -> None:
    a = booking_key("2026-08-10", 42, 7, "1,2", "AB1,AB2")
    b = booking_key("2026-08-10", 42, 7, "1,2", "AB1,AB2")
    c = booking_key("2026-08-11", 42, 7, "1,2", "AB1,AB2")
    assert a == b
    assert a != c


def test_corrupt_journal_does_not_lose_guard(tmp_path: Path) -> None:
    path = tmp_path / "bookings.json"
    path.write_text("{битый", encoding="utf-8")
    guard = BookingGuard(path)
    assert guard.blocking_attempt() is None
    assert (tmp_path / "bookings.json.corrupt").exists(), "битый файл должен сохраняться"


# --------------------------------------------------------------------------- #
# Разбор ответа
# --------------------------------------------------------------------------- #


def _ok_body(**booking: object) -> dict:
    base = {"booking_number": "RW-1", "formUrl": "https://bank/pay/1"}
    base.update(booking)
    return {"success": True, "data": {"booking": base}}


def test_parse_success() -> None:
    result = parse_booking_response(_ok_body())
    assert result.ok and not result.unknown
    assert result.booking_number == "RW-1"
    assert result.form_url == "https://bank/pay/1"


def test_parse_booking_without_form_url_is_success_not_failure() -> None:
    """Есть номер брони — бронь существует, даже если ссылки на оплату нет."""
    result = parse_booking_response(_ok_body(formUrl=""))
    assert result.ok is True
    assert result.unknown is False
    assert result.warning


def test_parse_server_rejection_is_certain_failure() -> None:
    result = parse_booking_response({"success": False, "message": "мест нет"})
    assert result.ok is False
    assert result.unknown is False
    assert result.certain_failure is True


def test_parse_structured_error_is_certain_failure() -> None:
    """Отказ проверки входных данных — брони нет, блокировать автобронь не за что."""
    result = parse_booking_response({
        "success": False,
        "error": {"id": "input.invalidInputParam", "message": "input variables are not compatible"},
    })
    assert result.certain_failure is True
    assert "input variables are not compatible" in result.error
    assert "input.invalidInputParam" in result.error


def _pax(**extra) -> dict:
    return {"first_name": "A", "last_name": "B", "dob": "01.01.1980", "type": "adult",
            "gender": "M", "doc_number": "X", **extra}


def test_contact_is_taken_from_any_passenger(monkeypatch) -> None:
    """Случай из жизни: у первого пассажира нет ни почты, ни телефона, у второго есть."""
    from railway_booking import booking_contact

    for name in ("RAILWAY_CONTACT_EMAIL", "RAILWAY_CONTACT_PHONE", "NOTIFY_EMAIL"):
        monkeypatch.setenv(name, "")
    email, phone = booking_contact([_pax(), _pax(email="b@example.com", phone="+99361000000")])
    assert email == "b@example.com"
    assert phone == "+99361000000"


def test_process_contact_wins_over_passenger(monkeypatch) -> None:
    from railway_booking import booking_contact

    monkeypatch.setenv("RAILWAY_CONTACT_EMAIL", "pay@example.com")
    monkeypatch.setenv("RAILWAY_CONTACT_PHONE", "+99365000000")
    assert booking_contact([_pax(email="b@example.com")]) == ("pay@example.com", "+99365000000")


def test_booking_without_contact_is_not_sent(monkeypatch) -> None:
    """Пустой контакт — отказ до POST, а не невнятный invalidInputParam от сервера."""
    from railway_booking import BookingError, build_booking_payload

    for name in ("RAILWAY_CONTACT_EMAIL", "RAILWAY_CONTACT_PHONE", "NOTIFY_EMAIL"):
        monkeypatch.setenv(name, "")
    with pytest.raises(BookingError, match="телефон"):
        build_booking_payload(journey_id=1, seats=[object()], passengers=[_pax(email="a@b.c")])


def test_request_summary_has_tariffs_and_ages_but_no_documents() -> None:
    from railway_booking import request_summary

    payload = {
        "passengers": [
            {"tariff": "adult", "dob": "16-01-1973", "identity_number": "I-AS 000000",
             "name": "Имя", "surname": "Фамилия"},
            {"tariff": "child", "dob": "01-01-2011"},
        ],
        "outbound": {"selected_journeys": [{"id": 1, "seats": [{"id": 1}, {"id": 2}]}]},
    }
    line = request_summary(payload, {"departure_time": "2026-10-20T17:22:00+05:00"},
                           [{"type": "adult"}, {"type": "child"}])

    assert "adult 53 лет" in line
    assert "child 15 лет" in line
    assert "мест 2" in line
    assert "поиск adult=1 child=1" in line
    for secret in ("I-AS", "Имя", "Фамилия"):
        assert secret not in line


def test_parse_5xx_is_unknown() -> None:
    result = parse_booking_response({"success": True, "data": {}}, status=503)
    assert result.unknown is True
    assert result.certain_failure is False


def test_parse_non_json_is_unknown() -> None:
    result = parse_booking_response("<html>502 Bad Gateway</html>")
    assert result.unknown is True


def test_parse_missing_booking_is_unknown() -> None:
    result = parse_booking_response({"success": True, "data": {"other": 1}})
    assert result.unknown is True


def test_parse_success_false_without_message_is_unknown() -> None:
    result = parse_booking_response({"success": False})
    assert result.unknown is True


def test_parse_truncates_long_body() -> None:
    result = parse_booking_response({"success": False, "data": {"x": "y" * 5000}})
    assert len(result.error) < 500
