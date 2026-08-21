"""Роуты панели, разложенные по разделам.

Приложение (`web/app.py`) только подключает их: сборка и бизнес-логика
разнесены намеренно, иначе app.py быстро становится файлом на тысячу строк.
"""

from __future__ import annotations

__all__ = ["auth", "dashboard", "passengers", "stations", "users"]
