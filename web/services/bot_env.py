"""Подготовка запуска бота: временный `check.env` и `passengers.json`.

Боты не переписывались под панель — они по-прежнему читают настройки из файла
окружения и список пассажиров из JSON. Панель просто готовит эти файлы из базы
перед каждым запуском. Отлаженная логика антибота (тайминги слотов, ротация
User-Agent, backoff, защита от повторной брони) при этом не трогается вовсе.

Почему именно файл, а не переменные окружения процесса: внутри бота стоит
`load_dotenv(path, override=True)`, и он перезаписал бы всё, что мы передали
через `os.environ`. Файл — единственный способ говорить с ботом на его языке.

Почта у бота намеренно НЕ настраивается: `GMAIL_*` не передаём, а вместо них
ставим `NOTIFY_EXTERNAL=1`. Бот тогда не пытается слать письма и, что важнее,
не пишет в лог «Gmail не настроен» — при запуске из панели это ложная тревога.
Рассылает панель: у неё и настройки, и история отправок.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from web.config import AIRLINES_API_DIR, RAILWAY_DATA_DIR, RAILWAY_DIR, RUNS_DIR
from web.services.bot_logs import logs_dir
from web.models import Job, Service
from web.services.settings import get_service_config, get_service_secret

__all__ = [
    "BotLaunch",
    "prepare_launch",
    "cleanup_run_dir",
    "local_env_warning",
    "booking_contacts",
    "SERVICE_DIRS",
    "SERVICE_DATA_DIRS",
    "SERVICE_SCRIPTS",
]

SERVICE_DIRS = {
    Service.RAILWAY.value: RAILWAY_DIR,
    Service.AIRLINES.value: AIRLINES_API_DIR,
}

SERVICE_SCRIPTS = {
    Service.RAILWAY.value: "railway_check.py",
    Service.AIRLINES.value: "api_check.py",
}

# Где боты держат свои журналы. Для поезда это не обязательно каталог кода:
# в контейнере журнал лежит в томе с данными (RAILWAY_DATA_DIR).
SERVICE_DATA_DIRS = {
    Service.RAILWAY.value: RAILWAY_DATA_DIR,
    Service.AIRLINES.value: AIRLINES_API_DIR,
}

# Журналы броней ботов: панель читает их после выхода процесса, чтобы узнать
# номер брони и ссылку на оплату. Разбирать ради этого печать в консоль не нужно.
SERVICE_JOURNALS = {
    Service.RAILWAY.value: "railway_bookings.json",
    Service.AIRLINES.value: "api_bookings.json",
}


class BotLaunch:
    """Всё, что нужно, чтобы запустить бота: где, чем и с каким окружением."""

    def __init__(self, *, service: str, run_dir: Path, env_file: Path, cwd: Path, script: Path):
        self.service = service
        self.run_dir = run_dir
        self.env_file = env_file
        self.cwd = cwd
        self.script = script

    @property
    def args(self) -> list[str]:
        """Аргументы после пути к интерпретатору."""
        return [str(self.script), "--env", str(self.env_file)]

    @property
    def journal(self) -> Path:
        return SERVICE_DATA_DIRS[self.service] / SERVICE_JOURNALS[self.service]


def local_env_warning(service: str) -> str:
    """
    Предупредить, если рядом с ботом лежит `.env.local`.

    Бот грузит его ПОСЛЕ нашего файла и с `override=True`, то есть значения
    оттуда побеждают настройки панели. Для запусков из командной строки это
    задумано, но человек должен знать, что панель тут не главная.
    """
    if service == Service.AIRLINES.value:
        # Самолётному боту панель даёт свой файл на аккаунт (API_LOCAL_ENV),
        # и общий airlines_api/.env.local он в этом случае не читает вовсе.
        return ""
    local = SERVICE_DIRS[service] / ".env.local"
    if not local.exists():
        return ""
    return (
        f"Внимание: {local} перекрывает настройки панели — "
        f"бот читает его после нашего файла. Переименуйте, если это не нужно."
    )


def booking_contacts(db, job: Job) -> tuple[str, str]:
    """
    Куда сервис перевозчика пришлёт подтверждение брони: (email, телефон).

    Порядок: настройки процесса → значения по умолчанию из настроек панели.
    Пассажирский email бот подставит сам, если тут пусто, — но полагаться на
    это нельзя: у пассажира почты может не быть вовсе, и тогда сервер T5
    отвечает `Error Create (HTTP 404)`, отвергая бронь на валидации.

    Это НЕ то же самое, что адрес рассылки панели: там мы шлём ссылку на оплату
    себе, а здесь — говорим перевозчику, на чьё имя оформляется бронь.
    Совпадают они по умолчанию, но развести их должно быть можно.
    """
    from web.services.settings import get_setting  # noqa: PLC0415

    email = (job.notify_email or "").strip() or get_setting(db, "default_notify_email")
    phone = (job.notify_phone or "").strip() or get_setting(db, "default_notify_phone")
    # Свои контакты аккаунта — первыми: общие на все аккаунты позволяли
    # перевозчику связать их между собой (см. Provider.contact_email).
    provider = job.provider if job.service == Service.AIRLINES.value else None
    if provider is not None:
        email = provider.contact_email or email
        phone = provider.contact_phone or phone
    return email.strip(), phone.strip()


def _format_env(values: dict[str, Any]) -> str:
    """
    Собрать содержимое `.env`.

    Значения не экранируются кавычками намеренно: python-dotenv снял бы их,
    и пароль с кавычкой внутри поехал бы. Все значения здесь однострочные,
    переводы строк вырезаем.
    """
    lines = ["# Файл создан панелью автоматически. Правки будут затёрты."]
    for key, value in values.items():
        if value is None:
            continue
        text = str(value).replace("\r", " ").replace("\n", " ")
        lines.append(f"{key}={text}")
    return "\n".join(lines) + "\n"


def _railway_env(db, job: Job, passengers_file: Path, passenger_ids: str) -> dict[str, Any]:
    config = get_service_config(db, Service.RAILWAY.value)
    params = job.params
    contact_email, contact_phone = booking_contacts(db, job)

    values: dict[str, Any] = {
        # Рассылку берёт на себя панель — бот не должен ни слать письма,
        # ни жаловаться, что Gmail не настроен.
        "NOTIFY_EXTERNAL": "1",
        "SOURCE_STATION_ID": job.source,
        "DESTINATION_STATION_ID": job.destination,
        "DATE": job.depart_date,
        "PASSENGERS_FILE": passengers_file,
        "PASSENGER_IDS": passenger_ids,
        "RAILWAY_CHECK_PERIOD_SEC": config.check_period_sec,
        "RAILWAY_CHECK_AT_SEC": config.check_at_second,
        "RAILWAY_AUTO_BOOK": "1" if config.auto_book else "0",
        "RAILWAY_MULTIMEDIA": "1" if params.get("multimedia") else "0",
        "RAILWAY_STRICT": "1" if params.get("strict") else "0",
        # Контакт брони. Без него бот брал почту и телефон первого пассажира,
        # а если их нет — слал пустые, и сервер отвечал input.invalidInputParam.
        "RAILWAY_CONTACT_EMAIL": contact_email,
        "RAILWAY_CONTACT_PHONE": contact_phone,
        # Каждый HTTP-запрос — строкой в файл дня (core.http_journal).
        "BOT_HTTP_LOG_DIR": logs_dir(),
    }

    # Тип вагона и выбор поезда — необязательные: без них бот смотрит все.
    if params.get("wagon_type_id"):
        values["WAGON_TYPE_ID"] = params["wagon_type_id"]
    if params.get("train_index"):
        values["TRAIN_INDEX"] = params["train_index"]
    elif params.get("train_time"):
        values["TRAIN_TIME"] = params["train_time"]
    elif params.get("train_run_number"):
        values["TRAIN_RUN_NUMBER"] = params["train_run_number"]

    return values


def _airlines_env(db, job: Job, passengers_file: Path, passenger_ids: str) -> dict[str, Any]:
    from web.services.providers import (
        provider_env_path,
        provider_journal_path,
        provider_password,
        provider_slot_second,
        provider_state_path,
    )

    from web.services.rate_limit import state_path as rate_limit_state_path

    config = get_service_config(db, Service.AIRLINES.value)
    params = job.params
    contact_email, contact_phone = booking_contacts(db, job)

    provider = job.provider
    if provider is None:
        # Выбирает провайдера запуск (runner.start_job). Сюда без него попасть
        # не должны — а если попали, лучше не запускать, чем пойти с пустым
        # аккаунтом и получить отказ авторизации уже на сайте.
        raise ValueError("не выбран аккаунт перевозчика (провайдер)")

    values: dict[str, Any] = {
        # См. комментарий в _railway_env: письма и SMS отправляет панель.
        "NOTIFY_EXTERNAL": "1",
        "DEPART_PORT_CODE": job.source,
        "ARRIVAL_PORT_CODE": job.destination,
        "DATE": job.depart_date,
        "PASSENGERS_FILE": passengers_file,
        "PASSENGER_IDS": passenger_ids,
        # Доступы: по ним бот сам получает и обновляет Bearer-токены.
        # Аккаунт — у провайдера, учётка приложения — общая в настройках.
        "API_CUSTOMER_PHONE": provider.login,
        "API_CUSTOMER_PASSWORD": provider_password(provider),
        "API_IOS_LOGIN": config.extra_login or "ios",
        "API_IOS_PASSWORD": get_service_secret(db, Service.AIRLINES.value, "extra_password_enc"),
        "API_AUTO_REFRESH_TOKENS": "1",
        # Токены в .env.local не пишем: панель запускает бота из своего каталога
        # и не должна править файлы модуля.
        "API_SAVE_TOKENS": "0",
        "API_CHECK_PERIOD_SEC": config.check_period_sec,
        # «Поисков в час» из настроек. Раньше до бота не доходило вовсе:
        # при 30 в час он делал 60, круглые сутки.
        "API_MAX_SEARCHES_PER_HOUR": config.max_searches_per_hour,
        # Ночная пауза и часы точных слотов (core.hours).
        "API_QUIET_HOURS": config.quiet_hours,
        "API_EXACT_HOURS": config.exact_hours,
        # У каждого аккаунта своя секунда: с одного адреса в одну и ту же
        # секунду — ровно та картина, по которой сайт режет запросы.
        "API_CHECK_AT_SEC": provider_slot_second(
            db, provider, period_sec=config.check_period_sec, base_second=config.check_at_second
        ),
        # Свои файлы у каждого аккаунта. Общими они перетирали бы друг друга:
        # отпечаток аккаунта, журнал броней, состояние монитора.
        "API_LOCAL_ENV": provider_env_path(provider.id),
        "API_BOOKINGS_JOURNAL": provider_journal_path(provider.id),
        "API_STATE_FILE": provider_state_path(provider.id),
        # Картинка капчи — своя у запуска: иначе один процесс решал бы чужую.
        "API_CAPTCHA_IMAGE": passengers_file.parent / "captcha_last.png",
        # Лестница пауз после 429 — ОДНА на все процессы самолётов: все они
        # ходят с одного адреса, и 429 у одного аккаунта — пауза для всех.
        # Аккаунт ходит со своего адреса, и лестница 429 у адреса своя.
        # Без явного адреса бот уйдёт с адреса по умолчанию — его и записали
        # в job.source_ip при запуске.
        "API_SOURCE_IP": provider.source_ip,
        # Настоящий адрес, в том числе «по умолчанию», — для журнала запросов:
        # без него строки аккаунта с адресом по умолчанию шли с пустым IP.
        "API_EFFECTIVE_IP": job.source_ip,
        "API_RATE_LIMIT_STATE": rate_limit_state_path(job.source_ip),
        # Чей аккаунт поймал 429 — для журнала ошибок и письма о стопе.
        "API_PROVIDER_NAME": provider.title,
        "API_AUTO_BOOK": "1" if config.auto_book else "0",
        "API_REPEATED_ERROR_PAUSE_AFTER": config.repeated_error_pause_after,
        # Капча: процесс не ждёт человека. Нерешённые уходят в раздел «Капчи».
        "CAPTCHA_API_MANUAL": "1" if config.captcha_manual else "0",
        "CAPTCHA_API_MAX_ATTEMPTS": config.captcha_max_attempts,
        "CAPTCHA_API_COLLECT": "1",
        "CAPTCHA_API_COLLECT_REJECTED": "1",
        "FLIGHT_STRICT": "1" if params.get("strict") else "0",
        # Контакты для брони. Без email сервер T5 отвергает CreateBooking
        # с `Error Create (HTTP 404)`: на этот адрес он шлёт подтверждение
        # и ссылку на оплату. Раньше панель их не передавала вовсе, и бот
        # оставался с пустым полем, если у пассажира не было почты.
        "API_CONTACT_EMAIL": contact_email,
        "API_CONTACT_PHONE": contact_phone,
        # Каждый HTTP-запрос — строкой в файл дня (core.http_journal):
        # консоль процесса — окно, а разбор блока требует всей истории.
        "BOT_HTTP_LOG_DIR": logs_dir(),
    }

    if params.get("flight_time"):
        values["FLIGHT_TIME"] = params["flight_time"]
    elif params.get("flight_index"):
        values["FLIGHT_INDEX"] = params["flight_index"]
    # Ни то ни другое не задано — переменную не передаём вовсе. Для бота это
    # «рейс не выбран»: он смотрит все прямые рейсы с местами, как railway
    # с пустым выбором поезда.

    return values


_BUILDERS = {
    Service.RAILWAY.value: _railway_env,
    Service.AIRLINES.value: _airlines_env,
}


def prepare_launch(db, job: Job) -> BotLaunch:
    """
    Разложить всё нужное для запуска в отдельный каталог процесса.

    Каталог свой у каждого запуска: два процесса не должны драться за один
    `passengers.json`, а после завершения остаётся ровно то, с чем бот работал,
    — это сильно упрощает разбор «почему не сработало».
    """
    if job.service not in _BUILDERS:
        raise ValueError(f"неизвестный сервис: {job.service!r}")

    run_dir = RUNS_DIR / f"job-{job.id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    passengers = [link.passenger.to_bot_dict() for link in sorted(job.passengers, key=lambda x: x.position)]
    if not passengers:
        raise ValueError("в процессе нет пассажиров")

    passengers_file = run_dir / "passengers.json"
    passengers_file.write_text(
        json.dumps(passengers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passenger_ids = ",".join(str(p["id"]) for p in passengers)

    values = _BUILDERS[job.service](db, job, passengers_file, passenger_ids)

    env_file = run_dir / "check.env"
    env_file.write_text(_format_env(values), encoding="utf-8")

    cwd = SERVICE_DIRS[job.service]
    return BotLaunch(
        service=job.service,
        run_dir=run_dir,
        env_file=env_file,
        cwd=cwd,
        script=cwd / SERVICE_SCRIPTS[job.service],
    )


def cleanup_run_dir(job: Job) -> None:
    """
    Удалить каталог запуска.

    Вызывается только по явной команде: там лежит `check.env` с паролями,
    но там же лежит и всё, что нужно для разбора неудачного запуска.
    """
    if not job.run_dir:
        return
    path = Path(job.run_dir)
    if path.exists() and path.is_dir() and path.parent == RUNS_DIR:
        shutil.rmtree(path, ignore_errors=True)
