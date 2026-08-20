"""HTTP-клиент с постоянным соединением, раздельными таймаутами и ретраями.

Зачем: `urllib.request.urlopen` открывает новый TCP+TLS на каждый запрос.
В гонке за билетом это 2-3 лишних RTT на каждый шаг цепочки
(поиск → детали рейса → бронь), то есть сотни миллисекунд впустую.

Что даёт этот модуль:
  * одно живое соединение между запросами (keep-alive реально работает);
  * переиспользование TLS-сессии при переподключении (resumption);
  * раздельные connect / read таймауты вместо одного `timeout=60`;
  * ретраи с экспоненциальным backoff и уважением `Retry-After`;
  * ЧЁТКОЕ различие «запрос не ушёл» (безопасно повторить) и
    «ответ не получен» (сервер МОГ выполнить операцию) — см. `ResultUnknown`.

Только stdlib, никаких новых зависимостей.
"""

from __future__ import annotations

import gzip
import http.client
import json
import random
import re
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

__all__ = [
    "HttpClient",
    "Response",
    "RequestFailed",
    "ResultUnknown",
    "redact",
]

# --------------------------------------------------------------------------- #
# Ошибки
# --------------------------------------------------------------------------- #


class RequestFailed(RuntimeError):
    """Запрос не выполнен, но и НЕ был доставлен серверу — повторить безопасно."""

    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class ResultUnknown(RequestFailed):
    """
    Запрос ушёл на сервер, но ответ не получен (таймаут чтения, обрыв, 5xx).

    Для GET это безобидно. Для POST /bookings это значит, что бронь МОГЛА быть
    создана — повторять запрос нельзя, нужно либо проверить состояние на сервере,
    либо остановиться и разбираться руками.
    """


# --------------------------------------------------------------------------- #
# Ответ
# --------------------------------------------------------------------------- #

_SECRET_KEYS = re.compile(
    r'("(?:[a-z_]*(?:token|password|authorization|secret|otp|refresh)[a-z_]*)"\s*:\s*")([^"]{4,})(")',
    re.IGNORECASE,
)


def redact(text: str, *, keep: int = 4) -> str:
    """Вырезает значения токенов/паролей из тела ответа перед логом или письмом."""

    def _mask(match: re.Match[str]) -> str:
        value = match.group(2)
        tail = value[-keep:] if len(value) > keep else ""
        return f"{match.group(1)}***{tail}{match.group(3)}"

    return _SECRET_KEYS.sub(_mask, text or "")


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    attempts: int = 1

    @property
    def json(self) -> Any | None:
        """Распарсенный JSON или None, если тело не JSON."""
        try:
            return json.loads(self.text)
        except (json.JSONDecodeError, ValueError):
            return None

    @property
    def body(self) -> dict[str, Any] | list[Any] | str:
        """Совместимость со старым `api_request`: JSON, иначе сырой текст."""
        parsed = self.json
        return self.text if parsed is None else parsed

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def retry_after(self) -> float | None:
        return _parse_retry_after(self.headers.get("retry-after"))

    def safe_text(self, limit: int = 600) -> str:
        """Тело для лога/письма: без секретов и без простыни на 200 КБ."""
        text = redact(self.text)
        if len(text) > limit:
            return f"{text[:limit]}… (+{len(text) - limit} симв.)"
        return text


def _parse_retry_after(raw: str | None) -> float | None:
    if not raw:
        return None
    value = raw.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:  # HTTP-date форма
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        if target is None:
            return None
        import datetime as _dt

        now = _dt.datetime.now(tz=target.tzinfo)
        return max(0.0, (target - now).total_seconds())
    except Exception:  # noqa: BLE001 — формат Retry-After не критичен
        return None


# --------------------------------------------------------------------------- #
# Соединение с переиспользованием TLS-сессии
# --------------------------------------------------------------------------- #


class _SessionCache:
    """Хранит ssl.SSLSession между переподключениями — экономит RTT на handshake."""

    def __init__(self) -> None:
        self._session: ssl.SSLSession | None = None

    def get(self) -> ssl.SSLSession | None:
        return self._session

    def set(self, session: ssl.SSLSession | None) -> None:
        if session is not None:
            self._session = session


class _ResumingHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection с раздельными connect/read таймаутами и TLS resumption."""

    def __init__(
        self,
        host: str,
        port: int | None,
        *,
        context: ssl.SSLContext,
        session_cache: _SessionCache,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=connect_timeout, context=context)
        self._session_cache = session_cache
        self._connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def connect(self) -> None:  # noqa: D102 — переопределение stdlib
        sock = socket.create_connection(
            (self.host, self.port), timeout=self._connect_timeout
        )
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            sock,
            server_hostname=server_hostname,
            session=self._session_cache.get(),
        )
        self._session_cache.set(getattr(self.sock, "session", None))
        self.sock.settimeout(self.read_timeout)


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpClient:
    """
    Постоянное соединение к одному хосту.

    Экземпляр НЕ потокобезопасен для параллельных запросов — он сериализует их
    внутренним локом. Для параллельного опроса заводи по клиенту на поток.

    Пример:
        client = HttpClient("https://api.railway.gov.tm", headers_factory=build_headers)
        client.warmup()
        resp = client.request("GET", "/v2/trips", query={"source": 17})
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers_factory: Callable[[], Mapping[str, str]] | None = None,
        connect_timeout: float = 2.5,
        read_timeout: float = 6.0,
        write_timeout: float = 12.0,
        max_retries: int = 2,
        backoff_base: float = 0.4,
        backoff_cap: float = 8.0,
        name: str = "http",
        debug: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parts = urlsplit(self.base_url)
        if parts.scheme != "https":
            raise ValueError(f"{name}: поддерживается только https, получено {base_url!r}")
        self._host = parts.hostname or ""
        self._port = parts.port or 443
        self._path_prefix = parts.path.rstrip("/")

        self._headers_factory = headers_factory or (lambda: {})
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.name = name
        self.debug = debug

        self._context = ssl.create_default_context()
        self._session_cache = _SessionCache()
        self._conn: _ResumingHTTPSConnection | None = None
        self._lock = threading.Lock()

    # -- соединение --------------------------------------------------------- #

    def _new_conn(self, read_timeout: float) -> _ResumingHTTPSConnection:
        return _ResumingHTTPSConnection(
            self._host,
            self._port,
            context=self._context,
            session_cache=self._session_cache,
            connect_timeout=self.connect_timeout,
            read_timeout=read_timeout,
        )

    def _ensure_conn(self, read_timeout: float) -> _ResumingHTTPSConnection:
        if self._conn is None:
            self._conn = self._new_conn(read_timeout)
        else:
            self._conn.read_timeout = read_timeout
            if self._conn.sock is not None:
                self._conn.sock.settimeout(read_timeout)
        return self._conn

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — соединение и так мертво
                pass

    def warmup(self) -> bool:
        """
        Установить TCP+TLS заранее, до наступления слота.

        Возвращает True, если соединение поднято. Ошибку не бросает: прогрев —
        оптимизация, а не обязательный шаг.
        """
        with self._lock:
            try:
                conn = self._ensure_conn(self.read_timeout)
                if conn.sock is None:
                    started = time.monotonic()
                    conn.connect()
                    if self.debug:
                        elapsed = (time.monotonic() - started) * 1000
                        print(f"[{self.name}] warmup: соединение за {elapsed:.0f} мс")
                return True
            except OSError as exc:
                if self.debug:
                    print(f"[{self.name}] warmup не удался: {exc}")
                self._drop_conn()
                return False

    def close(self) -> None:
        with self._lock:
            self._drop_conn()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- запрос ------------------------------------------------------------- #

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int] | None = None,
        body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
        read_timeout: float | None = None,
        retries: int | None = None,
    ) -> Response:
        """
        Выполнить запрос.

        idempotent=None → GET/HEAD/OPTIONS считаются идемпотентными, остальное нет.
        Неидемпотентный запрос НЕ повторяется после отправки байтов: вместо этого
        бросается `ResultUnknown`, чтобы вызывающий код решил сам.
        """
        method = method.upper()
        if idempotent is None:
            idempotent = method in _IDEMPOTENT_METHODS
        if retries is None:
            retries = self.max_retries if idempotent else 0
        if read_timeout is None:
            read_timeout = self.read_timeout if idempotent else self.write_timeout

        url_path = self._build_path(path, query)
        payload, req_headers = self._build_request(method, body, headers)

        last_error: Exception | None = None
        with self._lock:
            for attempt in range(1, retries + 2):
                try:
                    response = self._attempt(
                        method, url_path, payload, req_headers, read_timeout, attempt
                    )
                except ResultUnknown as exc:
                    if not idempotent:
                        raise
                    last_error = exc
                except RequestFailed as exc:
                    last_error = exc
                else:
                    if response.status in _RETRYABLE_STATUS and attempt <= retries:
                        self._sleep_backoff(attempt, response.retry_after)
                        continue
                    return response

                if attempt > retries:
                    break
                self._sleep_backoff(attempt, None)

        assert last_error is not None
        raise last_error

    def _attempt(
        self,
        method: str,
        url_path: str,
        payload: bytes | None,
        headers: dict[str, str],
        read_timeout: float,
        attempt: int,
    ) -> Response:
        started = time.monotonic()
        sent = False
        try:
            conn = self._ensure_conn(read_timeout)
            conn.request(method, url_path, body=payload, headers=headers)
            sent = True
            raw_response = conn.getresponse()
            raw = raw_response.read()
        except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
            self._drop_conn()
            elapsed = (time.monotonic() - started) * 1000
            detail = f"{method} {url_path} ({elapsed:.0f} мс, попытка {attempt}): {exc}"
            if sent:
                # Байты ушли, ответ не получен — сервер мог выполнить операцию.
                raise ResultUnknown(detail, attempts=attempt) from exc
            raise RequestFailed(detail, attempts=attempt) from exc

        resp_headers = {k.lower(): v for k, v in raw_response.getheaders()}
        text = _decode_body(raw, resp_headers.get("content-encoding"))
        elapsed = (time.monotonic() - started) * 1000

        if resp_headers.get("connection", "").lower() == "close":
            self._drop_conn()

        if self.debug:
            print(
                f"[{self.name}] {method} {url_path} → {raw_response.status} "
                f"за {elapsed:.0f} мс (попытка {attempt})"
            )

        return Response(
            status=raw_response.status,
            text=text,
            headers=resp_headers,
            elapsed_ms=elapsed,
            attempts=attempt,
        )

    # -- вспомогательное ---------------------------------------------------- #

    def _build_path(self, path: str, query: Mapping[str, str | int] | None) -> str:
        url_path = f"{self._path_prefix}/{path.lstrip('/')}"
        if query:
            url_path = f"{url_path}?{urlencode(query)}"
        return url_path

    def _build_request(
        self,
        method: str,
        body: Any | None,
        extra_headers: Mapping[str, str] | None,
    ) -> tuple[bytes | None, dict[str, str]]:
        # Заголовки собираем заново на каждый запрос — общий mutable dict
        # между вызовами приводил к тому, что нельзя понять, какой токен ушёл.
        headers: dict[str, str] = {k.lower(): v for k, v in self._headers_factory().items()}
        headers.setdefault("host", self._host)
        headers["connection"] = "keep-alive"
        headers["accept-encoding"] = "gzip, deflate"

        payload: bytes | None = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                payload = bytes(body)
            else:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["content-type"] = "application/json"
            headers["content-length"] = str(len(payload))
        elif method in ("POST", "PUT", "PATCH"):
            headers["content-length"] = "0"

        for key, value in (extra_headers or {}).items():
            headers[key.lower()] = value
        return payload, headers

    def _sleep_backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            delay = min(retry_after, self.backoff_cap)
        else:
            delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_cap)
        delay += random.uniform(0, self.backoff_base)
        if self.debug:
            print(f"[{self.name}] пауза {delay:.2f}с перед повтором #{attempt + 1}")
        time.sleep(delay)


def _decode_body(raw: bytes, encoding: str | None) -> str:
    enc = (encoding or "").lower()
    data = raw
    try:
        if "gzip" in enc:
            data = gzip.decompress(raw)
        elif "deflate" in enc:
            # Сервер может отдать как zlib-обёртку, так и голый deflate.
            try:
                data = zlib.decompress(raw)
            except zlib.error:
                data = zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        data = raw
    return data.decode("utf-8", errors="replace")
