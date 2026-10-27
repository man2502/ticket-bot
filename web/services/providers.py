"""Провайдеры — аккаунты перевозчика, от имени которых идут поиск и бронь.

Зачем они. Аккаунт в личном кабинете T5 был один и жил в настройках сервиса,
поэтому самолётный поиск мог идти только один: второй процесс пошёл бы с теми же
токенами и затёр бы первому всё, что тот хранит на диске. Теперь процессов может
быть столько, сколько аккаунтов.

**У каждого провайдера свой каталог данных** — `web/data/providers/<id>/`.
Туда бот пишет то, что раньше лежало общим в `airlines_api/`:

    .env.local         отпечаток аккаунта и токены. Этот файл ПЕРЕКРЫВАЕТ
                       настройки панели, и два аккаунта в одном файле
                       перелогинивали бы друг друга по кругу;
    api_bookings.json  журнал броней — защита от повторной брони;
    api_state.json     что было доступно в прошлый раз.

Модель капчи и датасет остаются общими: модель только читается, а у картинок
датасета уникальные имена.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import web.config as config
from core.slots import normalize_check_period
from web.crypto import SecretUnreadable, decrypt, encrypt
from web.models import Job, JobStatus, Provider, Service

__all__ = [
    "LEGACY_PROVIDER_NAME",
    "providers_root",
    "provider_dir",
    "provider_journal_path",
    "provider_state_path",
    "provider_env_path",
    "list_providers",
    "get_provider",
    "save_provider",
    "provider_password",
    "provider_missing",
    "busy_provider_ids",
    "provider_slot_second",
    "running_count",
    "seed_from_legacy",
    "providers_view",
    "delete_provider",
    "mark_blocked",
    "clear_block",
    "ProviderError",
]


class ProviderError(RuntimeError):
    """Действие с аккаунтом нельзя выполнить — с объяснением для человека."""

LEGACY_PROVIDER_NAME = "Основной аккаунт"

# Как в настройках: пустое поле — не менять, «-» — стереть.
MASK = "••••••••"


# --------------------------------------------------------------------------- #
# Каталоги
# --------------------------------------------------------------------------- #


def providers_root() -> Path:
    # Читаем config.DATA_DIR в момент вызова, а не при импорте: тесты и
    # переменная TICKET_DATA_DIR подменяют его уже после загрузки модуля.
    return config.DATA_DIR / "providers"


def provider_dir(provider_id: int) -> Path:
    path = providers_root() / str(int(provider_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def provider_journal_path(provider_id: int) -> Path:
    return provider_dir(provider_id) / "api_bookings.json"


def provider_state_path(provider_id: int) -> Path:
    return provider_dir(provider_id) / "api_state.json"


def provider_env_path(provider_id: int) -> Path:
    return provider_dir(provider_id) / ".env.local"


# --------------------------------------------------------------------------- #
# Чтение и запись
# --------------------------------------------------------------------------- #


def list_providers(
    db, service: str = Service.AIRLINES.value, *, enabled_only: bool = False
) -> list[Provider]:
    query = db.query(Provider).filter(Provider.service == service)
    if enabled_only:
        query = query.filter(Provider.is_enabled.is_(True))
    return query.order_by(Provider.id).all()


def get_provider(db, provider_id: int | None) -> Provider | None:
    if not provider_id:
        return None
    return db.get(Provider, int(provider_id))


def provider_password(provider: Provider) -> str:
    """Расшифрованный пароль личного кабинета. Пусто — не задан или не читается."""
    if not provider.password_enc:
        return ""
    try:
        return decrypt(provider.password_enc)
    except SecretUnreadable as exc:
        print(f"[providers] {provider.title}: {exc}")
        return ""


def save_provider(db, provider: Provider | None, data: dict[str, Any]) -> Provider:
    """
    Создать или обновить провайдера.

    Пароль по тем же правилам, что и в настройках: пустое поле — не менять
    (иначе правка названия стирала бы пароль), «-» — стереть.
    """
    # Проверяем до любых правок: на ошибке аккаунт должен остаться как был.
    source_ip = _checked_source_ip(db, str(data["source_ip"] or "")) if "source_ip" in data else None

    if provider is None:
        provider = Provider(service=str(data.get("service") or Service.AIRLINES.value))
        db.add(provider)

    if "name" in data:
        provider.name = str(data["name"] or "").strip()[:64]
    if "login" in data:
        provider.login = str(data["login"] or "").strip()[:64]
    if "is_enabled" in data:
        provider.is_enabled = bool(data["is_enabled"])
    if "contact_email" in data:
        provider.contact_email = str(data["contact_email"] or "").strip()[:255]
    if "contact_phone" in data:
        provider.contact_phone = str(data["contact_phone"] or "").strip()[:32]

    if "password" in data:
        raw = str(data["password"] or "").strip()
        if raw == "-":
            provider.password_enc = ""
        elif raw and raw != MASK:
            provider.password_enc = encrypt(raw)

    if source_ip is not None:
        provider.source_ip = source_ip

    if "check_at_second" in data:
        raw = str(data["check_at_second"] if data["check_at_second"] is not None else "").strip()
        if raw == "":
            provider.check_at_second = None
        else:
            try:
                provider.check_at_second = max(0, min(59, int(raw)))
            except ValueError:
                pass  # мусор — оставляем прежнее значение

    db.commit()
    return provider


def _checked_source_ip(db, raw: str) -> str:
    """
    Адрес для аккаунта — только из списка в настройках. Пусто — по умолчанию.

    Опечатка в адресе не должна молча увести аккаунт на чужой IP: бот с
    адресом, которого нет на машине, не запустится, а адрес не из списка
    панель не покажет в лестнице 429.
    """
    from web.services.source_ips import SourceIpError, configured_ips, normalize_ip

    try:
        ip = normalize_ip(raw)
    except SourceIpError as exc:
        raise ProviderError(str(exc)) from exc
    if ip and ip not in {item["ip"] for item in configured_ips(db)}:
        raise ProviderError(
            f"Адреса {ip} нет в списке исходящих адресов — добавьте его в настройках самолётов"
        )
    return ip


def provider_missing(provider: Provider) -> list[str]:
    """Чего не хватает аккаунту, чтобы с ним можно было работать."""
    missing: list[str] = []
    if not provider.login:
        missing.append("телефон аккаунта T5")
    if not provider.password_enc:
        missing.append("пароль аккаунта T5")
    return missing


# --------------------------------------------------------------------------- #
# Разнесение по времени
# --------------------------------------------------------------------------- #


def provider_slot_second(db, provider: Provider, *, period_sec: int, base_second: int) -> int:
    """
    Секунда первой отметки для аккаунта.

    Все аккаунты ходят с одного адреса, а сайт режет запросы по адресу. Если
    три процесса бьют в одну и ту же секунду, для сайта это одна тройная
    очередь. Поэтому, если человек не задал секунду сам, отметки разносятся
    равномерно внутри периода: при 30 секундах и двух аккаунтах — :00 и :15.

    Порядок — по номеру провайдера среди включённых, а не среди работающих:
    иначе отметка аккаунта менялась бы от того, кто ещё запущен, и два
    процесса могли бы съехаться в одну секунду.
    """
    if provider.check_at_second is not None:
        return provider.check_at_second

    enabled_ids = [p.id for p in list_providers(db, provider.service, enabled_only=True)]
    index = enabled_ids.index(provider.id) if provider.id in enabled_ids else len(enabled_ids)
    count = max(1, len(enabled_ids))
    # Реже раза в минуту отметка одна, разносить можно по всей минуте.
    span = min(normalize_check_period(period_sec), 60)
    step = max(1, span // count)
    return (base_second + index * step) % 60


# --------------------------------------------------------------------------- #
# Занятость
# --------------------------------------------------------------------------- #


def busy_provider_ids(db, *, exclude_job_id: int | None = None) -> dict[int, int]:
    """Провайдер → процесс, который его сейчас занимает."""
    query = db.query(Job).filter(
        Job.provider_id.isnot(None),
        Job.status == JobStatus.RUNNING.value,
    )
    if exclude_job_id is not None:
        query = query.filter(Job.id != exclude_job_id)
    return {job.provider_id: job.id for job in query.all()}


def running_count(db, service: str, *, exclude_job_id: int | None = None) -> int:
    query = db.query(Job).filter(
        Job.service == service, Job.status == JobStatus.RUNNING.value
    )
    if exclude_job_id is not None:
        query = query.filter(Job.id != exclude_job_id)
    return query.count()


# --------------------------------------------------------------------------- #
# Для страницы
# --------------------------------------------------------------------------- #


def providers_view(db, service: str = Service.AIRLINES.value) -> list[dict[str, Any]]:
    """Аккаунты с состоянием — всё, что нужно форме, без паролей."""
    from web.services.bookings import provider_blocker
    from web.services.settings import get_service_config
    from web.services.source_ips import effective_ip, ip_title

    config = get_service_config(db, service)
    busy = busy_provider_ids(db)
    out: list[dict[str, Any]] = []
    all_providers = list_providers(db, service)
    for provider in all_providers:
        blocker = provider_blocker(db, provider.id)
        missing = provider_missing(provider)
        second = provider_slot_second(
            db, provider, period_sec=config.check_period_sec, base_second=config.check_at_second
        )
        if provider.blocked_at is not None:
            state, tone = f"заблокирован перевозчиком с {provider.blocked_at:%d.%m %H:%M}", "tag-error"
        elif not provider.is_enabled:
            state, tone = "выключен", ""
        elif missing:
            state, tone = "не заполнено: " + ", ".join(missing), "tag-error"
        elif provider.id in busy:
            state, tone = f"занят процессом #{busy[provider.id]}", "tag-ok"
        elif blocker is not None:
            state, tone = f"незакрытая попытка брони ({blocker.record.status})", "tag-error"
        else:
            state, tone = "свободен", "tag-ok"
        out.append(
            {
                "id": provider.id,
                "title": provider.title,
                "name": provider.name,
                "login": provider.login,
                "password_set": bool(provider.password_enc),
                "is_enabled": provider.is_enabled,
                # Строкой: 0 — законная секунда, а шаблон пустое `0` не показал бы.
                "check_at_second_raw": (
                    "" if provider.check_at_second is None else str(provider.check_at_second)
                ),
                "slot_second": second,
                "source_ip": provider.source_ip,
                # Реальный адрес — «по умолчанию» тоже конкретный IP, и его
                # человек должен видеть: с него может ходить другой аккаунт.
                "ip_title": ip_title(db, effective_ip(provider)),
                "slot_auto": provider.check_at_second is None,
                "busy_job": busy.get(provider.id),
                "blocked_at": provider.blocked_at,
                "blocked_reason": provider.blocked_reason,
                "contact_email": provider.contact_email,
                "contact_phone": provider.contact_phone,
                "contact_warning": contact_warning(provider, all_providers),
                "state": state,
                "tone": tone,
            }
        )
    return out


def _same(a: str, b: str) -> bool:
    """Тот же контакт: регистр и всё, кроме букв, цифр и @, не важны."""
    def norm(value: str) -> str:
        return "".join(ch for ch in (value or "").lower() if ch.isalnum() or ch == "@")

    return bool(norm(a)) and norm(a) == norm(b)


def contact_warning(provider: Provider, providers: list[Provider]) -> str:
    """
    Предупредить, если по контактам брони аккаунты легко связать между собой.

    Перевозчик видит email и телефон в каждой брони. Одни и те же контакты
    у нескольких аккаунтов — это для него один человек с несколькими
    кабинетами: так блок одного аккаунта потянул за собой второй.
    """
    if not (provider.contact_email and provider.contact_phone):
        return (
            "Контакты брони не заданы — берутся общие из настроек, одни на все "
            "аккаунты. Перевозчику так легко связать аккаунты между собой."
        )
    for other in providers:
        if other.id == provider.id:
            continue
        if _same(other.contact_email, provider.contact_email) or _same(
            other.contact_phone, provider.contact_phone
        ):
            return f"Те же контакты брони, что у «{other.title}» — аккаунты легко связать."
    return ""


def mark_blocked(db, provider: Provider, reason: str) -> None:
    """Отметить блок перевозчиком. Повторная отметка время первой не сдвигает."""
    import datetime as dt

    if provider.blocked_at is None:
        provider.blocked_at = dt.datetime.now()
    provider.blocked_reason = (reason or "")[:300]


def clear_block(db, provider: Provider) -> str:
    """Человек убедился, что блок сняли: аккаунт снова можно запускать."""
    if provider.blocked_at is None:
        return f"Аккаунт «{provider.title}» и не был заблокирован"
    provider.blocked_at = None
    provider.blocked_reason = ""
    db.commit()
    return (
        f"Отметка о блоке у «{provider.title}» снята. Запускайте осторожно: "
        "если блок на самом деле не снят, бот остановится на первом же ответе."
    )


def delete_provider(db, provider: Provider) -> str:
    """
    Удалить аккаунт. Отказ — если им что-то пользуется.

    Каталог с журналом НЕ удаляем. Если в журнале была настоящая бронь,
    её след должен остаться, а панель свои записи о бронях уже хранит в базе.
    Но аккаунт с незакрытой попыткой удалить нельзя вовсе: панель перестала бы
    видеть его журнал, блокировка молча исчезла бы, и те же люди ушли бы
    в бронь на другом аккаунте.
    """
    from web.services.bookings import provider_blocker

    busy = busy_provider_ids(db)
    if provider.id in busy:
        raise ProviderError(
            f"Аккаунт «{provider.title}» занят процессом #{busy[provider.id]} — "
            f"сначала остановите его"
        )
    if provider_blocker(db, provider.id) is not None:
        raise ProviderError(
            f"В журнале аккаунта «{provider.title}» незакрытая попытка брони. "
            f"Разберитесь с ней в разделе «Брони» или просто выключите аккаунт."
        )
    title = provider.title
    db.delete(provider)
    db.commit()
    return f"Аккаунт «{title}» удалён"


# --------------------------------------------------------------------------- #
# Переезд со старой схемы
# --------------------------------------------------------------------------- #


def seed_from_legacy(db) -> Provider | None:
    """
    Превратить аккаунт из настроек самолётов в первого провайдера.

    Вызывается один раз, пока провайдеров нет. Кроме аккаунта переносит его
    файлы: журнал броней и состояние. Журнал именно копируется, а не
    переносится — там может лежать незакрытая попытка брони, и запуск бота
    из командной строки обязан её по-прежнему видеть. Панель при этом
    понимает, что это одна и та же запись, и показывает её один раз.
    """
    from web.models import Booking, ServiceConfig

    if db.query(Provider).filter(Provider.service == Service.AIRLINES.value).count():
        return None

    legacy = db.get(ServiceConfig, Service.AIRLINES.value)
    if legacy is None or not (legacy.login or legacy.password_enc):
        return None

    provider = Provider(
        service=Service.AIRLINES.value,
        name=LEGACY_PROVIDER_NAME,
        login=legacy.login,
        # Зашифровано тем же ключом — переносим как есть, не расшифровывая.
        password_enc=legacy.password_enc,
        is_enabled=True,
    )
    db.add(provider)
    db.flush()

    # Всё, что было сделано до провайдеров, сделано от этого аккаунта.
    db.query(Job).filter(
        Job.service == Service.AIRLINES.value, Job.provider_id.is_(None)
    ).update({Job.provider_id: provider.id}, synchronize_session=False)
    db.query(Booking).filter(
        Booking.service == Service.AIRLINES.value, Booking.provider_id.is_(None)
    ).update({Booking.provider_id: provider.id}, synchronize_session=False)
    db.commit()

    for name, target in (
        ("api_bookings.json", provider_journal_path(provider.id)),
        ("api_state.json", provider_state_path(provider.id)),
    ):
        source = config.AIRLINES_API_DIR / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)

    print(f"[providers] аккаунт из настроек стал провайдером #{provider.id}")
    return provider
