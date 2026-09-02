"""Единая нумерация и выбор рейса: FLIGHT_INDEX 1 = [1], 2 = [2] (как TRAIN_INDEX в railway).

Несколько рейсов: FLIGHT_INDEX=4,3 — сначала [4], затем [3].

Здесь же живут разбор FLIGHT_STRICT / FLIGHT_TIME и человеко-читаемое описание
режима. Раньше эти три функции были продублированы в `airlines_api/api_booking.py`
и `airlines_web/steps.py` — а поскольку правки вносились в одну копию, режимы
двух ботов на одном и том же .env могли разойтись.

Как именно найти рейс — по JSON (api) или по DOM (web) — по-прежнему решает
каждый модуль сам; общей здесь только настройка.
"""

from __future__ import annotations

import os


class FlightIndexError(ValueError):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _flag(name: str, default: str = "0") -> bool:
    return _env(name, default).lower() not in ("0", "false", "no", "")


def flight_strict() -> bool:
    """FLIGHT_STRICT=1 → только указанный рейс/время, без запасного выбора."""
    return _flag("FLIGHT_STRICT", "0")


def flight_prefer_time() -> str:
    """FLIGHT_TIME=19.20 и 19:20 — одно и то же; точку приводим к двоеточию."""
    return _env("FLIGHT_TIME").replace(".", ":")


def flight_want_indexes() -> list[int]:
    """
    FLIGHT_INDEX: 1 = [1], 4,3 = [4] затем [3] — как TRAIN_INDEX в railway.

    Пусто = рейс не выбран, смотрим все. Так же ведёт себя railway с пустым
    TRAIN_INDEX, и разное поведение двух ботов на одинаково пустой настройке
    было бы неожиданностью.
    """
    return parse_flight_indexes(_env("FLIGHT_INDEX"), default="")


def flight_selection_label(*, fallback_hint: str = "другие прямые с местами") -> str:
    """
    Строка режима выбора для стартового лога.

    fallback_hint различается у ботов: в api запасной рейс это «с местами»,
    в браузере — «с кнопкой Выбрать». Смысл один, формулировка под интерфейс.
    """
    strict = flight_strict()
    prefer_time = flight_prefer_time()
    want = flight_want_indexes()
    mode = "строго (только указанные рейсы)" if strict else "с запасным выбором"
    if prefer_time:
        scope = "среди всех рейсов" if strict else "среди доступных"
        return f"{mode}, время={prefer_time}, {scope}"
    if not want:
        return "любой — все прямые рейсы с местами, по порядку вылета"
    want_idx = format_flight_indexes(want)
    if strict:
        return f"{mode}, index={want_idx} (только эти позиции в списке)"
    return f"{mode}, index={want_idx} (сначала эти, иначе {fallback_hint})"


def parse_flight_indexes(raw: str | None, *, default: str = "1") -> list[int]:
    """
    FLIGHT_INDEX: 1 = первый рейс, 4,3 = [4] затем [3]. Нумерация с 1.

    Пустой список — законный ответ: он означает «рейс не выбран, смотрим все».
    """
    text = (raw if raw is not None else default).strip() or default
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if not parts:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for part in parts:
        try:
            idx = int(part)
        except ValueError as exc:
            raise FlightIndexError(f"FLIGHT_INDEX: не число: {part!r} в {text!r}") from exc
        if idx < 1:
            raise FlightIndexError(
                f"FLIGHT_INDEX: нумерация с 1 — [1] первый, [2] второй; получено {idx}"
            )
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def parse_flight_index(raw: str | None, *, default: str = "1") -> int:
    """Первый индекс из FLIGHT_INDEX. Ничего не выбрано — первый по списку."""
    indexes = parse_flight_indexes(raw, default=default)
    return indexes[0] if indexes else 1


def format_flight_indexes(user_indexes: list[int]) -> str:
    return ",".join(str(i) for i in user_indexes)


def flight_array_index(user_index: int) -> int:
    """0-based индекс для массивов и DOM."""
    return user_index - 1


def flight_array_indexes(user_indexes: list[int]) -> list[int]:
    return [flight_array_index(i) for i in user_indexes]
