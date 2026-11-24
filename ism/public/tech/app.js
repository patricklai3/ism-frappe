/**
 * Main controller for the 3-Layer ISM Technician Execution Workbench (/tech).
 * Vanilla ES module — zero build step.
 *
 * Layer 1: My Assigned Jobs (Approved Ready ROs + Unlinked Diagnostic Schedule Entries)
 * Layer 2A: Diagnostic Appointment (Schedule Entry) with inline SE PIN reveal
 * Layer 2B: Approved Repair Order Execution (Pick Up -> Checklist -> Required Odometer Out & Drop Off)
 * Layer 3: Exception Chat Board (lazily loaded, reusing BoardCaptureController, renderEventCard & BoardRealtimeClient)
 */

import {
	dropOffVehicle,
	getTechRepairOrder,
	getTechScheduleEntry,
	listMyJobs,
	pickUpVehicle,
	revealScheduleEntryPin,
	setTechOfflineHandler,
	toggleServiceItem,
	updateInspectionItem,
} from "./api.js";
import {
	getBoardThread,
	getThreadDelta,
	revealRoPin,
	setOfflineHandler as setBoardOfflineHandler,
} from "../board/api.js";
import { BoardCaptureController } from "../board/capture.js";
import { escapeHtml, renderEventCard } from "../board/cards.js";
import { BoardRealtimeClient } from "../board/realtime.js";

const PIN_AUTO_MASK_SECONDS = 15;
const DESKTOP_BREAKPOINT = 960;

const boot = window.ISM_TECH_BOOT || {};
const ACTOR_ROLE = boot.actor_role || "TECH";

const state = {
	tab: "ACTIVE", // "ACTIVE" | "COMPLETED"
	searchQuery: "",
	employeeLinked: boot.employee_linked !== false,
	jobs: Array.isArray(boot.initial_jobs) ? [...boot.initial_jobs] : [],
	activeCount: Number(boot.active_count || 0),
	completedCount: Number(boot.completed_count || 0),
	isLoadingJobs: false,

	// Navigation state
	activeKind: null, // "REPAIR_ORDER" | "SCHEDULE_ENTRY" | null
	activeId: null,
	activeView: "LIST", // "LIST" (L1) | "DETAIL" (L2) | "BOARD" (L3)

	// Layer 2B (RO) state
	roData: null,
	roPinState: null,
	roExceptionSummary: null,
	isLoadingDetail: false,
	isMutatingLifecycle: false,
	togglingServices: new Set(),
	inspectionExpanded: false,
	tempParkOpen: false,
	dropOffOdoDraft: "",
	dropOffOdoError: "",
	tempParkOdoDraft: "",
	tempParkOdoError: "",

	// Layer 2A (SE) state
	seData: null,
	sePinState: null,

	// Transient in-memory PIN reveal state (NEVER persisted to storage or DOM dataset)
	pinRevealed: false,
	pinTargetKey: null,
	pinSecondsLeft: 0,
	pinTimerId: null,

	// Layer 3 (Exception Chat Board) state
	boardThreadKey: null,
	boardThread: null,
	boardEvents: [],
	boardHasMore: false,
	boardNextBeforeEvent: null,
	boardCanPost: true,
	boardReadOnlyReason: "",
	isLoadingBoard: false,
	isSyncingDelta: false,
	pendingDeltaSync: false,
};

const el = {
	shell: document.getElementById("tech-shell"),
	totalCount: document.getElementById("tech-total-count"),
	refreshJobsBtn: document.getElementById("tech-refresh-jobs-btn"),
	tabActive: document.getElementById("tab-active-jobs"),
	tabCompleted: document.getElementById("tab-completed-jobs"),
	tabActiveCount: document.getElementById("tab-active-count"),
	tabCompletedCount: document.getElementById("tab-completed-count"),
	jobSearch: document.getElementById("tech-job-search"),
	jobsList: document.getElementById("tech-jobs-list"),

	layer2: document.getElementById("tech-layer-2"),
	l2Empty: document.getElementById("tech-l2-empty"),
	l2Loading: document.getElementById("tech-l2-loading"),
	l2LoadingText: document.getElementById("tech-l2-loading-text"),
	seView: document.getElementById("tech-se-view"),
	roView: document.getElementById("tech-ro-view"),

	layer3: document.getElementById("tech-layer-3"),
	l3BackBtn: document.getElementById("tech-l3-back-btn"),
	l3Title: document.getElementById("tech-l3-title"),
	l3Subtitle: document.getElementById("tech-l3-subtitle"),
	l3StatusBadge: document.getElementById("tech-l3-status-badge"),
	l3BlockedTimer: document.getElementById("tech-l3-blocked-timer"),
	l3SyncBtn: document.getElementById("tech-l3-sync-btn"),
	timelineScroll: document.getElementById("tech-timeline-scroll"),
	loadMoreWrap: document.getElementById("tech-load-more-wrap"),
	loadMoreBtn: document.getElementById("tech-load-more-btn"),
	timelineList: document.getElementById("tech-timeline-list"),
	aiStatusBar: document.getElementById("tech-ai-status-bar"),

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
let liveTimerInterval = null;
let customSocketListeners = [];

/* =========================================================================
   Utilities: Toast, Lightbox, Timers, Formatting
   ========================================================================= */

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

function handleOffline(isOffline, reason) {
	if (!el.offlineBanner) return;
	el.offlineBanner.classList.toggle("is-hidden", !isOffline);
	if (isOffline && reason && el.offlineBannerText) {
		el.offlineBannerText.textContent = reason;
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

function isDesktopLayout() {
	return window.innerWidth >= DESKTOP_BREAKPOINT;
}

function parseTimestamp(ts) {
	if (!ts) return NaN;
	const str = String(ts).trim();
	const normalized = str.includes("T") ? str : str.replace(" ", "T");
	return new Date(normalized).getTime();
}

function formatBayElapsed(isoOrSqlTs) {
	const start = parseTimestamp(isoOrSqlTs);
	if (Number.isNaN(start)) return "";
	const diffSec = Math.max(0, Math.floor((Date.now() - start) / 1000));
	const hrs = Math.floor(diffSec / 3600);
	const mins = Math.floor((diffSec % 3600) / 60);
	if (hrs > 0) {
		return `${hrs}h ${String(mins).padStart(2, "0")}m`;
	}
	return `${mins}m`;
}

function formatBlockedElapsed(isoOrSqlTs) {
	const start = parseTimestamp(isoOrSqlTs);
	if (Number.isNaN(start)) return "";
	const diffSec = Math.max(0, Math.floor((Date.now() - start) / 1000));
	const hrs = Math.floor(diffSec / 3600);
	const mins = Math.floor((diffSec % 3600) / 60);
	const secs = diffSec % 60;
	if (hrs > 0) {
		return `${hrs}h ${String(mins).padStart(2, "0")}m`;
	}
	if (mins > 0) {
		return `${mins}m`;
	}
	return `${secs}s`;
}

function formatShortTime(ts) {
	const ms = parseTimestamp(ts);
	if (Number.isNaN(ms)) return "";
	return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatScheduleTime(timeStr) {
	if (!timeStr) return "";
	const parts = String(timeStr).split(":");
	if (parts.length < 2) return String(timeStr);
	const h = Number(parts[0]);
	const m = Number(parts[1]);
	if (Number.isNaN(h) || Number.isNaN(m)) return String(timeStr);
	const suffix = h >= 12 ? "PM" : "AM";
	const h12 = h % 12 || 12;
	return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
}

function formatScheduleDate(dateStr) {
	if (!dateStr) return "";
	const today = new Date();
	const ymd = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(
		today.getDate()
	).padStart(2, "0")}`;
	if (String(dateStr) === ymd) return "Today";
	const d = new Date(`${dateStr}T00:00:00`);
	if (Number.isNaN(d.getTime())) return String(dateStr);
	return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatNumber(n) {
	if (n === null || n === undefined || n === "") return "";
	const num = Number(n);
	if (Number.isNaN(num)) return String(n);
	return num.toLocaleString();
}

function formatHours(h) {
	if (h === null || h === undefined || h === "") return "";
	const num = Number(h);
	if (Number.isNaN(num)) return "";
	return `${Number(num.toFixed(2))}h`;
}

function safeHttpUrl(raw) {
	const trimmed = String(raw || "").trim();
	if (!trimmed) return "";
	try {
		const parsed = new URL(trimmed, window.location.origin);
		if (parsed.protocol === "http:" || parsed.protocol === "https:") {
			return parsed.href;
		}
	} catch (_err) {
		return "";
	}
	return "";
}

function isWaiterValue(val) {
	const s = String(val || "").trim().toLowerCase();
	return s === "waiter" || s === "waiting" || s === "yes" || s === "true";
}

function updateLiveTimers() {
	// Layer 2B In Bay elapsed timer
	const bayTimerEl = document.getElementById("ro-bay-timer");
	if (bayTimerEl && state.roData && state.roData.picked_up_at) {
		if (state.roData.vehicle_location === "In Bay / Being Serviced") {
			const elapsed = formatBayElapsed(state.roData.picked_up_at);
			if (elapsed) {
				bayTimerEl.textContent = `In Bay · ${elapsed}`;
			}
		}
	}

	// Layer 3 Andon blocked timer
	if (el.l3BlockedTimer) {
		const t = state.boardThread;
		if (!t || !t.blocked_since || t.board_status === "ACTIVE" || t.board_status === "CLOSED") {
			el.l3BlockedTimer.classList.add("is-hidden");
			el.l3BlockedTimer.textContent = "";
		} else {
			const elapsed = formatBlockedElapsed(t.blocked_since);
			if (elapsed) {
				el.l3BlockedTimer.textContent = elapsed;
				el.l3BlockedTimer.classList.remove("is-hidden");
			} else {
				el.l3BlockedTimer.classList.add("is-hidden");
			}
		}
	}
}

/* =========================================================================
   Transient Inline PIN-to-Drive Reveal (15s Auto-Mask, Zero Persistence)
   ========================================================================= */

function clearPinRevealState() {
	if (state.pinTimerId) {
		clearInterval(state.pinTimerId);
		state.pinTimerId = null;
	}
	state.pinRevealed = false;
	state.pinTargetKey = null;
	state.pinSecondsLeft = 0;
}

function renderInlinePinPill(slotEl, { kind, id, pinState, revealedPlaintext = null }) {
	if (!slotEl) return;
	slotEl.innerHTML = "";

	const shouldShow = Boolean(
		id && pinState && pinState.pin_feature_installed && pinState.has_pin && pinState.can_reveal
	);
	if (!shouldShow) {
		return;
	}

	const targetKey = `${kind}:${id}`;
	const isActiveReveal =
		state.pinRevealed && state.pinTargetKey === targetKey && Boolean(revealedPlaintext);

	const pill = document.createElement("span");
	pill.className = "pin-pill";

	const label = document.createElement("span");
	label.textContent = "PIN:";

	const codeEl = document.createElement("span");
	codeEl.className = "pin-pill__code";
	// Plaintext PIN is ONLY held transiently in this text node — NEVER stored in storage or DOM dataset
	codeEl.textContent = isActiveReveal ? String(revealedPlaintext) : "••••";

	pill.appendChild(label);
	pill.appendChild(codeEl);

	if (isActiveReveal) {
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
			renderInlinePinPill(slotEl, { kind, id, pinState, revealedPlaintext: null });
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
				const res =
					kind === "SCHEDULE_ENTRY"
						? await revealScheduleEntryPin(id)
						: await revealRoPin(id);
				const pinVal = res && (res.pin || res.plaintext_pin || res.pin_code);
				if (!res || !res.available || !pinVal) {
					showToast("PIN-to-Drive is not available for this job.", "warn");
					renderInlinePinPill(slotEl, { kind, id, pinState, revealedPlaintext: null });
					return;
				}
				const maskSecs = Number(res.auto_mask_seconds || PIN_AUTO_MASK_SECONDS);
				clearPinRevealState();
				state.pinRevealed = true;
				state.pinTargetKey = targetKey;
				state.pinSecondsLeft = maskSecs;
				renderInlinePinPill(slotEl, { kind, id, pinState, revealedPlaintext: pinVal });

				state.pinTimerId = setInterval(() => {
					state.pinSecondsLeft -= 1;
					const cdEl = slotEl.querySelector(".pin-pill__countdown");
					if (state.pinSecondsLeft <= 0) {
						clearPinRevealState();
						renderInlinePinPill(slotEl, { kind, id, pinState, revealedPlaintext: null });
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

	slotEl.appendChild(pill);
}

/* =========================================================================
   URL & 3-Layer Navigation
   ========================================================================= */

function applyLayerVisibility() {
	if (!el.shell) return;
	let layerAttr = "L1";
	if (state.activeView === "BOARD" && state.activeKind === "REPAIR_ORDER" && state.activeId) {
		layerAttr = "L3";
	} else if (state.activeView === "DETAIL" && state.activeId) {
		layerAttr = "L2";
	}
	el.shell.dataset.activeLayer = layerAttr;

	const showLayer3 = layerAttr === "L3";
	el.layer2.classList.toggle("is-hidden", showLayer3);
	el.layer3.classList.toggle("is-hidden", !showLayer3);
}

function buildUrlForState({ kind, id, view }) {
	const url = new URL(window.location.href);
	url.pathname = "/tech";
	url.searchParams.delete("ro");
	url.searchParams.delete("se");
	url.searchParams.delete("view");
	if (kind === "REPAIR_ORDER" && id) {
		url.searchParams.set("ro", id);
		if (view === "BOARD") {
			url.searchParams.set("view", "board");
		}
	} else if (kind === "SCHEDULE_ENTRY" && id) {
		url.searchParams.set("se", id);
	}
	return url.toString();
}

function syncUrl({ push = false } = {}) {
	const targetUrl = buildUrlForState({
		kind: state.activeKind,
		id: state.activeId,
		view: state.activeView,
	});
	const histState = {
		kind: state.activeKind,
		id: state.activeId,
		view: state.activeView,
	};
	if (push) {
		window.history.pushState(histState, "", targetUrl);
	} else {
		window.history.replaceState(histState, "", targetUrl);
	}
}

function parseRouteFromUrl() {
	const params = new URLSearchParams(window.location.search);
	const ro = (params.get("ro") || "").trim();
	const se = (params.get("se") || "").trim();
	const viewParam = (params.get("view") || "").trim().toLowerCase();

	if (ro) {
		return {
			kind: "REPAIR_ORDER",
			id: ro,
			view: viewParam === "board" ? "BOARD" : "DETAIL",
		};
	}
	if (se) {
		return {
			kind: "SCHEDULE_ENTRY",
			id: se,
			view: "DETAIL",
		};
	}

	const pathMatch = window.location.pathname.match(/^\/tech\/([^/]+)$/);
	if (pathMatch && pathMatch[1]) {
		const decoded = decodeURIComponent(pathMatch[1]).trim();
		if (decoded) {
			return {
				kind: "REPAIR_ORDER",
				id: decoded,
				view: viewParam === "board" ? "BOARD" : "DETAIL",
			};
		}
	}

	return { kind: null, id: null, view: "LIST" };
}

function navigateToList({ pushHistory = true } = {}) {
	clearPinRevealState();
	state.activeView = "LIST";
	if (!isDesktopLayout()) {
		state.activeKind = null;
		state.activeId = null;
	}
	applyLayerVisibility();
	renderJobsList();
	syncUrl({ push: pushHistory });
}

/* =========================================================================
   Layer 1: My Assigned Jobs Queue
   ========================================================================= */

function getJobKey(job) {
	return `${job.kind || "REPAIR_ORDER"}:${job.id || job.name || ""}`;
}

function getFilteredJobs() {
	const q = state.searchQuery.trim().toLowerCase();
	if (!q) return state.jobs;
	return state.jobs.filter((job) => {
		const hay = [
			job.id,
			job.name,
			job.vehicle_display,
			job.license_plate,
			job.vin,
			job.bay,
			job.customer_name,
			job.entry_type,
		]
			.filter(Boolean)
			.join(" ")
			.toLowerCase();
		return hay.includes(q);
	});
}

function buildJobCard(job) {
	const kind = job.kind || "REPAIR_ORDER";
	const id = job.id || job.name || "";
	const isSelected = state.activeKind === kind && state.activeId === id;

	let accentCls = "job-card--ready-lot";
	let pillCls = "tech-pill--ready-lot";
	let pillLabel = "READY IN LOT";

	if (kind === "SCHEDULE_ENTRY") {
		accentCls = "job-card--diagnostic";
		pillCls = "tech-pill--diagnostic";
		pillLabel = (job.entry_type || "DIAGNOSTIC").toUpperCase();
	} else {
		const approved = Number(job.approved_count || 0);
		const completed = Number(job.completed_count || 0);
		const lifecycle = job.lifecycle_state || "";
		if (lifecycle === "COMPLETED" || state.tab === "COMPLETED") {
			accentCls = "job-card--completed";
			pillCls = "tech-pill--done";
			pillLabel = "COMPLETED";
		} else if (lifecycle === "READY_FOR_DROPOFF" || (approved > 0 && completed >= approved)) {
			accentCls = "job-card--done";
			pillCls = "tech-pill--done";
			pillLabel = `${completed}/${approved} DONE`;
		} else if (
			lifecycle === "IN_BAY_WORKING" ||
			job.vehicle_location === "In Bay / Being Serviced"
		) {
			accentCls = "job-card--in-bay";
			pillCls = "tech-pill--in-bay";
			pillLabel = "IN BAY";
		}
	}

	const btn = document.createElement("button");
	btn.type = "button";
	btn.className = `job-card ${accentCls}${isSelected ? " is-selected" : ""}`;
	btn.dataset.jobKind = kind;
	btn.dataset.jobId = id;

	const openIssues = Number(job.open_exception_count || 0);
	const hasPin = Boolean(job.has_pin);
	const vehicleDisplay = job.vehicle_display || id;

	const topMetaParts = [];
	if (job.bay) topMetaParts.push(job.bay);
	if (job.scheduled_time) topMetaParts.push(formatScheduleTime(job.scheduled_time));

	const subMetaParts = [];
	if (job.license_plate) subMetaParts.push(`Plate ${job.license_plate}`);
	subMetaParts.push(id);
	if (kind === "SCHEDULE_ENTRY" && isWaiterValue(job.waiter_preference)) {
		subMetaParts.push("Waiter");
	}

	let footerHtml = "";
	if (kind === "SCHEDULE_ENTRY") {
		footerHtml = `
			<div class="job-card__footer">
				<span>Scheduled diagnostic · No RO yet</span>
				${
					isWaiterValue(job.waiter_preference)
						? `<span class="tech-pill tech-pill--waiter">WAITER</span>`
						: ""
				}
			</div>
		`;
	} else {
		const approved = Number(job.approved_count || 0);
		const completed = Number(job.completed_count || 0);
		const pct =
			job.progress_percent !== undefined && job.progress_percent !== null
				? Math.max(0, Math.min(100, Math.round(Number(job.progress_percent))))
				: approved > 0
				? Math.round((completed / approved) * 100)
				: 0;
		footerHtml = `
			<div class="job-card__progress" aria-hidden="true">
				<div class="job-card__progress-fill" style="width:${pct}%"></div>
			</div>
			<div class="job-card__footer">
				<span>${pct}%</span>
				<span>${completed} / ${approved} tasks</span>
			</div>
		`;
	}

	btn.innerHTML = `
		${isSelected ? `<div class="job-card__selected-tag">SELECTED</div>` : ""}
		<div class="job-card__top">
			<div class="job-card__top-left">
				<span class="tech-pill ${pillCls}">${escapeHtml(pillLabel)}</span>
				${
					topMetaParts.length
						? `<span class="job-card__meta">${escapeHtml(topMetaParts.join(" · "))}</span>`
						: ""
				}
			</div>
			<div class="job-card__top-right">
				${hasPin ? `<span class="tech-pill tech-pill--pin">PIN</span>` : ""}
				${openIssues > 0 ? `<span class="tech-pill tech-pill--issue" title="${openIssues} open exception(s)">${openIssues}</span>` : ""}
			</div>
		</div>
		<div class="job-card__vehicle">${escapeHtml(vehicleDisplay)}</div>
		<div class="job-card__meta">${escapeHtml(subMetaParts.join(" · "))}</div>
		${footerHtml}
	`;

	btn.addEventListener("click", () => {
		if (kind === "SCHEDULE_ENTRY") {
			openScheduleEntry(id, { pushHistory: true });
		} else {
			openRepairOrder(id, { view: "DETAIL", pushHistory: true });
		}
	});

	return btn;
}

function renderJobsList() {
	if (el.tabActiveCount) el.tabActiveCount.textContent = String(state.activeCount);
	if (el.tabCompletedCount) el.tabCompletedCount.textContent = String(state.completedCount);
	if (el.totalCount) {
		const count = state.tab === "COMPLETED" ? state.completedCount : state.activeCount;
		el.totalCount.textContent = `(${count})`;
	}

	if (el.tabActive && el.tabCompleted) {
		el.tabActive.classList.toggle("is-active", state.tab === "ACTIVE");
		el.tabActive.setAttribute("aria-selected", String(state.tab === "ACTIVE"));
		el.tabCompleted.classList.toggle("is-active", state.tab === "COMPLETED");
		el.tabCompleted.setAttribute("aria-selected", String(state.tab === "COMPLETED"));
	}

	if (!el.jobsList) return;
	el.jobsList.innerHTML = "";

	if (ACTOR_ROLE === "TECH" && !state.employeeLinked) {
		el.jobsList.innerHTML = `
			<div class="tech-onboarding-card" role="status">
				<h3>Employee Profile Not Linked</h3>
				<p>No Employee profile linked to your login — ask a manager to link your User account.</p>
			</div>
		`;
		return;
	}

	const filtered = getFilteredJobs();
	if (!filtered.length) {
		const msg = state.searchQuery
			? "No assigned jobs match your search."
			: state.tab === "COMPLETED"
			? "No recently completed jobs."
			: "No active assigned jobs right now. Approved Repair Orders and Diagnostic appointments assigned to you appear here automatically.";
		el.jobsList.innerHTML = `<div class="tech-jobs-empty">${escapeHtml(msg)}</div>`;
		return;
	}

	const frag = document.createDocumentFragment();
	for (const job of filtered) {
		frag.appendChild(buildJobCard(job));
	}
	el.jobsList.appendChild(frag);
}

async function refreshJobsQueue({ silent = false } = {}) {
	if (state.isLoadingJobs) return;
	state.isLoadingJobs = true;
	try {
		const res = await listMyJobs({
			include_completed: state.tab === "COMPLETED",
			limit: 50,
		});
		if (res) {
			if (res.employee_linked !== undefined) {
				state.employeeLinked = Boolean(res.employee_linked);
			}
			state.activeCount = Number(res.active_count ?? state.activeCount);
			state.completedCount = Number(res.completed_count ?? state.completedCount);
			state.jobs = Array.isArray(res.jobs) ? res.jobs : [];
			renderJobsList();
		}
	} catch (err) {
		if (!silent) {
			showToast(err.message || "Failed to refresh assigned jobs.", "error");
		}
	} finally {
		state.isLoadingJobs = false;
	}
}

/* =========================================================================
   Layer 2A: Unlinked Diagnostic Schedule Entry
   ========================================================================= */

function renderScheduleEntryView() {
	const se = state.seData;
	if (!se || !el.seView) return;

	el.l2Empty.classList.add("is-hidden");
	el.l2Loading.classList.add("is-hidden");
	el.roView.classList.add("is-hidden");
	el.seView.classList.remove("is-hidden");

	const entryType = (se.entry_type || "DIAGNOSTIC").toUpperCase();
	const isWaiter = isWaiterValue(se.waiter_preference);
	const dateLabel = formatScheduleDate(se.scheduled_date);
	const timeLabel = formatScheduleTime(se.scheduled_time);
	const durLabel = se.estimated_duration ? `(${se.estimated_duration} min)` : "";
	const dateTimeChip = [dateLabel, timeLabel].filter(Boolean).join(" · ") + (durLabel ? ` ${durLabel}` : "");

	const bookedItems = Array.isArray(se.booked_packages) ? se.booked_packages : [];
	const notesText = (se.notes || "").trim();
	const itemsSummaryText = (se.items_summary || "").trim();

	el.seView.innerHTML = `
		<div class="tech-mobile-subbar">
			<button type="button" class="btn btn-sm btn-ghost js-back-to-jobs">&larr; My Jobs</button>
			<span class="tech-mobile-subbar__id">${escapeHtml(se.name)}</span>
			<span></span>
		</div>

		<section class="tech-hero tech-hero--diagnostic">
			<div class="tech-hero__badges">
				<span class="tech-pill tech-pill--diagnostic">${escapeHtml(entryType)}</span>
				${se.status ? `<span class="tech-pill tech-pill--status">${escapeHtml(se.status)}</span>` : ""}
				${isWaiter ? `<span class="tech-pill tech-pill--waiter">WAITER</span>` : ""}
			</div>

			<div class="tech-hero__top">
				<div class="tech-hero__vehicle-row">
					<h2 class="tech-hero__vehicle-title">${escapeHtml(se.vehicle_display || se.name)}</h2>
					${se.license_plate ? `<span class="plate-badge">${escapeHtml(se.license_plate)}</span>` : ""}
				</div>
				<div class="tech-hero__actions">
					<span id="se-pin-slot" class="pin-pill-slot"></span>
				</div>
			</div>

			<div class="tech-hero__meta">
				<span class="tech-meta-chip">${escapeHtml(se.name)}</span>
				${se.bay ? `<span class="tech-meta-chip">${escapeHtml(se.bay)}</span>` : ""}
				${dateTimeChip.trim() ? `<span class="tech-meta-chip">${escapeHtml(dateTimeChip.trim())}</span>` : ""}
				${se.customer_name ? `<span class="tech-meta-chip">Customer: ${escapeHtml(se.customer_name)}</span>` : ""}
				${se.vin ? `<span class="tech-meta-chip">VIN: ${escapeHtml(se.vin)}</span>` : ""}
			</div>
		</section>

		<div class="tech-se-notice" role="note">
			<span class="tech-info-icon" aria-hidden="true">i</span>
			<span>
				<strong>Pre-RO Diagnostic Appointment</strong> &mdash; Once a Repair Order is created and approved,
				this card automatically switches to the full Repair Order execution view.
			</span>
		</div>

		<section class="tech-card">
			<h3 class="tech-card__heading">Reported Concern &amp; Appointment Notes</h3>
			<p class="tech-card__body">${escapeHtml(notesText || "No customer concern or appointment notes recorded yet.")}</p>
		</section>

		<section class="tech-card">
			<h3 class="tech-card__heading">Requested Diagnostic / Booked Items</h3>
			${
				bookedItems.length
					? `<ul class="tech-booked-list">${bookedItems
							.map((pkg) => `<li>${escapeHtml(typeof pkg === "string" ? pkg : pkg.title || "")}</li>`)
							.join("")}</ul>`
					: itemsSummaryText
					? `<p class="tech-card__body">${escapeHtml(itemsSummaryText)}</p>`
					: `<p class="tech-card__body">No diagnostic packages listed.</p>`
			}
		</section>
	`;

	const backBtn = el.seView.querySelector(".js-back-to-jobs");
	if (backBtn) {
		backBtn.addEventListener("click", () => navigateToList({ pushHistory: true }));
	}

	const pinSlot = document.getElementById("se-pin-slot");
	renderInlinePinPill(pinSlot, {
		kind: "SCHEDULE_ENTRY",
		id: se.name,
		pinState: state.sePinState,
		revealedPlaintext: null,
	});
}

async function openScheduleEntry(seId, { pushHistory = true } = {}) {
	if (!seId) return;
	clearPinRevealState();
	state.activeKind = "SCHEDULE_ENTRY";
	state.activeId = seId;
	state.activeView = "DETAIL";
	state.roData = null;
	state.seData = null;
	state.sePinState = null;
	state.isLoadingDetail = true;

	applyLayerVisibility();
	renderJobsList();
	syncUrl({ push: pushHistory });

	el.l2Empty.classList.add("is-hidden");
	el.roView.classList.add("is-hidden");
	el.seView.classList.add("is-hidden");
	el.l2Loading.classList.remove("is-hidden");
	if (el.l2LoadingText) el.l2LoadingText.textContent = `Loading ${seId}...`;

	try {
		const res = await getTechScheduleEntry(seId);
		if (state.activeKind !== "SCHEDULE_ENTRY" || state.activeId !== seId) return;

		if (res && res.superseded_by_ro) {
			await refreshJobsQueue({ silent: true });
			if (res.ro_eligible) {
				showToast(`Appointment upgraded to ${res.superseded_by_ro}.`, "info");
				await openRepairOrder(res.superseded_by_ro, { view: "DETAIL", pushHistory: false });
			} else {
				showToast(
					`Appointment linked to ${res.superseded_by_ro} (awaiting approval / parts readiness).`,
					"info"
				);
				state.activeKind = null;
				state.activeId = null;
				state.activeView = "LIST";
				el.l2Loading.classList.add("is-hidden");
				el.l2Empty.classList.remove("is-hidden");
				applyLayerVisibility();
				renderJobsList();
				syncUrl({ push: false });
			}
			return;
		}

		state.seData = res.schedule_entry || null;
		state.sePinState = res.pin_state || null;
		renderScheduleEntryView();
	} catch (err) {
		showToast(err.message || `Unable to open ${seId}.`, "error");
		el.l2Loading.classList.add("is-hidden");
		el.l2Empty.classList.remove("is-hidden");
		if (err.status === 403 || err.status === 404) {
			await refreshJobsQueue({ silent: true });
			navigateToList({ pushHistory: false });
		}
	} finally {
		state.isLoadingDetail = false;
	}
}

/* =========================================================================
   Layer 2B: Approved Repair Order Execution
   ========================================================================= */

function validateOdometerInput(rawValue, odometerIn) {
	const trimmed = String(rawValue ?? "").trim();
	if (!trimmed) {
		return { ok: false, error: "Odometer Out (km) is required before parking in the lot." };
	}
	if (!/^\d+$/.test(trimmed)) {
		return { ok: false, error: "Enter a whole number for Odometer Out (km)." };
	}
	const parsed = parseInt(trimmed, 10);
	const minOdo = Number(odometerIn || 0);
	if (minOdo > 0 && parsed < minOdo) {
		return {
			ok: false,
			error: `Odometer Out must be at least ${formatNumber(minOdo)} km (Odometer In).`,
		};
	}
	return { ok: true, value: parsed, error: "" };
}

function buildLifecycleBannerHtml(ro) {
	const lifecycle = ro.lifecycle_state || "READY_FOR_PICKUP";
	const approvedCount = Number(ro.approved_count || 0);
	const minOdo = Number(ro.odometer_in || 0);

	if (lifecycle === "READY_FOR_PICKUP") {
		return `
			<section class="lifecycle-banner lifecycle-banner--pickup" id="ro-lifecycle-banner">
				<div class="lifecycle-banner__eyebrow">Vehicle is in Customer Lot · Ready for Bay</div>
				<button
					type="button"
					class="btn btn-primary lifecycle-btn-primary js-pickup-btn"
					${state.isMutatingLifecycle ? "disabled" : ""}
				>
					${state.isMutatingLifecycle ? "Moving into Bay..." : "Pick Up Car (Into Bay)"}
				</button>
			</section>
		`;
	}

	if (lifecycle === "READY_FOR_DROPOFF") {
		const hasErr = Boolean(state.dropOffOdoError);
		return `
			<section class="lifecycle-banner lifecycle-banner--dropoff" id="ro-lifecycle-banner">
				<div class="lifecycle-dropoff-grid">
					<div class="lifecycle-dropoff-info">
						<span class="lifecycle-check-circle" aria-hidden="true">&#10003;</span>
						<div>
							<h3 class="lifecycle-dropoff-title">All ${approvedCount} Approved Service${approvedCount === 1 ? "" : "s"} Completed</h3>
							<p class="lifecycle-dropoff-sub">Odometer reading is required before parking vehicle in the customer lot.</p>
						</div>
					</div>
					<div class="lifecycle-dropoff-controls">
						<label class="odo-field">
							<span class="odo-field__label">Odometer Out (km) *</span>
							<input
								type="number"
								inputmode="numeric"
								id="dropoff-odo-input"
								class="odo-field__input${hasErr ? " is-invalid" : ""}"
								min="${minOdo}"
								step="1"
								placeholder="${minOdo > 0 ? `Min ${minOdo}` : "Enter km"}"
								value="${escapeHtml(state.dropOffOdoDraft)}"
								required
							/>
							${hasErr ? `<span class="odo-field__error">${escapeHtml(state.dropOffOdoError)}</span>` : ""}
						</label>
						<button
							type="button"
							class="lifecycle-btn-emerald js-dropoff-finish-btn"
							${state.isMutatingLifecycle ? "disabled" : ""}
						>
							${state.isMutatingLifecycle ? "Finishing..." : "Drop Off Car in Lot & Finish"}
						</button>
					</div>
				</div>
			</section>
		`;
	}

	if (lifecycle === "COMPLETED") {
		const odoOutText = ro.odometer_out ? `${formatNumber(ro.odometer_out)} km` : "Recorded";
		const bayHrsText = ro.total_bay_hours ? `${Number(ro.total_bay_hours).toFixed(2)}h total bay time` : "";
		return `
			<section class="lifecycle-banner lifecycle-banner--completed" id="ro-lifecycle-banner">
				<div class="lifecycle-dropoff-info">
					<span class="lifecycle-check-circle" aria-hidden="true">&#10003;</span>
					<div>
						<h3 class="lifecycle-dropoff-title">Work Completed · Parked in Customer Lot</h3>
						<p class="lifecycle-dropoff-sub">
							Odometer Out: <strong>${escapeHtml(odoOutText)}</strong>
							${bayHrsText ? ` · ${escapeHtml(bayHrsText)}` : ""}
						</p>
					</div>
				</div>
			</section>
		`;
	}

	// IN_BAY_WORKING — progress bar sits right below hero, no extra clutter banner needed
	return "";
}

function buildServiceCardHtml(svc, { isCompletedRo }) {
	const checked = Boolean(svc.is_completed);
	const isToggling = state.togglingServices.has(svc.name);
	const parts = Array.isArray(svc.parts) ? svc.parts : [];
	const hoursLabel = formatHours(svc.labor_hours);
	const pos = (svc.service_position || "").trim();
	const showSide = pos && ["LH", "RH", "Front", "Rear", "FL", "FR", "RL", "RR"].includes(pos);

	const doneTime = checked && svc.completed_at ? `Done ${formatShortTime(svc.completed_at)}` : "";
	const metaBits = [];
	if (doneTime) metaBits.push(doneTime);
	if (hoursLabel) metaBits.push(hoursLabel);

	let partsHtml = "";
	if (parts.length > 0) {
		partsHtml = `<div class="service-card__parts">${parts
			.map((p) => {
				const code = p.item_code ? `${p.item_code} ` : "";
				const name = p.item_name || "";
				const qty = p.qty !== undefined && p.qty !== null ? ` ×${p.qty}` : "";
				return `<span class="part-chip">${escapeHtml(`${code}${name}${qty}`.trim())}</span>`;
			})
			.join("")}</div>`;
	} else {
		partsHtml = `<div class="service-card__sub">Labor only</div>`;
	}

	const safeManualUrl = safeHttpUrl(svc.tesla_manual_url);
	return `
		<article class="service-card${checked ? " is-completed" : ""}" data-service-id="${escapeHtml(svc.name)}">
			<button
				type="button"
				class="service-check-btn${checked ? " is-checked" : ""}"
				role="checkbox"
				aria-checked="${checked ? "true" : "false"}"
				aria-label="${checked ? "Mark incomplete:" : "Mark complete:"} ${escapeHtml(svc.service_title || "Service")}"
				data-service-id="${escapeHtml(svc.name)}"
				${isToggling || isCompletedRo ? "disabled" : ""}
			>
				&#10003;
			</button>
			<div class="service-card__content">
				<div class="service-card__top">
					<div class="service-card__title-wrap">
						<span class="service-card__title">${escapeHtml(svc.service_title || svc.name)}</span>
						${showSide ? `<span class="tech-pill tech-pill--side">${escapeHtml(pos)}</span>` : ""}
						${svc.is_supplement ? `<span class="tech-pill tech-pill--supplement">Supplement</span>` : ""}
					</div>
					${hoursLabel && !doneTime ? `<span class="service-card__hours">${escapeHtml(hoursLabel)}</span>` : ""}
				</div>
				${metaBits.length ? `<div class="service-card__sub">${escapeHtml(metaBits.join(" · "))}</div>` : ""}
				${partsHtml}
				${
					svc.advisor_notes
						? `<div class="service-card__notes"><strong>Advisor:</strong> ${escapeHtml(svc.advisor_notes)}</div>`
						: ""
				}
				${
					safeManualUrl
						? `<div class="service-card__links">
							<a class="service-manual-link" href="${escapeHtml(safeManualUrl)}" target="_blank" rel="noopener noreferrer">
								Tesla Manual &#8599;
							</a>
						</div>`
						: ""
				}
			</div>
		</article>
	`;
}

function buildInspectionSectionHtml(ro) {
	const items = Array.isArray(ro.inspection_items) ? ro.inspection_items : [];
	if (!items.length) return "";

	const passedCount = items.filter((i) => i.status === "Pass").length;
	const arrow = state.inspectionExpanded ? "&#9662;" : "&#9656;";
	const statuses = ["Pass", "Attention", "Fail", "N/A"];

	let listHtml = "";
	if (state.inspectionExpanded) {
		listHtml = `
			<div class="inspection-list">
				${items
					.map(
						(item) => `
					<div class="inspection-item" data-insp-id="${escapeHtml(item.name)}">
						<div class="inspection-item__desc">${escapeHtml(item.inspection_description || item.name)}</div>
						<div class="inspection-status-row">
							${statuses
								.map(
									(st) => `
								<button
									type="button"
									class="insp-pill${item.status === st ? " is-active" : ""}"
									data-insp-id="${escapeHtml(item.name)}"
									data-insp-status="${escapeHtml(st)}"
								>
									${escapeHtml(st)}
								</button>
							`
								)
								.join("")}
						</div>
						<div class="inspection-note-row">
							<input
								type="text"
								class="inspection-note-input"
								data-insp-note-id="${escapeHtml(item.name)}"
								placeholder="Optional inspection note..."
								value="${escapeHtml(item.notes || "")}"
							/>
							<button
								type="button"
								class="btn btn-sm btn-ghost js-save-insp-note"
								data-insp-id="${escapeHtml(item.name)}"
							>
								Save
							</button>
						</div>
					</div>
				`
					)
					.join("")}
			</div>
		`;
	}

	return `
		<button type="button" class="inspection-toggle-btn js-toggle-inspection" aria-expanded="${state.inspectionExpanded ? "true" : "false"}">
			<span aria-hidden="true">${arrow}</span>
			<span>Intake Inspection (${passedCount}/${items.length} Passed)</span>
		</button>
		${listHtml}
	`;
}

function renderRepairOrderView() {
	const ro = state.roData;
	if (!ro || !el.roView) return;

	el.l2Empty.classList.add("is-hidden");
	el.l2Loading.classList.add("is-hidden");
	el.seView.classList.add("is-hidden");
	el.roView.classList.remove("is-hidden");

	const ex = state.roExceptionSummary || {};
	const openIssues = Number(ex.open_action_count || 0);
	const msgCount = Number(ex.message_count || 0);
	const badgeCount = openIssues > 0 ? openIssues : msgCount;
	const hasAlert = openIssues > 0;
	const exceptionBtnLabel = msgCount > 0 || openIssues > 0 ? "Exception Board" : "Report Issue";

	const isInBay = ro.vehicle_location === "In Bay / Being Serviced";
	const isCompletedRo = ro.lifecycle_state === "COMPLETED";
	const bayElapsed = isInBay && ro.picked_up_at ? formatBayElapsed(ro.picked_up_at) : "";
	const approvedCount = Number(ro.approved_count || (ro.services ? ro.services.length : 0));
	const completedCount = Number(ro.completed_count || 0);
	const progressPct =
		ro.progress_percent !== undefined && ro.progress_percent !== null
			? Math.max(0, Math.min(100, Math.round(Number(ro.progress_percent))))
			: approvedCount > 0
			? Math.round((completedCount / approvedCount) * 100)
			: 0;

	const services = Array.isArray(ro.services) ? ro.services : [];
	const pendingServices = Array.isArray(ro.pending_services) ? ro.pending_services : [];
	const declinedCount = Number(ro.declined_count || 0);
	const inspectionItems = Array.isArray(ro.inspection_items) ? ro.inspection_items : [];
	const minOdo = Number(ro.odometer_in || 0);

	const exceptionButtonHtml = (extraCls = "") => `
		<button type="button" class="tech-exception-btn${hasAlert ? " tech-exception-btn--alert" : ""} ${extraCls} js-open-exception-board">
			<span>${escapeHtml(exceptionBtnLabel)}</span>
			${badgeCount > 0 ? `<span class="tech-pill ${hasAlert ? "tech-pill--issue" : "tech-pill--status"}">${badgeCount}</span>` : ""}
		</button>
	`;

	const pendingNoticeHtml =
		pendingServices.length > 0 || declinedCount > 0
			? `
			<details class="tech-pending-details">
				<summary>
					${pendingServices.length > 0 ? `${pendingServices.length} Pending Supplement${pendingServices.length === 1 ? "" : "s"} (Awaiting Customer Approval)` : ""}
					${pendingServices.length > 0 && declinedCount > 0 ? " · " : ""}
					${declinedCount > 0 ? `${declinedCount} Declined Service${declinedCount === 1 ? "" : "s"}` : ""}
				</summary>
				${
					pendingServices.length > 0
						? `<ul class="tech-pending-list">${pendingServices
								.map((ps) => `<li>${escapeHtml(ps.service_title || ps.title || ps.name || "Pending service")}</li>`)
								.join("")}</ul>`
						: ""
				}
			</details>
		`
			: "";

	const tempParkPanelHtml =
		isInBay && state.tempParkOpen
			? `
			<div class="temp-park-panel" id="temp-park-panel">
				<h4 class="temp-park-panel__title">Park Temporarily in Customer Lot</h4>
				<p class="temp-park-panel__sub">
					Enter current Odometer Out (km) before moving this vehicle back to the lot. Your RO stays active so you can resume later.
				</p>
				<div class="temp-park-panel__row">
					<label class="odo-field">
						<span class="odo-field__label">Odometer Out (km) *</span>
						<input
							type="number"
							inputmode="numeric"
							id="temp-park-odo-input"
							class="odo-field__input${state.tempParkOdoError ? " is-invalid" : ""}"
							min="${minOdo}"
							step="1"
							placeholder="${minOdo > 0 ? `Min ${minOdo}` : "Enter km"}"
							value="${escapeHtml(state.tempParkOdoDraft)}"
							required
						/>
						${state.tempParkOdoError ? `<span class="odo-field__error">${escapeHtml(state.tempParkOdoError)}</span>` : ""}
					</label>
					<button type="button" class="btn btn-amber js-confirm-temp-park" ${state.isMutatingLifecycle ? "disabled" : ""}>
						${state.isMutatingLifecycle ? "Parking..." : "Confirm Park in Lot"}
					</button>
					<button type="button" class="btn btn-ghost js-cancel-temp-park">Cancel</button>
				</div>
			</div>
		`
			: "";

	const footerBarHtml =
		inspectionItems.length > 0 || isInBay
			? `
			<section class="tech-footer-bar">
				<div class="tech-footer-row">
					<div>${inspectionItems.length > 0 ? buildInspectionSectionHtml(ro) : ""}</div>
					${
						isInBay
							? `<button type="button" class="btn btn-ghost js-toggle-temp-park">
								Park Temporarily in Lot (Odometer Required)
							</button>`
							: ""
					}
				</div>
				${tempParkPanelHtml}
			</section>
		`
			: "";

	el.roView.innerHTML = `
		<div class="tech-mobile-subbar">
			<button type="button" class="btn btn-sm btn-ghost js-back-to-jobs">&larr; My Jobs</button>
			<span class="tech-mobile-subbar__id">${escapeHtml(ro.name)}</span>
			${exceptionButtonHtml("btn-sm")}
		</div>

		<section class="tech-hero tech-hero--ro">
			<div class="tech-hero__top">
				<div class="tech-hero__vehicle-row">
					<h2 class="tech-hero__vehicle-title">${escapeHtml(ro.vehicle_display || ro.name)}</h2>
					${ro.license_plate ? `<span class="plate-badge">${escapeHtml(ro.license_plate)}</span>` : ""}
				</div>
				<div class="tech-hero__actions">
					<span id="ro-pin-slot" class="pin-pill-slot"></span>
					${exceptionButtonHtml("tech-exception-btn--desktop")}
				</div>
			</div>

			<div class="tech-hero__meta">
				<span class="tech-meta-chip">${escapeHtml(ro.name)}</span>
				${ro.bay ? `<span class="tech-meta-chip">${escapeHtml(ro.bay)}</span>` : ""}
				${
					isInBay
						? `<span id="ro-bay-timer" class="tech-meta-chip tech-meta-chip--timer">In Bay${bayElapsed ? ` · ${escapeHtml(bayElapsed)}` : ""}</span>`
						: ro.vehicle_location
						? `<span class="tech-meta-chip">${escapeHtml(ro.vehicle_location)}</span>`
						: ""
				}
				${ro.odometer_in ? `<span class="tech-meta-chip">Odo In: ${escapeHtml(formatNumber(ro.odometer_in))} km</span>` : ""}
				${ro.customer_name ? `<span class="tech-meta-chip">${escapeHtml(ro.customer_name)}</span>` : ""}
			</div>
		</section>

		${
			ro.customer_concern
				? `<section class="tech-concern-card"><strong>Customer Concern:</strong> ${escapeHtml(ro.customer_concern)}</section>`
				: ""
		}

		${buildLifecycleBannerHtml(ro)}

		<section class="checklist-section">
			<div class="checklist-header">
				<h3 class="checklist-header__title">
					Service Checklist &mdash; ${completedCount} of ${approvedCount} Completed (${progressPct}%)
				</h3>
				<span class="checklist-header__pct">${progressPct}%</span>
			</div>
			<div class="checklist-progress" aria-hidden="true">
				<div class="checklist-progress__fill${progressPct >= 100 ? " is-complete" : ""}" style="width:${progressPct}%"></div>
			</div>
			<div class="service-list">
				${
					services.length
						? services.map((svc) => buildServiceCardHtml(svc, { isCompletedRo })).join("")
						: `<div class="tech-card"><p class="tech-card__body">No approved services listed on this Repair Order.</p></div>`
				}
			</div>
		</section>

		${pendingNoticeHtml}
		${footerBarHtml}
	`;

	// Bind events inside Layer 2B
	const backBtn = el.roView.querySelector(".js-back-to-jobs");
	if (backBtn) {
		backBtn.addEventListener("click", () => navigateToList({ pushHistory: true }));
	}

	for (const exBtn of el.roView.querySelectorAll(".js-open-exception-board")) {
		exBtn.addEventListener("click", () => openExceptionBoard({ pushHistory: true }));
	}

	const pickupBtn = el.roView.querySelector(".js-pickup-btn");
	if (pickupBtn) {
		pickupBtn.addEventListener("click", () => handlePickUpVehicle());
	}

	const dropoffInput = el.roView.querySelector("#dropoff-odo-input");
	if (dropoffInput) {
		dropoffInput.addEventListener("input", (evt) => {
			state.dropOffOdoDraft = evt.target.value;
			if (state.dropOffOdoError) {
				state.dropOffOdoError = "";
				dropoffInput.classList.remove("is-invalid");
				const errEl = dropoffInput.parentElement && dropoffInput.parentElement.querySelector(".odo-field__error");
				if (errEl) errEl.remove();
			}
		});
	}

	const dropoffFinishBtn = el.roView.querySelector(".js-dropoff-finish-btn");
	if (dropoffFinishBtn) {
		dropoffFinishBtn.addEventListener("click", () => handleDropOffVehicle({ temporary: false }));
	}

	for (const checkBtn of el.roView.querySelectorAll(".service-check-btn")) {
		checkBtn.addEventListener("click", () => {
			const rowId = checkBtn.dataset.serviceId;
			if (rowId) handleToggleService(rowId);
		});
	}

	const inspToggle = el.roView.querySelector(".js-toggle-inspection");
	if (inspToggle) {
		inspToggle.addEventListener("click", () => {
			state.inspectionExpanded = !state.inspectionExpanded;
			renderRepairOrderView();
		});
	}

	for (const pill of el.roView.querySelectorAll(".insp-pill")) {
		pill.addEventListener("click", () => {
			const itemId = pill.dataset.inspId;
			const nextStatus = pill.dataset.inspStatus;
			if (itemId && nextStatus) {
				const noteInput = el.roView.querySelector(`[data-insp-note-id="${CSS.escape(itemId)}"]`);
				handleUpdateInspection(itemId, nextStatus, noteInput ? noteInput.value : null);
			}
		});
	}

	for (const saveBtn of el.roView.querySelectorAll(".js-save-insp-note")) {
		saveBtn.addEventListener("click", () => {
			const itemId = saveBtn.dataset.inspId;
			const item = inspectionItems.find((i) => i.name === itemId);
			const noteInput = el.roView.querySelector(`[data-insp-note-id="${CSS.escape(itemId)}"]`);
			if (itemId) {
				handleUpdateInspection(itemId, (item && item.status) || "Pass", noteInput ? noteInput.value : "");
			}
		});
	}

	const tempParkToggle = el.roView.querySelector(".js-toggle-temp-park");
	if (tempParkToggle) {
		tempParkToggle.addEventListener("click", () => {
			state.tempParkOpen = !state.tempParkOpen;
			state.tempParkOdoError = "";
			renderRepairOrderView();
			if (state.tempParkOpen) {
				const input = document.getElementById("temp-park-odo-input");
				if (input) input.focus();
			}
		});
	}

	const tempParkInput = el.roView.querySelector("#temp-park-odo-input");
	if (tempParkInput) {
		tempParkInput.addEventListener("input", (evt) => {
			state.tempParkOdoDraft = evt.target.value;
			if (state.tempParkOdoError) {
				state.tempParkOdoError = "";
				tempParkInput.classList.remove("is-invalid");
				const errEl = tempParkInput.parentElement && tempParkInput.parentElement.querySelector(".odo-field__error");
				if (errEl) errEl.remove();
			}
		});
	}

	const confirmTempParkBtn = el.roView.querySelector(".js-confirm-temp-park");
	if (confirmTempParkBtn) {
		confirmTempParkBtn.addEventListener("click", () => handleDropOffVehicle({ temporary: true }));
	}

	const cancelTempParkBtn = el.roView.querySelector(".js-cancel-temp-park");
	if (cancelTempParkBtn) {
		cancelTempParkBtn.addEventListener("click", () => {
			state.tempParkOpen = false;
			state.tempParkOdoError = "";
			renderRepairOrderView();
		});
	}

	const pinSlot = document.getElementById("ro-pin-slot");
	renderInlinePinPill(pinSlot, {
		kind: "REPAIR_ORDER",
		id: ro.name,
		pinState: state.roPinState,
		revealedPlaintext: null,
	});
}

function applyRoPayload(res) {
	if (!res || !res.repair_order) return;
	state.roData = res.repair_order;
	state.roPinState = res.pin_state || null;
	state.roExceptionSummary = res.exception_summary || null;

	// Sync matching card in Layer 1 rail immediately
	const ro = state.roData;
	const idx = state.jobs.findIndex((j) => j.kind === "REPAIR_ORDER" && (j.id === ro.name || j.name === ro.name));
	if (idx >= 0) {
		state.jobs[idx] = {
			...state.jobs[idx],
			lifecycle_state: ro.lifecycle_state,
			vehicle_location: ro.vehicle_location,
			approved_count: ro.approved_count,
			completed_count: ro.completed_count,
			progress_percent: ro.progress_percent,
			open_exception_count: state.roExceptionSummary ? state.roExceptionSummary.open_action_count : state.jobs[idx].open_exception_count,
		};
		renderJobsList();
	}
}

async function openRepairOrder(roId, { view = "DETAIL", pushHistory = true } = {}) {
	if (!roId) return;
	const switchingRo = state.activeKind !== "REPAIR_ORDER" || state.activeId !== roId;
	if (switchingRo) {
		clearPinRevealState();
		state.roData = null;
		state.roPinState = null;
		state.roExceptionSummary = null;
		state.seData = null;
		state.sePinState = null;
		state.inspectionExpanded = false;
		state.tempParkOpen = false;
		state.dropOffOdoDraft = "";
		state.dropOffOdoError = "";
		state.tempParkOdoDraft = "";
		state.tempParkOdoError = "";
		state.boardThread = null;
		state.boardEvents = [];
	}

	state.activeKind = "REPAIR_ORDER";
	state.activeId = roId;
	state.activeView = view === "BOARD" ? "BOARD" : "DETAIL";

	applyLayerVisibility();
	renderJobsList();
	syncUrl({ push: pushHistory });

	if (switchingRo || !state.roData) {
		state.isLoadingDetail = true;
		el.l2Empty.classList.add("is-hidden");
		el.seView.classList.add("is-hidden");
		el.roView.classList.add("is-hidden");
		el.l2Loading.classList.remove("is-hidden");
		if (el.l2LoadingText) el.l2LoadingText.textContent = `Loading ${roId}...`;

		try {
			const res = await getTechRepairOrder(roId);
			if (state.activeKind !== "REPAIR_ORDER" || state.activeId !== roId) return;
			applyRoPayload(res);
			renderRepairOrderView();
		} catch (err) {
			showToast(err.message || `Unable to open ${roId}.`, "error");
			el.l2Loading.classList.add("is-hidden");
			el.l2Empty.classList.remove("is-hidden");
			if (err.status === 403 || err.status === 404) {
				await refreshJobsQueue({ silent: true });
				navigateToList({ pushHistory: false });
			}
			return;
		} finally {
			state.isLoadingDetail = false;
		}
	} else {
		renderRepairOrderView();
	}

	if (state.activeView === "BOARD") {
		await loadExceptionBoardForActiveRo();
	}
}

async function handlePickUpVehicle() {
	if (!state.roData || state.isMutatingLifecycle) return;
	state.isMutatingLifecycle = true;
	renderRepairOrderView();
	try {
		const res = await pickUpVehicle(state.roData.name);
		applyRoPayload(res);
		showToast("Vehicle moved into bay.", "success");
		renderRepairOrderView();
		refreshJobsQueue({ silent: true });
	} catch (err) {
		showToast(err.message || "Failed to pick up vehicle.", "error");
	} finally {
		state.isMutatingLifecycle = false;
		renderRepairOrderView();
	}
}

async function handleToggleService(serviceRowId) {
	if (!state.roData || state.togglingServices.has(serviceRowId)) return;
	const services = Array.isArray(state.roData.services) ? state.roData.services : [];
	const target = services.find((s) => s.name === serviceRowId);
	if (!target) return;

	// Optimistic update with rollback snapshot
	const prevSnapshot = {
		is_completed: target.is_completed,
		completed_at: target.completed_at,
		completed_count: state.roData.completed_count,
		progress_percent: state.roData.progress_percent,
		lifecycle_state: state.roData.lifecycle_state,
	};

	const nextCompleted = !Boolean(target.is_completed);
	target.is_completed = nextCompleted;
	const approvedCount = services.length;
	const completedCount = services.filter((s) => Boolean(s.is_completed)).length;
	state.roData.completed_count = completedCount;
	state.roData.progress_percent = approvedCount > 0 ? Math.round((completedCount / approvedCount) * 100) : 0;
	if (state.roData.vehicle_location === "In Bay / Being Serviced") {
		state.roData.lifecycle_state =
			approvedCount > 0 && completedCount >= approvedCount ? "READY_FOR_DROPOFF" : "IN_BAY_WORKING";
	}

	state.togglingServices.add(serviceRowId);
	renderRepairOrderView();

	try {
		const res = await toggleServiceItem({
			repair_order: state.roData.name,
			service_row_id: serviceRowId,
			is_completed: nextCompleted,
		});
		applyRoPayload(res);
	} catch (err) {
		// Rollback optimistic change
		target.is_completed = prevSnapshot.is_completed;
		target.completed_at = prevSnapshot.completed_at;
		state.roData.completed_count = prevSnapshot.completed_count;
		state.roData.progress_percent = prevSnapshot.progress_percent;
		state.roData.lifecycle_state = prevSnapshot.lifecycle_state;
		showToast(err.message || "Failed to update service item.", "error");
	} finally {
		state.togglingServices.delete(serviceRowId);
		renderRepairOrderView();
	}
}

async function handleDropOffVehicle({ temporary = false } = {}) {
	if (!state.roData || state.isMutatingLifecycle) return;
	const rawVal = temporary ? state.tempParkOdoDraft : state.dropOffOdoDraft;
	const check = validateOdometerInput(rawVal, state.roData.odometer_in);

	if (!check.ok) {
		if (temporary) {
			state.tempParkOdoError = check.error;
		} else {
			state.dropOffOdoError = check.error;
		}
		renderRepairOrderView();
		return;
	}

	state.isMutatingLifecycle = true;
	if (temporary) {
		state.tempParkOdoError = "";
	} else {
		state.dropOffOdoError = "";
	}
	renderRepairOrderView();

	try {
		const res = await dropOffVehicle({
			repair_order: state.roData.name,
			odometer_out: check.value,
			temporary,
		});
		applyRoPayload(res);
		state.tempParkOpen = false;
		state.tempParkOdoDraft = "";
		state.dropOffOdoDraft = "";
		const isTemporaryOutcome =
			temporary || (state.roData && state.roData.lifecycle_state !== "COMPLETED");
		showToast(
			isTemporaryOutcome
				? "Vehicle temporarily parked in customer lot."
				: "Vehicle dropped off in lot & work completed!",
			"success"
		);
		await refreshJobsQueue({ silent: true });
	} catch (err) {
		const msg = err.message || "Failed to park vehicle.";
		if (temporary) {
			state.tempParkOdoError = msg;
		} else {
			state.dropOffOdoError = msg;
		}
		showToast(msg, "error");
	} finally {
		state.isMutatingLifecycle = false;
		renderRepairOrderView();
	}
}

async function handleUpdateInspection(itemRowId, status, notes) {
	if (!state.roData) return;
	try {
		const res = await updateInspectionItem({
			repair_order: state.roData.name,
			item_row_id: itemRowId,
			status,
			notes,
		});
		applyRoPayload(res);
		renderRepairOrderView();
		showToast("Inspection item saved.", "success");
	} catch (err) {
		showToast(err.message || "Failed to update inspection item.", "error");
	}
}

/* =========================================================================
   Layer 3: Exception Chat Board (Lazily Loaded)
   ========================================================================= */

function renderLayer3Header() {
	const ro = state.roData;
	const t = state.boardThread;
	const roName = (ro && ro.name) || (t && t.repair_order) || state.activeId || "";

	if (el.l3Title) {
		el.l3Title.textContent = roName ? `Exception Board · ${roName}` : "Exception Board";
	}
	if (el.l3Subtitle) {
		const parts = [];
		if (ro && ro.vehicle_display) parts.push(ro.vehicle_display);
		if (ro && ro.license_plate) parts.push(`Plate ${ro.license_plate}`);
		if ((ro && ro.bay) || (t && t.bay)) parts.push((ro && ro.bay) || (t && t.bay));
		el.l3Subtitle.textContent = parts.join(" · ");
	}
	if (el.l3StatusBadge) {
		const status = (t && t.board_status) || "ACTIVE";
		el.l3StatusBadge.className = `andon-badge andon-badge--${status}`;
		el.l3StatusBadge.textContent = status.replace(/_/g, " ");
	}
	updateLiveTimers();
}

function mergeBoardEvents(incoming, { prepend = false } = {}) {
	if (!Array.isArray(incoming) || !incoming.length) return false;
	// Defense-in-depth: never render routine STATE_MUTATION pills in Layer 3
	const filteredIncoming = incoming.filter((ev) => ev && ev.name && ev.payload_type !== "STATE_MUTATION");
	if (!filteredIncoming.length) return false;

	const byName = new Map(state.boardEvents.map((e) => [e.name, e]));
	let changed = false;

	for (const ev of filteredIncoming) {
		const prev = byName.get(ev.name);
		const merged = { ...(prev || {}), ...ev };
		if (!prev) {
			changed = true;
		} else if (
			prev.resolution_state !== merged.resolution_state ||
			prev.ai_status !== merged.ai_status ||
			prev.ai_error !== merged.ai_error ||
			prev.transcript !== merged.transcript ||
			JSON.stringify(prev.card_schema ?? null) !== JSON.stringify(merged.card_schema ?? null) ||
			JSON.stringify(prev.resolution_payload ?? null) !==
				JSON.stringify(merged.resolution_payload ?? null)
		) {
			changed = true;
		}
		byName.set(ev.name, merged);
	}

	const ordered = prepend
		? [
				...filteredIncoming.filter((e) => !state.boardEvents.some((x) => x.name === e.name)),
				...state.boardEvents,
		  ]
		: Array.from(byName.values());

	ordered.sort((a, b) => {
		const ta = String(a.event_timestamp || "");
		const tb = String(b.event_timestamp || "");
		if (ta !== tb) return ta.localeCompare(tb);
		return String(a.name || "").localeCompare(String(b.name || ""));
	});

	state.boardEvents = ordered;
	return changed;
}

function renderLayer3Timeline({ scrollToBottom = false } = {}) {
	if (!el.timelineList) return;
	el.timelineList.innerHTML = "";

	if (el.loadMoreWrap) {
		el.loadMoreWrap.classList.toggle("is-hidden", !state.boardHasMore);
	}

	if (!state.boardEvents.length) {
		el.timelineList.innerHTML = `
			<div class="timeline-empty">
				No exceptions or escalations on this Repair Order. Tap PTT, Cam, or type below only if you hit a blocker, missing part, or need a supplement.
			</div>
		`;
		return;
	}

	const handlers = {
		canManage: false,
		onOpenLightbox: openLightbox,
	};

	const frag = document.createDocumentFragment();
	for (const ev of state.boardEvents) {
		if (ev.payload_type === "STATE_MUTATION") continue;
		frag.appendChild(renderEventCard(ev, handlers));
	}
	el.timelineList.appendChild(frag);

	if (scrollToBottom && el.timelineScroll) {
		requestAnimationFrame(() => {
			el.timelineScroll.scrollTop = el.timelineScroll.scrollHeight;
		});
	}
}

async function openExceptionBoard({ pushHistory = true } = {}) {
	if (state.activeKind !== "REPAIR_ORDER" || !state.activeId) return;
	clearPinRevealState();
	state.activeView = "BOARD";
	applyLayerVisibility();
	syncUrl({ push: pushHistory });
	await loadExceptionBoardForActiveRo();
}

function closeExceptionBoard({ pushHistory = true } = {}) {
	state.activeView = "DETAIL";
	applyLayerVisibility();
	syncUrl({ push: pushHistory });
	renderRepairOrderView();
}

async function loadExceptionBoardForActiveRo() {
	if (state.activeKind !== "REPAIR_ORDER" || !state.activeId) return;
	const roId = state.activeId;
	const threadKey =
		(state.roExceptionSummary && state.roExceptionSummary.thread_key) || `RO:${roId}`;
	state.boardThreadKey = threadKey;
	state.isLoadingBoard = true;

	renderLayer3Header();
	if (el.timelineList) {
		el.timelineList.innerHTML = `<div class="timeline-empty">Loading exception board for ${escapeHtml(roId)}...</div>`;
	}

	if (realtimeClient) {
		realtimeClient.subscribeThread(threadKey);
	}

	try {
		const res = await getBoardThread({ repair_order: roId, limit: 100 });
		if (state.activeId !== roId) return;

		state.boardThread = res.thread || null;
		state.boardThreadKey = (res.thread && res.thread.thread_key) || threadKey;
		state.boardEvents = (Array.isArray(res.events) ? res.events : []).filter(
			(ev) => ev && ev.payload_type !== "STATE_MUTATION"
		);
		state.boardHasMore = Boolean(res.has_more);
		state.boardNextBeforeEvent = res.next_before_event || null;
		state.boardCanPost = res.can_post !== false;
		state.boardReadOnlyReason = res.read_only_reason || "";

		if (realtimeClient && state.boardThreadKey) {
			realtimeClient.subscribeThread(state.boardThreadKey);
		}

		renderLayer3Header();
		renderLayer3Timeline({ scrollToBottom: true });

		if (captureController) {
			captureController.setThreadType("REPAIR_ORDER");
			captureController.setReadOnly(!state.boardCanPost, state.boardReadOnlyReason);
		}
	} catch (err) {
		showToast(err.message || "Failed to load exception board.", "error");
		if (el.timelineList) {
			el.timelineList.innerHTML = `<div class="timeline-empty">${escapeHtml(err.message || "Unable to load exception board.")}</div>`;
		}
	} finally {
		state.isLoadingBoard = false;
	}
}

async function loadOlderBoardEvents() {
	if (!state.boardThreadKey || !state.boardNextBeforeEvent) return;
	if (el.loadMoreBtn) el.loadMoreBtn.disabled = true;
	try {
		const res = await getBoardThread({
			thread_key: state.boardThreadKey,
			limit: 100,
			before_event: state.boardNextBeforeEvent,
		});
		state.boardHasMore = Boolean(res.has_more);
		state.boardNextBeforeEvent = res.next_before_event || null;
		mergeBoardEvents(res.events || [], { prepend: true });
		renderLayer3Timeline({ scrollToBottom: false });
	} catch (err) {
		showToast(err.message || "Failed to load earlier events.", "error");
	} finally {
		if (el.loadMoreBtn) el.loadMoreBtn.disabled = false;
	}
}

async function syncBoardDelta() {
	if (state.activeView !== "BOARD" || !state.boardThreadKey || state.isLoadingBoard) return;
	if (state.isSyncingDelta) {
		state.pendingDeltaSync = true;
		return;
	}
	state.isSyncingDelta = true;
	state.pendingDeltaSync = false;
	try {
		const lastEv = state.boardEvents.length
			? state.boardEvents[state.boardEvents.length - 1].name
			: null;
		const knownVer = state.boardThread ? Number(state.boardThread.projection_version || 0) : 0;
		const res = await getThreadDelta({
			thread_key: state.boardThreadKey,
			after_event: lastEv,
			known_projection_version: knownVer,
		});

		if (res.thread) {
			state.boardThread = res.thread;
		}
		if (res.can_post !== undefined) {
			state.boardCanPost = res.can_post !== false;
			state.boardReadOnlyReason = res.read_only_reason || "";
			if (captureController) {
				captureController.setReadOnly(!state.boardCanPost, state.boardReadOnlyReason);
			}
		}

		const newEvents = Array.isArray(res.events) ? res.events : [];
		const openCards = Array.isArray(res.open_cards) ? res.open_cards : [];
		const updatedCards = Array.isArray(res.updated_cards) ? res.updated_cards : [];
		const eventsChanged = mergeBoardEvents([...newEvents, ...openCards, ...updatedCards]);

		if (res.changed || eventsChanged) {
			renderLayer3Header();
			renderLayer3Timeline({ scrollToBottom: newEvents.length > 0 });
		}
	} catch (_err) {
		// offline banner managed by api.js
	} finally {
		state.isSyncingDelta = false;
		if (state.pendingDeltaSync) {
			state.pendingDeltaSync = false;
			syncBoardDelta();
		}
	}
}

/* =========================================================================
   Realtime Queue & Execution Handlers
   ========================================================================= */

async function handleTechQueueUpdated(payload) {
	await refreshJobsQueue({ silent: true });

	// Live SE -> RO superseding while viewing Layer 2A
	if (state.activeKind === "SCHEDULE_ENTRY" && state.activeId) {
		const supersededRo = payload && payload.superseded_by_ro;
		const matchedSe =
			payload && (payload.schedule_entry === state.activeId || payload.name === state.activeId);
		if (matchedSe && supersededRo) {
			await openScheduleEntry(state.activeId, { pushHistory: false });
			return;
		}
		// If SE disappeared from queue, re-check it to trigger clean superseding or removal
		const stillInQueue = state.jobs.some(
			(j) => j.kind === "SCHEDULE_ENTRY" && (j.id === state.activeId || j.name === state.activeId)
		);
		if (!stillInQueue && state.tab === "ACTIVE") {
			await openScheduleEntry(state.activeId, { pushHistory: false });
		}
		return;
	}

	// Live RO removal (e.g., put on Waiting on Parts or reassigned)
	if (state.activeKind === "REPAIR_ORDER" && state.activeId && state.tab === "ACTIVE") {
		const roInQueue = state.jobs.some(
			(j) => j.kind === "REPAIR_ORDER" && (j.id === state.activeId || j.name === state.activeId)
		);
		if (!roInQueue && state.roData && state.roData.lifecycle_state !== "COMPLETED") {
			try {
				const res = await getTechRepairOrder(state.activeId);
				applyRoPayload(res);
				renderRepairOrderView();
			} catch (err) {
				if (err.status === 403 || err.status === 404) {
					showToast(`${state.activeId} is no longer in your active queue.`, "warn");
					state.activeKind = null;
					state.activeId = null;
					state.activeView = "LIST";
					el.roView.classList.add("is-hidden");
					el.l2Empty.classList.remove("is-hidden");
					applyLayerVisibility();
					syncUrl({ push: false });
				}
			}
		}
	}
}

async function handleRoExecutionUpdated(payload) {
	const roName = payload && (payload.repair_order || payload.name);
	await refreshJobsQueue({ silent: true });
	if (state.activeKind === "REPAIR_ORDER" && state.activeId && (!roName || roName === state.activeId)) {
		try {
			const res = await getTechRepairOrder(state.activeId);
			applyRoPayload(res);
			if (state.activeView === "DETAIL") {
				renderRepairOrderView();
			}
		} catch (_err) {
			// handled by queue update if access revoked
		}
	}
}

function attachCustomRealtimeListeners() {
	const bindTarget = (emitter) => {
		if (!emitter || typeof emitter.on !== "function") return;
		const onQueue = (payload) => handleTechQueueUpdated(payload || {});
		const onExec = (payload) => handleRoExecutionUpdated(payload || {});
		emitter.on("ism_tech_queue_updated", onQueue);
		emitter.on("ism_ro_execution_updated", onExec);
		customSocketListeners.push([emitter, "ism_tech_queue_updated", onQueue]);
		customSocketListeners.push([emitter, "ism_ro_execution_updated", onExec]);
	};

	if (realtimeClient && realtimeClient.socket) {
		bindTarget(realtimeClient.socket);
	} else if (window.frappe && window.frappe.realtime && typeof window.frappe.realtime.on === "function") {
		bindTarget(window.frappe.realtime);
	}
}

function detachCustomRealtimeListeners() {
	for (const [emitter, evt, fn] of customSocketListeners) {
		try {
			if (typeof emitter.off === "function") {
				emitter.off(evt, fn);
			}
		} catch (_err) {
			// ignore
		}
	}
	customSocketListeners = [];
}

/* =========================================================================
   Bootstrap & Event Wiring
   ========================================================================= */

function bindUiListeners() {
	if (el.refreshJobsBtn) {
		el.refreshJobsBtn.addEventListener("click", async () => {
			await refreshJobsQueue({ silent: false });
			showToast("Jobs refreshed.", "info");
		});
	}

	for (const tabBtn of [el.tabActive, el.tabCompleted]) {
		if (!tabBtn) continue;
		tabBtn.addEventListener("click", async () => {
			const nextTab = tabBtn.dataset.tab === "COMPLETED" ? "COMPLETED" : "ACTIVE";
			if (state.tab === nextTab) return;
			state.tab = nextTab;
			renderJobsList();
			await refreshJobsQueue({ silent: false });
		});
	}

	if (el.jobSearch) {
		el.jobSearch.addEventListener("input", (evt) => {
			state.searchQuery = evt.target.value || "";
			renderJobsList();
		});
	}

	if (el.l3BackBtn) {
		el.l3BackBtn.addEventListener("click", () => closeExceptionBoard({ pushHistory: true }));
	}

	if (el.l3SyncBtn) {
		el.l3SyncBtn.addEventListener("click", async () => {
			await syncBoardDelta();
			showToast("Exception board synced.", "info");
		});
	}

	if (el.loadMoreBtn) {
		el.loadMoreBtn.addEventListener("click", () => loadOlderBoardEvents());
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
			if (state.activeView === "BOARD" && captureController && (await captureController.retryLastFailed())) {
				return;
			}
			await refreshJobsQueue({ silent: false });
			if (state.activeView === "BOARD") {
				await syncBoardDelta();
			}
		});
	}

	window.addEventListener("popstate", () => {
		const route = parseRouteFromUrl();
		if (route.kind === "REPAIR_ORDER" && route.id) {
			openRepairOrder(route.id, { view: route.view, pushHistory: false });
		} else if (route.kind === "SCHEDULE_ENTRY" && route.id) {
			openScheduleEntry(route.id, { pushHistory: false });
		} else {
			clearPinRevealState();
			state.activeView = "LIST";
			if (!isDesktopLayout()) {
				state.activeKind = null;
				state.activeId = null;
			}
			applyLayerVisibility();
			renderJobsList();
		}
	});
}

function init() {
	setTechOfflineHandler(handleOffline);
	setBoardOfflineHandler(handleOffline);

	bindUiListeners();
	renderJobsList();

	captureController = new BoardCaptureController({
		getContext: () => ({
			thread_key: state.boardThreadKey,
			ro_id: state.roData ? state.roData.name : state.activeId,
			vin: state.roData ? state.roData.vin : null,
			bay_id: state.roData ? state.roData.bay : null,
			tech_id: boot.tech_id || null,
		}),
		onCaptureSent: (res) => {
			if (res && res.thread) {
				state.boardThread = res.thread;
				renderLayer3Header();
			}
			if (res && res.event) {
				mergeBoardEvents([res.event]);
				renderLayer3Timeline({ scrollToBottom: true });
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
					el.aiStatusBar.textContent = "Board AI analyzing exception...";
					el.aiStatusBar.classList.remove("is-hidden");
				}
				const prevCount = state.boardEvents.length;
				syncBoardDelta().then(() => {
					if (state.boardEvents.length > prevCount && evStatus !== "FAILED" && el.aiStatusBar) {
						el.aiStatusBar.classList.add("is-hidden");
					}
					refreshJobsQueue({ silent: true });
				});
			}
		},
		onToast: showToast,
		elements: {
			readonlyBanner: document.getElementById("tech-capture-readonly-banner"),
			intentPills: document.getElementById("tech-intent-pills"),
			captureNotice: document.getElementById("tech-capture-notice"),
			photoTray: document.getElementById("tech-photo-tray"),
			uploadProgress: document.getElementById("tech-upload-progress"),
			uploadProgressFill: document.getElementById("tech-upload-progress-fill"),
			uploadProgressLabel: document.getElementById("tech-upload-progress-label"),
			pttRecordingBar: document.getElementById("tech-ptt-recording-bar"),
			pttTimer: document.getElementById("tech-ptt-timer"),
			pttCancelBtn: document.getElementById("tech-ptt-cancel-btn"),
			pttStopSendBtn: document.getElementById("tech-ptt-stop-send-btn"),
			pttBtn: document.getElementById("tech-ptt-btn"),
			cameraInput: document.getElementById("tech-camera-input"),
			textInput: document.getElementById("tech-capture-text"),
			sendBtn: document.getElementById("tech-send-btn"),
		},
	});
	captureController.setThreadType("REPAIR_ORDER");

	realtimeClient = new BoardRealtimeClient({
		sitename: boot.sitename,
		socketioPort: boot.socketio_port,
		onBoardEvent: (payload) => {
			if (state.activeView === "BOARD" && payload.thread === state.boardThreadKey) {
				syncBoardDelta();
			}
			refreshJobsQueue({ silent: true });
		},
		onProjection: (payload) => {
			if (state.activeView === "BOARD" && payload.thread_key === state.boardThreadKey) {
				state.boardThread = { ...(state.boardThread || {}), ...payload };
				renderLayer3Header();
			}
			refreshJobsQueue({ silent: true });
		},
		onAiStatus: (payload) => {
			if (state.activeView !== "BOARD" || payload.thread_key !== state.boardThreadKey) return;
			if (payload.ai_status === "PROCESSING" || payload.ai_status === "QUEUED") {
				el.aiStatusBar.textContent = "Board AI analyzing exception...";
				el.aiStatusBar.classList.remove("is-hidden");
			} else if (payload.ai_status === "FAILED") {
				el.aiStatusBar.textContent = `AI note: ${payload.ai_error || "Processing failed"}`;
				el.aiStatusBar.classList.remove("is-hidden");
				syncBoardDelta();
			} else {
				el.aiStatusBar.classList.add("is-hidden");
				syncBoardDelta();
			}
		},
		onThreadSummary: () => {
			refreshJobsQueue({ silent: true });
		},
		onCatchUpSync: () => {
			refreshJobsQueue({ silent: true });
			if (state.activeView === "BOARD") {
				syncBoardDelta();
			} else if (state.activeView === "DETAIL" && state.activeKind === "REPAIR_ORDER" && state.activeId) {
				handleRoExecutionUpdated({ repair_order: state.activeId });
			}
		},
	});
	realtimeClient.connect();
	attachCustomRealtimeListeners();

	liveTimerInterval = setInterval(updateLiveTimers, 1000);
	window.addEventListener("beforeunload", () => {
		clearPinRevealState();
		if (liveTimerInterval) clearInterval(liveTimerInterval);
		detachCustomRealtimeListeners();
		if (realtimeClient) realtimeClient.destroy();
	});

	if (boot.initial_error) {
		showToast(boot.initial_error, "warn");
	}

	// Resolve initial route from boot or URL
	const urlRoute = parseRouteFromUrl();
	const initKind = boot.initial_kind || urlRoute.kind;
	const initId = boot.initial_id || urlRoute.id;
	const initView =
		(boot.initial_view && String(boot.initial_view).toUpperCase() === "BOARD") ||
		urlRoute.view === "BOARD"
			? "BOARD"
			: "DETAIL";

	if (initKind === "REPAIR_ORDER" && initId) {
		openRepairOrder(initId, { view: initView, pushHistory: false });
	} else if (initKind === "SCHEDULE_ENTRY" && initId) {
		openScheduleEntry(initId, { pushHistory: false });
	} else if (isDesktopLayout() && state.jobs.length > 0) {
		const first = state.jobs[0];
		if (first.kind === "SCHEDULE_ENTRY") {
			openScheduleEntry(first.id || first.name, { pushHistory: false });
		} else {
			openRepairOrder(first.id || first.name, { view: "DETAIL", pushHistory: false });
		}
	} else {
		applyLayerVisibility();
	}
}

if (document.readyState === "loading") {
	document.addEventListener("DOMContentLoaded", init);
} else {
	init();
}
