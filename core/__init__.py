"""Общие инфраструктурные модули для всех ботов репозитория.

Здесь только транспорт, состояние и тайминг — никакой доменной логики
(рейсы, места, капча остаются в railway/, airlines_api/, airlines_web/).
"""

from __future__ import annotations

__all__ = ["http", "state", "slots", "booking_guard"]
