#!/bin/bash
set -e
cd "$(dirname "$0")"
exec ../.venv/bin/python railway_check.py "$@"
