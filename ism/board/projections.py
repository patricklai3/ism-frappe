# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Thread provisioning, deterministic Andon projections, and Seam 5 event listeners."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from ism.board.contracts import (
	BLOCKED_BOARD_STATUSES,
	CLOSED_RO_STATUSES,
	PENDING_DECISION_PAYLOAD_TYPES,
	SELF_ECHO_DEDUP_PREFIX,
	SINGLETON_THREADS,
	parse_ro_from_thread_key,
	ro_thread_key,
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
	if vehicle_name:
		veh = frappe.db.get_value(
			"Repair Vehicle",
			vehicle_name,
			["vin", "model_year", "model", "manufacturer"],
			as_dict=True,
		)
		if veh:
			vin = veh.get("vin") or vehicle_name
			parts = [str(veh.get("model_year") or "").strip(), str(veh.get("model") or "").strip()]
			vehicle_label = " ".join(p for p in parts if p) or str(veh.get("manufacturer") or "").strip() or vehicle_name

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
		"customer": ro.get("customer") or None,
		"bay": bay,
		"assigned_technician": technician,
	}


def ensure_singleton_shop_threads() -> list[str]:
	"""Idempotently ensure global singleton shop threads exist."""
	if not frappe.db.exists("DocType", "Shop Board Thread"):
		return []

	ensured: list[str] = []
	for thread_key, (thread_type, title) in SINGLETON_THREADS.items():
		if not frappe.db.exists("Shop Board Thread", thread_key):
			try:
				doc = frappe.get_doc(
					{
						"doctype": "Shop Board Thread",
						"thread_key": thread_key,
						"thread_type": thread_type,
						"title": title,
						"board_status": "ACTIVE",
						"open_action_count": 0,
						"open_approval_count": 0,
						"open_clarification_count": 0,
						"open_action_card_ids": "[]",
						"projection_version": 1,
					}
				)
				doc.insert(ignore_permissions=True)
			except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
				pass
		ensured.append(thread_key)
	return ensured


def ensure_thread_for_ro(ro_name: str) -> str:
	"""Idempotently provision ``RO:<ro_name>`` thread with ``thread_type='REPAIR_ORDER'``."""
	thread_key = ro_thread_key(ro_name)
	canonical_ro = parse_ro_from_thread_key(thread_key)
	if not canonical_ro:
		frappe.throw(_("Invalid Repair Order name: {0}").format(ro_name), frappe.ValidationError)

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
				"open_approval_count": 0,
				"open_clarification_count": 0,
				"open_action_card_ids": "[]",
				"projection_version": 1,
			}
		)
		doc.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		pass
	return thread_key


def resolve_thread_key(thread_key: str | None = None, repair_order: str | None = None) -> str:
	"""Accept canonical ``thread_key`` or bare ``repair_order``, ensuring thread existence lazily."""
	if thread_key and isinstance(thread_key, str) and thread_key.strip():
		key = thread_key.strip()
		if key in SINGLETON_THREADS:
			ensure_singleton_shop_threads()
			return key
		ro_from_key = parse_ro_from_thread_key(key)
		if ro_from_key:
			return ensure_thread_for_ro(ro_from_key)
		if frappe.db.exists("Shop Board Thread", key):
			return key
		if frappe.db.exists("Repair Order", key):
			return ensure_thread_for_ro(key)
		frappe.throw(
			_("Shop Board Thread {0} not found.").format(key),
			frappe.DoesNotExistError,
		)

	if repair_order and isinstance(repair_order, str) and repair_order.strip():
		return ensure_thread_for_ro(repair_order.strip())

	frappe.throw(
		_("Either thread_key or repair_order is required."),
		frappe.ValidationError,
	)


def _derive_ro_board_status(
	ro: dict[str, Any],
	open_approval_count: int,
	open_clarification_count: int,
) -> tuple[str, str]:
	ro_status = ro.get("status") or ""
	approval_status = ro.get("approval_status") or ""
	manual_parts_hold = int(ro.get("manual_parts_hold") or 0)

	if ro_status in CLOSED_RO_STATUSES:
		return "CLOSED", ""

	open_total = open_approval_count + open_clarification_count
	if open_total > 0:
		parts = []
		if open_approval_count:
			parts.append(f"{open_approval_count} pending approval" + ("s" if open_approval_count != 1 else ""))
		if open_clarification_count:
			parts.append(
				f"{open_clarification_count} pending clarification"
				+ ("s" if open_clarification_count != 1 else "")
			)
		return "BLOCKED_DECISION", ", ".join(parts)

	if ro_status == "Waiting on Parts" or manual_parts_hold == 1:
		return "BLOCKED_PARTS", "Waiting on Parts"

	if approval_status == "Pending Approval" or ro_status == "Pending Authorization":
		return "WAITING_CUSTOMER", "Waiting on Customer Approval"

	return "ACTIVE", ""


def recompute_thread_projection(thread_key: str, *, publish: bool = True) -> dict[str, Any]:
	"""Deterministically recompute a thread's Andon projection and optionally publish realtime updates."""
	canonical_key = resolve_thread_key(thread_key=thread_key)
	thread = frappe.get_doc("Shop Board Thread", canonical_key)

	open_cards = frappe.get_all(
		"Shop Board Event",
		filters={
			"thread": canonical_key,
			"payload_type": ["in", list(PENDING_DECISION_PAYLOAD_TYPES)],
			"resolution_state": "PENDING",
		},
		fields=["name", "payload_type"],
		order_by="event_timestamp asc, creation asc",
	)
	open_approval_count = sum(1 for row in open_cards if row.get("payload_type") == "APPROVAL_GATE")
	open_clarification_count = sum(1 for row in open_cards if row.get("payload_type") == "CLARIFICATION_CARD")
	open_action_count = open_approval_count + open_clarification_count
	open_action_card_ids = [row["name"] for row in open_cards]

	latest_events = frappe.get_all(
		"Shop Board Event",
		filters={"thread": canonical_key},
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
		"open_approval_count": open_approval_count,
		"open_clarification_count": open_clarification_count,
		"open_action_card_ids": json.dumps(open_action_card_ids),
		"last_event_at": last_event_at,
		"last_event_preview": last_event_preview,
	}

	if thread.thread_type == "REPAIR_ORDER" and thread.repair_order:
		meta = _build_ro_thread_metadata(thread.repair_order)
		ro = meta.get("ro") or {}
		new_status, blocked_reason = _derive_ro_board_status(
			ro, open_approval_count, open_clarification_count
		)
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
	else:
		if open_action_count > 0:
			new_status = "BLOCKED_DECISION"
			blocked_reason = f"{open_action_count} pending decision" + ("s" if open_action_count != 1 else "")
		else:
			new_status = "ACTIVE"
			blocked_reason = ""
		updates["board_status"] = new_status
		updates["blocked_reason"] = blocked_reason

	prev_status = thread.board_status or "ACTIVE"
	prev_blocked_since = thread.blocked_since
	if updates["board_status"] in BLOCKED_BOARD_STATUSES:
		if prev_status in BLOCKED_BOARD_STATUSES and prev_blocked_since:
			blocked_since = prev_blocked_since
		else:
			blocked_since = now_datetime()
	else:
		blocked_since = None

	new_version = int(thread.projection_version or 0) + 1
	updates["blocked_since"] = blocked_since
	updates["projection_version"] = new_version

	thread.db_set(updates, update_modified=True)

	payload = {
		"thread_key": canonical_key,
		"thread_type": thread.thread_type,
		"title": updates.get("title", thread.title),
		"repair_order": thread.repair_order,
		"vehicle": updates.get("vehicle", thread.vehicle),
		"vin": updates.get("vin", thread.vin),
		"customer": updates.get("customer", thread.customer),
		"bay": updates.get("bay", thread.bay),
		"assigned_technician": updates.get("assigned_technician", thread.assigned_technician),
		"board_status": updates["board_status"],
		"blocked_reason": updates["blocked_reason"],
		"blocked_since": str(blocked_since) if blocked_since else None,
		"open_action_count": open_action_count,
		"open_approval_count": open_approval_count,
		"open_clarification_count": open_clarification_count,
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
		frappe.publish_realtime(
			"ism_board_thread_summary",
			payload,
			after_commit=True,
		)

	return payload


def on_repair_order_after_insert(doc, method=None):
	"""Auto-provision and project Shop Board Thread when a Repair Order is created."""
	if not _board_tables_ready():
		return
	thread_key = ensure_thread_for_ro(doc.name)
	recompute_thread_projection(thread_key, publish=True)


def on_repair_order_update(doc, method=None):
	"""Recompute Repair Order board projection whenever the RO changes."""
	if not _board_tables_ready():
		return
	thread_key = ensure_thread_for_ro(doc.name)
	recompute_thread_projection(thread_key, publish=True)


def on_schedule_entry_update(doc, method=None):
	"""Sync bay and technician assignment onto linked Repair Order thread(s)."""
	if not _board_tables_ready():
		return

	affected_ros: list[str] = []
	current_ro = doc.get("repair_order")
	if current_ro and frappe.db.exists("Repair Order", current_ro):
		affected_ros.append(current_ro)

	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	prev_ro = before.get("repair_order") if before else None
	if prev_ro and prev_ro != current_ro and frappe.db.exists("Repair Order", prev_ro):
		affected_ros.append(prev_ro)

	for ro_name in affected_ros:
		thread_key = ensure_thread_for_ro(ro_name)
		recompute_thread_projection(thread_key, publish=True)


def project_external_domain_event(
	event_type: str,
	payload: dict[str, Any],
	*,
	idempotency_key: str | None = None,
) -> str | None:
	"""Project a decoupled external/Track A domain event onto the appropriate Shop Board Thread."""
	if not _board_tables_ready():
		return None

	clean_idem = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
	if clean_idem:
		existing = frappe.db.get_value("Shop Board Event", {"idempotency_key": clean_idem}, "name")
		if existing:
			return existing

	data = dict(payload or {})
	target_thread_key = None

	explicit_key = data.get("thread_key")
	if isinstance(explicit_key, str) and explicit_key.strip():
		target_thread_key = resolve_thread_key(thread_key=explicit_key.strip())
	elif data.get("repair_order") and frappe.db.exists("Repair Order", data["repair_order"]):
		target_thread_key = ensure_thread_for_ro(data["repair_order"])
	elif data.get("schedule_entry"):
		linked_ro = frappe.db.get_value("Schedule Entry", data["schedule_entry"], "repair_order")
		if linked_ro and frappe.db.exists("Repair Order", linked_ro):
			target_thread_key = ensure_thread_for_ro(linked_ro)
		else:
			ensure_singleton_shop_threads()
			target_thread_key = "SHOP:INTAKE"
	else:
		ensure_singleton_shop_threads()
		et_lower = (event_type or "").lower()
		if any(tok in et_lower for tok in ("intake", "booking", "appointment")):
			target_thread_key = "SHOP:INTAKE"
		elif any(tok in et_lower for tok in ("receiving", "purchase", "vendor_bill")):
			target_thread_key = "SHOP:AP_RECEIVING"
		else:
			target_thread_key = "SHOP:DISPATCH"

	summary = data.get("summary") or data.get("title") or event_type
	details = data.get("details")
	body_text = str(summary)
	if details:
		body_text = f"{body_text}\n{details}"

	metadata = {"event_type": event_type, **data}
	thread_meta = frappe.db.get_value(
		"Shop Board Thread",
		target_thread_key,
		["thread_type", "repair_order"],
		as_dict=True,
	) or {}

	try:
		ev = frappe.get_doc(
			{
				"doctype": "Shop Board Event",
				"thread": target_thread_key,
				"thread_type": thread_meta.get("thread_type"),
				"repair_order": thread_meta.get("repair_order"),
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


def on_shop_action_item_after_insert(doc, method=None):
	"""Seam 5 soft listener: project Shop Action Item alerts onto Shop Board threads."""
	if not _board_tables_ready():
		return None

	dedup_key = (doc.get("dedup_key") or "").strip()
	source = (doc.get("source") or "").strip()
	if dedup_key.startswith(SELF_ECHO_DEDUP_PREFIX) or source == "Rule Snooze":
		return None

	ref_dt = (doc.get("reference_doctype") or "").strip()
	ref_name = (doc.get("reference_name") or "").strip()

	payload: dict[str, Any] = {
		"action_item": doc.name,
		"category": doc.get("category"),
		"priority": doc.get("priority"),
		"source": source or None,
		"reference_doctype": ref_dt or None,
		"reference_name": ref_name or None,
		"dedup_key": dedup_key or None,
		"title": f"[{doc.get('priority') or 'Info'}] {doc.get('title') or doc.name}",
		"details": doc.get("details") or None,
	}

	if ref_dt == "Repair Order" and ref_name and frappe.db.exists("Repair Order", ref_name):
		payload["repair_order"] = ref_name
	elif ref_dt == "Schedule Entry" and ref_name:
		payload["schedule_entry"] = ref_name

	return project_external_domain_event(
		"shop_action_item",
		payload,
		idempotency_key=f"action_item:{doc.name}",
	)


__all__ = [
	"ensure_singleton_shop_threads",
	"ensure_thread_for_ro",
	"on_repair_order_after_insert",
	"on_repair_order_update",
	"on_schedule_entry_update",
	"on_shop_action_item_after_insert",
	"project_external_domain_event",
	"recompute_thread_projection",
	"resolve_thread_key",
]
