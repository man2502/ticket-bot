"""Загрузка check.env для HTTP-проверки рейсов T5."""

from __future__ import annotations

import os
import random
import secrets
import sys
import time
from pathlib import Path

REPO_ROOT_PATH = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_PATH))

from dotenv import load_dotenv

from core.env import env

IOS_UA_VERSIONS = ("26.5", "26.4", "26.2", "26.1")
IPHONE_UA_MODELS = (
    "16 Pro",
    "16",
    "16 Pro Max",
    "15",
    "15 Pro",
    "15 Pro Max",
    "17 Pro",
    "17",
    "17 Pro Max",
)
_UA_ROTATE_MIN_SEC = 45 * 60
_UA_ROTATE_MAX_SEC = 55 * 60

_ios_user_agent = ""
_ios_user_agent_until = 0.0

API_ROOT = Path(__file__).resolve().parent
REPO_ROOT = API_ROOT.parent
DEFAULT_API_ENV = API_ROOT / "check.env"
API_LOCAL_ENV = API_ROOT / ".env.local"
LEGACY_LOCAL_ENV = REPO_ROOT / ".env.local"
_loaded_env_path: Path | None = None


# Реализация общая — core.env (там же описано, почему пустое значение = default).


def load_api_env(path: Path | str | None = None) -> Path:
    """
    1) check.env — маршрут, дата, логин клиента
    2) airlines_api/.env.local — ios password, Gmail, CAPTCHA
    """
    if path is None:
        candidate = DEFAULT_API_ENV
    else:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = API_ROOT / candidate

    if not candidate.exists():
        raise SystemExit(
            f"Нет {candidate}\n"
            f"Создай: cp check.env.example check.env"
        )

    load_dotenv(candidate, override=True)
    notify_email = env("NOTIFY_EMAIL")
    global _loaded_env_path
    _loaded_env_path = candidate
    print(f"[api-env] {candidate.name}")

    if API_LOCAL_ENV.exists():
        load_dotenv(API_LOCAL_ENV, override=True)
        print(f"[api-env] {API_LOCAL_ENV.name}")
    elif LEGACY_LOCAL_ENV.exists():
        load_dotenv(LEGACY_LOCAL_ENV, override=True)
        print(f"[api-env] ../{LEGACY_LOCAL_ENV.name} (лучше: cp .env.local.example .env.local в airlines_api/)")

    # Источник списка получателей — check.env, локальные секреты не должны его затирать.
    if notify_email:
        os.environ["NOTIFY_EMAIL"] = notify_email

    return candidate


def api_env_path() -> Path:
    return _loaded_env_path or DEFAULT_API_ENV


def api_secrets_env_path() -> Path:
    """Локальный env для Bearer-токенов (airlines_api/.env.local)."""
    return API_LOCAL_ENV


def to_api_date(raw: str) -> str:
    """DD.MM.YYYY или MM/DD/YYYY → MM/DD/YYYY для GetAirAvailability."""
    text = (raw or "").strip()
    if not text:
        raise SystemExit("DATE пуст — укажи в check.env, напр. DATE=31.07.2026")

    if "/" in text:
        parts = text.split("/")
        if len(parts) != 3:
            raise SystemExit(f"DATE: не разобрал {text!r}")
        month, day, year = (int(parts[0]), int(parts[1]), int(parts[2]))
        return f"{month:02d}/{day:02d}/{year}"

    if "." in text:
        parts = text.split(".")
        if len(parts) != 3:
            raise SystemExit(f"DATE: не разобрал {text!r}")
        day, month, year = (int(parts[0]), int(parts[1]), int(parts[2]))
        return f"{month:02d}/{day:02d}/{year}"

    raise SystemExit(f"DATE: не разобрал {text!r} — нужен DD.MM.YYYY или MM/DD/YYYY")


def api_build_version() -> int:
    raw = env("API_BUILD_VERSION", "79") or "79"
    try:
        return max(1, int(raw))
    except ValueError:
        return 79


def generate_ios_user_agent() -> str:
    """iOS/{ver}; iPhone {model}; v79; {32 hex}."""
    ios = random.choice(IOS_UA_VERSIONS)
    phone = random.choice(IPHONE_UA_MODELS)
    device_id = secrets.token_hex(16)
    return f"iOS/{ios}; iPhone {phone}; v{api_build_version()}; {device_id}"


def current_ios_user_agent(*, force: bool = False) -> str:
    """Новый UA при старте и каждые 45–55 минут."""
    global _ios_user_agent, _ios_user_agent_until
    now = time.monotonic()
    if force or not _ios_user_agent or now >= _ios_user_agent_until:
        prev = _ios_user_agent
        _ios_user_agent = generate_ios_user_agent()
        interval = random.uniform(_UA_ROTATE_MIN_SEC, _UA_ROTATE_MAX_SEC)
        _ios_user_agent_until = now + interval
        if prev:
            print(
                f"[ua] сменён: {_ios_user_agent}  "
                f"(следующая смена через {interval / 60:.0f} мин)",
                flush=True,
            )
    return _ios_user_agent


def ios_user_agent_rotate_in_min() -> float:
    return max(0.0, (_ios_user_agent_until - time.monotonic()) / 60.0)


def build_api_headers() -> dict[str, str]:
    """Заголовки как в iOS-приложении (curl checkVersion / GetAirAvailability)."""
    authorization = env("API_AUTHORIZATION")
    customer = env("API_CUSTOMER_AUTHORIZATION")

    missing = [
        name
        for name, val in (
            ("API_AUTHORIZATION", authorization),
            ("API_CUSTOMER_AUTHORIZATION", customer),
        )
        if not val
    ]
    if missing:
        raise SystemExit(f"Не хватает в airlines_api/.env.local: {', '.join(missing)}")

    if not authorization.lower().startswith("bearer "):
        authorization = f"Bearer {authorization}"
    if not customer.lower().startswith("bearer "):
        customer = f"Bearer {customer}"

    return {
        "accept-encoding": "deflate, gzip",
        "authorization": authorization,
        "content-type": "application/json",
        "host": env("API_HOST", "turkmenistanairlines.tm") or "turkmenistanairlines.tm",
        "user-agent": current_ios_user_agent(),
        "x-customer-authorization": customer,
        "x-language": env("API_LANGUAGE", "ru") or "ru",
    }
