"""Открыть файл в системном просмотрщике — на любой ОС.

Во всех четырёх модулях показ картинки капчи был написан как
`subprocess.run(["open", path])`. Команда `open` есть только в macOS: на Windows
и Linux вызов молча падал в `except`, картинка не открывалась, а человек за
терминалом видел приглашение «введи 5 цифр» и ни одной цифры перед глазами.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

__all__ = ["open_in_viewer"]


def open_in_viewer(path: Path | str, *, quiet: bool = False) -> bool:
    """
    Показать файл системным средством. True — команда запущена.

    Ошибку не бросает: не открылась картинка — это неудобство, а не причина
    ронять монитор. Путь всё равно печатается, файл можно открыть руками.
    """
    target = Path(path)
    if not target.exists():
        return False

    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        elif sys.platform == "win32":
            # os.startfile есть только на Windows; аналога `open` там нет.
            os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
        return True
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"[warn] не открыл {target.name}: {exc} — открой вручную: {target}")
        return False
