"""Дымовой тест оформления: каждая страница открывается и рисует общий каркас.

Ловит то, что не видно в тестах логики: опечатку в шаблоне, макрос без
импорта, переменную, которую роут перестал передавать. Такая ошибка роняет
страницу целиком (500), а заметили бы её только в браузере.

Запуск (из корня репозитория):
    python run_tests.py web/test_pages.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import captcha_dataset_api
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module
    import web.services.bookings as bookings_module

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)
    monkeypatch.setattr(captcha_dataset_api, "DATASET_DIR", tmp_path / "dataset")
    journals = tmp_path / "journals"
    journals.mkdir()
    monkeypatch.setattr(bookings_module, "journal_path",
                        lambda service: journals / f"{service}_bookings.json")

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


def _seed(db):
    """Немного всего: процессы во всех статусах, бронь, пассажир, станции, ошибка."""
    from web.models import (
        Booking, ErrorEvent, Job, JobLog, JobPassenger, Passenger, Role, Station, User,
    )
    from web.security import hash_password

    admin = User(login="root", password_hash=hash_password("x" * 8), role=Role.ADMIN.value)
    db.add(admin)
    db.flush()
    pax = Passenger(first_name="ANNA", last_name="TEST", dob="01.01.1990", gender="F",
                    doc_number="AA1")
    db.add(pax)
    db.add_all([
        Station(service="railway", external_id="17", title_ru="Ашхабад", code="17"),
        Station(service="railway", external_id="32", title_ru="Мары", code="32"),
    ])
    db.flush()
    jobs = []
    for status in ("queued", "running", "booked", "needs_review", "failed", "stopped"):
        job = Job(user_id=admin.id, service="railway", source="17", destination="32",
                  depart_date="20.10.2026", status=status)
        job.params = {"wagon_type_id": "3,2", "multimedia": True}
        db.add(job)
        db.flush()
        db.add(JobPassenger(job_id=job.id, passenger_id=pax.id, position=0))
        db.add(JobLog(job_id=job.id, level="ok", text="[ok] ЕСТЬ места", ts=dt.datetime.now()))
        jobs.append(job)
    db.add(Booking(job_id=jobs[2].id, service="railway", status="done", guard_key="g",
                   booking_number="YM85UZ", payment_url="https://pay.example/1"))
    db.add(ErrorEvent(job_id=jobs[1].id, key="network", service="airlines", detail="timeout"))
    db.commit()
    return admin, jobs, pax


def _request(path: str):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": path, "headers": [],
                    "query_string": b""})


def _html(response) -> str:
    assert response.status_code == 200, response.status_code
    return response.body.decode("utf-8")


def test_every_page_renders_with_the_shell(db) -> None:
    from web.routes import (
        bookings, captcha, dashboard, errors, jobs, passengers, settings, stations, users,
    )

    admin, job_list, pax = _seed(db)
    pages = {
        "/": lambda: dashboard.dashboard(_request("/"), user=admin, db=db),
        "/jobs": lambda: jobs.list_jobs(_request("/jobs"), user=admin, db=db),
        "/jobs/new": lambda: jobs.new_job(_request("/jobs/new"), service="airlines", user=admin, db=db),
        "/jobs/1": lambda: jobs.job_detail(job_list[1].id, _request("/jobs/1"), user=admin, db=db),
        "/bookings": lambda: bookings.list_bookings(_request("/bookings"), user=admin, db=db),
        "/passengers": lambda: passengers.list_passengers(_request("/passengers"), q="", user=admin, db=db),
        "/passengers/1": lambda: passengers.edit_passenger(pax.id, _request("/passengers/1"), user=admin, db=db),
        "/stations": lambda: stations.list_stations(_request("/stations"), user=admin, db=db),
        "/captcha": lambda: captcha.captcha_page(_request("/captcha"), user=admin, db=db),
        "/errors": lambda: errors.errors_page(_request("/errors"), provider="", key="", user=admin, db=db),
        "/settings": lambda: settings.settings_page(_request("/settings"), user=admin, db=db),
        "/users": lambda: users.list_users(_request("/users"), user=admin, db=db),
    }
    for path, call in pages.items():
        html = _html(call())
        assert 'class="sidebar"' in html, path
        assert 'id="i-home"' in html, f"{path}: нет спрайта значков"
        assert "app.css?v=" in html, f"{path}: стили без версии — браузер возьмёт старые из кеша"


def test_statuses_are_words_not_codes(db) -> None:
    """Код needs_review человеку ничего не говорит — в списке должны быть слова."""
    from web.routes import jobs

    admin, _, _ = _seed(db)
    html = _html(jobs.list_jobs(_request("/jobs"), user=admin, db=db))
    for word in ("ожидает", "идёт поиск", "готово", "бронь под вопросом", "ошибка", "остановлен"):
        assert word in html, word
    assert ">needs_review<" not in html


def test_routes_show_station_names(db) -> None:
    from web.routes import dashboard

    admin, _, _ = _seed(db)
    html = _html(dashboard.dashboard(_request("/"), user=admin, db=db))
    assert "Ашхабад" in html and "Мары" in html


def test_dashboard_points_at_what_needs_attention(db) -> None:
    from web.routes import dashboard

    admin, job_list, _ = _seed(db)
    html = _html(dashboard.dashboard(_request("/"), user=admin, db=db))
    assert "Требует внимания" in html
    assert f"/jobs/{job_list[3].id}" in html  # процесс с бронью под вопросом


def test_job_params_are_titled(db) -> None:
    from web.routes import jobs

    admin, job_list, _ = _seed(db)
    html = _html(jobs.job_detail(job_list[0].id, _request("/jobs/1"), user=admin, db=db))
    assert "Тип вагона" in html and "Плацкарт, иначе купе" in html
    assert ">wagon_type_id<" not in html


def test_login_page_has_no_menu() -> None:
    from web.routes import auth

    html = _html(auth.login_form(_request("/login"), next="/", user=None))
    assert 'class="login-card"' in html
    assert 'class="sidebar"' not in html
