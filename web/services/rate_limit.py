"""Состояние лестницы 429 для панели: показать, запретить запуск, снять стоп.

Файл свой у каждого исходящего адреса: сайт режет запросы по адресу, поэтому
429 на одном IP ставит на паузу только процессы с этого IP, а второй адрес
продолжает работать. Аккаунты на одном адресе (в том числе «по умолчанию»,
если система уходит с того же IP) делят один файл. Пишут его боты
(core.rate_limit), панель только читает и по решению человека снимает стоп.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import web.config as config
from core.netaddr import ip_slug
from core.rate_limit import LADDER_SEC, STOP_AFTER, clear_stop, read_state

__all__ = ["state_path", "status_view", "start_blocker", "clear", "stop_notice_key"]


def state_path(ip: str = ""):
    """Файл лестницы адреса. Пусто — адрес неизвестен (нет сети), общий запасной."""
    # В момент вызова, а не при импорте: тесты подменяют DATA_DIR.
    return config.DATA_DIR / f"rate_limit_airlines_{ip_slug(ip)}.json"


def _clock(epoch: float) -> str:
    if not epoch:
        return ""
    moment = dt.datetime.fromtimestamp(epoch)
    if moment.date() == dt.date.today():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%d.%m %H:%M")


def status_view(ip: str = "") -> dict[str, Any]:
    """Всё о текущей паузе адреса для страницы настроек."""
    now = time.time()
    state = read_state(state_path(ip))
    level = state.effective_level(now)
    pause_left = max(0.0, state.until - now)
    stop_left = max(0.0, state.stopped_until - now)

    if stop_left:
        headline, tone = f"стоп до {_clock(state.stopped_until)}", "tag-error"
    elif pause_left:
        headline, tone = f"пауза до {_clock(state.until)}", "tag-error"
    elif level:
        headline, tone = f"работает, ступень {level}", ""
    else:
        headline, tone = "работает, 429 не было", "tag-ok"

    next_step = (
        "стоп процессов с адреса"
        if level + 1 >= STOP_AFTER
        else f"{LADDER_SEC[level] // 60} мин"
    )
    return {
        "ip": ip,
        "headline": headline,
        "tone": tone,
        "level": level,
        "stop_after": STOP_AFTER,
        "ladder": [s // 60 for s in LADDER_SEC],
        "next_step": next_step,
        "pause_left_min": round(pause_left / 60, 1),
        "stopped": stop_left > 0,
        "last_429_at": _clock(state.last_429_at),
        "last_retry_after": state.last_retry_after,
        "last_source": state.last_source,
        "last_endpoint": state.last_endpoint,
        "events": state.events,
    }


def start_blocker(ip: str = "") -> str:
    """Почему с этого адреса сейчас запускать нельзя. Пустая строка — можно."""
    state = read_state(state_path(ip))
    left = state.stopped_until - time.time()
    if left > 0:
        return (
            f"адрес {ip or 'по умолчанию'} остановлен после повторных 429 до "
            f"{_clock(state.stopped_until)}. Перезапуск сейчас — прямой путь к "
            f"блокировке аккаунта; снять стоп вручную можно в «Настройки → Самолёты»"
        )
    return ""


def clear(ip: str = "") -> str:
    clear_stop(state_path(ip))
    return (
        f"Адрес {ip or 'по умолчанию'}: стоп и пауза сняты. Ступень оставлена: "
        "следующий 429 сразу даст длинную паузу."
    )


def stop_notice_key(ip: str = "") -> str:
    """Ключ текущего стопа — чтобы уведомление о нём ушло один раз, а не от каждого процесса."""
    state = read_state(state_path(ip))
    return f"{ip_slug(ip)}:{state.stopped_until:.0f}" if state.stopped_until else ""
