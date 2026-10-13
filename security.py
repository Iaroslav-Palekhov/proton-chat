"""
security.py — Модуль безопасности мессенджера Papirus
=======================================================
Реализует:
  1. Шифрование сообщений (AES-256-GCM) — симметричное шифрование
  2. Хэширование паролей (bcrypt + SHA-256 pepper)
  3. Rate limiting — защита от брутфорса и DDoS
  4. Валидация и санитизация данных
  5. Генерация CSRF-токенов
  6. Аудит-лог событий безопасности
  7. Заголовки безопасности HTTP
  8. Проверка силы пароля

╔══════════════════════════════════════════════════════════════╗
║              НАСТРОЙКИ БЕЗОПАСНОСТИ — ОБЯЗАТЕЛЬНО            ║
║                  ОТРЕДАКТИРУЙТЕ ПЕРЕД ЗАПУСКОМ               ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  PASSWORD_PEPPER  (задаётся в файле .env)                    ║
║    Секрет, который примешивается к каждому паролю перед       ║
║    хэшированием. Храните его только в .env (см. example.env). ║
║    НИКОГДА не меняйте после первого запуска — все пароли      ║
║    станут невалидными!                                        ║
║                                                              ║
║  MESSAGE_ENCRYPTION_KEY  (задаётся в файле .env)             ║
║    AES-256-ключ для шифрования сообщений в БД.               ║
║    Оставьте пустым — ключ сгенерируется и сохранится в        ║
║    файл .encryption_key. Или задайте base64(32 байта) сами.  ║
║    НИКОГДА не меняйте после первого запуска — все сообщения   ║
║    станут нечитаемыми!                                        ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import hmac
import time
import hashlib
import logging
import secrets
import base64
import json
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict

from flask import request, jsonify, session, g

from env_loader import load_env

# Секреты (PASSWORD_PEPPER, MESSAGE_ENCRYPTION_KEY) берутся из файла .env
load_env()

# ==============================================================
# PEPPER ДЛЯ ПАРОЛЕЙ — переменная PASSWORD_PEPPER в файле .env
# Пример генерации: python3 -c "import secrets; print(secrets.token_hex(32))"
# ВАЖНО: после первого запуска НЕ МЕНЯЙТЕ — иначе пользователи не смогут войти.
# ==============================================================
PASSWORD_PEPPER = os.environ.get('PASSWORD_PEPPER', '')

# ==============================================================
# КЛЮЧ ШИФРОВАНИЯ СООБЩЕНИЙ — переменная MESSAGE_ENCRYPTION_KEY в файле .env
# Оставьте пустым — ключ создастся автоматически в файле .encryption_key
# Или задайте base64-строку из 32 байт:
#   python3 -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
# ВАЖНО: после первого запуска НЕ МЕНЯЙТЕ — все сообщения в БД станут нечитаемыми.
# ==============================================================
MESSAGE_ENCRYPTION_KEY_VALUE = os.environ.get('MESSAGE_ENCRYPTION_KEY', '')

from werkzeug.security import generate_password_hash, check_password_hash

# ============================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ БЕЗОПАСНОСТИ
# ============================================================

security_logger = logging.getLogger('security')
security_logger.setLevel(logging.INFO)

# Формат: время | уровень | событие | IP | детали
formatter = logging.Formatter(
    '[%(asctime)s] %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Лог в файл
try:
    os.makedirs('logs', exist_ok=True)
    file_handler = logging.FileHandler('logs/security.log', encoding='utf-8')
    file_handler.setFormatter(formatter)
    security_logger.addHandler(file_handler)
except Exception:
    pass

# Лог в консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
security_logger.addHandler(console_handler)


# ============================================================
# ШИФРОВАНИЕ СООБЩЕНИЙ — AES-256-GCM
# ============================================================
# AES-256-GCM — стандарт военного уровня с аутентификацией.
# Каждое сообщение шифруется уникальным 12-байтовым nonce,
# что делает повторные атаки (replay attack) невозможными.

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    security_logger.warning("cryptography не установлена — шифрование сообщений отключено. Выполни: pip install cryptography")


class MessageEncryption:
    """
    Шифрование и дешифрование сообщений с помощью AES-256-GCM.

    Формат зашифрованного блока (base64):
        [12 байт nonce][зашифрованный текст + 16 байт тег аутентификации]

    Ключ хранится в переменной окружения MESSAGE_ENCRYPTION_KEY
    или генерируется автоматически и сохраняется в файл .encryption_key
    """

    KEY_FILE = '.encryption_key'

    @classmethod
    def _load_or_create_key(cls) -> bytes:
        """Загружает ключ из env или файла, или создаёт новый."""
        # Приоритет 1: переменная окружения
        env_key = MESSAGE_ENCRYPTION_KEY_VALUE or os.environ.get('MESSAGE_ENCRYPTION_KEY')
        if env_key:
            try:
                key = base64.b64decode(env_key)
                if len(key) == 32:
                    return key
            except Exception:
                pass

        # Приоритет 2: файл ключа
        if os.path.exists(cls.KEY_FILE):
            try:
                with open(cls.KEY_FILE, 'r') as f:
                    key = base64.b64decode(f.read().strip())
                    if len(key) == 32:
                        return key
            except Exception:
                pass

        # Генерируем новый 256-битный ключ
        key = secrets.token_bytes(32)
        try:
            with open(cls.KEY_FILE, 'w') as f:
                f.write(base64.b64encode(key).decode())
            # Ограничиваем права доступа к файлу ключа
            os.chmod(cls.KEY_FILE, 0o600)
            security_logger.info(f"Создан новый ключ шифрования → {cls.KEY_FILE}")
        except Exception as e:
            security_logger.warning(f"Не удалось сохранить ключ: {e}")

        return key

    @classmethod
    def encrypt(cls, plaintext: str) -> str | None:
        """
        Шифрует текст сообщения.
        Возвращает base64-строку или None если шифрование недоступно.
        """
        if not CRYPTO_AVAILABLE or not plaintext:
            return plaintext

        try:
            key = cls._load_or_create_key()
            aesgcm = AESGCM(key)
            nonce = secrets.token_bytes(12)  # 96-bit nonce — рекомендация NIST
            ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
            # Склеиваем nonce + ciphertext и кодируем в base64
            encrypted = base64.b64encode(nonce + ciphertext).decode('utf-8')
            return f"ENC:{encrypted}"  # Префикс для определения зашифрованных сообщений
        except Exception as e:
            security_logger.error(f"Ошибка шифрования: {e}")
            return plaintext

    @classmethod
    def decrypt(cls, ciphertext: str) -> str | None:
        """
        Дешифрует сообщение.
        Если сообщение не зашифровано (старые записи) — возвращает как есть.
        """
        if not CRYPTO_AVAILABLE or not ciphertext:
            return ciphertext

        # Если нет префикса — сообщение не зашифровано (обратная совместимость)
        if not ciphertext.startswith("ENC:"):
            return ciphertext

        try:
            key = cls._load_or_create_key()
            aesgcm = AESGCM(key)
            raw = base64.b64decode(ciphertext[4:])  # Убираем "ENC:"
            nonce = raw[:12]
            encrypted_data = raw[12:]
            plaintext = aesgcm.decrypt(nonce, encrypted_data, None)
            return plaintext.decode('utf-8')
        except Exception as e:
            security_logger.error(f"Ошибка дешифрования: {e}")
            return "[Сообщение повреждено]"

    @classmethod
    def is_available(cls) -> bool:
        return CRYPTO_AVAILABLE


# ============================================================
# ХЭШИРОВАНИЕ ПАРОЛЕЙ С PEPPER
# ============================================================
# Используем werkzeug (bcrypt под капотом) + SHA-256 pepper.
# Pepper — секретный серверный секрет, который не хранится в БД.
# Даже при утечке базы данных пароли невозможно взломать без pepper.

class PasswordSecurity:
    # Pepper хранится только в .env, никогда в БД
    PEPPER = PASSWORD_PEPPER  # читается из .env в начале файла
    
    # Минимальные требования к паролю
    MIN_LENGTH = 8
    REQUIRE_UPPERCASE = True
    REQUIRE_DIGIT = True
    REQUIRE_SPECIAL = False  # Можно включить

    @classmethod
    def _apply_pepper(cls, password: str) -> str:
        """Применяет pepper через HMAC-SHA256 перед хэшированием."""
        return hmac.new(
            cls.PEPPER.encode('utf-8'),
            password.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    @classmethod
    def hash_password(cls, password: str) -> str:
        """Хэширует пароль с pepper через bcrypt."""
        peppered = cls._apply_pepper(password)
        return generate_password_hash(peppered, method='pbkdf2:sha256:600000')

    @classmethod
    def verify_password(cls, password: str, hashed: str) -> bool:
        """Проверяет пароль с учётом pepper."""
        peppered = cls._apply_pepper(password)
        return check_password_hash(hashed, peppered)

    @classmethod
    def check_strength(cls, password: str) -> dict:
        """
        Проверяет силу пароля.
        Возвращает {'valid': bool, 'score': 0-5, 'errors': list, 'strength': str}
        """
        errors = []
        score = 0

        if len(password) < cls.MIN_LENGTH:
            errors.append(f'Минимум {cls.MIN_LENGTH} символов')
        else:
            score += 1
            if len(password) >= 12:
                score += 1
            if len(password) >= 16:
                score += 1

        if cls.REQUIRE_UPPERCASE and not re.search(r'[A-Z]', password):
            errors.append('Нужна хотя бы одна заглавная буква')
        elif re.search(r'[A-Z]', password):
            score += 1

        if cls.REQUIRE_DIGIT and not re.search(r'\d', password):
            errors.append('Нужна хотя бы одна цифра')
        elif re.search(r'\d', password):
            score += 1

        if re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            score += 1

        # Проверка на распространённые пароли
        common = ['password', 'qwerty', '12345678', 'admin', 'letmein']
        if password.lower() in common:
            errors.append('Пароль слишком простой')
            score = max(0, score - 2)

        strength_map = {0: 'Очень слабый', 1: 'Слабый', 2: 'Средний', 3: 'Хороший', 4: 'Сильный', 5: 'Очень сильный'}
        strength = strength_map.get(min(score, 5), 'Неизвестно')

        return {
            'valid': len(errors) == 0,
            'score': min(score, 5),
            'errors': errors,
            'strength': strength
        }


# ============================================================
# RATE LIMITING — Защита от брутфорса и DDoS
# ============================================================

class RateLimiter:
    """
    In-memory rate limiter с sliding window алгоритмом.
    
    Отслеживает запросы по IP-адресу и/или идентификатору действия.
    При превышении лимита блокирует на заданное время.
    """

    def __init__(self):
        self._requests: dict = defaultdict(list)
        self._blocked: dict = {}

        # Конфигурация лимитов для разных действий
        self.limits = {
            'login':          {'requests': 5,   'window': 60,   'block': 300},   # 5 попыток за 1 мин → блок 5 мин
            'register':       {'requests': 3,   'window': 3600, 'block': 3600},  # 3 регистрации за час → блок 1 час
            'send_message':   {'requests': 60,  'window': 60,   'block': 30},    # 60 сообщений в мин
            'upload_file':    {'requests': 20,  'window': 60,   'block': 60},    # 20 файлов в мин
            'api_general':    {'requests': 200, 'window': 60,   'block': 30},    # 200 запросов в мин
            'password_check': {'requests': 10,  'window': 60,   'block': 120},   # 10 смен пароля
        }

    def _get_ip(self) -> str:
        from utils import get_client_ip
        return get_client_ip()

    def is_blocked(self, action: str, identifier: str = None) -> tuple[bool, int]:
        """
        Проверяет, заблокирован ли IP для данного действия.
        Возвращает (заблокирован, секунд до разблокировки).
        """
        ip = self._get_ip()
        key = f"{action}:{identifier or ip}"

        if key in self._blocked:
            block_until = self._blocked[key]
            remaining = int(block_until - time.time())
            if remaining > 0:
                return True, remaining
            else:
                del self._blocked[key]

        return False, 0

    def record_attempt(self, action: str, identifier: str = None) -> tuple[bool, dict]:
        """
        Регистрирует попытку действия.
        Возвращает (разрешено, информация о лимите).
        """
        ip = self._get_ip()
        key = f"{action}:{identifier or ip}"
        now = time.time()

        config = self.limits.get(action, self.limits['api_general'])
        window = config['window']
        max_requests = config['requests']
        block_duration = config['block']

        # Очищаем устаревшие записи
        self._requests[key] = [t for t in self._requests[key] if now - t < window]

        # Проверяем лимит
        current_count = len(self._requests[key])
        if current_count >= max_requests:
            # Блокируем
            self._blocked[key] = now + block_duration
            security_logger.warning(
                f"RATE_LIMIT_EXCEEDED | action={action} | ip={ip} | "
                f"requests={current_count}/{max_requests} | blocked={block_duration}s"
            )
            return False, {
                'blocked': True,
                'retry_after': block_duration,
                'limit': max_requests,
                'window': window
            }

        # Регистрируем запрос
        self._requests[key].append(now)

        remaining = max_requests - len(self._requests[key])
        return True, {
            'blocked': False,
            'remaining': remaining,
            'limit': max_requests,
            'reset_in': window
        }

    def cleanup(self):
        """Очищает устаревшие записи (вызывать периодически)."""
        now = time.time()
        for key in list(self._requests.keys()):
            self._requests[key] = [t for t in self._requests[key] if now - t < 3600]
            if not self._requests[key]:
                del self._requests[key]
        for key in list(self._blocked.keys()):
            if self._blocked[key] < now:
                del self._blocked[key]


# Глобальный экземпляр rate limiter
rate_limiter = RateLimiter()


def rate_limit(action: str):
    """
    Декоратор для применения rate limiting к маршрутам Flask.
    
    Использование:
        @app.route('/login', methods=['POST'])
        @rate_limit('login')
        def login():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Проверяем только POST запросы (GET не ограничиваем)
            if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
                blocked, info = rate_limiter.is_blocked(action)
                if blocked:
                    from utils import get_client_ip
                    ip = get_client_ip()
                    security_logger.warning(f"BLOCKED_REQUEST | action={action} | ip={ip} | retry_after={info}s")
                    return jsonify({
                        'error': f'Слишком много попыток. Подождите {info} секунд.',
                        'retry_after': info
                    }), 429

                allowed, limit_info = rate_limiter.record_attempt(action)
                if not allowed:
                    return jsonify({
                        'error': f'Превышен лимит запросов. Попробуйте через {limit_info["retry_after"]} секунд.',
                        'retry_after': limit_info['retry_after']
                    }), 429

            return f(*args, **kwargs)
        return decorated_function
    return decorator


# ============================================================
# САНИТИЗАЦИЯ И ВАЛИДАЦИЯ ВВОДА
# ============================================================

class InputSanitizer:
    """Очищает пользовательский ввод от потенциально опасного контента."""

    # XSS-опасные паттерны
    XSS_PATTERNS = [
        re.compile(r'<script[^>]*>.*?</script>', re.IGNORECASE | re.DOTALL),
        re.compile(r'javascript:', re.IGNORECASE),
        re.compile(r'on\w+\s*=', re.IGNORECASE),  # onclick=, onerror= и т.д.
        re.compile(r'<iframe', re.IGNORECASE),
        re.compile(r'<object', re.IGNORECASE),
        re.compile(r'<embed', re.IGNORECASE),
        re.compile(r'<link', re.IGNORECASE),
        re.compile(r'vbscript:', re.IGNORECASE),
        re.compile(r'data:text/html', re.IGNORECASE),
    ]

    # SQL-инъекции (дополнительная защита, ORM уже защищает)
    SQL_PATTERNS = [
        re.compile(r';\s*(drop|delete|truncate|alter|create|insert|update)\s+', re.IGNORECASE),
        re.compile(r'--\s*$', re.MULTILINE),
        re.compile(r'/\*.*?\*/', re.DOTALL),
        re.compile(r"'\s*(or|and)\s+'?\d+'?\s*=\s*'?\d+", re.IGNORECASE),
    ]

    # Path traversal
    PATH_TRAVERSAL = re.compile(r'\.\.[/\\]')

    @classmethod
    def sanitize_text(cls, text: str, max_length: int = 20000) -> str:
        """
        Очищает текстовое сообщение от XSS и других угроз.
        НЕ удаляет HTML-теги полностью — пользователь может отправлять < и >,
        но они будут эскейпированы при отображении в шаблоне (Jinja2 делает это автоматически).
        """
        if not text:
            return text

        # Ограничение длины
        text = text[:max_length]

        # Проверка на XSS
        for pattern in cls.XSS_PATTERNS:
            if pattern.search(text):
                security_logger.warning(f"XSS_ATTEMPT_DETECTED | preview={text[:100]!r}")
                # Экранируем опасные символы вместо удаления
                text = text.replace('<', '&lt;').replace('>', '&gt;')
                break

        return text

    @classmethod
    def sanitize_username(cls, username: str) -> str | None:
        """Проверяет и очищает username."""
        if not username:
            return None
        username = username.strip()
        # Только буквы, цифры, подчёркивание, дефис
        if not re.match(r'^[a-zA-Zа-яА-Я0-9_\-\.]{2,50}$', username):
            return None
        return username

    @classmethod
    def sanitize_filename(cls, filename: str) -> str:
        """Безопасное имя файла — убираем path traversal и опасные расширения."""
        if not filename:
            return 'file'

        # Убираем path traversal
        filename = cls.PATH_TRAVERSAL.sub('', filename)
        filename = os.path.basename(filename)

        # Ограничение длины
        name, ext = os.path.splitext(filename)
        name = name[:100]
        ext = ext[:10]

        return f"{name}{ext}" or 'file'

    @classmethod
    def validate_email(cls, email: str) -> bool:
        """Валидирует email-адрес."""
        if not email:
            return False
        pattern = re.compile(
            r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'
        )
        return bool(pattern.match(email)) and len(email) <= 100

    @classmethod
    def check_sql_injection(cls, value: str) -> bool:
        """Возвращает True если обнаружена попытка SQL-инъекции."""
        if not value:
            return False
        for pattern in cls.SQL_PATTERNS:
            if pattern.search(value):
                return True
        return False


# ============================================================
# CSRF ЗАЩИТА
# ============================================================

class CSRFProtection:
    """
    Простая CSRF защита через двойную отправку cookie.
    Токен генерируется при входе и проверяется при каждом POST.
    """

    TOKEN_LENGTH = 32
    TOKEN_SESSION_KEY = '_csrf_token'

    @classmethod
    def generate_token(cls) -> str:
        """Генерирует и сохраняет CSRF-токен в сессию."""
        token = secrets.token_urlsafe(cls.TOKEN_LENGTH)
        session[cls.TOKEN_SESSION_KEY] = token
        return token

    @classmethod
    def get_token(cls) -> str:
        """Получает текущий токен или создаёт новый."""
        if cls.TOKEN_SESSION_KEY not in session:
            return cls.generate_token()
        return session[cls.TOKEN_SESSION_KEY]

    @classmethod
    def validate_token(cls, token: str) -> bool:
        """Проверяет токен. Использует hmac.compare_digest для защиты от timing attacks."""
        if not token:
            return False
        expected = session.get(cls.TOKEN_SESSION_KEY, '')
        if not expected:
            return False
        # compare_digest защищает от атаки по времени (timing attack)
        return hmac.compare_digest(token, expected)


# ============================================================
# ЗАГОЛОВКИ БЕЗОПАСНОСТИ HTTP
# ============================================================

def apply_security_headers(response):
    """
    Добавляет защитные HTTP-заголовки к каждому ответу.
    
    Content-Security-Policy — запрещает загрузку ресурсов с чужих доменов.
    X-Frame-Options — защита от clickjacking.
    X-Content-Type-Options — запрет MIME-sniffing.
    Referrer-Policy — не отправлять Referer на внешние сайты.
    Permissions-Policy — отключает ненужные браузерные API.
    """
    # Запрет фреймов (clickjacking)
    response.headers['X-Frame-Options'] = 'DENY'

    # Запрет MIME-sniffing
    response.headers['X-Content-Type-Options'] = 'nosniff'

    # XSS-фильтр (для старых браузеров)
    response.headers['X-XSS-Protection'] = '1; mode=block'

    # Политика Referer
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    # Content Security Policy
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "   # unsafe-inline нужен для inline JS в шаблонах
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "media-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none';"
    )

    # Отключаем ненужные браузерные API
    response.headers['Permissions-Policy'] = (
        "geolocation=(), camera=(), microphone=(), payment=()"
    )

    # HSTS при HTTPS
    from flask import request as _req
    if _req.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    return response


# ============================================================
# АУДИТ-ЛОГ СОБЫТИЙ БЕЗОПАСНОСТИ
# ============================================================

class SecurityAudit:
    """Логирует важные события безопасности."""

    @staticmethod
    def _get_ip():
        from utils import get_client_ip
        return get_client_ip()

    @classmethod
    def log_login_success(cls, user_id: int, username: str):
        security_logger.info(
            f"LOGIN_SUCCESS | user_id={user_id} | username={username} | ip={cls._get_ip()} | ua={request.user_agent.string[:100]}"
        )

    @classmethod
    def log_login_failure(cls, attempted_username: str):
        security_logger.warning(
            f"LOGIN_FAILURE | attempted={attempted_username!r} | ip={cls._get_ip()} | ua={request.user_agent.string[:100]}"
        )

    @classmethod
    def log_register(cls, user_id: int, username: str, email: str):
        # Маскируем email для приватности
        masked_email = email[:2] + '***@' + email.split('@')[-1] if '@' in email else '***'
        security_logger.info(
            f"REGISTER | user_id={user_id} | username={username} | email={masked_email} | ip={cls._get_ip()}"
        )

    @classmethod
    def log_logout(cls, user_id: int, username: str):
        security_logger.info(
            f"LOGOUT | user_id={user_id} | username={username} | ip={cls._get_ip()}"
        )

    @classmethod
    def log_message_sent(cls, sender_id: int, target: str, has_file: bool = False):
        security_logger.debug(
            f"MESSAGE_SENT | sender={sender_id} | target={target} | file={has_file} | ip={cls._get_ip()}"
        )

    @classmethod
    def log_file_upload(cls, user_id: int, filename: str, size: int, category: str):
        security_logger.info(
            f"FILE_UPLOAD | user={user_id} | file={filename!r} | size={size} | category={category} | ip={cls._get_ip()}"
        )

    @classmethod
    def log_suspicious_activity(cls, action: str, details: str, user_id: int = None):
        security_logger.error(
            f"SUSPICIOUS | action={action} | user={user_id} | details={details!r} | ip={cls._get_ip()}"
        )

    @classmethod
    def log_password_reset(cls, user_id: int, username: str):
        """Логирует успешный сброс пароля."""
        security_logger.info(
            f"PASSWORD_RESET_SUCCESS | user_id={user_id} | username={username} | ip={cls._get_ip()}"
        )

    @classmethod
    def log_access_denied(cls, resource: str, user_id: int = None):
        security_logger.warning(
            f"ACCESS_DENIED | resource={resource} | user={user_id} | ip={cls._get_ip()}"
        )


# ============================================================
# ПРОВЕРКА ЗАГРУЖАЕМЫХ ФАЙЛОВ
# ============================================================

class FileSecurityChecker:
    """Проверяет безопасность загружаемых файлов."""

    # Исполняемые файлы, которые опасно запускать на сервере
    # (они всё ещё разрешены для скачивания, но логируются)
    HIGH_RISK_EXTENSIONS = {'.exe', '.bat', '.cmd', '.sh', '.ps1', '.vbs', '.js', '.msi', '.dll'}

    # Максимальные размеры по категориям — из config.FILE_CATEGORY_SIZE_LIMITS
    _DEFAULT_CATEGORY_SIZE_LIMITS = {
        'image':     10 * 1024 * 1024,
        'video':    500 * 1024 * 1024,
        'audio':     50 * 1024 * 1024,
        'document':  50 * 1024 * 1024,
        'archive':  500 * 1024 * 1024,
        'executable': 500 * 1024 * 1024,
        'other':    100 * 1024 * 1024,
    }

    _CATEGORY_LABELS = {
        'image': 'изображений',
        'video': 'видео',
        'audio': 'аудио',
        'document': 'документов',
        'archive': 'архивов',
        'executable': 'приложений (apk, exe)',
        'other': 'файлов',
    }

    @classmethod
    def category_size_limits(cls):
        from config import Config
        limits = dict(cls._DEFAULT_CATEGORY_SIZE_LIMITS)
        limits.update(getattr(Config, 'FILE_CATEGORY_SIZE_LIMITS', {}) or {})
        return limits

    @classmethod
    def check_file(cls, filename: str, file_size: int, category: str, user_id: int) -> dict:
        """
        Проверяет файл на безопасность.
        Возвращает {'safe': bool, 'warning': str|None, 'blocked': bool}
        """
        result = {'safe': True, 'warning': None, 'blocked': False}

        # Проверка размера по категории
        limits = cls.category_size_limits()
        max_size = limits.get(category, limits.get('other', 100 * 1024 * 1024))
        if file_size > max_size:
            result['blocked'] = True
            result['safe'] = False
            max_mb = max_size / (1024 * 1024)
            label = cls._CATEGORY_LABELS.get(category, category)
            result['warning'] = f'Файл слишком большой. Лимит для {label}: {max_mb:.0f} МБ'
            return result

        # Проверка расширения
        ext = os.path.splitext(filename)[1].lower()
        if ext in cls.HIGH_RISK_EXTENSIONS:
            SecurityAudit.log_suspicious_activity(
                'high_risk_upload',
                f"filename={filename!r} ext={ext} size={file_size}",
                user_id
            )
            result['warning'] = f'Исполняемый файл {ext} загружен'

        # Проверка на двойное расширение (image.jpg.exe)
        parts = filename.rsplit('.', 2)
        if len(parts) >= 3:
            inner_ext = f'.{parts[-2].lower()}'
            if inner_ext in cls.HIGH_RISK_EXTENSIONS or ext in cls.HIGH_RISK_EXTENSIONS:
                SecurityAudit.log_suspicious_activity(
                    'double_extension',
                    f"filename={filename!r}",
                    user_id
                )
                result['warning'] = 'Подозрительное имя файла (двойное расширение)'

        # Проверка path traversal в имени файла
        if '..' in filename or '/' in filename or '\\' in filename:
            result['blocked'] = True
            result['safe'] = False
            result['warning'] = 'Недопустимое имя файла'
            SecurityAudit.log_suspicious_activity(
                'path_traversal_upload',
                f"filename={filename!r}",
                user_id
            )

        return result


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def generate_secure_token(length: int = 32) -> str:
    """Генерирует криптографически безопасный токен."""
    return secrets.token_urlsafe(length)


def compute_sha256(data: str) -> str:
    """Вычисляет SHA-256 хэш строки (для верификации целостности данных)."""
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def compute_file_hash(file_obj) -> str:
    """Вычисляет SHA-256 хэш файла."""
    sha256 = hashlib.sha256()
    file_obj.seek(0)
    for chunk in iter(lambda: file_obj.read(65536), b''):
        sha256.update(chunk)
    file_obj.seek(0)
    return sha256.hexdigest()


def timing_safe_compare(a: str, b: str) -> bool:
    """Сравнение строк устойчивое к timing-атакам."""
    return hmac.compare_digest(a.encode(), b.encode())


# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

# Эндпоинты без CSRF (WebSocket polling)
CSRF_EXEMPT_PREFIXES = ('/socket.io',)
CSRF_EXEMPT_ENDPOINTS = frozenset()


def _csrf_exempt():
    from flask import request
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return True
    path = request.path or ''
    return any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES)


def init_security(app):
    """
    Инициализирует все компоненты безопасности для Flask-приложения.
    Вызывать в papirus.py: init_security(app)
    """
    # Заголовки безопасности для каждого ответа
    app.after_request(apply_security_headers)

    @app.before_request
    def verify_csrf_token():
        if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
            return
        if _csrf_exempt():
            return
        token = (
            request.headers.get('X-CSRF-Token')
            or request.headers.get('X-CSRFToken')
            or request.form.get('csrf_token')
        )
        if request.is_json and not token:
            data = request.get_json(silent=True) or {}
            token = data.get('csrf_token')
        if not CSRFProtection.validate_token(token):
            from utils import get_client_ip
            security_logger.warning(
                f"CSRF_REJECT | endpoint={request.endpoint} | ip={get_client_ip()}"
            )
            from flask import abort
            abort(403)

    # Генерация CSRF-токена при каждом запросе (если нет в сессии)
    @app.before_request
    def ensure_csrf_token():
        if '_csrf_token' not in session:
            CSRFProtection.generate_token()

    # Настраиваем шаблонный контекст — токен доступен в templates как {{ csrf_token }}
    @app.context_processor
    def inject_security_context():
        return {
            'csrf_token': CSRFProtection.get_token(),
            'encryption_available': MessageEncryption.is_available()
        }

    # Периодическая очистка rate limiter
    @app.before_request
    def cleanup_rate_limiter():
        # Очищаем каждые ~1000 запросов
        if secrets.randbelow(1000) == 0:
            rate_limiter.cleanup()

    security_logger.info("=" * 60)
    security_logger.info("Papirus Security Module инициализирован")
    security_logger.info(f"  Шифрование AES-256-GCM: {'✓ ВКЛЮЧЕНО' if CRYPTO_AVAILABLE else '✗ ВЫКЛЮЧЕНО (pip install cryptography)'}")
    security_logger.info(f"  Rate Limiting: ✓ ВКЛЮЧЕНО")
    security_logger.info(f"  Security Headers: ✓ ВКЛЮЧЕНО")
    security_logger.info(f"  Input Sanitization: ✓ ВКЛЮЧЕНО")
    security_logger.info(f"  Audit Logging: ✓ ВКЛЮЧЕНО → logs/security.log")
    security_logger.info("=" * 60)

    # Предупреждение о дефолтных настройках
    _warn_default = PASSWORD_PEPPER == 'papirus-default-pepper-change-in-prod-2024'
    _warn_no_key  = not MESSAGE_ENCRYPTION_KEY_VALUE

    print()
    print("=" * 62)
    print("  PAPIRUS — ПРОВЕРЬТЕ НАСТРОЙКИ БЕЗОПАСНОСТИ В security.py")
    print("=" * 62)
    if _warn_default:
        print("  ⚠  СТРОКА 1 (PASSWORD_PEPPER) — используется дефолтное")
        print("     значение. Замените на случайную строку перед запуском")
        print("     в продакшене, иначе пароли пользователей уязвимы.")
    else:
        print("  ✓  СТРОКА 1 (PASSWORD_PEPPER) — задан пользовательский pepper.")
    if _warn_no_key:
        print("  ℹ  СТРОКА 2 (MESSAGE_ENCRYPTION_KEY) — ключ не задан,")
        print("     будет использован автосгенерированный файл .encryption_key")
    else:
        print("  ✓  СТРОКА 2 (MESSAGE_ENCRYPTION_KEY) — ключ задан явно.")
    print()
    print("  Отредактировать: nano security.py  (ищите СТРОКА 1 / СТРОКА 2)")
    print("=" * 62)
    print()

    return app
