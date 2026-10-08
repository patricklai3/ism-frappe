// Content script running on ISM Desk pages to broadcast active Repair Order context

function checkAndSyncActiveRO() {
  try {
    let ro = null;

    // 1. Try reading from localStorage
    const raw = localStorage.getItem("ism_active_ro");
    if (raw) {
      try {
        ro = JSON.parse(raw);
      } catch (e) {}
    }

    // 2. Try extracting from current URL if on a Repair Order page
    const href = window.location.href;
    const match = href.match(/(?:Form\/Repair%20Order\/|repair-order\/)([A-Za-z0-9-_]+)/i);
    if (match && (!ro || ro.name !== match[1])) {
      const roName = decodeURIComponent(match[1]);
      ro = {
        name: roName,
        timestamp: Date.now()
      };
    }

    // 3. Extract CSRF token from cookie
    let csrfToken = "";
    try {
      const cMatch = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
      if (cMatch) csrfToken = decodeURIComponent(cMatch[1]);
    } catch (e) {}

    // 4. Broadcast to extension background
    if (ro && ro.name) {
      chrome.runtime.sendMessage({
        action: "SYNC_ISM_CONTEXT",
        ro: ro,
        csrf_token: csrfToken,
        origin: window.location.origin
      });
    }
  } catch (e) {
    console.debug("ISM sync error:", e);
  }
}

// Run immediately and periodically or on navigation
checkAndSyncActiveRO();
window.addEventListener("storage", (e) => {
  if (e.key === "ism_active_ro") {
    checkAndSyncActiveRO();
  }
});
window.addEventListener("hashchange", checkAndSyncActiveRO);
window.addEventListener("popstate", checkAndSyncActiveRO);
setInterval(checkAndSyncActiveRO, 2500);
