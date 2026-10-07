"""Веб-панель управления поиском и бронированием билетов.

Здесь только сборка приложения: подключение роутов, статики и обработчиков
ошибок. Сами роуты лежат в `web/routes/`, бизнес-логика — в `web/services/`.

Запуск:
    python -m web          # или run_web.cmd / run_web.sh
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from web.auth import LoginRedirect
from web.config import STATIC_DIR, ensure_dirs
from web.db import get_session_factory, init_db
from web.models import User
from web.routes import (
    auth,
    bookings,
    captcha,
    dashboard,
    errors,
    jobs,
    passengers,
    settings as settings_routes,
    stations,
    users,
)
from web.security import read_session_token, SESSION_COOKIE
from web.templating import render

app = FastAPI(title="Ticket Panel", docs_url=None, redoc_url=None)

ensure_dirs()
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(errors.router)
app.include_router(bookings.router)
app.include_router(captcha.router)
app.include_router(jobs.router)
app.include_router(passengers.router)
app.include_router(settings_routes.router)
app.include_router(stations.router)
app.include_router(users.router)


@app.on_event("startup")
def _on_startup() -> None:
    """Таблицы и первичные справочники. Отдельная команда миграции не нужна."""
    init_db()

    # Справочники тянем только если они пусты. Сети может не быть — панель
    # обязана подняться в любом случае, обновить можно кнопкой.
    from web.db import session_scope
    from web.services.stations import ensure_stations

    try:
        with session_scope() as session:
            ensure_stations(session)
    except Exception as exc:  # noqa: BLE001
        print(f"[stations] первичная синхронизация пропущена: {exc}")

    # Процессы не переживают остановку панели, а записи в базе остаются:
    # без этого очередь сервиса оказалась бы занята навсегда.
    from web.services.runner import recover_orphans

    with session_scope() as session:
        fixed = recover_orphans(session)
        if fixed:
            print(f"[runner] помечено остановленными после перезапуска: {fixed}")

    # То же самое для обучения модели: час работы не переживает перезапуск,
    # а запись «идёт обучение» осталась бы висеть и не дала бы запустить новое.
    from web.services.training import recover_orphans as recover_training

    with session_scope() as session:
        fixed = recover_training(session)
        if fixed:
            print(f"[training] помечено остановленными после перезапуска: {fixed}")

    # Журнал ошибок держим месяц: сводка смотрит на неделю, остальное — на разбор.
    from web.services.errors import prune as prune_errors

    with session_scope() as session:
        removed = prune_errors(session)
        if removed:
            print(f"[errors] убрано старых ошибок: {removed}")

    # Брони, о которых панель не знает: бота могли запустить из командной строки
    # или панель упала между POST и чтением журнала. Ссылка на оплату лежит
    # в журнале бота, и без этого обхода про неё никто бы не узнал.
    from web.services.bookings import notify_orphan_bookings

    try:
        with session_scope() as session:
            picked = notify_orphan_bookings(session)
            if picked:
                print(f"[bookings] подобрано из журналов: {len(picked)}")
    except Exception as exc:  # noqa: BLE001 — панель обязана подняться
        print(f"[bookings] обход журналов пропущен: {exc}")


@app.on_event("shutdown")
def _on_shutdown() -> None:
    """Не оставлять ботов и обучение работать после закрытия панели."""
    from web.services.runner import stop_all
    from web.services.training import stop_all as stop_training_all

    stop_all()
    stop_training_all()


@app.exception_handler(LoginRedirect)
async def _login_redirect(request: Request, exc: LoginRedirect) -> RedirectResponse:
    """Истёкшая cookie должна приводить к форме входа, а не к странице ошибки."""
    return RedirectResponse(
        f"/login?next={exc.next_url or '/'}", status_code=status.HTTP_303_SEE_OTHER
    )


def _user_for_error_page(request: Request) -> User | None:
    """
    Пользователь для страницы ошибки.

    Обработчик исключений живёт вне цикла зависимостей, готовой сессии у него
    нет — открываем свою. Ошибку глотаем: страница 403 не должна падать
    из-за того, что не удалось нарисовать шапку.
    """
    uid = read_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if uid is None:
        return None
    session = get_session_factory()()
    try:
        return session.get(User, uid)
    except Exception:  # noqa: BLE001
        return None
    finally:
        session.close()


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException) -> HTMLResponse:
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return render(
            request,
            "not_found.html",
            _user_for_error_page(request),
            status_code=404,
            title="Страница не найдена",
            back_url="/",
        )
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return render(
            request,
            "not_found.html",
            _user_for_error_page(request),
            status_code=403,
            title="Доступ закрыт",
            note=str(exc.detail),
            back_url="/",
        )
    raise exc
