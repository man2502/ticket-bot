"""Журнал ошибок ботов: сводка по типам и аккаунтам и лента последних.

Строки сюда попадают из вывода процессов (runner._record_error_event). Здесь —
только чтение и уборка старого.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import case, func

from web.models import ErrorEvent, Provider

__all__ = ["ERROR_TITLES", "summary", "feed", "count", "prune", "KEEP_DAYS", "PAGE_SIZE"]

# Подписи — те же, что у бота (airlines_api/api_errors.py). Продублированы, а не
# импортированы: панель не должна зависеть от модуля бота ради словаря.
ERROR_TITLES = {
    "rate_limit": "лимит запросов (429)",
    "rate_limit_stop": "стоп после повторных 429",
    "auth_login": "вход не удался",
    "auth_forbidden": "доступ запрещён (401/403)",
    "error_search": "Error Search / hitit",
    "captcha_failed": "капча не принята",
    "maintenance": "профилактика на сайте",
    "network": "сеть / таймаут",
    "http_5xx": "ошибка сервера (5xx)",
    "booking_failed": "бронь не удалась",
    "booking_unknown": "бронь под вопросом",
    "other": "прочее",
}

# Сколько дней хранить. Сводка смотрит на неделю, месяц — с запасом на разбор.
KEEP_DAYS = 30

# Записей на странице ленты. За месяц их тысячи: раньше показывались только
# последние 200, а всё, что старше, со страницы было не достать.
PAGE_SIZE = 50


def _now() -> dt.datetime:
    return dt.datetime.now()


def summary(db) -> dict[str, Any]:
    """
    Сводка «тип ошибки × аккаунт»: сколько за 24 часа, за 7 дней и когда последняя.

    Аккаунт — колонка, а не строка: вопрос, ради которого всё затевалось, —
    «у какого аккаунта сегодня семь 429, а у какого ни одного».
    """
    now = _now()
    day_ago = now - dt.timedelta(days=1)
    week_ago = now - dt.timedelta(days=7)

    rows = (
        db.query(
            ErrorEvent.key,
            ErrorEvent.provider_id,
            func.count(ErrorEvent.id),
            func.sum(case((ErrorEvent.created_at >= day_ago, 1), else_=0)),
            func.max(ErrorEvent.created_at),
        )
        .filter(ErrorEvent.created_at >= week_ago)
        .group_by(ErrorEvent.key, ErrorEvent.provider_id)
        .all()
    )

    titles = {p.id: p.title for p in db.query(Provider).all()}
    column_ids: list[int | None] = []
    cells: dict[tuple[str, int | None], dict[str, Any]] = {}
    totals: dict[str, dict[str, Any]] = {}
    for key, provider_id, week, day, last in rows:
        if provider_id not in column_ids:
            column_ids.append(provider_id)
        cells[(key, provider_id)] = {"week": week, "day": int(day or 0), "last": last}
        total = totals.setdefault(key, {"week": 0, "day": 0, "last": None})
        total["week"] += week
        total["day"] += int(day or 0)
        if total["last"] is None or (last and last > total["last"]):
            total["last"] = last

    column_ids.sort(key=lambda pid: (pid is None, pid or 0))
    columns = [
        {"id": pid, "title": titles.get(pid, f"#{pid}") if pid else "без аккаунта"}
        for pid in column_ids
    ]
    keys = sorted(totals, key=lambda k: (-totals[k]["day"], -totals[k]["week"], k))
    table = [
        {
            "key": key,
            "title": ERROR_TITLES.get(key, key),
            "total": totals[key],
            "cells": [cells.get((key, column["id"])) for column in columns],
        }
        for key in keys
    ]
    return {"columns": columns, "rows": table}


def _filtered(db, provider_id: int | None, key: str):
    query = db.query(ErrorEvent)
    if provider_id is not None:
        query = query.filter(ErrorEvent.provider_id == provider_id)
    if key:
        query = query.filter(ErrorEvent.key == key)
    return query


def feed(
    db,
    *,
    provider_id: int | None = None,
    key: str = "",
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> list[ErrorEvent]:
    """Ошибки по фильтру, новые первыми; `offset` — для постраничного вывода."""
    return (
        _filtered(db, provider_id, key)
        .order_by(ErrorEvent.id.desc())
        .offset(max(0, offset))
        .limit(limit)
        .all()
    )


def count(db, *, provider_id: int | None = None, key: str = "") -> int:
    """Сколько всего ошибок по тому же фильтру — для номеров страниц."""
    return _filtered(db, provider_id, key).count()


def prune(db, *, keep_days: int = KEEP_DAYS) -> int:
    """Убрать ошибки старше `keep_days`. Зовётся при старте панели."""
    border = _now() - dt.timedelta(days=keep_days)
    removed = (
        db.query(ErrorEvent)
        .filter(ErrorEvent.created_at < border)
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed
