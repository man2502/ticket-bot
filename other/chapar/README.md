# Çapar (Turkmenpost)

OTP-авторизация на `billing.post.gov.tm` + монитор внутренних рейсов T5.

## Login

1. `POST /api/users/auth/otp` — SMS-код (`phone`, `type`: `login` | `register`)
2. `POST /api/users/auth/login` — token (`phone`, `otp`)

```bash
cd chapar
cp chapar_check.env.example chapar_check.env
cp .env.local.example .env.local
# CHAPAR_PHONE в chapar_check.env

../.venv/bin/python chapar_check.py --login
# введи OTP из SMS (или CHAPAR_OTP=12345 в .env.local)
```

## Монитор

```bash
./run.sh
./run.sh --once
```

При старте: OTP → login → token в памяти (не пишется в файл).

## Env

| Файл | Что |
|------|-----|
| `chapar_check.env` | телефон, маршрут, DATE |
| `.env.local` | device-uuid, version, Gmail, `CHAPAR_OTP` |

Общие с корнем: `.venv/`, `passengers.json`.

## Дальше

Пришли curl поиска рейсов из Çapar — подключим в `run_check_once()`.
