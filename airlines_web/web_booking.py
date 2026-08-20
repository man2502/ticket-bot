"""Защита от повторной брони в браузерном боте.

Почему это отдельный модуль. В `book.py` точка невозврата — клик по
`#booking-form-submit` («Подтвердить»). После него на сервере T5 может
появиться настоящая бронь, и узнать об этом из браузера напрямую нельзя:
единственный признак успеха — редирект на страницу банка, а он может не
случиться по десятку причин, никак не связанных с тем, создалась бронь или нет.

Старое поведение было опасным. Обработчик ошибок в `main()` делал так:

    if booking_lock or _on_booking_page():
        booking_fail_count += 1
        bank_url = step_fill_passenger(page)   # ← заново заполняет и жмёт «Подтвердить»

То есть до трёх повторных submit подряд, а после третьего — сброс и новый цикл
с новым поиском и новой бронью. Каждый такой клик — потенциально отдельная
бронь на тех же пассажиров и отдельный счёт в банке.

Здесь вводится то же разделение, что уже работает в railway и airlines_api:

  ok=True                  ссылка банка получена, бронь точно есть;
  ok=False, unknown=False  до клика не дошло (форма не собралась, кнопки нет) —
                           брони НЕТ, повторять безопасно;
  ok=False, unknown=True   клик был, ссылки нет — бронь МОГЛА быть создана.
                           Повторять submit нельзя; можно только продолжать
                           ждать ссылку на той же странице.

Журнал попыток пишется на диск ДО клика, поэтому переживает падение процесса.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WEB_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.booking_guard import BookingGuard, booking_key
from core.term import red

__all__ = [
    "BOOKINGS_JOURNAL",
    "WebBookingResult",
    "check_booking_guard",
    "compute_web_booking_key",
    "make_guard",
]

# Отдельно от state.json: обычное сохранение состояния не должно иметь
# возможности затереть защиту от повторной брони.
BOOKINGS_JOURNAL = WEB_ROOT / "web_bookings.json"


@dataclass
class WebBookingResult:
    """Итог одной попытки брони через браузер."""

    ok: bool
    bank_url: str = ""
    error: str = ""
    unknown: bool = False
    booking_key: str = ""

    @property
    def certain_failure(self) -> bool:
        """Клика по «Подтвердить» не было — брони нет, повтор безопасен."""
        return not self.ok and not self.unknown


def make_guard(path: Path | str | None = None) -> BookingGuard:
    return BookingGuard(path or BOOKINGS_JOURNAL)


def compute_web_booking_key(
    *,
    depart: str,
    arrival: str,
    flight_date: str,
    flight_time: str,
    passengers: list[dict],
) -> str:
    """
    Один и тот же рейс + пассажиры → один и тот же ключ между запусками.

    Номер попытки, время старта и прочее сюда не входят намеренно: ключ должен
    совпасть после перезапуска процесса, иначе повторную бронь не опознать.
    """
    docs = ",".join(sorted(str(p.get("doc_number") or "") for p in passengers))
    return booking_key(depart, arrival, flight_date, flight_time, docs)


def check_booking_guard(
    guard: BookingGuard,
    *,
    force_rebook: bool,
    account_tag: str = "",
) -> str | None:
    """
    Проверить журнал перед стартом. Возвращает текст причины отказа или None.

    Раньше браузерный бот ничего не помнил между запусками: упал после клика
    «Подтвердить» — при следующем старте спокойно бронировал заново.
    """
    blocker = guard.blocking_attempt()
    if blocker is None:
        return None

    if force_rebook:
        print(red(f"[book] --force-rebook: снимаю блокировку ({blocker.status})"))
        guard.clear_all()
        return None

    tag = f" [{account_tag}]" if account_tag else ""
    lines = [
        f"Автобронь{tag} заблокирована незакрытой попыткой:",
        f"  статус:  {blocker.status}",
        f"  начата:  {blocker.started_at}",
    ]
    if blocker.form_url:
        lines.append(f"  оплата:  {blocker.form_url}")
    if blocker.detail:
        lines.append(f"  детали:  {blocker.detail}")
    lines.append(f"  журнал:  {BOOKINGS_JOURNAL}")
    lines.append("Проверь брони в личном кабинете T5, затем запусти с --force-rebook")
    return "\n".join(lines)
