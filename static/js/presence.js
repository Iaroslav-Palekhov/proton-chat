/**
 * presence.js — глобальный WebSocket для статуса «в сети».
 * Один сокет на вкладку: работает на всех страницах, пока пользователь авторизован.
 */
(function () {
    var socket = null;
    var heartbeatTimer = null;

    function emitHeartbeat() {
        if (socket && socket.connected && document.visibilityState === 'visible') {
            socket.emit('heartbeat');
        }
    }

    function startHeartbeat() {
        if (heartbeatTimer) return;
        emitHeartbeat();
        heartbeatTimer = setInterval(emitHeartbeat, 20000);
    }

    window.getAppSocket = function () {
        if (socket) return socket;

        if (typeof io === 'undefined') {
            console.warn('[presence] socket.io не загружен');
            return null;
        }

        socket = io({
            transports: ['websocket'],
            reconnectionDelay: 1000,
            reconnectionAttempts: Infinity,
        });

        socket.on('connect', function () {
            startHeartbeat();
        });

        socket.on('reconnect', function () {
            emitHeartbeat();
        });

        if (!window.__presenceVisibilityBound) {
            window.__presenceVisibilityBound = true;
            document.addEventListener('visibilitychange', function () {
                if (document.visibilityState === 'visible') {
                    emitHeartbeat();
                }
            });
        }

        if (socket.connected) {
            startHeartbeat();
        }

        return socket;
    };

    if (window.__AUTH__) {
        window.getAppSocket();
    }
})();
