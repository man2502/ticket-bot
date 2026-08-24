"""Подключение к SQLite и сессии SQLAlchemy.

Особенности SQLite, которые здесь учтены:

* `check_same_thread=False` — воркер читает вывод бота в отдельном потоке
  и пишет логи в ту же базу.
* `WAL` — читатели не блокируют писателя. Без него открытая страница с логами
  вешала бы запись новых строк.
* `busy_timeout` — вместо мгновенного «database is locked» ждём освобождения.
* `foreign_keys=ON` — SQLite по умолчанию НЕ проверяет внешние ключи, и каскадное
  удаление логов вместе с процессом просто не сработало бы.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from web.config import database_url, ensure_dirs
from web.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    # NORMAL вместо FULL: на порядок быстрее записи логов, при этом потеря
    # возможна только при отключении питания, а не при падении процесса.
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        ensure_dirs()
        _engine = create_engine(
            database_url(),
            connect_args={"check_same_thread": False},
            future=True,
        )
        event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Сессия с автоматическим commit/rollback. Для кода вне запросов."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """Зависимость FastAPI: сессия на время одного запроса."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Создать таблицы и значения по умолчанию. Идемпотентно."""
    ensure_dirs()
    Base.metadata.create_all(get_engine())
    _migrate()
    _seed_defaults()


def _migrate() -> None:
    """
    Догнать схему уже существующей базы до моделей.

    Alembic не заводим: правок за всё время единицы, а лишняя зависимость и
    каталог версий стоят дороже, чем несколько идемпотентных шагов здесь.
    `create_all` умеет создавать таблицы, но не менять их, поэтому у людей
    со старой базой новая колонка не появилась бы вовсе.
    """
    from sqlalchemy import text

    with get_engine().begin() as conn:
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(service_configs)"))
        }
        if not columns:
            return

        # Период проверки вместо пары галочек «:00» и «:30».
        if "check_period_sec" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE service_configs "
                    "ADD COLUMN check_period_sec INTEGER NOT NULL DEFAULT 30"
                )
            )
            if {"slot_00_enabled", "slot_30_enabled"} <= columns:
                # Обе галочки = дважды в минуту, одна = раз в минуту. Если
                # оставалось только окно :30 — переносим и сдвиг, иначе бот
                # начал бы стрелять в :00, куда его никто не просил.
                conn.execute(
                    text(
                        "UPDATE service_configs SET "
                        "check_period_sec = CASE "
                        "  WHEN slot_00_enabled AND slot_30_enabled THEN 30 ELSE 60 END, "
                        "check_at_second = CASE "
                        "  WHEN slot_30_enabled AND NOT slot_00_enabled THEN 30 "
                        "  ELSE check_at_second END "
                        "WHERE service <> 'railway'"
                    )
                )
                # У railway эти галочки не доезжали до бота вовсе: он всегда
                # делал два запроса в минуту (:00 и :28–32). Любое их состояние
                # означало 30 секунд, и менять фактический темп при обновлении
                # панели было бы неожиданностью.
                conn.execute(
                    text(
                        "UPDATE service_configs SET check_period_sec = 30 "
                        "WHERE service = 'railway'"
                    )
                )

        # Старые колонки NOT NULL без значения по умолчанию: пока они есть,
        # вставка новой строки конфигурации падала бы на них.
        for dead in ("slot_00_enabled", "slot_30_enabled"):
            if dead in columns:
                conn.execute(text(f"ALTER TABLE service_configs DROP COLUMN {dead}"))


def _seed_defaults() -> None:
    """Строки конфигурации сервисов и общие настройки — чтобы формы не были пустыми."""
    from web.models import Service, ServiceConfig, Setting

    defaults: dict[str, tuple[str, bool]] = {
        # SMTP
        "smtp_host": ("smtp.gmail.com", False),
        "smtp_port": ("587", False),
        "smtp_user": ("", False),
        "smtp_password": ("", True),
        "smtp_from": ("", False),
        # SMS-шлюз: POST на указанный URL с полями phone и message.
        "sms_url": ("", False),
        "sms_api_key": ("", True),
        "sms_api_key_field": ("api_key", False),
        "sms_api_key_place": ("body", False),
        "sms_phone_field": ("phone", False),
        "sms_message_field": ("message", False),
        # Куда слать ссылку на оплату, если в процессе не задано своё.
        "default_notify_email": ("", False),
        "default_notify_phone": ("", False),
    }

    with session_scope() as session:
        existing = {s.key for s in session.query(Setting).all()}
        for key, (value, is_secret) in defaults.items():
            if key not in existing:
                session.add(Setting(key=key, value=value, is_secret=is_secret))

        configured = {c.service for c in session.query(ServiceConfig).all()}
        for service in (Service.AIRLINES, Service.RAILWAY):
            if service.value in configured:
                continue
            session.add(_default_service_config(service))


def _default_service_config(service):
    """Значения по умолчанию повторяют check.env.example каждого бота."""
    from web.models import Service, ServiceConfig

    if service == Service.AIRLINES:
        return ServiceConfig(
            service=service.value,
            # Самолёт проверяем дважды в минуту: отметки :00 и :30.
            check_period_sec=30,
            check_at_second=0,
            captcha_manual=False,
            captcha_max_attempts=5,
            rate_limit_pause_sec=150,
        )
    return ServiceConfig(
        service=service.value,
        # У поезда капчи нет и запрос дешевле, но чаще 30 с смысла мало:
        # места в кассе появляются пачками, а не по одному в секунду.
        check_period_sec=30,
        check_at_second=0,
        captcha_manual=False,
        rate_limit_pause_sec=60,
    )
