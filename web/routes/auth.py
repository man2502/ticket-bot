"""Вход и выход."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from web.auth import current_user, login_user, logout_user
from web.db import get_db
from web.models import User
from web.security import verify_password
from web.templating import render

router = APIRouter(tags=["auth"])


def _safe_next(target: str) -> str:
    """
    Куда вернуть после входа.

    Принимаем только внутренние пути: без этой проверки ссылка вида
    `/login?next=https://зло.example` увела бы человека на чужой сайт
    сразу после ввода пароля.
    """
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


@router.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    next: str = "/",
    user: User | None = Depends(current_user),
):
    if user is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", None, next_url=_safe_next(next), error=None)


@router.post("/login")
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.login == login.strip()).one_or_none()

    # Одно сообщение на все случаи: по разным текстам можно перебором узнать,
    # какие логины существуют.
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return render(
            request,
            "login.html",
            None,
            status_code=401,
            next_url=_safe_next(next),
            error="Неверный логин или пароль",
        )

    user.last_login_at = dt.datetime.now()
    db.commit()

    response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    login_user(response, user)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    logout_user(response)
    return response
