"""HTTP-транспорт для API Turkmenistan Airlines (то же, что использует iOS-приложение).

Раньше каждый из трёх модулей (`api_check`, `api_auth`, а также `api_booking`
через переданный колбэк) открывал соединение сам через `urllib.request.urlopen`
с таймаутом 100 секунд. Отсюда три проблемы:

  * новый TCP+TLS на КАЖДЫЙ шаг цепочки (customers/info → captcha/finding →
    GetAirAvailability) — три handshake подряд в момент, когда счёт идёт на
    десятки миллисекунд;
  * один общий таймаут: подвисший коннект и медленный ответ неразличимы,
    и оба съедали до 100 секунд, то есть полтора слота проверки;
  * `urlopen` не различает «запрос не ушёл» и «ответ потерян». Для поиска это
    неважно, для `CreateBooking` — принципиально: повтор создаёт вторую бронь.

Здесь всё это решает общий `core.http.HttpClient`. Публичная функция
`api_request` намеренно сохраняет старую сигнатуру и старый кортеж
`(status, body, retry_after)`, чтобы вызывающий код не переписывать.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import http_journal  # noqa: E402
from core.http import HttpClient, RequestFailed, Response, ResultUnknown  # noqa: E402
from api_env import current_ios_user_agent, env  # noqa: E402
from api_rate_limit import is_rate_limited_response, on_429, wait_if_paused  # noqa: E402

__all__ = [
    "ACCOUNT_BLOCKED_EXIT_CODE",
    "AccountBlocked",
    "is_account_blocked_response",
    "api_request",
    "api_call",
    "base_url",
    "get_client",
    "reset_client",
    "warmup_connection",
    "RequestFailed",
    "ResultUnknown",
    "Response",
]

DEFAULT_BASE_URL = "https://turkmenistanairlines.tm/api/v1"

# Код выхода «аккаунт заблокирован перевозчиком». Отдельный, как у 429:
# панель по нему запрещает запуск с этого аккаунта, пока человек не разрешит.
ACCOUNT_BLOCKED_EXIT_CODE = 4


class AccountBlocked(BaseException):
    """
    Сервер ответил «Customer Block» (HTTP 450): аккаунт заблокирован.

    Раньше бот принимал это за обычную неудачу цикла и трое суток раз
    в минуту стучался заблокированным аккаунтом — 4000 отказов подряд.
    Каждый такой запрос — повод продлить блок, поэтому дальше нельзя ни
    одного запроса. BaseException, как и RateLimitStop: многочисленные
    `except Exception` в боте превратили бы стоп в «ошибку цикла, идём дальше».
    """

    def __init__(self, reason: str, *, status: int, endpoint: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.endpoint = endpoint


def is_account_blocked_response(status: int, text: str) -> bool:
    """HTTP 450 или «Customer Block» в теле — с любым статусом."""
    if status == 450:
        return True
    low = (text or "").lower()
    return "customer block" in low or "аккаунт временно заблокирован" in low


_TAG = re.compile(r"<[^>]+>")
# Приветствие с именем владельца аккаунта: в журнале оно ни к чему.
_GREETING = re.compile(r"<h\d[^>]*>.*?</h\d>", re.IGNORECASE | re.DOTALL)


def block_reason(text: str) -> str:
    """Коротко, что сказал сервер: «Customer Block: Ваш аккаунт временно заблокирован…»."""
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        body = None
    if isinstance(body, dict):
        message = str(body.get("message") or "").strip()
        data = body.get("data")
        data = data if isinstance(data, str) else ""
    else:
        message, data = "", str(text or "")
    words = " ".join(html.unescape(_TAG.sub(" ", _GREETING.sub(" ", data))).split())
    # Первые два предложения: третье — телефон и почта поддержки.
    sentences = re.split(r"(?<=[.!?])\s+", words)
    detail = " ".join(sentences[:2])[:200]
    reason = ": ".join(part for part in (message or "Customer Block", detail) if part)
    return reason


def base_url() -> str:
    """Боевой хост по умолчанию; API_BASE_URL позволяет подменить на стенд."""
    return (env("API_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")


def _float_env(name: str, default: float) -> float:
    raw = env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"[warn] {name}={raw!r} — не число, беру {default}")
        return default
    return value if value > 0 else default


_client: HttpClient | None = None
_client_base: str = ""
# Блок, пойманный в этом процессе. После него — ни одного запроса.
_blocked: AccountBlocked | None = None


def source_ip() -> str:
    """Исходящий адрес аккаунта (API_SOURCE_IP). Пусто — какой выберет система."""
    return (env("API_SOURCE_IP") or "").strip()


def get_client() -> HttpClient:
    """Один клиент на процесс. Пересоздаётся, если сменился API_BASE_URL."""
    global _client, _client_base

    current = base_url()
    if _client is not None and _client_base == current:
        return _client

    if _client is not None:
        _client.close()

    _client = HttpClient(
        current,
        # Заголовки приходят готовыми в каждый вызов — общей фабрики тут нет,
        # потому что Bearer-токены живут в вызывающем коде и меняются на лету.
        headers_factory=dict,
        connect_timeout=_float_env("API_CONNECT_TIMEOUT", 3.0),
        # Поиск отвечает за 1-3 с; 8 с уже означает, что слот потерян и ждать
        # дальше бессмысленно — лучше зайти в следующий слот со свежей капчей.
        read_timeout=_float_env("API_READ_TIMEOUT", 8.0),
        # CreateBooking тяжелее поиска, ему даём отдельный, больший бюджет.
        write_timeout=_float_env("API_WRITE_TIMEOUT", 20.0),
        max_retries=int(_float_env("API_HTTP_RETRIES", 2)),
        name="t5-http",
        debug=(env("API_HTTP_DEBUG") or "0") not in ("0", "false", "no"),
        source_ip=source_ip(),
    )
    _client_base = current
    return _client


def reset_client() -> None:
    """Закрыть соединение (смена окружения, конец теста)."""
    global _client, _client_base, _blocked
    if _client is not None:
        _client.close()
    _client = None
    _client_base = ""
    _blocked = None


def warmup_connection() -> bool:
    """Поднять TCP+TLS заранее, чтобы в момент слота не платить handshake."""
    return get_client().warmup()


def api_host() -> str:
    return urlsplit(base_url()).hostname or "turkmenistanairlines.tm"


def api_call(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    idempotent: bool | None = None,
    rotate_user_agent: bool = True,
) -> Response:
    """
    Полный ответ (статус, заголовки, тело, латентность). Бросает RequestFailed.

    User-Agent по умолчанию подставляется здесь: он ротируется по времени,
    и правильнее брать актуальное значение в момент отправки, чем полагаться
    на то, что вызывающий не забыл обновить свой словарь заголовков.

    `rotate_user_agent=False` оставляет заголовок таким, каким его передали.
    Нужно там, где настоящее приложение шлёт не iOS-строку: вход в личный
    кабинет оно делает клиентом на Dart, и подмена там заметна.
    """
    # Неидемпотентный запрос — это CreateBooking. Перед ним пауза уже
    # выдержана в переборе рейсов, ДО записи попытки в журнал: ждать или
    # останавливаться между записью pending и самим POST нельзя.
    global _blocked
    if _blocked is not None:
        raise _blocked

    booking = idempotent is False
    if not booking:
        # Общая пауза после 429 — в том числе пойманного другим процессом.
        wait_if_paused(label=path)

    request_headers = dict(headers)
    if rotate_user_agent:
        request_headers["user-agent"] = current_ios_user_agent()
    else:
        request_headers.setdefault("user-agent", current_ios_user_agent())
    request_headers.setdefault("host", api_host())

    try:
        response = get_client().request(
            method,
            path,
            headers=request_headers,
            body=body,
            idempotent=idempotent,
        )
    except RequestFailed as exc:
        _journal(method, path, status=0, error=str(exc))
        raise
    # getattr: подставные ответы в тестах — не настоящий Response.
    _journal(method, path, status=response.status, ms=getattr(response, "elapsed_ms", 0.0),
             attempts=getattr(response, "attempts", 1), text=response.text)

    if is_account_blocked_response(response.status, response.text):
        _blocked = AccountBlocked(
            block_reason(response.text), status=response.status, endpoint=path
        )
        # Внутри брони не бросаем — по той же причине, что и стоп после 429:
        # попытка уже записана как pending. CreateBooking вернёт отказ сам,
        # а следующий же запрос процесса упрётся в блок.
        if not booking:
            raise _blocked

    if is_rate_limited_response(response.status, response.text):
        on_429(
            endpoint=path,
            status=response.status,
            retry_after=response.retry_after,
            source=env("API_PROVIDER_NAME") or _masked_phone(),
            wait=not booking,
        )
    return response


def _journal(
    method: str,
    path: str,
    *,
    status: int,
    ms: float = 0.0,
    attempts: int = 1,
    text: str = "",
    error: str = "",
) -> None:
    """Строка в файловый журнал запросов (core.http_journal)."""
    record: dict[str, Any] = {
        "svc": "airlines",
        "acct": env("API_PROVIDER_NAME") or _masked_phone(),
        # Адрес по умолчанию в API_SOURCE_IP пуст — панель передаёт и настоящий.
        "ip": source_ip() or env("API_EFFECTIVE_IP"),
        "m": method,
        "ep": path,
        "st": status,
        "ms": round(ms),
    }
    if attempts > 1:
        record["n"] = attempts
    if error:
        record["err"] = http_journal.mask(error)[:300]
    # Тело — только у отказов: у успешного поиска это сотни килобайт рейсов.
    http_journal.write(record, body=text if status and not 200 <= status < 300 else "")


def _masked_phone() -> str:
    """Чей аккаунт поймал 429 — без полного номера в общем файле."""
    phone = env("API_CUSTOMER_PHONE")
    return f"…{phone[-4:]}" if len(phone) >= 4 else ""


def api_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    idempotent: bool | None = None,
) -> tuple[int, dict[str, Any] | str, float | None]:
    """
    Совместимая обёртка: `(status, тело, Retry-After)` — как у старого api_request.

    Сетевые сбои превращаются в кортеж со статусом 0, чтобы вызывающий цикл
    обработал их как обычную неудачу цикла и ушёл в следующий слот. Исключение —
    `ResultUnknown` для неидемпотентных запросов: его пробрасываем, потому что
    «ответ потерян» на CreateBooking нельзя молча считать неудачей.
    """
    try:
        response = api_call(
            method, path, headers=headers, body=body, idempotent=idempotent
        )
    except ResultUnknown:
        raise
    except RequestFailed as exc:
        return 0, f"сеть: {exc}", None

    return response.status, response.body, response.retry_after  # type: ignore[return-value]
