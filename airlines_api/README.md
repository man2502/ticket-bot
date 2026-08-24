# T5 Ticket Watch — API (без браузера)

Монитор мест через HTTP API iOS-приложения.

**Общие с браузерным проектом** (корень `../`):
- `.venv/` — одно виртуальное окружение
- `passengers.json` — пассажиры

**Env API** (в этой папке `airlines_api/`):
- `check.env` — маршрут, DATE, логин/пароль T5
- `.env.local` — user-agent, ios password, Gmail, CAPTCHA

## Запуск

```bash
# из корня репозитория
.venv/bin/python airlines_api/api_check.py

# или
cd airlines_api && ./run.sh
```

Первый раз (если ещё нет venv в корне):

```bash
cd ..
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Env

```bash
cp .env.local.example .env.local
cp check.env.example check.env
```

`.env.local` — user-agent, **API_IOS_PASSWORD**, Gmail, CAPTCHA.  
`check.env` — маршрут, **API_CUSTOMER_PHONE** / **API_CUSTOMER_PASSWORD**.

### Токены

Cookies не используются — только Bearer-токены.

При старте login **не** делается, если в env уже есть оба Bearer **и** логин/пароль не менялись. Смена `API_CUSTOMER_PHONE` / `API_CUSTOMER_PASSWORD` / `API_IOS_PASSWORD` — сразу полный re-login. Каждый цикл поиска:

1. `GET /api/v1/customers/info` — ping. Тело **200 не читается** (ни имя, ни телефон).
2. `401`/`403` с `"token_type": "client"` → `POST /auth/login` (ios + `API_IOS_PASSWORD`)
3. `401`/`403` с `"token_type": "customer"` → `POST /customers/auth/login` (телефон + пароль, нужен живой client token)
4. `GET /api/v1/captcha/finding`
5. `POST /api/v1/GetAirAvailability`

Если токенов ещё нет — один раз `auth/login` → `customers/auth/login`. Дальше они в памяти (`API_SAVE_TOKENS=0`).

## Капча

При запуске из CLI — ручной ввод в терминале (`CAPTCHA_API_MANUAL=1`
в `airlines_api/.env.local`). Веб-панель запускает бота с `CAPTCHA_API_MANUAL=0`:
процесс не должен ждать человека, нерешённые капчи уходят в раздел «Капчи».

Обучение 5-цифровой CNN (pretrain из браузерной
`other/airlines_web/captcha_model.pt` — та же капча, 6→5 цифр):

```bash
../.venv/bin/python label_api_captcha.py
../.venv/bin/python label_api_rejected.py   # rejected/ → hard/
../.venv/bin/python train_captcha_cnn_api.py
# без pretrain: --no-pretrain
```

Размечать удобнее в панели: раздел «Капчи» показывает картинку рядом
с вариантом модели и сам раскладывает файлы по папкам датасета.

## Авто-бронь

При `API_AUTO_BOOK=1` в `airlines_api/.env.local` после появления мест:

1. `POST /api/v1/CreateBooking` — `searchKey` из ответа GetAirAvailability
2. Email с PNR, маршрутом, временем рейса, пассажиром и `checkoutUrl` для оплаты

`adultexpdt1` и аналоги — **сегодня + 1 год** (не срок из passengers.json).

## Выбор рейса

Те же переменные, что в браузерном боте (`book.py` / `steps.py`):

| Переменная | Описание |
|------------|----------|
| `FLIGHT_INDEX=1` | 1 = первый рейс в списке ([1], [2], …); несколько: `4,3` |
| `FLIGHT_STRICT=0` | сначала указанные индексы, иначе другой прямой с местами |
| `FLIGHT_STRICT=1` | только указанные индексы/время — иначе «мест нет» |
| `FLIGHT_TIME=06:30` | выбор по времени вылета (опционально) |

Слот: каждую минуту в :29–:31 (настройка `API_CHECK_AT_SEC=29-31` в `airlines_api/.env.local`).
