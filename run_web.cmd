@echo off
rem Запуск веб-панели на Windows.
rem Первый запуск: сначала завести администратора —
rem   .venv\Scripts\python.exe -m web.cli create-admin
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m web %*
exit /b %errorlevel%
