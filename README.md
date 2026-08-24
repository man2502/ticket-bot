# T5 Ticket Watch

Поиск и бронирование билетов Turkmenistan Airlines и Turkmenistan Railway.

Проектом можно пользоваться двумя способами: через **веб-панель** (управление
из браузера, несколько пользователей, живые логи) или **из командной строки**
(каждый бот запускается отдельно своим `run.sh` / `run.cmd`).

## Структура

| Папка | Назначение |
|-------|------------|
| [`web/`](web/README.md) | Веб-панель: пассажиры, процессы, брони, настройки |
| [`airlines_api/`](airlines_api/README.md) | Самолёты — HTTP API приложения T5 |
| [`railway-api/`](railway-api/README.md) | Поезда — api.railway.gov.tm |
| `core/` | инфраструктура, общая для всех ботов |
| [`other/airlines_web/`](other/airlines_web/README.md) | Самолёты через браузер (Playwright) — запасной путь |
| [`other/chapar/`](other/chapar/README.md) | Çapar / Turkmenpost |

Панель управляет только `airlines_api` и `railway-api`. Модули в `other/`
работают из командной строки и в панель не входят.

## Общее в корне

- **`.venv/`** — одно виртуальное окружение для всех модулей
- **`passengers.json`** — пассажиры для запуска из CLI
  (`cp passengers.example.json passengers.json`); панель хранит их в своей базе
- **`requirements.txt`** — зависимости ботов
- **`requirements-web.txt`** — зависимости веб-панели
- **`requirements-dev.txt`** — pytest и линтер
- **`passenger_loader.py`**, **`notify_helper.py`**, **`flight_index.py`** — общие утилиты

### `core/`

| Модуль | Назначение |
|--------|------------|
| `http.py` | HTTP с keep-alive и раздельными таймаутами; различает «запрос не ушёл» и «ответ потерян» |
| `booking_guard.py` | журнал попыток брони — защита от повторной брони |
| `slots.py` | тайминг проверочных слотов с долями секунды |
| `state.py` | атомарное чтение/запись JSON |
| `bootstrap.py` | нужный python из `.venv`, UTF-8 в консоли, мягкая остановка по Ctrl+Break |
| `console.py`, `term.py` | кодировка вывода и палитра |
| `env.py` | чтение переменных окружения |
| `open_file.py` | показ картинки капчи на любой ОС |

Корень репозитория модули ищут **по маркеру** (папке `core/`), а не отсчётом
`..`, — поэтому папки модулей можно переносить, ничего не сломав.

## Установка

```bash
git clone <url-репозитория>
cd ticket

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cp passengers.example.json passengers.json
```

Windows:

```cmd
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy passengers.example.json passengers.json
```

> `.venv` с Mac **не работает** в Windows — создайте окружение заново на каждой ОС.

## Запуск

### Веб-панель

```bash
.venv/bin/python -m pip install -r requirements-web.txt
.venv/bin/python -m web.cli create-admin
./run_web.sh                      # Windows: run_web.cmd
```

Подробности — [`web/README.md`](web/README.md).

### Из командной строки

```bash
cd railway-api && ./run.sh        # Windows: run.cmd
cd airlines_api && ./run.sh
```

Настройка — `check.env` в папке модуля (см. README внутри).

## Тесты

```bash
python run_tests.py               # Windows: run_tests.cmd
python run_tests.py railway-api   # только один модуль
```

Раннер отдаёт управление pytest, если он установлен, иначе поднимает встроенную
мини-замену — тестам не нужен интернет и лишние пакеты. Папки с тестами ищутся
по дереву, а не перечисляются: переименование модуля не должно молча выключать
его тесты.

## Бронь и повторный запуск

Бронирование — единственная операция, которую нельзя «просто повторить»:
потерянный ответ означает, что бронь **могла** быть создана. Каждый бот ведёт
журнал попыток (`railway_bookings.json`, `api_bookings.json`, `web_bookings.json`)
и при неопределённом исходе останавливается с кодом 2, вместо того чтобы
бронировать заново.

Разблокировать — **только после проверки броней у перевозчика**:

```bash
./run.sh --force-rebook
```

В панели то же самое делается в разделе «Брони», с подтверждением.

## Что не попадает в git

| Файл / папка | Действие |
|--------------|----------|
| `.venv/` | создать на каждой ОС отдельно |
| `passengers.json` | `cp` / `copy` из `passengers.example.json` |
| `web/data/` | база панели, ключи шифрования, картинки капч — создаётся сама |
| `*/.env`, `*/.env.local` | `cp` из `*.example` |
| `*_bookings.json`, `*_state.json` | рабочие файлы ботов |

## Безопасность

- Пароли перевозчиков, SMTP и ключ SMS панель держит в базе **зашифрованными**;
  ключ лежит отдельным файлом в `web/data/`. Копия базы без ключа паролей не выдаёт.
- **Не коммитьте** `passengers.json`, `.env.local`, `web/data/`, пароли.
- Оплату на сайте банка выполняйте **вручную** — проект только доводит до ссылки.
