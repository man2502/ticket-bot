#!/bin/bash
set -e
cd "$(dirname "$0")"

# Слот acc1 по умолчанию. Второй браузер — run_dual или --account acc2:
#   ../.venv/bin/python book.py --account acc2
exec ../.venv/bin/python book.py --account acc1 "$@"
