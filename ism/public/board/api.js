/**
 * Authenticated REST API client for ISM Shop OS Blackboard (/board).
 * Vanilla ES module — zero build step.
 */

const METHOD_BASE = "/api/method/ism.api.board";

let _offlineCallback = null;

export function setOfflineHandler(fn) {
	_offlineCallback = typeof fn === "function" ? fn : null;
}

function notifyOffline(isOffline, reason = "") {
	if (_offlineCallback) {
		try {
			_offlineCallback(Boolean(isOffline), reason);
		} catch (_err) {
			// ignore callback errors
		}
	}
}

export function getCsrfToken() {
	if (window.ISM_BOARD_BOOT && window.ISM_BOARD_BOOT.csrf_token) {
		return window.ISM_BOARD_BOOT.csrf_token;
	}
	if (window.frappe && window.frappe.csrf_token) {
		return window.frappe.csrf_token;
	}
	return "";
}

export function generateIdempotencyKey() {
	if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
		return crypto.randomUUID();
	}
	return "idem-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
}

export function parseFrappeError(payload, fallbackMessage = "Request failed.") {
	if (!payload) return fallbackMessage;
	if (typeof payload === "string") return payload;

	if (payload._server_messages) {
		try {
			const outer = JSON.parse(payload._server_messages);
			if (Array.isArray(outer) && outer.length > 0) {
				const first = typeof outer[0] === "string" ? JSON.parse(outer[0]) : outer[0];
				if (first && first.message) {
					return String(first.message).replace(/<[^>]+>/g, "").trim();
				}
			}
		} catch (_err) {
			// fall through
		}
	}

	if (payload.message && typeof payload.message === "string") {
		return payload.message;
	}
	if (payload.exception && typeof payload.exception === "string") {
		const parts = payload.exception.split(":");
		return parts.length > 1 ? parts.slice(1).join(":").trim() : payload.exception;
	}
	return fallbackMessage;
}

async function callJsonEndpoint(methodName, params = {}, { httpMethod = "POST" } = {}) {
	const url = new URL(`${METHOD_BASE}.${methodName}`, window.location.origin);
	const headers = {
		Accept: "application/json",
		"X-Frappe-CSRF-Token": getCsrfToken(),
	};

	const fetchOpts = {
		method: httpMethod,
		credentials: "same-origin",
		headers,
	};

	if (httpMethod === "GET") {
		for (const [k, v] of Object.entries(params || {})) {
			if (v !== undefined && v !== null && v !== "") {
				url.searchParams.set(k, typeof v === "object" ? JSON.stringify(v) : String(v));
			}
		}
	} else {
		headers["Content-Type"] = "application/json";
		const cleaned = {};
		for (const [k, v] of Object.entries(params || {})) {
			if (v !== undefined && v !== null) {
				cleaned[k] = v;
			}
		}
		fetchOpts.body = JSON.stringify(cleaned);
	}

	let response;
	try {
		response = await fetch(url.toString(), fetchOpts);
	} catch (networkErr) {
		notifyOffline(true, "Network error — check Wi-Fi connection.");
		const err = new Error("Network error — unable to reach ISM server.");
		err.isNetworkError = true;
		err.cause = networkErr;
		throw err;
	}

	let data = null;
	try {
		data = await response.json();
	} catch (_err) {
		data = null;
	}

	if (!response.ok) {
		if (response.status >= 502 && response.status <= 504) {
			notifyOffline(true, `Server unavailable (${response.status}).`);
		}
		const msg = parseFrappeError(data, `Request failed (${response.status}).`);
		const err = new Error(msg);
		err.status = response.status;
		err.payload = data;
		throw err;
	}

	notifyOffline(false);
	return data && data.message !== undefined ? data.message : data;
}

export async function listBoardThreads({
	status = "OPEN",
	assigned_only = false,
	query = null,
	limit = 100,
} = {}) {
	return callJsonEndpoint(
		"list_board_threads",
		{ status, assigned_only, query, limit },
		{ httpMethod: "GET" }
	);
}

export async function getBoardThread({
	thread_key = null,
	repair_order = null,
	limit = 100,
	before_event = null,
} = {}) {
	return callJsonEndpoint(
		"get_board_thread",
		{ thread_key, repair_order, limit, before_event },
		{ httpMethod: "GET" }
	);
}

export async function getThreadDelta({
	thread_key = null,
	repair_order = null,
	after_event = null,
	known_projection_version = 0,
} = {}) {
	return callJsonEndpoint(
		"get_thread_delta",
		{ thread_key, repair_order, after_event, known_projection_version },
		{ httpMethod: "GET" }
	);
}

export function postBoardCapture({
	thread_key = null,
	repair_order = null,
	text = "",
	intent_tag = null,
	metadata = {},
	idempotency_key = null,
	files = [],
	onProgress = null,
} = {}) {
	const idem = idempotency_key || generateIdempotencyKey();

	if (!files || files.length === 0) {
		return callJsonEndpoint("post_board_capture", {
			thread_key,
			repair_order,
			text: text || "",
			intent_tag,
			metadata,
			idempotency_key: idem,
		});
	}

	return new Promise((resolve, reject) => {
		const xhr = new XMLHttpRequest();
		const formData = new FormData();
		if (thread_key) formData.append("thread_key", thread_key);
		if (repair_order) formData.append("repair_order", repair_order);
		if (text) formData.append("text", text);
		if (intent_tag) formData.append("intent_tag", intent_tag);
		formData.append("metadata", JSON.stringify(metadata || {}));
		formData.append("idempotency_key", idem);

		files.forEach((fileObj, idx) => {
			const blob = fileObj.blob || fileObj;
			const filename = fileObj.filename || fileObj.name || `capture-${idx + 1}.bin`;
			formData.append("files", blob, filename);
		});

		xhr.open("POST", `${METHOD_BASE}.post_board_capture`, true);
		xhr.withCredentials = true;
		xhr.setRequestHeader("Accept", "application/json");
		xhr.setRequestHeader("X-Frappe-CSRF-Token", getCsrfToken());

		if (xhr.upload && typeof onProgress === "function") {
			xhr.upload.addEventListener("progress", (evt) => {
				if (evt.lengthComputable && evt.total > 0) {
					onProgress(Math.round((evt.loaded / evt.total) * 100));
				}
			});
		}

		xhr.onload = () => {
			let parsed = null;
			try {
				parsed = JSON.parse(xhr.responseText || "{}");
			} catch (_err) {
				parsed = null;
			}

			if (xhr.status >= 200 && xhr.status < 300) {
				notifyOffline(false);
				resolve(parsed && parsed.message !== undefined ? parsed.message : parsed);
			} else {
				if (xhr.status >= 502 && xhr.status <= 504) {
					notifyOffline(true, `Upload failed (${xhr.status}).`);
				}
				const err = new Error(parseFrappeError(parsed, `Upload failed (${xhr.status}).`));
				err.status = xhr.status;
				err.payload = parsed;
				reject(err);
			}
		};

		xhr.onerror = () => {
			notifyOffline(true, "Upload interrupted — network error.");
			const err = new Error("Network error during upload.");
			err.isNetworkError = true;
			reject(err);
		};

		xhr.send(formData);
	});
}

export async function listBoardActionFeed({
	status = "Open",
	repair_order = null,
	priority = null,
	limit = 50,
} = {}) {
	return callJsonEndpoint(
		"list_board_action_feed",
		{ status, repair_order, priority, limit },
		{ httpMethod: "GET" }
	);
}

export async function updateBoardActionCard({
	action_item,
	action,
	notes = null,
} = {}) {
	return callJsonEndpoint("update_board_action_card", {
		action_item,
		action,
		notes,
	});
}

export async function revealRoPin(repairOrder) {
	return callJsonEndpoint("reveal_ro_pin", {
		repair_order: repairOrder,
	});
}

export function getMediaUrl(eventName, fileRef) {
	return (
		`${METHOD_BASE}.get_media` +
		`?event_name=${encodeURIComponent(String(eventName || ""))}` +
		`&file_ref=${encodeURIComponent(String(fileRef || ""))}`
	);
}
