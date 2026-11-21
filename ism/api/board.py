# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Whitelisted REST API facade for Frappe-hosted Shop OS (/board & /cockpit)."""

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
	optional_flt,
	optional_int,
	optional_str,
	parse_json_arg,
	require_str,
)
from ism.api.agent_serializers import build_ro_urls
from ism.board import cockpit_view, media_storage, pricing
from ism.board.contracts import (
	BOARD_STATUSES,
	PENDING_DECISION_PAYLOAD_TYPES,
	SINGLETON_THREADS,
	THREAD_TYPES,
)
from ism.board.gateways import (
	_as_system_user,
	_lazy_enqueue_board_event,
	_publish_board_event,
	assert_can_commit_approval_gate,
	assert_can_manage_media_storage,
	resolve_actor_role,
	resolve_card_action,
	undo_receipt_action,
)
from ism.board.projections import (
	ensure_singleton_shop_threads,
	recompute_thread_projection,
	resolve_thread_key,
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


def _serialize_thread(row: dict[str, Any] | Any) -> dict[str, Any]:
	get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
	thread_key = get("thread_key") or get("name")
	encoded_key = quote(str(thread_key), safe="")
	return {
		"name": thread_key,
		"thread_key": thread_key,
		"thread_type": get("thread_type"),
		"title": get("title"),
		"repair_order": get("repair_order"),
		"vehicle": get("vehicle"),
		"vin": get("vin"),
		"customer": get("customer"),
		"bay": get("bay"),
		"assigned_technician": get("assigned_technician"),
		"board_status": get("board_status") or "ACTIVE",
		"blocked_reason": get("blocked_reason") or "",
		"blocked_since": str(get("blocked_since")) if get("blocked_since") else None,
		"open_action_count": cint(get("open_action_count")),
		"open_approval_count": cint(get("open_approval_count")),
		"open_clarification_count": cint(get("open_clarification_count")),
		"open_action_card_ids": _parse_json_safe(get("open_action_card_ids"), []),
		"last_event_at": str(get("last_event_at")) if get("last_event_at") else None,
		"last_event_preview": get("last_event_preview") or "",
		"projection_version": cint(get("projection_version") or 1),
		"is_singleton": thread_key in SINGLETON_THREADS,
		"board_url": f"/board?thread={encoded_key}",
	}


def _serialize_event(row: dict[str, Any] | Any) -> dict[str, Any]:
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

	return {
		"name": ev_name,
		"thread": get("thread"),
		"thread_type": get("thread_type"),
		"repair_order": get("repair_order"),
		"event_timestamp": str(get("event_timestamp")) if get("event_timestamp") else None,
		"actor_type": get("actor_type"),
		"actor_user": get("actor_user"),
		"intent_tag": get("intent_tag"),
		"payload_type": get("payload_type"),
		"target_role": get("target_role") or "ALL",
		"body_text": get("body_text") or "",
		"transcript": get("transcript"),
		"confidence_score": flt(get("confidence_score")) if get("confidence_score") is not None else None,
		"metadata": _parse_json_safe(get("metadata_json"), {}),
		"media_attachments": media_list,
		"card_schema": _parse_json_safe(get("card_schema"), None),
		"resolution_state": get("resolution_state") or "NONE",
		"resolved_by": get("resolved_by"),
		"resolved_at": str(get("resolved_at")) if get("resolved_at") else None,
		"resolution_payload": _parse_json_safe(get("resolution_payload"), None),
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
	return {
		"name": ro.name,
		"status": ro.status,
		"approval_status": ro.approval_status,
		"customer": ro.customer,
		"vehicle": ro.vehicle,
		"odometer_in": ro.odometer_in,
		"odometer_out": ro.odometer_out,
		"progress_percent": flt(ro.progress_percent, 1),
		"manual_parts_hold": cint(ro.manual_parts_hold),
		"payment_status": ro.payment_status,
		"grand_total": flt(ro.grand_total, 2),
		"deposit_amount": flt(ro.deposit_amount, 2),
		"outstanding_amount": flt(ro.outstanding_amount, 2),
		"vehicle_location": ro.vehicle_location,
		"urls": build_ro_urls(ro.name),
	}


def _should_auto_enqueue_ai(metadata: dict[str, Any]) -> bool:
	if coerce_bool(metadata.get("skip_ai"), default=False):
		return False
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
		# Force LocalProxy resolution safely
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


_EVENT_FIELDS = [
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


# ---------------------------------------------------------------------------
# Whitelisted Shop Board Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_board_threads(
	thread_type: str | None = None,
	status: str | None = None,
	assigned_only: Any = False,
	query: str | None = None,
	limit: int = 50,
) -> dict[str, Any]:
	"""List active and singleton Shop Board Threads with Andon projection badges."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	with _as_system_user():
		ensure_singleton_shop_threads()

	safe_limit = optional_int(limit, "limit", min_val=1, max_val=200, default=50)
	clean_type = optional_str(thread_type, "thread_type")
	if clean_type and clean_type not in THREAD_TYPES:
		frappe.throw(
			_("Invalid thread_type '{0}'. Expected one of: {1}.").format(clean_type, ", ".join(THREAD_TYPES)),
			frappe.ValidationError,
		)

	clean_status = optional_str(status, "status")
	only_assigned = coerce_bool(assigned_only, default=False)
	clean_query = optional_str(query, "query")

	conditions: list[str] = ["1=1"]
	values: list[Any] = []

	if clean_type:
		conditions.append("thread_type = %s")
		values.append(clean_type)

	if clean_status and clean_status.upper() != "ALL":
		if clean_status.upper() == "OPEN":
			conditions.append("board_status != 'CLOSED'")
		else:
			if clean_status not in BOARD_STATUSES:
				frappe.throw(
					_("Invalid status '{0}'. Expected one of: {1}.").format(
						clean_status, ", ".join(BOARD_STATUSES)
					),
					frappe.ValidationError,
				)
			conditions.append("board_status = %s")
			values.append(clean_status)

	if only_assigned:
		emp_name = frappe.db.get_value("Employee", {"user_id": caller_user}, "name")
		if not emp_name:
			return {
				"ok": True,
				"actor_role": actor_role,
				"can_approve": actor_role == "MANAGER",
				"threads": [],
				"count": 0,
			}
		conditions.append("assigned_technician = %s")
		values.append(emp_name)

	if clean_query:
		like_val = f"%{escape_like(clean_query)}%"
		conditions.append(
			"(thread_key LIKE %s OR title LIKE %s OR repair_order LIKE %s OR vin LIKE %s "
			"OR customer LIKE %s OR bay LIKE %s OR last_event_preview LIKE %s)"
		)
		values.extend([like_val] * 7)

	where_sql = " AND ".join(conditions)
	values.append(safe_limit)
	rows = frappe.db.sql(
		f"""
		SELECT name, thread_key, thread_type, title, repair_order, vehicle, vin, customer,
		       bay, assigned_technician, board_status, blocked_reason, blocked_since,
		       open_action_count, open_approval_count, open_clarification_count,
		       open_action_card_ids, last_event_at, last_event_preview, projection_version
		FROM `tabShop Board Thread`
		WHERE {where_sql}
		ORDER BY
		    CASE WHEN thread_key IN ('SHOP:INTAKE', 'SHOP:AP_RECEIVING', 'SHOP:DISPATCH') THEN 0 ELSE 1 END ASC,
		    open_action_count DESC,
		    COALESCE(last_event_at, modified) DESC
		LIMIT %s
		""",
		tuple(values),
		as_dict=True,
	)

	threads = [_serialize_thread(r) for r in rows]
	return {
		"ok": True,
		"actor_role": actor_role,
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
	"""Return a thread's projection, paginated chronological timeline, and Seam 6 pin_state."""
	actor_role = resolve_actor_role()
	safe_limit = optional_int(limit, "limit", min_val=1, max_val=500, default=100)
	clean_before = optional_str(before_event, "before_event")

	with _as_system_user():
		canonical_key = resolve_thread_key(
			thread_key=optional_str(thread_key, "thread_key"),
			repair_order=optional_str(repair_order, "repair_order"),
		)
		thread_doc = frappe.get_doc("Shop Board Thread", canonical_key)

		conditions = ["thread = %s"]
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
		cols_sql = ", ".join(_EVENT_FIELDS)
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
		events = [_serialize_event(r) for r in page_rows]
		next_before_event = events[0]["name"] if (has_more and events) else None

		open_card_rows = frappe.get_all(
			"Shop Board Event",
			filters={
				"thread": canonical_key,
				"payload_type": ["in", list(PENDING_DECISION_PAYLOAD_TYPES)],
				"resolution_state": "PENDING",
			},
			fields=_EVENT_FIELDS,
			order_by="event_timestamp asc, creation asc",
		)
		open_cards = [_serialize_event(r) for r in open_card_rows]

		pin_state = _get_ro_pin_state_safe(
			thread_doc.repair_order if thread_doc.thread_type == "REPAIR_ORDER" else None
		)
		ro_summary = _get_ro_summary(
			thread_doc.repair_order if thread_doc.thread_type == "REPAIR_ORDER" else None
		)

	return {
		"ok": True,
		"actor_role": actor_role,
		"can_approve": actor_role == "MANAGER",
		"thread": _serialize_thread(thread_doc),
		"events": events,
		"open_cards": open_cards,
		"has_more": has_more,
		"next_before_event": next_before_event,
		"pin_state": pin_state,
		"repair_order_summary": ro_summary,
	}


@frappe.whitelist()
def get_thread_delta(
	thread_key: str | None = None,
	repair_order: str | None = None,
	after_event: str | None = None,
	known_projection_version: int = 0,
) -> dict[str, Any]:
	"""Return incremental events & updated projection state for reconnect/catch-up sync."""
	actor_role = resolve_actor_role()
	known_ver = optional_int(known_projection_version, "known_projection_version", min_val=0, default=0)
	clean_after = optional_str(after_event, "after_event")

	with _as_system_user():
		canonical_key = resolve_thread_key(
			thread_key=optional_str(thread_key, "thread_key"),
			repair_order=optional_str(repair_order, "repair_order"),
		)
		thread_doc = frappe.get_doc("Shop Board Thread", canonical_key)
		cols_sql = ", ".join(_EVENT_FIELDS)

		conditions = ["thread = %s"]
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
		new_events = [_serialize_event(r) for r in raw_rows]

		open_card_rows = frappe.get_all(
			"Shop Board Event",
			filters={
				"thread": canonical_key,
				"payload_type": ["in", list(PENDING_DECISION_PAYLOAD_TYPES)],
				"resolution_state": "PENDING",
			},
			fields=_EVENT_FIELDS,
			order_by="event_timestamp asc, creation asc",
		)
		open_cards = [_serialize_event(r) for r in open_card_rows]

		current_ver = cint(thread_doc.projection_version or 1)
		changed = current_ver > known_ver or bool(new_events)
		pin_state = _get_ro_pin_state_safe(
			thread_doc.repair_order if thread_doc.thread_type == "REPAIR_ORDER" else None
		)

	return {
		"ok": True,
		"actor_role": actor_role,
		"thread_key": canonical_key,
		"projection_version": current_ver,
		"changed": changed,
		"thread": _serialize_thread(thread_doc),
		"events": new_events,
		"open_cards": open_cards,
		"pin_state": pin_state,
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
	"""Append an immutable text/media capture event, persist media, update projection, and enqueue AI."""
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

		# Idempotency short-circuit before storing media or creating events
		if clean_idem:
			existing_name = frappe.db.get_value("Shop Board Event", {"idempotency_key": clean_idem}, "name")
			if existing_name:
				existing_doc = frappe.get_doc("Shop Board Event", existing_name)
				thread_doc = frappe.get_doc("Shop Board Thread", existing_doc.thread)
				return {
					"ok": True,
					"duplicate": True,
					"event": _serialize_event(existing_doc),
					"thread": _serialize_thread(thread_doc),
					"ai_enqueued": False,
				}

		thread_doc = frappe.get_doc("Shop Board Thread", canonical_key)

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
		enqueue_ai = _should_auto_enqueue_ai(verified_meta)
		initial_ai_status = "QUEUED" if enqueue_ai else "NONE"

		try:
			ev = frappe.get_doc(
				{
					"doctype": "Shop Board Event",
					"thread": canonical_key,
					"thread_type": thread_doc.thread_type,
					"repair_order": thread_doc.repair_order,
					"event_timestamp": now_datetime(),
					"actor_type": actor_role,
					"actor_user": caller_user,
					"intent_tag": clean_intent,
					"payload_type": payload_type,
					"target_role": "ALL",
					"body_text": clean_text or "",
					"metadata_json": json.dumps(verified_meta, default=str),
					"media_attachments": json.dumps(stored_attachments, default=str) if stored_attachments else None,
					"resolution_state": "NONE",
					"idempotency_key": clean_idem,
					"ai_status": initial_ai_status,
				}
			)
			ev.insert(ignore_permissions=True)
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			if clean_idem:
				existing_name = frappe.db.get_value("Shop Board Event", {"idempotency_key": clean_idem}, "name")
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
def resolve_board_card(
	event_name: str,
	decision: str,
	selected_option: Any = None,
	edited_message: str | None = None,
	form_patch: dict[str, Any] | str | None = None,
	notes: str | None = None,
	idempotency_key: str | None = None,
) -> dict[str, Any]:
	"""Resolve a CLARIFICATION_CARD or Manager-gated APPROVAL_GATE with FOR UPDATE idempotency."""
	clean_event = require_str(event_name, "event_name")
	clean_decision = require_str(decision, "decision")
	parsed_patch = parse_json_arg(form_patch, None, label="form_patch") if form_patch is not None else None
	if parsed_patch is not None and not isinstance(parsed_patch, dict):
		frappe.throw(_("form_patch must be a JSON object."), frappe.ValidationError)

	result = resolve_card_action(
		event_name=clean_event,
		decision=clean_decision,
		selected_option=selected_option,
		edited_message=optional_str(edited_message, "edited_message"),
		form_patch=parsed_patch,
		notes=optional_str(notes, "notes"),
		idempotency_key=optional_str(idempotency_key, "idempotency_key"),
		user=frappe.session.user,
	)
	return {"ok": True, **result}


@frappe.whitelist()
def undo_board_receipt(
	event_name: str,
	reason: str | None = None,
	idempotency_key: str | None = None,
) -> dict[str, Any]:
	"""Execute exact compensating undo on a reversible AI_RECEIPT with FOR UPDATE idempotency."""
	clean_event = require_str(event_name, "event_name")
	result = undo_receipt_action(
		event_name=clean_event,
		reason=optional_str(reason, "reason"),
		idempotency_key=optional_str(idempotency_key, "idempotency_key"),
		user=frappe.session.user,
	)
	return {"ok": True, **result}


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
		["name", "media_attachments"],
		as_dict=True,
	)
	if not event_row:
		frappe.throw(_("Board event not found."), frappe.PermissionError)
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


# ---------------------------------------------------------------------------
# Manager / Advisor Only Cockpit, Pricing & Storage Admin Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_cockpit_dashboard(date: str | None = None) -> dict[str, Any]:
	"""Return merged executive & dispatch cockpit view (Manager / Advisor / System Manager ONLY)."""
	assert_can_commit_approval_gate()
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
	assert_can_commit_approval_gate()
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
def price_part(
	tesla_oem_price: float | int | str | None = None,
	cost: float | int | str | None = None,
	part_source: str = "Aftermarket",
	part_condition: str = "New",
	override_rate: float | int | str | None = None,
	part_number: str | None = None,
	part_name: str | None = None,
) -> dict[str, Any]:
	"""Internal-only CAD parts pricing calculator (Manager / Advisor / System Manager ONLY)."""
	assert_can_commit_approval_gate()
	try:
		with _as_system_user():
			res = pricing.calculate_part_price(
				tesla_oem_price=optional_flt(tesla_oem_price, "tesla_oem_price", positive=True, default=None),
				cost=optional_flt(cost, "cost", positive=True, default=None),
				part_source=optional_str(part_source, "part_source") or "Aftermarket",
				part_condition=optional_str(part_condition, "part_condition") or "New",
				override_rate=optional_flt(override_rate, "override_rate", positive=True, default=None),
				part_number=optional_str(part_number, "part_number"),
				part_name=optional_str(part_name, "part_name"),
			)
	except ValueError as exc:
		frappe.throw(_(str(exc)), frappe.ValidationError)
	return {"ok": True, **res}


@frappe.whitelist()
def test_media_storage() -> dict[str, Any]:
	"""Verify Google Drive media storage connectivity (ISM Manager / System Manager ONLY)."""
	assert_can_manage_media_storage()
	caller_user = frappe.session.user
	with _as_system_user():
		res = media_storage.test_google_drive_connection(user=caller_user)
	return {"ok": True, **res}


__all__ = [
	"get_board_thread",
	"get_cockpit_dashboard",
	"get_media",
	"get_thread_delta",
	"list_board_threads",
	"post_board_capture",
	"price_part",
	"resolve_board_card",
	"reveal_ro_pin",
	"sync_cockpit_action",
	"test_media_storage",
	"undo_board_receipt",
]
