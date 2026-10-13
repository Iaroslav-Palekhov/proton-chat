from flask import render_template, request, jsonify, redirect, url_for, send_from_directory, session, abort
from flask_login import login_user, login_required, logout_user, current_user
from security import PasswordSecurity, rate_limit, InputSanitizer, FileSecurityChecker
from upload_security import (
    resolve_upload_file, user_can_access_upload, user_can_access_message,
    can_send_private_message, is_inline_safe_mime, normalize_upload_relpath,
)
from encryption import hmac_hash
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import joinedload
from sqlalchemy import func
from sqlalchemy import text as sa_text
import os
import mimetypes
import secrets
import threading
import zipfile
import io
import shutil

from config import Config
from models import User, Group, GroupMember, GroupJoinRequest, GroupBan, Chat, Message, ForwardedMessage, PasswordReset, UserSession, BlockedUser, Contact, UserPrivacy, MessageReaction, UserBan, MessageReport
from socketio_events import socketio
from utils import (
    compress_image, get_file_category, get_file_icon,
    format_file_size, is_file_too_large, save_file, allowed_file,
    extract_link_preview, contains_url, extract_urls_from_text,
    generate_avatar_file, delete_uploaded_file,
    is_custom_upload, should_regenerate_avatar,
    can_delete_group_message, get_member_role, user_display_name, delete_user_account,
    is_platform_admin, ensure_admin_privacy, get_active_user_ban, terminate_user_sessions,
    BAN_PERIODS, apply_platform_ban, is_platform_bot, get_proton_bot_user,
    ensure_proton_chat_for_user, send_proton_broadcast, ensure_proton_bot_user,
    get_or_create_proton_chat,
    get_or_create_saved_chat, get_saved_chat, saved_chat_has_messages,
    cleanup_empty_saved_chat, SAVED_CHAT_NAME,
)

def _format_chat_time(dt):
    """Форматирует время последнего сообщения для списка чатов.
    Сегодня → 'HH:MM', вчера → 'Вчера', в этом году → 'дд мес', старше → 'дд.мм.гг'
    """
    if not dt:
        return ''
    # Убеждаемся что dt timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    # Сравниваем по календарным дням (UTC)
    today = now.date()
    msg_day = dt.date()
    diff_days = (today - msg_day).days
    if diff_days == 0:
        return dt.strftime('%H:%M')
    elif diff_days == 1:
        return 'Вчера'
    elif diff_days < 365:
        months = ['янв', 'фев', 'мар', 'апр', 'май', 'июн',
                  'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
        return f"{dt.day} {months[dt.month - 1]}"
    else:
        return dt.strftime('%d.%m.%y')


def register_routes(app, db, login_manager):

    @app.context_processor
    def inject_avatar_helpers():
        bot = get_proton_bot_user(db)
        def uploads_url(path):
            if not path:
                return ''
            return url_for('serve_media', filepath=path)
        return {
            'is_custom_upload': is_custom_upload,
            'is_platform_admin': is_platform_admin,
            'proton_bot_id': bot.id if bot else None,
            'uploads_url': uploads_url,
            'open_registration': app.config.get('OPEN_REGISTRATION', True),
        }

    @app.before_request
    def block_public_upload_directory():
        """Запрет прямого доступа к /static/uploads/ (IDOR)."""
        if request.path.startswith('/static/uploads/'):
            abort(404)

    @app.before_request
    def check_platform_ban():
        """Забаненных пользователей перенаправляем на /banb."""
        if not current_user.is_authenticated:
            return
        if is_platform_admin(current_user):
            return
        if request.endpoint in (None, 'static', 'ban_page', 'logout', 'login', 'register'):
            return
        if request.path.startswith('/static/'):
            return
        ban = get_active_user_ban(current_user.id, db)
        if ban:
            return redirect(url_for('ban_page'))

    @app.after_request
    def add_cache_headers(response):
        """Кэшируем статику на 30 дней, HTML не кэшируем."""
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
        elif response.content_type and 'text/html' in response.content_type:
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

    # ============================================================
    # ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ СЕССИЙ
    # ============================================================

    def _parse_user_agent(ua_string):
        """Разбирает User-Agent строку в читаемый вид."""
        try:
            import user_agents as ua_lib
            ua = ua_lib.parse(ua_string)
            browser  = f"{ua.browser.family} {ua.browser.version_string}".strip()
            os_info  = f"{ua.os.family} {ua.os.version_string}".strip()
            if ua.is_mobile:
                device_type = 'mobile'
            elif ua.is_tablet:
                device_type = 'tablet'
            else:
                device_type = 'desktop'
            return browser, os_info, device_type
        except Exception:
            short_ua = (ua_string[:80] if ua_string else 'Неизвестно')
            return short_ua, 'Неизвестно', 'desktop'

    def _get_client_ip():
        from utils import get_client_ip
        return get_client_ip()

    def _create_session_record(user_id, session_token, is_current=False):
        """Создаёт запись о новой сессии в БД."""
        ua_string = request.user_agent.string or ''
        browser, os_info, device_type = _parse_user_agent(ua_string)

        sess = UserSession(
            user_id=user_id,
            session_token=session_token,
            ip_address=_get_client_ip(),
            user_agent=ua_string[:500],
            browser=browser[:100],
            os=os_info[:100],
            device_type=device_type,
            is_active=True,
            is_current=is_current
        )
        db.session.add(sess)
        db.session.commit()
        return sess

    def _current_session_age():
        """Возвращает возраст текущей сессии в секундах. None если сессия не найдена."""
        current_token = session.get('session_token')
        if not current_token:
            return None
        sess = UserSession.query.filter_by(
            session_token=current_token,
            user_id=current_user.id
        ).first()
        if not sess:
            return None
        ca = sess.created_at if sess.created_at.tzinfo else sess.created_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ca).total_seconds()

    def _is_blocked_between(user_a_id, user_b_id):
        return BlockedUser.query.filter(
            ((BlockedUser.blocker_id == user_a_id) & (BlockedUser.blocked_id == user_b_id)) |
            ((BlockedUser.blocker_id == user_b_id) & (BlockedUser.blocked_id == user_a_id))
        ).first() is not None

    def _get_status_display(target_user, viewer_id):
        """Возвращает статус target_user с учётом его настроек приватности."""
        if target_user.id == viewer_id:
            return target_user.status
        privacy = UserPrivacy.query.filter_by(user_id=target_user.id).first()
        setting = privacy.last_seen if privacy else 'all'
        if setting == 'nobody':
            return target_user.status if target_user.status == 'online' else 'offline'
        if setting == 'contacts':
            i_am_contact = Contact.query.filter_by(owner_id=target_user.id, contact_id=viewer_id).first() is not None
            if not i_am_contact:
                return target_user.status if target_user.status == 'online' else 'offline'
        return target_user.status

    def _get_last_seen_display(target_user, viewer_id):
        """Возвращает ISO UTC строку last_seen с учётом приватности. None или 'недавно'/'давно' если не показывать."""
        if target_user.id == viewer_id:
            return target_user.last_seen.isoformat() + 'Z' if target_user.last_seen else None
        if target_user.status == 'online':
            return None
        privacy = UserPrivacy.query.filter_by(user_id=target_user.id).first()
        setting = privacy.last_seen if privacy else 'all'
        if setting == 'nobody':
            return 'недавно'
        if setting == 'contacts':
            i_am_contact = Contact.query.filter_by(owner_id=target_user.id, contact_id=viewer_id).first() is not None
            if not i_am_contact:
                return 'недавно'
        return target_user.last_seen.isoformat() + 'Z' if target_user.last_seen else 'давно'

    def _user_can_access_message(user_id, message):
        """Пользователь видит сообщение в своём чате или группе."""
        if not message or message.is_deleted:
            return False
        if message.chat_id:
            chat = Chat.query.get(message.chat_id)
            if not chat:
                return False
            if chat.is_saved:
                return chat.user1_id == user_id
            return user_id in (chat.user1_id, chat.user2_id)
        if message.group_id:
            return GroupMember.query.filter_by(group_id=message.group_id, user_id=user_id).first() is not None
        return False

    def _message_preview(message, max_len=200):
        if message.image_path:
            return '📷 Фото'
        if message.file_path:
            return '📎 ' + (message.file_name or 'Файл')
        text = (message.content or '').strip()
        if not text:
            return '(пустое сообщение)'
        return text if len(text) <= max_len else text[:max_len] + '…'

    def _message_report_context(message):
        if message.chat_id:
            chat = Chat.query.get(message.chat_id)
            if chat:
                return 'Личный чат', url_for('chat', chat_id=chat.id)
        if message.group_id:
            group = Group.query.get(message.group_id)
            if group:
                return f'Группа: {group.name}', url_for('group_chat', group_id=group.id)
        return 'Сообщение', None

    # ============================================================
    # ИНДЕКС
    # ============================================================

    @app.route('/')
    def index():
        if current_user.is_authenticated:
            return redirect(url_for('chats'))
        return redirect(url_for('login'))

    # ============================================================
    # АВТОРИЗАЦИЯ
    # ============================================================

    @app.route('/register', methods=['GET', 'POST'])
    @rate_limit('register')
    def register():
        if not app.config.get('OPEN_REGISTRATION', True):
            abort(404)

        if current_user.is_authenticated:
            return redirect(url_for('chats'))

        if request.method == 'POST':
            import re
            email    = (request.form.get('email') or '').strip()[:254]
            username = (request.form.get('username') or '').strip()[:32]
            password = request.form.get('password') or ''
            password2 = request.form.get('password2') or ''

            if not email or not password or not username:
                return render_template('register.html', error='Заполните все поля')

            # Username: 5-32, только буквы/цифры/_
            if not re.match(r'^[a-zA-Z0-9_]{5,32}$', username):
                return render_template('register.html', error='Имя пользователя: от 5 до 32 символов (буквы, цифры, _)')

            # Email базовая проверка
            if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
                return render_template('register.html', error='Укажите email в формате user@example.com')

            # Пароль минимум 8 символов
            if len(password) < 8:
                return render_template('register.html', error='Пароль должен быть минимум 8 символов')

            if len(password) > 128:
                return render_template('register.html', error='Пароль слишком длинный (максимум 128 символов)')

            # Подтверждение пароля (если поле передано)
            if password2 and password != password2:
                return render_template('register.html', error='Пароли не совпадают')

            if User.query.filter_by(email_hash=hmac_hash(email)).first():
                return render_template('register.html', error='Пользователь с таким email уже существует')

            if User.query.filter_by(username=username).first():
                return render_template('register.html', error='Имя пользователя уже занято')

            if username.lower() == Config.PLATFORM_BOT_USERNAME.lower():
                return render_template('register.html', error='Это имя пользователя зарезервировано')

            hashed_password = PasswordSecurity.hash_password(password)
            user = User(email=email, email_hash=hmac_hash(email), username=username, password=hashed_password)

            db.session.add(user)
            db.session.commit()

            # Генерируем аватарку: случайный градиент + первая буква username
            user.avatar = generate_avatar_file(username, 'avatar', app)
            db.session.commit()

            if is_platform_admin(user):
                ensure_admin_privacy(user, db)

            ensure_proton_chat_for_user(user.id, db, app)

            login_user(user)
            session.permanent = True
            db.session.commit()

            tok = secrets.token_hex(32)
            session['session_token'] = tok
            _create_session_record(user.id, tok, is_current=True)

            return redirect(url_for('chats'))

        return render_template('register.html')

    @app.route('/login', methods=['GET', 'POST'])
    @rate_limit('login')
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('chats'))

        if request.method == 'POST':
            username = (request.form.get('username') or '').strip()[:254]
            password = request.form.get('password') or ''

            # Базовые проверки входных данных
            if not username or not password:
                return render_template('login.html', error='Заполните все поля')

            if len(password) > 128:
                return render_template('login.html', error='Неверный email, имя пользователя или пароль')

            # Простая защита от брутфорса через сессию
            fail_key = '_login_fails'
            fail_time_key = '_login_fail_time'
            import time as _time

            fails = session.get(fail_key, 0)
            fail_time = session.get(fail_time_key, 0)

            # Сброс счётчика если прошло больше 15 минут
            if _time.time() - fail_time > 900:
                fails = 0

            if fails >= 10:
                remaining = int(900 - (_time.time() - fail_time))
                mins = remaining // 60
                return render_template('login.html', error=f'Слишком много попыток. Подождите {mins} мин.')

            user = User.query.filter(
                (User.email_hash == hmac_hash(username)) | (User.username == username)
            ).first()

            if user and user.is_deleted:
                return render_template('login.html', error='Этот аккаунт был удалён. Восстановление невозможно.')

            if user and PasswordSecurity.verify_password(password, user.password):
                # Сброс счётчика при успехе
                session.pop(fail_key, None)
                session.pop(fail_time_key, None)

                login_user(user)
                session.permanent = True
                user.status    = 'online'
                user.last_seen = datetime.now(timezone.utc)
                db.session.commit()

                tok = secrets.token_hex(32)
                session['session_token'] = tok
                new_sess = _create_session_record(user.id, tok, is_current=True)

                if is_platform_admin(user):
                    ensure_admin_privacy(user, db)

                ban = get_active_user_ban(user.id, db)
                if ban:
                    return redirect(url_for('ban_page'))

                other_sessions = UserSession.query.filter_by(
                    user_id=user.id, is_active=True
                ).count()

                return redirect(url_for('chats'))
            else:
                session[fail_key] = fails + 1
                session[fail_time_key] = _time.time()
                return render_template('login.html', error='Неверный email, имя пользователя или пароль')

        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        current_token = session.get('session_token')
        if current_token:
            sess = UserSession.query.filter_by(session_token=current_token).first()
            if sess:
                sess.end_session()

        current_user.status    = 'offline'
        current_user.last_seen = datetime.now(timezone.utc)
        db.session.commit()
        logout_user()
        return redirect(url_for('login'))


    # ============================================================
    # ПУБЛИЧНЫЕ СТРАНИЦЫ (Соглашение / Политика конфиденциальности)
    # ============================================================

    @app.route('/terms')
    def terms():
        """Страница пользовательского соглашения"""
        return render_template('terms.html')

    @app.route('/privacy')
    def privacy_policy():
        """Страница политики конфиденциальности"""
        return render_template('privacy_policy.html')
    

    # ============================================================
    # ЧАТЫ
    # ============================================================

    @app.route('/chats')
    @login_required
    def chats():
        # Личные чаты — один запрос с joinedload
        user_chats = Chat.query.filter(
            (Chat.user1_id == current_user.id) | (Chat.user2_id == current_user.id)
        ).options(joinedload(Chat.user1), joinedload(Chat.user2)).order_by(Chat.last_message_at.desc()).all()

        chat_ids = [c.id for c in user_chats]
        if chat_ids:
            last_msg_subq = db.session.query(
                Message.chat_id, func.max(Message.id).label('max_id')
            ).filter(Message.chat_id.in_(chat_ids)).group_by(Message.chat_id).subquery()
            last_msgs = {m.chat_id: m for m in Message.query.join(
                last_msg_subq, Message.id == last_msg_subq.c.max_id).all()}
            unread_rows = db.session.query(
                Message.chat_id, func.count(Message.id)
            ).filter(
                Message.chat_id.in_(chat_ids),
                Message.receiver_id == current_user.id,
                Message.is_read == False
            ).group_by(Message.chat_id).all()
            unread_map = {r[0]: r[1] for r in unread_rows}
        else:
            last_msgs = {}; unread_map = {}

        chats_data = []
        for c in user_chats:
            if c.is_saved:
                if not saved_chat_has_messages(c.id, db):
                    continue
                chats_data.append({
                    'id': c.id, 'type': 'saved',
                    'last_message': last_msgs.get(c.id),
                    'unread_count': 0,
                    'last_message_time': c.last_message_at,
                    'last_message_time_str': _format_chat_time(c.last_message_at),
                    'last_message_ts': c.last_message_at.isoformat() + 'Z' if c.last_message_at else '',
                })
                continue
            chats_data.append({
                'id': c.id, 'type': 'private',
                'other_user': c.user2 if c.user1_id == current_user.id else c.user1,
                'last_message': last_msgs.get(c.id),
                'unread_count': unread_map.get(c.id, 0),
                'last_message_time': c.last_message_at,
                'last_message_time_str': _format_chat_time(c.last_message_at), 'last_message_ts': c.last_message_at.isoformat() + 'Z' if c.last_message_at else ''
            })

        # Группы — один запрос с joinedload
        user_groups = GroupMember.query.filter_by(user_id=current_user.id).options(
            joinedload(GroupMember.group)
        ).all()
        group_ids = [m.group.id for m in user_groups if m.group]
        if group_ids:
            last_gsubq = db.session.query(
                Message.group_id, func.max(Message.id).label('max_id')
            ).filter(Message.group_id.in_(group_ids)).group_by(Message.group_id).subquery()
            last_gmsgs = {m.group_id: m for m in Message.query.join(
                last_gsubq, Message.id == last_gsubq.c.max_id).all()}
            unread_grows = db.session.query(
                Message.group_id, func.count(Message.id)
            ).filter(
                Message.group_id.in_(group_ids),
                Message.is_read == False,
                Message.sender_id != current_user.id
            ).group_by(Message.group_id).all()
            unread_gmap = {r[0]: r[1] for r in unread_grows}
        else:
            last_gmsgs = {}; unread_gmap = {}

        for m in user_groups:
            g = m.group
            if not g: continue
            chats_data.append({
                'id': g.id, 'type': 'group', 'group': g,
                'last_message': last_gmsgs.get(g.id),
                'unread_count': unread_gmap.get(g.id, 0),
                'last_message_time': g.last_message_at,
                'last_message_time_str': _format_chat_time(g.last_message_at), 'last_message_ts': g.last_message_at.isoformat() + 'Z' if g.last_message_at else ''
            })

        chats_data.sort(key=lambda x: x['last_message_time'] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        saved_items = [x for x in chats_data if x['type'] == 'saved']
        other_items = [x for x in chats_data if x['type'] != 'saved']
        chats_data = saved_items + other_items
        return render_template('chats.html', chats=chats_data)

    @app.route('/start_chat', methods=['GET', 'POST'])
    @login_required
    def start_chat():
        if request.method == 'GET':
            username = request.args.get('username')
        else:
            username = request.form.get('username')

        if not username or username == current_user.username:
            return redirect(url_for('chats'))

        other_user = User.query.filter_by(username=username).first()
        if not other_user or other_user.is_deleted:
            return redirect(url_for('chats'))

        if is_platform_bot(other_user):
            chat = get_or_create_proton_chat(current_user.id, other_user.id, db)
            db.session.commit()
            return redirect(url_for('chat', chat_id=chat.id))

        # Нельзя создать чат если есть блокировка в любую сторону
        if _is_blocked_between(current_user.id, other_user.id):
            return redirect(url_for('chats'))

        # Проверяем настройку приватности собеседника: кто может писать ему
        other_privacy = UserPrivacy.query.filter_by(user_id=other_user.id).first()
        if other_privacy and other_privacy.who_can_write == 'contacts':
            # Разрешаем только если current_user есть в контактах собеседника
            is_in_contacts = Contact.query.filter_by(
                owner_id=other_user.id, contact_id=current_user.id
            ).first() is not None
            if not is_in_contacts:
                # Чат уже существует — можно открыть (собеседник сам начал)
                existing = Chat.query.filter(
                    ((Chat.user1_id == current_user.id) & (Chat.user2_id == other_user.id)) |
                    ((Chat.user1_id == other_user.id) & (Chat.user2_id == current_user.id))
                ).first()
                if existing:
                    return redirect(url_for('chat', chat_id=existing.id))
                return redirect(url_for('chats'))

        existing_chat = Chat.query.filter(
            ((Chat.user1_id == current_user.id) & (Chat.user2_id == other_user.id)) |
            ((Chat.user1_id == other_user.id) & (Chat.user2_id == current_user.id))
        ).first()

        if existing_chat:
            return redirect(url_for('chat', chat_id=existing_chat.id))

        new_chat = Chat(user1_id=current_user.id, user2_id=other_user.id)
        db.session.add(new_chat)
        db.session.commit()

        return redirect(url_for('chat', chat_id=new_chat.id))

    @app.route('/chat/<int:chat_id>')
    @login_required
    def chat(chat_id):
        chat_obj = Chat.query.get_or_404(chat_id)
        if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
            return redirect(url_for('chats'))

        is_saved_chat = bool(chat_obj.is_saved)
        if is_saved_chat:
            if chat_obj.user1_id != current_user.id:
                return redirect(url_for('chats'))
            if not saved_chat_has_messages(chat_obj.id, db):
                return redirect(url_for('chats'))
            other_user = current_user
        else:
            other_user = chat_obj.user2 if chat_obj.user1_id == current_user.id else chat_obj.user1

        # Только последние 50 сообщений — остальные грузятся lazy через get_messages
        messages = Message.query.filter_by(chat_id=chat_id, is_deleted=False).options(
            joinedload(Message.sender), joinedload(Message.reply_to),
        ).order_by(Message.timestamp.desc()).limit(50).all()
        messages = list(reversed(messages))

        # Помечаем прочитанными одним UPDATE
        db.session.query(Message).filter(
            Message.chat_id == chat_id,
            Message.receiver_id == current_user.id,
            Message.is_read == False
        ).update({'is_read': True}, synchronize_session=False)
        db.session.commit()

        # Закреплённые — без N+1
        pinned_messages_raw = Message.query.filter_by(
            chat_id=chat_id, is_pinned=True, is_deleted=False
        ).options(joinedload(Message.sender)).order_by(Message.pinned_at.desc()).all()

        pinned_messages = []
        for msg in pinned_messages_raw:
            pinned_messages.append({
                'id': msg.id,
                'content': msg.content,
                'sender_name': user_display_name(msg.sender),
                'timestamp': msg.timestamp.isoformat() + 'Z',
                'has_image': bool(msg.image_path),
                'has_file': bool(msg.file_path),
                'file_name': msg.file_name
            })

        # Проверяем блокировку между участниками
        chat_is_blocked = False if is_saved_chat else _is_blocked_between(current_user.id, other_user.id)

        # Применяем настройки приватности собеседника для отображения статуса
        other_status_display = 'offline' if is_saved_chat else _get_status_display(other_user, current_user.id)
        other_last_seen_display = None if is_saved_chat else _get_last_seen_display(other_user, current_user.id)

        bot = get_proton_bot_user(db)
        is_proton_chat = bool(bot and other_user.id == bot.id and not is_saved_chat)

        return render_template('chat.html', chat=chat_obj, messages=messages,
                               other_user=other_user, pinned_messages=pinned_messages,
                               chat_is_blocked=chat_is_blocked,
                               other_status_display=other_status_display,
                               other_last_seen_display=other_last_seen_display,
                               is_proton_chat=is_proton_chat,
                               is_saved_chat=is_saved_chat)

    @app.route('/chat/<int:chat_id>/delete', methods=['POST'])
    @login_required
    def delete_chat(chat_id):
        chat = Chat.query.get_or_404(chat_id)

        if chat.user1_id != current_user.id and chat.user2_id != current_user.id:
            return jsonify({'error': 'Нет доступа'}), 403

        bot = get_proton_bot_user(db)
        if bot and bot.id in (chat.user1_id, chat.user2_id):
            return jsonify({'error': 'Нельзя удалить чат с Proton'}), 400

        try:
            # Запоминаем участников ДО удаления
            u1_id = chat.user1_id
            u2_id = chat.user2_id

            messages = Message.query.filter_by(chat_id=chat_id).all()
            for message in messages:
                ForwardedMessage.query.filter(
                    (ForwardedMessage.original_message_id == message.id) |
                    (ForwardedMessage.forwarded_message_id == message.id)
                ).delete()

            Message.query.filter_by(chat_id=chat_id).delete()
            db.session.delete(chat)
            db.session.commit()

            # Оповещаем обоих участников через WebSocket
            from socketio_events import broadcast_chat_deleted
            broadcast_chat_deleted(chat_id, u1_id, u2_id)

            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка при удалении чата: {str(e)}'}), 500

    @app.route('/api/get_chats_data')
    @login_required
    def get_chats_data():
        """API endpoint для получения обновлённых данных чатов.
        Все данные грузятся батч-запросами — без N+1.
        """
        uid = current_user.id

        # ── 1. Личные чаты ──────────────────────────────────────
        user_chats = Chat.query.filter(
            (Chat.user1_id == uid) | (Chat.user2_id == uid)
        ).options(
            joinedload(Chat.user1),
            joinedload(Chat.user2)
        ).all()

        chat_ids = [c.id for c in user_chats]

        if chat_ids:
            # Последнее сообщение на чат — один запрос
            last_msg_subq = db.session.query(
                Message.chat_id,
                func.max(Message.id).label('max_id')
            ).filter(Message.chat_id.in_(chat_ids)).group_by(Message.chat_id).subquery()

            last_msgs = {
                m.chat_id: m for m in
                Message.query.join(last_msg_subq, Message.id == last_msg_subq.c.max_id)
                .options(joinedload(Message.sender)).all()
            }

            # Непрочитанные на чат — один запрос
            unread_rows = db.session.query(
                Message.chat_id, func.count(Message.id)
            ).filter(
                Message.chat_id.in_(chat_ids),
                Message.receiver_id == uid,
                Message.is_read == False
            ).group_by(Message.chat_id).all()
            unread_map = {r[0]: r[1] for r in unread_rows}
        else:
            last_msgs = {}
            unread_map = {}

        chats_data = []
        for chat in user_chats:
            if chat.is_saved:
                if not saved_chat_has_messages(chat.id, db):
                    continue
                last_message = last_msgs.get(chat.id)
                if last_message:
                    if last_message.content:
                        preview = last_message.content[:30]
                    elif last_message.image_path:
                        preview = '[Фото]'
                    elif last_message.file_path:
                        preview = f'[Файл] {last_message.file_name[:20] if last_message.file_name else "Файл"}'
                    else:
                        preview = ''
                else:
                    preview = 'Нет сообщений'
                chats_data.append({
                    'id': chat.id,
                    'type': 'saved',
                    'other_user_id': uid,
                    'other_username': SAVED_CHAT_NAME,
                    'other_avatar': url_for('serve_media', filepath=current_user.avatar),
                    'other_status': 'offline',
                    'last_message': preview,
                    'last_message_time': _format_chat_time(chat.last_message_at),
                    'last_message_ts': chat.last_message_at.isoformat() if chat.last_message_at else '',
                    'unread_count': 0,
                    'chat_url': url_for('chat', chat_id=chat.id),
                })
                continue

            other_user = chat.user2 if chat.user1_id == uid else chat.user1
            last_message = last_msgs.get(chat.id)

            if last_message:
                if last_message.content:
                    prefix = 'Вы: ' if last_message.sender_id == uid else ''
                    preview = f"{prefix}{last_message.content[:30]}"
                elif last_message.image_path:
                    preview = '[Фото]'
                elif last_message.file_path:
                    preview = f'[Файл] {last_message.file_name[:20] if last_message.file_name else "Файл"}'
                else:
                    preview = ''
            else:
                preview = 'Нет сообщений'

            chats_data.append({
                'id': chat.id,
                'type': 'private',
                'other_user_id': other_user.id,
                'other_username': other_user.display_name,
                'other_avatar': url_for('serve_media', filepath=other_user.avatar),
                'other_status': _get_status_display(other_user, uid),
                'last_message': preview,
                'last_message_time': _format_chat_time(chat.last_message_at),
                'last_message_ts': chat.last_message_at.isoformat() if chat.last_message_at else '',
                'unread_count': unread_map.get(chat.id, 0),
                'chat_url': url_for('chat', chat_id=chat.id)
            })

        # ── 2. Группы ────────────────────────────────────────────
        user_memberships = GroupMember.query.filter_by(user_id=uid).options(
            joinedload(GroupMember.group)
        ).all()
        groups = [m.group for m in user_memberships if m.group]
        group_ids = [g.id for g in groups]

        if group_ids:
            # Последнее сообщение на группу — один запрос
            last_gmsg_subq = db.session.query(
                Message.group_id,
                func.max(Message.id).label('max_id')
            ).filter(Message.group_id.in_(group_ids)).group_by(Message.group_id).subquery()

            last_gmsgs = {
                m.group_id: m for m in
                Message.query.join(last_gmsg_subq, Message.id == last_gmsg_subq.c.max_id)
                .options(joinedload(Message.sender)).all()
            }

            # Непрочитанные на группу — один запрос
            unread_grows = db.session.query(
                Message.group_id, func.count(Message.id)
            ).filter(
                Message.group_id.in_(group_ids),
                Message.is_read == False,
                Message.sender_id != uid
            ).group_by(Message.group_id).all()
            unread_gmap = {r[0]: r[1] for r in unread_grows}

            # Количество участников на группу — один запрос
            member_counts = dict(
                db.session.query(
                    GroupMember.group_id, func.count(GroupMember.id)
                ).filter(GroupMember.group_id.in_(group_ids))
                .group_by(GroupMember.group_id).all()
            )
        else:
            last_gmsgs = {}
            unread_gmap = {}
            member_counts = {}

        for group in groups:
            last_message = last_gmsgs.get(group.id)

            if last_message:
                if last_message.content:
                    sender_name = user_display_name(last_message.sender) if last_message.sender else 'Пользователь'
                    prefix = 'Вы: ' if last_message.sender_id == uid else f'{sender_name}: '
                    preview = f"{prefix}{last_message.content[:30]}"
                elif last_message.image_path:
                    preview = '[Фото]'
                elif last_message.file_path:
                    preview = f'[Файл] {last_message.file_name[:20] if last_message.file_name else "Файл"}'
                else:
                    preview = ''
            else:
                preview = 'Нет сообщений'

            chats_data.append({
                'id': group.id,
                'type': 'group',
                'group_name': group.name,
                'group_icon': url_for('serve_media', filepath=group.icon),
                'members_count': member_counts.get(group.id, 0),
                'last_message': preview,
                'last_message_time': _format_chat_time(group.last_message_at),
                'last_message_ts': group.last_message_at.isoformat() if group.last_message_at else '',
                'unread_count': unread_gmap.get(group.id, 0),
                'chat_url': url_for('group_chat', group_id=group.id)
            })

        chats_data.sort(key=lambda x: x['last_message_ts'] or '', reverse=True)
        saved_items = [x for x in chats_data if x['type'] == 'saved']
        other_items = [x for x in chats_data if x['type'] != 'saved']
        chats_data = saved_items + other_items
        total_unread = sum(c['unread_count'] for c in chats_data)

        return jsonify({
            'chats': chats_data,
            'total_unread': total_unread
        })

    # ============================================================
    # ПРОФИЛЬ
    # ============================================================

    @app.route('/profile/<int:user_id>')
    @login_required
    def profile(user_id):
        user = User.query.get_or_404(user_id)

        if is_platform_bot(user) and not is_platform_admin(current_user):
            return render_template('404.html'), 404

        is_blocked = False
        is_contact = False
        if user.id != current_user.id:
            is_blocked = BlockedUser.query.filter_by(
                blocker_id=current_user.id,
                blocked_id=user.id
            ).first() is not None
            is_contact = Contact.query.filter_by(
                owner_id=current_user.id,
                contact_id=user.id
            ).first() is not None

        return render_template('profile.html', profile_user=user, is_blocked=is_blocked, is_contact=is_contact,
                               profile_status_display=_get_status_display(user, current_user.id),
                               profile_last_seen_display=_get_last_seen_display(user, current_user.id))

    @app.route('/profile/edit', methods=['GET', 'POST'])
    @login_required
    def edit_profile():
        if current_user.is_deleted:
            return redirect(url_for('login'))
        if request.method == 'POST':
            username = request.form.get('username')
            bio      = request.form.get('bio')
            avatar   = request.files.get('avatar')
            old_username = current_user.username

            if username:
                username = username.strip()
                if len(username) < 5:
                    return jsonify({'error': 'Имя пользователя должно содержать минимум 5 символов'}), 400
                existing_user = User.query.filter_by(username=username).first()
                if existing_user and existing_user.id != current_user.id:
                    return jsonify({'error': 'Имя пользователя уже занято'}), 400
                current_user.username = username

            if bio is not None:
                current_user.bio = bio

            if avatar and avatar.filename:
                try:
                    old_avatar = current_user.avatar
                    filepath, _ = save_file(avatar, 'avatar', app)
                    current_user.avatar = filepath
                    delete_uploaded_file(old_avatar, app)
                except Exception as e:
                    return jsonify({'error': f'Ошибка обработки изображения: {str(e)}'}), 400
            elif username and username != old_username and should_regenerate_avatar(current_user.avatar):
                try:
                    old_avatar = current_user.avatar
                    current_user.avatar = generate_avatar_file(username, 'avatar', app)
                    delete_uploaded_file(old_avatar, app)
                except Exception as e:
                    return jsonify({'error': f'Ошибка генерации аватарки: {str(e)}'}), 400

            db.session.commit()
            return jsonify({
                'success': True,
                'avatar': url_for('serve_media', filepath=current_user.avatar),
            })

        return render_template('edit_profile.html')

    @app.route('/profile/delete_avatar', methods=['POST'])
    @login_required
    def delete_avatar():
        if current_user.is_deleted:
            return jsonify({'error': 'Аккаунт удалён'}), 400
        if not is_custom_upload(current_user.avatar):
            return jsonify({'error': 'Своя фотография не загружена'}), 400
        try:
            old_avatar = current_user.avatar
            current_user.avatar = generate_avatar_file(current_user.username, 'avatar', app)
            delete_uploaded_file(old_avatar, app)
            db.session.commit()
            return jsonify({
                'success': True,
                'avatar': url_for('serve_media', filepath=current_user.avatar),
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    @app.route('/profile/delete_account', methods=['POST'])
    @login_required
    def delete_account():
        if current_user.is_deleted:
            return jsonify({'error': 'Аккаунт уже удалён'}), 400
        try:
            delete_user_account(current_user, db, app)
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка при удалении: {str(e)}'}), 500
        logout_user()
        session.clear()
        return jsonify({'success': True, 'redirect': url_for('login')})

    # ============================================================
    # БЕЗОПАСНОСТЬ — ГЛАВНАЯ (меню)
    # ============================================================

    @app.route('/security')
    @login_required
    def security():
        sessions = UserSession.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).all()

        blocked_count = BlockedUser.query.filter_by(blocker_id=current_user.id).count()

        return render_template('security.html', sessions=sessions, blocked_count=blocked_count,
                               admin=is_platform_admin(current_user))

    @app.route('/theme')
    @login_required
    def theme_page():
        return render_template('theme.html')

    # ============================================================
    # БЕЗОПАСНОСТЬ — СМЕНА ПАРОЛЯ (страница)
    # ============================================================

    @app.route('/security/password')
    @login_required
    def security_password():
        return render_template('password.html')

    @app.route('/security/change_password', methods=['POST'])
    @login_required
    def change_password():
        current_password = request.form.get('current_password', '').strip()
        new_password     = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if not current_password or not new_password or not confirm_password:
            return jsonify({'error': 'Заполните все поля'}), 400

        if not PasswordSecurity.verify_password(current_password, current_user.password):
            return jsonify({'error': 'Неверный текущий пароль'}), 400

        if new_password != confirm_password:
            return jsonify({'error': 'Новые пароли не совпадают'}), 400

        if len(new_password) < 8:
            return jsonify({'error': 'Пароль должен быть минимум 8 символов'}), 400

        if new_password == current_password:
            return jsonify({'error': 'Новый пароль совпадает со старым'}), 400

        current_user.password = PasswordSecurity.hash_password(new_password)
        db.session.commit()

        try:
            from security import SecurityAudit
            SecurityAudit.log_password_reset(current_user.id, current_user.username)
        except Exception:
            pass

        return jsonify({'success': True, 'message': 'Пароль успешно изменён'})

    # ============================================================
    # БЕЗОПАСНОСТЬ — УСТРОЙСТВА
    # ============================================================

    @app.route('/security/devices')
    @login_required
    def security_devices():
        sessions = UserSession.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).order_by(UserSession.last_active.desc()).all()

        current_token = session.get('session_token')
        current_sess  = None
        for s in sessions:
            s.is_current = (s.session_token == current_token)
            if s.is_current:
                current_sess = s

        # Ограничения по времени отключены — управление сессиями доступно сразу
        account_too_new = False
        for s in sessions:
            s.terminate_locked = False

        return render_template('devices.html', sessions=sessions, account_too_new=account_too_new)

    @app.route('/security/terminate_session/<int:session_id>', methods=['POST'])
    @login_required
    def terminate_session(session_id):
        sess = UserSession.query.filter_by(
            id=session_id,
            user_id=current_user.id
        ).first_or_404()

        current_token = session.get('session_token')
        if sess.session_token == current_token:
            return jsonify({'error': 'Нельзя завершить текущую сессию. Используйте «Выйти».'}), 400

        # Ограничения по времени отключены — завершать сессию можно сразу
        sess.end_session()
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/security/terminate_all_sessions', methods=['POST'])
    @login_required
    def terminate_all_sessions():
        """Завершает все сессии кроме текущей."""
        # Ограничения по времени отключены
        current_token   = session.get('session_token')
        sessions_to_end = UserSession.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).all()

        ended = 0
        for s in sessions_to_end:
            if s.session_token != current_token:
                s.end_session()
                ended += 1

        db.session.commit()
        return jsonify({'success': True, 'ended': ended})

    # ============================================================
    # БЕЗОПАСНОСТЬ — ЧЁРНЫЙ СПИСОК
    # ============================================================

    @app.route('/security/blacklist')
    @login_required
    def security_blacklist():
        blocked_users = BlockedUser.query.filter_by(blocker_id=current_user.id)\
            .order_by(BlockedUser.created_at.desc()).all()
        return render_template('blacklist.html', blocked_users=blocked_users)

    @app.route('/security/block/<int:user_id>', methods=['POST'])
    @login_required
    def block_user(user_id):
        if user_id == current_user.id:
            return jsonify({'error': 'Нельзя заблокировать себя'}), 400

        target = User.query.get_or_404(user_id)

        if is_platform_bot(target):
            return jsonify({'error': 'Нельзя заблокировать Proton'}), 400

        existing = BlockedUser.query.filter_by(
            blocker_id=current_user.id,
            blocked_id=user_id
        ).first()

        if existing:
            return jsonify({'error': 'Пользователь уже заблокирован'}), 400

        block = BlockedUser(blocker_id=current_user.id, blocked_id=user_id)
        db.session.add(block)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/security/unblock/<int:user_id>', methods=['POST'])
    @login_required
    def unblock_user(user_id):
        block = BlockedUser.query.filter_by(
            blocker_id=current_user.id,
            blocked_id=user_id
        ).first()

        if not block:
            return jsonify({'error': 'Пользователь не заблокирован'}), 404

        db.session.delete(block)
        db.session.commit()
        return jsonify({'success': True})

    # ============================================================
    # ПЛАТФОРМЕННЫЙ АДМИН — ДАШБОРД И БАНЫ
    # ============================================================

    @app.route('/banb')
    @login_required
    def ban_page():
        ban = get_active_user_ban(current_user.id, db)
        if not ban:
            return redirect(url_for('chats'))
        expires_iso = None
        if ban.expires_at:
            exp = ban.expires_at if ban.expires_at.tzinfo else ban.expires_at.replace(tzinfo=timezone.utc)
            expires_iso = exp.isoformat()
        return render_template('banb.html', ban=ban, expires_iso=expires_iso)

    @app.route('/dashboard')
    @login_required
    def dashboard():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        stats = {
            'users': User.query.filter_by(is_deleted=False).count(),
            'messages': Message.query.filter_by(is_deleted=False).count(),
            'groups': Group.query.count(),
            'chats': Chat.query.count(),
            'online': User.query.filter_by(is_deleted=False, status='online').count(),
            'pending_reports': MessageReport.query.filter_by(status='pending').count(),
        }
        return render_template('dashboard.html', stats=stats, ban_periods=BAN_PERIODS)

    @app.route('/dashboard/api/users')
    @login_required
    def dashboard_search_users():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        q = (request.args.get('q') or '').strip()
        query = User.query.filter(User.is_deleted == False)
        if q:
            query = query.filter(User.username.ilike(f'%{q}%'))
        users = query.order_by(User.created_at.desc()).limit(30).all()

        result = []
        for u in users:
            ban = get_active_user_ban(u.id, db)
            result.append({
                'id': u.id,
                'username': u.username,
                'avatar': url_for('serve_media', filepath=u.avatar) if u.avatar else '',
                'created_at': u.created_at.strftime('%d.%m.%Y') if u.created_at else '',
                'is_admin': is_platform_admin(u),
                'is_banned': ban is not None,
                'ban_until': ban.expires_at.strftime('%d.%m.%Y %H:%M') if ban and ban.expires_at else ('навсегда' if ban else None),
            })
        return jsonify({'users': result})

    @app.route('/dashboard/ban', methods=['POST'])
    @login_required
    def dashboard_ban_user():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id')
        period = data.get('period', '30d')

        if period not in BAN_PERIODS:
            return jsonify({'error': 'Недопустимый срок блокировки'}), 400

        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Некорректный пользователь'}), 400

        target = User.query.get(user_id)
        if not target or target.is_deleted:
            return jsonify({'error': 'Пользователь не найден'}), 404

        if is_platform_admin(target):
            return jsonify({'error': 'Нельзя заблокировать администратора'}), 400

        if user_id == current_user.id:
            return jsonify({'error': 'Нельзя заблокировать себя'}), 400

        ok, err = apply_platform_ban(user_id, period, current_user.id, db)
        if not ok:
            return jsonify({'error': err}), 400

        return jsonify({'success': True, 'period_label': BAN_PERIODS[period]['label']})

    @app.route('/dashboard/unban/<int:user_id>', methods=['POST'])
    @login_required
    def dashboard_unban_user(user_id):
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        ban = UserBan.query.filter_by(user_id=user_id).first()
        if not ban:
            return jsonify({'error': 'Пользователь не заблокирован'}), 404

        db.session.delete(ban)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/dashboard/bot')
    @login_required
    def dashboard_bot():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        bot = get_proton_bot_user(db)
        user_count = User.query.filter(User.is_deleted == False).count()
        if bot:
            user_count = max(0, user_count - 1)

        last_msg = None
        if bot:
            last_msg = Message.query.filter_by(sender_id=bot.id, is_deleted=False)\
                .order_by(Message.timestamp.desc()).first()

        return render_template('dashboard_bot.html', bot=bot, user_count=user_count, last_msg=last_msg)

    @app.route('/dashboard/bot/send', methods=['POST'])
    @login_required
    def dashboard_bot_send():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        data = request.get_json(silent=True) or {}
        content = (data.get('content') or request.form.get('content') or '').strip()

        count, err = send_proton_broadcast(content, db, app)
        if err:
            return jsonify({'error': err}), 400

        return jsonify({'success': True, 'sent_count': count})

    @app.route('/dashboard/bot/avatar', methods=['POST'])
    @login_required
    def dashboard_bot_avatar():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        bot = get_proton_bot_user(db) or ensure_proton_bot_user(db, app)
        avatar = request.files.get('avatar')
        if not avatar or not avatar.filename:
            return jsonify({'error': 'Выберите изображение'}), 400

        try:
            old_avatar = bot.avatar
            filepath, _ = save_file(avatar, 'avatar', app)
            bot.avatar = filepath
            delete_uploaded_file(old_avatar, app)
            db.session.commit()
            return jsonify({
                'success': True,
                'avatar': url_for('serve_media', filepath=bot.avatar),
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка загрузки: {str(e)}'}), 400

    @app.route('/dashboard/bot/avatar/reset', methods=['POST'])
    @login_required
    def dashboard_bot_avatar_reset():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        bot = get_proton_bot_user(db) or ensure_proton_bot_user(db, app)
        try:
            old_avatar = bot.avatar
            bot.avatar = generate_avatar_file(bot.username, 'avatar', app)
            delete_uploaded_file(old_avatar, app)
            db.session.commit()
            return jsonify({
                'success': True,
                'avatar': url_for('serve_media', filepath=bot.avatar),
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    @app.route('/dashboard/api/reports')
    @login_required
    def dashboard_reports_list():
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        reports = MessageReport.query.filter_by(status='pending')\
            .options(joinedload(MessageReport.message), joinedload(MessageReport.reporter))\
            .order_by(MessageReport.created_at.asc()).all()

        result = []
        for rep in reports:
            msg = rep.message
            if not msg:
                db.session.delete(rep)
                continue
            author = User.query.get(msg.sender_id)
            ctx_label, ctx_url = _message_report_context(msg)
            created = rep.created_at
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            preview = '(сообщение удалено)' if msg.is_deleted else _message_preview(msg)
            result.append({
                'id': rep.id,
                'message_id': msg.id,
                'created_at': created.strftime('%d.%m.%Y %H:%M') if created else '',
                'reporter_username': rep.reporter.username if rep.reporter else '?',
                'author_id': msg.sender_id,
                'author_username': author.username if author and not author.is_deleted else 'Удалённый аккаунт',
                'message_preview': preview,
                'context': ctx_label,
                'context_url': ctx_url,
            })
        db.session.commit()
        return jsonify({'reports': result})

    @app.route('/dashboard/reports/<int:report_id>/dismiss', methods=['POST'])
    @login_required
    def dashboard_report_dismiss(report_id):
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        rep = MessageReport.query.filter_by(id=report_id, status='pending').first()
        if not rep:
            return jsonify({'error': 'Жалоба не найдена'}), 404

        db.session.delete(rep)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/dashboard/reports/<int:report_id>/resolve', methods=['POST'])
    @login_required
    def dashboard_report_resolve(report_id):
        if not is_platform_admin(current_user):
            return render_template('404.html'), 404

        data = request.get_json(silent=True) or {}
        period = data.get('period', '30d')
        if period not in BAN_PERIODS:
            return jsonify({'error': 'Недопустимый срок блокировки'}), 400

        rep = MessageReport.query.filter_by(id=report_id, status='pending').first()
        if not rep:
            return jsonify({'error': 'Жалоба не найдена'}), 404

        msg = rep.message
        if not msg:
            db.session.delete(rep)
            db.session.commit()
            return jsonify({'error': 'Сообщение удалено'}), 404

        ok, err = apply_platform_ban(msg.sender_id, period, current_user.id, db)
        if not ok:
            return jsonify({'error': err}), 400

        db.session.delete(rep)
        db.session.commit()
        return jsonify({'success': True, 'period_label': BAN_PERIODS[period]['label']})

    @app.route('/message/<int:message_id>/report', methods=['POST'])
    @login_required
    def report_message(message_id):
        message = Message.query.get_or_404(message_id)

        if not message.group_id:
            return jsonify({'error': 'Жалобы доступны только для сообщений в группах'}), 400

        if message.sender_id == current_user.id:
            return jsonify({'error': 'Нельзя пожаловаться на своё сообщение'}), 400

        if not _user_can_access_message(current_user.id, message):
            return jsonify({'error': 'Нет доступа к сообщению'}), 403

        existing = MessageReport.query.filter_by(message_id=message_id).first()
        if existing:
            return jsonify({'success': True, 'already': True})

        report = MessageReport(message_id=message_id, reporter_id=current_user.id)
        db.session.add(report)
        db.session.commit()
        return jsonify({'success': True, 'already': False})

    # ============================================================
    # ПРИВАТНОСТЬ
    # ============================================================

    @app.route('/security/privacy')
    @login_required
    def security_privacy():
        """Страница настроек приватности."""
        privacy = UserPrivacy.query.filter_by(user_id=current_user.id).first()
        if not privacy:
            privacy = UserPrivacy(user_id=current_user.id)
            db.session.add(privacy)
            db.session.commit()
        return render_template('privacy.html', privacy=privacy)

    @app.route('/security/privacy', methods=['POST'])
    @login_required
    def security_privacy_save():
        """Сохраняет настройку приватности (AJAX)."""
        data = request.get_json(silent=True) or {}
        setting = data.get('setting')
        value   = data.get('value')

        allowed = {
            'who_can_write': ('all', 'contacts'),
            'last_seen':     ('all', 'contacts', 'nobody'),
        }

        if setting not in allowed or value not in allowed[setting]:
            return jsonify({'ok': False, 'error': 'Недопустимое значение'}), 400

        privacy = UserPrivacy.query.filter_by(user_id=current_user.id).first()
        if not privacy:
            privacy = UserPrivacy(user_id=current_user.id)
            db.session.add(privacy)

        setattr(privacy, setting, value)
        db.session.commit()
        return jsonify({'ok': True})

    # ============================================================
    # КОНТАКТЫ
    # ============================================================

    @app.route('/contacts')
    @login_required
    def contacts():
        user_contacts = Contact.query.filter_by(owner_id=current_user.id)\
            .order_by(Contact.created_at.desc()).all()
        return render_template('contacts.html', contacts=user_contacts)

    @app.route('/contacts/add/<int:user_id>', methods=['POST'])
    @login_required
    def add_contact(user_id):
        if user_id == current_user.id:
            return jsonify({'error': 'Нельзя добавить себя в контакты'}), 400
        User.query.get_or_404(user_id)
        existing = Contact.query.filter_by(owner_id=current_user.id, contact_id=user_id).first()
        if existing:
            return jsonify({'error': 'Уже в контактах'}), 400
        contact = Contact(owner_id=current_user.id, contact_id=user_id)
        db.session.add(contact)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/contacts/remove/<int:user_id>', methods=['POST'])
    @login_required
    def remove_contact(user_id):
        contact = Contact.query.filter_by(owner_id=current_user.id, contact_id=user_id).first()
        if not contact:
            return jsonify({'error': 'Не найдено в контактах'}), 404
        db.session.delete(contact)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/search_users')
    @login_required
    def search_users():
        q = request.args.get('q', '').strip()
        if len(q) < 4:
            return jsonify({'users': []})
        users = User.query.filter(
            User.username.ilike(f'%{q}%'),
            User.id != current_user.id,
            User.is_deleted == False,
        ).limit(20).all()
        result = []
        for u in users:
            if is_platform_bot(u):
                continue
            is_contact = Contact.query.filter_by(owner_id=current_user.id, contact_id=u.id).first() is not None
            result.append({
                'id': u.id,
                'username': u.username,
                'avatar': url_for('serve_media', filepath=u.avatar),
                'status': u.status,
                'is_contact': is_contact,
                'profile_url': url_for('profile', user_id=u.id),
            })
        return jsonify({'users': result})

    # ============================================================
    # ГРУППЫ
    # ============================================================

    @app.route('/groups')
    @login_required
    def groups():
        user_groups = GroupMember.query.filter_by(user_id=current_user.id).all()
        groups_data = []

        for membership in user_groups:
            group        = membership.group
            last_message = Message.query.filter_by(group_id=group.id).order_by(Message.timestamp.desc()).first()
            unread_count = Message.query.filter_by(
                group_id=group.id,
                is_read=False
            ).filter(Message.sender_id != current_user.id).count()

            groups_data.append({
                'id': group.id,
                'name': group.name,
                'description': group.description,
                'icon': group.icon,
                'members_count': len(group.members),
                'last_message': last_message,
                'unread_count': unread_count,
                'role': membership.role,
                'last_message_time': group.last_message_at, 'last_message_ts': group.last_message_at.isoformat() + 'Z' if group.last_message_at else ''
            })

        return render_template('groups.html', groups=groups_data)

    @app.route('/group/create', methods=['POST'])
    @login_required
    def create_group():
        name        = request.form.get('name')
        description = request.form.get('description', '')
        icon        = request.files.get('icon')
        visibility  = request.form.get('visibility', 'private')
        join_type   = request.form.get('join_type', 'open')
        group_username = request.form.get('group_username', '').strip() or None

        if not name:
            return jsonify({'error': 'Название группы обязательно'}), 400

        # Публичная группа не может быть приватной по видимости — именно так и есть,
        # но если visibility='public', join_type может быть любым (open или request).
        # Если visibility='private', join_type тоже свободен.
        if visibility not in ('public', 'private'):
            visibility = 'private'
        if join_type not in ('open', 'request'):
            join_type = 'open'

        if group_username:
            if len(group_username) > 50 or not group_username.replace('_', '').isalnum():
                return jsonify({'error': 'Юзернейм может содержать только буквы, цифры и _'}), 400
            if Group.query.filter_by(group_username=group_username).first():
                return jsonify({'error': 'Юзернейм уже занят'}), 400

        group = Group(
            name=name,
            description=description,
            owner_id=current_user.id,
            visibility=visibility,
            join_type=join_type,
            group_username=group_username,
            invite_token=secrets.token_urlsafe(32)
        )

        if icon and icon.filename:
            try:
                filepath, _ = save_file(icon, 'group_icon', app)
                group.icon = filepath
            except Exception as e:
                return jsonify({'error': f'Ошибка обработки иконки: {str(e)}'}), 400
        else:
            group.icon = generate_avatar_file(name, 'group_icon', app)

        db.session.add(group)
        db.session.flush()

        owner_member = GroupMember(
            group_id=group.id,
            user_id=current_user.id,
            role='owner'
        )
        db.session.add(owner_member)
        db.session.commit()

        return redirect(url_for('group_chat', group_id=group.id))

    @app.route('/group/<int:group_id>')
    @login_required
    def group_chat(group_id):
        group = Group.query.get_or_404(group_id)

        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if not membership:
            # Приватная группа — посторонние видят 404 (вступление только по ссылке)
            if group.visibility == 'private':
                abort(404)
            # Публичная группа — показываем превью для вступления
            pending_request = GroupJoinRequest.query.filter_by(
                group_id=group_id,
                user_id=current_user.id,
                status='pending'
            ).first()
            members_count = GroupMember.query.filter_by(group_id=group_id).count()
            return render_template('group_join_preview.html',
                                   group=group,
                                   pending_request=pending_request,
                                   members_count=members_count,
                                   is_banned=GroupBan.query.filter_by(group_id=group_id, user_id=current_user.id).first() is not None)

        # Только последние 50 сообщений
        messages = Message.query.filter_by(group_id=group_id, is_deleted=False).options(
            joinedload(Message.sender), joinedload(Message.reply_to),
        ).order_by(Message.timestamp.desc()).limit(50).all()
        messages = list(reversed(messages))

        from read_receipts import mark_group_messages_read
        group_members_count = GroupMember.query.filter_by(group_id=group_id).count()
        readers_count = max(group_members_count - 1, 0)
        mark_group_messages_read(group_id, current_user.id, readers_count)
        db.session.commit()

        # Закреплённые — без N+1
        pinned_messages_raw = Message.query.filter_by(
            group_id=group_id, is_pinned=True, is_deleted=False
        ).options(joinedload(Message.sender)).order_by(Message.pinned_at.desc()).all()

        pinned_messages = []
        for msg in pinned_messages_raw:
            pinned_messages.append({
                'id': msg.id,
                'content': msg.content,
                'sender_name': user_display_name(msg.sender),
                'timestamp': msg.timestamp.isoformat() + 'Z',
                'has_image': bool(msg.image_path),
                'has_file': bool(msg.file_path),
                'file_name': msg.file_name
            })

        return render_template('group_chat.html', group=group, messages=messages,
                               membership=membership, pinned_messages=pinned_messages,
                               group_members_count=group_members_count,
                               member_roles={m.user_id: m.role for m in GroupMember.query.filter_by(group_id=group_id).all()})

    @app.route('/group/<int:group_id>/members')
    @login_required
    def group_members(group_id):
        group = Group.query.get_or_404(group_id)

        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if not membership:
            return redirect(url_for('groups'))

        members = GroupMember.query.filter_by(group_id=group_id).all()
        return render_template('group_members.html', group=group, members=members, current_membership=membership)

    @app.route('/group/<int:group_id>/members/list')
    @login_required
    def group_members_list(group_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership:
            return jsonify({'error': 'Нет доступа'}), 403
        rows = GroupMember.query.filter_by(group_id=group_id).options(
            joinedload(GroupMember.user)
        ).all()
        return jsonify({
            'total': len(rows),
            'owner_id': group.owner_id,
            'my_role': membership.role,
            'members': [{
                'user_id': m.user_id,
                'username': user_display_name(m.user),
                'avatar': m.user.avatar if m.user else '',
                'role': m.role,
            } for m in rows],
        })

    @app.route('/group/<int:group_id>/add_member', methods=['POST'])
    @login_required
    def add_group_member(group_id):
        group = Group.query.get_or_404(group_id)

        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403

        username = request.form.get('username')
        user     = User.query.filter_by(username=username).first()

        if not user:
            return jsonify({'error': 'Пользователь не найден'}), 404

        existing = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=user.id
        ).first()

        if existing:
            return jsonify({'error': 'Пользователь уже в группе'}), 400

        GroupBan.query.filter_by(group_id=group_id, user_id=user.id).delete()

        new_member = GroupMember(
            group_id=group_id,
            user_id=user.id,
            role='member'
        )

        db.session.add(new_member)
        db.session.commit()
        return jsonify({'success': True, 'user_id': user.id, 'username': user.username, 'avatar': user.avatar, 'role': 'member'})

    @app.route('/group/<int:group_id>/remove_member/<int:user_id>', methods=['POST'])
    @login_required
    def remove_group_member(group_id, user_id):
        group = Group.query.get_or_404(group_id)

        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403

        if user_id == group.owner_id:
            return jsonify({'error': 'Нельзя удалить владельца группы'}), 400

        member = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=user_id
        ).first()

        if not member:
            return jsonify({'error': 'Участник не найден'}), 404

        if membership.role == 'admin' and member.role != 'member':
            return jsonify({'error': 'Администратор может удалять только участников'}), 403

        data = request.get_json(silent=True) or {}
        do_ban = data.get('ban') in (True, 'true', '1', 1) or request.form.get('ban') in ('true', '1', 'on')

        db.session.delete(member)
        if do_ban:
            if not GroupBan.query.filter_by(group_id=group_id, user_id=user_id).first():
                db.session.add(GroupBan(group_id=group_id, user_id=user_id, banned_by_id=current_user.id))
        GroupJoinRequest.query.filter_by(group_id=group_id, user_id=user_id).delete()
        db.session.commit()

        try:
            from socketio_events import socketio
            socketio.emit('group_kicked', {'group_id': group_id}, to=f'user_{user_id}')
        except Exception:
            pass
        return jsonify({'success': True, 'banned': do_ban})

    @app.route('/group/<int:group_id>/unban/<int:user_id>', methods=['POST'])
    @login_required
    def unban_group_member(group_id, user_id):
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        ban = GroupBan.query.filter_by(group_id=group_id, user_id=user_id).first()
        if not ban:
            return jsonify({'error': 'Пользователь не заблокирован'}), 404
        db.session.delete(ban)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/group/<int:group_id>/banned')
    @login_required
    def group_banned_list(group_id):
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        bans = GroupBan.query.filter_by(group_id=group_id).options(joinedload(GroupBan.user)).all()
        return jsonify({'banned': [{
            'user_id': b.user_id,
            'username': user_display_name(b.user),
            'avatar': b.user.avatar if b.user else '',
            'banned_at': b.banned_at.isoformat() + 'Z' if b.banned_at else None,
        } for b in bans]})

    @app.route('/group/<int:group_id>/change_role/<int:user_id>', methods=['POST'])
    @login_required
    def change_group_role(group_id, user_id):
        group = Group.query.get_or_404(group_id)

        if current_user.id != group.owner_id:
            return jsonify({'error': 'Только владелец может назначать админов'}), 403

        new_role = request.form.get('role')
        if new_role not in ['admin', 'member']:
            return jsonify({'error': 'Некорректная роль'}), 400

        member = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=user_id
        ).first()

        if not member:
            return jsonify({'error': 'Участник не найден'}), 404

        member.role = new_role
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/group/<int:group_id>/edit', methods=['POST'])
    @login_required
    def edit_group(group_id):
        group = Group.query.get_or_404(group_id)

        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403

        name           = request.form.get('name')
        description    = request.form.get('description')
        icon           = request.files.get('icon')
        visibility     = request.form.get('visibility')
        join_type      = request.form.get('join_type')
        group_username = request.form.get('group_username', '').strip() or None
        old_name       = group.name

        if name:
            group.name = name

        if description is not None:
            group.description = description

        if visibility in ('public', 'private'):
            group.visibility = visibility

        if join_type in ('open', 'request'):
            group.join_type = join_type

        if group_username is not None:
            if group_username == '':
                group.group_username = None
            else:
                if len(group_username) > 50 or not group_username.replace('_', '').isalnum():
                    return jsonify({'error': 'Юзернейм может содержать только буквы, цифры и _'}), 400
                existing = Group.query.filter(
                    Group.group_username == group_username,
                    Group.id != group_id
                ).first()
                if existing:
                    return jsonify({'error': 'Юзернейм уже занят'}), 400
                group.group_username = group_username

        if icon and icon.filename:
            try:
                old_icon = group.icon
                filepath, _ = save_file(icon, 'group_icon', app)
                group.icon = filepath
                delete_uploaded_file(old_icon, app)
            except Exception as e:
                return jsonify({'error': f'Ошибка обработки иконки: {str(e)}'}), 400
        elif name and name != old_name and should_regenerate_avatar(group.icon):
            try:
                old_icon = group.icon
                group.icon = generate_avatar_file(name, 'group_icon', app)
                delete_uploaded_file(old_icon, app)
            except Exception as e:
                return jsonify({'error': f'Ошибка генерации иконки: {str(e)}'}), 400

        db.session.commit()
        return redirect(url_for('group_chat', group_id=group.id))

    @app.route('/group/<int:group_id>/delete_icon', methods=['POST'])
    @login_required
    def delete_group_icon(group_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        if not is_custom_upload(group.icon):
            return jsonify({'error': 'Своё изображение не загружено'}), 400
        try:
            old_icon = group.icon
            group.icon = generate_avatar_file(group.name, 'group_icon', app)
            delete_uploaded_file(old_icon, app)
            db.session.commit()
            return jsonify({
                'success': True,
                'icon': url_for('serve_media', filepath=group.icon),
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    @app.route('/group/<int:group_id>/set_write_permission', methods=['POST'])
    @login_required
    def set_group_write_permission(group_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        permission = request.json.get('write_permission', 'all')
        if permission not in ['all', 'admins_only']:
            return jsonify({'error': 'Неверное значение'}), 400
        group.write_permission = permission
        db.session.commit()
        return jsonify({'success': True, 'write_permission': group.write_permission})

    # ── Invite link: получить / сбросить ─────────────────────────────────────

    @app.route('/group/<int:group_id>/invite_link', methods=['GET'])
    @login_required
    def get_invite_link(group_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership:
            return jsonify({'error': 'Вы не в группе'}), 403
        base = app.config.get('LINK_URL', '').rstrip('/')
        link = f"{base}/join/{group.invite_token}" if base else f"/join/{group.invite_token}"
        return jsonify({'invite_link': link, 'token': group.invite_token})

    @app.route('/group/<int:group_id>/reset_invite_link', methods=['POST'])
    @login_required
    def reset_invite_link(group_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        group.invite_token = secrets.token_urlsafe(32)
        db.session.commit()
        base = app.config.get('LINK_URL', '').rstrip('/')
        link = f"{base}/join/{group.invite_token}" if base else f"/join/{group.invite_token}"
        return jsonify({'invite_link': link, 'token': group.invite_token})

    # ── Вступление по ссылке (/join/<token>) ─────────────────────────────────

    @app.route('/join/<token>')
    @login_required
    def join_by_invite(token):
        group = Group.query.filter_by(invite_token=token).first_or_404()
        existing = GroupMember.query.filter_by(group_id=group.id, user_id=current_user.id).first()
        if existing:
            return redirect(url_for('group_chat', group_id=group.id))
        members_count = GroupMember.query.filter_by(group_id=group.id).count()
        pending_request = GroupJoinRequest.query.filter_by(
            group_id=group.id, user_id=current_user.id, status='pending'
        ).first()
        return render_template('group_join_preview.html',
                               group=group,
                               pending_request=pending_request,
                               members_count=members_count,
                               via_token=token,
                               is_banned=GroupBan.query.filter_by(group_id=group.id, user_id=current_user.id).first() is not None)

    # ── Вступление по юзернейму группы (/g/<username>) ───────────────────────

    @app.route('/g/<group_username>')
    @login_required
    def join_by_username(group_username):
        group = Group.query.filter_by(group_username=group_username).first_or_404()
        if group.visibility == 'private':
            abort(404)
        existing = GroupMember.query.filter_by(group_id=group.id, user_id=current_user.id).first()
        if existing:
            return redirect(url_for('group_chat', group_id=group.id))
        members_count = GroupMember.query.filter_by(group_id=group.id).count()
        pending_request = GroupJoinRequest.query.filter_by(
            group_id=group.id, user_id=current_user.id, status='pending'
        ).first()
        return render_template('group_join_preview.html',
                               group=group,
                               pending_request=pending_request,
                               members_count=members_count,
                               is_banned=GroupBan.query.filter_by(group_id=group.id, user_id=current_user.id).first() is not None)

    # ── POST: непосредственное вступление / отправка заявки ──────────────────

    @app.route('/group/<int:group_id>/join', methods=['POST'])
    @login_required
    def join_group(group_id):
        group = Group.query.get_or_404(group_id)
        existing = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if existing:
            return jsonify({'error': 'Вы уже в группе'}), 400

        if GroupBan.query.filter_by(group_id=group_id, user_id=current_user.id).first():
            return jsonify({'error': 'Вы заблокированы в этой группе'}), 403

        data = request.get_json(silent=True) or {}
        invite_token = data.get('invite_token') or request.form.get('invite_token')

        if group.visibility == 'private':
            if not invite_token or invite_token != group.invite_token:
                return jsonify({'error': 'Для вступления нужна ссылка-приглашение'}), 403

        if group.join_type == 'open':
            new_member = GroupMember(group_id=group_id, user_id=current_user.id, role='member')
            db.session.add(new_member)
            db.session.commit()
            return jsonify({'success': True, 'joined': True,
                            'redirect': url_for('group_chat', group_id=group_id)})
        else:
            existing_req = GroupJoinRequest.query.filter_by(
                group_id=group_id, user_id=current_user.id
            ).first()
            if existing_req:
                if existing_req.status == 'pending':
                    return jsonify({'error': 'Заявка уже отправлена'}), 400
                existing_req.status = 'pending'
                existing_req.created_at = datetime.now(timezone.utc)
                existing_req.reviewed_at = None
                existing_req.reviewed_by_id = None
                db.session.commit()
                return jsonify({'success': True, 'joined': False,
                                'message': 'Заявка на вступление отправлена'})
            req = GroupJoinRequest(group_id=group_id, user_id=current_user.id)
            db.session.add(req)
            db.session.commit()
            return jsonify({'success': True, 'joined': False,
                            'message': 'Заявка на вступление отправлена'})

    # ── Список заявок (для админов) ───────────────────────────────────────────

    @app.route('/group/<int:group_id>/join_requests')
    @login_required
    def group_join_requests(group_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        pending = GroupJoinRequest.query.filter_by(
            group_id=group_id, status='pending'
        ).options(joinedload(GroupJoinRequest.user)).order_by(GroupJoinRequest.created_at).all()
        result = [{
            'id': r.id,
            'user_id': r.user_id,
            'username': r.user.username,
            'avatar': r.user.avatar,
            'created_at': r.created_at.isoformat() + 'Z'
        } for r in pending]
        return jsonify({'requests': result, 'count': len(result)})

    # ── Одобрить / отклонить заявку ───────────────────────────────────────────

    @app.route('/group/<int:group_id>/join_requests/<int:request_id>/approve', methods=['POST'])
    @login_required
    def approve_join_request(group_id, request_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        req = GroupJoinRequest.query.filter_by(id=request_id, group_id=group_id).first_or_404()
        if req.status != 'pending':
            return jsonify({'error': 'Заявка уже обработана'}), 400
        if GroupBan.query.filter_by(group_id=group_id, user_id=req.user_id).first():
            req.status = 'rejected'
            req.reviewed_at = datetime.now(timezone.utc)
            req.reviewed_by_id = current_user.id
            db.session.commit()
            return jsonify({'error': 'Пользователь заблокирован в группе'}), 400
        # Проверим, вдруг уже член
        existing = GroupMember.query.filter_by(group_id=group_id, user_id=req.user_id).first()
        if not existing:
            new_member = GroupMember(group_id=group_id, user_id=req.user_id, role='member')
            db.session.add(new_member)
        req.status = 'approved'
        req.reviewed_at = datetime.now(timezone.utc)
        req.reviewed_by_id = current_user.id
        db.session.commit()
        user = User.query.get(req.user_id)
        return jsonify({
            'success': True,
            'member': {
                'user_id': req.user_id,
                'username': user_display_name(user),
                'avatar': user.avatar if user else '',
                'role': 'member',
            },
        })

    @app.route('/group/<int:group_id>/join_requests/<int:request_id>/reject', methods=['POST'])
    @login_required
    def reject_join_request(group_id, request_id):
        group = Group.query.get_or_404(group_id)
        membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
        if not membership or membership.role not in ['owner', 'admin']:
            return jsonify({'error': 'Недостаточно прав'}), 403
        req = GroupJoinRequest.query.filter_by(id=request_id, group_id=group_id).first_or_404()
        if req.status != 'pending':
            return jsonify({'error': 'Заявка уже обработана'}), 400
        req.status = 'rejected'
        req.reviewed_at = datetime.now(timezone.utc)
        req.reviewed_by_id = current_user.id
        db.session.commit()
        return jsonify({'success': True})

    # ── Поиск публичных групп ─────────────────────────────────────────────────

    @app.route('/groups/search')
    @login_required
    def search_groups():
        q = request.args.get('q', '').strip()
        if not q:
            return jsonify({'groups': []})
        q_lower = q.lower()
        # name зашифровано — нельзя фильтровать через SQL LIKE.
        # Загружаем все публичные группы и фильтруем по расшифрованному имени в Python.
        all_public = Group.query.filter(Group.visibility == 'public').all()
        results = [
            g for g in all_public
            if q_lower in (g.name or '').lower()
            or (g.group_username and q_lower in g.group_username.lower())
        ][:20]
        data = []
        for g in results:
            is_member = bool(GroupMember.query.filter_by(group_id=g.id, user_id=current_user.id).first())
            data.append({
                'id': g.id,
                'name': g.name,
                'description': g.description,
                'icon': url_for('serve_media', filepath=g.icon),
                'members_count': GroupMember.query.filter_by(group_id=g.id).count(),
                'join_type': g.join_type,
                'group_username': g.group_username,
                'is_member': is_member
            })
        return jsonify({'groups': data})

    @app.route('/group/<int:group_id>/delete', methods=['POST'])
    @login_required
    def delete_group(group_id):
        group = Group.query.get_or_404(group_id)

        if current_user.id != group.owner_id:
            return jsonify({'error': 'Только владелец может удалить группу'}), 403

        group_icon = group.icon

        try:
            messages = Message.query.filter_by(group_id=group_id).all()
            for message in messages:
                ForwardedMessage.query.filter(
                    (ForwardedMessage.original_message_id == message.id) |
                    (ForwardedMessage.forwarded_message_id == message.id)
                ).delete()

            Message.query.filter_by(group_id=group_id).delete()
            GroupMember.query.filter_by(group_id=group_id).delete()
            db.session.delete(group)
            db.session.commit()

            # Удаляем файл иконки группы с диска (если не дефолтная заглушка)
            delete_uploaded_file(group_icon, app)

            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Ошибка при удалении группы: {str(e)}'}), 500

    @app.route('/group/<int:group_id>/leave', methods=['POST'])
    @login_required
    def leave_group(group_id):
        group = Group.query.get_or_404(group_id)

        if current_user.id == group.owner_id:
            return jsonify({'error': 'Владелец не может покинуть группу. Передайте права или удалите группу.'}), 400

        membership = GroupMember.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if membership:
            db.session.delete(membership)
            db.session.commit()

        return jsonify({'success': True})

    # ============================================================
    # СООБЩЕНИЯ
    # ============================================================

    @app.route('/send_message', methods=['POST'])
    @login_required
    @rate_limit('send_message')
    def send_message():
        try:
            chat_id     = request.form.get('chat_id')
            group_id    = request.form.get('group_id')
            content     = request.form.get('content')
            file        = request.files.get('file')
            reply_to_id = request.form.get('reply_to_id')

            has_file = bool(file and file.filename)
            if content:
                content = InputSanitizer.sanitize_text(content)
            has_content = bool(content and content.strip())
            if not has_file and not has_content:
                return jsonify({'error': 'Пустое сообщение'}), 400

            message = Message(
                sender_id=current_user.id,
                content=content if content else None,
                reply_to_id=reply_to_id if reply_to_id else None
            )

            # ─────────────────────────
            # ЧАТ / ГРУППА
            # ─────────────────────────
            if chat_id:
                chat_obj = Chat.query.get_or_404(chat_id)

                if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                    return jsonify({'error': 'Нет доступа'}), 403

                if chat_obj.is_saved:
                    if chat_obj.user1_id != current_user.id:
                        return jsonify({'error': 'Нет доступа'}), 403
                    message.chat_id = chat_id
                    message.receiver_id = current_user.id
                    message.is_read = True
                    chat_obj.last_message_at = datetime.now(timezone.utc)
                else:
                    bot = get_proton_bot_user(db)
                    if bot and bot.id in (chat_obj.user1_id, chat_obj.user2_id):
                        return jsonify({'error': 'В этот чат нельзя отправлять сообщения'}), 403

                    receiver_id = chat_obj.user2_id if chat_obj.user1_id == current_user.id else chat_obj.user1_id

                    if _is_blocked_between(current_user.id, receiver_id):
                        return jsonify({'error': 'blocked'}), 403

                    if not can_send_private_message(current_user.id, receiver_id, chat_obj):
                        return jsonify({'error': 'Пользователь принимает сообщения только от контактов'}), 403

                    message.chat_id     = chat_id
                    message.receiver_id = receiver_id
                    chat_obj.last_message_at = datetime.now(timezone.utc)

            elif group_id:
                group = Group.query.get_or_404(group_id)

                membership = GroupMember.query.filter_by(
                    group_id=group_id,
                    user_id=current_user.id
                ).first()

                if not membership:
                    return jsonify({'error': 'Нет доступа'}), 403

                if getattr(group, 'write_permission', 'all') == 'admins_only':
                    if membership.role not in ['owner', 'admin']:
                        return jsonify({'error': 'write_restricted'}), 403

                message.group_id      = group_id
                group.last_message_at = datetime.now(timezone.utc)

            else:
                return jsonify({'error': 'Укажите chat_id или group_id'}), 400

            if reply_to_id:
                try:
                    reply_msg = Message.query.get(int(reply_to_id))
                except (TypeError, ValueError):
                    reply_msg = None
                if not reply_msg or not user_can_access_message(current_user.id, reply_msg):
                    return jsonify({'error': 'Нельзя ответить на это сообщение'}), 403

            # ─────────────────────────
            # ФАЙЛ
            # ─────────────────────────
            if file and file.filename:
                filename = file.filename
                if not allowed_file(filename):
                    return jsonify({'error': 'Этот тип файла запрещён для загрузки'}), 400
                file_category = get_file_category(filename)

                message.file_category = file_category
                message.file_name     = filename
                message.file_type     = file.content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'

                file.seek(0, os.SEEK_END)
                file_size = file.tell()
                file.seek(0)

                if is_file_too_large(file_size, app):
                    return jsonify({'error': 'Файл слишком большой'}), 400

                file_check = FileSecurityChecker.check_file(
                    filename, file_size, file_category, current_user.id
                )
                if file_check['blocked']:
                    return jsonify({'error': file_check['warning'] or 'Файл отклонён'}), 400

                filepath, size = save_file(file, file_category, app)

                if file_category == 'image':
                    message.image_path = filepath
                else:
                    message.file_path = filepath

                message.file_size = size

            # ─────────────────────────
            # СОХРАНЕНИЕ
            # ─────────────────────────
            db.session.add(message)
            db.session.commit()

            # ─────────────────────────
            # PREVIEW (фикс бага)
            # ─────────────────────────
            if content and contains_url(content):
                urls = extract_urls_from_text(content)

                if urls:
                    def fetch_preview_async(app_ctx, msg_id, url):
                        with app_ctx:
                            try:
                                preview_data = extract_link_preview(url)
                                if preview_data:
                                    msg = Message.query.get(msg_id)
                                    if msg:
                                        msg.link_url = preview_data['url']
                                        msg.link_title = preview_data['title']
                                        msg.link_description = preview_data['description']
                                        msg.link_image = preview_data['image']
                                        msg.link_fetched_at = datetime.now(timezone.utc)
                                        db.session.commit()
                            except Exception as e:
                                print("Preview error:", e)

                    threading.Thread(
                        target=fetch_preview_async,
                        args=(app.app_context(), message.id, urls[0]),
                        daemon=True
                    ).start()

            # ─────────────────────────
            # SOCKET (ВСЕГДА!)
            # ─────────────────────────
            from socketio_events import socketio, _serialize_message, _emit_chat_update, _emit_group_update

            payload = _serialize_message(message, current_user.id, app)

            if message.chat_id:
                socketio.emit('new_message', payload, to=f'chat_{message.chat_id}')
                _emit_chat_update(message.chat_id, message, message.receiver_id)

            elif message.group_id:
                socketio.emit('new_message', payload, to=f'group_{message.group_id}')
                _emit_group_update(message.group_id, message)

            payload['success'] = True
            payload['message_id'] = payload.pop('id')
            return jsonify(payload)

        except Exception as e:
            print("SEND_MESSAGE ERROR:", e)
            db.session.rollback()
            return jsonify({'error': 'Не удалось отправить сообщение'}), 500

    @app.route('/forward_message', methods=['POST'])
    @login_required
    def forward_message():
        message_id      = request.form.get('message_id')
        target_type     = request.form.get('target_type')
        target_id       = request.form.get('target_id')
        show_sender     = request.form.get('show_sender', 'true').lower() == 'true'
        additional_text = request.form.get('additional_text', '')

        original_message = Message.query.get_or_404(message_id)

        if original_message.chat_id:
            chat = Chat.query.get(original_message.chat_id)
            if not chat:
                return jsonify({'error': 'Нет доступа к оригинальному сообщению'}), 403
            if chat.is_saved:
                if chat.user1_id != current_user.id:
                    return jsonify({'error': 'Нет доступа к оригинальному сообщению'}), 403
            elif chat.user1_id != current_user.id and chat.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа к оригинальному сообщению'}), 403
        elif original_message.group_id:
            membership = GroupMember.query.filter_by(
                group_id=original_message.group_id,
                user_id=current_user.id
            ).first()
            if not membership:
                return jsonify({'error': 'Нет доступа к оригинальному сообщению'}), 403

        if original_message.content and additional_text:
            forwarded_content = f"{additional_text}\n\n{original_message.content}"
        elif original_message.content:
            forwarded_content = original_message.content
        elif additional_text:
            forwarded_content = additional_text
        else:
            forwarded_content = None

        forwarded_message = Message(
            sender_id=current_user.id,
            content=forwarded_content,
            is_forwarded=True,
            forwarded_from_id=original_message.id,
            show_forward_sender=show_sender
        )

        if original_message.image_path:
            forwarded_message.image_path = original_message.image_path
            forwarded_message.message_type = 'file'
        elif original_message.file_path:
            forwarded_message.file_path     = original_message.file_path
            forwarded_message.file_name     = original_message.file_name
            forwarded_message.file_type     = original_message.file_type
            forwarded_message.file_size     = original_message.file_size
            forwarded_message.file_category = original_message.file_category
            forwarded_message.message_type  = 'file'

        if target_type == 'saved':
            saved_chat = get_or_create_saved_chat(current_user.id, db)
            forwarded_message.chat_id = saved_chat.id
            forwarded_message.receiver_id = current_user.id
            forwarded_message.is_read = True
            saved_chat.last_message_at = datetime.now(timezone.utc)

        elif target_type == 'chat':
            chat_obj = Chat.query.get_or_404(target_id)
            if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа к чату'}), 403

            receiver_id = chat_obj.user2_id if chat_obj.user1_id == current_user.id else chat_obj.user1_id
            forwarded_message.chat_id     = target_id
            forwarded_message.receiver_id = receiver_id
            chat_obj.last_message_at      = datetime.now(timezone.utc)

        elif target_type == 'group':
            group      = Group.query.get_or_404(target_id)
            membership = GroupMember.query.filter_by(
                group_id=target_id,
                user_id=current_user.id
            ).first()

            if not membership:
                return jsonify({'error': 'Нет доступа к группе'}), 403

            forwarded_message.group_id = target_id
            group.last_message_at      = datetime.now(timezone.utc)

        else:
            return jsonify({'error': 'Неизвестный тип получателя'}), 400

        db.session.add(forwarded_message)
        db.session.flush()

        forwarded_record = ForwardedMessage(
            original_message_id=original_message.id,
            forwarded_message_id=forwarded_message.id,
            forwarded_by_id=current_user.id,
            forwarded_to_chat_id=forwarded_message.chat_id,
            forwarded_to_group_id=forwarded_message.group_id,
            show_sender=show_sender
        )

        db.session.add(forwarded_record)
        db.session.commit()

        # Эмитим WS-событие new_message — получатели увидят пересланное сообщение мгновенно
        try:
            from socketio_events import socketio as _sio, _serialize_message, _emit_chat_update, _emit_group_update
            payload = _serialize_message(forwarded_message, current_user.id, app)
            if forwarded_message.chat_id:
                _sio.emit('new_message', payload, to=f'chat_{forwarded_message.chat_id}')
                _emit_chat_update(forwarded_message.chat_id, forwarded_message, forwarded_message.receiver_id)
            elif forwarded_message.group_id:
                _sio.emit('new_message', payload, to=f'group_{forwarded_message.group_id}')
                _emit_group_update(forwarded_message.group_id, forwarded_message)
        except Exception:
            pass  # Не блокируем ответ если WS недоступен

        return jsonify({
            'success': True,
            'message_id': forwarded_message.id,
            'timestamp': forwarded_message.timestamp.isoformat() + 'Z'
        })

    # ── Реакции на сообщения ─────────────────────────────────────────────────

    ALLOWED_REACTIONS = {'❤️', '👍', '👎', '🔥', '🥰'}

    @app.route('/message/<int:message_id>/react', methods=['POST'])
    @login_required
    def react_to_message(message_id):
        from socketio_events import socketio as sio
        msg = Message.query.get_or_404(message_id)
        if not user_can_access_message(current_user.id, msg):
            return jsonify({'error': 'Нет доступа'}), 403
        emoji = request.json.get('emoji', '')
        if emoji not in ALLOWED_REACTIONS:
            return jsonify({'error': 'Недопустимая реакция'}), 400

        existing = MessageReaction.query.filter_by(
            message_id=message_id, user_id=current_user.id
        ).first()

        if existing:
            if existing.emoji == emoji:
                # Снять реакцию
                db.session.delete(existing)
                db.session.commit()
                action = 'removed'
            else:
                # Заменить реакцию
                existing.emoji = emoji
                db.session.commit()
                action = 'updated'
        else:
            r = MessageReaction(message_id=message_id, user_id=current_user.id, emoji=emoji)
            db.session.add(r)
            db.session.commit()
            action = 'added'

        # Собираем актуальные реакции для рассылки
        reactions = _get_reactions_summary(message_id)
        room = f'group_{msg.group_id}' if msg.group_id else f'chat_{msg.chat_id}'

        # my_reactions — словарь {user_id: emoji} чтобы каждый клиент знал своё
        my_reactions = {
            str(r.user_id): r.emoji
            for r in MessageReaction.query.filter_by(message_id=message_id).all()
        }

        sio.emit('reaction_updated', {
            'message_id': message_id,
            'reactions': reactions,
            'my_reactions': my_reactions,
            'action': action,
            'user_id': current_user.id,
            'emoji': emoji
        }, room=room)

        return jsonify({'success': True, 'reactions': reactions, 'action': action})

    @app.route('/message/<int:message_id>/reactions')
    @login_required
    def get_message_reactions(message_id):
        Message.query.get_or_404(message_id)
        details = _get_reactions_detail(message_id)
        return jsonify({'reactions': details})

    def _get_reactions_summary(message_id):
        """Возвращает {emoji: count} для сообщения."""
        from sqlalchemy import func as sqlfunc
        rows = db.session.query(MessageReaction.emoji, sqlfunc.count(MessageReaction.id))\
            .filter_by(message_id=message_id).group_by(MessageReaction.emoji).all()
        return {emoji: count for emoji, count in rows}

    def _get_reactions_detail(message_id):
        """Возвращает {emoji: [username, ...]} для модального окна."""
        reactions = MessageReaction.query.filter_by(message_id=message_id)\
            .options(db.joinedload(MessageReaction.user)).all()
        result = {}
        for r in reactions:
            result.setdefault(r.emoji, []).append(user_display_name(r.user))
        return result

    @app.route('/get_chats_and_groups_for_forward')
    @login_required
    def get_chats_and_groups_for_forward():
        saved_chat = get_saved_chat(current_user.id, db)
        saved_target = {
            'id': saved_chat.id if saved_chat else 0,
            'type': 'saved',
            'name': SAVED_CHAT_NAME,
            'avatar': url_for('serve_media', filepath=current_user.avatar),
            'last_message_time': '',
            'last_message_ts': '',
        }

        user_chats = Chat.query.filter(
            (Chat.user1_id == current_user.id) | (Chat.user2_id == current_user.id),
            Chat.is_saved == False,
        ).order_by(Chat.last_message_at.desc()).all()

        chats_data = []
        for chat in user_chats:
            other_user = chat.user2 if chat.user1_id == current_user.id else chat.user1
            chats_data.append({
                'id': chat.id,
                'type': 'chat',
                'name': other_user.display_name,
                'avatar': url_for('serve_media', filepath=other_user.avatar),
                'last_message_time': _format_chat_time(chat.last_message_at),
                'last_message_ts': chat.last_message_at.isoformat() if chat.last_message_at else ''
            })

        user_groups = GroupMember.query.filter_by(user_id=current_user.id).all()
        groups_data = []
        for membership in user_groups:
            group = membership.group
            groups_data.append({
                'id': group.id,
                'type': 'group',
                'name': group.name,
                'avatar': url_for('serve_media', filepath=group.icon),
                'members_count': len(group.members),
                'last_message_time': _format_chat_time(group.last_message_at),
                'last_message_ts': group.last_message_at.isoformat() if group.last_message_at else ''
            })

        all_targets = [saved_target] + chats_data + groups_data
        all_targets.sort(key=lambda x: (0 if x['type'] == 'saved' else 1, x.get('last_message_ts') or ''), reverse=False)
        # saved всегда первый; остальные — по времени убыванию
        rest = [t for t in all_targets if t['type'] != 'saved']
        rest.sort(key=lambda x: x.get('last_message_ts') or '', reverse=True)
        return jsonify([saved_target] + rest)

    @app.route('/get_messages/<int:chat_id>')
    @login_required
    def get_messages(chat_id):
        last_id   = request.args.get('last_id',   0, type=int)
        before_id = request.args.get('before_id', 0, type=int)
        is_group  = request.args.get('is_group', False, type=bool)

        if is_group:
            group      = Group.query.get_or_404(chat_id)
            membership = GroupMember.query.filter_by(
                group_id=chat_id,
                user_id=current_user.id
            ).first()

            if not membership:
                return jsonify({'error': 'Нет доступа'}), 403

            q = Message.query.filter(
                Message.group_id == chat_id,
                Message.is_deleted == False
            ).options(
                joinedload(Message.sender),
                joinedload(Message.reply_to),
                joinedload(Message.forwarded_from)
            )
            if before_id:
                messages = list(reversed(q.filter(Message.id < before_id).order_by(Message.timestamp.desc()).limit(30).all()))
            else:
                messages = q.filter(Message.id > last_id).order_by(Message.timestamp).all()

            for msg in messages:
                if msg.sender_id != current_user.id and not msg.is_read:
                    msg.is_read = True

            db.session.commit()
        else:
            chat_obj = Chat.query.get_or_404(chat_id)
            if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа'}), 403

            q = Message.query.filter(
                Message.chat_id == chat_id,
                Message.is_deleted == False
            ).options(
                joinedload(Message.sender),
                joinedload(Message.reply_to),
                joinedload(Message.forwarded_from)
            )
            if before_id:
                messages = list(reversed(q.filter(Message.id < before_id).order_by(Message.timestamp.desc()).limit(30).all()))
            else:
                messages = q.filter(Message.id > last_id).order_by(Message.timestamp).all()

            for msg in messages:
                if msg.receiver_id == current_user.id and not msg.is_read:
                    msg.is_read = True

            db.session.commit()

        # Собираем все нужные sender_id для reply/forward одним запросом
        extra_user_ids = set()
        for msg in messages:
            if msg.reply_to:
                extra_user_ids.add(msg.reply_to.sender_id)
            if msg.forwarded_from:
                extra_user_ids.add(msg.forwarded_from.sender_id)

        extra_users = {}
        if extra_user_ids:
            for u in User.query.filter(User.id.in_(extra_user_ids)).all():
                extra_users[u.id] = u

        member_roles = {}
        if is_group and messages:
            for gm in GroupMember.query.filter_by(group_id=chat_id).all():
                member_roles[gm.user_id] = gm.role

        messages_data = []
        for msg in messages:
            sender = msg.sender

            file_url = None
            if msg.file_path:
                file_url = url_for('download_file', filepath=msg.file_path)
            elif msg.image_path:
                file_url = url_for('download_file', filepath=msg.image_path)

            file_size_formatted = format_file_size(msg.file_size) if msg.file_size else None
            file_icon           = get_file_icon(msg.file_name) if msg.file_name else '[file]'

            reply_to_data = None
            if msg.reply_to:
                reply_msg    = msg.reply_to
                reply_sender = extra_users.get(reply_msg.sender_id) or reply_msg.sender
                reply_to_data = {
                    'id': reply_msg.id,
                    'sender_id': reply_msg.sender_id,
                    'sender_name': user_display_name(reply_sender),
                    'content': reply_msg.content[:100] if reply_msg.content else None,
                    'has_image': bool(reply_msg.image_path),
                    'has_file': bool(reply_msg.file_path),
                    'file_name': reply_msg.file_name
                }

            forwarded_from_data = None
            if msg.is_forwarded and msg.forwarded_from:
                original_msg    = msg.forwarded_from
                original_sender = extra_users.get(original_msg.sender_id) or original_msg.sender
                forwarded_from_data = {
                    'id': original_msg.id,
                    'sender_id': original_msg.sender_id,
                    'sender_name': user_display_name(original_sender),
                    'show_sender': msg.show_forward_sender
                }

            messages_data.append({
                'id': msg.id,
                'message_type': getattr(msg, 'message_type', None) or 'text',
                'sender_id': msg.sender_id,
                'sender_name': user_display_name(sender),
                'sender_username': user_display_name(sender),
                'sender_role': member_roles.get(msg.sender_id) if is_group else None,
                'sender_avatar': url_for('serve_media', filepath=sender.avatar) if sender and sender.avatar else '',
                'content': msg.content,
                'image_path': url_for('download_file', filepath=msg.image_path) if msg.image_path else None,
                'file_path': file_url,
                'file_name': msg.file_name,
                'file_type': msg.file_type,
                'file_size': file_size_formatted,
                'file_category': msg.file_category or get_file_category(msg.file_name) if msg.file_name else None,
                'file_icon': file_icon,
                'timestamp': msg.timestamp.isoformat() + 'Z',
                'is_read': msg.is_read,
                'is_edited': msg.is_edited,
                'reply_to': reply_to_data,
                'is_forwarded': msg.is_forwarded,
                'forwarded_from': forwarded_from_data,
                'show_forward_sender': msg.show_forward_sender,
                'link_url': msg.link_url,
                'link_title': msg.link_title,
                'link_description': msg.link_description,
                'link_image': msg.link_image,
            })
            if is_group:
                messages_data[-1]['read_count'] = msg.read_count or 0
                messages_data[-1]['members_count'] = max(GroupMember.query.filter_by(group_id=chat_id).count() - 1, 0)

        return jsonify(messages_data)

    @app.route('/message_context/<int:message_id>')
    @login_required
    def message_context(message_id):
        """Возвращает окно сообщений вокруг указанного id (для перехода к закреплённым/поиску)."""
        from socketio_events import _serialize_message
        chat_id = request.args.get('chat_id', type=int)
        group_id = request.args.get('group_id', type=int)

        target = Message.query.get_or_404(message_id)
        if target.is_deleted:
            return jsonify({'error': 'Сообщение удалено'}), 404

        if chat_id:
            if target.chat_id != chat_id:
                return jsonify({'error': 'Нет доступа'}), 403
            chat_obj = Chat.query.get_or_404(chat_id)
            if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа'}), 403
            base_q = Message.query.filter(
                Message.chat_id == chat_id,
                Message.is_deleted == False
            )
        elif group_id:
            if target.group_id != group_id:
                return jsonify({'error': 'Нет доступа'}), 403
            membership = GroupMember.query.filter_by(group_id=group_id, user_id=current_user.id).first()
            if not membership:
                return jsonify({'error': 'Нет доступа'}), 403
            base_q = Message.query.filter(
                Message.group_id == group_id,
                Message.is_deleted == False
            )
        else:
            return jsonify({'error': 'Укажите chat_id или group_id'}), 400

        before_ids = [
            m.id for m in base_q.filter(Message.id < message_id)
            .order_by(Message.id.desc()).limit(30).all()
        ]
        after_ids = [
            m.id for m in base_q.filter(Message.id >= message_id)
            .order_by(Message.id.asc()).limit(31).all()
        ]
        all_ids = sorted(set(before_ids + after_ids))
        if not all_ids:
            return jsonify({'error': 'Сообщение не найдено'}), 404

        messages = Message.query.filter(Message.id.in_(all_ids)).options(
            joinedload(Message.sender),
            joinedload(Message.reply_to),
            joinedload(Message.forwarded_from),
        ).order_by(Message.id.asc()).all()

        serialized = [_serialize_message(m, current_user.id, app) for m in messages]
        return jsonify({'messages': serialized, 'target_id': message_id})

    def _serve_upload_response(filepath, as_attachment=False):
        """Безопасная выдача файла из uploads с проверкой прав."""
        from flask import make_response
        rel = normalize_upload_relpath(filepath)
        if not rel or not user_can_access_upload(current_user.id, rel, db):
            abort(403)
        resolved = resolve_upload_file(app, rel)
        if not resolved:
            abort(404)
        directory, filename = resolved
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = 'application/octet-stream'
        force_attachment = as_attachment
        if not as_attachment and not is_inline_safe_mime(mime_type):
            force_attachment = True
        if len(filename) > 33 and filename[32] == '_':
            download_name = filename[33:]
        else:
            download_name = filename
        response = make_response(send_from_directory(
            directory,
            filename,
            as_attachment=force_attachment,
            download_name=download_name if force_attachment else None,
            mimetype=mime_type,
        ))
        if not force_attachment:
            response.headers['Content-Disposition'] = 'inline'
        response.headers['Cache-Control'] = 'private, max-age=604800'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.route('/media/<path:filepath>')
    @login_required
    def serve_media(filepath):
        return _serve_upload_response(filepath, as_attachment=False)

    @app.route('/view/<path:filepath>')
    @login_required
    def view_file(filepath):
        return _serve_upload_response(filepath, as_attachment=False)

    @app.route('/download/<path:filepath>')
    @login_required
    def download_file(filepath):
        return _serve_upload_response(filepath, as_attachment=True)

    @app.route('/delete_message/<int:message_id>', methods=['POST'])
    @login_required
    def delete_message(message_id):
        message = Message.query.get_or_404(message_id)
        if not can_delete_group_message(message, current_user.id, GroupMember):
            return jsonify({'error': 'Нет прав для удаления этого сообщения'}), 403
        chat_id = message.chat_id
        group_id = message.group_id
        # Удаляем файл/изображение с диска только если больше нет сообщений с этим файлом
        for fpath in [message.image_path, message.file_path]:
            if fpath:
                refs = Message.query.filter(
                    (Message.image_path == fpath) | (Message.file_path == fpath),
                    Message.is_deleted == False,
                    Message.id != message.id
                ).count()
                if refs == 0:
                    full_path = os.path.join(app.root_path, app.config['UPLOAD_FOLDER'], fpath)
                    if os.path.exists(full_path):
                        try:
                            os.remove(full_path)
                        except Exception:
                            pass
        message.is_deleted = True
        message.content    = None
        removed_saved_chat = False
        if chat_id:
            chat = Chat.query.get(chat_id)
            if chat and chat.is_saved:
                removed_saved_chat = cleanup_empty_saved_chat(chat, db)
        db.session.commit()
        # Уведомляем других участников через WebSocket
        try:
            from socketio_events import broadcast_message_deleted, broadcast_chat_deleted
            broadcast_message_deleted(message_id, chat_id=chat_id, group_id=group_id)
            if removed_saved_chat and chat_id:
                broadcast_chat_deleted(chat_id, current_user.id, current_user.id)
        except Exception:
            pass
        return jsonify({'success': True, 'saved_chat_removed': removed_saved_chat})

    @app.route('/edit_message/<int:message_id>', methods=['POST'])
    @login_required
    def edit_message(message_id):
        message = Message.query.get_or_404(message_id)
        if message.sender_id != current_user.id:
            return jsonify({'error': 'Нет прав для редактирования этого сообщения'}), 403
        new_content = InputSanitizer.sanitize_text(request.form.get('content', '').strip())
        if not new_content:
            return jsonify({'error': 'Текст сообщения не может быть пустым'}), 400
        chat_id = message.chat_id
        group_id = message.group_id
        message.content   = new_content
        message.is_edited = True
        db.session.commit()
        # Уведомляем через WebSocket
        try:
            from socketio_events import broadcast_message_edited
            broadcast_message_edited(message_id, new_content, chat_id=chat_id, group_id=group_id)
        except Exception:
            pass
        return jsonify({'success': True, 'content': new_content})

    @app.route('/get_unread_counts')
    @login_required
    def get_unread_counts():
        private_chats = Chat.query.filter(
            (Chat.user1_id == current_user.id) | (Chat.user2_id == current_user.id)
        ).all()

        total_unread = 0

        for chat in private_chats:
            unread = Message.query.filter_by(
                chat_id=chat.id,
                receiver_id=current_user.id,
                is_read=False
            ).count()
            total_unread += unread

        user_groups = GroupMember.query.filter_by(user_id=current_user.id).all()
        for membership in user_groups:
            unread = Message.query.filter_by(
                group_id=membership.group_id,
                is_read=False
            ).filter(Message.sender_id != current_user.id).count()
            total_unread += unread

        return jsonify({'unread_count': total_unread})

    # ============================================================
    # ЗАКРЕПЛЕНИЕ СООБЩЕНИЙ
    # ============================================================

    @app.route('/pin_message/<int:message_id>', methods=['POST'])
    @login_required
    def pin_message(message_id):
        message = Message.query.get_or_404(message_id)
        if message.chat_id:
            chat_obj = Chat.query.get(message.chat_id)
            if not chat_obj or (chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id):
                return jsonify({'error': 'Нет доступа'}), 403
        elif message.group_id:
            membership = GroupMember.query.filter_by(group_id=message.group_id, user_id=current_user.id).first()
            if not membership or membership.role not in ['owner', 'admin']:
                return jsonify({'error': 'Только администраторы могут закреплять сообщения'}), 403
        message.is_pinned    = True
        message.pinned_by_id = current_user.id
        message.pinned_at    = datetime.now(timezone.utc)
        db.session.commit()
        sender = User.query.get(message.sender_id)
        return jsonify({
            'success': True, 'message_id': message.id,
            'content': message.content,
            'sender_name': user_display_name(sender),
            'timestamp': message.timestamp.isoformat() + 'Z',
            'has_image': bool(message.image_path),
            'has_file': bool(message.file_path),
            'file_name': message.file_name
        })

    @app.route('/unpin_message/<int:message_id>', methods=['POST'])
    @login_required
    def unpin_message(message_id):
        message = Message.query.get_or_404(message_id)
        if message.chat_id:
            chat_obj = Chat.query.get(message.chat_id)
            if not chat_obj or (chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id):
                return jsonify({'error': 'Нет доступа'}), 403
        elif message.group_id:
            membership = GroupMember.query.filter_by(group_id=message.group_id, user_id=current_user.id).first()
            if not membership or membership.role not in ['owner', 'admin']:
                return jsonify({'error': 'Только администраторы могут откреплять сообщения'}), 403
        message.is_pinned    = False
        message.pinned_by_id = None
        message.pinned_at    = None
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/get_pinned_messages/<int:context_id>')
    @login_required
    def get_pinned_messages(context_id):
        is_group = request.args.get('is_group', 'false').lower() == 'true'
        if is_group:
            membership = GroupMember.query.filter_by(group_id=context_id, user_id=current_user.id).first()
            if not membership:
                return jsonify({'error': 'Нет доступа'}), 403
            pinned = Message.query.filter_by(group_id=context_id, is_pinned=True, is_deleted=False).order_by(Message.pinned_at.desc()).all()
        else:
            chat_obj = Chat.query.get_or_404(context_id)
            if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа'}), 403
            pinned = Message.query.filter_by(chat_id=context_id, is_pinned=True, is_deleted=False).order_by(Message.pinned_at.desc()).all()
        result = []
        for msg in pinned:
            sender = User.query.get(msg.sender_id)
            result.append({
                'id': msg.id, 'content': msg.content,
                'sender_name': user_display_name(sender),
                'timestamp': msg.timestamp.isoformat() + 'Z',
                'has_image': bool(msg.image_path),
                'has_file': bool(msg.file_path),
                'file_name': msg.file_name
            })
        return jsonify(result)

    # ============================================================
    # ПОИСК В ЧАТЕ
    # ============================================================

    @app.route('/search_messages/<int:context_id>')
    @login_required
    def search_messages(context_id):
        is_group  = request.args.get('is_group', 'false').lower() == 'true'
        query_str = request.args.get('q', '').strip()
        if not query_str:
            return jsonify([])

        if is_group:
            membership = GroupMember.query.filter_by(group_id=context_id, user_id=current_user.id).first()
            if not membership:
                return jsonify({'error': 'Нет доступа'}), 403
            # Шифрование: ilike невозможен — грузим не удалённые сообщения, фильтруем в Python
            all_msgs = Message.query.filter(
                Message.group_id == context_id,
                Message.is_deleted == False
            ).order_by(Message.timestamp.desc()).limit(500).all()
            messages = [m for m in all_msgs if m.content and query_str.lower() in m.content.lower()][:50]
        else:
            chat_obj = Chat.query.get_or_404(context_id)
            if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа'}), 403
            all_msgs = Message.query.filter(
                Message.chat_id == context_id,
                Message.is_deleted == False
            ).options(joinedload(Message.sender)).order_by(Message.timestamp.desc()).limit(500).all()
            messages = [m for m in all_msgs if m.content and query_str.lower() in m.content.lower()][:50]

        result = []
        for msg in messages:
            sender = msg.sender
            result.append({
                'id': msg.id, 'content': msg.content,
                'sender_name': user_display_name(sender),
                'timestamp': msg.timestamp.isoformat() + 'Z',
                'is_mine': msg.sender_id == current_user.id
            })
        return jsonify(result)

    # ============================================================
    # ЭКСПОРТ ЧАТА
    # ============================================================

    @app.route('/export_chat/<int:context_id>')
    @login_required
    def export_chat(context_id):
        """Экспорт истории чата в ZIP (HTML + вложения)."""
        is_group = request.args.get('is_group', False, type=bool)

        if is_group:
            group = Group.query.get_or_404(context_id)
            membership = GroupMember.query.filter_by(
                group_id=context_id, user_id=current_user.id
            ).first()
            if not membership:
                return jsonify({'error': 'Нет доступа'}), 403
            chat_title = group.name
            chat_subtitle = f'{len(group.members)} участников'
            messages = Message.query.filter(
                Message.group_id == context_id,
                Message.is_deleted == False
            ).options(
                joinedload(Message.sender),
                joinedload(Message.reply_to)
            ).order_by(Message.timestamp).all()
        else:
            chat_obj = Chat.query.get_or_404(context_id)
            if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа'}), 403
            other = chat_obj.user2 if chat_obj.user1_id == current_user.id else chat_obj.user1
            chat_title = other.display_name
            chat_subtitle = 'Личный чат'
            messages = Message.query.filter(
                Message.chat_id == context_id,
                Message.is_deleted == False
            ).options(
                joinedload(Message.sender),
                joinedload(Message.reply_to)
            ).order_by(Message.timestamp).all()

        upload_folder = app.config.get('UPLOAD_FOLDER', 'static/uploads')

        attachments = {}
        for msg in messages:
            for fpath in [msg.image_path, msg.file_path]:
                if fpath and fpath not in attachments:
                    full = os.path.join(upload_folder, fpath)
                    if os.path.exists(full):
                        attachments[fpath] = f'files/{fpath}'

        import html as html_mod
        export_time = datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')

        # ── Генерация HTML ─────────────────────────────────────────────────
        lines = []
        lines.append('''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Экспорт: ''' + html_mod.escape(chat_title) + '''</title>
<style>
/* ── Reset & base ────────────────────────── */
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg-primary:#0b141a;
  --bg-secondary:#1f2c33;
  --bg-tertiary:#2a3942;
  --accent:#9f60f5;
  --sent-message:#3b1a7a;
  --sent-message-text:#ffffff;
  --received-message:#1f2c33;
  --text-primary:#ffffff;
  --text-secondary:#8696a0;
  --text-muted:#667781;
  --border:#3c4a53;
  --messages-bg:#0e1c26;
  --transition:all .2s ease;
}
body{
  background:var(--bg-primary);
  color:var(--text-primary);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  font-size:15px;
  line-height:1.5;
  min-height:100vh;
}

/* ── Шапка ───────────────────────────────── */
.export-header{
  position:sticky;top:0;z-index:100;
  background:var(--bg-secondary);
  border-bottom:1px solid var(--border);
  padding:14px 24px;
  display:flex;align-items:center;gap:16px;
}
.header-avatar{
  width:46px;height:46px;border-radius:50%;
  object-fit:cover;flex-shrink:0;
  background:var(--accent);
  display:flex;align-items:center;justify-content:center;
  font-size:18px;font-weight:700;color:#fff;
}
.header-info h1{font-size:17px;font-weight:600;color:var(--text-primary)}
.header-info p{font-size:13px;color:var(--text-secondary);margin-top:2px}
.header-meta{
  margin-left:auto;color:var(--text-muted);
  font-size:12px;text-align:right;line-height:1.7;
}

/* ── Зона сообщений ──────────────────────── */
.messages-bg{
  background:var(--messages-bg);
  min-height:calc(100vh - 75px);
  padding:20px 0;
}
.messages-wrap{max-width:860px;margin:0 auto;padding:0 16px;}

/* ── Разделитель даты ────────────────────── */
.date-divider{
  display:flex;align-items:center;
  justify-content:center;
  margin:20px 0 10px;
}
.date-divider span{
  background:rgba(17,27,33,.8);
  backdrop-filter:blur(4px);
  color:var(--text-secondary);
  font-size:12px;padding:5px 14px;
  border-radius:20px;
  border:1px solid var(--border);
}

/* ── Строка сообщения ────────────────────── */
.message{
  display:flex;
  flex-direction:column;
  margin-bottom:4px;
  max-width:72%;
}
.message.sent{align-self:flex-end}
.message.received{align-self:flex-start}
.message-wrapper{
  display:flex;align-items:flex-end;gap:8px;
}
.message.sent .message-wrapper{flex-direction:row-reverse}

/* ── Аватар ──────────────────────────────── */
.message-avatar{
  width:34px;height:34px;border-radius:50%;
  object-fit:cover;flex-shrink:0;
  align-self:flex-end;
}

/* ── Пузырь ──────────────────────────────── */
.message-bubble{
  padding:10px 14px;
  border-radius:18px;
  word-wrap:break-word;
  overflow-wrap:break-word;
  position:relative;
  max-width:100%;
}
.message.sent .message-bubble{
  background:var(--sent-message);
  color:var(--sent-message-text);
  border-bottom-right-radius:4px;
}
.message.received .message-bubble{
  background:var(--received-message);
  color:var(--text-primary);
  border-bottom-left-radius:4px;
}

/* ── Имя отправителя ─────────────────────── */
.sender-name{
  font-size:13px;font-weight:600;
  color:var(--accent);
  margin-bottom:4px;
}

/* ── Текст сообщения ─────────────────────── */
.message-content{
  margin-bottom:4px;
  line-height:1.55;
  white-space:pre-wrap;
  font-size:15px;
}

/* ── Мета (время) ────────────────────────── */
.message-meta{
  display:flex;justify-content:flex-end;
  align-items:center;gap:4px;
  font-size:11px;margin-top:2px;
}
.message.sent .message-meta{
  color:var(--sent-message-text);opacity:.65;
}
.message.received .message-meta{color:var(--text-muted)}

/* ── Reply ───────────────────────────────── */
.reply-preview{
  background:rgba(255,255,255,.09);
  border-left:3px solid var(--accent);
  padding:6px 10px;
  margin-bottom:8px;
  border-radius:0 8px 8px 0;
}
.reply-sender{
  font-size:12px;font-weight:600;
  color:var(--accent);margin-bottom:2px;
}
.reply-content{
  font-size:12px;color:var(--text-secondary);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:340px;
}

/* ── Forwarded ───────────────────────────── */
.forwarded-header{
  font-size:12px;color:var(--text-muted);
  margin-bottom:6px;padding-bottom:6px;
  border-bottom:1px solid rgba(255,255,255,.1);
  display:flex;align-items:center;gap:5px;
}
.forwarded-header svg{opacity:.7}

/* ── Изображение ─────────────────────────── */
.message-image{
  max-width:300px;max-height:300px;
  border-radius:12px;display:block;
  margin-bottom:4px;
  cursor:pointer;
}
.message-image:hover{opacity:.9}

/* ── Файл ────────────────────────────────── */
.file-message{
  display:flex;align-items:center;gap:12px;
  padding:10px 12px;
  background:rgba(255,255,255,.07);
  border-radius:12px;
  text-decoration:none;
  color:var(--text-primary);
  min-width:220px;max-width:320px;
  transition:background .15s;
}
.file-message:hover{background:rgba(255,255,255,.12)}
.file-icon{font-size:30px;flex-shrink:0}
.file-info{flex:1;min-width:0}
.file-name{
  font-weight:600;font-size:14px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  margin-bottom:3px;
}
.file-size{font-size:11px;color:var(--text-muted)}

/* ── Превью ссылки ───────────────────────── */
.link-preview{
  margin-top:8px;
  border-radius:10px;overflow:hidden;
  border:1px solid rgba(255,255,255,.1);
  background:rgba(255,255,255,.05);
  text-decoration:none;color:var(--text-primary);
  display:block;max-width:320px;
}
.link-preview:hover{border-color:var(--accent)}
.link-preview-img{width:100%;max-height:160px;object-fit:cover;display:block}
.link-preview-body{padding:10px 12px}
.link-preview-title{font-size:14px;font-weight:600;margin-bottom:3px}
.link-preview-desc{font-size:12px;color:var(--text-secondary);margin-bottom:4px}
.link-preview-url{font-size:11px;color:var(--accent)}

/* ── Scrollbar ───────────────────────────── */
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--text-muted)}
</style>
</head>
<body>
''')

        # Шапка
        title_letter = html_mod.escape(chat_title[0].upper()) if chat_title else '?'
        lines.append(f'''
<div class="export-header">
  <div class="header-avatar">{title_letter}</div>
  <div class="header-info">
    <h1>{html_mod.escape(chat_title)}</h1>
    <p>{html_mod.escape(chat_subtitle)}</p>
  </div>
  <div class="header-meta">
    <div>{len(messages)} сообщений</div>
    <div>Экспорт: {export_time}</div>
  </div>
</div>
<div class="messages-bg">
<div class="messages-wrap">
''')

        current_date = None
        for msg in messages:
            msg_date = msg.timestamp.strftime('%d %B %Y')
            if msg_date != current_date:
                current_date = msg_date
                lines.append(f'<div class="date-divider"><span>{msg_date}</span></div>')

            is_me = msg.sender_id == current_user.id
            row_class = 'sent' if is_me else 'received'
            sender = msg.sender
            sender_name = user_display_name(sender)
            time_str = msg.timestamp.strftime('%H:%M')

            # Аватар
            if sender and sender.avatar and sender.avatar in attachments:
                avatar_src = f'files/{sender.avatar}'
            else:
                color = '9f60f5'
                avatar_src = f'https://ui-avatars.com/api/?name={sender_name}&size=68&background={color}&color=fff&bold=true'

            lines.append(f'<div class="message {row_class}">')
            lines.append(f'<div class="message-wrapper">')
            lines.append(f'<img class="message-avatar" src="{avatar_src}" alt="{html_mod.escape(sender_name)}">')
            lines.append('<div class="message-bubble">')

            # Имя в группах или у собеседника
            if is_group or not is_me:
                lines.append(f'<div class="sender-name">{html_mod.escape(sender_name)}</div>')

            # Пересылка
            if msg.is_forwarded and msg.forwarded_from:
                orig = msg.forwarded_from
                orig_name = user_display_name(orig.sender)
                fwd_label = f'от {html_mod.escape(orig_name)}' if msg.show_forward_sender else ''
                lines.append(f'''<div class="forwarded-header">
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 10l5 5-5 5"/><path d="M4 4v7a4 4 0 004 4h12"/></svg>
  Переслано {fwd_label}
</div>''')

            # Цитата
            if msg.reply_to and not msg.reply_to.is_deleted:
                r = msg.reply_to
                r_name = user_display_name(r.sender)
                if r.content:
                    r_text = html_mod.escape(r.content[:80])
                elif r.image_path:
                    r_text = '🖼 Фото'
                else:
                    r_text = '📎 Файл'
                lines.append(f'''<div class="reply-preview">
  <div class="reply-sender">{html_mod.escape(r_name)}</div>
  <div class="reply-content">{r_text}</div>
</div>''')

            # Картинка
            if msg.image_path:
                img_src = attachments.get(msg.image_path, f'files/{msg.image_path}')
                lines.append(f'<img class="message-image" src="{img_src}" alt="Фото" onclick="this.style.maxWidth=this.style.maxWidth?'':\'100vw\'">')

            # Файл
            if msg.file_path:
                file_href = attachments.get(msg.file_path, f'files/{msg.file_path}')
                fname = html_mod.escape(msg.file_name or 'Файл')
                fsize = format_file_size(msg.file_size) if msg.file_size else ''
                # Иконка по расширению
                ext = (msg.file_name or '').rsplit('.', 1)[-1].lower() if msg.file_name else ''
                icon_map = {
                    'pdf':'📄', 'doc':'📝', 'docx':'📝', 'xls':'📊', 'xlsx':'📊',
                    'ppt':'📊', 'pptx':'📊', 'zip':'🗜', 'rar':'🗜', '7z':'🗜',
                    'mp3':'🎵', 'wav':'🎵', 'ogg':'🎵', 'm4a':'🎵',
                    'mp4':'🎬', 'mov':'🎬', 'avi':'🎬', 'mkv':'🎬',
                    'jpg':'🖼', 'jpeg':'🖼', 'png':'🖼', 'gif':'🖼', 'webp':'🖼',
                    'txt':'📃', 'py':'🐍', 'js':'📜', 'html':'🌐', 'css':'🎨',
                }
                icon = icon_map.get(ext, '📎')
                lines.append(f'''<a class="file-message" href="{file_href}" download="{fname}">
  <span class="file-icon">{icon}</span>
  <div class="file-info">
    <div class="file-name">{fname}</div>
    <div class="file-size">{fsize}</div>
  </div>
</a>''')

            # Превью ссылки
            if msg.link_url and msg.link_title:
                lnk_href = html_mod.escape(msg.link_url)
                lnk_title = html_mod.escape(msg.link_title or '')
                lnk_desc = html_mod.escape((msg.link_description or '')[:120])
                lnk_img = f'<img class="link-preview-img" src="{html_mod.escape(msg.link_image)}" onerror="this.remove()">' if msg.link_image else ''
                lines.append(f'''<a class="link-preview" href="{lnk_href}" target="_blank" rel="noopener">
  {lnk_img}
  <div class="link-preview-body">
    <div class="link-preview-title">{lnk_title}</div>
    {f'<div class="link-preview-desc">{lnk_desc}</div>' if lnk_desc else ''}
    <div class="link-preview-url">{lnk_href}</div>
  </div>
</a>''')

            # Текст
            if msg.content:
                safe = html_mod.escape(msg.content)
                lines.append(f'<div class="message-content">{safe}</div>')

            lines.append(f'<div class="message-meta"><span>{time_str}</span></div>')
            lines.append('</div></div></div>')  # bubble, message-wrapper, message

        lines.append('</div></div></body></html>')
        html_content = '\n'.join(lines)

        # ── Сборка ZIP ─────────────────────────────────────────────────────
        zip_buffer = io.BytesIO()
        safe_title = ''.join(c for c in chat_title if c.isalnum() or c in '_ -')[:40]
        html_filename = f'chat_{safe_title}.html'

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(html_filename, html_content.encode('utf-8'))
            from upload_security import upload_base_dirs
            for orig_path, arc_path in attachments.items():
                rel = normalize_upload_relpath(orig_path)
                if not rel:
                    continue
                full = None
                for base in upload_base_dirs(app):
                    candidate = os.path.join(base, rel)
                    if os.path.isfile(candidate):
                        full = candidate
                        break
                if full:
                    try:
                        zf.write(full, arc_path)
                    except Exception:
                        pass

        zip_name = f'export_{safe_title}_user{current_user.id}.zip'
        zip_buffer.seek(0)

        from flask import send_file
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name=zip_name,
            mimetype='application/zip'
        )


    # ============================================================
    # ВОССТАНОВЛЕНИЕ ПАРОЛЯ
    # ============================================================

    @app.route('/forgot_password', methods=['GET', 'POST'])
    @rate_limit('password_check')
    def forgot_password():
        if current_user.is_authenticated:
            return redirect(url_for('chats'))

        if request.method == 'POST':
            username = request.form.get('username')
            email    = request.form.get('email')

            if not username or not email:
                return render_template('forgot_password.html', error='Заполните все поля')

            user = User.query.filter_by(username=username, email_hash=hmac_hash(email)).first()

            if not user:
                return render_template('forgot_password.html',
                                       success='Если данные верны, инструкции отправлены на email')

            PasswordReset.query.filter_by(user_id=user.id, used=False).delete()

            token = secrets.token_urlsafe(32)

            reset_request = PasswordReset(
                user_id=user.id,
                token=token,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
            )

            db.session.add(reset_request)
            db.session.commit()

            session['password_reset_token'] = token
            return redirect(url_for('reset_password'))

        return render_template('forgot_password.html')

    @app.route('/reset_password', methods=['GET', 'POST'])
    @app.route('/reset_password/<token>', methods=['GET', 'POST'])
    def reset_password(token=None):
        if current_user.is_authenticated:
            return redirect(url_for('chats'))

        if token:
            session['password_reset_token'] = token
            return redirect(url_for('reset_password'))

        token = session.get('password_reset_token')
        if not token:
            return render_template('forgot_password.html',
                                   error='Недействительная ссылка для сброса пароля')

        reset_request = PasswordReset.query.filter_by(token=token, used=False).first()

        if not reset_request:
            session.pop('password_reset_token', None)
            return render_template('forgot_password.html',
                                   error='Недействительная ссылка для сброса пароля')

        exp = reset_request.expires_at if reset_request.expires_at.tzinfo else reset_request.expires_at.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            reset_request.used = True
            db.session.commit()
            session.pop('password_reset_token', None)
            return render_template('forgot_password.html',
                                   error='Срок действия ссылки истек')

        if request.method == 'POST':
            new_password     = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')

            if not new_password or not confirm_password:
                return render_template('reset_password.html', error='Заполните все поля')

            if new_password != confirm_password:
                return render_template('reset_password.html', error='Пароли не совпадают')

            if len(new_password) < 8:
                return render_template('reset_password.html',
                                       error='Пароль должен быть минимум 8 символов')

            user = User.query.get(reset_request.user_id)
            if not user:
                session.pop('password_reset_token', None)
                return render_template('forgot_password.html', error='Пользователь не найден')

            user.password      = PasswordSecurity.hash_password(new_password)
            reset_request.used = True
            db.session.commit()
            session.pop('password_reset_token', None)

            try:
                from security import SecurityAudit
                SecurityAudit.log_password_reset(user.id, user.username)
            except Exception:
                pass

            terminate_user_sessions(user.id, db)

            return render_template('login.html',
                                   success='Пароль успешно изменен. Теперь вы можете войти.')

        return render_template('reset_password.html')

    # ============================================================
    # МЕДИАГАЛЕРЕЯ
    # ============================================================

    @app.route('/get_media/<int:context_id>')
    @login_required
    def get_media(context_id):
        is_group   = request.args.get('is_group', 'false').lower() == 'true'
        media_type = request.args.get('type', 'images')
        if is_group:
            membership = GroupMember.query.filter_by(group_id=context_id, user_id=current_user.id).first()
            if not membership:
                return jsonify({'error': 'Нет доступа'}), 403
            base_query = Message.query.filter(Message.group_id == context_id, Message.is_deleted == False)
        else:
            chat_obj = Chat.query.get_or_404(context_id)
            if chat_obj.user1_id != current_user.id and chat_obj.user2_id != current_user.id:
                return jsonify({'error': 'Нет доступа'}), 403
            base_query = Message.query.filter(Message.chat_id == context_id, Message.is_deleted == False)

        result = []
        if media_type == 'images':
            msgs = base_query.filter(Message.image_path != None).options(joinedload(Message.sender)).order_by(Message.timestamp.desc()).limit(100).all()
            for msg in msgs:
                sender = msg.sender
                result.append({
                    'id': msg.id,
                    'url': url_for('download_file', filepath=msg.image_path),
                    'sender_name': user_display_name(sender),
                    'timestamp': msg.timestamp.isoformat() + 'Z'
                })
        elif media_type == 'files':
            msgs = base_query.filter(Message.file_path != None).options(joinedload(Message.sender)).order_by(Message.timestamp.desc()).limit(100).all()
            for msg in msgs:
                sender = msg.sender
                result.append({
                    'id': msg.id,
                    'url': url_for('download_file', filepath=msg.file_path),
                    'file_name': msg.file_name,
                    'file_size': format_file_size(msg.file_size) if msg.file_size else None,
                    'file_icon': get_file_icon(msg.file_name) if msg.file_name else '[file]',
                    'sender_name': user_display_name(sender),
                    'timestamp': msg.timestamp.isoformat() + 'Z'
                })
        elif media_type == 'links':
            msgs = base_query.filter(Message.link_url != None).options(joinedload(Message.sender)).order_by(Message.timestamp.desc()).limit(100).all()
            for msg in msgs:
                sender = msg.sender
                result.append({
                    'id': msg.id,
                    'url': msg.link_url,
                    'title': msg.link_title,
                    'description': msg.link_description,
                    'image': msg.link_image,
                    'sender_name': user_display_name(sender),
                    'timestamp': msg.timestamp.isoformat() + 'Z'
                })
        return jsonify(result)

    from extra_routes import register_extra_routes
    register_extra_routes(app, db)

    return app
