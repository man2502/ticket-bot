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


# --------------------------------------------------------------------------
# Брони без процесса
# --------------------------------------------------------------------------


def test_journal_booking_without_job_is_picked_up(db) -> None:
    """
    Бота могли запустить из командной строки. Бронь при этом настоящая:
    у перевозчика она есть, счёт выставлен, ссылка лежит в журнале. Раньше
    панель её не видела вовсе, и человек узнавал о ней, только открыв JSON.
    """
    from web.services.bookings import sync_orphan_bookings

    _write_journal("airlines", [{"key": "k1", "number": "46MBJ0", "url": "https://bank.tm/1"}])

    created = sync_orphan_bookings(db)

    assert len(created) == 1
    assert created[0].job_id is None
    assert created[0].booking_number == "46MBJ0"


def test_orphan_sync_is_idempotent(db) -> None:
    """Обход зовётся при каждом открытии страницы — письмо не должно уходить дважды."""
    from web.models import Booking
    from web.services.bookings import sync_orphan_bookings

    _write_journal("airlines", [{"key": "k1", "number": "A1", "url": "https://bank.tm/1"}])

    assert len(sync_orphan_bookings(db)) == 1
    assert sync_orphan_bookings(db) == []
    assert db.query(Booking).count() == 1


def test_orphan_notification_carries_the_link(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.services.bookings import notify_orphan_bookings
    import web.services.notifications as notifications

    sent = []
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda db, *, to, subject, body: sent.append(body)
        or notifications.SendResult(channel="email", ok=True, recipient=to),
    )
    monkeypatch.setattr(notifications, "send_sms", lambda db, **k: None)
    from web.services.settings import save_settings

    save_settings(db, {"default_notify_email": "pay@example.com"})
    _write_journal("airlines", [{"key": "k1", "number": "A1", "url": "https://bank.tm/pay"}])

    notify_orphan_bookings(db)

    assert len(sent) == 1
    assert "https://bank.tm/pay" in sent[0]


def test_orphan_without_link_is_not_announced(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Отказ и «под вопросом» в панель попадают, но письмом о них не трубим:
    ссылки нет, платить нечего, а тревогу человек увидит в разделе «Брони».
    """
    from web.models import Booking
    from web.services.bookings import notify_orphan_bookings
    import web.services.notifications as notifications

    sent = []
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda db, **k: sent.append(1)
        or notifications.SendResult(channel="email", ok=True, recipient="x"),
    )
    monkeypatch.setattr(notifications, "send_sms", lambda db, **k: None)

    _write_journal("airlines", [{"key": "k1", "status": "failed", "detail": "Error Create"}])
    notify_orphan_bookings(db)

    assert sent == []
    assert db.query(Booking).count() == 1


def test_job_adopts_its_orphan_instead_of_duplicating(db) -> None:
    """
    Обход журнала мог опередить процесс. Создать вторую запись значило бы
    отправить человеку второе письмо про ту же бронь.
    """
    from web.models import Booking
    from web.services.bookings import sync_from_journal, sync_orphan_bookings

    job = _job(db, service="railway")
    _write_journal("railway", [{"key": "k1", "number": "RW-1", "url": "https://bank.tm/1"}])

    sync_orphan_bookings(db)
    created = sync_from_journal(db, job)

    assert created == [], "процесс не должен создавать вторую запись"
    assert db.query(Booking).count() == 1
    assert db.query(Booking).one().job_id == job.id, "бронь должна прирасти к процессу"


def test_foreign_orphan_is_not_adopted(db) -> None:
    """Попытка из прошлого запуска остаётся бесхозной — она не этого процесса."""
    from web.models import Booking
    from web.services.bookings import sync_from_journal, sync_orphan_bookings

    _write_journal("railway", [{"key": "k1", "number": "RW-1", "url": "https://bank.tm/1"}])
    sync_orphan_bookings(db)

    job = _job(db, service="railway", started=dt.datetime.now() + dt.timedelta(hours=1))
    assert sync_from_journal(db, job) == []
    assert db.query(Booking).one().job_id is None


def test_deleting_a_job_keeps_the_booking(db) -> None:
    """
    Удаление поиска не должно стирать бронь: она есть у перевозчика, за неё
    выставлен счёт, и вместе с ней исчезла бы ссылка на оплату.
    """
    from web.models import Booking
    from web.services.bookings import sync_from_journal
    from web.services.runner import delete_job

    job = _job(db, service="railway")
    _write_journal("railway", [{"key": "k1", "number": "RW-1", "url": "https://bank.tm/1"}])
    sync_from_journal(db, job)

    delete_job(db, job)

    booking = db.query(Booking).one()
    assert booking.job_id is None
    assert booking.payment_url == "https://bank.tm/1"


def test_deleted_job_booking_is_not_announced_again(db) -> None:
    """
    После удаления процесса запись остаётся, и обход журнала не должен
    посчитать её новой — иначе письмо уходило бы после каждой уборки.
    """
    from web.services.bookings import sync_from_journal, sync_orphan_bookings
    from web.services.runner import delete_job

    job = _job(db, service="railway")
    _write_journal("railway", [{"key": "k1", "number": "RW-1", "url": "https://bank.tm/1"}])
    sync_from_journal(db, job)
    delete_job(db, job)

    assert sync_orphan_bookings(db) == []


def test_old_database_allows_bookings_without_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Пересборка таблицы не должна ломать внешний ключ из журнала отправок.

    Первая версия миграции переименовывала старую таблицу, и SQLite переписывал
    ссылку в `notification_logs` на временное имя. После удаления временной
    таблицы любая запись в журнал падала с «no such table».
    """
    import sqlite3

    import web.config as config
    import web.crypto as crypto
    import web.db as db_module

    old_db = tmp_path / "old.db"
    con = sqlite3.connect(old_db)
    con.executescript(
        """
        CREATE TABLE jobs (id INTEGER NOT NULL PRIMARY KEY);
        CREATE TABLE bookings (
            id INTEGER NOT NULL PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            service VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL,
            guard_key VARCHAR(64) NOT NULL,
            booking_number VARCHAR(64) NOT NULL,
            payment_url TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE notification_logs (
            id INTEGER NOT NULL PRIMARY KEY,
            booking_id INTEGER REFERENCES bookings(id),
            channel VARCHAR(8) NOT NULL,
            recipient VARCHAR(255) NOT NULL,
            subject VARCHAR(255) NOT NULL,
            is_sent BOOLEAN NOT NULL,
            error TEXT NOT NULL,
            created_at DATETIME NOT NULL
        );
        INSERT INTO jobs VALUES (1);
        INSERT INTO bookings VALUES
            (1, 1, 'railway', 'done', 'k1', 'RW-1', 'https://bank.tm/1', '', '2026-08-20 12:00:00');
        """
    )
    con.commit()
    con.close()

    monkeypatch.setenv("TICKET_DB_PATH", str(old_db))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    db_module.init_db()

    con = sqlite3.connect(old_db)
    job_id = [row for row in con.execute("PRAGMA table_info(bookings)") if row[1] == "job_id"][0]
    assert not job_id[3], "NOT NULL должен уйти"

    # Данные на месте.
    assert con.execute("SELECT booking_number FROM bookings").fetchone()[0] == "RW-1"
    # Ссылка из журнала отправок ведёт в существующую таблицу.
    targets = {row[2] for row in con.execute("PRAGMA foreign_key_list(notification_logs)")}
    assert targets == {"bookings"}
    con.execute(
        "INSERT INTO notification_logs (booking_id, channel, recipient, subject, "
        "is_sent, error, created_at) VALUES (1, 'email', 'a@b.c', 's', 1, '', '2026-08-20')"
    )
    con.commit()
    assert list(con.execute("PRAGMA foreign_key_check")) == []
    con.close()


# --------------------------------------------------------------------------
# Новая бронь по тому же ключу
# --------------------------------------------------------------------------


def _rebook(service: str, key: str, *, number: str, url: str) -> None:
    """Снять блокировку и забронировать заново — как это делает --force-rebook."""
    from core.booking_guard import BookingGuard
    from web.services.bookings import journal_path

    guard = BookingGuard(journal_path(service))
    guard.clear_all()
    guard.begin(key)
    guard.settle(key, ok=True, booking_number=number, form_url=url)


def test_second_booking_with_the_same_key_is_picked_up(db) -> None:
    """
    Ключ брони — это «пассажир + рейс + дата», а не конкретная бронь.

    После снятия блокировки по тому же ключу появляется НОВАЯ бронь с новым
    номером. Панель, сверявшая только ключ, считала её уже известной
    и молчала — так потерялась 46MEMB, хотя письмо про неё никто не получил.
    """
    from web.models import Booking
    from web.services.bookings import sync_from_journal

    job = _job(db, service="airlines")
    _write_journal("airlines", [{"key": "k1", "number": "46MBJ0", "url": "https://bank.tm/1"}])
    sync_from_journal(db, job)

    _rebook("airlines", "k1", number="46MEMB", url="https://bank.tm/2")
    created = sync_from_journal(db, job)

    assert [b.booking_number for b in created] == ["46MEMB"]
    assert db.query(Booking).count() == 2, "обе брони должны остаться в панели"


def test_same_record_twice_is_still_skipped(db) -> None:
    """Сверка по содержимому не должна ломать защиту от повторной рассылки."""
    from web.models import Booking
    from web.services.bookings import sync_from_journal

    job = _job(db, service="airlines")
    _write_journal("airlines", [{"key": "k1", "number": "46MBJ0", "url": "https://bank.tm/1"}])

    assert len(sync_from_journal(db, job)) == 1
    assert sync_from_journal(db, job) == []
    assert db.query(Booking).count() == 1


def test_pending_that_became_done_is_updated_not_duplicated(db) -> None:
    """
    `pending`, ставший `done`, — это та же попытка, а не вторая бронь.

    Плодить на неё вторую строку значило бы показать человеку две брони
    там, где она одна.
    """
    from core.booking_guard import BookingGuard
    from web.models import Booking
    from web.services.bookings import journal_path, sync_from_journal

    job = _job(db, service="airlines")
    guard = BookingGuard(journal_path("airlines"))
    guard.begin("k1")

    first = sync_from_journal(db, job)
    assert [b.status for b in first] == ["pending"]

    guard.settle("k1", ok=True, booking_number="46MEMB", form_url="https://bank.tm/2")
    second = sync_from_journal(db, job)

    assert db.query(Booking).count() == 1, "должна быть одна бронь, а не две"
    row = db.query(Booking).one()
    assert row.status == "done"
    assert row.booking_number == "46MEMB"
    # Ссылка появилась только сейчас — про неё нужно сообщить.
    assert second == [row]


def test_pending_without_link_is_not_announced_twice(db) -> None:
    """Пока ссылки нет, сообщать не о чем — и повторно тоже."""
    from core.booking_guard import BookingGuard
    from web.services.bookings import journal_path, sync_from_journal

    job = _job(db, service="airlines")
    guard = BookingGuard(journal_path("airlines"))
    guard.begin("k1")
    sync_from_journal(db, job)

    guard.settle("k1", ok=False, detail="мест нет")
    created = sync_from_journal(db, job)

    assert created == [], "отказ без ссылки — не повод для письма"


def test_rebooked_pair_notifies_only_about_the_new_one(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Письмо должно уйти по новой броне и только по ней."""
    import web.services.notifications as notifications
    from web.services.runner import _collect_bookings

    subjects = []
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda db, *, to, subject, body: subjects.append(subject)
        or notifications.SendResult(channel="email", ok=True, recipient=to),
    )
    monkeypatch.setattr(notifications, "send_sms", lambda db, **k: None)
    from web.services.settings import save_settings

    save_settings(db, {"default_notify_email": "pay@example.com"})

    job = _job(db, service="airlines")
    _write_journal("airlines", [{"key": "k1", "number": "46MBJ0", "url": "https://bank.tm/1"}])
    _collect_bookings(db, job)
    subjects.clear()

    _rebook("airlines", "k1", number="46MEMB", url="https://bank.tm/2")
    _collect_bookings(db, job)

    assert len(subjects) == 1
    assert "46MEMB" in subjects[0]
