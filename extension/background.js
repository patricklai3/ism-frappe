// Background Service Worker for ISM Tesla Importer (Manifest V3)

chrome.runtime.onInstalled.addListener(async () => {
  console.log("ISM Tesla Importer extension installed/updated.");
  // Purge any accidental tesla.com origins saved in storage
  const stored = await getStorage(["ism_origin"]);
  if (stored.ism_origin && stored.ism_origin.includes("tesla.com")) {
    await setStorage({ ism_origin: "http://localhost:8000" });
  }
});

// Storage Promise helpers
function getStorage(keys) {
  return new Promise((resolve) => {
    chrome.storage.local.get(keys, (data) => {
      resolve(data || {});
    });
  });
}

function setStorage(items) {
  return new Promise((resolve) => {
    chrome.storage.local.set(items, () => {
      resolve();
    });
  });
}

// Origin sanitizer to prevent tesla.com or invalid URLs from poisoning ISM origin
function sanitizeIsmOrigin(candidate) {
  if (!candidate || typeof candidate !== "string") return null;
  const trimmed = candidate.trim().replace(/\/+$/, "");
  // Must NOT be a Tesla Service or documentation website
  if (trimmed.includes("tesla.com") || trimmed.includes("service.tesla")) {
    return null;
  }
  // Must start with http:// or https://
  if (!trimmed.startsWith("http://") && !trimmed.startsWith("https://")) {
    return null;
  }
  return trimmed;
}

// Resolve the proper ISM origin, falling back to localhost:8000
async function getEffectiveOrigin(requestOrigin) {
  const reqClean = sanitizeIsmOrigin(requestOrigin);
  if (reqClean) return reqClean;

  const data = await getStorage(["ism_origin"]);
  const storedClean = sanitizeIsmOrigin(data.ism_origin);
  if (storedClean) return storedClean;

  // Clean poisoned storage if present
  if (data.ism_origin && data.ism_origin.includes("tesla.com")) {
    await setStorage({ ism_origin: "http://localhost:8000" });
  }

  return "http://localhost:8000";
}

// Fetch helper with timeout
async function fetchWithTimeout(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(timeoutId);
    return res;
  } catch (err) {
    clearTimeout(timeoutId);
    if (err.name === "AbortError") {
      throw new Error(`Connection timed out after ${Math.round(timeoutMs / 1000)}s. Please check if ISM is running.`);
    }
    throw err;
  }
}

// Extract human-friendly error messages from Frappe responses
function extractFrappeErrorMessage(json, httpStatus) {
  if (!json) {
    return `Server returned HTTP ${httpStatus || "error"}.`;
  }

  // 1. Check _server_messages (Frappe's structured error list)
  if (json._server_messages) {
    try {
      const msgs = typeof json._server_messages === "string" ? JSON.parse(json._server_messages) : json._server_messages;
      if (Array.isArray(msgs) && msgs.length > 0) {
        const item = typeof msgs[0] === "string" ? JSON.parse(msgs[0]) : msgs[0];
        if (item?.message) {
          return item.message.replace(/<[^>]+>/g, "").trim();
        }
      }
    } catch (e) {}
  }

  // 2. Check for PermissionError or 403 Forbidden
  if (
    httpStatus === 403 ||
    json.exc_type === "PermissionError" ||
    (typeof json.exception === "string" && json.exception.includes("PermissionError"))
  ) {
    return "Session expired or not logged in. Please open ISM in your browser, log in, and try again.";
  }

  // 3. Check json.exception
  if (typeof json.exception === "string" && json.exception) {
    const parts = json.exception.split(":");
    return parts.length > 1 ? parts.slice(1).join(":").trim() : json.exception;
  }

  // 4. Check json.message
  if (typeof json.message === "string" && json.message) {
    return json.message;
  }

  return `Request failed with status ${httpStatus || 500}`;
}

// CSRF token retrieval helper
async function getCsrfToken(origin) {
  // 1. Try reading from chrome.cookies
  try {
    const cleanUrl = origin.endsWith("/") ? origin : origin + "/";
    const cookie = await chrome.cookies.get({ url: cleanUrl, name: "csrf_token" });
    if (cookie && cookie.value && cookie.value !== "None") {
      return cookie.value;
    }
  } catch (e) {
    console.debug("Cookie read error:", e);
  }

  // 2. Try fetching from get_session_info
  try {
    const res = await fetchWithTimeout(`${origin}/api/method/ism.api.tesla.get_session_info`, {
      method: "GET",
      credentials: "include",
      headers: { "Accept": "application/json" }
    }, 5000);
    const json = await res.json();
    const token = json?.message?.csrf_token;
    if (token) {
      await setStorage({ csrf_token: token });
      return token;
    }
  } catch (e) {
    console.debug("Failed fetching session CSRF info:", e);
  }

  // 3. Fallback to cached token in storage
  const cached = await getStorage(["csrf_token"]);
  return cached?.csrf_token || "";
}

// Action Handlers
async function handleSyncContext(request, sender) {
  try {
    const updates = {};
    if (request.ro !== undefined) {
      updates.active_ro = request.ro;
    }

    // Only accept origin from request or sender if NOT tesla.com
    const cleanOrigin = sanitizeIsmOrigin(request.origin) ||
      (sender.tab?.url ? sanitizeIsmOrigin(new URL(sender.tab.url).origin) : null);

    if (cleanOrigin) {
      updates.ism_origin = cleanOrigin;
    }
    if (request.csrf_token) {
      updates.csrf_token = request.csrf_token;
    }
    await setStorage(updates);
    return { status: "ok" };
  } catch (err) {
    return { status: "error", error: err.message };
  }
}

async function handleGetContext() {
  try {
    const data = await getStorage(["active_ro", "ism_origin", "csrf_token"]);
    if (data.ism_origin && data.ism_origin.includes("tesla.com")) {
      data.ism_origin = "http://localhost:8000";
      await setStorage({ ism_origin: "http://localhost:8000" });
    }
    return data || {};
  } catch (err) {
    return {};
  }
}

async function handleFetchActiveRos(request, sender) {
  try {
    const origin = await getEffectiveOrigin(request.origin);
    const res = await fetchWithTimeout(`${origin}/api/method/ism.api.tesla.get_active_repair_orders`, {
      method: "GET",
      credentials: "include",
      headers: { "Accept": "application/json" }
    }, 10000);

    let json;
    try {
      json = await res.json();
    } catch (e) {
      return {
        success: false,
        orders: [],
        error: `Server (${origin}) returned non-JSON response (${res.status}). Ensure ISM is running and you are logged in.`
      };
    }

    if (!res.ok || json.exc || json.exception) {
      const errorMsg = extractFrappeErrorMessage(json, res.status);
      return { success: false, orders: [], error: errorMsg };
    }

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
      await setStorage({ csrf_token: csrf });
    }
    return { success: true, orders: orders, csrf_token: csrf };
  } catch (err) {
    return { success: false, orders: [], error: err.message || String(err) };
  }
}

async function handleImportProcedure(request, sender) {
  try {
    const origin = await getEffectiveOrigin(request.origin);
    const data = await getStorage(["csrf_token"]);
    let csrfToken = request.csrf_token || data.csrf_token || "";

    if (!csrfToken) {
      csrfToken = await getCsrfToken(origin);
    }

    const endpoint = `${origin}/api/method/ism.api.tesla.import_tesla_procedure`;
    const formData = new FormData();
    formData.append("repair_order", request.repair_order);
    formData.append("data", JSON.stringify(request.procedure_data));
    if (request.side || request.procedure_data?.side) {
      formData.append("side", request.side || request.procedure_data.side);
    }
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

    const res = await fetchWithTimeout(endpoint, {
      method: "POST",
      credentials: "include",
      headers: headers,
      body: formData
    }, 25000);

    let json;
    try {
      json = await res.json();
    } catch (e) {
      return { success: false, error: `Invalid server response (${res.status} ${res.statusText}) from ${origin}` };
    }

    if (!res.ok || json.exc || json._server_messages || json.exception) {
      const errorMsg = extractFrappeErrorMessage(json, res.status);
      return { success: false, error: errorMsg };
    }

    return { success: true, result: json.message };
  } catch (err) {
    return { success: false, error: err.message || String(err) };
  }
}

// Master message listener
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "SYNC_ISM_CONTEXT") {
    handleSyncContext(request, sender)
      .then(res => sendResponse(res))
      .catch(err => sendResponse({ status: "error", error: err.message }));
    return true;
  }

  if (request.action === "GET_CONTEXT") {
    handleGetContext()
      .then(res => sendResponse(res))
      .catch(() => sendResponse({}));
    return true;
  }

  if (request.action === "FETCH_ACTIVE_ROS") {
    handleFetchActiveRos(request, sender)
      .then(res => sendResponse(res))
      .catch(err => sendResponse({ success: false, orders: [], error: err.message }));
    return true;
  }

  if (request.action === "IMPORT_PROCEDURE") {
    handleImportProcedure(request, sender)
      .then(res => sendResponse(res))
      .catch(err => sendResponse({ success: false, error: err.message || String(err) }));
    return true;
  }
});
