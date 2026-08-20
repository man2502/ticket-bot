"""Вывод в консоль без падений на не-UTF-8 системах.

Проблема, которую решает модуль. На Windows стандартный поток вывода Python
берёт кодировку из системной локали — в русской это cp1251. Наши логи полны
символов, которых в cp1251 нет: `→` в маршруте, `≈` в таймингах, `•` в списке
альтернатив, `✓`/`✗` в отчёте по капче. Первый же такой символ роняет процесс:

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u2192'

Причём падает он в `print`, то есть монитор умирает не на сетевой ошибке, а на
собственном логе — и именно в тот момент, когда печатает найденный рейс.

Второй эффект: ANSI-цвета. Старые консоли Windows их не понимают и выводят
`[92m` текстом. Здесь же включаем обработку escape-последовательностей, если
терминал это умеет.

Функция идемпотентна — вызывать можно из любого числа точек входа.
"""

from __future__ import annotations

import os
import sys

__all__ = ["enable_utf8_output", "supports_color"]

_applied = False


def _reconfigure(stream: object) -> None:
    """Перевести поток на UTF-8, не ломая его, если он этого не умеет."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        # errors="replace": даже если поток окажется не UTF-8 (перенаправление
        # в файл с чужой кодировкой), лог должен продолжаться, а не падать.
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass


def _enable_windows_ansi() -> None:
    """ENABLE_VIRTUAL_TERMINAL_PROCESSING — иначе цвета печатаются как мусор."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 — косметика, не повод падать
        pass


def enable_utf8_output() -> None:
    """Вызывать первой строкой в точке входа, до любого print."""
    global _applied
    if _applied:
        return
    _applied = True

    _reconfigure(sys.stdout)
    _reconfigure(sys.stderr)
    _enable_windows_ansi()


def supports_color() -> bool:
    """Красить ли вывод: не красим при перенаправлении в файл и при NO_COLOR."""
    if os.getenv("NO_COLOR"):
        return False
    if (os.getenv("FORCE_COLOR") or "").strip() not in ("", "0"):
        return True
    stream = sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())
