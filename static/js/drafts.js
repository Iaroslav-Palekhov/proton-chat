/**
 * drafts.js — система черновиков сообщений
 * Черновики хранятся в cookies.
 * Ключ: "draft_p_{chatId}" для личных чатов, "draft_g_{groupId}" для групп.
 * На chats.html рядом с превью последнего сообщения показывается иконка и начало черновика.
 */

const Drafts = (function () {
    const COOKIE_PREFIX_PRIVATE = 'draft_p_';
    const COOKIE_PREFIX_GROUP   = 'draft_g_';
    const COOKIE_DAYS           = 30;

    // ─── Cookie helpers ──────────────────────────────────────────────────────
    function setCookie(name, value, days) {
        const expires = new Date(Date.now() + days * 864e5).toUTCString();
        // encodeURIComponent чтобы пробелы/кириллица не ломали cookie
        document.cookie = encodeURIComponent(name) + '=' + encodeURIComponent(value)
            + '; expires=' + expires + '; path=/; SameSite=Lax';
    }

    function getCookie(name) {
        const key = encodeURIComponent(name) + '=';
        for (const part of document.cookie.split('; ')) {
            if (part.startsWith(key)) {
                try { return decodeURIComponent(part.slice(key.length)); }
                catch (e) { return ''; }
            }
        }
        return '';
    }

    function deleteCookie(name) {
        document.cookie = encodeURIComponent(name) + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax';
    }

    // ─── Public API ──────────────────────────────────────────────────────────
    function keyPrivate(chatId)  { return COOKIE_PREFIX_PRIVATE + chatId; }
    function keyGroup(groupId)   { return COOKIE_PREFIX_GROUP   + groupId; }

    /** Сохранить черновик */
    function save(key, text) {
        if (text && text.trim()) {
            setCookie(key, text, COOKIE_DAYS);
        } else {
            deleteCookie(key);
        }
    }

    /** Получить черновик (пустая строка если нет) */
    function get(key) {
        return getCookie(key) || '';
    }

    /** Удалить черновик (после отправки) */
    function remove(key) {
        deleteCookie(key);
    }

    /**
     * Инициализировать поле ввода: загрузить черновик и навесить авто-сохранение.
     * @param {string} key       - ключ черновика
     * @param {HTMLTextAreaElement} textarea
     * @param {Function} [resizeFn] - функция авторесайза (необязательно)
     */
    function initInput(key, textarea, resizeFn) {
        if (!textarea) return;

        // Загружаем черновик
        const saved = get(key);
        if (saved) {
            textarea.value = saved;
            if (resizeFn) resizeFn(textarea);
        }

        // Сохраняем при каждом вводе
        textarea.addEventListener('input', function () {
            save(key, this.value);
        });

        // Сохраняем при потере фокуса
        textarea.addEventListener('blur', function () {
            save(key, this.value);
        });

        // Сохраняем перед уходом со страницы
        window.addEventListener('beforeunload', function () {
            save(key, textarea.value);
        });
    }

    /**
     * Обновить превью черновиков на странице chats.html.
     * Ищет все .chat-item-wrapper с data-chat-id и data-type,
     * и если есть черновик — показывает его в .chat-preview p.
     */
    function renderDraftPreviews() {
        document.querySelectorAll('.chat-item-wrapper[data-chat-id]').forEach(function (wrapper) {
            const chatId = wrapper.dataset.chatId;
            const type   = wrapper.dataset.type; // 'private' или 'group'
            if (!chatId) return;

            const key    = type === 'group' ? keyGroup(chatId) : keyPrivate(chatId);
            const draft  = get(key);
            if (!draft) return;

            const previewP = wrapper.querySelector('.chat-preview p');
            if (!previewP) return;

            const preview = draft.length > 30 ? draft.substring(0, 30) + '…' : draft;

            // Сохраняем оригинальный контент (если ещё не сохраняли)
            if (!wrapper.dataset.origPreview) {
                wrapper.dataset.origPreview = previewP.innerHTML;
            }

            previewP.innerHTML =
                '<span style="color: var(--danger, #ff4757); font-weight: 600; margin-right: 4px;">'
                + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;margin-right:2px;">'
                + '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>'
                + '<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'
                + '</svg>'
                + 'Черновик:</span>'
                + escapeHtmlSimple(preview);
        });
    }

    function escapeHtmlSimple(s) {
        return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    return { keyPrivate, keyGroup, save, get, remove, initInput, renderDraftPreviews };
})();
