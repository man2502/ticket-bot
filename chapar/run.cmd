@echo off
rem Запуск монитора Çapar на Windows. Аналог run.sh: run.sh — для macOS/Linux.
rem Интерпретатор берём из общего .venv репозитория; если его нет,
rem падаем на python из PATH (тогда зависимости должны стоять глобально).
setlocal
cd /d "%~dp0"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" chapar_check.py %*
exit /b %errorlevel%
