// ISM Shop OS — Executive & Dispatch Cockpit (Vanilla ES Module)
// Consumes ism.api.board.get_cockpit_dashboard & ism.api.board.sync_cockpit_action.

const BOOT = window.ISM_COCKPIT_BOOT || {};
const CSRF_TOKEN = String(BOOT.csrf_token || "");

const state = {
  selectedDate: String(BOOT.initial_date || ""),
  statusTab: "open", // "open" | "snoozed" | "dismissed" | "all"
  priorityFilter: "ALL", // "ALL" | "Critical" | "High" | "Medium"
  view: BOOT.initial_snapshot && typeof BOOT.initial_snapshot === "object" ? BOOT.initial_snapshot : null,
  suppressedRecords: [], // Loaded from Shop Action Item resource + session fallback
  sessionSuppressed: new Map(), // dedup_key -> item metadata
  loading: false,
  lastFetchedAt: 0,
  realtimeMode: "fallback", // "socketio" | "fallback"
};

let debounceTimer = null;
let toastTimer = null;
let fallbackTimer = null;

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeHref(url) {
  const raw = String(url || "").trim();
  if (!raw) return "";
  if (raw.startsWith("/") && !raw.startsWith("//")) return raw;
  if (/^https?:\/\//i.test(raw)) return raw;
  return "";
}

function buildBoardThreadHref(threadKey, fallbackUrl = "") {
  if (threadKey) {
    return `/board?thread=${encodeURIComponent(String(threadKey))}`;
  }
  const safe = safeHref(fallbackUrl);
  if (safe && safe.startsWith("/board?thread=")) {
    const rawKey = safe.slice("/board?thread=".length);
    try {
      return `/board?thread=${encodeURIComponent(decodeURIComponent(rawKey))}`;
    } catch {
      return safe;
    }
  }
  return safe;
}

function getLocalIsoDate(dateObj = new Date()) {
  const y = dateObj.getFullYear();
  const m = String(dateObj.getMonth() + 1).padStart(2, "0");
  const d = String(dateObj.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function shiftIsoDate(isoDate, deltaDays) {
  const base = /^\d{4}-\d{2}-\d{2}$/.test(isoDate) ? isoDate : getLocalIsoDate();
  const [y, m, d] = base.split("-").map(Number);
  const dt = new Date(y, m - 1, d + deltaDays);
  return getLocalIsoDate(dt);
}

function parseServerDate(raw) {
  if (!raw) return null;
  const normalized = String(raw).trim().replace(" ", "T");
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatClock(raw) {
  const dt = parseServerDate(raw);
  if (!dt) return String(raw || "");
  return dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function showError(message) {
  const el = document.getElementById("errBanner");
  if (!el) return;
  if (!message) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.style.display = "block";
  el.textContent = message;
}

function showToast(message) {
  const el = document.getElementById("toastBanner");
  if (!el) return;
  if (toastTimer) {
    clearTimeout(toastTimer);
    toastTimer = null;
  }
  if (!message) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.style.display = "block";
  el.textContent = message;
  toastTimer = setTimeout(() => {
    el.style.display = "none";
    el.textContent = "";
  }, 3200);
}

function setRealtimeStatus(mode, label) {
  state.realtimeMode = mode;
  const badge = document.getElementById("realtimeBadge");
  if (!badge) return;
  badge.textContent = label;
  badge.classList.toggle("is-fallback", mode === "fallback");
  badge.classList.toggle("is-error", mode === "error");
}

function ruleBadgeLabel(rule) {
  const map = {
    quote_needed: "Quote",
    quote_cold: "Cold Quote",
    parts_deficit: "Parts Hold",
    parts_ready: "Parts Ready",
    dead_lift: "Dead Lift",
    unscheduled_ro: "Unscheduled",
    checkout_ready: "Checkout",
    board_decision: "Board Decision",
    synced_action: "Alert",
  };
  return map[rule] || "Action";
}

function priorityPillClass(priority) {
  if (priority === "Critical") return "cockpit-pill--red";
  if (priority === "High") return "cockpit-pill--amber";
  return "cockpit-pill--sky";
}

function boardStatusPill(status) {
  if (!status) return "";
  const map = {
    ACTIVE: { cls: "cockpit-pill--green", label: "Active" },
    BLOCKED_PARTS: { cls: "cockpit-pill--red", label: "Blocked: Parts" },
    BLOCKED_DECISION: { cls: "cockpit-pill--amber", label: "Blocked: Decision" },
    WAITING_CUSTOMER: { cls: "cockpit-pill--sky", label: "Waiting Customer" },
    CLOSED: { cls: "cockpit-pill--muted", label: "Closed" },
  };
  const meta = map[status] || { cls: "cockpit-pill--muted", label: status };
  return `<span class="cockpit-pill ${meta.cls}">${esc(meta.label)}</span>`;
}

function extractFrappeError(payload, fallback) {
  if (!payload || typeof payload !== "object") return fallback;
  if (payload.exception && typeof payload.exception === "string") {
    const parts = payload.exception.split(":");
    return parts.slice(1).join(":").trim() || payload.exception;
  }
  if (payload._server_messages) {
    try {
      const msgs = JSON.parse(payload._server_messages);
      if (Array.isArray(msgs) && msgs.length) {
        const first = JSON.parse(msgs[0]);
        if (first && first.message) return String(first.message);
      }
    } catch {
      // ignore parse errors
    }
  }
  return String(payload.message || payload.error || fallback);
}

async function apiGet(methodPath, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") {
      qs.set(k, String(v));
    }
  }
  const url = qs.toString()
    ? `/api/method/${methodPath}?${qs.toString()}`
    : `/api/method/${methodPath}`;
  const resp = await fetch(url, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  const json = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(extractFrappeError(json, `Request failed (${resp.status})`));
  }
  return json && json.message !== undefined ? json.message : json;
}

async function apiPost(methodPath, body = {}) {
  const resp = await fetch(`/api/method/${methodPath}`, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-Frappe-CSRF-Token": CSRF_TOKEN,
    },
    body: JSON.stringify(body),
  });
  const json = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(extractFrappeError(json, `Action failed (${resp.status})`));
  }
  return json && json.message !== undefined ? json.message : json;
}

async function fetchSuppressedActionItems() {
  try {
    const fields = JSON.stringify([
      "name",
      "dedup_key",
      "title",
      "status",
      "priority",
      "category",
      "source",
      "snoozed_until",
      "reference_doctype",
      "reference_name",
      "details",
      "resolution_notes",
      "modified",
    ]);
    const url =
      `/api/resource/Shop%20Action%20Item?fields=${encodeURIComponent(fields)}` +
      `&limit_page_length=100&order_by=${encodeURIComponent("modified desc")}`;
    const resp = await fetch(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!resp.ok) return;
    const json = await resp.json().catch(() => ({}));
    const rows = Array.isArray(json.data) ? json.data : [];
    state.suppressedRecords = rows;
  } catch {
    // Graceful fallback: keep sessionSuppressed map
  }
}

function getCategorizedQueueItems() {
  const view = state.view || {};
  const openItems = Array.isArray(view.action_queue) ? view.action_queue : [];
  const refNow = parseServerDate(view.server_now) || new Date();

  const snoozedMap = new Map();
  const dismissedMap = new Map();

  for (const row of state.suppressedRecords) {
    if (!row || typeof row !== "object") continue;
    const key = String(row.dedup_key || "").trim() || `sai:${row.name}`;
    const status = String(row.status || "");
    const snoozeDt = parseServerDate(row.snoozed_until);
    const roRef = row.reference_doctype === "Repair Order" ? row.reference_name : null;
    const normalized = {
      id: String(row.name || key),
      dedup_key: key,
      title: String(row.title || key),
      subtitle: String(row.details || row.resolution_notes || row.category || ""),
      priority: String(row.priority || "Medium"),
      rule: "synced_action",
      reference_doctype: row.reference_doctype || null,
      reference_name: row.reference_name || null,
      thread_key: roRef ? `RO:${roRef}` : null,
      board_url: roRef ? `/board?thread=${encodeURIComponent(`RO:${roRef}`)}` : null,
      snoozed_until: row.snoozed_until || null,
      status,
    };

    if (status === "Dismissed") {
      dismissedMap.set(key, { ...normalized, queue_bucket: "dismissed" });
    } else if (snoozeDt && snoozeDt.getTime() > refNow.getTime()) {
      snoozedMap.set(key, { ...normalized, queue_bucket: "snoozed" });
    }
  }

  for (const [key, item] of state.sessionSuppressed.entries()) {
    if (item.queue_bucket === "dismissed" && !dismissedMap.has(key)) {
      dismissedMap.set(key, item);
    } else if (item.queue_bucket === "snoozed" && !snoozedMap.has(key)) {
      const snoozeDt = parseServerDate(item.snoozed_until);
      if (!snoozeDt || snoozeDt.getTime() > refNow.getTime()) {
        snoozedMap.set(key, item);
      }
    }
  }

  const openKeys = new Set(openItems.map((it) => it.dedup_key));
  for (const key of openKeys) {
    snoozedMap.delete(key);
    dismissedMap.delete(key);
    state.sessionSuppressed.delete(key);
  }

  return {
    open: openItems.map((it) => ({ ...it, queue_bucket: "open" })),
    snoozed: Array.from(snoozedMap.values()),
    dismissed: Array.from(dismissedMap.values()),
  };
}

function renderToolbar(view) {
  const cap = (view && view.capacity) || {};
  const bn = String(cap.bottleneck || "Balanced");
  const bnPill = document.getElementById("bottleneckPill");
  if (bnPill) {
    bnPill.textContent = bn;
    const tone =
      bn === "Balanced"
        ? "cockpit-pill--green"
        : bn === "No Capacity"
          ? "cockpit-pill--red"
          : "cockpit-pill--amber";
    bnPill.className = `cockpit-pill ${tone}`;
  }

  const capMeta = document.getElementById("capacityMeta");
  if (capMeta) {
    const bays = Number(cap.active_bays || 0);
    const techs = Number(cap.on_duty_techs || 0);
    const freeHrs = cap.effective_bookable_hours != null ? ` · ${cap.effective_bookable_hours}h bookable` : "";
    capMeta.textContent = `${bays} Bays · ${techs} Techs${freeHrs}`;
  }

  const tagGroup = document.getElementById("tagSaturationGroup");
  if (tagGroup) {
    const tagSat = cap.tag_saturation && typeof cap.tag_saturation === "object" ? cap.tag_saturation : {};
    const chips = Object.entries(tagSat).map(([tagName, info]) => {
      const pct = Number((info && info.utilization_pct) || 0);
      const cls = pct >= 85 ? "cockpit-pill--amber" : "cockpit-pill--muted";
      return `<span class="cockpit-pill ${cls}">${esc(tagName)} ${pct.toFixed(0)}%</span>`;
    });
    tagGroup.innerHTML = chips.join("");
  }

  const picker = document.getElementById("datePicker");
  if (picker && view && view.date) {
    picker.value = view.date;
  }

  const updatedEl = document.getElementById("updatedAt");
  if (updatedEl) {
    const now = new Date();
    updatedEl.textContent = now.toTimeString().slice(0, 8);
  }
}

function renderGauges(view) {
  const grid = document.getElementById("gaugeGrid");
  if (!grid) return;
  const gauges = Array.isArray(view && view.gauges) ? view.gauges : [];

  if (!gauges.length) {
    grid.innerHTML =
      '<div class="cockpit-empty-state cockpit-empty-state--neutral">No gauge metrics available for this date.</div>';
    return;
  }

  grid.innerHTML = gauges
    .map((g) => {
      const maxVal = Number(g.max_value || 100);
      const rawNum = g.numeric_value == null ? 0 : Number(g.numeric_value);
      const clamped = Number.isFinite(rawNum) ? Math.max(0, Math.min(maxVal, rawNum)) : 0;
      const pct = maxVal > 0 ? Math.round((clamped / maxVal) * 100) : 0;
      const color = ["green", "amber", "red"].includes(g.color) ? g.color : "green";
      const deltaTxt =
        g.delta == null || g.delta === ""
          ? ""
          : `${Number(g.delta) >= 0 ? "+" : ""}${g.delta}`;

      let freshnessHtml = "";
      if (g.is_stale) {
        freshnessHtml = '<span class="cockpit-pill cockpit-pill--amber">Stale</span>';
      } else if (g.period_date) {
        freshnessHtml = `<span class="cockpit-pill cockpit-pill--muted">${esc(g.period_date)}</span>`;
      }

      const link = safeHref(g.link);
      const linkHtml = link
        ? `<a class="cockpit-gauge-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Report ↗</a>`
        : "";

      return (
        `<article class="cockpit-gauge-card">` +
        `<div class="cockpit-gauge-head">` +
        `<span class="cockpit-gauge-title">${esc(g.title || g.key || "Metric")}</span>` +
        `<span class="cockpit-pill cockpit-pill--${esc(color)}">${esc(g.grade || "Healthy")}</span>` +
        `</div>` +
        `<div class="cockpit-gauge-val-row">` +
        `<span class="cockpit-gauge-val">${esc(g.value != null ? g.value : "—")}</span>` +
        `<div style="display:flex;gap:4px;align-items:center;">` +
        (deltaTxt ? `<span class="cockpit-pill">${esc(deltaTxt)}</span>` : "") +
        freshnessHtml +
        `</div>` +
        `</div>` +
        `<div class="cockpit-gauge-bar-track">` +
        `<div class="cockpit-gauge-bar-fill cockpit-fill--${esc(color)}" style="width:${pct}%;"></div>` +
        `</div>` +
        `<div class="cockpit-gauge-foot">` +
        `<span class="cockpit-gauge-driver" title="${esc(g.driver || "")}">${esc(g.driver || "")}</span>` +
        linkHtml +
        `</div>` +
        `</article>`
      );
    })
    .join("");
}

function renderActionCard(it) {
  const bucket = it.queue_bucket || "open";
  const isSuppressed = bucket !== "open";
  const threadKey =
    it.thread_key ||
    (it.reference_doctype === "Repair Order" && it.reference_name
      ? `RO:${it.reference_name}`
      : null);
  const boardHref = buildBoardThreadHref(threadKey, it.board_url || "");
  const deskHref = safeHref(it.deep_link);
  const showDeskLink = deskHref && !deskHref.startsWith("/board");

  const approvals = Number(it.open_approval_count || 0);
  const clarifications = Number(it.open_clarification_count || 0);

  const boardBadges = [
    boardStatusPill(it.board_status),
    approvals > 0
      ? `<span class="cockpit-pill cockpit-pill--amber">${approvals} Approval${approvals > 1 ? "s" : ""}</span>`
      : "",
    clarifications > 0
      ? `<span class="cockpit-pill cockpit-pill--sky">${clarifications} Clarification${clarifications > 1 ? "s" : ""}</span>`
      : "",
  ]
    .filter(Boolean)
    .join("");

  let actionsHtml = "";
  if (isSuppressed) {
    actionsHtml =
      `<button type="button" class="cockpit-btn cockpit-btn--sm cockpit-btn--reopen js-sync-btn" ` +
      `data-action="reopen" data-key="${esc(it.dedup_key)}" data-title="${esc(it.title)}">Reopen</button>`;
  } else {
    const snoozeBtns =
      it.can_snooze !== false
        ? `<button type="button" class="cockpit-btn cockpit-btn--sm cockpit-btn--subtle js-sync-btn" ` +
          `data-action="snooze" data-minutes="60" data-key="${esc(it.dedup_key)}" data-title="${esc(it.title)}">Snooze 1h</button>` +
          `<button type="button" class="cockpit-btn cockpit-btn--sm cockpit-btn--subtle js-sync-btn" ` +
          `data-action="snooze" data-minutes="240" data-key="${esc(it.dedup_key)}" data-title="${esc(it.title)}">4h</button>`
        : "";
    const dismissBtn = it.can_dismiss
      ? `<button type="button" class="cockpit-btn cockpit-btn--sm cockpit-btn--subtle cockpit-btn--danger js-sync-btn" ` +
        `data-action="dismiss" data-key="${esc(it.dedup_key)}" data-title="${esc(it.title)}">Dismiss</button>`
      : "";
    actionsHtml = snoozeBtns + dismissBtn;
  }

  const stateBadge =
    bucket === "snoozed"
      ? `<span class="cockpit-pill cockpit-pill--amber">Snoozed${it.snoozed_until ? ` · ${esc(formatClock(it.snoozed_until))}` : ""}</span>`
      : bucket === "dismissed"
        ? `<span class="cockpit-pill cockpit-pill--muted">Dismissed</span>`
        : `<span class="cockpit-pill ${priorityPillClass(it.priority)}">${esc(it.priority || "Medium")}</span>` +
          `<span class="cockpit-pill">${esc(ruleBadgeLabel(it.rule))}</span>`;

  const cardClass = isSuppressed
    ? "cockpit-action-card cockpit-action-card--suppressed"
    : `cockpit-action-card cockpit-action-card--${esc(it.priority || "Medium")}`;

  return (
    `<article class="${cardClass}">` +
    `<div class="cockpit-card-top">` +
    `<div class="cockpit-card-badges">${stateBadge}</div>` +
    `<span class="cockpit-card-age">${esc(it.age_display || "")}</span>` +
    `</div>` +
    `<div class="cockpit-card-title">${esc(it.title || it.dedup_key || "Action Item")}</div>` +
    (it.subtitle ? `<div class="cockpit-card-sub">${esc(it.subtitle)}</div>` : "") +
    (boardBadges ? `<div class="cockpit-card-board-meta">${boardBadges}</div>` : "") +
    `<div class="cockpit-card-foot">` +
    `<div class="cockpit-card-links">` +
    (boardHref
      ? `<a class="cockpit-thread-link" href="${esc(boardHref)}">Open Thread &rarr;</a>`
      : "") +
    (showDeskLink
      ? `<a class="cockpit-desk-link" href="${esc(deskHref)}" target="_blank" rel="noopener noreferrer">Desk ↗</a>`
      : "") +
    `</div>` +
    `<div class="cockpit-card-actions">${actionsHtml}</div>` +
    `</div>` +
    `</article>`
  );
}

function renderActionQueue(view) {
  const listEl = document.getElementById("actionQueueList");
  const countPill = document.getElementById("queueCountPill");
  const suppPill = document.getElementById("suppressedPill");
  const prioRow = document.getElementById("priorityFilters");
  if (!listEl) return;

  const counts = (view && view.action_counts) || {};
  const buckets = getCategorizedQueueItems();

  const allCount = buckets.open.length + buckets.snoozed.length + buckets.dismissed.length;
  const tabCounts = {
    open: buckets.open.length,
    snoozed: buckets.snoozed.length,
    dismissed: buckets.dismissed.length,
    all: allCount,
  };
  document.querySelectorAll("[data-count-for]").forEach((el) => {
    const key = el.getAttribute("data-count-for");
    if (key && key in tabCounts) {
      el.textContent = String(tabCounts[key]);
    }
  });

  if (countPill) {
    const openTotal = Number(counts.total ?? buckets.open.length);
    countPill.textContent = String(openTotal);
    countPill.className =
      "cockpit-pill " +
      ((counts.critical || 0) > 0
        ? "cockpit-pill--red"
        : openTotal > 0
          ? "cockpit-pill--amber"
          : "cockpit-pill--green");
  }

  if (suppPill) {
    const suppressedTotal = Math.max(
      Number(counts.suppressed || 0),
      buckets.snoozed.length + buckets.dismissed.length
    );
    if (suppressedTotal > 0) {
      suppPill.style.display = "inline-flex";
      suppPill.textContent = `${suppressedTotal} snoozed/dismissed`;
    } else {
      suppPill.style.display = "none";
    }
  }

  if (prioRow) {
    prioRow.style.display =
      state.statusTab === "open" || state.statusTab === "all" ? "flex" : "none";
  }

  let items = [];
  if (state.statusTab === "open") {
    items = buckets.open;
  } else if (state.statusTab === "snoozed") {
    items = buckets.snoozed;
  } else if (state.statusTab === "dismissed") {
    items = buckets.dismissed;
  } else {
    items = [...buckets.open, ...buckets.snoozed, ...buckets.dismissed];
  }

  if (
    (state.statusTab === "open" || state.statusTab === "all") &&
    state.priorityFilter !== "ALL"
  ) {
    items = items.filter((it) => it.priority === state.priorityFilter);
  }

  if (!items.length) {
    const isAllClear = state.statusTab === "open" && state.priorityFilter === "ALL";
    listEl.innerHTML =
      `<div class="cockpit-empty-state ${isAllClear ? "" : "cockpit-empty-state--neutral"}">` +
      `<div style="font-weight:800;font-size:0.9rem;margin-bottom:4px;">${
        isAllClear ? "All Clear" : "No Matching Items"
      }</div>` +
      `<div style="font-size:0.76rem;">${
        isAllClear
          ? "Zero open manager exceptions right now."
          : `No ${esc(state.statusTab)} items match this filter.`
      }</div>` +
      `</div>`;
    return;
  }

  listEl.innerHTML = items.map(renderActionCard).join("");
}

function detectLaneCollisions(blocks) {
  const active = (blocks || [])
    .map((b, idx) => ({
      idx,
      start: Number(b.left_pct || 0),
      end: Number(b.left_pct || 0) + Number(b.width_pct || 0),
      state: String(b.state || ""),
    }))
    .filter((b) => b.state !== "completed");

  const collidingIndices = new Set();
  const rowAssignment = new Map();

  for (let i = 0; i < active.length; i += 1) {
    for (let j = i + 1; j < active.length; j += 1) {
      const a = active[i];
      const b = active[j];
      if (a.start < b.end - 0.15 && b.start < a.end - 0.15) {
        collidingIndices.add(a.idx);
        collidingIndices.add(b.idx);
        if (!rowAssignment.has(a.idx)) rowAssignment.set(a.idx, 0);
        if (!rowAssignment.has(b.idx)) rowAssignment.set(b.idx, 1);
      }
    }
  }

  return { collidingIndices, rowAssignment };
}

function renderLaneBlocksHtml(blocks, swim) {
  const safeBlocks = Array.isArray(blocks) ? blocks : [];
  const { collidingIndices, rowAssignment } = detectLaneCollisions(safeBlocks);

  const br = swim.break_band
    ? `<div class="cockpit-break-band" title="${esc(swim.break_band.label || "Break")}" ` +
      `style="left:${Number(swim.break_band.left_pct || 0)}%;width:${Number(swim.break_band.width_pct || 0)}%;"></div>`
    : "";

  const nowLn =
    swim.now_marker_pct != null
      ? `<div class="cockpit-now-line" style="left:${Number(swim.now_marker_pct)}%;" title="Current time"></div>`
      : "";

  if (!safeBlocks.length) {
    return `${br}${nowLn}<div class="cockpit-lane-empty-hint">Idle / Open capacity</div>`;
  }

  const itemsHtml = safeBlocks
    .map((b, idx) => {
      const threadKey = b.thread_key || (b.repair_order ? `RO:${b.repair_order}` : null);
      const href = buildBoardThreadHref(threadKey, b.board_url || b.deep_link || "");
      const tag = href ? "a" : "div";
      const attrs = href ? ` href="${esc(href)}"` : "";
      const isCollision = collidingIndices.has(idx);
      const rowSlot = rowAssignment.get(idx);
      const blockClasses = [
        "cockpit-job-block",
        `cockpit-job-block--${esc(b.state || "scheduled")}`,
        isCollision ? "cockpit-job-block--collision" : "",
        rowSlot === 0 ? "cockpit-job-block--row-0" : "",
        rowSlot === 1 ? "cockpit-job-block--row-1" : "",
      ]
        .filter(Boolean)
        .join(" ");

      const pendingCount =
        Number(b.open_approval_count || 0) + Number(b.open_clarification_count || 0);
      const badgeHtml = isCollision
        ? `<span class="cockpit-job-badge">Overlap</span>`
        : pendingCount > 0
          ? `<span class="cockpit-job-badge">${pendingCount} Pending</span>`
          : "";

      const tooltip = `${b.label || ""} — ${b.sublabel || ""} [${b.status || ""}]${
        b.start_clock ? ` · Internal T: ${b.start_clock}–${b.end_clock || ""}` : ""
      }`;

      return (
        `<${tag}${attrs} class="${blockClasses}" ` +
        `style="left:${Number(b.left_pct || 0)}%;width:${Number(b.width_pct || 4)}%;" ` +
        `title="${esc(tooltip)}">` +
        `<div class="cockpit-job-title-row">` +
        `<span class="cockpit-job-title">${esc(b.label || b.name || "Job")}</span>` +
        badgeHtml +
        `</div>` +
        `<div class="cockpit-job-sub">${esc(b.sublabel || "")}</div>` +
        `</${tag}>`
      );
    })
    .join("");

  return `${br}${nowLn}${itemsHtml}`;
}

function renderSwimlane(view) {
  const swim = (view && view.swimlane) || {};
  const body = document.getElementById("swimBody");
  const dateLabel = document.getElementById("swimDateLabel");
  const collisionBadge = document.getElementById("collisionBadge");
  if (!body) return;

  if (dateLabel) {
    dateLabel.textContent = swim.date || (view && view.date) || "Today";
  }

  const hours = Array.isArray(swim.hours) ? swim.hours : [];
  const hoursHtml = hours
    .map(
      (h) =>
        `<span class="cockpit-hour-tick" style="left:${Number(h.left_pct || 0)}%;">${esc(h.label)}</span>`
    )
    .join("");

  let totalCollisions = 0;
  let rowsHtml =
    `<div class="cockpit-timeline-grid">` +
    `<div class="cockpit-ruler-label">Resource</div>` +
    `<div class="cockpit-ruler-track">${hoursHtml}</div>`;

  rowsHtml += `<div class="cockpit-lane-section-label">Service Bays</div>`;
  const bayLanes = Array.isArray(swim.bay_lanes) ? swim.bay_lanes : [];
  if (!bayLanes.length) {
    rowsHtml +=
      `<div style="grid-column:1/-1;color:var(--shop-text-muted,#94a3b8);padding:8px 0;">No active service bays configured.</div>`;
  } else {
    rowsHtml += bayLanes
      .map((lane) => {
        const blocks = Array.isArray(lane.blocks) ? lane.blocks : [];
        const { collidingIndices } = detectLaneCollisions(blocks);
        if (collidingIndices.size > 0) totalCollisions += 1;

        const tagsHtml = (Array.isArray(lane.tags) ? lane.tags : [])
          .map((t) => `<span class="cockpit-tag-chip">${esc(t)}</span>`)
          .join("");
        const idleChip =
          blocks.length === 0
            ? `<span class="cockpit-tag-chip cockpit-tag-chip--idle">Idle Lift</span>`
            : "";

        return (
          `<div class="cockpit-lane-meta">` +
          `<div class="cockpit-lane-name">` +
          `<span>${esc(lane.label || lane.id)}</span>` +
          `<span class="cockpit-lane-util">${esc(lane.utilization_pct ?? 0)}%</span>` +
          `</div>` +
          (tagsHtml || idleChip
            ? `<div class="cockpit-lane-tags">${tagsHtml}${idleChip}</div>`
            : "") +
          `</div>` +
          `<div class="cockpit-lane-track ${collidingIndices.size ? "cockpit-lane-track--collision" : ""}">` +
          renderLaneBlocksHtml(blocks, swim) +
          `</div>`
        );
      })
      .join("");
  }

  rowsHtml += `<div class="cockpit-lane-section-label">Technicians</div>`;
  const techLanes = Array.isArray(swim.tech_lanes) ? swim.tech_lanes : [];
  if (!techLanes.length) {
    rowsHtml +=
      `<div style="grid-column:1/-1;color:var(--shop-text-muted,#94a3b8);padding:8px 0;">No active technicians found.</div>`;
  } else {
    rowsHtml += techLanes
      .map((lane) => {
        const blocks = Array.isArray(lane.blocks) ? lane.blocks : [];
        const { collidingIndices } = detectLaneCollisions(blocks);
        if (collidingIndices.size > 0) totalCollisions += 1;

        const badge = lane.on_leave
          ? `<span class="cockpit-pill cockpit-pill--amber" style="padding:1px 6px;font-size:0.64rem;">Leave</span>`
          : `<span class="cockpit-lane-util">${esc(lane.utilization_pct ?? 0)}%</span>`;

        return (
          `<div class="cockpit-lane-meta">` +
          `<div class="cockpit-lane-name">` +
          `<span>${esc(lane.label || lane.id)}</span>` +
          badge +
          `</div>` +
          `</div>` +
          `<div class="cockpit-lane-track ${collidingIndices.size ? "cockpit-lane-track--collision" : ""}">` +
          renderLaneBlocksHtml(blocks, swim) +
          `</div>`
        );
      })
      .join("");
  }

  rowsHtml += `</div>`;
  body.innerHTML = rowsHtml;

  if (collisionBadge) {
    if (totalCollisions > 0) {
      collisionBadge.style.display = "inline-flex";
      collisionBadge.textContent = `${totalCollisions} Lane Overlap${totalCollisions > 1 ? "s" : ""}`;
    } else {
      collisionBadge.style.display = "none";
    }
  }

  // Unslotted In-Bay ROs strip
  const unslottedBar = document.getElementById("unslottedBar");
  const unslotted = Array.isArray(swim.unslotted_bay_ros) ? swim.unslotted_bay_ros : [];
  if (unslottedBar) {
    if (unslotted.length) {
      unslottedBar.style.display = "flex";
      unslottedBar.innerHTML =
        `<span class="cockpit-strip__label">In Bay Without Schedule Slot:</span>` +
        unslotted
          .map((r) => {
            const href = buildBoardThreadHref(
              r.thread_key || (r.name ? `RO:${r.name}` : null),
              r.board_url || r.desk_url || ""
            );
            const cls = r.is_dead_lift ? "cockpit-pill--red" : "cockpit-pill--amber";
            const label = `${r.name || "RO"} (${r.status || ""})`;
            return href
              ? `<a class="cockpit-pill ${cls}" href="${esc(href)}">${esc(label)} &rarr;</a>`
              : `<span class="cockpit-pill ${cls}">${esc(label)}</span>`;
          })
          .join("");
    } else {
      unslottedBar.style.display = "none";
      unslottedBar.innerHTML = "";
    }
  }

  // Unscheduled Approved Jobs strip
  const unscheduledBar = document.getElementById("unscheduledBar");
  if (unscheduledBar) {
    const queueItems = Array.isArray(view && view.action_queue) ? view.action_queue : [];
    const fromQueue = queueItems.filter((it) => it && it.rule === "unscheduled_ro");
    const roRows = Array.isArray(view && view.ro_rows) ? view.ro_rows : [];
    const seenRo = new Set(fromQueue.map((it) => it.reference_name).filter(Boolean));
    const extraRos = roRows.filter(
      (r) =>
        r &&
        r.name &&
        !seenRo.has(r.name) &&
        Number(r.unscheduled_approved_services || 0) > 0 &&
        !r.has_active_or_future_booking
    );

    const pills = [
      ...fromQueue.map((it) => {
        const roId = it.reference_name || it.id;
        const href = buildBoardThreadHref(it.thread_key || (roId ? `RO:${roId}` : null), it.board_url);
        return `<a class="cockpit-pill cockpit-pill--sky" href="${esc(href)}">${esc(roId)} &rarr;</a>`;
      }),
      ...extraRos.map((r) => {
        const href = buildBoardThreadHref(r.thread_key || `RO:${r.name}`, r.board_url);
        return `<a class="cockpit-pill cockpit-pill--sky" href="${esc(href)}">${esc(r.name)} (${Number(r.unscheduled_approved_services)} svc) &rarr;</a>`;
      }),
    ];

    if (pills.length) {
      unscheduledBar.style.display = "flex";
      unscheduledBar.innerHTML =
        `<span class="cockpit-strip__label">Unscheduled Approved Jobs:</span>` + pills.join("");
    } else {
      unscheduledBar.style.display = "none";
      unscheduledBar.innerHTML = "";
    }
  }
}

function renderAll(view) {
  if (!view || typeof view !== "object") return;
  state.view = view;
  renderToolbar(view);
  renderActionQueue(view);
  renderGauges(view);
  renderSwimlane(view);
}

async function loadDashboard({ silent = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  const refreshBtn = document.getElementById("refreshBtn");
  if (refreshBtn && !silent) refreshBtn.disabled = true;

  try {
    const [dashboard] = await Promise.all([
      apiGet("ism.api.board.get_cockpit_dashboard", {
        date: state.selectedDate || undefined,
      }),
      fetchSuppressedActionItems(),
    ]);

    if (!dashboard || dashboard.ok === false) {
      throw new Error(
        (dashboard && (dashboard.error || dashboard.message)) || "Cockpit dashboard load failed"
      );
    }

    showError("");
    state.lastFetchedAt = Date.now();
    renderAll(dashboard);
  } catch (err) {
    showError(`Cockpit refresh error: ${err && err.message ? err.message : String(err)}`);
  } finally {
    state.loading = false;
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

function scheduleDebouncedRefresh(delayMs = 550) {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    if (document.visibilityState === "visible") {
      loadDashboard({ silent: true });
    }
  }, delayMs);
}

async function handleSyncAction(btn) {
  const action = btn.getAttribute("data-action");
  const dedupKey = btn.getAttribute("data-key");
  const title = btn.getAttribute("data-title") || dedupKey;
  const minutesRaw = btn.getAttribute("data-minutes");
  const snoozeMinutes = minutesRaw ? Number(minutesRaw) : action === "snooze" ? 60 : undefined;

  if (!action || !dedupKey) return;
  btn.disabled = true;

  try {
    const res = await apiPost("ism.api.board.sync_cockpit_action", {
      dedup_key: dedupKey,
      action,
      snooze_minutes: snoozeMinutes,
    });
    if (res && res.ok === false) {
      throw new Error(res.error || res.message || "Action sync failed");
    }

    const resData = (res && res.data) || {};
    if (action === "snooze") {
      state.sessionSuppressed.set(dedupKey, {
        dedup_key: dedupKey,
        title,
        queue_bucket: "snoozed",
        snoozed_until: resData.snoozed_until || null,
        reference_doctype: resData.reference_doctype || null,
        reference_name: resData.reference_name || null,
      });
      showToast(`Snoozed "${title}" for ${snoozeMinutes || 60}m`);
    } else if (action === "dismiss") {
      state.sessionSuppressed.set(dedupKey, {
        dedup_key: dedupKey,
        title,
        queue_bucket: "dismissed",
        reference_doctype: resData.reference_doctype || null,
        reference_name: resData.reference_name || null,
      });
      showToast(`Dismissed "${title}"`);
    } else if (action === "reopen") {
      state.sessionSuppressed.delete(dedupKey);
      showToast(`Reopened "${title}"`);
    }

    await loadDashboard({ silent: true });
  } catch (err) {
    showError(`Action sync error: ${err && err.message ? err.message : String(err)}`);
    btn.disabled = false;
  }
}

function syncDateToUrl(dateStr) {
  try {
    const url = new URL(window.location.href);
    if (dateStr) {
      url.searchParams.set("date", dateStr);
    } else {
      url.searchParams.delete("date");
    }
    window.history.replaceState({}, "", url.toString());
  } catch {
    // ignore history errors
  }
}

function initRealtime() {
  const events = [
    "ism_board_thread_summary",
    "ism_board_projection",
    "ism_board_event",
    "ism_board_ai_status",
    "list_update",
  ];

  // 1. Reuse Frappe's realtime wrapper if present
  if (window.frappe && window.frappe.realtime && typeof window.frappe.realtime.on === "function") {
    events.forEach((evt) => {
      window.frappe.realtime.on(evt, () => scheduleDebouncedRefresh(500));
    });
    setRealtimeStatus("socketio", "Live · Socket.IO");
    return;
  }

  // 2. Direct Socket.IO connection if window.io is available
  if (typeof window.io === "function") {
    try {
      const rt = BOOT.realtime || {};
      const siteName = rt.site_name || BOOT.site_name || "";
      const port = rt.socketio_port || BOOT.socketio_port;
      const isLocal = ["localhost", "127.0.0.1"].includes(window.location.hostname) ||
        window.location.hostname.endsWith(".localhost");
      const origin =
        isLocal && port
          ? `${window.location.protocol}//${window.location.hostname}:${port}`
          : window.location.origin;
      const namespace = siteName ? `/${siteName}` : "/";
      const socket = window.io(`${origin}${namespace}`, {
        withCredentials: true,
        reconnectionAttempts: 5,
      });

      socket.on("connect", () => setRealtimeStatus("socketio", "Live · Socket.IO"));
      socket.on("disconnect", () => setRealtimeStatus("fallback", "Live · Tab Sync"));
      socket.on("connect_error", () => setRealtimeStatus("fallback", "Live · Tab Sync"));
      events.forEach((evt) => {
        socket.on(evt, () => scheduleDebouncedRefresh(500));
      });
      return;
    } catch {
      // fall through cleanly
    }
  }

  // 3. Graceful fallback: visibilitychange + gentle 60s refresh only when tab is visible
  setRealtimeStatus("fallback", "Live · Tab Sync");
  fallbackTimer = setInterval(() => {
    if (document.visibilityState === "visible" && Date.now() - state.lastFetchedAt >= 55000) {
      loadDashboard({ silent: true });
    }
  }, 60000);
}

function bindEvents() {
  const datePicker = document.getElementById("datePicker");
  const prevDayBtn = document.getElementById("prevDayBtn");
  const nextDayBtn = document.getElementById("nextDayBtn");
  const todayBtn = document.getElementById("todayBtn");
  const refreshBtn = document.getElementById("refreshBtn");
  const statusTabs = document.getElementById("queueStatusTabs");
  const priorityFilters = document.getElementById("priorityFilters");
  const actionQueueList = document.getElementById("actionQueueList");

  if (datePicker) {
    datePicker.addEventListener("change", (ev) => {
      state.selectedDate = ev.target.value || "";
      syncDateToUrl(state.selectedDate);
      loadDashboard();
    });
  }

  if (prevDayBtn) {
    prevDayBtn.addEventListener("click", () => {
      const current = state.selectedDate || (state.view && state.view.date) || getLocalIsoDate();
      state.selectedDate = shiftIsoDate(current, -1);
      syncDateToUrl(state.selectedDate);
      loadDashboard();
    });
  }

  if (nextDayBtn) {
    nextDayBtn.addEventListener("click", () => {
      const current = state.selectedDate || (state.view && state.view.date) || getLocalIsoDate();
      state.selectedDate = shiftIsoDate(current, 1);
      syncDateToUrl(state.selectedDate);
      loadDashboard();
    });
  }

  if (todayBtn) {
    todayBtn.addEventListener("click", () => {
      state.selectedDate = "";
      syncDateToUrl("");
      loadDashboard();
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => loadDashboard());
  }

  if (statusTabs) {
    statusTabs.addEventListener("click", (ev) => {
      const tab = ev.target.closest("button[data-status]");
      if (!tab) return;
      state.statusTab = tab.getAttribute("data-status") || "open";
      statusTabs.querySelectorAll(".cockpit-tab").forEach((el) => {
        const active = el === tab;
        el.classList.toggle("is-active", active);
        el.setAttribute("aria-selected", active ? "true" : "false");
      });
      if (state.view) renderActionQueue(state.view);
    });
  }

  if (priorityFilters) {
    priorityFilters.addEventListener("click", (ev) => {
      const chip = ev.target.closest("button[data-prio]");
      if (!chip) return;
      state.priorityFilter = chip.getAttribute("data-prio") || "ALL";
      priorityFilters.querySelectorAll(".cockpit-chip").forEach((el) => {
        el.classList.toggle("is-active", el === chip);
      });
      if (state.view) renderActionQueue(state.view);
    });
  }

  if (actionQueueList) {
    actionQueueList.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button.js-sync-btn");
      if (btn) handleSyncAction(btn);
    });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Date.now() - state.lastFetchedAt > 10000) {
      scheduleDebouncedRefresh(250);
    }
  });

  window.addEventListener("online", () => scheduleDebouncedRefresh(300));

  document.addEventListener("keydown", (ev) => {
    const tag = document.activeElement && document.activeElement.tagName;
    if (
      ev.key === "r" &&
      !ev.metaKey &&
      !ev.ctrlKey &&
      !ev.altKey &&
      tag !== "INPUT" &&
      tag !== "TEXTAREA" &&
      tag !== "SELECT"
    ) {
      loadDashboard();
    }
  });

  window.addEventListener("beforeunload", () => {
    if (fallbackTimer) clearInterval(fallbackTimer);
    if (debounceTimer) clearTimeout(debounceTimer);
  });
}

function init() {
  bindEvents();
  if (state.view) {
    renderAll(state.view);
    fetchSuppressedActionItems().then(() => {
      if (state.view) renderActionQueue(state.view);
    });
    state.lastFetchedAt = Date.now();
  } else {
    loadDashboard();
  }
  initRealtime();
}

init();
