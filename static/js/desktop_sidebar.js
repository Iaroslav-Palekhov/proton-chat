/* desktop_sidebar.js
   Sidebar that loads chats via API and handles navigation
   without iframes — just normal page loads, sidebar persists
   by being rendered on every page (chat.html, group_chat.html, chats.html)
*/

// Called on DOMContentLoaded from each page
function dsStartLiveChats() {
    const socket = typeof window.getAppSocket === 'function' ? window.getAppSocket() : null;
    if (socket) {
        function onReady() { dsFetchAndRender(); }
        socket.on('connect', onReady);
        socket.on('chat_updated', () => dsFetchAndRender());
        socket.on('reconnect', () => dsFetchAndRender());
        if (socket.connected) onReady();
    } else {
        dsFetchAndRender();
    }

    // Сбрасываем поле поиска при загрузке (защита от автозаполнения браузера)
    const si = document.querySelector('.ds-search input');
    if (si) si.value = '';
}

function dsFetchAndRender() {
    fetch('/api/get_chats_data')
        .then(r => r.ok ? r.json() : null)
        .then(data => { if (data) dsRenderChats(data.chats); })
        .catch(() => {});
}

function dsRenderChats(chats) {
    const list = document.getElementById('dsChatsList');
    if (!list) return;
    const scroll = list.scrollTop;

    // Detect current chat URL for active highlighting
    const currentUrl = window.location.pathname;

    let html = '';
    if (chats && chats.length > 0) {
        chats.forEach(c => {
            const isActive = currentUrl === new URL(c.chat_url, window.location.origin).pathname;
            const activeClass = isActive ? ' ds-active' : '';

            if (c.type === 'saved') {
                html += `
                <a href="${c.chat_url}" class="ds-chat-item${activeClass}" data-chat-id="${c.id}" data-name="Избранное" data-type="saved">
                    <div class="ds-chat-ava">
                        <div class="ds-saved-ava">
                            <svg width="20" height="20" viewBox="0 0 24 24" class="ico"><use xlink:href="#icon-bookmark"></use></svg>
                        </div>
                    </div>
                    <div class="ds-chat-info">
                        <div class="ds-chat-row">
                            <span class="ds-chat-name">Избранное</span>
                            <span class="ds-chat-time">${c.last_message_time}</span>
                        </div>
                        <div class="ds-chat-preview-row">
                            <span class="ds-preview-text">${c.last_message}</span>
                        </div>
                    </div>
                    <button class="ds-delete-btn" onclick="event.preventDefault();event.stopPropagation();dsDeleteChat(${c.id},'Избранное')" title="Удалить">
                        <svg width="14" height="14" viewBox="0 0 24 24" class="ico"><use xlink:href="#icon-delete"></use></svg>
                    </button>
                </a>`;
            } else if (c.type === 'private') {
                // Не показываем badge если чат сейчас открыт — сообщения уже читаются
                const showUnread = c.unread_count > 0 && !isActive;
                html += `
                <a href="${c.chat_url}" class="ds-chat-item${activeClass}" data-chat-id="${c.id}" data-name="${dsEscape(c.other_username)}" data-type="private">
                    <div class="ds-chat-ava">
                        <img src="${c.other_avatar}" alt="">
                        <span class="ds-status ${c.other_status}"></span>
                        ${showUnread ? `<span class="ds-badge">${c.unread_count}</span>` : ''}
                    </div>
                    <div class="ds-chat-info">
                        <div class="ds-chat-row">
                            <span class="ds-chat-name">${dsEscape(c.other_username)}</span>
                            <span class="ds-chat-time">${c.last_message_time}</span>
                        </div>
                        <div class="ds-chat-preview-row">
                            <span class="ds-preview-text">${c.last_message}</span>
                            ${showUnread ? `<span class="ds-unread-count">${c.unread_count}</span>` : ''}
                        </div>
                    </div>
                    <button class="ds-delete-btn" onclick="event.preventDefault();event.stopPropagation();dsDeleteChat(${c.id},'${dsEscape(c.other_username)}')" title="Удалить">
                        <svg width="14" height="14" viewBox="0 0 24 24" class="ico"><use xlink:href="#icon-delete"></use></svg>
                    </button>
                </a>`;
            } else if (c.type === 'group') {
                // Не показываем badge если группа сейчас открыта
                const showUnread = c.unread_count > 0 && !isActive;
                html += `
                <a href="${c.chat_url}" class="ds-chat-item${activeClass}" data-chat-id="${c.id}" data-name="${dsEscape(c.group_name)}" data-type="group">
                    <div class="ds-chat-ava">
                        <img src="${c.group_icon}" alt="">
                        ${showUnread ? `<span class="ds-badge">${c.unread_count}</span>` : ''}
                    </div>
                    <div class="ds-chat-info">
                        <div class="ds-chat-row">
                            <span class="ds-chat-name">${dsEscape(c.group_name)}</span>
                            <span class="ds-chat-time">${c.last_message_time}</span>
                        </div>
                        <div class="ds-chat-preview-row">
                            <span class="ds-preview-text">${c.last_message}</span>
                            ${showUnread ? `<span class="ds-unread-count">${c.unread_count}</span>` : ''}
                        </div>
                    </div>
                </a>`;
            }
        });
    } else {
        html = `<div class="ds-empty">
            <svg width="48" height="48" viewBox="0 0 24 24" class="ico" style="opacity:0.25;"><use xlink:href="#icon-chat"></use></svg>
            <p>Нет чатов</p>
        </div>`;
    }

    if (list.innerHTML !== html) {
        list.innerHTML = html;
        list.scrollTop = scroll;
    }
}

function dsEscape(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function dsDeleteChat(chatId, chatName) {
    if (!confirm(`Удалить чат с ${chatName}? Все сообщения будут удалены.`)) return;
    fetch(`/chat/${chatId}/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            dsFetchAndRender();
            // If we deleted the currently open chat, go to chats list
            if (window.location.pathname.includes(`/chat/${chatId}`)) {
                window.location.href = '/chats';
            }
        } else {
            alert(data.error || 'Ошибка');
        }
    });
}

let _dsSearchTimeout = null;

function dsFilterChats(q) {
    const query = q.toLowerCase().trim();

    // Фильтруем существующие чаты
    document.querySelectorAll('.ds-chat-item').forEach(item => {
        const name = (item.dataset.name || '').toLowerCase();
        item.style.display = name.includes(query) ? '' : 'none';
    });

    // Удаляем старую секцию поиска
    const old = document.getElementById('dsUserSearchSection');
    if (old) old.remove();

    // Отменяем pending-запрос ВСЕГДА — до проверки на пустую строку,
    // иначе при быстром стирании таймер не отменяется и запрос всё равно уходит
    clearTimeout(_dsSearchTimeout);

    if (!query) return;

    _dsSearchTimeout = setTimeout(() => {
        Promise.all([
            fetch(`/api/search_users?q=${encodeURIComponent(q)}`).then(r => r.json()).catch(() => ({ users: [] })),
            fetch(`/groups/search?q=${encodeURIComponent(q)}`).then(r => r.json()).catch(() => ({ groups: [] }))
        ]).then(([userData, groupData]) => {
            const users  = userData.users  || [];
            const groups = groupData.groups || [];

            if (users.length === 0 && groups.length === 0) return;

            const section = document.createElement('div');
            section.id = 'dsUserSearchSection';

            const usersHtml = users.length > 0 ? `
                <div style="font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.7px;padding:12px 14px 6px;">Пользователи</div>
                ${users.map(u => `
                    <div class="ds-chat-item" style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;transition:background .15s;border-radius:10px;"
                         onmouseenter="this.style.background='var(--bg-hover)'"
                         onmouseleave="this.style.background=''">
                        <img src="${u.avatar}" alt="" style="width:38px;height:38px;border-radius:50%;object-fit:cover;flex-shrink:0;">
                        <div style="flex:1;min-width:0;">
                            <div style="font-size:14px;font-weight:600;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${dsEscape(u.username)}</div>
                            <div style="font-size:12px;color:var(--text-muted);">${u.status === 'online' ? 'Онлайн' : 'Не в сети'}</div>
                        </div>
                        <button onclick="dsChatFromSearch('${dsEscape(u.username)}')" title="Написать"
                                style="background:rgba(0,168,132,.12);color:var(--accent);border:none;border-radius:8px;padding:6px 10px;font-size:12px;font-weight:600;cursor:pointer;flex-shrink:0;">
                            Написать
                        </button>
                    </div>
                `).join('')}
            ` : '';

            const groupsHtml = groups.length > 0 ? `
                <div style="font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.7px;padding:12px 14px 6px;">Публичные группы</div>
                ${groups.map(g => `
                    <div class="ds-chat-item" style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;transition:background .15s;border-radius:10px;"
                         onmouseenter="this.style.background='var(--bg-hover)'"
                         onmouseleave="this.style.background=''"
                         onclick="window.location.href='/group/${g.id}'">
                        <img src="${g.icon}" alt="" style="width:38px;height:38px;border-radius:50%;object-fit:cover;flex-shrink:0;">
                        <div style="flex:1;min-width:0;">
                            <div style="font-size:14px;font-weight:600;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${dsEscape(g.name)}</div>
                            <div style="font-size:12px;color:var(--text-muted);">
                                ${g.members_count} участников · ${g.join_type === 'request' ? '📋 По заявке' : '✅ Открытая'}
                            </div>
                        </div>
                        <button onclick="event.stopPropagation();window.location.href='/group/${g.id}'" title="${g.is_member ? 'Открыть' : 'Вступить'}"
                                style="background:rgba(0,168,132,.12);color:var(--accent);border:none;border-radius:8px;padding:6px 10px;font-size:12px;font-weight:600;cursor:pointer;flex-shrink:0;">
                            ${g.is_member ? 'Открыть' : 'Вступить'}
                        </button>
                    </div>
                `).join('')}
            ` : '';

            section.innerHTML = usersHtml + groupsHtml;
            const chatsList = document.getElementById('dsChatsList');
            if (chatsList) chatsList.prepend(section);
        });
    }, 250);
}

function dsChatFromSearch(username) {
    window.location.href = `/start_chat?username=${encodeURIComponent(username)}`;
}