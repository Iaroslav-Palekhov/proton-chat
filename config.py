import os
import secrets
from datetime import timedelta, timezone, datetime
from urllib.parse import quote_plus

from env_loader import load_env

# Подтягиваем секреты и данные подключения к БД из файла .env (см. example.env)
load_env()


# ══════════════════════════════════════════════════════════════
# PostgreSQL — данные берутся из .env (DB_USER, DB_PASSWORD, ...)
# ══════════════════════════════════════════════════════════════
DB_USER = os.environ.get('DB_USER', '')
DB_PASSWORD = os.environ.get('DB_PASSWORD', '')
DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_PORT = os.environ.get('DB_PORT', '5432')
DB_NAME = os.environ.get('DB_NAME', '')
# Если psql заходит без пароля (peer), а приложение — нет: оставь пустым
# или укажи каталог сокета, например /var/run/postgresql
DB_SOCKET_DIR = os.environ.get('DB_SOCKET_DIR') or None

# Локальные переопределения (необязательный local_config.py рядом с config.py)
try:
    from local_config import *  # noqa: F401, F403
except ImportError:
    pass


def _build_database_uri():
    """Строка подключения из DB_* (.env / local_config.py), без DATABASE_URL."""
    if DB_SOCKET_DIR:
        pwd = quote_plus(DB_PASSWORD) if DB_PASSWORD else ''
        if pwd:
            return (
                f'postgresql+psycopg2://{DB_USER}:{pwd}@/{DB_NAME}'
                f'?host={quote_plus(DB_SOCKET_DIR)}'
            )
        return f'postgresql+psycopg2:///{DB_NAME}?host={quote_plus(DB_SOCKET_DIR)}'
    pwd = quote_plus(DB_PASSWORD)
    return f'postgresql+psycopg2://{DB_USER}:{pwd}@{DB_HOST}:{DB_PORT}/{DB_NAME}'


def database_uri_for_log():
    """Для лога при старте — без пароля."""
    if DB_SOCKET_DIR:
        return f'postgresql://{DB_USER}@/{DB_NAME} (socket {DB_SOCKET_DIR})'
    return f'postgresql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}'


def _get_or_create_secret_key():
    env_key = os.environ.get('FLASK_SECRET_KEY')
    if env_key:
        return env_key
    key_file = '.secret_key'
    if os.path.exists(key_file):
        with open(key_file, 'r') as f:
            key = f.read().strip()
            if len(key) >= 32:
                return key
    key = secrets.token_hex(64)
    try:
        with open(key_file, 'w') as f:
            f.write(key)
        os.chmod(key_file, 0o600)
        print(f"[SECURITY] Создан новый SECRET_KEY -> {key_file}")
    except Exception:
        pass
    return key


class Config:
    # Базовые настройки
    SECRET_KEY = _get_or_create_secret_key()
    SQLALCHEMY_DATABASE_URI = _build_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Оптимизация SQLAlchemy connection pool для PostgreSQL
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,     # проверяет соединение перед использованием
        'pool_recycle': 280,       # пересоздаёт соединения каждые ~5 минут
        'pool_size': 20,
        'max_overflow': 10,
    }

    # Кэширование статических файлов (30 дней)
    SEND_FILE_MAX_AGE_DEFAULT = 60 * 60 * 24 * 30

    # Загрузка файлов (вне static — только через /media/ и /download/)
    UPLOAD_FOLDER = 'private_uploads'
    MAX_CONTENT_LENGTH = 5000 * 1024 * 1024  # 500 MB — общий потолок Flask (и nginx/Caddy: client_max_body_size)
    # Лимиты по типу файла (apk → executable). Не больше MAX_CONTENT_LENGTH.
    FILE_CATEGORY_SIZE_LIMITS = {
        'image':      100 * 1024 * 1024,    # 100 MB
        'video':      500 * 1024 * 1024,    # 500 MB
        'audio':      500 * 1024 * 1024,    # 500 MB
        'document':   500 * 1024 * 1024,    # 500 MB
        'archive':    500 * 1024 * 1024,    # 500 MB
        'executable': 500 * 1024 * 1024,    # 500 MB — apk, exe, …
        'other':      500 * 1024 * 1024,    # 500 MB
    }
    ALLOWED_EXTENSIONS = None

    # Сессии и куки
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    SESSION_COOKIE_HTTPONLY = True       # JS не может читать cookie
    SESSION_COOKIE_SAMESITE = 'Lax'     # Защита от CSRF
    SESSION_COOKIE_SECURE = False        # True только при HTTPS
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = False

    # Безопасность
    PASSWORD_PEPPER = os.environ.get('PASSWORD_PEPPER', '')
    MESSAGE_ENCRYPTION_KEY = os.environ.get('MESSAGE_ENCRYPTION_KEY')
    MAX_MESSAGE_LENGTH = 0  # Без ограничений
    MAX_USERNAME_LENGTH = 50
    MIN_PASSWORD_LENGTH = 8
    RATELIMIT_ENABLED = True

    # Сжатие ответов (Flask-Compress)
    COMPRESS_MIMETYPES = [
        'text/html', 'text/css', 'text/javascript',
        'application/javascript', 'application/json',
        'text/plain', 'image/svg+xml'
    ]
    COMPRESS_LEVEL = 6        # Оптимальный баланс скорости и сжатия (1-9)
    COMPRESS_MIN_SIZE = 500   # Сжимаем только если > 500 байт

    # Аккаунт владельца/поддержки — username совпадает с этим значением
    PLATFORM_ADMIN_USERNAME = os.environ.get('PLATFORM_ADMIN_USERNAME', 'fsociety')

    # Системный бот уведомлений (как Telegram-канал обновлений)
    PLATFORM_BOT_USERNAME = os.environ.get('PLATFORM_BOT_USERNAME', 'Proton')

    # Базовый URL для invite-ссылок групп (укажи домен или IP:порт, без слэша на конце)
    # Пример: "https://example.com" или "http://192.168.1.10:5000"
    LINK_URL = os.environ.get('LINK_URL', 'https://chat.proton-net.ru')

    # Продакшен: раскомментируй при HTTPS:
    # SESSION_COOKIE_SECURE = True
    # REMEMBER_COOKIE_SECURE = True

    # Регистрация: True — доступны /register и кнопка «Создать аккаунт».
    # False — страница и кнопка регистрации скрыты. Если закомментировать — считается True.
    OPEN_REGISTRATION = False

class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
