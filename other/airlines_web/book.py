#!/usr/bin/env python3
"""
ASB → Mary (MYP): поиск → капча (CNN) → выбор рейса → форма пассажира.
Оплату картой скрипт НЕ завершает — только заполняет данные.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import re
import sys
import time
from pathlib import Path

from _bootstrap import WEB_ROOT, reexec_with_project_venv, setup_paths

reexec_with_project_venv()
setup_paths()

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from steps import (
    BASE,
    LANG,
    AccountBlocked,
    CaptchaImageTimeout,
    LoginFailed,
    NoFlightsAvailable,
    SKIP_FLIGHT_RE,
    TicketsSearchTimeout,
    captcha_backoff_pause,
    captcha_still_open,
    click_search,
    cnn_captcha_ready,
    ensure_route_filled,
    env,
    fill_route_now,
    get_route_defaults,
    login_fail_streak_limit,
    login_cooldown_pause,
    normalize_tm_phone,
    note_captcha_success,
    on_home_url,
    on_flights_search_page,
    record_search_attempt,
    route_fields_empty,
    route_notify_text,
    search_form_on_page,
    search_max_rounds,
    solve_captcha,
    step_login,
    user_already_passed_captcha,
    wait_and_restart_from_zero,
    wait_for_tickets_list,
    wait_search_rate_limit,
)
from core.booking_guard import AlreadySettled
from core.term import green, light_purple, orange, purple, red
from notify_helper import flush_notifications, gmail_configured, notify
from passenger_loader import get_passenger_by_id, get_passengers, get_passengers_by_type
from web_booking import (
    BOOKINGS_JOURNAL,
    WebBookingResult,
    check_booking_guard,
    compute_web_booking_key,
    make_guard,
)
from dual_coord import (
    DualShutdown,
    bank_browser_wait_sec,
    check_dual_stop,
    clear_dual_stop,
    dual_sync_enabled,
    peek_dual_stop,
    request_dual_stop,
    stop_file_path,
    wait_interruptible,
)

ROOT = WEB_ROOT

# Палитра общая (core.term). Она же не красит вывод, если он идёт в файл.
_green, _purple, _light_purple = green, purple, light_purple

# Сколько кругов ожидания ссылки банка допускается ПОСЛЕ клика «Подтвердить».
# Это именно ожидание, а не повторные submit: второй клик = вторая бронь.
BOOKING_WAIT_ROUNDS = 3


_red = red


def passenger_notify_label(pax: dict | None = None) -> str:
    """Пассажир(и) для уведомлений."""
    pax_list = get_passengers()
    if pax is not None:
        pid = str(pax.get("id") or "?").strip()
        name = f"{pax.get('first_name', '')} {pax.get('last_name', '')}".strip()
        return f"пассажир №{pid}: {name}"
    if len(pax_list) == 1:
        p = pax_list[0]
        pid = str(p.get("id") or "?").strip()
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        return f"пассажир №{pid}: {name}"
    parts = []
    for p in pax_list:
        pid = str(p.get("id") or "?").strip()
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        parts.append(f"№{pid} ({p.get('type', 'adult')}) {name}")
    return "пассажиры: " + ", ".join(parts)


def flight_selected_summary(
    picked: dict | None,
    flight_date: str | None = None,
    flight_time: str | None = None,
) -> str:
    """Кратко: какой рейс выбран (маршрут, дата, время)."""
    picked = picked or {}
    time_val = (flight_time or picked.get("time") or "").strip()
    return route_notify_text(flight_date, time_val or None)


def notify_flight_selected_body(
    picked: dict | None,
    flight_date: str | None = None,
    flight_time: str | None = None,
    pax: dict | None = None,
) -> str:
    """Текст: выбран рейс + для какого пассажира."""
    if pax is None:
        pax = get_passenger_by_id()
    flight_line = flight_selected_summary(picked, flight_date, flight_time)
    return f"Выбран {flight_line}.\n{passenger_notify_label()}.\nЗаполняю данные пассажира."


def notify_bank_body(
    route_line: str,
    bank_url: str,
    pax: dict | None = None,
) -> str:
    """Текст уведомления со ссылкой банка."""
    if pax is None:
        pax = get_passenger_by_id()
    return (
        f"{route_line}\n"
        f"{passenger_notify_label()}.\n"
        f"Ссылка банка для оплаты:\n\n{bank_url}"
    )


def extract_flight_time(text: str = "") -> str:
    """Достаёт время рейса вида 06:30 из текста карточки."""
    m = re.search(r"\b(\d{1,2}[:.]\d{2})\b", text or "")
    if m:
        return m.group(1).replace(".", ":")
    return (env("FLIGHT_TIME") or "").strip().replace(".", ":")


def step_fill_route(page) -> str:
    """Заполняет маршрут из .env и первую дату из DATES. Возвращает date_str."""
    print("\n=== ШАГ 2: ВЫБОР МАРШРУТА ===")
    return fill_route_now(page, force_home=True)


def route_ready(page) -> bool:
    """Форма есть и откуда/куда/дата не пустые."""
    try:
        if page.locator("#internal-depart-port").count() == 0:
            return False
        return not route_fields_empty(page)
    except Exception:
        return False


def retry_pause(page=None, reason: str = "") -> None:
    """
    Короткая пауза перед новой попыткой при ошибке/капче (без выдачи).
    Если рейсы уже обнаружены, но мест нет — используй wait_until_next_minute.
    """
    try:
        delay = float((os.getenv("BOOK_RETRY_DELAY_SEC") or "0").strip() or "0")
    except ValueError:
        delay = 0.0
    delay = max(0.0, delay)
    if delay <= 0:
        if reason:
            print(f"[retry] {reason} — сразу новая попытка")
        return
    print(f"[retry] {reason} — пауза {delay:.1f}с" if reason else f"[retry] пауза {delay:.1f}с")
    end = time.time() + delay
    account = env("ACCOUNT_NAME") or env("CHROME_PROFILE") or ""
    while time.time() < end:
        if dual_sync_enabled():
            try:
                check_dual_stop(self_source=account)
            except DualShutdown:
                raise
        left = end - time.time()
        chunk = min(0.25, left)
        if page is not None:
            try:
                page.wait_for_timeout(int(chunk * 1000))
            except Exception:
                time.sleep(chunk)
        else:
            time.sleep(chunk)


def ensure_fields_then_search(page, *, wait_if_already_filled: bool = False) -> None:
    """Поля пустые → заполнить; затем сразу искать (без ожидания минуты)."""
    if captcha_still_open(page):
        return

    if "/flights/booking" in (page.url or ""):
        return

    if not route_ready(page):
        print("[route] поля пустые — сразу заполняю")
        ensure_route_filled(page)
        if route_fields_empty(page):
            fill_route_now(page, force_home=False)
    elif on_flights_search_page(page):
        print("[route] на выдаче, форма готова — повторный поиск")
    else:
        print("[route] откуда/куда/дата уже есть")

    if wait_if_already_filled:
        retry_pause(page, "перед повторным поиском")
        if not route_ready(page):
            print("[route] поля сбросились — заполняю снова")
            fill_route_now(page, force_home=True)

    print("[search] жму поиск…")
    wait_search_rate_limit(page)
    click_search(page)
    record_search_attempt()
    try:
        page.locator("#captchaPopup").wait_for(state="visible", timeout=15000)
    except PlaywrightTimeout:
        if not captcha_still_open(page):
            print("[warn] капча после поиска не появилась")


def step_search_and_captcha(page, max_rounds: int | None = None) -> None:
    print("\n=== ШАГ 3: ПОИСК + CAPTCHA ===")
    if max_rounds is None:
        max_rounds = search_max_rounds()
    if cnn_captcha_ready():
        print("[cnn] локальная модель captcha_model.pt")
    else:
        raise RuntimeError(
            "CNN недоступна: нужен captcha_model.pt (см. README)"
        )

    account = env("ACCOUNT_NAME") or env("CHROME_PROFILE") or ""
    for round_n in range(1, max_rounds + 1):
        if dual_sync_enabled():
            check_dual_stop(self_source=account)
        print(f"\n[search] === раунд {round_n}/{max_rounds} ===")

        if not captcha_still_open(page):
            if round_n > 1:
                retry_pause(page, f"раунд {round_n}")
            ensure_fields_then_search(page, wait_if_already_filled=False)
            if not captcha_still_open(page) and "/flights/booking" not in (page.url or ""):
                if not route_ready(page) and not on_flights_search_page(page):
                    continue
                if round_n < max_rounds:
                    print("[search] капча не открылась — сразу жму поиск ещё раз")
                    ensure_fields_then_search(page, wait_if_already_filled=False)
                if not captcha_still_open(page):
                    continue

        try:
            if solve_captcha(page):
                # успех только если реально выдача — иначе не идём в «Выбрать» на главной
                if "/flights/" not in (page.url or "") and not user_already_passed_captcha(page):
                    print("[captcha] solver сказал ok, но /flights ещё нет — жду…")
                    try:
                        page.wait_for_url("**/flights/**", timeout=5000)
                    except PlaywrightTimeout:
                        print(f"[warn] нет /flights после капчи | url={page.url}")
                        print("[warn] капча не прошла — backoff и новый поиск")
                        captcha_backoff_pause(page)
                        ensure_fields_then_search(page, wait_if_already_filled=False)
                        continue
                try:
                    wait_for_tickets_list(
                        page, skip_flight_re=SKIP_FLIGHT_RE, allow_reload=False
                    )
                except NoFlightsAvailable:
                    raise
                except TicketsSearchTimeout as exc:
                    print(f"[warn] капча ok, но выдача не загрузилась: {exc}")
                    if "/flights/search" in (page.url or ""):
                        # Уже на выдаче — повторный клик «Поиск» без refresh
                        ensure_fields_then_search(page, wait_if_already_filled=False)
                        continue
                    ensure_fields_then_search(page, wait_if_already_filled=False)
                    continue
                print(f"[ok] капча + выдача готовы → выбираю рейс | URL={page.url}")
                note_captcha_success()
                return
        except CaptchaImageTimeout:
            wait_and_restart_from_zero(page)
            continue

        print("[warn] капча не прошла — backoff и новый поиск")
        captcha_backoff_pause(page)
        ensure_fields_then_search(page, wait_if_already_filled=False)

    raise RuntimeError("Капча не пройдена после всех раундов")


def step_select_first_flight(page) -> dict:
    """Выбрать → сразу Продолжить. Возвращает {time, text, index, total} выбранного рейса."""
    from steps import step_select_and_continue

    info = step_select_and_continue(page) or {}
    if not info.get("time"):
        info["time"] = extract_flight_time(info.get("text") or "")
    return info


def fill_if_exists(page, selectors: list[str], value: str) -> bool:
    if value is None or value == "":
        return False
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count() == 0:
            continue
        el = loc.first
        try:
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            typ = el.get_attribute("type") or ""
            if tag == "select" or typ == "radio":
                continue
            el.fill(str(value))
            el.dispatch_event("input")
            el.dispatch_event("change")
            return True
        except Exception:
            continue
    return False


def parse_passenger_date(raw: str) -> tuple[int, int, int]:
    """ДД.ММ.ГГГГ или MM/DD/YYYY → (day, month, year)."""
    s = (raw or "").strip()
    m = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$", s)
    if not m:
        raise ValueError(f"Непонятная дата: {raw!r} (нужно ДД.ММ.ГГГГ)")
    a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # если похоже на MM/DD/YYYY (месяц в первой части > 12 не бывает; для dob/exp часто DD.MM)
    if "." in s:
        day, month = a, b
    elif a > 12:
        day, month = a, b
    else:
        # MM/DD/YYYY как пишет сайт после выбора в календаре
        month, day = a, b
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"Некорректная дата: {raw!r}")
    return day, month, year


def to_site_datepicker_value(raw: str) -> str:
    """Сайт пишет DOB как MM/DD/YYYY (CustomDatepicker._ISODateformat)."""
    day, month, year = parse_passenger_date(raw)
    return f"{month:02d}/{day:02d}/{year}"


def set_datepicker_field(page, selectors: list[str], raw_date: str, *, label: str) -> dict:
    """
    Дата через qs-datepicker (клик по календарю), не просто .value=.
    overlay → год → месяц → день.
    """
    if not raw_date:
        return {"ok": False, "reason": "empty"}
    day, month, year = parse_passenger_date(raw_date)
    site_val = f"{month:02d}/{day:02d}/{year}"

    loc = None
    for sel in selectors:
        cand = page.locator(sel)
        if cand.count():
            loc = cand.first
            break
    if not loc:
        return {"ok": False, "reason": "input not found", "label": label}

    print(f"[booking] {label}: выбираю в календаре {day:02d}.{month:02d}.{year} → {site_val}")

    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass

    # открыть календарь: иконка append рядом с полем или сам input
    input_id = page.evaluate(
        """(sels) => {
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el) return el.id || null;
            }
            return null;
        }""",
        selectors,
    )
    opened = False
    open_tries = []
    if input_id:
        open_tries.append(f'label.form__input-append[for="{input_id}"]')
        open_tries.append(f'label[for="{input_id}"]')
    open_tries.extend(selectors)
    for open_sel in open_tries:
        try:
            tip = page.locator(open_sel)
            if tip.count():
                tip.first.click(force=True, timeout=2500)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        page.evaluate(
            """(sels) => {
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el) { el.focus(); el.click(); return; }
                }
            }""",
            selectors,
        )
    page.wait_for_timeout(300)

    # контейнер календаря рядом с полем
    cal = page.locator(".qs-datepicker-container:not(.qs-hidden)")
    if cal.count() == 0:
        cal = page.locator(".qs-datepicker").locator("xpath=ancestor::div[contains(@class,'qs-datepicker-container')]")
    if cal.count() == 0:
        # fallback: записать MM/DD/YYYY напрямую
        page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (!el) return;
                el.value = args.val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }""",
            {"sel": selectors[0], "val": site_val},
        )
        got = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                return el ? el.value : '';
            }""",
            selectors[0],
        )
        return {"ok": bool(got), "value": got, "via": "direct", "label": label}

    # overlay года/месяца
    try:
        cal.first.locator(".qs-month-year, .qs-year").first.click(force=True, timeout=2000)
    except Exception:
        pass
    page.wait_for_timeout(200)

    overlay = page.locator(".qs-overlay:not(.qs-hidden), .qs-datepicker .qs-overlay")
    if overlay.count():
        year_inp = overlay.locator(".qs-overlay-year")
        if year_inp.count():
            try:
                year_inp.first.click(force=True)
                year_inp.first.fill("")
                year_inp.first.fill(str(year))
                year_inp.first.press("Enter")
            except Exception:
                page.evaluate(
                    """(y) => {
                        const inp = document.querySelector('.qs-overlay-year');
                        if (!inp) return;
                        inp.focus();
                        inp.value = String(y);
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter' }));
                    }""",
                    year,
                )
        page.wait_for_timeout(150)
        month_el = overlay.locator(f'.qs-overlay-month[data-month-num="{month - 1}"]')
        if month_el.count():
            try:
                month_el.first.click(force=True)
            except Exception:
                pass
        page.wait_for_timeout(150)
        # Submit только если overlay ещё открыт
        still = page.locator(".qs-overlay:not(.qs-hidden)")
        if still.count():
            submit = still.locator(".qs-submit")
            if submit.count():
                try:
                    page.evaluate(
                        """() => {
                            const s = document.querySelector('.qs-overlay:not(.qs-hidden) .qs-submit');
                            if (s) s.classList.remove('qs-disabled');
                        }"""
                    )
                    submit.first.click(force=True)
                except Exception:
                    pass
        page.wait_for_timeout(250)

    # клик по дню текущего месяца
    clicked_day = page.evaluate(
        """(dayNum) => {
            const root = document.querySelector('.qs-datepicker-container:not(.qs-hidden) .qs-datepicker')
                || document.querySelector('.qs-datepicker');
            if (!root) return false;
            const cells = [...root.querySelectorAll('.qs-num')];
            for (const el of cells) {
                const cls = el.className || '';
                if (/qs-empty|qs-disabled|qs-outside/i.test(cls)) continue;
                if (String(el.textContent || '').trim() !== String(dayNum)) continue;
                el.click();
                return true;
            }
            return false;
        }""",
        day,
    )
    page.wait_for_timeout(200)

    # если день не кликнулся — navigate стрелками как запас (редко)
    if not clicked_day:
        page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (!el) return;
                el.value = args.val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            {"sel": selectors[0], "val": site_val},
        )

    got = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            return el ? (el.value || '') : '';
        }""",
        selectors[0],
    )
    ok = bool(got) and (
        got.replace(" ", "") == site_val
        or got.endswith(str(year))
    )
    print(f"[booking] {label}: поле={got!r} ok={ok} dayClick={clicked_day}")
    return {
        "ok": ok,
        "value": got,
        "expected": site_val,
        "dayClick": clicked_day,
        "label": label,
    }


def _gender_variants(gender: str) -> list[str]:
    if not gender:
        return []
    g = gender.strip().upper()
    if g in ("M", "MALE", "М", "МУЖ"):
        return ["male", "M", "Male", "1", "м", "Мужской"]
    if g in ("F", "FEMALE", "Ж", "ЖЕН"):
        return ["female", "F", "Female", "2", "ж", "Женский"]
    return [gender.strip(), g]


def _pax_field_selectors(index: int, pax_type: str = "adult") -> dict[str, list[str]]:
    prefix = pax_type if pax_type in ("adult", "child", "infant") else "adult"
    i = index
    first = [f"#{prefix}-name-{i}", f'input[name="{prefix}name{i}"]']
    last = [f"#{prefix}-surname-{i}", f'input[name="{prefix}surname{i}"]']
    dob = [f"#{prefix}-dob-{i}", f'input[name="{prefix}dob{i}"]']
    doc = [f"#{prefix}-docser-{i}", f'input[name="{prefix}docser{i}"]']
    exp = [f"#{prefix}-expdt-{i}", f'input[name="{prefix}expdt{i}"]']
    if prefix == "adult" and i == 1:
        first.insert(0, '[data-autocomplete="first_name"]')
        last.insert(0, '[data-autocomplete="last_name"]')
        dob.append('[data-autocomplete="dob"]')
        doc.insert(0, '[data-autocomplete="docser"]')
        doc.append('input[placeholder="Номер паспорта"]')
        exp.insert(0, '[data-autocomplete="expdt"]')
    return {"first": first, "last": last, "dob": dob, "doc": doc, "exp": exp}


def fill_passenger_js(page, data: dict, *, index: int = 1, pax_type: str = "adult") -> dict:
    """Заполняет поля пассажира №index (adult / child / infant)."""
    payload = {**data, "index": index, "paxType": pax_type}
    return page.evaluate(
        """(payload) => {
            const idx = payload.index || 1;
            const prefix = payload.paxType || 'adult';
            const filled = [];
            const setVal = (el, value) => {
                if (!el || value === null || value === undefined || value === '') return false;
                const tag = el.tagName.toLowerCase();
                const type = (el.type || '').toLowerCase();
                if (type === 'checkbox' || type === 'radio') return false;
                if (tag === 'select') {
                    el.value = value;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                el.removeAttribute('readonly');
                el.value = String(value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                return true;
            };

            const one = (...sels) => {
                for (const sel of sels) {
                    if (!sel) continue;
                    const el = document.querySelector(sel);
                    if (el) return el;
                }
                return null;
            };

            const firstEl = one(
                '#' + prefix + '-name-' + idx,
                'input[name="' + prefix + 'name' + idx + '"]',
                prefix === 'adult' && idx === 1 ? '[data-autocomplete="first_name"]' : null
            );
            const lastEl = one(
                '#' + prefix + '-surname-' + idx,
                'input[name="' + prefix + 'surname' + idx + '"]',
                prefix === 'adult' && idx === 1 ? '[data-autocomplete="last_name"]' : null
            );
            const docEl = one(
                '#' + prefix + '-docser-' + idx,
                'input[name="' + prefix + 'docser' + idx + '"]',
                prefix === 'adult' && idx === 1 ? '[data-autocomplete="docser"]' : null
            );

            if (setVal(firstEl, payload.first)) filled.push('first_name');
            if (setVal(lastEl, payload.last)) filled.push('last_name');
            if (setVal(docEl, payload.doc)) filled.push('docser');

            if (prefix === 'adult' && idx === 1) {
                if (setVal(one('#contact-email, input[name="contactEmail"]'), payload.email))
                    filled.push('email');
                if (setVal(one('#contact-telephone, input[name="contact_telephone"]'), payload.phoneLocal))
                    filled.push('phone');
                if (setVal(one('#contact-telephone-hidden, input[name="subscriberNumber"]'), payload.phoneFull || ('+993' + (payload.phoneLocal || ''))))
                    filled.push('phone_hidden');
            }

            const anchor = firstEl || lastEl || docEl;
            const scope = anchor
                ? (anchor.closest('.card-dropdown, .passenger-card, .booking-passenger, section, .row') || document)
                : document;
            const genderVals = payload.genderVariants || [];
            for (const g of genderVals) {
                const radio = scope.querySelector(
                    'input[data-autocomplete="gender"][value="' + g + '"], input[name*="gender" i][value="' + g + '"]'
                ) || document.querySelector(
                    'input[data-autocomplete="gender"][value="' + g + '"], input[name*="gender" i][value="' + g + '"]'
                );
                if (radio) {
                    radio.checked = true;
                    radio.dispatchEvent(new Event('change', { bubbles: true }));
                    try { radio.click(); } catch (e) {}
                    const lab = document.querySelector('label[for="' + radio.id + '"]');
                    if (lab) try { lab.click(); } catch (e) {}
                    filled.push('gender:' + g);
                    break;
                }
            }

            return {
                filled,
                docValue: docEl ? docEl.value : null,
                docSel: !!docEl,
                index: idx,
                paxType: prefix,
            };
        }""",
        payload,
    )


def _fill_passenger_dob(page, dob: str, index: int, *, pax_type: str = "adult") -> None:
    sels = _pax_field_selectors(index, pax_type)["dob"]
    site_dob = to_site_datepicker_value(dob)
    page.evaluate(
        """([sels, val]) => {
            let el = null;
            for (const sel of sels) {
                el = document.querySelector(sel);
                if (el) break;
            }
            if (!el) return;
            el.removeAttribute('readonly');
            el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""",
        [sels, site_dob],
    )
    got_dob = page.evaluate(
        """(sels) => {
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el) return el.value || '';
            }
            return '';
        }""",
        sels,
    )
    label = f"{pax_type}#{index} dob"
    if got_dob.replace(" ", "") != site_dob.replace(" ", ""):
        dob_res = set_datepicker_field(page, sels, dob, label=label)
        print(f"[booking] {label} calendar: {dob_res}")
    else:
        print(f"[booking] {label} быстро: {got_dob!r}")


def _fill_passenger_exp(page, doc_exp: str, index: int, *, pax_type: str = "adult") -> None:
    sels = _pax_field_selectors(index, pax_type)["exp"]
    if not doc_exp or not page.locator(", ".join(sels)).count():
        return
    site_exp = to_site_datepicker_value(doc_exp)
    page.evaluate(
        """([sels, val]) => {
            let el = null;
            for (const sel of sels) {
                el = document.querySelector(sel);
                if (el) break;
            }
            if (!el) return;
            el.removeAttribute('readonly');
            el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [sels, site_exp],
    )
    got_exp = page.evaluate(
        """(sels) => {
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el) return el.value || '';
            }
            return '';
        }""",
        sels,
    )
    label = f"{pax_type}#{index} expdt"
    if got_exp.replace(" ", "") != site_exp.replace(" ", ""):
        exp_res = set_datepicker_field(page, sels, doc_exp, label=label)
        print(f"[booking] {label} calendar: {exp_res}")
    else:
        print(f"[booking] {label} быстро: {got_exp!r}")


def _fill_one_passenger(
    page,
    pax: dict,
    index: int,
    *,
    pax_type: str = "adult",
    with_contact: bool,
) -> None:
    pid = pax.get("id", index)
    first = pax["first_name"]
    last = pax["last_name"]
    dob = pax["dob"]
    doc_number = pax["doc_number"]
    doc_exp = pax["doc_expdt"]
    email = pax["email"]
    phone = pax["phone"] or env("T5_PHONE")
    gender_variants = _gender_variants(pax.get("gender", ""))

    local, full = ("", "")
    if phone:
        local, full = normalize_tm_phone(phone)

    sels = _pax_field_selectors(index, pax_type)
    try:
        page.locator(sels["first"][0]).first.wait_for(state="attached", timeout=5000)
    except PlaywrightTimeout:
        print(f"[warn] поля {pax_type} #{index} (id={pid}) ещё не видны")

    print(
        f"[booking] {pax_type} #{index} id={pid}: {first} {last}, "
        f"паспорт={doc_number!r}"
    )
    filled = fill_passenger_js(
        page,
        {
            "first": first,
            "last": last,
            "doc": doc_number,
            "email": email if with_contact else "",
            "phoneLocal": local if with_contact else "",
            "phoneFull": full if with_contact else "",
            "genderVariants": gender_variants,
        },
        index=index,
        pax_type=pax_type,
    )
    print(f"[booking] JS fill {pax_type}#{index}: {filled}")

    fill_if_exists(page, sels["doc"], doc_number)
    page.evaluate(
        """([sels, doc]) => {
            let el = null;
            for (const sel of sels) {
                el = document.querySelector(sel);
                if (el) break;
            }
            if (!el) return;
            el.value = doc;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [sels["doc"], doc_number],
    )
    fill_if_exists(page, sels["first"], first)
    fill_if_exists(page, sels["last"], last)
    if with_contact:
        fill_if_exists(page, ["#contact-email", 'input[name="contactEmail"]'], email)
        fill_if_exists(page, ["#contact-telephone", 'input[name="contact_telephone"]'], local)

    _fill_passenger_dob(page, dob, index, pax_type=pax_type)
    _fill_passenger_exp(page, doc_exp, index, pax_type=pax_type)


def select_halkbank_payment(page) -> dict:
    """Выбирает radio paymentType=halkbank через label #payment-type-2."""
    # принять confirm-диалоги, если вдруг всплывут
    page.once("dialog", lambda d: d.accept())
    result = page.evaluate(
        """() => {
            const radio = document.getElementById('payment-type-2')
                || document.querySelector('input[name="paymentType"][value="halkbank"]');
            if (!radio) {
                return {
                    ok: false,
                    all: [...document.querySelectorAll('input[name="paymentType"]')].map(r => r.value),
                };
            }
            radio.checked = true;
            radio.dispatchEvent(new Event('input', { bubbles: true }));
            radio.dispatchEvent(new Event('change', { bubbles: true }));
            try { radio.click(); } catch (e) {}
            const lab = document.querySelector('label[for="payment-type-2"]');
            if (lab) try { lab.click(); } catch (e) {}
            return { ok: true, value: radio.value, checked: !!radio.checked };
        }"""
    )
    if not result.get("ok") or not result.get("checked"):
        page.locator('label[for="payment-type-2"], #payment-type-2').first.click(force=True)
        page.evaluate(
            """() => {
                const r = document.querySelector('input[name="paymentType"][value="halkbank"]');
                if (r) r.checked = true;
            }"""
        )
        result = page.evaluate(
            """() => {
                const r = document.querySelector('input[name="paymentType"]:checked');
                return { ok: !!r, value: r && r.value, checked: !!(r && r.value === 'halkbank') };
            }"""
        )
    return result


def check_confirm_accuracy_checkbox(page) -> dict:
    """Галочка #confirmInputData — текст про полноту и точность данных."""
    def _accept(dialog):
        try:
            dialog.accept()
        except Exception:
            pass

    # диалог при клике по галочке (правила возврата)
    page.on("dialog", _accept)

    # клик по label for=confirmInputData (как пользователь)
    lab = page.locator('label[for="confirmInputData"], label.confirmInputData')
    box = page.locator("#confirmInputData")
    try:
        if lab.count():
            lab.first.click(force=True)
        elif box.count():
            box.first.click(force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[booking] click confirm fail: {exc}")

    page.wait_for_timeout(300)
    # гарантируем checked после confirm-диалога
    page.evaluate(
        """() => {
            const box = document.getElementById('confirmInputData');
            if (!box) return;
            box.checked = true;
            box.dispatchEvent(new Event('input', { bubbles: true }));
            box.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
    )
    return page.evaluate(
        """() => {
            const box = document.getElementById('confirmInputData');
            return { ok: !!box, checked: !!(box && box.checked), id: box && box.id };
        }"""
    )


def step_fill_passenger(
    page,
    *,
    guard=None,
    booking_key_value: str = "",
) -> WebBookingResult:
    """
    Заполняет пассажира(ов), жмёт «Подтвердить», возвращает результат брони.

    Раньше возвращалась просто строка URL, и пустая строка означала сразу две
    разные вещи: «форма не собралась, брони нет» и «кнопку нажали, но ссылку не
    увидели». Вызывающий код не мог их различить и повторял submit в обоих
    случаях — см. web_booking.py.
    """
    grouped = get_passengers_by_type()
    n = sum(len(v) for v in grouped.values())
    print(f"\n=== ШАГ 5: заполняю {n} пассажир(а) + Halkbank + галочка ===")

    try:
        page.locator(
            "#adult-name-1, [data-autocomplete='first_name'], #booking-form-submit"
        ).first.wait_for(state="attached", timeout=3000)
    except PlaywrightTimeout:
        print(f"[warn] форма ещё не видна, всё равно пробую | url={page.url}")

    page.evaluate(
        """() => {
            document.querySelectorAll('.card-dropdown.close .ticket-autocomplete-button, .card-dropdown.close .card-header').forEach(el => {
                try { el.click(); } catch (e) {}
            });
        }"""
    )

    fill_plan: list[tuple[str, dict, int]] = []
    for pax_type in ("adult", "child", "infant"):
        for i, pax in enumerate(grouped[pax_type], start=1):
            fill_plan.append((pax_type, pax, i))

    for pax_type, pax, i in fill_plan:
        _fill_one_passenger(
            page,
            pax,
            i,
            pax_type=pax_type,
            with_contact=(pax_type == "adult" and i == 1),
        )

    snap_plan = [
        {"type": t, "index": i}
        for t, _, i in fill_plan
    ]

    page.evaluate("() => { const el = document.getElementById('payment-type-2'); if (el) el.scrollIntoView({block:'center'}); }")
    pay = select_halkbank_payment(page)
    print(f"[booking] оплата Halkbank: {pay}")
    if not (pay.get("ok") and (pay.get("value") == "halkbank" or pay.get("checked"))):
        page.locator('label[for="payment-type-2"]').click(force=True)
        page.evaluate(
            """() => {
                const r = document.getElementById('payment-type-2');
                if (r) { r.checked = true; r.dispatchEvent(new Event('change', { bubbles: true })); }
            }"""
        )

    page.evaluate("() => { const el = document.getElementById('confirmInputData'); if (el) el.scrollIntoView({block:'center'}); }")
    confirm = check_confirm_accuracy_checkbox(page)
    print(f"[booking] галочка подтверждения: {confirm}")
    if not confirm.get("checked"):
        page.locator('label[for="confirmInputData"]').click(force=True)
        page.evaluate(
            """() => {
                const c = document.getElementById('confirmInputData');
                if (c) c.checked = true;
            }"""
        )

    snap = page.evaluate(
        """(plan) => {
            const q = (s) => { const el = document.querySelector(s); return el ? el.value : null; };
            const pay = document.querySelector('input[name="paymentType"]:checked');
            const conf = document.getElementById('confirmInputData');
            const passengers = plan.map(({ type, index }) => ({
                type,
                index,
                first: q('#' + type + '-name-' + index + ', input[name="' + type + 'name' + index + '"]'),
                last: q('#' + type + '-surname-' + index + ', input[name="' + type + 'surname' + index + '"]'),
                dob: q('#' + type + '-dob-' + index + ', input[name="' + type + 'dob' + index + '"]'),
                passport: q('#' + type + '-docser-' + index + ', input[name="' + type + 'docser' + index + '"]'),
            }));
            return {
                passengers,
                email: q('#contact-email'),
                phone: q('#contact-telephone'),
                payment: pay ? pay.value : null,
                confirmChecked: !!(conf && conf.checked),
            };
        }""",
        snap_plan,
    )
    print("[booking] снимок:", snap)
    for p in snap.get("passengers") or []:
        idx = p.get("index")
        ptype = p.get("type", "adult")
        tag = f"{ptype}#{idx}"
        if p.get("passport"):
            print(f"[ok] Паспорт {tag}: {p.get('passport')!r}")
        else:
            print(_red(f"[error] Паспорт {tag} НЕ заполнен"))
        if p.get("dob"):
            print(f"[ok] Дата рождения {tag}: {p.get('dob')!r}")
        else:
            print(_red(f"[error] Дата рождения {tag} НЕ заполнена"))
    if snap.get("payment") == "halkbank":
        print("[ok] Оплата: halkbank")
    else:
        print(_red("[error] Halkbank НЕ выбран"))
    if snap.get("confirmChecked"):
        print("[ok] Галочка подтверждения стоит")
    else:
        print(_red("[error] Галочка подтверждения НЕ стоит"))

    # debug после заполнения
    try:
        (ROOT / ".debug_booking.html").write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001 — дамп для отладки, не критичен
        pass

    return click_podtverdit(page, guard=guard, booking_key_value=booking_key_value)


def _looks_like_bank_url(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return False
    # не страница airline booking
    if "turkmenistanairlines.tm" in low and "/flights/booking" in low:
        return False
    keys = (
        "halkbank",
        "pay.",
        "/pay",
        "payment",
        "acquiring",
        "gateway",
        "mpi",
        "3ds",
        "bank",
        "checkout",
        "egate",
        "processing",
        "redirect",
    )
    return any(k in low for k in keys)


def collect_open_urls(page) -> list[str]:
    urls: list[str] = []
    try:
        for p in page.context.pages:
            u = p.url or ""
            if u and u not in urls:
                urls.append(u)
    except Exception:
        pass
    try:
        u = page.url or ""
        if u and u not in urls:
            urls.append(u)
    except Exception:
        pass
    return urls


def wait_for_bank_url(page, *, timeout_sec: int = 90) -> str:
    """
    Ждёт URL страницы банка (редирект / новая вкладка).
    Возвращает лучший найденный URL или текущий page.url.
    """
    print("[pay] жду страницу банка…")
    start = set(collect_open_urls(page))
    deadline = time.time() + timeout_sec
    last_log = 0.0
    best = ""

    while time.time() < deadline:
        # модалка airline → ещё раз Подтвердить
        try:
            modal = page.locator("#booking-modal, .booking-modal, .modal.active")
            if modal.count() and modal.first.is_visible():
                mbtn = page.locator(
                    "#booking-modal-confirm, "
                    "button:has-text('Подтвердить'), button:has-text('Tassyklamak')"
                )
                if mbtn.count():
                    try:
                        cls = mbtn.first.get_attribute("class") or ""
                        if "disabled" not in cls:
                            print("[booking] жму подтверждение в модалке…")
                            mbtn.first.click(force=True)
                    except Exception:
                        pass
        except Exception:
            pass

        for url in collect_open_urls(page):
            if _looks_like_bank_url(url):
                # если airlines redirect — подождём финальный bank URL ещё чуть
                if "turkmenistanairlines.tm" in url.lower() and "halkbank" not in url.lower():
                    best = url
                    continue
                print(_green(f"[ok] страница банка: {url}"))
                return url
            if url not in start and url.startswith("http"):
                best = best or url

        now = time.time()
        if now - last_log >= 5:
            print(f"[pay] ещё жду банк… urls={collect_open_urls(page)}")
            last_log = now
        page.wait_for_timeout(350)

    # fallback: любой новый URL или текущий
    current = collect_open_urls(page)
    for url in current:
        if url not in start and url.startswith("http"):
            print(f"[pay] новый URL (возможно банк): {url}")
            return url
    final = best or (page.url or "")
    if _looks_like_bank_url(final):
        print(_green(f"[ok] страница банка: {final}"))
    else:
        print(f"[pay] банк явно не распознал, текущий URL: {final}")
    return final


def _save_last_bank_url(bank_url: str) -> None:
    try:
        (ROOT / ".last_bank_url.txt").write_text(bank_url + "\n", encoding="utf-8")
    except OSError:
        pass


def resume_wait_bank_url(page, *, timeout_sec: int = 60) -> str:
    """
    Продолжить ждать ссылку банка БЕЗ повторного «Подтвердить».

    Это единственный безопасный способ восстановиться после того, как клик уже
    состоялся: сама по себе ссылка могла просто задержаться, а второй submit
    завёл бы вторую настоящую бронь.
    """
    print("[booking] клик уже был — жду ссылку банка, повторный submit запрещён")
    bank_url = wait_for_bank_url(page, timeout_sec=timeout_sec)
    if bank_url:
        _save_last_bank_url(bank_url)
    return bank_url


def click_podtverdit(page, *, guard=None, booking_key_value: str = "") -> WebBookingResult:
    """
    Жмёт «Подтвердить» и ждёт URL банка.

    Точка невозврата всего скрипта. До клика ошибки безопасны — брони нет.
    После клика любая неясность означает «бронь могла быть создана», и результат
    помечается unknown: повторять submit нельзя.
    """
    print("\n=== ШАГ 6: жму «Подтвердить» ===")

    # кнопка активна только если paymentType + confirmInputData
    page.evaluate(
        """() => {
            const pay = document.querySelector('input[name="paymentType"][value="halkbank"]');
            if (pay) {
                pay.checked = true;
                pay.dispatchEvent(new Event('change', { bubbles: true }));
            }
            const conf = document.getElementById('confirmInputData');
            if (conf) {
                conf.checked = true;
                conf.dispatchEvent(new Event('change', { bubbles: true }));
            }
            const btn = document.getElementById('booking-form-submit');
            if (btn) {
                btn.classList.remove('button_disabled');
                btn.removeAttribute('disabled');
                btn.disabled = false;
            }
        }"""
    )
    page.wait_for_timeout(300)

    btn = page.locator("#booking-form-submit, button:has-text('Подтвердить')")
    if btn.count() == 0:
        # Кнопки нет — клика не было, брони нет. Достоверная неудача.
        return WebBookingResult(
            ok=False,
            booking_key=booking_key_value,
            error="кнопка «Подтвердить» не найдена",
        )

    try:
        btn.first.scroll_into_view_if_needed(timeout=3000)
    except Exception:  # noqa: BLE001 — прокрутка не обязательна для клика
        pass

    # Запись pending уходит на диск ДО клика: если процесс умрёт сразу после
    # него, следующий запуск увидит незакрытую попытку и не станет бронировать.
    if guard is not None and booking_key_value:
        try:
            guard.begin(booking_key_value)
        except AlreadySettled as exc:
            # Кликать «Подтвердить» поверх существующей брони нельзя: это
            # вторая бронь и второй счёт.
            raise RuntimeError(f"бронь уже была: {exc}") from exc

    print("[booking] кликаю #booking-form-submit «Подтвердить»…")
    try:
        btn.first.click(force=True, timeout=5000)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] playwright click: {exc} — пробую JS")
        page.evaluate(
            """() => {
                const b = document.getElementById('booking-form-submit');
                if (!b) return;
                b.classList.remove('button_disabled');
                b.click();
            }"""
        )

    try:
        bank_url = wait_for_bank_url(page, timeout_sec=90)
    except Exception as exc:  # noqa: BLE001
        # Клик состоялся, ждать не смогли — состояние брони неизвестно.
        detail = f"ожидание банка сорвалось: {type(exc).__name__}: {exc}"
        if guard is not None and booking_key_value:
            guard.mark_unknown(booking_key_value, detail)
        return WebBookingResult(
            ok=False, unknown=True, booking_key=booking_key_value, error=detail
        )

    if bank_url:
        _save_last_bank_url(bank_url)

    if _looks_like_bank_url(bank_url):
        if guard is not None and booking_key_value:
            guard.settle(booking_key_value, ok=True, form_url=bank_url)
        return WebBookingResult(ok=True, bank_url=bank_url, booking_key=booking_key_value)

    detail = f"ссылку банка не получил за 90с | url={bank_url or page.url}"
    if guard is not None and booking_key_value:
        guard.mark_unknown(booking_key_value, detail)
    return WebBookingResult(
        ok=False, unknown=True, bank_url=bank_url, booking_key=booking_key_value, error=detail
    )


def recover_empty_route_immediately(page) -> bool:
    """
    Если откуда/куда/дата пустые (после ошибки капчи / сброса формы) —
    сразу заполнить и вернуть True (= нужно новый поиск без ожидания минуты).
    """
    from browser_helper import save_bandwidth
    from steps import fill_route_now, goto_retry, route_fields_empty

    on_search_form = search_form_on_page(page)
    on_search_page = on_flights_search_page(page)
    on_flights = "/flights/" in (page.url or "")
    if (on_flights and not on_search_page) or not on_search_form:
        if save_bandwidth() and on_search_form and (on_home_url(page) or on_search_page):
            print("[route] форма на месте — без goto (SAVE_BANDWIDTH)")
        else:
            try:
                goto_retry(page, f"{BASE}/{LANG}", timeout_ms=90000, attempts=5, label="route")
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] не открыл главную: {exc}")
                return False

    if route_fields_empty(page) or not search_form_on_page(page):
        print("[route] поля пустые после ошибки — СРАЗУ заполняю откуда/куда/дату и новый поиск")
        try:
            fill_route_now(page, force_home=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] заполнение после ошибки: {exc}")
            return False
        return True

    print("[route] поля на месте — сразу новая попытка")
    return False


def _search_at_second() -> int:
    """Секунда минуты для синхронного поиска (оба аккаунта). По умолчанию 30."""
    try:
        sec = int((os.getenv("BOOK_SEARCH_AT_SEC") or "30").strip() or "30")
    except ValueError:
        sec = 30
    return max(0, min(59, sec))


def _search_slot_jitter_sec() -> float:
    """Случайный сдвиг относительно слота: 0 / +0.1 / +0.2 сек."""
    return random.choice((0.0, 0.1, 0.2))


def prepare_home_route(page) -> str:
    """Форма поиска готова к следующему слоту (главная или /flights/search)."""
    from steps import goto_retry

    if search_form_on_page(page) and route_ready(page):
        where = "выдаче" if on_flights_search_page(page) else "главной"
        print(f"[route] форма на {where} готова — без refresh, жду слот :{_search_at_second():02d}")
        return get_route_defaults()[4]

    if search_form_on_page(page):
        print("[route] форма на странице — заполняю без refresh")
        return fill_route_now(page, force_home=False) or get_route_defaults()[4]

    try:
        goto_retry(page, f"{BASE}/{LANG}", timeout_ms=90000, attempts=5, label="route")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] главная до слота: {exc}")
    if not route_ready(page):
        print("[route] готовлю маршрут до слота…")
        return fill_route_now(page, force_home=False) or get_route_defaults()[4]
    print(f"[route] форма готова до слота :{_search_at_second():02d}")
    return get_route_defaults()[4]


def next_search_slot() -> tuple[dt.datetime, dt.datetime, float]:
    """
    Сейчас, ближайший слот BOOK_SEARCH_AT_SEC и секунд до него (один вызов now).

    Микросекунды учитываются. Раньше `now.replace(second=at_sec, microsecond=0)`
    обнулял их у ЦЕЛИ, но не у `now`, поэтому в 12:00:29.700 расчёт «до :30»
    давал 0.3 с — верно, а в 12:00:30.700 сразу прыгал на следующую минуту,
    хотя до конца слота ещё оставалось время. Ошибка равномерна на [0, 1) с.
    """
    at_sec = _search_at_second()
    now = dt.datetime.now()
    nxt = now.replace(second=at_sec, microsecond=0)
    if now >= nxt:
        nxt += dt.timedelta(minutes=1)
    until = max(0.0, (nxt - now).total_seconds())
    return now, nxt, until


def seconds_until_search_slot() -> float:
    """Секунд до ближайшего слота BOOK_SEARCH_AT_SEC (:00 / :30)."""
    return next_search_slot()[2]


def _sleep_with_page(page, seconds: float) -> None:
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        left = end - time.time()
        if left <= 0:
            break
        chunk = min(0.25, left)
        if page is not None:
            try:
                page.wait_for_timeout(int(chunk * 1000))
            except Exception:
                time.sleep(chunk)
        else:
            time.sleep(chunk)


def prepare_after_no_seats(page, flight_date: str = "") -> str:
    """
    Только после «рейсы обнаружены, мест нет»:
    если до следующего слота > BOOK_PREPARE_IF_LEFT_SEC — пауза BOOK_PREPARE_PRE_WAIT_SEC,
    затем подготовка формы к следующему поиску.
    """
    try:
        threshold = float((os.getenv("BOOK_PREPARE_IF_LEFT_SEC") or "10").strip() or "10")
    except ValueError:
        threshold = 10.0
    try:
        pre_wait = float((os.getenv("BOOK_PREPARE_PRE_WAIT_SEC") or "5").strip() or "5")
    except ValueError:
        pre_wait = 5.0

    at_sec = _search_at_second()
    until = seconds_until_search_slot()

    if until > threshold:
        print(
            f"[route] мест нет | до слота :{at_sec:02d} ещё {until:.1f}с (>{threshold:.0f}с) — "
            f"жду {pre_wait:.0f}с, потом готовлю форму"
        )
        _sleep_with_page(page, pre_wait)
    else:
        print(
            f"[route] мест нет | до слота :{at_sec:02d} {until:.1f}с — "
            "сразу готовлю форму"
        )

    return prepare_home_route(page) or flight_date or get_route_defaults()[4]


def wait_until_next_minute(page=None) -> None:
    """
    Ждёт до BOOK_SEARCH_AT_SEC (acc1 :00 / acc2 :30).
    К моменту возврата форма уже должна быть готова — сразу жми поиск.
    """
    at_sec = _search_at_second()
    now, nxt, until_slot = next_search_slot()

    quick_on = (os.getenv("BOOK_QUICK_RETRY") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    slot_label = f":{at_sec:02d}"
    jitter = 0.0

    if quick_on:
        try:
            threshold = float((os.getenv("BOOK_QUICK_RETRY_IF_LEFT_SEC") or "30").strip() or "30")
        except ValueError:
            threshold = 30.0
        try:
            quick_wait = float((os.getenv("BOOK_QUICK_RETRY_WAIT_SEC") or "5").strip() or "5")
        except ValueError:
            quick_wait = 5.0
        if until_slot > threshold:
            wait_sec = max(0.05, quick_wait)
            print(
                f"[wait] BOOK_QUICK_RETRY=1: до {slot_label} ещё {until_slot:.1f}с "
                f"(> {threshold:.0f}с) — повтор через {wait_sec:.1f}с "
                f"(сейчас {now.strftime('%H:%M:%S')})"
            )
        else:
            jitter = _search_slot_jitter_sec()
            wait_sec = max(0.05, until_slot + jitter)
            jitter_note = (
                f", jitter {jitter:+.1f}с"
                if jitter != 0.0
                else ", ровно в слот"
            )
            print(
                f"[wait] BOOK_QUICK_RETRY=1: до {slot_label} {until_slot:.1f}с "
                f"(≤ {threshold:.0f}с) — жду {nxt.strftime('%H:%M:%S')}{jitter_note}"
            )
    else:
        jitter = _search_slot_jitter_sec()
        wait_sec = max(0.05, until_slot + jitter)
        jitter_note = (
            f" (jitter {jitter:+.1f}с)"
            if jitter != 0.0
            else " (ровно в слот)"
        )
        print(
            f"[wait] форма готова | сейчас {now.strftime('%H:%M:%S')} → "
            f"поиск в {nxt.strftime('%H:%M:%S')} "
            f"(через {wait_sec:.1f} сек){jitter_note} | слот {slot_label}"
        )

    # Ждём по монотонным часам: перевод времени или скачок NTP не должен
    # сдвигать слот. Настенное время используется только для печати.
    end = time.monotonic() + wait_sec
    account = env("ACCOUNT_NAME") or env("CHROME_PROFILE") or ""
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        if dual_sync_enabled():
            # Второй аккаунт мог уже дойти до банка — тогда ждать слот незачем.
            check_dual_stop(self_source=account)
        # ближе к слоту — чаще тикаем, чтобы нажать поиск точнее
        chunk = 0.05 if left < 1.0 else min(0.4, left)
        if page is not None:
            try:
                page.wait_for_timeout(int(chunk * 1000))
            except Exception:  # noqa: BLE001 — страница закрыта, досыпаем сами
                time.sleep(chunk)
        else:
            time.sleep(chunk)

    fired = dt.datetime.now()
    if jitter != 0.0:
        print(
            f"[wait] слот! {fired.strftime('%H:%M:%S.%f')[:-3]} "
            f"(jitter {jitter:+.1f}с) — сразу поиск"
        )
    else:
        print(f"[wait] слот! {fired.strftime('%H:%M:%S')} — сразу поиск")


def recover_to_search_form(page, reason: str, *, force_home: bool = False) -> bool:
    """
    Вернуть браузер к готовой форме поиска после сбоя.

    Один и тот же блок из четырёх шагов (быстрое восстановление полей → пауза →
    goto на главную → заполнение маршрута) был скопирован в `main()` четыре раза,
    с расхождениями: где-то `fill_route_now` вызывался только при пустой форме,
    где-то безусловно, где-то после ошибки навигации ждали 5 секунд, а где-то нет.

    force_home=True — заполнять маршрут даже если форма выглядит готовой
    (после срыва брони странице доверять нельзя).

    Возвращает True, если форма готова к новому поиску.
    """
    from steps import goto_retry

    try:
        recover_empty_route_immediately(page)
    except Exception as exc:  # noqa: BLE001 — быстрый путь, не обязателен
        print(f"[warn] recover: {exc}")

    retry_pause(page, reason)

    try:
        goto_retry(page, f"{BASE}/{LANG}", timeout_ms=90000, attempts=5, label="route")
        if force_home or not route_ready(page):
            fill_route_now(page, force_home=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {reason}: {exc}")
        # Сайт или сеть недоступны — дать им время, следующий круг попробует снова.
        page.wait_for_timeout(5000)
        return False
    return True


def report_bank_url(page, *, bank_url: str, route_line: str, account_tag: str) -> None:
    """
    Финал успешного сценария: письмо со ссылкой оплаты и удержание окна Chrome.

    Письмо не отправляется, если ссылку уже получил второй аккаунт этой же брони
    (DUAL_STOP_ON_BANK) — иначе человек получал два письма про одну бронь.
    """
    stop_info = peek_dual_stop() if dual_sync_enabled() else None
    if stop_info and (stop_info.get("source") or "") != account_tag:
        print(
            f"[dual] {account_tag}: банк уже у {stop_info.get('source')} — "
            "закрываю Chrome без повторного письма"
        )
        return

    if not bank_url:
        return

    if not _looks_like_bank_url(bank_url):
        print(f"[warn] URL не похож на банк: {bank_url}")
        return

    try:
        bank_pax = get_passenger_by_id()
    except Exception:  # noqa: BLE001 — имя в письме приятно, но не обязательно
        bank_pax = None

    bank_msg = notify_bank_body(route_line, bank_url, bank_pax)
    notify(f"T5: страница банка — {route_line}", bank_msg)
    print(_green("\n=== ГОТОВО: URL БАНКА ==="))
    print(_green(bank_msg.replace("\n\n", "\n")))

    if not stop_info:
        request_dual_stop(account_tag, bank_url=bank_url)

    # Окно оставляем открытым: оплату человек делает руками на странице банка.
    hold = bank_browser_wait_sec()
    if hold > 0:
        print(f"[main] Chrome откроется ещё {hold:.0f}с, затем закроется…")
        wait_interruptible(page, hold, self_source=account_tag)
    else:
        print("[main] закрываю Chrome…")


def _report_unknown_booking(
    result: WebBookingResult,
    *,
    route_line: str,
    account_tag: str,
) -> int:
    """
    Остановиться и позвать человека: состояние брони неизвестно.

    Возвращает код 2 — отличается и от успеха (0), и от обычной ошибки (1),
    чтобы обёртки (run_dual, cron) могли отличить «нужна ручная проверка».
    """
    text = "\n".join(
        [
            f"Аккаунт {account_tag}: {route_line}",
            "«Подтвердить» нажали, но ссылку банка получить не удалось.",
            "Бронь МОГЛА быть создана.",
            "",
            result.error or "подробностей нет",
            "",
            "Проверь брони в личном кабинете T5 ПЕРЕД повторным запуском.",
            f"Журнал попыток: {BOOKINGS_JOURNAL}",
            "Снять блокировку после проверки: --force-rebook",
        ]
    )
    print(_red("\n[book] СТОП — результат брони неизвестен."))
    print(_red(text))
    notify("T5: бронь под вопросом", text)
    return 2


def load_book_env() -> Path:
    """
    .env.local → .env (аккаунты) → profiles/* (--profile).
    --account acc1|acc2 ; CLI: --passenger, --dates, --depart, …
    """
    from cli_env import apply_cli_overrides, booking_summary, parse_book_cli

    cli = parse_book_cli()
    from env_loader import load_project_env
    from dual_coord import dual_sync_enabled, ensure_dual_stop_scope

    book_path = load_project_env(
        book_env=cli["env_path"],
        profile=cli["profile"],
    )

    stop_path = ensure_dual_stop_scope(
        profile=cli.get("profile"),
        env_path=cli.get("env_path"),
    )
    if dual_sync_enabled():
        print(f"[dual] stop-файл этой брони: {stop_path.name}")

    apply_cli_overrides(cli["overrides"])

    account = cli["account"] or (os.getenv("ACCOUNT") or "acc1").strip() or "acc1"

    from account_env import apply_account_overlay

    apply_account_overlay(account)

    print(f"[env] бронь: {booking_summary()}")
    # --force-rebook пробрасываем через env: main() читает его при проверке журнала.
    if cli.get("force_rebook"):
        os.environ["BOOK_FORCE_REBOOK"] = "1"
    return book_path


def main() -> int:
    load_book_env()
    dep = env("DEPART_PORT_CODE", "ASB")
    arr = env("ARRIVAL_PORT_CODE", "MYP")
    profile = env("CHROME_PROFILE") or env("ACCOUNT_NAME") or "default"
    slot = _search_at_second()
    print(f"=== T5 BOOK: {dep} → {arr} | profile={profile} | поиск в :{slot:02d} ===")
    channels = []
    if gmail_configured():
        channels.append("Gmail")
    channels.append("macOS")
    print(f"[notify] каналы: {', '.join(channels)}")
    print(f"[slot] BOOK_SEARCH_AT_SEC={slot} → ждёт :{slot:02d} каждой минуты")
    from browser_helper import effective_clear_every_min, save_bandwidth

    if save_bandwidth():
        every = effective_clear_every_min()
        print(
            f"[bandwidth] SAVE_BANDWIDTH=1: блок картинок/шрифтов, "
            f"кэш сохраняем, очистка cookies ≥{every:.0f} мин"
        )
    if not gmail_configured():
        print("[notify] Gmail: GMAIL_USER + GMAIL_APP_PASSWORD + NOTIFY_EMAIL в .env")

    from browser_helper import (
        clear_browser_data,
        clear_due,
        launch_browser,
        wipe_profile_dir,
    )
    from steps import NoFlightsAvailable, TicketsSearchTimeout, route_fields_empty

    def _flag(name: str, default: str = "1") -> bool:
        return env(name, default) not in ("0", "false", "no")

    attempt = 0
    bank_url = ""
    account_tag = env("ACCOUNT_NAME") or env("CHROME_PROFILE") or "acc1"

    # Журнал броней проверяем ДО запуска Chrome: если прошлая попытка осталась
    # незакрытой, бронировать повторно нельзя, и открывать браузер незачем.
    guard = make_guard()
    blocked = check_booking_guard(
        guard,
        force_rebook=_flag("BOOK_FORCE_REBOOK", "0"),
        account_tag=account_tag,
    )
    if blocked:
        print(_red(blocked))
        return 2

    clear_dual_stop()
    if dual_sync_enabled():
        print(
            f"[dual] DUAL_STOP_ON_BANK=1 — при банке закроются acc1+acc2 "
            f"этой брони ({account_tag}, {stop_file_path().name})"
        )

    with sync_playwright() as p:
        # Каждый запуск — как с нового браузера: сносим профиль ДО старта Chrome.
        if _flag("WIPE_PROFILE_ON_START"):
            wipe_profile_dir()
        browser_or_ctx, page, _is_persistent = launch_browser(p)
        try:
            # Подстраховка: даже если профиль не сносили — чистим cookies/cache/storage.
            try:
                clear_browser_data(page)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] очистка на старте: {exc}")

            flight_date = ""
            flight_time = ""
            picked: dict = {}
            # True = сначала подготовить форму, дождаться :00/:30, потом сразу поиск
            align_to_slot = True
            # True = уже на /flights/booking — нельзя refresh, пока не получим ссылку банка
            booking_lock = False
            booking_fail_count = 0
            login_fail_streak = 0
            # Заполняется, если клик «Подтвердить» состоялся, а ссылки банка нет:
            # бронь могла быть создана, и цикл обязан остановиться.
            unknown_booking: WebBookingResult | None = None

            def _handle_login_failure(exc: LoginFailed, *, after_clear: bool = False) -> None:
                nonlocal login_fail_streak, align_to_slot
                login_fail_streak += 1
                tag = " после очистки cookies" if after_clear else ""
                print(_red(f"[login]{tag} {exc} (серия {login_fail_streak})"))
                if login_fail_streak >= login_fail_streak_limit():
                    login_cooldown_pause(page)
                    login_fail_streak = 0
                try:
                    step_login(page)
                    login_fail_streak = 0
                except AccountBlocked:
                    raise
                except LoginFailed as retry_exc:
                    raise LoginFailed(str(retry_exc)) from retry_exc
                align_to_slot = True

            def _login_step(*, after_clear: bool = False) -> str:
                """
                Логин с повтором. Возвращает ok | blocked | failed.

                Один и тот же блок из шести строк (печать, письмо, выход) был
                скопирован в трёх местах по два раза каждый — на первичный вход,
                на вход после очистки cookies и на LoginFailed в цикле. Различия
                между копиями были только в тексте и в том, что делать дальше;
                решение «дальше» осталось за вызывающим, всё остальное здесь.
                """
                try:
                    step_login(page)
                    return "ok"
                except AccountBlocked as exc:
                    print(_red(f"[login] блокировка: {exc}"))
                    notify("T5: аккаунт заблокирован", f"Аккаунт {account_tag}:\n{exc}")
                    return "blocked"
                except LoginFailed as exc:
                    # Python удаляет имя exc при выходе из except — сохраняем сами.
                    first_failure = exc

                # Первая попытка не прошла — пауза по серии неудач и второй заход.
                try:
                    _handle_login_failure(first_failure, after_clear=after_clear)
                    return "ok"
                except AccountBlocked as block_exc:
                    print(_red(f"[login] блокировка: {block_exc}"))
                    notify("T5: аккаунт заблокирован", f"Аккаунт {account_tag}:\n{block_exc}")
                    return "blocked"
                except LoginFailed as exc2:
                    tag = " после очистки" if after_clear else ""
                    print(_red(f"[login] не удалось войти{tag}: {exc2}"))
                    notify("T5: ошибка входа", f"Аккаунт {account_tag}:\n{exc2}")
                    return "failed"

            first_login = _login_step()
            if first_login != "ok":
                # Без входа делать нечего. Код 1 — раньше здесь был голый return,
                # то есть процесс завершался с кодом 0 «успех» при блокировке.
                return 1
            login_fail_streak = 0

            last_browser_clear = time.time()

            def _on_booking_page() -> bool:
                url = page.url or ""
                if "/flights/booking" in url:
                    return True
                try:
                    return page.locator("#adult-name-1, #booking-form-submit").count() > 0
                except Exception:
                    return False

            def _must_refresh_now(err: str) -> bool:
                """Ошибки на выдаче без кнопки «Выбрать» — сразу refresh."""
                low = (err or "").lower()
                return any(
                    x in low
                    for x in (
                        "рейсы с кнопкой",
                        "выбрать» не появились",
                        "выбрать\" не появились",
                        "не появились",
                        "нет прямых рейсов для выбора",
                        "ticketsselect",
                        "рейс не выбран",
                    )
                )

            def _booking_key(date_str: str, time_str: str) -> str:
                """Ключ брони для журнала: рейс + пассажиры, стабильный между запусками."""
                try:
                    passengers = get_passengers(verbose=False)
                except Exception:  # noqa: BLE001 — без списка ключ всё равно нужен
                    passengers = []
                return compute_web_booking_key(
                    depart=dep,
                    arrival=arr,
                    flight_date=date_str or "",
                    flight_time=time_str or "",
                    passengers=passengers,
                )

            while True:
                attempt += 1
                print("\n" + orange(f"======== ПОПЫТКА #{attempt} ========"))

                if dual_sync_enabled():
                    try:
                        check_dual_stop(self_source=account_tag)
                    except DualShutdown as ds:
                        print(
                            f"[dual] {account_tag}: другой аккаунт получил банк "
                            f"({ds.info.get('source')}) — закрываю Chrome"
                        )
                        break

                # раз в N минут — новая сессия ТОЛЬКО если ещё не на booking
                if not booking_lock:
                    due, last_browser_clear = clear_due(last_browser_clear)
                    if due:
                        try:
                            clear_browser_data(page)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[warn] очистка сессии: {exc}")
                        print("[browser] новая сессия — снова логин и маршрут")
                        status = _login_step(after_clear=True)
                        if status == "blocked":
                            break
                        if status == "failed":
                            align_to_slot = True
                            continue
                        login_fail_streak = 0
                        try:
                            page.goto(f"{BASE}/{LANG}", wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            pass
                        flight_date = step_fill_route(page) or flight_date
                else:
                    print("[browser] на /flights/booking — очистку/refresh пропускаю")

                try:
                    if not booking_lock:
                        # Подготовка ДО слота: маршрут должен быть готов до :00 / :30
                        if route_fields_empty(page) or page.locator("#internal-depart-port").count() == 0:
                            print("[main] заполняю маршрут до слота…")
                            flight_date = step_fill_route(page) or flight_date
                        else:
                            print("[main] маршрут уже готов")
                            if not flight_date:
                                flight_date = get_route_defaults()[4]

                        if align_to_slot:
                            print(
                                f"[main] жду слот :{_search_at_second():02d} — "
                                "в этот момент сразу жму поиск"
                            )
                            wait_until_next_minute(page)
                            align_to_slot = False

                        print("[main] поиск + капча…")
                        step_search_and_captcha(page)

                        print("[main] выбор рейса…")
                        picked = step_select_first_flight(page) or {}
                        flight_time = (picked.get("time") or "").strip()

                    # На booking — lock; иначе обычный fill (после успешного выбора)
                    if _on_booking_page():
                        if not booking_lock:
                            booking_lock = True
                            booking_fail_count = 0
                            print(
                                "[main] на /flights/booking — refresh запрещён до ссылки банка"
                            )

                    route_line = route_notify_text(flight_date, flight_time)
                    try:
                        current_pax = get_passenger_by_id()
                    except Exception:  # noqa: BLE001 — имя в письме не критично
                        current_pax = None
                    fill_msg = notify_flight_selected_body(
                        picked or {}, flight_date, flight_time, current_pax
                    )
                    print(f"[main] {fill_msg.replace(chr(10), ' | ')}")
                    notify(f"T5: есть рейс — {route_line}", fill_msg)

                    book_result = step_fill_passenger(
                        page,
                        guard=guard,
                        booking_key_value=_booking_key(flight_date, flight_time),
                    )
                    if _on_booking_page():
                        booking_lock = True

                    if book_result.ok:
                        bank_url = book_result.bank_url
                        break
                    if book_result.unknown:
                        # Клик по «Подтвердить» состоялся. Второй submit создаст
                        # вторую настоящую бронь — останавливаемся и зовём человека.
                        unknown_booking = book_result
                        break
                    # Клика не было — обычная ошибка цикла, можно пробовать снова.
                    raise RuntimeError(book_result.error)

                except DualShutdown as ds:
                    print(
                        f"[dual] {account_tag}: остановка — другой аккаунт завершил "
                        f"({ds.info.get('source')})"
                    )
                    break

                except AccountBlocked as exc:
                    print(_red(f"[login] блокировка: {exc}"))
                    notify("T5: аккаунт заблокирован", f"Аккаунт {account_tag}:\n{exc}")
                    break

                except LoginFailed:
                    # Сессия отвалилась посреди цикла — перелогиниться и начать заново.
                    if _login_step() == "blocked":
                        break
                    align_to_slot = True
                    continue

                except NoFlightsAvailable as exc:
                    booking_lock = False
                    booking_fail_count = 0
                    print(_purple(f"[tickets] {exc}"))
                    print("[main] мест нет — готовлю форму до следующего слота…")
                    flight_date = prepare_after_no_seats(page, flight_date)
                    align_to_slot = True
                    continue

                except TicketsSearchTimeout as exc:
                    booking_lock = False
                    booking_fail_count = 0
                    print(_light_purple(f"[tickets] {exc}"))
                    print("[main] выдача зависла — refresh и новый поиск…")
                    align_to_slot = False
                    recover_to_search_form(page, "после зависшей выдачи")
                    continue

                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    print(_red(f"[error] {msg}"), file=sys.stderr)

                    # На /flights/booking — НЕ refresh и НЕ повторный submit.
                    #
                    # Раньше здесь вызывался step_fill_passenger() ещё до трёх раз,
                    # а потом цикл шёл на новый поиск и новую бронь. Каждый такой
                    # вызов заново жал «Подтвердить», то есть заводил отдельную
                    # настоящую бронь на тех же пассажиров. Теперь допустимо
                    # только продолжать ждать ссылку на той же странице.
                    if booking_lock or _on_booking_page():
                        booking_lock = True
                        booking_fail_count += 1
                        print(
                            f"[main] на booking: ошибка #{booking_fail_count} — "
                            "жду ссылку банка без повторного «Подтвердить»"
                        )
                        try:
                            late_url = resume_wait_bank_url(page, timeout_sec=60)
                        except Exception as retry_exc:  # noqa: BLE001
                            print(_red(f"[error] ожидание банка: {retry_exc}"), file=sys.stderr)
                            late_url = ""

                        if late_url and _looks_like_bank_url(late_url):
                            bank_url = late_url
                            key = _booking_key(flight_date, flight_time)
                            guard.settle(key, ok=True, form_url=bank_url)
                            break

                        if booking_fail_count >= BOOKING_WAIT_ROUNDS:
                            # Клик был, ссылки нет. Бронь могла быть создана —
                            # искать заново нельзя, это второй счёт в банке.
                            key = _booking_key(flight_date, flight_time)
                            guard.mark_unknown(
                                key,
                                f"ссылку банка не получили за {BOOKING_WAIT_ROUNDS} кругов: {msg}",
                            )
                            unknown_booking = WebBookingResult(
                                ok=False,
                                unknown=True,
                                booking_key=key,
                                error=f"ссылку банка не получили: {msg}",
                            )
                            break
                        page.wait_for_timeout(1500)
                        continue

                    # Нет кнопки «Выбрать» / выдача пустая — refresh
                    if _must_refresh_now(msg):
                        booking_lock = False
                        booking_fail_count = 0
                        align_to_slot = False
                        cards_count = 0
                        try:
                            cards_count = page.locator(".ticket-card").count()
                        except Exception:
                            pass
                        appeared = False
                        if cards_count > 0:
                            # рейсы есть, но «Выбрать» ещё нет — короткая пауза
                            print("[main] нет «Выбрать» — жду 10с, потом refresh…")
                            try:
                                page.locator(
                                    '[data-ticket-card-button], button:has-text("Выбрать")'
                                ).first.wait_for(state="visible", timeout=10000)
                                appeared = True
                            except Exception:
                                pass
                            if appeared:
                                print("[main] «Выбрать» появилась за 10с — выбираю без refresh")
                                late_result: WebBookingResult | None = None
                                try:
                                    picked = step_select_first_flight(page) or {}
                                    flight_time = (picked.get("time") or "").strip() or flight_time
                                    if _on_booking_page():
                                        booking_lock = True
                                    late_result = step_fill_passenger(
                                        page,
                                        guard=guard,
                                        booking_key_value=_booking_key(flight_date, flight_time),
                                    )
                                except Exception as late_exc:  # noqa: BLE001
                                    print(_red(f"[error] поздний выбор: {late_exc}"), file=sys.stderr)

                                if late_result is not None:
                                    if late_result.ok:
                                        bank_url = late_result.bank_url
                                        break
                                    if late_result.unknown:
                                        # Клик был — новый поиск завёл бы вторую бронь.
                                        unknown_booking = late_result
                                        break
                            print("[main] за 10с не удалось взять рейс — делаю refresh и новый поиск")
                        else:
                            print("[main] пустая выдача после 16с — сразу refresh…")
                        recover_to_search_form(page, "после пустой выдачи")
                        continue

                    # Прочие ошибки до booking — сразу заново
                    align_to_slot = False
                    booking_lock = False
                    recover_to_search_form(page, "после ошибки")
                    print("[main] сразу новая попытка поиска")
                    continue

            if unknown_booking is not None:
                # «Подтвердить» нажали, ссылки банка нет. Бронь могла появиться —
                # молча начинать новый цикл нельзя, это второй счёт в банке.
                return _report_unknown_booking(
                    unknown_booking,
                    route_line=route_notify_text(flight_date, flight_time),
                    account_tag=account_tag,
                )

            report_bank_url(
                page,
                bank_url=bank_url,
                route_line=route_notify_text(flight_date, flight_time),
                account_tag=account_tag,
            )
        except DualShutdown as ds:
            print(
                f"[dual] {account_tag}: остановка — другой аккаунт завершил "
                f"({ds.info.get('source')})"
            )
        except KeyboardInterrupt:
            print("\n[stop]")
            return 130
        except Exception as exc:  # noqa: BLE001
            print(_red(f"[error] {exc}"), file=sys.stderr)
            try:
                page.wait_for_timeout(120000)
            except Exception:
                pass
            return 1
        finally:
            browser_or_ctx.close()
    return 0


if __name__ == "__main__":
    try:
        _code = main()
    finally:
        # Письма уходят в фоновом демон-потоке. Без этого ожидания процесс
        # завершался раньше SMTP, и письмо со ссылкой банка молча терялось —
        # ровно то письмо, ради которого всё и запускалось.
        flush_notifications()
    raise SystemExit(_code)
