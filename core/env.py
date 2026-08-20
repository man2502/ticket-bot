"""Чтение переменных окружения — одна реализация на весь репозиторий.

В проекте было семь копий функции `env()`, и они делились на два лагеря
с РАЗНЫМ поведением на пустом значении:

    (os.getenv(name) or default).strip()   # railway_env, api_env, notify_helper
    os.getenv(name, default).strip()       # steps, watch, browser_helper

Разница видна, когда переменная задана, но пуста — `NOTIFY_EMAIL=` в .env.
Первый вариант вернёт default, второй — пустую строку. То есть одна и та же
строка в .env означала разное в зависимости от того, какой модуль её прочитал.

Здесь принят первый вариант: «переменная есть, но пустая» трактуется как
«не задана». Именно этого ждёт человек, который закомментировал значение,
оставив ключ. Все места, где нужен именно пустой ответ, вызывают `env(name)`
без default — там поведение не изменилось.
"""

from __future__ import annotations

import os

__all__ = ["env", "env_flag", "env_int", "env_float"]

# Значения, которые считаем выключенным флагом. Пустая строка тоже здесь:
# `FLAG=` — это «не задано», а не «включено».
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def env(name: str, default: str = "") -> str:
    """Значение переменной без пробелов по краям; пустое значение = default."""
    return (os.getenv(name) or default).strip()


def env_flag(name: str, default: str = "0") -> bool:
    """Булев флаг: 1/true/yes/on — включено, 0/false/no/off/пусто — выключено."""
    return env(name, default).lower() not in _FALSE_VALUES


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Целое из env. Мусор вместо числа не роняет запуск — берём default."""
    raw = env(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        print(f"[warn] {name}={raw!r} — не целое число, беру {default}")
        value = default
    if minimum is not None:
        return max(minimum, value)
    return value


def env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    """Дробное из env с тем же поведением при мусоре, что и env_int."""
    raw = env(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        print(f"[warn] {name}={raw!r} — не число, беру {default}")
        value = default
    if minimum is not None:
        return max(minimum, value)
    return value
