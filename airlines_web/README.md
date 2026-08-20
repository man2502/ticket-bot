# T5 Ticket Watch — Web (браузер)

Автоматизация поиска и бронирования билетов на [Turkmenistan Airlines](https://turkmenistanairlines.tm): логин, капча, выбор рейса, заполнение пассажира. Оплату картой скрипт **не завершает** — только доводит до ссылки банка.

**Общие с корнем репозитория** (`../`):
- `.venv/` — одно виртуальное окружение
- `passengers.json` — пассажиры
- `requirements.txt` — зависимости Python

**Env web** (в этой папке `airlines_web/`):
- `.env` — маршрут, аккаунт T5
- `.env.local` — браузер, Gmail, капча

## Что умеет

| Скрипт | Назначение |
|--------|------------|
| `book.py` | Поиск → капча → выбор рейса → форма пассажира → ссылка банка |
| `watch.py` | Только мониторинг мест (уведомления, без покупки) |
| `run_dual.py` | Два браузера acc1+acc2 (macOS: два окна или один терминал с `--same`) |
| `train_captcha_cnn.py` | Обучение локальной CNN |
| `label_rejected.py` | Разметка `rejected/` → `hard/` |
| `mark_hard.py` | Копирование капч из `confirmed/` в `hard/` (миграция) |

## Запуск

```bash
cd airlines_web
cp .env.example .env
cp .env.local.example .env.local
cp ../passengers.example.json ../passengers.json

./run.sh
# или
../.venv/bin/python book.py
```

Два браузера:

```bash
../.venv/bin/python run_dual.py
../.venv/bin/python run_dual.py --same
```

## Капча

Локальная CNN (`captcha_model.pt` в этой папке). Сбор данных — `CAPTCHA_COLLECT=1` в `.env.local`.

## Профили (`profiles/`)

Файлы `profiles/*.env` — отдельный маршрут, пассажир и/или аккаунт T5 для другой брони.

Примеры в репозитории: `malika.env`, `gulshen.env`, `ahmet_crz.env` и др. Шаблон — `profiles/example.env`.

### Создать профиль

```bash
cp profiles/example.env profiles/noviy.env
# отредактировать noviy.env: маршрут, DATES, PASSENGER_IDS, T5_PHONE, T5_PASSWORD…
```

### Два способа запуска

**`--profile`** (рекомендуется) — профиль **накладывается поверх** `.env`. Общие настройки из `.env`, переопределения — из `profiles/имя.env`.

```bash
../.venv/bin/python book.py --profile malika
# или
./run.sh --profile malika
```

**`--env`** — файл профиля **полностью заменяет** `.env` (нужен полный клон, не только diff):

```bash
../.venv/bin/python book.py --env profiles/malika.env
```

### Два браузера с профилем

```bash
../.venv/bin/python run_dual.py --profile malika
../.venv/bin/python run_dual.py --env profiles/malika.env
../.venv/bin/python run_dual.py --profile malika --same   # один терминал вместо двух окон
```

### Быстрые переопределения из CLI

Поверх профиля можно задать маршрут/пассажира без правки файла:

```bash
../.venv/bin/python book.py --profile malika --passenger 2 --dates 28.07.2026
../.venv/bin/python book.py --profile malika --depart ASB --arrival CRZ
```

Доступные флаги: `--passenger`, `--dates`, `--depart`, `--arrival`, `--flight-index`, `--flight-strict`, `--flight-time`, `--notify-email`.

### Порядок загрузки env

| Флаг | Что загружается |
|------|-----------------|
| `--profile malika` | `.env.local` → `.env` → `profiles/malika.env` |
| `--env profiles/malika.env` | `.env.local` → `profiles/malika.env` (без `.env`) |
| без флагов | `.env.local` → `.env` |

`.env.local` всегда первый — браузер, капча, Gmail.

### Ограничения

- `watch.py` профили **не поддерживает** — только `.env`.
- `--account acc1|acc2` — слот Chrome (тайминг :00 / :30), не отдельный логин; логин берётся из `.env` / `profiles/*.env` (`T5_PHONE`, `T5_PASSWORD`).
