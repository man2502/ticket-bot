#!/usr/bin/env python3
"""Ручная разметка API-капчи (5 цифр) из captcha_dataset_api/unlabeled/."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
for _path in (str(ROOT), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core.open_file import open_in_viewer  # noqa: E402

from captcha_dataset_api import CONFIRMED_DIR, N_DIGITS, UNLABELED_DIR, save_confirmed  # noqa: E402


def main() -> int:
    if not UNLABELED_DIR.exists():
        print(f"Нет папки {UNLABELED_DIR}")
        return 1

    files = sorted(UNLABELED_DIR.glob("*.png"))
    if not files:
        print("unlabeled/ пуст — запусти api_check.py для сбора картинок")
        return 0

    print(f"Разметка {len(files)} капч (Enter — пропуск, q — выход)")
    done = 0
    for path in files:
        print(f"\n{path.name}")
        open_in_viewer(path)

        while True:
            raw = input(f"Введи {N_DIGITS} цифр (Enter=пропуск, q=выход): ").strip()
            if raw.lower() in ("q", "quit", "exit"):
                print(f"Готово: {done} размечено")
                return 0
            if not raw:
                break
            code = re.sub(r"\D", "", raw)
            if len(code) != N_DIGITS:
                print(f"Нужно ровно {N_DIGITS} цифр")
                continue
            data = path.read_bytes()
            save_confirmed(data, code, source="manual")
            path.unlink(missing_ok=True)
            done += 1
            break

    print(f"Готово: {done} размечено → {CONFIRMED_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
