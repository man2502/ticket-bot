"""Журнал HTTP-запросов ботов в файлы: строка JSON на запрос, файл на день.

Зачем отдельно от консоли процесса. Консоль в панели — окно последних строк:
поиск идёт сутками, и через несколько часов всё, что было до блокировки
аккаунта, из неё уже стёрто. Разобрать «что мы слали и что нам отвечали
перед блоком» было не по чему. Здесь каждый запрос остаётся в файле:

    <BOT_HTTP_LOG_DIR>/http-2026-10-25.jsonl

    {"ts": "2026-10-25 10:59:30.522", "svc": "airlines", "acct": "Основной",
     "ip": "172.16.16.214", "m": "POST", "ep": "GetAirAvailability",
     "st": 450, "ms": 312, "body": "{\"success\": false, ...}"}

Тело пишется только у неуспешных ответов: у успешного поиска это сотни
килобайт рейсов, а для разбора важны именно отказы. Номера документов,
токены и почта маскируются — файл скачивают и пересылают.

Пишется только при заданном BOT_HTTP_LOG_DIR (панель задаёт его сама).
Запись не должна ронять бота: ошибка диска — молча пропускаем строку.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from core.http import redact

__all__ = ["LOG_DIR_ENV", "log_dir", "log_path", "mask", "write"]

LOG_DIR_ENV = "BOT_HTTP_LOG_DIR"

# Сколько тела ответа сохранять. Отказ сервера — это пара строк JSON;
# страница ошибки nginx целиком нужна редко, её начала хватает.
BODY_LIMIT = 2000

# Паспорта: «I-AH123456», «I-AH 123456», «ABC123456».
_DOC_NUMBER = re.compile(r"\b(?:[A-Z]-)?[A-Z]{1,3}[ -]?\d{6,9}\b")
_JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")


def log_dir() -> Path | None:
    raw = (os.environ.get(LOG_DIR_ENV) or "").strip()
    return Path(raw) if raw else None


def log_path(day: dt.date, folder: Path) -> Path:
    return folder / f"http-{day.isoformat()}.jsonl"


def mask(text: str) -> str:
    """Убрать из текста то, что нельзя хранить в файле: токены, документы, почту."""
    text = redact(text or "")
    text = _JWT.sub("<token>", text)
    text = _DOC_NUMBER.sub("<doc>", text)
    return _EMAIL.sub(r"***@\1", text)


def write(record: dict[str, Any], *, body: str = "") -> None:
    """Дописать строку о запросе. `body` маскируется и обрезается."""
    folder = log_dir()
    if folder is None:
        return
    now = dt.datetime.now()
    line = {"ts": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], **record}
    if body:
        line["body"] = mask(" ".join(body.split()))[:BODY_LIMIT]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # Одна запись одним write в режиме append: процессы с разных аккаунтов
        # пишут в общий файл дня, и строки не должны перемешиваться.
        with log_path(now.date(), folder).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass
