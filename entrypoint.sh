#!/usr/bin/env sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
CERT_DIR="${DATA_DIR}/certs"
CERT_FILE="${CERT_DIR}/server.crt"
KEY_FILE="${CERT_DIR}/server.key"
HTTPS_PORT="${HTTPS_PORT:-18237}"

mkdir -p "${CERT_DIR}"

# Самоподписанный сертификат генерируется один раз и сохраняется в томе /data.
if [ ! -f "${CERT_FILE}" ] || [ ! -f "${KEY_FILE}" ]; then
    echo "[tls] Генерация самоподписанного сертификата (срок 10 лет)..."
    SAN="${TLS_SAN:-IP:127.0.0.1,DNS:localhost}"
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "${KEY_FILE}" -out "${CERT_FILE}" -days 3650 \
        -subj "/CN=update-tracker" \
        -addext "subjectAltName=${SAN}" 2>/dev/null
    echo "[tls] Готово: ${CERT_FILE}"
fi

if [ -z "${SECRET_KEY}" ]; then
    echo "[warn] SECRET_KEY не задан — используется небезопасный ключ по умолчанию."
    echo "[warn] Задайте SECRET_KEY в .env для production."
fi

echo "[run] Запуск Update Tracker на https://0.0.0.0:${HTTPS_PORT}"
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${HTTPS_PORT}" \
    --ssl-keyfile "${KEY_FILE}" \
    --ssl-certfile "${CERT_FILE}" \
    --proxy-headers \
    --forwarded-allow-ips "*"
