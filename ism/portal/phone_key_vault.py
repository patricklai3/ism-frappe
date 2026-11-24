# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Encrypted Tesla Phone Key invitation vault, advisor/manager audited reveal, and release cleanup.

Stores official Tesla driver invite URLs (``https://ts.la/...`` / ``https://*.tesla.com/...``)
inside an RO-bound or Schedule-Entry-bound authenticated JSON envelope encrypted with
``cryptography.fernet.Fernet`` (using ``ISM Settings.portal_pin_encryption_key``) and persisted
strictly in Frappe's ``__Auth`` table via ``set_encrypted_password``.

Security invariants:
- Invite URLs are write-only to customers (never returned in ``/portal`` payloads or logs).
- Staff reveal is strictly limited to ``ISM Service Advisor``, ``ISM Manager``,
  ``System Manager``, and ``Administrator`` (explicitly excluding ``ISM Technician`` and ``/tech``).
- Every staff access increments audit counters and inserts a non-sensitive ``Comment``.
- Automatically purged from ``__Auth`` on ``Closed / Released`` (transitioning status to
  ``Released``) and ``Cancelled`` / ``No Show`` (transitioning status to ``Purged``).
- Never imports ``ism.board`` (Seam 5 decoupling).
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from cryptography.fernet import InvalidToken
from frappe import _
from frappe.utils import cint, get_fullname, now_datetime
from frappe.utils.password import (
	get_decrypted_password,
	remove_encrypted_password,
	set_encrypted_password,
)

from ism.portal.contracts import (
	DEFAULT_PHONE_KEY_DISCLAIMER,
	PHONE_KEY_AUTO_MASK_SECONDS,
	PHONE_KEY_STAFF_ALLOWED_ROLES,
	PHONE_KEY_STATUS_NOT_PROVIDED,
	PHONE_KEY_STATUS_PROVIDED,
	PHONE_KEY_STATUS_PURGED,
	PHONE_KEY_STATUS_RELEASED,
	RO_PHONE_KEY_MUTATION_LOCKED_STATUSES,
	RO_PHONE_KEY_PURGE_STATUSES,
	SE_PHONE_KEY_MUTATION_LOCKED_STATUSES,
	SE_PHONE_KEY_PURGE_STATUSES,
	is_tesla_vehicle_info,
	validate_tesla_phone_key_url,
)
from ism.portal.events import (
	emit_portal_phone_key_purged_event,
	emit_portal_phone_key_submitted_event,
	emit_portal_schedule_entry_phone_key_purged_event,
	emit_portal_schedule_entry_phone_key_submitted_event,
)

__all__ = [
	"dispatch_vehicle_release_credential_sms",
	"get_ro_phone_key_customer_state",
	"get_ro_phone_key_for_staff",
	"get_ro_phone_key_state",
	"get_schedule_entry_phone_key_customer_state",
	"get_schedule_entry_phone_key_for_staff",
	"get_schedule_entry_phone_key_state",
	"is_ro_tesla_vehicle",
	"is_schedule_entry_tesla_vehicle",
	"purge_ro_phone_key",
	"purge_schedule_entry_phone_key",
	"purge_stale_closed_phone_keys",
	"remove_ro_phone_key",
	"remove_schedule_entry_phone_key",
	"reveal_ro_phone_key_desk",
	"reveal_schedule_entry_phone_key_desk",
	"submit_ro_phone_key",
	"submit_schedule_entry_phone_key",
	"transfer_schedule_entry_phone_key_to_ro",
]

_PHONE_KEY_FIELDNAME = "tesla_phone_key_encrypted"
_PASSWORD_DUMMY_MASK = "********"
_RO_PHONE_KEY_SELECT_FIELDS = (
	"name",
	"status",
	"vehicle",
	"vehicle_released",
	"tesla_phone_key_status",
	"tesla_phone_key_submitted_at",
	"tesla_phone_key_updated_at",
	"tesla_phone_key_released_at",
	"tesla_phone_key_released_by",
	"tesla_phone_key_purged_at",
	"tesla_phone_key_last_viewed_by",
	"tesla_phone_key_last_viewed_at",
	"tesla_phone_key_view_count",
)
_SE_PHONE_KEY_SELECT_FIELDS = (
	"name",
	"status",
	"repair_order",
	"repair_vehicle",
	"provisional_vehicle_model",
	"provisional_vehicle_info",
	"tesla_phone_key_status",
	"tesla_phone_key_submitted_at",
	"tesla_phone_key_updated_at",
	"tesla_phone_key_released_at",
	"tesla_phone_key_released_by",
	"tesla_phone_key_purged_at",
	"tesla_phone_key_last_viewed_by",
	"tesla_phone_key_last_viewed_at",
	"tesla_phone_key_view_count",
)


def _get_vault_fernet():
	from ism.portal.pin_vault import _get_pin_fernet

	return _get_pin_fernet()


def _normalize_ro_name(repair_order: Any) -> str:
	if not isinstance(repair_order, str) or not repair_order.strip():
		frappe.throw(_("Repair Order is required."), frappe.ValidationError)
	return repair_order.strip()


def _normalize_se_name(schedule_entry: Any) -> str:
	if not isinstance(schedule_entry, str) or not schedule_entry.strip():
		frappe.throw(_("Schedule Entry is required."), frappe.ValidationError)
	return schedule_entry.strip()


def _lock_repair_order_row(repair_order: str) -> frappe._dict:
	ro_name = _normalize_ro_name(repair_order)
	columns = ", ".join(_RO_PHONE_KEY_SELECT_FIELDS)
	rows = frappe.db.sql(
		f"SELECT {columns} FROM `tabRepair Order` WHERE name = %s FOR UPDATE",
		(ro_name,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Repair Order {0} not found.").format(ro_name), frappe.DoesNotExistError)
	return frappe._dict(rows[0])


def _get_repair_order_row(repair_order: str) -> frappe._dict:
	ro_name = _normalize_ro_name(repair_order)
	row = frappe.db.get_value(
		"Repair Order",
		ro_name,
		list(_RO_PHONE_KEY_SELECT_FIELDS),
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Repair Order {0} not found.").format(ro_name), frappe.DoesNotExistError)
	return frappe._dict(row)


def _lock_schedule_entry_row(schedule_entry: str) -> frappe._dict:
	se_name = _normalize_se_name(schedule_entry)
	columns = ", ".join(_SE_PHONE_KEY_SELECT_FIELDS)
	rows = frappe.db.sql(
		f"SELECT {columns} FROM `tabSchedule Entry` WHERE name = %s FOR UPDATE",
		(se_name,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Schedule Entry {0} not found.").format(se_name), frappe.DoesNotExistError)
	return frappe._dict(rows[0])


def _get_schedule_entry_row(schedule_entry: str) -> frappe._dict:
	se_name = _normalize_se_name(schedule_entry)
	row = frappe.db.get_value(
		"Schedule Entry",
		se_name,
		list(_SE_PHONE_KEY_SELECT_FIELDS),
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Schedule Entry {0} not found.").format(se_name), frappe.DoesNotExistError)
	return frappe._dict(row)


def _format_dt(value: Any) -> str | None:
	return str(value) if value else None


def _get_phone_key_disclaimer() -> str:
	configured = frappe.db.get_single_value("ISM Settings", "portal_phone_key_disclaimer")
	if isinstance(configured, str) and configured.strip():
		return configured.strip()
	return DEFAULT_PHONE_KEY_DISCLAIMER


def _insert_audit_comment(doctype: str, docname: str, text: str) -> None:
	user = getattr(frappe.session, "user", None) or "Administrator"
	comment = frappe.new_doc("Comment")
	comment.comment_type = "Comment"
	comment.reference_doctype = doctype
	comment.reference_name = docname
	comment.comment_email = user
	comment.comment_by = get_fullname(user) or user
	comment.content = text
	comment.insert(ignore_permissions=True)


def _has_staff_phone_key_access(doctype: str, docname: str | None = None) -> bool:
	"""Return True iff user is an authorized Advisor/Manager/System Manager/Admin with doc read permission.

	Explicitly excludes ``ISM Technician`` and ``Guest``.
	"""
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		return False

	if user != "Administrator":
		user_roles = set(frappe.get_roles(user))
		if not (user_roles & PHONE_KEY_STAFF_ALLOWED_ROLES):
			return False

	if docname:
		return bool(frappe.has_permission(doctype, ptype="read", doc=docname, user=user))
	return bool(frappe.has_permission(doctype, ptype="read", user=user))


def _is_repair_vehicle_tesla(vehicle_name: str | None) -> bool:
	clean = (vehicle_name or "").strip() if isinstance(vehicle_name, str) else ""
	if not clean:
		return False
	veh = frappe.db.get_value(
		"Repair Vehicle",
		clean,
		["manufacturer", "model", "vin"],
		as_dict=True,
	)
	if not veh:
		return False
	return is_tesla_vehicle_info(
		manufacturer=veh.get("manufacturer"),
		model=veh.get("model"),
		vin=veh.get("vin"),
	)


def is_ro_tesla_vehicle(ro_doc: Any) -> bool:
	if isinstance(ro_doc, str):
		ro_row = _get_repair_order_row(ro_doc)
		vehicle_name = ro_row.get("vehicle")
	elif isinstance(ro_doc, dict):
		vehicle_name = ro_doc.get("vehicle")
	else:
		vehicle_name = getattr(ro_doc, "vehicle", None)
	return _is_repair_vehicle_tesla(vehicle_name)


def is_schedule_entry_tesla_vehicle(se_doc: Any) -> bool:
	if isinstance(se_doc, str):
		se = _get_schedule_entry_row(se_doc)
	elif isinstance(se_doc, dict):
		se = frappe._dict(se_doc)
	else:
		se = frappe._dict({field: getattr(se_doc, field, None) for field in _SE_PHONE_KEY_SELECT_FIELDS})

	linked_ro = (se.get("repair_order") or "").strip()
	if linked_ro and frappe.db.exists("Repair Order", linked_ro):
		return is_ro_tesla_vehicle(linked_ro)

	if se.get("repair_vehicle"):
		return _is_repair_vehicle_tesla(se.get("repair_vehicle"))

	return is_tesla_vehicle_info(
		model=se.get("provisional_vehicle_model"),
		provisional_info=se.get("provisional_vehicle_info"),
	)


def _assert_customer_ro_phone_key_mutable(ro: frappe._dict) -> None:
	ro_status = (ro.get("status") or "").strip()
	if ro_status in RO_PHONE_KEY_MUTATION_LOCKED_STATUSES:
		frappe.throw(
			_("Tesla Phone Key cannot be changed because this Repair Order is {0}.").format(ro_status),
			frappe.ValidationError,
		)

	prev_status = ro.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	if prev_status in (PHONE_KEY_STATUS_RELEASED, PHONE_KEY_STATUS_PURGED):
		frappe.throw(
			_("Tesla Phone Key has already been {0} for this Repair Order.").format(prev_status.lower()),
			frappe.ValidationError,
		)


def _assert_customer_se_phone_key_mutable(se: frappe._dict) -> None:
	se_status = (se.get("status") or "").strip()
	if se_status in SE_PHONE_KEY_MUTATION_LOCKED_STATUSES:
		frappe.throw(
			_("Tesla Phone Key cannot be changed because this appointment is {0}.").format(se_status),
			frappe.ValidationError,
		)

	prev_status = se.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	if prev_status in (PHONE_KEY_STATUS_RELEASED, PHONE_KEY_STATUS_PURGED):
		frappe.throw(
			_("Tesla Phone Key has already been {0} for this appointment.").format(prev_status.lower()),
			frappe.ValidationError,
		)


def get_ro_phone_key_state(repair_order: str) -> dict[str, Any]:
	"""Return strictly non-sensitive staff metadata for Desk & /board (never reads __Auth)."""
	row = _get_repair_order_row(repair_order)
	pk_status = row.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	has_phone_key = (pk_status == PHONE_KEY_STATUS_PROVIDED) and (
		row.get("status") not in RO_PHONE_KEY_PURGE_STATUSES
	)
	can_reveal = bool(has_phone_key and _has_staff_phone_key_access("Repair Order", row.name))

	return {
		"repair_order": row.name,
		"phone_key_feature_installed": True,
		"status": pk_status,
		"has_phone_key": has_phone_key,
		"can_reveal": can_reveal,
		"submitted_at": _format_dt(row.get("tesla_phone_key_submitted_at")),
		"updated_at": _format_dt(row.get("tesla_phone_key_updated_at")),
		"released_at": _format_dt(row.get("tesla_phone_key_released_at")),
		"released_by": row.get("tesla_phone_key_released_by") or None,
		"purged_at": _format_dt(row.get("tesla_phone_key_purged_at")),
		"last_viewed_by": row.get("tesla_phone_key_last_viewed_by") or None,
		"last_viewed_at": _format_dt(row.get("tesla_phone_key_last_viewed_at")),
		"view_count": cint(row.get("tesla_phone_key_view_count") or 0),
	}


def get_ro_phone_key_customer_state(
	ro_doc: Any,
	*,
	is_tesla: bool | None = None,
) -> dict[str, Any]:
	"""Customer-safe Tesla Phone Key projection for /portal (NEVER returns invite URL)."""
	if isinstance(ro_doc, str):
		ro = _get_repair_order_row(ro_doc)
	elif isinstance(ro_doc, dict):
		ro = frappe._dict(ro_doc)
	else:
		ro = frappe._dict({field: getattr(ro_doc, field, None) for field in _RO_PHONE_KEY_SELECT_FIELDS})

	resolved_is_tesla = bool(is_tesla) if is_tesla is not None else is_ro_tesla_vehicle(ro)
	pk_status = ro.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	ro_status = (ro.get("status") or "").strip()
	is_locked = ro_status in RO_PHONE_KEY_MUTATION_LOCKED_STATUSES
	is_terminal_key = pk_status in (PHONE_KEY_STATUS_RELEASED, PHONE_KEY_STATUS_PURGED)

	can_submit = bool(
		resolved_is_tesla and pk_status == PHONE_KEY_STATUS_NOT_PROVIDED and not is_locked
	)
	can_update = bool(resolved_is_tesla and not is_terminal_key and not is_locked)
	can_remove = bool(
		resolved_is_tesla and pk_status == PHONE_KEY_STATUS_PROVIDED and not is_locked
	)

	return {
		"is_tesla": resolved_is_tesla,
		"status": pk_status,
		"can_submit": can_submit,
		"can_update": can_update,
		"can_remove": can_remove,
		"submitted_at": _format_dt(ro.get("tesla_phone_key_submitted_at")),
		"updated_at": _format_dt(ro.get("tesla_phone_key_updated_at")),
		"released_at": _format_dt(ro.get("tesla_phone_key_released_at")),
		"purged_at": _format_dt(ro.get("tesla_phone_key_purged_at")),
		"disclaimer": _get_phone_key_disclaimer(),
	}


def submit_ro_phone_key(
	repair_order: str,
	raw_invite_url: str,
	*,
	actor_label: str = "Customer Portal",
) -> dict[str, Any]:
	"""Validate, encrypt into an RO-bound Fernet envelope, and store a Tesla invite link in __Auth."""
	del actor_label
	invite_url = validate_tesla_phone_key_url(raw_invite_url)
	ro = _lock_repair_order_row(repair_order)
	if not is_ro_tesla_vehicle(ro):
		frappe.throw(
			_("Tesla Phone Key invitations are only supported for Tesla vehicles."),
			frappe.ValidationError,
		)
	_assert_customer_ro_phone_key_mutable(ro)

	fernet = _get_vault_fernet()
	envelope = json.dumps(
		{"v": 1, "ro": ro.name, "invite_url": invite_url},
		separators=(",", ":"),
		sort_keys=True,
	).encode("utf-8")
	ciphertext = fernet.encrypt(envelope).decode("ascii")

	set_encrypted_password(
		"Repair Order",
		ro.name,
		ciphertext,
		fieldname=_PHONE_KEY_FIELDNAME,
	)

	now_ts = now_datetime()
	submitted_at = ro.get("tesla_phone_key_submitted_at") or now_ts
	frappe.db.set_value(
		"Repair Order",
		ro.name,
		{
			"tesla_phone_key_status": PHONE_KEY_STATUS_PROVIDED,
			"tesla_phone_key_encrypted": _PASSWORD_DUMMY_MASK,
			"tesla_phone_key_submitted_at": submitted_at,
			"tesla_phone_key_updated_at": now_ts,
			"tesla_phone_key_released_at": None,
			"tesla_phone_key_released_by": None,
			"tesla_phone_key_purged_at": None,
		},
		update_modified=True,
	)

	emit_portal_phone_key_submitted_event(ro.name, PHONE_KEY_STATUS_PROVIDED)
	return get_ro_phone_key_customer_state(ro.name, is_tesla=True)


def remove_ro_phone_key(
	repair_order: str,
	*,
	actor_label: str = "Customer Portal",
) -> dict[str, Any]:
	"""Erase encrypted Tesla Phone Key invite from __Auth and reset status to Not Provided."""
	del actor_label
	ro = _lock_repair_order_row(repair_order)
	_assert_customer_ro_phone_key_mutable(ro)

	remove_encrypted_password("Repair Order", ro.name, fieldname=_PHONE_KEY_FIELDNAME)
	prev_status = ro.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED

	frappe.db.set_value(
		"Repair Order",
		ro.name,
		{
			"tesla_phone_key_status": PHONE_KEY_STATUS_NOT_PROVIDED,
			"tesla_phone_key_encrypted": None,
			"tesla_phone_key_updated_at": now_datetime(),
		},
		update_modified=True,
	)

	if prev_status == PHONE_KEY_STATUS_PROVIDED:
		_insert_audit_comment(
			"Repair Order",
			ro.name,
			_("Customer removed Tesla Phone Key invitation link via Customer Portal."),
		)
		emit_portal_phone_key_submitted_event(ro.name, PHONE_KEY_STATUS_NOT_PROVIDED)

	return get_ro_phone_key_customer_state(ro.name)


def get_ro_phone_key_for_staff(repair_order: str, *, channel: str = "Desk") -> dict[str, Any]:
	"""Advisor/Manager-only audited reveal of a Repair Order Tesla Phone Key invitation link."""
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		frappe.throw(
			_("Authentication is required to view Tesla Phone Key invitations."),
			frappe.PermissionError,
		)

	if user != "Administrator":
		user_roles = set(frappe.get_roles(user))
		if not (user_roles & PHONE_KEY_STAFF_ALLOWED_ROLES):
			frappe.throw(
				_("Only authorized service advisors and managers can view Tesla Phone Key invitations."),
				frappe.PermissionError,
			)

	ro = _lock_repair_order_row(repair_order)
	if not frappe.has_permission("Repair Order", ptype="read", doc=ro.name, user=user):
		frappe.throw(
			_("You do not have permission to view this Repair Order."),
			frappe.PermissionError,
		)

	if ro.status in RO_PHONE_KEY_PURGE_STATUSES:
		purge_ro_phone_key(
			ro.name,
			reason=ro.status,
			released=(ro.status == "Closed / Released"),
		)
		frappe.throw(
			_("Tesla Phone Key invitation has been purged because Repair Order {0} is {1}.").format(
				ro.name, ro.status
			),
			frappe.PermissionError,
		)

	pk_status = ro.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	if pk_status != PHONE_KEY_STATUS_PROVIDED:
		return {
			"available": False,
			"phone_key_feature_installed": True,
			"repair_order": ro.name,
			"status": pk_status,
			"invite_url": None,
		}

	ciphertext = get_decrypted_password(
		"Repair Order",
		ro.name,
		fieldname=_PHONE_KEY_FIELDNAME,
		raise_exception=False,
	)
	if not ciphertext:
		return {
			"available": False,
			"phone_key_feature_installed": True,
			"repair_order": ro.name,
			"status": pk_status,
			"invite_url": None,
		}

	try:
		raw_bytes = _get_vault_fernet().decrypt(ciphertext.encode("ascii"))
		envelope = json.loads(raw_bytes.decode("utf-8"))
	except (InvalidToken, ValueError, TypeError, UnicodeError):
		frappe.throw(
			_("Stored Tesla Phone Key payload could not be decrypted."),
			frappe.ValidationError,
		)

	if not isinstance(envelope, dict) or envelope.get("ro") != ro.name:
		frappe.throw(
			_("Stored Tesla Phone Key payload is not bound to this Repair Order."),
			frappe.ValidationError,
		)

	invite_url = validate_tesla_phone_key_url(envelope.get("invite_url"))

	now_ts = now_datetime()
	new_count = cint(ro.get("tesla_phone_key_view_count") or 0) + 1
	frappe.db.set_value(
		"Repair Order",
		ro.name,
		{
			"tesla_phone_key_last_viewed_by": user,
			"tesla_phone_key_last_viewed_at": now_ts,
			"tesla_phone_key_view_count": new_count,
		},
		update_modified=False,
	)

	clean_channel = channel.strip() if isinstance(channel, str) and channel.strip() else "Desk"
	_insert_audit_comment(
		"Repair Order",
		ro.name,
		_("Tesla Phone Key invitation link accessed by {0} via {1} (access #{2}).").format(
			user, clean_channel, new_count
		),
	)

	return {
		"available": True,
		"phone_key_feature_installed": True,
		"repair_order": ro.name,
		"status": PHONE_KEY_STATUS_PROVIDED,
		"invite_url": invite_url,
		"auto_mask_seconds": PHONE_KEY_AUTO_MASK_SECONDS,
		"viewed_by": user,
		"viewed_at": str(now_ts),
		"view_count": new_count,
	}


@frappe.whitelist()
def reveal_ro_phone_key_desk(repair_order: str) -> dict[str, Any]:
	"""Authenticated Desk endpoint for Repair Order 'Tesla Phone Key Invite' button."""
	return get_ro_phone_key_for_staff(repair_order, channel="Desk")


def purge_ro_phone_key(
	repair_order: str,
	*,
	reason: str = "Closed / Released",
	released: bool = False,
	released_by: str | None = None,
) -> bool:
	"""Idempotently erase __Auth Tesla Phone Key ciphertext and transition Provided -> Released/Purged."""
	ro_name = _normalize_ro_name(repair_order)
	rows = frappe.db.sql(
		"""
		SELECT name, tesla_phone_key_status, tesla_phone_key_released_at, tesla_phone_key_released_by
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro_name,),
		as_dict=True,
	)

	remove_encrypted_password("Repair Order", ro_name, fieldname=_PHONE_KEY_FIELDNAME)

	if not rows:
		return False

	row = rows[0]
	prev_status = row.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "Closed / Released"

	if prev_status == PHONE_KEY_STATUS_PROVIDED:
		now_ts = now_datetime()
		target_status = PHONE_KEY_STATUS_RELEASED if released else PHONE_KEY_STATUS_PURGED
		updates: dict[str, Any] = {
			"tesla_phone_key_status": target_status,
			"tesla_phone_key_encrypted": None,
			"tesla_phone_key_purged_at": now_ts,
		}
		if released:
			updates["tesla_phone_key_released_at"] = row.get("tesla_phone_key_released_at") or now_ts
			updates["tesla_phone_key_released_by"] = (
				row.get("tesla_phone_key_released_by")
				or released_by
				or getattr(frappe.session, "user", None)
				or "Administrator"
			)

		frappe.db.set_value(
			"Repair Order",
			ro_name,
			updates,
			update_modified=False,
		)
		if released:
			_insert_audit_comment(
				"Repair Order",
				ro_name,
				_("Tesla Phone Key released and invitation link permanently purged ({0}).").format(
					clean_reason
				),
			)
		else:
			_insert_audit_comment(
				"Repair Order",
				ro_name,
				_("Tesla Phone Key invitation link automatically purged ({0}).").format(clean_reason),
			)
		emit_portal_phone_key_purged_event(ro_name, status=target_status, reason=clean_reason)
		return True

	frappe.db.set_value(
		"Repair Order",
		ro_name,
		"tesla_phone_key_encrypted",
		None,
		update_modified=False,
	)
	return False


def get_schedule_entry_phone_key_state(schedule_entry: str) -> dict[str, Any]:
	"""Return strictly non-sensitive staff Phone Key metadata for a Schedule Entry (delegates to RO when linked)."""
	row = _get_schedule_entry_row(schedule_entry)
	linked_ro = (row.get("repair_order") or "").strip()
	if linked_ro and frappe.db.exists("Repair Order", linked_ro):
		ro_state = get_ro_phone_key_state(linked_ro)
		ro_state["schedule_entry"] = row.name
		return ro_state

	pk_status = row.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	has_phone_key = (pk_status == PHONE_KEY_STATUS_PROVIDED) and (
		row.get("status") not in SE_PHONE_KEY_PURGE_STATUSES
	)
	can_reveal = bool(has_phone_key and _has_staff_phone_key_access("Schedule Entry", row.name))

	return {
		"schedule_entry": row.name,
		"repair_order": None,
		"phone_key_feature_installed": True,
		"status": pk_status,
		"has_phone_key": has_phone_key,
		"can_reveal": can_reveal,
		"submitted_at": _format_dt(row.get("tesla_phone_key_submitted_at")),
		"updated_at": _format_dt(row.get("tesla_phone_key_updated_at")),
		"released_at": _format_dt(row.get("tesla_phone_key_released_at")),
		"released_by": row.get("tesla_phone_key_released_by") or None,
		"purged_at": _format_dt(row.get("tesla_phone_key_purged_at")),
		"last_viewed_by": row.get("tesla_phone_key_last_viewed_by") or None,
		"last_viewed_at": _format_dt(row.get("tesla_phone_key_last_viewed_at")),
		"view_count": cint(row.get("tesla_phone_key_view_count") or 0),
	}


def get_schedule_entry_phone_key_customer_state(
	se_doc: Any,
	*,
	is_tesla: bool | None = None,
) -> dict[str, Any]:
	"""Customer-safe Tesla Phone Key projection for a Schedule Entry (NEVER returns invite URL)."""
	if isinstance(se_doc, str):
		se = _get_schedule_entry_row(se_doc)
	elif isinstance(se_doc, dict):
		se = frappe._dict(se_doc)
	else:
		se = frappe._dict({field: getattr(se_doc, field, None) for field in _SE_PHONE_KEY_SELECT_FIELDS})

	linked_ro = (se.get("repair_order") or "").strip()
	if linked_ro and frappe.db.exists("Repair Order", linked_ro):
		return get_ro_phone_key_customer_state(linked_ro, is_tesla=is_tesla)

	resolved_is_tesla = (
		bool(is_tesla) if is_tesla is not None else is_schedule_entry_tesla_vehicle(se)
	)
	pk_status = se.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	se_status = (se.get("status") or "").strip()
	is_locked = se_status in SE_PHONE_KEY_MUTATION_LOCKED_STATUSES
	is_terminal_key = pk_status in (PHONE_KEY_STATUS_RELEASED, PHONE_KEY_STATUS_PURGED)

	can_submit = bool(
		resolved_is_tesla and pk_status == PHONE_KEY_STATUS_NOT_PROVIDED and not is_locked
	)
	can_update = bool(resolved_is_tesla and not is_terminal_key and not is_locked)
	can_remove = bool(
		resolved_is_tesla and pk_status == PHONE_KEY_STATUS_PROVIDED and not is_locked
	)

	return {
		"is_tesla": resolved_is_tesla,
		"status": pk_status,
		"can_submit": can_submit,
		"can_update": can_update,
		"can_remove": can_remove,
		"submitted_at": _format_dt(se.get("tesla_phone_key_submitted_at")),
		"updated_at": _format_dt(se.get("tesla_phone_key_updated_at")),
		"released_at": _format_dt(se.get("tesla_phone_key_released_at")),
		"purged_at": _format_dt(se.get("tesla_phone_key_purged_at")),
		"disclaimer": _get_phone_key_disclaimer(),
	}


def submit_schedule_entry_phone_key(
	schedule_entry: str,
	raw_invite_url: str,
	*,
	actor_label: str = "Customer Portal",
) -> dict[str, Any]:
	"""Validate, encrypt into an SE-bound Fernet envelope, and store a Tesla invite link in __Auth."""
	invite_url = validate_tesla_phone_key_url(raw_invite_url)
	se = _lock_schedule_entry_row(schedule_entry)
	linked_ro = (se.get("repair_order") or "").strip()
	if linked_ro and frappe.db.exists("Repair Order", linked_ro):
		return submit_ro_phone_key(linked_ro, invite_url, actor_label=actor_label)

	if not is_schedule_entry_tesla_vehicle(se):
		frappe.throw(
			_("Tesla Phone Key invitations are only supported for Tesla vehicles."),
			frappe.ValidationError,
		)
	_assert_customer_se_phone_key_mutable(se)

	fernet = _get_vault_fernet()
	envelope = json.dumps(
		{"v": 1, "se": se.name, "invite_url": invite_url},
		separators=(",", ":"),
		sort_keys=True,
	).encode("utf-8")
	ciphertext = fernet.encrypt(envelope).decode("ascii")

	set_encrypted_password(
		"Schedule Entry",
		se.name,
		ciphertext,
		fieldname=_PHONE_KEY_FIELDNAME,
	)

	now_ts = now_datetime()
	submitted_at = se.get("tesla_phone_key_submitted_at") or now_ts
	frappe.db.set_value(
		"Schedule Entry",
		se.name,
		{
			"tesla_phone_key_status": PHONE_KEY_STATUS_PROVIDED,
			"tesla_phone_key_encrypted": _PASSWORD_DUMMY_MASK,
			"tesla_phone_key_submitted_at": submitted_at,
			"tesla_phone_key_updated_at": now_ts,
			"tesla_phone_key_released_at": None,
			"tesla_phone_key_released_by": None,
			"tesla_phone_key_purged_at": None,
		},
		update_modified=True,
	)

	emit_portal_schedule_entry_phone_key_submitted_event(se.name, PHONE_KEY_STATUS_PROVIDED)
	return get_schedule_entry_phone_key_customer_state(se.name, is_tesla=True)


def remove_schedule_entry_phone_key(
	schedule_entry: str,
	*,
	actor_label: str = "Customer Portal",
) -> dict[str, Any]:
	"""Erase Schedule Entry Tesla Phone Key invite from __Auth and reset status to Not Provided."""
	se = _lock_schedule_entry_row(schedule_entry)
	linked_ro = (se.get("repair_order") or "").strip()
	if linked_ro and frappe.db.exists("Repair Order", linked_ro):
		return remove_ro_phone_key(linked_ro, actor_label=actor_label)

	_assert_customer_se_phone_key_mutable(se)
	remove_encrypted_password("Schedule Entry", se.name, fieldname=_PHONE_KEY_FIELDNAME)
	prev_status = se.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED

	frappe.db.set_value(
		"Schedule Entry",
		se.name,
		{
			"tesla_phone_key_status": PHONE_KEY_STATUS_NOT_PROVIDED,
			"tesla_phone_key_encrypted": None,
			"tesla_phone_key_updated_at": now_datetime(),
		},
		update_modified=True,
	)

	if prev_status == PHONE_KEY_STATUS_PROVIDED:
		_insert_audit_comment(
			"Schedule Entry",
			se.name,
			_("Customer removed Tesla Phone Key invitation link via Customer Portal."),
		)
		emit_portal_schedule_entry_phone_key_submitted_event(se.name, PHONE_KEY_STATUS_NOT_PROVIDED)

	return get_schedule_entry_phone_key_customer_state(se.name)


def get_schedule_entry_phone_key_for_staff(
	schedule_entry: str,
	*,
	channel: str = "Desk",
) -> dict[str, Any]:
	"""Advisor/Manager-only audited reveal of a Schedule Entry Tesla Phone Key (delegates to RO when linked)."""
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		frappe.throw(
			_("Authentication is required to view Tesla Phone Key invitations."),
			frappe.PermissionError,
		)

	if user != "Administrator":
		user_roles = set(frappe.get_roles(user))
		if not (user_roles & PHONE_KEY_STAFF_ALLOWED_ROLES):
			frappe.throw(
				_("Only authorized service advisors and managers can view Tesla Phone Key invitations."),
				frappe.PermissionError,
			)

	se = _lock_schedule_entry_row(schedule_entry)
	linked_ro = (se.get("repair_order") or "").strip()
	if linked_ro and frappe.db.exists("Repair Order", linked_ro):
		ro_res = get_ro_phone_key_for_staff(linked_ro, channel=channel)
		ro_res["schedule_entry"] = se.name
		return ro_res

	if not frappe.has_permission("Schedule Entry", ptype="read", doc=se.name, user=user):
		frappe.throw(
			_("You do not have permission to view this Schedule Entry."),
			frappe.PermissionError,
		)

	if se.status in SE_PHONE_KEY_PURGE_STATUSES:
		purge_schedule_entry_phone_key(se.name, reason=se.status)
		frappe.throw(
			_("Tesla Phone Key invitation has been purged because appointment {0} is {1}.").format(
				se.name, se.status
			),
			frappe.PermissionError,
		)

	pk_status = se.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	if pk_status != PHONE_KEY_STATUS_PROVIDED:
		return {
			"available": False,
			"phone_key_feature_installed": True,
			"schedule_entry": se.name,
			"repair_order": None,
			"status": pk_status,
			"invite_url": None,
		}

	ciphertext = get_decrypted_password(
		"Schedule Entry",
		se.name,
		fieldname=_PHONE_KEY_FIELDNAME,
		raise_exception=False,
	)
	if not ciphertext:
		return {
			"available": False,
			"phone_key_feature_installed": True,
			"schedule_entry": se.name,
			"repair_order": None,
			"status": pk_status,
			"invite_url": None,
		}

	try:
		raw_bytes = _get_vault_fernet().decrypt(ciphertext.encode("ascii"))
		envelope = json.loads(raw_bytes.decode("utf-8"))
	except (InvalidToken, ValueError, TypeError, UnicodeError):
		frappe.throw(
			_("Stored Tesla Phone Key payload could not be decrypted."),
			frappe.ValidationError,
		)

	if not isinstance(envelope, dict) or envelope.get("se") != se.name:
		frappe.throw(
			_("Stored Tesla Phone Key payload is not bound to this Schedule Entry."),
			frappe.ValidationError,
		)

	invite_url = validate_tesla_phone_key_url(envelope.get("invite_url"))

	now_ts = now_datetime()
	new_count = cint(se.get("tesla_phone_key_view_count") or 0) + 1
	frappe.db.set_value(
		"Schedule Entry",
		se.name,
		{
			"tesla_phone_key_last_viewed_by": user,
			"tesla_phone_key_last_viewed_at": now_ts,
			"tesla_phone_key_view_count": new_count,
		},
		update_modified=False,
	)

	clean_channel = channel.strip() if isinstance(channel, str) and channel.strip() else "Desk"
	_insert_audit_comment(
		"Schedule Entry",
		se.name,
		_("Tesla Phone Key invitation link accessed by {0} via {1} (access #{2}).").format(
			user, clean_channel, new_count
		),
	)

	return {
		"available": True,
		"phone_key_feature_installed": True,
		"schedule_entry": se.name,
		"repair_order": None,
		"status": PHONE_KEY_STATUS_PROVIDED,
		"invite_url": invite_url,
		"auto_mask_seconds": PHONE_KEY_AUTO_MASK_SECONDS,
		"viewed_by": user,
		"viewed_at": str(now_ts),
		"view_count": new_count,
	}


@frappe.whitelist()
def reveal_schedule_entry_phone_key_desk(schedule_entry: str) -> dict[str, Any]:
	"""Authenticated Desk endpoint for Schedule Entry 'Tesla Phone Key Invite' button."""
	return get_schedule_entry_phone_key_for_staff(schedule_entry, channel="Desk")


def transfer_schedule_entry_phone_key_to_ro(schedule_entry: str, repair_order: str) -> bool:
	"""Atomically re-encrypt a Schedule Entry Tesla Phone Key into an RO-bound envelope and erase the SE copy."""
	se_name = _normalize_se_name(schedule_entry)
	ro_name = _normalize_ro_name(repair_order)
	if not frappe.db.exists("Schedule Entry", se_name) or not frappe.db.exists("Repair Order", ro_name):
		return False

	se = _lock_schedule_entry_row(se_name)
	ro = _lock_repair_order_row(ro_name)

	if ro.get("status") in RO_PHONE_KEY_PURGE_STATUSES:
		purge_schedule_entry_phone_key(
			se.name,
			reason=ro.get("status") or "Closed / Released",
			released=(ro.get("status") == "Closed / Released"),
		)
		return False

	ro_pk_status = ro.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	se_pk_status = se.get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED

	if ro_pk_status in (
		PHONE_KEY_STATUS_PROVIDED,
		PHONE_KEY_STATUS_RELEASED,
		PHONE_KEY_STATUS_PURGED,
	):
		remove_encrypted_password("Schedule Entry", se.name, fieldname=_PHONE_KEY_FIELDNAME)
		frappe.db.set_value(
			"Schedule Entry",
			se.name,
			"tesla_phone_key_encrypted",
			None,
			update_modified=False,
		)
		return False

	if se_pk_status != PHONE_KEY_STATUS_PROVIDED:
		return False

	ciphertext = get_decrypted_password(
		"Schedule Entry",
		se.name,
		fieldname=_PHONE_KEY_FIELDNAME,
		raise_exception=False,
	)
	if not ciphertext:
		return False

	try:
		fernet = _get_vault_fernet()
		raw_bytes = fernet.decrypt(ciphertext.encode("ascii"))
		envelope = json.loads(raw_bytes.decode("utf-8"))
	except (InvalidToken, ValueError, TypeError, UnicodeError):
		frappe.log_error(
			title=f"Schedule Entry Phone Key Transfer Decrypt Failed: {se.name}",
			message=frappe.get_traceback(),
		)
		return False

	if not isinstance(envelope, dict) or envelope.get("se") != se.name:
		return False

	invite_url = validate_tesla_phone_key_url(envelope.get("invite_url"))
	ro_envelope = json.dumps(
		{"v": 1, "ro": ro.name, "invite_url": invite_url},
		separators=(",", ":"),
		sort_keys=True,
	).encode("utf-8")
	ro_ciphertext = fernet.encrypt(ro_envelope).decode("ascii")

	set_encrypted_password(
		"Repair Order",
		ro.name,
		ro_ciphertext,
		fieldname=_PHONE_KEY_FIELDNAME,
	)

	now_ts = now_datetime()
	frappe.db.set_value(
		"Repair Order",
		ro.name,
		{
			"tesla_phone_key_status": PHONE_KEY_STATUS_PROVIDED,
			"tesla_phone_key_encrypted": _PASSWORD_DUMMY_MASK,
			"tesla_phone_key_submitted_at": se.get("tesla_phone_key_submitted_at") or now_ts,
			"tesla_phone_key_updated_at": se.get("tesla_phone_key_updated_at") or now_ts,
			"tesla_phone_key_released_at": None,
			"tesla_phone_key_released_by": None,
			"tesla_phone_key_purged_at": None,
			"tesla_phone_key_last_viewed_by": se.get("tesla_phone_key_last_viewed_by") or None,
			"tesla_phone_key_last_viewed_at": se.get("tesla_phone_key_last_viewed_at") or None,
			"tesla_phone_key_view_count": cint(se.get("tesla_phone_key_view_count") or 0),
		},
		update_modified=False,
	)

	remove_encrypted_password("Schedule Entry", se.name, fieldname=_PHONE_KEY_FIELDNAME)
	frappe.db.set_value(
		"Schedule Entry",
		se.name,
		"tesla_phone_key_encrypted",
		None,
		update_modified=False,
	)

	_insert_audit_comment(
		"Repair Order",
		ro.name,
		_("Tesla Phone Key invitation carried over from appointment {0}.").format(se.name),
	)
	emit_portal_phone_key_submitted_event(ro.name, PHONE_KEY_STATUS_PROVIDED)
	return True


def purge_schedule_entry_phone_key(
	schedule_entry: str,
	*,
	reason: str = "Cancelled",
	released: bool = False,
) -> bool:
	"""Idempotently erase Schedule Entry __Auth Tesla Phone Key ciphertext and mark Provided -> Released/Purged."""
	se_name = _normalize_se_name(schedule_entry)
	rows = frappe.db.sql(
		"""
		SELECT name, tesla_phone_key_status
		FROM `tabSchedule Entry`
		WHERE name = %s
		FOR UPDATE
		""",
		(se_name,),
		as_dict=True,
	)

	remove_encrypted_password("Schedule Entry", se_name, fieldname=_PHONE_KEY_FIELDNAME)

	if not rows:
		return False

	prev_status = rows[0].get("tesla_phone_key_status") or PHONE_KEY_STATUS_NOT_PROVIDED
	clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "Cancelled"

	if prev_status == PHONE_KEY_STATUS_PROVIDED:
		now_ts = now_datetime()
		target_status = PHONE_KEY_STATUS_RELEASED if released else PHONE_KEY_STATUS_PURGED
		updates: dict[str, Any] = {
			"tesla_phone_key_status": target_status,
			"tesla_phone_key_encrypted": None,
			"tesla_phone_key_purged_at": now_ts,
		}
		if released:
			updates["tesla_phone_key_released_at"] = now_ts
			updates["tesla_phone_key_released_by"] = (
				getattr(frappe.session, "user", None) or "Administrator"
			)

		frappe.db.set_value(
			"Schedule Entry",
			se_name,
			updates,
			update_modified=False,
		)
		_insert_audit_comment(
			"Schedule Entry",
			se_name,
			_("Tesla Phone Key invitation link automatically {0} ({1}).").format(
				"released and purged" if released else "purged",
				clean_reason,
			),
		)
		emit_portal_schedule_entry_phone_key_purged_event(
			se_name,
			status=target_status,
			reason=clean_reason,
		)
		return True

	frappe.db.set_value(
		"Schedule Entry",
		se_name,
		"tesla_phone_key_encrypted",
		None,
		update_modified=False,
	)
	return False


def purge_stale_closed_phone_keys() -> int:
	"""Daily scheduler safety net: purge stale closed/cancelled RO & SE Tesla Phone Key rows."""
	purge_statuses = tuple(sorted(RO_PHONE_KEY_PURGE_STATUSES))

	stale_by_status = frappe.db.sql(
		"""
		SELECT name, status
		FROM `tabRepair Order`
		WHERE status IN %s
		  AND tesla_phone_key_status = %s
		""",
		(purge_statuses, PHONE_KEY_STATUS_PROVIDED),
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
		(_PHONE_KEY_FIELDNAME, purge_statuses),
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
		purged = purge_ro_phone_key(
			ro_name,
			reason=reason,
			released=(reason == "Closed / Released"),
		)
		if purged or had_auth_row:
			purged_count += 1

	se_purge_statuses = tuple(sorted(SE_PHONE_KEY_PURGE_STATUSES))
	stale_se_by_status = frappe.db.sql(
		"""
		SELECT name, status
		FROM `tabSchedule Entry`
		WHERE status IN %s
		  AND IFNULL(repair_order, '') = ''
		  AND tesla_phone_key_status = %s
		""",
		(se_purge_statuses, PHONE_KEY_STATUS_PROVIDED),
		as_dict=True,
	)
	stale_se_by_auth = frappe.db.sql(
		"""
		SELECT a.name AS name, se.status AS status, se.repair_order AS repair_order
		FROM `__Auth` a
		LEFT JOIN `tabSchedule Entry` se ON se.name = a.name
		WHERE a.doctype = 'Schedule Entry'
		  AND a.fieldname = %s
		  AND (se.name IS NULL OR se.status IN %s OR IFNULL(se.repair_order, '') != '')
		""",
		(_PHONE_KEY_FIELDNAME, se_purge_statuses),
		as_dict=True,
	)

	se_targets: dict[str, tuple[str, bool, str | None]] = {}
	for row in stale_se_by_status:
		se_targets[row["name"]] = (row["status"] or "Cancelled", False, None)
	for row in stale_se_by_auth:
		se_name = row["name"]
		linked_ro = (row.get("repair_order") or "").strip() or None
		reason = row.get("status") or "Orphaned Schedule Entry"
		se_targets[se_name] = (reason, True, linked_ro)

	for se_name, (reason, had_auth_row, linked_ro) in se_targets.items():
		if linked_ro:
			transfer_schedule_entry_phone_key_to_ro(se_name, linked_ro)
			remove_encrypted_password("Schedule Entry", se_name, fieldname=_PHONE_KEY_FIELDNAME)
			purged_count += 1
			continue
		purged = purge_schedule_entry_phone_key(se_name, reason=reason, released=False)
		if purged or had_auth_row:
			purged_count += 1

	return purged_count


def dispatch_vehicle_release_credential_sms(
	ro_doc: Any,
	*,
	pin_removed: bool,
	phone_key_removed: bool,
) -> bool:
	"""Dispatch a single dynamic SMS confirming removal of PIN-to-Drive and/or Tesla Phone Key on release.

	Never raises to callers and never blocks vehicle release if SMS is disabled or fails.
	"""
	if not (pin_removed or phone_key_removed):
		return False

	ro_name = getattr(ro_doc, "name", None) or (
		ro_doc.get("name") if isinstance(ro_doc, dict) else None
	)
	if not ro_name:
		return False

	try:
		from ism.api.sms import dispatch_sms, get_sms_unconfigured_reason
		from ism.portal.auth import get_public_portal_branding, resolve_customer_phone_e164
		from ism.utils.vehicle_display import format_vehicle_display

		if get_sms_unconfigured_reason():
			return False

		customer = getattr(ro_doc, "customer", None) or (
			ro_doc.get("customer") if isinstance(ro_doc, dict) else None
		)
		contact_person = getattr(ro_doc, "contact_person", None) or (
			ro_doc.get("contact_person") if isinstance(ro_doc, dict) else None
		)
		vehicle = getattr(ro_doc, "vehicle", None) or (
			ro_doc.get("vehicle") if isinstance(ro_doc, dict) else None
		)

		target_phone = resolve_customer_phone_e164(
			customer,
			contact_person=contact_person,
		)
		if not target_phone:
			return False

		shop_name = (get_public_portal_branding().get("company_name") or "").strip() or "ISM Automotive"
		vehicle_display = (
			format_vehicle_display(vehicle, fallback=_("your vehicle"))
			if vehicle
			else _("your vehicle")
		)

		if pin_removed and phone_key_removed:
			sms_body = _(
				"{0}: Your {1} has been released. For your privacy, your PIN-to-Drive and Tesla Phone Key "
				"have been permanently removed from our system and shop device. Reply STOP to opt out."
			).format(shop_name, vehicle_display)
		elif pin_removed:
			sms_body = _(
				"{0}: Your {1} has been released. For your privacy, your PIN-to-Drive has been permanently "
				"removed from our system. Reply STOP to opt out."
			).format(shop_name, vehicle_display)
		else:
			sms_body = _(
				"{0}: Your {1} has been released. For your privacy, your Tesla Phone Key has been removed "
				"from our shop device and your invite link permanently deleted. Reply STOP to opt out."
			).format(shop_name, vehicle_display)

		res = dispatch_sms(
			target_phone,
			sms_body,
			message_type="Notification",
			reference_doctype="Repair Order",
			reference_name=ro_name,
		)
		return bool(res.get("success"))
	except Exception:
		frappe.log_error(
			title=f"Vehicle Release Credential Removal SMS Failed ({ro_name})",
			message=frappe.get_traceback(),
		)
		return False
