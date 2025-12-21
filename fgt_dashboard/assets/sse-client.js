/**
 * SSE Client for real-time dashboard updates
 * Connects to backend and triggers refresh when check-in events arrive
 */
(function() {
    let eventSource = null;
    let reconnectAttempts = 0;
    const maxReconnectAttempts = 10;
    const reconnectDelay = 3000;
    let fallbackInterval = null;

    function updateIndicator(status) {
        const dot = document.getElementById('connection-dot');
        const text = document.getElementById('connection-text');
        if (!dot || !text) {
            console.log('Indicator elements not found yet, retrying...');
            setTimeout(() => updateIndicator(status), 500);
            return;
        }

        const colors = {
            connected: { bg: '#10b981', text: 'Live (SSE)' },
            connecting: { bg: '#f59e0b', text: 'Connecting...' },
            fallback: { bg: '#f59e0b', text: 'Polling (fallback)' },
            disconnected: { bg: '#ef4444', text: 'Disconnected' }
        };

        const c = colors[status] || colors.disconnected;
        dot.style.backgroundColor = c.bg;
        dot.style.boxShadow = '0 0 10px ' + c.bg;
        text.style.color = c.bg;
        text.textContent = c.text;
        console.log('SSE status:', status);
    }

    function triggerRefresh() {
        // Find and click the refresh button to trigger Dash callback
        const btn = document.getElementById('btn-refresh');
        if (btn) {
            console.log('Triggering dashboard refresh');
            btn.click();
        } else {
            console.log('Refresh button not found');
        }
    }

    function startFallbackPolling() {
        if (fallbackInterval) return;
        updateIndicator('fallback');
        fallbackInterval = setInterval(triggerRefresh, 30000);
        console.log('Started fallback polling');
    }

    function stopFallbackPolling() {
        if (fallbackInterval) {
            clearInterval(fallbackInterval);
            fallbackInterval = null;
            console.log('Stopped fallback polling');
        }
    }

    function connectSSE() {
        if (eventSource) {
            eventSource.close();
        }

        updateIndicator('connecting');

        // Connect to backend SSE endpoint
        // In dev: localhost:8000, in prod: same origin (nginx proxies)
        const sseUrl = window.location.port === '8050'
            ? 'http://localhost:8000/api/events/stream'
            : '/api/events/stream';

        console.log('Connecting to SSE:', sseUrl);

        try {
            eventSource = new EventSource(sseUrl);

            eventSource.onopen = function(e) {
                console.log('SSE connection opened');
                reconnectAttempts = 0;
                stopFallbackPolling();
                updateIndicator('connected');
            };

            eventSource.addEventListener('connected', function(e) {
                console.log('SSE connected event received:', e.data);
                updateIndicator('connected');
            });

            eventSource.addEventListener('checkin', function(e) {
                console.log('New checkin event:', e.data);
                triggerRefresh();
            });

            eventSource.addEventListener('update', function(e) {
                console.log('Update event:', e.data);
                triggerRefresh();
            });

            eventSource.onerror = function(e) {
                console.error('SSE error:', e);
                console.log('EventSource readyState:', eventSource.readyState);

                // Only handle if connection is closed
                if (eventSource.readyState === EventSource.CLOSED) {
                    eventSource.close();

                    if (reconnectAttempts < maxReconnectAttempts) {
                        reconnectAttempts++;
                        updateIndicator('connecting');
                        console.log('Reconnecting... attempt ' + reconnectAttempts);
                        setTimeout(connectSSE, reconnectDelay);
                    } else {
                        console.log('Max reconnect attempts reached, falling back to polling');
                        startFallbackPolling();
                    }
                }
            };
        } catch (err) {
            console.error('Failed to create EventSource:', err);
            startFallbackPolling();
        }
    }

    // Wait for DOM to be ready
    function init() {
        console.log('SSE client initializing...');
        // Small delay to ensure Dash has rendered
        setTimeout(connectSSE, 1000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Reconnect when tab becomes visible
    document.addEventListener('visibilitychange', function() {
        if (document.visibilityState === 'visible') {
            console.log('Tab visible, checking SSE connection...');
            if (!eventSource || eventSource.readyState === EventSource.CLOSED) {
                reconnectAttempts = 0;
                connectSSE();
            }
        }
    });
})();
