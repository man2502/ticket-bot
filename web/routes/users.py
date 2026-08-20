"""Раздел «Пользователи» — только для администратора."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from web.auth import require_admin
from web.db import get_db
from web.models import Role, User
from web.security import hash_password
from web.templating import render

router = APIRouter(prefix="/users", tags=["users"])

MIN_PASSWORD_LEN = 8


def _page(
    request: Request,
    user: User,
    db: Session,
    *,
    error: str | None = None,
) -> HTMLResponse:
    users = db.query(User).order_by(User.login).all()
    return render(
        request,
        "users.html",
        user,
        status_code=422 if error else 200,
        users=users,
        error=error,
        roles=list(Role),
    )


@router.get("", response_class=HTMLResponse)
def list_users(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _page(request, user, db)


@router.post("/create")
def create_user(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    role: str = Form(Role.USER.value),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    login = login.strip()

    if not login:
        return _page(request, user, db, error="Логин не может быть пустым")
    if len(password) < MIN_PASSWORD_LEN:
        return _page(request, user, db, error=f"Пароль короче {MIN_PASSWORD_LEN} символов")
    if role not in (Role.ADMIN.value, Role.USER.value):
        return _page(request, user, db, error="Неизвестная роль")
    if db.query(User).filter(User.login == login).one_or_none() is not None:
        return _page(request, user, db, error=f"Пользователь {login!r} уже есть")

    db.add(User(login=login, password_hash=hash_password(password), role=role))
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{user_id}/toggle")
def toggle_user(
    user_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return _page(request, user, db, error="Пользователь не найден")
    if target.id == user.id:
        # Иначе админ закроет себе доступ и панель останется без администратора.
        return _page(request, user, db, error="Нельзя заблокировать самого себя")

    target.is_active = not target.is_active
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{user_id}/password")
def change_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return _page(request, user, db, error="Пользователь не найден")
    if len(password) < MIN_PASSWORD_LEN:
        return _page(request, user, db, error=f"Пароль короче {MIN_PASSWORD_LEN} символов")

    target.password_hash = hash_password(password)
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)
