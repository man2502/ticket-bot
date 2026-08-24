"""Раздел «Процессы»: создание, запуск, остановка и живая консоль.

Права: свои процессы видит и создаёт любой вошедший, чужие — только админ.
Форма создания разная для самолётов и поездов: у первых рейс и время,
у вторых тип вагона и номер поезда. Общее — маршрут, дата и пассажиры.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from web.auth import require_user
from web.db import get_db, session_scope
from web.models import (
    Job,
    JobLog,
    JobPassenger,
    JobStatus,
    Passenger,
    Service,
    Station,
    User,
)
from web.services.passengers import validate_group
from web.services.runner import (
    JobStartError,
    active_job_id,
    delete_job,
    is_running,
    start_job,
    stop_job,
)
from web.services.settings import get_service_config, missing_credentials
from web.templating import render

router = APIRouter(prefix="/jobs", tags=["jobs"])

SERVICE_TITLES = {
    Service.AIRLINES.value: "Самолёт",
    Service.RAILWAY.value: "Поезд",
}

# Типы вагонов у railway. Значения — те, что понимает бот (WAGON_TYPE_ID).
WAGON_TYPES = (
    ("3,2", "Плацкарт, иначе купе"),
    ("3", "Только плацкарт"),
    ("2", "Только купе"),
    ("5", "VIP-купе"),
    ("2,5", "Купе, иначе VIP"),
    ("", "Любой"),
)


def _visible_jobs(db: Session, user: User):
    query = db.query(Job)
    if not user.is_admin:
        query = query.filter(Job.user_id == user.id)
    return query


def _job_or_none(db: Session, user: User, job_id: int) -> Job | None:
    job = db.get(Job, job_id)
    if job is None:
        return None
    # Чужой процесс обычному пользователю не показываем и не даём остановить.
    if not user.is_admin and job.user_id != user.id:
        return None
    return job


def _not_found(request: Request, user: User) -> HTMLResponse:
    return render(
        request, "not_found.html", user,
        status_code=404, title="Процесс не найден", back_url="/jobs",
    )


# --------------------------------------------------------------------------- #
# Список
# --------------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
def list_jobs(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    jobs = _visible_jobs(db, user).order_by(Job.created_at.desc()).limit(100).all()

    return render(
        request,
        "jobs.html",
        user,
        jobs=jobs,
        service_titles=SERVICE_TITLES,
        # Заняты ли очереди сервисов — чтобы кнопка «Запустить» не обманывала.
        busy={s.value: active_job_id(db, s.value) for s in Service},
    )


# --------------------------------------------------------------------------- #
# Создание
# --------------------------------------------------------------------------- #


def _new_job_page(
    request: Request,
    user: User,
    db: Session,
    *,
    service: str,
    form: dict | None = None,
    errors: list[str] | None = None,
) -> HTMLResponse:
    stations = (
        db.query(Station)
        .filter(Station.service == service, Station.is_active.is_(True))
        .order_by(Station.title_ru, Station.code)
        .all()
    )
    passengers = db.query(Passenger).order_by(Passenger.last_name, Passenger.first_name).all()
    config = get_service_config(db, service)

    return render(
        request,
        "job_form.html",
        user,
        status_code=422 if errors else 200,
        service=service,
        service_title=SERVICE_TITLES[service],
        stations=stations,
        passengers=passengers,
        wagon_types=WAGON_TYPES,
        form=form or {},
        errors=errors or [],
        missing=missing_credentials(db, service),
        auto_book=config.auto_book,
        service_enabled=config.is_enabled,
    )


@router.get("/new", response_class=HTMLResponse)
def new_job(
    request: Request,
    service: str = Service.RAILWAY.value,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if service not in SERVICE_TITLES:
        service = Service.RAILWAY.value
    return _new_job_page(request, user, db, service=service)


@router.post("/new")
async def create_job(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    raw = await request.form()
    service = str(raw.get("service", Service.RAILWAY.value))
    if service not in SERVICE_TITLES:
        return RedirectResponse("/jobs/new", status_code=status.HTTP_303_SEE_OTHER)

    form = {key: str(value) for key, value in raw.multi_items() if key != "passenger_ids"}
    passenger_ids = [int(v) for v in raw.getlist("passenger_ids") if str(v).isdigit()]
    form["passenger_ids"] = passenger_ids

    errors: list[str] = []
    source = str(raw.get("source", "")).strip()
    destination = str(raw.get("destination", "")).strip()
    depart_date = str(raw.get("depart_date", "")).strip()

    if not source or not destination:
        errors.append("Укажите откуда и куда")
    elif source == destination:
        errors.append("Пункт отправления и назначения совпадают")
    if not depart_date:
        errors.append("Укажите дату")

    passengers = (
        db.query(Passenger).filter(Passenger.id.in_(passenger_ids)).all()
        if passenger_ids
        else []
    )
    if passengers:
        # Порядок из формы важен: он влияет на распределение мест.
        by_id = {p.id: p for p in passengers}
        passengers = [by_id[pid] for pid in passenger_ids if pid in by_id]
        errors.extend(validate_group([p.to_bot_dict() for p in passengers], depart_date))
    else:
        errors.append("Выберите хотя бы одного пассажира")

    if errors:
        return _new_job_page(request, user, db, service=service, form=form, errors=errors)

    job = Job(
        user_id=user.id,
        service=service,
        source=source,
        destination=destination,
        depart_date=depart_date,
        notify_email=str(raw.get("notify_email", "")).strip(),
        notify_phone=str(raw.get("notify_phone", "")).strip(),
        status=JobStatus.QUEUED.value,
    )
    job.params = _collect_params(service, raw)
    db.add(job)
    db.flush()

    for position, passenger in enumerate(passengers):
        db.add(JobPassenger(job_id=job.id, passenger_id=passenger.id, position=position))
    db.commit()

    return RedirectResponse(f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


def _collect_params(service: str, raw) -> dict:
    """Поля, специфичные для сервиса. Схему знает только этот модуль."""
    strict = "strict" in raw

    if service == Service.RAILWAY.value:
        return {
            "wagon_type_id": str(raw.get("wagon_type_id", "")).strip(),
            "train_index": str(raw.get("train_index", "")).strip(),
            "train_time": str(raw.get("train_time", "")).strip(),
            "multimedia": "multimedia" in raw,
            "strict": strict,
        }

    return {
        "flight_index": str(raw.get("flight_index", "")).strip() or "1",
        "flight_time": str(raw.get("flight_time", "")).strip(),
        "strict": strict,
    }


# --------------------------------------------------------------------------- #
# Карточка процесса
# --------------------------------------------------------------------------- #


@router.get("/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    job = _job_or_none(db, user, job_id)
    if job is None:
        return _not_found(request, user)

    logs = (
        db.query(JobLog)
        .filter(JobLog.job_id == job.id)
        .order_by(JobLog.id.desc())
        .limit(500)
        .all()
    )

    return render(
        request,
        "job_detail.html",
        user,
        job=job,
        # В шаблон отдаём в прямом порядке: терминал читается сверху вниз.
        logs=list(reversed(logs)),
        running=is_running(job.id),
        service_title=SERVICE_TITLES[job.service],
        busy_with=active_job_id(db, job.service),
        stations=_station_names(db, job),
    )


def _station_names(db: Session, job: Job) -> dict[str, str]:
    """Человеческие названия для кодов маршрута."""
    rows = (
        db.query(Station)
        .filter(Station.service == job.service, Station.external_id.in_([job.source, job.destination]))
        .all()
    )
    names = {row.external_id: row.title for row in rows}
    return {
        "source": names.get(job.source, job.source),
        "destination": names.get(job.destination, job.destination),
    }


# --------------------------------------------------------------------------- #
# Управление
# --------------------------------------------------------------------------- #


@router.post("/{job_id}/start")
def start(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    job = _job_or_none(db, user, job_id)
    if job is None:
        return _not_found(request, user)

    try:
        start_job(db, job)
    except JobStartError as exc:
        return render(
            request,
            "job_detail.html",
            user,
            status_code=409,
            job=job,
            logs=[],
            running=False,
            service_title=SERVICE_TITLES[job.service],
            busy_with=active_job_id(db, job.service),
            stations=_station_names(db, job),
            error=str(exc),
        )

    return RedirectResponse(f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{job_id}/stop")
def stop(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    job = _job_or_none(db, user, job_id)
    if job is None:
        return _not_found(request, user)

    stop_job(db, job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{job_id}/delete")
def delete(
    job_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    job = _job_or_none(db, user, job_id)
    if job is None:
        return _not_found(request, user)

    if is_running(job.id):
        # Иначе процесс остался бы работать, а строки в базе исчезли —
        # и остановить его стало бы нечем.
        stop_job(db, job)

    delete_job(db, job)
    return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Живая консоль
# --------------------------------------------------------------------------- #


@router.get("/{job_id}/stream")
async def stream_logs(
    job_id: int,
    request: Request,
    after: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Поток новых строк лога (Server-Sent Events).

    Опрос базы, а не подписка: строки пишет другой поток, и городить очередь
    в памяти ради одного читателя незачем. Раз в секунду для лога — с запасом.

    `after` — id последней показанной строки: при переподключении клиент
    не получит заново то, что уже видел.
    """
    job = _job_or_none(db, user, job_id)
    if job is None:
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    async def events():
        last_id = after
        idle = 0

        while True:
            if await request.is_disconnected():
                return

            with session_scope() as session:
                rows = (
                    session.query(JobLog)
                    .filter(JobLog.job_id == job_id, JobLog.id > last_id)
                    .order_by(JobLog.id)
                    .limit(200)
                    .all()
                )
                payload = [
                    {
                        "id": row.id,
                        "ts": row.ts.strftime("%H:%M:%S"),
                        "level": row.level,
                        "text": row.text,
                    }
                    for row in rows
                ]
                current = session.get(Job, job_id)
                job_status = current.status if current else JobStatus.FAILED.value

            if payload:
                last_id = payload[-1]["id"]
                idle = 0
                yield f"event: logs\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                idle += 1

            if job_status not in (JobStatus.RUNNING.value, JobStatus.QUEUED.value):
                # Процесс завершился: отдаём финальный статус и закрываемся,
                # чтобы соединение не висело вечно.
                yield f"event: done\ndata: {json.dumps({'status': job_status})}\n\n"
                return

            # Комментарий-пинг держит соединение живым через прокси,
            # которые рвут его при молчании.
            if idle and idle % 15 == 0:
                yield ": ping\n\n"

            await asyncio.sleep(1.0)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx иначе буферизует поток и лог приходит пачками с задержкой.
            "X-Accel-Buffering": "no",
        },
    )
