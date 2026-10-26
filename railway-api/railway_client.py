"""HTTP-клиент Railway API (как в iOS-приложении).

Транспорт вынесен в `core.http`: одно живое соединение на весь процесс,
раздельные connect/read таймауты, ретраи для GET, честный `ResultUnknown`
для POST. Публичная функция `api_request` сохраняет прежнюю сигнатуру
`(status, parsed)`, поэтому вызывающий код менять не пришлось.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import http_journal  # noqa: E402
from core.http import HttpClient, RequestFailed, Response, ResultUnknown  # noqa: E402
from railway_env import base_url, env  # noqa: E402

__all__ = [
    "api_request",
    "api_call",
    "build_headers",
    "get_client",
    "reset_client",
    "warmup_connection",
    "RequestFailed",
    "ResultUnknown",
    "Response",
]

DEFAULT_USER_AGENT = "Railway/1 CFNetwork/3860.600.12 Darwin/25.5.0"


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


def build_headers() -> dict[str, str]:
    # host больше не захардкожен: RAILWAY_BASE_URL может указывать на стенд/прокси,
    # и раньше туда уходил заголовок от боевого хоста.
    host = urlsplit(base_url()).hostname or "api.railway.gov.tm"
    return {
        "host": host,
        "accept": "*/*",
        "accept-language": env("RAILWAY_LANGUAGE", "ru") or "ru",
        "user-agent": env("RAILWAY_USER_AGENT", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
    }


_client: HttpClient | None = None
_client_base: str = ""


def get_client() -> HttpClient:
    """Общий клиент процесса. Пересоздаётся, если сменился RAILWAY_BASE_URL."""
    global _client, _client_base

    current_base = base_url()
    if _client is not None and _client_base == current_base:
        return _client

    if _client is not None:
        _client.close()

    _client = HttpClient(
        current_base,
        headers_factory=build_headers,
        connect_timeout=_float_env("RAILWAY_CONNECT_TIMEOUT", 2.5),
        read_timeout=_float_env("RAILWAY_READ_TIMEOUT", 6.0),
        write_timeout=_float_env("RAILWAY_WRITE_TIMEOUT", 12.0),
        max_retries=int(_float_env("RAILWAY_HTTP_RETRIES", 2)),
        name="railway-http",
        debug=(env("RAILWAY_HTTP_DEBUG") or "0") not in ("0", "false", "no"),
    )
    _client_base = current_base
    return _client


def reset_client() -> None:
    global _client, _client_base
    if _client is not None:
        _client.close()
    _client = None
    _client_base = ""


def warmup_connection() -> bool:
    """Поднять TCP+TLS заранее, до наступления слота."""
    return get_client().warmup()


def api_call(
    method: str,
    path: str,
    *,
    query: dict[str, str | int] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    idempotent: bool | None = None,
) -> Response:
    """Полный ответ (статус, заголовки, тело, латентность). Бросает RequestFailed."""
    record = {"svc": "railway", "m": method, "ep": path}
    try:
        response = get_client().request(
            method,
            path,
            query=query,
            body=body,
            headers=headers,
            idempotent=idempotent,
        )
    except RequestFailed as exc:
        http_journal.write({**record, "st": 0, "err": http_journal.mask(str(exc))[:300]})
        raise
    # Тело — только у отказов: список поездов и мест на успехе слишком велик.
    http_journal.write(
        {**record, "st": response.status, "ms": round(response.elapsed_ms)},
        body="" if response.ok else response.text,
    )
    return response


def api_request(
    method: str,
    path: str,
    *,
    query: dict[str, str | int] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    """Совместимая обёртка: (status, распарсенный JSON или сырой текст)."""
    response = api_call(method, path, query=query, body=body)
    return response.status, response.body  # type: ignore[return-value]
