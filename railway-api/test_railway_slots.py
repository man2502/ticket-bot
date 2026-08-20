"""Тесты тайминга слотов и атомарного состояния.

Запуск (из корня репозитория):
    .venv/bin/python -m pytest railway/test_railway_slots.py -q
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "railway")):
    if path not in sys.path:
        sys.path.insert(0, path)

from core.slots import now_second, seconds_until_second
from core.state import load_json, save_json


def _at(second: int, micro: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 20, 12, 0, second, micro)


# --------------------------------------------------------------------------- #
# Слоты
# --------------------------------------------------------------------------- #


def test_now_second_keeps_fraction() -> None:
    assert now_second(_at(57, 900_000)) == pytest.approx(57.9)


def test_no_systematic_half_second_lag() -> None:
    """
    Старый расчёт: (60 - 57) + 0 = 3.0 → выстрел в :00.900.
    Правильный: 60 - 57.9 = 2.1 → выстрел ровно в :00.000.
    """
    delay = seconds_until_second(0.0, now=_at(57, 900_000))
    assert delay == pytest.approx(2.1, abs=1e-6)


@pytest.mark.parametrize("micro", [0, 250_000, 500_000, 999_999])
def test_target_hit_is_exact_regardless_of_fraction(micro: int) -> None:
    now = _at(30, micro)
    delay = seconds_until_second(45.0, now=now)
    landed = (now_second(now) + delay) % 60
    assert landed == pytest.approx(45.0, abs=1e-6)


def test_wraps_to_next_minute() -> None:
    delay = seconds_until_second(5.0, now=_at(50))
    assert delay == pytest.approx(15.0)


def test_jitter_is_added_to_target() -> None:
    delay = seconds_until_second(0.0, jitter=0.2, now=_at(59, 0))
    assert delay == pytest.approx(1.2)


def test_small_lateness_fires_immediately() -> None:
    delay = seconds_until_second(30.0, now=_at(30, 200_000), allow_past_window=0.5)
    assert delay == 0.0


def test_large_lateness_waits_full_minute() -> None:
    delay = seconds_until_second(30.0, now=_at(35), allow_past_window=0.5)
    assert delay == pytest.approx(55.0)


# --------------------------------------------------------------------------- #
# Состояние
# --------------------------------------------------------------------------- #


def test_save_json_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    save_json(target, {"booked": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"booked": True}
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_load_json_quarantines_corrupt_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"booked": tru', encoding="utf-8")
    assert load_json(target, {}) == {}
    assert (tmp_path / "state.json.corrupt").exists()


def test_load_json_missing_returns_default(tmp_path: Path) -> None:
    assert load_json(tmp_path / "nope.json", {"a": 1}) == {"a": 1}


def test_overwrite_keeps_previous_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    save_json(target, {"v": 1})
    with pytest.raises(TypeError):
        save_json(target, {"v": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
