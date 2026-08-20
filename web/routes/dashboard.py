"""Обзорная страница и заглушки разделов, которые делаются на следующих этапах."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from web.auth import require_admin, require_user
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

    stations = {
        service.value: db.query(Station).filter(Station.service == service.value).count()
        for service in Service
    }

    return render(
        request,
        "dashboard.html",
        user,
        stats=stats,
        stations=stations,
        recent_jobs=jobs.order_by(Job.created_at.desc()).limit(10).all(),
    )


def _placeholder(request: Request, user: User, title: str, note: str) -> HTMLResponse:
    return render(request, "placeholder.html", user, title=title, note=note)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    return _placeholder(request, user, "Процессы", "Запуск поиска и живая консоль — этап 5-6.")


@router.get("/bookings", response_class=HTMLResponse)
def bookings_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    return _placeholder(request, user, "Брони", "Ссылки на оплату и разблокировка — этап 7.")


@router.get("/stations", response_class=HTMLResponse)
def stations_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return _placeholder(
        request, user, "Справочники", "Станции и порты, кнопка пересинхронизации — этап 3."
    )


@router.get("/captcha", response_class=HTMLResponse)
def captcha_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return _placeholder(request, user, "Капчи", "Разметка нерешённых капч — этап 8.")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return _placeholder(
        request, user, "Настройки", "SMTP, SMS-шлюз, параметры сервисов — этап 4."
    )
