"""Тесты слота поиска и общего env для браузерного бота.

Слот `BOOK_SEARCH_AT_SEC` — момент, в который оба аккаунта одновременно жмут
поиск. Ошибка в пол-секунды здесь означает проигранное место, поэтому расчёт
проверяется с точностью до микросекунд.

Запуск (из корня репозитория):
    python run_tests.py airlines_web
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_web")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import book
from core.env import env, env_flag, env_float, env_int


class _FrozenDateTime(dt.datetime):
    """datetime с фиксированным now() — слот считается от него."""

    _now = dt.datetime(2026, 8, 20, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls._now


@pytest.fixture()
def freeze(monkeypatch: pytest.MonkeyPatch):
    def _freeze(second: int, micro: int = 0) -> None:
        _FrozenDateTime._now = dt.datetime(2026, 8, 20, 12, 0, second, micro)
        monkeypatch.setattr(book.dt, "datetime", _FrozenDateTime)

    return _freeze


# --------------------------------------------------------------------------
# Секунда слота
# --------------------------------------------------------------------------


def test_default_slot_is_30(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOK_SEARCH_AT_SEC", raising=False)
    assert book._search_at_second() == 30


def test_slot_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "0")
    assert book._search_at_second() == 0


def test_slot_is_clamped_to_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "99")
    assert book._search_at_second() == 59
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "-5")
    assert book._search_at_second() == 0


def test_garbage_slot_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "полночь")
    assert book._search_at_second() == 30


# --------------------------------------------------------------------------
# Расчёт времени до слота
# --------------------------------------------------------------------------


def test_fraction_of_second_is_not_lost(
    monkeypatch: pytest.MonkeyPatch, freeze
) -> None:
    """12:00:29.700 → до :30 ровно 0.3 с, а не 1.0 и не 0."""
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "30")
    freeze(29, 700_000)
    _now, _nxt, until = book.next_search_slot()
    assert until == pytest.approx(0.3, abs=1e-6)


def test_just_after_slot_waits_full_minute(
    monkeypatch: pytest.MonkeyPatch, freeze
) -> None:
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "30")
    freeze(30, 200_000)
    _now, _nxt, until = book.next_search_slot()
    assert until == pytest.approx(59.8, abs=1e-6)


def test_slot_target_is_on_the_exact_second(
    monkeypatch: pytest.MonkeyPatch, freeze
) -> None:
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "30")
    freeze(10, 123_456)
    _now, nxt, until = book.next_search_slot()
    assert (nxt.second, nxt.microsecond) == (30, 0)
    assert until == pytest.approx(19.876544, abs=1e-6)


def test_seconds_until_slot_matches_next_slot(
    monkeypatch: pytest.MonkeyPatch, freeze
) -> None:
    monkeypatch.setenv("BOOK_SEARCH_AT_SEC", "0")
    freeze(45, 500_000)
    assert book.seconds_until_search_slot() == pytest.approx(14.5, abs=1e-6)


def test_jitter_stays_within_two_tenths() -> None:
    """Разброс относительно слота нужен против детекта, но не должен его срывать."""
    values = {book._search_slot_jitter_sec() for _ in range(200)}
    assert values <= {0.0, 0.1, 0.2}


# --------------------------------------------------------------------------
# Общий env: пустое значение = не задано
# --------------------------------------------------------------------------


def test_empty_value_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`NOTIFY_EMAIL=` в .env — это «не задано», а не пустая строка."""
    monkeypatch.setenv("T5_TEST_VALUE", "")
    assert env("T5_TEST_VALUE", "default") == "default"


def test_value_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T5_TEST_VALUE", "  ASB  ")
    assert env("T5_TEST_VALUE") == "ASB"


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  "])
def test_flag_off(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("T5_TEST_FLAG", raw)
    assert env_flag("T5_TEST_FLAG", "0") is False


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_flag_on(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("T5_TEST_FLAG", raw)
    assert env_flag("T5_TEST_FLAG", "0") is True


def test_int_garbage_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T5_TEST_INT", "пять")
    assert env_int("T5_TEST_INT", 5) == 5


def test_int_minimum_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T5_TEST_INT", "0")
    assert env_int("T5_TEST_INT", 5, minimum=1) == 1


def test_float_garbage_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T5_TEST_FLOAT", "-")
    assert env_float("T5_TEST_FLOAT", 1.5) == 1.5
