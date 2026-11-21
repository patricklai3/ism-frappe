# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Customer-safe binary-status Visit Hub projection for Project 4 Customer Portal.

Strictly enforces:
- Binary repair status ONLY (``scheduled``, ``being_worked_on``, ``ready_for_pickup``, ``cancelled``)
- Zero ETA, countdown, progress_percent, mid-work completion flags, bay/technician, or internal times
- Zero secret/token/signature_image/PIN leakage across the entire hub payload
- Zero import of ``ism.board`` (Seam 5 decoupling)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.comms.contracts import PortalSessionInfo, mask_phone_e164
from ism.payments.deposit_requests import get_ro_payment_state
from ism.payments.square_client import get_sdk_boot_config
from ism.portal.auth import (
	assert_session_owns_ro,
	assert_session_owns_schedule_entry,
	get_public_portal_branding,
	is_ro_mutating_locked,
	list_accessible_visit_keys,
)
from ism.portal.contracts import (
	PORTAL_FORBIDDEN_PAYLOAD_KEYS,
	PORTAL_STATUS_BEING_WORKED_ON,
	PORTAL_STATUS_CANCELLED,
	PORTAL_STATUS_LABELS,
	PORTAL_STATUS_READY_FOR_PICKUP,
	PORTAL_STATUS_SCHEDULED,
)
from ism.portal.invoices import get_ro_invoice_summary
from ism.portal.pin_vault import get_ro_pin_customer_state
from ism.scheduling.arrival_policy import format_customer_appointment_display
from ism.scheduling.self_serve import evaluate_reschedule_eligibility

__all__ = [
	"build_portal_hub_payload",
	"derive_binary_visit_status",
]

_READY_RO_STATUSES = frozenset({"Work Completed", "Billed / Paid", "Closed / Released"})
_PRE_DROP_OFF_RO_STATUSES = frozenset({"Draft Estimate", "Pending Authorization"})
_ACTIVE_SCHEDULED_SE_STATUSES = frozenset({"Scheduled", "Confirmed"})
_CANCELLED_SE_STATUSES = frozenset({"Cancelled", "No Show"})
_READ_ONLY_SE_STATUSES = frozenset({"Cancelled", "No Show", "Completed"})
_PENDING_SERVICE_STATUSES = frozenset({"Draft", "Estimating", "Pending Approval"})
_SUBMITTED_APPROVAL_STATUSES = frozenset({"Approved", "Partially Approved", "Declined"})


def _field(obj: Any, name: str, default: Any = None) -> Any:
	if obj is None:
		return default
	if isinstance(obj, Mapping):
		return obj.get(name, default)
	return getattr(obj, name, default)


def _to_plain_value(value: Any) -> Any:
	"""Recursively serialize dataclasses/mappings/dates into plain JSON-safe primitives and strip forbidden keys."""
	if value is None or isinstance(value, str | int | float | bool):
		return value
	if is_dataclass(value) and not isinstance(value, type):
		return _to_plain_value(asdict(value))
	if isinstance(value, Mapping):
		out: dict[str, Any] = {}
		for key, item in value.items():
			key_str = str(key)
			if key_str in PORTAL_FORBIDDEN_PAYLOAD_KEYS:
				continue
			out[key_str] = _to_plain_value(item)
		return out
	if isinstance(value, list | tuple):
		return [_to_plain_value(item) for item in value]
	if isinstance(value, date | datetime):
		return str(value)
	return str(value)


def _safe_appointment_display(se_doc: Any) -> dict[str, Any] | None:
	if se_doc is None:
		return None
	sched_date = _field(se_doc, "scheduled_date")
	sched_time = _field(se_doc, "scheduled_time")
	if not sched_date or sched_time is None or sched_time == "":
		return None
	return _to_plain_value(format_customer_appointment_display(sched_date, sched_time))


def derive_binary_visit_status(
	*,
	ro_doc: Any = None,
	schedule_entry_doc: Any = None,
	pickup_hours_text: str | None = None,
) -> dict[str, Any]:
	"""Project internal Repair Order and/or Schedule Entry state into strictly binary customer status.

	Returns ONLY one of: ``scheduled``, ``being_worked_on``, ``ready_for_pickup``, or ``cancelled``.
	Strictly omits any ETA, countdown, progress_percent, mid-work checklist, bay, technician, or internal time.
	"""
	ro_status = (_field(ro_doc, "status") or "").strip() if ro_doc is not None else ""
	se_status = (_field(schedule_entry_doc, "status") or "").strip() if schedule_entry_doc is not None else ""

	# 1. Cancelled
	if ro_status == "Cancelled" or (ro_doc is None and se_status in _CANCELLED_SE_STATUSES):
		return {
			"code": PORTAL_STATUS_CANCELLED,
			"label": PORTAL_STATUS_LABELS[PORTAL_STATUS_CANCELLED],
		}

	# 2. Done & Ready for Pickup
	if ro_doc is not None and ro_status in _READY_RO_STATUSES:
		resolved_pickup_hours = (
			pickup_hours_text
			if isinstance(pickup_hours_text, str)
			else (get_public_portal_branding().get("pickup_hours_text") or "")
		)
		vehicle_released = bool(
			cint(_field(ro_doc, "vehicle_released", 0) or 0) == 1 or ro_status == "Closed / Released"
		)
		return {
			"code": PORTAL_STATUS_READY_FOR_PICKUP,
			"label": PORTAL_STATUS_LABELS[PORTAL_STATUS_READY_FOR_PICKUP],
			"vehicle_released": vehicle_released,
			"pickup_hours": resolved_pickup_hours,
		}

	# 3. Scheduled (strictly P3 silent drop-off time, zero buffer/offset keys)
	has_active_scheduled_se = (
		schedule_entry_doc is not None
		and se_status in _ACTIVE_SCHEDULED_SE_STATUSES
		and bool(_field(schedule_entry_doc, "scheduled_date"))
		and _field(schedule_entry_doc, "scheduled_time") not in (None, "")
	)
	if (ro_doc is None and se_status in _ACTIVE_SCHEDULED_SE_STATUSES) or (
		ro_doc is not None
		and ro_status in _PRE_DROP_OFF_RO_STATUSES
		and has_active_scheduled_se
		and not _field(ro_doc, "picked_up_at")
		and not cint(_field(ro_doc, "vehicle_released", 0) or 0)
	):
		return {
			"code": PORTAL_STATUS_SCHEDULED,
			"label": PORTAL_STATUS_LABELS[PORTAL_STATUS_SCHEDULED],
			"appointment": _safe_appointment_display(schedule_entry_doc),
		}

	# 4. Being Worked On (all remaining active in-shop states)
	return {
		"code": PORTAL_STATUS_BEING_WORKED_ON,
		"label": PORTAL_STATUS_LABELS[PORTAL_STATUS_BEING_WORKED_ON],
	}


def _build_vehicle_section(ro_doc: Any, se_doc: Any) -> dict[str, Any]:
	vehicle_name = (_field(ro_doc, "vehicle") or _field(se_doc, "repair_vehicle") or "").strip()
	if vehicle_name:
		row = frappe.db.get_value(
			"Repair Vehicle",
			vehicle_name,
			["model_year", "manufacturer", "model", "license_plate", "vin"],
			as_dict=True,
		)
		if row:
			year = row.get("model_year") or None
			manufacturer = (row.get("manufacturer") or "").strip() or None
			model = (row.get("model") or "").strip() or None
			plate = (row.get("license_plate") or "").strip() or None
			vin = (row.get("vin") or "").strip() or None
			label_parts = [str(year) if year else "", manufacturer or "", model or ""]
			base_label = " ".join(part for part in label_parts if part).strip() or vehicle_name
			label = f"{base_label} ({plate})" if plate else base_label
			return {
				"model_year": year,
				"manufacturer": manufacturer,
				"make": manufacturer,
				"model": model,
				"license_plate": plate,
				"vin": vin,
				"label": label,
			}

	if se_doc is not None:
		prov_year = _field(se_doc, "provisional_vehicle_year") or None
		prov_model = (_field(se_doc, "provisional_vehicle_model") or "").strip() or None
		prov_info = (_field(se_doc, "provisional_vehicle_info") or "").strip() or None
		prov_plate = (_field(se_doc, "provisional_vin_or_plate") or "").strip() or None
		display_label = (
			se_doc.get_display_vehicle()
			if hasattr(se_doc, "get_display_vehicle")
			else " ".join(part for part in [str(prov_year) if prov_year else "", prov_model or prov_info or ""] if part)
		)
		return {
			"model_year": prov_year,
			"manufacturer": None,
			"make": None,
			"model": prov_model or prov_info,
			"license_plate": prov_plate,
			"vin": None,
			"label": (display_label or "").strip() or _("Vehicle"),
		}

	return {
		"model_year": None,
		"manufacturer": None,
		"make": None,
		"model": None,
		"license_plate": None,
		"vin": None,
		"label": _("Vehicle"),
	}


def _build_appointment_section(se_doc: Any) -> dict[str, Any] | None:
	if se_doc is None:
		return None
	display = _safe_appointment_display(se_doc)
	eligibility = _to_plain_value(evaluate_reschedule_eligibility(se_doc))
	return {
		"schedule_entry": se_doc.name,
		"status": _field(se_doc, "status"),
		"display": display,
		"eligibility": eligibility,
	}


def _build_quote_section(ro_doc: Any, *, is_read_only: bool) -> dict[str, Any] | None:
	if ro_doc is None:
		return None

	# Deferred import avoids circular dependency between ism.portal.__init__ and ism.api.approval.
	from ism.api.approval import _serialize_approval_logs, _serialize_portal_services

	services = list(getattr(ro_doc, "services", None) or [])
	services_list = _to_plain_value(_serialize_portal_services(services))
	has_pending_services = any(
		(_field(svc, "status") or "") in _PENDING_SERVICE_STATUSES for svc in services
	)
	already_submitted = bool(
		not has_pending_services
		and (_field(ro_doc, "approval_status") or "") in _SUBMITTED_APPROVAL_STATUSES
		and (_field(ro_doc, "signature_image") or _field(ro_doc, "authorized_at"))
	)
	can_authorize = bool(not is_read_only and has_pending_services)

	settings = frappe.get_cached_doc("ISM Settings")
	disclaimer = (getattr(settings, "disclaimer_text", None) or "").strip() or _(
		"I hereby authorize the repair work listed above to be done along with the necessary material. "
		"You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
	)
	approval_logs = _to_plain_value(_serialize_approval_logs(ro_doc))

	return {
		"repair_order": ro_doc.name,
		"posting_date": str(ro_doc.posting_date) if getattr(ro_doc, "posting_date", None) else None,
		"approval_status": getattr(ro_doc, "approval_status", None),
		"customer_concern": getattr(ro_doc, "customer_concern", None),
		"odometer_in": getattr(ro_doc, "odometer_in", None),
		"is_supplement": int(any(s.get("is_supplement") for s in services_list)),
		"services": services_list,
		"net_total": flt(getattr(ro_doc, "net_total", 0.0), 2),
		"tax_rate": flt(getattr(ro_doc, "tax_rate", 0.0), 2),
		"tax_total": flt(getattr(ro_doc, "total_taxes_and_charges", 0.0), 2),
		"grand_total": flt(getattr(ro_doc, "grand_total", 0.0), 2),
		"cost_tolerance_percentage": flt(getattr(settings, "cost_tolerance_percentage", 0.0), 2),
		"cost_tolerance_flat_amount": flt(getattr(settings, "cost_tolerance_flat_amount", 0.0), 2),
		"disclaimer_text": disclaimer,
		"has_pending_services": has_pending_services,
		"can_authorize": can_authorize,
		"already_submitted": already_submitted,
		"authorized_by": getattr(ro_doc, "authorized_by", None),
		"authorized_at": str(ro_doc.authorized_at) if getattr(ro_doc, "authorized_at", None) else None,
		"approval_logs": approval_logs,
		"audit_hash": getattr(ro_doc, "audit_hash", None),
	}


def _build_payment_section(ro_doc: Any) -> dict[str, Any] | None:
	if ro_doc is None:
		return None
	return {
		"sdk_config": _to_plain_value(get_sdk_boot_config()),
		"state": _to_plain_value(get_ro_payment_state(ro_doc.name)),
	}


def _pick_primary_schedule_entry(entries: list[Any]) -> Any | None:
	if not entries:
		return None

	def _rank(se_doc: Any) -> tuple[int, str, str]:
		status = (_field(se_doc, "status") or "").strip()
		if status in _ACTIVE_SCHEDULED_SE_STATUSES:
			bucket = 3
		elif status == "In Progress":
			bucket = 2
		elif status not in _READ_ONLY_SE_STATUSES:
			bucket = 1
		else:
			bucket = 0
		sched_date = str(_field(se_doc, "scheduled_date") or "")
		modified = str(_field(se_doc, "modified") or "")
		return (bucket, sched_date, modified)

	return max(entries, key=_rank)


def _compute_action_badges(
	*,
	ro_doc: Any,
	is_read_only: bool,
	pin_state: dict[str, Any] | None,
	payment_state: dict[str, Any] | None,
) -> list[str]:
	badges: list[str] = []
	if ro_doc is None:
		return badges

	if not is_read_only:
		services = getattr(ro_doc, "services", None) or []
		if any((_field(svc, "status") or "") in _PENDING_SERVICE_STATUSES for svc in services):
			badges.append("Quote Approval Needed")
		if pin_state and pin_state.get("prompt_required"):
			badges.append("PIN-to-Drive Requested")

	if payment_state:
		pay_badge = payment_state.get("portal_payment_badge")
		if pay_badge == "Manual Deposit Requested" and not is_read_only:
			badges.append("Deposit Requested")
		elif pay_badge == "Balance Due":
			badges.append("Balance Due")

	return badges


def _visit_sort_key(record: dict[str, Any]) -> tuple[int, str, str]:
	is_active = 0 if record["is_read_only"] else 1
	ro_doc = record["ro_doc"]
	se_doc = record["se_doc"]
	primary_dt = ""
	if ro_doc is not None:
		primary_dt = str(getattr(ro_doc, "modified", "") or getattr(ro_doc, "creation", "") or "")
	elif se_doc is not None:
		primary_dt = str(getattr(se_doc, "modified", "") or getattr(se_doc, "creation", "") or "")
	return (is_active, primary_dt, record["visit_key"])


def build_portal_hub_payload(
	session: PortalSessionInfo,
	*,
	active_ro: str | None = None,
	active_schedule_entry: str | None = None,
) -> dict[str, Any]:
	"""Build complete customer-safe Visit Hub payload for an authenticated portal session.

	Enforces strict scope ownership on requested active_ro / active_schedule_entry targets,
	projects binary repair status only, serializes upstream P2/P3 structures to plain dicts,
	and guarantees zero forbidden keys across the entire payload.
	"""
	requested_ro: str | None = None
	if active_ro is not None and str(active_ro).strip():
		if not isinstance(active_ro, str):
			frappe.throw(_("You do not have permission to access this visit."), frappe.PermissionError)
		requested_ro = active_ro.strip()
		assert_session_owns_ro(session, requested_ro)

	requested_se: str | None = None
	if active_schedule_entry is not None and str(active_schedule_entry).strip():
		if not isinstance(active_schedule_entry, str):
			frappe.throw(_("You do not have permission to access this visit."), frappe.PermissionError)
		requested_se = active_schedule_entry.strip()
		assert_session_owns_schedule_entry(session, requested_se)

	branding = get_public_portal_branding()
	pickup_hours = branding.get("pickup_hours_text") or ""

	accessible = list_accessible_visit_keys(session)
	ro_names = accessible["repair_orders"]
	se_names = accessible["schedule_entries"]

	se_docs_by_name: dict[str, Any] = {}
	se_by_ro: dict[str, list[Any]] = {}
	for se_name in se_names:
		if not frappe.db.exists("Schedule Entry", se_name):
			continue
		se_doc = frappe.get_doc("Schedule Entry", se_name)
		se_docs_by_name[se_name] = se_doc
		linked_ro = (getattr(se_doc, "repair_order", None) or "").strip()
		if linked_ro:
			se_by_ro.setdefault(linked_ro, []).append(se_doc)

	claimed_se: set[str] = set()
	records: list[dict[str, Any]] = []

	for ro_name in ro_names:
		if not frappe.db.exists("Repair Order", ro_name):
			continue
		ro_doc = frappe.get_doc("Repair Order", ro_name)
		linked_entries = se_by_ro.get(ro_name, [])
		for entry in linked_entries:
			claimed_se.add(entry.name)
		primary_se = _pick_primary_schedule_entry(linked_entries)
		is_read_only = is_ro_mutating_locked(getattr(ro_doc, "status", None))
		status_obj = derive_binary_visit_status(
			ro_doc=ro_doc,
			schedule_entry_doc=primary_se,
			pickup_hours_text=pickup_hours,
		)
		vehicle_obj = _build_vehicle_section(ro_doc, primary_se)
		pin_state = _to_plain_value(get_ro_pin_customer_state(ro_doc))
		payment_state = _to_plain_value(get_ro_payment_state(ro_doc.name))
		badges = _compute_action_badges(
			ro_doc=ro_doc,
			is_read_only=is_read_only,
			pin_state=pin_state,
			payment_state=payment_state,
		)
		records.append({
			"visit_key": f"ro:{ro_doc.name}",
			"kind": "repair_order",
			"repair_order": ro_doc.name,
			"schedule_entry": primary_se.name if primary_se is not None else None,
			"linked_schedule_entries": {entry.name for entry in linked_entries},
			"ro_doc": ro_doc,
			"se_doc": primary_se,
			"is_read_only": is_read_only,
			"status": status_obj,
			"vehicle": vehicle_obj,
			"pin_state": pin_state,
			"payment_state": payment_state,
			"action_required_badges": badges,
		})

	for se_name, se_doc in se_docs_by_name.items():
		if se_name in claimed_se:
			continue
		is_read_only = (getattr(se_doc, "status", None) or "").strip() in _READ_ONLY_SE_STATUSES
		status_obj = derive_binary_visit_status(
			ro_doc=None,
			schedule_entry_doc=se_doc,
			pickup_hours_text=pickup_hours,
		)
		vehicle_obj = _build_vehicle_section(None, se_doc)
		records.append({
			"visit_key": f"se:{se_doc.name}",
			"kind": "booking",
			"repair_order": None,
			"schedule_entry": se_doc.name,
			"linked_schedule_entries": {se_doc.name},
			"ro_doc": None,
			"se_doc": se_doc,
			"is_read_only": is_read_only,
			"status": status_obj,
			"vehicle": vehicle_obj,
			"pin_state": None,
			"payment_state": None,
			"action_required_badges": [],
		})

	records.sort(key=_visit_sort_key, reverse=True)

	selected: dict[str, Any] | None = None
	if requested_ro:
		selected = next((rec for rec in records if rec["repair_order"] == requested_ro), None)
	elif requested_se:
		selected = next(
			(
				rec
				for rec in records
				if rec["schedule_entry"] == requested_se or requested_se in rec["linked_schedule_entries"]
			),
			None,
		)
	if selected is None and records:
		if session.repair_order:
			selected = next((rec for rec in records if rec["repair_order"] == session.repair_order), None)
		if selected is None and session.schedule_entry:
			selected = next(
				(
					rec
					for rec in records
					if rec["schedule_entry"] == session.schedule_entry
					or session.schedule_entry in rec["linked_schedule_entries"]
				),
				None,
			)
		if selected is None:
			selected = records[0]

	visits_list = [
		{
			"visit_key": rec["visit_key"],
			"kind": rec["kind"],
			"repair_order": rec["repair_order"],
			"schedule_entry": rec["schedule_entry"],
			"vehicle_label": rec["vehicle"]["label"],
			"status_code": rec["status"]["code"],
			"status_label": rec["status"]["label"],
			"is_read_only": rec["is_read_only"],
			"action_required_badges": rec["action_required_badges"],
		}
		for rec in records
	]

	active_visit: dict[str, Any] | None = None
	if selected is not None:
		ro_doc = selected["ro_doc"]
		se_doc = selected["se_doc"]
		is_read_only = selected["is_read_only"]
		payment_block = (
			{
				"sdk_config": _to_plain_value(get_sdk_boot_config()),
				"state": selected["payment_state"],
			}
			if ro_doc is not None
			else None
		)
		active_visit = {
			"visit_key": selected["visit_key"],
			"kind": selected["kind"],
			"repair_order": selected["repair_order"],
			"schedule_entry": selected["schedule_entry"],
			"is_read_only": is_read_only,
			"status": selected["status"],
			"vehicle": selected["vehicle"],
			"appointment": _build_appointment_section(se_doc),
			"quote": _build_quote_section(ro_doc, is_read_only=is_read_only),
			"pin_to_drive": selected["pin_state"],
			"payment": payment_block,
			"invoice": _to_plain_value(get_ro_invoice_summary(ro_doc)) if ro_doc is not None else None,
		}

	hub = {
		"session": {
			"auth_method": session.auth_method,
			"scope_type": session.scope_type,
			"phone_masked": mask_phone_e164(session.phone_e164) if session.phone_e164 else None,
			"can_upgrade_to_account_view": session.scope_type != "customer",
		},
		"shop": {
			"portal_enabled": branding["portal_enabled"],
			"company_name": branding["company_name"],
			"support_phone": branding["support_phone"],
			"pickup_hours_text": pickup_hours,
		},
		"visits": visits_list,
		"active_visit": active_visit,
	}
	return _to_plain_value(hub)
