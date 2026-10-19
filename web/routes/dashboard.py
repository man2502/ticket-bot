"""Обзорная страница: счётчики, состояние справочников, последние процессы."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from web.auth import require_user
from web.db import get_db
from web.models import Job, JobStatus, Passenger, Service, Station, User
from web.templating import render

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    jobs = db.query(Job)
    if not user.is_admin:
        # Обычный пользователь видит только свои процессы.
        jobs = jobs.filter(Job.user_id == user.id)

    active = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
    stats = {
        "passengers": db.query(Passenger).count(),
        "jobs_active": jobs.filter(Job.status.in_(active)).count(),
        "jobs_total": jobs.count(),
        "needs_review": jobs.filter(Job.status == JobStatus.NEEDS_REVIEW.value).count(),
    }

    stats["booked"] = jobs.filter(Job.status == JobStatus.BOOKED.value).count()

    stations = {
        service.value: db.query(Station).filter(Station.service == service.value).count()
        for service in Service
    }

    # Что требует человека: бронь под вопросом и заблокированная автобронь.
    # Пока это висит, новый поиск по сервису не запустится — показываем сразу.
    from web.services.bookings import blockers
    from web.services.stations import title_map

    blocked = [
        service.value for service in Service if blockers(db, service.value)
    ]
    review_jobs = (
        jobs.filter(Job.status == JobStatus.NEEDS_REVIEW.value)
        .order_by(Job.created_at.desc()).limit(5).all()
    )

    return render(
        request,
        "dashboard.html",
        user,
        stats=stats,
        stations=stations,
        blocked_services=blocked,
        review_jobs=review_jobs,
        station_names=title_map(db),
        recent_jobs=jobs.order_by(Job.created_at.desc()).limit(8).all(),
    )




