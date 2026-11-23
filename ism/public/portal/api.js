/**
 * ISM Customer Portal REST API client & shared utilities (Vanilla ES Module).
 * All POST requests inject X-Frappe-CSRF-Token; GET/POST use same-origin cookies.
 */

const API_BASE = "/api/method/ism.api.portal.";

let cachedCsrfToken = "";

export function setCsrfToken(token) {
	if (typeof token === "string" && token.trim()) {
		cachedCsrfToken = token.trim();
	}
}

export function getCsrfToken() {
	if (cachedCsrfToken) {
		return cachedCsrfToken;
	}
	const meta = document.querySelector('meta[name="ism-csrf-token"]');
	if (meta && meta.content) {
		cachedCsrfToken = meta.content.trim();
		return cachedCsrfToken;
	}
	if (window.frappe && typeof window.frappe.csrf_token === "string") {
		return window.frappe.csrf_token;
	}
	return "";
}

/**
 * Remove sensitive ?token and ?booking_token query params from browser history immediately on boot.
 */
export function stripQueryTokens() {
	try {
		const url = new URL(window.location.href);
		let changed = false;
		for (const key of ["token", "booking_token"]) {
			if (url.searchParams.has(key)) {
				url.searchParams.delete(key);
				changed = true;
			}
		}
		if (changed && window.history && typeof window.history.replaceState === "function") {
			const cleanPath =
				url.pathname +
				(url.searchParams.toString() ? `?${url.searchParams.toString()}` : "") +
				(url.hash || "");
			window.history.replaceState({}, document.title, cleanPath);
		}
	} catch {
		// Ignore malformed URL edge cases in non-standard environments
	}
}

export function escapeHtml(value) {
	if (value === null || value === undefined) {
		return "";
	}
	return String(value)
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#39;");
}

export function formatCurrency(amount, currency = "CAD") {
	const num = Number(amount || 0);
	const formatted = num.toLocaleString("en-CA", {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2,
	});
	return `$${formatted} ${escapeHtml(currency || "CAD")}`;
}

export function generateIdempotencyKey() {
	if (window.crypto && typeof window.crypto.randomUUID === "function") {
		return window.crypto.randomUUID();
	}
	const rand = () => Math.floor((1 + Math.random()) * 0x10000).toString(16).slice(1);
	return `${rand()}${rand()}-${rand()}-4${rand().slice(1)}-8${rand().slice(1)}-${rand()}${rand()}${rand()}`;
}

export function decodeFrappeError(payload, fallback = "Something went wrong. Please try again.") {
	if (!payload || typeof payload !== "object") {
		return fallback;
	}
	if (payload._server_messages) {
		try {
			const list = JSON.parse(payload._server_messages);
			if (Array.isArray(list) && list.length > 0) {
				const first = JSON.parse(list[0]);
				if (first && typeof first.message === "string" && first.message.trim()) {
					return first.message.replace(/<[^>]+>/g, "").trim();
				}
			}
		} catch {
			// Fall through to other error fields
		}
	}
	if (typeof payload.error === "string" && payload.error.trim()) {
		return payload.error.trim();
	}
	if (typeof payload.message === "string" && payload.message.trim()) {
		return payload.message.trim();
	}
	if (payload.message && typeof payload.message.error === "string" && payload.message.error.trim()) {
		return payload.message.error.trim();
	}
	return fallback;
}

async function parseResponse(response) {
	let data = null;
	try {
		data = await response.json();
	} catch {
		data = null;
	}
	if (!response.ok) {
		const err = new Error(decodeFrappeError(data, `Request failed (${response.status}).`));
		err.status = response.status;
		err.payload = data;
		throw err;
	}
	return data && Object.prototype.hasOwnProperty.call(data, "message") ? data.message : data;
}

export async function portalGet(method, params = {}) {
	const search = new URLSearchParams();
	for (const [key, value] of Object.entries(params || {})) {
		if (value !== null && value !== undefined && value !== "") {
			search.set(key, String(value));
		}
	}
	const qs = search.toString();
	const url = `${API_BASE}${method}${qs ? `?${qs}` : ""}`;
	const response = await fetch(url, {
		method: "GET",
		credentials: "same-origin",
		headers: {
			Accept: "application/json",
		},
	});
	return parseResponse(response);
}

export async function portalPost(method, body = {}) {
	const headers = {
		Accept: "application/json",
		"Content-Type": "application/json",
	};
	const csrf = getCsrfToken();
	if (csrf) {
		headers["X-Frappe-CSRF-Token"] = csrf;
	}
	const response = await fetch(`${API_BASE}${method}`, {
		method: "POST",
		credentials: "same-origin",
		headers,
		body: JSON.stringify(body || {}),
	});
	return parseResponse(response);
}

/* Frozen REST Contract Wrappers */

export function getPortalBootstrap({ active_ro, active_schedule_entry } = {}) {
	return portalGet("get_portal_bootstrap", { active_ro, active_schedule_entry });
}

export function exchangePortalLink({ token, booking_token } = {}) {
	return portalPost("exchange_portal_link", { token, booking_token });
}

export function requestLoginOtp({ phone }) {
	return portalPost("request_login_otp", { phone });
}

export function verifyLoginOtp({ challenge_id, code, phone, upgrade_existing_session = false }) {
	return portalPost("verify_login_otp", {
		challenge_id,
		code,
		phone,
		upgrade_existing_session,
	});
}

export function signOut() {
	return portalPost("sign_out", {});
}

export function getPortalHub({ active_ro, active_schedule_entry } = {}) {
	return portalGet("get_portal_hub", { active_ro, active_schedule_entry });
}

export function submitPortalQuoteApproval({ repair_order, approvals, signer_name, signature_data }) {
	return portalPost("submit_portal_quote_approval", {
		repair_order,
		approvals: typeof approvals === "string" ? approvals : JSON.stringify(approvals),
		signer_name,
		signature_data,
	});
}

export function submitPinToDrive({ repair_order, schedule_entry, pin }) {
	const body = { pin };
	if (repair_order) {
		body.repair_order = repair_order;
	}
	if (schedule_entry) {
		body.schedule_entry = schedule_entry;
	}
	return portalPost("submit_pin_to_drive", body);
}

export function optOutPinToDrive({ repair_order, schedule_entry }) {
	const body = {};
	if (repair_order) {
		body.repair_order = repair_order;
	}
	if (schedule_entry) {
		body.schedule_entry = schedule_entry;
	}
	return portalPost("opt_out_pin_to_drive", body);
}

export function getRescheduleOptions({ schedule_entry, start_date, days_ahead } = {}) {
	return portalGet("get_reschedule_options", { schedule_entry, start_date, days_ahead });
}

export function rescheduleAppointment({ schedule_entry, target_date, customer_arrival_time, reason = "" }) {
	return portalPost("reschedule_appointment", {
		schedule_entry,
		target_date,
		customer_arrival_time,
		reason,
	});
}

export function submitPortalPayment({
	repair_order,
	payment_intent,
	source_id,
	idempotency_key,
	expected_amount_cents,
	buyer_email,
}) {
	return portalPost("submit_portal_payment", {
		repair_order,
		payment_intent,
		source_id,
		idempotency_key,
		expected_amount_cents,
		buyer_email,
	});
}

export function emailInvoiceCopy({ repair_order, recipient_email }) {
	return portalPost("email_invoice_copy", {
		repair_order,
		recipient_email: recipient_email || undefined,
	});
}
