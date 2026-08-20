"""Запуск панели: python -m web

Адрес и порт — переменные TICKET_HOST и TICKET_PORT (по умолчанию 127.0.0.1:8000).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.console import enable_utf8_output  # noqa: E402
from web.config import host, port  # noqa: E402


def main() -> int:
    # Логи uvicorn содержат кириллицу — на русской Windows консоль в cp1251
    # без этого роняет вывод с UnicodeEncodeError.
    enable_utf8_output()

    import uvicorn  # noqa: PLC0415 — импорт после настройки консоли

    bind_host, bind_port = host(), port()
    print(f"[web] http://{bind_host}:{bind_port}")
    uvicorn.run("web.app:app", host=bind_host, port=bind_port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
