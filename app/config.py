"""Конфигурация приложения из переменных окружения."""
import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

# Мастер-ключ. Используется для подписи сессий и (через производный ключ) для
# шифрования секретов AES-256-GCM. ОБЯЗАТЕЛЕН в production.
SECRET_KEY = os.environ.get("SECRET_KEY", "")

# Первичная инициализация администратора (применяется только при первом запуске,
# когда в БД ещё нет пользователя).
ADMIN_LOGIN = os.environ.get("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Часовой пояс рассылки по умолчанию.
TIMEZONE = os.environ.get("TZ", "Europe/Moscow")

# Порт HTTPS (используется entrypoint-скриптом/uvicorn).
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "18237"))

# Сид-значения секретов на первый запуск (далее редактируются в UI и хранятся
# в БД в зашифрованном виде).
SEED_TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SEED_TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEED_DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "")
SEED_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Интервал опроса реестра (минуты). Рассылка — отдельно по расписанию.
DEFAULT_POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_MINUTES", "60"))

# Расписание рассылки по умолчанию: воскресенье 12:00.
DEFAULT_SCHEDULE_DAY = os.environ.get("SCHEDULE_DAY", "sun")
DEFAULT_SCHEDULE_HOUR = int(os.environ.get("SCHEDULE_HOUR", "12"))
DEFAULT_SCHEDULE_MINUTE = int(os.environ.get("SCHEDULE_MINUTE", "0"))

# Время жизни сессии (секунды).
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(60 * 60 * 12)))

# Параметры защиты входа.
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "300"))


def effective_secret_key() -> str:
    """Возвращает SECRET_KEY; в крайнем случае — детерминированный запасной,
    чтобы приложение не падало в dev. В production переменная обязательна."""
    if SECRET_KEY:
        return SECRET_KEY
    # Запасной вариант только для локального запуска без заданного ключа.
    return "INSECURE-DEV-KEY-CHANGE-ME"
