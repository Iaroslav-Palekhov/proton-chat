from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timezone

from encryption import EncryptedType

db = SQLAlchemy()

DELETED_ACCOUNT_NAME = 'Удалённый аккаунт'


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # ── Зашифрованные поля ──────────────────────────────────────
    # email хранится зашифрованным.
    # email_hash — детерминированный HMAC-SHA256 для точного поиска (login, forgot_password).
    email      = db.Column(EncryptedType(254), unique=False, nullable=False)
    email_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)

    password   = db.Column(db.String(200), nullable=False)
    username   = db.Column(db.String(50), unique=True, nullable=False)
    avatar     = db.Column(db.String(200), nullable=True)
    bio        = db.Column(EncryptedType(200), default='')
    status     = db.Column(db.String(50), default='online')
    last_seen  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, index=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    messages_sent     = db.relationship('Message', foreign_keys='Message.sender_id', backref='sender', lazy=True)
    messages_received = db.relationship('Message', foreign_keys='Message.receiver_id', backref='receiver', lazy=True)
    owned_groups      = db.relationship('Group', backref='owner', lazy=True)
    group_memberships = db.relationship('GroupMember', backref='user', lazy=True)
    sessions          = db.relationship('UserSession', backref='user', lazy=True, cascade='all, delete-orphan')

    @property
    def display_name(self):
        return DELETED_ACCOUNT_NAME if self.is_deleted else self.username

    @property
    def display_avatar(self):
        return self.avatar or ''


class Group(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(EncryptedType(100), nullable=False)
    description = db.Column(EncryptedType(500), default='')
    write_permission = db.Column(db.String(20), default='all', nullable=False)
    icon        = db.Column(db.String(200), nullable=True)
    owner_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_message_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    visibility     = db.Column(db.String(20), default='private', nullable=False)
    join_type      = db.Column(db.String(20), default='open', nullable=False)
    group_username = db.Column(db.String(50), unique=True, nullable=True, index=True)
    invite_token   = db.Column(db.String(64), unique=True, nullable=True, index=True)

    members       = db.relationship('GroupMember', backref='group', lazy=True, cascade='all, delete-orphan')
    messages      = db.relationship('Message', backref='group', lazy=True)
    join_requests = db.relationship('GroupJoinRequest', backref='group', lazy=True, cascade='all, delete-orphan')
    bans          = db.relationship('GroupBan', backref='group', lazy=True, cascade='all, delete-orphan')


class GroupBan(db.Model):
    """Бан пользователя в группе — не может вступить снова."""
    id           = db.Column(db.Integer, primary_key=True)
    group_id     = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False, index=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    banned_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    banned_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user      = db.relationship('User', foreign_keys=[user_id])
    banned_by = db.relationship('User', foreign_keys=[banned_by_id])

    __table_args__ = (db.UniqueConstraint('group_id', 'user_id', name='unique_group_ban'),)


class GroupMember(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    group_id  = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    user_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role      = db.Column(db.String(20), default='member')
    joined_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (db.UniqueConstraint('group_id', 'user_id', name='unique_group_member'),)


class GroupJoinRequest(db.Model):
    """Заявка на вступление в закрытую группу."""
    id       = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False, index=True)
    user_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    status   = db.Column(db.String(20), default='pending', nullable=False)
    created_at     = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at    = db.Column(db.DateTime, nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    user        = db.relationship('User', foreign_keys=[user_id], backref=db.backref('join_requests', lazy=True))
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id])

    __table_args__ = (db.UniqueConstraint('group_id', 'user_id', name='unique_join_request'),)


class Chat(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    user1_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    user2_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    is_saved  = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at      = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_message_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        db.Index('ix_chat_users', 'user1_id', 'user2_id'),
    )

    messages = db.relationship('Message', backref='chat', lazy=True, cascade='all, delete-orphan')
    user1    = db.relationship('User', foreign_keys=[user1_id])
    user2    = db.relationship('User', foreign_keys=[user2_id])


class Message(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    chat_id     = db.Column(db.Integer, db.ForeignKey('chat.id'), nullable=True, index=True)
    group_id    = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True, index=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)

    # ── Зашифрованный контент ───────────────────────────────────
    content   = db.Column(EncryptedType(), nullable=True)

    image_path    = db.Column(db.String(200), nullable=True)
    file_path     = db.Column(db.String(200), nullable=True)
    file_name     = db.Column(db.String(200), nullable=True)
    file_type     = db.Column(db.String(100), nullable=True)
    file_size     = db.Column(db.Integer, nullable=True)
    file_category = db.Column(db.String(50), nullable=True)
    timestamp     = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    is_read       = db.Column(db.Boolean, default=False, index=True)
    read_count    = db.Column(db.Integer, default=0)  # кол-во участников группы прочитавших сообщение
    is_edited     = db.Column(db.Boolean, default=False)
    is_deleted    = db.Column(db.Boolean, default=False, index=True)

    reply_to_id       = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=True)
    forwarded_from_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=True)
    is_forwarded      = db.Column(db.Boolean, default=False)
    show_forward_sender = db.Column(db.Boolean, default=True)

    reply_to       = db.relationship('Message', foreign_keys=[reply_to_id], remote_side=[id], backref='replies')
    forwarded_from = db.relationship('Message', foreign_keys=[forwarded_from_id], remote_side=[id], backref='forwarded_copies')

    is_pinned    = db.Column(db.Boolean, default=False)
    pinned_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    pinned_at    = db.Column(db.DateTime, nullable=True)

    # ── Зашифрованные превью ссылок ─────────────────────────────
    link_url         = db.Column(db.String(500), nullable=True)
    link_title       = db.Column(EncryptedType(200), nullable=True)
    link_description = db.Column(EncryptedType(), nullable=True)
    link_image       = db.Column(db.String(500), nullable=True)
    link_fetched_at  = db.Column(db.DateTime, nullable=True)

    message_type = db.Column(db.String(20), default='text', nullable=False)  # text, file

    __table_args__ = (
        # Композитные индексы для быстрой выборки сообщений
        db.Index('ix_message_chat_ts', 'chat_id', 'timestamp'),
        db.Index('ix_message_group_ts', 'group_id', 'timestamp'),
        db.Index('ix_message_chat_unread', 'chat_id', 'is_read', 'is_deleted'),
    )


class ForwardedMessage(db.Model):
    id                   = db.Column(db.Integer, primary_key=True)
    original_message_id  = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False)
    forwarded_message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False)
    forwarded_by_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    forwarded_to_chat_id  = db.Column(db.Integer, db.ForeignKey('chat.id'), nullable=True)
    forwarded_to_group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    show_sender  = db.Column(db.Boolean, default=True)
    forwarded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    original_message  = db.relationship('Message', foreign_keys=[original_message_id])
    forwarded_message = db.relationship('Message', foreign_keys=[forwarded_message_id])
    forwarded_by      = db.relationship('User', foreign_keys=[forwarded_by_id])


class PasswordReset(db.Model):
    """Модель для токенов сброса пароля"""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    token      = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)

    user = db.relationship('User', backref=db.backref('reset_tokens', lazy=True))


class UserSession(db.Model):
    """
    Хранит информацию об активных и завершённых сессиях пользователя.
    """
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    session_token = db.Column(db.String(64), unique=True, nullable=False, index=True)

    # ── Зашифрованные данные устройства / геолокации ───────────
    ip_address  = db.Column(EncryptedType(45), nullable=True)
    user_agent  = db.Column(EncryptedType(500), nullable=True)
    device_type = db.Column(db.String(20), nullable=True)   # mobile/tablet/desktop — не PII
    browser     = db.Column(EncryptedType(100), nullable=True)
    os          = db.Column(EncryptedType(100), nullable=True)
    country     = db.Column(EncryptedType(100), nullable=True)
    city        = db.Column(EncryptedType(100), nullable=True)

    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_active = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at    = db.Column(db.DateTime, nullable=True)
    is_active   = db.Column(db.Boolean, default=True)
    is_current  = db.Column(db.Boolean, default=False)

    def end_session(self):
        self.is_active = False
        self.ended_at  = datetime.now(timezone.utc)

    @staticmethod
    def _aware(dt):
        """Приводим naive datetime к UTC-aware (для совместимости с БД без tzinfo)."""
        if dt is not None and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def duration_str(self):
        end   = self._aware(self.ended_at) or datetime.now(timezone.utc)
        start = self._aware(self.created_at)
        delta = end - start
        days  = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if days > 0:
            return f"{days} д. {hours} ч."
        elif hours > 0:
            return f"{hours} ч. {minutes} мин."
        else:
            return f"{minutes} мин."

    def last_active_str(self):
        now   = datetime.now(timezone.utc)
        delta = now - self._aware(self.last_active)
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return "только что"
        elif total_seconds < 3600:
            return f"{total_seconds // 60} мин. назад"
        elif delta.days == 0:
            return f"{total_seconds // 3600} ч. назад"
        elif delta.days == 1:
            return "вчера"
        else:
            return self.last_active.strftime('%d.%m.%Y %H:%M')


class Contact(db.Model):
    """Контакты пользователя."""
    id         = db.Column(db.Integer, primary_key=True)
    owner_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    contact_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    owner        = db.relationship('User', foreign_keys=[owner_id], backref=db.backref('contacts', lazy=True))
    contact_user = db.relationship('User', foreign_keys=[contact_id])

    __table_args__ = (db.UniqueConstraint('owner_id', 'contact_id', name='unique_contact'),)


class UserPrivacy(db.Model):
    """Настройки приватности пользователя."""
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True, index=True)
    who_can_write = db.Column(db.String(20), default='all', nullable=False)
    last_seen     = db.Column(db.String(20), default='all', nullable=False)
    updated_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', backref=db.backref('privacy_settings', uselist=False, lazy=True))


class BlockedUser(db.Model):
    """Чёрный список — заблокированные пользователи."""
    id         = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    blocked_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    blocker      = db.relationship('User', foreign_keys=[blocker_id], backref=db.backref('blocked_users', lazy=True))
    blocked_user = db.relationship('User', foreign_keys=[blocked_id])

    __table_args__ = (db.UniqueConstraint('blocker_id', 'blocked_id', name='unique_block'),)


class UserBan(db.Model):
    """Глобальный бан пользователя на платформе."""
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True, index=True)
    banned_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    banned_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at   = db.Column(db.DateTime, nullable=True)  # None = навсегда

    user      = db.relationship('User', foreign_keys=[user_id], backref=db.backref('platform_ban', uselist=False))
    banned_by = db.relationship('User', foreign_keys=[banned_by_id])

    def is_active(self):
        if self.expires_at is None:
            return True
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < exp


class MessageReport(db.Model):
    """Жалоба на сообщение. Одна активная жалоба на сообщение."""
    id           = db.Column(db.Integer, primary_key=True)
    message_id   = db.Column(db.Integer, db.ForeignKey('message.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    reporter_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    status       = db.Column(db.String(20), default='pending', nullable=False, index=True)
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    closed_at    = db.Column(db.DateTime, nullable=True)
    closed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    resolution   = db.Column(db.String(20), nullable=True)  # dismissed | banned

    message  = db.relationship('Message', foreign_keys=[message_id], backref=db.backref('report', uselist=False))
    reporter = db.relationship('User', foreign_keys=[reporter_id])
    closed_by = db.relationship('User', foreign_keys=[closed_by_id])


class MessageReaction(db.Model):
    """Реакция пользователя на сообщение. Один пользователь — одна реакция на сообщение."""
    id         = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id',    ondelete='CASCADE'), nullable=False, index=True)
    emoji      = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user    = db.relationship('User',    foreign_keys=[user_id])
    message = db.relationship('Message', foreign_keys=[message_id],
                               backref=db.backref('reactions', lazy=True, cascade='all, delete-orphan'))

    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', name='unique_msg_reaction'),)


class MessageRead(db.Model):
    """Кто прочитал сообщение в группе (точный учёт)."""
    id         = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    read_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user    = db.relationship('User', foreign_keys=[user_id])
    message = db.relationship('Message', foreign_keys=[message_id],
                               backref=db.backref('read_by', lazy=True, cascade='all, delete-orphan'))

    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', name='unique_message_read'),)
