/**
 * Interactive Event & Action Card Renderer for /board.
 * Covers ALL Phase 1 + Phase 2 actions + Seam 5 SYSTEM notification pills.
 * Enforces client-side double-tap prevention & per-card idempotency keys.
 */

import { generateIdempotencyKey, getMediaUrl } from "./api.js";

const DEFAULT_PAYMENT_METHODS = [
	"Debit / Interac",
	"Credit Card",
	"E-Transfer",
	"Cash",
];

// Client-side double-tap guard & idempotency key cache per card action
const _inFlightCards = new Set();
const _cardIdempotencyKeys = new Map();

function getActionIdempotencyKey(eventName, actionKey) {
	const key = `${eventName}:${actionKey}`;
	if (!_cardIdempotencyKeys.has(key)) {
		_cardIdempotencyKeys.set(key, generateIdempotencyKey());
	}
	return _cardIdempotencyKeys.get(key);
}

function clearCardIdempotencyKey(eventName, actionKey) {
	_cardIdempotencyKeys.delete(`${eventName}:${actionKey}`);
}

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

function formatMoney(val) {
	const num = Number(val);
	if (Number.isNaN(num)) return "$0.00";
	return `$${num.toFixed(2)}`;
}

function buildDeskLinkForReceipt(event, schema) {
	const result = (schema && schema.result) || {};
	const data = result.data || result;
	const ro = data.repair_order || event.repair_order;
	if (data.purchase_order) {
		return {
			label: `PO ${data.purchase_order}`,
			href: `/app/purchase-order/${encodeURIComponent(data.purchase_order)}`,
		};
	}
	if (Array.isArray(data.purchase_orders) && data.purchase_orders[0]) {
		const po = data.purchase_orders[0];
		return {
			label: `PO ${po}`,
			href: `/app/purchase-order/${encodeURIComponent(po)}`,
		};
	}
	if (ro) {
		return {
			label: `RO ${ro}`,
			href: `/app/repair-order/${encodeURIComponent(ro)}`,
		};
	}
	return null;
}

function disableCardControls(cardEl, disabled) {
	for (const btn of cardEl.querySelectorAll("button, input, textarea, select")) {
		btn.disabled = disabled;
	}
}

function buildEventHeader(event) {
	const actor = event.actor_type || "SYSTEM";
	const userLabel = event.actor_user && actor !== "AI" && actor !== "SYSTEM"
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

function renderCardResolutionPill(event) {
	const el = document.createElement("article");
	el.className = "system-pill-card";
	el.dataset.eventName = event.name;
	el.innerHTML = `
		<div class="system-pill-card__main">
			<span class="actor-pill actor-pill--${escapeHtml(event.actor_type || "MANAGER")}">${escapeHtml(event.actor_type || "STAFF")}</span>
			<span>${escapeHtml(event.body_text || "Action resolved")}</span>
		</div>
		<time>${escapeHtml(formatShortTime(event.event_timestamp))}</time>
	`;
	return el;
}

function renderCaptureCard(event, { onOpenLightbox }) {
	const el = document.createElement("article");
	el.className = "event-card event-card--capture";
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

function renderReceiptCard(event, { onUndoReceipt }) {
	const schema = event.card_schema || {};
	const isUndone = event.resolution_state === "UNDONE";
	const reversible = Boolean(schema.reversible && schema.undo_spec) && !isUndone;
	const deskLink = buildDeskLinkForReceipt(event, schema);

	const el = document.createElement("article");
	el.className = `event-card event-card--receipt ${isUndone ? "is-undone" : ""}`;
	el.dataset.eventName = event.name;

	const confText =
		event.confidence_score !== null && event.confidence_score !== undefined
			? `${Math.round(Number(event.confidence_score) * 100)}%`
			: "";

	el.innerHTML = `
		${buildEventHeader(event)}
		<div class="receipt-row">
			<div>
				<strong>${escapeHtml(schema.summary || event.body_text || "Action Executed")}</strong>
				<div class="event-card__meta-stamp">
					<span>${escapeHtml(schema.action_type || "")}</span>
					${confText ? `<span>Confidence ${escapeHtml(confText)}</span>` : ""}
					${isUndone ? `<span>UNDONE</span>` : ""}
				</div>
			</div>
			<div class="receipt-actions">
				${deskLink ? `<a class="btn btn-sm btn-ghost" href="${escapeHtml(deskLink.href)}" target="_blank" rel="noopener">${escapeHtml(deskLink.label)}</a>` : ""}
				${reversible ? `<button type="button" class="btn btn-sm btn-amber" data-action="undo">Undo</button>` : ""}
			</div>
		</div>
	`;

	const undoBtn = el.querySelector('[data-action="undo"]');
	if (undoBtn && onUndoReceipt) {
		undoBtn.addEventListener("click", async () => {
			if (_inFlightCards.has(event.name)) return;
			_inFlightCards.add(event.name);
			disableCardControls(el, true);
			try {
				await onUndoReceipt({
					event_name: event.name,
					idempotency_key: getActionIdempotencyKey(event.name, "undo"),
				});
				clearCardIdempotencyKey(event.name, "undo");
			} finally {
				_inFlightCards.delete(event.name);
				if (el.isConnected) disableCardControls(el, false);
			}
		});
	}

	return el;
}

function renderClarificationCard(event, { onResolveCard }) {
	const schema = event.card_schema || {};
	const isPending = event.resolution_state === "PENDING";
	const el = document.createElement("article");
	el.className = `event-card event-card--clarification ${!isPending ? "event-card--resolved" : ""}`;
	el.dataset.eventName = event.name;

	const options = Array.isArray(schema.options) ? schema.options : [];
	const inputField = schema.input_field || "";
	const isNumericField = ["odometer_out", "odometer_in", "amount", "paid_amount"].includes(inputField);

	let bodyHtml = `<div class="event-card__body"><strong>${escapeHtml(schema.prompt || event.body_text || "Clarification Needed")}</strong></div>`;

	if (schema.acoustic_clarity_capped) {
		bodyHtml += `<div class="capture-notice">Noisy shop audio detected — please confirm before updating ERP.</div>`;
	}

	if (!isPending) {
		const resPayload = event.resolution_payload || {};
		const answer = resPayload.selected_option || JSON.stringify(resPayload.form_patch || {});
		bodyHtml += `
			<div class="event-card__meta-stamp">
				<span>Status: ${escapeHtml(event.resolution_state)}</span>
				${event.resolved_by ? `<span>By: ${escapeHtml(event.resolved_by)}</span>` : ""}
				${answer && answer !== "{}" ? `<span>Answer: ${escapeHtml(answer)}</span>` : ""}
			</div>
		`;
	} else {
		if (options.length) {
			bodyHtml += `<div class="clarification-options">${options
				.map(
					(opt, idx) =>
						`<button type="button" class="clarification-option-btn" data-opt-idx="${idx}">${escapeHtml(opt.label || opt.value)}</button>`
				)
				.join("")}</div>`;
		}

		if (inputField) {
			const label = inputField.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
			bodyHtml += `
				<div class="gate-field-group">
					<label class="gate-field-label">${escapeHtml(label)}</label>
					<div class="clarification-input-row">
						<input
							type="${isNumericField ? "number" : "text"}"
							inputmode="${isNumericField ? "numeric" : "text"}"
							class="gate-input"
							data-clarify-input="${escapeHtml(inputField)}"
							placeholder="Enter ${escapeHtml(label)}..."
						/>
						<button type="button" class="btn btn-primary btn-touch" data-clarify-submit="1">Submit</button>
					</div>
				</div>
			`;
		}
	}

	el.innerHTML = `${buildEventHeader(event)}${bodyHtml}`;

	if (isPending && onResolveCard) {
		for (const btn of el.querySelectorAll("[data-opt-idx]")) {
			btn.addEventListener("click", async () => {
				if (_inFlightCards.has(event.name)) return;
				const idx = Number(btn.dataset.optIdx);
				const chosen = options[idx];
				if (!chosen) return;
				_inFlightCards.add(event.name);
				disableCardControls(el, true);
				try {
					await onResolveCard({
						event_name: event.name,
						decision: "clarify",
						selected_option: chosen.value ?? chosen.label,
						idempotency_key: getActionIdempotencyKey(event.name, `opt:${idx}`),
					});
					clearCardIdempotencyKey(event.name, `opt:${idx}`);
				} finally {
					_inFlightCards.delete(event.name);
					if (el.isConnected) disableCardControls(el, false);
				}
			});
		}

		const submitBtn = el.querySelector("[data-clarify-submit]");
		const fieldInput = el.querySelector("[data-clarify-input]");
		if (submitBtn && fieldInput) {
			submitBtn.addEventListener("click", async () => {
				if (_inFlightCards.has(event.name)) return;
				const rawVal = fieldInput.value.trim();
				if (!rawVal) {
					fieldInput.focus();
					return;
				}
				const val = isNumericField ? Number(rawVal) : rawVal;
				_inFlightCards.add(event.name);
				disableCardControls(el, true);
				try {
					await onResolveCard({
						event_name: event.name,
						decision: "clarify",
						form_patch: { [inputField]: val },
						idempotency_key: getActionIdempotencyKey(event.name, `input:${inputField}`),
					});
					clearCardIdempotencyKey(event.name, `input:${inputField}`);
				} finally {
					_inFlightCards.delete(event.name);
					if (el.isConnected) disableCardControls(el, false);
				}
			});
		}
	}

	return el;
}

function buildGateSummaryHtml(actionType, schema, params) {
	const items = [];
	if (actionType === "SUBMIT_PO") {
		const pos = params.purchase_orders || (params.purchase_order ? [params.purchase_order] : []);
		if (pos.length) items.push(["Purchase Order(s)", pos.join(", ")]);
		if (params.supplier) items.push(["Supplier", params.supplier]);
		if (params.total_amount !== undefined) items.push(["Total", formatMoney(params.total_amount)]);
	} else if (actionType === "AMEND_QUOTE") {
		if (schema.current_grand_total !== undefined) items.push(["Current Total", formatMoney(schema.current_grand_total)]);
		if (params.discount_amount !== undefined) items.push(["Discount", formatMoney(params.discount_amount)]);
	} else if (actionType === "CHECKOUT_REPAIR_ORDER") {
		if (schema.grand_total !== undefined) items.push(["Grand Total", formatMoney(schema.grand_total)]);
		if (schema.deposit_amount !== undefined) items.push(["Deposits Paid", formatMoney(schema.deposit_amount)]);
		if (schema.outstanding_amount !== undefined) items.push(["Balance Due", formatMoney(schema.outstanding_amount)]);
	} else if (actionType === "RECORD_DEPOSIT") {
		if (params.amount !== undefined) items.push(["Deposit Amount", formatMoney(params.amount)]);
	} else if (actionType === "INTAKE_QUOTE_AND_BOOK") {
		if (params.customer_name) items.push(["Customer", params.customer_name]);
		if (params.vin || params.vehicle_identifier) items.push(["Vehicle / VIN", params.vin || params.vehicle_identifier]);
		if (params.concern || params.customer_concern) items.push(["Concern", params.concern || params.customer_concern]);
	} else if (actionType === "CONVERT_APPOINTMENT_TO_RO") {
		if (params.schedule_entry) items.push(["Appointment", params.schedule_entry]);
	} else if (actionType === "RECEIVE_PO" || actionType === "POST_PURCHASE_INVOICE" || actionType === "PAY_PURCHASE") {
		if (params.supplier) items.push(["Supplier", params.supplier]);
		if (params.purchase_order) items.push(["Purchase Order", params.purchase_order]);
		if (params.purchase_invoice) items.push(["Purchase Invoice", params.purchase_invoice]);
		if (params.paid_amount !== undefined) items.push(["Amount", formatMoney(params.paid_amount)]);
	} else if (actionType === "DISPATCH_CUSTOMER_SMS" || actionType === "SEND_QUOTE_APPROVAL") {
		if (params.phone_number) items.push(["Phone", params.phone_number]);
	} else if (actionType === "DISPATCH_CUSTOMER_EMAIL") {
		if (params.recipient) items.push(["Email", params.recipient]);
		if (schema.subject || params.subject) items.push(["Subject", schema.subject || params.subject]);
	}

	let html = "";
	if (items.length) {
		html += `<div class="gate-kv-grid">${items
			.map(
				([k, v]) =>
					`<div><div class="gate-kv__label">${escapeHtml(k)}</div><div class="gate-kv__val">${escapeHtml(v)}</div></div>`
			)
			.join("")}</div>`;
	}

	const listItems = params.add_services || params.services || params.parts_data || params.parts || [];
	if (Array.isArray(listItems) && listItems.length) {
		html += `<ul class="gate-items-list">${listItems
			.slice(0, 8)
			.map((row) => {
				if (typeof row === "string") return `<li>${escapeHtml(row)}</li>`;
				const name = row.service_name || row.item_name || row.part_name || row.item_code || "Item";
				const qty = row.qty !== undefined ? ` ×${row.qty}` : "";
				const price =
					row.sell_price !== undefined
						? ` — ${formatMoney(row.sell_price)}`
						: row.rate !== undefined
							? ` — ${formatMoney(row.rate)}`
							: "";
				return `<li>${escapeHtml(name)}${escapeHtml(qty)}${escapeHtml(price)}</li>`;
			})
			.join("")}</ul>`;
	}

	return html;
}

function getApproveButtonLabel(actionType) {
	switch (actionType) {
		case "SUBMIT_PO":
			return "Approve & Submit PO";
		case "AMEND_QUOTE":
			return "Approve Quote Amendment";
		case "SEND_QUOTE_APPROVAL":
		case "DISPATCH_CUSTOMER_SMS":
		case "DISPATCH_CUSTOMER_EMAIL":
			return "Send to Customer";
		case "CHECKOUT_REPAIR_ORDER":
			return "Bill & Release";
		case "RECORD_DEPOSIT":
			return "Record Deposit";
		case "RESCHEDULE_APPOINTMENT":
			return "Confirm Reschedule";
		case "INTAKE_QUOTE_AND_BOOK":
			return "Book & Create RO";
		case "CONVERT_APPOINTMENT_TO_RO":
			return "Check In & Create RO";
		case "RECEIVE_PO":
			return "Receive Parts";
		case "POST_PURCHASE_INVOICE":
			return "Receive & Post Bill";
		case "PAY_PURCHASE":
			return "Mark Paid";
		default:
			return "Approve & Commit";
	}
}

function renderApprovalGateCard(event, { canApprove, onResolveCard, onJumpThread }) {
	const schema = event.card_schema || {};
	const params = schema.params || {};
	const actionType = schema.action_type || "";
	const isPending = event.resolution_state === "PENDING";

	const el = document.createElement("article");
	const stateClass =
		event.resolution_state === "RESOLVED"
			? "event-card--resolved"
			: event.resolution_state === "REJECTED"
				? "event-card--rejected"
				: "event-card--gate";
	el.className = `event-card ${stateClass}`;
	el.dataset.eventName = event.name;

	const summaryHtml = buildGateSummaryHtml(actionType, schema, params);

	if (!isPending) {
		const resPayload = event.resolution_payload || {};
		const execRes = resPayload.execution_result || {};
		const jumpedKey = execRes.ro_thread_key || null;

		el.innerHTML = `
			${buildEventHeader(event)}
			<h3 class="gate-title">${escapeHtml(schema.title || event.body_text || actionType)}</h3>
			${summaryHtml}
			<div class="event-card__meta-stamp">
				<span>Status: <strong>${escapeHtml(event.resolution_state)}</strong></span>
				${event.resolved_by ? `<span>By: ${escapeHtml(event.resolved_by)}</span>` : ""}
			</div>
			${jumpedKey ? `<div><button type="button" class="btn btn-sm btn-primary" data-jump-thread="${escapeHtml(jumpedKey)}">Open ${escapeHtml(jumpedKey)} &rarr;</button></div>` : ""}
		`;

		const jumpBtn = el.querySelector("[data-jump-thread]");
		if (jumpBtn && onJumpThread) {
			jumpBtn.addEventListener("click", () => onJumpThread(jumpBtn.dataset.jumpThread));
		}
		return el;
	}

	// Read-only view for ISM Technician
	if (!canApprove) {
		el.innerHTML = `
			${buildEventHeader(event)}
			<h3 class="gate-title">${escapeHtml(schema.title || event.body_text || actionType)}</h3>
			${summaryHtml}
			<div class="gate-readonly-banner">Pending Manager Approval</div>
		`;
		return el;
	}

	// Manager interactive controls
	let interactiveHtml = "";

	// 1. Editable sanitized customer message
	if (
		actionType === "DISPATCH_CUSTOMER_SMS" ||
		actionType === "DISPATCH_CUSTOMER_EMAIL" ||
		actionType === "SEND_QUOTE_APPROVAL"
	) {
		const msg = schema.message_preview || params.message || "";
		interactiveHtml += `
			<div class="gate-field-group">
				<label class="gate-field-label">Sanitized Customer Message (Editable)</label>
				<textarea class="gate-textarea" data-field="edited_message">${escapeHtml(msg)}</textarea>
			</div>
		`;
	}

	// 2. Payment method pills for CHECKOUT / DEPOSIT / PAY_PURCHASE
	if (
		actionType === "CHECKOUT_REPAIR_ORDER" ||
		actionType === "RECORD_DEPOSIT" ||
		actionType === "PAY_PURCHASE"
	) {
		const methods =
			Array.isArray(schema.options) && schema.options.length
				? schema.options.map((o) => ({ label: o.label || o.value, value: o.value || o.label }))
				: DEFAULT_PAYMENT_METHODS.map((m) => ({ label: m, value: m }));
		const defaultMethod = params.payment_method || methods[0].value;

		if (actionType === "CHECKOUT_REPAIR_ORDER") {
			const odoVal = params.odometer_out ?? schema.odometer_out ?? "";
			interactiveHtml += `
				<div class="gate-field-group">
					<label class="gate-field-label">Verified Odometer Out</label>
					<input type="number" inputmode="numeric" class="gate-input" data-patch="odometer_out" value="${escapeHtml(odoVal)}" placeholder="Enter odometer reading..." />
				</div>
			`;
		}

		if (actionType === "RECORD_DEPOSIT" || actionType === "PAY_PURCHASE") {
			const refVal = params.reference_no || "";
			interactiveHtml += `
				<div class="gate-field-group">
					<label class="gate-field-label">Reference # (Optional)</label>
					<input type="text" class="gate-input" data-patch="reference_no" value="${escapeHtml(refVal)}" placeholder="Auth / e-transfer ref..." />
				</div>
			`;
		}

		interactiveHtml += `
			<div class="gate-field-group">
				<span class="gate-field-label">Payment Method</span>
				<div class="gate-pill-group" data-choice-group="payment_method">
					${methods
						.map(
							(m) =>
								`<button type="button" class="gate-choice-pill ${m.value === defaultMethod ? "is-selected" : ""}" data-choice-val="${escapeHtml(m.value)}">${escapeHtml(m.label)}</button>`
						)
						.join("")}
				</div>
			</div>
		`;
	}

	// 3. Slot picker for RESCHEDULE_APPOINTMENT / INTAKE_QUOTE_AND_BOOK
	if (actionType === "RESCHEDULE_APPOINTMENT" || actionType === "INTAKE_QUOTE_AND_BOOK") {
		const slotOptions = Array.isArray(schema.options) ? schema.options : [];
		if (slotOptions.length) {
			interactiveHtml += `
				<div class="gate-field-group">
					<span class="gate-field-label">Select Time Slot (1-Tap)</span>
					<div class="gate-pill-group" data-choice-group="slot_index">
						${slotOptions
							.map(
								(opt, idx) =>
									`<button type="button" class="gate-choice-pill ${idx === 0 ? "is-selected" : ""}" data-choice-val="${escapeHtml(opt.value ?? idx)}">${escapeHtml(opt.label)}</button>`
							)
							.join("")}
					</div>
				</div>
			`;
		}

		if (actionType === "RESCHEDULE_APPOINTMENT") {
			const checked = schema.send_customer_sms_default ? "checked" : "";
			interactiveHtml += `
				<label class="gate-toggle-row">
					<input type="checkbox" data-patch-bool="send_customer_sms" ${checked} />
					<span>Send Customer ETA SMS</span>
				</label>
			`;
			if (schema.customer_eta_preview) {
				interactiveHtml += `
					<div class="gate-field-group">
						<label class="gate-field-label">Customer ETA SMS Preview</label>
						<textarea class="gate-textarea" data-field="edited_message">${escapeHtml(schema.customer_eta_preview)}</textarea>
					</div>
				`;
			}
		}
	}

	// 4. Walk-in conversion odometer_in
	if (actionType === "CONVERT_APPOINTMENT_TO_RO") {
		interactiveHtml += `
			<div class="gate-field-group">
				<label class="gate-field-label">Verified Odometer In (Required)</label>
				<input type="number" inputmode="numeric" class="gate-input" data-patch="odometer_in" value="${escapeHtml(params.odometer_in ?? "")}" placeholder="Enter odometer in..." />
			</div>
		`;
	}

	// 5. AP Vendor Bill fields (POST_PURCHASE_INVOICE)
	if (actionType === "POST_PURCHASE_INVOICE") {
		const updateStockChecked = schema.update_stock || params.update_stock ? "checked" : "";
		const alsoReceiveChecked = params.also_receive_po ? "checked" : "";
		interactiveHtml += `
			<div class="gate-kv-grid">
				<div class="gate-field-group">
					<label class="gate-field-label">Vendor Bill #</label>
					<input type="text" class="gate-input" data-patch="bill_no" value="${escapeHtml(params.bill_no || "")}" placeholder="INV-12345" />
				</div>
				<div class="gate-field-group">
					<label class="gate-field-label">Bill Date</label>
					<input type="date" class="gate-input" data-patch="bill_date" value="${escapeHtml(params.bill_date || "")}" />
				</div>
			</div>
			<label class="gate-toggle-row">
				<input type="checkbox" data-patch-bool="update_stock" ${updateStockChecked} />
				<span>Update Stock directly on Invoice</span>
			</label>
			<label class="gate-toggle-row">
				<input type="checkbox" data-patch-bool="also_receive_po" ${alsoReceiveChecked} />
				<span>Also create Purchase Receipt against PO</span>
			</label>
		`;
	}

	const approveLabel = getApproveButtonLabel(actionType);

	el.innerHTML = `
		${buildEventHeader(event)}
		<h3 class="gate-title">${escapeHtml(schema.title || event.body_text || actionType)}</h3>
		${summaryHtml}
		${interactiveHtml}
		<div class="gate-actions">
			<button type="button" class="btn btn-green" data-gate-decision="approve">${escapeHtml(approveLabel)}</button>
			<button type="button" class="btn btn-danger" data-gate-decision="reject">Reject</button>
		</div>
	`;

	// Wire 1-tap choice pill groups
	for (const group of el.querySelectorAll("[data-choice-group]")) {
		const pills = group.querySelectorAll(".gate-choice-pill");
		for (const pill of pills) {
			pill.addEventListener("click", () => {
				for (const other of pills) other.classList.remove("is-selected");
				pill.classList.add("is-selected");
			});
		}
	}

	const handleDecision = async (decision) => {
		if (_inFlightCards.has(event.name)) return;

		const formPatch = {};
		let selectedOption = null;
		let editedMessage = null;

		const msgBox = el.querySelector('[data-field="edited_message"]');
		if (msgBox) editedMessage = msgBox.value.trim();

		for (const input of el.querySelectorAll("[data-patch]")) {
			const field = input.dataset.patch;
			const raw = input.value.trim();
			if (raw !== "") {
				formPatch[field] = input.type === "number" ? Number(raw) : raw;
			}
		}

		for (const cb of el.querySelectorAll("[data-patch-bool]")) {
			formPatch[cb.dataset.patchBool] = Boolean(cb.checked);
		}

		for (const group of el.querySelectorAll("[data-choice-group]")) {
			const key = group.dataset.choiceGroup;
			const active = group.querySelector(".gate-choice-pill.is-selected");
			if (active) {
				const val = active.dataset.choiceVal;
				if (key === "slot_index") {
					formPatch.slot_index = Number.isNaN(Number(val)) ? val : Number(val);
					selectedOption = val;
				} else {
					formPatch[key] = val;
					selectedOption = val;
				}
			}
		}

		_inFlightCards.add(event.name);
		disableCardControls(el, true);
		try {
			const res = await onResolveCard({
				event_name: event.name,
				decision,
				selected_option: selectedOption,
				edited_message: editedMessage,
				form_patch: Object.keys(formPatch).length ? formPatch : null,
				idempotency_key: getActionIdempotencyKey(event.name, decision),
			});
			clearCardIdempotencyKey(event.name, decision);

			// Jump to newly created RO thread after Intake booking / walk-in check-in
			const nextThreadKey =
				res && res.execution_result && res.execution_result.ro_thread_key;
			if (decision === "approve" && nextThreadKey && onJumpThread) {
				onJumpThread(nextThreadKey);
			}
		} finally {
			_inFlightCards.delete(event.name);
			if (el.isConnected) disableCardControls(el, false);
		}
	};

	const approveBtn = el.querySelector('[data-gate-decision="approve"]');
	const rejectBtn = el.querySelector('[data-gate-decision="reject"]');
	if (approveBtn && onResolveCard) {
		approveBtn.addEventListener("click", () => handleDecision("approve"));
	}
	if (rejectBtn && onResolveCard) {
		rejectBtn.addEventListener("click", () => handleDecision("reject"));
	}

	return el;
}

export function renderEventCard(event, handlers = {}) {
	if (!event) return document.createDocumentFragment();
	const type = event.payload_type;

	if (type === "STATE_MUTATION" && event.actor_type === "SYSTEM") {
		return renderSystemNotificationPill(event);
	}
	if (type === "CARD_RESOLUTION") {
		return renderCardResolutionPill(event);
	}
	if (type === "AI_RECEIPT") {
		return renderReceiptCard(event, handlers);
	}
	if (type === "CLARIFICATION_CARD") {
		return renderClarificationCard(event, handlers);
	}
	if (type === "APPROVAL_GATE") {
		return renderApprovalGateCard(event, handlers);
	}
	return renderCaptureCard(event, handlers);
}
