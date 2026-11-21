# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frozen contracts, enums, and card payload validators for Shop OS Blackboard."""

from __future__ import annotations

from typing import Any

import frappe

THREAD_TYPES: tuple[str, ...] = (
	"REPAIR_ORDER",
	"INTAKE",
	"AP_RECEIVING",
	"DISPATCH",
)

SINGLETON_THREADS: dict[str, tuple[str, str]] = {
	"SHOP:INTAKE": ("INTAKE", "Front Desk Intake & Booking"),
	"SHOP:AP_RECEIVING": ("AP_RECEIVING", "Dock Receiving & Vendor Bills"),
	"SHOP:DISPATCH": ("DISPATCH", "Shop Dispatch & Floor Alerts"),
}

BOARD_STATUSES: tuple[str, ...] = (
	"ACTIVE",
	"BLOCKED_PARTS",
	"BLOCKED_DECISION",
	"WAITING_CUSTOMER",
	"CLOSED",
)

BLOCKED_BOARD_STATUSES: tuple[str, ...] = (
	"BLOCKED_PARTS",
	"BLOCKED_DECISION",
	"WAITING_CUSTOMER",
)

CLOSED_RO_STATUSES: tuple[str, ...] = (
	"Closed / Released",
	"Cancelled",
	"Billed / Paid",
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
	"AI_RECEIPT",
	"CLARIFICATION_CARD",
	"APPROVAL_GATE",
	"STATE_MUTATION",
	"CARD_RESOLUTION",
)

PENDING_DECISION_PAYLOAD_TYPES: tuple[str, ...] = (
	"CLARIFICATION_CARD",
	"APPROVAL_GATE",
)

RESOLUTION_STATES: tuple[str, ...] = (
	"NONE",
	"PENDING",
	"EXECUTING",
	"RESOLVED",
	"REJECTED",
	"UNDONE",
	"EXPIRED",
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

AUTONOMOUS_ACTIONS: tuple[str, ...] = (
	"LOG_RO_NOTE",
	"ATTACH_INSPECTION_MEDIA",
	"ADD_SUPPLEMENT_DRAFT",
	"SET_PARTS_HOLD",
	"CLEAR_PARTS_HOLD",
	"DRAFT_PURCHASE_ORDER",
	"UPDATE_JOB_STATUS",
	"TOGGLE_SERVICE_COMPLETION",
	"ADD_DEFERRED_SERVICES",
)

MANAGER_GATED_ACTIONS: tuple[str, ...] = (
	"SUBMIT_PO",
	"AMEND_QUOTE",
	"SEND_QUOTE_APPROVAL",
	"DISPATCH_CUSTOMER_SMS",
	"DISPATCH_CUSTOMER_EMAIL",
	"RECORD_DEPOSIT",
	"CHECKOUT_REPAIR_ORDER",
	"RESCHEDULE_APPOINTMENT",
	"INTAKE_QUOTE_AND_BOOK",
	"CONVERT_APPOINTMENT_TO_RO",
	"RECEIVE_PO",
	"POST_PURCHASE_INVOICE",
	"PAY_PURCHASE",
)

ALL_ACTIONS: tuple[str, ...] = AUTONOMOUS_ACTIONS + MANAGER_GATED_ACTIONS

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
	"""Raised when a board identifier or card payload violates frozen contracts."""


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


def _normalize_options(options: Any) -> list[dict[str, Any]]:
	if not isinstance(options, list):
		raise BoardContractError("Card 'options' must be a list.")
	normalized: list[dict[str, Any]] = []
	for idx, item in enumerate(options):
		if isinstance(item, str):
			val = item.strip()
			if not val:
				raise BoardContractError(f"Card option at index {idx} cannot be empty.")
			normalized.append({"label": val, "value": val})
		elif isinstance(item, dict):
			label = item.get("label")
			value = item.get("value", label)
			if not isinstance(label, str) or not label.strip():
				raise BoardContractError(f"Card option at index {idx} requires a non-empty 'label'.")
			if value is None or (isinstance(value, str) and not value.strip()):
				raise BoardContractError(f"Card option at index {idx} requires a non-empty 'value'.")
			entry = dict(item)
			entry["label"] = label.strip()
			entry["value"] = value.strip() if isinstance(value, str) else value
			normalized.append(entry)
		else:
			raise BoardContractError(f"Card option at index {idx} must be a string or dict.")
	return normalized


def validate_clarification_card(card: dict[str, Any]) -> dict[str, Any]:
	"""Validate and normalize a ``CLARIFICATION_CARD`` payload."""
	if not isinstance(card, dict):
		raise BoardContractError("Clarification card payload must be a dict.")

	prompt = card.get("prompt") or card.get("question")
	if not isinstance(prompt, str) or not prompt.strip():
		raise BoardContractError("Clarification card requires a non-empty 'prompt' string.")

	raw_options = card.get("options")
	input_field = card.get("input_field")
	if raw_options is None and not (isinstance(input_field, str) and input_field.strip()):
		raise BoardContractError(
			"Clarification card requires either non-empty 'options' or an 'input_field'."
		)

	options: list[dict[str, Any]] = []
	if raw_options is not None:
		options = _normalize_options(raw_options)
		if not options and not (isinstance(input_field, str) and input_field.strip()):
			raise BoardContractError(
				"Clarification card 'options' cannot be empty unless 'input_field' is provided."
			)

	action_type = card.get("action_type")
	if action_type is not None and action_type not in ALL_ACTIONS:
		raise BoardContractError(f"Unknown clarification action_type: {action_type!r}.")

	target_role = card.get("target_role", "TECH")
	if target_role not in TARGET_ROLES:
		raise BoardContractError(f"Invalid target_role for clarification card: {target_role!r}.")

	result = dict(card)
	result["prompt"] = prompt.strip()
	result["options"] = options
	result["target_role"] = target_role
	if isinstance(input_field, str) and input_field.strip():
		result["input_field"] = input_field.strip()
	return result


def validate_approval_gate_card(card: dict[str, Any]) -> dict[str, Any]:
	"""Validate and normalize an ``APPROVAL_GATE`` payload."""
	if not isinstance(card, dict):
		raise BoardContractError("Approval gate card payload must be a dict.")

	action_type = card.get("action_type")
	if action_type not in MANAGER_GATED_ACTIONS:
		raise BoardContractError(
			f"Approval gate requires action_type in MANAGER_GATED_ACTIONS, got {action_type!r}."
		)

	title = card.get("title") or card.get("summary")
	if not isinstance(title, str) or not title.strip():
		raise BoardContractError("Approval gate card requires a non-empty 'title' or 'summary'.")

	target_role = card.get("target_role", "MANAGER")
	if target_role != "MANAGER":
		raise BoardContractError("Approval gate cards must target 'MANAGER'.")

	params = card.get("params", {})
	if params is not None and not isinstance(params, dict):
		raise BoardContractError("Approval gate 'params' must be a dict.")

	result = dict(card)
	result["action_type"] = action_type
	result["title"] = title.strip()
	result["target_role"] = "MANAGER"
	result["params"] = dict(params or {})

	if "options" in card and card["options"] is not None:
		result["options"] = _normalize_options(card["options"])

	return result


def validate_receipt_card(card: dict[str, Any]) -> dict[str, Any]:
	"""Validate and normalize an ``AI_RECEIPT`` payload."""
	if not isinstance(card, dict):
		raise BoardContractError("Receipt card payload must be a dict.")

	action_type = card.get("action_type")
	if action_type not in ALL_ACTIONS:
		raise BoardContractError(f"Receipt card requires valid action_type, got {action_type!r}.")

	summary = card.get("summary") or card.get("title")
	if not isinstance(summary, str) or not summary.strip():
		raise BoardContractError("Receipt card requires a non-empty 'summary' or 'title'.")

	undo_spec = card.get("undo_spec")
	if undo_spec is not None and not isinstance(undo_spec, dict):
		raise BoardContractError("Receipt card 'undo_spec' must be a dict or None.")

	result = dict(card)
	result["action_type"] = action_type
	result["summary"] = summary.strip()
	result["undo_spec"] = dict(undo_spec) if undo_spec is not None else None
	result["reversible"] = bool(card.get("reversible", result["undo_spec"] is not None))
	return result


__all__ = [
	"ACTOR_TYPES",
	"AI_STATUSES",
	"ALL_ACTIONS",
	"AUTONOMOUS_ACTIONS",
	"BLOCKED_BOARD_STATUSES",
	"BOARD_STATUSES",
	"CLOSED_RO_STATUSES",
	"IMMUTABLE_EVENT_FIELDS",
	"MANAGER_GATED_ACTIONS",
	"PAYLOAD_TYPES",
	"PENDING_DECISION_PAYLOAD_TYPES",
	"RESOLUTION_STATES",
	"SELF_ECHO_DEDUP_PREFIX",
	"SINGLETON_THREADS",
	"TARGET_ROLES",
	"THREAD_TYPES",
	"BoardContractError",
	"parse_ro_from_thread_key",
	"ro_thread_key",
	"validate_approval_gate_card",
	"validate_clarification_card",
	"validate_receipt_card",
]
