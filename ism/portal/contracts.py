# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Frozen constants, status enums, and pure validators for Project 4 Customer Portal."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

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

PHONE_KEY_STATUS_NOT_PROVIDED: str = "Not Provided"
PHONE_KEY_STATUS_PROVIDED: str = "Provided"
PHONE_KEY_STATUS_RELEASED: str = "Released"
PHONE_KEY_STATUS_PURGED: str = "Purged"
PHONE_KEY_VALID_STATUSES: tuple[str, ...] = (
	PHONE_KEY_STATUS_NOT_PROVIDED,
	PHONE_KEY_STATUS_PROVIDED,
	PHONE_KEY_STATUS_RELEASED,
	PHONE_KEY_STATUS_PURGED,
)
PHONE_KEY_STAFF_ALLOWED_ROLES: frozenset[str] = frozenset({
	"ISM Service Advisor",
	"ISM Manager",
	"System Manager",
})
PHONE_KEY_AUTO_MASK_SECONDS: int = 60

RO_CLOSED_LOCKED_STATUSES: frozenset[str] = frozenset({"Closed / Released", "Cancelled"})
RO_PIN_PURGE_STATUSES: frozenset[str] = frozenset({"Closed / Released", "Cancelled"})
RO_PIN_MUTATION_LOCKED_STATUSES: frozenset[str] = frozenset({
	"Work Completed",
	"Billed / Paid",
	"Closed / Released",
	"Cancelled",
})
RO_PHONE_KEY_PURGE_STATUSES: frozenset[str] = RO_PIN_PURGE_STATUSES
RO_PHONE_KEY_MUTATION_LOCKED_STATUSES: frozenset[str] = RO_PIN_MUTATION_LOCKED_STATUSES
SE_PIN_PROMPT_STATUSES: frozenset[str] = frozenset({"Scheduled", "Confirmed"})
SE_PIN_PURGE_STATUSES: frozenset[str] = frozenset({"Completed", "Cancelled", "No Show"})
SE_PIN_MUTATION_LOCKED_STATUSES: frozenset[str] = frozenset({"Completed", "Cancelled", "No Show"})
SE_PHONE_KEY_PURGE_STATUSES: frozenset[str] = SE_PIN_PURGE_STATUSES
SE_PHONE_KEY_MUTATION_LOCKED_STATUSES: frozenset[str] = SE_PIN_MUTATION_LOCKED_STATUSES

PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS: int = 1
PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS: int = 180
PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS: int = 30
PORTAL_DEFAULT_INVOICE_PRINT_FORMAT: str = "Standard"
DEFAULT_PIN_PROMPT_DISCLAIMER: str = (
	"Your 4-digit PIN-to-Drive is encrypted at rest, accessible only to authorized shop technicians "
	"while your vehicle is in service, and permanently purged as soon as your vehicle is released."
)
DEFAULT_PHONE_KEY_DISCLAIMER: str = (
	"Sharing a Tesla Phone Key invitation link is completely optional. Your link is encrypted at rest, "
	"accessible only to authorized service advisors while your vehicle is in our care, and permanently "
	"deleted—with your phone key removed from our shop device—as soon as your vehicle is released."
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
	"tesla_phone_key_encrypted",
	"tesla_phone_key_url",
	"tesla_phone_key_invite_url",
	"phone_key_invite_url",
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
_HTTPS_URL_EXTRACT_RE = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)
_TESLA_WMI_PREFIXES: frozenset[str] = frozenset({"5YJ", "7SA", "7G2", "LRW", "XP7", "SFZ"})
_TESLA_MODEL_TOKENS: tuple[str, ...] = (
	"model s",
	"model 3",
	"model x",
	"model y",
	"cybertruck",
	"roadster",
	"tesla",
)
_PHONE_KEY_MAX_URL_LENGTH: int = 2048


def _is_allowed_tesla_invite_host(hostname: str | None) -> bool:
	if not isinstance(hostname, str):
		return False
	host = hostname.strip().lower().rstrip(".")
	if not host:
		return False
	if host in ("ts.la", "www.ts.la", "tesla.com"):
		return True
	return host.endswith(".tesla.com") and len(host) > len(".tesla.com")


def validate_tesla_phone_key_url(raw_input: Any) -> str:
	"""Extract and strictly validate a canonical HTTPS Tesla Phone Key invitation link.

	Allows pasting either a bare link or full share-sheet text containing an ``https://ts.la/...``
	or ``https://*.tesla.com/...`` URL. Rejects non-HTTPS schemes, embedded credentials, control
	characters, empty paths, and non-Tesla hostnames.
	"""
	if not isinstance(raw_input, str) or not raw_input.strip():
		frappe.throw(
			_("Please paste a valid Tesla Phone Key invitation link (https://ts.la/... or https://tesla.com/...)."),
			frappe.ValidationError,
		)

	text = raw_input.strip()
	if any(ord(ch) < 32 and ch not in ("\n", "\r", "\t") for ch in text):
		frappe.throw(_("Invalid characters in Tesla invitation link."), frappe.ValidationError)

	matches = _HTTPS_URL_EXTRACT_RE.findall(text)
	if not matches:
		frappe.throw(
			_("Tesla invitation link must start with https:// (for example, https://ts.la/...)."),
			frappe.ValidationError,
		)

	candidate = matches[0].rstrip(".,;:!?)")
	if len(candidate) > _PHONE_KEY_MAX_URL_LENGTH:
		frappe.throw(_("Tesla invitation link is too long."), frappe.ValidationError)
	if any(ch.isspace() or ord(ch) < 32 for ch in candidate):
		frappe.throw(_("Tesla invitation link contains invalid whitespace or control characters."), frappe.ValidationError)

	parsed = urlparse(candidate)
	if parsed.scheme.lower() != "https":
		frappe.throw(_("Tesla invitation link must use HTTPS."), frappe.ValidationError)
	if parsed.username or parsed.password:
		frappe.throw(_("Tesla invitation link must not contain embedded credentials."), frappe.ValidationError)
	if parsed.port not in (None, 443):
		frappe.throw(_("Tesla invitation link must use standard HTTPS port."), frappe.ValidationError)
	if not _is_allowed_tesla_invite_host(parsed.hostname):
		frappe.throw(
			_("Please enter an official Tesla invitation link from ts.la or tesla.com."),
			frappe.ValidationError,
		)

	path = (parsed.path or "").strip()
	if not path or path == "/":
		frappe.throw(
			_("Please paste the full Tesla driver invitation link, not just the homepage URL."),
			frappe.ValidationError,
		)

	return candidate


def is_tesla_vehicle_info(
	*,
	manufacturer: Any = None,
	model: Any = None,
	vin: Any = None,
	provisional_info: Any = None,
) -> bool:
	"""Return True iff the vehicle metadata identifies a Tesla vehicle."""
	if isinstance(manufacturer, str) and "tesla" in manufacturer.strip().lower():
		return True

	if isinstance(vin, str):
		clean_vin = vin.strip().upper()
		if len(clean_vin) >= 3 and clean_vin[:3] in _TESLA_WMI_PREFIXES:
			return True

	for text in (model, provisional_info):
		if isinstance(text, str) and text.strip():
			lowered = text.strip().lower()
			if any(token in lowered for token in _TESLA_MODEL_TOKENS):
				return True

	return False


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
