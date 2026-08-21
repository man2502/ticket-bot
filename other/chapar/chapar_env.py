"""Env для монитора Çapar (billing.post.gov.tm)."""

from __future__ import annotations

import os
from pathlib import Path

from _bootstrap import CHAPAR_ROOT

from dotenv import load_dotenv

from core.env import env
DEFAULT_ENV = CHAPAR_ROOT / "chapar_check.env"
LOCAL_ENV = CHAPAR_ROOT / ".env.local"


# Реализация общая — core.env (там же описано, почему пустое значение = default).


def load_chapar_env(path: Path | str | None = None) -> Path:
    if path is None:
        candidate = DEFAULT_ENV
    else:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = CHAPAR_ROOT / candidate

    if not candidate.exists():
        raise SystemExit(
            f"Нет {candidate}\n"
            f"Создай: cp chapar_check.env.example chapar_check.env"
        )

    load_dotenv(candidate, override=True)
    notify_email = env("NOTIFY_EMAIL")
    print(f"[chapar-env] {candidate.name}")

    if LOCAL_ENV.exists():
        load_dotenv(LOCAL_ENV, override=True)
        print(f"[chapar-env] {LOCAL_ENV.name}")

    if notify_email:
        os.environ["NOTIFY_EMAIL"] = notify_email

    return candidate


def base_url() -> str:
    return (env("CHAPAR_BASE_URL", "https://billing.post.gov.tm") or "https://billing.post.gov.tm").rstrip("/")


def route_line(date_str: str | None = None) -> str:
    depart = env("DEPART_PORT_CODE")
    arrival = env("ARRIVAL_PORT_CODE")
    date = (date_str or env("DATE") or "").strip()
    return f"{depart} → {arrival}, {date}"
