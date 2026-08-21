"""Тесты броней: чтение журнала бота, блокировка и её снятие.

Журнал бота — источник правды: именно он не даёт забронировать дважды.
Панель его читает и умеет снимать блокировку, поэтому проверяется в первую
очередь то, что стоит денег: не приписываются ли чужие брони этому процессу
и не снимается ли блокировка без явного решения человека.

Запуск (из корня репозитория):
    python run_tests.py web/test_bookings.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    # Журналы — во временную папку, а не в рабочие каталоги ботов.
    journals = tmp_path / "journals"
    journals.mkdir()
    monkeypatch.setattr(
        bookings_module,
        "journal_path",
        lambda service: journals / f"{service}_bookings.json",
    )

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


def _job(db, *, service: str = "railway", started: dt.datetime | None = None):
    from web.models import Job, JobPassenger, JobStatus, Passenger, Role, User
    from web.security import hash_password

    user = db.query(User).first()
    if user is None:
        user = User(login="t", password_hash=hash_password("x" * 8), role=Role.USER.value)
        db.add(user)
        db.flush()

    pax = Passenger(
        first_name="ANVAR", last_name="TEST", dob="01.01.1990", gender="M",
        doc_number=f"AA{db.query(Passenger).count():07d}",
    )
    db.add(pax)
    db.flush()

    job = Job(
        user_id=user.id, service=service, source="17", destination="49",
        depart_date="10.08.2026", status=JobStatus.RUNNING.value,
        started_at=started or dt.datetime.now() - dt.timedelta(minutes=1),
    )
    db.add(job)
    db.flush()
    db.add(JobPassenger(job_id=job.id, passenger_id=pax.id, position=0))
    db.commit()
    return job


def _write_journal(service: str, records: list[dict]) -> None:
    """Записать журнал так, как это делает сам бот."""
    from core.booking_guard import BookingGuard
    from web.services.bookings import journal_path

    guard = BookingGuard(journal_path(service))
    for record in records:
        key = record["key"]
        guard.begin(key)
        status = record.get("status", "done")
        if status == "done":
            guard.settle(key, ok=True,
                         booking_number=record.get("number", ""),
                         form_url=record.get("url", ""))
        elif status == "failed":
            guard.settle(key, ok=False, detail=record.get("detail", "отказ"))
        elif status == "unknown":
            guard.mark_unknown(key, record.get("detail", "ответ потерян"))
        # pending остаётся как есть после begin()


# --------------------------------------------------------------------------
# Чтение журнала
# --------------------------------------------------------------------------


def test_successful_booking_is_picked_up(db) -> None:
    from web.services.bookings import sync_from_journal

    job = _job(db)
    _write_journal("railway", [{"key": "k1", "number": "RW-77", "url": "https://bank.tm/1"}])

    created = sync_from_journal(db, job)

    assert len(created) == 1
    assert created[0].status == "done"
    assert created[0].booking_number == "RW-77"
    assert created[0].payment_url == "https://bank.tm/1"


def test_unknown_booking_is_picked_up(db) -> None:
    """Неопределённая попытка тоже должна попасть в панель — она блокирует запуск."""
    from web.services.bookings import sync_from_journal

    job = _job(db)
    _write_journal("railway", [{"key": "k1", "status": "unknown", "detail": "таймаут"}])

    created = sync_from_journal(db, job)
    assert created[0].status == "unknown"
    assert "таймаут" in created[0].detail


def test_sync_is_idempotent(db) -> None:
    """Повторный вызов не должен создавать дубли — иначе письмо уйдёт дважды."""
    from web.models import Booking
    from web.services.bookings import sync_from_journal

    job = _job(db)
    _write_journal("railway", [{"key": "k1", "number": "RW-1", "url": "https://bank.tm/1"}])

    first = sync_from_journal(db, job)
    second = sync_from_journal(db, job)

    assert len(first) == 1
    assert second == []
    assert db.query(Booking).count() == 1


def test_old_attempts_are_not_attributed_to_this_job(db) -> None:
    """
    Журнал общий для всех запусков сервиса.

    Попытка, начатая до старта процесса, — из прошлого запуска, и приписывать
    её текущему нельзя: иначе человек получит письмо о старой броне.
    """
    from web.services.bookings import sync_from_journal

    _write_journal("railway", [{"key": "old", "number": "RW-OLD", "url": "https://bank.tm/old"}])

    # Процесс стартовал ПОСЛЕ того, как запись появилась.
    job = _job(db, started=dt.datetime.now() + dt.timedelta(minutes=5))

    assert sync_from_journal(db, job) == []


def test_missing_journal_is_not_an_error(db) -> None:
    """Бот мог ни разу не дойти до брони — файла просто нет."""
    from web.services.bookings import sync_from_journal

    job = _job(db)
    assert sync_from_journal(db, job) == []


# --------------------------------------------------------------------------
# Блокировка
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["done", "unknown", "pending"])
def test_these_statuses_block(db, status: str) -> None:
    """Эти три состояния бот считает поводом не бронировать снова."""
    from web.services.bookings import blocking_attempt

    _write_journal("railway", [{"key": "k1", "status": status}])
    assert blocking_attempt("railway") is not None


def test_failed_does_not_block(db) -> None:
    """Достоверный отказ сервера — брони нет, мешать следующему запуску нечему."""
    from web.services.bookings import blocking_attempt

    _write_journal("railway", [{"key": "k1", "status": "failed"}])
    assert blocking_attempt("railway") is None


def test_no_journal_no_block(db) -> None:
    from web.services.bookings import blocking_attempt

    assert blocking_attempt("railway") is None


def test_services_block_independently(db) -> None:
    """Заблокированный поезд не должен мешать искать самолёт."""
    from web.services.bookings import blocking_attempt

    _write_journal("railway", [{"key": "k1", "status": "unknown"}])
    assert blocking_attempt("railway") is not None
    assert blocking_attempt("airlines") is None


def test_start_is_refused_while_blocked(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Панель обязана сказать заранее, а не дать боту молча выйти через секунду."""
    from web.services.runner import JobStartError, start_job

    _write_journal("railway", [{"key": "k1", "status": "unknown"}])
    job = _job(db)

    with pytest.raises(JobStartError) as exc:
        start_job(db, job)
    assert "заблокирована" in str(exc.value)


# --------------------------------------------------------------------------
# Снятие блокировки
# --------------------------------------------------------------------------


def test_unblock_clears_journal(db) -> None:
    from web.services.bookings import blocking_attempt, unblock_service

    _write_journal("railway", [{"key": "k1", "status": "unknown"}])
    unblock_service(db, "railway")

    assert blocking_attempt("railway") is None


def test_unblock_marks_bookings_but_keeps_them(db) -> None:
    """История нужна: что было, видно и после снятия блокировки."""
    from web.models import Booking
    from web.services.bookings import sync_from_journal, unblock_service

    job = _job(db)
    _write_journal("railway", [{"key": "k1", "status": "unknown"}])
    sync_from_journal(db, job)

    unblock_service(db, "railway")

    booking = db.query(Booking).one()
    assert "снята вручную" in booking.detail


def test_unblock_single_key(db) -> None:
    """Можно снять одну запись, не трогая остальные."""
    from web.services.bookings import blocking_attempt, unblock_service

    _write_journal("railway", [
        {"key": "k1", "status": "unknown"},
        {"key": "k2", "status": "done"},
    ])

    unblock_service(db, "railway", guard_key="k1")
    remaining = blocking_attempt("railway")

    assert remaining is not None and remaining.booking_key == "k2"


def test_unblock_unknown_key_is_reported(db) -> None:
    from web.services.bookings import unblock_service

    _write_journal("railway", [{"key": "k1", "status": "done"}])
    assert "нет" in unblock_service(db, "railway", guard_key="какой-то-другой")


def test_unblock_without_journal(db) -> None:
    from web.services.bookings import unblock_service

    assert "пуст" in unblock_service(db, "railway")


def test_unblock_lets_job_start_again(db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Полный круг: заблокировано → снято → запускается."""
    import web.services.bot_env as bot_env
    import web.services.runner as runner
    from web.services.bookings import unblock_service
    from web.services.runner import start_job, stop_job

    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    monkeypatch.setattr(bot_env, "RUNS_DIR", runs)

    script = tmp_path / "fake.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    real = bot_env.prepare_launch

    def fake(session, job):
        launch = real(session, job)
        launch.script = script
        launch.cwd = script.parent
        return launch

    monkeypatch.setattr(runner, "prepare_launch", fake)

    _write_journal("railway", [{"key": "k1", "status": "unknown"}])
    job = _job(db)

    unblock_service(db, "railway")
    start_job(db, job)  # не должно бросить
    stop_job(db, job, timeout=5)


# --------------------------------------------------------------------------
# Текст уведомления
# --------------------------------------------------------------------------


def test_notification_contains_payment_link() -> None:
    from web.services.notifications import format_booking_message

    subject, body, sms = format_booking_message(
        route="Ашхабад → Туркменабад, 10.08.2026",
        payment_url="https://bank.tm/pay/77",
        booking_number="RW-77",
        passengers="TEST ANVAR",
    )

    assert "RW-77" in subject
    assert "https://bank.tm/pay/77" in body
    assert "TEST ANVAR" in body
    assert "https://bank.tm/pay/77" in sms
