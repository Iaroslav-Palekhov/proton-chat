/* Общий код для chat.html и group_chat.html: список прочитавших сообщение в группе */
window.ChatExtras = (function() {
    function notify(msg, type) {
        if (typeof showNotification === 'function') showNotification(msg, type || 'info');
        else alert(msg);
    }

    function escapeAttr(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    }

    function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s || '';
        return d.innerHTML;
    }

    function showReaders(messageId) {
        fetch('/message/' + messageId + '/readers').then(function(r) { return r.json(); }).then(function(data) {
            if (data.error) { notify(data.error, 'error'); return; }
            var list = (data.readers || []).map(function(u) {
                return '<div class="reader-row"><img src="' + escapeAttr(u.avatar || '') + '" onerror="this.style.display=\'none\'"><span>' + escapeHtml(u.username) + '</span></div>';
            }).join('') || '<div class="reader-empty">Пока никто не прочитал</div>';
            var modal = document.createElement('div');
            modal.className = 'modal';
            modal.innerHTML = '<div class="modal-content" style="max-width:360px"><div class="modal-header"><h2>Прочитали</h2><span class="close-modal" onclick="this.closest(\'.modal\').remove()">&times;</span></div><div style="max-height:300px;overflow-y:auto">' + list + '</div></div>';
            document.body.appendChild(modal);
            modal.addEventListener('click', function(e) { if (e.target === modal) modal.remove(); });
        });
    }

    function init() {
        document.addEventListener('click', function(e) {
            var rs = e.target.closest('.read-status-clickable');
            if (rs) showReaders(rs.dataset.messageId);
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();

    return { showReaders: showReaders };
})();
