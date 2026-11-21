# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Encrypted PIN-to-Drive vault, Seam 6 staff reveal, and auto-purge lifecycle.

Stores 4-digit vehicle PINs inside an RO-bound authenticated JSON envelope
encrypted with ``cryptography.fernet.Fernet`` (using ``ISM Settings.portal_pin_encryption_key``)
and persisted strictly in Frappe's ``__Auth`` table via ``set_encrypted_password``.
PINs are write-only to customers, RBAC-gated and audited for shop staff, and
automatically purged when a Repair Order reaches ``Closed / Released`` or ``Cancelled``.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from cryptography.fernet import Fernet, InvalidToken
from frappe import _
from frappe.utils import cint, get_fullname, now_datetime
from frappe.utils.password import (
	get_decrypted_password,
	remove_encrypted_password,
	set_encrypted_password,
)

from ism.portal.contracts import (
	DEFAULT_PIN_PROMPT_DISCLAIMER,
	PIN_AUTO_MASK_SECONDS,
	PIN_STAFF_ALLOWED_ROLES,
	PIN_STATUS_NOT_PROMPTED,
	PIN_STATUS_OPTED_OUT,
	PIN_STATUS_PROVIDED,
	PIN_STATUS_PURGED,
	RO_PIN_MUTATION_LOCKED_STATUSES,
	RO_PIN_PURGE_STATUSES,
	validate_pin_format,
)
from ism.portal.events import (
	emit_portal_pin_purged_event,
	emit_portal_pin_submitted_event,
)

__all__ = [
	"get_ro_pin_customer_state",
	"get_ro_pin_for_staff",
	"get_ro_pin_state",
	"on_repair_order_update_purge_pin",
	"opt_out_ro_pin",
	"purge_ro_pin",
	"purge_stale_closed_ro_pins",
	"reveal_ro_pin_desk",
	"submit_ro_pin",
]

_PIN_FIELDNAME = "pin_to_drive_encrypted"
_PASSWORD_DUMMY_MASK = "********"
_RO_PIN_SELECT_FIELDS = (
	"name",
	"status",
	"approval_status",
	"vehicle_location",
	"picked_up_at",
	"vehicle_released",
	"pin_to_drive_status",
	"pin_to_drive_submitted_at",
	"pin_to_drive_updated_at",
	"pin_to_drive_opted_out_at",
	"pin_to_drive_purged_at",
	"pin_to_drive_last_viewed_by",
	"pin_to_drive_last_viewed_at",
	"pin_to_drive_view_count",
)
_APPROVED_RO_APPROVAL_STATUSES = frozenset({"Approved", "Partially Approved"})


def _get_pin_fernet() -> Fernet:
	"""Read ISM Settings.portal_pin_encryption_key; lazily seed if absent in fresh test contexts."""
	key = get_decrypted_password(
		"ISM Settings",
		"ISM Settings",
		fieldname="portal_pin_encryption_key",
		raise_exception=False,
	)
	if isinstance(key, bytes):
		key = key.decode("ascii")
	if isinstance(key, str):
		key = key.strip()

	if not key:
		key = Fernet.generate_key().decode("ascii")
		set_encrypted_password(
			"ISM Settings",
			"ISM Settings",
			key,
			fieldname="portal_pin_encryption_key",
		)
		frappe.db.set_single_value(
			"ISM Settings",
			"portal_pin_encryption_key",
			_PASSWORD_DUMMY_MASK,
			update_modified=False,
		)
		frappe.clear_document_cache("ISM Settings", "ISM Settings")

	try:
		return Fernet(key.encode("ascii"))
	except Exception:
		frappe.throw(
			_("Invalid PIN-to-Drive encryption key configured in ISM Settings."),
			frappe.ValidationError,
		)


def _normalize_ro_name(repair_order: Any) -> str:
	if not isinstance(repair_order, str) or not repair_order.strip():
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)
	return repair_order.strip()


def _lock_repair_order_row(repair_order: str) -> frappe._dict:
	"""Acquire a row-level FOR UPDATE lock on Repair Order metadata (never selects password column)."""
	ro_name = _normalize_ro_name(repair_order)
	columns = ", ".join(_RO_PIN_SELECT_FIELDS)
	rows = frappe.db.sql(
		f"SELECT {columns} FROM `tabRepair Order` WHERE name = %s FOR UPDATE",
		(ro_name,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(
			_("Repair Order {0} not found.").format(ro_name),
			frappe.DoesNotExistError,
		)
	return frappe._dict(rows[0])


def _get_repair_order_row(repair_order: str) -> frappe._dict:
	"""Read non-sensitive Repair Order PIN metadata without locking or touching __Auth."""
	ro_name = _normalize_ro_name(repair_order)
	row = frappe.db.get_value(
		"Repair Order",
		ro_name,
		list(_RO_PIN_SELECT_FIELDS),
		as_dict=True,
	)
	if not row:
		frappe.throw(
			_("Repair Order {0} not found.").format(ro_name),
			frappe.DoesNotExistError,
		)
	return frappe._dict(row)


def _has_staff_pin_access(repair_order: str | None = None) -> bool:
	"""Return True iff current user is authenticated staff with allowed role and RO read permission."""
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		return False

	if user != "Administrator":
		user_roles = set(frappe.get_roles(user))
		if not (user_roles & PIN_STAFF_ALLOWED_ROLES):
			return False

	if repair_order:
		return bool(frappe.has_permission("Repair Order", ptype="read", doc=repair_order, user=user))
	return bool(frappe.has_permission("Repair Order", ptype="read", user=user))


def _insert_ro_audit_comment(ro_name: str, text: str) -> None:
	"""Append a non-sensitive audit Comment to a Repair Order."""
	user = getattr(frappe.session, "user", None) or "Administrator"
	comment = frappe.new_doc("Comment")
	comment.comment_type = "Comment"
	comment.reference_doctype = "Repair Order"
	comment.reference_name = ro_name
	comment.comment_email = user
	comment.comment_by = get_fullname(user) or user
	comment.content = text
	comment.insert(ignore_permissions=True)


def _format_dt(value: Any) -> str | None:
	return str(value) if value else None


def _get_pin_prompt_disclaimer() -> str:
	configured = frappe.db.get_single_value("ISM Settings", "portal_pin_prompt_disclaimer")
	if isinstance(configured, str) and configured.strip():
		return configured.strip()
	return DEFAULT_PIN_PROMPT_DISCLAIMER


def _assert_customer_pin_mutable(ro: frappe._dict) -> None:
	"""Enforce active pre-work lifecycle rules for customer PIN submission and updates."""
	ro_status = ro.get("status") or ""
	if ro_status in RO_PIN_MUTATION_LOCKED_STATUSES:
		frappe.throw(
			_("PIN-to-Drive cannot be changed because this Repair Order is {0}.").format(ro_status),
			frappe.ValidationError,
		)

	prev_status = ro.get("pin_to_drive_status") or PIN_STATUS_NOT_PROMPTED
	if prev_status == PIN_STATUS_PURGED:
		frappe.throw(
			_("PIN-to-Drive has already been purged for this Repair Order."),
			frappe.ValidationError,
		)

	if prev_status in (PIN_STATUS_PROVIDED, PIN_STATUS_OPTED_OUT):
		if ro.get("picked_up_at") or ro.get("vehicle_location") == "In Bay / Being Serviced":
			frappe.throw(
				_("PIN-to-Drive cannot be updated after a technician has picked up the vehicle."),
				frappe.ValidationError,
			)


def get_ro_pin_state(repair_order: str) -> dict[str, Any]:
	"""FROZEN EXPORT FOR DESK & PROJECT 6 (/board Seam 6).

	Returns strictly non-sensitive metadata (never reads or decrypts ``__Auth``).
	"""
	row = _get_repair_order_row(repair_order)
	pin_status = row.get("pin_to_drive_status") or PIN_STATUS_NOT_PROMPTED
	has_pin = (pin_status == PIN_STATUS_PROVIDED) and (row.get("status") not in RO_PIN_PURGE_STATUSES)
	can_reveal = bool(has_pin and _has_staff_pin_access(row.name))

	return {
		"repair_order": row.name,
		"pin_feature_installed": True,
		"status": pin_status,
		"has_pin": has_pin,
		"can_reveal": can_reveal,
		"submitted_at": _format_dt(row.get("pin_to_drive_submitted_at")),
		"updated_at": _format_dt(row.get("pin_to_drive_updated_at")),
		"opted_out_at": _format_dt(row.get("pin_to_drive_opted_out_at")),
		"purged_at": _format_dt(row.get("pin_to_drive_purged_at")),
		"last_viewed_by": row.get("pin_to_drive_last_viewed_by") or None,
		"last_viewed_at": _format_dt(row.get("pin_to_drive_last_viewed_at")),
		"view_count": cint(row.get("pin_to_drive_view_count") or 0),
	}


def get_ro_pin_customer_state(ro_doc: Any) -> dict[str, Any]:
	"""Customer-safe projection for /portal visit hub (NEVER returns plaintext PIN)."""
	if isinstance(ro_doc, str):
		ro = _get_repair_order_row(ro_doc)
	elif isinstance(ro_doc, dict):
		ro = frappe._dict(ro_doc)
	else:
		ro = frappe._dict({field: getattr(ro_doc, field, None) for field in _RO_PIN_SELECT_FIELDS})

	pin_status = ro.get("pin_to_drive_status") or PIN_STATUS_NOT_PROMPTED
	ro_status = ro.get("status") or ""
	approval_status = ro.get("approval_status") or ""

	is_locked_status = ro_status in RO_PIN_MUTATION_LOCKED_STATUSES
	is_in_bay = bool(ro.get("picked_up_at")) or (ro.get("vehicle_location") == "In Bay / Being Serviced")

	prompt_required = (
		approval_status in _APPROVED_RO_APPROVAL_STATUSES
		and pin_status == PIN_STATUS_NOT_PROMPTED
		and not is_locked_status
	)
	can_submit = (pin_status == PIN_STATUS_NOT_PROMPTED) and not is_locked_status
	can_update = (
		pin_status != PIN_STATUS_PURGED
		and not is_in_bay
		and not is_locked_status
	)

	return {
		"status": pin_status,
		"masked_display": "••••" if pin_status == PIN_STATUS_PROVIDED else None,
		"prompt_required": prompt_required,
		"can_submit": can_submit,
		"can_update": can_update,
		"submitted_at": _format_dt(ro.get("pin_to_drive_submitted_at")),
		"updated_at": _format_dt(ro.get("pin_to_drive_updated_at")),
		"opted_out_at": _format_dt(ro.get("pin_to_drive_opted_out_at")),
		"purged_at": _format_dt(ro.get("pin_to_drive_purged_at")),
		"disclaimer": _get_pin_prompt_disclaimer(),
	}


def submit_ro_pin(
	repair_order: str,
	raw_pin: str,
	*,
	actor_label: str = "Customer Portal",
) -> dict[str, Any]:
	"""Validate, encrypt into an RO-bound Fernet envelope, and store a 4-digit PIN in ``__Auth``."""
	_ = actor_label
	pin = validate_pin_format(raw_pin)
	ro = _lock_repair_order_row(repair_order)
	_assert_customer_pin_mutable(ro)

	fernet = _get_pin_fernet()
	envelope = json.dumps(
		{"v": 1, "ro": ro.name, "pin": pin},
		separators=(",", ":"),
		sort_keys=True,
	).encode("utf-8")
	ciphertext = fernet.encrypt(envelope).decode("ascii")

	set_encrypted_password(
		"Repair Order",
		ro.name,
		ciphertext,
		fieldname=_PIN_FIELDNAME,
	)

	now_ts = now_datetime()
	submitted_at = ro.get("pin_to_drive_submitted_at") or now_ts
	frappe.db.set_value(
		"Repair Order",
		ro.name,
		{
			"pin_to_drive_status": PIN_STATUS_PROVIDED,
			"pin_to_drive_encrypted": _PASSWORD_DUMMY_MASK,
			"pin_to_drive_submitted_at": submitted_at,
			"pin_to_drive_updated_at": now_ts,
			"pin_to_drive_opted_out_at": None,
			"pin_to_drive_purged_at": None,
		},
		update_modified=True,
	)

	emit_portal_pin_submitted_event(ro.name, PIN_STATUS_PROVIDED)
	return get_ro_pin_customer_state(ro.name)


def opt_out_ro_pin(
	repair_order: str,
	*,
	actor_label: str = "Customer Portal",
) -> dict[str, Any]:
	"""Record customer 1-tap 'No PIN-to-Drive enabled' opt-out and erase any prior ciphertext."""
	_ = actor_label
	ro = _lock_repair_order_row(repair_order)
	_assert_customer_pin_mutable(ro)

	remove_encrypted_password("Repair Order", ro.name, fieldname=_PIN_FIELDNAME)

	now_ts = now_datetime()
	frappe.db.set_value(
		"Repair Order",
		ro.name,
		{
			"pin_to_drive_status": PIN_STATUS_OPTED_OUT,
			"pin_to_drive_encrypted": None,
			"pin_to_drive_opted_out_at": now_ts,
			"pin_to_drive_purged_at": None,
		},
		update_modified=True,
	)

	emit_portal_pin_submitted_event(ro.name, PIN_STATUS_OPTED_OUT)
	return get_ro_pin_customer_state(ro.name)


def get_ro_pin_for_staff(repair_order: str, *, channel: str = "Desk") -> dict[str, Any]:
	"""FROZEN EXPORT FOR DESK & PROJECT 6 (/board Seam 6).

	Enforces staff RBAC + Repair Order read permission + active RO lifecycle,
	verifies RO-bound Fernet decryption, increments audit counters, and records
	a non-sensitive audit Comment on the Repair Order.
	"""
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		frappe.throw(
			_("Authentication is required to view vehicle PIN-to-Drive."),
			frappe.PermissionError,
		)

	if user != "Administrator":
		user_roles = set(frappe.get_roles(user))
		if not (user_roles & PIN_STAFF_ALLOWED_ROLES):
			frappe.throw(
				_("You do not have permission to view vehicle PIN-to-Drive."),
				frappe.PermissionError,
			)

	ro = _lock_repair_order_row(repair_order)
	if not frappe.has_permission("Repair Order", ptype="read", doc=ro.name, user=user):
		frappe.throw(
			_("You do not have permission to view this Repair Order."),
			frappe.PermissionError,
		)

	if ro.status in RO_PIN_PURGE_STATUSES:
		purge_ro_pin(ro.name, reason=ro.status)
		frappe.throw(
			_("PIN-to-Drive has been purged because Repair Order {0} is {1}.").format(
				ro.name, ro.status
			),
			frappe.PermissionError,
		)

	pin_status = ro.get("pin_to_drive_status") or PIN_STATUS_NOT_PROMPTED
	if pin_status != PIN_STATUS_PROVIDED:
		return {
			"available": False,
			"pin_feature_installed": True,
			"repair_order": ro.name,
			"status": pin_status,
			"pin": None,
		}

	ciphertext = get_decrypted_password(
		"Repair Order",
		ro.name,
		fieldname=_PIN_FIELDNAME,
		raise_exception=False,
	)
	if not ciphertext:
		return {
			"available": False,
			"pin_feature_installed": True,
			"repair_order": ro.name,
			"status": pin_status,
			"pin": None,
		}

	try:
		raw_bytes = _get_pin_fernet().decrypt(ciphertext.encode("ascii"))
		envelope = json.loads(raw_bytes.decode("utf-8"))
	except (InvalidToken, ValueError, TypeError, UnicodeError):
		frappe.throw(
			_("Stored PIN-to-Drive payload could not be decrypted."),
			frappe.ValidationError,
		)

	if not isinstance(envelope, dict) or envelope.get("ro") != ro.name:
		frappe.throw(
			_("Stored PIN-to-Drive payload is not bound to this Repair Order."),
			frappe.ValidationError,
		)

	pin = validate_pin_format(envelope.get("pin"))

	now_ts = now_datetime()
	new_count = cint(ro.get("pin_to_drive_view_count") or 0) + 1
	frappe.db.set_value(
		"Repair Order",
		ro.name,
		{
			"pin_to_drive_last_viewed_by": user,
			"pin_to_drive_last_viewed_at": now_ts,
			"pin_to_drive_view_count": new_count,
		},
		update_modified=False,
	)

	clean_channel = channel.strip() if isinstance(channel, str) and channel.strip() else "Desk"
	_insert_ro_audit_comment(
		ro.name,
		_("PIN-to-Drive revealed by {0} via {1} (view #{2}).").format(
			user, clean_channel, new_count
		),
	)

	return {
		"available": True,
		"pin_feature_installed": True,
		"repair_order": ro.name,
		"status": PIN_STATUS_PROVIDED,
		"pin": pin,
		"auto_mask_seconds": PIN_AUTO_MASK_SECONDS,
		"viewed_by": user,
		"viewed_at": str(now_ts),
		"view_count": new_count,
	}


@frappe.whitelist()
def reveal_ro_pin_desk(repair_order: str) -> dict[str, Any]:
	"""Authenticated Desk endpoint for Repair Order Workshop 'Reveal PIN-to-Drive' button."""
	return get_ro_pin_for_staff(repair_order, channel="Desk")


def purge_ro_pin(repair_order: str, *, reason: str = "Closed / Released") -> bool:
	"""Idempotently erase ``__Auth`` PIN ciphertext and mark Provided PINs as Purged."""
	ro_name = _normalize_ro_name(repair_order)
	rows = frappe.db.sql(
		"""
		SELECT name, pin_to_drive_status
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro_name,),
		as_dict=True,
	)

	remove_encrypted_password("Repair Order", ro_name, fieldname=_PIN_FIELDNAME)

	if not rows:
		return False

	prev_status = rows[0].get("pin_to_drive_status") or PIN_STATUS_NOT_PROMPTED
	clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "Closed / Released"

	if prev_status == PIN_STATUS_PROVIDED:
		now_ts = now_datetime()
		frappe.db.set_value(
			"Repair Order",
			ro_name,
			{
				"pin_to_drive_status": PIN_STATUS_PURGED,
				"pin_to_drive_encrypted": None,
				"pin_to_drive_purged_at": now_ts,
			},
			update_modified=False,
		)
		_insert_ro_audit_comment(
			ro_name,
			_("Vehicle PIN-to-Drive automatically purged ({0}).").format(clean_reason),
		)
		emit_portal_pin_purged_event(ro_name, reason=clean_reason)
		return True

	frappe.db.set_value(
		"Repair Order",
		ro_name,
		"pin_to_drive_encrypted",
		None,
		update_modified=False,
	)
	return False


def on_repair_order_update_purge_pin(doc: Any, method: str | None = None) -> None:
	"""Repair Order.on_update hook: purge PIN whenever status enters Closed / Released or Cancelled."""
	_ = method
	if not doc or not getattr(doc, "name", None):
		return

	status = getattr(doc, "status", None)
	if status not in RO_PIN_PURGE_STATUSES:
		return

	purged = purge_ro_pin(doc.name, reason=status)
	doc.pin_to_drive_encrypted = None
	if purged:
		doc.pin_to_drive_status = PIN_STATUS_PURGED
		doc.pin_to_drive_purged_at = frappe.db.get_value(
			"Repair Order",
			doc.name,
			"pin_to_drive_purged_at",
		)


def purge_stale_closed_ro_pins() -> int:
	"""Daily scheduler safety net: purge any Closed/Released/Cancelled RO still holding PIN state or __Auth rows."""
	purge_statuses = tuple(sorted(RO_PIN_PURGE_STATUSES))

	stale_by_status = frappe.db.sql(
		"""
		SELECT name, status
		FROM `tabRepair Order`
		WHERE status IN %s
		  AND pin_to_drive_status = %s
		""",
		(purge_statuses, PIN_STATUS_PROVIDED),
		as_dict=True,
	)

	stale_by_auth = frappe.db.sql(
		"""
		SELECT a.name AS name, ro.status AS status
		FROM `__Auth` a
		LEFT JOIN `tabRepair Order` ro ON ro.name = a.name
		WHERE a.doctype = 'Repair Order'
		  AND a.fieldname = %s
		  AND (ro.name IS NULL OR ro.status IN %s)
		""",
		(_PIN_FIELDNAME, purge_statuses),
		as_dict=True,
	)

	targets: dict[str, tuple[str, bool]] = {}
	for row in stale_by_status:
		targets[row["name"]] = (row["status"] or "Closed / Released", False)
	for row in stale_by_auth:
		ro_name = row["name"]
		reason = row.get("status") or "Orphaned Repair Order"
		targets[ro_name] = (reason, True)

	purged_count = 0
	for ro_name, (reason, had_auth_row) in targets.items():
		purged = purge_ro_pin(ro_name, reason=reason)
		if purged or had_auth_row:
			purged_count += 1

	return purged_count
