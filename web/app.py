"""Веб-панель управления поиском и бронированием билетов.

Этап 1: база, вход, роли, навигация. Разделы наполняются дальше.

Запуск:
    python -m web            # или run_web.cmd на Windows
"""

from __future__ import annotations

import datetime as dt

from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from web.auth import (
    LoginRedirect,
    current_user,
    login_user,
    logout_user,
    require_admin,
    require_user,
)
from web.config import STATIC_DIR, TEMPLATES_DIR, ensure_dirs
from web.db import get_db, init_db
from web.models import Job, JobStatus, Passenger, Role, Service, Station, User
from web.security import hash_password, verify_password

app = FastAPI(title="Ticket Panel", docs_url=None, redoc_url=None)

ensure_dirs()
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --------------------------------------------------------------------------- #
# Инфраструктура шаблонов
# --------------------------------------------------------------------------- #

# Пункты верхнего меню. admin_only скрывает пункт от обычного пользователя —
# но это только внешний вид; настоящую проверку делает require_admin в роуте.
NAV_ITEMS = [
    {"url": "/jobs", "title": "Процессы", "admin_only": False},
    {"url": "/passengers", "title": "Пассажиры", "admin_only": False},
    {"url": "/bookings", "title": "Брони", "admin_only": False},
    {"url": "/stations", "title": "Справочники", "admin_only": True},
    {"url": "/captcha", "title": "Капчи", "admin_only": True},
    {"url": "/settings", "title": "Настройки", "admin_only": True},
    {"url": "/users", "title": "Пользователи", "admin_only": True},
]


def render(
    request: Request,
    template: str,
    user: User | None,
    **context,
) -> HTMLResponse:
    """Отрисовать страницу с общим контекстом (меню, текущий пользователь)."""
    visible_nav = [
        item for item in NAV_ITEMS if not item["admin_only"] or (user and user.is_admin)
    ]
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "user": user,
            "nav_items": visible_nav,
            "current_path": request.url.path,
            **context,
        },
    )


@app.on_event("startup")
def _on_startup() -> None:
    """Создать таблицы при первом запуске — отдельная команда миграции не нужна."""
    init_db()


@app.exception_handler(LoginRedirect)
async def _login_redirect_handler(request: Request, exc: LoginRedirect) -> RedirectResponse:
    target = exc.next_url or "/"
    return RedirectResponse(
        f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER
    )


# --------------------------------------------------------------------------- #
# Вход и выход
# --------------------------------------------------------------------------- #


@app.get("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    next: str = "/",
    user: User | None = Depends(current_user),
) -> HTMLResponse:
    if user is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", None, next_url=next, error=None)


@app.post("/login")
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.login == login.strip()).one_or_none()

    # Одно сообщение на все случаи: по разным текстам можно узнать,
    # какие логины существуют.
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return render(
            request,
            "login.html",
            None,
            next_url=next,
            error="Неверный логин или пароль",
        )

    user.last_login_at = dt.datetime.now()
    db.commit()

    # Открытый редирект: принимаем только внутренние пути.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    login_user(response, user)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    logout_user(response)
    return response


# --------------------------------------------------------------------------- #
# Главная
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    active_statuses = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)

    jobs_query = db.query(Job)
    if not user.is_admin:
        # Обычный пользователь видит только свои процессы.
        jobs_query = jobs_query.filter(Job.user_id == user.id)

    stats = {
        "passengers": db.query(Passenger).count(),
        "jobs_active": jobs_query.filter(Job.status.in_(active_statuses)).count(),
        "jobs_total": jobs_query.count(),
        "needs_review": jobs_query.filter(
            Job.status == JobStatus.NEEDS_REVIEW.value
        ).count(),
    }

    stations = {
        service.value: db.query(Station).filter(Station.service == service.value).count()
        for service in Service
    }

    recent = jobs_query.order_by(Job.created_at.desc()).limit(10).all()

    return render(
        request,
        "dashboard.html",
        user,
        stats=stats,
        stations=stations,
        recent_jobs=recent,
    )


# --------------------------------------------------------------------------- #
# Заглушки разделов — наполняются на следующих этапах
# --------------------------------------------------------------------------- #


def _placeholder(request: Request, user: User, title: str, note: str) -> HTMLResponse:
    return render(request, "placeholder.html", user, title=title, note=note)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    return _placeholder(
        request, user, "Процессы", "Запуск поиска и живая консоль — этап 5-6."
    )


@app.get("/passengers", response_class=HTMLResponse)
def passengers_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    return _placeholder(request, user, "Пассажиры", "Общий список пассажиров — этап 2.")


@app.get("/bookings", response_class=HTMLResponse)
def bookings_page(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
    return _placeholder(
        request, user, "Брони", "Ссылки на оплату и разблокировка — этап 7."
    )


@app.get("/stations", response_class=HTMLResponse)
def stations_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return _placeholder(
        request, user, "Справочники", "Станции и порты, кнопка пересинхронизации — этап 3."
    )


@app.get("/captcha", response_class=HTMLResponse)
def captcha_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return _placeholder(request, user, "Капчи", "Разметка нерешённых капч — этап 8.")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_admin)) -> HTMLResponse:
    return _placeholder(
        request, user, "Настройки", "SMTP, SMS-шлюз, параметры сервисов — этап 4."
    )


# --------------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------------- #


@app.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    error: str | None = None,
) -> HTMLResponse:
    users = db.query(User).order_by(User.login).all()
    return render(request, "users.html", user, users=users, error=error, roles=list(Role))


@app.post("/users/create")
def users_create(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    role: str = Form(Role.USER.value),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    login = login.strip()
    error: str | None = None

    if not login:
        error = "Логин не может быть пустым"
    elif len(password) < 8:
        error = "Пароль короче 8 символов"
    elif role not in (Role.ADMIN.value, Role.USER.value):
        error = "Неизвестная роль"
    elif db.query(User).filter(User.login == login).one_or_none() is not None:
        error = f"Пользователь {login!r} уже есть"

    if error:
        users = db.query(User).order_by(User.login).all()
        return render(request, "users.html", user, users=users, error=error, roles=list(Role))

    db.add(User(login=login, password_hash=hash_password(password), role=role))
    db.commit()
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/users/{user_id}/toggle")
def users_toggle(
    user_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    error: str | None = None

    if target is None:
        error = "Пользователь не найден"
    elif target.id == user.id:
        # Иначе админ может закрыть себе доступ и остаться без панели.
        error = "Нельзя заблокировать самого себя"
    else:
        target.is_active = not target.is_active
        db.commit()

    if error:
        users = db.query(User).order_by(User.login).all()
        return render(request, "users.html", user, users=users, error=error, roles=list(Role))
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/users/{user_id}/password")
def users_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    error: str | None = None

    if target is None:
        error = "Пользователь не найден"
    elif len(password) < 8:
        error = "Пароль короче 8 символов"
    else:
        target.password_hash = hash_password(password)
        db.commit()

    if error:
        users = db.query(User).order_by(User.login).all()
        return render(request, "users.html", user, users=users, error=error, roles=list(Role))
    return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)
