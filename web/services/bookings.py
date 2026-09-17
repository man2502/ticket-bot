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

from dataclasses import dataclass  # noqa: E402

from core.booking_guard import (  # noqa: E402
    DONE,
    FAILED,
    PENDING,
    UNKNOWN,
    BookingGuard,
    passenger_tag,
    split_tags,
)

from web.models import Booking, Job, JobStatus, Service  # noqa: E402
from web.services.bot_env import SERVICE_JOURNALS, SERVICE_DIRS  # noqa: E402

__all__ = [
    "Journal",
    "Blocker",
    "journals",
    "blockers",
    "job_start_blocker",
    "provider_blocker",
    "SERVICE_ROUTE_UNKNOWN",
    "SERVICE_NAMES",
    "booking_route",
    "booking_passengers",
    "booking_message_fields",
    "sync_orphan_bookings",
    "notify_orphan_bookings",
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


# --------------------------------------------------------------------------- #
# Журналы всех аккаунтов
# --------------------------------------------------------------------------- #


@dataclass
class Journal:
    """Журнал броней: общий (provider_id=None) или конкретного аккаунта."""

    path: Path
    provider_id: int | None = None

    def guard(self) -> BookingGuard:
        return BookingGuard(self.path)


@dataclass
class Blocker:
    """Незакрытая попытка брони и журналы, где она лежит."""

    record: object
    journals: list[Journal]

    @property
    def provider_ids(self) -> list[int]:
        return [j.provider_id for j in self.journals if j.provider_id is not None]


def journals(db, service: str) -> list[Journal]:
    """
    Все журналы сервиса. Сначала журналы аккаунтов, общий — последним.

    Порядок важен при подборе броней: запись, скопированная из общего журнала
    в журнал аккаунта при переезде, должна достаться аккаунту, а не повиснуть
    бесхозной.
    """
    found: list[Journal] = []
    if service == Service.AIRLINES.value:
        from web.services.providers import list_providers, provider_journal_path

        for provider in list_providers(db, service):
            path = provider_journal_path(provider.id)
            if path.exists():
                found.append(Journal(path, provider.id))
    legacy = journal_path(service)
    if legacy.exists():
        found.append(Journal(legacy, None))
    return found


def job_journal(job: Job) -> Journal:
    """Журнал, в который пишет бот этого процесса."""
    if job.service == Service.AIRLINES.value and job.provider_id:
        from web.services.providers import provider_journal_path

        return Journal(provider_journal_path(job.provider_id), job.provider_id)
    return Journal(journal_path(job.service), None)


def blockers(db, service: str) -> list[Blocker]:
    """
    Незакрытые попытки брони по всем журналам сервиса.

    Одна и та же запись может лежать в двух журналах: при переезде на
    провайдеров общий журнал скопирован в журнал первого аккаунта. Человеку
    это одна бронь, и показывать её дважды — значит заставить снимать
    блокировку дважды.
    """
    merged: dict[tuple[str, str], Blocker] = {}
    for journal in journals(db, service):
        for record in journal.guard().records():
            if record.status not in BLOCKING_STATUSES:
                continue
            key = (record.booking_key, record.started_at)
            if key in merged:
                merged[key].journals.append(journal)
            else:
                merged[key] = Blocker(record=record, journals=[journal])
    return list(merged.values())


def _job_tags(job: Job) -> set[str]:
    return {
        tag
        for tag in (passenger_tag(link.passenger.doc_number) for link in job.passengers)
        if tag
    }


def provider_blocker(db, provider_id: int) -> Blocker | None:
    """
    Незакрытая попытка в журнале самого аккаунта.

    Бот при старте смотрит в свой журнал и остановится сам, если там что-то
    висит, — вне зависимости от пассажиров. Панель говорит об этом заранее.
    """
    for blocker in blockers(db, Service.AIRLINES.value):
        if provider_id in blocker.provider_ids:
            return blocker
    return None


def job_start_blocker(db, job: Job) -> str:
    """
    Почему этот процесс самолётов нельзя запускать. Пустая строка — можно.

    Аккаунтов несколько, и у каждого свой журнал — значит защита от повторной
    брони внутри журнала не видит соседей. Второй аккаунт спокойно забронировал
    бы тех же людей на тот же рейс: две брони, два счёта. Поэтому панель
    сверяет пассажиров со ВСЕМИ журналами и со всеми работающими процессами.

    Запись без меток пассажиров (сделана до их появления) блокирует всё: на кого
    она, неизвестно, а ошибиться здесь стоит денег.
    """
    tags = _job_tags(job)

    for blocker in blockers(db, Service.AIRLINES.value):
        record = blocker.record
        record_tags = split_tags(getattr(record, "passengers", ""))
        if record_tags and not (record_tags & tags):
            continue
        who = "тех же пассажиров" if record_tags else "неизвестно кого (старая запись)"
        return (
            f"Есть незакрытая попытка брони на {who}: {record.status}, "
            f"{record.started_at}"
            + (f", бронь {record.booking_number}" if record.booking_number else "")
            + ". Проверьте брони у перевозчика и снимите блокировку в разделе «Брони»."
        )

    from web.services.settings import get_service_config

    if get_service_config(db, Service.AIRLINES.value).allow_shared_passengers:
        # Человек разрешил искать одних и тех же людей с нескольких аккаунтов.
        # Проверка незакрытых попыток выше при этом остаётся: там бронь уже
        # есть или могла появиться, и это не параллельный поиск, а повтор.
        return ""

    others = (
        db.query(Job)
        .filter(
            Job.service == Service.AIRLINES.value,
            Job.status == JobStatus.RUNNING.value,
            Job.id != job.id,
        )
        .all()
    )
    for other in others:
        if _job_tags(other) & tags:
            return (
                f"Этих пассажиров уже ищет процесс #{other.id}. Два аккаунта на одних "
                f"и тех же людей — это две брони и два счёта."
            )
    return ""


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
    started = job.started_at or dt.datetime.min
    journal = job_journal(job)
    records = [r for r in journal.guard().records() if _belongs_to(r, started)]
    return _import_records(
        db, records, service=job.service, job_id=job.id, provider_id=journal.provider_id
    )


def _belongs_to(record, started: dt.datetime) -> bool:
    """Попытка начата не раньше старта процесса — значит, его."""
    began = _parse_time(record.started_at)
    return began is None or began >= started


def sync_orphan_bookings(db) -> list[Booking]:
    """
    Подобрать из журналов брони, которых панель не видела.

    Такое бывает, когда бота запускали из командной строки или когда процесс
    удалили раньше, чем панель успела прочитать журнал. Бронь при этом реальна:
    у перевозчика она есть, счёт выставлен, и ссылка на оплату лежит в журнале.
    Без этого обхода она пропадала совсем — человек узнавал о ней, только
    заглянув в JSON руками.

    Возвращает только НОВЫЕ записи: по ним уходит уведомление, и слать его
    заново при каждом открытии страницы нельзя.
    """
    created: list[Booking] = []

    for service in Service:
        for journal in journals(db, service.value):
            # Архив тоже смотрим: там лежат записи, с которых сняли блокировку
            # (`--force-rebook` или кнопка «разблокировать»). Настоящая бронь
            # с номером и ссылкой на оплату от этого никуда не делась.
            guard = journal.guard()
            for record in [*guard.records(), *guard.archived()]:
                # Процесс мог отработать и завершиться раньше, чем панель дошла
                # до журнала. Если попытка укладывается в его время, это его
                # бронь — тогда в письме будет настоящий маршрут.
                owner = _job_around(
                    db, service.value, record.started_at, provider_id=journal.provider_id
                )
                created.extend(
                    _import_records(
                        db,
                        [record],
                        service=service.value,
                        job_id=owner,
                        provider_id=journal.provider_id,
                    )
                )

    return created


def _job_around(
    db, service: str, started_at: str, *, provider_id: int | None = None
) -> int | None:
    """
    Процесс того же сервиса, во время которого шла попытка. None — не нашёлся.

    Если журнал аккаунта — ищем только среди процессов этого аккаунта: при
    нескольких параллельных процессах по времени подходят сразу несколько.
    """
    began = _parse_time(started_at)
    if began is None:
        return None

    query = db.query(Job).filter(Job.service == service, Job.started_at.isnot(None))
    if provider_id is not None:
        query = query.filter(Job.provider_id == provider_id)
    for job in query.order_by(Job.started_at.desc()).limit(20).all():
        finished = job.finished_at or dt.datetime.now()
        if job.started_at <= began <= finished:
            return job.id
    return None


def _import_records(
    db, records, *, service: str, job_id: int | None, provider_id: int | None = None
) -> list[Booking]:
    """
    Перенести записи журнала в таблицу броней. Вернуть те, о которых стоит сообщить.

    Сверяем не только ключ, но и номер брони. Ключ — это «пассажир + рейс +
    дата», а не конкретная бронь: после снятия блокировки по тому же ключу
    появляется НОВАЯ бронь с новым номером. Ровно так и потерялась 46MEMB —
    панель увидела знакомый ключ у записи с номером 46MBJ0 и промолчала.

    Незакрытую запись того же ключа дополняем на месте: `pending`, ставший
    `done`, — это та же попытка, а не вторая бронь.
    """
    rows: dict[str, list[Booking]] = {}
    for booking in db.query(Booking).filter(Booking.service == service).all():
        rows.setdefault(booking.guard_key, []).append(booking)

    touched: list[Booking] = []
    for record in records:
        same_key = rows.setdefault(record.booking_key, [])

        if any(
            row.status == record.status and row.booking_number == record.booking_number
            for row in same_key
        ):
            # Ровно это уже лежит в базе. Если бронь бесхозная, а процесс её
            # признал — присваиваем, но повторно не рассылаем.
            for row in same_key:
                if job_id is not None and row.job_id is None:
                    row.job_id = job_id
                if provider_id is not None and row.provider_id is None:
                    row.provider_id = provider_id
            continue

        stale = next((row for row in same_key if not row.booking_number), None)
        if stale is not None:
            # Та же попытка дозрела: pending → done. Дополняем, а не дублируем.
            had_url = bool(stale.payment_url)
            stale.status = record.status
            stale.booking_number = record.booking_number
            stale.payment_url = record.form_url
            stale.trip = record.trip
            stale.detail = record.detail
            if job_id is not None and stale.job_id is None:
                stale.job_id = job_id
            if provider_id is not None and stale.provider_id is None:
                stale.provider_id = provider_id
            # Сообщать есть о чём, только если ссылка появилась сейчас.
            if record.form_url and not had_url:
                touched.append(stale)
            continue

        booking = Booking(
            job_id=job_id,
            provider_id=provider_id,
            service=service,
            status=record.status,
            guard_key=record.booking_key,
            booking_number=record.booking_number,
            payment_url=record.form_url,
            trip=record.trip,
            detail=record.detail,
        )
        db.add(booking)
        same_key.append(booking)
        touched.append(booking)

    db.commit()
    return touched


def notify_orphan_bookings(db) -> list[Booking]:
    """
    Подобрать бесхозные брони и разослать по ним ссылку на оплату.

    Маршрут и пассажиров взять неоткуда — процесса нет. Пишем то, что есть
    в журнале: номер брони и ссылку. Этого достаточно, чтобы оплатить.
    """
    from web.services.notifications import format_booking_message, notify_booking

    created = sync_orphan_bookings(db)
    payable = [b for b in created if b.status == DONE and b.payment_url]

    for booking in payable:
        subject, body, sms = format_booking_message(
            **booking_message_fields(booking),
        )
        try:
            notify_booking(
                db,
                subject=subject,
                body=body,
                sms_text=sms,
                booking_id=booking.id,
            )
        except Exception as exc:  # noqa: BLE001 — бронь важнее рассылки
            print(f"[bookings] рассылка по бесхозной броне не удалась: {exc}")

    return created


# Маршрут неизвестен: процесса, из которого его можно было бы взять, нет.
SERVICE_ROUTE_UNKNOWN = {
    Service.AIRLINES.value: "Самолёты (запуск вне панели)",
    Service.RAILWAY.value: "Поезда (запуск вне панели)",
}


SERVICE_NAMES = {
    Service.AIRLINES.value: "Turkmenistan Airlines",
    Service.RAILWAY.value: "Türkmendemirýollary",
}


def booking_route(booking: Booking) -> str:
    """
    Маршрут для письма: из процесса, а если его нет — пустая строка.

    Пустая, а не «маршрут неизвестен»: процесс могли удалить или запустить
    бота из командной строки, и писать в теме письма извинение вместо данных
    незачем — перевозчик, рейс и номер брони на месте, этого достаточно.

    Дату сюда не добавляем: у неё отдельная строка, и дублировать её
    в заголовке значит сделать тему длиннее без пользы.
    """
    if booking.job is None:
        return ""
    return f"{booking.job.source} → {booking.job.destination}"


def booking_message_fields(booking: Booking) -> dict[str, str]:
    """Всё, что панель знает о броне, — одним словарём для `format_booking_message`."""
    job = booking.job
    return {
        "route": booking_route(booking),
        "payment_url": booking.payment_url,
        "booking_number": booking.booking_number,
        "passengers": booking_passengers(booking),
        "service": SERVICE_NAMES.get(booking.service, booking.service),
        "trip": booking.trip,
        "depart_date": job.depart_date if job is not None else "",
    }


def booking_passengers(booking: Booking) -> str:
    """Пассажиры для письма. Без процесса их взять неоткуда — пустая строка."""
    if booking.job is None:
        return ""
    return ", ".join(
        f"{link.passenger.last_name} {link.passenger.first_name}"
        for link in sorted(booking.job.passengers, key=lambda x: x.position)
    )


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
    found = journals(db, service)
    if not found:
        return "Журнал броней пуст — блокировки нет"

    # Запись может лежать в нескольких журналах (общий скопирован в журнал
    # первого аккаунта при переезде). Снимать нужно везде, иначе блокировка
    # вернётся из копии, и человек будет гадать почему.
    removed = 0
    if guard_key:
        for journal in found:
            guard = journal.guard()
            if guard.get(guard_key) is not None:
                guard.clear(guard_key)
                removed += 1
        if not removed:
            return "Такой записи в журнале нет"
    else:
        for journal in found:
            guard = journal.guard()
            removed += sum(1 for _ in guard.records())
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
