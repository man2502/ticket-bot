"""CLI: --profile, --account и быстрые переопределения маршрута/пассажира."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# CLI флаг → переменная окружения
CLI_OVERRIDES: dict[str, str] = {
    "passenger": "PASSENGER_IDS",
    "passengers": "PASSENGER_IDS",
    "dates": "DATES",
    "depart": "DEPART_PORT_CODE",
    "arrival": "ARRIVAL_PORT_CODE",
    "flight-index": "FLIGHT_INDEX",
    "flight-strict": "FLIGHT_STRICT",
    "flight-time": "FLIGHT_TIME",
    "notify-email": "NOTIFY_EMAIL",
}


def parse_book_cli(argv: list[str] | None = None) -> dict:
    """
    Разбор аргументов book.py.

    Returns:
        env_path — явный --env (редко)
        profile — имя профиля (--profile aliya → profiles/aliya.env)
        account — слот браузера acc1 / acc2 (не отдельный логин)
        overrides — PASSENGER_IDS, DATES, … из CLI
    """
    argv = list(argv if argv is not None else sys.argv[1:])
    env_path: Path | None = None
    profile: str | None = None
    account: str | None = None
    overrides: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--env" and i + 1 < len(argv):
            env_path = Path(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--env="):
            env_path = Path(arg.split("=", 1)[1])
            i += 1
            continue
        if arg == "--profile" and i + 1 < len(argv):
            profile = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
            i += 1
            continue
        if arg == "--account" and i + 1 < len(argv):
            account = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--account="):
            account = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("--") and "=" in arg:
            key = arg[2:].split("=", 1)[0].lower()
            val = arg.split("=", 1)[1]
            if key in CLI_OVERRIDES:
                overrides[CLI_OVERRIDES[key]] = val
                i += 1
                continue
        if arg.startswith("--"):
            key = arg[2:].lower()
            if key in CLI_OVERRIDES and i + 1 < len(argv):
                overrides[CLI_OVERRIDES[key]] = argv[i + 1]
                i += 2
                continue
        i += 1
    return {
        "env_path": env_path,
        "profile": profile,
        "account": account,
        "overrides": overrides,
    }


def apply_cli_overrides(overrides: dict[str, str]) -> None:
    for env_key, val in overrides.items():
        if val is not None and str(val).strip() != "":
            os.environ[env_key] = str(val).strip()
            cli_flag = next(k for k, v in CLI_OVERRIDES.items() if v == env_key)
            print(f"[env] CLI --{cli_flag} → {env_key}={val!r}")


def booking_summary() -> str:
    dep = (os.getenv("DEPART_PORT_CODE") or "?").strip()
    arr = (os.getenv("ARRIVAL_PORT_CODE") or "?").strip()
    dates = (os.getenv("DATES") or "?").strip()
    pid = (os.getenv("PASSENGER_IDS") or "?").strip()
    fi = (os.getenv("FLIGHT_INDEX") or "1").strip()
    try:
        from passenger_loader import get_passenger_ids

        pids = ",".join(get_passenger_ids())
    except Exception:
        pids = pid
    return f"{dep}→{arr} | dates={dates} | passengers={pids} | flight_index={fi}"
