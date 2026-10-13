"""Проверка доступа к загруженным файлам и безопасное разрешение путей."""

import os
import ipaddress
import socket
from urllib.parse import urlparse

from models import User, Group, Chat, GroupMember, Message, BlockedUser, Contact, UserPrivacy


# MIME-типы, которые нельзя отдавать inline (XSS)
FORCE_ATTACHMENT_MIMES = frozenset({
    'text/html', 'image/svg+xml', 'application/xhtml+xml',
    'text/xml', 'application/xml', 'application/javascript',
    'text/javascript',
})

BLOCKED_UPLOAD_EXTENSIONS = frozenset({
    '.html', '.htm', '.svg', '.xhtml', '.shtml',
    '.php', '.php3', '.php4', '.php5', '.phtml',
    '.asp', '.aspx', '.jsp', '.cgi',
})


def normalize_upload_relpath(filepath: str) -> str | None:
    """Нормализует относительный путь внутри UPLOAD_FOLDER; блокирует traversal."""
    if not filepath:
        return None
    filepath = filepath.replace('\\', '/').strip('/')
    parts = []
    for part in filepath.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            return None
        parts.append(part)
    return '/'.join(parts) if parts else None


def upload_base_dirs(app) -> list[str]:
    """Корневые каталоги загрузок (новый + legacy static/uploads)."""
    bases = [
        os.path.realpath(os.path.join(app.root_path, app.config['UPLOAD_FOLDER'])),
    ]
    legacy = os.path.realpath(os.path.join(app.root_path, 'static', 'uploads'))
    if legacy not in bases:
        bases.append(legacy)
    return bases


def resolve_upload_file(app, relative_path: str) -> tuple[str, str] | None:
    """
    Возвращает (directory, filename) для send_from_directory или None.
    Проверяет, что итоговый путь остаётся внутри разрешённых каталогов.
    """
    rel = normalize_upload_relpath(relative_path)
    if not rel:
        return None

    for base in upload_base_dirs(app):
        full = os.path.realpath(os.path.join(base, rel))
        if full == base or full.startswith(base + os.sep):
            if os.path.isfile(full):
                return os.path.dirname(full), os.path.basename(full)
    return None


def _user_in_chat(user_id: int, chat: Chat) -> bool:
    if chat.is_saved:
        return chat.user1_id == user_id
    return user_id in (chat.user1_id, chat.user2_id)


def user_can_access_message(user_id: int, message: Message) -> bool:
    if not message or message.is_deleted:
        return False
    if message.chat_id:
        chat = Chat.query.get(message.chat_id)
        return bool(chat and _user_in_chat(user_id, chat))
    if message.group_id:
        return GroupMember.query.filter_by(
            group_id=message.group_id, user_id=user_id
        ).first() is not None
    return False


def user_can_access_upload(user_id: int, relative_path: str, db) -> bool:
    """Проверяет право авторизованного пользователя на файл."""
    rel = normalize_upload_relpath(relative_path)
    if not rel:
        return False

    if User.query.filter(User.avatar == rel).first():
        return True

    if Group.query.filter(Group.icon == rel).first():
        return True

    messages = Message.query.filter(
        Message.is_deleted == False,
        (Message.image_path == rel) | (Message.file_path == rel),
    ).all()
    for msg in messages:
        if user_can_access_message(user_id, msg):
            return True
    return False


def is_blocked_between(user_a_id: int, user_b_id: int) -> bool:
    return BlockedUser.query.filter(
        ((BlockedUser.blocker_id == user_a_id) & (BlockedUser.blocked_id == user_b_id))
        | ((BlockedUser.blocker_id == user_b_id) & (BlockedUser.blocked_id == user_a_id))
    ).first() is not None


def can_send_private_message(sender_id: int, receiver_id: int, chat: Chat | None) -> bool:
    """
    Можно ли отправить личное сообщение с учётом блокировки и who_can_write.
    Существующий чат (в т.ч. начатый собеседником) — разрешён.
    """
    if sender_id == receiver_id:
        return True
    if is_blocked_between(sender_id, receiver_id):
        return False
    if chat is not None:
        return True
    privacy = UserPrivacy.query.filter_by(user_id=receiver_id).first()
    if privacy and privacy.who_can_write == 'contacts':
        return Contact.query.filter_by(
            owner_id=receiver_id, contact_id=sender_id
        ).first() is not None
    return True


def is_safe_url_for_fetch(url: str) -> bool:
    """Блокирует SSRF: private/loopback/link-local и не-http(s) схемы."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = (parsed.hostname or '').strip().lower()
        if not hostname:
            return False
        if hostname in ('localhost', 'localhost.localdomain'):
            return False
        if hostname.endswith('.local') or hostname.endswith('.internal'):
            return False

        port = parsed.port
        for family, _, _, _, sockaddr in socket.getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        ):
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False
        return True
    except Exception:
        return False


def is_inline_safe_mime(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    return mime_type.lower() not in FORCE_ATTACHMENT_MIMES
