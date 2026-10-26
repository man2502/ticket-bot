"""Тесты стопа при блокировке аккаунта, предохранителя и файлового журнала запросов.

Аккаунт заблокировали («Customer Block», HTTP 450), а бот трое суток раз
в минуту продолжал им стучаться: ответ шёл в журнал как «прочее», без кода,
и считался обычной неудачей цикла. Здесь проверяется, что теперь:

* 450 на любом запросе останавливает процесс, и следующий запрос не уходит;
* код ответа и запрос попадают в журнал ошибок;
* неопознанная ошибка подряд останавливает процесс, а не крутится сутками;
* «Поисков в час» из настроек доходит до бота;
* каждый запрос пишется в файл, тело отказа — без документов и токенов.

Запуск (из корня репозитория):
    python run_tests.py airlines_api
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import api_check  # noqa: E402
import api_client  # noqa: E402

BLOCK_BODY = json.dumps(
    {
        "success": False,
        "message": "Customer Block",
        "data": (
            "<h3>Уважаемый(ая) Иванова Анна,</h3>\n"
            "<p>Ваш аккаунт временно заблокирован. Покупка билетов недоступна "
            "до завершения блокировки.</p>\n<p>Приносим извинения за неудобства. "
            "По вопросам обратитесь по номеру: +99312 00-00-00.</p>"
        ),
    },
    ensure_ascii=False,
)


class _Response:
    def __init__(self, status: int, text: str = "{}"):
        self.status = status
        self.text = text
        self.retry_after = None
        self.elapsed_ms = 12.3
        self.attempts = 1

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent: list[str] = []

    def request(self, method, path, **kwargs):
        self.sent.append(path)
        return self.responses.pop(0)


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Подменённый сервер, журнал запросов и пауза 429 — в tmp."""
    monkeypatch.setenv("API_RATE_LIMIT_STATE", str(tmp_path / "rate_limit.json"))
    monkeypatch.setenv("BOT_HTTP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("API_PROVIDER_NAME", "Тестовый")
    api_client.reset_client()

    def install(*responses):
        client = _Client(responses)
        monkeypatch.setattr(api_client, "get_client", lambda: client)
        return client

    yield install
    api_client.reset_client()


def _journal_lines(tmp_path: Path) -> list[dict]:
    files = list((tmp_path / "logs").glob("http-*.jsonl"))
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# Блок аккаунта
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status, text",
    [(450, "{}"), (200, BLOCK_BODY), (403, '{"message": "customer block"}')],
)
def test_block_is_recognised_by_status_or_text(status: int, text: str) -> None:
    assert api_client.is_account_blocked_response(status, text)


def test_ordinary_refusals_are_not_a_block() -> None:
    assert not api_client.is_account_blocked_response(402, '{"message": "captcha"}')
    assert not api_client.is_account_blocked_response(429, "Too Many Requests")


def test_block_reason_is_short_and_without_owner_name() -> None:
    reason = api_client.block_reason(BLOCK_BODY)

    assert reason.startswith("Customer Block: Ваш аккаунт временно заблокирован.")
    assert "Иванова" not in reason, "имя владельца аккаунта в журнале ни к чему"
    assert "+99312" not in reason


def test_block_stops_the_process_and_no_more_requests_go_out(server) -> None:
    client = server(_Response(450, BLOCK_BODY), _Response(200))

    with pytest.raises(api_client.AccountBlocked) as caught:
        api_client.api_call("POST", "GetAirAvailability", headers={})
    assert caught.value.status == 450
    assert caught.value.endpoint == "GetAirAvailability"

    with pytest.raises(api_client.AccountBlocked):
        api_client.api_call("GET", "customers/info", headers={})
    assert client.sent == ["GetAirAvailability"], "с заблокированного аккаунта — ни запроса"


def test_block_passes_through_except_exception() -> None:
    """В боте много `except Exception` — стоп обязан дойти до главного цикла."""
    assert not issubclass(api_client.AccountBlocked, Exception)


def test_block_inside_booking_returns_the_refusal_then_stops(server) -> None:
    """Внутри CreateBooking не бросаем: попытка уже записана как pending."""
    client = server(_Response(450, BLOCK_BODY), _Response(200))

    response = api_client.api_call("POST", "CreateBooking", headers={}, idempotent=False)
    assert response.status == 450

    with pytest.raises(api_client.AccountBlocked):
        api_client.api_call("GET", "customers/info", headers={})
    assert client.sent == ["CreateBooking"]


def test_watch_loop_exits_with_its_own_code_and_journal_line(monkeypatch) -> None:
    def blocked(**kwargs):
        raise api_client.AccountBlocked(
            "Customer Block: Ваш аккаунт временно заблокирован.",
            status=450,
            endpoint="GetAirAvailability",
        )

    monkeypatch.setattr(api_check, "_watch_loop", blocked)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = api_check.run_watch_loop()

    assert code == api_client.ACCOUNT_BLOCKED_EXIT_CODE == 4
    assert (
        "[err] account_blocked | HTTP 450 | GetAirAvailability | Customer Block" in out.getvalue()
    )


# --------------------------------------------------------------------------- #
# Код ответа в журнале ошибок
# --------------------------------------------------------------------------- #


def test_failure_hint_gives_status_and_endpoint_to_the_journal() -> None:
    from api_errors import report_error

    hint = api_check.failure_hint("GetAirAvailability", 418, {"success": False, "message": "x"})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        report_error("other", detail=hint)

    assert out.getvalue().startswith("[err] other | HTTP 418 | GetAirAvailability | ")


def test_5xx_hint_is_classified_as_server_error() -> None:
    from api_errors import classify_error_text

    hint = api_check.failure_hint("GetAirAvailability", 502, "Bad Gateway")
    assert classify_error_text(hint) == "http_5xx"


# --------------------------------------------------------------------------- #
# Предохранитель
# --------------------------------------------------------------------------- #


def test_breaker_stops_after_n_unknown_errors_in_a_row() -> None:
    breaker = api_check.UnknownErrorBreaker(3)
    assert not breaker.note("other")
    assert not breaker.note("other")
    assert breaker.note("other")


def test_breaker_resets_on_normal_cycle_and_ignores_known_errors() -> None:
    breaker = api_check.UnknownErrorBreaker(3)
    breaker.note("other")
    breaker.note("other")
    # Сетевой сбой посередине счёт не трогает: блок вперемешку с таймаутами
    # не должен прятаться от предохранителя.
    assert not breaker.note("network")
    assert breaker.note("other")

    breaker = api_check.UnknownErrorBreaker(3)
    breaker.note("other")
    breaker.note("other")
    breaker.note(None)
    assert not breaker.note("other")


def test_breaker_limit_from_env(monkeypatch) -> None:
    monkeypatch.setenv("API_UNKNOWN_ERROR_STOP_AFTER", "8")
    assert api_check.unknown_error_stop_after() == 8
    monkeypatch.setenv("API_UNKNOWN_ERROR_STOP_AFTER", "мусор")
    assert api_check.unknown_error_stop_after() == 5


# --------------------------------------------------------------------------- #
# Поисков в час
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw, gap", [("30", 120.0), ("60", 60.0), ("0", 0.0), ("", 0.0), ("x", 0.0)])
def test_searches_per_hour_become_a_minimum_gap(monkeypatch, raw: str, gap: float) -> None:
    monkeypatch.setenv("API_MAX_SEARCHES_PER_HOUR", raw)
    assert api_check.search_min_gap_sec() == gap


def test_budget_waits_until_the_gap_is_over(monkeypatch) -> None:
    monkeypatch.setenv("API_MAX_SEARCHES_PER_HOUR", "30")
    slept: list[float] = []
    monkeypatch.setattr(api_check, "precise_sleep", slept.append)
    monkeypatch.setattr(api_check.time, "monotonic", lambda: 1000.0)

    api_check.wait_for_search_budget(None)  # первый поиск — сразу
    api_check.wait_for_search_budget(1000.0 - 30)
    # 120 с от прошлого поиска, минус прогрев и секунда запаса на выбор слота.
    assert slept == [pytest.approx(120 - 30 - api_check.WARMUP_LEAD_SEC - 1)]

    slept.clear()
    api_check.wait_for_search_budget(1000.0 - 500)
    assert slept == []


# --------------------------------------------------------------------------- #
# Файловый журнал запросов
# --------------------------------------------------------------------------- #


def test_every_request_is_written_to_the_day_file(server, tmp_path: Path) -> None:
    server(_Response(200, '{"success": true}'), _Response(402, '{"message": "captcha"}'))

    api_client.api_call("GET", "customers/info", headers={})
    api_client.api_call("POST", "GetAirAvailability", headers={})

    first, second = _journal_lines(tmp_path)
    assert (first["ep"], first["st"], first["acct"]) == ("customers/info", 200, "Тестовый")
    assert "body" not in first, "тело успешного ответа не пишем — это сотни килобайт рейсов"
    assert second["st"] == 402
    assert "captcha" in second["body"]


def test_journal_masks_documents_tokens_and_email(server, tmp_path: Path) -> None:
    body = json.dumps({
        "message": "bad passenger",
        "doc": "I-AH 123456",
        "passport": "AB1234567",
        "access_token": "secret-token-value-1234",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
        "mail": "someone@example.com",
    })
    server(_Response(400, body))

    api_client.api_call("POST", "CreateBooking", headers={}, idempotent=False)

    text = _journal_lines(tmp_path)[0]["body"]
    assert "AB1234567" not in text and "123456" not in text and "<doc>" in text
    assert "secret-token-value" not in text
    assert "eyJhbGci" not in text
    assert "someone@" not in text and "***@example.com" in text


def test_network_failure_is_written_with_zero_status(server, tmp_path: Path, monkeypatch) -> None:
    class _Down:
        def request(self, method, path, **kwargs):
            raise api_client.RequestFailed("connect timed out")

    monkeypatch.setattr(api_client, "get_client", lambda: _Down())

    status, _body, _ra = api_client.api_request("GET", "captcha/finding", headers={})

    assert status == 0
    line = _journal_lines(tmp_path)[0]
    assert (line["st"], line["err"]) == (0, "connect timed out")


def test_no_log_dir_means_no_files(server, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BOT_HTTP_LOG_DIR", raising=False)
    server(_Response(200))

    api_client.api_call("GET", "customers/info", headers={})

    assert not (tmp_path / "logs").exists()
