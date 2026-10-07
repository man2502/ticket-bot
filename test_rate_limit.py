"""Тесты лестницы пауз после HTTP 429.

Аккаунты блокировали после серии 429. Здесь проверяется то, из-за чего это
происходило, и то, что теперь так не будет:

* транспорт не повторяет 429 сам;
* 429 на любом запросе — в том числе первом в цикле и на входе — включает паузу;
* пауза растёт 2 → 5 → 15 → 30 мин → стоп, и переживает перезапуск;
* Retry-After больше ступени соблюдается;
* пауза общая для всех процессов, волна 429 считается одним событием.

Запуск (из корня репозитория):
    python run_tests.py test_rate_limit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "airlines_api")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core import rate_limit as rl  # noqa: E402


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    return tmp_path / "rate_limit.json"


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch: pytest.MonkeyPatch):
    """Разброс ±10% проверяем отдельно — в остальных тестах нужны точные числа."""
    monkeypatch.setattr(rl.random, "uniform", lambda a, b: 0.0)


T0 = 1_800_000_000.0


def _after_pause(path: Path) -> float:
    """Момент сразу после конца текущей паузы — следующий 429 уже не «та же волна»."""
    return rl.read_state(path).until + 1


# --------------------------------------------------------------------------- #
# Лестница
# --------------------------------------------------------------------------- #


def test_ladder_climbs_two_five_fifteen_thirty_then_stops(state: Path) -> None:
    delays = []
    now = T0
    for _ in range(4):
        decision = rl.register_429(state, retry_after=None, now=now)
        assert not decision.stop
        delays.append(round(decision.delay))
        now = _after_pause(state)

    assert delays == [120, 300, 900, 1800]

    final = rl.register_429(state, retry_after=None, now=now)
    assert final.stop
    assert rl.is_stopped(state, now=now) == pytest.approx(rl.STOP_COOLDOWN_SEC)


def test_retry_after_longer_than_step_wins(state: Path) -> None:
    """Сервер сказал ждать 15 минут — ждём 15, а не 2."""
    decision = rl.register_429(state, retry_after=900, now=T0)
    assert decision.delay == pytest.approx(900)
    assert decision.level == 1
    assert rl.read_state(state).last_retry_after == 900


def test_retry_after_shorter_than_step_is_ignored(state: Path) -> None:
    decision = rl.register_429(state, retry_after=5, now=T0)
    assert decision.delay == pytest.approx(120)


def test_retry_after_is_reported_in_the_description(state: Path) -> None:
    with_header = rl.register_429(state, retry_after=900, now=T0).describe()
    assert "Retry-After=900с" in with_header


def test_jitter_stays_within_ten_percent(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    monkeypatch.setattr(rl.random, "uniform", random.uniform)
    delay = rl.register_429(state, retry_after=None, now=T0).delay
    assert 108 <= delay <= 132


# --------------------------------------------------------------------------- #
# Волна и спуск
# --------------------------------------------------------------------------- #


def test_wave_of_429_counts_once(state: Path) -> None:
    """
    Процессы с одного адреса ловят 429 почти одновременно. Считать это тремя
    ступенями значило бы остановить всё на первой же волне.
    """
    rl.register_429(state, retry_after=None, now=T0)
    rl.register_429(state, retry_after=None, now=T0 + 3)
    third = rl.register_429(state, retry_after=None, now=T0 + 7)

    assert third.level == 1
    assert third.same_wave
    assert rl.read_state(state).until == pytest.approx(T0 + 120)


def test_wave_still_respects_a_longer_retry_after(state: Path) -> None:
    rl.register_429(state, retry_after=None, now=T0)
    rl.register_429(state, retry_after=600, now=T0 + 2)
    assert rl.read_state(state).until == pytest.approx(T0 + 2 + 600)


def test_step_goes_down_one_per_clean_hour(state: Path) -> None:
    now = T0
    for _ in range(3):  # ступень 3
        rl.register_429(state, retry_after=None, now=now)
        now = _after_pause(state)

    after_one_hour = rl.read_state(state).until + 3600 + 1
    decision = rl.register_429(state, retry_after=None, now=after_one_hour)
    # Было 3, час чистый → 2, и новый 429 поднимает до 3 (15 мин), а не до 4.
    assert decision.level == 3
    assert decision.delay == pytest.approx(900)


def test_long_quiet_period_returns_to_the_start(state: Path) -> None:
    rl.register_429(state, retry_after=None, now=T0)
    decision = rl.register_429(state, retry_after=None, now=T0 + 5 * 3600)
    assert decision.level == 1


def test_state_survives_restart(state: Path) -> None:
    """Раньше пауза жила в памяти: перезапуск — и снова 150 с, как будто 429 не было."""
    rl.register_429(state, retry_after=None, now=T0)
    # «Перезапуск» — новый процесс читает тот же файл.
    decision = rl.register_429(state, retry_after=None, now=_after_pause(state))
    assert decision.level == 2


# --------------------------------------------------------------------------- #
# Общая пауза и стоп
# --------------------------------------------------------------------------- #


def test_pause_is_visible_to_other_processes(state: Path) -> None:
    rl.register_429(state, retry_after=None, now=T0)
    assert rl.active_pause(state, now=T0 + 60) == pytest.approx(60)
    assert rl.active_pause(state, now=T0 + 200) == 0


def test_clear_lifts_stop_but_keeps_the_step(state: Path) -> None:
    """Следующий 429 после ручного снятия должен сразу дать длинную паузу."""
    now = T0
    for _ in range(4):
        rl.register_429(state, retry_after=None, now=now)
        now = _after_pause(state)
    assert rl.register_429(state, retry_after=None, now=now).stop
    assert rl.is_stopped(state, now=now + 1)

    rl.clear_stop(state)
    assert rl.is_stopped(state) == 0
    assert rl.active_pause(state) == 0
    assert rl.read_state(state).level == len(rl.LADDER_SEC)


# --------------------------------------------------------------------------- #
# Бот
# --------------------------------------------------------------------------- #


def test_transport_does_not_retry_429() -> None:
    """Каждый 429 в логе на деле был тремя запросами подряд за пару секунд."""
    from core.http import _RETRYABLE_STATUS

    assert 429 not in _RETRYABLE_STATUS
    assert 503 in _RETRYABLE_STATUS


class _Response:
    def __init__(self, status: int, retry_after: float | None = None):
        self.status = status
        self.text = "Too Many Requests" if status == 429 else "{}"
        self.body = {}
        self.retry_after = retry_after


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def request(self, method, path, **kwargs):
        self.sent.append(path)
        return self.responses.pop(0)


@pytest.fixture()
def bot(state: Path, monkeypatch: pytest.MonkeyPatch):
    """api_client с подменённым сервером и общей паузой в tmp — без настоящего сна."""
    import api_client
    import api_rate_limit

    monkeypatch.setenv("API_RATE_LIMIT_STATE", str(state))
    slept = []
    monkeypatch.setattr(api_rate_limit, "_sleep_chunked", lambda s: slept.append(s))
    # Время в боте — то же, что видит лестница: сон двигает часы вперёд.
    clock = {"now": T0}
    monkeypatch.setattr(rl.time, "time", lambda: clock["now"])

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(api_rate_limit, "_sleep_chunked", fake_sleep)
    return api_client, slept, clock


def test_429_on_first_request_of_the_cycle_pauses(bot, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Главная дыра: `customers/info` — первый запрос каждого цикла, и 429 на нём
    раньше не включал паузу вовсе. Бот стучался дальше по расписанию.
    """
    api_client, slept, _clock = bot
    client = _Client([_Response(429, retry_after=None)])
    monkeypatch.setattr(api_client, "get_client", lambda: client)

    response = api_client.api_call("GET", "customers/info", headers={})

    assert response.status == 429
    assert sum(slept) == pytest.approx(120)


def test_next_request_waits_for_a_pause_set_by_another_process(
    bot, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 у одного аккаунта — пауза для всех: они ходят с одного адреса."""
    api_client, slept, _clock = bot
    rl.register_429(state, retry_after=None, now=T0)  # «другой процесс»
    client = _Client([_Response(200)])
    monkeypatch.setattr(api_client, "get_client", lambda: client)

    api_client.api_call("GET", "captcha/finding", headers={})

    assert sum(slept) == pytest.approx(120)
    assert client.sent == ["captcha/finding"]


def test_stop_reaches_every_process(bot, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import api_rate_limit

    api_client, _slept, _clock = bot
    now = T0
    for _ in range(5):
        rl.register_429(state, retry_after=None, now=now)
        now = _after_pause(state)
    client = _Client([_Response(200)])
    monkeypatch.setattr(api_client, "get_client", lambda: client)

    with pytest.raises(api_rate_limit.RateLimitStop):
        api_client.api_call("GET", "customers/info", headers={})
    assert client.sent == [], "после стопа запрос не должен уходить вовсе"


def test_stop_is_not_swallowed_by_except_exception() -> None:
    """В боте много `except Exception` — стоп обязан пройти сквозь них."""
    from api_rate_limit import RateLimitStop

    assert not issubclass(RateLimitStop, Exception)


def test_booking_request_never_sleeps_between_journal_and_post(
    bot, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    CreateBooking уже записан в журнал как pending. Ждать или останавливаться
    между записью и POST нельзя: прерванная там бронь заблокировала бы аккаунт.
    """
    api_client, slept, _clock = bot
    rl.register_429(state, retry_after=None, now=T0)
    client = _Client([_Response(429)])
    monkeypatch.setattr(api_client, "get_client", lambda: client)

    api_client.api_call("POST", "CreateBooking", headers={}, idempotent=False)

    assert slept == []
    assert client.sent == ["CreateBooking"]
    # Но ступень поднята — остальные процессы подождут.
    assert rl.read_state(state).events == 2


def test_booking_429_is_a_certain_failure() -> None:
    """Без этого страница «Too Many Requests» считалась «бронь под вопросом»."""
    import api_booking

    result = api_booking.parse_booking_response("Too Many Requests", status=429)
    assert result.ok is False
    assert result.unknown is False


def test_login_429_does_not_kill_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Раньше 429 на входе был фатальным: процесс падал, его перезапускали, и он
    сразу логинился снова. Частые входы после 429 — самое подозрительное.
    """
    import api_auth

    monkeypatch.setenv("API_IOS_PASSWORD", "x")
    monkeypatch.setattr(api_auth, "_auth_request", lambda *a, **k: (429, "Too Many Requests"))

    with pytest.raises(api_auth.AuthTransientError):
        api_auth.refresh_app_token(verbose=False)


def test_error_line_names_endpoint_and_retry_after(
    bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    from contextlib import redirect_stdout

    api_client, _slept, _clock = bot
    client = _Client([_Response(429, retry_after=900)])
    monkeypatch.setattr(api_client, "get_client", lambda: client)

    out = io.StringIO()
    with redirect_stdout(out):
        api_client.api_call("GET", "customers/info", headers={})

    line = next(l for l in out.getvalue().splitlines() if l.startswith("[err]"))
    assert "rate_limit" in line
    assert "customers/info" in line
    assert "Retry-After=900с" in line
