# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Confidence & permission gateways, action executors, and idempotent card resolution for Shop OS."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from ism.api.agent_serializers import build_ro_urls
from ism.board.contracts import (
	ALL_ACTIONS,
	AUTONOMOUS_ACTIONS,
	CLOSED_RO_STATUSES,
	MANAGER_GATED_ACTIONS,
	PENDING_DECISION_PAYLOAD_TYPES,
	SELF_ECHO_DEDUP_PREFIX,
	BoardContractError,
	parse_ro_from_thread_key,
	validate_approval_gate_card,
	validate_clarification_card,
	validate_receipt_card,
)
from ism.board.projections import (
	ensure_thread_for_ro,
	recompute_thread_projection,
)

MANAGER_ROLES: frozenset[str] = frozenset({"System Manager", "ISM Manager", "ISM Service Advisor"})
ADMIN_MEDIA_ROLES: frozenset[str] = frozenset({"System Manager", "ISM Manager"})
TECH_ROLES: frozenset[str] = frozenset({"ISM Technician"})

DEFAULT_CONFIDENCE_THRESHOLD: float = 0.85
ACOUSTIC_CLARITY_FLOOR: float = 0.75
ACOUSTIC_CLARITY_CONFIDENCE_CAP: float = 0.70

DEFAULT_PAYMENT_METHODS: tuple[str, ...] = (
	"Debit / Interac",
	"Credit Card",
	"E-Transfer",
	"Cash",
)

_OPT_OUT_TOKENS: tuple[str, ...] = ("opt-out", "opt out", "opted out", "stop", "unsubscribed")


# ---------------------------------------------------------------------------
# RBAC Helpers
# ---------------------------------------------------------------------------


def resolve_actor_role(user: str | None = None) -> str:
	"""Return 'MANAGER' for Manager/Advisor/System Manager, 'TECH' for Technician/RO reader, else throw."""
	target_user = (user or getattr(frappe.session, "user", None) or "").strip()
	if not target_user or target_user == "Guest":
		frappe.throw(
			_("Authentication is required to access the Shop Board."),
			frappe.PermissionError,
		)

	if target_user == "Administrator":
		return "MANAGER"

	roles = set(frappe.get_roles(target_user) or [])
	if roles & MANAGER_ROLES:
		return "MANAGER"

	if (roles & TECH_ROLES) or frappe.has_permission("Repair Order", "read", user=target_user):
		return "TECH"

	frappe.throw(
		_("You do not have permission to access the Shop Board."),
		frappe.PermissionError,
	)


def assert_can_commit_approval_gate(user: str | None = None) -> str:
	"""Assert that user is a Manager/Advisor/System Manager; raise frappe.PermissionError otherwise."""
	role = resolve_actor_role(user)
	if role != "MANAGER":
		frappe.throw(
			_("Only ISM Manager, ISM Service Advisor, or System Manager can approve manager-gated actions."),
			frappe.PermissionError,
		)
	return "MANAGER"


def assert_can_manage_media_storage(user: str | None = None) -> str:
	"""Assert that user is an ISM Manager or System Manager for media storage diagnostics."""
	target_user = (user or getattr(frappe.session, "user", None) or "").strip()
	if not target_user or target_user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)
	if target_user == "Administrator":
		return "MANAGER"
	roles = set(frappe.get_roles(target_user) or [])
	if roles & ADMIN_MEDIA_ROLES:
		return "MANAGER"
	frappe.throw(
		_("Only ISM Manager or System Manager can test or manage media storage."),
		frappe.PermissionError,
	)


# ---------------------------------------------------------------------------
# Internal Helpers & Lazy Wave 2B Bridges
# ---------------------------------------------------------------------------


def _parse_json(raw: Any, default: Any) -> Any:
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


def _get_confidence_threshold() -> float:
	try:
		settings = frappe.get_single("ISM Settings")
		val = getattr(settings, "board_ai_confidence_threshold", None)
		if val is not None and flt(val) > 0:
			return flt(val)
	except Exception:
		pass
	return DEFAULT_CONFIDENCE_THRESHOLD


def _lazy_comms_firewall():
	try:
		from ism.board import comms_firewall

		return comms_firewall
	except ImportError:
		return None


def _lazy_enqueue_board_event(
	event_name: str, clarification_patch: dict[str, Any] | None = None
) -> dict[str, Any]:
	try:
		from ism.board.orchestrator import enqueue_board_event
	except ImportError:
		return {
			"enqueued": False,
			"orchestrator_installed": False,
			"event_name": event_name,
			"clarification_patch": clarification_patch,
		}
	res = enqueue_board_event(event_name, clarification_patch=clarification_patch)
	if isinstance(res, dict):
		return {"enqueued": True, "orchestrator_installed": True, **res}
	return {"enqueued": True, "orchestrator_installed": True, "result": res}


def _resolve_customer_arrival_time_safe(internal_start: str | None) -> str | None:
	if not internal_start:
		return None
	fw = _lazy_comms_firewall()
	if fw is not None and hasattr(fw, "resolve_customer_arrival_time"):
		try:
			return fw.resolve_customer_arrival_time(internal_start)
		except Exception:
			pass
	try:
		from ism.scheduling.arrival_policy import to_customer_arrival_time

		return to_customer_arrival_time(internal_start)
	except ImportError:
		return str(internal_start)


def _get_ro_customer_contact(ro_doc: Any) -> dict[str, str | None]:
	customer_name = getattr(ro_doc, "customer", None) if ro_doc else None
	phone = None
	email = None
	display_name = customer_name
	if customer_name and frappe.db.exists("Customer", customer_name):
		cust = frappe.db.get_value(
			"Customer",
			customer_name,
			["customer_name", "mobile_no", "email_id"],
			as_dict=True,
		) or {}
		display_name = cust.get("customer_name") or customer_name
		phone = cust.get("mobile_no") or None
		email = cust.get("email_id") or None
		if not phone or not email:
			contact_links = frappe.get_all(
				"Dynamic Link",
				filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Contact"},
				pluck="parent",
				limit=3,
			)
			for cname in contact_links:
				cinfo = frappe.db.get_value(
					"Contact",
					cname,
					["mobile_no", "phone", "email_id"],
					as_dict=True,
				) or {}
				if not phone:
					phone = cinfo.get("mobile_no") or cinfo.get("phone") or None
				if not email:
					email = cinfo.get("email_id") or None
				if phone and email:
					break
	return {"customer_name": display_name, "phone": phone, "email": email}


def _sanitize_and_draft_comms(
	ro_doc: Any,
	*,
	finding_summary: str = "",
	approved_items: list[dict[str, Any]] | None = None,
	clean_photo_refs: list[str] | None = None,
	approval_url: str | None = None,
	eta_window: str | None = None,
	internal_start_time: str | None = None,
	channel: str = "sms",
	subject_hint: str | None = None,
) -> dict[str, Any]:
	"""Draft customer-facing preview strictly via Context Firewall (or safe allowlisted fallback)."""
	fw = _lazy_comms_firewall()
	if fw is not None and hasattr(fw, "sanitize_for_customer_comms") and hasattr(fw, "draft_customer_message"):
		try:
			kwargs: dict[str, Any] = {
				"approved_items": approved_items,
				"clean_photo_refs": clean_photo_refs,
				"approval_url": approval_url,
				"eta_window": eta_window,
			}
			if internal_start_time:
				kwargs["internal_start_time"] = internal_start_time
			try:
				sanitized = fw.sanitize_for_customer_comms(ro_doc, finding_summary, **kwargs)
			except TypeError:
				kwargs.pop("internal_start_time", None)
				if internal_start_time and not kwargs.get("eta_window"):
					kwargs["eta_window"] = _resolve_customer_arrival_time_safe(internal_start_time)
				sanitized = fw.sanitize_for_customer_comms(ro_doc, finding_summary, **kwargs)
			drafted = fw.draft_customer_message(sanitized, channel=channel)
			if isinstance(drafted, dict) and drafted.get("body"):
				return drafted
		except Exception:
			pass

	# Safe allowlisted fallback when Wave 2B comms_firewall is not yet merged
	contact = _get_ro_customer_contact(ro_doc)
	first_name = (contact.get("customer_name") or "Customer").split()[0]
	ro_ref = getattr(ro_doc, "name", None) or "your service visit"
	vehicle_label = ""
	if ro_doc and getattr(ro_doc, "vehicle", None):
		veh = frappe.db.get_value(
			"Repair Vehicle",
			ro_doc.vehicle,
			["model_year", "manufacturer", "model"],
			as_dict=True,
		) or {}
		parts = [str(veh.get(k) or "").strip() for k in ("model_year", "manufacturer", "model")]
		vehicle_label = " ".join(p for p in parts if p)

	resolved_eta = eta_window
	if not resolved_eta and internal_start_time:
		resolved_eta = _resolve_customer_arrival_time_safe(internal_start_time)

	safe_lines: list[str] = [f"Hi {first_name},"]
	if vehicle_label:
		safe_lines.append(f"Update on your {vehicle_label} ({ro_ref}):")
	else:
		safe_lines.append(f"Update on {ro_ref}:")
	if finding_summary:
		safe_lines.append(str(finding_summary).strip())
	if resolved_eta:
		safe_lines.append(f"Arrival / timing: {resolved_eta}.")
	if approval_url:
		safe_lines.append(f"Review & approve here: {approval_url}")
	body = " ".join(line.strip() for line in safe_lines if line and line.strip())

	allowlisted_facts = {
		"customer_first_name": first_name,
		"ro_ref": ro_ref,
		"vehicle": vehicle_label,
		"finding_summary": str(finding_summary or "").strip(),
		"eta_window": resolved_eta,
		"approval_url": approval_url,
		"clean_photo_refs": list(clean_photo_refs or []),
	}
	facts_hash = hashlib.sha256(
		json.dumps(allowlisted_facts, sort_keys=True, default=str).encode("utf-8")
	).hexdigest()
	subject = subject_hint or f"Service Update for {ro_ref}"
	return {
		"channel": channel,
		"subject": subject,
		"body": body,
		"sanitized_facts_hash": facts_hash,
		"clean_photo_refs": list(clean_photo_refs or []),
	}


def _insert_board_event(
	*,
	thread_key: str,
	thread_type: str | None,
	repair_order: str | None,
	actor_type: str,
	payload_type: str,
	body_text: str,
	actor_user: str | None = None,
	intent_tag: str | None = None,
	target_role: str = "ALL",
	confidence_score: float | None = None,
	card_schema: dict[str, Any] | None = None,
	metadata: dict[str, Any] | None = None,
	resolution_state: str = "NONE",
	resolution_payload: dict[str, Any] | None = None,
	parent_event: str | None = None,
	idempotency_key: str | None = None,
	ai_status: str = "NONE",
) -> Any:
	clean_idem = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
	doc = frappe.get_doc(
		{
			"doctype": "Shop Board Event",
			"thread": thread_key,
			"thread_type": thread_type,
			"repair_order": repair_order,
			"event_timestamp": now_datetime(),
			"actor_type": actor_type,
			"actor_user": actor_user,
			"intent_tag": intent_tag,
			"payload_type": payload_type,
			"target_role": target_role,
			"body_text": body_text,
			"confidence_score": confidence_score,
			"card_schema": json.dumps(card_schema, default=str) if card_schema is not None else None,
			"metadata_json": json.dumps(metadata, default=str) if metadata is not None else None,
			"resolution_state": resolution_state,
			"resolution_payload": (
				json.dumps(resolution_payload, default=str) if resolution_payload is not None else None
			),
			"parent_event": parent_event,
			"idempotency_key": clean_idem,
			"ai_status": ai_status,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _publish_board_event(event_doc: Any) -> None:
	payload = {
		"name": event_doc.name,
		"thread": event_doc.thread,
		"thread_type": event_doc.thread_type,
		"repair_order": event_doc.repair_order,
		"event_timestamp": str(event_doc.event_timestamp) if event_doc.event_timestamp else None,
		"actor_type": event_doc.actor_type,
		"actor_user": event_doc.actor_user,
		"intent_tag": event_doc.intent_tag,
		"payload_type": event_doc.payload_type,
		"target_role": event_doc.target_role,
		"body_text": event_doc.body_text,
		"confidence_score": event_doc.confidence_score,
		"card_schema": _parse_json(event_doc.card_schema, None),
		"resolution_state": event_doc.resolution_state,
		"parent_event": event_doc.parent_event,
		"ai_status": event_doc.ai_status,
	}
	frappe.publish_realtime(
		"ism_board_event",
		payload,
		doctype="Shop Board Thread",
		docname=event_doc.thread,
		after_commit=True,
	)


def _resolve_ro_name(action_payload: dict[str, Any], source_doc: Any = None, fallback_ro: str | None = None) -> str:
	ro = (
		action_payload.get("repair_order")
		or fallback_ro
		or (getattr(source_doc, "repair_order", None) if source_doc else None)
		or (parse_ro_from_thread_key(getattr(source_doc, "thread", None)) if source_doc else None)
	)
	if not isinstance(ro, str) or not ro.strip():
		raise BoardContractError("Action requires a valid repair_order.")
	return ro.strip()


def _price_parts_internally(parts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
	"""Compute internal CAD selling rates via ism.board.pricing when parts carry OEM price or cost."""
	if not parts:
		return []
	from ism.board.pricing import calculate_part_price

	priced: list[dict[str, Any]] = []
	for raw in parts:
		if not isinstance(raw, dict):
			continue
		entry = dict(raw)
		has_explicit_rate = entry.get("rate") is not None and str(entry.get("rate")).strip() != ""
		tesla_oem_price = entry.get("tesla_oem_price")
		cost = entry.get("cost")
		if not has_explicit_rate and (tesla_oem_price is not None or cost is not None):
			calc = calculate_part_price(
				tesla_oem_price=flt(tesla_oem_price) if tesla_oem_price is not None else None,
				cost=flt(cost) if cost is not None else None,
				part_source=entry.get("part_source") or "Aftermarket",
				part_condition=entry.get("part_condition") or "New",
				override_rate=flt(entry["override_rate"]) if entry.get("override_rate") is not None else None,
				part_number=entry.get("oem_part_number") or entry.get("part_number") or entry.get("item_code"),
				part_name=entry.get("item_name") or entry.get("part_name"),
			)
			entry["rate"] = calc["sell_price"]
			entry["_pricing_rule"] = calc["rule_applied"]
		priced.append(entry)
	return priced


# ---------------------------------------------------------------------------
# Autonomous Action Executors (9 actions + Undo)
# ---------------------------------------------------------------------------


def _exec_log_ro_note(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	ro_name = _resolve_ro_name(payload, source_doc)
	ro = frappe.get_doc("Repair Order", ro_name)
	note_text = str(
		payload.get("note")
		or payload.get("notes")
		or payload.get("body")
		or (getattr(source_doc, "body_text", None) if source_doc else "")
		or ""
	).strip()
	if not note_text:
		raise BoardContractError("LOG_RO_NOTE requires non-empty note text.")

	prev_note = ro.override_notes or ""
	stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
	appended_line = f"[Board {stamp}] {note_text}"
	new_note = f"{prev_note}\n{appended_line}".strip() if prev_note else appended_line
	ro.db_set("override_notes", new_note, update_modified=True)
	comment = ro.add_comment("Comment", appended_line)

	return {
		"summary": f"Logged internal note on {ro_name}.",
		"result": {
			"repair_order": ro_name,
			"note": appended_line,
			"comment": getattr(comment, "name", None),
		},
		"undo_spec": {
			"action_type": "LOG_RO_NOTE",
			"repair_order": ro_name,
			"previous_note": prev_note,
			"comment_name": getattr(comment, "name", None),
		},
	}


def _exec_attach_inspection_media(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	from ism.api import inspection as inspection_api

	ro_name = _resolve_ro_name(payload, source_doc)
	ro_before = frappe.get_doc("Repair Order", ro_name)
	prev_odo = ro_before.odometer_in
	prev_concern = ro_before.customer_concern
	prev_rows_by_name = {
		row.name: {
			"status": row.status,
			"notes": row.notes,
			"photos": row.photos,
		}
		for row in (ro_before.get("inspection_items") or [])
		if row.name
	}

	items_input = payload.get("items")
	if items_input is None:
		# Build a default inspection finding item from payload or source media/text
		media_refs = _parse_json(getattr(source_doc, "media_attachments", None), []) if source_doc else []
		ref_note = ""
		if media_refs:
			ref_ids = [m.get("file_ref") for m in media_refs if isinstance(m, dict) and m.get("file_ref")]
			if ref_ids:
				ref_note = f" [Board media refs: {', '.join(ref_ids)}]"
		note_text = (str(payload.get("notes") or getattr(source_doc, "body_text", "") or "").strip() + ref_note).strip()
		items_input = [
			{
				"inspection_description": (
					payload.get("inspection_description")
					or payload.get("item_name")
					or "Shop Board Finding"
				),
				"category": payload.get("category") or "Underbody",
				"status": payload.get("status") or "Attention",
				"notes": note_text,
				"photos": payload.get("photos") or [],
			}
		]

	res = inspection_api.record_check_in_inspection(
		repair_order=ro_name,
		items=items_input,
		odometer_in=payload.get("odometer_in"),
		customer_concern=payload.get("customer_concern"),
		template_name=payload.get("template_name"),
		auto_apply_template=bool(payload.get("auto_apply_template", False)),
	)

	ro_after = frappe.get_doc("Repair Order", ro_name)
	added_row_names = [
		row.name
		for row in (ro_after.get("inspection_items") or [])
		if row.name and row.name not in prev_rows_by_name
	]

	return {
		"summary": f"Recorded check-in inspection finding on {ro_name}.",
		"result": res,
		"undo_spec": {
			"action_type": "ATTACH_INSPECTION_MEDIA",
			"repair_order": ro_name,
			"previous_odometer_in": prev_odo,
			"previous_customer_concern": prev_concern,
			"added_row_names": added_row_names,
			"previous_rows": prev_rows_by_name,
		},
	}


def _exec_add_supplement_draft(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	from ism.api import agent_quotes

	ro_name = _resolve_ro_name(payload, source_doc)
	ro_before = frappe.get_doc("Repair Order", ro_name)
	before_row_names = {s.name for s in (ro_before.services or []) if s.name}

	service_title = str(payload.get("service_title") or payload.get("title") or "").strip()
	if not service_title:
		raise BoardContractError("ADD_SUPPLEMENT_DRAFT requires 'service_title'.")

	priced_parts = _price_parts_internally(payload.get("parts"))
	res = agent_quotes.add_supplement(
		repair_order=ro_name,
		service_title=service_title,
		category=payload.get("category") or "Maintenance",
		labor_hours=flt(payload.get("labor_hours", 1.0)),
		labor_rate=flt(payload["labor_rate"]) if payload.get("labor_rate") is not None else None,
		parts=priced_parts or None,
		notes=payload.get("notes"),
	)

	ro_mid = frappe.get_doc("Repair Order", ro_name)
	new_rows = [s for s in (ro_mid.services or []) if s.name and s.name not in before_row_names]
	added_names = [s.name for s in new_rows]

	# Symmetrical both-sides duplication when explicitly requested
	side_val = str(payload.get("side") or "").strip().lower()
	is_both = bool(payload.get("symmetrical") or payload.get("duplicate_opposite_side")) or side_val in (
		"both",
		"lh+rh",
		"both sides",
	)
	dup_res = None
	if is_both and added_names:
		try:
			dup_res = agent_quotes.duplicate_service_for_opposite_side(
				repair_order=ro_name,
				service_row_id=added_names[0],
			)
			ro_after = frappe.get_doc("Repair Order", ro_name)
			added_names = [s.name for s in (ro_after.services or []) if s.name and s.name not in before_row_names]
		except Exception:
			# If title lacks LH/RH token, still keep primary supplement cleanly
			dup_res = None

	return {
		"summary": f"Added draft supplement '{service_title}' to {ro_name}.",
		"result": {
			"supplement": res,
			"duplicate_opposite_side": dup_res,
			"added_service_rows": added_names,
		},
		"undo_spec": {
			"action_type": "ADD_SUPPLEMENT_DRAFT",
			"repair_order": ro_name,
			"service_row_names": added_names,
		},
	}


def _exec_set_parts_hold(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	ro_name = _resolve_ro_name(payload, source_doc)
	ro = frappe.get_doc("Repair Order", ro_name)
	prev_hold = cint(ro.manual_parts_hold)
	prev_status = ro.status

	ro.manual_parts_hold = 1
	if ro.status not in CLOSED_RO_STATUSES and ro.status in ("Authorized - In Progress", "Checked In", "Work Completed"):
		ro.status = "Waiting on Parts"
	ro.save(ignore_permissions=True)

	# Seam 5: tag dedup_key with "board:parts_hold:<ro_name>" to suppress self-echo
	dedup_key = f"{SELF_ECHO_DEDUP_PREFIX}parts_hold:{ro_name}"
	reason = str(payload.get("reason") or payload.get("notes") or f"Parts hold placed on {ro_name} via Shop Board").strip()
	existing = frappe.get_all(
		"Shop Action Item",
		filters={"dedup_key": dedup_key},
		fields=["name", "status"],
		order_by="creation desc",
		limit=1,
	)
	created_ai = False
	prev_ai_status = None
	if existing:
		sai = frappe.get_doc("Shop Action Item", existing[0]["name"])
		prev_ai_status = sai.status
		sai.status = "Open"
		sai.details = reason
		sai.resolved_by = None
		sai.resolved_at = None
		sai.save(ignore_permissions=True)
	else:
		sai = frappe.get_doc(
			{
				"doctype": "Shop Action Item",
				"title": payload.get("title") or f"Parts Hold: {ro_name}",
				"category": "Inventory / Parts",
				"priority": payload.get("priority") or "High",
				"status": "Open",
				"source": "Agent",
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
				"item_code": payload.get("item_code"),
				"dedup_key": dedup_key,
				"reported_by": frappe.session.user,
				"details": reason,
			}
		)
		sai.insert(ignore_permissions=True)
		created_ai = True

	return {
		"summary": f"Placed parts hold on {ro_name}.",
		"result": {
			"repair_order": ro_name,
			"manual_parts_hold": 1,
			"status": ro.status,
			"action_item": sai.name,
			"dedup_key": dedup_key,
		},
		"undo_spec": {
			"action_type": "SET_PARTS_HOLD",
			"repair_order": ro_name,
			"previous_manual_parts_hold": prev_hold,
			"previous_status": prev_status,
			"action_item": sai.name,
			"created_action_item": created_ai,
			"previous_action_item_status": prev_ai_status,
		},
	}


def _exec_clear_parts_hold(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	ro_name = _resolve_ro_name(payload, source_doc)
	ro = frappe.get_doc("Repair Order", ro_name)
	prev_hold = cint(ro.manual_parts_hold)
	prev_status = ro.status

	ro.manual_parts_hold = 0
	if ro.status == "Waiting on Parts":
		target_status = payload.get("restore_status")
		if target_status:
			ro.status = target_status
		elif ro.approval_status in ("Approved", "Partially Approved"):
			ro.status = "Authorized - In Progress"
	ro.save(ignore_permissions=True)

	dedup_key = f"{SELF_ECHO_DEDUP_PREFIX}parts_hold:{ro_name}"
	open_items = frappe.get_all(
		"Shop Action Item",
		filters={"dedup_key": dedup_key, "status": ["in", ["Open", "In Progress"]]},
		pluck="name",
	)
	resolved_items: list[str] = []
	for sai_name in open_items:
		sai = frappe.get_doc("Shop Action Item", sai_name)
		sai.status = "Resolved"
		sai.resolution_notes = payload.get("notes") or "Parts hold cleared via Shop Board"
		sai.resolved_by = frappe.session.user
		sai.resolved_at = now_datetime()
		sai.save(ignore_permissions=True)
		resolved_items.append(sai_name)

	return {
		"summary": f"Cleared parts hold on {ro_name}.",
		"result": {
			"repair_order": ro_name,
			"manual_parts_hold": 0,
			"status": ro.status,
			"resolved_action_items": resolved_items,
		},
		"undo_spec": {
			"action_type": "CLEAR_PARTS_HOLD",
			"repair_order": ro_name,
			"previous_manual_parts_hold": prev_hold,
			"previous_status": prev_status,
			"resolved_action_items": resolved_items,
		},
	}


def _exec_draft_purchase_order(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	from ism.api import agent_procurement

	ro_name = payload.get("repair_order") or (getattr(source_doc, "repair_order", None) if source_doc else None)
	parts_data = payload.get("parts_data") or payload.get("parts")
	if not parts_data:
		raise BoardContractError("DRAFT_PURCHASE_ORDER requires non-empty 'parts_data'.")

	# Autonomous PO drafts ALWAYS run with submit=False; Manager SUBMIT_PO gate commits them.
	res = agent_procurement.submit_procurement_orders(
		repair_order=ro_name,
		parts_data=parts_data,
		submit=False,
		company=payload.get("company"),
	)
	data = res.get("data") or {}
	created_pos = list(data.get("purchase_orders") or [])
	created_rfqs = list(data.get("rfqs") or [])

	extra_gates: list[dict[str, Any]] = []
	if created_pos:
		po_list_label = ", ".join(created_pos)
		extra_gates.append(
			{
				"action_type": "SUBMIT_PO",
				"title": f"Submit Draft PO ({po_list_label})",
				"summary": f"Manager approval required to submit draft Purchase Order(s): {po_list_label}.",
				"target_role": "MANAGER",
				"params": {
					"purchase_orders": created_pos,
					"repair_order": ro_name,
				},
			}
		)

	return {
		"summary": f"Created draft Purchase Order(s): {', '.join(created_pos) or 'RFQ only'}.",
		"result": res,
		"undo_spec": {
			"action_type": "DRAFT_PURCHASE_ORDER",
			"purchase_orders": created_pos,
			"rfqs": created_rfqs,
		},
		"extra_gates": extra_gates,
	}


def _exec_update_job_status(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	from ism.api import agent_quotes

	ro_name = _resolve_ro_name(payload, source_doc)
	ro_before = frappe.get_doc("Repair Order", ro_name)
	action_verb = str(payload.get("action") or payload.get("status_action") or "").strip().lower()
	if not action_verb:
		raise BoardContractError("UPDATE_JOB_STATUS requires 'action' (start/hold/parts/park/complete).")

	res = agent_quotes.update_job_status(
		repair_order=ro_name,
		action=action_verb,
		odometer_out=payload.get("odometer_out"),
		notes=payload.get("notes"),
	)
	new_status = (res.get("data") or {}).get("status")
	return {
		"summary": f"Updated {ro_name} status to {new_status or action_verb}.",
		"result": res,
		"undo_spec": {
			"action_type": "UPDATE_JOB_STATUS",
			"repair_order": ro_name,
			"previous_status": ro_before.status,
			"previous_manual_parts_hold": cint(ro_before.manual_parts_hold),
			"previous_vehicle_location": ro_before.vehicle_location,
			"previous_odometer_out": ro_before.odometer_out,
		},
	}


def _exec_toggle_service_completion(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	from ism.api import agent_quotes

	ro_name = _resolve_ro_name(payload, source_doc)
	ro_before = frappe.get_doc("Repair Order", ro_name)
	target_id = payload.get("service_row_id")
	target_ids = payload.get("service_row_ids")
	is_completed = payload.get("is_completed", True)

	raw_targets: list[str] = []
	if isinstance(target_id, list):
		raw_targets.extend(str(x) for x in target_id)
	elif target_id is not None:
		raw_targets.append(str(target_id))
	if isinstance(target_ids, list):
		raw_targets.extend(str(x) for x in target_ids)
	elif isinstance(target_ids, str) and target_ids.strip():
		parsed = _parse_json(target_ids, [target_ids])
		if isinstance(parsed, list):
			raw_targets.extend(str(x) for x in parsed)

	prev_states: list[dict[str, Any]] = []
	for tid in raw_targets:
		for s in ro_before.services or []:
			if str(s.name) == tid or str(s.idx) == tid or str(s.service_title) == tid:
				prev_states.append({"service_row_id": s.name, "previous_is_completed": cint(s.is_completed)})
				break

	res = agent_quotes.toggle_service_completion(
		repair_order=ro_name,
		service_row_id=target_id,
		is_completed=is_completed,
		service_row_ids=target_ids,
	)
	return {
		"summary": f"Updated service completion on {ro_name}.",
		"result": res,
		"undo_spec": {
			"action_type": "TOGGLE_SERVICE_COMPLETION",
			"repair_order": ro_name,
			"previous_rows": prev_states,
		},
	}


def _exec_add_deferred_services(payload: dict[str, Any], source_doc: Any) -> dict[str, Any]:
	from ism.api import agent_quotes

	ro_name = _resolve_ro_name(payload, source_doc)
	ro_before = frappe.get_doc("Repair Order", ro_name)
	before_row_names = {s.name for s in (ro_before.services or []) if s.name}

	deferred_items = payload.get("deferred_items") or payload.get("services")
	if not deferred_items:
		def_res = agent_quotes.get_deferred_services(repair_order=ro_name)
		pending = (def_res.get("data") or {}).get("deferred_services") or []
		deferred_items = [
			item.get("name") or item.get("job_title")
			for item in pending
			if isinstance(item, dict) and (item.get("name") or item.get("job_title"))
		]

	res = agent_quotes.add_deferred_services(
		repair_order=ro_name,
		deferred_items=deferred_items,
	)
	ro_after = frappe.get_doc("Repair Order", ro_name)
	added_row_names = [s.name for s in (ro_after.services or []) if s.name and s.name not in before_row_names]

	return {
		"summary": f"Added {len(added_row_names)} deferred service(s) to {ro_name}.",
		"result": res,
		"undo_spec": {
			"action_type": "ADD_DEFERRED_SERVICES",
			"repair_order": ro_name,
			"service_row_names": added_row_names,
		},
	}


def _execute_autonomous_action(
	action: str, payload: dict[str, Any], *, source_doc: Any = None
) -> dict[str, Any]:
	dispatch_map = {
		"LOG_RO_NOTE": _exec_log_ro_note,
		"ATTACH_INSPECTION_MEDIA": _exec_attach_inspection_media,
		"ADD_SUPPLEMENT_DRAFT": _exec_add_supplement_draft,
		"SET_PARTS_HOLD": _exec_set_parts_hold,
		"CLEAR_PARTS_HOLD": _exec_clear_parts_hold,
		"DRAFT_PURCHASE_ORDER": _exec_draft_purchase_order,
		"UPDATE_JOB_STATUS": _exec_update_job_status,
		"TOGGLE_SERVICE_COMPLETION": _exec_toggle_service_completion,
		"ADD_DEFERRED_SERVICES": _exec_add_deferred_services,
	}
	handler = dispatch_map.get(action)
	if handler is None:
		raise BoardContractError(f"Unsupported autonomous action: {action!r}")
	return handler(payload, source_doc)


# ---------------------------------------------------------------------------
# Compensating Undo Executors
# ---------------------------------------------------------------------------


def _remove_unapproved_service_rows(ro_name: str, row_names: list[str]) -> list[str]:
	if not row_names:
		return []
	ro = frappe.get_doc("Repair Order", ro_name)
	target_set = set(row_names)
	removed: list[str] = []
	kept = []
	for row in ro.services or []:
		if row.name in target_set:
			if row.status == "Approved":
				frappe.throw(
					_("Cannot undo service row '{0}' because it has already been Approved.").format(
						row.service_title or row.name
					),
					frappe.ValidationError,
				)
			removed.append(row.name)
		else:
			kept.append(row)
	if removed:
		ro.set("services", kept)
		ro.save(ignore_permissions=True)
	return removed


def _execute_undo_action(action_type: str, undo_spec: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
	if action_type == "LOG_RO_NOTE":
		ro_name = undo_spec["repair_order"]
		ro = frappe.get_doc("Repair Order", ro_name)
		ro.db_set("override_notes", undo_spec.get("previous_note", ""), update_modified=True)
		comment_name = undo_spec.get("comment_name")
		if comment_name and frappe.db.exists("Comment", comment_name):
			frappe.delete_doc("Comment", comment_name, force=True, ignore_permissions=True)
		return {"repair_order": ro_name, "restored_note": undo_spec.get("previous_note", "")}

	if action_type == "ATTACH_INSPECTION_MEDIA":
		ro_name = undo_spec["repair_order"]
		ro = frappe.get_doc("Repair Order", ro_name)
		added_set = set(undo_spec.get("added_row_names") or [])
		prev_rows = undo_spec.get("previous_rows") or {}
		kept = []
		for row in ro.get("inspection_items") or []:
			if row.name in added_set:
				continue
			if row.name in prev_rows:
				snap = prev_rows[row.name]
				row.status = snap.get("status")
				row.notes = snap.get("notes")
				row.photos = snap.get("photos")
			kept.append(row)
		ro.set("inspection_items", kept)
		if "previous_odometer_in" in undo_spec:
			ro.odometer_in = undo_spec["previous_odometer_in"]
		if "previous_customer_concern" in undo_spec:
			ro.customer_concern = undo_spec["previous_customer_concern"]
		ro.save(ignore_permissions=True)
		return {"repair_order": ro_name, "removed_rows": list(added_set)}

	if action_type in ("ADD_SUPPLEMENT_DRAFT", "ADD_DEFERRED_SERVICES"):
		ro_name = undo_spec["repair_order"]
		removed = _remove_unapproved_service_rows(ro_name, undo_spec.get("service_row_names") or [])
		return {"repair_order": ro_name, "removed_service_rows": removed}

	if action_type == "SET_PARTS_HOLD":
		ro_name = undo_spec["repair_order"]
		ro = frappe.get_doc("Repair Order", ro_name)
		ro.manual_parts_hold = cint(undo_spec.get("previous_manual_parts_hold", 0))
		prev_status = undo_spec.get("previous_status")
		if prev_status and ro.status not in CLOSED_RO_STATUSES:
			ro.status = prev_status
		ro.save(ignore_permissions=True)
		sai_name = undo_spec.get("action_item")
		if sai_name and frappe.db.exists("Shop Action Item", sai_name):
			sai = frappe.get_doc("Shop Action Item", sai_name)
			if undo_spec.get("created_action_item"):
				sai.status = "Resolved"
				sai.resolution_notes = reason or "Parts hold undone via Shop Board"
				sai.resolved_by = frappe.session.user
				sai.resolved_at = now_datetime()
			elif undo_spec.get("previous_action_item_status"):
				sai.status = undo_spec["previous_action_item_status"]
			sai.save(ignore_permissions=True)
		return {"repair_order": ro_name, "manual_parts_hold": ro.manual_parts_hold, "status": ro.status}

	if action_type == "CLEAR_PARTS_HOLD":
		ro_name = undo_spec["repair_order"]
		ro = frappe.get_doc("Repair Order", ro_name)
		ro.manual_parts_hold = cint(undo_spec.get("previous_manual_parts_hold", 1))
		prev_status = undo_spec.get("previous_status")
		if prev_status and ro.status not in CLOSED_RO_STATUSES:
			ro.status = prev_status
		ro.save(ignore_permissions=True)
		for sai_name in undo_spec.get("resolved_action_items") or []:
			if frappe.db.exists("Shop Action Item", sai_name):
				sai = frappe.get_doc("Shop Action Item", sai_name)
				sai.status = "Open"
				sai.resolved_by = None
				sai.resolved_at = None
				sai.save(ignore_permissions=True)
		return {"repair_order": ro_name, "manual_parts_hold": ro.manual_parts_hold, "status": ro.status}

	if action_type == "DRAFT_PURCHASE_ORDER":
		deleted_pos: list[str] = []
		for po_name in undo_spec.get("purchase_orders") or []:
			if frappe.db.exists("Purchase Order", po_name):
				po_docstatus = cint(frappe.db.get_value("Purchase Order", po_name, "docstatus"))
				if po_docstatus != 0:
					frappe.throw(
						_("Cannot undo Purchase Order {0} because it has already been submitted.").format(po_name),
						frappe.ValidationError,
					)
				frappe.delete_doc("Purchase Order", po_name, force=True, ignore_permissions=True)
				deleted_pos.append(po_name)
		deleted_rfqs: list[str] = []
		for rfq_name in undo_spec.get("rfqs") or []:
			if frappe.db.exists("Request for Quotation", rfq_name):
				if cint(frappe.db.get_value("Request for Quotation", rfq_name, "docstatus")) == 0:
					frappe.delete_doc("Request for Quotation", rfq_name, force=True, ignore_permissions=True)
					deleted_rfqs.append(rfq_name)
		return {"deleted_purchase_orders": deleted_pos, "deleted_rfqs": deleted_rfqs}

	if action_type == "UPDATE_JOB_STATUS":
		ro_name = undo_spec["repair_order"]
		ro = frappe.get_doc("Repair Order", ro_name)
		if ro.status in CLOSED_RO_STATUSES:
			frappe.throw(
				_("Cannot undo status change on {0} because it is in terminal status '{1}'.").format(
					ro_name, ro.status
				),
				frappe.ValidationError,
			)
		ro.status = undo_spec.get("previous_status") or ro.status
		ro.manual_parts_hold = cint(undo_spec.get("previous_manual_parts_hold", 0))
		if "previous_vehicle_location" in undo_spec:
			ro.vehicle_location = undo_spec["previous_vehicle_location"]
		if "previous_odometer_out" in undo_spec:
			ro.odometer_out = undo_spec["previous_odometer_out"]
		ro.save(ignore_permissions=True)
		return {"repair_order": ro_name, "restored_status": ro.status}

	if action_type == "TOGGLE_SERVICE_COMPLETION":
		from ism.api import agent_quotes

		ro_name = undo_spec["repair_order"]
		reverted: list[str] = []
		for entry in undo_spec.get("previous_rows") or []:
			agent_quotes.toggle_service_completion(
				repair_order=ro_name,
				service_row_id=entry["service_row_id"],
				is_completed=bool(entry.get("previous_is_completed", 0)),
			)
			reverted.append(entry["service_row_id"])
		return {"repair_order": ro_name, "reverted_service_rows": reverted}

	raise BoardContractError(f"Undo is not supported for action_type: {action_type!r}")


# ---------------------------------------------------------------------------
# Approval Gate Card Builders (Manager-Gated Actions — NEVER auto-executed)
# ---------------------------------------------------------------------------


def _format_slot_options(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
	options: list[dict[str, Any]] = []
	for idx, slot in enumerate(slots[:5]):
		s_date = slot.get("scheduled_date") or slot.get("date") or ""
		s_time = slot.get("start_time") or slot.get("scheduled_time") or ""
		bay = slot.get("service_bay") or slot.get("bay") or ""
		tech = slot.get("technician") or slot.get("assigned_technician") or ""
		label_parts = [f"{s_date} {s_time}".strip()]
		if bay:
			label_parts.append(str(bay))
		if tech:
			label_parts.append(str(tech))
		options.append(
			{
				"label": " · ".join(label_parts),
				"value": str(idx),
				"slot_index": idx,
				"scheduled_date": s_date,
				"start_time": s_time,
				"service_bay": bay or None,
				"technician": tech or None,
			}
		)
	return options


def _build_approval_gate_card(
	action: str,
	payload: dict[str, Any],
	*,
	source_doc: Any = None,
	summary: str = "",
	customer_comms: dict[str, Any] | None = None,
) -> dict[str, Any]:
	params = dict(payload or {})
	ro_name = params.get("repair_order") or (getattr(source_doc, "repair_order", None) if source_doc else None)
	if ro_name and "repair_order" not in params:
		params["repair_order"] = ro_name
	ro_doc = frappe.get_doc("Repair Order", ro_name) if (ro_name and frappe.db.exists("Repair Order", ro_name)) else None

	card: dict[str, Any] = {
		"action_type": action,
		"title": summary or f"Approve {action.replace('_', ' ').title()}",
		"target_role": "MANAGER",
		"params": params,
	}

	if action == "SUBMIT_PO":
		pos = params.get("purchase_orders") or ([params["purchase_order"]] if params.get("purchase_order") else [])
		if pos:
			card["title"] = summary or f"Submit Purchase Order ({', '.join(str(p) for p in pos)})"

	elif action == "AMEND_QUOTE":
		if ro_doc:
			card["current_grand_total"] = flt(ro_doc.grand_total, 2)
		card["title"] = summary or f"Amend Quote on {ro_name or 'Repair Order'}"

	elif action in ("SEND_QUOTE_APPROVAL", "DISPATCH_CUSTOMER_SMS", "DISPATCH_CUSTOMER_EMAIL"):
		channel = "email" if action == "DISPATCH_CUSTOMER_EMAIL" else (params.get("communication_medium") or "sms").lower()
		finding = (
			params.get("finding_summary")
			or (customer_comms.get("finding_summary") if isinstance(customer_comms, dict) else "")
			or summary
		)
		drafted = _sanitize_and_draft_comms(
			ro_doc,
			finding_summary=finding,
			approved_items=params.get("approved_items"),
			clean_photo_refs=params.get("clean_photo_refs"),
			approval_url=params.get("approval_url"),
			eta_window=params.get("eta_window"),
			internal_start_time=params.get("internal_start_time"),
			channel="email" if channel == "email" else "sms",
			subject_hint=params.get("subject"),
		)
		contact = _get_ro_customer_contact(ro_doc)
		if action == "DISPATCH_CUSTOMER_SMS" and not params.get("phone_number") and contact.get("phone"):
			params["phone_number"] = contact["phone"]
		if action == "DISPATCH_CUSTOMER_EMAIL" and not params.get("recipient") and contact.get("email"):
			params["recipient"] = contact["email"]
		card["message_preview"] = drafted["body"]
		card["subject"] = drafted.get("subject")
		card["channel"] = drafted.get("channel")
		card["sanitized_facts_hash"] = drafted.get("sanitized_facts_hash")
		card["clean_photo_refs"] = drafted.get("clean_photo_refs") or []
		card["editable_message"] = True
		card["params"] = params

	elif action == "RECORD_DEPOSIT":
		amt = flt(params.get("amount", 0.0), 2)
		card["title"] = summary or f"Record ${amt:,.2f} Deposit on {ro_name or 'RO'}"
		card["amount"] = amt
		card["options"] = [{"label": m, "value": m} for m in DEFAULT_PAYMENT_METHODS]
		card["editable_fields"] = ["payment_method", "reference_no", "amount"]

	elif action == "CHECKOUT_REPAIR_ORDER":
		grand_total = flt(getattr(ro_doc, "grand_total", 0.0), 2) if ro_doc else flt(params.get("grand_total", 0.0), 2)
		deposit_amount = flt(getattr(ro_doc, "deposit_amount", 0.0), 2) if ro_doc else 0.0
		outstanding = flt(getattr(ro_doc, "outstanding_amount", grand_total - deposit_amount), 2) if ro_doc else grand_total
		card["title"] = summary or f"Checkout & Release {ro_name or 'RO'} (${outstanding:,.2f} due)"
		card["grand_total"] = grand_total
		card["deposit_amount"] = deposit_amount
		card["outstanding_amount"] = outstanding
		card["odometer_out"] = params.get("odometer_out") or (getattr(ro_doc, "odometer_out", None) if ro_doc else None)
		card["options"] = [{"label": m, "value": m} for m in DEFAULT_PAYMENT_METHODS]
		card["editable_fields"] = ["payment_method", "odometer_out", "paid_amount"]

	elif action == "RESCHEDULE_APPOINTMENT":
		slots = list(params.get("candidate_slots") or [])
		if not slots and ro_name:
			try:
				from ism.api import agent_scheduling

				slot_res = agent_scheduling.find_available_slots(
					repair_order=ro_name,
					duration_minutes=params.get("duration_minutes"),
					days_ahead=3,
					max_slots=5,
				)
				slots = list((slot_res.get("data") or {}).get("slots") or [])
			except Exception:
				slots = []
		params["candidate_slots"] = slots
		if slots:
			card["options"] = _format_slot_options(slots)
			first_start = slots[0].get("start_time") or slots[0].get("scheduled_time")
			customer_eta = _resolve_customer_arrival_time_safe(first_start)
			drafted = _sanitize_and_draft_comms(
				ro_doc,
				finding_summary=params.get("finding_summary") or "We need to update your appointment timing.",
				eta_window=f"{slots[0].get('scheduled_date', '')} at {customer_eta}".strip() if customer_eta else None,
				channel="sms",
			)
			card["customer_eta_preview"] = drafted["body"]
			card["customer_arrival_time"] = customer_eta
		card["send_customer_sms_default"] = bool(params.get("send_customer_sms", False))
		card["editable_fields"] = ["slot_index", "scheduled_date", "start_time", "send_customer_sms"]
		card["params"] = params

	elif action == "INTAKE_QUOTE_AND_BOOK":
		# Optionally price any parts in the intake services internally
		if params.get("parts"):
			params["parts"] = _price_parts_internally(params.get("parts"))
		slots = list(params.get("candidate_slots") or [])
		if not slots:
			try:
				from ism.api import agent_scheduling

				slot_res = agent_scheduling.find_available_slots(
					duration_minutes=params.get("duration_minutes") or 60,
					start_date=params.get("start_date"),
					days_ahead=3,
					max_slots=5,
				)
				slots = list((slot_res.get("data") or {}).get("slots") or [])
			except Exception:
				slots = []
		params["candidate_slots"] = slots
		if slots:
			card["options"] = _format_slot_options(slots)
		card["title"] = summary or f"Create Quote & Book Intake ({params.get('customer_name') or 'New Customer'})"
		card["editable_fields"] = ["slot_index", "scheduled_date", "start_time", "odometer_in", "provisional"]
		card["params"] = params

	elif action == "CONVERT_APPOINTMENT_TO_RO":
		card["title"] = summary or f"Check In Appointment {params.get('schedule_entry') or ''} -> Repair Order"
		card["editable_fields"] = ["odometer_in"]

	elif action == "RECEIVE_PO":
		target = params.get("purchase_order") or params.get("purchase_invoice") or "Order"
		card["title"] = summary or f"Receive Parts against {target}"

	elif action == "POST_PURCHASE_INVOICE":
		bill_no = params.get("bill_no") or "Pending Bill #"
		card["title"] = summary or f"Post Purchase Invoice ({bill_no})"
		card["update_stock"] = bool(params.get("update_stock", False))
		card["editable_fields"] = ["bill_no", "bill_date", "update_stock", "also_receive_po"]

	elif action == "PAY_PURCHASE":
		target = params.get("purchase_invoice") or params.get("purchase_order") or "Vendor"
		card["title"] = summary or f"Record Supplier Payment for {target}"
		card["options"] = [{"label": m, "value": m} for m in DEFAULT_PAYMENT_METHODS]
		card["editable_fields"] = ["payment_method", "paid_amount", "reference_no", "reference_date"]

	return validate_approval_gate_card(card)


# ---------------------------------------------------------------------------
# Manager-Gated Action Executors (13 actions)
# ---------------------------------------------------------------------------


def _resolve_chosen_slot(
	params: dict[str, Any],
	selected_option: Any,
	form_patch: dict[str, Any],
) -> dict[str, Any]:
	slots = list(params.get("candidate_slots") or [])
	slot_idx = form_patch.get("slot_index")
	if slot_idx is None and selected_option is not None and str(selected_option).strip().isdigit():
		slot_idx = int(str(selected_option).strip())

	chosen: dict[str, Any] = {}
	if slot_idx is not None and slots:
		idx_int = cint(slot_idx)
		if 0 <= idx_int < len(slots):
			chosen = dict(slots[idx_int])

	if form_patch.get("scheduled_date"):
		chosen["scheduled_date"] = form_patch["scheduled_date"]
	elif not chosen.get("scheduled_date") and params.get("scheduled_date"):
		chosen["scheduled_date"] = params["scheduled_date"]

	if form_patch.get("start_time"):
		chosen["start_time"] = form_patch["start_time"]
	elif not chosen.get("start_time") and params.get("start_time"):
		chosen["start_time"] = params["start_time"]

	if form_patch.get("service_bay"):
		chosen["service_bay"] = form_patch["service_bay"]
	elif not chosen.get("service_bay") and params.get("service_bay"):
		chosen["service_bay"] = params["service_bay"]

	if form_patch.get("technician"):
		chosen["technician"] = form_patch["technician"]
	elif not chosen.get("technician") and params.get("technician"):
		chosen["technician"] = params["technician"]

	return chosen


def _detect_sms_opt_out(sms_result: dict[str, Any]) -> bool:
	if sms_result.get("opted_out") is True:
		return True
	err = str(sms_result.get("error") or "").lower()
	return any(tok in err for tok in _OPT_OUT_TOKENS)


def _execute_manager_gated_action(
	action_type: str,
	params: dict[str, Any],
	*,
	card_schema: dict[str, Any],
	selected_option: Any = None,
	edited_message: str | None = None,
	form_patch: dict[str, Any] | None = None,
	repair_order: str | None = None,
) -> dict[str, Any]:
	patch = dict(form_patch or {})
	merged = {**params, **patch}
	ro_name = merged.get("repair_order") or repair_order

	if action_type == "SUBMIT_PO":
		from ism.api import agent_procurement

		po_names = merged.get("purchase_orders")
		if not po_names and merged.get("purchase_order"):
			po_names = [merged["purchase_order"]]
		if po_names:
			submitted: list[str] = []
			for po_name in po_names:
				po = frappe.get_doc("Purchase Order", po_name)
				po.check_permission("submit")
				if po.docstatus == 0:
					po.submit()
				submitted.append(po.name)
			return {"success": True, "submitted_purchase_orders": submitted, "repair_order": ro_name}
		return agent_procurement.submit_procurement_orders(
			repair_order=ro_name,
			parts_data=merged.get("parts_data") or merged.get("parts"),
			submit=True,
			company=merged.get("company"),
			payment_method=merged.get("payment_method"),
			paid_amount=merged.get("paid_amount"),
			reference_no=merged.get("reference_no"),
			reference_date=merged.get("reference_date"),
		)

	if action_type == "AMEND_QUOTE":
		from ism.api import agent_quotes

		return agent_quotes.amend_quote(
			repair_order=ro_name,
			add_services=merged.get("add_services"),
			remove_service_indices=merged.get("remove_service_indices"),
			update_services=merged.get("update_services"),
			notes=merged.get("notes"),
			discount_amount=merged.get("discount_amount"),
			shop_supplies_amount=merged.get("shop_supplies_amount"),
			environmental_fee_amount=merged.get("environmental_fee_amount"),
		)

	if action_type == "SEND_QUOTE_APPROVAL":
		from ism.api import agent_quotes

		return agent_quotes.send_quote_for_approval(
			repair_order=ro_name,
			communication_medium=merged.get("communication_medium") or "sms",
			phone_number=merged.get("phone_number"),
			recipient=merged.get("recipient"),
		)

	if action_type == "DISPATCH_CUSTOMER_SMS":
		from ism.api.sms import dispatch_sms

		phone = merged.get("phone_number") or merged.get("recipient")
		if not phone and ro_name and frappe.db.exists("Repair Order", ro_name):
			ro_doc = frappe.get_doc("Repair Order", ro_name)
			phone = _get_ro_customer_contact(ro_doc).get("phone")
		message = (
			edited_message.strip()
			if isinstance(edited_message, str) and edited_message.strip()
			else (merged.get("message") or card_schema.get("message_preview") or "").strip()
		)
		if not phone:
			return {
				"success": False,
				"delivery_status": "FAILED",
				"opted_out": False,
				"error": "Customer phone number is required to dispatch SMS.",
				"sms_result": {"success": False, "message_id": None, "error": "Missing customer phone number."},
			}
		sms_res = dispatch_sms(phone, message)
		success = bool(isinstance(sms_res, dict) and sms_res.get("success"))
		opted_out = _detect_sms_opt_out(sms_res) if isinstance(sms_res, dict) else False
		return {
			"success": success,
			"delivery_status": "SENT" if success else ("OPTED_OUT" if opted_out else "FAILED"),
			"opted_out": opted_out,
			"phone_number": phone,
			"message": message,
			"message_id": sms_res.get("message_id") if isinstance(sms_res, dict) else None,
			"error": sms_res.get("error") if isinstance(sms_res, dict) else "SMS dispatch failed.",
			"sms_result": sms_res,
		}

	if action_type == "DISPATCH_CUSTOMER_EMAIL":
		recipient = merged.get("recipient") or merged.get("email")
		if not recipient and ro_name and frappe.db.exists("Repair Order", ro_name):
			ro_doc = frappe.get_doc("Repair Order", ro_name)
			recipient = _get_ro_customer_contact(ro_doc).get("email")
		subject = merged.get("subject") or card_schema.get("subject") or f"Service Update ({ro_name or 'ISM'})"
		body = (
			edited_message.strip()
			if isinstance(edited_message, str) and edited_message.strip()
			else (merged.get("message") or card_schema.get("message_preview") or "").strip()
		)
		if not recipient:
			return {
				"success": False,
				"delivery_status": "FAILED",
				"error": "Customer email address is required.",
			}
		try:
			frappe.sendmail(
				recipients=[recipient],
				subject=subject,
				message=body,
				reference_doctype="Repair Order" if ro_name else None,
				reference_name=ro_name,
				now=True,
			)
			return {
				"success": True,
				"delivery_status": "SENT",
				"recipient": recipient,
				"subject": subject,
			}
		except Exception as exc:
			return {
				"success": False,
				"delivery_status": "FAILED",
				"recipient": recipient,
				"subject": subject,
				"error": str(exc),
			}

	if action_type == "RECORD_DEPOSIT":
		from ism.api import agent_quotes

		method = patch.get("payment_method") or (
			str(selected_option).strip() if selected_option else None
		) or merged.get("payment_method") or "Debit / Interac"
		return agent_quotes.record_ro_deposit(
			repair_order=ro_name,
			amount=merged.get("amount"),
			payment_method=method,
			reference_no=merged.get("reference_no"),
		)

	if action_type == "CHECKOUT_REPAIR_ORDER":
		from ism.api import agent_quotes

		method = patch.get("payment_method") or (
			str(selected_option).strip() if selected_option else None
		) or merged.get("payment_method")
		return agent_quotes.checkout_repair_order(
			repair_order=ro_name,
			payment_method=method,
			paid_amount=merged.get("paid_amount"),
			odometer_out=merged.get("odometer_out"),
			payments=merged.get("payments"),
			confirm=True,
		)

	if action_type == "RESCHEDULE_APPOINTMENT":
		from ism.api import agent_scheduling

		schedule_entry = merged.get("schedule_entry")
		if not schedule_entry and ro_name:
			rows = frappe.get_all(
				"Schedule Entry",
				filters={"repair_order": ro_name, "status": ["not in", ["Cancelled", "No Show", "Completed"]]},
				pluck="name",
				order_by="scheduled_date asc, scheduled_time asc",
				limit=1,
			)
			schedule_entry = rows[0] if rows else None
		if not schedule_entry:
			frappe.throw(_("Schedule Entry is required to reschedule appointment."), frappe.ValidationError)

		slot = _resolve_chosen_slot(params, selected_option, patch)
		res = agent_scheduling.reschedule_appointment(
			schedule_entry=schedule_entry,
			scheduled_date=slot.get("scheduled_date"),
			start_time=slot.get("start_time"),
			duration_minutes=merged.get("duration_minutes"),
			service_bay=slot.get("service_bay"),
			technician=slot.get("technician"),
			notes=merged.get("notes"),
		)

		sms_dispatch_res = None
		if bool(merged.get("send_customer_sms", False)):
			from ism.api.sms import dispatch_sms

			ro_doc = frappe.get_doc("Repair Order", ro_name) if (ro_name and frappe.db.exists("Repair Order", ro_name)) else None
			phone = merged.get("phone_number") or (_get_ro_customer_contact(ro_doc).get("phone") if ro_doc else None)
			customer_time = _resolve_customer_arrival_time_safe(slot.get("start_time"))
			eta_text = f"{slot.get('scheduled_date', '')} at {customer_time}".strip() if customer_time else ""
			if edited_message and isinstance(edited_message, str) and edited_message.strip():
				sms_body = edited_message.strip()
			else:
				drafted = _sanitize_and_draft_comms(
					ro_doc,
					finding_summary=merged.get("finding_summary") or "Your appointment has been rescheduled.",
					eta_window=eta_text or None,
					channel="sms",
				)
				sms_body = drafted["body"]
			if phone:
				sms_dispatch_res = dispatch_sms(phone, sms_body)

		return {
			"success": True,
			"reschedule": res,
			"chosen_slot": slot,
			"customer_arrival_time": _resolve_customer_arrival_time_safe(slot.get("start_time")),
			"sms_result": sms_dispatch_res,
		}

	if action_type == "INTAKE_QUOTE_AND_BOOK":
		from ism.api import agent_quotes, agent_scheduling

		slot = _resolve_chosen_slot(params, selected_option, patch)
		is_provisional = bool(merged.get("provisional", False))
		quote_res = None
		created_ro = None
		ro_thread = None

		if not is_provisional:
			priced_parts = _price_parts_internally(merged.get("parts"))
			quote_res = agent_quotes.create_quote(
				customer_name=merged.get("customer_name"),
				vehicle_identifier=merged.get("vehicle_identifier") or merged.get("vin"),
				customer=merged.get("customer"),
				vehicle=merged.get("vehicle"),
				services=merged.get("services"),
				parts=priced_parts or None,
				notes=merged.get("notes"),
				concern=merged.get("concern") or merged.get("customer_concern"),
				customer_phone=merged.get("customer_phone"),
				customer_email=merged.get("customer_email"),
				company=merged.get("company"),
				odometer_in=merged.get("odometer_in"),
			)
			created_ro = (quote_res.get("data") or {}).get("name") or (quote_res.get("data") or {}).get("repair_order")

		booking_res = None
		if slot.get("scheduled_date") and slot.get("start_time"):
			booking_res = agent_scheduling.book_appointment(
				repair_order=created_ro,
				scheduled_date=slot["scheduled_date"],
				start_time=slot["start_time"],
				service_bay=slot.get("service_bay"),
				technician=slot.get("technician"),
				entry_type=merged.get("entry_type"),
				notes=merged.get("notes"),
				estimated_duration=merged.get("duration_minutes") or merged.get("estimated_duration"),
				provisional_customer_name=merged.get("customer_name") if is_provisional else None,
				provisional_vehicle_info=(
					merged.get("provisional_vehicle_info") or merged.get("vehicle_identifier") or merged.get("vin")
				)
				if is_provisional
				else None,
			)

		urls = {}
		if created_ro:
			ro_thread = ensure_thread_for_ro(created_ro)
			recompute_thread_projection(ro_thread, publish=True)
			urls = build_ro_urls(created_ro)

		return {
			"success": True,
			"repair_order": created_ro,
			"ro_thread_key": ro_thread,
			"board_url": urls.get("board_url"),
			"quote": quote_res,
			"booking": booking_res,
			"chosen_slot": slot,
		}

	if action_type == "CONVERT_APPOINTMENT_TO_RO":
		from ism.api import agent_scheduling

		odometer_in = merged.get("odometer_in")
		if odometer_in is None or str(odometer_in).strip() == "":
			frappe.throw(
				_("Verified odometer_in is required to convert an appointment into a Repair Order."),
				frappe.ValidationError,
			)
		res = agent_scheduling.create_ro_from_appointment(
			schedule_entry=merged.get("schedule_entry"),
			odometer_in=cint(odometer_in),
		)
		created_ro = (res.get("data") or {}).get("repair_order") or (res.get("data") or {}).get("name")
		ro_thread = None
		urls = {}
		if created_ro:
			ro_thread = ensure_thread_for_ro(created_ro)
			recompute_thread_projection(ro_thread, publish=True)
			urls = build_ro_urls(created_ro)
		return {
			"success": True,
			"repair_order": created_ro,
			"ro_thread_key": ro_thread,
			"board_url": urls.get("board_url"),
			"conversion": res,
		}

	if action_type == "RECEIVE_PO":
		from ism.api import agent_procurement

		return agent_procurement.receive_purchase_order(
			purchase_order=merged.get("purchase_order"),
			submit_po=merged.get("submit_po", True),
			purchase_invoice=merged.get("purchase_invoice"),
		)

	if action_type == "POST_PURCHASE_INVOICE":
		from ism.api import agent_procurement

		update_stock = bool(merged.get("update_stock", False))
		pi_res = agent_procurement.create_purchase_invoice(
			purchase_order=merged.get("purchase_order"),
			supplier=merged.get("supplier"),
			parts_data=merged.get("parts_data") or merged.get("parts"),
			update_stock=update_stock,
			company=merged.get("company"),
			bill_no=merged.get("bill_no"),
			bill_date=merged.get("bill_date"),
			attach_advances=merged.get("attach_advances", True),
		)
		pr_res = None
		if bool(merged.get("also_receive_po", False)) and not update_stock:
			po_ref = merged.get("purchase_order")
			pi_name = (pi_res.get("data") or {}).get("purchase_invoice")
			if po_ref or pi_name:
				pr_res = agent_procurement.receive_purchase_order(
					purchase_order=po_ref,
					purchase_invoice=None if po_ref else pi_name,
				)
		return {
			"success": True,
			"purchase_invoice_result": pi_res,
			"purchase_receipt_result": pr_res,
		}

	if action_type == "PAY_PURCHASE":
		from ism.api import agent_procurement

		method = patch.get("payment_method") or (
			str(selected_option).strip() if selected_option else None
		) or merged.get("payment_method")
		return agent_procurement.create_purchase_payment(
			purchase_order=merged.get("purchase_order"),
			purchase_invoice=merged.get("purchase_invoice"),
			payment_method=method,
			paid_amount=merged.get("paid_amount"),
			reference_no=merged.get("reference_no"),
			reference_date=merged.get("reference_date"),
		)

	raise BoardContractError(f"Unsupported manager-gated action: {action_type!r}")


# ---------------------------------------------------------------------------
# Public Policy Router: dispatch_orchestrator_decision
# ---------------------------------------------------------------------------


def _build_clarification_schema(
	*,
	action: str | None,
	action_payload: dict[str, Any],
	missing_fields: list[str],
	clarification: dict[str, Any] | None,
	acoustic_clarity_capped: bool,
	audio_clarity_score: float | None,
	effective_confidence: float,
	raw_confidence: float,
	source_event_name: str,
	followup_actions: list[dict[str, Any]],
) -> dict[str, Any]:
	clar = dict(clarification or {})
	prompt = clar.get("prompt") or clar.get("question")
	if not prompt:
		if acoustic_clarity_capped:
			prompt = "Audio was noisy — please confirm or re-record this capture before taking action."
		elif missing_fields:
			prompt = f"Please provide missing information ({', '.join(missing_fields)}) to continue."
		else:
			prompt = "Please clarify the intended action before updating the Repair Order."

	input_field = clar.get("input_field")
	if not input_field and missing_fields:
		for candidate in ("odometer_out", "odometer_in", "bill_no", "amount"):
			if candidate in missing_fields:
				input_field = candidate
				break

	options = clar.get("options")
	if options is None and not input_field:
		options = [
			{"label": "Confirm", "value": "confirm"},
			{"label": "Clarify / Retry", "value": "retry"},
		]

	default_role = "MANAGER" if (action in MANAGER_GATED_ACTIONS) else "TECH"
	target_role = clar.get("target_role") or default_role

	card: dict[str, Any] = {
		"prompt": prompt,
		"target_role": target_role,
		"missing_fields": list(missing_fields),
		"acoustic_clarity_capped": acoustic_clarity_capped,
		"audio_clarity_score": audio_clarity_score,
		"effective_confidence": effective_confidence,
		"raw_confidence": raw_confidence,
		"source_event": source_event_name,
		"action_payload": action_payload,
		"followup_actions": followup_actions,
	}
	if action and action in ALL_ACTIONS:
		card["action_type"] = action
	if options is not None:
		card["options"] = options
	if input_field:
		card["input_field"] = input_field

	return validate_clarification_card(card)


def dispatch_orchestrator_decision(source_event: Any, decision: dict[str, Any]) -> dict[str, Any]:
	"""Route an AI orchestrator decision into CLARIFICATION_CARD, AI_RECEIPT, or APPROVAL_GATE."""
	if isinstance(source_event, str):
		source_doc = frappe.get_doc("Shop Board Event", source_event)
	elif hasattr(source_event, "name") and hasattr(source_event, "thread"):
		source_doc = source_event
	else:
		raise BoardContractError("source_event must be a Shop Board Event document or event name.")

	if not isinstance(decision, dict):
		raise BoardContractError("decision must be a dict.")

	threshold = _get_confidence_threshold()
	raw_confidence = flt(decision.get("confidence", 0.0))
	audio_clarity_raw = decision.get("audio_clarity_score")
	audio_clarity_score = flt(audio_clarity_raw) if audio_clarity_raw is not None else None

	acoustic_clarity_capped = False
	effective_confidence = raw_confidence
	if audio_clarity_score is not None and audio_clarity_score < ACOUSTIC_CLARITY_FLOOR:
		effective_confidence = min(raw_confidence, ACOUSTIC_CLARITY_CONFIDENCE_CAP)
		acoustic_clarity_capped = True

	# Persist non-immutable AI telemetry on the source event
	source_updates: dict[str, Any] = {"confidence_score": effective_confidence}
	if decision.get("transcript") and not getattr(source_doc, "transcript", None):
		source_updates["transcript"] = str(decision["transcript"])
	frappe.db.set_value("Shop Board Event", source_doc.name, source_updates, update_modified=False)

	action = str(decision.get("action") or "").strip()
	if action and action not in ALL_ACTIONS:
		raise BoardContractError(f"Unknown board action: {action!r}.")

	action_payload = dict(decision.get("action_payload") or {})
	missing_fields = [str(f).strip() for f in (decision.get("missing_fields") or []) if str(f).strip()]
	clarification = decision.get("clarification")
	customer_comms = decision.get("customer_comms")
	followup_actions = list(decision.get("followup_actions") or [])
	summary = str(decision.get("summary") or "").strip()
	intent = decision.get("intent")

	# Zero-hallucination mandatory odometer guard for UPDATE_JOB_STATUS(complete)
	if action == "UPDATE_JOB_STATUS" and str(action_payload.get("action") or "").strip().lower() == "complete":
		ro_name = action_payload.get("repair_order") or source_doc.repair_order
		odo_payload = action_payload.get("odometer_out")
		existing_odo = (
			cint(frappe.db.get_value("Repair Order", ro_name, "odometer_out"))
			if (ro_name and frappe.db.exists("Repair Order", ro_name))
			else 0
		)
		if (odo_payload is None or str(odo_payload).strip() == "") and existing_odo <= 0:
			if "odometer_out" not in missing_fields:
				missing_fields.append("odometer_out")

	needs_clarification = (
		acoustic_clarity_capped
		or effective_confidence < threshold
		or bool(missing_fields)
		or (not action)
	)

	if needs_clarification:
		clar_schema = _build_clarification_schema(
			action=action or None,
			action_payload=action_payload,
			missing_fields=missing_fields,
			clarification=clarification if isinstance(clarification, dict) else None,
			acoustic_clarity_capped=acoustic_clarity_capped,
			audio_clarity_score=audio_clarity_score,
			effective_confidence=effective_confidence,
			raw_confidence=raw_confidence,
			source_event_name=source_doc.name,
			followup_actions=followup_actions,
		)
		ev = _insert_board_event(
			thread_key=source_doc.thread,
			thread_type=source_doc.thread_type,
			repair_order=source_doc.repair_order,
			actor_type="AI",
			intent_tag=intent,
			payload_type="CLARIFICATION_CARD",
			target_role=clar_schema["target_role"],
			body_text=clar_schema["prompt"],
			confidence_score=effective_confidence,
			card_schema=clar_schema,
			resolution_state="PENDING",
			parent_event=source_doc.name,
		)
		projection = recompute_thread_projection(source_doc.thread, publish=True)
		_publish_board_event(ev)
		return {
			"outcome": "CLARIFICATION_CARD",
			"effective_confidence": effective_confidence,
			"acoustic_clarity_capped": acoustic_clarity_capped,
			"events": [ev.name],
			"cards": [ev.name],
			"receipts": [],
			"gates": [],
			"projection": projection,
		}

	created_events: list[str] = []
	receipt_events: list[str] = []
	gate_events: list[str] = []
	execution_results: list[dict[str, Any]] = []

	def _emit_gate(gate_action: str, gate_payload: dict[str, Any], gate_summary: str = "", gate_comms: dict[str, Any] | None = None):
		gate_schema = _build_approval_gate_card(
			gate_action,
			gate_payload,
			source_doc=source_doc,
			summary=gate_summary,
			customer_comms=gate_comms,
		)
		gate_ev = _insert_board_event(
			thread_key=source_doc.thread,
			thread_type=source_doc.thread_type,
			repair_order=source_doc.repair_order,
			actor_type="AI",
			intent_tag=intent or gate_action,
			payload_type="APPROVAL_GATE",
			target_role="MANAGER",
			body_text=gate_schema["title"],
			confidence_score=effective_confidence,
			card_schema=gate_schema,
			resolution_state="PENDING",
			parent_event=source_doc.name,
		)
		created_events.append(gate_ev.name)
		gate_events.append(gate_ev.name)
		_publish_board_event(gate_ev)

	def _run_autonomous(auto_action: str, auto_payload: dict[str, Any], auto_summary: str = ""):
		exec_out = _execute_autonomous_action(auto_action, auto_payload, source_doc=source_doc)
		receipt_schema = validate_receipt_card(
			{
				"action_type": auto_action,
				"summary": auto_summary or exec_out["summary"],
				"undo_spec": exec_out.get("undo_spec"),
				"result": exec_out.get("result"),
				"reversible": exec_out.get("undo_spec") is not None,
			}
		)
		receipt_ev = _insert_board_event(
			thread_key=source_doc.thread,
			thread_type=source_doc.thread_type,
			repair_order=source_doc.repair_order,
			actor_type="AI",
			intent_tag=intent or auto_action,
			payload_type="AI_RECEIPT",
			target_role="ALL",
			body_text=receipt_schema["summary"],
			confidence_score=effective_confidence,
			card_schema=receipt_schema,
			resolution_state="NONE",
			parent_event=source_doc.name,
		)
		created_events.append(receipt_ev.name)
		receipt_events.append(receipt_ev.name)
		execution_results.append({"action": auto_action, "event": receipt_ev.name, "result": exec_out.get("result")})
		_publish_board_event(receipt_ev)

		for extra_gate in exec_out.get("extra_gates") or []:
			_emit_gate(
				extra_gate["action_type"],
				extra_gate.get("params") or {},
				gate_summary=extra_gate.get("title") or extra_gate.get("summary") or "",
			)

	if action in AUTONOMOUS_ACTIONS:
		_run_autonomous(action, action_payload, auto_summary=summary)
		outcome = "EXECUTED"
	elif action in MANAGER_GATED_ACTIONS:
		_emit_gate(action, action_payload, gate_summary=summary, gate_comms=customer_comms)
		outcome = "APPROVAL_GATE"
	else:
		raise BoardContractError(f"Unhandled board action: {action!r}")

	# Process followup_actions (e.g., DRAFT_PURCHASE_ORDER -> SUBMIT_PO + sanitized DISPATCH_CUSTOMER_SMS)
	has_comms_followup = False
	for item in followup_actions:
		if not isinstance(item, dict):
			continue
		f_action = str(item.get("action") or item.get("action_type") or "").strip()
		if not f_action:
			continue
		if f_action not in ALL_ACTIONS:
			raise BoardContractError(f"Unknown followup action: {f_action!r}.")
		f_payload = dict(item.get("action_payload") or item.get("params") or {})
		f_summary = str(item.get("summary") or item.get("title") or "").strip()
		if f_action in ("DISPATCH_CUSTOMER_SMS", "DISPATCH_CUSTOMER_EMAIL", "SEND_QUOTE_APPROVAL"):
			has_comms_followup = True
		if f_action in AUTONOMOUS_ACTIONS:
			_run_autonomous(f_action, f_payload, auto_summary=f_summary)
		else:
			_emit_gate(f_action, f_payload, gate_summary=f_summary, gate_comms=item.get("customer_comms") or customer_comms)

	# If top-level customer_comms dict was supplied without an explicit comms followup, emit gated SMS/Email preview
	if isinstance(customer_comms, dict) and customer_comms and not has_comms_followup and action not in (
		"DISPATCH_CUSTOMER_SMS",
		"DISPATCH_CUSTOMER_EMAIL",
		"SEND_QUOTE_APPROVAL",
	):
		comms_channel = str(customer_comms.get("channel") or "sms").strip().lower()
		comms_action = "DISPATCH_CUSTOMER_EMAIL" if comms_channel == "email" else "DISPATCH_CUSTOMER_SMS"
		_emit_gate(
			comms_action,
			dict(customer_comms),
			gate_summary=customer_comms.get("summary") or "Send Sanitized Customer Update",
			gate_comms=customer_comms,
		)

	projection = recompute_thread_projection(source_doc.thread, publish=True)
	return {
		"outcome": outcome,
		"effective_confidence": effective_confidence,
		"acoustic_clarity_capped": False,
		"events": created_events,
		"receipts": receipt_events,
		"gates": gate_events,
		"results": execution_results,
		"projection": projection,
	}


# ---------------------------------------------------------------------------
# Row-Locked Idempotent Card Resolution & Receipt Undo
# ---------------------------------------------------------------------------


def resolve_card_action(
	event_name: str,
	decision: str,
	*,
	selected_option: Any = None,
	edited_message: str | None = None,
	form_patch: dict[str, Any] | None = None,
	notes: str | None = None,
	idempotency_key: str | None = None,
	user: str | None = None,
) -> dict[str, Any]:
	"""Atomically resolve a CLARIFICATION_CARD or APPROVAL_GATE using SELECT ... FOR UPDATE."""
	actor_user = (user or getattr(frappe.session, "user", None) or "").strip()
	actor_role = resolve_actor_role(actor_user)

	decision_norm = str(decision or "").strip().lower()
	if decision_norm not in ("approve", "reject", "clarify"):
		frappe.throw(
			_("Decision must be one of: approve, reject, clarify."),
			frappe.ValidationError,
		)

	rows = frappe.db.sql(
		"""
		SELECT name, thread, thread_type, repair_order, payload_type, target_role,
		       resolution_state, card_schema, resolution_payload, parent_event
		FROM `tabShop Board Event`
		WHERE name = %s
		FOR UPDATE
		""",
		(event_name,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Shop Board Event {0} not found.").format(event_name), frappe.DoesNotExistError)

	card = rows[0]
	if card.payload_type not in PENDING_DECISION_PAYLOAD_TYPES:
		raise BoardContractError(
			f"Event {event_name} ({card.payload_type}) is not a resolvable decision card."
		)

	# Enforce Manager RBAC BEFORE returning state on APPROVAL_GATE or MANAGER-targeted cards
	if card.payload_type == "APPROVAL_GATE" or card.target_role == "MANAGER":
		assert_can_commit_approval_gate(actor_user)

	# Row-locked double-tap idempotency
	if card.resolution_state != "PENDING":
		existing_payload = _parse_json(card.resolution_payload, {})
		return {
			"already_resolved": True,
			"event_name": event_name,
			"thread_key": card.thread,
			"payload_type": card.payload_type,
			"resolution_state": card.resolution_state,
			"resolution_payload": existing_payload,
		}

	clean_idem = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
	if clean_idem:
		existing_res_ev = frappe.db.get_value(
			"Shop Board Event",
			{"idempotency_key": clean_idem},
			["name", "resolution_payload"],
			as_dict=True,
		)
		if existing_res_ev:
			return {
				"already_resolved": True,
				"event_name": event_name,
				"thread_key": card.thread,
				"payload_type": card.payload_type,
				"resolution_state": "RESOLVED",
				"resolution_event": existing_res_ev.name,
				"resolution_payload": _parse_json(existing_res_ev.resolution_payload, {}),
			}

	card_schema = _parse_json(card.card_schema, {})
	patch = dict(form_patch or {})
	resolved_at = now_datetime()

	# Mark EXECUTING under row lock
	frappe.db.set_value("Shop Board Event", event_name, "resolution_state", "EXECUTING", update_modified=True)

	if decision_norm == "reject":
		res_payload = {
			"decision": "reject",
			"notes": notes,
			"resolved_by": actor_user,
		}
		frappe.db.set_value(
			"Shop Board Event",
			event_name,
			{
				"resolution_state": "REJECTED",
				"resolved_by": actor_user,
				"resolved_at": resolved_at,
				"resolution_payload": json.dumps(res_payload, default=str),
			},
			update_modified=True,
		)
		res_ev = _insert_board_event(
			thread_key=card.thread,
			thread_type=card.thread_type,
			repair_order=card.repair_order,
			actor_type=actor_role,
			actor_user=actor_user,
			intent_tag="CARD_REJECTED",
			payload_type="CARD_RESOLUTION",
			target_role="ALL",
			body_text=f"Rejected {card_schema.get('action_type') or card.payload_type}: {notes or 'Declined'}",
			metadata=res_payload,
			resolution_state="NONE",
			parent_event=event_name,
			idempotency_key=clean_idem,
		)
		projection = recompute_thread_projection(card.thread, publish=True)
		_publish_board_event(res_ev)
		return {
			"already_resolved": False,
			"event_name": event_name,
			"thread_key": card.thread,
			"payload_type": card.payload_type,
			"resolution_state": "REJECTED",
			"resolution_event": res_ev.name,
			"resolution_payload": res_payload,
			"projection": projection,
		}

	if card.payload_type == "CLARIFICATION_CARD":
		clarification_patch: dict[str, Any] = dict(patch)
		if selected_option is not None:
			clarification_patch["selected_option"] = selected_option
			input_field = card_schema.get("input_field")
			if input_field and input_field not in clarification_patch:
				clarification_patch[input_field] = selected_option
		if notes:
			clarification_patch["notes"] = notes

		res_payload = {
			"decision": decision_norm,
			"selected_option": selected_option,
			"clarification_patch": clarification_patch,
			"notes": notes,
			"resolved_by": actor_user,
		}
		frappe.db.set_value(
			"Shop Board Event",
			event_name,
			{
				"resolution_state": "RESOLVED",
				"resolved_by": actor_user,
				"resolved_at": resolved_at,
				"resolution_payload": json.dumps(res_payload, default=str),
			},
			update_modified=True,
		)
		res_ev = _insert_board_event(
			thread_key=card.thread,
			thread_type=card.thread_type,
			repair_order=card.repair_order,
			actor_type=actor_role,
			actor_user=actor_user,
			intent_tag="CLARIFICATION_RESOLVED",
			payload_type="CARD_RESOLUTION",
			target_role="ALL",
			body_text=f"Clarified: {selected_option or json.dumps(clarification_patch, default=str)}",
			metadata=res_payload,
			resolution_state="NONE",
			parent_event=event_name,
			idempotency_key=clean_idem,
		)
		projection = recompute_thread_projection(card.thread, publish=True)
		_publish_board_event(res_ev)

		source_event_name = card_schema.get("source_event") or card.parent_event or event_name
		re_enqueue_res = _lazy_enqueue_board_event(source_event_name, clarification_patch=clarification_patch)
		return {
			"already_resolved": False,
			"event_name": event_name,
			"thread_key": card.thread,
			"payload_type": card.payload_type,
			"resolution_state": "RESOLVED",
			"clarification_patch": clarification_patch,
			"re_enqueued": re_enqueue_res,
			"resolution_event": res_ev.name,
			"resolution_payload": res_payload,
			"projection": projection,
		}

	# APPROVAL_GATE approval path (RBAC already asserted above)
	action_type = card_schema.get("action_type")
	if action_type not in MANAGER_GATED_ACTIONS:
		frappe.db.set_value("Shop Board Event", event_name, "resolution_state", "PENDING", update_modified=False)
		raise BoardContractError(f"Invalid APPROVAL_GATE action_type: {action_type!r}.")

	try:
		exec_result = _execute_manager_gated_action(
			action_type,
			dict(card_schema.get("params") or {}),
			card_schema=card_schema,
			selected_option=selected_option,
			edited_message=edited_message,
			form_patch=patch,
			repair_order=card.repair_order,
		)
	except Exception:
		frappe.db.set_value("Shop Board Event", event_name, "resolution_state", "PENDING", update_modified=False)
		raise

	res_payload = {
		"decision": "approve",
		"action_type": action_type,
		"selected_option": selected_option,
		"form_patch": patch,
		"notes": notes,
		"resolved_by": actor_user,
		"execution_result": exec_result,
	}
	frappe.db.set_value(
		"Shop Board Event",
		event_name,
		{
			"resolution_state": "RESOLVED",
			"resolved_by": actor_user,
			"resolved_at": resolved_at,
			"resolution_payload": json.dumps(res_payload, default=str),
		},
		update_modified=True,
	)

	delivery_suffix = ""
	if isinstance(exec_result, dict) and exec_result.get("delivery_status") in ("FAILED", "OPTED_OUT"):
		delivery_suffix = f" ({exec_result['delivery_status']}: {exec_result.get('error') or 'delivery failed'})"

	res_ev = _insert_board_event(
		thread_key=card.thread,
		thread_type=card.thread_type,
		repair_order=card.repair_order,
		actor_type="MANAGER",
		actor_user=actor_user,
		intent_tag=action_type,
		payload_type="CARD_RESOLUTION",
		target_role="ALL",
		body_text=f"Approved {action_type}{delivery_suffix}",
		metadata=res_payload,
		resolution_state="NONE",
		parent_event=event_name,
		idempotency_key=clean_idem,
	)
	projection = recompute_thread_projection(card.thread, publish=True)
	_publish_board_event(res_ev)

	return {
		"already_resolved": False,
		"event_name": event_name,
		"thread_key": card.thread,
		"payload_type": card.payload_type,
		"action_type": action_type,
		"resolution_state": "RESOLVED",
		"execution_result": exec_result,
		"resolution_event": res_ev.name,
		"resolution_payload": res_payload,
		"projection": projection,
	}


def undo_receipt_action(
	event_name: str,
	*,
	reason: str | None = None,
	idempotency_key: str | None = None,
	user: str | None = None,
) -> dict[str, Any]:
	"""Atomically undo a reversible AI_RECEIPT using SELECT ... FOR UPDATE."""
	actor_user = (user or getattr(frappe.session, "user", None) or "").strip()
	actor_role = resolve_actor_role(actor_user)

	rows = frappe.db.sql(
		"""
		SELECT name, thread, thread_type, repair_order, payload_type,
		       resolution_state, card_schema, resolution_payload
		FROM `tabShop Board Event`
		WHERE name = %s
		FOR UPDATE
		""",
		(event_name,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Shop Board Event {0} not found.").format(event_name), frappe.DoesNotExistError)

	receipt = rows[0]
	if receipt.payload_type != "AI_RECEIPT":
		raise BoardContractError(f"Event {event_name} ({receipt.payload_type}) is not an AI_RECEIPT.")

	if receipt.resolution_state == "UNDONE":
		return {
			"already_undone": True,
			"already_resolved": True,
			"event_name": event_name,
			"thread_key": receipt.thread,
			"resolution_state": "UNDONE",
			"resolution_payload": _parse_json(receipt.resolution_payload, {}),
		}

	card_schema = _parse_json(receipt.card_schema, {})
	action_type = card_schema.get("action_type")
	undo_spec = card_schema.get("undo_spec")
	if not card_schema.get("reversible", True) or not isinstance(undo_spec, dict) or not action_type:
		raise BoardContractError(f"AI receipt {event_name} does not have a reversible undo_spec.")

	undo_result = _execute_undo_action(action_type, undo_spec, reason=reason)
	resolved_at = now_datetime()
	res_payload = {
		"decision": "undo",
		"action_type": action_type,
		"reason": reason,
		"undone_by": actor_user,
		"undo_result": undo_result,
	}
	frappe.db.set_value(
		"Shop Board Event",
		event_name,
		{
			"resolution_state": "UNDONE",
			"resolved_by": actor_user,
			"resolved_at": resolved_at,
			"resolution_payload": json.dumps(res_payload, default=str),
		},
		update_modified=True,
	)

	clean_idem = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
	res_ev = _insert_board_event(
		thread_key=receipt.thread,
		thread_type=receipt.thread_type,
		repair_order=receipt.repair_order,
		actor_type=actor_role,
		actor_user=actor_user,
		intent_tag="UNDO",
		payload_type="CARD_RESOLUTION",
		target_role="ALL",
		body_text=f"Undid {action_type}: {reason or 'Reverted via Shop Board'}",
		metadata=res_payload,
		resolution_state="NONE",
		parent_event=event_name,
		idempotency_key=clean_idem,
	)
	projection = recompute_thread_projection(receipt.thread, publish=True)
	_publish_board_event(res_ev)

	return {
		"already_undone": False,
		"already_resolved": False,
		"event_name": event_name,
		"thread_key": receipt.thread,
		"action_type": action_type,
		"resolution_state": "UNDONE",
		"undo_result": undo_result,
		"resolution_event": res_ev.name,
		"resolution_payload": res_payload,
		"projection": projection,
	}


__all__ = [
	"ACOUSTIC_CLARITY_CONFIDENCE_CAP",
	"ACOUSTIC_CLARITY_FLOOR",
	"ADMIN_MEDIA_ROLES",
	"DEFAULT_CONFIDENCE_THRESHOLD",
	"DEFAULT_PAYMENT_METHODS",
	"MANAGER_ROLES",
	"TECH_ROLES",
	"assert_can_commit_approval_gate",
	"assert_can_manage_media_storage",
	"dispatch_orchestrator_decision",
	"resolve_actor_role",
	"resolve_card_action",
	"undo_receipt_action",
]
