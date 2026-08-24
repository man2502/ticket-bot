"""Тесты тайминга слотов и выбора рейса в airlines_api.

Слоты — это про доли секунды. Старый расчёт брал `datetime.now().second`
и терял микросекунды: в 12:00:00.700 «ждать до :00» давало 0, но фактически
запрос уходил на 700 мс позже намеченной отметки. Здесь проверяется, что
расчёт ведётся с долями и что окно текущей минуты не пропускается зря.

Запуск (из корня репозитория):
    python run_tests.py airlines_api
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import api_booking
import api_check
from core.slots import now_second, seconds_until_second


def _at(second: int, micro: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 20, 12, 0, second, micro)


# --------------------------------------------------------------------------
# Точность секунд
# --------------------------------------------------------------------------


def test_now_second_keeps_fraction() -> None:
    assert now_second(_at(57, 900_000)) == pytest.approx(57.9)


def test_seconds_until_second_uses_fraction() -> None:
    # 12:00:57.900 → до :00 следующей минуты ровно 2.1 с, а не 3.0.
    assert seconds_until_second(0.0, now=_at(57, 900_000)) == pytest.approx(2.1, abs=1e-6)


def test_seconds_until_second_with_jitter() -> None:
    assert seconds_until_second(0.0, jitter=0.2, now=_at(59, 500_000)) == pytest.approx(0.7, abs=1e-6)


# --------------------------------------------------------------------------
# Окна проверки
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _both_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CHECK_EVERY_00", "1")
    monkeypatch.setenv("API_CHECK_EVERY_30", "1")


def test_two_windows_configured() -> None:
    assert api_check.check_at_second_ranges() == [(0, 1), (30, 31)]


def test_window_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CHECK_EVERY_00", "0")
    assert api_check.check_at_second_ranges() == [(30, 31)]


def test_no_windows_is_a_hard_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CHECK_EVERY_00", "0")
    monkeypatch.setenv("API_CHECK_EVERY_30", "0")
    with pytest.raises(SystemExit):
        api_check.check_at_second_range()


def test_ticks_cover_whole_window() -> None:
    ticks = api_check.slot_offsets(30, 31)
    assert ticks[0] == 30.0
    assert ticks[-1] == 31.0
    assert len(ticks) == 11  # шаг 0.1 с


def test_next_slot_is_in_the_future(monkeypatch: pytest.MonkeyPatch) -> None:
    """В середине окна :30 стреляем в оставшийся тик этой же минуты, не ждём минуту."""
    monkeypatch.setattr(api_check, "now_second", lambda: 30.45)
    offset, window, until = api_check.next_check_slot()
    assert window == (30, 31)
    assert 0 < until <= 0.6
    assert 30.4 < offset <= 31.0


def test_passed_window_rolls_to_next_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_check, "now_second", lambda: 45.0)
    _offset, window, until = api_check.next_check_slot()
    assert window == (0, 1)
    assert 15.0 <= until <= 16.0


def test_nearest_window_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """В :05 ближе окно :30, а не :00 следующей минуты."""
    monkeypatch.setattr(api_check, "now_second", lambda: 5.0)
    _offset, window, until = api_check.next_check_slot()
    assert window == (30, 31)
    assert 25.0 <= until <= 26.0


# --------------------------------------------------------------------------
# Выбор рейса
# --------------------------------------------------------------------------


def _offer(idx: int, *, seat: int = 4, time: str = "09:40", number: str = "T5-1") -> api_booking.FlightOffer:
    return api_booking.FlightOffer(
        raw={},
        summary=f"flightNo={number}",
        search_id=idx + 1,
        booking_class_id=1,
        departure_time=time,
        flight_number=number,
        seat=seat,
        list_index=idx,
    )


def test_index_picks_requested_position(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "2")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    offers = [_offer(0, number="T5-1"), _offer(1, number="T5-2")]
    picked, err = api_booking.pick_flight_offer(offers)
    assert err is None
    assert picked.flight_number == "T5-2"


def test_strict_refuses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """FLIGHT_STRICT=1: на выбранном мест нет — бронировать соседний нельзя."""
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "1")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    offers = [_offer(0, seat=0), _offer(1, seat=9)]
    picked, err = api_booking.pick_flight_offer(offers)
    assert picked is None
    assert "strict" in err


def test_non_strict_falls_back_to_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    offers = [_offer(0, seat=0, number="T5-1"), _offer(1, seat=9, number="T5-2")]
    picked, err = api_booking.pick_flight_offer(offers)
    assert err is None
    assert picked.flight_number == "T5-2"


def test_time_beats_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.setenv("FLIGHT_TIME", "19:20")
    offers = [_offer(0, time="09:40", number="T5-1"), _offer(1, time="19:20", number="T5-2")]
    picked, err = api_booking.pick_flight_offer(offers)
    assert err is None
    assert picked.flight_number == "T5-2"


def test_transit_flight_is_never_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_INDEX", "1")
    monkeypatch.setenv("FLIGHT_STRICT", "0")
    monkeypatch.delenv("FLIGHT_TIME", raising=False)
    transit = _offer(0, number="T5-1")
    transit.skipped = True
    offers = [transit, _offer(1, number="T5-2")]
    picked, _err = api_booking.pick_flight_offer(offers)
    assert picked.flight_number == "T5-2"


# --------------------------------------------------------------------------
# Заголовки запросов авторизации
# --------------------------------------------------------------------------


class _StubResponse:
    status, elapsed_ms, text = 200, 5.0, '{"token":"T"}'
    body = {"token": "T"}

    @property
    def json(self):
        return {"token": "T"}

    def safe_text(self, limit=600):
        return self.text


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list:
    """Перехватить заголовки, которые реально уходят в сеть."""
    import api_client

    captured: list = []

    class _Client:
        def request(self, method, path, headers=None, body=None, **kw):
            captured.append((path, dict(headers or {})))
            return _StubResponse()

        def close(self):
            pass

    monkeypatch.setattr(api_client, "get_client", lambda: _Client())

    monkeypatch.setenv("API_CUSTOMER_PHONE", "+99361234567")
    monkeypatch.setenv("API_CUSTOMER_PASSWORD", "line003")
    monkeypatch.setenv("API_AUTHORIZATION", "Bearer clienttoken")
    monkeypatch.setenv("API_IOS_PASSWORD", "app-secret")
    monkeypatch.setenv("API_SAVE_TOKENS", "0")
    return captured


def test_customer_login_uses_dart_user_agent(sent: list) -> None:
    """
    Приложение входит в личный кабинет Dart-клиентом, а не как iOS.

    Мы слали туда iOS-строку: логин с одного клиента, поиск с другого —
    заметное несоответствие для того, кто смотрит на трафик.
    """
    import api_auth

    api_auth.refresh_customer_token(verbose=False)

    _path, headers = sent[-1]
    assert headers["user-agent"] == "Dart/3.11 (dart:io)"


def test_customer_login_sends_accept_language(sent: list) -> None:
    """Набор заголовков повторяет рабочий запрос приложения."""
    import api_auth

    api_auth.refresh_customer_token(verbose=False)
    assert sent[-1][1]["accept-language"] == "ru"


def test_customer_login_user_agent_is_overridable(
    sent: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Приложение обновится — версию Dart можно будет поменять без правки кода."""
    import api_auth

    monkeypatch.setenv("API_CUSTOMER_LOGIN_UA", "Dart/3.20 (dart:io)")
    api_auth.refresh_customer_token(verbose=False)

    assert sent[-1][1]["user-agent"] == "Dart/3.20 (dart:io)"


def test_ordinary_requests_still_rotate_ios_agent(sent: list) -> None:
    """
    Ротация iOS-строки для остальных запросов должна остаться.

    Она меняется раз в 45-55 минут, и подмена её на фиксированную строку
    сделала бы бота узнаваемым.
    """
    import api_client

    api_client.api_call("GET", "customers/info", headers={"authorization": "Bearer x"})

    agent = sent[-1][1]["user-agent"]
    assert agent.startswith("iOS/")
    assert "iPhone" in agent


def test_explicit_agent_survives_when_rotation_off(sent: list) -> None:
    import api_client

    api_client.api_call(
        "POST", "somewhere",
        headers={"user-agent": "Custom/1.0"},
        rotate_user_agent=False,
    )
    assert sent[-1][1]["user-agent"] == "Custom/1.0"


# --------------------------------------------------------------------------
# Объяснение отказа на входе
# --------------------------------------------------------------------------

BACKSLASH = chr(92)


def test_401_explains_which_account_was_rejected() -> None:
    """
    Сервер отвечает на неверный пароль пустым телом.

    Голое «HTTP 401: []» не говорит человеку ничего — и особенно не говорит,
    что отвергнут пароль учётки приложения, а не личного кабинета.
    """
    from api_auth import _login_error

    text = _login_error(401, [], "ios", "wrongpass")
    assert "учётки приложения" in text
    assert "НЕ пароль вашего личного кабинета" in text


def test_401_hints_at_doubled_backslash() -> None:
    """
    Пароль скопировали из экспорта коллекции, где слеш экранирован.

    Самая частая причина отказа: в настоящем пароле слеш один, а в поле
    уехали оба символа.
    """
    from api_auth import _login_error

    doubled = "]*" + BACKSLASH * 2 + "d6)RrG{rVY:6H"
    assert "двойной обратный слеш" in _login_error(401, [], "ios", doubled)


def test_no_backslash_hint_when_password_is_clean() -> None:
    from api_auth import _login_error

    assert "двойной обратный слеш" not in _login_error(401, [], "ios", "plainpass")


def test_401_hints_at_stray_spaces() -> None:
    from api_auth import _login_error

    assert "пробелы" in _login_error(401, [], "ios", " secret ")


def test_password_itself_is_never_printed() -> None:
    """В сообщение уходит длина, но не значение: оно попадёт в логи процесса."""
    from api_auth import _login_error

    secret = "s3cret-do-not-leak"
    text = _login_error(401, [], "ios", secret)
    assert secret not in text
    assert str(len(secret)) in text


def test_other_statuses_keep_raw_answer() -> None:
    """Не 401 — причина неизвестна, показываем ответ как есть."""
    from api_auth import _login_error

    assert "oops" in _login_error(500, {"message": "oops"}, "ios", "x")
