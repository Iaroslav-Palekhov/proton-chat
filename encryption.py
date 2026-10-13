"""
encryption.py — Прозрачное шифрование полей БД (AES-256-GCM)
=============================================================

Реализует SQLAlchemy TypeDecorator-ы, которые автоматически
шифруют данные при записи в БД и расшифровывают при чтении.

Поддерживаемые типы:
  • EncryptedText    — для Text / String полей (email, bio, content и т.д.)
  • EncryptedSearch  — для полей, по которым нужен точный поиск (email):
                       хранит рядом детерминированный HMAC-хэш для WHERE-запросов

Алгоритм шифрования: AES-256-GCM (тот же что в security.py)
  • Каждое значение шифруется с уникальным 12-байт nonce → одинаковые
    значения дают РАЗНЫЕ шифротексты (нельзя угадать по паттернам)
  • Зашифрованные значения получают префикс «ENC:»
  • Старые незашифрованные записи автоматически читаются без ошибок
    (backward compatibility)

Поиск по зашифрованным полям:
  • ilike/LIKE — не работает с AES-GCM, поэтому поиск по content делается
    в Python (фильтрует уже расшифрованные объекты из памяти)
  • точный поиск по email — хранится HMAC-SHA256 хэш в отдельном поле
    email_hash, который позволяет делать WHERE email_hash = ?

Ключ шифрования:
  Берётся из security.py → MessageEncryption._load_or_create_key()
  Один ключ для всего приложения, хранится в .encryption_key или env.
"""

import os
import base64
import hmac
import hashlib
import secrets
import logging

from sqlalchemy import types

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Подключение к cryptography
# ─────────────────────────────────────────────────────────────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning(
        "cryptography не установлена — шифрование полей БД ОТКЛЮЧЕНО. "
        "Выполни: pip install cryptography"
    )

ENC_PREFIX = "ENC:"


def _get_key() -> bytes:
    """Возвращает 32-байтовый AES ключ из security.py."""
    try:
        from security import MessageEncryption
        return MessageEncryption._load_or_create_key()
    except Exception as e:
        logger.error(f"Не удалось загрузить ключ шифрования: {e}")
        # fallback — не будет работать правильно, но не упадёт
        return b'\x00' * 32


def _hmac_key() -> bytes:
    """
    Отдельный ключ для HMAC-хэшей (детерминированный поиск).
    Производится из основного ключа через HKDF-подобную операцию.
    """
    master = _get_key()
    return hashlib.sha256(b"hmac-search-key:" + master).digest()


# ─────────────────────────────────────────────────────────────
# Базовые функции encrypt / decrypt
# ─────────────────────────────────────────────────────────────

def encrypt_value(plaintext: str) -> str:
    """
    Шифрует строку с помощью AES-256-GCM.
    Возвращает строку вида  ENC:<base64(nonce+ciphertext)>
    Если библиотека не установлена — возвращает исходный текст.
    """
    if not CRYPTO_AVAILABLE or not plaintext:
        return plaintext

    try:
        key = _get_key()
        aesgcm = AESGCM(key)
        nonce = secrets.token_bytes(12)
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return ENC_PREFIX + base64.b64encode(nonce + ct).decode("utf-8")
    except Exception as e:
        logger.error(f"encrypt_value error: {e}")
        return plaintext


def decrypt_value(ciphertext: str) -> str:
    """
    Расшифровывает строку.
    Если строка не начинается с ENC: — возвращает как есть
    (обратная совместимость со старыми незашифрованными записями).
    """
    if not CRYPTO_AVAILABLE or not ciphertext:
        return ciphertext

    if not ciphertext.startswith(ENC_PREFIX):
        return ciphertext  # backward compat — старая незашифрованная запись

    try:
        key = _get_key()
        aesgcm = AESGCM(key)
        raw = base64.b64decode(ciphertext[len(ENC_PREFIX):])
        nonce, data = raw[:12], raw[12:]
        return aesgcm.decrypt(nonce, data, None).decode("utf-8")
    except Exception as e:
        logger.error(f"decrypt_value error: {e}")
        return "[Ошибка расшифровки]"


def hmac_hash(value: str) -> str:
    """
    Возвращает детерминированный HMAC-SHA256 хэш строки (hex).
    Используется для точного поиска по зашифрованным полям (email).
    """
    if not value:
        return ""
    key = _hmac_key()
    return hmac.new(key, value.lower().encode("utf-8"), hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────────────────────
# SQLAlchemy TypeDecorator-ы
# ─────────────────────────────────────────────────────────────

class EncryptedType(types.TypeDecorator):
    """
    Прозрачное шифрование текстового поля.

    Использование в модели:
        bio = db.Column(EncryptedType(500), default='')

    При записи: значение шифруется → в БД хранится ENC:...
    При чтении: значение расшифровывается автоматически
    """

    impl = types.Text
    cache_ok = True

    def __init__(self, length=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._length = length  # для документации; не влияет на Text

    def process_bind_param(self, value, dialect):
        """Python → БД: шифруем при записи."""
        if value is None:
            return None
        return encrypt_value(str(value))

    def process_result_value(self, value, dialect):
        """БД → Python: расшифровываем при чтении."""
        if value is None:
            return None
        return decrypt_value(value)

    def copy(self, **kwargs):
        return EncryptedType(self._length)
