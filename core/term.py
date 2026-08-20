"""Цвета вывода в терминал — один набор на весь репозиторий.

Раньше палитра объявлялась в пяти файлах, и оттенки успели разъехаться: один
и тот же «фиолетовый» в railway и в book.py задавался разными кодами, а часть
модулей знала только про два цвета из пяти.

Дополнительно здесь решается то, чего не делала ни одна из копий: если вывод
перенаправлен в файл или в лог (`./run.sh > log.txt`), escape-последовательности
не пишутся. Иначе лог наполняется мусором вида `[92m[ok] ...[0m`.
Отключить принудительно — `NO_COLOR=1`, включить принудительно — `FORCE_COLOR=1`
(обе переменные обрабатывает `core.console.supports_color`).
"""

from __future__ import annotations

from core.console import supports_color

__all__ = [
    "green",
    "red",
    "purple",
    "light_purple",
    "cyan",
    "orange",
    "colorize",
    "strip_ansi",
]

RESET = "\033[0m"
GREEN = "\033[92m"
RED = "\033[91m"
PURPLE = "\033[38;5;135m"
LIGHT_PURPLE = "\033[38;5;183m"
CYAN = "\033[96m"
ORANGE = "\033[38;5;208m"


def colorize(text: str, color: str) -> str:
    """Покрасить, если вывод идёт в терминал. Иначе вернуть текст как есть."""
    if not supports_color():
        return text
    return f"{color}{text}{RESET}"


def green(text: str) -> str:
    """Успех: места найдены, бронь создана, ссылка на оплату."""
    return colorize(text, GREEN)


def red(text: str) -> str:
    """Ошибка, требующая внимания человека."""
    return colorize(text, RED)


def purple(text: str) -> str:
    """Обычный отрицательный результат цикла: мест нет. Не ошибка."""
    return colorize(text, PURPLE)


def light_purple(text: str) -> str:
    """Мягкое предупреждение: выдача зависла, повтор."""
    return colorize(text, LIGHT_PURPLE)


def cyan(text: str) -> str:
    """Подсказки CNN и прочая справочная информация."""
    return colorize(text, CYAN)


def orange(text: str) -> str:
    """Заголовок попытки/цикла."""
    return colorize(text, ORANGE)


def strip_ansi(text: str) -> str:
    """Убрать цвета из строки — нужно перед отправкой текста в письмо."""
    import re

    return re.sub(r"\033\[[0-9;]*m", "", text or "")
