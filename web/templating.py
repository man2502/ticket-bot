"""Отрисовка страниц и общий контекст шаблонов.

Вынесено из `app.py`, потому что нужно всем роутам, а импортировать роуты
из приложения, которое само их подключает, — прямой путь к циклическому импорту.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from web.config import STATIC_DIR, TEMPLATES_DIR
from web.models import User

__all__ = ["render", "templates", "NAV_ITEMS"]

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Пункты верхнего меню. admin_only убирает пункт из вида обычного пользователя,
# но это только оформление: настоящую проверку делает require_admin в роуте.
# group — подпись раздела в боковом меню, icon — значок из templates/_icons.html.
NAV_ITEMS = [
    {"url": "/", "title": "Обзор", "admin_only": False, "group": "", "icon": "home"},
    {"url": "/jobs", "title": "Процессы", "admin_only": False, "group": "", "icon": "activity"},
    {"url": "/bookings", "title": "Брони", "admin_only": False, "group": "", "icon": "ticket"},
    {"url": "/passengers", "title": "Пассажиры", "admin_only": False, "group": "", "icon": "users"},
    {"url": "/errors", "title": "Ошибки", "admin_only": True, "group": "Наблюдение", "icon": "alert"},
    {"url": "/captcha", "title": "Капчи", "admin_only": True, "group": "Наблюдение", "icon": "scan"},
    {"url": "/settings", "title": "Настройки", "admin_only": True, "group": "Управление",
     "icon": "sliders"},
    {"url": "/stations", "title": "Справочники", "admin_only": True, "group": "Управление",
     "icon": "pin"},
    {"url": "/users", "title": "Пользователи", "admin_only": True, "group": "Управление",
     "icon": "shield"},
]


def static_version() -> str:
    """
    Версия стилей для адреса `app.css?v=…`.

    Без неё браузер после обновления панели держит старый файл из кеша, и
    новая разметка рисуется старыми стилями. Время изменения файла меняется
    ровно тогда, когда меняются стили.
    """
    try:
        return str(int((STATIC_DIR / "app.css").stat().st_mtime))
    except OSError:
        return "0"


def render(
    request: Request,
    template: str,
    user: User | None,
    *,
    status_code: int = 200,
    **context,
) -> HTMLResponse:
    """Страница с общим контекстом: меню, текущий пользователь, активный путь."""
    visible_nav = [
        item for item in NAV_ITEMS if not item["admin_only"] or (user and user.is_admin)
    ]
    return templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status_code,
        context={
            "user": user,
            "nav_items": visible_nav,
            "current_path": request.url.path,
            "static_version": static_version(),
            **context,
        },
    )
