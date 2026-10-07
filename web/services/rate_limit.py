"""Состояние лестницы 429 для панели: показать, запретить запуск, снять стоп.

Файл один на все процессы самолётов — они ходят с одного адреса, и 429 у одного
аккаунта означает паузу для всех. Пишут его боты (core.rate_limit), панель
только читает и по решению человека снимает стоп.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import web.config as config
from core.rate_limit import LADDER_SEC, STOP_AFTER, clear_stop, read_state

__all__ = ["state_path", "status_view", "start_blocker", "clear", "stop_notice_key"]


def state_path():
    # В момент вызова, а не при импорте: тесты подменяют DATA_DIR.
    return config.DATA_DIR / "rate_limit_airlines.json"


def _clock(epoch: float) -> str:
    if not epoch:
        return ""
    moment = dt.datetime.fromtimestamp(epoch)
    if moment.date() == dt.date.today():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%d.%m %H:%M")


def status_view() -> dict[str, Any]:
    """Всё о текущей паузе для страницы настроек."""
    now = time.time()
    state = read_state(state_path())
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
        "стоп всех процессов"
        if level + 1 >= STOP_AFTER
        else f"{LADDER_SEC[level] // 60} мин"
    )
    return {
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


def start_blocker() -> str:
    """Почему процесс самолётов сейчас запускать нельзя. Пустая строка — можно."""
    state = read_state(state_path())
    left = state.stopped_until - time.time()
    if left > 0:
        return (
            f"Самолёты остановлены после повторных 429 до {_clock(state.stopped_until)}. "
            f"Перезапуск сейчас — прямой путь к блокировке аккаунта. Снять стоп "
            f"вручную можно в «Настройки → Самолёты»."
        )
    return ""


def clear() -> str:
    clear_stop(state_path())
    return (
        "Стоп и пауза сняты. Ступень оставлена: следующий 429 сразу даст "
        "длинную паузу."
    )


def stop_notice_key() -> str:
    """Ключ текущего стопа — чтобы уведомление о нём ушло один раз, а не от каждого процесса."""
    state = read_state(state_path())
    return f"{state.stopped_until:.0f}" if state.stopped_until else ""
