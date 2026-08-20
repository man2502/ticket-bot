"""ANSI-цвета для вывода Railway в терминал.

Палитра переехала в `core.term` — она была одинаковой в пяти файлах. Здесь
осталась тонкая обёртка ради обратной совместимости: модули railway импортируют
`green` / `red` / `purple` именно отсюда.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.term import GREEN, PURPLE, RED, RESET, green, purple, red  # noqa: E402,F401

__all__ = ["green", "purple", "red", "GREEN", "PURPLE", "RED", "RESET"]
