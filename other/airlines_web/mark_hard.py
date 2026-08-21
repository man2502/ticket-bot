#!/usr/bin/env python3
"""Скопировать капчи из confirmed/ в hard/."""

from __future__ import annotations

import argparse

from _bootstrap import WEB_ROOT, reexec_with_project_venv, setup_paths

reexec_with_project_venv()
setup_paths()

ROOT = WEB_ROOT

from captcha_dataset import CONFIRMED_DIR, hard_count, mark_hard  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Скопировать PNG из confirmed/ в hard/ (миграция старых overlap)."
    )
    ap.add_argument(
        "files",
        nargs="*",
        help="Имена файлов из confirmed/ (например 123456_abc123def456.png)",
    )
    ap.add_argument(
        "--all-confirmed",
        action="store_true",
        help="Скопировать все PNG из confirmed/ в hard/",
    )
    args = ap.parse_args()

    marked = 0
    if args.all_confirmed:
        paths = sorted(CONFIRMED_DIR.glob("*.png"))
    else:
        paths = [CONFIRMED_DIR / name for name in args.files]

    for path in paths:
        if not path.is_file():
            print(f"пропуск (нет файла): {path.name}")
            continue
        before = hard_count()
        if mark_hard(source_path=path) and hard_count() > before:
            marked += 1
            print(f"+ hard/{path.name}")

    if not marked and not args.all_confirmed and not args.files:
        ap.print_help()
        return 1

    print(f"[mark_hard] добавлено {marked}, всего hard/: {hard_count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
