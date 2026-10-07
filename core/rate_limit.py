"""Общая лестница пауз после HTTP 429 — одна на все процессы с одного адреса.

Почему так строго. Аккаунты блокировали после серии 429, и разбор показал,
что бот вёл себя ровно так, как выглядит робот, игнорирующий сигнал сайта:

* транспорт сам повторял 429 ещё дважды за пару секунд;
* 429 на первом запросе цикла (`customers/info`) не включал паузу вовсе;
* пауза жила в памяти и сбрасывалась перезапуском;
* `Retry-After` от сервера печатался, но не соблюдался;
* остальные аккаунты с того же адреса продолжали стучаться как ни в чём не бывало.

Как теперь. Состояние лежит в одном JSON-файле, который читают все процессы
самолётов. Любой 429 поднимает ОБЩУЮ ступень, и все процессы ждут до общего
момента `until`:

    1-й 429  →  2 мин
    2-й      →  5 мин
    3-й      →  15 мин
    4-й      →  30 мин
    5-й      →  стоп всех процессов на час, уведомление человеку

Если сервер прислал `Retry-After` больше ступени — ждём сколько он сказал.
Ступень опускается на одну за каждый час без 429, а не сбрасывается разом.

Несколько 429 подряд в пределах одной паузы считаются одним событием: процессы
с одного адреса ловят 429 почти одновременно, и считать это четырьмя ступенями
значило бы остановить всё на первой же волне.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.state import load_json, save_json

__all__ = [
    "LADDER_SEC",
    "STOP_AFTER",
    "STOP_COOLDOWN_SEC",
    "RateLimitDecision",
    "RateLimitState",
    "rate_limit_state_path",
    "register_429",
    "read_state",
    "active_pause",
    "is_stopped",
    "clear_stop",
]

# Ступени: 2, 5, 15, 30 минут.
LADDER_SEC = (120, 300, 900, 1800)
# На какой по счёту ступени процессы останавливаются совсем.
STOP_AFTER = len(LADDER_SEC) + 1
# Сколько стоим после стопа, прежде чем панель разрешит запуск.
STOP_COOLDOWN_SEC = 3600
# Чистый час — ступень вниз.
STEP_DOWN_AFTER_SEC = 3600
# 429, пришедший в первые секунды после предыдущего, — та же волна.
SAME_WAVE_SEC = 30
# Разброс паузы: процессы не должны просыпаться в одну секунду.
JITTER = 0.10


def rate_limit_state_path(default: Path) -> Path:
    """Файл состояния: из API_RATE_LIMIT_STATE (так его задаёт панель) или default."""
    own = (os.getenv("API_RATE_LIMIT_STATE") or "").strip()
    return Path(own) if own else default


@dataclass
class RateLimitState:
    level: int = 0
    last_429_at: float = 0.0
    until: float = 0.0
    stopped_until: float = 0.0
    last_retry_after: float | None = None
    last_source: str = ""
    last_endpoint: str = ""
    events: int = 0

    @classmethod
    def from_dict(cls, raw: Any) -> "RateLimitState":
        if not isinstance(raw, dict):
            return cls()
        state = cls()
        for field in cls.__dataclass_fields__:
            if field in raw:
                setattr(state, field, raw[field])
        return state

    def as_dict(self) -> dict[str, Any]:
        data = {field: getattr(self, field) for field in self.__dataclass_fields__}
        # Человеку в файле удобнее читать время, а не эпоху.
        for key in ("last_429_at", "until", "stopped_until"):
            value = getattr(self, key)
            data[f"{key}_human"] = _human(value) if value else ""
        return data

    def effective_level(self, now: float) -> int:
        """Ступень с учётом спуска: минус одна за каждый чистый час."""
        if self.level <= 0 or not self.last_429_at:
            return 0
        clean = max(0.0, now - max(self.last_429_at, self.until))
        return max(0, self.level - int(clean // STEP_DOWN_AFTER_SEC))


@dataclass
class RateLimitDecision:
    """Что делать после 429."""

    delay: float
    level: int
    stop: bool
    retry_after: float | None
    same_wave: bool

    def describe(self) -> str:
        if self.stop:
            return (
                f"ступень {self.level} — стоп всех процессов на "
                f"{STOP_COOLDOWN_SEC // 60} мин"
            )
        ladder = LADDER_SEC[self.level - 1]
        parts = [f"ступень {self.level} ({ladder // 60} мин)"]
        if self.retry_after is not None:
            parts.append(f"Retry-After={self.retry_after:.0f}с")
        parts.append(f"ждём {self.delay / 60:.1f} мин")
        if self.same_wave:
            parts.append("та же волна — ступень не поднята")
        return ", ".join(parts)


def _human(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def read_state(path: Path) -> RateLimitState:
    return RateLimitState.from_dict(load_json(path, {}))


def _write(path: Path, state: RateLimitState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, state.as_dict())


def register_429(
    path: Path,
    *,
    retry_after: float | None,
    source: str = "",
    endpoint: str = "",
    now: float | None = None,
) -> RateLimitDecision:
    """
    Учесть 429 и решить, сколько ждать. Состояние сразу пишется на диск,
    чтобы остальные процессы увидели паузу на своём следующем шаге.
    """
    now = time.time() if now is None else now
    state = read_state(path)

    same_wave = bool(state.last_429_at) and (
        now < state.until and now - state.last_429_at < SAME_WAVE_SEC
    )
    if same_wave:
        level = max(1, state.level)
    else:
        level = state.effective_level(now) + 1

    stop = level >= STOP_AFTER
    if stop:
        delay = float(STOP_COOLDOWN_SEC)
        state.stopped_until = now + delay
    else:
        delay = float(LADDER_SEC[level - 1])
        if retry_after is not None and retry_after > delay:
            delay = float(retry_after)
        delay *= 1 + random.uniform(-JITTER, JITTER)
        if same_wave:
            # Волна уже идёт — не продлеваем её чужим 429, разве что сервер
            # прямо попросил ждать дольше.
            delay = max(state.until - now, delay if retry_after else 0.0)

    state.level = min(level, STOP_AFTER)
    state.last_429_at = now
    state.until = max(state.until, now + delay)
    state.last_retry_after = retry_after
    state.last_source = source
    state.last_endpoint = endpoint
    state.events += 1
    _write(path, state)

    return RateLimitDecision(
        delay=max(0.0, state.until - now),
        level=state.level,
        stop=stop,
        retry_after=retry_after,
        same_wave=same_wave,
    )


def active_pause(path: Path, *, now: float | None = None) -> float:
    """Сколько секунд ещё действует общая пауза. 0 — можно работать."""
    now = time.time() if now is None else now
    state = read_state(path)
    return max(0.0, state.until - now)


def is_stopped(path: Path, *, now: float | None = None) -> float:
    """Сколько секунд ещё действует стоп после пятого 429. 0 — стопа нет."""
    now = time.time() if now is None else now
    return max(0.0, read_state(path).stopped_until - now)


def clear_stop(path: Path) -> None:
    """
    Снять стоп и паузу вручную — только по решению человека.

    Ступень не сбрасываем: следующий 429 должен сразу дать длинную паузу,
    а не начать лестницу с двух минут.
    """
    state = read_state(path)
    state.stopped_until = 0.0
    state.until = 0.0
    if state.level >= STOP_AFTER:
        state.level = len(LADDER_SEC)
    _write(path, state)
