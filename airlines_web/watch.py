#!/usr/bin/env python3
"""
Монитор свободных мест Turkmenistan Airlines (только уведомления, без покупки).

Сайт требует капчу при каждом поиске — скрипт открывает браузер;
капчу вводите вы, дальше он сам ищет и шлёт уведомление, если билеты есть.
"""

from __future__ import annotations

import json
import os
import re
import time

from _bootstrap import WEB_ROOT, reexec_with_project_venv, setup_paths

reexec_with_project_venv()
setup_paths()

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ROOT = WEB_ROOT
STATE_FILE = ROOT / "state.json"
BASE = "https://turkmenistanairlines.tm"
LANG = "ru"

ID_TO_CODE = {
    "5": "ASB",
    "14": "CRZ",
    "3": "BKN",
    "6": "TAZ",
    "15": "MYP",
    "21": "KRW",
    "25": "KEA",
}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_dates() -> list[str]:
    raw = env("DATES", "17.07.2026,18.07.2026")
    return [d.strip() for d in raw.split(",") if d.strip()]


from notify_helper import notify  # noqa: E402


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def to_site_date(date_str: str) -> str:
    """17.07.2026 → 7/17/2026 (как в календаре сайта)."""
    date_str = date_str.strip()
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", date_str):
        return date_str
    m = re.match(r"^(\d{1,2})[.](\d{1,2})[.](\d{4})$", date_str)
    if not m:
        raise ValueError(f"Непонятная дата: {date_str} (нужно ДД.ММ.ГГГГ)")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{month}/{day}/{year}"


def parse_dot_date(date_str: str) -> tuple[int, int, int]:
    site = to_site_date(date_str)
    month, day, year = [int(x) for x in site.split("/")]
    return day, month, year


def normalize_tm_phone(raw: str) -> tuple[str, str]:
    """Возвращает (локальный номер, полный +993…)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("993") and len(digits) > 8:
        digits = digits[3:]
    if digits.startswith("8") and len(digits) == 9:
        digits = digits[1:]
    return digits, f"+993{digits}"


def normalize_port_code(raw: str, fallback: str) -> str:
    value = (raw or fallback).strip().upper()
    return ID_TO_CODE.get(value, value)


def maybe_login(page) -> None:
    phone = env("T5_PHONE")
    password = env("T5_PASSWORD")
    if not phone or not password:
        print("[info] Логин не задан в .env — ищем без входа.")
        return

    local, full = normalize_tm_phone(phone)
    if not local:
        print("[warn] Пустой телефон в .env — пропуск логина.")
        return

    print(f"[info] Вход в личный кабинет… (телефон {full})")
    page.goto(f"{BASE}/{LANG}/auth/login", wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_selector("#telephone", timeout=20000)
        page.wait_for_selector("#password", timeout=5000)
        page.wait_for_selector("#auth-login-submit", timeout=5000)
    except PlaywrightTimeout:
        print("[warn] Форма входа не загрузилась. Войдите вручную (90 сек)…")
        page.wait_for_timeout(90000)
        return

    page.wait_for_timeout(800)

    visible = page.locator("#telephone")
    visible.click()
    visible.fill("")
    visible.type(local, delay=30)

    page.evaluate(
        """([full, local]) => {
            const v = document.getElementById('telephone');
            const h = document.getElementById('telephone-hidden');
            if (v) {
                v.value = local;
                v.dispatchEvent(new Event('input', { bubbles: true }));
            }
            if (h) {
                h.value = full;
                h.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""",
        [full, local],
    )

    page.locator("#password").fill(password)
    page.locator("#password").dispatch_event("input")
    page.locator("#auth-login-submit").click()

    try:
        page.wait_for_function(
            "() => !window.location.pathname.includes('/auth/login')",
            timeout=20000,
        )
    except PlaywrightTimeout:
        pass

    page.wait_for_timeout(1500)
    if "/auth/login" in page.url:
        err_tel = ""
        err_pwd = ""
        if page.locator("#telephone-error").count():
            err_tel = (page.locator("#telephone-error").inner_text() or "").strip()
        if page.locator("#password-error").count():
            err_pwd = (page.locator("#password-error").inner_text() or "").strip()
        details = ", ".join(x for x in [err_tel, err_pwd] if x) or "остались на странице входа"
        print(f"[warn] Логин не прошёл: {details}")
        print("[action] Можете войти вручную (60 сек)…")
        page.wait_for_timeout(60000)
    else:
        print(f"[ok] Вход выполнен, URL: {page.url}")


def captcha_still_open(page) -> bool:
    popup = page.locator("#captchaPopup")
    try:
        if popup.count() == 0:
            return False
        return bool(popup.first.is_visible())
    except Exception:
        return False


def wait_user_captcha(page, timeout_sec: int = 180) -> None:
    popup = page.locator("#captchaPopup")
    if popup.count() == 0:
        return
    try:
        visible = popup.first.is_visible()
    except Exception:  # noqa: BLE001
        visible = False
    if not visible:
        return

    print(
        f"[action] Введите капчу («цифры с картинки») в окне браузера "
        f"и нажмите «Отправить». Жду до {timeout_sec} сек…"
    )
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if not popup.first.is_visible():
                return
        except Exception:  # noqa: BLE001
            return
        if page.locator(".ticket-card, form[name='tickets-cards']").count():
            return
        page.wait_for_timeout(500)


def select_autocomplete_port(page, input_selector: str, query: str, portcode: str) -> None:
    field = page.locator(input_selector)
    field.click()
    field.fill("")
    # Короткий запрос стабильнее полного названия города
    typed = query[:4] if len(query) >= 3 else query
    field.type(typed, delay=60)
    page.wait_for_timeout(800)

    # Пункты автокомплита: "Ashgabat (ASB), Ashgabat TM"
    item = page.locator(".autocomplete__item").filter(has_text=portcode)
    try:
        item.first.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeout as exc:
        # Повтор с полным именем
        field.fill("")
        field.type(query, delay=50)
        page.wait_for_timeout(1000)
        item = page.locator(".autocomplete__item").filter(has_text=portcode)
        try:
            item.first.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeout:
            raise RuntimeError(
                f"Автокомплит не показал «{query}» ({portcode})"
            ) from exc

    item.first.click()
    page.wait_for_timeout(400)

    span_id = (
        "#internal-departure-port-span"
        if "depart" in input_selector
        else "#internal-arrival-port-span"
    )
    span = page.locator(span_id)
    if span.count() == 0 or span.inner_text().strip() != portcode:
        name = "departPort" if "depart" in input_selector else "arrivalPort"
        page.evaluate(
            """([name, code, selector, city, spanId]) => {
                const form = document.forms['internal_ticket_search'];
                if (form && form[name]) form[name].value = code;
                const input = document.querySelector(selector);
                if (input) input.value = city;
                const span = document.getElementById(spanId);
                if (span) span.textContent = code;
            }""",
            [name, portcode, input_selector, query, span_id.lstrip("#")],
        )


def select_depart_date(page, date_str: str) -> None:
    day, _month, _year = parse_dot_date(date_str)
    site_date = to_site_date(date_str)

    date_input = page.locator("#internal-depart-date")
    date_input.click()
    page.wait_for_timeout(700)

    try:
        page.wait_for_selector(".ticket-calendar__day-cell", timeout=12000)
    except PlaywrightTimeout as exc:
        raise RuntimeError("Календарь не открылся") from exc

    cell_sel = f'.ticket-calendar__day-cell[data-date-input="{site_date}"]'
    for _ in range(8):
        cell = page.locator(cell_sel)
        if cell.count():
            classes = cell.first.get_attribute("class") or ""
            if "disabled" in classes:
                raise RuntimeError(f"Дата {date_str} недоступна")
            cell.first.click()
            page.wait_for_timeout(300)
            page.evaluate(
                """(siteDate) => {
                    const f = document.forms['internal_ticket_search'];
                    if (f && f.departDate) f.departDate.value = siteDate;
                }""",
                site_date,
            )
            return

        next_btn = page.locator(
            ".tickets-calendar-column_second .ticket-calendar-column__button, "
            ".ticket-calendar-column__button"
        )
        if next_btn.count() == 0:
            break
        next_btn.last.click()
        page.wait_for_timeout(500)

    # Fallback по номеру дня
    day_cells = page.locator(
        ".ticket-calendar__day-cell:not(.disabled):not(.empty)"
    ).filter(has_text=re.compile(rf"^{day}$"))
    if day_cells.count():
        day_cells.first.click()
        page.wait_for_timeout(200)
        page.evaluate(
            """(siteDate) => {
                const f = document.forms['internal_ticket_search'];
                if (f && f.departDate) f.departDate.value = siteDate;
            }""",
            site_date,
        )
        return

    raise RuntimeError(f"Не удалось выбрать дату {date_str} в календаре")


def fill_search_form(
    page,
    depart_city: str,
    arrival_city: str,
    depart_code: str,
    arrival_code: str,
    date_str: str,
) -> None:
    from browser_helper import save_bandwidth
    from steps import on_home_url, search_form_on_page

    skip_goto = (
        save_bandwidth()
        and search_form_on_page(page)
        and on_home_url(page)
        and "/flights/" not in (page.url or "")
    )
    if skip_goto:
        print("[form] форма уже открыта — без перезагрузки (SAVE_BANDWIDTH)")
    else:
        page.goto(f"{BASE}/{LANG}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(800 if save_bandwidth() else 1500)

    for sel in [
        "button:has-text('Внутренние')",
        "button:has-text('Içerki')",
    ]:
        tab = page.locator(sel)
        if tab.count():
            tab.first.click()
            page.wait_for_timeout(500)
            break

    page.wait_for_selector("#internal-depart-port", timeout=15000)

    print(f"[form] Откуда: {depart_city} ({depart_code})")
    select_autocomplete_port(page, "#internal-depart-port", depart_city, depart_code)

    print(f"[form] Куда: {arrival_city} ({arrival_code})")
    select_autocomplete_port(page, "#internal-arrival-port", arrival_city, arrival_code)

    print(f"[form] Дата: {date_str} → {to_site_date(date_str)}")
    select_depart_date(page, date_str)

    # Страховка: сайт ждёт portcode (ASB/CRZ), не числовой id
    page.evaluate(
        """([dep, arr, siteDate]) => {
            const f = document.forms['internal_ticket_search'];
            if (!f) return;
            if (f.departPort) f.departPort.value = dep;
            if (f.arrivalPort) f.arrivalPort.value = arr;
            if (f.departDate) f.departDate.value = siteDate;
            if (f.tripType) f.tripType.value = 'OW';
            const depSpan = document.getElementById('internal-departure-port-span');
            const arrSpan = document.getElementById('internal-arrival-port-span');
            if (depSpan) depSpan.textContent = dep;
            if (arrSpan) arrSpan.textContent = arr;
        }""",
        [depart_code, arrival_code, to_site_date(date_str)],
    )


def parse_results(page) -> tuple[bool, list[str]]:
    html = page.content()
    cards = page.locator(".ticket-card")
    count = cards.count()
    items: list[str] = []

    for i in range(count):
        text = re.sub(r"\s+", " ", (cards.nth(i).inner_text() or "").strip())
        if text:
            items.append(text[:200])

    no_markers = [
        "нет доступ",
        "нет рейс",
        "билетов нет",
        "рейсов не найдено",
        "no flights",
        "not available",
        "tapylmady",
    ]
    lower = html.lower()
    looks_empty = any(m in lower for m in no_markers)

    if count > 0:
        return True, items
    if looks_empty:
        return False, []
    if "tickets-cards" in html or "/flights/search" in page.url:
        return False, []
    return False, ["(страница без карточек — возможно нужна капча)"]


def check_date(
    page,
    date: str,
    depart_code: str,
    arrival_code: str,
    depart_city: str,
    arrival_city: str,
    *,
    _retry_after_captcha_timeout: bool = True,
) -> tuple[bool, list[str]]:
    print(f"[check] {depart_city} → {arrival_city}, {date}")
    fill_search_form(page, depart_city, arrival_city, depart_code, arrival_code, date)

    submit = page.locator("#internal-ticket-submit")
    if not submit.count():
        raise RuntimeError("Кнопка поиска не найдена")
    submit.first.click()
    page.wait_for_timeout(1200)

    # капча
    popup = page.locator("#captchaPopup")
    try:
        popup.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeout:
        pass

    if captcha_still_open(page):
        from steps import (
            CaptchaImageTimeout,
            cnn_captcha_ready,
            solve_captcha,
            wait_and_restart_from_zero,
        )

        if cnn_captcha_ready():
            try:
                if not solve_captcha(page):
                    wait_user_captcha(page, timeout_sec=180)
            except CaptchaImageTimeout:
                wait_and_restart_from_zero(page)
                maybe_login(page)
                if _retry_after_captcha_timeout:
                    return check_date(
                        page,
                        date,
                        depart_code,
                        arrival_code,
                        depart_city,
                        arrival_city,
                        _retry_after_captcha_timeout=False,
                    )
                raise
        else:
            wait_user_captcha(page, timeout_sec=180)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(1500)
    return parse_results(page)


def main() -> int:
    from env_loader import load_project_env

    load_project_env()

    depart_code = normalize_port_code(
        env("DEPART_PORT_CODE") or env("DEPART_PORT_ID"), "ASB"
    )
    arrival_code = normalize_port_code(
        env("ARRIVAL_PORT_CODE") or env("ARRIVAL_PORT_ID"), "CRZ"
    )
    depart_city = env("DEPART_CITY", "Ashgabat")
    arrival_city = env("ARRIVAL_CITY", "Turkmenabat")
    dates = load_dates()
    interval = int(env("INTERVAL_SEC", "60") or "60")

    print("=== T5 ticket watch (уведомления только) ===")
    print(f"Маршрут: {depart_city} ({depart_code}) → {arrival_city} ({arrival_code})")
    print(f"Даты: {', '.join(dates)}")
    print(f"Интервал: {interval} сек")
    from browser_helper import effective_clear_every_min, save_bandwidth

    if save_bandwidth():
        every = effective_clear_every_min()
        print(
            f"[bandwidth] SAVE_BANDWIDTH=1: блок картинок/шрифтов, "
            f"кэш сохраняем, очистка cookies ≥{every:.0f} мин"
        )
    print()

    state = load_state()

    with sync_playwright() as p:
        from browser_helper import launch_browser

        browser_or_ctx, page, _is_persistent = launch_browser(p)
        maybe_login(page)

        try:
            while True:
                for date in dates:
                    try:
                        available, items = check_date(
                            page,
                            date,
                            depart_code,
                            arrival_code,
                            depart_city,
                            arrival_city,
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"\033[91m[error] {date}: {exc}\033[0m")
                        continue

                    key = f"{depart_code}-{arrival_code}-{date}"
                    prev = state.get(key, {}).get("available")

                    if available:
                        summary = " | ".join(items[:3]) if items else "есть карточки рейсов"
                        print(f"[ok] {date}: ЕСТЬ места — {summary}")
                        if prev is not True:
                            notify(
                                "T5: появились билеты",
                                f"{depart_city} → {arrival_city}, {date}\n{summary}\n"
                                f"Купите сами: {BASE}/{LANG}",
                            )
                        state[key] = {
                            "available": True,
                            "items": items[:5],
                            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    else:
                        print(f"[--] {date}: свободных мест не видно")
                        state[key] = {
                            "available": False,
                            "items": items[:5],
                            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }

                    save_state(state)

                print(f"[sleep] следующая проверка через {interval} сек…\n")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[stop] Остановлено пользователем.")
        finally:
            browser_or_ctx.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
