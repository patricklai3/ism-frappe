# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""RBAC helpers, Board AI result dispatcher (plain follow-up TEXT + Shop Action Item / ACTION_CARD), and manual action status fallback."""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from ism.board.contracts import (
	SELF_ECHO_DEDUP_PREFIX,
	SUGGESTED_ACTION_TO_CLEARING_CONDITION,
	parse_ro_from_thread_key,
	ro_thread_key,
	validate_action_card,
)
from ism.board.projections import recompute_thread_projection
from ism.ism.doctype.shop_action_item.clearing import (
	CLEARING_CONDITION_DESCRIPTIONS,
	build_clearing_condition_for_ro,
)
from ism.ism.doctype.shop_action_item.shop_action_item import create_shop_action_item

MANAGER_ROLES: frozenset[str] = frozenset({"System Manager", "ISM Manager", "ISM Service Advisor"})
ADMIN_MEDIA_ROLES: frozenset[str] = frozenset({"System Manager", "ISM Manager"})
TECH_ROLES: frozenset[str] = frozenset({"ISM Technician"})

MAX_DISPATCHED_ACTION_ITEMS = 5


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


def assert_can_manage_board_actions(user: str | None = None) -> str:
	"""Assert that user is a Manager/Advisor/System Manager; raise frappe.PermissionError otherwise."""
	role = resolve_actor_role(user)
	if role != "MANAGER":
		frappe.throw(
			_("Only ISM Manager, ISM Service Advisor, or System Manager can manage board action cards."),
			frappe.PermissionError,
		)
	return "MANAGER"


assert_can_commit_approval_gate = assert_can_manage_board_actions


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
# Internal Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _as_system_user():
	"""Temporarily elevate to Administrator for verified internal ERP calls; always restores prior user."""
	prev_user = getattr(frappe.session, "user", None) or "Administrator"
	if prev_user == "Administrator":
		yield prev_user
		return
	frappe.set_user("Administrator")
	try:
		yield prev_user
	finally:
		frappe.set_user(prev_user)


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


def _lazy_enqueue_board_event(event_name: str) -> dict[str, Any]:
	try:
		from ism.board.orchestrator import enqueue_board_event
	except ImportError:
		return {
			"enqueued": False,
			"orchestrator_installed": False,
			"event_name": event_name,
		}
	res = enqueue_board_event(event_name)
	if isinstance(res, dict):
		return {"enqueued": True, "orchestrator_installed": True, **res}
	return {"enqueued": True, "orchestrator_installed": True, "result": res}


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
	action_item: str | None = None,
	card_schema: dict[str, Any] | None = None,
	metadata: dict[str, Any] | None = None,
	resolution_state: str = "NONE",
	resolution_payload: dict[str, Any] | None = None,
	parent_event: str | None = None,
	idempotency_key: str | None = None,
	ai_status: str = "NONE",
) -> Any:
	clean_idem = (
		idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
	)
	payload: dict[str, Any] = {
		"doctype": "Shop Board Event",
		"thread": thread_key,
		"thread_type": thread_type or "REPAIR_ORDER",
		"repair_order": repair_order,
		"event_timestamp": now_datetime(),
		"actor_type": actor_type,
		"actor_user": actor_user,
		"intent_tag": intent_tag,
		"payload_type": payload_type,
		"target_role": target_role,
		"body_text": body_text,
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
	if action_item and frappe.db.has_column("Shop Board Event", "action_item"):
		payload["action_item"] = action_item

	doc = frappe.get_doc(payload)
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
		"action_item": getattr(event_doc, "action_item", None),
		"card_schema": _parse_json(getattr(event_doc, "card_schema", None), None),
		"resolution_state": event_doc.resolution_state,
		"resolution_payload": _parse_json(getattr(event_doc, "resolution_payload", None), None),
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


# ---------------------------------------------------------------------------
# Lightweight AI Result Dispatcher & Manual Action Fallback
# ---------------------------------------------------------------------------


def dispatch_board_ai_result(source_event: Any, ai_result: dict[str, Any]) -> dict[str, Any]:
	"""Dispatch lightweight Board AI output into either a plain AI TEXT follow-up question or 0..N linked Shop Action Items + ACTION_CARD events."""
	result = ai_result if isinstance(ai_result, dict) else {}
	thread_key = getattr(source_event, "thread", None) or ""
	thread_type = getattr(source_event, "thread_type", None) or "REPAIR_ORDER"
	ro_name = getattr(source_event, "repair_order", None) or parse_ro_from_thread_key(thread_key)

	raw_items = result.get("action_items") if isinstance(result.get("action_items"), list) else []
	action_items = [it for it in raw_items if isinstance(it, dict) and str(it.get("title") or "").strip()][
		:MAX_DISPATCHED_ACTION_ITEMS
	]
	raw_followup = result.get("followup_question")
	followup_question = raw_followup.strip() if isinstance(raw_followup, str) and raw_followup.strip() else None

	with _as_system_user():
		# 1. Plain natural-language AI follow-up question in chat (zero custom clarification card UI)
		if followup_question and not action_items:
			followup_idem = f"followup:{source_event.name}"
			existing_name = frappe.db.get_value(
				"Shop Board Event",
				{"idempotency_key": followup_idem},
				"name",
			)
			if existing_name:
				ev = frappe.get_doc("Shop Board Event", existing_name)
			else:
				ev = _insert_board_event(
					thread_key=thread_key,
					thread_type=thread_type,
					repair_order=ro_name,
					actor_type="AI",
					payload_type="TEXT",
					target_role="TECH",
					body_text=followup_question,
					parent_event=source_event.name,
					resolution_state="NONE",
					idempotency_key=followup_idem,
					ai_status="NONE",
				)
				_publish_board_event(ev)

			projection = recompute_thread_projection(thread_key, publish=True)
			return {
				"outcome": "FOLLOWUP_QUESTION",
				"events": [ev.name],
				"action_items": [],
				"projection": projection,
			}

		# 2. Emit 0..N Shop Action Items + linked inline ACTION_CARD events
		if not action_items:
			projection = recompute_thread_projection(thread_key, publish=True)
			return {
				"outcome": "NO_ACTION",
				"events": [],
				"action_items": [],
				"projection": projection,
			}

		media_refs = _parse_json(getattr(source_event, "media_attachments", None), [])
		if not isinstance(media_refs, list):
			media_refs = []
		transcript_excerpt = (
			getattr(source_event, "transcript", None) or getattr(source_event, "body_text", None) or ""
		)[:300]
		reporter = getattr(source_event, "actor_user", None) or "Administrator"

		created_events: list[str] = []
		created_action_items: list[str] = []

		for idx, item in enumerate(action_items, start=1):
			title = str(item.get("title") or "").strip()[:140]
			details = str(item.get("details") or "").strip()
			category = str(item.get("category") or "General").strip() or "General"
			priority = str(item.get("priority") or "Medium").strip() or "Medium"
			suggested_action = str(item.get("suggested_action") or "manual_review").strip() or "manual_review"
			part_names = (
				[str(p).strip() for p in item.get("part_names") if isinstance(p, str) and p.strip()]
				if isinstance(item.get("part_names"), list)
				else []
			)
			side = item.get("side") if isinstance(item.get("side"), str) and item.get("side").strip() else None
			odometer = item.get("odometer") if isinstance(item.get("odometer"), int) else None

			requested_ctype = item.get("clearing_condition_type")
			if not requested_ctype:
				requested_ctype = SUGGESTED_ACTION_TO_CLEARING_CONDITION.get(
					suggested_action.upper(), "manual"
				)

			overrides: dict[str, Any] = {}
			if isinstance(item.get("clearing_condition_description"), str) and item[
				"clearing_condition_description"
			].strip():
				overrides["description"] = item["clearing_condition_description"].strip()

			ctype, cparams = build_clearing_condition_for_ro(
				requested_ctype,
				ro_name,
				overrides=overrides or None,
			)
			clearing_desc = (
				str(cparams.get("description") or "").strip()
				or CLEARING_CONDITION_DESCRIPTIONS.get(ctype, "")
			)

			action_payload = {
				"suggested_action": suggested_action,
				"part_names": part_names,
				"side": side,
				"odometer": odometer,
				"media_refs": media_refs,
				"transcript_excerpt": transcript_excerpt,
				"source_event": source_event.name,
			}

			dedup_key = f"{SELF_ECHO_DEDUP_PREFIX}{source_event.name}:{idx}"
			sai_name = create_shop_action_item(
				title=title,
				category=category,
				priority=priority,
				reference_doctype="Repair Order" if ro_name else None,
				reference_name=ro_name if ro_name else None,
				details=details,
				reported_by=reporter,
				dedup_key=dedup_key,
				source="Board AI",
				board_event=source_event.name,
				action_payload_json=action_payload,
				clearing_condition_type=ctype,
				clearing_condition_json=cparams,
			)
			created_action_items.append(sai_name)

			card_schema = validate_action_card(
				{
					"action_item": sai_name,
					"repair_order": ro_name,
					"title": title,
					"details": details,
					"category": category,
					"priority": priority,
					"target_role": "MANAGER",
					"suggested_action": suggested_action,
					"clearing_condition_type": ctype,
					"clearing_condition_description": clearing_desc,
					"clearing_condition": clearing_desc,
					"part_names": part_names,
					"side": side,
					"odometer": odometer,
					"status": "Open",
					"auto_cleared": 0,
				}
			)

			card_idem = f"action_card:{sai_name}"
			existing_card = frappe.db.get_value(
				"Shop Board Event",
				{"idempotency_key": card_idem},
				"name",
			)
			if existing_card:
				card_ev = frappe.get_doc("Shop Board Event", existing_card)
			else:
				card_ev = _insert_board_event(
					thread_key=thread_key,
					thread_type=thread_type,
					repair_order=ro_name,
					actor_type="AI",
					payload_type="ACTION_CARD",
					target_role="MANAGER",
					body_text=title,
					action_item=sai_name,
					card_schema=card_schema,
					metadata={"action_item": sai_name, "repair_order": ro_name},
					resolution_state="PENDING",
					parent_event=source_event.name,
					idempotency_key=card_idem,
					ai_status="NONE",
				)
				_publish_board_event(card_ev)

			created_events.append(card_ev.name)

		projection = recompute_thread_projection(thread_key, publish=True)
		return {
			"outcome": "ACTION_CARDS",
			"events": created_events,
			"action_items": created_action_items,
			"projection": projection,
		}


def update_board_action_card_status(
	action_item: str,
	action: str,
	notes: str | None = None,
	user: str | None = None,
) -> dict[str, Any]:
	"""Manager/Advisor-only manual fallback to dismiss, resolve, or reopen a board Shop Action Item."""
	assert_can_manage_board_actions(user)
	actor_user = (user or getattr(frappe.session, "user", None) or "Administrator").strip()

	clean_item = (action_item or "").strip() if isinstance(action_item, str) else ""
	if not clean_item:
		frappe.throw(_("action_item is required."), frappe.ValidationError)

	action_norm = (action or "").strip().lower() if isinstance(action, str) else ""
	if action_norm not in ("dismiss", "resolve", "reopen"):
		frappe.throw(
			_("Invalid action '{0}'. Expected one of: dismiss, resolve, reopen.").format(action),
			frappe.ValidationError,
		)

	clean_notes = notes.strip() if isinstance(notes, str) and notes.strip() else None

	with _as_system_user():
		if not frappe.db.exists("Shop Action Item", clean_item):
			frappe.throw(
				_("Shop Action Item {0} not found.").format(clean_item),
				frappe.DoesNotExistError,
			)

		sai = frappe.get_doc("Shop Action Item", clean_item)

		if action_norm == "dismiss":
			sai.status = "Dismissed"
			sai.snoozed_until = None
			sai.resolution_notes = clean_notes or "Dismissed from board"
			sai.resolved_by = actor_user
			sai.resolved_at = now_datetime()
		elif action_norm == "resolve":
			if sai.status != "Resolved":
				sai.status = "Resolved"
				sai.snoozed_until = None
				sai.resolution_notes = clean_notes or "Resolved from board"
				sai.resolved_by = actor_user
				sai.resolved_at = now_datetime()
		else:
			sai.status = "Open"
			sai.snoozed_until = None
			sai.resolved_by = None
			sai.resolved_at = None
			if hasattr(sai, "auto_cleared"):
				sai.auto_cleared = 0
			if clean_notes:
				sai.resolution_notes = clean_notes

		# Suppress Seam 5 chat pill echo; on_shop_action_item_on_update STILL silently syncs linked ACTION_CARD in-place!
		sai.flags.skip_board_projection = True
		sai.save(ignore_permissions=True)

		thread_key = None
		projection = None
		if sai.reference_doctype == "Repair Order" and sai.reference_name:
			candidate_key = ro_thread_key(sai.reference_name)
			if frappe.db.exists("Shop Board Thread", candidate_key):
				thread_key = candidate_key
				projection = recompute_thread_projection(candidate_key, publish=True)

		return {
			"action_item": sai.name,
			"action": action_norm,
			"status": sai.status,
			"auto_cleared": cint(getattr(sai, "auto_cleared", 0)),
			"resolution_notes": sai.resolution_notes,
			"resolved_by": sai.resolved_by,
			"resolved_at": str(sai.resolved_at) if sai.resolved_at else None,
			"thread_key": thread_key,
			"projection": projection,
		}


__all__ = [
	"ADMIN_MEDIA_ROLES",
	"MANAGER_ROLES",
	"MAX_DISPATCHED_ACTION_ITEMS",
	"TECH_ROLES",
	"_as_system_user",
	"_insert_board_event",
	"_lazy_enqueue_board_event",
	"_publish_board_event",
	"assert_can_commit_approval_gate",
	"assert_can_manage_board_actions",
	"assert_can_manage_media_storage",
	"dispatch_board_ai_result",
	"resolve_actor_role",
	"update_board_action_card_status",
]
