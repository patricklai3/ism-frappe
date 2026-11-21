# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Union

import frappe
from frappe import _
from frappe.utils import cint

from ism.scheduling.bookable_catalog import _get_catalog_settings, list_bookable_packages
from ism.scheduling.self_serve import (
	create_provisional_booking,
	evaluate_reschedule_eligibility,
	find_self_serve_slots,
	get_required_tags_for_entry,
	get_self_serve_policy,
	reschedule_self_serve_appointment,
)

_MAX_PUBLIC_DAYS_AHEAD = 60


def _parse_optional_int(value: Any, field_label: str) -> Optional[int]:
	if value is None or (isinstance(value, str) and not value.strip()):
		return None
	if isinstance(value, bool):
		frappe.throw(_("{0} must be an integer.").format(field_label), frappe.ValidationError)
	try:
		return int(value)
	except (TypeError, ValueError):
		frappe.throw(_("{0} must be an integer.").format(field_label), frappe.ValidationError)


def _ensure_authenticated_permission(
	doctype: str,
	ptype: str,
	doc: Optional[str] = None,
) -> None:
	if frappe.session.user == "Guest":
		frappe.throw(_("Authentication is required to access this endpoint."), frappe.PermissionError)
	frappe.has_permission(doctype, ptype, doc=doc, throw=True)


@frappe.whitelist(allow_guest=True)
def get_bookable_packages(
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[Union[int, str]] = None,
	search: Optional[str] = None,
) -> Dict[str, Any]:
	"""Return read-only zero-PII curated self-serve packages plus non-sensitive policy flags."""
	parsed_year = _parse_optional_int(vehicle_year, "vehicle_year")
	packages = list_bookable_packages(
		vehicle_model=vehicle_model,
		vehicle_year=parsed_year,
		search=search,
	)
	policy = get_self_serve_policy()
	catalog_settings = _get_catalog_settings()

	return {
		"packages": packages,
		"policy": {
			"enable_waiter_preference": bool(policy["enable_waiter_preference"]),
			"horizon_days": int(policy["self_serve_horizon_days"]),
			"currency": catalog_settings.get("currency") or "CAD",
		},
	}


@frappe.whitelist(allow_guest=True)
def get_self_serve_slots(
	package_ids: Optional[Union[str, Sequence[str]]] = None,
	start_date: Optional[str] = None,
	days_ahead: Union[int, str] = 7,
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[Union[int, str]] = None,
	max_slots_per_day: Optional[Union[int, str]] = None,
) -> Dict[str, Any]:
	"""Return strictly sanitized customer-facing slots for curated self-serve packages."""
	if package_ids is None or (isinstance(package_ids, str) and not package_ids.strip()) or (
		isinstance(package_ids, (list, tuple, set)) and len(package_ids) == 0
	):
		frappe.throw(_("At least one service package must be selected."), frappe.ValidationError)

	policy = get_self_serve_policy()
	horizon_days = max(1, int(policy["self_serve_horizon_days"]))

	parsed_days = _parse_optional_int(days_ahead, "days_ahead")
	requested_days = 7 if parsed_days is None else parsed_days
	if requested_days <= 0:
		frappe.throw(_("days_ahead must be a positive integer."), frappe.ValidationError)
	capped_days = min(requested_days, horizon_days, _MAX_PUBLIC_DAYS_AHEAD)

	parsed_year = _parse_optional_int(vehicle_year, "vehicle_year")
	parsed_max_slots = _parse_optional_int(max_slots_per_day, "max_slots_per_day")
	if parsed_max_slots is not None and parsed_max_slots <= 0:
		frappe.throw(_("max_slots_per_day must be a positive integer."), frappe.ValidationError)

	return find_self_serve_slots(
		package_ids=package_ids,
		start_date=start_date,
		days_ahead=capped_days,
		vehicle_model=vehicle_model,
		vehicle_year=parsed_year,
		max_slots_per_day=parsed_max_slots,
	)


@frappe.whitelist()
def get_reschedule_policy_and_slots(
	schedule_entry: str,
	start_date: Optional[str] = None,
	days_ahead: Union[int, str] = 7,
	max_slots_per_day: Optional[Union[int, str]] = None,
) -> Dict[str, Any]:
	"""Return reschedule eligibility and candidate customer-facing slots for an authenticated caller."""
	_ensure_authenticated_permission("Schedule Entry", "read")
	if not schedule_entry or not isinstance(schedule_entry, str) or not schedule_entry.strip():
		frappe.throw(_("Schedule Entry is required."), frappe.ValidationError)
	se_name = schedule_entry.strip()
	if not frappe.db.exists("Schedule Entry", se_name):
		frappe.throw(_("Appointment '{0}' was not found.").format(se_name), frappe.DoesNotExistError)
	_ensure_authenticated_permission("Schedule Entry", "read", doc=se_name)

	se = frappe.get_doc("Schedule Entry", se_name)
	policy = get_self_serve_policy()
	horizon_days = max(1, int(policy["self_serve_horizon_days"]))

	parsed_days = _parse_optional_int(days_ahead, "days_ahead")
	requested_days = 7 if parsed_days is None else parsed_days
	if requested_days <= 0:
		frappe.throw(_("days_ahead must be a positive integer."), frappe.ValidationError)
	capped_days = min(requested_days, horizon_days, _MAX_PUBLIC_DAYS_AHEAD)

	parsed_max_slots = _parse_optional_int(max_slots_per_day, "max_slots_per_day")
	if parsed_max_slots is not None and parsed_max_slots <= 0:
		frappe.throw(_("max_slots_per_day must be a positive integer."), frappe.ValidationError)

	eligibility = evaluate_reschedule_eligibility(se)
	if eligibility["allowed"]:
		required_tags = get_required_tags_for_entry(se)
		duration = cint(se.estimated_duration) or int(policy["default_diagnostic_duration"])
		slots_result = find_self_serve_slots(
			duration_minutes=duration,
			required_tags=required_tags,
			start_date=start_date,
			days_ahead=capped_days,
			exclude_entry=se.name,
			max_slots_per_day=parsed_max_slots,
		)
	else:
		slots_result = {
			"start_date": start_date or eligibility["current_appointment"]["appointment_date"],
			"end_date": start_date or eligibility["current_appointment"]["appointment_date"],
			"horizon_days": horizon_days,
			"days": [],
			"total_slots": 0,
		}

	return {
		"schedule_entry": se.name,
		"eligibility": eligibility,
		"slots": slots_result,
	}


@frappe.whitelist()
def execute_self_serve_reschedule(
	schedule_entry: str,
	target_date: str,
	customer_arrival_time: str,
	reason: Optional[str] = None,
) -> Dict[str, Any]:
	"""Execute an atomic self-serve appointment reschedule for an authenticated caller."""
	_ensure_authenticated_permission("Schedule Entry", "write")
	if not schedule_entry or not isinstance(schedule_entry, str) or not schedule_entry.strip():
		frappe.throw(_("Schedule Entry is required."), frappe.ValidationError)
	se_name = schedule_entry.strip()
	if not frappe.db.exists("Schedule Entry", se_name):
		frappe.throw(_("Appointment '{0}' was not found.").format(se_name), frappe.DoesNotExistError)
	_ensure_authenticated_permission("Schedule Entry", "write", doc=se_name)

	return reschedule_self_serve_appointment(
		se_name,
		target_date,
		customer_arrival_time,
		reason=reason,
	)


@frappe.whitelist()
def submit_provisional_booking(
	package_ids: Union[str, Sequence[str]],
	target_date: str,
	customer_arrival_time: str,
	customer_name: str,
	phone: str,
	email: Optional[str] = None,
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[Union[int, str]] = None,
	vin_or_plate: Optional[str] = None,
	notes: Optional[str] = None,
	waiter_preference: Optional[str] = None,
) -> Dict[str, Any]:
	"""Submit a provisional self-serve booking for an authenticated caller."""
	_ensure_authenticated_permission("Schedule Entry", "create")
	parsed_year = _parse_optional_int(vehicle_year, "vehicle_year")

	return create_provisional_booking(
		package_ids=package_ids,
		target_date=target_date,
		customer_arrival_time=customer_arrival_time,
		customer_name=customer_name,
		phone=phone,
		email=email,
		vehicle_model=vehicle_model,
		vehicle_year=parsed_year,
		vin_or_plate=vin_or_plate,
		notes=notes,
		waiter_preference=waiter_preference,
	)
