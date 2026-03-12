// SSE Connection Health Monitor
// Safety net for Gradio SSE stream failures. Works alongside the Gradio JS patch
// in patch_basic.py that fixes the root cause (stream_status.open not reset on error).
// This monitor detects two failure modes:
// 1. Server unreachable (network failure, server crash)
// 2. Gradio UI stuck in pending state (SSE stream died, results never delivered)
//
// Session cleanup on unload:
// Intercepts fetch() calls to capture the Gradio session hash, then sends a
// close-session beacon on beforeunload so the server can remove stale sessions.
// This prevents session accumulation (gradio_sse_sessions growing unbounded)
// which causes the browser's HTTP/1.1 per-origin connection limit (6) to be
// exhausted when 5+ tabs are open, resulting in tabs stuck on "Loading...".

(function () {
    "use strict";

    const HEALTH_CHECK_INTERVAL = 30000;
    const HEALTH_CHECK_TIMEOUT = 10000;
    const STUCK_CHECK_INTERVAL = 10000;
    const STUCK_THRESHOLD = 60000; // 60s of being stuck before showing banner
    const CONSECUTIVE_FAILURES_THRESHOLD = 2;
    const BANNER_ID = "sse-health-banner";

    let consecutiveFailures = 0;
    let bannerDismissed = false;
    let stuckSince = null;
    let capturedSessionHash = null;

    // --- Session hash capture (installed immediately at script load) ---
    // Intercept fetch() to sniff the session_hash from /queue/data requests.
    // Must run before Gradio's own JS fires, so it's outside onUiLoaded.
    (function installFetchInterceptor() {
        const origFetch = window.fetch;
        window.fetch = function (resource, init) {
            if (!capturedSessionHash) {
                const url = (resource instanceof Request) ? resource.url : String(resource);
                if (url.includes("/queue/data")) {
                    try {
                        const match = url.match(/[?&]session_hash=([^&]+)/);
                        if (match) capturedSessionHash = decodeURIComponent(match[1]);
                    } catch (_) {}
                }
            }
            return origFetch.apply(this, arguments);
        };
    })();

    // --- Cleanup on tab close / reload ---
    window.addEventListener("beforeunload", function () {
        if (capturedSessionHash) {
            navigator.sendBeacon(
                "/internal/close-session?session_hash=" + encodeURIComponent(capturedSessionHash)
            );
        }
    });

    // --- Health check ---
    function checkServerHealth() {
        const controller = new AbortController();
        const timeoutId = setTimeout(function () { controller.abort(); }, HEALTH_CHECK_TIMEOUT);

        fetch("./internal/debug-state", { signal: controller.signal })
            .then(function (response) {
                clearTimeout(timeoutId);
                if (response.ok) {
                    consecutiveFailures = 0;
                    removeBanner();
                } else {
                    onHealthCheckFailed("HTTP " + response.status);
                }
            })
            .catch(function (err) {
                clearTimeout(timeoutId);
                onHealthCheckFailed(err.name === "AbortError" ? "timeout" : err.message);
            });
    }

    function onHealthCheckFailed(reason) {
        consecutiveFailures++;
        console.warn("[SSE Monitor] Health check failed (" + consecutiveFailures + "): " + reason);
        if (consecutiveFailures >= CONSECUTIVE_FAILURES_THRESHOLD) {
            showBanner("Server connection lost. UI may not respond correctly.");
        }
    }

    // Detect Gradio UI stuck in generating/loading state without progress
    function checkForStuckUI() {
        if (bannerDismissed) return;

        // Gradio wraps components in .wrap.generating or .wrap.loading during calls
        let stuckIndicators = document.querySelectorAll('.wrap.generating, .wrap.loading');
        // But exclude normal generation (which has a progressDiv)
        let hasProgressBar = document.querySelector('.progressDiv') !== null;

        if (stuckIndicators.length > 0 && !hasProgressBar) {
            if (!stuckSince) {
                stuckSince = Date.now();
            } else if (Date.now() - stuckSince > STUCK_THRESHOLD) {
                showBanner("UI appears stuck. The connection to the server may have been lost.");
            }
        } else {
            stuckSince = null;
        }
    }

    function showBanner(message) {
        if (bannerDismissed || document.getElementById(BANNER_ID)) return;

        let banner = document.createElement("div");
        banner.id = BANNER_ID;
        banner.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#b91c1c;color:#fff;padding:8px 16px;display:flex;align-items:center;justify-content:center;gap:12px;font-size:14px;font-family:system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,0.3);";

        let text = document.createElement("span");
        text.textContent = message;

        let reloadBtn = document.createElement("button");
        reloadBtn.textContent = "Reload Page";
        reloadBtn.style.cssText = "background:#fff;color:#b91c1c;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-weight:bold;font-size:13px;";
        reloadBtn.onclick = function () { location.reload(); };

        let dismissBtn = document.createElement("button");
        dismissBtn.textContent = "Dismiss";
        dismissBtn.style.cssText = "background:transparent;color:#fff;border:1px solid #fff;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px;";
        dismissBtn.onclick = function () {
            removeBanner();
            bannerDismissed = true;
        };

        banner.appendChild(text);
        banner.appendChild(reloadBtn);
        banner.appendChild(dismissBtn);
        document.body.appendChild(banner);
    }

    function removeBanner() {
        let el = document.getElementById(BANNER_ID);
        if (el) el.remove();
    }

    onUiLoaded(function () {
        setTimeout(function () {
            checkServerHealth();
            setInterval(checkServerHealth, HEALTH_CHECK_INTERVAL);
            setInterval(checkForStuckUI, STUCK_CHECK_INTERVAL);
        }, 5000);
    });
})();
