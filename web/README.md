# Веб-панель

Управление поиском и бронированием билетов: пассажиры, процессы, брони,
настройки сервисов, разметка капч.

Панель **не содержит логики поиска**. Она хранит настройки, запускает ботов
(`railway-api/railway_check.py`, `airlines_api/api_check.py`) отдельными
процессами и показывает их вывод. Боты остаются работоспособными и из CLI.

## Установка

```bash
.venv/bin/python -m pip install -r requirements-web.txt
.venv/bin/python -m web.cli create-admin
```

Windows:

```cmd
.venv\Scripts\python.exe -m pip install -r requirements-web.txt
.venv\Scripts\python.exe -m web.cli create-admin
```

Учётной записи по умолчанию нет: панель ведёт к странице оплаты, и забытый
`admin/admin` здесь стоит слишком дорого.

## Запуск

```bash
./run_web.sh          # macOS / Linux
run_web.cmd           # Windows
```

По умолчанию `http://127.0.0.1:8000`. Адрес меняется переменными
`TICKET_HOST` и `TICKET_PORT`.

## Роли

| Может | user | admin |
|-------|:----:|:-----:|
| Вести список пассажиров | ✅ | ✅ |
| Создавать процессы поиска | ✅ | ✅ |
| Видеть чужие процессы | — | ✅ |
| Настройки, SMTP, SMS | — | ✅ |
| Справочники станций | — | ✅ |
| Разметка капч | — | ✅ |
| Разблокировка броней | — | ✅ |
| Пользователи | — | ✅ |

## Команды

```bash
python -m web.cli init-db
python -m web.cli create-admin
python -m web.cli reset-password --login имя
python -m web.cli list-users
```

## Данные

Всё рабочее лежит в `web/data/` (в git не попадает):

| Файл | Что |
|------|-----|
| `ticket.db` | SQLite: пользователи, пассажиры, процессы, логи, брони |
| `secret.key` | ключ шифрования паролей в базе |
| `session.key` | ключ подписи cookie |
| `captcha/` | картинки нерешённых капч |
| `runs/` | временные `check.env` и `passengers.json` запусков |

**Про ключи.** Пароли T5/Railway, SMTP и ключ SMS хранятся в базе
зашифрованными, ключ лежит отдельным файлом. Копия `ticket.db` без `secret.key`
паролей не выдаёт. Потеря `secret.key` — пароли придётся ввести заново
(остальные данные целы).

На сервере ключи удобнее задавать переменными `TICKET_SECRET_KEY` и
`TICKET_SESSION_KEY` вместо файлов.

## Устройство

```
web/
  app.py        роуты FastAPI
  auth.py       вход, роли
  cli.py        команды обслуживания
  config.py     пути, порт, каталоги
  crypto.py     Fernet для секретов
  db.py         подключение к SQLite
  models.py     схема базы
  security.py   scrypt-пароли, cookie сессии
  services/     подготовка запуска ботов, справочники, уведомления
  templates/ static/
```

## Состояние

Этап 1 готов: база, вход, роли, навигация, управление пользователями.
Разделы «Пассажиры», «Справочники», «Настройки», «Процессы», «Брони», «Капчи»
пока заглушки — наполняются дальше по этапам.
