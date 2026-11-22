/**
 * Event & Action Card Renderer for /board (Active RO Communications & AI Action Feed).
 * Renders MEDIA_CAPTURE, TEXT (TECH / MANAGER / AI natural-language), ACTION_CARD, and STATE_MUTATION.
 */

import { getMediaUrl } from "./api.js";

const _inFlightActionItems = new Set();

export function escapeHtml(str) {
	return String(str ?? "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#39;");
}

function formatShortTime(ts) {
	if (!ts) return "";
	const normalized = String(ts).includes("T") ? String(ts) : String(ts).replace(" ", "T");
	const d = new Date(normalized);
	if (Number.isNaN(d.getTime())) return String(ts);
	return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function disableCardControls(cardEl, disabled) {
	for (const btn of cardEl.querySelectorAll("button, input, textarea, select")) {
		btn.disabled = disabled;
	}
}

function buildEventHeader(event) {
	const actor = event.actor_type || "SYSTEM";
	const userLabel =
		event.actor_user && actor !== "AI" && actor !== "SYSTEM"
			? `<span>${escapeHtml(event.actor_user)}</span>`
			: "";
	const intentHtml = event.intent_tag
		? `<span class="intent-badge">${escapeHtml(event.intent_tag)}</span>`
		: "";
	const aiPill =
		event.ai_status && event.ai_status !== "NONE" && event.ai_status !== "COMPLETED"
			? `<span class="intent-badge">AI: ${escapeHtml(event.ai_status)}</span>`
			: "";

	return `
		<div class="event-card__header">
			<div class="event-card__actor-group">
				<span class="actor-pill actor-pill--${escapeHtml(actor)}">${escapeHtml(actor)}</span>
				${userLabel}
				${intentHtml}
				${aiPill}
			</div>
			<time datetime="${escapeHtml(event.event_timestamp || "")}">${escapeHtml(formatShortTime(event.event_timestamp))}</time>
		</div>
	`;
}

function renderSystemNotificationPill(event) {
	const meta = event.metadata || {};
	const lines = String(event.body_text || "").split("\n").filter(Boolean);
	const headline = lines[0] || meta.title || event.intent_tag || "System Update";
	const details = lines.slice(1).join(" ") || meta.details || "";
	const refDt = meta.reference_doctype;
	const refName = meta.reference_name;

	let deskLink = "";
	if (refDt && refName) {
		const slug = String(refDt).toLowerCase().replace(/\s+/g, "-");
		deskLink = `<a href="/app/${encodeURIComponent(slug)}/${encodeURIComponent(refName)}" target="_blank" rel="noopener">${escapeHtml(refName)}</a>`;
	}

	const el = document.createElement("article");
	el.className = `system-pill-card ${details ? "system-pill-card--multiline" : ""}`;
	el.dataset.eventName = event.name;
	el.innerHTML = `
		<div class="system-pill-card__main">
			<span class="actor-pill actor-pill--SYSTEM">SYSTEM</span>
			<strong>${escapeHtml(headline)}</strong>
			${meta.category ? `<span class="meta-chip">${escapeHtml(meta.category)}</span>` : ""}
			${deskLink}
		</div>
		<time>${escapeHtml(formatShortTime(event.event_timestamp))}</time>
		${details ? `<div class="system-pill-card__details">${escapeHtml(details)}</div>` : ""}
	`;
	return el;
}

function renderCaptureCard(event, { onOpenLightbox }) {
	const isAiText = event.actor_type === "AI" && event.payload_type === "TEXT";
	const el = document.createElement("article");
	el.className = `event-card event-card--capture${isAiText ? " event-card--ai" : ""}`;
	el.dataset.eventName = event.name;

	const mediaList = Array.isArray(event.media_attachments) ? event.media_attachments : [];
	const audioItems = [];
	const photoItems = [];

	for (const item of mediaList) {
		const mime = String(item.content_type || item.mime_type || "").toLowerCase();
		const url = item.url || (item.file_ref ? getMediaUrl(event.name, item.file_ref) : "");
		if (!url) continue;
		if (mime.startsWith("audio/")) {
			audioItems.push({ ...item, url });
		} else if (mime.startsWith("image/")) {
			photoItems.push({ ...item, url });
		}
	}

	const audioHtml = audioItems
		.map(
			(a) => `
			<div class="media-audio-wrap">
				<audio controls preload="metadata" src="${escapeHtml(a.url)}"></audio>
				<span class="event-card__meta-stamp">${escapeHtml(a.filename || "Voice note")}</span>
			</div>
		`
		)
		.join("");

	const photoHtml = photoItems.length
		? `<div class="media-photo-grid">${photoItems
				.map(
					(p) =>
						`<img class="media-photo-thumb" src="${escapeHtml(p.url)}" alt="${escapeHtml(p.filename || "Photo")}" loading="lazy" data-lightbox-src="${escapeHtml(p.url)}" />`
				)
				.join("")}</div>`
		: "";

	const transcriptHtml = event.transcript
		? `
		<details class="transcript-accordion">
			<summary>View Voice Transcript</summary>
			<div class="transcript-accordion__text">${escapeHtml(event.transcript)}</div>
		</details>
	`
		: "";

	const aiErrorHtml = event.ai_error
		? `<div class="capture-notice">AI Note: ${escapeHtml(event.ai_error)}</div>`
		: "";

	const meta = event.metadata || {};
	const stampParts = [];
	if (meta.bay_id) stampParts.push(`Bay: ${meta.bay_id}`);
	if (meta.vin) stampParts.push(`VIN: ${meta.vin}`);
	if (meta.tech_id) stampParts.push(`Tech: ${meta.tech_id}`);

	const stampHtml = stampParts.length
		? `<div class="event-card__meta-stamp">${stampParts.map((s) => `<span>${escapeHtml(s)}</span>`).join("")}</div>`
		: "";

	el.innerHTML = `
		${buildEventHeader(event)}
		${event.body_text ? `<div class="event-card__body">${escapeHtml(event.body_text)}</div>` : ""}
		${audioHtml}
		${photoHtml}
		${transcriptHtml}
		${aiErrorHtml}
		${stampHtml}
	`;

	if (onOpenLightbox) {
		for (const img of el.querySelectorAll(".media-photo-thumb")) {
			img.addEventListener("click", () => onOpenLightbox(img.dataset.lightboxSrc));
		}
	}

	return el;
}

function getActionStatusMeta(resState, autoCleared) {
	if (resState === "RESOLVED") {
		return {
			cls: "action-status--resolved",
			cardCls: "action-card--resolved",
			label: autoCleared ? "AUTO-CLEARED" : "CLEARED",
		};
	}
	if (resState === "REJECTED") {
		return {
			cls: "action-status--dismissed",
			cardCls: "action-card--dismissed",
			label: "DISMISSED",
		};
	}
	return {
		cls: "action-status--open",
		cardCls: "action-card--open",
		label: "OPEN",
	};
}

function renderActionCard(event, { canManage = false, onCopyForAgent, onDismissActionCard } = {}) {
	const schema = event.card_schema || {};
	const meta = event.metadata || {};
	const resPayload = event.resolution_payload || {};

	const actId =
		event.action_item ||
		schema.action_item ||
		schema.name ||
		meta.action_item ||
		"";
	const roName =
		event.repair_order ||
		schema.repair_order ||
		schema.reference_name ||
		meta.repair_order ||
		"";
	const title = schema.title || event.body_text || actId || "Action Item";
	const details = schema.details || "";
	const priority = schema.priority || "Medium";
	const category = schema.category || "Action";
	const resState = event.resolution_state || "PENDING";
	const isOpen = resState === "PENDING" || resState === "NONE";
	const autoCleared = Boolean(schema.auto_cleared || resPayload.auto_cleared);
	const statusMeta = getActionStatusMeta(resState, autoCleared);

	const clearingText =
		schema.clearing_condition ||
		schema.clearing_condition_description ||
		resPayload.clearing_condition ||
		"";
	const resolutionNotes = resPayload.resolution_notes || schema.resolution_notes || "";

	let clearingBadgeHtml = "";
	if (resState === "RESOLVED") {
		const clearedMsg = autoCleared
			? resolutionNotes || "Cleared automatically"
			: resolutionNotes || "Resolved";
		clearingBadgeHtml = `<div class="action-card__clearing action-card__clearing--done">${escapeHtml(clearedMsg)}</div>`;
	} else if (clearingText) {
		clearingBadgeHtml = `<div class="action-card__clearing">Auto-clears: ${escapeHtml(clearingText)}</div>`;
	}

	const deskHref = actId ? `/app/shop-action-item/${encodeURIComponent(actId)}` : "";

	const el = document.createElement("article");
	el.className = `event-card action-card ${statusMeta.cardCls}`;
	el.dataset.eventName = event.name;
	if (actId) el.dataset.actionItem = actId;

	el.innerHTML = `
		<div class="event-card__header">
			<div class="event-card__actor-group">
				${actId ? `<span class="act-id-badge">${escapeHtml(actId)}</span>` : ""}
				<span class="prio-badge prio-badge--${escapeHtml(priority)}">${escapeHtml(priority)}</span>
				<span class="meta-chip">${escapeHtml(category)}</span>
				<span class="action-status-pill ${statusMeta.cls}">${escapeHtml(statusMeta.label)}</span>
			</div>
			<time datetime="${escapeHtml(event.event_timestamp || "")}">${escapeHtml(formatShortTime(event.event_timestamp))}</time>
		</div>
		<div class="action-card__title">${escapeHtml(title)}</div>
		${details ? `<div class="action-card__details">${escapeHtml(details)}</div>` : ""}
		${clearingBadgeHtml}
		<div class="action-card__footer">
			<div class="action-card__links">
				${deskHref ? `<a class="btn btn-sm btn-ghost" href="${escapeHtml(deskHref)}" target="_blank" rel="noopener">Desk</a>` : ""}
			</div>
			<div class="action-card__actions">
				${
					canManage && isOpen && actId
						? `<button type="button" class="btn btn-sm btn-ghost js-dismiss-action-btn">Dismiss</button>`
						: ""
				}
				<button type="button" class="btn btn-sm btn-primary js-copy-agent-btn">Copy for Agent</button>
			</div>
		</div>
	`;

	const copyBtn = el.querySelector(".js-copy-agent-btn");
	if (copyBtn) {
		copyBtn.addEventListener("click", () => {
			if (onCopyForAgent) {
				onCopyForAgent({ action_item: actId, repair_order: roName, title });
			}
		});
	}

	const dismissBtn = el.querySelector(".js-dismiss-action-btn");
	if (dismissBtn && onDismissActionCard && actId) {
		dismissBtn.addEventListener("click", async () => {
			if (_inFlightActionItems.has(actId)) return;
			_inFlightActionItems.add(actId);
			disableCardControls(el, true);
			try {
				await onDismissActionCard({
					action_item: actId,
					event_name: event.name,
					title,
				});
			} catch (_err) {
				disableCardControls(el, false);
			} finally {
				_inFlightActionItems.delete(actId);
			}
		});
	}

	return el;
}

export function renderEventCard(event, handlers = {}) {
	if (!event) return document.createDocumentFragment();
	const type = event.payload_type;

	if (type === "ACTION_CARD") {
		return renderActionCard(event, handlers);
	}
	if (type === "STATE_MUTATION") {
		return renderSystemNotificationPill(event);
	}
	return renderCaptureCard(event, handlers);
}
