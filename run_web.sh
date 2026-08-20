#!/bin/bash
# Запуск веб-панели на macOS / Linux.
# Первый запуск: сначала завести администратора —
#   .venv/bin/python -m web.cli create-admin
set -e
cd "$(dirname "$0")"

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" -m web "$@"
