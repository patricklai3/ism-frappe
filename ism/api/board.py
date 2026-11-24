# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Whitelisted REST API facade for Active-RO Shop OS Blackboard (/board) & Cockpit (/cockpit)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from ism.api._validation import (
	coerce_bool,
	escape_like,
	optional_int,
	optional_str,
	parse_json_arg,
	require_str,
)
from ism.api.agent_serializers import build_ro_urls
from ism.board import cockpit_view, media_storage
from ism.board.contracts import (
	ACTION_CARD_PRIORITIES,
	BOARD_STATUSES,
	CAPTURE_LOCKED_RO_STATUSES,
	is_routine_system_action_item,
	parse_ro_from_thread_key,
	ro_thread_key,
)
from ism.board.gateways import (
	_as_system_user,
	_lazy_enqueue_board_event,
	_publish_board_event,
	assert_can_manage_board_actions,
	assert_can_manage_media_storage,
	resolve_actor_role,
	update_board_action_card_status,
)
from ism.board.orchestrator import should_skip_ai_for_capture
from ism.board.projections import (
	recompute_thread_projection,
	resolve_thread_key,
)
from ism.ism.doctype.shop_action_item.clearing import (
	CLEARING_CONDITION_DESCRIPTIONS,
	sweep_open_action_items,
)
from ism.permissions import (
	assert_can_access_ro,
	get_eligible_ro_names_for_employee,
	get_employee_for_user,
)
from ism.utils.vehicle_display import (
	build_vehicle_display_dict,
	get_vehicle_display_map,
)


def _parse_json_safe(raw: Any, default: Any) -> Any:
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	if isinstance(raw, str):
		try:
			return json.loads(raw)
		except Exception:
			return default
	return default


def _get_event_fields() -> list[str]:
	fields = [
		"name",
		"thread",
		"thread_type",
		"repair_order",
		"event_timestamp",
		"creation",
		"actor_type",
		"actor_user",
		"intent_tag",
		"payload_type",
		"target_role",
		"body_text",
		"transcript",
		"confidence_score",
		"metadata_json",
		"media_attachments",
		"card_schema",
		"resolution_state",
		"resolved_by",
		"resolved_at",
		"resolution_payload",
		"parent_event",
		"idempotency_key",
		"ai_status",
		"ai_error",
	]
	if frappe.db.has_column("Shop Board Event", "action_item"):
		fields.append("action_item")
	return fields


def _get_action_item_fields() -> list[str]:
	fields = [
		"name",
		"title",
		"category",
		"priority",
		"status",
		"source",
		"dedup_key",
		"snoozed_until",
		"reference_doctype",
		"reference_name",
		"item_code",
		"details",
		"reported_by",
		"resolved_by",
		"resolved_at",
		"resolution_notes",
		"creation",
		"modified",
	]
	for extra_col in (
		"board_event",
		"action_payload_json",
		"clearing_condition_type",
		"clearing_condition_json",
		"auto_cleared",
	):
		if frappe.db.has_column("Shop Action Item", extra_col):
			fields.append(extra_col)
	return fields


def _fetch_action_items_map(event_rows: list[Any]) -> dict[str, dict[str, Any]]:
	"""Batch-fetch live Shop Action Item rows referenced by ACTION_CARD events."""
	ids: list[str] = []
	for row in event_rows:
		get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
		if get("payload_type") != "ACTION_CARD":
			continue
		act_id = get("action_item")
		if not act_id:
			schema = _parse_json_safe(get("card_schema"), {})
			if isinstance(schema, dict):
				act_id = schema.get("action_item") or schema.get("name")
		if isinstance(act_id, str) and act_id.strip():
			ids.append(act_id.strip())

	unique_ids = list(dict.fromkeys(ids))
	if not unique_ids:
		return {}

	rows = frappe.get_all(
		"Shop Action Item",
		filters={"name": ["in", unique_ids]},
		fields=_get_action_item_fields(),
	)
	return {row["name"]: row for row in rows if row.get("name")}


def _clearing_description_from_row(row: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
	ctype = str(row.get("clearing_condition_type") or "manual").strip() or "manual"
	parsed = _parse_json_safe(row.get("clearing_condition_json"), {})
	if not isinstance(parsed, dict):
		parsed = {}
	desc = str(parsed.get("description") or "").strip() or CLEARING_CONDITION_DESCRIPTIONS.get(ctype, "")
	enriched = dict(parsed)
	if desc and not enriched.get("description"):
		enriched["description"] = desc
	return ctype, enriched, desc


def _fetch_exception_counts_map(thread_keys: list[str]) -> dict[str, int]:
	unique_keys = list(dict.fromkeys(k for k in thread_keys if k))
	if not unique_keys:
		return {}
	placeholders = ", ".join(["%s"] * len(unique_keys))
	rows = frappe.db.sql(
		f"""
		SELECT thread, COUNT(*) AS cnt
		FROM `tabShop Board Event`
		WHERE thread IN ({placeholders})
		  AND IFNULL(payload_type, '') != 'STATE_MUTATION'
		GROUP BY thread
		""",
		tuple(unique_keys),
		as_dict=True,
	)
	return {str(r["thread"]): cint(r["cnt"]) for r in rows if r.get("thread")}


def _serialize_thread(
	row: dict[str, Any] | Any,
	*,
	vehicle_map: dict[str, dict[str, Any]] | None = None,
	exception_event_count: int | None = None,
) -> dict[str, Any]:
	get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
	thread_key = get("thread_key") or get("name")
	encoded_key = quote(str(thread_key), safe="")
	vehicle_name = get("vehicle")
	vin = get("vin")
	stored_display = get("vehicle_display")
	v_info: dict[str, Any] | None = None
	if vehicle_name:
		if vehicle_map is not None:
			v_info = vehicle_map.get(vehicle_name)
		else:
			v_info = get_vehicle_display_map([vehicle_name]).get(vehicle_name)
	if not v_info:
		v_info = build_vehicle_display_dict(
			{"name": vehicle_name, "vin": vin} if (vehicle_name or vin) else None,
			fallback=stored_display or vehicle_name or vin,
		)
	vehicle_display = v_info.get("display") or stored_display or ""
	exc_count = (
		cint(exception_event_count)
		if exception_event_count is not None
		else cint(get("exception_event_count") or 0)
	)
	return {
		"name": thread_key,
		"thread_key": thread_key,
		"thread_type": get("thread_type") or "REPAIR_ORDER",
		"title": get("title"),
		"repair_order": get("repair_order"),
		"vehicle": vehicle_name,
		"vin": vin or v_info.get("vin"),
		"vehicle_display": vehicle_display,
		"vehicle_compact_display": v_info.get("compact_display") or vehicle_display,
		"vehicle_year": v_info.get("model_year"),
		"vehicle_model": v_info.get("model"),
		"vehicle_color": v_info.get("color"),
		"license_plate": v_info.get("license_plate"),
		"vehicle_info": v_info,
		"customer": get("customer"),
		"bay": get("bay"),
		"assigned_technician": get("assigned_technician"),
		"board_status": get("board_status") or "ACTIVE",
		"blocked_reason": get("blocked_reason") or "",
		"blocked_since": str(get("blocked_since")) if get("blocked_since") else None,
		"open_action_count": cint(get("open_action_count")),
		"open_action_card_ids": _parse_json_safe(get("open_action_card_ids"), []),
		"exception_event_count": exc_count,
		"last_event_at": str(get("last_event_at")) if get("last_event_at") else None,
		"last_event_preview": get("last_event_preview") or "",
		"projection_version": cint(get("projection_version") or 1),
		"board_url": f"/board?thread={encoded_key}",
	}


def _serialize_event(
	row: dict[str, Any] | Any,
	*,
	action_items_map: dict[str, dict[str, Any]] | None = None,
	thread_vehicle_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
	get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
	ev_name = get("name")
	raw_media = _parse_json_safe(get("media_attachments"), [])
	media_list: list[dict[str, Any]] = []
	if isinstance(raw_media, list):
		for item in raw_media:
			if not isinstance(item, dict):
				continue
			entry = dict(item)
			ref = entry.get("file_ref")
			if ref and ev_name:
				entry["url"] = (
					f"/api/method/ism.api.board.get_media"
					f"?event_name={quote(str(ev_name), safe='')}&file_ref={quote(str(ref), safe='')}"
				)
			media_list.append(entry)

	payload_type = get("payload_type")
	card_schema = _parse_json_safe(get("card_schema"), None)
	resolution_state = get("resolution_state") or "NONE"
	resolved_by = get("resolved_by")
	resolved_at = str(get("resolved_at")) if get("resolved_at") else None
	resolution_payload = _parse_json_safe(get("resolution_payload"), None)
	action_item = get("action_item")

	if payload_type == "ACTION_CARD":
		schema_dict = dict(card_schema) if isinstance(card_schema, dict) else {}
		act_id = action_item or schema_dict.get("action_item") or schema_dict.get("name")
		if act_id and not action_item:
			action_item = act_id
		sai = (action_items_map or {}).get(act_id) if act_id else None
		if sai:
			ctype, _cparams, clearing_desc = _clearing_description_from_row(sai)
			sai_status = sai.get("status") or "Open"
			auto_cleared = cint(sai.get("auto_cleared") or 0)
			res_notes = sai.get("resolution_notes") or ""

			schema_dict["action_item"] = sai["name"]
			schema_dict["status"] = sai_status
			schema_dict["auto_cleared"] = auto_cleared
			schema_dict["resolution_notes"] = res_notes
			schema_dict["clearing_condition_type"] = ctype
			schema_dict["clearing_condition"] = clearing_desc
			schema_dict["clearing_condition_description"] = clearing_desc
			if sai.get("priority"):
				schema_dict["priority"] = sai["priority"]
			if sai.get("category"):
				schema_dict["category"] = sai["category"]

			if sai_status == "Resolved":
				resolution_state = "RESOLVED"
				resolved_by = sai.get("resolved_by") or resolved_by
				resolved_at = str(sai.get("resolved_at")) if sai.get("resolved_at") else resolved_at
				res_p = dict(resolution_payload) if isinstance(resolution_payload, dict) else {}
				res_p.update(
					{
						"status": "Resolved",
						"auto_cleared": auto_cleared,
						"resolution_notes": res_notes,
						"resolved_by": resolved_by,
					}
				)
				resolution_payload = res_p
			elif sai_status == "Dismissed":
				resolution_state = "REJECTED"
				resolved_by = sai.get("resolved_by") or resolved_by
				resolved_at = str(sai.get("resolved_at")) if sai.get("resolved_at") else resolved_at
				res_p = dict(resolution_payload) if isinstance(resolution_payload, dict) else {}
				res_p.update(
					{
						"status": "Dismissed",
						"resolution_notes": res_notes,
						"resolved_by": resolved_by,
					}
				)
				resolution_payload = res_p
			elif sai_status in ("Open", "In Progress"):
				resolution_state = "PENDING"
				resolved_by = None
				resolved_at = None
				resolution_payload = None

		card_schema = schema_dict

	metadata = _parse_json_safe(get("metadata_json"), {})
	if isinstance(metadata, dict) and thread_vehicle_info and (
		metadata.get("vin") or metadata.get("repair_order") or get("repair_order")
	):
		metadata = dict(metadata)
		metadata.setdefault("vehicle_display", thread_vehicle_info.get("display") or "")
		metadata.setdefault("vehicle_compact_display", thread_vehicle_info.get("compact_display") or "")
		metadata.setdefault("vehicle_year", thread_vehicle_info.get("model_year"))
		metadata.setdefault("vehicle_model", thread_vehicle_info.get("model"))
		metadata.setdefault("vehicle_color", thread_vehicle_info.get("color"))
		metadata.setdefault("license_plate", thread_vehicle_info.get("license_plate"))
		metadata.setdefault("vehicle_info", thread_vehicle_info)

	return {
		"name": ev_name,
		"thread": get("thread"),
		"thread_type": get("thread_type"),
		"repair_order": get("repair_order"),
		"event_timestamp": str(get("event_timestamp")) if get("event_timestamp") else None,
		"actor_type": get("actor_type"),
		"actor_user": get("actor_user"),
		"intent_tag": get("intent_tag"),
		"payload_type": payload_type,
		"target_role": get("target_role") or "ALL",
		"body_text": get("body_text") or "",
		"transcript": get("transcript"),
		"action_item": action_item,
		"confidence_score": flt(get("confidence_score")) if get("confidence_score") is not None else None,
		"metadata": metadata,
		"media_attachments": media_list,
		"card_schema": card_schema,
		"resolution_state": resolution_state,
		"resolved_by": resolved_by,
		"resolved_at": resolved_at,
		"resolution_payload": resolution_payload,
		"parent_event": get("parent_event"),
		"idempotency_key": get("idempotency_key"),
		"ai_status": get("ai_status") or "NONE",
		"ai_error": get("ai_error"),
	}


def _get_ro_pin_state_safe(repair_order: str | None) -> dict[str, Any]:
	"""Seam 6 soft import: return non-sensitive PIN state for a Repair Order, never plaintext PIN."""
	absent_state = {
		"pin_feature_installed": False,
		"status": "Not Prompted",
		"has_pin": False,
		"can_reveal": False,
	}
	if not repair_order:
		return absent_state

	try:
		from ism.portal.pin_vault import get_ro_pin_state
	except ImportError:
		return absent_state

	state = get_ro_pin_state(repair_order)
	if not isinstance(state, dict):
		return absent_state

	result = dict(state)
	result.pop("pin", None)
	result.pop("plaintext_pin", None)
	result.setdefault("repair_order", repair_order)
	result.setdefault("pin_feature_installed", True)
	result.setdefault("status", "Not Prompted")
	result.setdefault("has_pin", False)
	result.setdefault("can_reveal", False)
	return result


def _get_ro_phone_key_state_safe(repair_order: str | None) -> dict[str, Any]:
	"""Soft import: return non-sensitive Tesla Phone Key state for Advisors/Managers, never invite_url."""
	absent_state = {
		"phone_key_feature_installed": False,
		"status": "Not Provided",
		"has_phone_key": False,
		"can_reveal": False,
	}
	if not repair_order:
		return absent_state

	try:
		from ism.portal.phone_key_vault import get_ro_phone_key_state
	except ImportError:
		return absent_state

	state = get_ro_phone_key_state(repair_order)
	if not isinstance(state, dict):
		return absent_state

	result = dict(state)
	result.pop("invite_url", None)
	result.pop("tesla_phone_key_url", None)
	result.setdefault("repair_order", repair_order)
	result.setdefault("phone_key_feature_installed", True)
	result.setdefault("status", "Not Provided")
	result.setdefault("has_phone_key", False)
	result.setdefault("can_reveal", False)
	return result


def _get_ro_summary(repair_order: str | None) -> dict[str, Any] | None:
	if not repair_order or not frappe.db.exists("Repair Order", repair_order):
		return None
	ro = frappe.db.get_value(
		"Repair Order",
		repair_order,
		[
			"name",
			"status",
			"approval_status",
			"customer",
			"vehicle",
			"odometer_in",
			"odometer_out",
			"progress_percent",
			"manual_parts_hold",
			"payment_status",
			"grand_total",
			"deposit_amount",
			"outstanding_amount",
			"vehicle_location",
		],
		as_dict=True,
	)
	if not ro:
		return None
	v_info = (
		get_vehicle_display_map([ro.vehicle]).get(ro.vehicle)
		if ro.vehicle
		else build_vehicle_display_dict(None)
	) or build_vehicle_display_dict(None, fallback=ro.vehicle)

	svc_rows = frappe.get_all(
		"Repair Order Service",
		filters={"parent": ro.name, "parenttype": "Repair Order", "status": "Approved"},
		fields=["is_completed"],
	)
	approved_count = len(svc_rows)
	completed_count = sum(1 for s in svc_rows if cint(s.get("is_completed")))

	return {
		"name": ro.name,
		"status": ro.status,
		"approval_status": ro.approval_status,
		"customer": ro.customer,
		"vehicle": ro.vehicle,
		"vehicle_display": v_info.get("display") or "",
		"vehicle_compact_display": v_info.get("compact_display") or "",
		"vehicle_color": v_info.get("color") or "",
		"license_plate": v_info.get("license_plate") or "",
		"vehicle_info": v_info,
		"odometer_in": ro.odometer_in,
		"odometer_out": ro.odometer_out,
		"approved_count": approved_count,
		"completed_count": completed_count,
		"progress_percent": flt(ro.progress_percent, 1),
		"manual_parts_hold": cint(ro.manual_parts_hold),
		"payment_status": ro.payment_status,
		"grand_total": flt(ro.grand_total, 2),
		"deposit_amount": flt(ro.deposit_amount, 2),
		"outstanding_amount": flt(ro.outstanding_amount, 2),
		"vehicle_location": ro.vehicle_location,
		"urls": build_ro_urls(ro.name),
	}


def _compute_capture_lock(ro_status: str | None) -> tuple[bool, str]:
	status = (ro_status or "").strip()
	if status in CAPTURE_LOCKED_RO_STATUSES:
		return False, f"Repair Order is {status} — thread is read-only."
	return True, ""


def _is_ai_auto_process_enabled() -> bool:
	try:
		with _as_system_user():
			settings = frappe.get_single("ISM Settings")
			provider = (getattr(settings, "board_ai_provider", None) or "Disabled").strip()
			auto_process = cint(getattr(settings, "board_ai_auto_process", 0))
	except Exception:
		return False
	return provider != "Disabled" and auto_process == 1


def _get_bound_request():
	try:
		req = getattr(frappe, "request", None)
		if req is None:
			return None
		getattr(req, "headers", None)
		return req
	except RuntimeError:
		return None


def _extract_uploaded_files() -> list[tuple[str, str, bytes]]:
	req = _get_bound_request()
	files_dict = getattr(req, "files", None) if req is not None else None
	if not files_dict:
		return []
	collected: list[tuple[str, str, bytes]] = []
	for field_key in files_dict.keys():
		for storage in files_dict.getlist(field_key):
			if storage is None:
				continue
			data_bytes = storage.read()
			if not data_bytes:
				continue
			collected.append(
				(
					getattr(storage, "filename", None) or "upload.bin",
					getattr(storage, "content_type", None) or getattr(storage, "mimetype", None) or "",
					data_bytes,
				)
			)
	return collected


def _parse_action_item_ids(raw: Any) -> list[str]:
	if raw is None or raw == "":
		return []
	if isinstance(raw, (list, tuple, set)):
		return list(dict.fromkeys(str(x).strip() for x in raw if str(x).strip()))
	if isinstance(raw, str):
		cleaned = raw.strip()
		if not cleaned:
			return []
		if cleaned.startswith("["):
			parsed = _parse_json_safe(cleaned, None)
			if isinstance(parsed, list):
				return list(dict.fromkeys(str(x).strip() for x in parsed if str(x).strip()))
		parts = [p.strip() for p in cleaned.split(",") if p.strip()]
		return list(dict.fromkeys(parts))
	frappe.throw(_("action_items must be a list of IDs, JSON array, or comma-separated string."), frappe.ValidationError)


def _serialize_action_feed_item(row: dict[str, Any]) -> dict[str, Any]:
	ro_name = row.get("reference_name") if row.get("reference_doctype") == "Repair Order" else None
	t_key = ro_thread_key(ro_name) if ro_name else None
	board_url = f"/board?thread={quote(t_key, safe='')}" if t_key else None
	desk_url = f"/app/shop-action-item/{quote(str(row['name']), safe='')}"
	ctype, cparams, clearing_desc = _clearing_description_from_row(row)
	action_payload = _parse_json_safe(row.get("action_payload_json"), {})
	if not isinstance(action_payload, dict):
		action_payload = {}

	return {
		"name": row["name"],
		"title": row.get("title") or row["name"],
		"category": row.get("category"),
		"priority": row.get("priority") or "Medium",
		"status": row.get("status") or "Open",
		"source": row.get("source") or "Manual",
		"dedup_key": row.get("dedup_key"),
		"reference_doctype": row.get("reference_doctype"),
		"reference_name": row.get("reference_name"),
		"repair_order": ro_name,
		"item_code": row.get("item_code"),
		"details": row.get("details") or "",
		"board_event": row.get("board_event"),
		"reported_by": row.get("reported_by"),
		"resolved_by": row.get("resolved_by"),
		"resolved_at": str(row.get("resolved_at")) if row.get("resolved_at") else None,
		"resolution_notes": row.get("resolution_notes") or "",
		"auto_cleared": cint(row.get("auto_cleared") or 0),
		"clearing_condition_type": ctype,
		"clearing_condition": clearing_desc,
		"clearing_condition_description": clearing_desc,
		"clearing_condition_params": cparams,
		"action_payload": action_payload,
		"thread_key": t_key,
		"board_url": board_url,
		"desk_url": desk_url,
		"creation": str(row.get("creation")) if row.get("creation") else None,
		"modified": str(row.get("modified")) if row.get("modified") else None,
	}


# ---------------------------------------------------------------------------
# Whitelisted Shop Board Endpoints (Active-RO Only)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_board_threads(
	status: str | None = "OPEN",
	assigned_only: Any = False,
	query: str | None = None,
	limit: int = 50,
) -> dict[str, Any]:
	"""List Active-RO Shop Board Threads (REPAIR_ORDER only; defaults to status='OPEN')."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user

	safe_limit = optional_int(limit, "limit", min_val=1, max_val=200, default=50)
	clean_status = optional_str(status, "status") or "OPEN"
	only_assigned = coerce_bool(assigned_only, default=False)
	clean_query = optional_str(query, "query")

	conditions: list[str] = ["thread_type = 'REPAIR_ORDER'"]
	values: list[Any] = []

	status_upper = clean_status.upper()
	if status_upper != "ALL":
		if status_upper == "OPEN":
			conditions.append("board_status != 'CLOSED'")
		else:
			if clean_status not in BOARD_STATUSES:
				frappe.throw(
					_("Invalid status '{0}'. Expected OPEN, ALL, or one of: {1}.").format(
						clean_status, ", ".join(BOARD_STATUSES)
					),
					frappe.ValidationError,
				)
			conditions.append("board_status = %s")
			values.append(clean_status)

	if actor_role == "TECH":
		emp_name = get_employee_for_user(caller_user)
		eligible_ros = sorted(get_eligible_ro_names_for_employee(emp_name)) if emp_name else []
		if not eligible_ros:
			return {
				"ok": True,
				"actor_role": actor_role,
				"can_manage": False,
				"can_approve": False,
				"threads": [],
				"count": 0,
			}
		placeholders = ", ".join(["%s"] * len(eligible_ros))
		conditions.append(f"repair_order IN ({placeholders})")
		values.extend(eligible_ros)
	elif only_assigned:
		emp_name = get_employee_for_user(caller_user)
		if not emp_name:
			return {
				"ok": True,
				"actor_role": actor_role,
				"can_manage": actor_role == "MANAGER",
				"can_approve": actor_role == "MANAGER",
				"threads": [],
				"count": 0,
			}
		conditions.append("assigned_technician = %s")
		values.append(emp_name)

	has_vehicle_display_col = bool(frappe.db.has_column("Shop Board Thread", "vehicle_display"))
	if clean_query:
		like_val = f"%{escape_like(clean_query)}%"
		if has_vehicle_display_col:
			conditions.append(
				"(thread_key LIKE %s OR title LIKE %s OR repair_order LIKE %s OR vin LIKE %s "
				"OR vehicle_display LIKE %s OR customer LIKE %s OR bay LIKE %s OR last_event_preview LIKE %s)"
			)
			values.extend([like_val] * 8)
		else:
			conditions.append(
				"(thread_key LIKE %s OR title LIKE %s OR repair_order LIKE %s OR vin LIKE %s "
				"OR customer LIKE %s OR bay LIKE %s OR last_event_preview LIKE %s)"
			)
			values.extend([like_val] * 7)

	where_sql = " AND ".join(conditions)
	values.append(safe_limit)
	vehicle_display_select = ", vehicle_display" if has_vehicle_display_col else ""
	with _as_system_user():
		rows = frappe.db.sql(
			f"""
			SELECT name, thread_key, thread_type, title, repair_order, vehicle, vin{vehicle_display_select}, customer,
			       bay, assigned_technician, board_status, blocked_reason, blocked_since,
			       open_action_count, open_action_card_ids, last_event_at, last_event_preview,
			       projection_version
			FROM `tabShop Board Thread`
			WHERE {where_sql}
			ORDER BY
			    open_action_count DESC,
			    COALESCE(last_event_at, modified) DESC
			LIMIT %s
			""",
			tuple(values),
			as_dict=True,
		)
		vehicle_map = get_vehicle_display_map([r.get("vehicle") for r in rows if r.get("vehicle")])
		exc_count_map = _fetch_exception_counts_map(
			[r.get("thread_key") or r.get("name") for r in rows if r.get("thread_key") or r.get("name")]
		)

	threads = [
		_serialize_thread(
			r,
			vehicle_map=vehicle_map,
			exception_event_count=exc_count_map.get(str(r.get("thread_key") or r.get("name") or ""), 0),
		)
		for r in rows
	]
	return {
		"ok": True,
		"actor_role": actor_role,
		"can_manage": actor_role == "MANAGER",
		"can_approve": actor_role == "MANAGER",
		"threads": threads,
		"count": len(threads),
	}


@frappe.whitelist()
def get_board_thread(
	thread_key: str | None = None,
	repair_order: str | None = None,
	limit: int = 100,
	before_event: str | None = None,
) -> dict[str, Any]:
	"""Return an active-RO thread's projection, paginated chronological timeline, live hydrated ACTION_CARDs, and Seam 6 pin_state."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	safe_limit = optional_int(limit, "limit", min_val=1, max_val=500, default=100)
	clean_before = optional_str(before_event, "before_event")

	with _as_system_user():
		canonical_key = resolve_thread_key(
			thread_key=optional_str(thread_key, "thread_key"),
			repair_order=optional_str(repair_order, "repair_order"),
		)
		ro_candidate = parse_ro_from_thread_key(canonical_key) or optional_str(repair_order, "repair_order")
		assert_can_access_ro(ro_candidate, user=caller_user)

		thread_doc = frappe.get_doc("Shop Board Thread", canonical_key)
		if thread_doc.repair_order and thread_doc.repair_order != ro_candidate:
			assert_can_access_ro(thread_doc.repair_order, user=caller_user)

		if thread_doc.repair_order:
			try:
				cleared = sweep_open_action_items(repair_order=thread_doc.repair_order, limit=50)
				if cleared:
					thread_doc.reload()
			except Exception:
				pass

		event_fields = _get_event_fields()
		cols_sql = ", ".join(event_fields)

		conditions = ["thread = %s", "IFNULL(payload_type, '') != 'STATE_MUTATION'"]
		values: list[Any] = [canonical_key]
		if clean_before:
			ref = frappe.db.get_value(
				"Shop Board Event",
				clean_before,
				["event_timestamp", "creation", "name"],
				as_dict=True,
			)
			if ref:
				conditions.append("(event_timestamp, creation, name) < (%s, %s, %s)")
				values.extend([ref.event_timestamp, ref.creation, ref.name])

		where_sql = " AND ".join(conditions)
		values.append(safe_limit + 1)
		raw_rows = frappe.db.sql(
			f"""
			SELECT {cols_sql}
			FROM `tabShop Board Event`
			WHERE {where_sql}
			ORDER BY event_timestamp DESC, creation DESC, name DESC
			LIMIT %s
			""",
			tuple(values),
			as_dict=True,
		)

		has_more = len(raw_rows) > safe_limit
		page_rows = raw_rows[:safe_limit]
		page_rows.reverse()  # Chronological ascending

		open_card_rows = frappe.get_all(
			"Shop Board Event",
			filters={
				"thread": canonical_key,
				"payload_type": "ACTION_CARD",
				"resolution_state": "PENDING",
			},
			fields=event_fields,
			order_by="event_timestamp asc, creation asc",
		)

		action_items_map = _fetch_action_items_map(page_rows + open_card_rows)
		thread_vehicle_map = get_vehicle_display_map([thread_doc.vehicle]) if thread_doc.vehicle else {}
		exc_count = _fetch_exception_counts_map([canonical_key]).get(canonical_key, 0)
		serialized_thread = _serialize_thread(
			thread_doc,
			vehicle_map=thread_vehicle_map,
			exception_event_count=exc_count,
		)
		thread_vehicle_info = serialized_thread.get("vehicle_info")
		events = [
			_serialize_event(r, action_items_map=action_items_map, thread_vehicle_info=thread_vehicle_info)
			for r in page_rows
		]
		open_cards = [
			_serialize_event(r, action_items_map=action_items_map, thread_vehicle_info=thread_vehicle_info)
			for r in open_card_rows
		]
		next_before_event = events[0]["name"] if (has_more and events) else None

		ro_for_pin = thread_doc.repair_order
		ro_summary = _get_ro_summary(ro_for_pin)
		can_post, read_only_reason = _compute_capture_lock((ro_summary or {}).get("status"))

	pin_state = _get_ro_pin_state_safe(ro_for_pin)
	phone_key_state = _get_ro_phone_key_state_safe(ro_for_pin)

	return {
		"ok": True,
		"actor_role": actor_role,
		"can_manage": actor_role == "MANAGER",
		"can_approve": actor_role == "MANAGER",
		"can_post": can_post,
		"read_only_reason": read_only_reason,
		"thread": serialized_thread,
		"events": events,
		"open_cards": open_cards,
		"has_more": has_more,
		"next_before_event": next_before_event,
		"pin_state": pin_state,
		"phone_key_state": phone_key_state,
		"repair_order_summary": ro_summary,
	}


@frappe.whitelist()
def get_thread_delta(
	thread_key: str | None = None,
	repair_order: str | None = None,
	after_event: str | None = None,
	known_projection_version: int = 0,
) -> dict[str, Any]:
	"""Return incremental events, updated ACTION_CARDs, completed transcripts, and projection state."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	known_ver = optional_int(known_projection_version, "known_projection_version", min_val=0, default=0)
	clean_after = optional_str(after_event, "after_event")

	with _as_system_user():
		canonical_key = resolve_thread_key(
			thread_key=optional_str(thread_key, "thread_key"),
			repair_order=optional_str(repair_order, "repair_order"),
		)
		ro_candidate = parse_ro_from_thread_key(canonical_key) or optional_str(repair_order, "repair_order")
		assert_can_access_ro(ro_candidate, user=caller_user)

		thread_doc = frappe.get_doc("Shop Board Thread", canonical_key)
		if thread_doc.repair_order and thread_doc.repair_order != ro_candidate:
			assert_can_access_ro(thread_doc.repair_order, user=caller_user)

		if thread_doc.repair_order:
			try:
				cleared = sweep_open_action_items(repair_order=thread_doc.repair_order, limit=50)
				if cleared:
					thread_doc.reload()
			except Exception:
				pass

		event_fields = _get_event_fields()
		cols_sql = ", ".join(event_fields)

		conditions = ["thread = %s", "IFNULL(payload_type, '') != 'STATE_MUTATION'"]
		values: list[Any] = [canonical_key]
		if clean_after:
			ref = frappe.db.get_value(
				"Shop Board Event",
				clean_after,
				["event_timestamp", "creation", "name"],
				as_dict=True,
			)
			if ref:
				conditions.append("(event_timestamp, creation, name) > (%s, %s, %s)")
				values.extend([ref.event_timestamp, ref.creation, ref.name])

		where_sql = " AND ".join(conditions)
		raw_rows = frappe.db.sql(
			f"""
			SELECT {cols_sql}
			FROM `tabShop Board Event`
			WHERE {where_sql}
			ORDER BY event_timestamp ASC, creation ASC, name ASC
			LIMIT 200
			""",
			tuple(values),
			as_dict=True,
		)

		open_card_rows = frappe.get_all(
			"Shop Board Event",
			filters={
				"thread": canonical_key,
				"payload_type": "ACTION_CARD",
				"resolution_state": "PENDING",
			},
			fields=event_fields,
			order_by="event_timestamp asc, creation asc",
		)

		current_ver = cint(thread_doc.projection_version or 1)
		changed = current_ver > known_ver or bool(raw_rows)

		updated_rows_by_name: dict[str, dict[str, Any]] = {}
		if changed or clean_after:
			# 1. Terminal ACTION_CARD events (auto-cleared, resolved, or dismissed)
			terminal_card_rows = frappe.db.sql(
				f"""
				SELECT {cols_sql}
				FROM `tabShop Board Event`
				WHERE thread = %s
					AND payload_type = 'ACTION_CARD'
					AND resolution_state IN ('RESOLVED', 'REJECTED')
				ORDER BY modified DESC, event_timestamp DESC, creation DESC, name DESC
				LIMIT 50
				""",
				(canonical_key,),
				as_dict=True,
			)
			for r in terminal_card_rows:
				if r.get("name"):
					updated_rows_by_name[str(r["name"])] = r

			# 2. Recently completed/failed capture events (so STT transcripts and AI errors sync live)
			completed_capture_rows = frappe.db.sql(
				f"""
				SELECT {cols_sql}
				FROM `tabShop Board Event`
				WHERE thread = %s
					AND payload_type IN ('MEDIA_CAPTURE', 'TEXT')
					AND ai_status IN ('COMPLETED', 'FAILED')
				ORDER BY modified DESC, event_timestamp DESC, creation DESC, name DESC
				LIMIT 30
				""",
				(canonical_key,),
				as_dict=True,
			)
			for r in completed_capture_rows:
				if r.get("name") and str(r["name"]) not in updated_rows_by_name:
					updated_rows_by_name[str(r["name"])] = r

			# 3. Parent events of any newly arrived events
			parent_ids = sorted(
				{
					str(r["parent_event"])
					for r in raw_rows
					if r.get("parent_event") and str(r["parent_event"]) not in updated_rows_by_name
				}
			)
			if parent_ids:
				parent_rows = frappe.get_all(
					"Shop Board Event",
					filters={
						"thread": canonical_key,
						"name": ["in", parent_ids],
						"payload_type": ["!=", "STATE_MUTATION"],
					},
					fields=event_fields,
				)
				for r in parent_rows:
					if r.get("name"):
						updated_rows_by_name[str(r["name"])] = r

		ordered_updated_rows = sorted(
			updated_rows_by_name.values(),
			key=lambda r: (
				str(r.get("event_timestamp") or ""),
				str(r.get("creation") or ""),
				str(r.get("name") or ""),
			),
		)

		action_items_map = _fetch_action_items_map(raw_rows + open_card_rows + ordered_updated_rows)
		thread_vehicle_map = get_vehicle_display_map([thread_doc.vehicle]) if thread_doc.vehicle else {}
		exc_count = _fetch_exception_counts_map([canonical_key]).get(canonical_key, 0)
		serialized_thread = _serialize_thread(
			thread_doc,
			vehicle_map=thread_vehicle_map,
			exception_event_count=exc_count,
		)
		thread_vehicle_info = serialized_thread.get("vehicle_info")
		new_events = [
			_serialize_event(r, action_items_map=action_items_map, thread_vehicle_info=thread_vehicle_info)
			for r in raw_rows
		]
		open_cards = [
			_serialize_event(r, action_items_map=action_items_map, thread_vehicle_info=thread_vehicle_info)
			for r in open_card_rows
		]
		updated_cards = [
			_serialize_event(r, action_items_map=action_items_map, thread_vehicle_info=thread_vehicle_info)
			for r in ordered_updated_rows
		]

		ro_for_pin = thread_doc.repair_order
		ro_status = frappe.db.get_value("Repair Order", ro_for_pin, "status") if ro_for_pin else None
		can_post, read_only_reason = _compute_capture_lock(ro_status)

	pin_state = _get_ro_pin_state_safe(ro_for_pin)
	phone_key_state = _get_ro_phone_key_state_safe(ro_for_pin)

	return {
		"ok": True,
		"actor_role": actor_role,
		"can_manage": actor_role == "MANAGER",
		"can_post": can_post,
		"read_only_reason": read_only_reason,
		"thread_key": canonical_key,
		"projection_version": current_ver,
		"changed": changed,
		"thread": serialized_thread,
		"events": new_events,
		"open_cards": open_cards,
		"updated_cards": updated_cards,
		"pin_state": pin_state,
		"phone_key_state": phone_key_state,
	}


@frappe.whitelist()
def post_board_capture(
	thread_key: str | None = None,
	repair_order: str | None = None,
	text: str | None = None,
	intent_tag: str | None = None,
	metadata: dict[str, Any] | str | None = None,
	idempotency_key: str | None = None,
) -> dict[str, Any]:
	"""Append an immutable text/media capture event on an active RO thread and optionally run lightweight AI."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	clean_text = optional_str(text, "text")
	clean_intent = optional_str(intent_tag, "intent_tag")
	clean_idem = optional_str(idempotency_key, "idempotency_key")
	parsed_meta = parse_json_arg(metadata, {}, label="metadata")
	if not isinstance(parsed_meta, dict):
		frappe.throw(_("metadata must be a JSON object."), frappe.ValidationError)

	uploaded = _extract_uploaded_files()

	with _as_system_user():
		canonical_key = resolve_thread_key(
			thread_key=optional_str(thread_key, "thread_key"),
			repair_order=optional_str(repair_order, "repair_order"),
		)
		ro_candidate = parse_ro_from_thread_key(canonical_key) or optional_str(repair_order, "repair_order")
		assert_can_access_ro(ro_candidate, user=caller_user)

		thread_doc = frappe.get_doc("Shop Board Thread", canonical_key)
		if thread_doc.repair_order and thread_doc.repair_order != ro_candidate:
			assert_can_access_ro(thread_doc.repair_order, user=caller_user)

		# Idempotency short-circuit before storing media or creating events
		if clean_idem:
			existing_name = frappe.db.get_value("Shop Board Event", {"idempotency_key": clean_idem}, "name")
			if existing_name:
				existing_doc = frappe.get_doc("Shop Board Event", existing_name)
				existing_thread = frappe.get_doc("Shop Board Thread", existing_doc.thread)
				if existing_thread.repair_order:
					assert_can_access_ro(existing_thread.repair_order, user=caller_user)
				return {
					"ok": True,
					"duplicate": True,
					"event": _serialize_event(existing_doc),
					"thread": _serialize_thread(existing_thread),
					"ai_enqueued": False,
				}

		ro_status = (
			frappe.db.get_value("Repair Order", thread_doc.repair_order, "status")
			if thread_doc.repair_order
			else None
		)
		can_post, lock_reason = _compute_capture_lock(ro_status)
		if not can_post:
			frappe.throw(
				_(lock_reason or "Cannot post captures to a closed or cancelled Repair Order."),
				frappe.ValidationError,
			)

		if not clean_text and not uploaded:
			frappe.throw(
				_("Either text or at least one media file is required."),
				frappe.ValidationError,
			)

		stored_attachments: list[dict[str, Any]] = []
		for filename, content_type, data_bytes in uploaded:
			stored = media_storage.store_media_payload(
				filename=filename,
				content_type=content_type,
				data_bytes=data_bytes,
				thread_key=canonical_key,
				repair_order=thread_doc.repair_order,
				thread_name=thread_doc.name,
			)
			stored_attachments.append(stored)

		# Server-verified metadata (never trust client actor_type or file_refs)
		verified_meta = dict(parsed_meta)
		verified_meta.pop("actor_type", None)
		verified_meta.pop("media_attachments", None)
		verified_meta["verified_actor_role"] = actor_role
		verified_meta["verified_actor_user"] = caller_user
		verified_meta["media_count"] = len(stored_attachments)

		payload_type = "MEDIA_CAPTURE" if stored_attachments else "TEXT"
		skip_ai = should_skip_ai_for_capture(
			clean_text,
			clean_intent,
			stored_attachments,
			verified_meta,
		)
		enqueue_ai = (not skip_ai) and _is_ai_auto_process_enabled()
		initial_ai_status = "QUEUED" if enqueue_ai else "NONE"

		try:
			ev = frappe.get_doc(
				{
					"doctype": "Shop Board Event",
					"thread": canonical_key,
					"thread_type": "REPAIR_ORDER",
					"repair_order": thread_doc.repair_order,
					"event_timestamp": now_datetime(),
					"actor_type": actor_role,
					"actor_user": caller_user,
					"intent_tag": clean_intent,
					"payload_type": payload_type,
					"target_role": "ALL",
					"body_text": clean_text or "",
					"metadata_json": json.dumps(verified_meta, default=str),
					"media_attachments": (
						json.dumps(stored_attachments, default=str) if stored_attachments else None
					),
					"resolution_state": "NONE",
					"idempotency_key": clean_idem,
					"ai_status": initial_ai_status,
				}
			)
			ev.insert(ignore_permissions=True)
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			if clean_idem:
				existing_name = frappe.db.get_value(
					"Shop Board Event", {"idempotency_key": clean_idem}, "name"
				)
				if existing_name:
					existing_doc = frappe.get_doc("Shop Board Event", existing_name)
					return {
						"ok": True,
						"duplicate": True,
						"event": _serialize_event(existing_doc),
						"thread": _serialize_thread(thread_doc),
						"ai_enqueued": False,
					}
			raise

		projection = recompute_thread_projection(canonical_key, publish=True)
		_publish_board_event(ev)

		ai_enqueue_result: dict[str, Any] | None = None
		if enqueue_ai:
			ai_enqueue_result = _lazy_enqueue_board_event(ev.name)
			if isinstance(ai_enqueue_result, dict) and ai_enqueue_result.get("synchronous"):
				ev.reload()
				projection = recompute_thread_projection(canonical_key, publish=True)

	return {
		"ok": True,
		"duplicate": False,
		"event": _serialize_event(ev),
		"thread": projection,
		"ai_enqueued": bool(ai_enqueue_result and ai_enqueue_result.get("enqueued")),
		"ai_enqueue_info": ai_enqueue_result,
	}


@frappe.whitelist()
def list_board_action_feed(
	status: str | None = "Open",
	repair_order: str | None = None,
	priority: str | None = None,
	limit: int = 50,
) -> dict[str, Any]:
	"""Sweep clearing conditions and return enriched active-RO Shop Action Items for the /board Action Feed."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	clean_status = optional_str(status, "status") or "Open"
	clean_ro = optional_str(repair_order, "repair_order")
	clean_prio = optional_str(priority, "priority")
	safe_limit = optional_int(limit, "limit", min_val=1, max_val=200, default=50)

	if clean_prio and clean_prio not in ACTION_CARD_PRIORITIES:
		frappe.throw(
			_("Invalid priority '{0}'. Expected one of: {1}.").format(
				clean_prio, ", ".join(ACTION_CARD_PRIORITIES)
			),
			frappe.ValidationError,
		)

	eligible_ros: set[str] | None = None
	if actor_role == "TECH":
		emp_name = get_employee_for_user(caller_user)
		eligible_ros = get_eligible_ro_names_for_employee(emp_name) if emp_name else set()
		if not eligible_ros or (clean_ro and clean_ro not in eligible_ros):
			return {
				"ok": True,
				"actor_role": actor_role,
				"can_manage": False,
				"count": 0,
				"items": [],
			}

	with _as_system_user():
		try:
			sweep_open_action_items(repair_order=clean_ro, limit=100)
		except Exception:
			pass

		filters: dict[str, Any] = {
			"reference_doctype": "Repair Order",
			"source": ["!=", "Rule Snooze"],
		}
		status_lower = clean_status.lower()
		if status_lower == "open":
			filters["status"] = ["in", ["Open", "In Progress"]]
		elif status_lower != "all":
			filters["status"] = clean_status

		if clean_ro:
			filters["reference_name"] = clean_ro
		elif eligible_ros is not None:
			filters["reference_name"] = ["in", sorted(eligible_ros)]
		if clean_prio:
			filters["priority"] = clean_prio

		fetch_limit = max(safe_limit * 4, 200)
		rows = frappe.get_all(
			"Shop Action Item",
			filters=filters,
			fields=_get_action_item_fields(),
			order_by="creation desc",
			limit_page_length=fetch_limit,
		)

		# Exceptions-only Action Feed for ALL roles (routine approvals, PINs, Square payments, parts-ready live in /cockpit)
		rows = [r for r in rows if not is_routine_system_action_item(r)]

		# For Open feed without explicit RO, ensure linked ROs are active (not closed/cancelled)
		if status_lower == "open" and not clean_ro and rows:
			ro_names = list({r["reference_name"] for r in rows if r.get("reference_name")})
			if ro_names:
				locked_ros = set(
					frappe.get_all(
						"Repair Order",
						filters={
							"name": ["in", ro_names],
							"status": ["in", list(CAPTURE_LOCKED_RO_STATUSES)],
						},
						pluck="name",
					)
				)
				if locked_ros:
					rows = [r for r in rows if r.get("reference_name") not in locked_ros]

		rows = rows[:safe_limit]
		items = [_serialize_action_feed_item(r) for r in rows]

	return {
		"ok": True,
		"actor_role": actor_role,
		"can_manage": actor_role == "MANAGER",
		"count": len(items),
		"items": items,
	}


@frappe.whitelist()
def update_board_action_card(
	action_item: str,
	action: str,
	notes: str | None = None,
) -> dict[str, Any]:
	"""Manager/Advisor-only manual fallback to dismiss, resolve, or reopen a Shop Action Item card."""
	result = update_board_action_card_status(
		action_item=require_str(action_item, "action_item"),
		action=require_str(action, "action"),
		notes=optional_str(notes, "notes"),
		user=frappe.session.user,
	)
	return {"ok": True, **result}


@frappe.whitelist()
def get_action_cards_context(
	action_items: list[str] | str | None = None,
	repair_order: str | None = None,
	status: str | None = "Open",
	include_events_limit: int = 20,
	limit: int | None = None,
) -> dict[str, Any]:
	"""Manager/Advisor-only single-request Agent OS handoff bundle for 1..N action cards or an RO."""
	assert_can_manage_board_actions()

	requested_ids = _parse_action_item_ids(action_items)
	clean_ro = optional_str(repair_order, "repair_order")
	clean_status = optional_str(status, "status") or "Open"
	events_limit = optional_int(include_events_limit, "include_events_limit", min_val=1, max_val=100, default=20)
	safe_limit = optional_int(limit, "limit", min_val=1, max_val=100, default=50)

	with _as_system_user():
		try:
			sweep_open_action_items(repair_order=clean_ro, limit=100)
		except Exception:
			pass

		sai_fields = _get_action_item_fields()
		if requested_ids:
			raw_items = frappe.get_all(
				"Shop Action Item",
				filters={"name": ["in", requested_ids]},
				fields=sai_fields,
			)
			by_name = {r["name"]: r for r in raw_items}
			ordered_rows = [by_name[act_id] for act_id in requested_ids if act_id in by_name]
		else:
			filters: dict[str, Any] = {
				"reference_doctype": "Repair Order",
				"source": ["!=", "Rule Snooze"],
			}
			if clean_ro:
				filters["reference_name"] = clean_ro
			status_lower = clean_status.lower()
			if status_lower == "open":
				filters["status"] = ["in", ["Open", "In Progress"]]
			elif status_lower != "all":
				filters["status"] = clean_status

			ordered_rows = frappe.get_all(
				"Shop Action Item",
				filters=filters,
				fields=sai_fields,
				order_by="creation desc",
				limit_page_length=safe_limit,
			)

		event_fields = _get_event_fields()
		trigger_event_ids = list(
			dict.fromkeys(
				str(r["board_event"]).strip()
				for r in ordered_rows
				if isinstance(r.get("board_event"), str) and r["board_event"].strip()
			)
		)
		trigger_events_map: dict[str, dict[str, Any]] = {}
		if trigger_event_ids:
			ev_rows = frappe.get_all(
				"Shop Board Event",
				filters={"name": ["in", trigger_event_ids]},
				fields=event_fields,
			)
			for ev_row in ev_rows:
				trigger_events_map[ev_row["name"]] = _serialize_event(ev_row)

		enriched_items: list[dict[str, Any]] = []
		ro_names: list[str] = []
		if clean_ro and frappe.db.exists("Repair Order", clean_ro):
			ro_names.append(clean_ro)

		for row in ordered_rows:
			base = _serialize_action_feed_item(row)
			ctype, cparams, clearing_desc = _clearing_description_from_row(row)
			trig_id = row.get("board_event")
			trig_ev = trigger_events_map.get(trig_id) if trig_id else None
			media_urls = [
				m["url"]
				for m in (trig_ev.get("media_attachments") or [])
				if isinstance(m, dict) and m.get("url")
			] if trig_ev else []

			base["clearing_condition_type"] = ctype
			base["clearing_condition"] = cparams
			base["clearing_condition_description"] = clearing_desc
			base["triggering_event"] = trig_ev
			base["media_urls"] = media_urls
			enriched_items.append(base)

			if base.get("repair_order") and base["repair_order"] not in ro_names:
				ro_names.append(base["repair_order"])

		threads_by_ro: dict[str, dict[str, Any]] = {}
		cols_sql = ", ".join(event_fields)
		for ro_name in ro_names:
			ro_summary = _get_ro_summary(ro_name)
			if not ro_summary:
				continue

			services = frappe.get_all(
				"Repair Order Service",
				filters={"parent": ro_name, "parenttype": "Repair Order"},
				fields=[
					"name",
					"service_title",
					"status",
					"labor_hours",
					"is_completed",
					"service_package",
					"parts",
					"advisor_notes",
				],
				order_by="idx asc",
			)

			t_key = ro_thread_key(ro_name)
			thread_serialized = None
			recent_events: list[dict[str, Any]] = []
			if frappe.db.exists("Shop Board Thread", t_key):
				thread_doc = frappe.get_doc("Shop Board Thread", t_key)
				thread_serialized = _serialize_thread(thread_doc)
				ev_rows = frappe.db.sql(
					f"""
					SELECT {cols_sql}
					FROM `tabShop Board Event`
					WHERE thread = %s
					ORDER BY event_timestamp DESC, creation DESC, name DESC
					LIMIT %s
					""",
					(t_key, events_limit),
					as_dict=True,
				)
				ev_rows.reverse()
				ev_ai_map = _fetch_action_items_map(ev_rows)
				recent_events = [_serialize_event(r, action_items_map=ev_ai_map) for r in ev_rows]

			threads_by_ro[ro_name] = {
				"repair_order": ro_summary,
				"services": services,
				"thread": thread_serialized,
				"recent_events": recent_events,
			}

	return {
		"ok": True,
		"count": len(enriched_items),
		"items": enriched_items,
		"threads_by_ro": threads_by_ro,
	}


@frappe.whitelist()
def get_media(event_name: str, file_ref: str):
	"""Authenticated streaming media proxy with file_ref IDOR validation and Range passthrough."""
	resolve_actor_role()
	caller_user = frappe.session.user
	clean_event = require_str(event_name, "event_name")
	clean_ref = require_str(file_ref, "file_ref")
	event_row = frappe.db.get_value(
		"Shop Board Event",
		clean_event,
		["name", "thread", "repair_order", "media_attachments"],
		as_dict=True,
	)
	if not event_row:
		frappe.throw(_("Board event not found."), frappe.PermissionError)
	ro_name = event_row.repair_order or parse_ro_from_thread_key(event_row.thread)
	assert_can_access_ro(ro_name, user=caller_user)

	media_refs = _parse_json_safe(event_row.media_attachments, [])
	req = _get_bound_request()
	range_header = req.headers.get("Range") if (req is not None and getattr(req, "headers", None)) else None
	with _as_system_user():
		stream_result = media_storage.stream_event_media(
			{"name": clean_event, "media_refs": media_refs},
			clean_ref,
			range_header=range_header,
			user=caller_user,
		)
	return media_storage.build_media_http_response(stream_result)


@frappe.whitelist()
def reveal_ro_pin(repair_order: str) -> dict[str, Any]:
	"""Seam 6 optional staff-only PIN-to-Drive reveal delegating to Project 4 pin_vault."""
	resolve_actor_role()
	clean_ro = require_str(repair_order, "repair_order")
	assert_can_access_ro(clean_ro, user=frappe.session.user)

	try:
		from ism.portal.pin_vault import get_ro_pin_for_staff
	except ImportError:
		return {
			"ok": True,
			"available": False,
			"pin_feature_installed": False,
			"repair_order": clean_ro,
		}

	res = get_ro_pin_for_staff(clean_ro, channel="Board")
	if isinstance(res, dict):
		return {
			"ok": True,
			"available": True,
			"pin_feature_installed": True,
			**res,
		}
	return {
		"ok": True,
		"available": False,
		"pin_feature_installed": True,
		"repair_order": clean_ro,
	}


@frappe.whitelist()
def reveal_ro_phone_key(repair_order: str) -> dict[str, Any]:
	"""Advisor/Manager-only Tesla Phone Key reveal delegating to ism.portal.phone_key_vault."""
	resolve_actor_role()
	clean_ro = require_str(repair_order, "repair_order")
	assert_can_access_ro(clean_ro, user=frappe.session.user)

	try:
		from ism.portal.phone_key_vault import get_ro_phone_key_for_staff
	except ImportError:
		return {
			"ok": True,
			"available": False,
			"phone_key_feature_installed": False,
			"repair_order": clean_ro,
		}

	res = get_ro_phone_key_for_staff(clean_ro, channel="Board")
	if isinstance(res, dict):
		return {
			"ok": True,
			"available": bool(res.get("available")),
			"phone_key_feature_installed": True,
			**res,
		}
	return {
		"ok": True,
		"available": False,
		"phone_key_feature_installed": True,
		"repair_order": clean_ro,
	}


# ---------------------------------------------------------------------------
# Manager / Advisor Only Cockpit & Storage Admin Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_cockpit_dashboard(date: str | None = None) -> dict[str, Any]:
	"""Return merged executive & dispatch cockpit view (Manager / Advisor / System Manager ONLY)."""
	assert_can_manage_board_actions()
	clean_date = optional_str(date, "date")
	with _as_system_user():
		view = cockpit_view.get_cockpit_view(date=clean_date)
	return {"ok": True, **view}


@frappe.whitelist()
def sync_cockpit_action(
	dedup_key: str,
	action: str,
	snooze_minutes: int | None = None,
	notes: str | None = None,
) -> dict[str, Any]:
	"""Snooze, dismiss, or reopen a cockpit Action Queue item (Manager / Advisor ONLY)."""
	assert_can_manage_board_actions()
	from ism.api import agent_fleet

	with _as_system_user():
		res = agent_fleet.sync_action_item_state(
			dedup_key=require_str(dedup_key, "dedup_key"),
			action=require_str(action, "action"),
			snooze_minutes=snooze_minutes,
			notes=optional_str(notes, "notes"),
		)
	if isinstance(res, dict):
		return {"ok": True, **res}
	return {"ok": True, "result": res}


@frappe.whitelist()
def test_media_storage() -> dict[str, Any]:
	"""Verify Google Drive media storage connectivity (ISM Manager / System Manager ONLY)."""
	assert_can_manage_media_storage()
	caller_user = frappe.session.user
	with _as_system_user():
		res = media_storage.test_google_drive_connection(user=caller_user)
	return {"ok": True, **res}


__all__ = [
	"get_action_cards_context",
	"get_board_thread",
	"get_cockpit_dashboard",
	"get_media",
	"get_thread_delta",
	"list_board_action_feed",
	"list_board_threads",
	"post_board_capture",
	"reveal_ro_phone_key",
	"reveal_ro_pin",
	"sync_cockpit_action",
	"test_media_storage",
	"update_board_action_card",
]
