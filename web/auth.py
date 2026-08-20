"""Вход, выход и проверка прав в роутах.

Как устроен доступ:

* `current_user` — кто пришёл, или None. Для страниц, доступных всем.
* `require_user` — нужен вход. Иначе редирект на /login.
* `require_admin` — нужна роль admin. Иначе 403.

Редирект вместо 403 для неавторизованных сделан намеренно: человек, у которого
истекла cookie, должен увидеть форму входа, а не страницу ошибки.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from web.db import get_db
from web.models import User
from web.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SEC,
    make_session_token,
    read_session_token,
)

__all__ = [
    "current_user",
    "require_user",
    "require_admin",
    "login_user",
    "logout_user",
    "LoginRedirect",
]


class LoginRedirect(Exception):
    """Не авторизован. Обработчик в app.py превращает это в редирект на /login."""

    def __init__(self, next_url: str = "/") -> None:
        super().__init__("login required")
        self.next_url = next_url


def current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Пользователь из cookie или None. Заблокированный считается неавторизованным."""
    uid = read_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if uid is None:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def require_user(
    request: Request,
    user: User | None = Depends(current_user),
) -> User:
    if user is None:
        # Запоминаем, куда человек шёл, чтобы вернуть его туда после входа.
        raise LoginRedirect(next_url=str(request.url.path))
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Раздел доступен только администратору",
        )
    return user


def login_user(response: RedirectResponse, user: User) -> None:
    """Выдать cookie сессии."""
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(user.id),
        max_age=SESSION_MAX_AGE_SEC,
        httponly=True,  # недоступна из JavaScript
        samesite="lax",  # защита от CSRF при переходах с чужих сайтов
        # secure=True не ставим: панель обычно работает на localhost по http,
        # и с этим флагом cookie просто не сохранилась бы.
    )


def logout_user(response: RedirectResponse) -> None:
    response.delete_cookie(SESSION_COOKIE)
