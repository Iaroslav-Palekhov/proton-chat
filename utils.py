import os
import uuid
import io
import random
import mimetypes
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename


def get_client_ip(req=None):
    """IP клиента с учётом reverse proxy (Caddy, nginx)."""
    if req is None:
        from flask import request as flask_request
        req = flask_request

    for header in ('CF-Connecting-IP', 'X-Real-IP', 'X-Forwarded-For'):
        value = (req.headers.get(header) or '').strip()
        if not value:
            continue
        ip = value.split(',')[0].strip()
        if ip and ip.lower() != 'unknown':
            return ip

    return (req.remote_addr or '0.0.0.0').strip()


# ── Генерация identicon-аватарок (буква + градиент, в стиле Telegram) ──

# Путь к встроенному шрифту (кладём свой .ttf в проект, чтобы не зависеть
# от того, какие шрифты установлены на сервере в продакшене).
_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'fonts', 'DejaVuSans-Bold.ttf')
_FONT_CACHE = {}

# Палитра градиентов (верхний цвет -> нижний цвет), подобрана в стиле Telegram.
AVATAR_GRADIENTS = [
    ('#FF885E', '#FF516A'),  # красно-оранжевый
    ('#FFCD6A', '#FFA85C'),  # оранжево-жёлтый
    ('#82B1FF', '#665FFF'),  # сине-фиолетовый
    ('#A0DE7E', '#54CB68'),  # зелёный
    ('#53EDD6', '#28C9B7'),  # бирюзовый
    ('#72D5FD', '#2A9EF1'),  # голубой
    ('#E0A2F3', '#D669ED'),  # розово-фиолетовый
    ('#FF7AA2', '#E55C8A'),  # розовый
]


def _get_font(size):
    """Кэшируем загруженный шрифт по размеру, чтобы не читать файл каждый раз."""
    font = _FONT_CACHE.get(size)
    if font is None:
        try:
            font = ImageFont.truetype(_FONT_PATH, size)
        except Exception:
            font = ImageFont.load_default()
        _FONT_CACHE[size] = font
    return font


def _hex_to_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _first_letter(name):
    """Берёт первый отображаемый символ строки и переводит в верхний регистр.
    Падает обратно на '?' если строка пустая/состоит из пробелов."""
    if not name:
        return '?'
    name = name.strip()
    if not name:
        return '?'
    return name[0].upper()


def _make_diagonal_gradient(size, top_hex, bottom_hex):
    """
    Строит диагональный градиент (левый верхний угол -> правый нижний)
    без numpy: считаем 2*size-1 промежуточных цветов (по диагоналям
    постоянного x+y) и быстро собираем итоговое изображение через putdata.
    """
    top = _hex_to_rgb(top_hex)
    bottom = _hex_to_rgb(bottom_hex)
    max_d = 2 * (size - 1) or 1

    # Цвет для каждого значения diag = x + y (0..2*(size-1))
    diag_colors = []
    for diag in range(max_d + 1):
        t = diag / max_d
        r = round(top[0] + (bottom[0] - top[0]) * t)
        g = round(top[1] + (bottom[1] - top[1]) * t)
        b = round(top[2] + (bottom[2] - top[2]) * t)
        diag_colors.append((r, g, b))

    pixels = [diag_colors[x + y] for y in range(size) for x in range(size)]

    img = Image.new('RGB', (size, size))
    img.putdata(pixels)
    return img


def generate_identicon(letter, size=200):
    """
    Рисует PNG-аватарку: диагональный градиент случайного цвета из палитры
    AVATAR_GRADIENTS + первая буква по центру белым цветом.
    Возвращает PIL.Image в режиме RGB.
    """
    top_hex, bottom_hex = random.choice(AVATAR_GRADIENTS)
    img = _make_diagonal_gradient(size, top_hex, bottom_hex)

    draw = ImageDraw.Draw(img)
    font = _get_font(int(size * 0.5))
    bbox = draw.textbbox((0, 0), letter, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pos = ((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1])
    draw.text(pos, letter, font=font, fill='#FFFFFF')

    return img


LEGACY_DEFAULT_PATHS = frozenset({'avatars/default.png', 'group_icons/default.png'})


def is_generated_avatar(relative_path):
    """True, если аватарка сгенерирована сервером (буква + градиент)."""
    return bool(relative_path and relative_path.endswith('_generated.png'))


def is_legacy_default(relative_path):
    """True, если путь указывает на старую дефолтную заглушку."""
    return relative_path in LEGACY_DEFAULT_PATHS


def is_custom_upload(relative_path):
    """True, если пользователь загрузил своё изображение."""
    if not relative_path:
        return False
    return not is_generated_avatar(relative_path) and not is_legacy_default(relative_path)


def should_regenerate_avatar(relative_path):
    """Нужно ли пересоздать аватарку при смене имени (сгенерированная или пустая)."""
    if not relative_path:
        return True
    return is_generated_avatar(relative_path) or is_legacy_default(relative_path)


def generate_avatar_file(name, category, app):
    """
    Генерирует identicon-аватарку для пользователя или группы и сохраняет
    её на диск так же, как save_file() сохраняет загруженные файлы
    (уникальное имя, нужная подпапка). Возвращает относительный путь
    вида 'avatars/<uuid>_generated.png' или 'group_icons/<uuid>_generated.png'.

    category: 'avatar' (пользователь) или 'group_icon' (группа)
    """
    category_folders = {
        'avatar': 'avatars',
        'group_icon': 'group_icons',
    }
    folder = category_folders.get(category, 'avatars')
    folder_path = os.path.join(app.root_path, app.config['UPLOAD_FOLDER'], folder)
    os.makedirs(folder_path, exist_ok=True)

    letter = _first_letter(name)
    img = generate_identicon(letter)

    unique_filename = f"{uuid.uuid4().hex}_generated.png"
    file_path = os.path.join(folder_path, unique_filename)
    img.save(file_path, format='PNG')

    return f'{folder}/{unique_filename}'


def delete_uploaded_file(relative_path, app, default_paths=None):
    """
    Удаляет файл из static/uploads по относительному пути.
    Молча игнорирует ошибки — удаление аватарки не должно ронять основную операцию.
  """
    if not relative_path:
        return
    if default_paths and relative_path in default_paths:
        return
    full_path = os.path.join(app.root_path, app.config['UPLOAD_FOLDER'], relative_path)
    if os.path.exists(full_path):
        try:
            os.remove(full_path)
        except Exception:
            pass


def create_upload_folders(app):
    """Создает все необходимые папки для загрузок"""
    folders = [
        'avatars',
        'group_icons',
        'images',
        'videos',
        'audio',
        'documents',
        'archives',
        'executables',
        'other'
    ]
    
    for folder in folders:
        os.makedirs(os.path.join(app.root_path, app.config['UPLOAD_FOLDER'], folder), exist_ok=True)

def migrate_legacy_avatars(app, db):
    """Заменяет старые default.png и пустые аватарки на сгенерированные с буквой."""
    from models import User, Group, DELETED_ACCOUNT_NAME

    changed = False
    for user in User.query.filter(
        (User.avatar.is_(None)) | (User.avatar == '') | User.avatar.in_(LEGACY_DEFAULT_PATHS)
    ).all():
        name = DELETED_ACCOUNT_NAME if user.is_deleted else user.username
        try:
            old = user.avatar
            user.avatar = generate_avatar_file(name, 'avatar', app)
            delete_uploaded_file(old, app)
            changed = True
        except Exception as e:
            print(f'[avatars] migrate user {user.id}: {e}')

    for group in Group.query.filter(
        (Group.icon.is_(None)) | (Group.icon == '') | Group.icon.in_(LEGACY_DEFAULT_PATHS)
    ).all():
        try:
            old = group.icon
            group.icon = generate_avatar_file(group.name, 'group_icon', app)
            delete_uploaded_file(old, app)
            changed = True
        except Exception as e:
            print(f'[avatars] migrate group {group.id}: {e}')

    if changed:
        db.session.commit()

def compress_image(image_data, max_size=(800, 800), quality=85):
    try:
        img = Image.open(io.BytesIO(image_data))
        original_format = img.format

        if original_format == 'GIF':
            return image_data

        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')

        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        if original_format == 'WEBP':
            img.save(buffer, format='WEBP', quality=quality)
        else:
            img.save(buffer, format='JPEG', quality=quality, optimize=True)

        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        print(f"Ошибка при сжатии изображения: {e}")
        return image_data

def allowed_file(filename):
    if not filename or '.' not in filename:
        return True
    ext = '.' + filename.rsplit('.', 1)[1].lower()
    from upload_security import BLOCKED_UPLOAD_EXTENSIONS
    return ext not in BLOCKED_UPLOAD_EXTENSIONS

def get_file_category(filename):
    """Определяет категорию файла по расширению"""

    # 🔴 ФИКС: защита от None
    if not filename:
        return 'other'

    # Получаем расширение файла
    if '.' in filename:
        ext = filename.rsplit('.', 1)[1].lower()
    else:
        ext = ''

    # Офисные форматы
    if ext in ['docx', 'doc', 'xlsx', 'xls', 'pptx', 'ppt', 'odt', 'ods', 'odp', 'rtf', 'pdf']:
        return 'document'
    
    # Категории
    image_ext = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico', 'tiff', 'heic', 'raw'}
    video_ext = {'mp4', 'avi', 'mov', 'mkv', 'wmv', 'flv', 'webm', 'm4v', '3gp', 'mpg', 'mpeg'}
    audio_ext = {'mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac', 'wma', 'opus'}
    document_ext = {'pdf', 'doc', 'docx', 'odt', 'rtf', 'txt', 'xls', 'xlsx', 'ods', 'csv', 'ppt', 'pptx', 'odp', 'md'}
    archive_ext = {'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'iso', 'dmg'}
    executable_ext = {'exe', 'msi', 'sh', 'bat', 'cmd', 'appimage', 'deb', 'rpm', 'apk', 'ipa'}

    if ext in image_ext:
        return 'image'
    elif ext in video_ext:
        return 'video'
    elif ext in audio_ext:
        return 'audio'
    elif ext in document_ext:
        return 'document'
    elif ext in archive_ext:
        return 'archive'
    elif ext in executable_ext:
        return 'executable'
    else:
        return 'other'

def get_file_icon(filename):
    """Возвращает иконку для файла по расширению"""
    if '.' in filename:
        ext = filename.rsplit('.', 1)[1].lower()
    else:
        ext = ''

    icons = {
        # Изображения
        'jpg': '[img]', 'jpeg': '[img]', 'png': '[img]', 'gif': '[img]', 'webp': '[img]', 'svg': '[img]', 'bmp': '[img]',
        # Видео
        'mp4': '[video]', 'avi': '[video]', 'mov': '[video]', 'mkv': '[video]', 'webm': '[video]',
        # Аудио
        'mp3': '[audio]', 'wav': '[audio]', 'ogg': '[audio]', 'flac': '[audio]', 'm4a': '[audio]',
        # Документы
        'pdf': '[pdf]', 'doc': '[doc]', 'docx': '[doc]', 'txt': '[txt]', 'rtf': '[txt]',
        'xls': '[xls]', 'xlsx': '[xls]', 'csv': '[csv]', 'ppt': '[ppt]', 'pptx': '[ppt]',
        # Архивы
        'zip': '[zip]', 'rar': '[zip]', '7z': '[zip]', 'tar': '[zip]', 'gz': '[zip]',
        # Исполняемые
        'exe': '[exe]', 'msi': '[exe]', 'sh': '[sh]', 'bat': '[bat]', 'apk': '[apk]',
    }
    return icons.get(ext, '[file]')

def format_file_size(size_bytes):
    if size_bytes is None:
        return '0 B'
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

def is_file_too_large(file_size, app):
    max_size = app.config.get('MAX_CONTENT_LENGTH', 500 * 1024 * 1024)
    return file_size > max_size

def _find_duplicate(folder_path, original_filename, data_hash):
    """
    Ищет дубликат файла в папке.
    Файлы хранятся как '{uuid32}_{original_filename}',
    проверяем совпадение оригинального имени и SHA256 содержимого.
    """
    import hashlib
    if not os.path.isdir(folder_path):
        return None
    for entry in os.scandir(folder_path):
        if not entry.is_file():
            continue
        name = entry.name
        # uuid4().hex = 32 символа, потом '_'
        if len(name) > 33 and name[32] == '_':
            existing_original = name[33:]
        else:
            continue
        if existing_original != original_filename:
            continue
        try:
            with open(entry.path, 'rb') as f:
                existing_hash = hashlib.sha256(f.read()).hexdigest()
            if existing_hash == data_hash:
                return entry.path
        except Exception:
            continue
    return None


def safe_filename(filename):
    """
    Безопасное имя файла с поддержкой кириллицы и unicode.
    secure_filename() убирает все не-ASCII символы, из-за чего
    'файл.zip' превращается в 'zip' (без точки и расширение без имени).
    Здесь мы сохраняем оригинальное имя, убирая только по-настоящему
    опасные символы (слэши, нулевой байт и т.п.).
    """
    import re
    # Убираем path traversal и управляющие символы
    name = os.path.basename(filename)
    name = name.replace('\x00', '')
    # Убираем символы, опасные для файловой системы
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    if not name:
        name = 'file'
    return name


def save_file(file, category, app):
    """Универсальная функция сохранения файлов с дедупликацией."""
    import hashlib

    filename = safe_filename(file.filename)
    unique_filename = f"{uuid.uuid4().hex}_{filename}"

    category_folders = {
        'image': 'images',
        'video': 'videos',
        'audio': 'audio',
        'document': 'documents',
        'archive': 'archives',
        'executable': 'executables',
        'avatar': 'avatars',
        'group_icon': 'group_icons',
        'other': 'other'
    }

    folder = category_folders.get(category, 'other')
    folder_path = os.path.join(app.root_path, app.config['UPLOAD_FOLDER'], folder)
    os.makedirs(folder_path, exist_ok=True)

    if category in ('image', 'avatar', 'group_icon'):
        image_data = file.read()
        compressed_data = compress_image(image_data)
        data_hash = hashlib.sha256(compressed_data).hexdigest()

        # Аватарки и иконки групп не дедуплицируем — у каждого своя
        if category == 'image':
            duplicate = _find_duplicate(folder_path, filename, data_hash)
            if duplicate:
                rel = os.path.relpath(duplicate, os.path.join(app.root_path, app.config['UPLOAD_FOLDER']))
                return rel.replace(os.sep, '/'), len(compressed_data)

        file_path = os.path.join(folder_path, unique_filename)
        with open(file_path, 'wb') as f:
            f.write(compressed_data)
        return f'{folder}/{unique_filename}', len(compressed_data)
    else:
        # Потоковая запись чанками — не грузим весь файл в RAM.
        # Сначала пишем во временный файл, считаем SHA256 на лету,
        # затем ищем дубликат и либо удаляем tmp, либо переименовываем.
        import tempfile, shutil
        CHUNK = 4 * 1024 * 1024  # 4 МБ
        h = hashlib.sha256()
        tmp_fd, tmp_path = tempfile.mkstemp(dir=folder_path)
        try:
            with os.fdopen(tmp_fd, 'wb') as tmp_f:
                while True:
                    chunk = file.read(CHUNK)
                    if not chunk:
                        break
                    tmp_f.write(chunk)
                    h.update(chunk)
            data_hash = h.hexdigest()
            file_size = os.path.getsize(tmp_path)

            duplicate = _find_duplicate(folder_path, filename, data_hash)
            if duplicate:
                os.remove(tmp_path)
                rel = os.path.relpath(duplicate, os.path.join(app.root_path, app.config['UPLOAD_FOLDER']))
                return rel.replace(os.sep, '/'), os.path.getsize(duplicate)

            file_path = os.path.join(folder_path, unique_filename)
            shutil.move(tmp_path, file_path)
            return f'{folder}/{unique_filename}', file_size
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise


import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re

def extract_link_preview(url):
    """Извлекает метаданные со страницы (title, description, image)"""
    try:
        from upload_security import is_safe_url_for_fetch
        # Проверяем, что URL валидный
        parsed = urlparse(url)
        if not parsed.scheme:
            url = 'https://' + url
            parsed = urlparse(url)

        if not is_safe_url_for_fetch(url):
            print(f"Заблокирован небезопасный URL превью: {url}")
            return None
        
        # Заголовки для имитации браузера
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Делаем запрос с таймаутом
        response = requests.get(url, headers=headers, timeout=5, allow_redirects=True)
        response.raise_for_status()

        final_url = response.url
        if not is_safe_url_for_fetch(final_url):
            return None
        
        # Парсим HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Извлекаем title
        title = None
        if soup.find('meta', property='og:title'):
            title = soup.find('meta', property='og:title')['content']
        elif soup.find('meta', attrs={'name': 'title'}):
            title = soup.find('meta', attrs={'name': 'title'})['content']
        elif soup.title:
            title = soup.title.string
        
        # Извлекаем description
        description = None
        if soup.find('meta', property='og:description'):
            description = soup.find('meta', property='og:description')['content']
        elif soup.find('meta', attrs={'name': 'description'}):
            description = soup.find('meta', attrs={'name': 'description'})['content']
        
        # Извлекаем image
        image = None
        if soup.find('meta', property='og:image'):
            image = soup.find('meta', property='og:image')['content']
            image = urljoin(url, image)
        elif soup.find('meta', attrs={'name': 'twitter:image'}):
            image = soup.find('meta', attrs={'name': 'twitter:image'})['content']
            image = urljoin(url, image)
        else:
            # Пробуем найти первую картинку на странице
            img_tag = soup.find('img', src=True)
            if img_tag:
                image = urljoin(url, img_tag['src'])
        
        if image and not is_safe_url_for_fetch(image):
            image = None
        
        # Очищаем и ограничиваем данные
        if title:
            title = title.strip()[:200]
        if description:
            description = description.strip()[:500]
        
        return {
            'url': url,
            'title': title,
            'description': description,
            'image': image
        }
        
    except Exception as e:
        print(f"Ошибка при извлечении превью ссылки: {e}")
        return None

def contains_url(text):
    """Проверяет, содержит ли текст URL"""
    if not text:
        return False
    
    url_pattern = re.compile(
        r'(https?://)?'
        r'(www\.)?'
        r'[-a-zA-Z0-9@:%._\+~#=]{1,256}'
        r'\.[a-zA-Z0-9()]{1,6}'
        r'\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*)',
        re.IGNORECASE
    )
    
    return bool(url_pattern.search(text))

def extract_urls_from_text(text):
    """Извлекает все URL из текста"""
    if not text:
        return []
    
    url_pattern = re.compile(
        r'(https?://[^\s<>"{}|\\^`\[\]]+|'
        r'www\.[^\s<>"{}|\\^`\[\]]+)',
        re.IGNORECASE
    )
    
    return url_pattern.findall(text)


def get_member_role(group_id, user_id, GroupMember):
    m = GroupMember.query.filter_by(group_id=group_id, user_id=user_id).first()
    return m.role if m else None


def can_delete_group_message(message, actor_user_id, GroupMember):
    """Владелец удаляет любые сообщения; админ — только от обычных участников."""
    if not message.group_id:
        return message.sender_id == actor_user_id
    if message.sender_id == actor_user_id:
        return True
    actor = GroupMember.query.filter_by(group_id=message.group_id, user_id=actor_user_id).first()
    if not actor:
        return False
    if actor.role == 'owner':
        return True
    if actor.role == 'admin':
        sender = GroupMember.query.filter_by(group_id=message.group_id, user_id=message.sender_id).first()
        return bool(sender and sender.role == 'member')
    return False


def user_display_name(user):
    if not user:
        from models import DELETED_ACCOUNT_NAME
        return DELETED_ACCOUNT_NAME
    return user.display_name


def delete_user_account(user, db, app):
    """Мягкое удаление аккаунта: сообщения остаются, имя — «Удалённый аккаунт»."""
    import secrets
    from datetime import datetime, timezone
    from models import (
        DELETED_ACCOUNT_NAME, Group, GroupMember, GroupJoinRequest, GroupBan,
        Contact, BlockedUser, UserSession, Message, ForwardedMessage,
    )

    now = datetime.now(timezone.utc)
    uid = user.id

    for group in Group.query.filter_by(owner_id=uid).all():
        successor = GroupMember.query.filter(
            GroupMember.group_id == group.id,
            GroupMember.user_id != uid,
            GroupMember.role == 'admin',
        ).first()
        if not successor:
            successor = GroupMember.query.filter(
                GroupMember.group_id == group.id,
                GroupMember.user_id != uid,
            ).first()
        if successor:
            group.owner_id = successor.user_id
            successor.role = 'owner'
        else:
            gid = group.id
            icon = group.icon
            for message in Message.query.filter_by(group_id=gid).all():
                ForwardedMessage.query.filter(
                    (ForwardedMessage.original_message_id == message.id)
                    | (ForwardedMessage.forwarded_message_id == message.id)
                ).delete(synchronize_session=False)
            Message.query.filter_by(group_id=gid).delete(synchronize_session=False)
            GroupMember.query.filter_by(group_id=gid).delete(synchronize_session=False)
            GroupJoinRequest.query.filter_by(group_id=gid).delete(synchronize_session=False)
            GroupBan.query.filter_by(group_id=gid).delete(synchronize_session=False)
            db.session.delete(group)
            delete_uploaded_file(icon, app)

    for s in UserSession.query.filter_by(user_id=uid, is_active=True).all():
        s.end_session()

    GroupMember.query.filter_by(user_id=uid).delete(synchronize_session=False)
    GroupJoinRequest.query.filter_by(user_id=uid).delete(synchronize_session=False)
    GroupBan.query.filter_by(user_id=uid).delete(synchronize_session=False)
    Contact.query.filter(
        (Contact.owner_id == uid) | (Contact.contact_id == uid)
    ).delete(synchronize_session=False)
    BlockedUser.query.filter(
        (BlockedUser.blocker_id == uid) | (BlockedUser.blocked_id == uid)
    ).delete(synchronize_session=False)

    old_avatar = user.avatar
    try:
        user.avatar = generate_avatar_file(DELETED_ACCOUNT_NAME, 'avatar', app)
        delete_uploaded_file(old_avatar, app)
    except Exception:
        pass

    user.is_deleted = True
    user.deleted_at = now
    user.bio = ''
    user.status = 'offline'
    user.username = f'deleted_{uid}'
    user.password = secrets.token_hex(64)
    user.email = ''
    user.email_hash = secrets.token_hex(32)
    db.session.commit()


# ── Платформенный админ (владелец / поддержка) ─────────────────────────────

BAN_PERIODS = {
    '30d':     {'days': 30,   'label': '30 дней'},
    '90d':     {'days': 90,   'label': '90 дней'},
    '180d':    {'days': 180,  'label': 'полгода'},
    '365d':    {'days': 365,  'label': '1 год'},
    '10y':     {'days': 3650, 'label': '10 лет'},
    'forever': {'days': None, 'label': 'навсегда'},
}


def is_platform_admin(user):
    """Аккаунт fsociety — владелец/поддержка."""
    if not user or not getattr(user, 'is_authenticated', True):
        return False
    if not user.is_authenticated:
        return False
    from config import Config
    name = (getattr(user, 'username', None) or '').strip()
    if not name:
        return False
    return name.lower() == Config.PLATFORM_ADMIN_USERNAME.lower()


def ensure_admin_privacy(user, db):
    """Дефолтная приватность для аккаунта владельца: писать могут контакты, время никто."""
    if not is_platform_admin(user):
        return
    from models import UserPrivacy
    privacy = UserPrivacy.query.filter_by(user_id=user.id).first()
    if not privacy:
        privacy = UserPrivacy(user_id=user.id, who_can_write='contacts', last_seen='nobody')
        db.session.add(privacy)
    else:
        privacy.who_can_write = 'contacts'
        privacy.last_seen = 'nobody'
    db.session.commit()


def get_active_user_ban(user_id, db):
    """Возвращает активный бан или None; просроченные удаляет."""
    from models import UserBan
    from datetime import datetime, timezone
    ban = UserBan.query.filter_by(user_id=user_id).first()
    if not ban:
        return None
    if not ban.is_active():
        db.session.delete(ban)
        db.session.commit()
        return None
    return ban


def terminate_user_sessions(user_id, db):
    """Завершает все активные сессии пользователя."""
    from models import UserSession
    from datetime import datetime, timezone
    sessions = UserSession.query.filter_by(user_id=user_id, is_active=True).all()
    for s in sessions:
        s.end_session()
    db.session.commit()


def apply_platform_ban(user_id, period, banned_by_id, db):
    """Блокирует пользователя на платформе. Возвращает (ok, error_message)."""
    from models import UserBan, User
    from datetime import datetime, timedelta, timezone

    if period not in BAN_PERIODS:
        return False, 'Недопустимый срок блокировки'

    target = User.query.get(user_id)
    if not target or target.is_deleted:
        return False, 'Пользователь не найден'

    if is_platform_admin(target):
        return False, 'Нельзя заблокировать администратора'

    days = BAN_PERIODS[period]['days']
    expires_at = None if days is None else datetime.now(timezone.utc) + timedelta(days=days)

    ban = UserBan.query.filter_by(user_id=user_id).first()
    if ban:
        ban.banned_by_id = banned_by_id
        ban.banned_at = datetime.now(timezone.utc)
        ban.expires_at = expires_at
    else:
        ban = UserBan(user_id=user_id, banned_by_id=banned_by_id, expires_at=expires_at)
        db.session.add(ban)

    db.session.commit()
    terminate_user_sessions(user_id, db)
    return True, None


# ── Системный бот Proton (рассылка обновлений) ─────────────────────────────

def is_platform_bot(user):
    """Системный аккаунт Proton — официальные уведомления."""
    if not user:
        return False
    from config import Config
    return user.username.lower() == Config.PLATFORM_BOT_USERNAME.lower()


def get_proton_bot_user(db):
    from models import User
    from config import Config
    from sqlalchemy import func as sa_func
    return User.query.filter(
        sa_func.lower(User.username) == Config.PLATFORM_BOT_USERNAME.lower()
    ).first()


def ensure_proton_bot_user(db, app):
    """Создаёт или восстанавливает аккаунт Proton."""
    from models import User, UserPrivacy
    from config import Config
    from encryption import hmac_hash
    from security import PasswordSecurity
    import secrets

    bot = get_proton_bot_user(db)
    if bot:
        if not bot.avatar:
            bot.avatar = generate_avatar_file(Config.PLATFORM_BOT_USERNAME, 'avatar', app)
            db.session.commit()
        return bot

    email = f'proton-bot@{secrets.token_hex(8)}.internal'
    bot = User(
        email=email,
        email_hash=hmac_hash(email),
        username=Config.PLATFORM_BOT_USERNAME,
        password=PasswordSecurity.hash_password(secrets.token_hex(32)),
        bio='Официальные обновления и уведомления',
        status='online',
    )
    db.session.add(bot)
    db.session.commit()
    bot.avatar = generate_avatar_file(Config.PLATFORM_BOT_USERNAME, 'avatar', app)
    privacy = UserPrivacy(user_id=bot.id, who_can_write='nobody', last_seen='nobody')
    db.session.add(privacy)
    db.session.commit()
    return bot


def get_or_create_proton_chat(user_id, proton_id, db):
    from models import Chat
    if user_id == proton_id:
        return None
    chat = Chat.query.filter(
        ((Chat.user1_id == user_id) & (Chat.user2_id == proton_id)) |
        ((Chat.user1_id == proton_id) & (Chat.user2_id == user_id))
    ).first()
    if not chat:
        chat = Chat(user1_id=proton_id, user2_id=user_id)
        db.session.add(chat)
        db.session.flush()
    return chat


def ensure_proton_chat_for_user(user_id, db, app=None):
    """Гарантирует личный чат пользователя с Proton."""
    from models import User
    bot = get_proton_bot_user(db)
    if not bot and app:
        bot = ensure_proton_bot_user(db, app)
    if not bot:
        return None
    user = User.query.get(user_id)
    if not user or user.is_deleted or is_platform_bot(user):
        return None
    chat = get_or_create_proton_chat(user.id, bot.id, db)
    db.session.commit()
    return chat


def ensure_proton_chats_for_all_users(db, app):
    """Создаёт чаты Proton для всех зарегистрированных пользователей."""
    from models import User
    bot = ensure_proton_bot_user(db, app)
    users = User.query.filter(User.is_deleted == False, User.id != bot.id).all()
    for u in users:
        if is_platform_bot(u):
            continue
        get_or_create_proton_chat(u.id, bot.id, db)
    db.session.commit()
    return bot


def send_proton_broadcast(content, db, app):
    """Рассылает сообщение от Proton всем пользователям. Возвращает (count, error)."""
    from models import User, Message
    from datetime import datetime, timezone
    from socketio_events import socketio, _serialize_message, _emit_chat_update

    content = (content or '').strip()
    if not content:
        return 0, 'Введите текст сообщения'

    bot = ensure_proton_bot_user(db, app)
    now = datetime.now(timezone.utc)
    users = User.query.filter(User.is_deleted == False, User.id != bot.id).all()

    pending = []
    for u in users:
        if is_platform_bot(u):
            continue
        chat = get_or_create_proton_chat(u.id, bot.id, db)
        msg = Message(
            sender_id=bot.id,
            receiver_id=u.id,
            chat_id=chat.id,
            content=content,
            is_read=False,
        )
        chat.last_message_at = now
        db.session.add(msg)
        db.session.flush()
        pending.append((msg, u.id, chat.id))

    db.session.commit()

    for msg, uid, chat_id in pending:
        payload = _serialize_message(msg, uid, app)
        socketio.emit('new_message', {**payload, 'chat_id': chat_id}, to=f'chat_{chat_id}')
        socketio.emit('new_message', {**payload, 'chat_id': chat_id}, to=f'user_{uid}')
        _emit_chat_update(chat_id, msg, uid)

    return len(pending), None


SAVED_CHAT_NAME = 'Избранное'


def migrate_saved_chat_column(db):
    """Добавляет колонку is_saved в таблицу chat (для существующих БД)."""
    from sqlalchemy import inspect, text
    try:
        inspector = inspect(db.engine)
        if 'chat' not in inspector.get_table_names():
            return
        cols = [c['name'] for c in inspector.get_columns('chat')]
        if 'is_saved' in cols:
            return
        dialect = db.engine.dialect.name
        with db.engine.begin() as conn:
            if dialect == 'postgresql':
                conn.execute(text(
                    'ALTER TABLE chat ADD COLUMN is_saved BOOLEAN NOT NULL DEFAULT FALSE'
                ))
            else:
                conn.execute(text(
                    'ALTER TABLE chat ADD COLUMN is_saved BOOLEAN NOT NULL DEFAULT 0'
                ))
        print('[saved] колонка chat.is_saved добавлена')
    except Exception as e:
        print(f'[saved] migrate is_saved: {e}')


def migrate_message_features(db):
    """Новые таблицы/колонки: опросы, стикеры, message_read."""
    from sqlalchemy import inspect, text
    try:
        db.create_all()
        inspector = inspect(db.engine)
        if 'message' not in inspector.get_table_names():
            return
        cols = {c['name'] for c in inspector.get_columns('message')}
        dialect = db.engine.dialect.name
        additions = []
        if 'message_type' not in cols:
            additions.append(
                "message_type VARCHAR(20) NOT NULL DEFAULT 'text'"
                if dialect == 'postgresql' else
                "message_type VARCHAR(20) NOT NULL DEFAULT 'text'"
            )
        if additions:
            with db.engine.begin() as conn:
                for col in additions:
                    conn.execute(text(f'ALTER TABLE message ADD COLUMN {col}'))
            print('[migrate] message: добавлены новые колонки')
    except Exception as e:
        print(f'[migrate] message features: {e}')


def get_saved_chat(user_id, db):
    from models import Chat
    return Chat.query.filter_by(user1_id=user_id, user2_id=user_id, is_saved=True).first()


def get_or_create_saved_chat(user_id, db):
    from models import Chat
    from datetime import datetime, timezone
    chat = get_saved_chat(user_id, db)
    if chat:
        return chat
    chat = Chat(
        user1_id=user_id,
        user2_id=user_id,
        is_saved=True,
        last_message_at=datetime.now(timezone.utc),
    )
    db.session.add(chat)
    db.session.flush()
    return chat


def saved_chat_has_messages(chat_id, db):
    from models import Message
    return Message.query.filter_by(chat_id=chat_id, is_deleted=False).count() > 0


def cleanup_empty_saved_chat(chat, db):
    """Удаляет чат «Избранное», если в нём не осталось сообщений."""
    from models import Message, ForwardedMessage
    if not chat or not chat.is_saved:
        return False
    if saved_chat_has_messages(chat.id, db):
        return False
    msgs = Message.query.filter_by(chat_id=chat.id).all()
    for message in msgs:
        ForwardedMessage.query.filter(
            (ForwardedMessage.original_message_id == message.id) |
            (ForwardedMessage.forwarded_message_id == message.id)
        ).delete()
    Message.query.filter_by(chat_id=chat.id).delete()
    db.session.delete(chat)
    return True
