# Панель и оба бота в одном образе: панель запускает ботов дочерними
# процессами и останавливает их сигналами, поэтому им нужен общий контейнер.
FROM python:3.13-slim

# tzdata — без него TZ=Asia/Ashgabat не действует, и проверки по секундам
# и время в логах уезжают в UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Откуда ставить пакеты. pip читает PIP_INDEX_URL сам. Если files.pythonhosted.org
# закрыт (так бывает в Туркменистане), подставьте зеркало при сборке:
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

# torch — сборка только под процессор: обычная тянет CUDA и раздувает образ
# на несколько гигабайт, а видеокарты на машине нет. Сам torch — с его индекса
# без зависимостей (их ссылки там ведут на files.pythonhosted.org), зависимости —
# из PIP_INDEX_URL.
RUN pip install --no-deps --index-url "$TORCH_INDEX_URL" torch \
    && pip install torch

# playwright нужен только старому веб-боту в other/, панель его не запускает.
COPY requirements.txt requirements-web.txt ./
RUN grep -v -i '^playwright' requirements.txt > /tmp/requirements-bots.txt \
    && pip install -r /tmp/requirements-bots.txt -r requirements-web.txt

COPY . .

# Пользователь без прав root: боты ходят в интернет, давать им root незачем.
RUN command -v setpriv >/dev/null \
    && useradd --create-home --uid 1000 app \
    && sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod +x docker/entrypoint.sh \
    && chown -R app:app /app

# Все изменяемые данные — в томе /data (раскладка — в web/migrate.py).
ENV TICKET_HOST=0.0.0.0 \
    TICKET_PORT=8000 \
    TICKET_DATA_DIR=/data/web \
    CAPTCHA_API_DATASET_DIR=/data/captcha_dataset_api \
    CAPTCHA_API_CNN_MODEL=/data/model/captcha_model_api.pt \
    RAILWAY_DATA_DIR=/data/railway

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-m", "web"]
