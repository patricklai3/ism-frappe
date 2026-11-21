# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frozen constants, status enums, and pure validators for Project 4 Customer Portal."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

PORTAL_STATUS_SCHEDULED: str = "scheduled"
PORTAL_STATUS_BEING_WORKED_ON: str = "being_worked_on"
PORTAL_STATUS_READY_FOR_PICKUP: str = "ready_for_pickup"
PORTAL_STATUS_CANCELLED: str = "cancelled"

PORTAL_STATUS_LABELS: dict[str, str] = {
	PORTAL_STATUS_SCHEDULED: "Scheduled",
	PORTAL_STATUS_BEING_WORKED_ON: "Being Worked On",
	PORTAL_STATUS_READY_FOR_PICKUP: "Done & Ready for Pickup",
	PORTAL_STATUS_CANCELLED: "Cancelled",
}

PIN_STATUS_NOT_PROMPTED: str = "Not Prompted"
PIN_STATUS_PROVIDED: str = "Provided"
PIN_STATUS_OPTED_OUT: str = "Opted Out"
PIN_STATUS_PURGED: str = "Purged"
PIN_VALID_STATUSES: tuple[str, ...] = (
	PIN_STATUS_NOT_PROMPTED,
	PIN_STATUS_PROVIDED,
	PIN_STATUS_OPTED_OUT,
	PIN_STATUS_PURGED,
)

PIN_STAFF_ALLOWED_ROLES: frozenset[str] = frozenset({
	"ISM Technician",
	"ISM Service Advisor",
	"ISM Manager",
	"System Manager",
})
PIN_AUTO_MASK_SECONDS: int = 15

RO_CLOSED_LOCKED_STATUSES: frozenset[str] = frozenset({"Closed / Released", "Cancelled"})
RO_PIN_PURGE_STATUSES: frozenset[str] = frozenset({"Closed / Released", "Cancelled"})
RO_PIN_MUTATION_LOCKED_STATUSES: frozenset[str] = frozenset({
	"Work Completed",
	"Billed / Paid",
	"Closed / Released",
	"Cancelled",
})

PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS: int = 1
PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS: int = 180
PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS: int = 30
PORTAL_DEFAULT_INVOICE_PRINT_FORMAT: str = "Standard"
DEFAULT_PIN_PROMPT_DISCLAIMER: str = (
	"Your 4-digit PIN-to-Drive is encrypted at rest, accessible only to authorized shop technicians "
	"while your vehicle is in service, and permanently purged as soon as your vehicle is released."
)

PORTAL_FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset({
	"eta",
	"estimated_completion",
	"completion_date",
	"completion_time",
	"expected_completion",
	"countdown",
	"remaining_minutes",
	"progress_percent",
	"is_completed",
	"completed_at",
	"completed_by",
	"total_bay_time_hours",
	"bay_time_logs",
	"service_bay",
	"assigned_technician",
	"picked_up_by",
	"parked_by",
	"internal_start",
	"internal_start_time",
	"arrival_offset_minutes",
	"offset_minutes",
	"buffer_minutes",
	"leeway",
	"signature_image",
	"pin_to_drive_encrypted",
	"pin_to_drive_plain",
	"approval_token",
	"portal_booking_token_hash",
	"cookie_value",
	"correction_code",
	"hourly_rate",
	"tesla_manual_url",
	"valuation_rate",
	"cost_price",
})

_PIN_FORMAT_RE = re.compile(r"^[0-9]{4}$")


def validate_pin_format(raw_pin: Any) -> str:
	"""Validate a 4-digit numeric PIN-to-Drive string, preserving leading zeros."""
	if not isinstance(raw_pin, str):
		frappe.throw(_("PIN-to-Drive must be a 4-digit numeric string."), frappe.ValidationError)

	cleaned = raw_pin.strip()
	if not _PIN_FORMAT_RE.match(cleaned):
		frappe.throw(_("PIN-to-Drive must be exactly 4 numeric digits."), frappe.ValidationError)

	return cleaned


def mask_email_address(email: str | None) -> str | None:
	"""Mask an email address (e.g. 'driver@example.com' -> 'd***r@example.com') for UI confirmation."""
	if not isinstance(email, str):
		return None

	cleaned = email.strip()
	if not cleaned or "@" not in cleaned:
		return None

	local, domain = cleaned.split("@", 1)
	if not local or not domain:
		return None

	if len(local) <= 2:
		masked_local = f"{local[0]}***"
	else:
		masked_local = f"{local[0]}***{local[-1]}"

	return f"{masked_local}@{domain}"


def hash_portal_token(raw_token: str) -> str:
	"""Return canonical SHA-256 hex digest for a portal direct-access token."""
	if not isinstance(raw_token, str) or not raw_token.strip():
		frappe.throw(_("Portal token must be a non-empty string."), frappe.ValidationError)

	return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def find_forbidden_portal_keys(payload: Any, *, prefix: str = "") -> list[str]:
	"""Recursively locate any key in PORTAL_FORBIDDEN_PAYLOAD_KEYS inside a payload."""
	found: list[str] = []

	if isinstance(payload, Mapping):
		for key, value in payload.items():
			key_str = str(key)
			path = f"{prefix}.{key_str}" if prefix else key_str
			if isinstance(key, str) and key in PORTAL_FORBIDDEN_PAYLOAD_KEYS:
				found.append(path)
			found.extend(find_forbidden_portal_keys(value, prefix=path))
	elif isinstance(payload, list | tuple):
		for idx, item in enumerate(payload):
			item_prefix = f"{prefix}[{idx}]"
			found.extend(find_forbidden_portal_keys(item, prefix=item_prefix))

	return found
