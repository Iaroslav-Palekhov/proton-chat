# Proton Messenger

![Version](https://img.shields.io/badge/version-1.2.9-blue.svg)
![Flask](https://img.shields.io/badge/Flask-2.3+-green.svg)
![Python](https://img.shields.io/badge/Python-3.9+-yellow.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-13+-lightblue.svg)
![License](https://img.shields.io/badge/license-GPL--3.0-red.svg)

Self-hosted messenger with private and group chats, file sharing, and real-time updates. The server is **Python + Flask**; the client is **Jinja2 templates + vanilla JavaScript** (no heavy frontend frameworks).

**Prefer Russian?** → [README-RU.md](README-RU.md) (full documentation in Russian)

---

## Table of contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Reverse proxy](#reverse-proxy)
- [HTTP API](#http-api)
- [Socket.IO events](#socketio-events)
- [Security](#security)
- [Development](#development)
- [License](#license)

---

## Features

### Messaging

Private chats with read receipts, message history, and typing indicators. Group chats with roles: owner, administrator, member.

Reply, forward (with optional hidden original sender), pin, edit, soft delete, and emoji reactions.

**Saved messages** chat — personal notes and files. The **Proton** system bot delivers platform update notifications.

### Groups

Create and manage groups: icon, description, member list, role changes, in-group bans. Send permissions: all members or admins only.

Public and private groups, join via invite link or username (`/g/<username>`), join requests for closed groups with moderation.

### File sharing

Uploads up to **500 MB**: images, documents, video, archives, executables. Files are sorted into separate folders. Images are compressed and resized to max 800×800 px on upload.

Attachments live outside public `static/` and are served only to authorized users via `/media/` and `/download/`.

### Group read receipts (v1.2.9)

In group chats, sent messages show **how many members read** them; tap to see the reader list with avatars.

### Link previews

Open Graph metadata (title, description, image) is fetched from URLs in messages. Processing runs in a background thread; the result is pushed to the client over Socket.IO without blocking the UI.

### Export and search

Export a private or group chat to a ZIP archive (HTML page + attachments). Full-text search inside a chat or group with match context.

### Media gallery

Browse all images, files, and other media from a chat or group via a dedicated endpoint.

### Session management

View active devices: browser, OS, IP, last activity. Remotely end one session or all except the current one.

### Contacts, privacy, and moderation

Contact list, block list, privacy settings (who can message you, last seen visibility). Message reports and a platform admin panel: user bans, report review, broadcasts via the system bot, **user creation** (when registration is closed).

### UI

Responsive layout with a desktop sidebar. Dark theme. Client-side time localization.

### Online presence (v1.2.8)

A **global WebSocket** (`static/js/presence.js`) keeps the user **online** on every authenticated page — settings, profile, contacts, etc. — not only inside a chat. Login, registration, and logged-out pages do not open a socket. The server uses a short grace period before marking a user offline (page navigation and multiple tabs).

---

## Tech stack

### Backend

| Technology | Purpose |
|------------|---------|
| Python 3.9+ | Core language |
| Flask | Web framework |
| Flask-SQLAlchemy | ORM |
| Flask-Login | Authentication and sessions |
| Flask-SocketIO + gevent | WebSocket and real-time |
| Flask-Compress | Gzip/Brotli response compression |
| PostgreSQL (psycopg2) | Database |
| Pillow | Image processing |
| BeautifulSoup4 | HTML parsing for link previews |
| cryptography | DB field encryption (AES-256-GCM) |
| user-agents | User-Agent parsing for devices page |
| requests | HTTP for link metadata |

### Frontend

| Technology | Purpose |
|------------|---------|
| Jinja2 | Templates |
| Vanilla JS + Fetch API | UI logic |
| Socket.IO (client) | Real-time updates |
| `presence.js` | Shared socket for online status on all pages |
| `chat_extras.js` | "Who has read" list for group messages |

### Infrastructure

| Component | Purpose |
|-----------|---------|
| Werkzeug `secure_filename` | Safe file names |
| UUID4 | Unique upload names |
| SQLAlchemy connection pool | Pool with `pool_pre_ping` |
| ProxyFix | Correct client IP behind Caddy/nginx |

---

## Architecture

```
.
├── proton.py                 # Entry point, app initialization
├── config.py                 # Configuration (DB, security)
├── local_config.py           # Local overrides (optional)
├── models.py                 # ORM models
├── routing.py                # HTTP routes and API
├── extra_routes.py           # Admin user creation, message readers
├── read_receipts.py          # Group read receipts logic
├── socketio_events.py        # Socket.IO handlers + presence tracking
├── security.py               # Hashing, rate limiting, sanitization
├── encryption.py             # Transparent DB field encryption
├── upload_security.py        # File and message access checks
├── utils.py                  # Files, link previews, avatars
├── start_proton.py           # Windows launcher (venv, autostart)
├── start_proton.bat
├── start_proton_linux.py     # Linux launcher (venv, systemd)
├── start_proton.sh
├── templates/                # HTML templates
├── static/
│   ├── css/
│   ├── js/
│   │   ├── presence.js       # Global online-status WebSocket
│   │   ├── chat_extras.js    # Group read receipts modal
│   │   ├── drafts.js
│   │   └── ...
│   └── icon/
├── private_uploads/          # Created automatically
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

### Data models

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
User ──< UserBan (platform-wide ban)
```

| Model | Description |
|-------|-------------|
| `User` | Account: email (encrypted), username, avatar, bio, status |
| `Chat` | Private chat; `is_saved` flag for Saved messages |
| `Group` | Group: icon, description, visibility, join type, invite token |
| `GroupMember` | Member and role (`owner` / `admin` / `member`) |
| `Message` | Text, file; reply, forward, pin, link preview |
| `MessageReaction` | Emoji reaction on a message |
| `MessageRead` | Message read receipt (groups) |
| `UserSession` | Active session with device metadata |
| `BlockedUser` | User-to-user block |
| `Contact` | Contact list entry |
| `UserPrivacy` | Privacy settings |
| `MessageReport` | Report on a message |
| `PasswordReset` | Password reset token |

---

## Quick start

### Requirements

- Python 3.9 or newer
- PostgreSQL 13+
- pip

### Windows

Run `start_proton.bat`. The script creates a virtual environment, installs dependencies, and can set up autostart.

```bat
start_proton.bat
```

### Linux

```bash
chmod +x start_proton.sh
./start_proton.sh
```

`start_proton_linux.py` also supports a user/systemd service and autostart.

Configure PostgreSQL and `config.py` before the first run (see [Configuration](#configuration)).

Open in the browser: **http://localhost:2200**

On first run the app automatically creates:

- upload folders under `private_uploads/`
- default generated avatars
- PostgreSQL tables
- `.secret_key` — cryptographically strong session secret (mode `0600`)
- `.encryption_key` — DB field encryption key (if not set manually)

---

## Configuration

Secrets and database credentials live in a `.env` file next to `config.py` (copy `example.env` to `.env` and fill it in; `.env` is git-ignored). Other settings are in `config.py`. For local overrides without editing that file, create `local_config.py` next to it — those values take precedence.

### Database (`.env`)

```env
DB_USER=postgres
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432
DB_NAME=proton

# PostgreSQL peer auth without password:
DB_SOCKET_DIR=/var/run/postgresql
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FLASK_SECRET_KEY` | Auto in `.secret_key` | Flask secret key |
| `PASSWORD_PEPPER` | empty | Pepper for password hashes — set in `.env`, never change after first run |
| `MESSAGE_ENCRYPTION_KEY` | Auto in `.encryption_key` | DB encryption key — set in `.env`, never change after first run |
| `PLATFORM_ADMIN_USERNAME` | `fsociety` | Platform admin username |
| `PLATFORM_BOT_USERNAME` | `Proton` | System bot username |
| `LINK_URL` | — | Base URL for group invite links |
| `PROTON_DEBUG` | — | `1` / `true` — debug mode with reloader |

### Important `config.py` options

| Option | Default | Description |
|--------|---------|-------------|
| `MAX_CONTENT_LENGTH` | 500 MB | Max file upload size |
| `UPLOAD_FOLDER` | `private_uploads` | Upload directory |
| `RATELIMIT_ENABLED` | `True` | Request rate limiting |
| `OPEN_REGISTRATION` | `False` | `True` — open sign-up (`/register` and the button on the login page). `False` — page and button hidden (404). If the option is commented out, defaults to `True` |
| `SESSION_COOKIE_SECURE` | `False` | Set `True` when using HTTPS only |

### Production

Use unique `PASSWORD_PEPPER` and `FLASK_SECRET_KEY`, enable secure cookies, disable debug:

```python
class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
```

Or uncomment the corresponding lines in `Config`.

---

## Reverse proxy

The app listens on `0.0.0.0:2200`. `ProxyFix` is enabled for correct IP and scheme behind Caddy or nginx.

```caddy
chat.example.com {
    reverse_proxy localhost:2200
}
```

For Socket.IO, the proxy must forward `Upgrade` and `Connection` headers. After HTTPS is set up, configure `LINK_URL` and enable `SESSION_COOKIE_SECURE`.

---

## HTTP API

### Authentication

| Method | Path | Description |
|--------|------|-------------|
| GET, POST | `/login` | Sign in |
| GET, POST | `/register` | Sign up (only when `OPEN_REGISTRATION = True`) |
| GET | `/logout` | Sign out |
| GET, POST | `/forgot_password` | Request password reset |
| GET, POST | `/reset_password/<token>` | Reset password |

### Chats

| Method | Path | Description |
|--------|------|-------------|
| GET | `/chats` | Chat and group list |
| GET | `/chat/<chat_id>` | Private chat |
| GET, POST | `/start_chat` | Start a dialog |
| POST | `/chat/<chat_id>/delete` | Delete chat |
| GET | `/api/get_chats_data` | JSON chat list |

### Groups

| Method | Path | Description |
|--------|------|-------------|
| GET | `/groups` | Group list |
| POST | `/group/create` | Create group |
| GET | `/group/<group_id>` | Group chat |
| GET | `/group/<group_id>/members` | Members |
| POST | `/group/<group_id>/add_member` | Add member |
| POST | `/group/<group_id>/remove_member/<user_id>` | Remove member |
| POST | `/group/<group_id>/change_role/<user_id>` | Change role |
| POST | `/group/<group_id>/edit` | Edit group |
| POST | `/group/<group_id>/delete` | Delete group |
| POST | `/group/<group_id>/leave` | Leave group |
| GET | `/group/<group_id>/invite_link` | Get invite link |
| POST | `/group/<group_id>/reset_invite_link` | Reset invite link |
| GET | `/join/<token>` | Join by token |
| GET | `/g/<group_username>` | Public group page |
| GET | `/groups/search` | Search groups |

### Messages

| Method | Path | Description |
|--------|------|-------------|
| POST | `/send_message` | Send message |
| POST | `/edit_message/<message_id>` | Edit |
| POST | `/delete_message/<message_id>` | Soft delete |
| POST | `/pin_message/<message_id>` | Pin |
| POST | `/unpin_message/<message_id>` | Unpin |
| POST | `/forward_message` | Forward |
| POST | `/message/<message_id>/react` | React |
| GET | `/message/<message_id>/reactions` | List reactions |
| GET | `/get_messages/<chat_id>` | Message history |
| GET | `/get_pinned_messages/<context_id>` | Pinned messages |
| GET | `/get_unread_counts` | Unread counts |
| GET | `/search_messages/<context_id>` | Search |
| GET | `/export_chat/<context_id>` | Export to ZIP |
| GET | `/get_media/<context_id>` | Media gallery |
| GET | `/media/<filepath>` | View attachment |
| GET | `/download/<filepath>` | Download attachment |

### Read receipts

| Method | Path | Description |
|--------|------|-------------|
| GET | `/message/<message_id>/readers` | Who read a message (groups) |

### Profile and contacts

| Method | Path | Description |
|--------|------|-------------|
| GET | `/profile/<user_id>` | Profile |
| GET, POST | `/profile/edit` | Edit profile |
| POST | `/profile/delete_account` | Delete account |
| GET | `/contacts` | Contacts |
| POST | `/contacts/add/<user_id>` | Add contact |
| POST | `/contacts/remove/<user_id>` | Remove contact |
| GET | `/api/search_users` | Search users |

### Security

| Method | Path | Description |
|--------|------|-------------|
| GET | `/security` | Security hub |
| GET | `/security/password` | Change password page |
| POST | `/security/change_password` | Change password |
| GET | `/security/devices` | Active sessions |
| POST | `/security/terminate_session/<session_id>` | End session |
| POST | `/security/terminate_all_sessions` | End all except current |
| GET | `/security/blacklist` | Block list |
| POST | `/security/block/<user_id>` | Block user |
| POST | `/security/unblock/<user_id>` | Unblock user |
| GET, POST | `/security/privacy` | Privacy settings |

### Administration

| Method | Path | Description |
|--------|------|-------------|
| GET | `/dashboard` | Admin panel |
| POST | `/dashboard/ban` | Ban user |
| POST | `/dashboard/unban/<user_id>` | Unban user |
| GET | `/dashboard/bot` | System bot management |
| POST | `/dashboard/bot/send` | Broadcast from bot |
| GET | `/dashboard/api/reports` | Message reports |
| POST | `/dashboard/create_user` | Create user (admin) |

---

## Socket.IO events

### Presence (`presence.js`)

- Loaded on every **authenticated** page via `templates/base.html`
- One connection per browser tab: `window.getAppSocket()`
- Chat pages reuse the same socket and call `join_chat` / `join_group` on top of it
- `heartbeat` every 20 seconds while the tab is visible
- Server tracks connection count per user; **5 second grace** before marking offline on disconnect

### Client → server

| Event | Data | Description |
|-------|------|-------------|
| `join_chat` | `{ chat_id }` | Join private chat room |
| `leave_chat` | `{ chat_id }` | Leave chat room |
| `join_group` | `{ group_id }` | Join group room |
| `leave_group` | `{ group_id }` | Leave group room |
| `send_message` | `{ chat_id / group_id, content, ... }` | Send message |
| `typing` | `{ chat_id / group_id }` | Typing indicator |
| `stop_typing` | `{ chat_id / group_id }` | Stop typing |
| `messages_read` | `{ chat_id / group_id }` | Mark as read |
| `delete_message` | `{ message_id }` | Delete message |
| `edit_message` | `{ message_id, content }` | Edit message |
| `pin_message` | `{ message_id }` | Pin message |
| `heartbeat` | — | Keep online, update `last_seen` |

### Server → client

| Event | Description |
|-------|-------------|
| `new_message` | New message |
| `chat_updated` | Chat list metadata updated |
| `group_updated` | Group metadata updated |
| `message_deleted` | Message deleted |
| `message_edited` | Message edited |
| `link_preview_ready` | Link preview ready |
| `typing` / `stop_typing` | Typing indicator |
| `messages_read` | Messages marked read |
| `user_status` | User online / offline |
| `chat_deleted` | Chat deleted |

Rooms: `chat_{id}`, `group_{id}`, `user_{id}`.

---

## Security

### Authentication and sessions

Passwords are hashed with Werkzeug plus a pepper. Each session gets a unique `session_token` in the database; remote session termination invalidates it immediately. Cookies use `HttpOnly` and `SameSite=Lax`; `Secure` is enabled in production.

### Data encryption

Sensitive fields (email, bio, message text, session data, link preview data) are encrypted with AES-256-GCM at the ORM layer via `encryption.py`. Email lookup uses a deterministic HMAC hash in `email_hash`.

### Files

Names go through `secure_filename`; files get a UUID4 prefix. Images are recompressed with Pillow (max 800×800, quality 85). Attachment access is checked in `upload_security.py`.

### Access control

Built-in rate limiter in `security.py`. Input sanitization via `InputSanitizer`. Security headers (CSP, X-Frame-Options, X-Content-Type-Options) in `after_request`. Blocks and privacy settings are enforced when sending messages.

### WebSocket

WebSockets require `gevent` and `gevent-websocket` — the monkey-patch must remain the **first line** of `proton.py`.

---

## Development

| Task | Where to look |
|------|----------------|
| New route or API | `routing.py`, `extra_routes.py` |
| New table | `models.py` |
| Real-time logic | `socketio_events.py` |
| Chat UI | `templates/chat.html`, `templates/group_chat.html` |
| Read receipts (UI) | `static/js/chat_extras.js`, `templates/_chat_input_extras.html` |
| Read receipts (logic) | `read_receipts.py` |
| Global online status | `static/js/presence.js`, `socketio_events.py` |
| Auth and limits | `security.py` |

Recommended commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:` ([Conventional Commits](https://www.conventionalcommits.org/)).

Debug: set `PROTON_DEBUG=1` or use `DevelopmentConfig`.

---

## License

Distributed under **GPL-3.0**. See [LICENSE](LICENSE).
