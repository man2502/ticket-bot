"""Слоты acc1/acc2: два Chrome-профиля и тайминг; логин — общий T5_* из .env / profiles/*.env."""

from __future__ import annotations

import os

BROWSER_SLOT_KEYS = (
    "BOOK_SEARCH_AT_SEC",
    "BROWSER_WINDOW_POS",
)


def normalize_account(name: str | None) -> str:
    raw = (name or "acc1").strip().lower()
    if raw in ("1", "acc1"):
        return "acc1"
    if raw in ("2", "acc2"):
        return "acc2"
    raise SystemExit(f"Неизвестный --account: {name!r} (нужно acc1 или acc2)")


def account_var_prefix(account: str) -> str:
    return "ACC1" if normalize_account(account) == "acc1" else "ACC2"


def apply_account_overlay(account: str) -> str:
    """
    --account acc1|acc2 → CHROME_PROFILE и ACC1_* / ACC2_* только для браузера
    (BOOK_SEARCH_AT_SEC, BROWSER_WINDOW_POS из .env.local).

    T5_PHONE / T5_PASSWORD — из .env или profiles/*.env, без ACC1_/ACC2_.
    """
    acc = normalize_account(account)
    prefix = account_var_prefix(acc)
    applied = 0
    for key in BROWSER_SLOT_KEYS:
        val = (os.getenv(f"{prefix}_{key}") or "").strip()
        if val:
            os.environ[key] = val
            applied += 1

    os.environ["ACCOUNT_NAME"] = acc
    os.environ["CHROME_PROFILE"] = acc

    print(
        f"[env] слот {acc} ← {prefix}_* ({applied} браузер-параметров), "
        f"profile=.chrome-profile-{acc}"
    )
    return acc
