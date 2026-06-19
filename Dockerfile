FROM python:3.12-slim

# Системные зависимости: openssl нужен для генерации самоподписанного сертификата.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HTTPS_PORT=18237

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Том для БД и TLS-сертификата.
VOLUME ["/data"]
EXPOSE 18237

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsk https://127.0.0.1:${HTTPS_PORT}/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
