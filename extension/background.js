// Background Service Worker for ISM Tesla Importer

chrome.runtime.onInstalled.addListener(() => {
  console.log("ISM Tesla Importer extension installed.");
});

// Helper to get CSRF token from cookies or session API
async function getCsrfToken(origin) {
  // 1. Try reading from chrome.cookies
  try {
    const cookie = await chrome.cookies.get({ url: origin, name: "csrf_token" });
    if (cookie && cookie.value && cookie.value !== "None") {
      return cookie.value;
    }
  } catch (e) {
    console.debug("Cookie read error:", e);
  }

  // 2. Try fetching from get_session_info
  try {
    const res = await fetch(`${origin}/api/method/ism.api.tesla.get_session_info`, {
      method: "GET",
      credentials: "include",
      headers: { "Accept": "application/json" }
    });
    const json = await res.json();
    const token = json?.message?.csrf_token;
    if (token) {
      chrome.storage.local.set({ csrf_token: token });
      return token;
    }
  } catch (e) {
    console.debug("Failed fetching session CSRF info:", e);
  }

  // 3. Fallback to cached token in storage
  return new Promise((resolve) => {
    chrome.storage.local.get(["csrf_token"], (data) => {
      resolve(data?.csrf_token || "");
    });
  });
}

// Listen for messages from content scripts
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "SYNC_ISM_CONTEXT") {
    const updates = {
      active_ro: request.ro,
      ism_origin: request.origin || (sender.tab ? new URL(sender.tab.url).origin : null)
    };
    if (request.csrf_token) {
      updates.csrf_token = request.csrf_token;
    }
    chrome.storage.local.set(updates, () => {
      sendResponse({ status: "ok" });
    });
    return true;
  }

  if (request.action === "GET_CONTEXT") {
    chrome.storage.local.get(["active_ro", "ism_origin", "csrf_token"], (data) => {
      sendResponse(data);
    });
    return true;
  }

  if (request.action === "FETCH_ACTIVE_ROS") {
    chrome.storage.local.get(["ism_origin"], (data) => {
      const origin = request.origin || data.ism_origin || "http://localhost:8000";
      fetch(`${origin}/api/method/ism.api.tesla.get_active_repair_orders`, {
        method: "GET",
        credentials: "include",
        headers: { "Accept": "application/json" }
      })
      .then(res => res.json())
      .then(json => {
        const msg = json.message;
        let orders = [];
        let csrf = "";
        if (Array.isArray(msg)) {
          orders = msg;
        } else if (msg && typeof msg === "object") {
          orders = msg.orders || [];
          csrf = msg.csrf_token || "";
        }
        if (csrf) {
          chrome.storage.local.set({ csrf_token: csrf });
        }
        sendResponse({ success: true, orders: orders, csrf_token: csrf });
      })
      .catch(err => {
        sendResponse({ success: false, error: err.message });
      });
    });
    return true;
  }

  if (request.action === "IMPORT_PROCEDURE") {
    chrome.storage.local.get(["ism_origin", "csrf_token"], async (data) => {
      const origin = request.origin || data.ism_origin || "http://localhost:8000";
      let csrfToken = request.csrf_token || data.csrf_token || "";

      if (!csrfToken) {
        csrfToken = await getCsrfToken(origin);
      }

      const endpoint = `${origin}/api/method/ism.api.tesla.import_tesla_procedure`;
      const formData = new FormData();
      formData.append("repair_order", request.repair_order);
      formData.append("data", JSON.stringify(request.procedure_data));
      if (csrfToken) {
        formData.append("csrf_token", csrfToken);
      }
      if (request.allow_generation_mismatch) {
        formData.append("allow_generation_mismatch", "1");
      }
      if (request.duplicate_action) {
        formData.append("duplicate_action", request.duplicate_action);
      }

      const headers = { "Accept": "application/json" };
      if (csrfToken) {
        headers["X-Frappe-CSRF-Token"] = csrfToken;
      }

      fetch(endpoint, {
        method: "POST",
        credentials: "include",
        headers: headers,
        body: formData
      })
      .then(res => res.json())
      .then(json => {
        if (json.exc || json._server_messages) {
          sendResponse({ success: false, error: json.exc || "Server error occurred" });
        } else {
          sendResponse({ success: true, result: json.message });
        }
      })
      .catch(err => {
        sendResponse({ success: false, error: err.message });
      });
    });
    return true;
  }
});
