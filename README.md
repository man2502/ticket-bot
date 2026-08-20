# T5 Ticket Watch

Мониторинг и бронирование билетов Turkmenistan Airlines и смежных сервисов.

| Папка | Назначение |
|-------|------------|
| [`airlines_web/`](airlines_web/README.md) | Браузер — сайт turkmenistanairlines.tm |
| [`airlines_api/`](airlines_api/README.md) | HTTP API iOS-приложения T5 |
| [`chapar/`](chapar/README.md) | Çapar / Turkmenpost |
| [`railway/`](railway/README.md) | Turkmenistan Railway |

## Общее (корень репозитория)

- **`.venv/`** — одно виртуальное окружение для всех модулей
- **`passengers.json`** — пассажиры (`cp passengers.example.json passengers.json`)
- **`requirements.txt`** — зависимости Python
- **`passenger_loader.py`**, **`notify_helper.py`** — общие утилиты

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

## Что не попадает в git

| Файл / папка | Действие |
|--------------|----------|
| `.venv/` | создать на каждой ОС отдельно |
| `passengers.json` | `cp` / `copy` из `passengers.example.json` |
| `airlines_web/.env`, `airlines_web/.env.local` | `cp` из `*.example` |
| `airlines_api/.env.local`, `chapar/.env.local`, `railway/.env.local` | свой env в каждой папке |

**В git:** `passengers.example.json`, `requirements.txt`, `airlines_web/captcha_model.pt`.

## Безопасность

- **Не коммитьте** `passengers.json`, `.env.local`, пароли
- Оплату на сайте банка выполняйте **вручную**
