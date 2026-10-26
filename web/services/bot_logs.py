"""Файловые журналы ботов: запросы, ошибки и сводка кодов ответа.

Лежат в `<данные панели>/logs/`, файл на день:

    http-2026-10-25.jsonl     каждый HTTP-запрос ботов (пишут сами боты,
                              см. core.http_journal): аккаунт, IP, запрос,
                              код ответа, время, тело отказа;
    errors-2026-10-25.log     каждая строка `[err]` с процессом и аккаунтом
                              (пишет панель, когда разбирает вывод).

Зачем файлы, если есть база. Консоль процесса — окно последних 5000 строк,
и за сутки поиска всё, что было до блокировки аккаунта, из неё уходит.
Файлы живут два месяца, их можно скачать и разобрать целиком.

Сводка кодов ответа строится по файлам запросов: в базе её не из чего
собрать — ошибки там есть, а нормальные ответы и 402 (капча) нет.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import web.config as config

__all__ = [
    "KEEP_DAYS",
    "logs_dir",
    "append_error",
    "list_files",
    "file_for_download",
    "prune",
    "http_summary",
    "STATUS_COLUMNS",
]

# Два месяца: журнал ошибок в базе — месяц, файлы — с запасом для разбора.
KEEP_DAYS = 60

_NAME_RE = re.compile(r"^(http|errors)-(\d{4}-\d{2}-\d{2})\.(jsonl|log)$")


def logs_dir() -> Path:
    # config.DATA_DIR читаем в момент вызова: тесты подменяют его после импорта.
    return config.DATA_DIR / "logs"


def append_error(
    *,
    job_id: int,
    service: str,
    account: str,
    ip: str,
    key: str,
    status: int | None,
    endpoint: str,
    detail: str,
) -> None:
    """Дописать ошибку в файл дня. Сбой диска не должен мешать чтению вывода бота."""
    now = dt.datetime.now()
    line = " | ".join(
        [
            now.strftime("%Y-%m-%d %H:%M:%S"),
            f"процесс #{job_id}",
            service or "—",
            account or "—",
            ip or "—",
            key,
            f"HTTP {status}" if status else "—",
            endpoint or "—",
            " ".join(detail.split()),
        ]
    )
    folder = logs_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"errors-{now.date().isoformat()}.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"[logs] не записал ошибку в файл: {exc}")


def list_files() -> list[dict[str, Any]]:
    """Файлы журналов, новые первыми: для списка «скачать» на странице ошибок."""
    folder = logs_dir()
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in folder.iterdir():
        match = _NAME_RE.match(path.name)
        if not match or not path.is_file():
            continue
        out.append(
            {
                "name": path.name,
                "kind": "запросы" if match.group(1) == "http" else "ошибки",
                "day": match.group(2),
                "size_kb": max(1, round(path.stat().st_size / 1024)),
            }
        )
    out.sort(key=lambda item: (item["day"], item["name"]), reverse=True)
    return out


def file_for_download(name: str) -> Path | None:
    """Путь к файлу журнала по имени. Только имена из нашего формата — никаких `../`."""
    if not _NAME_RE.match(name or ""):
        return None
    path = logs_dir() / name
    return path if path.is_file() else None


def prune(*, keep_days: int = KEEP_DAYS) -> int:
    """Удалить файлы старше `keep_days`. Зовётся при старте панели и запуске процессов."""
    folder = logs_dir()
    if not folder.is_dir():
        return 0
    border = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    removed = 0
    for path in folder.iterdir():
        match = _NAME_RE.match(path.name)
        if match and match.group(2) < border:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------- #
# Сводка кодов ответа
# --------------------------------------------------------------------------- #

# Колонки сводки: что означает код для нас, а не его номер. 402 — это капча,
# нормальная часть цикла; 450 — блок аккаунта; 0 — ответа не было вовсе.
STATUS_COLUMNS = [
    ("ok", "2xx", ""),
    ("captcha", "402", "нужна капча — норма"),
    ("auth", "401/403", "вход"),
    ("rate", "429", "лимит"),
    ("blocked", "450", "блок аккаунта"),
    ("client", "другие 4xx", ""),
    ("server", "5xx", ""),
    ("network", "нет ответа", "таймаут, обрыв"),
]

_ALERT_COLUMNS = {"rate", "blocked", "server", "network", "client"}


def _bucket(status: int) -> str:
    if status == 0:
        return "network"
    if 200 <= status < 300:
        return "ok"
    if status == 402:
        return "captcha"
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate"
    if status == 450:
        return "blocked"
    if 500 <= status < 600:
        return "server"
    return "client"


# Разобранные файлы: (размер, mtime) → строки. Файл дня дописывается, и
# перечитывать его целиком на каждый показ страницы незачем, если он не менялся.
_cache: dict[Path, tuple[tuple[int, float], list[tuple[str, str, str, str, int]]]] = {}


def _read(path: Path) -> list[tuple[str, str, str, str, int]]:
    """(ts, сервис, аккаунт, IP, код) из файла запросов."""
    try:
        stat = path.stat()
    except OSError:
        return []
    stamp = (stat.st_size, stat.st_mtime)
    cached = _cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    rows: list[tuple[str, str, str, str, int]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                    rows.append(
                        (
                            str(item["ts"]),
                            str(item.get("svc") or ""),
                            str(item.get("acct") or ""),
                            str(item.get("ip") or ""),
                            int(item.get("st") or 0),
                        )
                    )
                except (ValueError, KeyError, TypeError):
                    continue  # оборванная строка при записи — не повод падать
    except OSError:
        return []
    _cache[path] = (stamp, rows)
    return rows


def http_summary(*, hours: int = 24, now: dt.datetime | None = None) -> dict[str, Any]:
    """
    Коды ответа по аккаунтам за последние `hours` часов.

    Строка — аккаунт (с адресом), колонки — группы кодов. Отдельно отмечаем
    тревожные клетки: 429, 450, 5xx и обрывы — то, что видно перед блоком.
    """
    now = now or dt.datetime.now()
    since = (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    folder = logs_dir()
    days = {(now - dt.timedelta(days=offset)).date() for offset in range(hours // 24 + 2)}

    counts: dict[tuple[str, str, str], Counter] = {}
    for day in sorted(days):
        for ts, svc, acct, ip, status in _read(folder / f"http-{day.isoformat()}.jsonl"):
            if ts < since:
                continue
            counts.setdefault((svc, acct, ip), Counter())[_bucket(status)] += 1

    rows = []
    for (svc, acct, ip), counter in sorted(counts.items()):
        total = sum(counter.values())
        rows.append(
            {
                "service": svc,
                "account": acct or ("поезда" if svc == "railway" else "—"),
                "ip": ip,
                "total": total,
                "cells": [
                    {
                        "count": counter.get(key, 0),
                        "hot": key in _ALERT_COLUMNS and counter.get(key, 0) > 0,
                    }
                    for key, _title, _hint in STATUS_COLUMNS
                ],
            }
        )
    return {
        "hours": hours,
        "columns": [{"title": title, "hint": hint} for _key, title, hint in STATUS_COLUMNS],
        "rows": rows,
    }
