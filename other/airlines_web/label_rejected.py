#!/usr/bin/env python3
"""Интерактивная разметка captcha_dataset/rejected/ → captcha_dataset/hard/."""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

# macOS: системный Tk deprecated — не спамить в консоль
os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

from _bootstrap import WEB_ROOT, reexec_with_project_venv, setup_paths

reexec_with_project_venv()
setup_paths()

ROOT = WEB_ROOT

from core.open_file import open_in_viewer  # noqa: E402

from captcha_dataset import (  # noqa: E402
    REJECTED_DIR,
    confirmed_count,
    hard_count,
    list_rejected_samples,
    move_rejected_to_uncertain,
    parse_rejected_filename,
    promote_rejected_to_confirmed,
)

HELP_LINE = (
    "6 цифр→hard/ | Enter=оставить в rejected | "
    "u=не понятно→uncertain | d=удалить | q=выход"
)


def _open_image(path: Path) -> None:
    """Показ картинки — общая кроссплатформенная реализация (см. core.open_file)."""
    open_in_viewer(path)


def _meta_line(path: Path) -> str:
    meta = parse_rejected_filename(path)
    if not meta:
        return path.name
    return (
        f"бот ввёл (неверно): {meta['wrong_code']} — solver {meta['source']}"
    )


def _run_terminal(paths: list[Path], *, open_preview: bool) -> int:
    labeled = skipped = uncertain = deleted = 0
    total = len(paths)

    print("[label] в hard/ попадает ТОЛЬКО то, что ты сам введёшь (6 цифр).")
    print("[label] easy-капчи бот копит сам в confirmed/.")
    print(f"[label] {HELP_LINE}")

    for i, path in enumerate(paths, 1):
        print(f"\n[{i}/{total}] {path.name}")
        print(f"  {_meta_line(path)}")

        if open_preview:
            _open_image(path)

        while True:
            try:
                raw = input(f"  {HELP_LINE}\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raw = "q"

            low = raw.lower()
            if low in ("q", "quit", "exit"):
                _print_summary(labeled, skipped, uncertain, deleted)
                return 0
            if low in ("", "s", "skip"):
                skipped += 1
                print("  оставлено в rejected (можно разметить позже)")
                break
            if low in ("u", "uncertain", "?"):
                if move_rejected_to_uncertain(path):
                    uncertain += 1
                    print("  → uncertain/ (не для обучения)")
                break
            if low in ("d", "delete", "del"):
                path.unlink(missing_ok=True)
                deleted += 1
                print("  удалено навсегда")
                break

            digits = "".join(ch for ch in raw if ch.isdigit())
            if len(digits) != 6:
                print("  нужно ровно 6 цифр (или u / Enter / d / q)")
                continue

            dest = promote_rejected_to_confirmed(path, digits)
            if dest:
                labeled += 1
            break

    _print_summary(labeled, skipped, uncertain, deleted)
    return 0


def _print_summary(labeled: int, skipped: int, uncertain: int, deleted: int) -> None:
    print(
        f"\n[label] +{labeled} hard/ | пропущено {skipped} | "
        f"uncertain {uncertain} | удалено {deleted} | "
        f"осталось rejected {len(list_rejected_samples())} | "
        f"confirmed/ {confirmed_count()} | hard/ {hard_count()}"
    )


def _run_gui(paths: list[Path]) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    from PIL import Image, ImageTk

    labeled = skipped = uncertain = deleted = 0
    index = 0
    photo_ref: list[object] = []

    root = tk.Tk()
    root.title("Разметка rejected")
    root.geometry("760x580")
    root.minsize(520, 460)

    status_var = tk.StringVar(value="")
    meta_var = tk.StringVar(value="")
    file_var = tk.StringVar(value="")
    hint_var = tk.StringVar(
        value=(
            "Правильный код → hard/ (overlap). "
            "Easy-капчи бот сохраняет в confirmed/. "
            "«Не понятно» — uncertain/, не для обучения."
        )
    )
    entry_var = tk.StringVar(value="")

    img_label = tk.Label(root, bg="#1e1e1e")
    img_label.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 6))

    info = ttk.Frame(root, padding=(12, 0))
    info.pack(fill=tk.X)
    ttk.Label(info, textvariable=file_var, font=("", 11, "bold")).pack(anchor=tk.W)
    ttk.Label(info, textvariable=meta_var).pack(anchor=tk.W)
    ttk.Label(info, textvariable=status_var).pack(anchor=tk.W, pady=(4, 0))
    ttk.Label(info, textvariable=hint_var, wraplength=700).pack(anchor=tk.W, pady=(6, 0))

    entry_frame = ttk.Frame(root, padding=12)
    entry_frame.pack(fill=tk.X)
    ttk.Label(entry_frame, text="Правильный код (если уверен):").pack(side=tk.LEFT)
    entry = ttk.Entry(entry_frame, textvariable=entry_var, width=12, font=("", 16))
    entry.pack(side=tk.LEFT, padx=(8, 0))

    btns = ttk.Frame(root, padding=(12, 0, 12, 12))
    btns.pack(fill=tk.X)

    def show_current() -> None:
        nonlocal index
        photo_ref.clear()
        if index >= len(paths):
            messagebox.showinfo(
                "Готово",
                f"+{labeled} в hard/\n"
                f"hard/ всего: {hard_count()}\n"
                f"пропущено (остались в rejected): {skipped}\n"
                f"не понятно → uncertain: {uncertain}\n"
                f"удалено: {deleted}\n"
                f"confirmed всего: {confirmed_count()}",
            )
            root.destroy()
            return

        path = paths[index]
        if not path.exists():
            index += 1
            show_current()
            return

        file_var.set(f"{index + 1} / {len(paths)} — {path.name}")
        meta_var.set(_meta_line(path))
        status_var.set(
            f"confirmed: {confirmed_count()} | "
            f"в этой сессии осталось: {len(paths) - index}"
        )
        entry_var.set("")
        entry.focus_set()

        try:
            img = Image.open(path).convert("RGB")
            max_w, max_h = 640, 300
            img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            photo_ref.append(photo)
            img_label.configure(image=photo, text="")
        except Exception as exc:  # noqa: BLE001
            img_label.configure(image="", text=f"Не открыть картинку:\n{exc}")

    def do_save() -> None:
        nonlocal index, labeled
        if index >= len(paths):
            return
        digits = "".join(ch for ch in entry_var.get() if ch.isdigit())
        if len(digits) != 6:
            messagebox.showwarning(
                "Код",
                "Введите ровно 6 цифр.\n\n"
                "Если не уверены — нажмите «Не понятно» (u), "
                "капча уйдёт в uncertain/ и не попадёт в обучение.",
            )
            entry.focus_set()
            return
        path = paths[index]
        if promote_rejected_to_confirmed(path, digits):
            labeled += 1
        index += 1
        show_current()

    def do_skip() -> None:
        nonlocal index, skipped
        skipped += 1
        index += 1
        show_current()

    def do_uncertain() -> None:
        nonlocal index, uncertain
        if index >= len(paths):
            return
        path = paths[index]
        if move_rejected_to_uncertain(path):
            uncertain += 1
        index += 1
        show_current()

    def do_delete() -> None:
        nonlocal index, deleted
        if index >= len(paths):
            return
        if not messagebox.askyesno(
            "Удалить",
            f"Удалить {paths[index].name} навсегда?\n"
            "(лучше «Не понятно», если просто не уверены)",
        ):
            return
        paths[index].unlink(missing_ok=True)
        deleted += 1
        index += 1
        show_current()

    def do_quit() -> None:
        if messagebox.askyesno("Выход", "Закрыть разметку?"):
            root.destroy()

    ttk.Button(btns, text="Сохранить (Enter)", command=do_save).pack(side=tk.LEFT)
    ttk.Button(btns, text="Позже (Esc)", command=do_skip).pack(side=tk.LEFT, padx=6)
    ttk.Button(btns, text="Не понятно (u)", command=do_uncertain).pack(side=tk.LEFT, padx=6)
    ttk.Button(btns, text="Удалить (Del)", command=do_delete).pack(side=tk.LEFT)
    ttk.Button(btns, text="Выход", command=do_quit).pack(side=tk.RIGHT)

    root.bind("<Return>", lambda _e: do_save())
    root.bind("<Escape>", lambda _e: do_skip())
    root.bind("u", lambda _e: do_uncertain())
    root.bind("<Delete>", lambda _e: do_delete())
    root.protocol("WM_DELETE_WINDOW", do_quit)

    show_current()
    root.mainloop()
    _print_summary(labeled, skipped, uncertain, deleted)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Разметка rejected → hard/. Easy-капчи — в confirmed/ (бот). "
            "Сомнительные → uncertain/."
        )
    )
    ap.add_argument("--terminal", action="store_true", help="Терминал + Preview (по умолчанию на macOS)")
    ap.add_argument(
        "--gui",
        action="store_true",
        help="Окно tkinter (на macOS часто хуже Preview)",
    )
    ap.add_argument(
        "--open",
        action="store_true",
        help="В терминальном режиме открывать картинку в Preview",
    )
    ap.add_argument("--shuffle", action="store_true", help="Случайный порядок")
    ap.add_argument("--limit", type=int, default=0, help="Максимум файлов за сессию (0=все)")
    args = ap.parse_args()

    paths = list_rejected_samples()
    if not paths:
        print(f"[label] в {REJECTED_DIR} пусто — нечего размечать")
        return 0

    if args.shuffle:
        random.shuffle(paths)
    if args.limit > 0:
        paths = paths[: args.limit]

    print(f"[label] файлов к разметке: {len(paths)} (confirmed сейчас: {confirmed_count()})")

    use_terminal = args.terminal or not args.gui
    if sys.platform == "darwin" and not args.gui:
        use_terminal = True
        args.open = True

    if use_terminal:
        if sys.platform == "darwin":
            print("[label] режим: терминал + Preview (на Mac надёжнее чем tkinter)")
        return _run_terminal(paths, open_preview=args.open)
    try:
        return _run_gui(paths)
    except Exception as exc:
        if type(exc).__name__ != "TclError":
            raise
        print("[label] GUI недоступен — терминал + Preview")
        return _run_terminal(paths, open_preview=True)


if __name__ == "__main__":
    raise SystemExit(main())
