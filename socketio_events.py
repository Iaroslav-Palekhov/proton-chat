"""
WebSocket события для мессенджера (Flask-SocketIO).

Комнаты:
  chat_{chat_id}   — личный чат
  group_{group_id} — групповой чат
  user_{user_id}   — личная комната пользователя (статус, уведомления)

Устанавливаемые пакеты:
  pip install flask-socketio==5.3.6 simple-websocket
"""

from flask import request
from flask_login import current_user
from flask_socketio import SocketIO, join_room, leave_room, emit
from datetime import datetime, timezone
import threading
import time

# Активные WS-подключения пользователя (несколько вкладок / смена страницы)
_OFFLINE_GRACE_SECONDS = 5
_user_sids: dict[int, set[str]] = {}
_offline_timers: dict[int, threading.Timer] = {}
_conn_lock = threading.Lock()

from models import db, User, Chat, Group, GroupMember, Message, BlockedUser, Contact, UserPrivacy
from utils import format_file_size, get_file_category, get_file_icon, user_display_name
from security import InputSanitizer
from upload_security import can_send_private_message, user_can_access_message

socketio = SocketIO(
    cors_allowed_origins=[],
    async_mode="gevent",      # gevent правильно обрабатывает WS-апгрейд
    ping_timeout=20,          # Быстрее обнаруживаем отвалившихся (60 → 20)
    ping_interval=10,         # Чаще ping — быстрее реагируем на обрыв (25 → 10)
    max_http_buffer_size=10 * 1024 * 1024,  # 10 MB на WS-сообщение
    logger=False,
    engineio_logger=False,
)


# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

def _is_blocked(user_a: int, user_b: int) -> bool:
    return BlockedUser.query.filter(
        ((BlockedUser.blocker_id == user_a) & (BlockedUser.blocked_id == user_b)) |
        ((BlockedUser.blocker_id == user_b) & (BlockedUser.blocked_id == user_a))
    ).first() is not None


def _serialize_message(msg: Message, current_user_id: int, app) -> dict:
    """Сериализует объект Message в dict для отправки по WS."""
    from flask import url_for

    sender = msg.sender

    file_url = None
    if msg.file_path:
        file_url = url_for("download_file", filepath=msg.file_path, _external=False)
    elif msg.image_path:
        file_url = url_for("download_file", filepath=msg.image_path, _external=False)

    reply_to_data = None
    if msg.reply_to:
        r = msg.reply_to
        reply_to_data = {
            "id": r.id,
            "sender_id": r.sender_id,
            "sender_name": user_display_name(r.sender),
            "content": r.content[:100] if r.content else None,
            "has_image": bool(r.image_path),
            "has_file": bool(r.file_path),
            "file_name": r.file_name,
        }

    forwarded_from_data = None
    if msg.is_forwarded and msg.forwarded_from:
        orig = msg.forwarded_from
        forwarded_from_data = {
            "id": orig.id,
            "sender_id": orig.sender_id,
            "sender_name": user_display_name(orig),
            "show_sender": msg.show_forward_sender,
        }

    sender_role = None
    if msg.group_id:
        from models import GroupMember
        gm = GroupMember.query.filter_by(group_id=msg.group_id, user_id=msg.sender_id).first()
        sender_role = gm.role if gm else "member"

    result = {
        "id": msg.id,
        "message_type": getattr(msg, 'message_type', None) or ('file' if (msg.file_path or msg.image_path) else 'text'),
        "sender_id": msg.sender_id,
        "sender_name": user_display_name(sender),
        "sender_username": user_display_name(sender),
        "sender_role": sender_role,
        "sender_avatar": url_for(
            "serve_media",
            filepath=sender.display_avatar,
            _external=False,
        ) if sender and sender.display_avatar else "",
        "content": msg.content,
        "image_path": url_for("download_file", filepath=msg.image_path, _external=False) if msg.image_path else None,
        "file_path": file_url,
        "file_name": msg.file_name,
        "file_type": msg.file_type,
        "file_size": format_file_size(msg.file_size) if msg.file_size else None,
        "file_category": msg.file_category or (get_file_category(msg.file_name) if msg.file_name else "file"),
        "file_icon": get_file_icon(msg.file_name) if msg.file_name else "[file]",
        "timestamp": msg.timestamp.isoformat() + "Z",
        "is_read": msg.is_read,
        "read_count": msg.read_count or 0,
        "members_count": (GroupMember.query.filter_by(group_id=msg.group_id).count() - 1) if msg.group_id else 0,
        "is_edited": msg.is_edited,
        "reply_to": reply_to_data,
        "is_forwarded": msg.is_forwarded,
        "forwarded_from": forwarded_from_data,
        "show_forward_sender": msg.show_forward_sender,
        "link_url": msg.link_url,
        "link_title": msg.link_title,
        "link_description": msg.link_description,
        "link_image": msg.link_image,
    }

    return result


# ──────────────────────────────────────────────
# Учёт подключений (статус «в сети» при смене страниц / нескольких вкладках)
# ──────────────────────────────────────────────

def _cancel_offline_timer(user_id: int):
    with _conn_lock:
        timer = _offline_timers.pop(user_id, None)
    if timer:
        timer.cancel()


def _schedule_offline(user_id: int):
    with _conn_lock:
        if user_id in _offline_timers:
            return

        def _go_offline():
            with _conn_lock:
                _offline_timers.pop(user_id, None)
                if _user_sids.get(user_id):
                    return

            from flask import current_app
            app = current_app._get_current_object()
            with app.app_context():
                user = User.query.get(user_id)
                if not user or user.status != "online":
                    return
                user.status = "offline"
                user.last_seen = datetime.now(timezone.utc)
                db.session.commit()
                _broadcast_status(user_id, "offline", user.last_seen.isoformat() + "Z")

        timer = threading.Timer(_OFFLINE_GRACE_SECONDS, _go_offline)
        timer.daemon = True
        _offline_timers[user_id] = timer
        timer.start()


def _register_connection(user_id: int, sid: str) -> bool:
    """True, если это первое подключение (нужно оповестить о входе в сеть)."""
    with _conn_lock:
        conns = _user_sids.setdefault(user_id, set())
        was_empty = len(conns) == 0
        conns.add(sid)
    _cancel_offline_timer(user_id)
    return was_empty


def _unregister_connection(user_id: int, sid: str):
    with _conn_lock:
        conns = _user_sids.get(user_id)
        if not conns:
            return
        conns.discard(sid)
        if conns:
            return
        _user_sids.pop(user_id, None)
    _schedule_offline(user_id)


# ──────────────────────────────────────────────
# Подключение / отключение
# ──────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    if not current_user.is_authenticated:
        return False  # отклоняем неавторизованных

    from utils import get_active_user_ban, is_platform_admin
    if not is_platform_admin(current_user):
        if get_active_user_ban(current_user.id, db):
            return False

    was_offline = _register_connection(current_user.id, request.sid)

    # Личная комната пользователя
    join_room(f"user_{current_user.id}")

    # Обновляем статус
    current_user.status = "online"
    current_user.last_seen = datetime.now(timezone.utc)
    db.session.commit()

    if was_offline:
        _broadcast_status(current_user.id, "online")


@socketio.on("disconnect")
def on_disconnect():
    if not current_user.is_authenticated:
        return

    _unregister_connection(current_user.id, request.sid)


def _broadcast_status(user_id: int, status: str, last_seen: str = None):
    """Рассылает обновление статуса всем личным чатам пользователя с учётом приватности."""
    # Получаем настройки приватности пользователя
    privacy = UserPrivacy.query.filter_by(user_id=user_id).first()
    last_seen_setting = privacy.last_seen if privacy else 'all'

    # Список id контактов пользователя (нужен для режима 'contacts')
    if last_seen_setting == 'contacts':
        contact_ids = {
            c.contact_id for c in Contact.query.filter_by(owner_id=user_id).all()
        }

    chats = Chat.query.filter(
        (Chat.user1_id == user_id) | (Chat.user2_id == user_id)
    ).all()

    for chat in chats:
        other_id = chat.user2_id if chat.user1_id == user_id else chat.user1_id

        # Определяем что отправить собеседнику в зависимости от настройки
        if last_seen_setting == 'all':
            # Всё как раньше — статус и точное время
            payload = {"user_id": user_id, "status": status}
            if last_seen:
                payload["last_seen"] = last_seen

        elif last_seen_setting == 'contacts':
            # Только контакты видят точное время; остальные — «был(а) недавно»
            payload = {"user_id": user_id, "status": status}
            if other_id in contact_ids:
                if last_seen:
                    payload["last_seen"] = last_seen
            else:
                # Если уходит офлайн — скрываем точное время
                if status == 'offline':
                    payload["last_seen"] = "недавно"

        else:  # nobody
            # Никто не видит точное время; онлайн-статус показываем только пока online
            if status == 'online':
                payload = {"user_id": user_id, "status": "online"}
            else:
                # Уходя офлайн отправляем «недавно» вместо точного времени
                payload = {"user_id": user_id, "status": "offline", "last_seen": "недавно"}

        socketio.emit("user_status", payload, to=f"user_{other_id}")


# ──────────────────────────────────────────────
# Комнаты
# ──────────────────────────────────────────────

@socketio.on("join_chat")
def on_join_chat(data):
    """Клиент заходит в личный чат."""
    if not current_user.is_authenticated:
        return
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    chat = Chat.query.get(chat_id)
    if not chat:
        return
    if chat.user1_id != current_user.id and chat.user2_id != current_user.id:
        return
    join_room(f"chat_{chat_id}")


@socketio.on("leave_chat")
def on_leave_chat(data):
    if not current_user.is_authenticated:
        return
    chat_id = data.get("chat_id")
    if chat_id:
        leave_room(f"chat_{chat_id}")


@socketio.on("join_group")
def on_join_group(data):
    """Клиент заходит в групповой чат."""
    if not current_user.is_authenticated:
        return
    group_id = data.get("group_id")
    if not group_id:
        return
    membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
    if not membership:
        return
    join_room(f"group_{group_id}")


@socketio.on("leave_group")
def on_leave_group(data):
    if not current_user.is_authenticated:
        return
    group_id = data.get("group_id")
    if group_id:
        leave_room(f"group_{group_id}")


# ──────────────────────────────────────────────
# Отправка сообщения
# ──────────────────────────────────────────────

@socketio.on("send_message")
def on_send_message(data):
    """
    Обрабатывает ТОЛЬКО текстовые сообщения через WS.
    Файлы по-прежнему отправляются через HTTP POST /send_message
    (multipart/form-data нельзя передать через WS).
    """
    if not current_user.is_authenticated:
        return

    chat_id = data.get("chat_id")
    group_id = data.get("group_id")
    content = InputSanitizer.sanitize_text((data.get("content") or "").strip())
    reply_to_id = data.get("reply_to_id")

    if not content:
        return

    msg = Message(
        sender_id=current_user.id,
        content=content,
        reply_to_id=reply_to_id or None,
    )

    if chat_id:
        chat = Chat.query.get(chat_id)
        if not chat:
            return
        if chat.user1_id != current_user.id and chat.user2_id != current_user.id:
            return
        if chat.is_saved:
            if chat.user1_id != current_user.id:
                return
            msg.chat_id = chat_id
            msg.receiver_id = current_user.id
            msg.is_read = True
            chat.last_message_at = datetime.now(timezone.utc)
        else:
            from utils import get_proton_bot_user
            bot = get_proton_bot_user(db)
            if bot and bot.id in (chat.user1_id, chat.user2_id):
                emit("error", {"message": "proton_readonly"})
                return
            receiver_id = chat.user2_id if chat.user1_id == current_user.id else chat.user1_id
            if _is_blocked(current_user.id, receiver_id):
                emit("error", {"message": "blocked"})
                return
            if not can_send_private_message(current_user.id, receiver_id, chat):
                emit("error", {"message": "write_restricted"})
                return
            msg.chat_id = chat_id
            msg.receiver_id = receiver_id
            chat.last_message_at = datetime.now(timezone.utc)

    elif group_id:
        group = Group.query.get(group_id)
        if not group:
            return
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership:
            return
        # Проверка прав на запись
        if getattr(group, 'write_permission', 'all') == 'admins_only':
            if membership.role not in ['owner', 'admin']:
                emit("error", {"message": "write_restricted"})
                return
        msg.group_id = group_id
        group.last_message_at = datetime.now(timezone.utc)
    else:
        return

    if reply_to_id:
        try:
            reply_msg = Message.query.get(int(reply_to_id))
        except (TypeError, ValueError):
            reply_msg = None
        if not reply_msg or not user_can_access_message(current_user.id, reply_msg):
            emit("error", {"message": "invalid_reply"})
            return

    db.session.add(msg)
    db.session.commit()

    from flask import current_app
    _app = current_app._get_current_object()
    payload = _serialize_message(msg, current_user.id, _app)

    if chat_id:
        # Рассылаем обоим участникам чата
        socketio.emit("new_message", payload, to=f"chat_{chat_id}")
        # Уведомление в личную комнату получателя (для списка чатов)
        _emit_chat_update(chat_id, msg, receiver_id)
    elif group_id:
        socketio.emit("new_message", payload, to=f"group_{group_id}")
        _emit_group_update(group_id, msg)

    # Запустить получение превью ссылки в фоне (через HTTP threading)
    _schedule_link_preview(msg.id, content, chat_id, group_id, _app)


def _emit_chat_update(chat_id: int, msg: Message, receiver_id: int):
    """Отправляет обновление списка чатов получателю."""
    from flask import url_for
    sender = msg.sender
    preview = msg.content[:30] if msg.content else ("[Фото]" if msg.image_path else "[Файл]")
    payload = {
        "chat_id": chat_id,
        "type": "private",
        "sender_id": msg.sender_id,
        "preview": preview,
        "timestamp": msg.timestamp.isoformat() + "Z",
    }
    socketio.emit("chat_updated", payload, to=f"user_{receiver_id}")
    # Себе тоже (чтобы список чатов обновился на другой вкладке)
    socketio.emit("chat_updated", payload, to=f"user_{msg.sender_id}")




def _emit_group_update(group_id: int, msg: Message):
    """Отправляет обновление группового чата всем участникам."""
    members = GroupMember.query.filter_by(group_id=group_id).all()
    preview = msg.content[:30] if msg.content else ("[Фото]" if msg.image_path else "[Файл]")
    payload = {
        "group_id": group_id,
        "type": "group",
        "sender_id": msg.sender_id,
        "sender_name": msg.sender.username if msg.sender else "Unknown",
        "preview": preview,
        "timestamp": msg.timestamp.isoformat() + "Z",
    }
    for m in members:
        socketio.emit("chat_updated", payload, to=f"user_{m.user_id}")




def _schedule_link_preview(msg_id: int, content: str, chat_id, group_id, app):
    """Запускает парсинг превью ссылки в фоне и рассылает результат по WS."""
    import threading
    from utils import contains_url, extract_urls_from_text, extract_link_preview

    if not contains_url(content):
        return

    urls = extract_urls_from_text(content)
    if not urls:
        return

    def _worker():
        with app.app_context():
            try:
                preview = extract_link_preview(urls[0])
                if not preview:
                    return
                msg = Message.query.get(msg_id)
                if not msg:
                    return
                msg.link_url = preview["url"]
                msg.link_title = preview["title"]
                msg.link_description = preview["description"]
                msg.link_image = preview["image"]
                msg.link_fetched_at = datetime.now(timezone.utc)
                db.session.commit()
                # Рассылаем превью в нужную комнату
                payload = {
                    "message_id": msg_id,
                    "link_url": preview["url"],
                    "link_title": preview["title"],
                    "link_description": preview["description"],
                    "link_image": preview["image"],
                }
                if chat_id:
                    socketio.emit("link_preview_ready", payload, to=f"chat_{chat_id}")
                elif group_id:
                    socketio.emit("link_preview_ready", payload, to=f"group_{group_id}")
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


# ──────────────────────────────────────────────
# Статус «печатает»
# ──────────────────────────────────────────────

# Серверные таймеры авто-стопа: ключ = (user_id, chat_id/group_id, тип)
_typing_timers: dict = {}
_typing_timers_lock = threading.Lock()


def _stop_typing_emit(user_id: int, username: str, chat_id=None, group_id=None):
    """Рассылает stop_typing и убирает таймер из словаря."""
    key = (user_id, chat_id or group_id, 'chat' if chat_id else 'group')
    with _typing_timers_lock:
        _typing_timers.pop(key, None)
    payload = {"user_id": user_id, "username": username}
    if chat_id:
        payload["chat_id"] = chat_id
        socketio.emit("stop_typing", payload, to=f"chat_{chat_id}")
    elif group_id:
        payload["group_id"] = group_id
        socketio.emit("stop_typing", payload, to=f"group_{group_id}")


def _schedule_stop_typing(user_id: int, username: str, chat_id=None, group_id=None, delay: float = 4.0):
    """Перезапускает серверный таймер авто-стопа (4 сек — чуть больше чем 3 сек на клиенте)."""
    key = (user_id, chat_id or group_id, 'chat' if chat_id else 'group')
    with _typing_timers_lock:
        old = _typing_timers.get(key)
        if old:
            old.cancel()
        t = threading.Timer(delay, _stop_typing_emit, args=[user_id, username, chat_id, group_id])
        t.daemon = True
        _typing_timers[key] = t
    t.start()


@socketio.on("typing")
def on_typing(data):
    if not current_user.is_authenticated:
        return
    chat_id  = data.get("chat_id")
    group_id = data.get("group_id")
    uid      = current_user.id
    uname    = current_user.username

    if chat_id:
        chat = Chat.query.get(chat_id)
        if not chat or (chat.user1_id != uid and chat.user2_id != uid):
            return
        socketio.emit("typing", {"user_id": uid, "username": uname, "chat_id": chat_id},
                      to=f"chat_{chat_id}", include_self=False)
        _schedule_stop_typing(uid, uname, chat_id=chat_id)

    elif group_id:
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=uid).first()
        if not membership:
            return
        socketio.emit("typing", {"user_id": uid, "username": uname, "group_id": group_id},
                      to=f"group_{group_id}", include_self=False)
        _schedule_stop_typing(uid, uname, group_id=group_id)


@socketio.on("stop_typing")
def on_stop_typing(data):
    """Клиент явно сообщает что перестал печатать (отправил, очистил поле)."""
    if not current_user.is_authenticated:
        return
    chat_id  = data.get("chat_id")
    group_id = data.get("group_id")
    uid      = current_user.id
    uname    = current_user.username

    # Отменяем серверный таймер — клиент сам сказал «стоп»
    key = (uid, chat_id or group_id, 'chat' if chat_id else 'group')
    with _typing_timers_lock:
        old = _typing_timers.pop(key, None)
        if old:
            old.cancel()

    payload = {"user_id": uid, "username": uname}
    if chat_id:
        payload["chat_id"] = chat_id
        socketio.emit("stop_typing", payload, to=f"chat_{chat_id}", include_self=False)
    elif group_id:
        payload["group_id"] = group_id
        socketio.emit("stop_typing", payload, to=f"group_{group_id}", include_self=False)


# ──────────────────────────────────────────────
# Прочтение сообщений
# ──────────────────────────────────────────────

@socketio.on("messages_read")
def on_messages_read(data):
    """Клиент сообщает, что прочитал сообщения в чате или группе."""
    if not current_user.is_authenticated:
        return
    chat_id  = data.get("chat_id")
    group_id = data.get("group_id")

    if chat_id:
        chat = Chat.query.get(chat_id)
        if not chat or (chat.user1_id != current_user.id and chat.user2_id != current_user.id):
            return
        unread = Message.query.filter_by(
            chat_id=chat_id,
            receiver_id=current_user.id,
            is_read=False,
        ).all()
        sender_ids = set()
        for m in unread:
            m.is_read = True
            sender_ids.add(m.sender_id)
        db.session.commit()
        for sid in sender_ids:
            socketio.emit("messages_read", {"chat_id": chat_id, "reader_id": current_user.id}, to=f"user_{sid}")

    elif group_id:
        membership = GroupMember.query.filter_by(
            group_id=group_id, user_id=current_user.id
        ).first()
        if not membership:
            return
        total_members = GroupMember.query.filter_by(group_id=group_id).count()
        readers_count = max(total_members - 1, 1)

        from read_receipts import mark_group_messages_read
        updated_msgs = mark_group_messages_read(group_id, current_user.id, readers_count)

        if updated_msgs:
            db.session.commit()
            sender_ids = set()
            for info in updated_msgs:
                m = Message.query.get(info['id'])
                if m:
                    sender_ids.add(m.sender_id)
            for sid in sender_ids:
                socketio.emit("messages_read", {
                    "group_id": group_id,
                    "reader_id": current_user.id,
                    "messages": updated_msgs,
                }, to=f"user_{sid}")


# ──────────────────────────────────────────────
# Удаление / редактирование (уведомления)
# ──────────────────────────────────────────────

def broadcast_message_deleted(message_id: int, chat_id: int = None, group_id: int = None):
    payload = {"message_id": message_id}
    if chat_id:
        socketio.emit("message_deleted", payload, to=f"chat_{chat_id}")
    elif group_id:
        socketio.emit("message_deleted", payload, to=f"group_{group_id}")


def broadcast_message_edited(message_id: int, new_content: str, chat_id: int = None, group_id: int = None):
    payload = {"message_id": message_id, "content": new_content}
    if chat_id:
        socketio.emit("message_edited", payload, to=f"chat_{chat_id}")
    elif group_id:
        socketio.emit("message_edited", payload, to=f"group_{group_id}")


def broadcast_chat_deleted(chat_id: int, user1_id: int, user2_id: int):
    """Оповещает обоих участников чата об удалении через их личные комнаты."""
    payload = {"chat_id": chat_id}
    socketio.emit("chat_deleted", payload, to=f"user_{user1_id}")
    socketio.emit("chat_deleted", payload, to=f"user_{user2_id}")


# ──────────────────────────────────────────────
# Удаление сообщения через WebSocket
# ──────────────────────────────────────────────

@socketio.on("delete_message")
def on_delete_message(data):
    """Удаляет сообщение и рассылает уведомление всем участникам комнаты."""
    if not current_user.is_authenticated:
        return
    message_id = data.get("message_id")
    if not message_id:
        return

    from models import Message, GroupMember
    from utils import can_delete_group_message
    msg = Message.query.get(message_id)
    if not msg:
        emit("error", {"message": "Сообщение не найдено"})
        return
    if not can_delete_group_message(msg, current_user.id, GroupMember):
        emit("error", {"message": "Нет прав для удаления"})
        return

    chat_id = msg.chat_id
    group_id = msg.group_id

    # Удаляем файл/изображение с диска только если больше нет сообщений с этим файлом
    import os
    from flask import current_app
    from models import Message as _Msg
    for fpath in [msg.image_path, msg.file_path]:
        if fpath:
            # Считаем сколько НЕудалённых сообщений (кроме текущего) используют этот файл
            refs = _Msg.query.filter(
                (_Msg.image_path == fpath) | (_Msg.file_path == fpath),
                _Msg.is_deleted == False,
                _Msg.id != msg.id
            ).count()
            if refs == 0:
                full_path = os.path.join(current_app.root_path, current_app.config['UPLOAD_FOLDER'], fpath)
                if os.path.exists(full_path):
                    try:
                        os.remove(full_path)
                    except Exception:
                        pass

    msg.is_deleted = True
    msg.content = None

    removed_saved_chat = False
    if chat_id:
        from models import Chat
        chat = Chat.query.get(chat_id)
        if chat and chat.is_saved:
            from utils import cleanup_empty_saved_chat
            removed_saved_chat = cleanup_empty_saved_chat(chat, db)

    db.session.commit()

    payload = {"message_id": message_id, "saved_chat_removed": removed_saved_chat}
    if chat_id:
        socketio.emit("message_deleted", payload, to=f"chat_{chat_id}")
        if removed_saved_chat:
            broadcast_chat_deleted(chat_id, current_user.id, current_user.id)
    elif group_id:
        socketio.emit("message_deleted", payload, to=f"group_{group_id}")


# ──────────────────────────────────────────────
# Редактирование сообщения через WebSocket
# ──────────────────────────────────────────────

@socketio.on("edit_message")
def on_edit_message(data):
    """Редактирует сообщение и рассылает обновление всем участникам комнаты."""
    if not current_user.is_authenticated:
        return
    message_id = data.get("message_id")
    new_content = InputSanitizer.sanitize_text((data.get("content") or "").strip())
    if not message_id or not new_content:
        return

    from models import Message
    msg = Message.query.get(message_id)
    if not msg:
        emit("error", {"message": "Сообщение не найдено"})
        return
    if msg.sender_id != current_user.id:
        emit("error", {"message": "Нет прав для редактирования"})
        return

    chat_id = msg.chat_id
    group_id = msg.group_id
    msg.content = new_content
    msg.is_edited = True
    db.session.commit()

    payload = {"message_id": message_id, "content": new_content}
    if chat_id:
        socketio.emit("message_edited", payload, to=f"chat_{chat_id}")
    elif group_id:
        socketio.emit("message_edited", payload, to=f"group_{group_id}")


# ──────────────────────────────────────────────
# Закрепление / открепление через WebSocket
# ──────────────────────────────────────────────

@socketio.on("pin_message")
def on_pin_message(data):
    """Закрепляет сообщение и рассылает событие всем участникам комнаты."""
    if not current_user.is_authenticated:
        return
    message_id = data.get("message_id")
    if not message_id:
        return

    from models import Message, GroupMember, Chat
    msg = Message.query.get(message_id)
    if not msg:
        emit("error", {"message": "Сообщение не найдено"})
        return

    if msg.chat_id:
        chat_obj = Chat.query.get(msg.chat_id)
        if not chat_obj or (chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id):
            emit("error", {"message": "Нет доступа"})
            return
    elif msg.group_id:
        membership = GroupMember.query.filter_by(group_id=msg.group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ["owner", "admin"]:
            emit("error", {"message": "Только администраторы могут закреплять сообщения"})
            return

    msg.is_pinned = True
    msg.pinned_by_id = current_user.id
    msg.pinned_at = datetime.now(timezone.utc)
    db.session.commit()

    sender = msg.sender
    payload = {
        "message_id": msg.id,
        "content": msg.content,
        "sender_name": sender.username if sender else "Unknown",
        "timestamp": msg.timestamp.isoformat() + "Z",
        "has_image": bool(msg.image_path),
        "has_file": bool(msg.file_path),
        "file_name": msg.file_name,
        "action": "pinned",
    }
    if msg.chat_id:
        socketio.emit("pin_updated", payload, to=f"chat_{msg.chat_id}")
    elif msg.group_id:
        socketio.emit("pin_updated", payload, to=f"group_{msg.group_id}")


@socketio.on("unpin_message")
def on_unpin_message(data):
    """Открепляет сообщение и рассылает событие всем участникам комнаты."""
    if not current_user.is_authenticated:
        return
    message_id = data.get("message_id")
    if not message_id:
        return

    from models import Message, GroupMember, Chat
    msg = Message.query.get(message_id)
    if not msg:
        emit("error", {"message": "Сообщение не найдено"})
        return

    if msg.chat_id:
        chat_obj = Chat.query.get(msg.chat_id)
        if not chat_obj or (chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id):
            emit("error", {"message": "Нет доступа"})
            return
    elif msg.group_id:
        membership = GroupMember.query.filter_by(group_id=msg.group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ["owner", "admin"]:
            emit("error", {"message": "Только администраторы могут откреплять сообщения"})
            return

    msg.is_pinned = False
    msg.pinned_by_id = None
    msg.pinned_at = None
    db.session.commit()

    payload = {"message_id": message_id, "action": "unpinned"}
    if msg.chat_id:
        socketio.emit("pin_updated", payload, to=f"chat_{msg.chat_id}")
    elif msg.group_id:
        socketio.emit("pin_updated", payload, to=f"group_{msg.group_id}")


# ──────────────────────────────────────────────
# Heartbeat
# ──────────────────────────────────────────────

@socketio.on("heartbeat")
def on_heartbeat():
    if not current_user.is_authenticated:
        return
    _cancel_offline_timer(current_user.id)
    with _conn_lock:
        conns = _user_sids.get(current_user.id)
        if conns is not None:
            conns.add(request.sid)
    current_user.status = "online"
    current_user.last_seen = datetime.now(timezone.utc)
    db.session.commit()


# ──────────────────────────────────────────────
# Запрос пропущенных сообщений (при reconnect)
# ──────────────────────────────────────────────

@socketio.on("request_missed_messages")
def on_request_missed_messages(data):
    """
    Клиент запрашивает пропущенные сообщения после переподключения.
    Вместо HTTP-fetch — через WebSocket.
    data: { chat_id или group_id, last_id }
    """
    if not current_user.is_authenticated:
        return

    from flask import current_app
    _app = current_app._get_current_object()

    chat_id = data.get("chat_id")
    group_id = data.get("group_id")
    last_id = int(data.get("last_id") or 0)

    if chat_id:
        chat = Chat.query.get(chat_id)
        if not chat:
            return
        if chat.user1_id != current_user.id and chat.user2_id != current_user.id:
            return
        messages = Message.query.filter(
            Message.chat_id == chat_id,
            Message.id > last_id
        ).order_by(Message.id.asc()).limit(50).all()
    elif group_id:
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership:
            return
        messages = Message.query.filter(
            Message.group_id == group_id,
            Message.id > last_id
        ).order_by(Message.id.asc()).limit(50).all()
    else:
        return

    serialized = [_serialize_message(m, current_user.id, _app) for m in messages]
    emit("missed_messages", {"messages": serialized})