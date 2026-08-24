"""Раздел «Капчи» — разметка того, что CNN не осилила. Только админ.

Процесс поиска сюда не заглядывает и никогда не ждёт человека: раздел нужен
для дообучения модели, а не для помощи в реальном времени.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from web.auth import require_admin
from web.db import get_db
from web.models import CaptchaSample, User
from web.services.captcha import (
    N_DIGITS,
    CaptchaError,
    discard_sample,
    label_sample,
    list_pending,
    sample_path,
    stats,
    training_hint,
)
from web.templating import render

router = APIRouter(prefix="/captcha", tags=["captcha"])


def _page(
    request: Request,
    user: User,
    db: Session,
    *,
    message: str | None = None,
    is_error: bool = False,
) -> HTMLResponse:
    try:
        items = list_pending()
        counters = stats()
        broken = ""
    except Exception as exc:  # noqa: BLE001 — датасета может не быть вовсе
        items, counters, broken = [], {}, str(exc)

    return render(
        request,
        "captcha.html",
        user,
        status_code=422 if is_error else 200,
        items=items,
        counters=counters,
        broken=broken,
        digits=N_DIGITS,
        hint=training_hint(),
        message=message,
        is_error=is_error,
        recent=(
            db.query(CaptchaSample)
            .filter(CaptchaSample.labeled_at.isnot(None))
            .order_by(CaptchaSample.labeled_at.desc())
            .limit(10)
            .all()
        ),
    )


@router.get("", response_class=HTMLResponse)
def captcha_page(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _page(request, user, db)


@router.get("/image/{name}")
def captcha_image(name: str, user: User = Depends(require_admin)):
    """
    Отдать картинку капчи.

    Проверку имени делает `sample_path`: оно приходит из адреса, и без неё
    можно было бы вытащить любой файл с диска.
    """
    try:
        path = sample_path(name)
    except CaptchaError:
        return RedirectResponse("/captcha", status_code=status.HTTP_303_SEE_OTHER)

    # no-store: картинка исчезает после разметки, и закешированная превратилась
    # бы в «призрак», который нельзя ни разметить, ни убрать.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/label")
def label(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        result = label_sample(db, name, code, user_id=user.id)
    except CaptchaError as exc:
        return _page(request, user, db, message=str(exc), is_error=True)
    return _page(request, user, db, message=result)


@router.post("/discard")
def discard(
    request: Request,
    name: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        result = discard_sample(db, name, user_id=user.id)
    except CaptchaError as exc:
        return _page(request, user, db, message=str(exc), is_error=True)
    return _page(request, user, db, message=result)
