@echo off
rem Запуск браузерного бота T5 на Windows. Аналог run.sh: run.sh — для macOS/Linux.
rem Интерпретатор берём из общего .venv репозитория; если его нет,
rem падаем на python из PATH (тогда зависимости должны стоять глобально).
setlocal
cd /d "%~dp0"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" book.py --account acc1 %*
exit /b %errorlevel%
