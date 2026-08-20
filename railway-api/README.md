# Railway (Turkmenistan)

HTTP API приложения Railway на `api.railway.gov.tm` — без браузера.

## Станции

```bash
cd railway
cp check.env.example check.env
cp .env.local.example .env.local

./run.sh --stations
./run.sh --stations --json
```

Первый endpoint (как в iOS-приложении):

```bash
curl -H 'Host: api.railway.gov.tm' \
  -H 'Accept: */*' \
  -H 'Accept-Language: ru' \
  -H 'Connection: keep-alive' \
  --compressed \
  -H 'User-Agent: Railway/1 CFNetwork/3860.600.12 Darwin/25.5.0' \
  https://api.railway.gov.tm/v2/stations
```

## Поиск поездов

```bash
./run.sh --trips
./run.sh --once      # один цикл монитора
./run.sh             # монитор каждую минуту
```

Endpoint:

```bash
curl --request GET \
  --url 'https://api.railway.gov.tm/v2/trips?source=17&destination=49&date=2026-08-10&adult=1&child=0' \
  --compressed \
  --header 'Accept: */*' \
  --header 'Accept-Encoding: deflate, gzip' \
  --header 'Accept-Language: ru' \
  --header 'Connection: keep-alive' \
  --header 'Host: api.railway.gov.tm' \
  --header 'User-Agent: Railway/1 CFNetwork/3860.600.12 Darwin/25.5.0'
```

`adult` / `child` берутся из `PASSENGER_IDS` и `passengers.json`.

### Тип вагона (fallback)

`WAGON_TYPE_ID=3` — только плацкарт.

`WAGON_TYPE_ID=3,2` — **по порядку списка**: сначала смотрим плацкарт (3), только если мест нет — купе (2). Так же в мониторе, `--seats` и авто-брони.

## Автобронь и защита от дублей

`POST /v2/bookings` — единственная неидемпотентная операция в проекте, поэтому
результат делится на три состояния, а не на «ок / не ок»:

| Состояние | Что значит | Что делает монитор |
|-----------|------------|--------------------|
| бронь создана | есть `booking_number` | останавливается, код `0` |
| достоверный отказ | сервер ответил `success=false` с текстом, или запрос вообще не ушёл | пробует следующий рейс |
| **неизвестно** | таймаут, обрыв, 5xx, неразобранный ответ | **останавливается, код `2`** |

Каждая попытка пишется в `railway_bookings.json` **до** отправки запроса
(атомарно, отдельно от `railway_state.json`), вместе с `Idempotency-Key`.
Поэтому:

- падение процесса между `POST` и ответом не приводит к слепому повтору;
- перезапуск после успешной брони не заводит вторую бронь;
- повтор по тем же местам уходит с тем же `Idempotency-Key`.

Разблокировать после ручной проверки броней в приложении Railway:

```bash
./run.sh --force-rebook
```

## Тесты

```bash
../.venv/bin/python -m pytest railway/ -q      # из корня репозитория
```

## Env

| Файл | Что |
|------|-----|
| `check.env` | ID станций (откуда/куда), DATE, PASSENGER_IDS |
| `.env.local` | User-Agent, Gmail, слот проверки, таймауты HTTP |

Общие с корнем: `.venv/`, `passengers.json`, `core/`.

### HTTP

Транспорт — `core/http.py`: одно живое соединение на весь процесс
(keep-alive + переиспользование TLS-сессии), прогрев за секунду до слота,
раздельные connect/read таймауты, ретраи только для GET.

| Переменная | По умолчанию | Что |
|------------|--------------|-----|
| `RAILWAY_CONNECT_TIMEOUT` | `2.5` | таймаут установки соединения |
| `RAILWAY_READ_TIMEOUT` | `6` | чтение ответа на GET |
| `RAILWAY_WRITE_TIMEOUT` | `12` | чтение ответа на POST (без ретраев) |
| `RAILWAY_HTTP_RETRIES` | `2` | ретраи GET на 429/5xx и обрывы |
| `RAILWAY_HTTP_DEBUG` | `0` | печатать статус и латентность запросов |
