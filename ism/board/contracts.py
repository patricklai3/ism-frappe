# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frozen contracts, enums, and action-card validators for Active-RO Shop Board."""

from __future__ import annotations

from typing import Any

import frappe

from ism.ism.doctype.shop_action_item.clearing import CLEARING_CONDITION_TYPES

THREAD_TYPES: tuple[str, ...] = ("REPAIR_ORDER",)

BOARD_STATUSES: tuple[str, ...] = (
	"ACTIVE",
	"NEEDS_ACTION",
	"BLOCKED_PARTS",
	"WAITING_CUSTOMER",
	"CLOSED",
)

BLOCKED_BOARD_STATUSES: tuple[str, ...] = (
	"NEEDS_ACTION",
	"BLOCKED_PARTS",
	"WAITING_CUSTOMER",
)

CLOSED_RO_STATUSES: tuple[str, ...] = (
	"Closed / Released",
	"Cancelled",
	"Billed / Paid",
)

CAPTURE_LOCKED_RO_STATUSES: tuple[str, ...] = (
	"Closed / Released",
	"Cancelled",
)

ACTOR_TYPES: tuple[str, ...] = (
	"TECH",
	"MANAGER",
	"AI",
	"SYSTEM",
)

PAYLOAD_TYPES: tuple[str, ...] = (
	"MEDIA_CAPTURE",
	"TEXT",
	"ACTION_CARD",
	"STATE_MUTATION",
)

RESOLUTION_STATES: tuple[str, ...] = (
	"NONE",
	"PENDING",
	"RESOLVED",
	"REJECTED",
)

TARGET_ROLES: tuple[str, ...] = (
	"TECH",
	"MANAGER",
	"ALL",
)

AI_STATUSES: tuple[str, ...] = (
	"NONE",
	"QUEUED",
	"PROCESSING",
	"COMPLETED",
	"FAILED",
)

ACTION_CARD_CATEGORIES: tuple[str, ...] = (
	"Shop Floor Discrepancy",
	"Inventory / Parts",
	"Reordering",
	"Quote / Supplement",
	"Customer Comms",
	"Analytics Threshold",
	"General",
)

ACTION_CARD_PRIORITIES: tuple[str, ...] = (
	"Low",
	"Medium",
	"High",
	"Critical",
)

SUGGESTED_ACTION_TO_CLEARING_CONDITION: dict[str, str] = {
	"ADD_SUPPLEMENT": "ro_has_supplement_or_service",
	"ADD_SERVICE": "ro_has_supplement_or_service",
	"AMEND_QUOTE": "ro_has_supplement_or_service",
	"SEND_APPROVAL": "ro_approval_status_in",
	"SEND_QUOTE_APPROVAL": "ro_approval_status_in",
	"ORDER_PARTS": "po_submitted_or_received",
	"PROCURE_PARTS": "po_submitted_or_received",
	"CREATE_PO": "po_submitted_or_received",
	"RECEIVE_PARTS": "po_submitted_or_received",
	"CHECK_PARTS": "ro_parts_covered",
	"CLEAR_PARTS_HOLD": "ro_parts_hold_cleared",
	"SET_PARTS_HOLD": "ro_parts_hold_cleared",
	"UPDATE_JOB_STATUS": "ro_status_in",
	"COMPLETE_JOB": "ro_status_in",
	"WORK_COMPLETED": "ro_status_in",
	"RESCHEDULE": "ro_schedule_updated",
	"UPDATE_SCHEDULE": "ro_schedule_updated",
	"NOTIFY_CUSTOMER": "ro_sms_or_email_sent",
	"SEND_SMS": "ro_sms_or_email_sent",
	"SEND_EMAIL": "ro_sms_or_email_sent",
	"CUSTOMER_UPDATE": "ro_sms_or_email_sent",
	"CONVERT_BOOKING": "schedule_converted_to_ro",
	"CHECKOUT_RO": "ro_closed",
	"CLOSE_RO": "ro_closed",
	"MANUAL_REVIEW": "manual",
}

SELF_ECHO_DEDUP_PREFIX = "board:"

IMMUTABLE_EVENT_FIELDS: tuple[str, ...] = (
	"thread",
	"thread_type",
	"repair_order",
	"actor_type",
	"actor_user",
	"payload_type",
	"body_text",
	"metadata_json",
	"media_attachments",
	"idempotency_key",
)


class BoardContractError(frappe.ValidationError, ValueError):
	"""Raised when a board identifier or action-card payload violates frozen contracts."""


def ro_thread_key(ro_name: str) -> str:
	"""Return canonical thread_key ``RO:<ro_name>``, normalizing already-prefixed values."""
	if not isinstance(ro_name, str) or not ro_name.strip():
		raise BoardContractError("ro_name must be a non-empty string.")
	cleaned = ro_name.strip()
	if cleaned.startswith("RO:"):
		inner = cleaned[3:].strip()
		if not inner:
			raise BoardContractError("ro_name cannot be empty after 'RO:' prefix.")
		return f"RO:{inner}"
	return f"RO:{cleaned}"


def parse_ro_from_thread_key(thread_key: str | None) -> str | None:
	"""Extract Repair Order name from a canonical ``RO:<ro_name>`` key, or None."""
	if not isinstance(thread_key, str):
		return None
	cleaned = thread_key.strip()
	if not cleaned.startswith("RO:"):
		return None
	inner = cleaned[3:].strip()
	return inner or None


def validate_action_card(card: dict[str, Any]) -> dict[str, Any]:
	"""Validate and normalize an ``ACTION_CARD`` payload."""
	if not isinstance(card, dict):
		raise BoardContractError("Action card payload must be a dict.")

	title = card.get("title")
	if not isinstance(title, str) or not title.strip():
		raise BoardContractError("Action card requires a non-empty 'title' string.")

	details = card.get("details", "")
	if details is not None and not isinstance(details, str):
		raise BoardContractError("Action card 'details' must be a string.")

	category = card.get("category") or "General"
	if not isinstance(category, str) or category.strip() not in ACTION_CARD_CATEGORIES:
		raise BoardContractError(f"Invalid action card category: {category!r}.")
	clean_category = category.strip()

	priority = card.get("priority") or "Medium"
	if not isinstance(priority, str) or priority.strip() not in ACTION_CARD_PRIORITIES:
		raise BoardContractError(f"Invalid action card priority: {priority!r}.")
	clean_priority = priority.strip()

	target_role = card.get("target_role") or "MANAGER"
	if not isinstance(target_role, str) or target_role.strip() not in TARGET_ROLES:
		raise BoardContractError(f"Invalid action card target_role: {target_role!r}.")
	clean_target_role = target_role.strip()

	action_item = card.get("action_item")
	if action_item is not None:
		if not isinstance(action_item, str) or not action_item.strip():
			raise BoardContractError("Action card 'action_item' must be a non-empty string when provided.")
		action_item = action_item.strip()

	raw_suggested = card.get("suggested_action")
	clean_suggested: str | None = None
	if raw_suggested is not None:
		if not isinstance(raw_suggested, str):
			raise BoardContractError("Action card 'suggested_action' must be a string.")
		clean_suggested = raw_suggested.strip().upper() or None

	raw_ctype = card.get("clearing_condition_type")
	if raw_ctype is None or (isinstance(raw_ctype, str) and not raw_ctype.strip()):
		clean_ctype = SUGGESTED_ACTION_TO_CLEARING_CONDITION.get(clean_suggested or "", "manual")
	elif isinstance(raw_ctype, str) and raw_ctype.strip() in CLEARING_CONDITION_TYPES:
		clean_ctype = raw_ctype.strip()
	else:
		raise BoardContractError(f"Invalid action card clearing_condition_type: {raw_ctype!r}.")

	raw_part_names = card.get("part_names")
	clean_part_names: list[str] | None = None
	if raw_part_names is not None:
		if not isinstance(raw_part_names, list):
			raise BoardContractError("Action card 'part_names' must be a list of strings.")
		clean_part_names = [
			str(p).strip() for p in raw_part_names if isinstance(p, str) and p.strip()
		]

	result = dict(card)
	result["title"] = title.strip()
	result["details"] = details.strip() if isinstance(details, str) else ""
	result["category"] = clean_category
	result["priority"] = clean_priority
	result["target_role"] = clean_target_role
	result["clearing_condition_type"] = clean_ctype
	if action_item is not None:
		result["action_item"] = action_item
	if clean_suggested is not None:
		result["suggested_action"] = clean_suggested
	if clean_part_names is not None:
		result["part_names"] = clean_part_names
	if isinstance(card.get("clearing_condition_description"), str):
		result["clearing_condition_description"] = card["clearing_condition_description"].strip()
	return result


__all__ = [
	"ACTION_CARD_CATEGORIES",
	"ACTION_CARD_PRIORITIES",
	"ACTOR_TYPES",
	"AI_STATUSES",
	"BLOCKED_BOARD_STATUSES",
	"BOARD_STATUSES",
	"CAPTURE_LOCKED_RO_STATUSES",
	"CLOSED_RO_STATUSES",
	"IMMUTABLE_EVENT_FIELDS",
	"PAYLOAD_TYPES",
	"RESOLUTION_STATES",
	"SELF_ECHO_DEDUP_PREFIX",
	"SUGGESTED_ACTION_TO_CLEARING_CONDITION",
	"TARGET_ROLES",
	"THREAD_TYPES",
	"BoardContractError",
	"parse_ro_from_thread_key",
	"ro_thread_key",
	"validate_action_card",
]
