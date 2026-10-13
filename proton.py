# gevent monkey-patch — ОБЯЗАТЕЛЬНО первой строкой, до всех других импортов!
# Без этого WebSocket-апгрейд падает с AssertionError в Werkzeug.
# Установка: pip install gevent gevent-websocket
try:
    from gevent import monkey
    monkey.patch_all()
except ImportError:
    pass  # gevent не установлен — WS не будет работать, см. README

from flask import Flask, session, redirect, url_for
from flask_login import LoginManager, logout_user, current_user
from config import Config
from models import db, User, UserSession
from routing import register_routes
from utils import create_upload_folders, migrate_legacy_avatars, migrate_saved_chat_column, is_platform_admin, ensure_admin_privacy, ensure_proton_chats_for_all_users
from security import init_security
from socketio_events import socketio
from datetime import datetime
import os
import secrets

login_manager = LoginManager()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # За Caddy/nginx: реальный IP клиента в request.remote_addr
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    except ImportError:
        pass

    # Flask-SocketIO
    _link = (app.config.get('LINK_URL') or '').rstrip('/')
    _extra = (app.config.get('SOCKETIO_CORS_ORIGINS') or '').strip()
    if _extra:
        _cors = [x.strip().rstrip('/') for x in _extra.split(',') if x.strip()]
    else:
        _cors = ([_link] if _link else [])
        # Allow common alias domain (ru <-> online) to avoid Origin mismatch
        if _link.endswith('chat.proton-net.ru'):
            _cors.append('https://chat.proton-net.online')
        elif _link.endswith('chat.proton-net.online'):
            _cors.append('https://chat.proton-net.ru')
    socketio.init_app(app, cors_allowed_origins=_cors)

    # Сжатие ответов (gzip/brotli) — уменьшает трафик на 60-80%
    try:
        from flask_compress import Compress
        Compress(app)
    except ImportError:
        print("[INFO] flask-compress не установлен. Запусти: pip install flask-compress")

    # Инициализация расширений
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'login'

    @login_manager.user_loader
    def load_user(user_id):
        user = User.query.get(int(user_id))
        if user and user.is_deleted:
            return None
        return user

    # Создаём папки для загрузок
    with app.app_context():
        create_upload_folders(app)
        from config import database_uri_for_log, DB_PASSWORD, DB_NAME
        print(f'[DB] Подключение: {database_uri_for_log()}')
        try:
            db.create_all()
        except Exception as e:
            err = str(e).lower()
            print()
            print('=' * 60)
            print('  ОШИБКА ПОДКЛЮЧЕНИЯ К PostgreSQL')
            print('=' * 60)
            print(f'  Строка: {database_uri_for_log()}')
            if 'password authentication failed' in err:
                print('  Пароль в config.py не совпадает с паролем пользователя postgres в PostgreSQL.')
                print('  На сервере выполни:')
                print(f"    sudo -u postgres psql -c \"ALTER USER postgres PASSWORD '{DB_PASSWORD}';\"")
                print('  Проверь также файл local_config.py (если есть) — он перебивает config.py.')
                print('  Если в psql заходишь без пароля (peer auth), в config.py укажи:')
                print("    DB_SOCKET_DIR = '/var/run/postgresql'")
            elif 'does not exist' in err and 'database' in err:
                print(f'  Создай базу: sudo -u postgres createdb {DB_NAME}')
            else:
                print(f'  {e}')
            print('=' * 60)
            print()
            raise
        migrate_legacy_avatars(app, db)
        migrate_saved_chat_column(db)
        from utils import migrate_message_features
        migrate_message_features(db)

        # Приватность по умолчанию для аккаунта владельца (fsociety)
        from sqlalchemy import func as sa_func
        admin_user = User.query.filter(
            sa_func.lower(User.username) == Config.PLATFORM_ADMIN_USERNAME.lower()
        ).first()
        if admin_user:
            ensure_admin_privacy(admin_user, db)

        ensure_proton_chats_for_all_users(db, app)

        # Оптимизация SQLite — только если используется SQLite
        if 'sqlite' in app.config.get('SQLALCHEMY_DATABASE_URI', ''):
            from sqlalchemy import event
            @event.listens_for(db.engine, 'connect')
            def set_sqlite_pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute('PRAGMA journal_mode=WAL')     # параллельные чтения + запись
                cur.execute('PRAGMA synchronous=NORMAL')   # быстрее, всё ещё безопасно
                cur.execute('PRAGMA cache_size=-32000')    # 32MB кэш в памяти
                cur.execute('PRAGMA temp_store=MEMORY')    # временные таблицы в RAM
                cur.execute('PRAGMA mmap_size=268435456')  # 256MB memory-mapped I/O
                cur.execute('PRAGMA foreign_keys=ON')      # целостность FK
                cur.execute('PRAGMA busy_timeout=10000')   # ждать 10с при блокировке
                cur.close()

    # Регистрируем маршруты
    register_routes(app, db, login_manager)

    # Инициализируем модуль безопасности (после регистрации маршрутов!)
    init_security(app)

    # ============================================================
    # КАСТОМНЫЕ СТРАНИЦЫ ОШИБОК
    # ============================================================

    @app.errorhandler(404)
    def page_not_found(e):
        from flask import render_template
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def internal_error(e):
        from flask import render_template
        db.session.rollback()
        return render_template('404.html', error_code=500,
                               error_title="Ошибка сервера",
                               error_message="Что-то пошло не так. Попробуйте позже."), 500

    # ============================================================
    # ПРОВЕРКА ВАЛИДНОСТИ СЕССИИ ПРИ КАЖДОМ ЗАПРОСЕ
    # ============================================================
    # Если сессия была завершена удалённо (например через страницу
    # безопасности с другого устройства), пользователь будет
    # принудительно разлогинен при следующем запросе.

    @app.before_request
    def validate_session():
        # Проверяем только авторизованных пользователей
        if not current_user.is_authenticated:
            return

        # Пропускаем статику
        from flask import request
        if request.endpoint and request.endpoint.startswith('static'):
            return

        # Получаем токен текущей сессии из Flask session
        session_token = session.get('session_token')

        if not session_token:
            tok = secrets.token_hex(32)
            session['session_token'] = tok
            ua_string = request.user_agent.string or ''
            try:
                import user_agents as ua_lib
                ua = ua_lib.parse(ua_string)
                browser = f"{ua.browser.family} {ua.browser.version_string}".strip()
                os_info = f"{ua.os.family} {ua.os.version_string}".strip()
                if ua.is_mobile:
                    device_type = 'mobile'
                elif ua.is_tablet:
                    device_type = 'tablet'
                else:
                    device_type = 'desktop'
            except Exception:
                browser, os_info, device_type = ua_string[:80] or 'Unknown', 'Unknown', 'desktop'

            from utils import get_client_ip
            ip = get_client_ip()

            sess = UserSession(
                user_id=current_user.id,
                session_token=tok,
                ip_address=ip,
                user_agent=ua_string[:500],
                browser=browser[:100],
                os=os_info[:100],
                device_type=device_type,
                is_active=True,
                is_current=True,
            )
            db.session.add(sess)
            db.session.commit()
            return

        # Кэшируем результат валидации в g, чтобы не лезть в БД дважды за запрос
        from flask import g as _g
        cache_key = f'sess_valid_{session_token}'
        if hasattr(_g, cache_key):
            return

        # Ищем сессию в БД
        user_session = UserSession.query.filter_by(
            session_token=session_token,
            user_id=current_user.id
        ).first()

        if not user_session:
            # Токен не найден в БД — разлогиниваем
            logout_user()
            session.clear()
            return redirect(url_for('login'))

        if not user_session.is_active:
            # Сессия была завершена удалённо — принудительно разлогиниваем
            logout_user()
            session.clear()
            return redirect(url_for('login'))

        # Помечаем как проверенное для этого запроса
        setattr(_g, cache_key, True)

        # Обновляем время последней активности (не чаще раза в минуту)
        now = datetime.utcnow()
        delta = now - user_session.last_active
        if delta.total_seconds() > 60:
            user_session.last_active = now
            db.session.commit()

    return app

if __name__ == '__main__':
    app = create_app()
    _debug = os.environ.get('PROTON_DEBUG', '').lower() in ('1', 'true', 'yes')
    # reloader ломает systemd и автозапуск — только при явном PROTON_DEBUG=1
    socketio.run(
        app,
        debug=_debug,
        use_reloader=_debug,
        host='0.0.0.0',
        port=2200,
    )