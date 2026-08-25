"""Раздел «Капчи» — разметка того, что CNN не осилила. Только админ.

Процесс поиска сюда не заглядывает и никогда не ждёт человека: раздел нужен
для дообучения модели, а не для помощи в реальном времени.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from sqlalchemy.orm import Session

from web.auth import require_admin
from web.db import get_db, session_scope
from web.models import CaptchaSample, TrainingLog, TrainingRun, TrainingStatus, User
from web.services.captcha import (
    N_DIGITS,
    CaptchaError,
    discard_sample,
    label_sample,
    list_pending,
    sample_path,
    stats,
)
from web.services.training import (
    TrainingError,
    apply_model,
    epoch_progress,
    format_duration,
    latest_run,
    rollback_model,
    start_training,
    stop_training,
    training_state,
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

    try:
        training = training_state(db)
    except Exception as exc:  # noqa: BLE001 — torch может быть не установлен
        training = {"broken": str(exc), "running": False, "can_start": False}

    run = training.get("run")
    if run is not None:
        training["duration"] = format_duration(run)
        training["progress"] = epoch_progress(db, run)
        # Хвост лога: обучение печатает раз в десять эпох, двухсот строк
        # хватает на любой прогон с запасом.
        training["logs"] = list(
            reversed(
                db.query(TrainingLog)
                .filter(TrainingLog.run_id == run.id)
                .order_by(TrainingLog.id.desc())
                .limit(200)
                .all()
            )
        )

    return render(
        request,
        "captcha.html",
        user,
        status_code=422 if is_error else 200,
        items=items,
        counters=counters,
        broken=broken,
        digits=N_DIGITS,
        training=training,
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


# --------------------------------------------------------------------------- #
# Обучение модели
# --------------------------------------------------------------------------- #


@router.post("/train")
def train(
    request: Request,
    epochs: int = Form(100),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Запустить дообучение.

    Поиск при этом не останавливается: обучение идёт около часа, и час без
    поиска ради модели никому не нужен. Результат ляжет в отдельный файл.
    """
    try:
        run = start_training(db, user_id=user.id, epochs=epochs)
    except TrainingError as exc:
        return _page(request, user, db, message=str(exc), is_error=True)
    return _page(
        request,
        user,
        db,
        message=f"Обучение #{run.id} запущено — следите за логом ниже",
    )


@router.post("/train/stop")
def train_stop(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    run = latest_run(db)
    if run is None:
        return _page(request, user, db, message="Обучений ещё не было", is_error=True)

    stopped = stop_training(db, run)
    return _page(
        request,
        user,
        db,
        message="Обучение остановлено" if stopped else "Процесс уже не работал",
    )


@router.post("/train/apply")
def train_apply(
    request: Request,
    run_id: int = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Сделать обученную модель рабочей."""
    run = db.get(TrainingRun, run_id)
    if run is None:
        return _page(request, user, db, message="Обучение не найдено", is_error=True)

    try:
        result = apply_model(db, run)
    except TrainingError as exc:
        return _page(request, user, db, message=str(exc), is_error=True)
    return _page(request, user, db, message=result)


@router.post("/train/rollback")
def train_rollback(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        result = rollback_model(db)
    except TrainingError as exc:
        return _page(request, user, db, message=str(exc), is_error=True)
    return _page(request, user, db, message=result)


@router.get("/train/{run_id}/stream")
async def train_stream(
    run_id: int,
    request: Request,
    after: int = 0,
    user: User = Depends(require_admin),
):
    """
    Живой лог обучения (Server-Sent Events).

    Опрос базы, а не подписка: строки пишет другой поток, и городить очередь
    в памяти ради одного читателя незачем. Обучение печатает раз в десять эпох,
    то есть раз в несколько минут, — секунды опроса тут с большим запасом.
    """

    async def events():
        last_id = after
        idle = 0

        while True:
            if await request.is_disconnected():
                return

            with session_scope() as session:
                rows = (
                    session.query(TrainingLog)
                    .filter(TrainingLog.run_id == run_id, TrainingLog.id > last_id)
                    .order_by(TrainingLog.id)
                    .limit(200)
                    .all()
                )
                payload = [
                    {
                        "id": row.id,
                        "ts": row.ts.strftime("%H:%M:%S"),
                        "level": row.level,
                        "text": row.text,
                    }
                    for row in rows
                ]
                current = session.get(TrainingRun, run_id)
                run_status = current.status if current else TrainingStatus.FAILED.value

            if payload:
                last_id = payload[-1]["id"]
                idle = 0
                yield f"event: logs\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                idle += 1

            if run_status != TrainingStatus.RUNNING.value:
                yield f"event: done\ndata: {json.dumps({'status': run_status})}\n\n"
                return

            # Комментарий-пинг держит соединение живым через прокси,
            # которые рвут его при молчании.
            if idle and idle % 15 == 0:
                yield ": ping\n\n"

            await asyncio.sleep(1.0)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx иначе буферизует поток и лог приходит пачками с задержкой.
            "X-Accel-Buffering": "no",
        },
    )
