"""Поиск корня репозитория и sys.path для скриптов chapar/.

Корень ищется по маркеру, а не отсчётом `.parent`: папку модуля уже переносили
(`chapar/` → `other/chapar/`), и жёсткое «корень — это на уровень выше меня»
после переезда указывало на `other/`, из-за чего переставал находиться `core`.

Функция поиска живёт здесь, а не в `core`, по необходимости: до вычисления
корня сам `core` ещё не виден.
"""

from __future__ import annotations

import sys
from pathlib import Path

CHAPAR_ROOT = Path(__file__).resolve().parent


def _find_repo_root(start: Path) -> Path:
    """Ближайшая папка вверх по дереву, в которой лежит пакет core."""
    for candidate in (start, *start.parents):
        if (candidate / "core" / "bootstrap.py").is_file():
            return candidate
    return start.parent


REPO_ROOT = _find_repo_root(CHAPAR_ROOT)


def setup_paths() -> None:
    """Корень репозитория и папку модуля — в sys.path."""
    for path in (REPO_ROOT, CHAPAR_ROOT):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


setup_paths()
