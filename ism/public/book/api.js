/**
 * Guest Self-Serve Booking API client (vanilla ES module).
 * Always sends credentials: "same-origin" so the browser persists Set-Cookie: ism_portal_session.
 */

const API_BASE = "/api/method/ism.api.booking";
const PORTAL_API_BASE = "/api/method/ism.api.portal";

export class BookingApiError extends Error {
	constructor(message, details = {}) {
		super(message || "Unable to complete request. Please try again.");
		this.name = "BookingApiError";
		this.status = Number.isInteger(details.status) ? details.status : 0;
		this.errorCode = details.errorCode || "REQUEST_FAILED";
		this.retryAfterSeconds =
			typeof details.retryAfterSeconds === "number" ? details.retryAfterSeconds : null;
		this.attemptsRemaining =
			typeof details.attemptsRemaining === "number" ? details.attemptsRemaining : null;
		this.otpVerified = Boolean(details.otpVerified);
		this.lockedUntil = details.lockedUntil || null;
		this.challengeId = details.challengeId || null;
		this.payload = details.payload || null;
	}
}

function getCsrfToken() {
	const meta = document.querySelector('meta[name="csrf-token"]');
	const token = meta ? meta.getAttribute("content") : "";
	return token ? token.trim() : "";
}

function stripTags(text) {
	return String(text || "").replace(/<[^>]*>/g, "").trim();
}

function parseServerMessages(raw) {
	if (!raw) return "";
	try {
		const list = typeof raw === "string" ? JSON.parse(raw) : raw;
		if (Array.isArray(list) && list.length > 0) {
			const first = typeof list[0] === "string" ? JSON.parse(list[0]) : list[0];
			if (first && typeof first === "object" && first.message) {
				return stripTags(first.message);
			}
			if (typeof first === "string") {
				return stripTags(first);
			}
		}
	} catch {
		// Ignore malformed _server_messages payloads.
	}
	return "";
}

function extractRetrySeconds(obj) {
	if (!obj || typeof obj !== "object") return null;
	if (typeof obj.retry_after_seconds === "number" && obj.retry_after_seconds > 0) {
		return obj.retry_after_seconds;
	}
	if (typeof obj.resend_available_in_seconds === "number" && obj.resend_available_in_seconds > 0) {
		return obj.resend_available_in_seconds;
	}
	return null;
}

async function callBookingApi(
	methodName,
	{ httpMethod = "POST", params = null, body = null, apiBase = API_BASE } = {}
) {
	let url = `${apiBase}.${methodName}`;
	const headers = {
		Accept: "application/json",
	};

	const init = {
		method: httpMethod,
		credentials: "same-origin",
		headers,
	};

	if (httpMethod === "GET" && params) {
		const qs = new URLSearchParams();
		for (const [key, value] of Object.entries(params)) {
			if (value === undefined || value === null || value === "") continue;
			qs.set(key, typeof value === "object" ? JSON.stringify(value) : String(value));
		}
		const queryStr = qs.toString();
		if (queryStr) {
			url = `${url}?${queryStr}`;
		}
	} else if (httpMethod === "POST") {
		headers["Content-Type"] = "application/json";
		const csrfToken = getCsrfToken();
		if (csrfToken) {
			headers["X-Frappe-CSRF-Token"] = csrfToken;
		}
		init.body = JSON.stringify(body || {});
	}

	let response;
	try {
		response = await fetch(url, init);
	} catch {
		throw new BookingApiError(
			"Unable to reach the booking service. Please check your connection and try again.",
			{ status: 0, errorCode: "NETWORK_ERROR" }
		);
	}

	let rawJson = null;
	try {
		rawJson = await response.json();
	} catch {
		rawJson = null;
	}

	const unwrapped =
		rawJson && typeof rawJson === "object" && "message" in rawJson ? rawJson.message : rawJson;
	const payload = unwrapped && typeof unwrapped === "object" ? unwrapped : null;

	if (!response.ok || (payload && payload.success === false)) {
		const serverMsg = parseServerMessages(rawJson && rawJson._server_messages);
		const errorMessage =
			(payload && typeof payload.error === "string" && payload.error) ||
			serverMsg ||
			(typeof unwrapped === "string" && stripTags(unwrapped)) ||
			"Unable to complete your request. Please try again.";

		throw new BookingApiError(errorMessage, {
			status: response.status,
			errorCode: (payload && payload.error_code) || `HTTP_${response.status}`,
			retryAfterSeconds: extractRetrySeconds(payload),
			attemptsRemaining:
				payload && typeof payload.attempts_remaining === "number"
					? payload.attempts_remaining
					: null,
			otpVerified: Boolean(payload && payload.otp_verified),
			lockedUntil: (payload && payload.locked_until) || null,
			challengeId: (payload && payload.challenge_id) || null,
			payload,
		});
	}

	if (!payload) {
		throw new BookingApiError("Unexpected response from the booking service.", {
			status: response.status,
			errorCode: "INVALID_RESPONSE",
		});
	}

	return payload;
}

export function fetchPackages({ vehicle_model, vehicle_year, vehicle_generation, search } = {}) {
	return callBookingApi("get_booking_packages", {
		httpMethod: "GET",
		params: {
			vehicle_model,
			vehicle_year,
			vehicle_generation,
			search,
		},
	});
}

export function fetchSlots({
	package_ids,
	start_date,
	days_ahead = 14,
	vehicle_model,
	vehicle_year,
	vehicle_generation,
	max_slots_per_day,
} = {}) {
	return callBookingApi("get_booking_slots", {
		httpMethod: "GET",
		params: {
			package_ids: Array.isArray(package_ids) ? JSON.stringify(package_ids) : package_ids,
			start_date,
			days_ahead,
			vehicle_model,
			vehicle_year,
			vehicle_generation,
			max_slots_per_day,
		},
	});
}

export function requestBookingOtp(payload) {
	return callBookingApi("request_booking_otp", {
		httpMethod: "POST",
		body: payload,
	});
}

export function confirmSelfServeBooking(payload) {
	return callBookingApi("confirm_self_serve_booking", {
		httpMethod: "POST",
		body: payload,
	});
}

export function submitBookingPinToDrive({ schedule_entry, pin }) {
	return callBookingApi("submit_pin_to_drive", {
		httpMethod: "POST",
		apiBase: PORTAL_API_BASE,
		body: { schedule_entry, pin },
	});
}

export function optOutBookingPinToDrive({ schedule_entry }) {
	return callBookingApi("opt_out_pin_to_drive", {
		httpMethod: "POST",
		apiBase: PORTAL_API_BASE,
		body: { schedule_entry },
	});
}
