"""Тест порядка операций в момент появления мест.

Раньше письмо уходило синхронно ДО брони: `notify(..., wait_gmail=True)` это
SMTP-коннект + STARTTLS + LOGIN, то есть 1-3 секунды (до 30 при затыке сети)
ровно в тот момент, когда за место идёт гонка.

Запуск (из корня репозитория):
    .venv/bin/python -m pytest railway/test_railway_check_order.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "railway")):
    if path not in sys.path:
        sys.path.insert(0, path)

import railway_check
from core.booking_guard import BookingGuard
from railway_booking import BookingResult


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Один слот, места есть, всё внешнее заглушено. Возвращает журнал событий."""
    events: list[str] = []

    monkeypatch.setattr(railway_check, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(
        railway_check, "plan_slot", lambda slot: ("now", 0.0, "") if slot == 0 else ("skip", 0.0, "")
    )
    monkeypatch.setattr(railway_check, "wait_for_planned_slot", lambda *a, **k: None)
    monkeypatch.setattr(railway_check, "plan_wait_next_minute_slot0", lambda: (0.0, ""))
    monkeypatch.setattr(railway_check, "precise_sleep", lambda _d: None)
    monkeypatch.setattr(railway_check, "route_line", lambda *_a: "ASB → CRZ")
    monkeypatch.setattr(railway_check, "format_passenger_names", lambda: "Anvar")
    monkeypatch.setattr(railway_check, "auto_book_enabled", lambda: True)
    monkeypatch.setattr(railway_check, "available_trips", lambda trips: trips)

    monkeypatch.setattr(
        railway_check,
        "run_check_once",
        lambda **_k: (True, ["поезд 1: 4 места"], {"trips": [{"id": 1}], "all_trips": [], "watched": []}),
    )

    def fake_notify(title, body="", *, wait_gmail=False):  # noqa: ANN001
        events.append(f"notify(wait_gmail={wait_gmail})")
        return True

    monkeypatch.setattr(railway_check, "notify", fake_notify)
    return events


def _run(guard: BookingGuard) -> int | None:
    return railway_check.run_planned_checks(
        slots=(0, 1), date_label="10.08.2026", state={}, state_key="k", guard=guard
    )


def test_booking_happens_before_email(
    harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_book(trips, guard, *, verbose=True):  # noqa: ANN001, ARG001
        harness.append("book")
        return BookingResult(ok=True, booking_number="RW-1", form_url="https://bank/1")

    monkeypatch.setattr(railway_check, "attempt_bookings", fake_book)

    assert _run(BookingGuard(tmp_path / "b.json")) == 0
    assert harness[0] == "book", f"бронь должна быть первой, получили {harness}"
    assert "notify(wait_gmail=False)" in harness, "письмо должно уходить в фоне"
    assert "notify(wait_gmail=True)" not in harness


def test_unknown_booking_stops_monitor_with_code_2(
    harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        railway_check,
        "attempt_bookings",
        lambda *a, **k: BookingResult(ok=False, unknown=True, error="ответ потерян"),
    )
    assert _run(BookingGuard(tmp_path / "b.json")) == 2


def test_certain_failure_keeps_monitor_running(
    harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        railway_check,
        "attempt_bookings",
        lambda *a, **k: BookingResult(ok=False, error="мест уже нет"),
    )
    assert _run(BookingGuard(tmp_path / "b.json")) is None, "монитор продолжает работу"
