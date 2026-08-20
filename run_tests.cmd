@echo off
rem Прогон тестов на Windows. Тот же run_tests.py, что и на macOS/Linux.
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" run_tests.py %*
exit /b %errorlevel%
