"""Раздел «Брони»: ссылки на оплату, история и снятие блокировки.

Свои брони видит любой вошедший, чужие — только админ. Снимать блокировку
журнала может только админ: это ровно тот случай, когда неверное решение
стоит второго счёта в банке.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from web.auth import require_admin, require_user
from web.db import get_db
from web.models import Booking, Job, NotificationLog, Service, User
from web.services.bookings import (
    STATUS_TITLES,
    blocking_attempt,
    journal_path,
    SERVICE_ROUTE_UNKNOWN,
    notify_orphan_bookings,
    unblock_service,
)
from web.services.notifications import format_booking_message, notify_booking
from web.templating import render

router = APIRouter(prefix="/bookings", tags=["bookings"])

SERVICE_TITLES = {
    Service.AIRLINES.value: "Самолёты",
    Service.RAILWAY.value: "Поезда",
}


def _page(
    request: Request,
    user: User,
    db: Session,
    *,
    message: str | None = None,
    is_error: bool = False,
) -> HTMLResponse:
    # Заодно подбираем брони, о которых панель не знала: бота могли запустить
    # из командной строки. Чтение двух небольших JSON — дешевле, чем потерянная
    # ссылка на оплату.
    try:
        notify_orphan_bookings(db)
    except Exception as exc:  # noqa: BLE001 — страница обязана открыться
        print(f"[bookings] обход журналов пропущен: {exc}")

    # Бесхозные брони (процесс удалён или запуск был вне панели) видит только
    # админ: понять, чьи они, неоткуда.
    query = db.query(Booking).outerjoin(Job, Booking.job_id == Job.id)
    if not user.is_admin:
        query = query.filter(Job.user_id == user.id)
    bookings = query.order_by(Booking.created_at.desc()).limit(100).all()

    # Что сейчас мешает запустить новый поиск по каждому сервису.
    blockers = []
    for service in Service:
        record = blocking_attempt(service.value)
        if record is not None:
            blockers.append(
                {
                    "service": service.value,
                    "title": SERVICE_TITLES[service.value],
                    "record": record,
                    "journal": journal_path(service.value),
                }
            )

    return render(
        request,
        "bookings.html",
        user,
        status_code=422 if is_error else 200,
        bookings=bookings,
        blockers=blockers,
        status_titles=STATUS_TITLES,
        service_titles=SERVICE_TITLES,
        message=message,
        is_error=is_error,
        sends={
            log.booking_id: log
            for log in db.query(NotificationLog)
            .filter(NotificationLog.booking_id.isnot(None))
            .order_by(NotificationLog.id.desc())
            .all()
        },
    )


@router.get("", response_class=HTMLResponse)
def list_bookings(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _page(request, user, db)


@router.post("/unblock")
def unblock(
    request: Request,
    service: str = Form(...),
    guard_key: str = Form(""),
    confirm: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if service not in SERVICE_TITLES:
        return RedirectResponse("/bookings", status_code=status.HTTP_303_SEE_OTHER)

    # Требуем явного подтверждения: блокировка обычно означает «бронь могла
    # быть создана», и снять её вслепую — риск второго счёта.
    if confirm.strip().lower() != "снять":
        return _page(
            request, user, db, is_error=True,
            message='Для снятия блокировки впишите слово «снять» — это защита от случайного нажатия',
        )

    result = unblock_service(db, service, guard_key=guard_key.strip())
    return _page(request, user, db, message=f"{SERVICE_TITLES[service]}: {result}")


@router.post("/{booking_id}/resend")
def resend(
    booking_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Отправить ссылку на оплату ещё раз — если письмо не дошло."""
    booking = db.get(Booking, booking_id)
    if booking is None:
        return RedirectResponse("/bookings", status_code=status.HTTP_303_SEE_OTHER)

    job = db.get(Job, booking.job_id) if booking.job_id else None
    # Бронь без процесса — законный случай: запуск из командной строки или
    # удалённый поиск. Разослать её может админ, обычному человеку неоткуда
    # знать, его она или чужая.
    if job is None:
        if not user.is_admin:
            return RedirectResponse("/bookings", status_code=status.HTTP_303_SEE_OTHER)
    elif not user.is_admin and job.user_id != user.id:
        return RedirectResponse("/bookings", status_code=status.HTTP_303_SEE_OTHER)

    if not booking.payment_url:
        return _page(request, user, db, is_error=True, message="У этой брони нет ссылки на оплату")

    if job is None:
        route = SERVICE_ROUTE_UNKNOWN.get(booking.service, booking.service)
        names, to_email, to_phone = "", "", ""
    else:
        route = f"{job.source} → {job.destination}, {job.depart_date}"
        names = ", ".join(
            f"{link.passenger.last_name} {link.passenger.first_name}"
            for link in sorted(job.passengers, key=lambda x: x.position)
        )
        to_email, to_phone = job.notify_email, job.notify_phone

    subject, body, sms = format_booking_message(
        route=route,
        payment_url=booking.payment_url,
        booking_number=booking.booking_number,
        passengers=names,
    )
    results = notify_booking(
        db, subject=subject, body=body, sms_text=sms,
        email=to_email, phone=to_phone, booking_id=booking.id,
    )

    if not results:
        return _page(request, user, db, is_error=True,
                     message="Некуда отправлять: не задан получатель ни в процессе, ни в настройках")

    summary = "; ".join(f"{r.channel}: {r.summary()}" for r in results)
    return _page(request, user, db, message=summary, is_error=not all(r.ok for r in results))
