"""Отметки о прочтении сообщений в группах."""
from models import db, User, MessageRead, Message


def get_message_readers(message_id, limit=50):
    rows = (
        MessageRead.query
        .filter_by(message_id=message_id)
        .order_by(MessageRead.read_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for r in rows:
        u = r.user
        if not u or u.is_deleted:
            continue
        result.append({
            'user_id': u.id,
            'username': u.username,
            'avatar': u.avatar or '',
            'read_at': r.read_at.isoformat() + 'Z' if r.read_at else None,
        })
    return result


def mark_group_messages_read(group_id, user_id, readers_count):
    """Отмечает прочитанными сообщения группы для user_id. Возвращает обновлённые msg info."""
    from datetime import datetime, timezone
    all_msgs = Message.query.filter(
        Message.group_id == group_id,
        Message.sender_id != user_id,
        Message.is_deleted == False,
    ).all()
    if not all_msgs:
        return []
    msg_ids = [m.id for m in all_msgs]
    already = {
        r.message_id
        for r in MessageRead.query.filter(
            MessageRead.user_id == user_id,
            MessageRead.message_id.in_(msg_ids),
        ).all()
    }
    updated = []
    now = datetime.now(timezone.utc)
    for m in all_msgs:
        if m.id in already:
            continue
        db.session.add(MessageRead(message_id=m.id, user_id=user_id, read_at=now))
        db.session.flush()
        cnt = MessageRead.query.filter_by(message_id=m.id).count()
        m.read_count = cnt
        if cnt >= readers_count:
            m.is_read = True
        updated.append({
            'id': m.id,
            'read_count': cnt,
            'members_count': readers_count,
        })
    return updated
