#!/usr/bin/env python3
"""Разметка API rejected (5 цифр) → captcha_dataset_api/hard/."""

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

from captcha_dataset_api import (  # noqa: E402
    N_DIGITS,
    REJECTED_DIR,
    confirmed_count,
    hard_count,
    list_rejected_samples,
    parse_rejected_filename,
    promote_rejected_to_hard,
)

HELP = f"{N_DIGITS} цифр→hard/ | Enter=оставить в rejected | q=выход"


def _open_image(path: Path) -> None:
    """Показ картинки — общая кроссплатформенная реализация (см. core.open_file)."""
    open_in_viewer(path)


def _meta_line(path: Path) -> str:
    meta = parse_rejected_filename(path)
    if not meta:
        return path.name
    return f"CNN ввёл (неверно): {meta['wrong_code']} — {meta['source']}, {meta['reason']}"


def main() -> int:
    paths = list_rejected_samples()
    if not paths:
        print(f"[label-api] в {REJECTED_DIR} пусто — нечего размечать")
        return 0

    print(f"[label-api] файлов: {len(paths)} | confirmed={confirmed_count()} hard={hard_count()}")
    print(f"[label-api] {HELP}")

    labeled = skipped = 0
    for i, path in enumerate(paths, 1):
        print(f"\n[{i}/{len(paths)}] {path.name}")
        print(f"  {_meta_line(path)}")
        _open_image(path)

        while True:
            try:
                raw = input(f"  {HELP}\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raw = "q"

            low = raw.lower()
            if low in ("q", "quit", "exit"):
                print(
                    f"\n[label-api] +{labeled} hard/ | пропущено {skipped} | "
                    f"осталось rejected {len(list_rejected_samples())} | "
                    f"confirmed={confirmed_count()} hard={hard_count()}"
                )
                return 0
            if low in ("", "s", "skip"):
                skipped += 1
                print("  оставлено в rejected")
                break

            digits = re.sub(r"\D", "", raw)
            if len(digits) != N_DIGITS:
                print(f"  нужно ровно {N_DIGITS} цифр (или Enter / q)")
                continue

            if promote_rejected_to_hard(path, digits):
                labeled += 1
            break

    print(
        f"\n[label-api] +{labeled} hard/ | пропущено {skipped} | "
        f"осталось rejected {len(list_rejected_samples())} | "
        f"confirmed={confirmed_count()} hard={hard_count()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
