#!/usr/bin/env python3
"""Turkmenistan Railway — API без браузера (api.railway.gov.tm)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

RAILWAY_ROOT = Path(__file__).resolve().parent
REPO_ROOT = RAILWAY_ROOT.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(RAILWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(RAILWAY_ROOT))

from core.bootstrap import bootstrap

# Переключиться на python из .venv и включить UTF-8 в консоли — до первого print.
bootstrap(__file__)

from core.booking_guard import BookingGuard
from core.slots import (
    is_slot_minute,
    normalize_check_period,
    now_second,
    precise_sleep,
    seconds_until_second,
    slot_anchors,
    slot_minute_stride,
)
from core.state import load_json, save_json
from notify_helper import (
    flush_notifications,
    gmail_configured,
    notifications_delegated,
    notify,
)
from passenger_loader import format_passenger_names, get_passenger_ids
from railway_client import RequestFailed, ResultUnknown, warmup_connection
from railway_env import env, load_railway_env, route_line, to_railway_date
from railway_stations import StationsError, format_station, get_stations
from railway_booking import (
    BOOKINGS_JOURNAL,
    BookingResult,
    auto_book_enabled,
    multimedia_enabled,
    try_auto_book,
)
from railway_seats import format_selected_seats
from railway_term import green, purple, red
from railway_trip_detail import TripDetailError, find_seats_for_trip
from railway_trips import (
    TripsError,
    alternatives_signature,
    available_trips,
    collect_alternatives,
    fetch_trips,
    filter_watched_trips,
    format_alternatives_notice,
    print_trip_catalog,
    railway_pax_counts,
    railway_strict,
    railway_strict_label,
    resolve_booking_trips,
    summarize_trips,
    format_watched_train_log,
    train_index,
    train_run_number,
    train_selector_label,
    train_time,
    wagon_type_ids,
)

STATE_FILE = RAILWAY_ROOT / "railway_state.json"


# Разброс вокруг отметки для второго и следующих слотов минуты. При периоде
# 30 с это даёт прежние :28–:32 — ровная сетка запросов слишком приметна.
SECONDARY_SLOT_SPREAD_SEC = 2

# Насколько секунд опоздание всё ещё считается попаданием в слот.
SLOT_GRACE_SEC = 0.5
# За сколько до слота поднимать TCP+TLS, чтобы в момент запроса не платить handshake.
WARMUP_LEAD_SEC = 1.0


def check_at_second() -> int:
    """Секунда первого окна: RAILWAY_CHECK_AT_SEC, 0–59."""
    try:
        return max(0, min(59, int(env("RAILWAY_CHECK_AT_SEC", "0") or "0")))
    except ValueError:
        return 0


def check_period_sec() -> int:
    """
    Как часто проверять: 10 / 30 / 60 / 120 секунд (RAILWAY_CHECK_PERIOD_SEC).

    Раньше расписание было зашито в код: первый запрос в :00, второй где-то
    между :28 и :32, и панель на него не влияла — хотя галочки в форме
    показывала. Теперь расписание задаётся отсюда, а прежнее поведение —
    это ровно период 30 с.
    """
    return normalize_check_period(env("RAILWAY_CHECK_PERIOD_SEC"))


def slot_seconds() -> list[int]:
    """Отметки текущего расписания: [0, 30] или, например, [7, 17, 27, 37, 47, 57]."""
    return slot_anchors(check_period_sec(), check_at_second())


def first_slot_jitter_sec() -> float:
    """После :00 — случайно +0 / +0.1 / +0.2 сек (как в api_check)."""
    return random.choice((0.0, 0.1, 0.2))


def slot_jitter_sec(index: int) -> float:
    """
    Сдвиг относительно отметки.

    Первый слот круга сдвигается только вперёд и на доли секунды: он гонится
    за моментом открытия продаж, и опоздание там дороже приметности. Остальные
    гуляют в обе стороны на пару секунд — им спешить некуда.
    """
    if index == 0:
        return first_slot_jitter_sec()
    return float(random.randint(-SECONDARY_SLOT_SPREAD_SEC, SECONDARY_SLOT_SPREAD_SEC))


def plan_slot(index: int) -> tuple[str, float, str]:
    """
    План для отметки номер `index`: (action, delay, label).
    action: now | wait | skip

    Секунды считаются с долями. Старый код брал `datetime.now().second` и терял
    микросекунды, из-за чего запрос уходил в среднем на 500 мс позже слота.
    """
    anchors = slot_seconds()
    if index >= len(anchors):
        return "skip", 0.0, ""

    anchor = float(anchors[index])
    spread = 0 if index == 0 else SECONDARY_SLOT_SPREAD_SEC
    current = now_second()

    # Решаем по окну целиком, а не по случайно выбранной точке внутри него.
    # Иначе на :31 бот с выпавшим сдвигом −2 счёл бы окно :28–:32 упущенным,
    # хотя оно ещё открыто, и пропустил бы половину вторых заходов.
    if current > anchor + spread + SLOT_GRACE_SEC:
        return "skip", 0.0, ""

    jitter = slot_jitter_sec(index)
    delay = (anchor + jitter) - current
    if delay <= 0:
        return "now", 0.0, f":{anchor:02.0f} сразу (опоздание {-delay * 1000:.0f} мс)"

    label = f":{anchor:02.0f}"
    if jitter:
        label += f" {jitter:+.1f}с"
    return "wait", delay, label


def plan_wait_next_slot0() -> tuple[float, str]:
    """Сколько ждать до первой отметки следующего круга."""
    period = check_period_sec()
    first_at = float(slot_seconds()[0])
    jitter = first_slot_jitter_sec()
    delay = seconds_until_second(first_at, jitter=jitter)
    if delay < 1.0:
        # Только что стреляли в этот слот — ждём полный круг, а не 0.2с.
        delay += 60.0

    # При периоде реже минуты нерабочие минуты пропускаем, иначе бот стрелял бы
    # каждую минуту вместо каждой второй.
    if slot_minute_stride(period) > 1:
        now = dt.datetime.now()
        while not is_slot_minute(period, now + dt.timedelta(seconds=delay)):
            delay += 60.0

    label = f":{first_at:02.0f} след. круг"
    if jitter:
        label += f" + {jitter:.1f}с"
    return delay, label


def wait_for_planned_slot(action: str, delay: float, label: str) -> None:
    """
    Ждать до слота по монотонным часам и поднять соединение заранее.

    TCP+TLS handshake — это 2-3 RTT. Делать его в момент слота значит дарить
    конкурентам сотни миллисекунд, поэтому прогрев идёт за секунду до запроса.
    """
    if action == "skip":
        return
    if action == "now":
        print(f"[slot] {label}")
        warmup_connection()
        return

    print(f"[slot] жду {label} (≈{delay:.2f}с до запроса)…")
    deadline = time.monotonic() + delay
    if delay > WARMUP_LEAD_SEC:
        precise_sleep(delay - WARMUP_LEAD_SEC)
    warmup_connection()
    precise_sleep(max(0.0, deadline - time.monotonic()))


def attempt_bookings(
    trips_with_seats: list[dict[str, Any]],
    guard: BookingGuard,
    *,
    verbose: bool = True,
) -> BookingResult | None:
    """
    Пройти по рейсам с местами и попытаться забронировать.

    Переход к следующему рейсу разрешён ТОЛЬКО если предыдущая попытка провалилась
    достоверно (сервер отказал, мест нет, payload не собрался). Любая неясность —
    таймаут, необработанное исключение, неразобранный ответ — означает, что бронь
    могла быть создана, и цикл немедленно останавливается.
    """
    last: BookingResult | None = None
    for trip in trips_with_seats:
        try:
            result = try_book_available_trip(trip, guard=guard, verbose=verbose)
        except ResultUnknown as exc:
            result = BookingResult(ok=False, unknown=True, error=f"ответ потерян: {exc}")
        except (TripDetailError, TripsError, RequestFailed) as exc:
            # Всё это происходит ДО POST /v2/bookings — брони точно нет.
            result = BookingResult(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            result = BookingResult(
                ok=False, unknown=True, error=f"{type(exc).__name__}: {exc}"
            )

        last = result
        if result.ok:
            return result
        if result.unknown:
            return result
        print(red(f"[book-railway] не удалось, брони нет: {result.error}"))
    return last


def _stop_on_unknown_booking(result: BookingResult, date_label: str) -> int:
    """Неопределённый результат брони: остановиться и позвать человека."""
    text = (
        f"{route_line(date_label)}\n"
        f"Бронь могла быть создана, но подтверждения нет:\n{result.error}\n\n"
        f"Проверь список броней в приложении Railway ПЕРЕД повторным запуском.\n"
        f"Журнал попыток: {BOOKINGS_JOURNAL}\n"
        f"Снять блокировку после проверки: ./run.sh --force-rebook"
    )
    print(red("[book-railway] СТОП — результат брони неизвестен."))
    notify("Railway: бронь под вопросом", text, wait_gmail=False)
    return 2


def run_planned_checks(
    *,
    slots: tuple[int, ...],
    date_label: str,
    state: dict,
    state_key: str,
    guard: BookingGuard,
) -> int | None:
    """Выполнить отметки текущего круга, затем ждать следующий."""
    for slot in slots:
        action, delay, label = plan_slot(slot)
        if action == "skip":
            continue
        wait_for_planned_slot(action, delay, label)
        started = time.strftime("%H:%M:%S")
        slot_note = f" {slot + 1}" if slot else ""
        print(
            f"[check{slot_note}] {started} {route_line(date_label)}, "
            f"passenger: {format_passenger_names()}"
        )
        try:
            available, items, body = run_check_once()
        except TripsError as exc:
            print(red(f"[error] {exc}"))
            available, items, body = False, [str(exc)], None
        except Exception as exc:  # noqa: BLE001
            print(red(f"[error] {exc}"))
            available, items, body = False, [str(exc)], None

        watched_trips = (body or {}).get("trips") or []
        all_trips = (body or {}).get("all_trips") or []
        wagon_notes, other_trains = collect_alternatives(watched_trips, all_trips)
        alt_notice = format_alternatives_notice(wagon_notes, other_trains)
        alt_sig = alternatives_signature(wagon_notes, other_trains)
        prev = state.get(state_key, {})
        prev_available = prev.get("available")
        prev_alt_sig = prev.get("alt_sig", "")

        if available:
            summary = " | ".join(items[:3]) if items else "есть места"
            print(f"[ok] ЕСТЬ места — {summary}")
            if alt_notice:
                print(f"[alt] {alt_notice.replace(chr(10), ' | ')}")

            # Сначала бронь, потом письмо. Раньше здесь стоял синхронный SMTP,
            # который съедал 1-3 секунды ровно в момент появления мест.
            book_result: BookingResult | None = None
            if auto_book_enabled():
                book_result = attempt_bookings(
                    available_trips((body or {}).get("trips") or []), guard
                )

            if prev_available is not True:
                notify_body = f"{route_line(date_label)}\n{summary}"
                if alt_notice:
                    notify_body = f"{notify_body}\n\n{alt_notice}"
                notify("Railway: есть места", notify_body, wait_gmail=False)

            if book_result is not None and book_result.ok:
                state[state_key] = {
                    "available": True,
                    "booked": True,
                    "booking_key": book_result.booking_key,
                    "booking_number": book_result.booking_number,
                    "form_url": book_result.form_url,
                    "items": items[:5],
                    "alt_sig": alt_sig,
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_state(state)
                print(green("[ok] Бронь создана — монитор остановлен."))
                return 0

            if book_result is not None and book_result.unknown:
                state[state_key] = {
                    "available": True,
                    "booking_unknown": book_result.error,
                    "booking_key": book_result.booking_key,
                    "items": items[:5],
                    "alt_sig": alt_sig,
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_state(state)
                return _stop_on_unknown_booking(book_result, date_label)

            state[state_key] = {
                "available": True,
                "items": items[:5],
                "alt_sig": alt_sig,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_state(state)
            if auto_book_enabled():
                print("[book-railway] бронь не удалась — поиск продолжается")
                continue
            print("[ok] Места найдены — монитор остановлен.")
            return 0

        hint = items[0] if items else "свободных мест нет"
        print(purple(f"[--] {hint}"))
        if alt_notice:
            print(f"[alt] {alt_notice.replace(chr(10), ' | ')}")
            if alt_sig and alt_sig != prev_alt_sig:
                notify(
                    "Railway: альтернативы",
                    f"{route_line(date_label)}\n{hint}\n\n{alt_notice}",
                    wait_gmail=False,
                )
        state[state_key] = {
            "available": available,
            "items": items[:5],
            "alt_sig": alt_sig,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_state(state)

    delay, label = plan_wait_next_slot0()
    print(f"[slot] жду {label} (≈{delay:.1f}с)…")
    time.sleep(delay)
    return None


def load_state() -> dict:
    state = load_json(STATE_FILE, {})
    return state if isinstance(state, dict) else {}


def save_state(state: dict) -> None:
    # Атомарно: раньше truncate+write мог оставить обрезанный файл и потерять
    # флаг «уже забронировано».
    save_json(STATE_FILE, state)


def cmd_stations(*, as_json: bool = False) -> int:
    try:
        stations = get_stations()
    except StationsError as exc:
        print(red(f"[railway] {exc}"))
        return 1

    if as_json:
        print(json.dumps(stations, ensure_ascii=False, indent=2))
        return 0

    print(f"[railway] Станций: {len(stations)}")
    for station in stations:
        print(format_station(station))
    return 0


def cmd_trips(*, as_json: bool = False) -> int:
    try:
        all_trips = fetch_trips()
    except TripsError as exc:
        print(red(f"[railway] {exc}"))
        return 1

    trips, not_found = filter_watched_trips(all_trips)

    if as_json:
        print(json.dumps(trips, ensure_ascii=False, indent=2))
        return 0

    adult, child = railway_pax_counts(verbose=True)
    wagon_part = ""
    configured_wagons = wagon_type_ids()
    if configured_wagons:
        wagon_part = f" | wagon={','.join(str(w) for w in configured_wagons)}"
    selector = train_selector_label()
    train_part = f" | {selector}" if selector else ""
    print(
        f"[railway] {route_line()} | adult={adult} child={child}{wagon_part}{train_part} | "
        f"date={to_railway_date(env('DATE'))}"
    )
    print_trip_catalog(all_trips)
    if not_found:
        print(purple(f"[--] {not_found}"))
        return 0

    action, used_fallback = resolve_booking_trips(trips, all_trips)
    if action:
        available, items = summarize_trips(action)
        if used_fallback:
            print(f"[ok] на выбранном мест нет — запасной рейс ({railway_strict_label()})")
        else:
            print(f"[ok] ЕСТЬ места — {len(items)} поезд(ов)")
        for item in items:
            print(f"  {item}")
        return 0

    available, items = summarize_trips(trips, all_trips=all_trips)
    for item in items:
        print(purple(f"[--] {item}"))
    return 0


def try_book_available_trip(
    trip: dict[str, Any],
    *,
    guard: BookingGuard | None = None,
    verbose: bool = True,
) -> BookingResult:
    """Детали рейса → выбор мест (fallback по WAGON_TYPE_ID) → POST /v2/bookings."""
    trip_id = trip.get("id")
    if trip_id is None or not wagon_type_ids():
        return BookingResult(ok=False, error="нет trip.id или WAGON_TYPE_ID")

    from passenger_loader import get_passengers

    passengers = get_passengers(verbose=False)

    # Всё до этой точки — только GET. Ошибка здесь означает, что POST не был
    # отправлен, то есть брони точно нет и можно пробовать следующий рейс.
    try:
        resolved = find_seats_for_trip(trip_id, len(passengers), verbose=verbose)
    except (TripDetailError, TripsError, RequestFailed) as exc:
        return BookingResult(ok=False, error=f"детали рейса {trip_id}: {exc}")

    if not resolved:
        ids = ",".join(str(w) for w in wagon_type_ids())
        return BookingResult(ok=False, error=f"нет свободных мест (WAGON_TYPE_ID={ids})")

    _wagon_id, journey, seats = resolved
    if verbose:
        print(f"[book-railway] выбраны: {format_selected_seats(seats)}")
    return try_auto_book(
        journey=journey,
        trip_id=trip_id,
        seats=seats,
        wagon_type_id=_wagon_id,
        guard=guard,
        verbose=verbose,
    )


def cmd_seats(*, as_json: bool = False) -> int:
    try:
        all_trips = fetch_trips()
    except TripsError as exc:
        print(red(f"[railway] {exc}"))
        return 1

    trips, not_found = filter_watched_trips(all_trips)
    if not_found:
        print(purple(f"[--] {not_found}"))
        return 1

    with_seats, used_fallback = resolve_booking_trips(trips, all_trips)
    if not with_seats:
        print(purple("[--] мест нет"))
        return 0

    trip = with_seats[0]
    if used_fallback:
        print(f"[railway] на выбранном мест нет — запасной: {format_watched_train_log(trip).strip()}")
    from passenger_loader import get_passengers

    passengers = get_passengers(verbose=False)
    try:
        resolved = find_seats_for_trip(trip["id"], len(passengers), verbose=True)
    except TripDetailError as exc:
        print(red(f"[railway] {exc}"))
        return 1

    if resolved:
        _wagon_id, _journey, seats = resolved
    else:
        seats = None

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "seat_id": s.seat_id,
                        "label": s.seat_label,
                        "level": s.level,
                        "wagon": s.wagon_number,
                        "tier": s.tier,
                        "score": s.score,
                    }
                    for s in (seats or [])
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not seats:
        print(purple("[--] свободных мест нет (или не прошли правила выбора)"))
        return 0

    print(f"[ok] {format_selected_seats(seats)}")
    return 0


def run_check_once(*, verbose: bool = True) -> tuple[bool, list[str], dict[str, Any] | None]:
    all_trips = fetch_trips()
    watched, not_found = filter_watched_trips(all_trips)
    used_fallback = False
    if not_found:
        available, items = False, [not_found]
        trips = watched
    else:
        action, used_fallback = resolve_booking_trips(watched, all_trips)
        if action:
            available, items = summarize_trips(action, all_trips=all_trips)
            if used_fallback:
                first = items[0] if items else "другой рейс"
                items = [f"на выбранном мест нет, беру другой: {first}"] + items[1:]
            trips = action
        else:
            available, items = summarize_trips(watched, all_trips=all_trips)
            trips = watched
    if verbose:
        if not_found:
            print(f"[railway] {route_line()} — {not_found}")
        elif watched:
            train_part = format_watched_train_log(watched[0])
            extra = " → запасной" if used_fallback else ""
            print(f"[railway] {route_line()}{train_part}{extra}")
        else:
            print(f"[railway] {route_line()} — поездов нет")
    return available, items, {"trips": trips, "all_trips": all_trips, "watched": watched}


def run_single_check(date_label: str) -> int:
    """Один цикл: дождаться ближайшего слота, проверить, напечатать результат."""
    for slot in range(len(slot_seconds())):
        action, delay, label = plan_slot(slot)
        if action == "skip":
            continue
        wait_for_planned_slot(action, delay, label)
        break
    else:
        delay, label = plan_wait_next_slot0()
        wait_for_planned_slot("wait", delay, label)

    print(
        f"[check] {time.strftime('%H:%M:%S')} {route_line(date_label)}, "
        f"passenger: {format_passenger_names()}"
    )
    try:
        available, items, _body = run_check_once()
    except (TripsError, RequestFailed) as exc:
        print(red(f"[error] {exc}"))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(red(f"[error] {exc}"))
        return 1

    if available:
        print(f"[ok] ЕСТЬ места — {' | '.join(items[:3])}")
    else:
        print(purple(f"[--] {items[0] if items else 'нет мест'}"))
    return 0


def check_booking_guard(guard: BookingGuard, *, force_rebook: bool) -> int | None:
    """
    Не дать забронировать второй раз после перезапуска.

    Раньше `booked` в state вообще не проверялся: перезапуск после успешной брони
    заводил вторую настоящую бронь на тех же пассажиров.
    """
    blocker = guard.blocking_attempt()
    if blocker is None:
        return None

    if force_rebook:
        print(red(f"[book-railway] --force-rebook: снимаю блокировку ({blocker.status})"))
        guard.clear_all()
        return None

    print(red("[book-railway] автобронь заблокирована незакрытой попыткой:"))
    print(red(f"  статус:  {blocker.status}"))
    print(red(f"  начата:  {blocker.started_at}"))
    if blocker.booking_number:
        print(red(f"  бронь:   {blocker.booking_number}"))
    if blocker.form_url:
        print(red(f"  оплата:  {blocker.form_url}"))
    if blocker.detail:
        print(red(f"  детали:  {blocker.detail}"))
    print(f"  журнал:  {BOOKINGS_JOURNAL}")
    print("Проверь брони в приложении Railway, затем: ./run.sh --force-rebook")
    return 2


def run_watch_loop(*, once: bool = False, force_rebook: bool = False) -> int:
    date_label = env("DATE")
    source = env("SOURCE_STATION_ID")
    destination = env("DESTINATION_STATION_ID")
    configured_wagons = wagon_type_ids()
    wagon_suffix = ""
    if configured_wagons:
        wagon_suffix = "-wagon" + "-".join(str(w) for w in configured_wagons)
    selector = train_selector_label()
    train_suffix = f"-{selector}" if selector else ""
    strict_suffix = "-strict" if railway_strict() else "-flex"
    state_key = f"{source}-{destination}-{date_label}{wagon_suffix}{train_suffix}{strict_suffix}"
    print("=== Railway watch ===")
    print(f"Маршрут: {route_line(date_label)}")
    if configured_wagons:
        if len(configured_wagons) == 1:
            print(f"Вагон: WAGON_TYPE_ID={configured_wagons[0]}")
        else:
            print(
                f"Вагон: WAGON_TYPE_ID={','.join(str(w) for w in configured_wagons)} "
                f"(fallback: сначала {configured_wagons[0]}, потом остальные)"
            )
    idx = train_index()
    time_wanted = train_time()
    run_no = train_run_number()
    if idx is not None:
        print(f"Рейс: TRAIN_INDEX={idx} → [{idx}] | {railway_strict_label()}")
    elif time_wanted:
        print(f"Рейс: TRAIN_TIME={time_wanted} | {railway_strict_label()}")
    elif run_no:
        print(f"Рейс: №{run_no} | {railway_strict_label()}")
    else:
        print(f"Рейс: все | {railway_strict_label()}")
    print(f"Пассажиры: {env('PASSENGER_IDS') or get_passenger_ids()[0]}")
    marks = ", ".join(f":{sec:02d}" for sec in slot_seconds())
    spread = f", остальные ±{SECONDARY_SLOT_SPREAD_SEC}с" if len(slot_seconds()) > 1 else ""
    print(f"Слот: каждые {check_period_sec()}с — {marks} (1-й +0/0.1/0.2с{spread})")
    if notifications_delegated():
        # Запуск из панели: она сама читает журнал броней и шлёт письмо и SMS.
        print("[notify] уведомления рассылает панель")
    else:
        print(f"[notify] {'Gmail настроен' if gmail_configured() else 'только macOS + консоль'}")
    if auto_book_enabled():
        print("[book-railway] RAILWAY_AUTO_BOOK=1 — при местах: выбор мест + бронь + ссылка на оплату")
        print(
            f"[book-railway] мультимедиа Wi‑Fi: "
            f"{'да' if multimedia_enabled() else 'нет'} (RAILWAY_MULTIMEDIA)"
        )
    print()

    if not source or not destination:
        print("[railway] Задай SOURCE_STATION_ID и DESTINATION_STATION_ID в check.env")
        print("[railway] Список станций: ./run.sh --stations")
        return 1

    if once:
        return run_single_check(date_label)

    guard = BookingGuard(BOOKINGS_JOURNAL)
    if auto_book_enabled():
        blocked = check_booking_guard(guard, force_rebook=force_rebook)
        if blocked is not None:
            return blocked

    state = load_state()
    previous = state.get(state_key, {})
    if auto_book_enabled() and previous.get("booked") and not force_rebook:
        print(red("[book-railway] по этому маршруту бронь уже создана:"))
        print(red(f"  {previous.get('booking_number', '?')} — {previous.get('form_url', '')}"))
        print("Повторить осознанно: ./run.sh --force-rebook")
        return 2

    try:
        while True:
            result = run_planned_checks(
                slots=tuple(range(len(slot_seconds()))),
                date_label=date_label,
                state=state,
                state_key=state_key,
                guard=guard,
            )
            if result is not None:
                return result
    except KeyboardInterrupt:
        print("\n[stop] Остановлено пользователем.")
        return 0
    finally:
        flush_notifications()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Turkmenistan Railway — API монитор")
    parser.add_argument("--env", dest="env_path", default="check.env")
    parser.add_argument("--once", action="store_true", help="Один цикл проверки")
    parser.add_argument("--stations", action="store_true", help="GET /v2/stations")
    parser.add_argument("--trips", action="store_true", help="GET /v2/trips (один раз)")
    parser.add_argument("--seats", action="store_true", help="GET /v2/trips/{id} + выбор мест")
    parser.add_argument("--json", action="store_true", help="JSON для --stations / --trips / --seats")
    parser.add_argument(
        "--force-rebook",
        action="store_true",
        help="Снять блокировку автоброни (только после ручной проверки броней!)",
    )
    args = parser.parse_args(argv)

    load_railway_env(args.env_path)

    if args.stations:
        return cmd_stations(as_json=args.json)
    if args.trips:
        return cmd_trips(as_json=args.json)
    if args.seats:
        return cmd_seats(as_json=args.json)

    return run_watch_loop(once=args.once, force_rebook=args.force_rebook)


if __name__ == "__main__":
    raise SystemExit(main())
