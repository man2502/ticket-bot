#!/usr/bin/env python3
"""Çapar (Turkmenpost) — OTP login + монитор рейсов T5."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from typing import Any

# Корень репозитория и sys.path — в _bootstrap: он ищет корень по маркеру,
# потому что папку модуля переносили и отсчёт «на уровень выше» врал.
from _bootstrap import CHAPAR_ROOT

from core.bootstrap import bootstrap

# Перейти на python из .venv и включить UTF-8 в консоли — до первого print.
bootstrap(__file__)

from chapar_auth import AuthFatalError, ensure_session, login_with_otp, token_configured
from chapar_env import env, load_chapar_env, route_line
from notify_helper import gmail_configured, notify
from passenger_loader import format_passenger_names

STATE_FILE = CHAPAR_ROOT / "chapar_state.json"


def check_at_second() -> int:
    try:
        return max(0, min(59, int(env("CHAPAR_CHECK_AT_SEC", "0") or "0")))
    except ValueError:
        return 0


def wait_for_check_slot() -> None:
    at_sec = check_at_second()
    now = dt.datetime.now()
    nxt = now.replace(second=at_sec, microsecond=0)
    if now >= nxt:
        nxt += dt.timedelta(minutes=1)
    delay = max(0.05, (nxt - now).total_seconds())
    print(f"[slot] жду :{at_sec:02d} (≈{delay:.1f}с до запроса)…")
    time.sleep(delay)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_check_once(*, verbose: bool = True) -> tuple[bool, list[str], dict[str, Any] | None]:
    """TODO: поиск рейсов — следующий curl из mitmweb."""
    msg = "поиск рейсов ещё не подключён — пришли curl GetFlights/search из Çapar"
    if verbose:
        print(f"[chapar] {msg}")
    return False, [msg], None


def cmd_login(*, force: bool = False) -> int:
    try:
        login_with_otp(request_code=True, verbose=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[chapar-auth] ошибка: {exc}")
        return 1
    return 0


def run_watch_loop(*, once: bool = False) -> int:
    try:
        ensure_session(force=not token_configured())
    except Exception as exc:  # noqa: BLE001
        if token_configured():
            print(f"[chapar-auth] предупреждение: {exc} — использую сохранённый token")
        else:
            raise SystemExit(f"[chapar-auth] login не удался: {exc}") from exc

    date_label = env("DATE")
    state_key = f"{env('DEPART_PORT_CODE')}-{env('ARRIVAL_PORT_CODE')}-{date_label}"
    at_sec = check_at_second()

    print("=== Çapar watch ===")
    print(f"Маршрут: {route_line(date_label)}")
    print(f"Пассажиры: {env('PASSENGER_IDS') or '1'}")
    print(f"Слот: каждую минуту в :{at_sec:02d}")
    print(f"[notify] {'Gmail настроен' if gmail_configured() else 'только macOS + консоль'}")
    print()

    state = load_state()

    try:
        while True:
            wait_for_check_slot()
            started = time.strftime("%H:%M:%S")
            print(f"[check] {started} {route_line(date_label)}, passenger: {format_passenger_names()}")
            try:
                available, items, _body = run_check_once()
            except AuthFatalError as exc:
                print(f"\033[91m[chapar-auth] {exc}\033[0m")
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"\033[91m[error] {exc}\033[0m")
                available, items = False, [str(exc)]

            prev = state.get(state_key, {}).get("available")
            if available:
                summary = " | ".join(items[:3]) if items else "есть рейсы"
                print(f"[ok] ЕСТЬ места — {summary}")
                if prev is not True:
                    notify("Çapar: появились билеты", f"{route_line(date_label)}\n{summary}")
                state[state_key] = {
                    "available": True,
                    "items": items[:5],
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_state(state)
                print("[ok] Билеты найдены — монитор остановлен.")
                return 0

            hint = items[0] if items else "свободных мест нет"
            print(f"[--] {hint}")
            state[state_key] = {
                "available": available,
                "items": items[:5],
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_state(state)
            if once:
                return 0
    except KeyboardInterrupt:
        print("\n[stop] Остановлено пользователем.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Çapar (Turkmenpost) — OTP login + монитор T5")
    parser.add_argument("--env", dest="env_path", default="chapar_check.env")
    parser.add_argument("--once", action="store_true", help="Один цикл")
    parser.add_argument("--login", action="store_true", help="Только OTP login")
    args = parser.parse_args(argv)

    load_chapar_env(args.env_path)
    if args.login:
        return cmd_login()
    return run_watch_loop(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
