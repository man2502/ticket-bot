"""Раздел «Пассажиры» — общий справочник для самолётов и поездов.

Список общий намеренно: один и тот же человек летит и едет, и вести его данные
в двух местах значит рано или поздно получить два написания фамилии в паспорте.

Права: заводить и править пассажиров может любой вошедший, удалять — только
администратор. Пассажир может участвовать в чужом процессе поиска, и удаление
его данных на ходу ломает бронь.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from web.auth import require_admin, require_user
from web.db import get_db
from web.models import JobPassenger, Passenger, User
from web.services.passengers import (
    GENDERS,
    PAX_TYPES,
    PassengerForm,
    clean_passenger_form,
    pax_type_on_date,
)
from web.templating import render

router = APIRouter(prefix="/passengers", tags=["passengers"])

# Поля формы: один список для чтения запроса и для отрисовки шаблона.
FORM_FIELDS = (
    "first_name",
    "last_name",
    "dob",
    "gender",
    "doc_number",
    "doc_expdt",
    "citizenship",
    "email",
    "phone",
    "pax_type",
    "note",
)


def _describe_age(passenger: Passenger) -> str:
    """
    Возраст и тип на сегодня — подсказка в списке.

    Именно на сегодня, а не на дату рейса: рейса в этом разделе ещё нет.
    Итоговый тариф считается при создании процесса, от даты вылета.
    """
    try:
        pax_type = pax_type_on_date(passenger.dob, dt.date.today())
    except ValueError:
        return "?"
    titles = {"adult": "взрослый", "child": "ребёнок", "infant": "младенец"}
    return titles.get(pax_type, pax_type)


def _form_from_passenger(passenger: Passenger) -> PassengerForm:
    return PassengerForm(
        first_name=passenger.first_name,
        last_name=passenger.last_name,
        dob=passenger.dob,
        gender=passenger.gender,
        doc_number=passenger.doc_number,
        doc_expdt=passenger.doc_expdt,
        citizenship=passenger.citizenship,
        email=passenger.email,
        phone=passenger.phone,
        pax_type=passenger.pax_type,
        note=passenger.note,
    )


async def _form_data(request: Request) -> dict[str, str]:
    """
    Значения формы одним словарём.

    Читаем через request.form(), а не объявляем одиннадцать параметров Form(...):
    список полей уже задан в FORM_FIELDS, и дублировать его в сигнатуре двух
    обработчиков — верный способ забыть добавить поле в одном из них.
    """
    raw = await request.form()
    return {name: str(raw.get(name, "")) for name in FORM_FIELDS}


def _render_form(
    request: Request,
    user: User,
    form: PassengerForm,
    *,
    passenger: Passenger | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return render(
        request,
        "passenger_form.html",
        user,
        # Ошибки валидации — это 422, а не 200: так их видно в логах и в тестах.
        status_code=422 if (form.errors or error) else 200,
        form=form,
        passenger=passenger,
        error=error,
        genders=GENDERS,
        pax_types=PAX_TYPES,
    )


@router.get("", response_class=HTMLResponse)
def list_passengers(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = db.query(Passenger)

    search = q.strip()
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                Passenger.first_name.ilike(pattern),
                Passenger.last_name.ilike(pattern),
                Passenger.doc_number.ilike(pattern),
            )
        )

    passengers = query.order_by(Passenger.last_name, Passenger.first_name).all()

    return render(
        request,
        "passengers.html",
        user,
        passengers=passengers,
        query=search,
        describe_age=_describe_age,
    )


@router.get("/new", response_class=HTMLResponse)
def new_passenger(
    request: Request,
    user: User = Depends(require_user),
) -> HTMLResponse:
    return _render_form(request, user, PassengerForm())


@router.post("/new")
async def create_passenger(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    form = clean_passenger_form(await _form_data(request))
    if not form.is_valid:
        return _render_form(request, user, form)

    passenger = Passenger(**form.as_dict(), created_by_id=user.id)
    db.add(passenger)
    try:
        db.commit()
    except IntegrityError:
        # Единственное ограничение уникальности — номер документа.
        db.rollback()
        return _render_form(
            request,
            user,
            form,
            error=f"Пассажир с документом {form.doc_number} уже есть в списке",
        )

    return RedirectResponse("/passengers", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{passenger_id}", response_class=HTMLResponse)
def edit_passenger(
    passenger_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    passenger = db.get(Passenger, passenger_id)
    if passenger is None:
        return render(
            request,
            "not_found.html",
            user,
            status_code=404,
            title="Пассажир не найден",
            back_url="/passengers",
        )
    return _render_form(request, user, _form_from_passenger(passenger), passenger=passenger)


@router.post("/{passenger_id}")
async def update_passenger(
    passenger_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    passenger = db.get(Passenger, passenger_id)
    if passenger is None:
        return render(
            request,
            "not_found.html",
            user,
            status_code=404,
            title="Пассажир не найден",
            back_url="/passengers",
        )

    form = clean_passenger_form(await _form_data(request))
    if not form.is_valid:
        return _render_form(request, user, form, passenger=passenger)

    for field, value in form.as_dict().items():
        setattr(passenger, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render_form(
            request,
            user,
            form,
            passenger=passenger,
            error=f"Документ {form.doc_number} уже записан на другого пассажира",
        )

    return RedirectResponse("/passengers", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{passenger_id}/delete")
def delete_passenger(
    passenger_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    passenger = db.get(Passenger, passenger_id)
    if passenger is None:
        return RedirectResponse("/passengers", status_code=status.HTTP_303_SEE_OTHER)

    # Пассажир может быть завязан на процесс — в том числе чужой и работающий.
    # Удалить его молча значит оставить в базе процесс без пассажира.
    used = db.query(JobPassenger).filter(JobPassenger.passenger_id == passenger_id).count()
    if used:
        return render(
            request,
            "passenger_form.html",
            user,
            status_code=409,
            form=_form_from_passenger(passenger),
            passenger=passenger,
            error=(
                f"Пассажир участвует в {used} процесс(ах) — удалить нельзя. "
                f"Сначала удали или заверши эти процессы."
            ),
            genders=GENDERS,
            pax_types=PAX_TYPES,
        )

    db.delete(passenger)
    db.commit()
    return RedirectResponse("/passengers", status_code=status.HTTP_303_SEE_OTHER)
