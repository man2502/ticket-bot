@echo off
cd /d "%~dp0"
echo === T5 Airlines Web - ustanovka Windows ===
echo.

where py >nul 2>&1
if %errorlevel%==0 (
    set PY=py -3
    goto :found
)
where python >nul 2>&1
if %errorlevel%==0 (
    set PY=python
    goto :found
)

echo OSHIBKA: Python ne naiden.
echo Ustanovi Python s https://www.python.org/downloads/
echo Vkluchi "Add python.exe to PATH"
pause
exit /b 1

:found
echo Python naiden: %PY%
echo.

if not exist "..\.venv\Scripts\python.exe" (
    echo Sozdayu ..\.venv ...
    cd ..
    %PY% -m venv .venv
    cd airlines_web
    if errorlevel 1 (
        echo OSHIBKA: ne udalos sozdat .venv
        pause
        exit /b 1
    )
)

echo Obnovlyayu pip ...
..\.venv\Scripts\python.exe -m pip install --upgrade pip

echo Stavlju torch ...
..\.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (
    ..\.venv\Scripts\python.exe -m pip install torch
)

echo Stavlju pakety ...
..\.venv\Scripts\python.exe -m pip install -r ..\requirements.txt

if not exist ".env" if exist ".env.example" copy .env.example .env
if not exist ".env.local" if exist ".env.local.example" copy .env.local.example .env.local
if not exist "..\passengers.json" if exist "..\passengers.example.json" copy ..\passengers.example.json ..\passengers.json

echo.
echo === Gotovo ===
echo Zapusk: ..\.venv\Scripts\python.exe book.py
pause
