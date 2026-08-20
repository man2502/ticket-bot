"""Атомарное чтение/запись JSON-состояния.

`Path.write_text` — это truncate + write. Ctrl-C или падение между ними
оставляет обрезанный файл, а `load_state` в таком случае молча возвращает {}
и теряет флаг «уже забронировано». Здесь запись идёт через временный файл
в той же директории и `os.replace` (атомарен на POSIX и на Windows).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["load_json", "save_json", "StateFileCorrupt"]


class StateFileCorrupt(RuntimeError):
    """Файл состояния существует, но не парсится."""


def load_json(path: Path | str, default: Any = None, *, strict: bool = False) -> Any:
    """
    Прочитать JSON. При битом файле: strict=False → default, strict=True → исключение.

    Битый файл переименовывается в `<имя>.corrupt`, чтобы не потерять содержимое
    и чтобы следующий запуск начал с чистого листа.
    """
    file_path = Path(path)
    if not file_path.exists():
        return {} if default is None else default

    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        backup = file_path.with_suffix(file_path.suffix + ".corrupt")
        try:
            os.replace(file_path, backup)
        except OSError:
            backup = file_path
        message = f"{file_path.name} повреждён ({exc}); сохранён как {backup.name}"
        if strict:
            raise StateFileCorrupt(message) from exc
        print(f"[warn] {message}")
        return {} if default is None else default


def save_json(path: Path | str, data: Any, *, indent: int | None = 2) -> None:
    """
    Атомарная запись: tmp в той же папке → fsync → os.replace.

    indent=None — компактный однострочный JSON (для служебных счётчиков,
    которые человек не читает).
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{file_path.name}.", suffix=".tmp", dir=str(file_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, file_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
