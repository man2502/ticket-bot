"""Авто-логин T5 API: customers/info ping, token_type → нужный login.

Транспорт общий с остальным модулем (`api_client` → `core.http`): раньше здесь
жила третья по счёту копия `urlopen` + `_decode_response`, со своим таймаутом
в 100 секунд и своим TCP-соединением. То есть каждый ping авторизации стоил
лишнего handshake прямо перед слотом проверки.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from typing import Any

from api_client import RequestFailed, api_call
from api_env import api_secrets_env_path, build_api_headers, env

_AUTH_FINGERPRINT_KEY = "API_AUTH_FINGERPRINT"


class AuthFatalError(RuntimeError):
    """Auth/refresh провалился повторно — монитор нужно остановить."""


class AuthTransientError(RuntimeError):
    """customers/info 429/5xx — этот цикл поиска пропускаем."""


def auto_refresh_tokens_enabled() -> bool:
    return (env("API_AUTO_REFRESH_TOKENS", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def auto_save_tokens_enabled() -> bool:
    """Записывать Bearer-токены в airlines_api/.env.local (0 = только в памяти)."""
    return (env("API_SAVE_TOKENS", "0") or "0").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def tokens_configured() -> bool:
    return bool(env("API_AUTHORIZATION") and env("API_CUSTOMER_AUTHORIZATION"))


def refresh_credentials_configured() -> bool:
    return bool(
        env("API_CUSTOMER_PHONE")
        and env("API_CUSTOMER_PASSWORD")
        and env("API_IOS_PASSWORD")
    )


def _credentials_fingerprint() -> str:
    raw = "\n".join(
        (
            env("API_IOS_LOGIN", "ios") or "ios",
            env("API_IOS_PASSWORD"),
            env("API_CUSTOMER_PHONE"),
            env("API_CUSTOMER_PASSWORD"),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def credentials_changed() -> bool:
    """True, если логин/пароль не те, с которыми брали текущие токены."""
    if not refresh_credentials_configured():
        return False
    stored = env(_AUTH_FINGERPRINT_KEY)
    if not stored:
        return False
    return stored != _credentials_fingerprint()


def _client_login_headers() -> dict[str, str]:
    """POST /auth/login — корень цепочки: без cookie и без других токенов."""
    return {"content-type": "application/json"}


# Вход в личный кабинет приложение T5 делает не тем же клиентом, что поиск:
# по рабочим запросам видно `Dart/3.11 (dart:io)` — это Flutter-часть приложения.
# Мы слали туда iOS-строку, и получалось заметное несоответствие: логин с одного
# клиента, а поиск с другого. Версию можно переопределить, когда приложение
# обновится, — API_CUSTOMER_LOGIN_UA.
DEFAULT_CUSTOMER_LOGIN_UA = "Dart/3.11 (dart:io)"


def customer_login_user_agent() -> str:
    return env("API_CUSTOMER_LOGIN_UA", DEFAULT_CUSTOMER_LOGIN_UA) or DEFAULT_CUSTOMER_LOGIN_UA


def _customer_login_headers(authorization: str) -> dict[str, str]:
    """
    POST /customers/auth/login — нужен только живой client Bearer.

    Набор заголовков повторяет рабочий запрос приложения, включая
    accept-language: лишние отличия от настоящего клиента ни к чему.
    """
    return {
        "content-type": "application/json",
        "user-agent": customer_login_user_agent(),
        "accept-language": env("API_LANGUAGE", "ru") or "ru",
        "authorization": _bearer(authorization),
    }


def auth_token_type(body: dict[str, Any] | str) -> str | None:
    """Какой токен отверг сервер: client | customer | None (тело 401 не парсим иначе)."""
    if not isinstance(body, dict):
        return None
    ttype = str(body.get("token_type") or "").strip().lower()
    return ttype if ttype in ("client", "customer") else None


def _auth_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    rotate_user_agent: bool = True,
) -> tuple[int, dict[str, Any] | str]:
    """
    Запрос авторизации. Сетевой сбой → (0, текст), а не исключение.

    Ноль в статусе для вызывающего кода означает «ответа нет»: он не 200, не 401,
    поэтому ни успехом, ни поводом для re-login не считается — цикл просто уйдёт
    в следующий слот. Раньше `urlopen` бросал наружу и ронял монитор целиком.

    idempotent=True здесь верно и для POST /auth/login: повторный логин просто
    выдаёт ещё один токен, ничего не создаёт и денег не стоит.

    rotate_user_agent=False нужен входу в личный кабинет: там свой клиент,
    и общая ротация iOS-строки затёрла бы его.
    """
    try:
        response = api_call(
            method,
            path,
            headers=headers,
            body=body,
            idempotent=True,
            rotate_user_agent=rotate_user_agent,
        )
    except RequestFailed as exc:
        return 0, f"сеть: {exc}"
    return response.status, response.body  # type: ignore[return-value]


def _login_error(status: int, body: Any, login: str, password: str) -> str:
    """
    Понятное объяснение отказа на auth/login.

    Сервер отвечает на неверный пароль пустым телом (`[]`), поэтому голое
    «HTTP 401: []» не говорит человеку ничего. А причина почти всегда одна
    и та же — пароль учётки приложения введён с ошибкой.
    """
    if status != 401:
        return f"auth/login → HTTP {status}: {body}"

    hints = [
        f"auth/login → HTTP 401: сервер отверг пароль учётки приложения "
        f"(логин {login!r}, пароль {len(password)} симв.)",
        "Это НЕ пароль вашего личного кабинета — у приложения своя учётка.",
    ]

    # Самая частая ошибка: значение скопировали из экспорта Insomnia или JSON,
    # где обратный слеш экранирован, и в пароль уехали оба символа.
    if "\\\\" in password:
        hints.append(
            "В пароле есть двойной обратный слеш. Если вы копировали его из "
            "файла коллекции, там он экранирован — в настоящем пароле слеш один."
        )
    if password != password.strip():
        hints.append("По краям пароля есть пробелы — вероятно, лишние.")

    return " ".join(hints)


def _token_from_body(body: dict[str, Any] | str) -> str:
    if not isinstance(body, dict):
        raise RuntimeError(f"login: не JSON-объект: {body!r}")
    token = str(body.get("token") or "").strip()
    if not token:
        raise RuntimeError(f"login: нет token в ответе: {body!r}")
    return token


def _persist_env_values(path: Path, values: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in values.items():
        pattern = rf"^{re.escape(key)}=.*$"
        line = f"{key}={value}"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, line, text, count=1, flags=re.MULTILINE)
        else:
            text = text.rstrip() + f"\n{line}\n"
        os.environ[key] = value
    path.write_text(text, encoding="utf-8")


def _env_file_has_key(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    return bool(re.search(rf"^{re.escape(key)}=", path.read_text(encoding="utf-8"), flags=re.MULTILINE))


def _bearer(value: str) -> str:
    token = (value or "").strip()
    if not token.lower().startswith("bearer "):
        return f"Bearer {token}"
    return token


def _apply_tokens(
    *,
    app_token: str | None = None,
    customer_token: str | None = None,
) -> None:
    if app_token:
        os.environ["API_AUTHORIZATION"] = _bearer(app_token)
    if customer_token:
        os.environ["API_CUSTOMER_AUTHORIZATION"] = _bearer(customer_token)


def _persist_auth_fingerprint(*, env_path: Path | None) -> None:
    if not refresh_credentials_configured():
        return
    path = env_path or api_secrets_env_path()
    values = {_AUTH_FINGERPRINT_KEY: _credentials_fingerprint()}
    # Если в .env.local уже лежат Bearer — перезаписать, иначе следующий старт
    # подхватит токены старого аккаунта при API_SAVE_TOKENS=0.
    app_auth = env("API_AUTHORIZATION")
    customer_auth = env("API_CUSTOMER_AUTHORIZATION")
    if app_auth and _env_file_has_key(path, "API_AUTHORIZATION"):
        values["API_AUTHORIZATION"] = _bearer(app_auth)
    if customer_auth and _env_file_has_key(path, "API_CUSTOMER_AUTHORIZATION"):
        values["API_CUSTOMER_AUTHORIZATION"] = _bearer(customer_auth)
    _persist_env_values(path, values)


def _store_tokens(
    *,
    env_path: Path | None,
    app_token: str | None = None,
    customer_token: str | None = None,
    verbose: bool = True,
) -> None:
    _apply_tokens(app_token=app_token, customer_token=customer_token)
    if not auto_save_tokens_enabled():
        if verbose:
            print("[auth] токены в памяти (API_SAVE_TOKENS=0)")
        return
    path = env_path or api_secrets_env_path()
    values: dict[str, str] = {}
    if app_token:
        values["API_AUTHORIZATION"] = _bearer(app_token)
    if customer_token:
        values["API_CUSTOMER_AUTHORIZATION"] = _bearer(customer_token)
    _persist_env_values(path, values)
    if verbose:
        print(f"[auth] токены сохранены → {path.name}")


def refresh_app_token(
    *,
    env_path: Path | None = None,
    verbose: bool = True,
) -> str:
    """Только POST /auth/login — обновить authorization (client)."""
    if not env("API_IOS_PASSWORD"):
        raise AuthFatalError("client token expired and API_IOS_PASSWORD not set")

    ios_login = env("API_IOS_LOGIN", "ios") or "ios"
    password = env("API_IOS_PASSWORD")
    status, body = _auth_request(
        "POST",
        "auth/login",
        headers=_client_login_headers(),
        body={"login": ios_login, "password": password},
    )
    if status != 200:
        raise AuthFatalError(_login_error(status, body, ios_login, password))

    app_token = _token_from_body(body)
    if verbose:
        print("[auth] client token refreshed via app credentials")
    _store_tokens(env_path=env_path, app_token=app_token, verbose=verbose)
    return app_token


def refresh_customer_token(
    *,
    env_path: Path | None = None,
    verbose: bool = True,
) -> str:
    """Только POST /customers/auth/login — обновить x-customer-authorization."""
    if not env("API_CUSTOMER_PHONE") or not env("API_CUSTOMER_PASSWORD"):
        raise AuthFatalError(
            "customer token expired and API_CUSTOMER_PHONE/API_CUSTOMER_PASSWORD not set"
        )

    app_auth = env("API_AUTHORIZATION")
    if not app_auth:
        raise AuthFatalError("Нет API_AUTHORIZATION — сначала client login")

    status, body = _auth_request(
        "POST",
        "customers/auth/login",
        headers=_customer_login_headers(app_auth),
        body={
            "phone": env("API_CUSTOMER_PHONE"),
            "password": env("API_CUSTOMER_PASSWORD"),
        },
        # Свой user-agent: приложение входит в кабинет Dart-клиентом,
        # и общая ротация iOS-строки затёрла бы его.
        rotate_user_agent=False,
    )
    if status != 200:
        raise AuthFatalError(f"customers/auth/login → HTTP {status}: {body}")

    customer_token = _token_from_body(body)
    if verbose:
        print("[auth] customer token refreshed via phone+password")
    _store_tokens(env_path=env_path, customer_token=customer_token, verbose=verbose)
    return customer_token


def _rebuild_headers(headers: dict[str, str]) -> None:
    headers.clear()
    headers.update(build_api_headers())


def ensure_live_auth(
    headers: dict[str, str],
    *,
    env_path: Path | None = None,
    verbose: bool = True,
) -> None:
    """GET /customers/info — ping. Тело 200 не читаем.

    401/403: из JSON берём только token_type (client|customer) и обновляем
    именно этот Bearer. До 3 раундов (умер client, за ним customer).
    """
    if not auto_refresh_tokens_enabled():
        _rebuild_headers(headers)
        return

    for _ in range(3):
        _rebuild_headers(headers)
        status, body = _auth_request("GET", "customers/info", headers=headers)
        if verbose:
            print(f"[auth] customers/info → HTTP {status}")
        if status == 200:
            return
        if status == 0:
            # Сеть не ответила. Это не «токен протух» — перелогиниваться незачем,
            # и уж точно нельзя ронять монитор: просто пропускаем этот слот.
            raise AuthTransientError(f"customers/info: {body}")
        if status == 429 or status >= 500:
            raise AuthTransientError(f"customers/info http {status}")
        ttype = auth_token_type(body)
        if verbose:
            print(f"[auth] {ttype or '?'} token rejected — attempting re-login")
        if ttype == "client":
            refresh_app_token(env_path=env_path, verbose=verbose)
        elif ttype == "customer":
            refresh_customer_token(env_path=env_path, verbose=verbose)
        else:
            raise AuthFatalError(f"customers/info HTTP {status} (no token_type): {body}")
    raise AuthFatalError("auth refresh did not converge")


def refresh_api_tokens(
    *,
    env_path: Path | None = None,
    verbose: bool = True,
) -> tuple[str, str]:
    """POST auth/login → POST customers/auth/login (когда токенов ещё нет)."""
    if not refresh_credentials_configured():
        raise RuntimeError(
            "Для авто-логина нужны API_CUSTOMER_PHONE, API_CUSTOMER_PASSWORD, API_IOS_PASSWORD"
        )

    app_token = refresh_app_token(env_path=env_path, verbose=verbose)
    customer_token = refresh_customer_token(env_path=env_path, verbose=verbose)
    _persist_auth_fingerprint(env_path=env_path)
    return app_token, customer_token


def ensure_api_session(
    *,
    env_path: Path | None = None,
    verbose: bool = True,
) -> bool:
    """Старт: login если нет Bearer или сменились логин/пароль."""
    if tokens_configured() and not credentials_changed():
        if verbose:
            print("[auth] токены из env — живые/мёртвые решит GET customers/info")
        if refresh_credentials_configured() and not env(_AUTH_FINGERPRINT_KEY):
            _persist_auth_fingerprint(env_path=env_path)
        return False
    if not auto_refresh_tokens_enabled() or not refresh_credentials_configured():
        return False
    if tokens_configured() and credentials_changed() and verbose:
        print("[auth] логин/пароль изменились — обновляю токены")
    refresh_api_tokens(env_path=env_path, verbose=verbose)
    return True


def ensure_api_tokens(
    *,
    env_path: Path | None = None,
    verbose: bool = True,
) -> bool:
    """Полный re-login (оба токена), если включён авто-логин и заданы credentials."""
    if not auto_refresh_tokens_enabled() or not refresh_credentials_configured():
        return False
    refresh_api_tokens(env_path=env_path, verbose=verbose)
    return True
