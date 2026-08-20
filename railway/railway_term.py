"""ANSI-цвета для вывода Railway в терминал."""

from __future__ import annotations

RESET = "\033[0m"
GREEN = "\033[92m"
RED = "\033[91m"
PURPLE = "\033[38;5;135m"


def green(text: str) -> str:
    return f"{GREEN}{text}{RESET}"


def red(text: str) -> str:
    return f"{RED}{text}{RESET}"


def purple(text: str) -> str:
    return f"{PURPLE}{text}{RESET}"
