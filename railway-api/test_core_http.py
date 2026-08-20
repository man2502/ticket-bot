"""Тесты транспорта: ретраи, семантика «ответ потерян», распаковка, редакция.

Самое важное здесь — `test_post_never_retried_after_send`: повтор POST /bookings
после отправки байтов означает вторую настоящую бронь.

Запуск (из корня репозитория):
    .venv/bin/python -m pytest railway/test_core_http.py -q
"""

from __future__ import annotations

import gzip
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.http import (
    HttpClient,
    RequestFailed,
    Response,
    ResultUnknown,
    _decode_body,
    _parse_retry_after,
    redact,
)


class _FakeRaw:
    def __init__(self, status: int, payload: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self._payload = payload
        self._headers = headers

    def read(self) -> bytes:
        return self._payload

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())


class _FakeConn:
    """Соединение-заглушка: сценарий задаётся списком шагов."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.sock = object()
        self.read_timeout = 1.0
        self.sent: list[tuple[str, str]] = []
        self.closed = 0

    def request(self, method: str, path: str, body=None, headers=None) -> None:
        self.sent.append((method, path))
        step = self.script[len(self.sent) - 1]
        if isinstance(step, tuple) and step[0] == "fail_before_send":
            # Ошибка ДО отправки байтов.
            self.sent.pop()
            raise OSError(step[1])

    def getresponse(self) -> _FakeRaw:
        step = self.script[len(self.sent) - 1]
        if isinstance(step, tuple) and step[0] == "fail_after_send":
            raise TimeoutError(step[1])
        status, payload, headers = step  # type: ignore[misc]
        return _FakeRaw(status, payload, headers)

    def close(self) -> None:
        self.closed += 1


def _client(script: list[object], **kwargs) -> tuple[HttpClient, _FakeConn]:
    client = HttpClient("https://example.test", backoff_base=0.0, **kwargs)
    conn = _FakeConn(script)

    def _ensure(_timeout: float) -> _FakeConn:
        client._conn = conn  # type: ignore[assignment]
        return conn

    client._ensure_conn = _ensure  # type: ignore[method-assign]
    return client, conn


OK = (200, b'{"success": true}', {})


# --------------------------------------------------------------------------- #
# Семантика неидемпотентных запросов
# --------------------------------------------------------------------------- #


def test_post_never_retried_after_send() -> None:
    """Байты ушли, ответа нет → ResultUnknown и РОВНО одна отправка."""
    client, conn = _client([("fail_after_send", "read timeout"), OK], max_retries=3)
    with pytest.raises(ResultUnknown):
        client.request("POST", "/v2/bookings", body={"a": 1})
    assert len(conn.sent) == 1, "повтор POST = вторая бронь"


def test_post_failing_before_send_is_safe_failure() -> None:
    """Соединение не встало — сервер запроса не видел, это не 'unknown'."""
    client, _conn = _client([("fail_before_send", "connect refused")], max_retries=0)
    with pytest.raises(RequestFailed) as exc:
        client.request("POST", "/v2/bookings", body={"a": 1})
    assert not isinstance(exc.value, ResultUnknown)


def test_get_retries_after_lost_response() -> None:
    client, conn = _client([("fail_after_send", "read timeout"), OK], max_retries=2)
    response = client.request("GET", "/v2/trips")
    assert response.status == 200
    assert len(conn.sent) == 2


def test_get_gives_up_after_max_retries() -> None:
    script = [("fail_after_send", "timeout")] * 5
    client, conn = _client(script, max_retries=2)
    with pytest.raises(RequestFailed):
        client.request("GET", "/v2/trips")
    assert len(conn.sent) == 3, "1 попытка + 2 ретрая"


def test_retryable_status_is_retried_then_returned() -> None:
    client, conn = _client([(503, b"", {}), (503, b"", {}), OK], max_retries=2)
    assert client.request("GET", "/v2/trips").status == 200
    assert len(conn.sent) == 3


def test_4xx_is_not_retried() -> None:
    client, conn = _client([(422, b'{"success": false}', {}), OK], max_retries=2)
    assert client.request("GET", "/v2/trips").status == 422
    assert len(conn.sent) == 1


def test_dead_connection_is_dropped() -> None:
    client, conn = _client([("fail_after_send", "reset"), OK], max_retries=1)
    client.request("GET", "/v2/trips")
    assert conn.closed >= 1, "мёртвое соединение нельзя переиспользовать"


# --------------------------------------------------------------------------- #
# Заголовки
# --------------------------------------------------------------------------- #


def test_headers_are_rebuilt_per_request() -> None:
    """Общий mutable dict между вызовами не давал понять, что реально ушло."""
    calls = {"n": 0}

    def factory() -> dict[str, str]:
        calls["n"] += 1
        return {"authorization": f"Bearer t{calls['n']}"}

    client = HttpClient("https://example.test", headers_factory=factory)
    _payload, first = client._build_request("GET", None, None)
    _payload, second = client._build_request("GET", None, None)
    assert first["authorization"] == "Bearer t1"
    assert second["authorization"] == "Bearer t2"


def test_idempotency_key_reaches_headers() -> None:
    client = HttpClient("https://example.test")
    _payload, headers = client._build_request("POST", {"a": 1}, {"Idempotency-Key": "abc"})
    assert headers["idempotency-key"] == "abc"
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == str(len(_payload or b""))


def test_only_https_allowed() -> None:
    with pytest.raises(ValueError):
        HttpClient("http://example.test")


# --------------------------------------------------------------------------- #
# Тело ответа
# --------------------------------------------------------------------------- #


def test_gzip_decoded() -> None:
    assert _decode_body(gzip.compress(b'{"a":1}'), "gzip") == '{"a":1}'


def test_deflate_zlib_wrapped_decoded() -> None:
    assert _decode_body(zlib.compress(b'{"a":1}'), "deflate") == '{"a":1}'


def test_deflate_raw_decoded() -> None:
    """Раньше accept-encoding просил deflate, а распаковывался только gzip."""
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = compressor.compress(b'{"a":1}') + compressor.flush()
    assert _decode_body(raw, "deflate") == '{"a":1}'


def test_broken_compression_does_not_raise() -> None:
    assert _decode_body(b"not compressed", "gzip") == "not compressed"


def test_response_json_and_body() -> None:
    ok = Response(status=200, text='{"a": 1}')
    assert ok.json == {"a": 1}
    assert ok.body == {"a": 1}
    html = Response(status=502, text="<html/>")
    assert html.json is None
    assert html.body == "<html/>"


# --------------------------------------------------------------------------- #
# Retry-After и секреты
# --------------------------------------------------------------------------- #


def test_retry_after_seconds() -> None:
    assert _parse_retry_after("12") == 12.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("тарабарщина") is None


def test_retry_after_http_date() -> None:
    value = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert value is not None and value > 0


def test_redact_hides_tokens() -> None:
    text = '{"access_token": "eyJhbGciOi.secret.value", "name": "Anvar"}'
    masked = redact(text)
    assert "eyJhbGciOi" not in masked
    assert "Anvar" in masked


def test_safe_text_truncates() -> None:
    response = Response(status=200, text="x" * 5000)
    assert len(response.safe_text(200)) < 260
