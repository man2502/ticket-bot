"""Тесты схемы базы и шифрования секретов.

База создаётся во временном файле, поэтому тесты не трогают рабочую
`web/data/ticket.db`.

Запуск (из корня репозитория):
    python run_tests.py web
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Чистая база и свежие ключи во временном каталоге."""
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)

    # Движок кешируется в модуле — сбрасываем, иначе тест возьмёт рабочую базу.
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


# --------------------------------------------------------------------------
# Инициализация
# --------------------------------------------------------------------------


def test_init_creates_service_configs(db) -> None:
    from web.models import Service, ServiceConfig

    services = {c.service for c in db.query(ServiceConfig).all()}
    assert services == {Service.AIRLINES.value, Service.RAILWAY.value}


def test_init_creates_settings(db) -> None:
    from web.models import Setting

    keys = {s.key for s in db.query(Setting).all()}
    assert {"smtp_host", "smtp_password", "sms_url", "default_notify_email"} <= keys


def test_secret_settings_are_marked(db) -> None:
    """Пароль SMTP и ключ SMS должны быть помечены как секреты."""
    from web.models import Setting

    for key in ("smtp_password", "sms_api_key"):
        setting = db.get(Setting, key)
        assert setting is not None and setting.is_secret is True


def test_init_is_idempotent(db) -> None:
    """Повторный запуск не должен плодить дубли настроек."""
    import web.db as db_module
    from web.models import Setting

    before = db.query(Setting).count()
    db_module.init_db()
    assert db.query(Setting).count() == before


def test_foreign_keys_are_enforced(db) -> None:
    """SQLite по умолчанию не проверяет внешние ключи — PRAGMA должна их включить."""
    result = db.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys")).scalar()
    assert result == 1


# --------------------------------------------------------------------------
# Пассажиры
# --------------------------------------------------------------------------


def _passenger(**over):
    from web.models import Passenger

    data = dict(
        first_name="Anvar",
        last_name="Test",
        dob="01.01.1990",
        gender="M",
        doc_number="AA1234567",
    )
    data.update(over)
    return Passenger(**data)


def test_passenger_to_bot_dict_matches_json_format(db) -> None:
    """Формат должен совпадать с passengers.json, который читают боты."""
    from passenger_loader import PASSENGER_FIELDS

    pax = _passenger(email="a@b.tm", phone="+99361234567")
    db.add(pax)
    db.flush()

    data = pax.to_bot_dict()
    assert set(PASSENGER_FIELDS) <= set(data)
    assert data["id"] == str(pax.id)
    # type не задан — бот определит его сам по дате рождения на дату рейса.
    assert "type" not in data


def test_passenger_type_is_passed_when_set(db) -> None:
    pax = _passenger(doc_number="BB7654321", pax_type="child")
    db.add(pax)
    db.flush()
    assert pax.to_bot_dict()["type"] == "child"


def test_duplicate_document_is_rejected(db) -> None:
    """Один паспорт дважды — почти всегда опечатка, а на оплате это отказ сервера."""
    from sqlalchemy.exc import IntegrityError

    db.add(_passenger())
    db.flush()
    db.add(_passenger(first_name="Другой"))
    with pytest.raises(IntegrityError):
        db.flush()

    # После неудачного flush сессия непригодна, пока её не откатили. Ровно это
    # делает роут создания пассажира — иначе следующий же запрос упал бы
    # с PendingRollbackError вместо понятного сообщения про дубль документа.
    db.rollback()


def test_full_name(db) -> None:
    assert _passenger().full_name == "Anvar Test"


# --------------------------------------------------------------------------
# Процессы
# --------------------------------------------------------------------------


def _job(db, **over):
    from web.models import Job, Role, Service, User
    from web.security import hash_password

    user = User(login=f"u{id(over)}", password_hash=hash_password("x" * 8), role=Role.USER.value)
    db.add(user)
    db.flush()

    data = dict(
        user_id=user.id,
        service=Service.RAILWAY.value,
        source="17",
        destination="49",
        depart_date="10.08.2026",
    )
    data.update(over)
    job = Job(**data)
    db.add(job)
    db.flush()
    return job


def test_job_params_roundtrip(db) -> None:
    job = _job(db)
    job.params = {"wagon_type_id": "3,2", "train_index": 1}
    db.flush()
    assert job.params["wagon_type_id"] == "3,2"


def test_job_params_survive_broken_json(db) -> None:
    """Испорченный JSON не должен ронять страницу списка процессов."""
    job = _job(db)
    job.params_json = "{это не json"
    assert job.params == {}


def test_job_is_active_only_while_working(db) -> None:
    from web.models import JobStatus

    job = _job(db)
    assert job.is_active is True  # queued

    job.status = JobStatus.RUNNING.value
    assert job.is_active is True

    job.status = JobStatus.BOOKED.value
    assert job.is_active is False


def test_deleting_job_removes_its_logs(db) -> None:
    from web.models import JobLog

    job = _job(db)
    db.add(JobLog(job_id=job.id, text="строка"))
    db.flush()

    db.delete(job)
    db.flush()
    assert db.query(JobLog).count() == 0


# --------------------------------------------------------------------------
# Шифрование секретов
# --------------------------------------------------------------------------


def test_secret_roundtrip(db) -> None:
    from web.crypto import decrypt, encrypt

    assert decrypt(encrypt("пароль-T5")) == "пароль-T5"


def test_encrypted_value_hides_original(db) -> None:
    from web.crypto import encrypt

    assert "пароль-T5" not in encrypt("пароль-T5")


def test_empty_secret_stays_empty(db) -> None:
    from web.crypto import decrypt, encrypt

    assert encrypt("") == ""
    assert decrypt("") == ""


def test_another_key_cannot_decrypt(db, tmp_path: Path) -> None:
    """Копия базы без ключа паролей не выдаёт — иначе шифрование бессмысленно."""
    from cryptography.fernet import Fernet

    import web.crypto as crypto
    from web.crypto import SecretUnreadable, encrypt

    encrypted = encrypt("пароль-T5")

    # Подменяем файл ключа — как если бы базу унесли на другую машину.
    (tmp_path / "secret.key").write_bytes(Fernet.generate_key())

    with pytest.raises(SecretUnreadable):
        crypto.decrypt(encrypted)
