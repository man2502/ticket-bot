"""Единая нумерация рейсов: FLIGHT_INDEX 1 = [1], 2 = [2] (как TRAIN_INDEX в railway).

Несколько рейсов: FLIGHT_INDEX=4,3 — сначала [4], затем [3].
"""

from __future__ import annotations


class FlightIndexError(ValueError):
    pass


def parse_flight_indexes(raw: str | None, *, default: str = "1") -> list[int]:
    """FLIGHT_INDEX: 1 = первый рейс, 4,3 = [4] затем [3]. Нумерация с 1."""
    text = (raw if raw is not None else default).strip() or default
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if not parts:
        parts = [default]
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
    """Первый индекс из FLIGHT_INDEX (для совместимости)."""
    return parse_flight_indexes(raw, default=default)[0]


def format_flight_indexes(user_indexes: list[int]) -> str:
    return ",".join(str(i) for i in user_indexes)


def flight_array_index(user_index: int) -> int:
    """0-based индекс для массивов и DOM."""
    return user_index - 1


def flight_array_indexes(user_indexes: list[int]) -> list[int]:
    return [flight_array_index(i) for i in user_indexes]
