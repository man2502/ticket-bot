#!/usr/bin/env python3
"""
Шаги бронирования T5: логин, маршрут, капча, выбор рейса.
Полный сценарий — book.main() (python book.py или python steps.py).
"""

from __future__ import annotations

import base64
import os
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.env import env, env_flag
from core.state import load_json, save_json
from core.term import cyan
from flight_index import (
    flight_array_indexes,
    flight_prefer_time,
    flight_selection_label as _selection_label,
    flight_strict,
    flight_want_indexes,
    format_flight_indexes,
)

BASE = "https://turkmenistanairlines.tm"
LANG = "ru"

# На сайте T5 капча всегда из 6 цифр
CAPTCHA_LEN = 6


class CaptchaImageTimeout(Exception):
    """Картинка капчи на сайте не появилась за отведённое время."""


class AccountBlocked(Exception):
    """Аккаунт заблокирован или временно ограничен на сайте."""


class LoginFailed(Exception):
    """Не удалось войти в личный кабинет."""


# Подсказки CNN печатаем голубым — палитра общая, см. core.term.
_light_blue = cyan


def goto_retry(
    page,
    url: str,
    *,
    timeout_ms: int = 90000,
    attempts: int = 5,
    wait_until: str = "domcontentloaded",
    label: str = "",
) -> None:
    """
    Открыть URL с повторами при net::ERR_TIMED_OUT / обрыве сети.
    Сайт T5 частоту таймаутит — одна ошибка не должна ронять скрипт.
    """
    last_exc: Exception | None = None
    tag = label or "nav"
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if i > 1:
                print(f"[{tag}] открылось с попытки {i}/{attempts}")
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc)
            transient = any(
                x in msg
                for x in (
                    "ERR_TIMED_OUT",
                    "ERR_CONNECTION",
                    "ERR_NAME_NOT_RESOLVED",
                    "ERR_NETWORK",
                    "ERR_INTERNET_DISCONNECTED",
                    "ERR_ADDRESS_UNREACHABLE",
                    "Timeout",
                    "timeout",
                    "NS_ERROR_NET",
                )
            )
            print(f"[{tag}] goto fail {i}/{attempts}: {msg.splitlines()[0][:120]}")
            if not transient and i >= 2:
                break
            pause_ms = min(2000 * i, 8000)
            try:
                page.wait_for_timeout(pause_ms)
            except Exception:
                time.sleep(pause_ms / 1000.0)
            # запасной URL с www
            if i == 2 and "://" in url and "://www." not in url:
                url = url.replace("://", "://www.", 1)
                print(f"[{tag}] пробую www: {url}")
    assert last_exc is not None
    raise last_exc


class NoFlightsAvailable(Exception):
    """На выдаче нет рейсов с местами (только «Мест нет» или пусто)."""


class TicketsSearchTimeout(Exception):
    """Карточки рейсов на /flights/search не появились (зависла выдача)."""


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or str(default)).strip() or str(default))
    except ValueError:
        return default


def tickets_wait_sec() -> float:
    return max(4.0, _env_float("TICKETS_WAIT_SEC", 16.0))


def tickets_stale_sec() -> float:
    """Нет карточек — считаем зависанием и выходим быстрее."""
    return max(2.0, _env_float("TICKETS_STALE_SEC", 6.0))


def tickets_reload_on_stuck() -> bool:
    return (os.getenv("TICKETS_RELOAD_ON_STUCK") or "0").strip().lower() not in (
        "0",
        "false",
        "no",
    )


SKIP_FLIGHT_RE = r"мест\s*нет|транзит|сквозн|transit|connecting|пересадк|via\b"

ACCOUNT_BLOCK_RE = re.compile(
    r"заблокирован|блокиров|too many|слишком много|превышен|"
    r"attempts exceeded|account (?:is )?blocked|denied|"
    r"попробуйте позже|try again later",
    re.I,
)

_CAPTCHA_FAIL_STREAK = 0
_CAPTCHA_BACKOFF_SEC = (5.0, 15.0, 30.0)


def captcha_max_attempts() -> int:
    try:
        return max(1, int(env("CAPTCHA_MAX_ATTEMPTS", "5") or "5"))
    except ValueError:
        return 5


def search_max_rounds() -> int:
    try:
        return max(1, int(env("BOOK_SEARCH_MAX_ROUNDS", "3") or "3"))
    except ValueError:
        return 3


def search_rate_max_per_hour() -> int:
    try:
        return max(0, int(env("BOOK_SEARCH_MAX_PER_HOUR", "40") or "40"))
    except ValueError:
        return 40


def login_fail_cooldown_sec() -> float:
    try:
        return max(0.0, float(env("LOGIN_FAIL_COOLDOWN_SEC", "600") or "600"))
    except ValueError:
        return 600.0


def login_fail_streak_limit() -> int:
    try:
        return max(1, int(env("LOGIN_FAIL_STREAK_LIMIT", "3") or "3"))
    except ValueError:
        return 3


def _rate_state_path() -> Path:
    account = env("ACCOUNT_NAME") or env("CHROME_PROFILE") or "default"
    safe = re.sub(r"[^\w.-]+", "_", account)
    return ROOT / f".search_rate_{safe}.json"


def _load_search_times() -> list[float]:
    """Метки времени прошлых поисков. Битый файл — не повод падать: считаем пустым."""
    raw = load_json(_rate_state_path(), [])
    if not isinstance(raw, list):
        return []
    out: list[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _save_search_times(times: list[float]) -> None:
    """
    Атомарная запись счётчика.

    Обычный write_text — это truncate + write: Ctrl-C между ними оставлял
    обрезанный JSON, счётчик обнулялся, и бот уходил за часовой лимит поисков
    ровно тогда, когда сайт и так злился.
    """
    try:
        save_json(_rate_state_path(), times, indent=None)
    except OSError as exc:
        print(f"[rate] не сохранил счётчик поисков: {exc}")


def wait_search_rate_limit(page=None) -> None:
    """Ждёт, если превышен лимит поисков в час (BOOK_SEARCH_MAX_PER_HOUR)."""
    max_h = search_rate_max_per_hour()
    if max_h <= 0:
        return
    while True:
        now = time.time()
        hour_ago = now - 3600.0
        times = [t for t in _load_search_times() if t > hour_ago]
        _save_search_times(times)
        if len(times) < max_h:
            return
        wait_sec = max(1.0, times[0] + 3600.0 - now)
        print(
            f"[rate] лимит {max_h} поисков/час — пауза {wait_sec:.0f}с "
            f"(сейчас {len(times)})"
        )
        end = time.time() + wait_sec
        while time.time() < end:
            chunk = min(1.0, end - time.time())
            if page is not None:
                try:
                    page.wait_for_timeout(int(chunk * 1000))
                except Exception:
                    time.sleep(chunk)
            else:
                time.sleep(chunk)


def record_search_attempt() -> None:
    max_h = search_rate_max_per_hour()
    if max_h <= 0:
        return
    now = time.time()
    hour_ago = now - 3600.0
    times = [t for t in _load_search_times() if t > hour_ago]
    times.append(now)
    _save_search_times(times)


def note_captcha_success() -> None:
    global _CAPTCHA_FAIL_STREAK
    _CAPTCHA_FAIL_STREAK = 0


def captcha_backoff_pause(page=None, *, reason: str = "капча не прошла") -> float:
    """Экспоненциальная пауза после неудачной капчи: 5 → 15 → 30 с."""
    global _CAPTCHA_FAIL_STREAK
    _CAPTCHA_FAIL_STREAK += 1
    idx = min(_CAPTCHA_FAIL_STREAK - 1, len(_CAPTCHA_BACKOFF_SEC) - 1)
    delay = _CAPTCHA_BACKOFF_SEC[idx]
    print(
        f"[captcha] {reason} — backoff {_CAPTCHA_FAIL_STREAK}, "
        f"пауза {delay:.0f}с"
    )
    end = time.time() + delay
    while time.time() < end:
        chunk = min(0.5, end - time.time())
        if page is not None:
            try:
                page.wait_for_timeout(int(chunk * 1000))
            except Exception:
                time.sleep(chunk)
        else:
            time.sleep(chunk)
    return delay


def detect_account_blocked(page) -> str | None:
    """Текст блокировки/лимита на странице логина или None."""
    chunks: list[str] = []
    try:
        body = page.evaluate(
            "() => ((document.body || {}).innerText || '').replace(/\\s+/g, ' ').trim()"
        )
        if body:
            chunks.append(str(body))
    except Exception:
        pass
    for sel in ("#telephone-error", "#password-error", ".alert-danger", ".error-message"):
        try:
            loc = page.locator(sel)
            if loc.count():
                txt = (loc.first.inner_text() or "").strip()
                if txt:
                    chunks.append(txt)
        except Exception:
            continue
    merged = " ".join(chunks)
    if not merged:
        return None
    if ACCOUNT_BLOCK_RE.search(merged):
        return merged[:240]
    return None


def _login_error_details(page) -> str:
    parts: list[str] = []
    for sel in ("#telephone-error", "#password-error"):
        try:
            loc = page.locator(sel)
            if loc.count():
                txt = (loc.first.inner_text() or "").strip()
                if txt:
                    parts.append(txt)
        except Exception:
            continue
    if parts:
        return ", ".join(parts)
    if "/auth/login" in (page.url or ""):
        return "остались на странице входа"
    return ""


def _ensure_logged_in(page) -> None:
    blocked = detect_account_blocked(page)
    if blocked:
        raise AccountBlocked(blocked)
    if "/auth/login" in (page.url or ""):
        details = _login_error_details(page)
        raise LoginFailed(details or "не удалось войти в личный кабинет")


def login_cooldown_pause(page=None, *, seconds: float | None = None) -> None:
    if seconds is None:
        seconds = login_fail_cooldown_sec()
    seconds = max(0.0, seconds)
    if seconds <= 0:
        return
    print(f"[login] cooldown {seconds:.0f}с после серии неудачных входов")
    end = time.time() + seconds
    while time.time() < end:
        chunk = min(1.0, end - time.time())
        if page is not None:
            try:
                page.wait_for_timeout(int(chunk * 1000))
            except Exception:
                time.sleep(chunk)
        else:
            time.sleep(chunk)


def _fill_login_form(page, local: str, full: str, password: str) -> None:
    """Заполнение формы входа через реальный ввод (как watch.py)."""
    visible = page.locator("#telephone")
    visible.click()
    visible.fill("")
    visible.type(local, delay=random.randint(25, 55))
    page.evaluate(
        """([full, local]) => {
            const v = document.getElementById('telephone');
            const h = document.getElementById('telephone-hidden');
            if (v) {
                v.value = local;
                v.dispatchEvent(new Event('input', { bubbles: true }));
                v.dispatchEvent(new Event('change', { bubbles: true }));
            }
            if (h) {
                h.value = full;
                h.dispatchEvent(new Event('input', { bubbles: true }));
                h.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""",
        [full, local],
    )
    pwd = page.locator("#password")
    pwd.click()
    pwd.fill("")
    pwd.type(password, delay=random.randint(25, 55))
    pwd.dispatch_event("input")
    page.locator("#auth-login-submit").click()


def on_flights_search_page(page) -> bool:
    return "/flights/search" in (page.url or "")


def save_tickets_debug(page, tag: str) -> Path | None:
    try:
        path = ROOT / f".debug_tickets_{tag}.html"
        path.write_text(page.content(), encoding="utf-8")
        print(f"[tickets] debug HTML: {path}")
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"[tickets] debug save fail: {exc}")
        return None


def _evaluate_tickets_wait_state(page, skip_flight_re: str) -> dict:
    """Состояние выдачи: карточки, пусто, капча."""
    try:
        return page.evaluate(
            """(skipRe) => {
                const skip = new RegExp(skipRe, 'i');
                const cards = [...document.querySelectorAll('.ticket-card')];
                const available = cards.filter(c => {
                    const t = (c.innerText || '');
                    if (skip.test(t)) return false;
                    const btn = c.querySelector('[data-ticket-card-button]')
                        || [...c.querySelectorAll('button,a')].find(b =>
                            /выбрать|отменить|saýla|sayla|ýatyrmak|yatyrmak|select|cancel/i
                                .test((b.innerText || '').trim())
                        );
                    return !!btn;
                });
                const bodyText = ((document.body || {}).innerText || '');
                const emptyMsg = /нет доступ|рейсов не найдено|билетов нет|no flights|рейсы не отображаются|30\\s*дней до даты/i
                    .test(bodyText);
                const captchaOpen = (() => {
                    const popup = document.getElementById('captchaPopup');
                    return !!(popup && popup.offsetParent !== null
                        && popup.style.display !== 'none');
                })();
                return {
                    url: location.href || '',
                    cards: cards.length,
                    available: available.length,
                    vybrat: available.filter(c =>
                        [...c.querySelectorAll('[data-ticket-card-button], button, a')]
                            .some(e => /выбрать|saýla|sayla|select/i.test((e.innerText || '').trim()))
                    ).length,
                    otmenit: available.filter(c =>
                        [...c.querySelectorAll('[data-ticket-card-button], button, a')]
                            .some(e => /отменить|ýatyrmak|yatyrmak|cancel/i.test((e.innerText || '').trim()))
                    ).length,
                    selected: document.querySelectorAll('.ticket-card.selected').length,
                    onlyBad: cards.length > 0 && available.length === 0,
                    emptyMsg,
                    captchaOpen,
                };
            }""",
            skip_flight_re,
        )
    except Exception as exc:  # noqa: BLE001
        return {"cards": 0, "available": 0, "error": str(exc)}


def wait_for_tickets_list(page, *, skip_flight_re: str, allow_reload: bool = True) -> dict:
    """
    Ждём карточки с «Выбрать» на /flights/search (до TICKETS_WAIT_SEC).
    Без карточек > TICKETS_STALE_SEC — быстрый выход (зависание).
    """
    deadline = time.time() + tickets_wait_sec()
    stale_sec = tickets_stale_sec()
    stale_since: float | None = None
    has: dict = {}
    last_log = 0.0

    cur_url = page.url or ""
    print(f"[tickets] жду выдачу на /flights/search… url={cur_url}")

    while time.time() < deadline:
        now = time.time()
        cur_url = page.url or ""
        has = _evaluate_tickets_wait_state(page, skip_flight_re)

        if has.get("captchaOpen"):
            raise TicketsSearchTimeout("Капча снова открылась на выдаче — нужен новый поиск")

        if not on_flights_search_page(page):
            save_tickets_debug(page, "left_search")
            raise TicketsSearchTimeout(
                "Сессия поиска сброшена — не на /flights/search "
                f"(cards={has.get('cards', 0)}, url={cur_url})"
            )

        if has.get("available") or has.get("vybrat") or has.get("otmenit"):
            print(f"[tickets] выдача (без транзита): {has}")
            return has

        if has.get("onlyBad") or has.get("emptyMsg"):
            print(f"[tickets] нет прямых рейсов с местами: {has}")
            raise NoFlightsAvailable("Нет прямых рейсов с местами (транзит не берём)")

        if has.get("cards", 0) == 0 and not has.get("emptyMsg"):
            if stale_since is None:
                stale_since = now
            elif now - stale_since >= stale_sec:
                print(
                    f"[tickets] выдача зависла: cards=0 уже "
                    f"{int(now - stale_since)}с — не жду до конца"
                )
                break
        else:
            stale_since = None

        if now - last_log >= 2.0:
            left = max(0, int(deadline - now))
            print(f"[tickets] жду выдачу… cards={has.get('cards', 0)} ({left}с)")
            last_log = now

        page.wait_for_timeout(150)

    if (
        tickets_reload_on_stuck()
        and allow_reload
        and on_flights_search_page(page)
        and not has.get("available")
    ):
        print(
            "[tickets] пробую reload страницы выдачи "
            "(может сбросить сессию — TICKETS_RELOAD_ON_STUCK=0 отключает)…"
        )
        try:
            page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:  # noqa: BLE001
            print(f"[tickets] reload fail: {exc}")
        else:
            page.wait_for_timeout(800)
            if not on_flights_search_page(page):
                save_tickets_debug(page, "reload_lost_session")
                raise TicketsSearchTimeout(
                    "Reload сбросил сессию поиска — нужен новый поиск "
                    f"(url={page.url})"
                )
            try:
                return wait_for_tickets_list(
                    page, skip_flight_re=skip_flight_re, allow_reload=False
                )
            except (NoFlightsAvailable, TicketsSearchTimeout):
                raise

    if has.get("onlyBad") or has.get("emptyMsg") or (
        has.get("cards", 0) > 0 and not has.get("available")
    ):
        raise NoFlightsAvailable("Нет прямых рейсов с местами (таймаут)")

    save_tickets_debug(page, "timeout")
    raise TicketsSearchTimeout(
        "Рейсы с кнопкой «Выбрать» не появились "
        f"(cards={has.get('cards', 0)}, url={page.url})"
    )


def captcha_restart_wait_sec() -> float:
    try:
        return float(os.getenv("CAPTCHA_IMG_TIMEOUT_WAIT_SEC", "30").strip() or "30")
    except ValueError:
        return 30.0


def is_captcha_image_timeout(exc: Exception) -> bool:
    msg = str(exc)
    if isinstance(exc, CaptchaImageTimeout):
        return True
    if not isinstance(exc, PlaywrightTimeout):
        return False
    if "refreshRecaptcha img" in msg or "captchaPopup img" in msg:
        return True
    return "Locator.screenshot" in msg and "element to be stable" in msg


def wait_and_restart_from_zero(page, *, wait_sec: float | None = None) -> None:
    """Пауза + очистка сессии, логин и заполнение маршрута с нуля."""
    from browser_helper import clear_browser_data, save_bandwidth

    if wait_sec is None:
        wait_sec = captcha_restart_wait_sec()
    wait_sec = max(0.0, wait_sec)
    print(
        f"\033[93m[captcha] картинка не загрузилась — "
        f"жду {wait_sec:.0f}с и начинаю с нуля…\033[0m"
    )
    end = time.time() + wait_sec
    while time.time() < end:
        left = end - time.time()
        chunk = min(0.5, left)
        try:
            page.wait_for_timeout(int(chunk * 1000))
        except Exception:
            time.sleep(chunk)

    try:
        clear_browser_data(page)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] очистка сессии: {exc}")

    print("[restart] логин и маршрут заново…")
    step_login(page)
    force_home = not (save_bandwidth() and search_form_on_page(page) and on_home_url(page))
    if force_home:
        try:
            goto_retry(page, f"{BASE}/{LANG}", timeout_ms=90000, attempts=5, label="restart")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] главная после restart: {exc}")
    else:
        print("[restart] форма уже на главной — без перезагрузки (SAVE_BANDWIDTH)")
    try:
        fill_route_now(page, force_home=force_home)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] маршрут после restart: {exc}")

ID_TO_CODE = {
    "5": "ASB",
    "14": "CRZ",
    "3": "BKN",
    "6": "TAZ",
    "15": "MYP",
    "21": "KRW",
    "25": "KEA",
}


# env() общий (core.env). Раньше здесь была локальная копия с другим
# поведением на пустом значении: `NOTIFY_EMAIL=` она возвращала как "",
# а notify_helper — как default. Одна строка .env означала разное.
_flag = env_flag


# flight_strict / flight_prefer_time / flight_want_indexes импортированы из
# flight_index — это те же FLIGHT_* переменные, что читает api_booking.
# Держать вторую копию значило рисковать тем, что два бота на одном .env
# начнут выбирать разные рейсы.


def flight_selection_label() -> str:
    """Режим выбора рейса для стартового лога."""
    # В браузере запасной рейс — это карточка с активной кнопкой «Выбрать».
    return _selection_label(fallback_hint="другие прямые с «Выбрать»")


def evaluate_wanted_flight_match(page, *, want_indexes: list[int], prefer_time: str, strict: bool) -> dict:
    """Совпадает ли уже выбранный на сайте рейс с FLIGHT_INDEX / FLIGHT_TIME."""
    return page.evaluate(
        """(args) => {
            const skip = new RegExp(args.skipRe, 'i');
            const wants = (args.wantIndexes || []).map(i => Math.max(0, i | 0));
            const preferTime = (args.preferTime || '').replace('.', ':').trim();
            const strict = !!args.strict;
            const allCards = [...document.querySelectorAll('.ticket-card')];
            const isSelectable = (card) => {
                const t = card.innerText || '';
                if (skip.test(t)) return false;
                return !!card.querySelector('[data-ticket-card-button]');
            };
            const selected = document.querySelector('.ticket-card.selected');
            if (!selected) {
                return { matches: false, reason: 'none selected', total: allCards.length };
            }
            const fullIdx = allCards.indexOf(selected);
            if (fullIdx < 0) {
                return {
                    matches: false,
                    reason: 'selected not available',
                    total: allCards.length,
                };
            }
            if (preferTime) {
                const t = (selected.innerText || '').replace(/\\s+/g, ' ').replace(/\\./g, ':');
                if (!t.includes(preferTime)) {
                    return {
                        matches: false,
                        reason: 'time mismatch',
                        pickedIndex: fullIdx,
                        preferTime,
                        total: allCards.length,
                    };
                }
                return { matches: true, pickedIndex: fullIdx, total: allCards.length };
            }
            const preferred = wants
                .filter(i => i < allCards.length)
                .map(i => allCards[i]);
            const preferredOk = preferred.filter(isSelectable);
            if (preferred.includes(selected) && isSelectable(selected)) {
                return {
                    matches: true,
                    pickedIndex: fullIdx,
                    want: wants,
                    total: allCards.length,
                };
            }
            if (strict || preferredOk.length) {
                return {
                    matches: false,
                    reason: 'index mismatch',
                    pickedIndex: fullIdx,
                    want: wants,
                    total: allCards.length,
                };
            }
            return {
                matches: true,
                pickedIndex: fullIdx,
                want: wants,
                total: allCards.length,
            };
        }""",
        {
            "skipRe": SKIP_FLIGHT_RE,
            "wantIndexes": want_indexes,
            "preferTime": prefer_time,
            "strict": strict,
        },
    )


def cnn_captcha_ready() -> bool:
    """Локальная CNN загружена и готова."""
    try:
        from captcha_cnn import is_ready

        return is_ready()
    except ImportError as exc:
        print(f"[captcha] CNN недоступна ({exc})")
        return False


def _solve_captcha_cnn(image: bytes) -> tuple[str, str]:
    """Распознавание капчи локальной CNN."""
    from captcha_cnn import min_confidence, predict

    try:
        code, conf = predict(image)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"CNN ошибка: {exc}") from exc
    if len(code) != CAPTCHA_LEN:
        raise RuntimeError(f"CNN дала не 6 цифр: {code!r}")
    print(_light_blue(f"[cnn] ответ: {code} (conf={conf:.2f})"))
    if conf < min_confidence():
        print(
            f"[cnn] conf={conf:.2f} < {min_confidence():.2f} "
            f"— всё равно ввожу"
        )
    return code, "cnn"


def normalize_tm_phone(raw: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("993") and len(digits) > 8:
        digits = digits[3:]
    if digits.startswith("8") and len(digits) == 9:
        digits = digits[1:]
    return digits, f"+993{digits}"


def normalize_port_code(raw: str, fallback: str) -> str:
    value = (raw or fallback).strip().upper()
    return ID_TO_CODE.get(value, value)


def to_site_date(date_str: str) -> str:
    """17.07.2026 → 7/17/2026"""
    date_str = date_str.strip()
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", date_str):
        return date_str
    m = re.match(r"^(\d{1,2})[.](\d{1,2})[.](\d{4})$", date_str)
    if not m:
        raise ValueError(f"Непонятная дата: {date_str}")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{month}/{day}/{year}"


def date_view_ru(date_str: str) -> str:
    """Видимый формат как на сайте: «Июль 17, Пт»."""
    import datetime

    site = to_site_date(date_str)
    month, day, year = [int(x) for x in site.split("/")]
    dt = datetime.date(year, month, day)

    months = [
        "Январь",
        "Февраль",
        "Март",
        "Апрель",
        "Май",
        "Июнь",
        "Июль",
        "Август",
        "Сентябрь",
        "Октябрь",
        "Ноябрь",
        "Декабрь",
    ]
    days_short = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    # Как на сайте: «Июль 17, Пт»
    return f"{months[month - 1]} {day}, {days_short[dt.weekday()]}"


def _wait_leave_login(page, seconds: int = 40) -> bool:
    """Ждёт ухода с /auth/login, каждую секунду проверяя URL (можно войти вручную)."""
    for i in range(seconds):
        try:
            if "/auth/login" not in (page.url or ""):
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
        if i in (5, 15, 30):
            print(f"[login] всё ещё на странице входа… ({i}с)")
    return "/auth/login" not in (page.url or "")


def step_login(page) -> None:
    print("\n=== ШАГ 1: LOGIN ===")
    phone = env("T5_PHONE")
    password = env("T5_PASSWORD")

    # Уже авторизован в persistent Chrome-профиле?
    try:
        goto_retry(page, f"{BASE}/{LANG}", timeout_ms=60000, attempts=4, label="login")
        page.wait_for_timeout(600)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] главная не открылась: {exc}")

    already = page.evaluate(
        """() => {
            const logout = document.querySelector(
                "a[href*='logout'], a[href*='auth/logout'], form[action*='logout']"
            );
            const account = document.querySelector(
                "a[href*='/account'], a[href*='profile'], .user-menu, .header-user"
            );
            return !!(logout || account);
        }"""
    )
    if already:
        print(f"[ok] Уже вошли (сессия Chrome) → {page.url}")
        print("[login] дальше → заполнение маршрута")
        return

    if not phone or not password:
        print("[warn] В .env нет T5_PHONE / T5_PASSWORD — войдите вручную (40 сек)…")
        goto_retry(page, f"{BASE}/{LANG}/auth/login", timeout_ms=60000, attempts=4, label="login")
        if not _wait_leave_login(page, 40):
            _ensure_logged_in(page)
        print("[login] дальше → заполнение маршрута")
        return

    local, full = normalize_tm_phone(phone)
    print(f"[login] телефон {full}")
    goto_retry(page, f"{BASE}/{LANG}/auth/login", timeout_ms=60000, attempts=4, label="login")
    page.wait_for_timeout(400)

    if "/auth/login" not in (page.url or ""):
        print(f"[ok] Уже авторизован, редирект → {page.url}")
        print("[login] дальше → заполнение маршрута")
        return

    try:
        page.wait_for_selector("#telephone", timeout=8000)
        page.wait_for_selector("#auth-login-submit", timeout=5000)
    except PlaywrightTimeout:
        if "/auth/login" not in (page.url or ""):
            print(f"[ok] Уже авторизован → {page.url}")
            print("[login] дальше → заполнение маршрута")
            return
        print("[warn] Форма логина не найдена. Войдите вручную (40 сек)…")
        if not _wait_leave_login(page, 40):
            _ensure_logged_in(page)
        print("[login] дальше → заполнение маршрута")
        return

    _fill_login_form(page, local, full, password)

    try:
        page.wait_for_function(
            "() => !location.pathname.includes('/auth/login')",
            timeout=12000,
        )
        print(f"[ok] Логин выполнен → {page.url}")
    except PlaywrightTimeout:
        blocked = detect_account_blocked(page)
        if blocked:
            raise AccountBlocked(blocked)
        print("[warn] Автологин не подтвердился. Войдите вручную в окне…")
        if _wait_leave_login(page, 40):
            print(f"[ok] Вошли вручную → {page.url}")
        else:
            _ensure_logged_in(page)
    else:
        blocked = detect_account_blocked(page)
        if blocked:
            raise AccountBlocked(blocked)
    print("[login] дальше → заполнение маршрута")


def fetch_ports(page) -> list[dict]:
    data = page.evaluate(
        """async () => {
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), 20000);
            try {
                const r = await fetch('/ru/get_internal_ports', {
                    credentials: 'same-origin',
                    signal: ctrl.signal,
                });
                return await r.json();
            } finally {
                clearTimeout(t);
            }
        }"""
    )
    if not data or not data.get("success"):
        raise RuntimeError(f"Не получил список аэропортов: {data}")
    return data["data"]


def find_port(ports: list[dict], code: str, city_hint: str) -> dict:
    code = code.upper()
    for p in ports:
        if p.get("portcode", "").upper() == code:
            return p
    city_hint_l = (city_hint or "").lower()
    for p in ports:
        if city_hint_l and city_hint_l in (p.get("cityname") or "").lower():
            return p
    raise RuntimeError(f"Аэропорт не найден: code={code}, city={city_hint}")


def set_port_fields(page, which: str, port: dict) -> None:
    """
    which: 'depart' | 'arrival'
    Эмулируем selectItem сайта: cityname в инпут, portcode в hidden + span.
    """
    assert which in ("depart", "arrival")
    page.evaluate(
        """([which, port]) => {
            const form = document.forms['internal_ticket_search'];
            if (!form) throw new Error('нет формы internal_ticket_search');

            const inputId = which === 'depart' ? 'internal-depart-port' : 'internal-arrival-port';
            const spanId = which === 'depart' ? 'internal-departure-port-span' : 'internal-arrival-port-span';
            const hiddenName = which === 'depart' ? 'departPort' : 'arrivalPort';

            const input = document.getElementById(inputId);
            const span = document.getElementById(spanId);
            let hidden = form.querySelector(`input[name="${hiddenName}"]`);
            if (!hidden) {
                hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = hiddenName;
                form.appendChild(hidden);
            }

            // как selectItem в home.js
            if (input) {
                input.value = port.cityname || '';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }
            hidden.value = port.portcode || '';
            if (span) span.textContent = port.portcode || '';
        }""",
        [which, port],
    )


def _set_date_direct(page, date_str: str, site: str, visible: str) -> None:
    """Запасной путь без календаря (если оверлей блокирует UI)."""
    page.evaluate(
        """([site, visible]) => {
            const f = document.forms['internal_ticket_search'];
            const vis = document.getElementById('internal-depart-date');
            if (!f) throw new Error('нет формы');
            let hidden = f.departDate;
            if (!hidden) {
                hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = 'departDate';
                f.appendChild(hidden);
            }
            hidden.value = site;
            if (f.tripType) f.tripType.value = 'OW';
            if (vis) {
                vis.value = visible;
                vis.dispatchEvent(new Event('input', { bubbles: true }));
                vis.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""",
        [site, visible],
    )
    print(f"[date] запасной путь (без календаря): visible={visible!r} hidden={site!r}")


def set_date_fields(page, date_str: str) -> None:
    """Открывает календарь и кликает нужный день — формат ставит сам сайт."""
    site = to_site_date(date_str)  # M/D/YYYY, напр. 7/17/2026
    visible = date_view_ru(date_str)

    # Только JS — focus()/click() Playwright часто зависают из‑за оверлеев
    opened = page.evaluate(
        """() => {
            const el = document.getElementById('internal-depart-date');
            if (!el) return false;
            el.scrollIntoView({ block: 'center' });
            el.dispatchEvent(new Event('focusin', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            el.focus();
            return true;
        }"""
    )
    if not opened:
        raise RuntimeError("Поле даты #internal-depart-date не найдено")

    page.wait_for_timeout(400)
    cells = page.locator(".ticket-calendar__day-cell")
    try:
        cells.first.wait_for(state="attached", timeout=8000)
    except PlaywrightTimeout:
        print("[warn] Календарь не открылся — ставлю дату напрямую")
        _set_date_direct(page, date_str, site, visible)
        return

    clicked = False
    for _ in range(10):
        found = page.evaluate(
            """(site) => {
                const list = [...document.querySelectorAll('.ticket-calendar__day-cell')];
                const el = list.find(c =>
                    c.dataset.dateInput === site
                    && !c.classList.contains('disabled')
                    && !c.classList.contains('empty')
                );
                if (!el) return false;
                el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                return true;
            }""",
            site,
        )
        if found:
            clicked = True
            break
        moved = page.evaluate(
            """() => {
                const btns = document.querySelectorAll(
                    '.tickets-calendar-column_second .ticket-calendar-column__button, .ticket-calendar-column__button'
                );
                if (!btns.length) return false;
                btns[btns.length - 1].click();
                return true;
            }"""
        )
        if not moved:
            break
        page.wait_for_timeout(350)

    if not clicked:
        print(f"[warn] День {date_str} в календаре не найден — ставлю дату напрямую")
        _set_date_direct(page, date_str, site, visible)
        return

    page.wait_for_timeout(250)
    snap = page.evaluate(
        """() => {
            const vis = document.getElementById('internal-depart-date');
            const f = document.forms['internal_ticket_search'];
            return {
                dateVisible: vis ? vis.value : null,
                departDate: f && f.departDate ? f.departDate.value : null,
            };
        }"""
    )
    print(f"[date] из календаря: visible={snap.get('dateVisible')!r} hidden={snap.get('departDate')!r}")

    if not snap.get("departDate") or not snap.get("dateVisible"):
        _set_date_direct(page, date_str, site, visible)


def read_form_snapshot(page) -> dict:
    return page.evaluate(
        """() => {
            const f = document.forms['internal_ticket_search'];
            if (!f) return { error: 'no form' };
            return {
                departPort: f.departPort ? f.departPort.value : null,
                arrivalPort: f.arrivalPort ? f.arrivalPort.value : null,
                departDate: f.departDate ? f.departDate.value : null,
                tripType: f.tripType ? f.tripType.value : null,
                departVisible: (document.getElementById('internal-depart-port') || {}).value || null,
                arrivalVisible: (document.getElementById('internal-arrival-port') || {}).value || null,
                dateVisible: (document.getElementById('internal-depart-date') || {}).value || null,
                departSpan: (document.getElementById('internal-departure-port-span') || {}).textContent || null,
                arrivalSpan: (document.getElementById('internal-arrival-port-span') || {}).textContent || null,
            };
        }"""
    )


def route_fields_empty(page) -> bool:
    """True если нет формы или пусты откуда/куда/дата (видимые или hidden)."""
    snap = read_form_snapshot(page)
    if snap.get("error"):
        return True
    dep = (snap.get("departPort") or snap.get("departVisible") or snap.get("departSpan") or "").strip()
    arr = (snap.get("arrivalPort") or snap.get("arrivalVisible") or snap.get("arrivalSpan") or "").strip()
    date = (snap.get("departDate") or snap.get("dateVisible") or "").strip()
    return not (dep and arr and date)


def get_route_defaults() -> tuple[str, str, str, str, str]:
    """depart_code, arrival_code, depart_city, arrival_city, date_str."""
    depart_code = normalize_port_code(
        env("DEPART_PORT_CODE") or env("DEPART_PORT_ID"), "ASB"
    )
    arrival_code = normalize_port_code(
        env("ARRIVAL_PORT_CODE") or env("ARRIVAL_PORT_ID"), "MYP"
    )
    depart_city = env("DEPART_CITY", "Ashgabat")
    arrival_city = env("ARRIVAL_CITY", "Mary")
    dates = [d.strip() for d in env("DATES", "17.07.2026").split(",") if d.strip()]
    return depart_code, arrival_code, depart_city, arrival_city, dates[0]


def route_notify_text(date_str: str | None = None, flight_time: str | None = None) -> str:
    """Строка для уведомлений и логов: Откуда → Куда, дата время."""
    depart_code, arrival_code, _, _, default_date = get_route_defaults()
    from_label = env("DEPART_CITY") or depart_code
    to_label = env("ARRIVAL_CITY") or arrival_code
    flight_date = (date_str or default_date or "").strip()
    time_str = (flight_time or env("FLIGHT_TIME") or "").strip().replace(".", ":")
    parts = [f"{from_label} → {to_label}"]
    when = " ".join(p for p in (flight_date, time_str) if p)
    if when:
        parts.append(when)
    return ", ".join(parts)


def log_picked_flight(clicked: dict | None, *, tag: str = "[tickets]") -> None:
    """Человекочитаемый лог выбранного рейса (маршрут + время, без «№X из Y»)."""
    clicked = clicked or {}
    if not clicked.get("ok"):
        print(f"{tag} выбор не удался: {clicked}")
        return
    time_val = (clicked.get("time") or "").strip()
    print(f"{tag} выбран: {route_notify_text(flight_time=time_val or None)}")


def on_home_url(page) -> bool:
    url = (page.url or "").split("?")[0].rstrip("/").lower()
    return url.endswith(f"turkmenistanairlines.tm/{LANG}") or url.endswith("turkmenistanairlines.tm")


def search_form_on_page(page) -> bool:
    try:
        return page.locator("#internal-depart-port").count() > 0
    except Exception:
        return False


def _search_passenger_counts() -> tuple[int, int, int]:
    """adult, child (5-12), infant (0-5) — для internal_ticket_search."""
    try:
        from passenger_loader import get_passenger_counts_by_type

        return get_passenger_counts_by_type()
    except Exception:
        return 1, 0, 0


def set_search_passenger_counts(page, adults: int, child: int, infant: int) -> None:
    """Ставит adult / child / infant в форме поиска (внутренние рейсы T5)."""
    if adults <= 1 and child <= 0 and infant <= 0:
        return
    result = page.evaluate(
        """({ adults, child, infant }) => {
            const form = document.forms['internal_ticket_search'];
            if (!form) return { ok: false, reason: 'no form' };
            const setField = (name, val) => {
                const el = form.querySelector(
                    '#internal-custom-numbers input[name="' + name + '"], input[name="' + name + '"]'
                );
                if (!el) return false;
                el.removeAttribute('readonly');
                el.removeAttribute('max');
                el.value = String(val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            };
            const okAdult = setField('adult', adults);
            const okChild = child > 0 ? setField('child', child) : true;
            const okInfant = infant > 0 ? setField('infant', infant) : true;
            const total = adults + child + infant;
            const display = document.getElementById('internal-passengers-select');
            if (display) {
                const lang = display.dataset.language || 'Пассажиры';
                display.value = lang + ' (' + total + ')';
            }
            return {
                ok: okAdult && okChild && okInfant,
                adult: adults,
                child,
                infant,
                total,
            };
        }""",
        {"adults": adults, "child": child, "infant": infant},
    )
    print(f"[route] пассажиры в поиске: adult={adults} child={child} infant={infant} ({result})")


def set_search_passenger_count(page, adults: int) -> None:
    """Обратная совместимость: только взрослые."""
    set_search_passenger_counts(page, adults, 0, 0)


def fill_route_now(page, *, force_home: bool = False) -> str:
    """
    Заполняет откуда/куда/дата на текущей главной (или открывает главную).
    Возвращает date_str.
    """
    from browser_helper import save_bandwidth

    need_home = force_home or route_fields_empty(page) or not search_form_on_page(page)
    form_on_search = search_form_on_page(page) and on_flights_search_page(page)
    need_nav = need_home and not form_on_search and (
        force_home
        or "/flights/" in (page.url or "")
        or not search_form_on_page(page)
    )
    if need_nav:
        if (
            save_bandwidth()
            and search_form_on_page(page)
            and (on_home_url(page) or on_flights_search_page(page))
            and "/flights/booking" not in (page.url or "")
        ):
            loc = "выдаче" if on_flights_search_page(page) else "главной"
            print(f"[route] форма на {loc} уже есть — без перезагрузки (SAVE_BANDWIDTH)")
        else:
            print("[route] открываю главную для заполнения…")
            goto_retry(page, f"{BASE}/{LANG}", timeout_ms=90000, attempts=5, label="route")
    elif form_on_search and need_home:
        print("[route] заполняю форму на выдаче — без refresh")

    page.wait_for_selector("#internal-depart-port", timeout=20000)

    depart_code, arrival_code, depart_city, arrival_city, date_str = get_route_defaults()
    ports = fetch_ports(page)
    dep = find_port(ports, depart_code, depart_city)
    arr = find_port(ports, arrival_code, arrival_city)
    print(
        f"[route] заполняю: {dep['cityname']} ({dep['portcode']}) → "
        f"{arr['cityname']} ({arr['portcode']}), {date_str}"
    )
    set_port_fields(page, "depart", dep)
    set_port_fields(page, "arrival", arr)
    set_date_fields(page, date_str)
    adults, child, infant = _search_passenger_counts()
    if adults > 1 or child > 0 or infant > 0:
        set_search_passenger_counts(page, adults, child, infant)
    snap = read_form_snapshot(page)
    print(
        f"[route] снимок: dep={snap.get('departPort')!r} "
        f"arr={snap.get('arrivalPort')!r} date={snap.get('departDate')!r} "
        f"vis={snap.get('dateVisible')!r}"
    )
    return date_str


def ensure_route_filled(page) -> str:
    """
    Перед поиском / при ошибке / новой попытке:
    если поля пустые — заполнить заново. Иначе оставить как есть.
    """
    if page.locator("#internal-depart-port").count() == 0:
        print("[route] формы нет — заполняю заново")
        return fill_route_now(page, force_home=True)

    if route_fields_empty(page):
        print("[route] поля пустые (откуда/куда/дата) — заполняю заново")
        return fill_route_now(page, force_home=False)

    snap = read_form_snapshot(page)
    print(
        f"[route] поля уже есть: {snap.get('departPort')} → "
        f"{snap.get('arrivalPort')}, date={snap.get('departDate')}"
    )
    dates = [d.strip() for d in env("DATES", "17.07.2026").split(",") if d.strip()]
    return dates[0]


def click_search(page) -> None:
    btn = page.locator("#internal-ticket-submit")
    if btn.count() == 0:
        raise RuntimeError("Кнопка поиска не найдена")
    btn.first.click(force=True)


def refresh_captcha_image(page) -> None:
    page.evaluate(
        """() => {
            const req = new XMLHttpRequest();
            req.open('POST', '/refresh-captcha', false);
            req.send();
            const box = document.getElementById('refreshRecaptcha');
            if (box) box.innerHTML = req.responseText;
            const input = document.getElementById('captchaInput');
            if (input) input.value = '';
        }"""
    )
    # ждём, пока НОВАЯ картинка реально прогрузится, иначе снимем пустую
    _wait_captcha_img_loaded(page, timeout_ms=3000)


def _wait_captcha_img_loaded(page, *, timeout_ms: int = 4000) -> None:
    """Ждёт, пока <img> капчи полностью загрузится (complete && naturalWidth>0)."""
    img = page.locator("#refreshRecaptcha img, #captchaPopup img").first
    try:
        img.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeout:
        return
    try:
        img.evaluate(
            """(el, ms) => new Promise((resolve) => {
                const done = () => resolve(true);
                if (el.complete && el.naturalWidth > 0) return done();
                el.addEventListener('load', done, { once: true });
                el.addEventListener('error', done, { once: true });
                setTimeout(done, ms);
            })""",
            timeout_ms,
        )
    except Exception:
        pass


def _captcha_img_png_via_canvas(img) -> bytes | None:
    """PNG из <img> через canvas — без scroll/stability Playwright."""
    try:
        b64 = img.evaluate(
            """(el) => {
                if (!el.complete || el.naturalWidth <= 0 || el.naturalHeight <= 0) {
                    return null;
                }
                const canvas = document.createElement('canvas');
                canvas.width = el.naturalWidth;
                canvas.height = el.naturalHeight;
                canvas.getContext('2d').drawImage(el, 0, 0);
                const dataUrl = canvas.toDataURL('image/png');
                return dataUrl.split(',')[1] || null;
            }"""
        )
        if not b64:
            return None
        return base64.b64decode(b64)
    except Exception:
        return None


def grab_captcha_image_bytes(page) -> bytes:
    """Берёт PNG картинки капчи из попапа (дождавшись полной загрузки)."""
    img = page.locator("#refreshRecaptcha img, #captchaPopup img").first
    try:
        img.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeout as exc:
        raise CaptchaImageTimeout(str(exc)) from exc
    # критично: не снимать скриншот, пока картинка не догрузилась —
    # иначе уходит старое/пустое изображение и CNN читает не то
    _wait_captcha_img_loaded(page, timeout_ms=4000)

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        if attempt > 1:
            time.sleep(0.4)
            _wait_captcha_img_loaded(page, timeout_ms=2000)
        try:
            return img.screenshot(type="png", animations="disabled", timeout=8000)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        png = _captcha_img_png_via_canvas(img)
        if png:
            if attempt > 1 or last_exc is not None:
                print("[captcha] screenshot не удался — взял PNG через canvas")
            return png

    msg = str(last_exc) if last_exc else "не удалось получить PNG капчи"
    raise CaptchaImageTimeout(msg) from last_exc


def captcha_still_open(page) -> bool:
    popup = page.locator("#captchaPopup")
    try:
        if popup.count() == 0:
            return False
        return bool(popup.first.is_visible())
    except Exception:
        return False


def user_already_passed_captcha(page) -> bool:
    """True на выдаче/брони без открытой капчи, или когда видны карточки рейсов."""
    if captcha_still_open(page):
        return False
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if "/flights/booking" in url:
        return True
    if "/flights/search" in url:
        return True
    try:
        if page.locator(".ticket-card, [data-ticket-card-button]").count() > 0:
            return True
    except Exception:
        pass
    return False


def form_had_route(snap: dict) -> bool:
    """Были ли заполнены откуда/куда/дата до отправки капчи."""
    if snap.get("error"):
        return False
    dep = (snap.get("departPort") or snap.get("departVisible") or snap.get("departSpan") or "").strip()
    arr = (snap.get("arrivalPort") or snap.get("arrivalVisible") or snap.get("arrivalSpan") or "").strip()
    date = (snap.get("departDate") or snap.get("dateVisible") or "").strip()
    return bool(dep and arr and date)


def _route_part_filled(snap: dict, keys: tuple[str, ...]) -> bool:
    return any((snap.get(k) or "").strip() for k in keys)


def route_fields_cleared_since(snap_before: dict, page) -> bool:
    """Сайт сбросил хотя бы одно из полей откуда/куда/дата."""
    if not form_had_route(snap_before):
        return False
    if not search_form_on_page(page):
        return False
    cur = read_form_snapshot(page)
    if cur.get("error"):
        return True
    groups = (
        ("departPort", "departVisible", "departSpan"),
        ("arrivalPort", "arrivalVisible", "arrivalSpan"),
        ("departDate", "dateVisible"),
    )
    for keys in groups:
        if _route_part_filled(snap_before, keys) and not _route_part_filled(cur, keys):
            return True
    return False


def captcha_rejection_reason(
    page,
    *,
    route_baseline: dict | None = None,
    captcha_had_input: bool = False,
    flights_seen: bool = False,
) -> str | None:
    """
    Сигналы отказа капчи без явного текста ошибки.
    Если выдача уже была — сброс полей НЕ считаем ошибкой капчи.
    """
    if flights_seen or user_already_passed_captcha(page):
        return None

    cur = captcha_snapshot(page)
    if cur.get("hasError"):
        return "ошибка на форме"

    if captcha_still_open(page) and captcha_had_input and cur.get("inputLen", 0) == 0:
        return "очищено поле капчи"

    if route_baseline and route_fields_cleared_since(route_baseline, page):
        return "очищены откуда/куда/дата"

    return None


def _log_captcha_rejected(tag: str, reason: str) -> None:
    print(f"\033[93m[{tag}] НЕ ПОДОШЛА ({reason}) — сразу новый поиск\033[0m")


def _captcha_src(page) -> str:
    return (captcha_snapshot(page).get("src") or "").strip()


def _wait_for_captcha_stable(
    page,
    *,
    settle_ms: float = 450,
    timeout_sec: float = 3.5,
) -> str:
    """
    После повторного «Поиск» сайт может мелькнуть старой капчей.
    Ждём, пока src перестанет меняться.
    """
    deadline = time.time() + timeout_sec
    last_src = ""
    stable_since = 0.0
    while time.time() < deadline:
        if not captcha_still_open(page):
            break
        src = _captcha_src(page)
        if not src:
            page.wait_for_timeout(50)
            continue
        now = time.time()
        if src != last_src:
            if last_src:
                print("[captcha] картинка сменилась — жду финальную…")
            last_src = src
            stable_since = now
        elif (now - stable_since) * 1000 >= settle_ms:
            return src
        page.wait_for_timeout(50)
    return last_src


def _type_captcha_code(page, code: str) -> None:
    inp = page.locator("#captchaInput")
    inp.click()
    inp.fill("")
    inp.press_sequentially(code, delay=random.randint(90, 160))


def _submit_captcha_code(
    page,
    code: str,
    tag: str,
    *,
    baseline_src: str = "",
) -> tuple[str, bytes | None]:
    """
    Ввод капчи; во время паузы 1.0–1.2 с перед «Отправить» следит за сменой картинки.
    Если капча обновилась — CNN читает новую и вводит заново (пауза 0.8–1.0 с).
    """
    pre_submit_delay = random.choice((1.0, 1.1, 1.2))
    after_swap_delay = random.choice((0.8, 0.9, 1.0))
    watch_src = (baseline_src or _captcha_src(page)).strip()
    final_code = code
    final_image: bytes | None = None

    _type_captcha_code(page, code)
    end = time.time() + pre_submit_delay

    while time.time() < end:
        if not captcha_still_open(page):
            break
        left = end - time.time()
        if left <= 0:
            break
        time.sleep(min(0.1, left))
        cur_src = _captcha_src(page)
        if not watch_src or not cur_src or cur_src == watch_src:
            continue
        print(f"[{tag}] капча сменилась во время паузы — читаю новую")
        try:
            image = grab_captcha_image_bytes(page)
            new_code, _ = _solve_captcha_cnn(image)
            if len(new_code) != CAPTCHA_LEN:
                print(f"[{tag}] новая капча: CNN дал {new_code!r} — оставляю старый ввод")
                watch_src = cur_src
                continue
            print(f"[{tag}] новая капча: ввожу {new_code}")
            final_code = new_code
            final_image = image
            watch_src = cur_src
            _type_captcha_code(page, new_code)
            end = time.time() + after_swap_delay
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] не прочитал новую капчу: {exc}")
            watch_src = cur_src

    if captcha_still_open(page):
        page.locator("#submitCaptcha").click(force=True)
    else:
        print(f"[{tag}] попап закрылся после ввода — жду проверку сайтом")
    return final_code, final_image


def wait_after_captcha_submit(
    page,
    baseline: dict,
    tag: str,
    *,
    route_baseline: dict | None = None,
    captcha_had_input: bool = False,
) -> bool:
    """
    После ввода кода:
      True  — попап закрылся и выдача доступна
      False — отказ (ошибка / новая картинка) или таймаут
    Пока попап открыт — не успех (важно для повторного поиска на /flights/search).
    """
    deadline = time.time() + 6.0
    print(f"[{tag}] жду выдачу рейсов после капчи…")

    while time.time() < deadline:
        if captcha_still_open(page):
            reason = captcha_rejection_reason(
                page,
                route_baseline=route_baseline,
                captcha_had_input=captcha_had_input,
            )
            if reason:
                _log_captcha_rejected(tag, reason)
                return False
            base_src = (baseline or {}).get("src") or ""
            cur_src = captcha_snapshot(page).get("src") or ""
            if base_src and cur_src and cur_src != base_src:
                _log_captcha_rejected(tag, "новая картинка")
                return False
            page.wait_for_timeout(100)
            continue

        if user_already_passed_captcha(page):
            print(f"[ok] Капча ПРИНЯТА → {page.url}")
            return True

        try:
            page.wait_for_url("**/flights/**", timeout=500)
        except PlaywrightTimeout:
            pass
        if user_already_passed_captcha(page):
            print(f"[ok] Капча ПРИНЯТА → {page.url}")
            return True

        page.wait_for_timeout(100)

    if captcha_still_open(page):
        _log_captcha_rejected(tag, "таймаут, попап ещё открыт")
        return False

    if user_already_passed_captcha(page):
        print(f"[ok] Капча ПРИНЯТА → {page.url}")
        return True

    reason = captcha_rejection_reason(
        page,
        route_baseline=route_baseline,
        captcha_had_input=captcha_had_input,
    )
    if reason:
        _log_captcha_rejected(tag, reason)
        return False

    print(
        f"\033[93m[{tag}] НЕ ПОДОШЛА (нет выдачи после закрытия попапа) "
        f"url={page.url} — сразу новый поиск\033[0m"
    )
    return False


def captcha_snapshot(page) -> dict:
    """Снимок попапа капчи: src картинки, open, ошибки."""
    return page.evaluate(
        """() => {
            const popup = document.getElementById('captchaPopup');
            const img = document.querySelector('#refreshRecaptcha img, #captchaPopup img');
            const input = document.getElementById('captchaInput');
            const text = (popup && (popup.innerText || '')) || '';
            const open = !!(popup && (popup.style.display !== 'none') && popup.offsetParent !== null);
            return {
                open,
                src: img ? (img.currentSrc || img.src || img.getAttribute('src') || '') : '',
                inputLen: input ? String(input.value || '').trim().length : 0,
                hasError: /неверн|ошибк|incorrect|wrong|invalid|captcha\\s*error|неправильн/i.test(text),
            };
        }"""
    )


def make_captcha_abort_check(
    page,
    baseline: dict,
    *,
    route_baseline: dict | None = None,
    captcha_had_input: bool = False,
):
    """
    Пока ждём solver:
      - прошли → 'passed'
      - сами отправили и ошибка / новая картинка / попап закрылся → 'retry'
    """
    flights_seen = False

    def check():
        nonlocal flights_seen
        if user_already_passed_captcha(page):
            flights_seen = True
            return "passed"
        reason = captcha_rejection_reason(
            page,
            route_baseline=route_baseline,
            captcha_had_input=captcha_had_input,
            flights_seen=flights_seen,
        )
        if reason:
            return "error"
        cur = captcha_snapshot(page)
        if not captcha_still_open(page):
            if user_already_passed_captcha(page):
                flights_seen = True
                return "passed"
            return "closed"
        base_src = (baseline or {}).get("src") or ""
        cur_src = cur.get("src") or ""
        if base_src and cur_src and cur_src != base_src:
            return "changed"
        return None

    return check


# src последней ОТПРАВЛЕННОЙ (не подошедшей) картинки — чтобы не слать её повторно
_LAST_CAPTCHA_SRC = ""


def solve_captcha(page, max_attempts: int | None = None) -> bool:
    """
    CNN решает капчу; при ошибке или неверном ответе
    выходим → снаружи backoff и новый поиск.
    """
    global _LAST_CAPTCHA_SRC

    if max_attempts is None:
        max_attempts = captcha_max_attempts()

    if not cnn_captcha_ready():
        print("[captcha] CNN недоступна (нет captcha_model.pt) — только ручной ввод.")
        return False

    tag = "cnn"
    print("[captcha] Можно ввести 6 цифр сами в Chrome → «Отправить».")
    print(f"[captcha] solver={tag} (макс. {max_attempts} попыток за раунд)")

    for attempt in range(1, max_attempts + 1):
        if user_already_passed_captcha(page):
            print(f"[ok] Капча уже пройдена (вами) → {page.url}")
            return True

        if not captcha_still_open(page):
            # Коротко ждём появление (после клика поиска) — без 5–10 сек зависания
            try:
                page.locator("#captchaPopup").wait_for(state="visible", timeout=1500)
            except PlaywrightTimeout:
                if user_already_passed_captcha(page):
                    return True
                print("[captcha] попап закрыт/не виден — сразу новый поиск")
                return False

        print(f"\n[{tag}] === попытка {attempt}/{max_attempts} ===")
        print(f"[{tag}] жду стабильную капчу (без старой картинки)…")
        _wait_for_captcha_stable(page)
        baseline = captcha_snapshot(page)
        route_baseline = read_form_snapshot(page)
        # если картинка та же, что мы уже отправляли и она НЕ подошла —
        # принудительно берём новую капчу, иначе снова получим «неверно»
        cur_src = (baseline or {}).get("src") or ""
        if cur_src and cur_src == _LAST_CAPTCHA_SRC:
            print("[captcha] та же картинка, что в прошлый раз — обновляю капчу")
            try:
                refresh_captcha_image(page)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] обновление капчи: {exc}")
            baseline = captcha_snapshot(page)
        try:
            image = grab_captcha_image_bytes(page)
            code, solver_used = _solve_captcha_cnn(image)
            tag = solver_used
        except CaptchaImageTimeout:
            raise
        except Exception as exc:  # noqa: BLE001
            if is_captcha_image_timeout(exc):
                raise CaptchaImageTimeout(str(exc)) from exc
            print(f"\033[91m[{tag}] ошибка запроса: {exc}\033[0m")
            if user_already_passed_captcha(page):
                print("[ok] Пока была ошибка, вы уже прошли капчу — продолжаю.")
                return True
            abort_check = make_captcha_abort_check(
                page, baseline, route_baseline=route_baseline
            )
            status = abort_check()
            if status == "passed":
                return True
            if status == "closed":
                if wait_after_captcha_submit(
                    page, baseline, tag, route_baseline=route_baseline
                ):
                    return True
                return False
            if status in ("retry", "changed", "error"):
                print(f"[captcha] вы уже отправили ({status}) — сразу новый поиск")
                return False
            continue

        if user_already_passed_captcha(page):
            print(f"[ok] Вы уже прошли капчу сами — ответ {tag} не нужен.")
            return True

        if len(code) != CAPTCHA_LEN:
            print(
                f"[{tag}] ОТКЛОНЕНО: нужно ровно {CAPTCHA_LEN} цифр, "
                f"а reader дал {code!r} (len={len(code)}). Сразу новый поиск."
            )
            if user_already_passed_captcha(page):
                return True
            return False

        print(f"[{tag}] ввожу на сайт: {code}")
        # запоминаем картинку, которую отправляем — если не подойдёт,
        # на следующей попытке заставим обновить капчу
        _LAST_CAPTCHA_SRC = (baseline or {}).get("src") or ""
        try:
            final_code, swapped_image = _submit_captcha_code(
                page,
                code,
                tag,
                baseline_src=_LAST_CAPTCHA_SRC,
            )
            if swapped_image is not None:
                image = swapped_image
                code = final_code
                baseline = captcha_snapshot(page)
        except Exception as exc:  # noqa: BLE001
            if user_already_passed_captcha(page):
                print("[ok] Вы ввели капчу сами — продолжаю.")
                return True
            print(f"[{tag}] не смог ввести: {exc}")
            return False

        # closed ≠ отказ: попап закрывается и при УСПЕХЕ до смены URL
        if wait_after_captcha_submit(
            page,
            baseline,
            tag,
            route_baseline=route_baseline,
            captcha_had_input=True,
        ):
            from captcha_dataset import maybe_auto_train, save_confirmed

            save_confirmed(image, code, source=tag)
            maybe_auto_train()
            note_captcha_success()
            return True
        from captcha_dataset import save_rejected

        save_rejected(image, code, source=tag, reason="rejected")
        return False

    if user_already_passed_captcha(page):
        note_captcha_success()
        return True
    return False


def step_select_and_continue(page) -> dict:
    """
    После капчи на /flights/search:
      1) ОБЯЗАТЕЛЬНО нажать «Выбрать» на рейсе с местами
      2) проверить, что рейс selected (кнопка стала «Отменить»)
      3) только потом «Продолжить»
    Без выбранного рейса дальше НЕ идём.
    Возвращает {text, time, index, total} выбранного рейса.
    """
    print("\n=== ШАГ 4: Выбрать → Продолжить ===")

    selected_meta = {"text": "", "time": "", "index": None, "total": None}

    def _remember_pick(clicked: dict) -> None:
        if clicked.get("pickedIndex") is not None:
            selected_meta["index"] = int(clicked["pickedIndex"]) + 1
        if clicked.get("total") is not None:
            selected_meta["total"] = int(clicked["total"])
        if clicked.get("text"):
            selected_meta["text"] = clicked["text"]
        if clicked.get("time"):
            selected_meta["time"] = clicked["time"]

    def capture_selected() -> dict:
        info = page.evaluate(
            """() => {
                const selected = document.querySelector('.ticket-card.selected')
                    || document.querySelector('.ticket-card');
                const text = selected
                    ? (selected.innerText || '').replace(/\\s+/g, ' ').trim()
                    : '';
                const m = text.match(/\\b(\\d{1,2}[:.]\\d{2})\\b/);
                return {
                    text: text.slice(0, 160),
                    time: m ? m[1].replace('.', ':') : '',
                };
            }"""
        )
        if info.get("text"):
            selected_meta["text"] = info["text"]
        if info.get("time"):
            selected_meta["time"] = info["time"]
        elif not selected_meta["time"]:
            prefer = (env("FLIGHT_TIME") or "").strip().replace(".", ":")
            if prefer:
                selected_meta["time"] = prefer
        return dict(selected_meta)
    # прямой рейс: не «Мест нет», не транзит/сквозной
    # Возврат не нужен — вызов важен как ожидание прогрузки списка рейсов.
    wait_for_tickets_list(page, skip_flight_re=SKIP_FLIGHT_RE)

    def selection_state() -> dict:
        return page.evaluate(
            """(skipRe) => {
                const skip = new RegExp(skipRe, 'i');
                const selected = document.querySelector('.ticket-card.selected');
                const text = selected ? (selected.innerText || '').replace(/\\s+/g, ' ').trim() : '';
                const isTransit = !!(selected && skip.test(text));
                const otmenit = [...document.querySelectorAll(
                    '.ticket-card.selected [data-ticket-card-button], .ticket-card.selected button, .ticket-card.selected a'
                )].some(e => /отменить|ýatyrmak|yatyrmak|cancel/i.test((e.innerText || '').trim()));
                const info = document.querySelector('.main-tickets-info');
                return {
                    selected: !!selected,
                    otmenit,
                    isTransit,
                    infoActive: !!(info && info.classList.contains('active')),
                    searchId: selected ? (selected.dataset.searchId || '') : '',
                    bookingClassId: selected ? (selected.dataset.bookingClassId || '') : '',
                    text: text.slice(0, 140),
                };
            }""",
            SKIP_FLIGHT_RE,
        )

    state = selection_state()
    # если выбран транзит — снять выбор
    if state.get("selected") and state.get("isTransit"):
        print(f"[tickets] сбрасываю транзитный рейс: {state.get('text')}")
        page.evaluate(
            """() => {
                const card = document.querySelector('.ticket-card.selected');
                if (!card) return;
                const btn = card.querySelector('[data-ticket-card-button]')
                    || [...card.querySelectorAll('button,a')].find(b =>
                        /отменить|выбрать/i.test((b.innerText || '').trim())
                    );
                if (btn) btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            }"""
        )
        page.wait_for_timeout(400)
        state = selection_state()

    def site_selection_ok() -> bool:
        """Сайт реально зарегистрировал выбор (панель active), не только CSS .selected."""
        return bool(
            page.evaluate(
                """() => {
                    const info = document.querySelector('.main-tickets-info');
                    const selected = document.querySelector('.ticket-card.selected');
                    return !!(info && info.classList.contains('active') && selected);
                }"""
            )
        )

    def click_vybrat_real(want_indexes: list[int] | None = None) -> dict:
        """
        Клик ТОЛЬКО по [data-ticket-card-button] — иначе TicketsSelect.from
        не выставляется и «Продолжить» молча ничего не делает.
        want_indexes: 0-based позиции в полном списке .ticket-card.
        FLIGHT_STRICT=1 — только эти; 0 — сначала эти, иначе любой прямой с «Выбрать».
        """
        return page.evaluate(
            """(args) => {
                const skip = new RegExp(args.skipRe, 'i');
                const wants = (args.wantIndexes || []).map(i => Math.max(0, i | 0));
                const preferTime = (args.preferTime || '').replace('.', ':').trim();

                document.querySelectorAll('.ticket-card.selected').forEach(c => {
                    c.classList.remove('selected');
                });
                const info = document.querySelector('.main-tickets-info');
                if (info) info.classList.remove('active', 'ow', 'rt');

                const strict = !!args.strict;
                const allCards = [...document.querySelectorAll('.ticket-card')];
                const isSelectable = (card) => {
                    const t = card.innerText || '';
                    if (skip.test(t)) return false;
                    return !!card.querySelector('[data-ticket-card-button]');
                };
                if (!allCards.length) return { ok: false, reason: 'no cards' };

                let pick = null;
                if (preferTime) {
                    const pool = strict ? allCards : allCards.filter(isSelectable);
                    pick = pool.find(c => {
                        const t = (c.innerText || '').replace(/\\s+/g, ' ').replace(/\\./g, ':');
                        return t.includes(preferTime);
                    }) || null;
                    if (!pick && strict) {
                        return { ok: false, reason: 'strict: time not found', preferTime, total: allCards.length };
                    }
                }
                if (!pick) {
                    for (const want of wants) {
                        if (want >= allCards.length) continue;
                        const card = allCards[want];
                        if (isSelectable(card)) {
                            pick = card;
                            break;
                        }
                    }
                    if (!pick && strict) {
                        return {
                            ok: false,
                            reason: 'strict: index not found',
                            want: wants,
                            total: allCards.length,
                        };
                    }
                    if (!pick) {
                        pick = allCards.find(isSelectable) || null;
                    }
                }
                if (!pick) return { ok: false, reason: 'no cards', total: allCards.length };

                const btn = pick.querySelector('[data-ticket-card-button]');
                if (!btn) {
                    if (strict) {
                        return {
                            ok: false,
                            reason: 'strict: card not selectable',
                            want: wants,
                            pickedIndex: allCards.indexOf(pick),
                            total: allCards.length,
                        };
                    }
                    return { ok: false, reason: 'no btn' };
                }
                btn.click();
                const t = (pick.innerText || '').replace(/\\s+/g, ' ').trim();
                const tm = t.match(/\\b(\\d{1,2}[:.]\\d{2})\\b/);
                return {
                    ok: true,
                    text: t.slice(0, 120),
                    time: tm ? tm[1].replace('.', ':') : '',
                    searchId: pick.dataset.searchId || '',
                    bookingClassId: pick.dataset.bookingClassId || '',
                    type: pick.dataset.type || '',
                    pickedIndex: allCards.indexOf(pick),
                    total: allCards.length,
                    infoActive: !!(info && info.classList.contains('active')),
                };
            }""",
            {
                "skipRe": SKIP_FLIGHT_RE,
                "wantIndexes": want_indexes if want_indexes is not None else [],
                "preferTime": flight_prefer_time(),
                "strict": flight_strict(),
            },
        )

    flight_indexes = flight_want_indexes()
    array_indexes = flight_array_indexes(flight_indexes)
    idx_label = format_flight_indexes(flight_indexes)
    strict = flight_strict()
    prefer_time = flight_prefer_time()
    print(f"[tickets] выбор рейса: {flight_selection_label()}")

    def pick_flight() -> dict:
        """Единый путь выбора — тот же фильтр, что и в wait_for_tickets_list."""
        clicked = click_vybrat_real(array_indexes)
        if clicked.get("ok"):
            return clicked
        if not strict:
            loc = (
                page.locator(".ticket-card")
                .filter(has_not_text=re.compile(r"Мест\s*нет|транзит|Сквозн|Transit|пересадк", re.I))
                .locator("[data-ticket-card-button]")
            )
            n = loc.count()
            if n:
                target = loc.first
                try:
                    target.scroll_into_view_if_needed(timeout=3000)
                    target.click(timeout=5000)
                    return {
                        "ok": True,
                        "via": "playwright-fallback",
                        "pickedIndex": 0,
                        "total": n,
                    }
                except Exception as exc:  # noqa: BLE001
                    print(f"[tickets] Playwright fallback fail: {exc}")
        return clicked

    if state.get("selected") and state.get("otmenit") and not state.get("isTransit") and site_selection_ok():
        match = evaluate_wanted_flight_match(
            page,
            want_indexes=array_indexes,
            prefer_time=prefer_time,
            strict=strict,
        )
        if match.get("matches"):
            print(f"[tickets] рейс уже выбран сайтом и совпадает: {state.get('text')}")
            selected_meta["index"] = int(match.get("pickedIndex", array_indexes[0])) + 1
            if match.get("total"):
                selected_meta["total"] = int(match["total"])
            selected_meta["text"] = state.get("text") or ""
            capture_selected()
        else:
            print(
                f"[tickets] сайт выбрал другой рейс ({match.get('reason')}) — "
                f"нужен {flight_selection_label()}"
            )
            state = selection_state()
    else:
        state = selection_state()

    if not (
        state.get("selected")
        and state.get("otmenit")
        and not state.get("isTransit")
        and site_selection_ok()
        and evaluate_wanted_flight_match(
            page,
            want_indexes=array_indexes,
            prefer_time=prefer_time,
            strict=strict,
        ).get("matches")
    ):
        print("[tickets] жму «Выбрать» через [data-ticket-card-button] (как сайт)…")
        clicked = pick_flight()
        if clicked and not clicked.get("ok"):
            reason = clicked.get("reason") or "unknown"
            if strict:
                if reason == "strict: time not found":
                    raise NoFlightsAvailable(
                        f"Строгий режим: рейс {prefer_time} не найден "
                        f"(всего рейсов {clicked.get('total', '?')})"
                    )
                if reason == "strict: index not found":
                    raise NoFlightsAvailable(
                        f"Строгий режим: FLIGHT_INDEX={idx_label}, "
                        f"всего рейсов={clicked.get('total', '?')}"
                    )
                if reason == "strict: card not selectable":
                    raise NoFlightsAvailable(
                        f"Строгий режим: позиции [{idx_label}] в списке без «Выбрать» "
                        f"(всего рейсов {clicked.get('total', '?')})"
                    )
        log_picked_flight(clicked)
        if not clicked or not clicked.get("ok"):
            raise NoFlightsAvailable("Нет прямых рейсов для выбора (транзит пропускаем)")
        _remember_pick(clicked)

        # ждём, что TicketsSelect выставил from → панель active
        for _ in range(20):
            if site_selection_ok():
                break
            page.wait_for_timeout(100)
        else:
            print("[tickets] панель не active — повторный клик Выбрать…")
            clicked = pick_flight()
            log_picked_flight(clicked, tag="[tickets] повтор")
            page.wait_for_timeout(400)
            _remember_pick(clicked)

        state = selection_state()
        if state.get("isTransit"):
            raise NoFlightsAvailable("Только транзитные рейсы — не берём")
        if not (state.get("selected") or state.get("otmenit") or site_selection_ok()):
            debug = ROOT / ".debug_no_select.html"
            debug.write_text(page.content(), encoding="utf-8")
            raise RuntimeError(
                "Рейс НЕ выбран сайтом (TicketsSelect.from пуст). "
                f"Продолжать нельзя. HTML: {debug}"
            )
        print(
            f"[ok] рейс выбран: {route_notify_text(flight_time=selected_meta.get('time') or None)}"
        )
        capture_selected()

    # Продолжить — только если выбор подтверждён
    state = selection_state()
    if not (state.get("selected") or state.get("otmenit") or site_selection_ok()):
        raise RuntimeError("Перед «Продолжить» рейс всё ещё не выбран")
    capture_selected()
    print(f"[tickets] для notify: {route_notify_text(flight_time=selected_meta.get('time') or None)}")

    def on_booking() -> bool:
        url = page.url or ""
        if "/flights/booking" in url:
            return True
        try:
            return page.locator("#adult-name-1, [data-autocomplete='first_name']").count() > 0
        except Exception:
            return False

    def click_continue_hard() -> dict:
        """
        Корень бага: #data-ticket-card-submit вызывает form.submit() ТОЛЬКО если
        TicketsSelect.from установлен. Иначе click = no-op.
        Поэтому сами ставим hidden searchId/bookingClassId как setInputs('Ow')
        и делаем form.submit().
        """
        return page.evaluate(
            """() => {
                const form = document.forms['tickets-cards'];
                if (!form) return { ok: false, reason: 'no form' };

                let from = document.querySelector('.ticket-card.selected');
                if (!from) {
                    from = [...document.querySelectorAll('.ticket-card')].find(c =>
                        (c.dataset.searchId || c.getAttribute('data-search-id'))
                        && (c.dataset.bookingClassId || c.getAttribute('data-booking-class-id'))
                        && !/мест\\s*нет|транзит|сквозн|transit|пересадк/i.test(c.innerText || '')
                    ) || null;
                }
                if (!from) return { ok: false, reason: 'no card' };

                const searchId = from.dataset.searchId
                    || from.getAttribute('data-search-id') || '';
                const bookingClassId = from.dataset.bookingClassId
                    || from.getAttribute('data-booking-class-id') || '';
                if (!searchId || !bookingClassId) {
                    return {
                        ok: false,
                        reason: 'no ids',
                        dataset: Object.assign({}, from.dataset),
                    };
                }

                // как TicketsSelect.removeInputs + setInputs('Ow')
                for (const name of ['searchId', 'bookingClassId', 'rtsearchId', 'rtBookingClassId']) {
                    form.querySelectorAll('input[type="hidden"][name=\"' + name + '\"]').forEach(el => el.remove());
                }
                for (const [name, value] of [['searchId', searchId], ['bookingClassId', bookingClassId]]) {
                    const input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = name;
                    input.value = String(value);
                    form.appendChild(input);
                }
                if (form.elements.tripType) {
                    try { form.elements.tripType.value = 'ow'; } catch (e) {}
                }

                const info = document.querySelector('.main-tickets-info');
                if (info) {
                    info.classList.add('active', 'ow');
                    info.classList.remove('rt');
                }
                from.classList.add('selected');

                // клик по Продолжить (на случай если from уже был в TicketsSelect)
                const btn = document.getElementById('data-ticket-card-submit');
                let clicked = false;
                if (btn) {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.classList.remove('button_disabled', 'disabled');
                    try { btn.click(); clicked = true; } catch (e) {}
                }

                // главный путь — прямой submit формы (обходит if (!this.from))
                let submitted = false;
                try {
                    form.submit();
                    submitted = true;
                } catch (e) {
                    return { ok: false, reason: String(e), clicked, searchId, bookingClassId };
                }

                return {
                    ok: true,
                    clicked,
                    submitted,
                    searchId,
                    bookingClassId,
                    action: form.action || '',
                    method: (form.method || 'get').toLowerCase(),
                    infoActive: !!(info && info.classList.contains('active')),
                };
            }"""
        )

    print("[tickets] жму «Продолжить» через form.submit (как TicketsSelect)…")
    for attempt in range(1, 6):
        if on_booking():
            print(f"[ok] booking: {page.url}")
            return capture_selected()

        if not site_selection_ok():
            print("[tickets] info не active перед Продолжить — снова Выбрать…")
            click_vybrat_real(array_indexes)
            page.wait_for_timeout(350)

        # Playwright клик (если TicketsSelect.from уже есть — сработает)
        cont = page.locator("#data-ticket-card-submit")
        if cont.count():
            try:
                cont.first.click(force=True, timeout=2500)
                print(f"[tickets] Playwright «Продолжить» #{attempt}")
            except Exception as exc:  # noqa: BLE001
                print(f"[tickets] Playwright Продолжить fail: {exc}")

        result = click_continue_hard()
        print(f"[tickets] continue hard #{attempt}: {result}")
        if not result.get("ok"):
            print(f"[tickets] submit не готов: {result}")

        # быстрее: ждём URL/форму, не крутим 30×200мс вслепую
        try:
            page.wait_for_url("**/flights/booking**", timeout=2500)
        except PlaywrightTimeout:
            pass
        for _ in range(15):
            if on_booking():
                print(f"[ok] booking: {page.url}")
                return capture_selected()
            page.wait_for_timeout(80)

        page.wait_for_timeout(150)

    if on_booking():
        print(f"[ok] booking: {page.url}")
        return capture_selected()

    debug = ROOT / ".debug_after_select.html"
    debug.write_text(page.content(), encoding="utf-8")
    raise RuntimeError(
        "Не открылся booking после «Продолжить». "
        f"URL={page.url} HTML={debug}"
    )


def main() -> int:
    """Полный сценарий — делегируем book.py (повтор каждую минуту если нет мест)."""
    from book import main as book_main

    return book_main()


if __name__ == "__main__":
    raise SystemExit(main())
