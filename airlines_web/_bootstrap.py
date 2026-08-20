"""Общий venv и sys.path для скриптов в airlines_web/.

Логика перезапуска на python из .venv переехала в `core.bootstrap` — она была
одинаковой в четырёх файлах и во всех четырёх искала интерпретатор только по
пути `.venv/bin/python`, которого на Windows не существует. Здесь остаётся
тонкая обёртка ради обратной совместимости: скрипты модуля зовут
`reexec_with_project_venv()` + `setup_paths()` в этом порядке.
"""

from __future__ import annotations

import sys
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WEB_ROOT.parent

# core должен быть импортируем до первого вызова — путь добавляем сразу.
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
