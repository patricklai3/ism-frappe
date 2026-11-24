/**
 * Authenticated REST API client for ISM Technician Execution Workbench (/tech).
 * Vanilla ES module — zero build step.
 */

import { parseFrappeError } from "../board/api.js";

const TECH_METHOD_BASE = "/api/method/ism.api.tech";

let _offlineCallback = null;

export function setTechOfflineHandler(fn) {
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

export function getTechCsrfToken() {
	if (window.ISM_TECH_BOOT && window.ISM_TECH_BOOT.csrf_token) {
		return window.ISM_TECH_BOOT.csrf_token;
	}
	if (window.frappe && window.frappe.csrf_token) {
		return window.frappe.csrf_token;
	}
	return "";
}

async function callTechEndpoint(methodName, params = {}, { httpMethod = "POST" } = {}) {
	const url = new URL(`${TECH_METHOD_BASE}.${methodName}`, window.location.origin);
	const headers = {
		Accept: "application/json",
		"X-Frappe-CSRF-Token": getTechCsrfToken(),
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

export async function listMyJobs({
	include_completed = false,
	query = null,
	technician = null,
	limit = 50,
} = {}) {
	return callTechEndpoint(
		"list_my_jobs",
		{
			include_completed: include_completed ? 1 : 0,
			query,
			technician,
			limit,
		},
		{ httpMethod: "GET" }
	);
}

export async function getTechRepairOrder(repairOrder) {
	return callTechEndpoint(
		"get_tech_repair_order",
		{ repair_order: repairOrder },
		{ httpMethod: "GET" }
	);
}

export async function getTechScheduleEntry(scheduleEntry) {
	return callTechEndpoint(
		"get_tech_schedule_entry",
		{ schedule_entry: scheduleEntry },
		{ httpMethod: "GET" }
	);
}

export async function pickUpVehicle(repairOrder) {
	return callTechEndpoint("pick_up_vehicle", { repair_order: repairOrder });
}

export async function toggleServiceItem({ repair_order, service_row_id, is_completed }) {
	return callTechEndpoint("toggle_service_item", {
		repair_order,
		service_row_id,
		is_completed: is_completed ? 1 : 0,
	});
}

export async function dropOffVehicle({ repair_order, odometer_out, temporary = false }) {
	return callTechEndpoint("drop_off_vehicle", {
		repair_order,
		odometer_out,
		temporary: temporary ? 1 : 0,
	});
}

export async function updateInspectionItem({ repair_order, item_row_id, status, notes = null }) {
	return callTechEndpoint("update_inspection_item", {
		repair_order,
		item_row_id,
		status,
		notes,
	});
}

export async function revealScheduleEntryPin(scheduleEntry) {
	return callTechEndpoint("reveal_schedule_entry_pin", {
		schedule_entry: scheduleEntry,
	});
}
