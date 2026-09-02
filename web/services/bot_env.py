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

from web.config import AIRLINES_API_DIR, RAILWAY_DIR, RUNS_DIR
from web.models import Job, Service
from web.services.settings import get_service_config, get_service_secret

__all__ = [
    "BotLaunch",
    "prepare_launch",
    "cleanup_run_dir",
    "local_env_warning",
    "booking_contacts",
    "SERVICE_DIRS",
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
        return self.cwd / SERVICE_JOURNALS[self.service]


def local_env_warning(service: str) -> str:
    """
    Предупредить, если рядом с ботом лежит `.env.local`.

    Бот грузит его ПОСЛЕ нашего файла и с `override=True`, то есть значения
    оттуда побеждают настройки панели. Для запусков из командной строки это
    задумано, но человек должен знать, что панель тут не главная.
    """
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
    config = get_service_config(db, Service.AIRLINES.value)
    params = job.params
    contact_email, contact_phone = booking_contacts(db, job)

    values: dict[str, Any] = {
        # См. комментарий в _railway_env: письма и SMS отправляет панель.
        "NOTIFY_EXTERNAL": "1",
        "DEPART_PORT_CODE": job.source,
        "ARRIVAL_PORT_CODE": job.destination,
        "DATE": job.depart_date,
        "PASSENGERS_FILE": passengers_file,
        "PASSENGER_IDS": passenger_ids,
        # Доступы: по ним бот сам получает и обновляет Bearer-токены.
        "API_CUSTOMER_PHONE": config.login,
        "API_CUSTOMER_PASSWORD": get_service_secret(db, Service.AIRLINES.value, "password_enc"),
        "API_IOS_LOGIN": config.extra_login or "ios",
        "API_IOS_PASSWORD": get_service_secret(db, Service.AIRLINES.value, "extra_password_enc"),
        "API_AUTO_REFRESH_TOKENS": "1",
        # Токены в .env.local не пишем: панель запускает бота из своего каталога
        # и не должна править файлы модуля.
        "API_SAVE_TOKENS": "0",
        "API_CHECK_PERIOD_SEC": config.check_period_sec,
        "API_CHECK_AT_SEC": config.check_at_second,
        "API_AUTO_BOOK": "1" if config.auto_book else "0",
        "API_RATE_LIMIT_PAUSE_SEC": config.rate_limit_pause_sec,
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
