"""Общий venv и sys.path для скриптов в airlines_web/.

Корень репозитория ищется по маркеру, а не отсчётом `.parent`. Причина простая:
папку модуля уже переносили (`airlines_web/` → `other/airlines_web/`), и жёсткое
«корень — это на уровень выше меня» после переезда указывало на `other/`,
из-за чего переставал находиться пакет `core`.

Функция поиска продублирована здесь намеренно: импортировать её из `core`
нельзя, потому что до вычисления корня сам `core` ещё не виден.
"""

from __future__ import annotations

import sys
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent


def _find_repo_root(start: Path) -> Path:
    """Ближайшая папка вверх по дереву, в которой лежит пакет core."""
    for candidate in (start, *start.parents):
        if (candidate / "core" / "bootstrap.py").is_file():
            return candidate
    # Маркер не нашёлся — старое поведение, чтобы ошибка была понятной
    # («нет модуля core»), а не «нет файла _bootstrap».
    return start.parent


REPO_ROOT = _find_repo_root(WEB_ROOT)

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.bootstrap import reexec_with_project_venv as _reexec  # noqa: E402
from core.console import enable_utf8_output  # noqa: E402


def reexec_with_project_venv() -> None:
    """Перезапуск нужным интерпретатором + UTF-8 в консоли (иначе `→` роняет print)."""
    # sys.argv[0] — сам запускаемый скрипт; именно его и надо перезапустить.
    _reexec(sys.argv[0] or __file__)
    enable_utf8_output()


def setup_paths() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if str(WEB_ROOT) not in sys.path:
        sys.path.insert(0, str(WEB_ROOT))
