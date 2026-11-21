# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Whitelisted REST API surface for Square CAD payments, manual deposits, and webhooks."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from ism.payments.contracts import (
	STAFF_PAYMENT_ROLES,
	assert_no_raw_card_fields,
)
from ism.payments.deposit_requests import (
	cancel_deposit_request,
	create_deposit_request,
	get_ro_payment_state,
	list_ro_payment_transactions,
)
from ism.payments.erpnext_bridge import (
	execute_tokenized_payment,
	process_square_webhook_event,
)
from ism.payments.square_client import get_sdk_boot_config
from ism.payments.webhooks import (
	parse_square_webhook_event,
	verify_square_webhook_signature,
)

_PAYMENT_RATE_LIMIT_PREFIX = "ism:payment_portal:rl"
_PAYMENT_RATE_LIMIT_WINDOW_SEC = 60
_PAYMENT_RATE_LIMITS = {
	"summary": {"ip": 60, "ro": 30},
	"charge": {"ip": 20, "ro": 10},
}
_DISALLOWED_AMOUNT_KEYS = frozenset({"amount", "amount_cents"})


def _get_payment_client_ip(ip_override: str | None = None) -> str:
	"""Resolve client IP from request context (ignoring untrusted overrides when HTTP request is active)."""
	if getattr(frappe.local, "request", None):
		raw_ip = (
			getattr(frappe.local, "request_ip", None)
			or frappe.get_request_header("X-Forwarded-For")
			or getattr(frappe.local.request, "remote_addr", None)
		)
	else:
		raw_ip = (
			ip_override.strip()
			if isinstance(ip_override, str) and ip_override.strip()
			else getattr(frappe.local, "request_ip", None)
		)
	if isinstance(raw_ip, str) and raw_ip.strip():
		return raw_ip.split(",")[0].strip()
	return "unknown"


def _increment_rate_limit_key(
	cache_key: str,
	limit: int,
	window_sec: int = _PAYMENT_RATE_LIMIT_WINDOW_SEC,
) -> None:
	"""Increment a Redis-backed rate-limit counter and throw TooManyRequestsError when exceeded."""
	cache = frappe.cache()
	current = cint(cache.get_value(cache_key) or 0) + 1
	cache.set_value(cache_key, current, expires_in_sec=window_sec)
	if current > limit:
		frappe.throw(
			_("Too many payment requests. Please wait a moment and try again."),
			frappe.TooManyRequestsError,
		)


def _enforce_guest_payment_rate_limit(
	endpoint: str,
	repair_order: str | None = None,
	ip_override: str | None = None,
) -> str:
	"""Enforce per-IP and per-RO Redis rate limits on public guest payment endpoints."""
	limits = _PAYMENT_RATE_LIMITS[endpoint]
	client_ip = _get_payment_client_ip(ip_override=ip_override)
	_increment_rate_limit_key(
		f"{_PAYMENT_RATE_LIMIT_PREFIX}:{endpoint}:ip:{client_ip}",
		limits["ip"],
	)
	if isinstance(repair_order, str) and repair_order.strip():
		ro_hash = hashlib.sha256(repair_order.strip().encode("utf-8")).hexdigest()[:24]
		_increment_rate_limit_key(
			f"{_PAYMENT_RATE_LIMIT_PREFIX}:{endpoint}:ro:{ro_hash}",
			limits["ro"],
		)
	return client_ip


def clear_payment_rate_limits() -> None:
	"""Clear guest payment rate-limit keys from cache (for hermetic test runs)."""
	cache = frappe.cache()
	if hasattr(cache, "delete_keys"):
		cache.delete_keys(f"{_PAYMENT_RATE_LIMIT_PREFIX}:*")


def _require_staff_payment_role() -> str:
	"""Ensure caller is an authenticated staff user with an allowed payment role."""
	user = frappe.session.user or "Guest"
	if user == "Guest":
		frappe.throw(
			_("You do not have permission to access Repair Order payment records."),
			frappe.PermissionError,
		)
	user_roles = set(frappe.get_roles(user))
	if not STAFF_PAYMENT_ROLES.intersection(user_roles):
		frappe.throw(
			_("You do not have permission to access Repair Order payment records."),
			frappe.PermissionError,
		)
	return user


def _validate_guest_approval_token(repair_order: str, approval_token: str | None) -> None:
	"""Validate a guest approval token against a specific Repair Order using constant-time comparison.

	Raises frappe.PermissionError with a generic, zero-PII message on any missing, invalid,
	expired, or cross-RO mismatched token.
	"""
	generic_error = _("Invalid or expired payment authorization.")
	if not isinstance(repair_order, str) or not repair_order.strip():
		frappe.throw(generic_error, frappe.PermissionError)
	if not isinstance(approval_token, str) or not approval_token.strip():
		frappe.throw(generic_error, frappe.PermissionError)

	ro_row = frappe.db.get_value(
		"Repair Order",
		repair_order.strip(),
		["name", "approval_token", "approval_token_expiry"],
		as_dict=True,
	)
	if not ro_row:
		frappe.throw(generic_error, frappe.PermissionError)

	stored_token = ro_row.get("approval_token")
	if not isinstance(stored_token, str) or not stored_token.strip():
		frappe.throw(generic_error, frappe.PermissionError)

	if not hmac.compare_digest(stored_token.strip(), approval_token.strip()):
		frappe.throw(generic_error, frappe.PermissionError)

	expiry = ro_row.get("approval_token_expiry")
	if expiry and now_datetime() > get_datetime(expiry):
		frappe.throw(generic_error, frappe.PermissionError)


def _authenticate_summary_caller(repair_order: str, approval_token: str | None) -> str:
	"""Authenticate summary caller as either authorized staff (RO read) or valid guest token holder."""
	user = frappe.session.user or "Guest"
	if user == "Guest":
		_enforce_guest_payment_rate_limit("summary", repair_order=repair_order)
		_validate_guest_approval_token(repair_order, approval_token)
		return "Guest"

	if not isinstance(repair_order, str) or not repair_order.strip():
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)

	user_roles = set(frappe.get_roles(user))
	has_staff_role = bool(STAFF_PAYMENT_ROLES.intersection(user_roles))
	if not has_staff_role:
		ro_name = repair_order.strip()
		has_read = (
			frappe.has_permission("Repair Order", ptype="read", doc=ro_name, user=user)
			if frappe.db.exists("Repair Order", ro_name)
			else frappe.has_permission("Repair Order", ptype="read", user=user)
		)
		if not has_read:
			frappe.throw(
				_("You do not have permission to view this Repair Order's payment summary."),
				frappe.PermissionError,
			)
	return user


def _authenticate_charge_caller(repair_order: str, approval_token: str | None) -> tuple[str, str]:
	"""Authenticate charge caller as authorized staff (RO write / staff role) or valid guest token holder.

	Returns (initiated_by, client_ip).
	"""
	user = frappe.session.user or "Guest"
	if user == "Guest":
		client_ip = _enforce_guest_payment_rate_limit("charge", repair_order=repair_order)
		_validate_guest_approval_token(repair_order, approval_token)
		return ("Customer Portal", client_ip)

	if not isinstance(repair_order, str) or not repair_order.strip():
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)

	user_roles = set(frappe.get_roles(user))
	has_staff_role = bool(STAFF_PAYMENT_ROLES.intersection(user_roles))
	if not has_staff_role:
		ro_name = repair_order.strip()
		has_write = (
			frappe.has_permission("Repair Order", ptype="write", doc=ro_name, user=user)
			if frappe.db.exists("Repair Order", ro_name)
			else frappe.has_permission("Repair Order", ptype="write", user=user)
		)
		if not has_write:
			frappe.throw(
				_("You do not have permission to submit payments for this Repair Order."),
				frappe.PermissionError,
			)
	return (user, _get_payment_client_ip())


def _reject_pci_and_amount_overrides(kwargs: dict[str, Any]) -> None:
	"""Reject raw PCI card fields and client-supplied amount overrides in kwargs and form_dict."""
	assert_no_raw_card_fields(kwargs)

	form_dict = getattr(frappe.local, "form_dict", None)
	form_snapshot = dict(form_dict) if isinstance(form_dict, dict) else {}
	if form_snapshot:
		assert_no_raw_card_fields(form_snapshot)

	for source in (kwargs, form_snapshot):
		for key, value in source.items():
			if str(key).strip().lower() in _DISALLOWED_AMOUNT_KEYS and value is not None:
				frappe.throw(
					_("Client-supplied payment amount overrides are not accepted. Amount is computed server-side."),
					frappe.ValidationError,
				)


def _coerce_expected_amount_cents(value: Any) -> int | None:
	"""Safely coerce expected_amount_cents from int or numeric HTTP POST string; reject bool/invalid."""
	if value is None:
		return None
	if isinstance(value, bool):
		frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
	if isinstance(value, int):
		if value <= 0:
			frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
		return value
	if isinstance(value, str):
		cleaned = value.strip()
		if not cleaned:
			return None
		if not cleaned.isdigit():
			frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
		parsed = int(cleaned)
		if parsed <= 0:
			frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)
		return parsed
	frappe.throw(_("expected_amount_cents must be a positive integer."), frappe.ValidationError)


@frappe.whitelist()
def request_ro_deposit(
	repair_order: str,
	amount: float | int | str,
	note: str | None = None,
) -> dict[str, Any]:
	"""Staff-only endpoint to initiate a manual CAD deposit request on a Repair Order."""
	return dict(create_deposit_request(repair_order, amount, note=note))


@frappe.whitelist()
def cancel_ro_deposit_request(
	repair_order: str,
	reason: str | None = None,
) -> dict[str, Any]:
	"""Staff-only endpoint to cancel an unpaid manual deposit request on a Repair Order."""
	return dict(cancel_deposit_request(repair_order, reason=reason))


@frappe.whitelist()
def get_ro_payment_ledger(repair_order: str) -> dict[str, Any]:
	"""Staff-only endpoint returning canonical payment summary and transaction ledger for a Repair Order."""
	_require_staff_payment_role()
	return {
		"summary": dict(get_ro_payment_state(repair_order)),
		"transactions": list_ro_payment_transactions(repair_order),
	}


@frappe.whitelist(allow_guest=True)
def get_square_sdk_boot_config() -> dict[str, Any]:
	"""Public guest-safe endpoint returning strictly non-secret CAD Square Web Payments SDK boot config."""
	return dict(get_sdk_boot_config())


@frappe.whitelist(allow_guest=True)
def get_ro_payment_summary(
	repair_order: str,
	approval_token: str | None = None,
) -> dict[str, Any]:
	"""Return PaymentStateSummary after authenticating staff RO read permission or valid guest approval token."""
	_authenticate_summary_caller(repair_order, approval_token)
	return dict(get_ro_payment_state(repair_order.strip()))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit_square_payment(
	repair_order: str,
	payment_intent: str,
	source_id: str,
	idempotency_key: str | None = None,
	expected_amount_cents: int | None = None,
	approval_token: str | None = None,
	buyer_email: str | None = None,
	**kwargs,
) -> dict[str, Any]:
	"""Public/staff tokenized CAD charge endpoint with PCI scrub, rate limits, and auth enforcement."""
	_reject_pci_and_amount_overrides(kwargs)
	initiated_by, client_ip = _authenticate_charge_caller(repair_order, approval_token)
	coerced_expected_cents = _coerce_expected_amount_cents(expected_amount_cents)

	result = execute_tokenized_payment(
		repair_order=repair_order.strip(),
		payment_intent=payment_intent,
		source_id=source_id,
		idempotency_key=idempotency_key,
		expected_amount_cents=coerced_expected_cents,
		buyer_email=buyer_email,
		initiated_by=initiated_by,
		client_ip=client_ip,
	)
	return dict(result)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def square_webhook() -> dict[str, Any]:
	"""Square webhook receiver verifying raw-body HMAC-SHA256 signature and dispatching normalized events."""
	if not hasattr(frappe.local, "response") or frappe.local.response is None:
		frappe.local.response = frappe._dict()

	req = getattr(frappe.local, "request", None)
	raw_body = req.get_data(as_text=False) if req is not None else b""
	if not isinstance(raw_body, bytes | bytearray):
		raw_body_bytes = b""
	else:
		raw_body_bytes = bytes(raw_body)

	signature_header = frappe.get_request_header("x-square-hmacsha256-signature")
	if not signature_header and req is not None and getattr(req, "headers", None):
		signature_header = req.headers.get("x-square-hmacsha256-signature")

	if not verify_square_webhook_signature(raw_body_bytes, signature_header):
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorized"}

	try:
		event = parse_square_webhook_event(raw_body_bytes)
	except ValueError:
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid_payload"}

	result = process_square_webhook_event(event)
	frappe.local.response["http_status_code"] = 200
	return result
