/**
 * time_local.js — конвертация UTC → локальное время пользователя.
 * Подключается глобально через base.html, работает на всех страницах.
 *
 * Все функции доступны как window.msgrFormatMsgTime, window.msgrFormatChatTime,
 * window.msgrFormatLastSeen, window.msgrFormatDateTime, window.msgrFormatDate.
 */
(function () {
    'use strict';

    // ─── Вспомогательные ───────────────────────────────────────────────────

    function parseUTC(iso) {
        if (!iso) return null;
        // Принимаем любой ISO: '2025-05-01T14:23:11Z', '2025-05-01T14:23:11',
        // '2025-05-01T14:23:11.123456' и т.п.
        var s = iso.trim();
        if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
        var d = new Date(s);
        return isNaN(d.getTime()) ? null : d;
    }

    function pad(n) { return n < 10 ? '0' + n : String(n); }

    function hhmm(d) {
        return pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    function ddmm(d) {
        var months = ['янв','фев','мар','апр','май','июн',
                      'июл','авг','сен','окт','ноя','дек'];
        return d.getDate() + ' ' + months[d.getMonth()];
    }

    function ddmmyy(d) {
        return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' +
               String(d.getFullYear()).slice(-2);
    }

    function isSameLocalDay(d1, d2) {
        return d1.getFullYear() === d2.getFullYear() &&
               d1.getMonth()    === d2.getMonth()    &&
               d1.getDate()     === d2.getDate();
    }

    function isYesterdayLocal(d, now) {
        var yesterday = new Date(now);
        yesterday.setDate(yesterday.getDate() - 1);
        return isSameLocalDay(d, yesterday);
    }

    function isSameYear(d, now) {
        return d.getFullYear() === now.getFullYear();
    }

    // ─── Публичные функции ─────────────────────────────────────────────────

    /**
     * Время сообщения: всегда ЧЧ:ММ в локальном времени.
     * Если передан null — возвращает текущее ЧЧ:ММ.
     */
    function formatMsgTime(iso) {
        var d = iso ? parseUTC(iso) : new Date();
        if (!d) return iso || '';
        return hhmm(d);
    }

    /**
     * Время в списке чатов: сегодня → ЧЧ:ММ, вчера → «Вчера»,
     * в этом году → «дд мес», старше → «дд.мм.гг».
     */
    function formatChatTime(iso) {
        if (!iso) return '';
        var d = parseUTC(iso);
        if (!d) return '';
        var now = new Date();
        var diffMs = now - d;
        var diffDays = Math.floor(diffMs / 86400000);
        if (isSameLocalDay(d, now)) return hhmm(d);
        if (isYesterdayLocal(d, now)) return 'Вчера';
        if (diffDays < 365 && isSameYear(d, now)) return ddmm(d);
        return ddmmyy(d);
    }

    /**
     * Дата+время: «дд.мм.гг ЧЧ:ММ» в локальном времени.
     */
    function formatDateTime(iso) {
        if (!iso) return '';
        var d = parseUTC(iso);
        if (!d) return iso;
        return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' +
               d.getFullYear() + ' ' + hhmm(d);
    }

    /**
     * Только дата: «дд.мм.гггг».
     */
    function formatDate(iso) {
        if (!iso) return '';
        var d = parseUTC(iso);
        if (!d) return iso;
        return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear();
    }

    /**
     * Last seen: умное форматирование.
     * Сегодня → «ЧЧ:ММ», вчера → «вчера в ЧЧ:ММ»,
     * в этом году → «дд мес в ЧЧ:ММ», старше → «дд.мм.гг».
     */
    function formatLastSeen(iso) {
        if (!iso || iso === 'недавно' || iso === 'давно') return iso || 'давно';
        var d = parseUTC(iso);
        if (!d) return iso;
        var now = new Date();
        if (isSameLocalDay(d, now)) return hhmm(d);
        if (isYesterdayLocal(d, now)) return 'вчера в ' + hhmm(d);
        if (isSameYear(d, now)) return ddmm(d) + ' в ' + hhmm(d);
        return ddmmyy(d);
    }

    // ─── Рендер DOM-элементов с data-utc ──────────────────────────────────

    function renderAll() {
        // Время сообщений (SSR)
        document.querySelectorAll('.msg-time[data-utc]').forEach(function (el) {
            var utc = el.getAttribute('data-utc');
            if (utc) el.textContent = formatMsgTime(utc);
        });

        // Время в списке чатов и групп
        document.querySelectorAll('[data-utc].chat-time, [data-utc].group-time').forEach(function (el) {
            var utc = el.getAttribute('data-utc');
            if (utc) el.textContent = formatChatTime(utc);
        });

        // Дата+время (устройства, и т.п.)
        document.querySelectorAll('.local-datetime[data-utc]').forEach(function (el) {
            var utc = el.getAttribute('data-utc');
            if (utc) el.textContent = formatDateTime(utc);
        });

        // Только дата (чёрный список и т.п.)
        document.querySelectorAll('.local-date[data-utc]').forEach(function (el) {
            var utc = el.getAttribute('data-utc');
            if (utc) el.textContent = formatDate(utc);
        });

        // Last seen
        document.querySelectorAll('.local-last-seen[data-utc]').forEach(function (el) {
            var utc = el.getAttribute('data-utc');
            if (utc) el.textContent = formatLastSeen(utc);
        });
    }

    // Запуск при DOMContentLoaded + после динамических изменений
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', renderAll);
    } else {
        renderAll();
    }

    // MutationObserver: подхватываем динамически добавленные элементы
    var observer = new MutationObserver(function (mutations) {
        var needRender = false;
        mutations.forEach(function (m) {
            m.addedNodes.forEach(function (node) {
                if (node.nodeType === 1) {
                    // Проверяем сам элемент и его детей
                    if (node.querySelector && node.querySelector('[data-utc]')) needRender = true;
                    if (node.hasAttribute && node.hasAttribute('data-utc')) needRender = true;
                }
            });
        });
        if (needRender) renderAll();
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // ─── Экспорт ───────────────────────────────────────────────────────────
    window.msgrFormatMsgTime  = formatMsgTime;
    window.msgrFormatChatTime = formatChatTime;
    window.msgrFormatDateTime = formatDateTime;
    window.msgrFormatDate     = formatDate;
    window.msgrFormatLastSeen = formatLastSeen;
    window.msgrRenderTimes    = renderAll;

})();
