/**
 * Main controller for the ISM Shop OS Blackboard (/board).
 * - Left rail: 2-tab switcher (Active ROs + Action Feed)
 * - Pinned Andon Header: pulsing status + 1s ticking blocked_since timer
 * - Seam 6: conditional masked tap-to-reveal PIN-to-Drive pill (15s auto-mask, zero persistence)
 * - Realtime Socket.IO + visibilitychange / reconnect delta sync
 */

import {
	getBoardThread,
	getThreadDelta,
	listBoardActionFeed,
	listBoardThreads,
	revealRoPin,
	setOfflineHandler,
	updateBoardActionCard,
} from "./api.js";
import { BoardCaptureController } from "./capture.js";
import { escapeHtml, renderEventCard } from "./cards.js";
import { BoardRealtimeClient } from "./realtime.js";

const PIN_AUTO_MASK_SECONDS = 15;

const boot = window.ISM_BOARD_BOOT || {};
const CAN_MANAGE = Boolean(boot.can_manage ?? boot.can_approve);

const state = {
	threads: Array.isArray(boot.initial_threads) ? [...boot.initial_threads] : [],
	actionFeed: Array.isArray(boot.initial_action_feed) ? [...boot.initial_action_feed] : [],
	selectedActionIds: new Set(),
	railTab: "THREADS",
	loadedListStatus: "OPEN",
	activeThreadKey: null,
	activeThread: null,
	events: [],
	hasMore: false,
	nextBeforeEvent: null,
	pinState: null,
	roSummary: null,
	canPost: true,
	readOnlyReason: "",
	filter: "ACTIVE",
	searchQuery: "",
	isLoadingThread: false,
	isSyncingDelta: false,
	pendingDeltaSync: false,
	// Transient in-memory PIN reveal state (NEVER persisted to storage or DOM datasets)
	pinRevealed: false,
	pinSecondsLeft: 0,
	pinTimerId: null,
};

const el = {
	railToggleBtn: document.getElementById("rail-toggle-btn"),
	rail: document.getElementById("board-rail"),
	railBackdrop: document.getElementById("rail-backdrop"),
	railTabs: document.querySelectorAll(".rail-tab[data-rail-tab]"),
	railThreadsCount: document.getElementById("rail-threads-count"),
	railActionsCount: document.getElementById("rail-actions-count"),
	railPanelThreads: document.getElementById("rail-panel-threads"),
	railPanelActions: document.getElementById("rail-panel-actions"),
	searchInput: document.getElementById("thread-search-input"),
	filterRow: document.getElementById("thread-filter-row"),
	roSectionLabel: document.getElementById("ro-section-label"),
	roThreadsList: document.getElementById("ro-threads-list"),
	activeRoCount: document.getElementById("active-ro-count"),
	actionFeedList: document.getElementById("action-feed-list"),
	actionFeedCopySelectedBtn: document.getElementById("action-feed-copy-selected-btn"),
	actionFeedSelectAllBtn: document.getElementById("action-feed-select-all-btn"),
	actionFeedClearBtn: document.getElementById("action-feed-clear-btn"),
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

async function copyTextToClipboard(text, successMsg = "Copied for Agent.") {
	if (!text) return;
	try {
		if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
			await navigator.clipboard.writeText(text);
		} else {
			const ta = document.createElement("textarea");
			ta.value = text;
			ta.setAttribute("readonly", "");
			ta.style.position = "fixed";
			ta.style.opacity = "0";
			document.body.appendChild(ta);
			ta.select();
			document.execCommand("copy");
			ta.remove();
		}
		showToast(successMsg, "success");
	} catch (_err) {
		showToast("Unable to copy to clipboard.", "error");
	}
}

function formatSingleActionPrompt({ action_item, repair_order, title }) {
	const actId = action_item || "";
	const roPart = repair_order ? ` (${repair_order})` : "";
	const titlePart = title ? `: ${title}` : "";
	return `Address ${actId}${roPart}${titlePart}`.trim();
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
		Boolean(thread && thread.repair_order) &&
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
   Left Rail: 2-Tab Switcher, Active ROs & Action Feed
   ========================================================================= */

function switchRailTab(tabName) {
	state.railTab = tabName === "ACTIONS" ? "ACTIONS" : "THREADS";
	for (const tabBtn of el.railTabs) {
		const active = tabBtn.dataset.railTab === state.railTab;
		tabBtn.classList.toggle("is-active", active);
		tabBtn.setAttribute("aria-selected", String(active));
	}
	if (el.railPanelThreads) {
		el.railPanelThreads.classList.toggle("is-hidden", state.railTab !== "THREADS");
	}
	if (el.railPanelActions) {
		el.railPanelActions.classList.toggle("is-hidden", state.railTab !== "ACTIONS");
	}
}

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

	if (state.filter === "CLOSED") {
		return thread.board_status === "CLOSED";
	}
	if (thread.board_status === "CLOSED") {
		return false;
	}
	if (state.filter === "MY_BAYS") {
		if (!boot.tech_id) return false;
		return thread.assigned_technician === boot.tech_id;
	}
	if (state.filter === "HAS_ACTIONS") {
		return Number(thread.open_action_count || 0) > 0;
	}
	if (state.filter === "BLOCKED") {
		return ["BLOCKED_PARTS", "NEEDS_ACTION", "WAITING_CUSTOMER"].includes(thread.board_status);
	}
	return true;
}

function buildThreadItemButton(thread) {
	const btn = document.createElement("button");
	btn.type = "button";
	btn.className = `thread-item ${thread.thread_key === state.activeThreadKey ? "is-selected" : ""}`;
	btn.dataset.threadKey = thread.thread_key;

	const displayTitle = thread.title || thread.repair_order || thread.thread_key;
	const openCount = Number(thread.open_action_count || 0);
	const subLine =
		thread.last_event_preview ||
		[thread.bay, thread.customer, thread.vin].filter(Boolean).join(" · ") ||
		"No updates yet";

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
	if (!el.roThreadsList) return;
	el.roThreadsList.innerHTML = "";

	const ros = state.threads.filter(threadMatchesFilter);

	if (el.roSectionLabel) {
		el.roSectionLabel.textContent = state.filter === "CLOSED" ? "Closed ROs" : "Active ROs";
	}
	if (el.activeRoCount) el.activeRoCount.textContent = String(ros.length);
	if (el.railThreadsCount && state.filter !== "CLOSED") {
		const openCount = state.threads.filter((t) => t.board_status !== "CLOSED").length;
		el.railThreadsCount.textContent = String(openCount);
	}

	if (!ros.length) {
		const empty = document.createElement("div");
		empty.className = "rail-empty-state";
		empty.textContent =
			state.filter === "CLOSED"
				? "No closed Repair Order threads."
				: "No active Repair Order threads. Active ROs appear automatically when created.";
		el.roThreadsList.appendChild(empty);
		return;
	}

	for (const roThread of ros) {
		el.roThreadsList.appendChild(buildThreadItemButton(roThread));
	}
}

function updateActionFeedSelectionButtons() {
	if (!el.actionFeedCopySelectedBtn) return;
	const selectedCount = state.selectedActionIds.size;
	el.actionFeedCopySelectedBtn.disabled = selectedCount === 0;
	el.actionFeedCopySelectedBtn.textContent =
		selectedCount > 0 ? `Copy Selected for Agent (${selectedCount})` : "Copy Selected for Agent";
}

function renderActionFeed() {
	if (!el.actionFeedList) return;
	const validIds = new Set(state.actionFeed.map((item) => item.name).filter(Boolean));
	for (const id of Array.from(state.selectedActionIds)) {
		if (!validIds.has(id)) {
			state.selectedActionIds.delete(id);
		}
	}

	if (el.railActionsCount) {
		el.railActionsCount.textContent = String(state.actionFeed.length);
	}
	updateActionFeedSelectionButtons();
	el.actionFeedList.innerHTML = "";

	if (!state.actionFeed.length) {
		const empty = document.createElement("div");
		empty.className = "rail-empty-state";
		empty.textContent = "No open RO action cards right now.";
		el.actionFeedList.appendChild(empty);
		return;
	}

	for (const item of state.actionFeed) {
		const actId = item.name || "";
		const roName = item.reference_name || "";
		const targetThreadKey = item.thread_key || (roName ? `RO:${roName}` : "");
		const checked = state.selectedActionIds.has(actId);

		const card = document.createElement("div");
		card.className = "action-feed-item";
		card.innerHTML = `
			<div class="action-feed-item__top">
				<label class="action-feed-item__check">
					<input type="checkbox" class="js-feed-checkbox" data-action-id="${escapeHtml(actId)}" ${checked ? "checked" : ""} />
					<span class="act-id-badge">${escapeHtml(actId)}</span>
				</label>
				<span class="prio-badge prio-badge--${escapeHtml(item.priority || "Medium")}">${escapeHtml(item.priority || "Medium")}</span>
			</div>
			<div class="action-feed-item__meta">
				${item.category ? `<span class="meta-chip">${escapeHtml(item.category)}</span>` : ""}
				${roName ? `<span class="meta-chip">${escapeHtml(roName)}</span>` : ""}
			</div>
			<div class="action-feed-item__title">${escapeHtml(item.title || actId)}</div>
			${
				item.clearing_condition
					? `<div class="action-feed-item__clearing">Auto-clears: ${escapeHtml(item.clearing_condition)}</div>`
					: ""
			}
			<div class="action-feed-item__actions">
				${
					targetThreadKey
						? `<button type="button" class="btn btn-sm btn-ghost js-feed-jump-btn">Open RO</button>`
						: ""
				}
				<button type="button" class="btn btn-sm btn-primary js-feed-copy-btn">Copy</button>
			</div>
		`;

		const cb = card.querySelector(".js-feed-checkbox");
		if (cb) {
			cb.addEventListener("change", () => {
				if (cb.checked) {
					state.selectedActionIds.add(actId);
				} else {
					state.selectedActionIds.delete(actId);
				}
				updateActionFeedSelectionButtons();
			});
		}

		const jumpBtn = card.querySelector(".js-feed-jump-btn");
		if (jumpBtn && targetThreadKey) {
			jumpBtn.addEventListener("click", () => {
				selectThread(targetThreadKey, { pushHistory: true });
				toggleMobileRail(false);
			});
		}

		const copyBtn = card.querySelector(".js-feed-copy-btn");
		if (copyBtn) {
			copyBtn.addEventListener("click", () => {
				copyTextToClipboard(
					formatSingleActionPrompt({
						action_item: actId,
						repair_order: roName,
						title: item.title || "",
					})
				);
			});
		}

		el.actionFeedList.appendChild(card);
	}
}

/* =========================================================================
   Andon Header & Timeline Rendering
   ========================================================================= */

function renderAndonHeader() {
	const t = state.activeThread;
	if (!t) {
		el.andonStatusBadge.className = "andon-badge andon-badge--CLOSED";
		el.andonStatusBadge.textContent = "NONE";
		el.andonTitle.textContent = state.threads.length ? "Select an Active RO" : "No Active Repair Orders";
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
	if (state.roSummary) {
		const locRaw = String(state.roSummary.vehicle_location || "");
		const locShort =
			locRaw === "In Bay / Being Serviced"
				? "In Bay"
				: locRaw === "In Customer Lot"
				? "In Lot"
				: locRaw === "Completed / Parked in Lot"
				? "Parked (Done)"
				: locRaw;
		const approvedCnt =
			state.roSummary.approved_count ?? state.roSummary.total_services ?? null;
		const completedCnt =
			state.roSummary.completed_count ?? state.roSummary.completed_services ?? null;
		const hasTasks = approvedCnt !== null && Number(approvedCnt) >= 0;
		const taskPart = hasTasks ? `${Number(completedCnt || 0)}/${Number(approvedCnt)} tasks` : "";
		const execLabel = [locShort, taskPart].filter(Boolean).join(" · ");
		if (execLabel) chips.push(execLabel);
	}
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

	if (!state.activeThread) {
		el.timelineList.innerHTML = `<div class="timeline-empty">No active Repair Order threads. Active ROs appear automatically when created.</div>`;
		return;
	}

	if (!state.events.length) {
		el.timelineList.innerHTML = `<div class="timeline-empty">No events in this RO thread yet. Tap PTT, Cam, or type an update below.</div>`;
		return;
	}

	const handlers = {
		canManage: CAN_MANAGE,
		onOpenLightbox: openLightbox,
		onCopyForAgent: ({ action_item, repair_order, title }) => {
			copyTextToClipboard(
				formatSingleActionPrompt({
					action_item,
					repair_order: repair_order || (state.activeThread && state.activeThread.repair_order) || "",
					title,
				})
			);
		},
		onDismissActionCard: handleDismissActionCard,
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
		const merged = { ...(prev || {}), ...ev };
		if (!prev) {
			changed = true;
		} else if (
			prev.resolution_state !== merged.resolution_state ||
			prev.resolved_by !== merged.resolved_by ||
			prev.resolved_at !== merged.resolved_at ||
			JSON.stringify(prev.resolution_payload ?? null) !==
				JSON.stringify(merged.resolution_payload ?? null) ||
			prev.ai_status !== merged.ai_status ||
			prev.ai_error !== merged.ai_error ||
			prev.transcript !== merged.transcript ||
			JSON.stringify(prev.card_schema ?? null) !== JSON.stringify(merged.card_schema ?? null)
		) {
			changed = true;
		}
		byName.set(ev.name, merged);
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
   Thread Loading, URL Sync, Delta Sync & Action Feed Actions
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
	if (!threadKey) {
		state.activeThreadKey = null;
		state.activeThread = null;
		state.events = [];
		state.canPost = false;
		state.readOnlyReason = "Select an active Repair Order to post updates.";
		renderThreadRail();
		renderAndonHeader();
		renderTimeline({ scrollToBottom: false });
		if (captureController) {
			captureController.setThreadType("REPAIR_ORDER");
			captureController.setReadOnly(true, state.readOnlyReason);
		}
		return;
	}

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
		state.canPost = res.can_post !== false;
		state.readOnlyReason = res.read_only_reason || "";

		upsertThreadSummary(res.thread);
		renderAndonHeader();
		renderTimeline({ scrollToBottom: true });

		if (captureController && res.thread) {
			captureController.setThreadType(res.thread.thread_type);
			captureController.setReadOnly(!state.canPost, state.readOnlyReason);
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

async function refreshActionFeed() {
	try {
		const res = await listBoardActionFeed({ status: "Open", limit: 50 });
		if (res && Array.isArray(res.items)) {
			state.actionFeed = res.items;
			renderActionFeed();
		}
	} catch (_err) {
		// ignore background action feed refresh error
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
		if (res.can_post !== undefined) {
			state.canPost = res.can_post !== false;
			state.readOnlyReason = res.read_only_reason || "";
			if (captureController) {
				captureController.setReadOnly(!state.canPost, state.readOnlyReason);
			}
		}
		const newEvents = Array.isArray(res.events) ? res.events : [];
		const openCards = Array.isArray(res.open_cards) ? res.open_cards : [];
		const updatedCards = Array.isArray(res.updated_cards) ? res.updated_cards : [];
		const eventsChanged = mergeEvents([...newEvents, ...openCards, ...updatedCards]);

		if (res.changed || eventsChanged) {
			renderAndonHeader();
			renderTimeline({ scrollToBottom: newEvents.length > 0 });
			refreshActionFeed();
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

async function refreshThreadList(statusOverride = null) {
	const targetStatus = statusOverride || (state.filter === "CLOSED" ? "CLOSED" : "OPEN");
	try {
		const res = await listBoardThreads({ status: targetStatus, limit: 100 });
		if (res && Array.isArray(res.threads)) {
			state.loadedListStatus = targetStatus;
			state.threads = res.threads;
			renderThreadRail();
		}
	} catch (_err) {
		// ignore background list refresh error
	}
}

async function handleDismissActionCard({ action_item, event_name }) {
	try {
		const res = await updateBoardActionCard({
			action_item,
			action: "dismiss",
		});
		if (event_name) {
			mergeEvents([
				{
					name: event_name,
					resolution_state: "REJECTED",
					resolution_payload: {
						resolution_notes: "Dismissed from board",
					},
				},
			]);
			renderTimeline({ scrollToBottom: false });
		}
		showToast("Action card dismissed.", "info");
		await Promise.all([syncThreadDelta(), refreshActionFeed()]);
		return res;
	} catch (err) {
		showToast(err.message || "Failed to dismiss action card.", "error");
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

	for (const tabBtn of el.railTabs) {
		tabBtn.addEventListener("click", () => {
			switchRailTab(tabBtn.dataset.railTab || "THREADS");
		});
	}

	if (el.searchInput) {
		el.searchInput.addEventListener("input", (evt) => {
			state.searchQuery = evt.target.value.trim();
			renderThreadRail();
		});
	}

	if (el.filterRow) {
		for (const chip of el.filterRow.querySelectorAll(".filter-chip")) {
			chip.addEventListener("click", async () => {
				const nextFilter = chip.dataset.filter || "ACTIVE";
				state.filter = nextFilter;
				for (const other of el.filterRow.querySelectorAll(".filter-chip")) {
					other.classList.toggle("is-active", other === chip);
				}
				const neededStatus = nextFilter === "CLOSED" ? "CLOSED" : "OPEN";
				if (state.loadedListStatus !== neededStatus) {
					await refreshThreadList(neededStatus);
				} else {
					renderThreadRail();
				}
			});
		}
	}

	if (el.actionFeedCopySelectedBtn) {
		el.actionFeedCopySelectedBtn.addEventListener("click", () => {
			const selected = state.actionFeed.filter((item) => state.selectedActionIds.has(item.name));
			if (!selected.length) return;
			const parts = selected.map((item) => {
				const ro = item.reference_name ? ` (${item.reference_name})` : "";
				return `${item.name}${ro}`;
			});
			copyTextToClipboard(`Address action cards: ${parts.join(", ")}`);
		});
	}

	if (el.actionFeedSelectAllBtn) {
		el.actionFeedSelectAllBtn.addEventListener("click", () => {
			for (const item of state.actionFeed) {
				if (item.name) state.selectedActionIds.add(item.name);
			}
			renderActionFeed();
		});
	}

	if (el.actionFeedClearBtn) {
		el.actionFeedClearBtn.addEventListener("click", () => {
			state.selectedActionIds.clear();
			renderActionFeed();
		});
	}

	if (el.refreshThreadBtn) {
		el.refreshThreadBtn.addEventListener("click", async () => {
			await Promise.all([refreshThreadList(), refreshActionFeed(), syncThreadDelta()]);
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
	renderActionFeed();

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
					el.aiStatusBar.textContent = "Board AI analyzing capture...";
					el.aiStatusBar.classList.remove("is-hidden");
				}
				const prevCount = state.events.length;
				syncThreadDelta().then(() => {
					if (state.events.length > prevCount && evStatus !== "FAILED" && el.aiStatusBar) {
						el.aiStatusBar.classList.add("is-hidden");
					}
					refreshActionFeed();
				});
			}
		},
		onToast: showToast,
		elements: {
			readonlyBanner: document.getElementById("capture-readonly-banner"),
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
				syncThreadDelta();
			}
			refreshActionFeed();
		},
		onProjection: (payload) => {
			upsertThreadSummary(payload);
			if (payload.thread_key === state.activeThreadKey) {
				state.activeThread = { ...(state.activeThread || {}), ...payload };
				renderAndonHeader();
			}
			refreshActionFeed();
		},
		onAiStatus: (payload) => {
			if (payload.thread_key !== state.activeThreadKey) return;
			if (payload.ai_status === "PROCESSING" || payload.ai_status === "QUEUED") {
				el.aiStatusBar.textContent = "Board AI analyzing update...";
				el.aiStatusBar.classList.remove("is-hidden");
			} else if (payload.ai_status === "FAILED") {
				el.aiStatusBar.textContent = `AI note: ${payload.ai_error || "Processing failed"}`;
				el.aiStatusBar.classList.remove("is-hidden");
				syncThreadDelta();
			} else {
				el.aiStatusBar.classList.add("is-hidden");
				syncThreadDelta();
				refreshActionFeed();
			}
		},
		onThreadSummary: (payload) => {
			if (
				boot.actor_role === "TECH" &&
				(!boot.tech_id || !payload || payload.assigned_technician !== boot.tech_id)
			) {
				return;
			}
			upsertThreadSummary(payload);
		},
		onCatchUpSync: () => {
			syncThreadDelta();
			refreshActionFeed();
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
		(state.threads[0] ? state.threads[0].thread_key : null);

	selectThread(initialKey, { pushHistory: false });
}

if (document.readyState === "loading") {
	document.addEventListener("DOMContentLoaded", init);
} else {
	init();
}
