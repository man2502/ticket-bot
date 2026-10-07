"""Тесты нескольких аккаунтов перевозчика (провайдеров).

Самолётный поиск раньше шёл одним процессом: аккаунт был один. Теперь аккаунтов
несколько, и процессов столько же. Здесь проверяется то, из-за чего это нельзя
было сделать простым снятием запрета:

* процессы не делят файлы — отпечаток аккаунта, журнал броней, состояние;
* два аккаунта не бронируют одних и тех же людей;
* процессы не бьют в сайт в одну и ту же секунду;
* переезд со старой схемы не теряет ни аккаунт, ни незакрытую попытку брони.

Запуск (из корня репозитория):
    python run_tests.py web/test_providers.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --------------------------------------------------------------------------- #
# Окружение
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module
    import web.services.bookings as bookings_module
    import web.services.bot_env as bot_env

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Общие файлы бота — тоже во временную папку: рабочий журнал в
    # airlines_api/ может содержать настоящую незакрытую бронь.
    legacy = tmp_path / "airlines_api"
    legacy.mkdir()
    monkeypatch.setattr(config, "AIRLINES_API_DIR", legacy)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(bot_env, "RUNS_DIR", runs)

    journals = tmp_path / "journals"
    journals.mkdir()
    monkeypatch.setattr(
        bookings_module,
        "journal_path",
        lambda service: journals / f"{service}_bookings.json",
    )

    db_module.init_db()
    with db_module.session_scope() as session:
        from web.services.settings import save_service_config, save_settings

        save_settings(session, {"default_notify_email": "pay@example.com"})
        save_service_config(session, "airlines", {"extra_password_enc": "пароль-приложения"})
        yield session


def _provider(db, name: str, *, login: str | None = None, **data):
    from web.services.providers import save_provider

    payload = {
        "name": name,
        "login": login or f"+9936{abs(hash(name)) % 10_000_000:07d}",
        "password": "пароль",
        "is_enabled": True,
    }
    payload.update(data)
    return save_provider(db, None, payload)


def _job(db, *, doc: str = "AA0000001", provider=None, choice=None):
    from web.models import Job, JobPassenger, Passenger, Role, User
    from web.security import hash_password

    user = db.query(User).first()
    if user is None:
        user = User(login="tester", password_hash=hash_password("x" * 8), role=Role.USER.value)
        db.add(user)
        db.flush()

    pax = db.query(Passenger).filter(Passenger.doc_number == doc).first()
    if pax is None:
        pax = Passenger(first_name="ANVAR", last_name="TEST", dob="01.01.1990",
                        gender="M", doc_number=doc)
        db.add(pax)
        db.flush()

    job = Job(user_id=user.id, service="airlines", source="ASB", destination="MYP",
              depart_date="18.09.2026")
    job.params = {"provider": str(choice.id)} if choice is not None else {}
    if provider is not None:
        job.provider = provider
    db.add(job)
    db.flush()
    db.add(JobPassenger(job_id=job.id, passenger_id=pax.id, position=0))
    db.commit()
    return job


def _env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def _mark_running(db, job, provider) -> None:
    """Процесс «работает» — без настоящего подпроцесса, как видит его панель."""
    from web.models import JobStatus

    job.provider = provider
    job.status = JobStatus.RUNNING.value
    db.commit()


def _write_attempt(path: Path, *, key: str, status: str = "done", passengers: str = "",
                   number: str = "", started: str = "2026-09-17 10:00:00") -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"attempts": {}}
    data.setdefault("attempts", {})[key] = {
        "booking_key": key, "idempotency_key": "i", "status": status,
        "started_at": started, "booking_number": number, "passengers": passengers,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Свои файлы у каждого аккаунта
# --------------------------------------------------------------------------- #


def test_each_provider_gets_its_own_files(db) -> None:
    """
    Общими эти файлы ломали бы всё сразу. `.env.local` перекрывает настройки
    панели — два аккаунта в нём перелогинивали бы друг друга по кругу. Журнал
    броней перезаписывается целиком — процессы теряли бы записи друг друга.
    """
    from web.services.bot_env import prepare_launch

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    a = _env_values(prepare_launch(db, _job(db, doc="AA1", provider=first)).env_file)
    b = _env_values(prepare_launch(db, _job(db, doc="AA2", provider=second)).env_file)

    for key in ("API_LOCAL_ENV", "API_BOOKINGS_JOURNAL", "API_STATE_FILE", "API_CAPTCHA_IMAGE"):
        assert a[key] and b[key], key
        assert a[key] != b[key], f"{key} общий у двух аккаунтов"

    assert a["API_CUSTOMER_PHONE"] == first.login
    assert b["API_CUSTOMER_PHONE"] == second.login


def test_app_account_is_shared(db) -> None:
    """Учётка приложения — ключ клиента T5, одна на всех аккаунтов."""
    from web.services.bot_env import prepare_launch

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    a = _env_values(prepare_launch(db, _job(db, doc="AA1", provider=first)).env_file)
    b = _env_values(prepare_launch(db, _job(db, doc="AA2", provider=second)).env_file)
    assert a["API_IOS_PASSWORD"] == b["API_IOS_PASSWORD"] == "пароль-приложения"


def test_job_without_provider_is_not_launched(db) -> None:
    """Пойти с пустым аккаунтом хуже, чем не пойти: отказ придёт уже с сайта."""
    from web.services.bot_env import prepare_launch

    with pytest.raises(ValueError):
        prepare_launch(db, _job(db))


# --------------------------------------------------------------------------- #
# Разнесение по времени
# --------------------------------------------------------------------------- #


def test_providers_do_not_share_a_second(db) -> None:
    """
    Все аккаунты ходят с одного адреса. Три процесса в одну и ту же секунду
    для сайта — одна тройная очередь, и режет он именно по адресу.
    """
    from web.services.providers import provider_slot_second

    providers = [_provider(db, f"P{i}") for i in range(3)]
    seconds = [provider_slot_second(db, p, period_sec=30, base_second=0) for p in providers]
    assert seconds == [0, 10, 20]


def test_two_providers_split_the_period(db) -> None:
    from web.services.providers import provider_slot_second

    first, second = _provider(db, "A"), _provider(db, "B")
    assert provider_slot_second(db, first, period_sec=30, base_second=0) == 0
    assert provider_slot_second(db, second, period_sec=30, base_second=0) == 15


def test_manual_second_wins(db) -> None:
    from web.services.providers import provider_slot_second

    provider = _provider(db, "A", check_at_second="42")
    assert provider_slot_second(db, provider, period_sec=30, base_second=0) == 42


def test_disabled_provider_does_not_take_a_slot(db) -> None:
    """Порядок — среди включённых: выключенный не должен оставлять дыру."""
    from web.services.providers import provider_slot_second

    _provider(db, "Выключен", is_enabled=False)
    second = _provider(db, "Работает")
    assert provider_slot_second(db, second, period_sec=30, base_second=0) == 0


def test_slot_second_reaches_the_bot(db) -> None:
    from web.services.bot_env import prepare_launch

    _provider(db, "A")
    second = _provider(db, "B")
    values = _env_values(prepare_launch(db, _job(db, provider=second)).env_file)
    assert values["API_CHECK_AT_SEC"] == "15"


# --------------------------------------------------------------------------- #
# Выбор аккаунта при запуске
# --------------------------------------------------------------------------- #


def test_any_free_provider_is_picked(db) -> None:
    from web.services.runner import _assign_provider

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _mark_running(db, _job(db, doc="AA1"), first)

    job = _job(db, doc="AA2")
    _assign_provider(db, job)
    assert job.provider.id == second.id


def test_chosen_busy_provider_is_refused(db) -> None:
    from web.services.runner import JobStartError, _assign_provider

    first = _provider(db, "Первый")
    _provider(db, "Второй")
    _mark_running(db, _job(db, doc="AA1"), first)

    job = _job(db, doc="AA2", choice=first)
    with pytest.raises(JobStartError) as exc:
        _assign_provider(db, job)
    assert "занят" in str(exc.value)


def test_all_busy_is_explained(db) -> None:
    from web.services.runner import JobStartError, _assign_provider
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"max_parallel": 5})
    only = _provider(db, "Единственный")
    _mark_running(db, _job(db, doc="AA1"), only)

    with pytest.raises(JobStartError) as exc:
        _assign_provider(db, _job(db, doc="AA2"))
    assert "Единственный" in str(exc.value)


def test_provider_without_password_is_skipped(db) -> None:
    from web.services.runner import _assign_provider

    _provider(db, "Без пароля", password="")
    ready = _provider(db, "Готов")

    job = _job(db)
    _assign_provider(db, job)
    assert job.provider.id == ready.id


def test_parallel_limit(db) -> None:
    """Предел одновременных процессов — все аккаунты ходят с одного адреса."""
    from web.services.runner import JobStartError, _assign_provider
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"max_parallel": 1})
    first = _provider(db, "Первый")
    _provider(db, "Второй")
    _mark_running(db, _job(db, doc="AA1"), first)

    with pytest.raises(JobStartError) as exc:
        _assign_provider(db, _job(db, doc="AA2"))
    assert "предел" in str(exc.value)


def test_provider_with_open_attempt_is_skipped(db) -> None:
    """С незакрытой попыткой в своём журнале бот остановится сразу после старта."""
    from web.services.providers import provider_journal_path
    from web.services.runner import _assign_provider

    blocked, free = _provider(db, "С попыткой"), _provider(db, "Чистый")
    _write_attempt(provider_journal_path(blocked.id), key="k1", status="unknown",
                   passengers="чужие")

    job = _job(db)
    _assign_provider(db, job)
    assert job.provider.id == free.id


# --------------------------------------------------------------------------- #
# Одни и те же люди на двух аккаунтах
# --------------------------------------------------------------------------- #


def _tag(doc: str) -> str:
    from core.booking_guard import passenger_tag

    return passenger_tag(doc)


def test_same_passengers_in_parallel_are_allowed_by_default(db) -> None:
    """
    По умолчанию одного человека можно искать сразу с нескольких аккаунтов:
    так решил владелец панели — шансов больше, риск двух броней он принимает.
    """
    from web.services.bookings import job_start_blocker

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _mark_running(db, _job(db, doc="I-LB 897989"), first)

    job = _job(db, doc="I-LB 897989", provider=second)
    assert job_start_blocker(db, job) == ""


def test_same_passengers_are_refused_when_sharing_is_off(db) -> None:
    """Галочку можно снять — и проверка вернётся: две брони, два счёта."""
    from web.services.bookings import job_start_blocker
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"allow_shared_passengers": False})
    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _mark_running(db, _job(db, doc="I-LB 897989"), first)

    job = _job(db, doc="I-LB 897989", provider=second)
    assert "процесс" in job_start_blocker(db, job)


def test_open_attempt_still_blocks_when_sharing_is_on(db) -> None:
    """
    Разрешение касается только параллельного поиска. Незакрытая попытка —
    это бронь, которая уже есть или могла появиться, и повтор поверх неё
    остаётся под запретом.
    """
    from web.services.bookings import job_start_blocker
    from web.services.providers import provider_journal_path
    from web.services.settings import get_service_config

    assert get_service_config(db, "airlines").allow_shared_passengers is True
    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _write_attempt(provider_journal_path(first.id), key="k1", status="unknown",
                   passengers=_tag("ILB897989"))

    job = _job(db, doc="I-LB 897989", provider=second)
    assert "незакрытая попытка" in job_start_blocker(db, job)


def test_different_passengers_run_in_parallel(db) -> None:
    from web.services.bookings import job_start_blocker

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _mark_running(db, _job(db, doc="AA1"), first)

    assert job_start_blocker(db, _job(db, doc="AA2", provider=second)) == ""


def test_open_attempt_on_another_provider_blocks_same_people(db) -> None:
    """
    Журнал каждого аккаунта видит только себя. Без сверки по всем журналам
    второй аккаунт забронировал бы тех, чья бронь на первом ещё под вопросом.
    """
    from web.services.bookings import job_start_blocker
    from web.services.providers import provider_journal_path

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _write_attempt(provider_journal_path(first.id), key="k1", status="unknown",
                   passengers=_tag("ILB897989"))

    job = _job(db, doc="I-LB 897989", provider=second)
    assert "незакрытая попытка" in job_start_blocker(db, job)


def test_open_attempt_for_other_people_does_not_block(db) -> None:
    from web.services.bookings import job_start_blocker
    from web.services.providers import provider_journal_path

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _write_attempt(provider_journal_path(first.id), key="k1", status="done",
                   passengers=_tag("ZZ999"), number="46MEMB")

    assert job_start_blocker(db, _job(db, doc="AA1", provider=second)) == ""


def test_old_attempt_without_passengers_blocks_everyone(db) -> None:
    """На кого старая запись, неизвестно, — а ошибиться здесь стоит денег."""
    from web.services.bookings import job_start_blocker

    _provider(db, "Первый")
    second = _provider(db, "Второй")
    import web.services.bookings as bookings

    _write_attempt(bookings.journal_path("airlines"), key="k1", status="done", number="46MBJ0")

    assert "старая запись" in job_start_blocker(db, _job(db, doc="AA1", provider=second))


def test_document_number_is_matched_regardless_of_format() -> None:
    """`I-LB 897989` и `ilb897989` — один и тот же паспорт."""
    assert _tag("I-LB 897989") == _tag("ilb897989")
    assert _tag("I-LB 897989") != _tag("I-LB 897988")


def test_journal_does_not_store_the_document_number(tmp_path: Path) -> None:
    from core.booking_guard import BookingGuard, passenger_tags

    path = tmp_path / "journal.json"
    guard = BookingGuard(path)
    guard.begin("k1", passengers=passenger_tags([{"doc_number": "I-LB 897989"}]))

    text = path.read_text(encoding="utf-8")
    assert "897989" not in text
    assert guard.get("k1").passengers == _tag("I-LB 897989")


# --------------------------------------------------------------------------- #
# Журналы в разделе «Брони»
# --------------------------------------------------------------------------- #


def test_copied_record_is_shown_once(db) -> None:
    """При переезде общий журнал скопирован к первому аккаунту — это одна бронь."""
    import web.services.bookings as bookings
    from web.services.providers import provider_journal_path

    provider = _provider(db, "Первый")
    for path in (bookings.journal_path("airlines"), provider_journal_path(provider.id)):
        _write_attempt(path, key="k1", status="done", number="46MEMB")

    found = bookings.blockers(db, "airlines")
    assert len(found) == 1
    assert len(found[0].journals) == 2


def test_unblock_clears_every_copy(db) -> None:
    """Иначе блокировка вернулась бы из копии, и человек гадал бы почему."""
    import web.services.bookings as bookings
    from web.services.providers import provider_journal_path

    provider = _provider(db, "Первый")
    for path in (bookings.journal_path("airlines"), provider_journal_path(provider.id)):
        _write_attempt(path, key="k1", status="done", number="46MEMB")

    bookings.unblock_service(db, "airlines", guard_key="k1")
    assert bookings.blockers(db, "airlines") == []


def test_booking_remembers_its_provider(db) -> None:
    """Платить и отменять бронь — в том аккаунте, на который она оформлена."""
    from web.services.bookings import sync_orphan_bookings
    from web.services.providers import provider_journal_path

    provider = _provider(db, "Второй")
    _write_attempt(provider_journal_path(provider.id), key="k1", status="done",
                   number="46MEMB")

    created = sync_orphan_bookings(db)
    assert created[0].provider_id == provider.id


# --------------------------------------------------------------------------- #
# Переезд со старой схемы
# --------------------------------------------------------------------------- #


def test_legacy_account_becomes_the_first_provider(db) -> None:
    import web.config as config
    from web.models import Booking, Job, Provider
    from web.services.providers import (
        provider_journal_path,
        provider_password,
        seed_from_legacy,
    )
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"login": "+99361234567", "password_enc": "старый"})
    (config.AIRLINES_API_DIR / "api_bookings.json").write_text(
        json.dumps({"attempts": {"k1": {"booking_key": "k1", "idempotency_key": "i",
                                        "status": "done", "started_at": "2026-09-01 13:43:33",
                                        "booking_number": "46MEMB"}}}),
        encoding="utf-8",
    )
    old_job = _job(db)
    db.add(Booking(service="airlines", status="done", guard_key="k1", booking_number="46MEMB"))
    db.commit()

    provider = seed_from_legacy(db)

    assert provider.login == "+99361234567"
    assert provider_password(provider) == "старый"
    db.expire_all()
    assert db.get(Job, old_job.id).provider_id == provider.id
    assert db.query(Booking).one().provider_id == provider.id
    # Незакрытая попытка переехала вместе с аккаунтом — и осталась в общем журнале.
    assert "46MEMB" in provider_journal_path(provider.id).read_text(encoding="utf-8")
    assert (config.AIRLINES_API_DIR / "api_bookings.json").exists()

    # Повторный запуск ничего не дублирует.
    assert seed_from_legacy(db) is None
    assert db.query(Provider).count() == 1


def test_nothing_to_seed_without_legacy_account(db) -> None:
    from web.models import Provider
    from web.services.providers import seed_from_legacy

    assert seed_from_legacy(db) is None
    assert db.query(Provider).count() == 0


# --------------------------------------------------------------------------- #
# Настоящий параллельный запуск
# --------------------------------------------------------------------------- #


def test_two_providers_really_run_at_once(
    db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Сквозная проверка: два процесса самолётов работают одновременно."""
    import web.services.bot_env as bot_env
    import web.services.runner as runner
    from web.services.runner import is_running, start_job, stop_job

    script = tmp_path / "fake_bot.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    real = bot_env.prepare_launch

    def fake(db_, job):
        launch = real(db_, job)
        launch.script = script
        launch.cwd = script.parent
        return launch

    monkeypatch.setattr(runner, "prepare_launch", fake)

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    a, b = _job(db, doc="AA1"), _job(db, doc="AA2")
    start_job(db, a)
    try:
        start_job(db, b)
        assert is_running(a.id) and is_running(b.id)
        assert {a.provider_id, b.provider_id} == {first.id, second.id}
    finally:
        stop_job(db, a, timeout=5)
        stop_job(db, b, timeout=5)


# --------------------------------------------------------------------------- #
# Удаление аккаунта и форма
# --------------------------------------------------------------------------- #


def test_busy_provider_cannot_be_deleted(db) -> None:
    from web.services.providers import ProviderError, delete_provider

    provider = _provider(db, "Занятый")
    _mark_running(db, _job(db), provider)

    with pytest.raises(ProviderError) as exc:
        delete_provider(db, provider)
    assert "занят" in str(exc.value)


def test_provider_with_open_attempt_cannot_be_deleted(db) -> None:
    """
    Иначе панель перестала бы видеть его журнал, блокировка молча исчезла бы,
    и те же люди ушли бы в бронь на другом аккаунте.
    """
    from web.services.providers import ProviderError, delete_provider, provider_journal_path

    provider = _provider(db, "С попыткой")
    _write_attempt(provider_journal_path(provider.id), key="k1", status="unknown")

    with pytest.raises(ProviderError):
        delete_provider(db, provider)


def test_deleting_keeps_the_journal_on_disk(db) -> None:
    """След настоящей брони должен остаться, даже когда аккаунт убрали."""
    from web.models import Provider
    from web.services.providers import delete_provider, provider_journal_path

    provider = _provider(db, "Старый")
    journal = provider_journal_path(provider.id)
    _write_attempt(journal, key="k1", status="done", number="46MEMB")
    guard_path = journal
    # Попытка закрыта и снята — блокировки нет, удалять можно.
    from core.booking_guard import BookingGuard

    BookingGuard(guard_path).clear_all()

    delete_provider(db, provider)
    assert db.query(Provider).count() == 0
    assert "46MEMB" in journal.read_text(encoding="utf-8")


def test_password_is_not_wiped_by_an_empty_field(db) -> None:
    """Правка названия не должна стирать пароль — пустое поле значит «не менять»."""
    from web.services.providers import provider_password, save_provider

    provider = _provider(db, "А")
    save_provider(db, provider, {"name": "Б", "password": ""})
    assert provider_password(provider) == "пароль"

    save_provider(db, provider, {"password": "-"})
    assert provider_password(provider) == ""


def test_form_keeps_the_chosen_provider() -> None:
    from web.routes.jobs import _collect_params

    assert _collect_params("airlines", {"provider": "3"})["provider"] == "3"
    assert _collect_params("airlines", {"provider": ""})["provider"] == ""
    # Мусор из формы — это «любой», а не падение при запуске.
    assert _collect_params("airlines", {"provider": "abc"})["provider"] == ""


def test_pages_are_registered_as_routes() -> None:
    """
    Страховка от незаметной поломки: вспомогательная функция, вставленная между
    декоратором маршрута и самим обработчиком, забирает декоратор себе, и
    список процессов пропадает, а панель падает на старте.
    """
    import importlib

    # Приложение должно собираться целиком: ошибка регистрации маршрута
    # роняет его на импорте.
    importlib.import_module("web.app")
    from web.routes import jobs, settings

    def endpoints(router) -> dict[str, str]:
        return {route.path: route.endpoint.__name__ for route in router.routes}

    job_routes = endpoints(jobs.router)
    assert job_routes.get("/jobs") == "list_jobs"
    assert "/settings/providers" in endpoints(settings.router)
    for router in (jobs.router, settings.router):
        names = endpoints(router).values()
        assert not any(name.startswith("_") for name in names), names


# --------------------------------------------------------------------------- #
# Лимит запросов (429)
# --------------------------------------------------------------------------- #


def _stop_now() -> None:
    """Довести общую лестницу до стопа, как это сделали бы боты."""
    from core import rate_limit as rl
    from web.services.rate_limit import state_path

    now = rl.time.time()
    for _ in range(rl.STOP_AFTER):
        decision = rl.register_429(state_path(), retry_after=None, now=now)
        now = rl.read_state(state_path()).until + 1 if not decision.stop else now


def test_all_providers_share_one_rate_limit_file(db) -> None:
    """Все аккаунты ходят с одного адреса — 429 у одного означает паузу для всех."""
    from web.services.bot_env import prepare_launch

    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    a = _env_values(prepare_launch(db, _job(db, doc="AA1", provider=first)).env_file)
    b = _env_values(prepare_launch(db, _job(db, doc="AA2", provider=second)).env_file)

    assert a["API_RATE_LIMIT_STATE"] == b["API_RATE_LIMIT_STATE"]
    assert a["API_PROVIDER_NAME"] == "Первый"


def test_start_is_refused_while_stopped(db) -> None:
    """Перезапуск сразу после стопа — прямой путь к блокировке аккаунта."""
    from web.services.runner import JobStartError, start_job

    _provider(db, "Первый")
    _stop_now()

    with pytest.raises(JobStartError) as exc:
        start_job(db, _job(db))
    assert "429" in str(exc.value)


def test_clear_allows_start_again(db) -> None:
    from web.services.rate_limit import clear, start_blocker

    _stop_now()
    assert start_blocker()
    clear()
    assert start_blocker() == ""


def test_stop_is_announced_once_for_all_processes(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Процессы останавливаются вместе, и каждый выходит с кодом 3. Письмо о стопе
    должно прийти одно, а не по одному от каждого процесса.
    """
    import web.services.notifications as notifications
    from web.services.runner import _notify_rate_limit_stop

    sent = []
    monkeypatch.setattr(
        notifications,
        "send_email",
        lambda db, *, to, subject, body: sent.append(subject)
        or notifications.SendResult(ok=True, channel="email", recipient=to),
    )
    monkeypatch.setattr(notifications, "send_sms", lambda db, **k: None)

    _stop_now()
    first, second = _provider(db, "Первый"), _provider(db, "Второй")
    _notify_rate_limit_stop(db, _job(db, doc="AA1", provider=first))
    _notify_rate_limit_stop(db, _job(db, doc="AA2", provider=second))

    assert len(sent) == 1
    assert "429" in sent[0]


def test_exit_code_3_is_a_rate_limit_stop() -> None:
    from web.services.runner import _EXIT_MEANING, RATE_LIMIT_EXIT_CODE

    import api_rate_limit

    assert RATE_LIMIT_EXIT_CODE == api_rate_limit.RATE_LIMIT_EXIT_CODE
    status, message = _EXIT_MEANING[RATE_LIMIT_EXIT_CODE]
    assert status == "failed"
    assert "429" in message
