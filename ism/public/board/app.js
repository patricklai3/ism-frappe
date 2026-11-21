/**
 * Main controller for the ISM Shop OS Blackboard (/board).
 * - Left rail: Shop Channels (#Intake, #Receiving-AP, #Dispatch) + Active ROs with filters
 * - Pinned Andon Header: pulsing status + 1s ticking blocked_since timer
 * - Seam 6: conditional masked tap-to-reveal PIN-to-Drive pill (15s auto-mask, zero persistence)
 * - Realtime Socket.IO + visibilitychange / reconnect delta sync
 */

import {
	getBoardThread,
	getThreadDelta,
	listBoardThreads,
	resolveBoardCard,
	revealRoPin,
	setOfflineHandler,
	undoBoardReceipt,
} from "./api.js";
import { BoardCaptureController } from "./capture.js";
import { escapeHtml, renderEventCard } from "./cards.js";
import { BoardRealtimeClient } from "./realtime.js";

const SINGLETON_KEYS = ["SHOP:INTAKE", "SHOP:AP_RECEIVING", "SHOP:DISPATCH"];
const CHANNEL_DISPLAY_NAMES = {
	"SHOP:INTAKE": "#Intake",
	"SHOP:AP_RECEIVING": "#Receiving-AP",
	"SHOP:DISPATCH": "#Dispatch",
};
const PIN_AUTO_MASK_SECONDS = 15;

const boot = window.ISM_BOARD_BOOT || {};

const state = {
	threads: Array.isArray(boot.initial_threads) ? [...boot.initial_threads] : [],
	activeThreadKey: null,
	activeThread: null,
	events: [],
	hasMore: false,
	nextBeforeEvent: null,
	pinState: null,
	roSummary: null,
	filter: "ALL",
	searchQuery: "",
	isLoadingThread: false,
	isSyncingDelta: false,
	// Transient in-memory PIN reveal state (NEVER persisted to storage or DOM datasets)
	pinRevealed: false,
	pinSecondsLeft: 0,
	pinTimerId: null,
};

const el = {
	railToggleBtn: document.getElementById("rail-toggle-btn"),
	rail: document.getElementById("board-rail"),
	railBackdrop: document.getElementById("rail-backdrop"),
	searchInput: document.getElementById("thread-search-input"),
	filterRow: document.getElementById("thread-filter-row"),
	shopChannelsList: document.getElementById("shop-channels-list"),
	roThreadsList: document.getElementById("ro-threads-list"),
	activeRoCount: document.getElementById("active-ro-count"),
	andonStatusBadge: document.getElementById("andon-status-badge"),
	andonBlockedTimer: document.getElementById("andon-blocked-timer"),
	andonTitle: document.getElementById("andon-title"),
	andonMeta: document.getElementById("andon-meta"),
	andonBlockedReason: document.getElementById("andon-blocked-reason"),
	andonDeskLink: document.getElementById("andon-desk-link"),
	pinPillSlot: document.getElementById("pin-pill-slot"),
	refreshThreadBtn: document.getElementById("refresh-thread-btn"),
	timelineScroll: document.getElementById("timeline-scroll"),
	loadMoreWrap: document.getElementById("load-more-wrap"),
	loadMoreBtn: document.getElementById("load-more-btn"),
	timelineList: document.getElementById("timeline-list"),
	aiStatusBar: document.getElementById("ai-status-bar"),
	offlineBanner: document.getElementById("offline-banner"),
	offlineBannerText: document.getElementById("offline-banner-text"),
	offlineRetryBtn: document.getElementById("offline-retry-btn"),
	lightbox: document.getElementById("photo-lightbox"),
	lightboxImg: document.getElementById("lightbox-img"),
	lightboxClose: document.getElementById("lightbox-close"),
	toastContainer: document.getElementById("toast-container"),
};

let realtimeClient = null;
let captureController = null;
let blockedTimerInterval = null;

function showToast(message, tone = "info") {
	if (!el.toastContainer || !message) return;
	const toast = document.createElement("div");
	toast.className = `toast toast--${tone}`;
	toast.textContent = message;
	el.toastContainer.appendChild(toast);
	setTimeout(() => {
		toast.remove();
	}, 4200);
}

function toggleMobileRail(open) {
	if (!el.rail || !el.railBackdrop) return;
	const shouldOpen = open !== undefined ? Boolean(open) : !el.rail.classList.contains("is-open");
	el.rail.classList.toggle("is-open", shouldOpen);
	el.railBackdrop.classList.toggle("is-hidden", !shouldOpen);
	if (el.railToggleBtn) {
		el.railToggleBtn.setAttribute("aria-expanded", String(shouldOpen));
	}
}

function openLightbox(src) {
	if (!src || !el.lightbox || !el.lightboxImg) return;
	el.lightboxImg.src = src;
	el.lightbox.classList.remove("is-hidden");
}

function closeLightbox() {
	if (!el.lightbox || !el.lightboxImg) return;
	el.lightbox.classList.add("is-hidden");
	el.lightboxImg.src = "";
}

function formatElapsed(isoOrSqlTs) {
	if (!isoOrSqlTs) return "";
	const normalized = String(isoOrSqlTs).includes("T")
		? String(isoOrSqlTs)
		: String(isoOrSqlTs).replace(" ", "T");
	const start = new Date(normalized).getTime();
	if (Number.isNaN(start)) return "";
	const diffSec = Math.max(0, Math.floor((Date.now() - start) / 1000));
	const hrs = Math.floor(diffSec / 3600);
	const mins = Math.floor((diffSec % 3600) / 60);
	const secs = diffSec % 60;
	if (hrs > 0) {
		return `${hrs}h ${String(mins).padStart(2, "0")}m ${String(secs).padStart(2, "0")}s`;
	}
	return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function updateBlockedTimerDisplay() {
	if (!el.andonBlockedTimer) return;
	const t = state.activeThread;
	if (!t || !t.blocked_since || t.board_status === "ACTIVE" || t.board_status === "CLOSED") {
		el.andonBlockedTimer.classList.add("is-hidden");
		el.andonBlockedTimer.textContent = "";
		return;
	}
	const elapsed = formatElapsed(t.blocked_since);
	if (!elapsed) {
		el.andonBlockedTimer.classList.add("is-hidden");
		return;
	}
	el.andonBlockedTimer.textContent = `Blocked ${elapsed}`;
	el.andonBlockedTimer.classList.remove("is-hidden");
}

/* =========================================================================
   Seam 6: Conditional Masked Tap-to-Reveal PIN-to-Drive Pill
   ========================================================================= */

function clearPinRevealState() {
	if (state.pinTimerId) {
		clearInterval(state.pinTimerId);
		state.pinTimerId = null;
	}
	state.pinRevealed = false;
	state.pinSecondsLeft = 0;
}

function renderPinPill(revealedPlaintext = null) {
	if (!el.pinPillSlot) return;
	el.pinPillSlot.innerHTML = "";

	const thread = state.activeThread;
	const ps = state.pinState;
	const shouldShow =
		Boolean(thread && thread.repair_order && thread.board_status !== "CLOSED") &&
		Boolean(ps && ps.pin_feature_installed && ps.has_pin && ps.can_reveal);

	if (!shouldShow) {
		clearPinRevealState();
		return;
	}

	const pill = document.createElement("span");
	pill.className = "pin-pill";

	const label = document.createElement("span");
	label.textContent = "PIN:";

	const codeEl = document.createElement("span");
	codeEl.className = "pin-pill__code";
	// Plaintext PIN is ONLY held transiently in this text node — never stored in storage or dataset
	codeEl.textContent = state.pinRevealed && revealedPlaintext ? String(revealedPlaintext) : "••••";

	pill.appendChild(label);
	pill.appendChild(codeEl);

	if (state.pinRevealed && revealedPlaintext) {
		const countdown = document.createElement("span");
		countdown.className = "pin-pill__countdown";
		countdown.textContent = `${state.pinSecondsLeft}s`;
		pill.appendChild(countdown);

		const hideBtn = document.createElement("button");
		hideBtn.type = "button";
		hideBtn.className = "pin-pill__btn";
		hideBtn.textContent = "Hide";
		hideBtn.addEventListener("click", () => {
			clearPinRevealState();
			renderPinPill(null);
		});
		pill.appendChild(hideBtn);
	} else {
		const revealBtn = document.createElement("button");
		revealBtn.type = "button";
		revealBtn.className = "pin-pill__btn";
		revealBtn.textContent = "Reveal";
		revealBtn.addEventListener("click", async () => {
			revealBtn.disabled = true;
			try {
				const res = await revealRoPin(thread.repair_order);
				const pinVal = res && (res.pin || res.plaintext_pin || res.pin_code);
				if (!res || !res.available || !pinVal) {
					showToast("PIN-to-Drive is not available for this Repair Order.", "warn");
					renderPinPill(null);
					return;
				}
				clearPinRevealState();
				state.pinRevealed = true;
				state.pinSecondsLeft = PIN_AUTO_MASK_SECONDS;
				renderPinPill(pinVal);

				state.pinTimerId = setInterval(() => {
					state.pinSecondsLeft -= 1;
					const cdEl = el.pinPillSlot.querySelector(".pin-pill__countdown");
					if (state.pinSecondsLeft <= 0) {
						clearPinRevealState();
						renderPinPill(null);
					} else if (cdEl) {
						cdEl.textContent = `${state.pinSecondsLeft}s`;
					}
				}, 1000);
			} catch (err) {
				showToast(err.message || "Unable to reveal PIN-to-Drive.", "error");
				revealBtn.disabled = false;
			}
		});
		pill.appendChild(revealBtn);
	}

	el.pinPillSlot.appendChild(pill);
}

/* =========================================================================
   Left Rail Rendering & Filtering
   ========================================================================= */

function upsertThreadSummary(summary) {
	if (!summary || !summary.thread_key) return;
	const idx = state.threads.findIndex((t) => t.thread_key === summary.thread_key);
	if (idx >= 0) {
		state.threads[idx] = { ...state.threads[idx], ...summary };
	} else {
		state.threads.unshift(summary);
	}
	renderThreadRail();
}

function threadMatchesFilter(thread) {
	if (state.searchQuery) {
		const q = state.searchQuery.toLowerCase();
		const hay = [
			thread.thread_key,
			thread.title,
			thread.repair_order,
			thread.vin,
			thread.customer,
			thread.bay,
			thread.last_event_preview,
		]
			.filter(Boolean)
			.join(" ")
			.toLowerCase();
		if (!hay.includes(q)) return false;
	}

	if (thread.is_singleton || SINGLETON_KEYS.includes(thread.thread_key)) {
		return true;
	}

	if (state.filter === "MY_BAYS") {
		if (!boot.tech_id) return false;
		return thread.assigned_technician === boot.tech_id;
	}
	if (state.filter === "NEEDS_APPROVAL") {
		return Number(thread.open_approval_count || 0) > 0 || Number(thread.open_action_count || 0) > 0;
	}
	if (state.filter === "BLOCKED") {
		return ["BLOCKED_PARTS", "BLOCKED_DECISION", "WAITING_CUSTOMER"].includes(thread.board_status);
	}
	return true;
}

function buildThreadItemButton(thread) {
	const btn = document.createElement("button");
	btn.type = "button";
	btn.className = `thread-item ${thread.thread_key === state.activeThreadKey ? "is-selected" : ""}`;
	btn.dataset.threadKey = thread.thread_key;

	const isChannel = SINGLETON_KEYS.includes(thread.thread_key);
	const displayTitle = isChannel
		? CHANNEL_DISPLAY_NAMES[thread.thread_key] || thread.title
		: thread.title || thread.thread_key;

	const openCount = Number(thread.open_action_count || 0);
	const subLine = thread.last_event_preview || [thread.bay, thread.customer, thread.vin].filter(Boolean).join(" · ") || "No updates yet";

	btn.innerHTML = `
		<div class="thread-item__top">
			<span class="thread-item__title">${escapeHtml(displayTitle)}</span>
			<span class="thread-item__badges">
				${openCount > 0 ? `<span class="count-pill">${openCount}</span>` : ""}
				<span class="status-dot status-dot--${escapeHtml(thread.board_status || "ACTIVE")}" title="${escapeHtml(thread.board_status || "ACTIVE")}"></span>
			</span>
		</div>
		<div class="thread-item__sub">${escapeHtml(subLine)}</div>
	`;

	btn.addEventListener("click", () => {
		selectThread(thread.thread_key, { pushHistory: true });
		toggleMobileRail(false);
	});
	return btn;
}

function renderThreadRail() {
	if (!el.shopChannelsList || !el.roThreadsList) return;
	el.shopChannelsList.innerHTML = "";
	el.roThreadsList.innerHTML = "";

	const channels = [];
	const ros = [];

	for (const thread of state.threads) {
		if (!threadMatchesFilter(thread)) continue;
		if (SINGLETON_KEYS.includes(thread.thread_key) || thread.is_singleton) {
			channels.push(thread);
		} else {
			ros.push(thread);
		}
	}

	channels.sort(
		(a, b) => SINGLETON_KEYS.indexOf(a.thread_key) - SINGLETON_KEYS.indexOf(b.thread_key)
	);

	for (const ch of channels) {
		el.shopChannelsList.appendChild(buildThreadItemButton(ch));
	}

	if (el.activeRoCount) el.activeRoCount.textContent = String(ros.length);

	if (!ros.length) {
		const empty = document.createElement("div");
		empty.className = "thread-item__sub";
		empty.style.padding = "8px 4px";
		empty.textContent = "No matching Repair Order threads.";
		el.roThreadsList.appendChild(empty);
	} else {
		for (const roThread of ros) {
			el.roThreadsList.appendChild(buildThreadItemButton(roThread));
		}
	}
}

/* =========================================================================
   Andon Header & Timeline Rendering
   ========================================================================= */

function renderAndonHeader() {
	const t = state.activeThread;
	if (!t) {
		el.andonTitle.textContent = "Select a Thread";
		el.andonMeta.innerHTML = "";
		el.andonBlockedReason.classList.add("is-hidden");
		el.andonDeskLink.classList.add("is-hidden");
		renderPinPill(null);
		updateBlockedTimerDisplay();
		return;
	}

	const status = t.board_status || "ACTIVE";
	el.andonStatusBadge.className = `andon-badge andon-badge--${status}`;
	el.andonStatusBadge.textContent = status.replace(/_/g, " ");

	el.andonTitle.textContent = t.title || t.thread_key;

	const chips = [];
	if (t.repair_order) chips.push(`RO: ${t.repair_order}`);
	if (t.bay) chips.push(`Bay: ${t.bay}`);
	if (t.assigned_technician) chips.push(`Tech: ${t.assigned_technician}`);
	if (t.vin) chips.push(`VIN: ${t.vin}`);
	if (t.customer) chips.push(t.customer);
	if (state.roSummary && state.roSummary.status) chips.push(state.roSummary.status);
	el.andonMeta.innerHTML = chips.map((c) => `<span class="meta-chip">${escapeHtml(c)}</span>`).join("");

	if (t.blocked_reason) {
		el.andonBlockedReason.textContent = t.blocked_reason;
		el.andonBlockedReason.classList.remove("is-hidden");
	} else {
		el.andonBlockedReason.classList.add("is-hidden");
	}

	if (t.repair_order) {
		el.andonDeskLink.href = `/app/repair-order/${encodeURIComponent(t.repair_order)}`;
		el.andonDeskLink.classList.remove("is-hidden");
	} else {
		el.andonDeskLink.classList.add("is-hidden");
	}

	updateBlockedTimerDisplay();
	renderPinPill(null);
}

function renderTimeline({ scrollToBottom = false } = {}) {
	if (!el.timelineList) return;
	el.timelineList.innerHTML = "";

	if (el.loadMoreWrap) {
		el.loadMoreWrap.classList.toggle("is-hidden", !state.hasMore);
	}

	if (!state.events.length) {
		el.timelineList.innerHTML = `<div class="timeline-empty">No events in this thread yet. Tap PTT, Cam, or type an update below.</div>`;
		return;
	}

	const handlers = {
		canApprove: Boolean(boot.can_approve),
		onOpenLightbox: openLightbox,
		onResolveCard: handleResolveCard,
		onUndoReceipt: handleUndoReceipt,
		onJumpThread: (nextKey) => selectThread(nextKey, { pushHistory: true }),
	};

	const frag = document.createDocumentFragment();
	for (const ev of state.events) {
		frag.appendChild(renderEventCard(ev, handlers));
	}
	el.timelineList.appendChild(frag);

	if (scrollToBottom && el.timelineScroll) {
		requestAnimationFrame(() => {
			el.timelineScroll.scrollTop = el.timelineScroll.scrollHeight;
		});
	}
}

function mergeEvents(incoming, { prepend = false } = {}) {
	if (!Array.isArray(incoming) || !incoming.length) return false;
	const byName = new Map(state.events.map((e) => [e.name, e]));
	let changed = false;

	for (const ev of incoming) {
		if (!ev || !ev.name) continue;
		const prev = byName.get(ev.name);
		if (!prev) {
			changed = true;
		} else if (
			prev.resolution_state !== ev.resolution_state ||
			prev.ai_status !== ev.ai_status ||
			prev.transcript !== ev.transcript
		) {
			changed = true;
		}
		byName.set(ev.name, { ...(prev || {}), ...ev });
	}

	const ordered = prepend
		? [...incoming.filter((e) => !state.events.some((x) => x.name === e.name)), ...state.events]
		: Array.from(byName.values());

	ordered.sort((a, b) => {
		const ta = String(a.event_timestamp || "");
		const tb = String(b.event_timestamp || "");
		if (ta !== tb) return ta.localeCompare(tb);
		return String(a.name || "").localeCompare(String(b.name || ""));
	});

	state.events = ordered;
	return changed;
}

/* =========================================================================
   Thread Loading, URL Sync, Delta Sync & Card Actions
   ========================================================================= */

function syncUrlForThread(threadKey, { push = false } = {}) {
	if (!threadKey) return;
	const url = new URL(window.location.href);
	url.pathname = "/board";
	url.searchParams.delete("ro");
	url.searchParams.set("thread", threadKey);
	if (push) {
		window.history.pushState({ threadKey }, "", url.toString());
	} else {
		window.history.replaceState({ threadKey }, "", url.toString());
	}
}

function getThreadKeyFromUrl() {
	const params = new URLSearchParams(window.location.search);
	const threadParam = params.get("thread");
	if (threadParam) return threadParam.trim();
	const roParam = params.get("ro") || params.get("repair_order");
	if (roParam) {
		const cleaned = roParam.trim();
		return cleaned.startsWith("RO:") ? cleaned : `RO:${cleaned}`;
	}
	const pathMatch = window.location.pathname.match(/^\/board\/(.+)$/);
	if (pathMatch && pathMatch[1]) {
		return decodeURIComponent(pathMatch[1]).trim();
	}
	return null;
}

async function selectThread(threadKey, { pushHistory = false } = {}) {
	if (!threadKey) return;
	clearPinRevealState();
	state.activeThreadKey = threadKey;
	state.isLoadingThread = true;
	renderThreadRail();

	if (pushHistory) {
		syncUrlForThread(threadKey, { push: true });
	} else {
		syncUrlForThread(threadKey, { push: false });
	}

	if (realtimeClient) {
		realtimeClient.subscribeThread(threadKey);
	}

	el.timelineList.innerHTML = `<div class="timeline-empty">Loading ${escapeHtml(threadKey)}...</div>`;

	try {
		const res = await getBoardThread({ thread_key: threadKey, limit: 100 });
		state.activeThread = res.thread;
		state.events = Array.isArray(res.events) ? res.events : [];
		state.hasMore = Boolean(res.has_more);
		state.nextBeforeEvent = res.next_before_event || null;
		state.pinState = res.pin_state || null;
		state.roSummary = res.repair_order_summary || null;

		upsertThreadSummary(res.thread);
		renderAndonHeader();
		renderTimeline({ scrollToBottom: true });

		if (captureController && res.thread) {
			captureController.setThreadType(res.thread.thread_type);
		}
	} catch (err) {
		showToast(err.message || `Failed to load ${threadKey}.`, "error");
		el.timelineList.innerHTML = `<div class="timeline-empty">${escapeHtml(err.message || "Unable to load thread.")}</div>`;
	} finally {
		state.isLoadingThread = false;
	}
}

async function loadOlderEvents() {
	if (!state.activeThreadKey || !state.nextBeforeEvent) return;
	el.loadMoreBtn.disabled = true;
	try {
		const res = await getBoardThread({
			thread_key: state.activeThreadKey,
			limit: 100,
			before_event: state.nextBeforeEvent,
		});
		state.hasMore = Boolean(res.has_more);
		state.nextBeforeEvent = res.next_before_event || null;
		mergeEvents(res.events || [], { prepend: true });
		renderTimeline({ scrollToBottom: false });
	} catch (err) {
		showToast(err.message || "Failed to load earlier events.", "error");
	} finally {
		el.loadMoreBtn.disabled = false;
	}
}

async function syncThreadDelta() {
	if (!state.activeThreadKey || state.isLoadingThread) return;
	if (state.isSyncingDelta) {
		state.pendingDeltaSync = true;
		return;
	}
	state.isSyncingDelta = true;
	state.pendingDeltaSync = false;
	try {
		const lastEv = state.events.length ? state.events[state.events.length - 1].name : null;
		const knownVer = state.activeThread ? Number(state.activeThread.projection_version || 0) : 0;
		const res = await getThreadDelta({
			thread_key: state.activeThreadKey,
			after_event: lastEv,
			known_projection_version: knownVer,
		});

		if (res.thread) {
			state.activeThread = res.thread;
			upsertThreadSummary(res.thread);
		}
		if (res.pin_state) {
			state.pinState = res.pin_state;
		}
		const newEvents = Array.isArray(res.events) ? res.events : [];
		const openCards = Array.isArray(res.open_cards) ? res.open_cards : [];
		const eventsChanged = mergeEvents([...newEvents, ...openCards]);

		if (res.changed || eventsChanged) {
			renderAndonHeader();
			renderTimeline({ scrollToBottom: newEvents.length > 0 });
		}
	} catch (_err) {
		// Offline banner is already managed by api.js
	} finally {
		state.isSyncingDelta = false;
		if (state.pendingDeltaSync) {
			state.pendingDeltaSync = false;
			syncThreadDelta();
		}
	}
}

async function refreshThreadList() {
	try {
		const res = await listBoardThreads({ limit: 100 });
		if (res && Array.isArray(res.threads)) {
			state.threads = res.threads;
			renderThreadRail();
		}
	} catch (_err) {
		// ignore background list refresh error
	}
}

async function handleResolveCard(params) {
	try {
		const res = await resolveBoardCard(params);
		if (res.already_resolved) {
			showToast("Card was already resolved.", "info");
		} else {
			showToast(
				params.decision === "reject" ? "Card rejected." : "Action committed.",
				params.decision === "reject" ? "warn" : "success"
			);
		}
		if (res && res.re_enqueued) {
			const reEnq = res.re_enqueued;
			const syncDone = Boolean(
				typeof reEnq === "object" &&
					(reEnq.synchronous || reEnq.mode === "sync" || reEnq.result)
			);
			if (!syncDone && el.aiStatusBar) {
				el.aiStatusBar.textContent = "AI Orchestrator processing clarification...";
				el.aiStatusBar.classList.remove("is-hidden");
			}
			await syncThreadDelta();
			if (syncDone && el.aiStatusBar) {
				el.aiStatusBar.classList.add("is-hidden");
			}
		} else {
			await syncThreadDelta();
		}
		return res;
	} catch (err) {
		showToast(err.message || "Failed to resolve card.", "error");
		throw err;
	}
}

async function handleUndoReceipt(params) {
	try {
		const res = await undoBoardReceipt(params);
		showToast(res.already_undone ? "Receipt was already undone." : "Action undone.", "success");
		await syncThreadDelta();
		return res;
	} catch (err) {
		showToast(err.message || "Undo failed.", "error");
		throw err;
	}
}

/* =========================================================================
   Bootstrap
   ========================================================================= */

function bindUiListeners() {
	if (el.railToggleBtn) {
		el.railToggleBtn.addEventListener("click", () => toggleMobileRail());
	}
	if (el.railBackdrop) {
		el.railBackdrop.addEventListener("click", () => toggleMobileRail(false));
	}

	if (el.searchInput) {
		el.searchInput.addEventListener("input", (evt) => {
			state.searchQuery = evt.target.value.trim();
			renderThreadRail();
		});
	}

	if (el.filterRow) {
		for (const chip of el.filterRow.querySelectorAll(".filter-chip")) {
			chip.addEventListener("click", () => {
				state.filter = chip.dataset.filter || "ALL";
				for (const other of el.filterRow.querySelectorAll(".filter-chip")) {
					other.classList.toggle("is-active", other === chip);
				}
				renderThreadRail();
			});
		}
	}

	// Intercept top-nav quick channel links for instant SPA switching
	for (const link of document.querySelectorAll("[data-quick-thread]")) {
		link.addEventListener("click", (evt) => {
			evt.preventDefault();
			const targetKey = link.dataset.quickThread;
			if (targetKey) {
				selectThread(targetKey, { pushHistory: true });
				toggleMobileRail(false);
			}
		});
	}

	if (el.refreshThreadBtn) {
		el.refreshThreadBtn.addEventListener("click", async () => {
			await Promise.all([refreshThreadList(), syncThreadDelta()]);
			showToast("Synced.", "info");
		});
	}

	if (el.loadMoreBtn) {
		el.loadMoreBtn.addEventListener("click", () => loadOlderEvents());
	}

	if (el.lightboxClose) {
		el.lightboxClose.addEventListener("click", closeLightbox);
	}
	if (el.lightbox) {
		el.lightbox.addEventListener("click", (evt) => {
			if (evt.target === el.lightbox) closeLightbox();
		});
	}
	document.addEventListener("keydown", (evt) => {
		if (evt.key === "Escape") closeLightbox();
	});

	if (el.offlineRetryBtn) {
		el.offlineRetryBtn.addEventListener("click", async () => {
			if (captureController && (await captureController.retryLastFailed())) {
				return;
			}
			await syncThreadDelta();
		});
	}

	window.addEventListener("popstate", () => {
		const keyFromUrl = getThreadKeyFromUrl();
		if (keyFromUrl && keyFromUrl !== state.activeThreadKey) {
			selectThread(keyFromUrl, { pushHistory: false });
		}
	});
}

function init() {
	setOfflineHandler((isOffline, reason) => {
		if (!el.offlineBanner) return;
		el.offlineBanner.classList.toggle("is-hidden", !isOffline);
		if (isOffline && reason && el.offlineBannerText) {
			el.offlineBannerText.textContent = reason;
		}
	});

	bindUiListeners();
	renderThreadRail();

	captureController = new BoardCaptureController({
		getContext: () => ({
			thread_key: state.activeThreadKey,
			ro_id: state.activeThread ? state.activeThread.repair_order : null,
			vin: state.activeThread ? state.activeThread.vin : null,
			bay_id: state.activeThread ? state.activeThread.bay : null,
			tech_id: boot.tech_id || null,
		}),
		onCaptureSent: (res) => {
			if (res && res.thread) {
				state.activeThread = res.thread;
				upsertThreadSummary(res.thread);
				renderAndonHeader();
			}
			if (res && res.event) {
				mergeEvents([res.event]);
				renderTimeline({ scrollToBottom: true });
			}
			if (res && res.ai_enqueued) {
				const aiInfo = res.ai_enqueue_info || {};
				const evStatus = res.event && res.event.ai_status ? res.event.ai_status : null;
				const syncDone = Boolean(
					aiInfo.synchronous ||
						aiInfo.mode === "sync" ||
						aiInfo.result ||
						(evStatus && evStatus !== "QUEUED" && evStatus !== "PROCESSING")
				);
				if (evStatus === "FAILED") {
					el.aiStatusBar.textContent = `AI note: ${(res.event && res.event.ai_error) || "Processing failed"}`;
					el.aiStatusBar.classList.remove("is-hidden");
				} else if (syncDone) {
					el.aiStatusBar.classList.add("is-hidden");
				} else {
					el.aiStatusBar.textContent = "AI Orchestrator analyzing capture...";
					el.aiStatusBar.classList.remove("is-hidden");
				}
				// Immediately sync delta so synchronous AI cards (CLARIFICATION_CARD, AI_RECEIPT, APPROVAL_GATE) render right away
				const prevCount = state.events.length;
				syncThreadDelta().then(() => {
					if (state.events.length > prevCount && evStatus !== "FAILED" && el.aiStatusBar) {
						el.aiStatusBar.classList.add("is-hidden");
					}
				});
			}
		},
		onToast: showToast,
		elements: {
			intentPills: document.getElementById("intent-pills"),
			captureNotice: document.getElementById("capture-notice"),
			photoTray: document.getElementById("photo-tray"),
			uploadProgress: document.getElementById("upload-progress"),
			uploadProgressFill: document.getElementById("upload-progress-fill"),
			uploadProgressLabel: document.getElementById("upload-progress-label"),
			pttRecordingBar: document.getElementById("ptt-recording-bar"),
			pttTimer: document.getElementById("ptt-timer"),
			pttCancelBtn: document.getElementById("ptt-cancel-btn"),
			pttStopSendBtn: document.getElementById("ptt-stop-send-btn"),
			pttBtn: document.getElementById("ptt-btn"),
			cameraInput: document.getElementById("camera-input"),
			textInput: document.getElementById("capture-text"),
			sendBtn: document.getElementById("send-btn"),
		},
	});

	realtimeClient = new BoardRealtimeClient({
		sitename: boot.sitename,
		socketioPort: boot.socketio_port,
		onBoardEvent: (payload) => {
			if (payload.thread === state.activeThreadKey) {
				// Fetch delta so full media URLs and card updates stay authoritative
				syncThreadDelta();
			}
		},
		onProjection: (payload) => {
			upsertThreadSummary(payload);
			if (payload.thread_key === state.activeThreadKey) {
				state.activeThread = { ...(state.activeThread || {}), ...payload };
				renderAndonHeader();
			}
		},
		onAiStatus: (payload) => {
			if (payload.thread_key !== state.activeThreadKey) return;
			if (payload.ai_status === "PROCESSING" || payload.ai_status === "QUEUED") {
				el.aiStatusBar.textContent = "AI Orchestrator processing update...";
				el.aiStatusBar.classList.remove("is-hidden");
			} else if (payload.ai_status === "FAILED") {
				el.aiStatusBar.textContent = `AI note: ${payload.ai_error || "Processing failed"}`;
				el.aiStatusBar.classList.remove("is-hidden");
				syncThreadDelta();
			} else {
				el.aiStatusBar.classList.add("is-hidden");
				syncThreadDelta();
			}
		},
		onThreadSummary: (payload) => {
			upsertThreadSummary(payload);
		},
		onCatchUpSync: () => {
			syncThreadDelta();
		},
	});
	realtimeClient.connect();

	// Live 1-second ticking blocked_since timer
	blockedTimerInterval = setInterval(updateBlockedTimerDisplay, 1000);
	window.addEventListener("beforeunload", () => {
		clearPinRevealState();
		if (blockedTimerInterval) clearInterval(blockedTimerInterval);
		if (realtimeClient) realtimeClient.destroy();
	});

	if (boot.initial_thread_error) {
		showToast(boot.initial_thread_error, "warn");
	}

	const initialKey =
		boot.initial_thread_key ||
		getThreadKeyFromUrl() ||
		(state.threads[0] ? state.threads[0].thread_key : "SHOP:INTAKE");

	selectThread(initialKey, { pushHistory: false });
}

if (document.readyState === "loading") {
	document.addEventListener("DOMContentLoaded", init);
} else {
	init();
}
