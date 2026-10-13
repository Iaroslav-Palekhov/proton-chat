# Proton Messenger

![Version](https://img.shields.io/badge/version-1.2.9-blue.svg)
![Flask](https://img.shields.io/badge/Flask-2.3+-green.svg)
![Python](https://img.shields.io/badge/Python-3.9+-yellow.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-13+-lightblue.svg)
![License](https://img.shields.io/badge/license-GPL--3.0-red.svg)

Self-hosted мессенджер с личными и групповыми чатами, обменом файлами и обновлениями в реальном времени. Сервер — **Python + Flask**; клиент — **шаблоны Jinja2 + vanilla JavaScript** (без тяжёлых фронтенд-фреймворков).

**English version** → [README.md](README.md)

---

## Содержание

- [Возможности](#возможности)
- [Технологический стек](#технологический-стек)
- [Архитектура](#архитектура)
- [Быстрый старт](#быстрый-старт)
- [Конфигурация](#конфигурация)
- [Reverse proxy](#reverse-proxy)
- [HTTP API](#http-api)
- [События Socket.IO](#события-socketio)
- [Безопасность](#безопасность)
- [Разработка](#разработка)
- [Лицензия](#лицензия)

---

## Возможности

### Сообщения и чаты

Личные диалоги с отметками о прочтении, историей переписки и индикатором набора текста. Групповые чаты с ролями: владелец, администратор, участник.

Ответы на сообщения, пересылка (с опциональным скрытием оригинального отправителя), закрепление, редактирование, мягкое удаление и реакции эмодзи.

Чат **«Избранное»** — личные заметки и файлы. Системный бот **Proton** доставляет уведомления об обновлениях платформы.

### Группы

Создание и управление группами: иконка, описание, список участников, смена ролей, бан внутри группы. Права на отправку: все участники или только администраторы.

Публичные и приватные группы, вступление по invite-ссылке или username (`/g/<username>`), заявки на вступление в закрытые группы с модерацией.

### Файлообменник

Отправка файлов до **500 МБ**: изображения, документы, видео, архивы, исполняемые файлы. Файлы раскладываются по отдельным каталогам. Изображения сжимаются и масштабируются до 800×800 px при загрузке.

Вложения хранятся вне публичной `static/` и отдаются только авторизованным пользователям через `/media/` и `/download/`.

### Кто прочитал в группах (v1.2.9)

В групповых чатах у отправленных сообщений отображается, **сколько человек прочитало**; по нажатию — список читателей с аватарами.

### Превью ссылок

Из URL в сообщениях извлекаются Open Graph-метаданные (заголовок, описание, картинка). Обработка идёт в фоновом потоке; результат приходит клиенту по Socket.IO без блокировки интерфейса.

### Экспорт и поиск

Экспорт личной или групповой переписки в ZIP (HTML-страница + вложения). Полнотекстовый поиск по истории чата или группы с контекстом совпадений.

### Медиагалерея

Просмотр всех изображений, файлов и других медиа из чата или группы через отдельный эндпоинт.

### Управление сессиями

Просмотр активных устройств: браузер, ОС, IP, время последней активности. Удалённое завершение одной сессии или всех, кроме текущей.

### Контакты, приватность и модерация

Список контактов, чёрный список, настройки приватности (кто может писать, видимость last seen). Жалобы на сообщения и панель администратора платформы: бан пользователей, рассмотрение жалоб, рассылка через системного бота, **создание пользователей** (при закрытой регистрации).

### Интерфейс

Адаптивная вёрстка с сайдбаром для десктопа. Тёмная тема. Локализация времени на стороне клиента.

### Статус «в сети» (v1.2.8)

**Глобальный WebSocket** (`static/js/presence.js`) держит пользователя **онлайн** на любой странице после входа — настройки, профиль, контакты и т.д., а не только внутри чата. На страницах входа, регистрации и после выхода сокет не открывается. Сервер даёт короткую паузу перед переводом в offline (переход между страницами и несколько вкладок).

---

## Технологический стек

### Backend

| Технология | Назначение |
|------------|------------|
| Python 3.9+ | Основной язык |
| Flask | Веб-фреймворк |
| Flask-SQLAlchemy | ORM |
| Flask-Login | Аутентификация и сессии |
| Flask-SocketIO + gevent | WebSocket и real-time |
| Flask-Compress | Gzip/Brotli сжатие ответов |
| PostgreSQL (psycopg2) | База данных |
| Pillow | Обработка изображений |
| BeautifulSoup4 | Парсинг HTML для превью ссылок |
| cryptography | Шифрование полей БД (AES-256-GCM) |
| user-agents | Парсинг User-Agent для страницы устройств |
| requests | HTTP для метаданных ссылок |

### Frontend

| Технология | Назначение |
|------------|------------|
| Jinja2 | Шаблоны |
| Vanilla JS + Fetch API | Интерактивность |
| Socket.IO (клиент) | Real-time обновления |
| `presence.js` | Общий сокет для статуса «в сети» на всех страницах |
| `chat_extras.js` | Список «Прочитали» для сообщений в группах |

### Инфраструктура

| Компонент | Назначение |
|-----------|------------|
| Werkzeug `secure_filename` | Безопасные имена файлов |
| UUID4 | Уникальные имена загрузок |
| SQLAlchemy connection pool | Пул соединений с `pool_pre_ping` |
| ProxyFix | Корректный IP за Caddy/nginx |

---

## Архитектура

```
.
├── proton.py                 # Точка входа, инициализация приложения
├── config.py                 # Конфигурация (БД, безопасность)
├── local_config.py           # Локальные переопределения (опционально)
├── models.py                 # ORM-модели
├── routing.py                # HTTP-маршруты и API
├── extra_routes.py           # Админ-создание пользователей, список прочитавших
├── read_receipts.py          # Логика отметок о прочтении в группах
├── socketio_events.py        # Обработчики Socket.IO + учёт presence
├── security.py               # Хеширование, rate limiting, санитизация
├── encryption.py             # Прозрачное шифрование полей БД
├── upload_security.py        # Проверка доступа к файлам и сообщениям
├── utils.py                  # Файлы, превью ссылок, аватары
├── start_proton.py           # Лаунчер Windows (venv, автозагрузка)
├── start_proton.bat
├── start_proton_linux.py     # Лаунчер Linux (venv, systemd)
├── start_proton.sh
├── templates/                # HTML-шаблоны
├── static/
│   ├── css/
│   ├── js/
│   │   ├── presence.js       # Глобальный WebSocket для статуса «в сети»
│   │   ├── chat_extras.js    # Окно «Прочитали» в группах
│   │   ├── drafts.js
│   │   └── ...
│   └── icon/
├── private_uploads/          # Создаётся автоматически
│   ├── avatars/
│   ├── group_icons/
│   ├── images/
│   ├── videos/
│   ├── audio/
│   ├── documents/
│   ├── archives/
│   ├── executables/
│   └── other/
└── requirements.txt
```

### Модели данных

```
User ──< Message >── Chat
 │                    │
 ├──< GroupMember >── Group ──< Message
 ├──< UserSession
 ├──< BlockedUser
 ├──< Contact
 ├──< UserPrivacy
 ├──< MessageReaction
 ├──< MessageRead

Group ──< GroupJoinRequest
Group ──< GroupBan
Message ──< MessageReport
User ──< UserBan (глобальный бан платформы)
```

| Модель | Описание |
|--------|----------|
| `User` | Аккаунт: email (зашифрован), username, avatar, bio, статус |
| `Chat` | Личный чат; флаг `is_saved` для «Избранного» |
| `Group` | Группа: иконка, описание, видимость, тип вступления, invite-токен |
| `GroupMember` | Участник и роль (`owner` / `admin` / `member`) |
| `Message` | Текст, файл; ответ, пересылка, закреп, превью ссылки |
| `MessageReaction` | Реакция эмодзи на сообщение |
| `MessageRead` | Отметка прочтения сообщения (группы) |
| `UserSession` | Активная сессия с метаданными устройства |
| `BlockedUser` | Блокировка между пользователями |
| `Contact` | Запись в списке контактов |
| `UserPrivacy` | Настройки приватности |
| `MessageReport` | Жалоба на сообщение |
| `PasswordReset` | Токен сброса пароля |

---

## Быстрый старт

### Требования

- Python 3.9 или выше
- PostgreSQL 13+
- pip

### Windows

Запустите `start_proton.bat`. Скрипт создаст виртуальное окружение, установит зависимости и может настроить автозагрузку.

```bat
start_proton.bat
```

### Linux

```bash
chmod +x start_proton.sh
./start_proton.sh
```

`start_proton_linux.py` также поддерживает user/systemd-сервис и автозапуск.

Перед первым запуском настройте PostgreSQL и `config.py` (см. [Конфигурация](#конфигурация)).

Откройте в браузере: **http://localhost:2200**

При первом запуске автоматически создаются:

- каталоги для загрузок в `private_uploads/`
- сгенерированные аватары по умолчанию
- таблицы в PostgreSQL
- `.secret_key` — криптографически стойкий секрет сессий (права `0600`)
- `.encryption_key` — ключ шифрования полей БД (если не задан вручную)

---

## Конфигурация

Секреты и данные подключения к БД хранятся в файле `.env` рядом с `config.py` (скопируйте `example.env` в `.env` и заполните; `.env` в `.gitignore`). Остальные настройки — в `config.py`. Для локальных переопределений без правки основного файла создайте `local_config.py` рядом с ним — его значения имеют приоритет.

### База данных (`.env`)

```env
DB_USER=postgres
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432
DB_NAME=proton

# PostgreSQL peer-авторизация без пароля:
DB_SOCKET_DIR=/var/run/postgresql
```

### Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `FLASK_SECRET_KEY` | Авто в `.secret_key` | Секретный ключ Flask |
| `PASSWORD_PEPPER` | пусто | Pepper для хешей паролей — задаётся в `.env`, после первого запуска не менять |
| `MESSAGE_ENCRYPTION_KEY` | Авто в `.encryption_key` | Ключ шифрования БД — задаётся в `.env`, после первого запуска не менять |
| `PLATFORM_ADMIN_USERNAME` | `fsociety` | Username администратора платформы |
| `PLATFORM_BOT_USERNAME` | `Proton` | Username системного бота |
| `LINK_URL` | — | Базовый URL для invite-ссылок групп |
| `PROTON_DEBUG` | — | `1` / `true` — режим отладки с reloader |

### Важные параметры в config.py

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `MAX_CONTENT_LENGTH` | 500 МБ | Максимальный размер файла |
| `UPLOAD_FOLDER` | `private_uploads` | Каталог загрузок |
| `RATELIMIT_ENABLED` | `True` | Ограничение частоты запросов |
| `OPEN_REGISTRATION` | `False` | `True` — открытая регистрация (`/register` и кнопка на входе). `False` — страница и кнопка скрыты (404). Если параметр закомментирован — считается `True` |
| `SESSION_COOKIE_SECURE` | `False` | `True` при работе только по HTTPS |

### Production

Задайте уникальные `PASSWORD_PEPPER` и `FLASK_SECRET_KEY`, включите secure-cookie, отключите отладку:

```python
class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
```

Или раскомментируйте соответствующие строки в `Config`.

---

## Reverse proxy

Приложение слушает `0.0.0.0:2200`. Включён `ProxyFix` для корректного IP и схемы за Caddy или nginx.

```caddy
chat.example.com {
    reverse_proxy localhost:2200
}
```

Для Socket.IO прокси должен передавать заголовки `Upgrade` и `Connection`. После настройки HTTPS задайте `LINK_URL` и включите `SESSION_COOKIE_SECURE`.

---

## HTTP API

### Аутентификация

| Метод | Путь | Описание |
|-------|------|----------|
| GET, POST | `/login` | Вход |
| GET, POST | `/register` | Регистрация (только при `OPEN_REGISTRATION = True`) |
| GET | `/logout` | Выход |
| GET, POST | `/forgot_password` | Запрос сброса пароля |
| GET, POST | `/reset_password/<token>` | Сброс пароля |

### Чаты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/chats` | Список диалогов и групп |
| GET | `/chat/<chat_id>` | Личный чат |
| GET, POST | `/start_chat` | Начать диалог |
| POST | `/chat/<chat_id>/delete` | Удалить чат |
| GET | `/api/get_chats_data` | JSON: список чатов |

### Группы

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/groups` | Список групп |
| POST | `/group/create` | Создать группу |
| GET | `/group/<group_id>` | Групповой чат |
| GET | `/group/<group_id>/members` | Участники |
| POST | `/group/<group_id>/add_member` | Добавить участника |
| POST | `/group/<group_id>/remove_member/<user_id>` | Удалить участника |
| POST | `/group/<group_id>/change_role/<user_id>` | Изменить роль |
| POST | `/group/<group_id>/edit` | Редактировать группу |
| POST | `/group/<group_id>/delete` | Удалить группу |
| POST | `/group/<group_id>/leave` | Покинуть группу |
| GET | `/group/<group_id>/invite_link` | Получить invite-ссылку |
| POST | `/group/<group_id>/reset_invite_link` | Сбросить invite-ссылку |
| GET | `/join/<token>` | Вступление по токену |
| GET | `/g/<group_username>` | Публичная страница группы |
| GET | `/groups/search` | Поиск групп |

### Сообщения

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/send_message` | Отправить сообщение |
| POST | `/edit_message/<message_id>` | Редактировать |
| POST | `/delete_message/<message_id>` | Мягкое удаление |
| POST | `/pin_message/<message_id>` | Закрепить |
| POST | `/unpin_message/<message_id>` | Открепить |
| POST | `/forward_message` | Переслать |
| POST | `/message/<message_id>/react` | Реакция |
| GET | `/message/<message_id>/reactions` | Список реакций |
| GET | `/get_messages/<chat_id>` | История сообщений |
| GET | `/get_pinned_messages/<context_id>` | Закреплённые |
| GET | `/get_unread_counts` | Непрочитанные |
| GET | `/search_messages/<context_id>` | Поиск |
| GET | `/export_chat/<context_id>` | Экспорт в ZIP |
| GET | `/get_media/<context_id>` | Медиагалерея |
| GET | `/media/<filepath>` | Просмотр вложения |
| GET | `/download/<filepath>` | Скачать вложение |

### Отметки о прочтении

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/message/<message_id>/readers` | Кто прочитал сообщение (группы) |

### Профиль и контакты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/profile/<user_id>` | Профиль |
| GET, POST | `/profile/edit` | Редактирование профиля |
| POST | `/profile/delete_account` | Удаление аккаунта |
| GET | `/contacts` | Контакты |
| POST | `/contacts/add/<user_id>` | Добавить контакт |
| POST | `/contacts/remove/<user_id>` | Удалить контакт |
| GET | `/api/search_users` | Поиск пользователей |

### Безопасность

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/security` | Раздел безопасности |
| GET | `/security/password` | Страница смены пароля |
| POST | `/security/change_password` | Сменить пароль |
| GET | `/security/devices` | Активные сессии |
| POST | `/security/terminate_session/<session_id>` | Завершить сессию |
| POST | `/security/terminate_all_sessions` | Завершить все, кроме текущей |
| GET | `/security/blacklist` | Чёрный список |
| POST | `/security/block/<user_id>` | Заблокировать |
| POST | `/security/unblock/<user_id>` | Разблокировать |
| GET, POST | `/security/privacy` | Настройки приватности |

### Администрирование

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/dashboard` | Панель администратора |
| POST | `/dashboard/ban` | Забанить пользователя |
| POST | `/dashboard/unban/<user_id>` | Разбанить |
| GET | `/dashboard/bot` | Управление системным ботом |
| POST | `/dashboard/bot/send` | Рассылка от бота |
| GET | `/dashboard/api/reports` | Жалобы на сообщения |
| POST | `/dashboard/create_user` | Создать пользователя (админ) |

---

## События Socket.IO

### Presence (`presence.js`)

- Подключается на каждой **авторизованной** странице через `templates/base.html`
- Одно соединение на вкладку браузера: `window.getAppSocket()`
- Страницы чатов переиспользуют тот же сокет и вызывают `join_chat` / `join_group` поверх него
- `heartbeat` каждые 20 секунд, пока вкладка видима
- Сервер считает активные подключения пользователя; **пауза 5 секунд** перед offline при отключении

### Клиент → сервер

| Событие | Данные | Описание |
|---------|--------|----------|
| `join_chat` | `{ chat_id }` | Войти в комнату личного чата |
| `leave_chat` | `{ chat_id }` | Покинуть комнату чата |
| `join_group` | `{ group_id }` | Войти в комнату группы |
| `leave_group` | `{ group_id }` | Покинуть группу |
| `send_message` | `{ chat_id / group_id, content, ... }` | Отправить сообщение |
| `typing` | `{ chat_id / group_id }` | Индикатор набора |
| `stop_typing` | `{ chat_id / group_id }` | Прекращение набора |
| `messages_read` | `{ chat_id / group_id }` | Отметить прочитанным |
| `delete_message` | `{ message_id }` | Удалить сообщение |
| `edit_message` | `{ message_id, content }` | Редактировать сообщение |
| `pin_message` | `{ message_id }` | Закрепить сообщение |
| `heartbeat` | — | Держать статус online, обновить `last_seen` |

### Сервер → клиент

| Событие | Описание |
|---------|----------|
| `new_message` | Новое сообщение |
| `chat_updated` | Обновлены метаданные в списке чатов |
| `group_updated` | Обновлены метаданные группы |
| `message_deleted` | Сообщение удалено |
| `message_edited` | Сообщение изменено |
| `link_preview_ready` | Превью ссылки готово |
| `typing` / `stop_typing` | Индикатор набора |
| `messages_read` | Сообщения прочитаны |
| `user_status` | Пользователь online / offline |
| `chat_deleted` | Чат удалён |

Комнаты: `chat_{id}`, `group_{id}`, `user_{id}`.

---

## Безопасность

### Аутентификация и сессии

Пароли хешируются через Werkzeug с pepper. Каждой сессии присваивается уникальный `session_token` в БД; при удалённом завершении сессии токен инвалидируется немедленно. Cookie с флагами `HttpOnly` и `SameSite=Lax`; в продакшене включается `Secure`.

### Шифрование данных

Чувствительные поля (email, bio, текст сообщений, данные сессий, превью ссылок) шифруются AES-256-GCM на уровне ORM через `encryption.py`. Для поиска по email используется детерминированный HMAC-хеш в поле `email_hash`.

### Файлы

Имена проходят через `secure_filename`; файлам назначается UUID4-префикс. Изображения пересжимаются через Pillow (макс. 800×800, quality 85). Доступ к вложениям проверяется в `upload_security.py`.

### Контроль доступа

Встроенный rate limiter в `security.py`. Санитизация входных данных через `InputSanitizer`. Security headers (CSP, X-Frame-Options, X-Content-Type-Options) в `after_request`. Блокировки и настройки приватности учитываются при отправке сообщений.

### WebSocket

Для WebSocket нужны `gevent` и `gevent-websocket` — monkey-patch должен оставаться **первой строкой** в `proton.py`.

---

## Разработка

| Задача | Где смотреть |
|--------|--------------|
| Новый маршрут или API | `routing.py`, `extra_routes.py` |
| Новая таблица | `models.py` |
| Real-time логика | `socketio_events.py` |
| UI чата | `templates/chat.html`, `templates/group_chat.html` |
| Отметки о прочтении (UI) | `static/js/chat_extras.js`, `templates/_chat_input_extras.html` |
| Отметки о прочтении (логика) | `read_receipts.py` |
| Глобальный статус «в сети» | `static/js/presence.js`, `socketio_events.py` |
| Авторизация и лимиты | `security.py` |

Рекомендуемые префиксы коммитов: `feat:`, `fix:`, `docs:`, `refactor:` ([Conventional Commits](https://www.conventionalcommits.org/)).

Отладка: `PROTON_DEBUG=1` или `DevelopmentConfig`.

---

## Лицензия

Распространяется под **GPL-3.0**. Подробнее — в файле [LICENSE](LICENSE).
