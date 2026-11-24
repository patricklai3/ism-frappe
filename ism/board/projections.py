# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Active-RO thread provisioning, deterministic Andon projections, silent ACTION_CARD sync, and Seam 5 listeners."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from ism.board.contracts import (
	BLOCKED_BOARD_STATUSES,
	CLOSED_RO_STATUSES,
	BoardContractError,
	parse_ro_from_thread_key,
	ro_thread_key,
)
from ism.permissions import (
	_get_assigned_employees_for_ro,
	_get_enabled_users_for_employees,
	_is_ro_tech_ready,
	get_realtime_recipients_for_ro,
	is_ro_visible_to_user,
)


def _board_tables_ready() -> bool:
	return bool(
		frappe.db.exists("DocType", "Shop Board Thread")
		and frappe.db.exists("DocType", "Shop Board Event")
	)


def _truncate(text: str | None, max_len: int = 140) -> str:
	if not text:
		return ""
	cleaned = " ".join(str(text).split())
	if len(cleaned) <= max_len:
		return cleaned
	return cleaned[: max_len - 3].rstrip() + "..."


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


def _get_active_schedule_assignment(ro_name: str) -> tuple[str | None, str | None]:
	"""Return (service_bay, assigned_technician) from latest non-cancelled Schedule Entry for RO."""
	if not frappe.db.exists("DocType", "Schedule Entry"):
		return None, None
	rows = frappe.get_all(
		"Schedule Entry",
		filters={
			"repair_order": ro_name,
			"status": ["not in", ["Cancelled", "No Show"]],
		},
		fields=["service_bay", "assigned_technician"],
		order_by="scheduled_date desc, scheduled_time desc, modified desc",
		limit=1,
	)
	if not rows:
		return None, None
	row = rows[0]
	return row.get("service_bay") or None, row.get("assigned_technician") or None


def _build_ro_thread_metadata(ro_name: str) -> dict[str, Any]:
	"""Gather Repair Order, Repair Vehicle, and Schedule Entry metadata for thread title & links."""
	ro = frappe.db.get_value(
		"Repair Order",
		ro_name,
		["name", "customer", "vehicle", "status", "approval_status", "manual_parts_hold"],
		as_dict=True,
	)
	if not ro:
		return {}

	vehicle_name = ro.get("vehicle") or None
	vin = None
	vehicle_label = None
	vehicle_color = None
	license_plate = None
	vehicle_display = None
	if vehicle_name:
		veh = frappe.db.get_value(
			"Repair Vehicle",
			vehicle_name,
			["vin", "model_year", "model", "manufacturer", "color", "license_plate"],
			as_dict=True,
		)
		if veh:
			vin = veh.get("vin") or vehicle_name
			vehicle_color = str(veh.get("color") or "").strip() or None
			license_plate = str(veh.get("license_plate") or "").strip() or None
			parts = [str(veh.get("model_year") or "").strip(), str(veh.get("model") or "").strip()]
			vehicle_label = (
				" ".join(p for p in parts if p) or str(veh.get("manufacturer") or "").strip() or vehicle_name
			)
			display_parts = [
				vehicle_color or "",
				str(veh.get("model_year") or "").strip(),
				str(veh.get("manufacturer") or "").strip(),
				str(veh.get("model") or "").strip(),
			]
			vehicle_display = " ".join(p for p in display_parts if p) or vehicle_label

	bay, technician = _get_active_schedule_assignment(ro_name)

	title_parts = [ro_name]
	if vehicle_label:
		title_parts.append(vehicle_label)
	if bay:
		title_parts.append(bay)
	title = _truncate(" · ".join(title_parts), 140)

	return {
		"ro": ro,
		"title": title,
		"vehicle": vehicle_name,
		"vin": vin,
		"color": vehicle_color,
		"vehicle_color": vehicle_color,
		"license_plate": license_plate,
		"vehicle_display": vehicle_display,
		"customer": ro.get("customer") or None,
		"bay": bay,
		"assigned_technician": technician,
	}


def ensure_thread_for_ro(ro_name: str) -> str:
	"""Idempotently provision ``RO:<ro_name>`` thread with ``thread_type='REPAIR_ORDER'``."""
	thread_key = ro_thread_key(ro_name)
	canonical_ro = parse_ro_from_thread_key(thread_key)
	if not canonical_ro:
		raise BoardContractError(f"Invalid Repair Order name: {ro_name!r}.")

	if frappe.db.exists("Shop Board Thread", thread_key):
		return thread_key

	if not frappe.db.exists("Repair Order", canonical_ro):
		frappe.throw(
			_("Repair Order {0} does not exist.").format(canonical_ro),
			frappe.DoesNotExistError,
		)

	meta = _build_ro_thread_metadata(canonical_ro)
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Shop Board Thread",
				"thread_key": thread_key,
				"thread_type": "REPAIR_ORDER",
				"title": meta.get("title") or canonical_ro,
				"repair_order": canonical_ro,
				"vehicle": meta.get("vehicle"),
				"vin": meta.get("vin"),
				"customer": meta.get("customer"),
				"bay": meta.get("bay"),
				"assigned_technician": meta.get("assigned_technician"),
				"board_status": "ACTIVE",
				"open_action_count": 0,
				"open_action_card_ids": "[]",
				"projection_version": 1,
			}
		)
		doc.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		pass
	return thread_key


def resolve_thread_key(thread_key: str | None = None, repair_order: str | None = None) -> str:
	"""Accept ONLY ``RO:<ro_name>`` or a valid ``Repair Order`` name; reject all non-RO keys."""
	if thread_key is not None and isinstance(thread_key, str) and thread_key.strip():
		key = thread_key.strip()
		if key.upper().startswith("SHOP:"):
			raise BoardContractError(
				f"Legacy singleton board thread '{key}' is no longer supported. Only RO:<repair_order> threads are allowed."
			)
		ro_from_key = parse_ro_from_thread_key(key)
		if ro_from_key:
			return ensure_thread_for_ro(ro_from_key)
		if frappe.db.exists("Repair Order", key):
			return ensure_thread_for_ro(key)
		raise BoardContractError(
			f"Invalid Shop Board thread_key '{key}'. Expected 'RO:<repair_order>' or a valid Repair Order name."
		)

	if repair_order is not None and isinstance(repair_order, str) and repair_order.strip():
		ro_clean = repair_order.strip()
		if ro_clean.upper().startswith("SHOP:"):
			raise BoardContractError(
				f"Legacy singleton board thread '{ro_clean}' is not a valid Repair Order."
			)
		return ensure_thread_for_ro(ro_clean)

	raise BoardContractError("Either thread_key (RO:<repair_order>) or repair_order is required.")


def _derive_ro_board_status(
	ro: dict[str, Any],
	open_action_count: int,
) -> tuple[str, str]:
	"""Derive deterministic Andon board_status and blocked_reason for a Repair Order thread."""
	ro_status = ro.get("status") or ""
	approval_status = ro.get("approval_status") or ""
	manual_parts_hold = cint(ro.get("manual_parts_hold") or 0)

	if ro_status in CLOSED_RO_STATUSES:
		return "CLOSED", ""

	if ro_status == "Waiting on Parts" or manual_parts_hold == 1:
		return "BLOCKED_PARTS", "Waiting on Parts"

	if open_action_count > 0:
		label = f"{open_action_count} open action item" + ("s" if open_action_count != 1 else "")
		return "NEEDS_ACTION", label

	if approval_status == "Pending Approval" or ro_status == "Pending Authorization":
		return "WAITING_CUSTOMER", "Waiting on Customer Approval"

	return "ACTIVE", ""


def recompute_thread_projection(thread_key: str, *, publish: bool = True) -> dict[str, Any]:
	"""Deterministically recompute an active-RO thread's Andon projection and optionally publish realtime."""
	canonical_key = resolve_thread_key(thread_key=thread_key)
	thread = frappe.get_doc("Shop Board Thread", canonical_key)

	event_fields = ["name"]
	if frappe.db.has_column("Shop Board Event", "action_item"):
		event_fields.append("action_item")

	open_cards = frappe.get_all(
		"Shop Board Event",
		filters={
			"thread": canonical_key,
			"payload_type": "ACTION_CARD",
			"resolution_state": "PENDING",
		},
		fields=event_fields,
		order_by="event_timestamp asc, creation asc",
	)
	open_action_count = len(open_cards)
	open_action_card_ids = [row.get("action_item") or row["name"] for row in open_cards]

	latest_events = frappe.get_all(
		"Shop Board Event",
		filters={
			"thread": canonical_key,
			"payload_type": ["!=", "STATE_MUTATION"],
		},
		fields=["event_timestamp", "body_text", "transcript", "payload_type"],
		order_by="event_timestamp desc, creation desc",
		limit=1,
	)
	last_event_at = None
	last_event_preview = ""
	if latest_events:
		latest = latest_events[0]
		last_event_at = latest.get("event_timestamp")
		preview_src = latest.get("body_text") or latest.get("transcript") or latest.get("payload_type") or ""
		last_event_preview = _truncate(preview_src, 140)

	updates: dict[str, Any] = {
		"open_action_count": open_action_count,
		"open_action_card_ids": json.dumps(open_action_card_ids),
		"last_event_at": last_event_at,
		"last_event_preview": last_event_preview,
	}

	ro_name = thread.repair_order or parse_ro_from_thread_key(canonical_key)
	meta = _build_ro_thread_metadata(ro_name) if ro_name else {}
	ro = meta.get("ro") or {}
	new_status, blocked_reason = _derive_ro_board_status(ro, open_action_count)
	updates.update(
		{
			"title": meta.get("title") or thread.title,
			"vehicle": meta.get("vehicle"),
			"vin": meta.get("vin"),
			"customer": meta.get("customer"),
			"bay": meta.get("bay"),
			"assigned_technician": meta.get("assigned_technician"),
			"board_status": new_status,
			"blocked_reason": blocked_reason,
		}
	)

	prev_status = thread.board_status or "ACTIVE"
	prev_blocked_since = thread.blocked_since
	if updates["board_status"] in BLOCKED_BOARD_STATUSES:
		if prev_status in BLOCKED_BOARD_STATUSES and prev_blocked_since:
			blocked_since = prev_blocked_since
		else:
			blocked_since = now_datetime()
	else:
		blocked_since = None

	new_version = cint(thread.projection_version or 0) + 1
	updates["blocked_since"] = blocked_since
	updates["projection_version"] = new_version

	thread.db_set(updates, update_modified=True)

	payload = {
		"thread_key": canonical_key,
		"thread_type": "REPAIR_ORDER",
		"title": updates.get("title", thread.title),
		"repair_order": ro_name,
		"vehicle": updates.get("vehicle", thread.vehicle),
		"vin": updates.get("vin", thread.vin),
		"color": meta.get("color"),
		"vehicle_color": meta.get("vehicle_color"),
		"license_plate": meta.get("license_plate"),
		"vehicle_display": meta.get("vehicle_display"),
		"customer": updates.get("customer", thread.customer),
		"bay": updates.get("bay", thread.bay),
		"assigned_technician": updates.get("assigned_technician", thread.assigned_technician),
		"board_status": updates["board_status"],
		"blocked_reason": updates["blocked_reason"],
		"blocked_since": str(blocked_since) if blocked_since else None,
		"open_action_count": open_action_count,
		"open_action_card_ids": open_action_card_ids,
		"last_event_at": str(last_event_at) if last_event_at else None,
		"last_event_preview": last_event_preview,
		"projection_version": new_version,
	}

	if publish:
		frappe.publish_realtime(
			"ism_board_projection",
			payload,
			doctype="Shop Board Thread",
			docname=canonical_key,
			after_commit=True,
		)
		for recipient in get_realtime_recipients_for_ro(ro_name):
			frappe.publish_realtime(
				"ism_board_thread_summary",
				payload,
				user=recipient,
				after_commit=True,
			)

	return payload


def _publish_tech_queue_update_for_ro(doc) -> None:
	"""Notify affected technicians' user rooms when an RO enters, leaves, or updates within tech eligibility."""
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	prev_ready = _is_ro_tech_ready(before) if before else False
	curr_ready = _is_ro_tech_ready(doc)

	# Never emit realtime pings to technicians while an RO remains unapproved or on parts hold.
	if not (curr_ready or prev_ready):
		return

	affected_emps: set[str] = set(_get_assigned_employees_for_ro(doc.name))
	for row in getattr(doc, "services", None) or []:
		emp = (getattr(row, "assigned_technician", None) or "").strip()
		if emp:
			affected_emps.add(emp)
	if before is not None:
		for row in getattr(before, "services", None) or []:
			emp = (getattr(row, "assigned_technician", None) or "").strip()
			if emp:
				affected_emps.add(emp)

	if not affected_emps:
		return

	for user_id in sorted(_get_enabled_users_for_employees(affected_emps)):
		frappe.publish_realtime(
			"ism_tech_queue_updated",
			{
				"kind": "REPAIR_ORDER",
				"repair_order": doc.name,
				"eligible": is_ro_visible_to_user(doc.name, user=user_id),
				"reason": "repair_order_updated",
			},
			user=user_id,
			after_commit=True,
		)


def _publish_tech_queue_update_for_schedule_entry(doc, before=None) -> None:
	"""Notify previous and current assigned technicians when a Schedule Entry changes or is superseded by an RO."""
	curr_tech = (doc.get("assigned_technician") or "").strip()
	prev_tech = (before.get("assigned_technician") or "").strip() if before else ""
	affected_emps = {emp for emp in (curr_tech, prev_tech) if emp}
	if not affected_emps:
		return

	current_ro = (doc.get("repair_order") or "").strip() or None
	for user_id in sorted(_get_enabled_users_for_employees(affected_emps)):
		ro_eligible = bool(current_ro and is_ro_visible_to_user(current_ro, user=user_id))
		frappe.publish_realtime(
			"ism_tech_queue_updated",
			{
				"kind": "SCHEDULE_ENTRY",
				"schedule_entry": doc.name,
				"superseded_by_ro": current_ro,
				"ro_eligible": ro_eligible,
				"reason": "schedule_entry_updated",
			},
			user=user_id,
			after_commit=True,
		)


def on_repair_order_after_insert(doc, method=None):
	"""Auto-provision and project Shop Board Thread when a Repair Order is created."""
	if not _board_tables_ready():
		return
	thread_key = ensure_thread_for_ro(doc.name)
	recompute_thread_projection(thread_key, publish=True)


def on_repair_order_update(doc, method=None):
	"""Recompute Repair Order board projection and notify affected technicians when RO changes."""
	if _board_tables_ready():
		thread_key = ensure_thread_for_ro(doc.name)
		recompute_thread_projection(thread_key, publish=True)
	_publish_tech_queue_update_for_ro(doc)


def on_schedule_entry_update(doc, method=None):
	"""Sync bay/technician assignment onto linked Repair Order thread(s) and notify affected tech queues."""
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None

	if _board_tables_ready():
		affected_ros: list[str] = []
		current_ro = doc.get("repair_order")
		if current_ro and frappe.db.exists("Repair Order", current_ro):
			affected_ros.append(current_ro)

		prev_ro = before.get("repair_order") if before else None
		if prev_ro and prev_ro != current_ro and frappe.db.exists("Repair Order", prev_ro):
			affected_ros.append(prev_ro)

		for ro_name in affected_ros:
			thread_key = ensure_thread_for_ro(ro_name)
			recompute_thread_projection(thread_key, publish=True)

	_publish_tech_queue_update_for_schedule_entry(doc, before=before)


def project_external_domain_event(
	event_type: str,
	payload: dict[str, Any],
	*,
	idempotency_key: str | None = None,
) -> str | None:
	"""Project an external/Track A domain event strictly onto an existing ``RO:<name>`` thread.

	Returns ``None`` cleanly for non-RO events (e.g. unlinked provisional bookings stay in
	``Shop Action Item`` for ``/cockpit`` and Agent OS without creating any board thread/event).
	"""
	if not _board_tables_ready():
		return None

	clean_idem = (
		idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
	)
	if clean_idem:
		existing = frappe.db.get_value("Shop Board Event", {"idempotency_key": clean_idem}, "name")
		if existing:
			return existing

	data = dict(payload or {})
	target_ro: str | None = None

	explicit_key = data.get("thread_key")
	if isinstance(explicit_key, str) and explicit_key.strip():
		ro_candidate = parse_ro_from_thread_key(explicit_key.strip())
		if ro_candidate and frappe.db.exists("Repair Order", ro_candidate):
			target_ro = ro_candidate
		else:
			return None
	elif data.get("repair_order") and frappe.db.exists("Repair Order", data["repair_order"]):
		target_ro = str(data["repair_order"]).strip()
	elif data.get("schedule_entry"):
		linked_ro = frappe.db.get_value("Schedule Entry", data["schedule_entry"], "repair_order")
		if linked_ro and frappe.db.exists("Repair Order", linked_ro):
			target_ro = str(linked_ro).strip()
		else:
			return None
	else:
		return None

	if not target_ro:
		return None

	target_thread_key = ensure_thread_for_ro(target_ro)

	summary = data.get("summary") or data.get("title") or event_type
	details = data.get("details")
	body_text = str(summary)
	if details:
		body_text = f"{body_text}\n{details}"

	metadata = {"event_type": event_type, **data}

	try:
		ev = frappe.get_doc(
			{
				"doctype": "Shop Board Event",
				"thread": target_thread_key,
				"thread_type": "REPAIR_ORDER",
				"repair_order": target_ro,
				"event_timestamp": now_datetime(),
				"actor_type": "SYSTEM",
				"intent_tag": event_type,
				"payload_type": "STATE_MUTATION",
				"target_role": "ALL",
				"body_text": body_text,
				"metadata_json": json.dumps(metadata, default=str),
				"resolution_state": "NONE",
				"idempotency_key": clean_idem,
				"ai_status": "NONE",
			}
		)
		ev.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		if clean_idem:
			return frappe.db.get_value("Shop Board Event", {"idempotency_key": clean_idem}, "name")
		return None

	recompute_thread_projection(target_thread_key, publish=True)
	return ev.name


def _sync_linked_action_cards_in_place(doc) -> list[str]:
	"""Silently sync Shop Action Item status onto linked ACTION_CARD events (zero confirmation pill)."""
	if not frappe.db.has_column("Shop Board Event", "action_item"):
		return []

	card_rows = frappe.get_all(
		"Shop Board Event",
		filters={
			"payload_type": "ACTION_CARD",
			"action_item": doc.name,
		},
		fields=[
			"name",
			"thread",
			"thread_type",
			"repair_order",
			"event_timestamp",
			"actor_type",
			"actor_user",
			"intent_tag",
			"payload_type",
			"target_role",
			"body_text",
			"card_schema",
			"resolution_state",
			"resolved_by",
			"resolved_at",
			"resolution_payload",
			"parent_event",
			"ai_status",
			"action_item",
		],
	)
	if not card_rows:
		return []

	status = (doc.get("status") or "").strip()
	auto_cleared = cint(doc.get("auto_cleared") or 0)
	res_notes = doc.get("resolution_notes") or ""

	if status == "Resolved":
		target_res_state = "RESOLVED"
		target_res_by = doc.get("resolved_by") or frappe.session.user or "Administrator"
		target_res_at = doc.get("resolved_at") or now_datetime()
		res_payload_obj: dict[str, Any] | None = {
			"status": "Resolved",
			"auto_cleared": auto_cleared,
			"resolution_notes": res_notes,
			"resolved_by": target_res_by,
		}
		target_res_payload = json.dumps(res_payload_obj, default=str)
	elif status == "Dismissed":
		target_res_state = "REJECTED"
		target_res_by = doc.get("resolved_by") or frappe.session.user or "Administrator"
		target_res_at = doc.get("resolved_at") or now_datetime()
		res_payload_obj = {
			"status": "Dismissed",
			"resolution_notes": res_notes,
			"resolved_by": target_res_by,
		}
		target_res_payload = json.dumps(res_payload_obj, default=str)
	else:
		target_res_state = "PENDING"
		target_res_by = None
		target_res_at = None
		res_payload_obj = None
		target_res_payload = None

	touched_threads: list[str] = []
	updated_events: list[str] = []

	for row in card_rows:
		schema = _parse_json_safe(row.get("card_schema"), {})
		if not isinstance(schema, dict):
			schema = {}
		updated_schema = dict(schema)
		updated_schema["status"] = status or "Open"
		updated_schema["auto_cleared"] = auto_cleared
		updated_schema["resolution_notes"] = res_notes
		schema_str = json.dumps(updated_schema, default=str)

		already_synced = (
			row.get("resolution_state") == target_res_state
			and (row.get("resolution_payload") or None) == target_res_payload
			and (row.get("card_schema") or None) == schema_str
		)
		if already_synced:
			continue

		frappe.db.set_value(
			"Shop Board Event",
			row["name"],
			{
				"resolution_state": target_res_state,
				"resolved_by": target_res_by,
				"resolved_at": target_res_at,
				"resolution_payload": target_res_payload,
				"card_schema": schema_str,
			},
			update_modified=True,
		)
		updated_events.append(row["name"])

		thread_key = row.get("thread")
		if thread_key:
			if thread_key not in touched_threads:
				touched_threads.append(thread_key)
			frappe.publish_realtime(
				"ism_board_event",
				{
					"name": row["name"],
					"thread": thread_key,
					"thread_type": row.get("thread_type") or "REPAIR_ORDER",
					"repair_order": row.get("repair_order"),
					"event_timestamp": str(row.get("event_timestamp")) if row.get("event_timestamp") else None,
					"actor_type": row.get("actor_type"),
					"actor_user": row.get("actor_user"),
					"intent_tag": row.get("intent_tag"),
					"payload_type": "ACTION_CARD",
					"target_role": row.get("target_role") or "MANAGER",
					"body_text": row.get("body_text") or "",
					"action_item": doc.name,
					"card_schema": updated_schema,
					"resolution_state": target_res_state,
					"resolved_by": target_res_by,
					"resolved_at": str(target_res_at) if target_res_at else None,
					"resolution_payload": res_payload_obj,
					"parent_event": row.get("parent_event"),
					"ai_status": row.get("ai_status") or "NONE",
				},
				doctype="Shop Board Thread",
				docname=thread_key,
				after_commit=True,
			)

	for thread_key in touched_threads:
		try:
			recompute_thread_projection(thread_key, publish=True)
		except Exception:
			frappe.log_error(
				title=f"Shop Board Projection Sync Failed ({thread_key})",
				message=frappe.get_traceback(),
			)

	return updated_events


def on_shop_action_item_after_insert(doc, method=None):
	"""Exception-only Shop Board: Shop Action Items stay in /cockpit without emitting STATE_MUTATION board pills."""
	_ = doc, method
	return None


def on_shop_action_item_on_update(doc, method=None):
	"""Silently sync Shop Action Item status onto linked ACTION_CARD events (zero STATE_MUTATION pill)."""
	_ = method
	if not _board_tables_ready():
		return None

	_sync_linked_action_cards_in_place(doc)
	return None


__all__ = [
	"ensure_thread_for_ro",
	"on_repair_order_after_insert",
	"on_repair_order_update",
	"on_schedule_entry_update",
	"on_shop_action_item_after_insert",
	"on_shop_action_item_on_update",
	"project_external_domain_event",
	"recompute_thread_projection",
	"resolve_thread_key",
]
