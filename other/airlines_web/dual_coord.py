"""Синхронизация двух book.py: один получил банк — оба закрывают Chrome."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STOP_FILE = ROOT / ".dual_stop"
_STALE_SEC = 7200  # 2 ч — старый флаг не мешает одиночному запуску


def dual_stop_basename(*, profile: str | None = None, env_path: Path | None = None) -> str:
    """
    Имя stop-файла для группы acc1+acc2 одного .env / профиля.
    Разные env не должны делить один .dual_stop.
    """
    if profile:
        safe = profile.replace("/", "_").removesuffix(".env")
        return f".dual_stop.{safe}"
    if env_path is not None:
        ep = env_path if env_path.is_absolute() else ROOT / env_path
        name = ep.name.removesuffix(".env")
        return f".dual_stop.{name}"
    return ".dual_stop"


def ensure_dual_stop_scope(
    *,
    profile: str | None = None,
    env_path: Path | None = None,
) -> Path:
    """Задаёт DUAL_STOP_FILE по --profile / --env, если ещё не задан снаружи."""
    raw = (os.getenv("DUAL_STOP_FILE") or "").strip()
    if not raw:
        name = dual_stop_basename(profile=profile, env_path=env_path)
        os.environ["DUAL_STOP_FILE"] = name
        raw = name
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


class DualShutdown(Exception):
    """Другой аккаунт завершил бронь — этот процесс тоже останавливается."""

    def __init__(self, info: dict) -> None:
        self.info = info
        src = info.get("source") or "?"
        super().__init__(f"стоп по сигналу от {src}")


def _flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() not in ("0", "false", "no", "")


def dual_sync_enabled() -> bool:
    """DUAL_STOP_ON_BANK=1 — при ссылке банка закрыть оба Chrome."""
    return _flag("DUAL_STOP_ON_BANK", "1")


def stop_file_path() -> Path:
    raw = (os.getenv("DUAL_STOP_FILE") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p
    return STOP_FILE


def clear_dual_stop() -> None:
    try:
        stop_file_path().unlink(missing_ok=True)
    except Exception:
        pass


def request_dual_stop(source: str, *, bank_url: str = "") -> None:
    if not dual_sync_enabled():
        return
    payload = {
        "ts": time.time(),
        "source": (source or "acc").strip(),
        "bank_url": (bank_url or "").strip(),
    }
    path = stop_file_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dual] сигнал стоп для пары acc1+acc2 ← {payload['source']} ({stop_file_path().name})")


def peek_dual_stop() -> dict | None:
    path = stop_file_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ts = float(data.get("ts") or 0)
    if ts and time.time() - ts > _STALE_SEC:
        clear_dual_stop()
        return None
    return data


def check_dual_stop(*, self_source: str = "") -> None:
    if not dual_sync_enabled():
        return
    info = peek_dual_stop()
    if not info:
        return
    src = (info.get("source") or "").strip()
    if src and self_source and src == self_source:
        return
    raise DualShutdown(info)


def wait_interruptible(page, seconds: float, *, self_source: str = "") -> bool:
    """
    Ждёт seconds. True = пришёл dual-stop (пора выходить).
    """
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if dual_sync_enabled():
            try:
                check_dual_stop(self_source=self_source)
            except DualShutdown:
                return True
        left = end - time.time()
        if left <= 0:
            break
        chunk = min(0.5, left)
        if page is not None:
            try:
                page.wait_for_timeout(int(chunk * 1000))
            except Exception:
                time.sleep(chunk)
        else:
            time.sleep(chunk)
    return False


def bank_browser_wait_sec() -> float:
    """Сколько держать Chrome после ссылки банка."""
    raw = (os.getenv("BOOK_BANK_BROWSER_WAIT_SEC") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 15.0 if dual_sync_enabled() else 240.0
