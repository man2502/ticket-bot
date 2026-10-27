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

__all__ = [
    "BookingGuard",
    "AttemptRecord",
    "AlreadySettled",
    "passenger_tag",
    "passenger_tags",
    "split_tags",
    "route_key",
    "PENDING",
    "DONE",
    "UNKNOWN",
    "FAILED",
]


class AlreadySettled(RuntimeError):
    """Попытка перезаписать закрытую запись журнала. Бронь могла бы удвоиться."""


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
    # Что именно забронировано: рейс и время, поезд и вагон. Заполняет бот —
    # только он знает подробности, а панель по журналу шлёт уведомление
    # и показывает бронь. Старые журналы без этого поля читаются как прежде:
    # `get()` подставляет пустую строку.
    trip: str = ""
    # На кого бронь: метки пассажиров через запятую (см. passenger_tag).
    # Нужны панели, чтобы не пустить второй аккаунт бронировать тех же людей.
    # Пусто — запись старая, и на кого она, неизвестно.
    passengers: str = ""
    # Маршрут и дата: «CRZ-ASB 30.10.2026» (см. route_key). По нему панель
    # не даёт повторно бронировать тех же людей на тот же рейс: четыре брони
    # одного пассажира за двое суток без оплаты — путь к блоку аккаунта.
    route: str = ""


def route_key(source: object, destination: object, date: object) -> str:
    """«crz», «ASB», « 30.10.2026» → «CRZ-ASB 30.10.2026». Один вид у бота и у панели."""
    src = str(source or "").strip().upper()
    dst = str(destination or "").strip().upper()
    return f"{src}-{dst} {str(date or '').strip()}"


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


def passenger_tag(doc_number: str) -> str:
    """
    Метка пассажира для журнала: короткий хеш номера документа.

    Почему не id. Номера пассажиров в панели и в passengers.json для запуска
    из командной строки — разные пространства: один и тот же человек там под
    разными номерами. А документ у человека один, и `I-LB 897989` с `ILB897989`
    — тоже он: разделители и регистр отбрасываются.

    Почему не сам номер. Журнал лежит на диске открытым текстом, а паспортные
    данные там не нужны — нужно только понять, тот же это человек или нет.
    """
    normalized = "".join(ch for ch in str(doc_number or "") if ch.isalnum()).upper()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def passenger_tags(passengers: Iterable[dict[str, Any]]) -> str:
    """Метки всех пассажиров брони через запятую, без пустых и повторов."""
    tags: list[str] = []
    for pax in passengers:
        tag = passenger_tag(pax.get("doc_number", ""))
        if tag and tag not in tags:
            tags.append(tag)
    return ",".join(tags)


def split_tags(raw: str) -> set[str]:
    return {tag for tag in (raw or "").split(",") if tag}


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

    def begin(self, key: str, *, passengers: str = "", route: str = "") -> str:
        """
        Зарегистрировать попытку ДО отправки запроса и вернуть idempotency-key.

        Запись на диск здесь обязательна: если процесс умрёт сразу после POST,
        следующий запуск увидит `pending` и не станет бронировать вслепую.

        Закрытую запись не перезаписываем. Раньше `begin` затирал что угодно,
        включая `done`: одна успешная бронь превратилась так в «failed» —
        и номер с ссылкой на оплату исчезли из журнала, хотя бронь у
        перевозчика осталась. Проверка выше по коду это ловит, но полагаться
        на одну проверку в операции, которая стоит второго счёта, нельзя.
        """
        existing = self.get(key)
        if existing is not None and existing.status in (PENDING, DONE, UNKNOWN):
            raise AlreadySettled(
                f"по ключу {key} уже есть попытка со статусом {existing.status}"
                + (f" (бронь {existing.booking_number})" if existing.booking_number else "")
            )
        idem = existing.idempotency_key if existing else str(uuid.uuid4())
        record = AttemptRecord(
            booking_key=key,
            idempotency_key=idem,
            status=PENDING,
            started_at=_now(),
            # Пишем на попытке, а не на результате: самые опасные записи —
            # pending и unknown, и как раз по ним нужно знать, на кого бронь.
            passengers=passengers,
            route=route,
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
        trip: str = "",
    ) -> None:
        """Закрыть попытку достоверным результатом: создана или точно отказано."""
        self._update(
            key,
            status=DONE if ok else FAILED,
            detail=detail,
            booking_number=booking_number,
            form_url=form_url,
            trip=trip,
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
        """
        Снять одну блокировку — только по явному решению человека.

        Как и `clear_all`, запись не удаляет, а уводит в архив: там может быть
        настоящая бронь с номером и ссылкой на оплату.
        """
        self._archive(key)
        self._data["attempts"].pop(key, None)
        self._save()

    def clear_all(self) -> None:
        """
        Снять все блокировки — «бронируй заново, я разобрался».

        Записи не удаляются, а уезжают в архив. Среди них может быть настоящая
        бронь с номером и ссылкой на оплату: `--force-rebook` стирал её вместе
        с блокировкой, и единственный след живой брони пропадал.
        """
        for key in list(self._data.get("attempts", {})):
            self._archive(key)
        self._data["attempts"] = {}
        self._save()

    def _archive(self, key: str) -> None:
        """Перенести запись в архив, сохранив момент снятия блокировки."""
        record = self._data.get("attempts", {}).get(key)
        if not record:
            return
        archive = self._data.setdefault("archive", {})
        archive[f"{key}:{_now()}"] = {**record, "archived_at": _now()}

    def archived(self) -> Iterable[AttemptRecord]:
        """Снятые с блокировки попытки. Нужны, чтобы не потерять их из виду."""
        for raw in self._data.get("archive", {}).values():
            fields = {k: v for k, v in raw.items() if k != "archived_at"}
            try:
                yield AttemptRecord(**fields)
            except TypeError:
                continue
