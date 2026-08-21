"""Брони: чтение журнала бота, показ ссылки на оплату и снятие блокировки.

Источник правды — журнал самого бота (`railway_bookings.json`,
`api_bookings.json`). Именно он защищает от повторной брони и переживает
перезапуск процесса, поэтому дублировать его логику в панели было бы ошибкой:
две «правды» рано или поздно разойдутся, и разойдутся они на деньгах.

Панель журнал только читает и складывает в таблицу `bookings` — чтобы показать
человеку ссылку на оплату и историю. Единственное изменяющее действие —
снятие блокировки, и делается оно через тот же `BookingGuard`, а не правкой
файла руками.

Статусы попытки (из `core.booking_guard`):

    done     бронь точно создана
    unknown  ответ потерян — бронь МОГЛА быть создана, нужен человек
    failed   сервер достоверно отказал, брони нет
    pending  запрос ушёл, процесс не дожил до ответа
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.booking_guard import DONE, FAILED, PENDING, UNKNOWN, BookingGuard  # noqa: E402

from web.models import Booking, Job, Service  # noqa: E402
from web.services.bot_env import SERVICE_JOURNALS, SERVICE_DIRS  # noqa: E402

__all__ = [
    "journal_path",
    "sync_from_journal",
    "blocking_attempt",
    "unblock_service",
    "STATUS_TITLES",
    "BLOCKING_STATUSES",
]

# Статусы, при которых бот откажется бронировать снова.
BLOCKING_STATUSES = (PENDING, DONE, UNKNOWN)

STATUS_TITLES = {
    DONE: "бронь создана",
    UNKNOWN: "под вопросом",
    FAILED: "отказ сервера",
    PENDING: "не завершена",
}


def journal_path(service: str) -> Path:
    """Файл журнала броней конкретного бота."""
    return SERVICE_DIRS[service] / SERVICE_JOURNALS[service]


def _guard(service: str) -> BookingGuard:
    return BookingGuard(journal_path(service))


def _parse_time(raw: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def sync_from_journal(db, job: Job) -> list[Booking]:
    """
    Забрать из журнала бота попытки, относящиеся к этому запуску.

    Журнал общий для всех запусков сервиса, поэтому отбираем по времени: всё,
    что начато не раньше старта процесса. Так брони прошлых запусков не
    приписываются текущему.

    Возвращает только НОВЫЕ записи — по ним потом уходит уведомление, и слать
    его повторно при каждом обновлении страницы нельзя.
    """
    guard = _guard(job.service)
    started = job.started_at or dt.datetime.min

    known = {
        booking.guard_key
        for booking in db.query(Booking).filter(Booking.job_id == job.id).all()
    }

    created: list[Booking] = []
    for record in guard.records():
        if record.booking_key in known:
            continue

        began = _parse_time(record.started_at)
        if began is not None and began < started:
            # Попытка из прошлого запуска — не наша.
            continue

        booking = Booking(
            job_id=job.id,
            service=job.service,
            status=record.status,
            guard_key=record.booking_key,
            booking_number=record.booking_number,
            payment_url=record.form_url,
            detail=record.detail,
        )
        db.add(booking)
        created.append(booking)

    if created:
        db.commit()
    return created


def blocking_attempt(service: str):
    """
    Что мешает запустить новый поиск по этому сервису.

    Возвращает запись журнала или None. Пока она есть, бот при запуске
    остановится сам — панель показывает это заранее, чтобы человек не гадал,
    почему процесс завершился сразу.
    """
    path = journal_path(service)
    if not path.exists():
        return None
    return _guard(service).blocking_attempt()


def unblock_service(db, service: str, *, guard_key: str = "") -> str:
    """
    Снять блокировку журнала. Только по явному решению человека.

    Это ровно то, что делает флаг `--force-rebook` у ботов. Опасность в том,
    что блокировка обычно означает «бронь могла быть создана»: снять её,
    не проверив брони у перевозчика, значит рискнуть вторым счётом.

    Записи в панели помечаем снятыми, но не удаляем — история нужна.
    """
    path = journal_path(service)
    if not path.exists():
        return "Журнал броней пуст — блокировки нет"

    guard = _guard(service)

    if guard_key:
        record = guard.get(guard_key)
        if record is None:
            return "Такой записи в журнале нет"
        guard.clear(guard_key)
        removed = 1
    else:
        removed = sum(1 for _ in guard.records())
        guard.clear_all()

    for booking in (
        db.query(Booking)
        .filter(Booking.service == service, Booking.status.in_(BLOCKING_STATUSES))
        .all()
    ):
        if guard_key and booking.guard_key != guard_key:
            continue
        booking.detail = (booking.detail + " · блокировка снята вручную").strip(" ·")
    db.commit()

    return f"Блокировка снята, записей в журнале очищено: {removed}"


def service_titles() -> dict[str, str]:
    return {
        Service.AIRLINES.value: "Самолёты",
        Service.RAILWAY.value: "Поезда",
    }
