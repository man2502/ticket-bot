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
    blockers as find_blockers,
    booking_message_fields,
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

# Слово-подтверждение для снятия блокировки. Латиница намеренно: на русской
# раскладке его не наберёшь случайно, а именно случайного нажатия мы и боимся —
# за ним стоит риск второй брони и второго счёта.
UNBLOCK_WORD = "open"


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

    # Незакрытые попытки по всем журналам: у самолётов их столько, сколько
    # аккаунтов, плюс общий для запусков из командной строки.
    from web.services.providers import list_providers

    titles = {p.id: p.title for p in list_providers(db)}
    blockers = []
    for service in Service:
        for blocker in find_blockers(db, service.value):
            names = [titles.get(pid, f"#{pid}") for pid in blocker.provider_ids]
            if any(j.provider_id is None for j in blocker.journals):
                names.append("общий журнал")
            blockers.append(
                {
                    "service": service.value,
                    "title": SERVICE_TITLES[service.value],
                    "record": blocker.record,
                    "journal": ", ".join(str(j.path) for j in blocker.journals),
                    "accounts": ", ".join(names),
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
        unblock_word=UNBLOCK_WORD,
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
    if confirm.strip().lower() != UNBLOCK_WORD:
        return _page(
            request, user, db, is_error=True,
            message=(
                f"Для снятия блокировки впишите слово «{UNBLOCK_WORD}» — "
                f"это защита от случайного нажатия"
            ),
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

    to_email = job.notify_email if job else ""
    to_phone = job.notify_phone if job else ""

    subject, body, sms = format_booking_message(**booking_message_fields(booking))
    results = notify_booking(
        db, subject=subject, body=body, sms_text=sms,
        email=to_email, phone=to_phone, booking_id=booking.id,
    )

    if not results:
        return _page(request, user, db, is_error=True,
                     message="Некуда отправлять: не задан получатель ни в процессе, ни в настройках")

    summary = "; ".join(f"{r.channel}: {r.summary()}" for r in results)
    return _page(request, user, db, message=summary, is_error=not all(r.ok for r in results))
