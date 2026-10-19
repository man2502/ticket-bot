"""Тесты запуска процессов: подготовка окружения бота и правила очереди.

Настоящие боты здесь не запускаются — вместо них подставляется короткий
скрипт-заглушка. Проверяется то, что нельзя проверить глазами: попадают ли
в `check.env` правильные имена переменных, не утекает ли пароль лишним,
и не даст ли панель запустить два поиска по одному сервису разом.

Запуск (из корня репозитория):
    python run_tests.py web/test_runner.py
"""

from __future__ import annotations

import sys
import time
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
    import web.services.bot_env as bot_env

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    # Каталоги запусков — во временную папку, а не в рабочую web/data/runs.
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(bot_env, "RUNS_DIR", runs)

    # Журналы броней тоже во временную папку. Без этого тесты читали бы рабочий
    # `railway-api/railway_bookings.json`, и настоящая бронь в нём валила бы их
    # все разом: `start_job` справедливо отказывается запускать поиск, пока
    # незакрытая попытка не снята человеком.
    import web.services.bookings as bookings_module

    journals = tmp_path / "journals"
    journals.mkdir()
    monkeypatch.setattr(
        bookings_module,
        "journal_path",
        lambda service: journals / f"{service}_bookings.json",
    )

    db_module.init_db()
    with db_module.session_scope() as session:
        # Адрес для брони: без него панель не даёт запустить поиск по самолётам,
        # потому что сервер перевозчика отвергает CreateBooking без contactEmail.
        # У рабочей панели он заполнен, тесты стартуют из того же состояния.
        from web.services.settings import save_settings

        # Поездам нужен ещё и телефон (иначе input.invalidInputParam).
        save_settings(session, {"default_notify_email": "pay@example.com",
                                "default_notify_phone": "+99361000000"})
        yield session


def _make_job(db, service: str = "railway", **over):
    from web.models import Job, JobPassenger, Passenger, Role, User
    from web.security import hash_password

    user = db.query(User).first()
    if user is None:
        user = User(login="tester", password_hash=hash_password("x" * 8), role=Role.USER.value)
        db.add(user)
        db.flush()

    pax = Passenger(
        first_name="ANVAR",
        last_name="TEST",
        dob="01.01.1990",
        gender="M",
        doc_number=f"AA{db.query(Passenger).count():07d}",
    )
    db.add(pax)
    db.flush()

    data = dict(
        user_id=user.id,
        service=service,
        source="17" if service == "railway" else "ASB",
        destination="49" if service == "railway" else "MYP",
        depart_date="10.08.2026",
    )
    data.update(over)
    job = Job(**data)
    if service == "airlines" and "provider_id" not in over:
        # Процесс самолётов без аккаунта не запускается: в жизни его назначает
        # start_job, а тестам подготовки окружения нужен аккаунт сразу.
        job.provider = _provider(db)
    db.add(job)
    db.flush()
    db.add(JobPassenger(job_id=job.id, passenger_id=pax.id, position=0))
    db.commit()
    return job


def _provider(db, *, login: str = "+99361234567", password: str = "пароль-кабинета", **over):
    """Аккаунт перевозчика. Без аргументов — первый существующий или новый."""
    from web.models import Provider
    from web.services.providers import save_provider

    if not over and login == "+99361234567":
        existing = db.query(Provider).order_by(Provider.id).first()
        if existing is not None:
            return existing
    data = {"name": over.pop("name", ""), "login": login, "password": password,
            "is_enabled": over.pop("is_enabled", True)}
    data.update(over)
    return save_provider(db, None, data)


def _env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


# --------------------------------------------------------------------------
# Подготовка окружения: railway
# --------------------------------------------------------------------------


def test_railway_env_has_route(db) -> None:
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "railway")
    values = _env_values(prepare_launch(db, job).env_file)

    assert values["SOURCE_STATION_ID"] == "17"
    assert values["DESTINATION_STATION_ID"] == "49"
    assert values["DATE"] == "10.08.2026"


def test_railway_params_reach_env(db) -> None:
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "railway")
    job.params = {"wagon_type_id": "3,2", "train_index": "2", "multimedia": True, "strict": True}
    db.commit()

    values = _env_values(prepare_launch(db, job).env_file)
    assert values["WAGON_TYPE_ID"] == "3,2"
    assert values["TRAIN_INDEX"] == "2"
    assert values["RAILWAY_MULTIMEDIA"] == "1"
    assert values["RAILWAY_STRICT"] == "1"


def test_train_time_used_when_no_index(db) -> None:
    """Индекс и время — взаимоисключающие: бот выберет по тому, что задано."""
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "railway")
    job.params = {"train_time": "19:50"}
    db.commit()

    values = _env_values(prepare_launch(db, job).env_file)
    assert values["TRAIN_TIME"] == "19:50"
    assert "TRAIN_INDEX" not in values


def test_empty_params_are_omitted(db) -> None:
    """Пустой WAGON_TYPE_ID означает «любой» — переменную не пишем вовсе."""
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "railway")
    values = _env_values(prepare_launch(db, job).env_file)
    assert "WAGON_TYPE_ID" not in values


# --------------------------------------------------------------------------
# Подготовка окружения: airlines
# --------------------------------------------------------------------------


def test_airlines_credentials_are_decrypted(db) -> None:
    """В файл бота пароль должен попасть открытым — иначе он не войдёт."""
    from web.services.bot_env import prepare_launch
    from web.services.settings import save_service_config

    # Аккаунт — у провайдера, учётка приложения — общая в настройках сервиса.
    save_service_config(
        db,
        "airlines",
        {"extra_login": "ios", "extra_password_enc": "пароль-приложения"},
    )

    job = _make_job(db, "airlines")
    values = _env_values(prepare_launch(db, job).env_file)

    assert values["API_CUSTOMER_PHONE"] == "+99361234567"
    assert values["API_CUSTOMER_PASSWORD"] == "пароль-кабинета"
    assert values["API_IOS_PASSWORD"] == "пароль-приложения"


def test_captcha_is_automatic_by_default(db) -> None:
    """Процесс не должен ждать человека — так договаривались."""
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "airlines")
    values = _env_values(prepare_launch(db, job).env_file)
    assert values["CAPTCHA_API_MANUAL"] == "0"


def test_slots_follow_service_config(db) -> None:
    from web.services.bot_env import prepare_launch
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"check_period_sec": "120", "check_at_second": "7"})
    job = _make_job(db, "airlines")
    values = _env_values(prepare_launch(db, job).env_file)

    assert values["API_CHECK_PERIOD_SEC"] == "120"
    assert values["API_CHECK_AT_SEC"] == "7"


def test_flight_time_replaces_index(db) -> None:
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "airlines")
    job.params = {"flight_time": "09:40"}
    db.commit()

    values = _env_values(prepare_launch(db, job).env_file)
    assert values["FLIGHT_TIME"] == "09:40"
    assert "FLIGHT_INDEX" not in values


def test_bot_email_is_not_configured(db) -> None:
    """
    Письма шлёт панель, а не бот.

    Если передать боту GMAIL_*, человек получит два письма об одной брони,
    и настройка расползётся по двум местам.
    """
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "airlines")
    values = _env_values(prepare_launch(db, job).env_file)

    assert "GMAIL_USER" not in values
    assert "GMAIL_APP_PASSWORD" not in values


@pytest.mark.parametrize("service", ["railway", "airlines"])
def test_bot_is_told_notifications_are_external(db, service: str) -> None:
    """
    Оба сервиса получают NOTIFY_EXTERNAL=1.

    Без него бот на каждом запуске писал в лог «Gmail не настроен: нужны
    GMAIL_USER…». Настраивать там нечего — рассылает панель, — но человек
    видел это как ошибку и шёл искать несуществующую проблему.
    """
    from web.services.bot_env import prepare_launch

    job = _make_job(db, service)
    values = _env_values(prepare_launch(db, job).env_file)

    assert values["NOTIFY_EXTERNAL"] == "1"


def test_tokens_are_not_saved_into_module(db) -> None:
    """Панель не должна править файлы модуля бота."""
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "airlines")
    assert _env_values(prepare_launch(db, job).env_file)["API_SAVE_TOKENS"] == "0"


# --------------------------------------------------------------------------
# Пассажиры
# --------------------------------------------------------------------------


def test_passengers_file_is_readable_by_bot(db) -> None:
    """Файл должен читаться тем же загрузчиком, которым пользуются боты."""
    from passenger_loader import load_passengers_list
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "railway")
    launch = prepare_launch(db, job)
    values = _env_values(launch.env_file)

    loaded = load_passengers_list(Path(values["PASSENGERS_FILE"]))
    assert loaded[0]["doc_number"].startswith("AA")
    assert values["PASSENGER_IDS"] == str(loaded[0]["id"])


def test_job_without_passengers_is_rejected(db) -> None:
    from web.models import JobPassenger
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "railway")
    db.query(JobPassenger).filter(JobPassenger.job_id == job.id).delete()
    db.commit()
    db.refresh(job)

    with pytest.raises(ValueError):
        prepare_launch(db, job)


def test_each_job_gets_own_directory(db) -> None:
    """Два процесса не должны драться за один passengers.json."""
    from web.services.bot_env import prepare_launch

    first = prepare_launch(db, _make_job(db, "railway"))
    second = prepare_launch(db, _make_job(db, "airlines"))
    assert first.run_dir != second.run_dir


def test_multiline_value_does_not_break_env(db) -> None:
    """Перевод строки в пароле сломал бы формат файла — вырезаем."""
    from web.services.bot_env import prepare_launch
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"password_enc": "стро­ка1\nстрока2"})
    job = _make_job(db, "airlines")
    lines = prepare_launch(db, job).env_file.read_text(encoding="utf-8").splitlines()

    assert all(line.startswith("#") or "=" in line for line in lines if line)


# --------------------------------------------------------------------------
# Разбор уровня строк лога
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, level",
    [
        ("[ok] ЕСТЬ места — 19:50 поезд №03", "ok"),
        ("[error] trips → HTTP 500", "error"),
        ("[warn] Gmail НЕ отправлен", "warn"),
        ("[rate-limit] Too Many Requests — пауза 150с", "warn"),
        ("[--] мест нет", "info"),
        ("[railway] Ашхабад → Туркменабад", "info"),
    ],
)
def test_log_level_from_tag(line: str, level: str) -> None:
    from web.services.runner import classify

    assert classify(line) == level


# --------------------------------------------------------------------------
# Очередь и запуск
# --------------------------------------------------------------------------


def _fake_bot(tmp_path: Path, body: str) -> Path:
    """Скрипт вместо настоящего бота: печатает что просили и выходит."""
    script = tmp_path / "fake_bot.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n" + body,
        encoding="utf-8",
    )
    return script


def _patch_launch(monkeypatch: pytest.MonkeyPatch, script: Path) -> None:
    import web.services.bot_env as bot_env
    import web.services.runner as runner

    real = bot_env.prepare_launch

    def fake(db, job):
        launch = real(db, job)
        launch.script = script
        launch.cwd = script.parent
        return launch

    monkeypatch.setattr(runner, "prepare_launch", fake)


def _wait_finished(job_id: int, timeout: float = 15.0) -> None:
    """
    Дождаться, пока фоновый поток запишет ИТОГ процесса.

    Ждём именно exit_code, а не статус: `stop_job` ставит статус сразу, а код
    выхода и финальную строку лога пишет поток чтения — уже после. Ожидание
    по статусу давало гонку и мигающий тест.
    """
    import web.db as db_module
    from web.models import Job

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db_module.session_scope() as session:
            if session.get(Job, job_id).exit_code is not None:
                return
        time.sleep(0.1)


def _wait_status(job_id: int, expected: set[str], timeout: float = 15.0) -> str:
    """Подождать, пока фоновый поток проставит итог процесса."""
    import web.db as db_module
    from web.models import Job

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db_module.session_scope() as session:
            status = session.get(Job, job_id).status
        if status in expected:
            return status
        time.sleep(0.1)
    return status


def test_successful_run_is_marked_booked(db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from web.models import JobLog
    from web.services.runner import start_job

    _patch_launch(monkeypatch, _fake_bot(tmp_path, "print('[ok] места найдены')\n"))
    job = _make_job(db, "railway")
    start_job(db, job)

    assert _wait_status(job.id, {"booked", "failed"}) == "booked"

    with __import__("web.db", fromlist=["x"]).session_scope() as session:
        texts = [row.text for row in session.query(JobLog).filter(JobLog.job_id == job.id).all()]
    assert any("места найдены" in t for t in texts)


def test_exit_code_2_needs_review(db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Код 2 — бронь под вопросом: повторный запуск может создать вторую."""
    from web.services.runner import start_job

    _patch_launch(monkeypatch, _fake_bot(tmp_path, "print('[book] стоп'); sys.exit(2)\n"))
    job = _make_job(db, "railway")
    start_job(db, job)

    assert _wait_status(job.id, {"needs_review", "failed", "booked"}) == "needs_review"


def test_failure_is_marked_failed(db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from web.services.runner import start_job

    _patch_launch(monkeypatch, _fake_bot(tmp_path, "print('[error] всё плохо'); sys.exit(1)\n"))
    job = _make_job(db, "railway")
    start_job(db, job)

    assert _wait_status(job.id, {"failed", "booked"}) == "failed"


def test_second_job_on_same_service_is_refused(
    db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Два поиска по одному сервису разом — это бан, а не вдвое больше шансов."""
    from web.services.runner import JobStartError, start_job, stop_job

    _patch_launch(monkeypatch, _fake_bot(tmp_path, "time.sleep(30)\n"))
    first = _make_job(db, "railway")
    start_job(db, first)

    second = _make_job(db, "railway")
    try:
        with pytest.raises(JobStartError) as exc:
            start_job(db, second)
        assert "уже работает" in str(exc.value)
    finally:
        stop_job(db, first, timeout=5)


def test_other_service_is_not_blocked(
    db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Поезд и самолёт — разные сайты, они друг другу не мешают."""
    from web.services.settings import save_service_config
    from web.services.runner import start_job, stop_job

    save_service_config(
        db, "airlines",
        {"login": "+99361234567", "password_enc": "п", "extra_password_enc": "и"},
    )
    _patch_launch(monkeypatch, _fake_bot(tmp_path, "time.sleep(30)\n"))

    train = _make_job(db, "railway")
    plane = _make_job(db, "airlines")
    start_job(db, train)
    try:
        start_job(db, plane)  # не должно бросить
    finally:
        stop_job(db, train, timeout=5)
        stop_job(db, plane, timeout=5)


def test_airlines_without_credentials_is_refused(db) -> None:
    from web.services.runner import JobStartError, start_job

    job = _make_job(db, "airlines")
    with pytest.raises(JobStartError) as exc:
        start_job(db, job)
    assert "доступы" in str(exc.value)


def test_stop_marks_stopped(db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from web.services.runner import start_job, stop_job

    _patch_launch(monkeypatch, _fake_bot(tmp_path, "time.sleep(30)\n"))
    job = _make_job(db, "railway")
    start_job(db, job)

    assert stop_job(db, job, timeout=5) is True
    assert _wait_status(job.id, {"stopped"}) == "stopped"


def test_orphans_are_recovered(db) -> None:
    """
    После перезапуска панели очередь не должна остаться занятой навсегда.

    Процессы умирают вместе с панелью, а строки «в работе» в базе остаются.
    """
    from web.models import JobStatus
    from web.services.runner import recover_orphans

    job = _make_job(db, "railway")
    job.status = JobStatus.RUNNING.value
    db.commit()

    assert recover_orphans(db) == 1
    db.refresh(job)
    assert job.status == JobStatus.STOPPED.value


def test_stopped_job_is_not_reported_as_success(
    db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Корректно остановленный бот выходит с кодом 0 — но это не «места найдены».

    Без этой оговорки в логе появлялось «Готово: места найдены или бронь
    создана» сразу после нажатия «Остановить».
    """
    import web.db as db_module
    from web.models import JobLog
    from web.services.runner import start_job, stop_job

    _patch_launch(monkeypatch, _fake_bot(tmp_path, "time.sleep(30)\n"))
    job = _make_job(db, "railway")
    start_job(db, job)
    stop_job(db, job, timeout=5)
    _wait_finished(job.id)

    assert _wait_status(job.id, {"stopped"}) == "stopped"

    with db_module.session_scope() as session:
        texts = [r.text for r in session.query(JobLog).filter(JobLog.job_id == job.id).all()]
    assert not any("места найдены" in t for t in texts)
    assert any("Остановлен по команде" in t for t in texts)


def test_windows_ctrl_break_code_is_a_stop() -> None:
    """0xC000013A — код Ctrl+Break на Windows, а не сбой."""
    from web.services.runner import WINDOWS_CONTROL_C_EXIT, _EXIT_MEANING

    assert _EXIT_MEANING[WINDOWS_CONTROL_C_EXIT][0] == "stopped"


def test_long_sleep_is_split_into_chunks() -> None:
    """
    Долгий сон должен идти кусками, иначе бот не реагирует на остановку.

    На Windows time.sleep не прерывается сигналом Ctrl+Break, и обработчик
    выполняется только между вызовами.
    """
    import core.slots as slots

    calls: list[float] = []
    real_sleep = slots.time.sleep

    def counting(seconds):
        calls.append(seconds)
        real_sleep(min(seconds, 0.001))

    slots.time.sleep = counting
    try:
        slots.precise_sleep(5.0)
    finally:
        slots.time.sleep = real_sleep

    assert max(calls) <= slots._MAX_SLEEP_CHUNK_SEC + 1e-9


# --------------------------------------------------------------------------
# Удаление процесса
# --------------------------------------------------------------------------


def _booking_with_notifications(db, job):
    """Процесс с бронью и отправленными уведомлениями — как после удачного поиска."""
    from web.models import Booking, NotificationLog

    booking = Booking(
        job_id=job.id, service=job.service, status="done",
        guard_key="k1", booking_number="RW-77", payment_url="https://bank.tm/1",
    )
    db.add(booking)
    db.flush()

    db.add(NotificationLog(booking_id=booking.id, channel="email",
                           recipient="boss@example.tm", is_sent=True))
    db.add(NotificationLog(booking_id=booking.id, channel="sms",
                           recipient="+99361234567", is_sent=False, error="шлюз молчит"))
    db.commit()
    return booking


def test_job_with_booking_can_be_deleted(db) -> None:
    """
    Удаление процесса не должно упираться во внешние ключи.

    notification_logs ссылается на bookings, и без развязывания SQLite
    отвечает «FOREIGN KEY constraint failed» — кнопка «Удалить» просто
    не работала на любом процессе, доведённом до брони.
    """
    from web.models import Booking, Job, JobLog
    from web.services.runner import delete_job

    job = _make_job(db, "railway")
    _booking_with_notifications(db, job)
    db.add(JobLog(job_id=job.id, text="строка"))
    db.commit()

    delete_job(db, job)

    assert db.get(Job, job.id) is None
    assert db.query(JobLog).count() == 0
    # Логи процесса уходят вместе с ним, бронь — нет: см. следующий тест.
    assert db.query(Booking).count() == 1


def test_booking_survives_job_deletion(db) -> None:
    """
    Бронь существует у перевозчика, и за неё выставлен счёт.

    Раньше она удалялась вместе с поиском — человек убирал из списка
    отработавший процесс и вместе с ним стирал ссылку на оплату, ничего
    про это не поняв. Теперь бронь остаётся в разделе «Брони» без процесса.
    """
    from web.models import Booking, Job
    from web.services.runner import delete_job

    job = _make_job(db, "railway")
    booking = _booking_with_notifications(db, job)
    payment_url = booking.payment_url

    delete_job(db, job)

    survivor = db.query(Booking).one()
    assert survivor.job_id is None
    assert survivor.payment_url == payment_url
    assert db.get(Job, job.id) is None


def test_send_history_survives_job_deletion(db) -> None:
    """
    История отправок переживает процесс намеренно.

    Запись «ссылка на оплату ушла на такой-то адрес» ценна сама по себе —
    особенно если бронь была настоящей и оплаченной. Связь с бронью при этом
    сохраняется: бронь тоже переживает удаление процесса.
    """
    from web.models import Booking, NotificationLog
    from web.services.runner import delete_job

    job = _make_job(db, "railway")
    booking = _booking_with_notifications(db, job)
    booking_id = booking.id

    delete_job(db, job)

    logs = db.query(NotificationLog).all()
    assert len(logs) == 2
    assert all(log.booking_id == booking_id for log in logs)
    assert {log.recipient for log in logs} == {"boss@example.tm", "+99361234567"}
    assert db.get(Booking, booking_id) is not None


def test_labelled_captcha_survives_job_deletion(db) -> None:
    """Размеченная капча остаётся в журнале независимо от судьбы процесса."""
    from web.models import CaptchaSample
    from web.services.runner import delete_job

    job = _make_job(db, "airlines")
    db.add(
        CaptchaSample(
            service="airlines", job_id=job.id, file_path="hard/12345_abc.png",
            correct_code="12345", status="labeled",
        )
    )
    db.commit()

    delete_job(db, job)

    sample = db.query(CaptchaSample).one()
    assert sample.job_id is None
    assert sample.correct_code == "12345"


def test_delete_removes_run_directory(db) -> None:
    """В каталоге запуска лежит check.env с паролями — он не должен остаться."""
    from web.services.bot_env import prepare_launch
    from web.services.runner import delete_job

    job = _make_job(db, "railway")
    launch = prepare_launch(db, job)
    job.run_dir = str(launch.run_dir)
    db.commit()

    assert launch.env_file.exists()
    delete_job(db, job)
    assert not launch.run_dir.exists()


# --------------------------------------------------------------------------
# Контакты для брони
# --------------------------------------------------------------------------


def test_contact_reaches_the_bot(db) -> None:
    """
    Без contactEmail сервер перевозчика отвергает бронь. Панель обязана
    передать адрес: у пассажира почты может не быть вовсе.
    """
    from web.services.bot_env import prepare_launch
    from web.services.settings import save_settings

    save_settings(db, {"default_notify_email": "pay@example.com",
                       "default_notify_phone": "+99312345678"})
    job = _make_job(db, "airlines")
    values = _env_values(prepare_launch(db, job).env_file)

    assert values["API_CONTACT_EMAIL"] == "pay@example.com"
    assert values["API_CONTACT_PHONE"] == "+99312345678"


def test_job_contact_wins_over_default(db) -> None:
    from web.services.bot_env import prepare_launch
    from web.services.settings import save_settings

    save_settings(db, {"default_notify_email": "default@example.com"})
    job = _make_job(db, "airlines")
    job.notify_email = "this-one@example.com"
    db.commit()

    values = _env_values(prepare_launch(db, job).env_file)
    assert values["API_CONTACT_EMAIL"] == "this-one@example.com"


def test_airlines_without_contact_email_is_refused(db) -> None:
    """
    Узнать о нехватке адреса в момент появления мест — значит потерять
    именно тот слот, ради которого всё и затевалось. Проверяем на старте.
    """
    from web.services.runner import JobStartError, start_job
    from web.services.settings import save_service_config, save_settings

    # Доступы на месте: иначе запуск споткнётся о них раньше, и тест
    # проверял бы не то, что задуман.
    save_service_config(
        db,
        "airlines",
        {"login": "+99361234567", "password_enc": "пароль",
         "extra_login": "ios", "extra_password_enc": "пароль"},
    )
    save_settings(db, {"default_notify_email": "", "default_notify_phone": ""})
    job = _make_job(db, "airlines")

    with pytest.raises(JobStartError) as exc:
        start_job(db, job)
    assert "email" in str(exc.value).lower()


def test_passenger_email_is_enough(db) -> None:
    """Почта пассажира — законный запасной вариант: бот подставит её сам."""
    from web.models import Passenger
    from web.services.runner import _missing_booking_contact
    from web.services.settings import save_settings

    save_settings(db, {"default_notify_email": ""})
    job = _make_job(db, "airlines")
    pax = db.query(Passenger).first()
    pax.email = "pax@example.com"
    db.commit()

    assert _missing_booking_contact(db, job) == ""


def test_contact_is_not_required_without_auto_book(db) -> None:
    """При выключенной автоброни бот только смотрит — контакты не нужны."""
    from web.services.runner import _missing_booking_contact
    from web.services.settings import save_service_config, save_settings

    save_settings(db, {"default_notify_email": ""})
    save_service_config(db, "airlines", {"auto_book": False})
    job = _make_job(db, "airlines")

    assert _missing_booking_contact(db, job) == ""


def test_railway_needs_email_and_phone(db) -> None:
    """Без контакта сервер поездов отвечает input.invalidInputParam — ловим до запуска."""
    from web.models import Passenger
    from web.services.runner import _missing_booking_contact
    from web.services.settings import save_settings

    save_settings(db, {"default_notify_email": "me@example.com", "default_notify_phone": ""})
    for pax in db.query(Passenger).all():
        pax.phone = ""
    db.commit()
    assert "телефон" in _missing_booking_contact(db, _make_job(db, "railway"))

    save_settings(db, {"default_notify_phone": "+99361000000"})
    assert _missing_booking_contact(db, _make_job(db, "railway")) == ""


def test_railway_env_passes_booking_contact(db) -> None:
    from web.services.bot_env import prepare_launch
    from web.services.settings import save_settings

    save_settings(db, {"default_notify_email": "me@example.com",
                       "default_notify_phone": "+99361000000"})
    env = _env_values(prepare_launch(db, _make_job(db, "railway")).env_file)
    assert env["RAILWAY_CONTACT_EMAIL"] == "me@example.com"
    assert env["RAILWAY_CONTACT_PHONE"] == "+99361000000"


def test_empty_flight_choice_means_any(db) -> None:
    """
    Пустое поле «Номер рейса» = любой рейс. Раньше панель подставляла единицу,
    и выразить «смотри все» было нельзя вовсе.
    """
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "airlines")
    job.params = {}
    db.commit()

    values = _env_values(prepare_launch(db, job).env_file)
    assert "FLIGHT_INDEX" not in values
    assert "FLIGHT_TIME" not in values


def test_flight_choice_reaches_the_bot(db) -> None:
    from web.services.bot_env import prepare_launch

    job = _make_job(db, "airlines")
    job.params = {"flight_index": "2,1"}
    db.commit()

    values = _env_values(prepare_launch(db, job).env_file)
    assert values["FLIGHT_INDEX"] == "2,1"


# --------------------------------------------------------------------------- #
# Лог процесса: окно последних строк, а не потолок
# --------------------------------------------------------------------------- #


class _FakeProcess:
    """Процесс бота, который печатает заданные строки и выходит с кодом 0."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = iter(line + "\n" for line in lines)

    def wait(self) -> int:
        return 0


def test_log_keeps_writing_past_the_window(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Раньше после 20 000 строк вывод переставал записываться совсем, и консоль
    процесса молчала, хотя бот работал сутками. Теперь старые строки уходят,
    а новые пишутся всегда.
    """
    import web.services.runner as runner
    from web.models import JobLog

    monkeypatch.setattr(runner, "KEEP_LOGS_PER_JOB", 25)
    monkeypatch.setattr(runner, "PRUNE_LOGS_EVERY", 10)
    job = _make_job(db)

    runner._pump_output(job.id, _FakeProcess([f"строка {n}" for n in range(1, 101)]))

    db.expire_all()
    texts = [row.text for row in db.query(JobLog).filter(JobLog.job_id == job.id)
             .order_by(JobLog.id)]
    assert "строка 100" in texts, "последняя строка должна дойти до консоли"
    assert "строка 1" not in texts, "старые строки должны уходить"
    assert not any("предел" in text for text in texts)


def test_prune_keeps_the_latest_lines(db) -> None:
    from web.models import JobLog
    from web.services.runner import prune_job_logs

    job, other = _make_job(db), _make_job(db)
    for n in range(10):
        db.add(JobLog(job_id=job.id, text=f"{n}"))
        db.add(JobLog(job_id=other.id, text=f"{n}"))
    db.commit()

    assert prune_job_logs(db, job.id, keep=3) == 7
    db.commit()
    assert [r.text for r in db.query(JobLog).filter(JobLog.job_id == job.id)
            .order_by(JobLog.id)] == ["7", "8", "9"]
    assert db.query(JobLog).filter(JobLog.job_id == other.id).count() == 10, "чужой лог не трогаем"
    assert prune_job_logs(db, job.id, keep=3) == 0
