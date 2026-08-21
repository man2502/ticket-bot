"""Тесты защиты от повторной брони в браузерном боте.

Главный инвариант: клик по «Подтвердить» — точка невозврата. До него ошибки
безопасны, после него любая неясность обязана пометиться unknown, попасть
в журнал на диск и остановить цикл. Второй submit — это вторая настоящая бронь
на тех же пассажиров и второй счёт в банке.

Запуск (из корня репозитория):
    python run_tests.py airlines_web
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_web")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import book
from core.booking_guard import DONE, FAILED, UNKNOWN, BookingGuard
from web_booking import WebBookingResult, check_booking_guard, compute_web_booking_key

BANK_URL = "https://halkbank.tm/payment/12345"


# --------------------------------------------------------------------------
# Заглушка страницы Playwright
# --------------------------------------------------------------------------


class _FakeLocator:
    def __init__(self, count: int = 1, on_click=None) -> None:
        self._count = count
        self._on_click = on_click

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> "_FakeLocator":
        return self

    def scroll_into_view_if_needed(self, **_kw) -> None:
        pass

    def click(self, **_kw) -> None:
        if self._on_click is not None:
            self._on_click()


class _FakePage:
    """Минимум, который трогает click_podtverdit: evaluate, locator, wait, url."""

    def __init__(self, *, button_count: int = 1, on_click=None) -> None:
        self.button_count = button_count
        self.clicks = 0
        self.url = "https://turkmenistanairlines.tm/ru/flights/booking"
        self._on_click = on_click

    def evaluate(self, _script, *_args):
        return None

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def locator(self, _selector: str) -> _FakeLocator:
        def _click() -> None:
            self.clicks += 1
            if self._on_click is not None:
                self._on_click()

        return _FakeLocator(count=self.button_count, on_click=_click)


PAX = [{"doc_number": "AA1234567"}, {"doc_number": "BB7654321"}]


@pytest.fixture()
def guard(tmp_path: Path) -> BookingGuard:
    return BookingGuard(tmp_path / "web_bookings.json")


@pytest.fixture(autouse=True)
def _no_disk_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """.last_bank_url.txt пишем во временную папку, а не в репозиторий."""
    monkeypatch.setattr(book, "ROOT", tmp_path)


def _key() -> str:
    return compute_web_booking_key(
        depart="ASB",
        arrival="MYP",
        flight_date="10.08.2026",
        flight_time="09:40",
        passengers=PAX,
    )


# --------------------------------------------------------------------------
# Ключ брони
# --------------------------------------------------------------------------


def test_key_is_stable_between_runs() -> None:
    assert _key() == _key()


def test_key_ignores_passenger_order() -> None:
    """Порядок пассажиров в .env не должен создавать «новую» бронь."""
    reversed_pax = list(reversed(PAX))
    other = compute_web_booking_key(
        depart="ASB",
        arrival="MYP",
        flight_date="10.08.2026",
        flight_time="09:40",
        passengers=reversed_pax,
    )
    assert other == _key()


def test_key_differs_for_other_flight_time() -> None:
    other = compute_web_booking_key(
        depart="ASB",
        arrival="MYP",
        flight_date="10.08.2026",
        flight_time="19:20",
        passengers=PAX,
    )
    assert other != _key()


# --------------------------------------------------------------------------
# click_podtverdit: до клика и после
# --------------------------------------------------------------------------


def test_missing_button_is_certain_failure(guard: BookingGuard) -> None:
    """Кнопки нет — клика не было. Журнал должен остаться пустым."""
    page = _FakePage(button_count=0)
    result = book.click_podtverdit(page, guard=guard, booking_key_value=_key())

    assert result.certain_failure is True
    assert page.clicks == 0
    assert guard.get(_key()) is None
    assert guard.blocking_attempt() is None


def test_bank_url_settles_as_done(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    monkeypatch.setattr(book, "wait_for_bank_url", lambda _page, **_kw: BANK_URL)
    page = _FakePage()

    result = book.click_podtverdit(page, guard=guard, booking_key_value=_key())

    assert result.ok is True
    assert result.bank_url == BANK_URL
    assert page.clicks == 1
    assert guard.get(_key()).status == DONE


def test_no_bank_url_after_click_is_unknown(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    """Клик был, ссылки нет — бронь могла быть создана."""
    monkeypatch.setattr(book, "wait_for_bank_url", lambda _page, **_kw: "")
    page = _FakePage()

    result = book.click_podtverdit(page, guard=guard, booking_key_value=_key())

    assert result.ok is False
    assert result.unknown is True
    assert guard.get(_key()).status == UNKNOWN


def test_airline_url_is_not_a_bank_url(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    """Остались на странице брони — это не оплата, а неизвестный исход."""
    monkeypatch.setattr(
        book,
        "wait_for_bank_url",
        lambda _page, **_kw: "https://turkmenistanairlines.tm/ru/flights/booking",
    )
    result = book.click_podtverdit(_FakePage(), guard=guard, booking_key_value=_key())

    assert result.unknown is True
    assert guard.get(_key()).status == UNKNOWN


def test_exception_while_waiting_is_unknown(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    def boom(_page, **_kw):
        raise RuntimeError("браузер отвалился")

    monkeypatch.setattr(book, "wait_for_bank_url", boom)
    result = book.click_podtverdit(_FakePage(), guard=guard, booking_key_value=_key())

    assert result.unknown is True
    assert guard.get(_key()).status == UNKNOWN


def test_pending_written_before_click(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    """
    Запись pending должна быть на диске уже в момент клика.

    Иначе падение процесса ровно между кликом и ответом стирает факт попытки,
    и следующий запуск бронирует вслепую.
    """
    journal_at_click: list[str] = []

    def _record_click() -> None:
        fresh = BookingGuard(guard.path)
        record = fresh.get(_key())
        journal_at_click.append(record.status if record else "нет записи")

    monkeypatch.setattr(book, "wait_for_bank_url", lambda _page, **_kw: BANK_URL)
    page = _FakePage(on_click=_record_click)

    book.click_podtverdit(page, guard=guard, booking_key_value=_key())

    assert journal_at_click == ["pending"]


# --------------------------------------------------------------------------
# Блокировка следующего запуска
# --------------------------------------------------------------------------


def test_unknown_blocks_next_run(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    monkeypatch.setattr(book, "wait_for_bank_url", lambda _page, **_kw: "")
    book.click_podtverdit(_FakePage(), guard=guard, booking_key_value=_key())

    fresh = BookingGuard(guard.path)
    assert check_booking_guard(fresh, force_rebook=False) is not None


def test_done_blocks_next_run(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    """Успешная бронь тоже блокирует: перезапуск не должен бронировать второй раз."""
    monkeypatch.setattr(book, "wait_for_bank_url", lambda _page, **_kw: BANK_URL)
    book.click_podtverdit(_FakePage(), guard=guard, booking_key_value=_key())

    fresh = BookingGuard(guard.path)
    assert check_booking_guard(fresh, force_rebook=False) is not None


def test_force_rebook_unblocks(
    monkeypatch: pytest.MonkeyPatch, guard: BookingGuard
) -> None:
    monkeypatch.setattr(book, "wait_for_bank_url", lambda _page, **_kw: "")
    book.click_podtverdit(_FakePage(), guard=guard, booking_key_value=_key())

    fresh = BookingGuard(guard.path)
    assert check_booking_guard(fresh, force_rebook=True) is None
    assert fresh.blocking_attempt() is None


def test_certain_failure_does_not_block(guard: BookingGuard) -> None:
    """Отказ до клика не должен мешать следующей попытке."""
    guard.begin(_key())
    guard.settle(_key(), ok=False, detail="кнопка не найдена")

    assert guard.get(_key()).status == FAILED
    assert check_booking_guard(guard, force_rebook=False) is None


# --------------------------------------------------------------------------
# Восстановление без повторного submit
# --------------------------------------------------------------------------


def test_resume_wait_never_clicks_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """resume_wait_bank_url только ждёт: повторный клик завёл бы вторую бронь."""
    monkeypatch.setattr(book, "wait_for_bank_url", lambda _page, **_kw: BANK_URL)
    page = _FakePage()

    url = book.resume_wait_bank_url(page, timeout_sec=1)

    assert url == BANK_URL
    assert page.clicks == 0


def test_result_flags_are_mutually_exclusive() -> None:
    ok = WebBookingResult(ok=True, bank_url=BANK_URL)
    unknown = WebBookingResult(ok=False, unknown=True, error="нет ссылки")
    failed = WebBookingResult(ok=False, error="кнопки нет")

    assert ok.certain_failure is False
    assert unknown.certain_failure is False
    assert failed.certain_failure is True
