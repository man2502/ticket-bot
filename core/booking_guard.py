"""Защита от повторной брони.

Проблема, которую решает модуль. Бронь — единственная неидемпотентная операция
во всём проекте, и по ней невозможно «просто повторить запрос»:

  * если ответ не распарсился или пришёл таймаут, бронь на сервере МОГЛА быть
    создана — повтор даёт вторую настоящую бронь на тех же пассажиров;
  * после перезапуска процесса память о попытке терялась, и монитор бронировал
    заново;
  * цикл по рейсам после неудачи шёл к следующему рейсу, не разбирая, была
    неудача достоверной («сервер отказал») или неопределённой.

Журнал попыток пишется на диск ДО отправки запроса, атомарно, отдельно от
общего state — чтобы его нельзя было затереть обычным сохранением состояния.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from core.state import load_json, save_json

__all__ = ["BookingGuard", "AttemptRecord", "PENDING", "DONE", "UNKNOWN", "FAILED"]

PENDING = "pending"  # запрос отправлен, ответа ещё нет
DONE = "done"  # бронь точно создана
UNKNOWN = "unknown"  # ответ потерян — бронь могла быть создана
FAILED = "failed"  # сервер достоверно отказал, брони нет


@dataclass
class AttemptRecord:
    booking_key: str
    idempotency_key: str
    status: str
    started_at: str
    finished_at: str = ""
    detail: str = ""
    booking_number: str = ""
    form_url: str = ""


def booking_key(*parts: Any) -> str:
    """
    Детерминированный ключ брони.

    Одинаковые (рейс, места, пассажиры) дают одинаковый ключ — значит повторную
    попытку по тем же данным можно опознать и после перезапуска процесса.
    """
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class BookingGuard:
    """
    Журнал попыток брони.

    Использование:

        guard = BookingGuard(path)
        blocker = guard.blocking_attempt()
        if blocker:                      # незакрытая попытка с прошлого запуска
            ...остановиться...

        key = booking_key(trip_id, journey_id, seat_ids, passenger_docs)
        if guard.is_settled(key):        # уже бронировали именно это
            ...пропустить...

        idem = guard.begin(key)          # пишет pending НА ДИСК до POST
        try:
            resp = post(..., headers={"idempotency-key": idem})
        except ResultUnknown as exc:
            guard.mark_unknown(key, str(exc))
            raise
        guard.settle(key, ok=..., ...)
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = load_json(self.path, {})
        if not isinstance(self._data, dict):
            self._data = {}
        self._data.setdefault("attempts", {})

    # -- чтение ------------------------------------------------------------- #

    def _attempts(self) -> dict[str, dict[str, Any]]:
        attempts = self._data.get("attempts")
        return attempts if isinstance(attempts, dict) else {}

    def get(self, key: str) -> AttemptRecord | None:
        raw = self._attempts().get(key)
        if not isinstance(raw, dict):
            return None
        fields = {f: raw.get(f, "") for f in AttemptRecord.__dataclass_fields__}
        fields["booking_key"] = key
        return AttemptRecord(**fields)

    def is_settled(self, key: str) -> bool:
        """True, если по этому ключу бронировать больше нельзя."""
        record = self.get(key)
        return record is not None and record.status in (PENDING, DONE, UNKNOWN)

    def blocking_attempt(self) -> AttemptRecord | None:
        """
        Первая попытка, которая запрещает автоброню в этом запуске.

        Это `done` (уже забронировано), `unknown` (неизвестно, что на сервере)
        и `pending` (процесс умер между записью и ответом).
        """
        for key in self._attempts():
            record = self.get(key)
            if record and record.status in (PENDING, DONE, UNKNOWN):
                return record
        return None

    def records(self) -> Iterable[AttemptRecord]:
        for key in self._attempts():
            record = self.get(key)
            if record:
                yield record

    # -- запись ------------------------------------------------------------- #

    def _save(self) -> None:
        save_json(self.path, self._data)

    def begin(self, key: str) -> str:
        """
        Зарегистрировать попытку ДО отправки запроса и вернуть idempotency-key.

        Запись на диск здесь обязательна: если процесс умрёт сразу после POST,
        следующий запуск увидит `pending` и не станет бронировать вслепую.
        """
        existing = self.get(key)
        idem = existing.idempotency_key if existing else str(uuid.uuid4())
        record = AttemptRecord(
            booking_key=key,
            idempotency_key=idem,
            status=PENDING,
            started_at=_now(),
        )
        self._data["attempts"][key] = asdict(record)
        self._save()
        return idem

    def settle(
        self,
        key: str,
        *,
        ok: bool,
        detail: str = "",
        booking_number: str = "",
        form_url: str = "",
    ) -> None:
        """Закрыть попытку достоверным результатом: создана или точно отказано."""
        self._update(
            key,
            status=DONE if ok else FAILED,
            detail=detail,
            booking_number=booking_number,
            form_url=form_url,
        )

    def mark_unknown(self, key: str, detail: str) -> None:
        """Ответ потерян. Дальше — только руками."""
        self._update(key, status=UNKNOWN, detail=detail)

    def _update(self, key: str, **changes: str) -> None:
        record = self._data["attempts"].get(key)
        if not isinstance(record, dict):
            record = asdict(
                AttemptRecord(
                    booking_key=key,
                    idempotency_key=str(uuid.uuid4()),
                    status=PENDING,
                    started_at=_now(),
                )
            )
        record.update(changes)
        record["finished_at"] = _now()
        self._data["attempts"][key] = record
        self._save()

    def clear(self, key: str) -> None:
        """Снять запись — только по явному решению человека (`--force-rebook`)."""
        self._data["attempts"].pop(key, None)
        self._save()

    def clear_all(self) -> None:
        self._data["attempts"] = {}
        self._save()
