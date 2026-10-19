"""Env для монитора Turkmenistan Railway (api.railway.gov.tm)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from core.env import env

RAILWAY_ROOT = Path(__file__).resolve().parent
# Где бот хранит журнал броней, состояние и кэш станций. В контейнере — том с
# данными (RAILWAY_DATA_DIR), иначе пересборка образа стёрла бы журнал, а с ним
# и защиту от двойной брони. Панель читает журнал по тому же пути.
RAILWAY_DATA_DIR = Path(os.getenv("RAILWAY_DATA_DIR") or RAILWAY_ROOT)
DEFAULT_ENV = RAILWAY_ROOT / "check.env"
LOCAL_ENV = RAILWAY_ROOT / ".env.local"


# Реализация общая — core.env (там же описано, почему пустое значение = default).


def load_railway_env(path: Path | str | None = None) -> Path:
    if path is None:
        candidate = DEFAULT_ENV
    else:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = RAILWAY_ROOT / candidate

    if not candidate.exists():
        raise SystemExit(
            f"Нет {candidate}\n"
            f"Создай: cp check.env.example check.env"
        )

    load_dotenv(candidate, override=True)
    notify_email = env("NOTIFY_EMAIL")
    print(f"[railway-env] {candidate.name}")

    if LOCAL_ENV.exists():
        load_dotenv(LOCAL_ENV, override=True)
        print(f"[railway-env] {LOCAL_ENV.name}")

    if notify_email:
        os.environ["NOTIFY_EMAIL"] = notify_email

    from railway_stations import refresh_stations_cache

    refresh_stations_cache()

    return candidate


def base_url() -> str:
    return (env("RAILWAY_BASE_URL", "https://api.railway.gov.tm") or "https://api.railway.gov.tm").rstrip("/")


def route_line(date_str: str | None = None) -> str:
    from railway_stations import station_label

    source = station_label(env("SOURCE_STATION_ID"))
    destination = station_label(env("DESTINATION_STATION_ID"))
    date = (date_str or env("DATE") or "").strip()
    return f"{source} → {destination}, {date}"


def to_railway_date(raw: str) -> str:
    """DD.MM.YYYY или YYYY-MM-DD → YYYY-MM-DD для /v2/trips."""
    text = (raw or "").strip()
    if not text:
        raise SystemExit("DATE пуст — укажи в check.env, напр. DATE=10.08.2026")

    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text

    if "." in text:
        parts = text.split(".")
        if len(parts) != 3:
            raise SystemExit(f"DATE: не разобрал {text!r}")
        day, month, year = (int(parts[0]), int(parts[1]), int(parts[2]))
        return f"{year:04d}-{month:02d}-{day:02d}"

    raise SystemExit(f"DATE: не разобрал {text!r} — нужен DD.MM.YYYY или YYYY-MM-DD")
