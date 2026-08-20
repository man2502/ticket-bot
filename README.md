# T5 Ticket Watch

Мониторинг и бронирование билетов Turkmenistan Airlines и смежных сервисов.

| Папка | Назначение |
|-------|------------|
| [`airlines_web/`](airlines_web/README.md) | Браузер — сайт turkmenistanairlines.tm |
| [`airlines_api/`](airlines_api/README.md) | HTTP API iOS-приложения T5 |
| [`chapar/`](chapar/README.md) | Çapar / Turkmenpost |
| [`railway-api/`](railway-api/README.md) | Turkmenistan Railway |

## Общее (корень репозитория)

- **`.venv/`** — одно виртуальное окружение для всех модулей
- **`passengers.json`** — пассажиры (`cp passengers.example.json passengers.json`)
- **`requirements.txt`** — зависимости Python
- **`requirements-dev.txt`** — pytest и линтер (для запуска ботов не нужны)
- **`passenger_loader.py`**, **`notify_helper.py`**, **`flight_index.py`** — общие утилиты
- **`core/`** — инфраструктура, одинаковая для всех ботов

### `core/`

| Модуль | Назначение |
|--------|------------|
| `http.py` | HTTP с keep-alive и раздельными таймаутами; различает «запрос не ушёл» и «ответ потерян» |
| `booking_guard.py` | журнал попыток брони — защита от повторной брони |
| `slots.py` | тайминг проверочных слотов с долями секунды |
| `state.py` | атомарное чтение/запись JSON |
| `bootstrap.py` | запуск нужным python из `.venv` + UTF-8 в консоли |
| `console.py`, `term.py` | кодировка вывода и палитра |
| `env.py` | чтение переменных окружения |
| `open_file.py` | показ картинки капчи на любой ОС |

## Тесты

```bash
python run_tests.py
```

Раннер отдаёт управление pytest, если он установлен, иначе поднимает встроенную
мини-замену — тестам не нужен интернет и лишние пакеты. Отдельный модуль:
`python run_tests.py railway-api`. На Windows — `run_tests.cmd`.

## Бронь и повторный запуск

Бронирование — единственная операция, которую нельзя «просто повторить»:
потерянный ответ означает, что бронь **могла** быть создана. Каждый бот ведёт
журнал попыток (`railway_bookings.json`, `api_bookings.json`, `web_bookings.json`)
и при неопределённом исходе останавливается с кодом 2, вместо того чтобы
бронировать заново.

Разблокировать — только после проверки броней вручную:

```bash
./run.sh --force-rebook
```

## Установка

```bash
git clone <url-репозитория>
cd t5-ticket-watch

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

cp passengers.example.json passengers.json
```

Дальше — настройка и запуск в нужной папке (см. README внутри).

### Windows

```cmd
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy passengers.example.json passengers.json
```

> `.venv` с Mac **не работает** в Windows — создайте окружение заново на каждой ОС.

Запуск на Windows — `run.cmd` в папке модуля (аналог `run.sh`).

## Что не попадает в git

| Файл / папка | Действие |
|--------------|----------|
| `.venv/` | создать на каждой ОС отдельно |
| `passengers.json` | `cp` / `copy` из `passengers.example.json` |
| `airlines_web/.env`, `airlines_web/.env.local` | `cp` из `*.example` |
| `airlines_api/.env.local`, `chapar/.env.local`, `railway-api/.env.local` | свой env в каждой папке |

**В git:** `passengers.example.json`, `requirements.txt`, `airlines_web/captcha_model.pt`.

## Безопасность

- **Не коммитьте** `passengers.json`, `.env.local`, пароли
- Оплату на сайте банка выполняйте **вручную**
