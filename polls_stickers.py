"""Опросы и стикеры — создание, сериализация, стандартный пак."""
import os
import uuid
import secrets

import requests
from flask import url_for
from werkzeug.utils import secure_filename

from models import (
    db, Poll, PollOption, PollVote, StickerPack, Sticker, UserStickerPack, User, MessageRead, Message,
)

DEFAULT_TWEMOJI = [
    ('😀', '1f600'), ('😂', '1f602'), ('😍', '1f60d'), ('😎', '1f60e'),
    ('😢', '1f622'), ('😡', '1f621'), ('👍', '1f44d'), ('👎', '1f44e'),
    ('❤️', '2764'), ('🔥', '1f525'), ('🎉', '1f389'), ('💯', '1f4af'),
    ('🤔', '1f914'), ('👀', '1f440'), ('🙏', '1f64f'), ('💪', '1f4aa'),
    ('🐱', '1f431'), ('🐶', '1f436'), ('🍕', '1f355'), ('☕', '2615'),
    ('✅', '2705'), ('❌', '274c'), ('⭐', '2b50'), ('💤', '1f4a4'),
]

TWEMOJI_CDN = 'https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{code}.png'


def _download_twemoji_png(code):
    """Скачивает PNG Twemoji, пробует варианты кода (с/без fe0f)."""
    variants = [code]
    if '-fe0f' in code:
        variants.append(code.replace('-fe0f', ''))
    for variant in variants:
        url = TWEMOJI_CDN.format(code=variant)
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.content
        except Exception:
            continue
    return None


def create_poll(question, options, allows_multiple=False, is_anonymous=True):
    poll = Poll(
        question=question.strip(),
        allows_multiple=bool(allows_multiple),
        is_anonymous=bool(is_anonymous),
    )
    db.session.add(poll)
    db.session.flush()
    for i, opt in enumerate(options):
        text = (opt or '').strip()
        if not text:
            continue
        db.session.add(PollOption(poll_id=poll.id, text=text, position=i))
    db.session.flush()
    return poll


def copy_poll(poll_id):
    """Копия опроса для пересылки (голоса сбрасываются)."""
    src = Poll.query.get(poll_id)
    if not src:
        return None
    new_poll = Poll(
        question=src.question,
        allows_multiple=src.allows_multiple,
        is_anonymous=src.is_anonymous,
    )
    db.session.add(new_poll)
    db.session.flush()
    for opt in sorted(src.options, key=lambda o: o.position):
        db.session.add(PollOption(
            poll_id=new_poll.id,
            text=opt.text,
            position=opt.position,
        ))
    db.session.flush()
    return new_poll


def get_poll_vote_map(poll_id, user_id):
    votes = PollVote.query.filter_by(poll_id=poll_id, user_id=user_id).all()
    return {v.option_id for v in votes}


def vote_poll(poll_id, option_ids, user_id):
    poll = Poll.query.get(poll_id)
    if not poll or poll.is_closed:
        return False, 'Опрос закрыт или не найден'
    if not option_ids:
        return False, 'Выберите вариант'

    option_ids = [int(x) for x in option_ids]
    valid_ids = {o.id for o in poll.options}
    if not all(oid in valid_ids for oid in option_ids):
        return False, 'Некорректный вариант'
    if not poll.allows_multiple and len(option_ids) > 1:
        return False, 'Можно выбрать только один вариант'

    PollVote.query.filter_by(poll_id=poll_id, user_id=user_id).delete()
    for oid in option_ids:
        db.session.add(PollVote(poll_id=poll_id, option_id=oid, user_id=user_id))
    db.session.flush()
    return True, None


def serialize_poll(poll, viewer_id=None):
    if not poll:
        return None
    total_voters = db.session.query(PollVote.user_id).filter_by(poll_id=poll.id).distinct().count()
    my_votes = get_poll_vote_map(poll.id, viewer_id) if viewer_id else set()
    options = []
    for opt in sorted(poll.options, key=lambda o: o.position):
        count = PollVote.query.filter_by(poll_id=poll.id, option_id=opt.id).count()
        pct = round(count * 100 / total_voters) if total_voters else 0
        options.append({
            'id': opt.id,
            'text': opt.text,
            'votes': count,
            'percent': pct,
            'voted': opt.id in my_votes,
        })
    return {
        'id': poll.id,
        'question': poll.question,
        'allows_multiple': poll.allows_multiple,
        'is_anonymous': poll.is_anonymous,
        'is_closed': poll.is_closed,
        'total_voters': total_voters,
        'options': options,
        'my_votes': list(my_votes),
    }


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


def ensure_user_has_default_pack(user_id):
    official = StickerPack.query.filter_by(is_official=True).first()
    if not official:
        return False
    exists = UserStickerPack.query.filter_by(user_id=user_id, pack_id=official.id).first()
    if not exists:
        db.session.add(UserStickerPack(user_id=user_id, pack_id=official.id))
        return True
    return False


def ensure_default_sticker_pack(app):
    """Скачивает стандартные Twemoji-стикеры при первом запуске."""
    try:
        with app.app_context():
            if StickerPack.query.filter_by(is_official=True).first():
                return
            pack = StickerPack(title='Стандартные', owner_id=None, is_official=True)
            db.session.add(pack)
            db.session.flush()

            base = os.path.join(app.config.get('UPLOAD_FOLDER', 'private_uploads'), 'stickers', str(pack.id))
            os.makedirs(base, exist_ok=True)

            pos = 0
            for emoji, code in DEFAULT_TWEMOJI:
                content = _download_twemoji_png(code)
                if not content:
                    print(f'[stickers] skip code {code}')
                    continue
                fname = f'{code}_{secrets.token_hex(4)}.png'
                full = os.path.join(base, fname)
                with open(full, 'wb') as f:
                    f.write(content)
                rel = os.path.join('stickers', str(pack.id), fname).replace('\\', '/')
                db.session.add(Sticker(pack_id=pack.id, file_path=rel, emoji=emoji, position=pos))
                pos += 1

            if pos == 0:
                db.session.rollback()
                print('[stickers] standard pack skipped: no images downloaded')
                return

            db.session.commit()
            print(f'[stickers] standard pack created: {pos} stickers')

            users = User.query.filter_by(is_deleted=False).all()
            for u in users:
                ensure_user_has_default_pack(u.id)
            db.session.commit()
    except Exception as e:
        try:
            with app.app_context():
                db.session.rollback()
        except Exception:
            pass
        print(f'[stickers] standard pack error: {e}')


def emit_poll_updated(poll_id, viewer_id, socketio):
    """Обновляет опрос во всех чатах, где он был отправлен/переслан."""
    poll_messages = Message.query.filter_by(poll_id=poll_id, is_deleted=False).all()
    if not poll_messages:
        return
    poll_data = serialize_poll(poll_messages[0].poll, viewer_id)
    for msg in poll_messages:
        room = f'chat_{msg.chat_id}' if msg.chat_id else f'group_{msg.group_id}'
        socketio.emit('poll_updated', {'message_id': msg.id, 'poll': poll_data}, to=room)


def _remove_sticker_file_if_unused(sticker, app):
    """Удаляет файл стикера с диска, если он нигде не используется в сообщениях."""
    if Message.query.filter_by(sticker_id=sticker.id).count() > 0:
        return
    from upload_security import resolve_upload_file
    resolved = resolve_upload_file(app, sticker.file_path)
    if resolved:
        full = os.path.join(resolved[0], resolved[1])
        try:
            os.remove(full)
        except OSError:
            pass


def delete_sticker(sticker_id, user_id, app):
    sticker = Sticker.query.get(sticker_id)
    if not sticker:
        return False, 'Стикер не найден'
    pack = sticker.pack
    if not pack or pack.is_official:
        return False, 'Нельзя удалить'
    if pack.owner_id != user_id:
        from utils import is_platform_admin
        u = User.query.get(user_id)
        if not u or not is_platform_admin(u):
            return False, 'Нет доступа'
    Message.query.filter_by(sticker_id=sticker.id).update(
        {'sticker_id': None}, synchronize_session=False
    )
    db.session.flush()
    _remove_sticker_file_if_unused(sticker, app)
    db.session.delete(sticker)
    return True, None


def delete_sticker_pack(pack_id, user_id, app):
    pack = StickerPack.query.get(pack_id)
    if not pack:
        return False, 'Пак не найден'
    if pack.is_official:
        return False, 'Нельзя удалить стандартный пак'
    if pack.owner_id != user_id:
        from utils import is_platform_admin
        u = User.query.get(user_id)
        if not u or not is_platform_admin(u):
            return False, 'Нет доступа'
    for sticker in list(pack.stickers):
        Message.query.filter_by(sticker_id=sticker.id).update(
            {'sticker_id': None}, synchronize_session=False
        )
    db.session.flush()
    for sticker in list(pack.stickers):
        _remove_sticker_file_if_unused(sticker, app)
    UserStickerPack.query.filter_by(pack_id=pack_id).delete()
    db.session.delete(pack)
    return True, None


def remove_pack_from_library(pack_id, user_id):
    pack = StickerPack.query.get(pack_id)
    if not pack:
        return False, 'Пак не найден'
    if pack.is_official:
        return False, 'Стандартный пак нельзя убрать'
    if pack.owner_id == user_id:
        return False, 'Свой пак удаляется в разделе «Мои паки»'
    row = UserStickerPack.query.filter_by(user_id=user_id, pack_id=pack_id).first()
    if not row:
        return False, 'Пак не в библиотеке'
    db.session.delete(row)
    return True, None


def save_sticker_file(file, pack_id, app):
    from utils import allowed_file
    if not file or not file.filename:
        return None, 'Файл не выбран'
    if not allowed_file(file.filename):
        return None, 'Недопустимый тип файла'
    ext = secure_filename(file.filename).rsplit('.', 1)[-1].lower()
    if ext not in ('png', 'webp', 'gif', 'jpg', 'jpeg'):
        return None, 'Стикер: png, webp, gif или jpg'
    base = os.path.join(app.config.get('UPLOAD_FOLDER', 'private_uploads'), 'stickers', str(pack_id))
    os.makedirs(base, exist_ok=True)
    fname = f'{uuid.uuid4().hex}.{ext}'
    full = os.path.join(base, fname)
    file.save(full)
    rel = os.path.join('stickers', str(pack_id), fname).replace('\\', '/')
    return rel, None


def serialize_sticker(sticker, app=None):
    if not sticker:
        return None
    return {
        'id': sticker.id,
        'pack_id': sticker.pack_id,
        'emoji': sticker.emoji or '',
        'url': url_for('serve_media', filepath=sticker.file_path),
    }


def serialize_sticker_pack(pack, user_id=None, include_stickers=False):
    count = Sticker.query.filter_by(pack_id=pack.id).count()
    in_library = False
    if user_id:
        in_library = UserStickerPack.query.filter_by(user_id=user_id, pack_id=pack.id).first() is not None
    data = {
        'id': pack.id,
        'title': pack.title,
        'is_official': pack.is_official,
        'owner_id': pack.owner_id,
        'sticker_count': count,
        'in_library': in_library or pack.is_official,
    }
    if include_stickers:
        stickers = Sticker.query.filter_by(pack_id=pack.id).order_by(Sticker.position).all()
        data['stickers'] = [serialize_sticker(s) for s in stickers]
    return data


def search_sticker_packs(query, user_id, limit=20):
    q = (query or '').strip()
    base = StickerPack.query
    if q:
        base = base.filter(StickerPack.title.ilike(f'%{q}%'))
    packs = base.order_by(StickerPack.is_official.desc(), StickerPack.title).limit(limit).all()
    return [serialize_sticker_pack(p, user_id) for p in packs]


def build_message_extras_maps(messages, viewer_id):
    """Словари poll/sticker для серверного рендера чатов."""
    polls = {}
    stickers = {}
    for msg in messages:
        mt = getattr(msg, 'message_type', None) or 'text'
        if mt == 'poll' and msg.poll:
            polls[msg.id] = serialize_poll(msg.poll, viewer_id)
        elif mt == 'sticker' and msg.sticker:
            stickers[msg.id] = serialize_sticker(msg.sticker)
    return polls, stickers


def get_user_sticker_packs(user_id, include_stickers=True):
    if ensure_user_has_default_pack(user_id):
        db.session.commit()
    rows = (
        UserStickerPack.query
        .filter_by(user_id=user_id)
        .join(StickerPack)
        .order_by(StickerPack.is_official.desc(), UserStickerPack.added_at)
        .all()
    )
    return [serialize_sticker_pack(r.pack, user_id, include_stickers=include_stickers) for r in rows]
