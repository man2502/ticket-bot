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

from core.booking_guard import DONE, FAILED, PENDING, UNKNOWN, BookingGuard, booking_key
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
    first = guard.begin("k1")
    guard.mark_unknown("k1", "таймаут")
    second = guard.begin("k1")
    assert first == second, "повтор по тому же ключу должен нести тот же idempotency-key"


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
