"""Пути и настройки самого приложения (не путать с настройками сервисов в БД).

Здесь только то, что нужно знать ДО подключения к базе: где лежит база, где
ключ шифрования, на каком порту слушать. Всё остальное — SMTP, пароли T5,
интервалы проверок — хранится в БД и настраивается через веб.

Переменные окружения:
    TICKET_DB_PATH      путь к SQLite-базе (по умолчанию web/data/ticket.db)
    TICKET_SECRET_KEY   ключ Fernet для секретов в БД
    TICKET_SESSION_KEY  ключ подписи cookie сессии
    TICKET_HOST/PORT    адрес и порт uvicorn
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WEB_ROOT.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Рабочие данные приложения: база, ключи, картинки капч, временные env ботов.
DATA_DIR = Path(os.getenv("TICKET_DATA_DIR") or (WEB_ROOT / "data"))

TEMPLATES_DIR = WEB_ROOT / "templates"
STATIC_DIR = WEB_ROOT / "static"

# Файлы-ключи держим отдельно от базы: копия .db, унесённая на другую машину,
# без ключа бесполезна — пароли из неё не достать.
SECRET_KEY_FILE = DATA_DIR / "secret.key"
SESSION_KEY_FILE = DATA_DIR / "session.key"

# Картинки капч, которые CNN не смогла решить. Разбирает админ.
CAPTCHA_DIR = DATA_DIR / "captcha"

# Временные check.env и passengers.json, которые воркер готовит для ботов.
RUNS_DIR = DATA_DIR / "runs"

# Каталоги модулей-ботов. Имя railway-api с дефисом — поэтому импортировать
# его как пакет нельзя; воркер запускает скрипт по пути, а не импортом.
RAILWAY_DIR = REPO_ROOT / "railway-api"
AIRLINES_API_DIR = REPO_ROOT / "airlines_api"

# Данные поездного бота (журнал броней, состояние). Та же переменная, что
# читает сам бот (railway-api/railway_env.py), — иначе панель смотрела бы
# в один журнал, а бот писал бы в другой.
RAILWAY_DATA_DIR = Path(os.getenv("RAILWAY_DATA_DIR") or RAILWAY_DIR)


def db_path() -> Path:
    return Path(os.getenv("TICKET_DB_PATH") or (DATA_DIR / "ticket.db"))


def database_url() -> str:
    return f"sqlite:///{db_path()}"


def host() -> str:
    return os.getenv("TICKET_HOST") or "127.0.0.1"


def port() -> int:
    raw = os.getenv("TICKET_PORT") or "8000"
    try:
        return int(raw)
    except ValueError:
        print(f"[warn] TICKET_PORT={raw!r} — не число, беру 8000")
        return 8000


def ensure_dirs() -> None:
    """Создать рабочие каталоги. Вызывается при инициализации базы и на старте."""
    for directory in (DATA_DIR, CAPTCHA_DIR, RUNS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
