"""Единый старт для всех точек входа: нужный python + рабочая консоль.

Раньше каждый скрипт (`railway_check.py`, `api_check.py`, `chapar_check.py`,
`airlines_web/_bootstrap.py`) нёс собственную копию одной и той же функции
`_reexec_with_project_venv`, и все четыре копии искали интерпретатор по пути
`.venv/bin/python`. На Windows его не существует — там `.venv/Scripts/python.exe`.
Итог: `python railway_check.py` из системного питона молча продолжал работу без
зависимостей проекта и падал позже, на `import dotenv`, с непонятной ошибкой.

Здесь это одно место, знающее про обе раскладки, плюс включение UTF-8 в консоли
(см. `core.console` — без него логи с `→` роняют процесс на русской Windows).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from core.console import enable_utf8_output

__all__ = ["venv_python", "reexec_with_project_venv", "bootstrap"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def venv_python() -> Path | None:
    """Путь к python из .venv проекта или None, если окружения нет."""
    candidates = (
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",  # Windows
        REPO_ROOT / ".venv" / "bin" / "python",  # macOS / Linux
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _same_interpreter(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def reexec_with_project_venv(script: str | Path) -> None:
    """
    Перезапустить скрипт интерпретатором из .venv, если запустили не им.

    Ничего не делает, когда .venv нет (окружение собрано иначе — pyenv, conda,
    системный python с установленными зависимостями): молча продолжаем.
    """
    target = venv_python()
    if target is None:
        return
    if _same_interpreter(Path(sys.executable), target):
        return

    script_path = str(Path(script).resolve())
    # execv заменяет процесс — возврата из этой строки не будет.
    os.execv(str(target), [str(target), script_path, *sys.argv[1:]])


def bootstrap(script: str | Path) -> None:
    """Полный старт точки входа: правильный python + UTF-8 в консоли."""
    reexec_with_project_venv(script)
    enable_utf8_output()
