#!/bin/sh
# Подготовка тома /data и запуск панели от пользователя app.
#
# Стартуем от root только ради одного: распакованный на хосте архив может
# принадлежать кому угодно, а писать в него будет app. Дальше — без root.
set -eu

for dir in "$TICKET_DATA_DIR" "$CAPTCHA_API_DATASET_DIR" "$(dirname "$CAPTCHA_API_CNN_MODEL")" "$RAILWAY_DATA_DIR"; do
    mkdir -p "$dir"
done

# Первый запуск без переноса данных: рабочая модель капчи — из образа.
if [ ! -f "$CAPTCHA_API_CNN_MODEL" ] && [ -f /app/airlines_api/captcha_model_api.pt ]; then
    cp /app/airlines_api/captcha_model_api.pt "$CAPTCHA_API_CNN_MODEL"
    echo "[entrypoint] модель капчи взята из образа"
fi

if [ "$(id -u)" = "0" ]; then
    # Только то, что ещё не принадлежит app: в наборе капч десятки тысяч файлов,
    # и chown -R на каждом старте тянулся бы заметно.
    find /data \! -user app -exec chown app:app {} +
    exec setpriv --reuid=app --regid=app --init-groups /app/docker/entrypoint.sh "$@"
fi

# Данные перенесены со старой машины — один раз переписать её пути в базе.
python -m web.cli relocate-paths --if-needed

exec "$@"
