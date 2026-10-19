# Запуск на сервере (Ubuntu + Docker Compose)

Панель и оба бота работают в одном контейнере. Все изменяемые данные лежат в
каталоге `data/` рядом с `docker-compose.yml`; образ можно пересобирать сколько
угодно — данные останутся.

```
data/
  web/                   база, ключи шифрования, аккаунты
  captcha_dataset_api/   набор капч для обучения
  model/                 модель капчи
  railway/               журнал броней поезда, состояние, станции
```

Панель слушает только `127.0.0.1:8000` на сервере. Снаружи — через SSH-туннель:
в ней паспорта и пароли, открывать порт в интернет нельзя.

## 0. Проверить, что сервер достаёт до перевозчиков

Сайты могут не пускать зарубежные IP и IP дата-центров. Проверить до всего остального:

```bash
curl -sS -o /dev/null -w "%{http_code} %{time_total}s\n" https://railway.gov.tm/
```

Код ответа и время — хорошо. `timed out` или `Connection refused` — с этого
сервера боты работать не будут, переезжать бессмысленно.

## 1. Поставить Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

После `usermod` перелогиньтесь, чтобы `docker` работал без `sudo`.

## 2. Забрать код

```bash
git clone <адрес репозитория> ticket
cd ticket
```

## 3. Перенести данные со старой машины

**На старой машине (Windows)** остановите все процессы в панели, затем саму
панель, и соберите архив:

```bash
.venv\Scripts\python.exe -m web.cli export-data --out ticket-data.tar.gz
```

В архиве база (снятая корректно, с последними записями), ключи шифрования,
аккаунты, модель капчи, набор капч и журнал поезда. **Ключи и паспортные данные
внутри — передавайте только по scp, не через мессенджеры и почту.**

```bash
scp ticket-data.tar.gz user@server:~/ticket/
```

**На сервере** распакуйте в `data/` и удалите архив:

```bash
mkdir -p data && tar -xzf ticket-data.tar.gz -C data && rm ticket-data.tar.gz
```

Пути старой машины (`D:\projects\ticket\...`) в базе контейнер перепишет сам
при первом запуске — по `data/MANIFEST.json`.

Без `secret.key` пароли аккаунтов и почты в базе не расшифровать — он должен
оказаться в `data/web/`.

## 4. Запустить

```bash
docker compose up -d --build
docker compose logs -f
```

В логах должно быть `Пути из D:\projects\ticket переписаны` и
`Uvicorn running on http://0.0.0.0:8000`. Первая сборка — несколько минут
(torch).

## 5. Открыть панель

На своём компьютере:

```bash
ssh -N -L 8000:127.0.0.1:8000 user@server
```

и в браузере — <http://127.0.0.1:8000>. Логин и пароль — прежние.

Запущенные процессы на старой машине при переезде не переносятся: после входа
запустите нужные заново.

## Обслуживание

| Что | Команда |
|---|---|
| логи | `docker compose logs -f --tail 200` |
| перезапуск | `docker compose restart` |
| обновить код | `git pull && docker compose up -d --build` |
| остановить | `docker compose down` |
| новый админ | `docker compose exec panel python -m web.cli create-admin` |
| сменить пароль | `docker compose exec panel python -m web.cli reset-password --login <логин>` |
| копия данных | `docker compose exec panel python -m web.cli export-data --out /data/backup.tar.gz` |

`docker compose down` и `restart` корректно останавливают ботов: панель шлёт
им сигнал и ждёт, пока они допишут журнал. Не используйте `docker kill` —
бот, оборванный посреди брони, оставит её «под вопросом».

Копию данных снимайте регулярно и уносите с сервера: архив ложится в
`data/backup.tar.gz`, оттуда — `scp` к себе.
