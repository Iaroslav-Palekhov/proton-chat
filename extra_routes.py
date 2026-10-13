"""Дополнительные маршруты: админ-создание пользователей, список прочитавших."""
import json
import re

from flask import request, jsonify, render_template, url_for
from flask_login import login_required, current_user

from config import Config
from encryption import hmac_hash
from models import db, User, Message, GroupMember
from security import PasswordSecurity, rate_limit, InputSanitizer
from read_receipts import get_message_readers
from upload_security import user_can_access_message
from utils import (
    generate_avatar_file, ensure_proton_chat_for_user, ensure_admin_privacy,
    is_platform_admin, get_proton_bot_user,
)


def register_extra_routes(app, db):
    def _admin_only():
        if not is_platform_admin(current_user):
            return jsonify({'error': 'Нет доступа'}), 403
        return None

    @app.route('/dashboard/create_user', methods=['POST'])
    @login_required
    @rate_limit('register')
    def dashboard_create_user():
        err = _admin_only()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        email = (data.get('email') or '').strip()[:254]
        username = (data.get('username') or '').strip()[:32]
        password = data.get('password') or ''
        if not email or not username or not password:
            return jsonify({'error': 'Заполните все поля'}), 400
        if not re.match(r'^[a-zA-Z0-9_]{5,32}$', username):
            return jsonify({'error': 'Username: 5–32 символа (буквы, цифры, _)'}), 400
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            return jsonify({'error': 'Email в формате user@example.com'}), 400
        if len(password) < 8:
            return jsonify({'error': 'Пароль минимум 8 символов'}), 400
        if User.query.filter_by(email_hash=hmac_hash(email)).first():
            return jsonify({'error': 'Email уже занят'}), 400
        if User.query.filter_by(username=username).first():
            return jsonify({'error': 'Username уже занят'}), 400
        if username.lower() == Config.PLATFORM_BOT_USERNAME.lower():
            return jsonify({'error': 'Имя зарезервировано'}), 400

        user = User(
            email=email,
            email_hash=hmac_hash(email),
            username=username,
            password=PasswordSecurity.hash_password(password),
        )
        db.session.add(user)
        db.session.commit()
        user.avatar = generate_avatar_file(username, 'avatar', app)
        db.session.commit()
        if is_platform_admin(user):
            ensure_admin_privacy(user, db)
        ensure_proton_chat_for_user(user.id, db, app)
        db.session.commit()
        return jsonify({'success': True, 'user_id': user.id, 'username': user.username})

    @app.route('/message/<int:message_id>/readers')
    @login_required
    def message_readers(message_id):
        msg = Message.query.get_or_404(message_id)
        if not msg.group_id:
            return jsonify({'error': 'Только для групповых чатов'}), 400
        if not user_can_access_message(current_user.id, msg):
            return jsonify({'error': 'Нет доступа'}), 403
        if msg.sender_id != current_user.id and not is_platform_admin(current_user):
            membership = GroupMember.query.filter_by(
                group_id=msg.group_id, user_id=current_user.id
            ).first()
            if not membership or membership.role not in ('owner', 'admin'):
                return jsonify({'error': 'Нет доступа'}), 403
        readers = get_message_readers(message_id)
        for r in readers:
            if r.get('avatar'):
                r['avatar'] = url_for('serve_media', filepath=r['avatar'])
        return jsonify({'readers': readers, 'count': len(readers)})
