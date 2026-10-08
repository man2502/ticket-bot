"""Тесты журнала ошибок: бот печатает `[err]`, панель сохраняет и сводит.

Запуск (из корня репозитория):
    python run_tests.py web/test_errors.py
"""

from __future__ import annotations

import datetime as dt
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "AIRLINES_API_DIR", tmp_path / "airlines_api")
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


def _provider(db, name: str):
    from web.services.providers import save_provider

    return save_provider(db, None, {"name": name, "login": f"+99365{len(name):06d}",
                                    "password": "x", "is_enabled": True})


def _job(db, provider):
    from web.models import Job, Role, User
    from web.security import hash_password

    user = db.query(User).first()
    if user is None:
        user = User(login="t", password_hash=hash_password("x" * 8), role=Role.USER.value)
        db.add(user)
        db.flush()
    job = Job(user_id=user.id, service="airlines", source="ASB", destination="MYP",
              depart_date="18.09.2026")
    job.provider = provider
    db.add(job)
    db.commit()
    return job


def _bot_line(**kwargs) -> str:
    """Строка ровно так, как её печатает бот."""
    from api_errors import report_error

    out = io.StringIO()
    with redirect_stdout(out):
        report_error(**kwargs)
    return out.getvalue().strip()


# --------------------------------------------------------------------------- #
# Формат
# --------------------------------------------------------------------------- #


def test_bot_line_round_trips_through_the_parser() -> None:
    from web.services.runner import parse_error_line

    line = _bot_line(key="booking_failed", detail="Inform (HTTP 400)",
                     status=400, endpoint="CreateBooking")
    parsed = parse_error_line(line)
    assert parsed == {"key": "booking_failed", "status": 400,
                      "endpoint": "CreateBooking", "detail": "Inform (HTTP 400)"}


def test_pipe_in_detail_does_not_break_parsing() -> None:
    """Разделитель внутри подробностей сдвинул бы поля."""
    from web.services.runner import parse_error_line

    line = _bot_line(key="other", detail="a | b | c", endpoint="x")
    assert parse_error_line(line)["detail"] == "a / b / c"


def test_unknown_key_becomes_other() -> None:
    assert _bot_line(key="что-то новое", detail="x").startswith("[err] other |")


def test_rate_limit_line_keeps_retry_after() -> None:
    """Строка 429 из лестницы: Retry-After должен дожить до журнала."""
    from web.services.runner import parse_error_line

    line = ("[err] rate_limit | HTTP 429 | customers/info | Retry-After=900с | "
            "ступень 1 (2 мин), ждём 15.0 мин")
    parsed = parse_error_line(line)
    assert parsed["status"] == 429
    assert parsed["endpoint"] == "customers/info"
    assert "Retry-After=900с" in parsed["detail"]


def test_colored_line_is_parsed() -> None:
    from web.services.runner import parse_error_line

    assert parse_error_line("\x1b[91m[err] network | — | — | timeout\x1b[0m")["key"] == "network"


def test_ordinary_lines_are_ignored() -> None:
    from web.services.runner import parse_error_line

    for line in ("[ok] ЕСТЬ места", "[--] мест нет", "[error] старый формат", ""):
        assert parse_error_line(line) is None


@pytest.mark.parametrize(
    "text,key",
    [
        ("customers/info http 429", "rate_limit"),
        ("Error Search (HTTP 400)", "error_search"),
        ("captcha failed", "captcha_failed"),
        ("403 forbidden", "auth_forbidden"),
        ("сеть: timeout", "network"),
        # Так пишет ssl при зависшем ответе — раньше уходило в «прочее».
        ("POST /api/v1/GetAirAvailability (20012 мс): The read operation timed out", "network"),
        ("HTTP 502 bad gateway", "http_5xx"),
        ("сервис на профилактике", "maintenance"),
        ("что-то невиданное", "other"),
    ],
)
def test_error_text_is_classified(text: str, key: str) -> None:
    from api_errors import classify_error_text

    assert classify_error_text(text) == key


def test_endpoint_is_taken_from_network_error_text() -> None:
    """Сетевая ошибка приходит текстом — запрос всё равно должен попасть в колонку."""
    from web.services.runner import parse_error_line

    line = _bot_line(key="network", detail="POST /api/v1/GetAirAvailability (20012 мс, "
                                            "попытка 1): The read operation timed out")
    assert parse_error_line(line)["endpoint"] == "GetAirAvailability"
    line = _bot_line(key="network", detail="сеть: GET /api/v1/customers/info (3014 мс): timed out")
    assert parse_error_line(line)["endpoint"] == "customers/info"


def test_explicit_endpoint_wins() -> None:
    from web.services.runner import parse_error_line

    line = _bot_line(key="network", detail="GET /api/v1/customers/info: x", endpoint="auth/login")
    assert parse_error_line(line)["endpoint"] == "auth/login"


# --------------------------------------------------------------------------- #
# Запись и сводка
# --------------------------------------------------------------------------- #


def test_error_line_from_process_is_stored_with_account(db) -> None:
    from web.models import ErrorEvent
    from web.services.runner import _record_error_event

    provider = _provider(db, "Второй")
    job = _job(db, provider)
    _record_error_event(db, job.id, _bot_line(key="error_search", detail="hitit_error",
                                              status=400, endpoint="GetAirAvailability"))
    db.commit()

    event = db.query(ErrorEvent).one()
    assert event.provider_id == provider.id
    assert event.job_id == job.id
    assert event.key == "error_search"
    assert event.service == "airlines"


def test_summary_shows_which_account_had_which_errors(db) -> None:
    """Ради этого всё и затевалось: «у аккаунта 2 сегодня семь 429, у аккаунта 1 ни одного»."""
    from web.models import ErrorEvent
    from web.services.errors import summary

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    for _ in range(7):
        db.add(ErrorEvent(provider_id=second.id, key="rate_limit", service="airlines"))
    db.add(ErrorEvent(provider_id=first.id, key="network", service="airlines"))
    # Позавчера — в «7 дней», но не в «24 часа».
    db.add(ErrorEvent(provider_id=first.id, key="rate_limit", service="airlines",
                      created_at=dt.datetime.now() - dt.timedelta(days=2)))
    db.commit()

    data = summary(db)
    titles = [c["title"] for c in data["columns"]]
    rate = next(r for r in data["rows"] if r["key"] == "rate_limit")
    by_account = dict(zip(titles, rate["cells"]))

    assert by_account["Второй"]["day"] == 7
    assert by_account["Первый"]["day"] == 0
    assert by_account["Первый"]["week"] == 1
    assert rate["total"]["week"] == 8
    # Самая частая сегодня ошибка — первой.
    assert data["rows"][0]["key"] == "rate_limit"


def test_errors_survive_job_deletion(db) -> None:
    """Сводка не должна терять ровно те случаи, после которых что-то удаляли."""
    from web.models import ErrorEvent
    from web.services.runner import delete_job

    job = _job(db, _provider(db, "Первый"))
    db.add(ErrorEvent(job_id=job.id, provider_id=job.provider_id, key="network"))
    db.commit()

    delete_job(db, job)
    db.expire_all()
    event = db.query(ErrorEvent).one()
    assert event.job_id is None


def test_old_errors_are_pruned(db) -> None:
    from web.models import ErrorEvent
    from web.services.errors import prune

    db.add(ErrorEvent(key="network", created_at=dt.datetime.now() - dt.timedelta(days=40)))
    db.add(ErrorEvent(key="network"))
    db.commit()

    assert prune(db) == 1
    assert db.query(ErrorEvent).count() == 1


def test_feed_filters_by_account_and_type(db) -> None:
    from web.models import ErrorEvent
    from web.services.errors import feed

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    db.add_all([
        ErrorEvent(provider_id=first.id, key="network"),
        ErrorEvent(provider_id=second.id, key="network"),
        ErrorEvent(provider_id=second.id, key="rate_limit"),
    ])
    db.commit()

    assert len(feed(db, provider_id=second.id)) == 2
    assert len(feed(db, key="network")) == 2
    assert len(feed(db, provider_id=second.id, key="rate_limit")) == 1


def test_errors_page_renders(db) -> None:
    from starlette.requests import Request

    from web.models import ErrorEvent, Role, User
    from web.routes.errors import errors_page
    from web.security import hash_password

    provider = _provider(db, "Второй")
    db.add(ErrorEvent(provider_id=provider.id, key="rate_limit", status=429,
                      endpoint="customers/info", detail="Retry-After=900с"))
    admin = User(login="root", password_hash=hash_password("x" * 8), role=Role.ADMIN.value)
    db.add(admin)
    db.commit()

    request = Request({"type": "http", "method": "GET", "path": "/errors",
                       "headers": [], "query_string": b""})
    html = errors_page(request, provider="", key="", user=admin, db=db).body.decode("utf-8")

    assert "лимит запросов (429)" in html
    assert "Второй" in html
    assert "Retry-After=900с" in html


def test_errors_page_is_in_the_menu_for_admins_only() -> None:
    from web.templating import NAV_ITEMS

    item = next(i for i in NAV_ITEMS if i["url"] == "/errors")
    assert item["admin_only"] is True
