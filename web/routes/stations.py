"""Раздел «Справочники» — станции поездов и аэропорты.

Только для администратора: список тянется с сайтов перевозчиков, и кнопка
обновления — это внешний запрос, которым не стоит давать управлять всем подряд.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from web.auth import require_admin
from web.db import get_db
from web.models import Service, Station, User
from web.services.stations import sync_service
from web.templating import render

router = APIRouter(prefix="/stations", tags=["stations"])

SERVICE_TITLES = {
    Service.AIRLINES.value: "Аэропорты",
    Service.RAILWAY.value: "Станции поездов",
}

SERVICE_SOURCES = {
    Service.AIRLINES.value: "turkmenistanairlines.tm/ru/get_internal_ports",
    Service.RAILWAY.value: "api.railway.gov.tm/v2/stations",
}


def _page(
    request: Request,
    user: User,
    db: Session,
    *,
    message: str | None = None,
    is_error: bool = False,
) -> HTMLResponse:
    groups = []
    for service in Service:
        stations = (
            db.query(Station)
            .filter(Station.service == service.value)
            # Скрытые — вниз, дальше по названию: активные нужны чаще.
            .order_by(Station.is_active.desc(), Station.title_ru, Station.code)
            .all()
        )
        groups.append(
            {
                "service": service.value,
                "title": SERVICE_TITLES[service.value],
                "source": SERVICE_SOURCES[service.value],
                "stations": stations,
                "active": sum(1 for s in stations if s.is_active),
                "synced_at": max((s.synced_at for s in stations), default=None),
            }
        )

    return render(
        request,
        "stations.html",
        user,
        status_code=502 if is_error else 200,
        groups=groups,
        message=message,
        is_error=is_error,
    )


@router.get("", response_class=HTMLResponse)
def list_stations(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _page(request, user, db)


@router.post("/{service}/sync")
def resync(
    service: str,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if service not in {s.value for s in Service}:
        return RedirectResponse("/stations", status_code=status.HTTP_303_SEE_OTHER)

    result = sync_service(db, service)
    title = SERVICE_TITLES[service]

    # Результат показываем на той же странице, а не редиректом: человеку нужно
    # видеть, что именно изменилось, а не просто обновлённый список.
    return _page(
        request,
        user,
        db,
        message=f"{title}: {result.summary()}",
        is_error=not result.ok,
    )
