# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Union

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


def _validate_required_package_ids(package_ids: Any) -> List[str]:
	"""Strictly validate non-empty package_ids; reject None, bool, dict, int, float, blank, or invalid JSON."""
	if package_ids is None or isinstance(package_ids, (bool, int, float, dict)):
		frappe.throw(_("At least one valid service package must be selected."), frappe.ValidationError)

	raw_items: List[str] = []
	if isinstance(package_ids, str):
		stripped = package_ids.strip()
		if not stripped:
			frappe.throw(_("At least one service package must be selected."), frappe.ValidationError)
		if stripped.startswith("{"):
			frappe.throw(_("Invalid package_ids parameter."), frappe.ValidationError)
		if stripped.startswith("["):
			try:
				parsed = json.loads(stripped)
			except json.JSONDecodeError:
				close_idx = stripped.find("]")
				prefix_tag = stripped[1:close_idx] if close_idx > 1 else ""
				is_bracket_prefixed_name = (
					close_idx > 1
					and close_idx < len(stripped) - 1
					and bool(prefix_tag.strip())
					and not any(ch in prefix_tag for ch in ('"', "'", ",", "[", "{"))
				)
				if not is_bracket_prefixed_name:
					frappe.throw(_("Invalid package_ids parameter."), frappe.ValidationError)
				raw_items = [part.strip() for part in stripped.split(",") if part.strip()]
			else:
				if not isinstance(parsed, list):
					frappe.throw(_("Invalid package_ids parameter."), frappe.ValidationError)
				for item in parsed:
					if not isinstance(item, str) or not item.strip():
						frappe.throw(_("Invalid package_ids parameter."), frappe.ValidationError)
					raw_items.append(item.strip())
		else:
			raw_items = [part.strip() for part in stripped.split(",") if part.strip()]
	elif isinstance(package_ids, (list, tuple, set)):
		for item in package_ids:
			if not isinstance(item, str) or not item.strip():
				frappe.throw(_("Invalid package_ids parameter."), frappe.ValidationError)
			raw_items.append(item.strip())
	else:
		frappe.throw(_("Invalid package_ids parameter."), frappe.ValidationError)

	deduped = list(dict.fromkeys(raw_items))
	if not deduped:
		frappe.throw(_("At least one service package must be selected."), frappe.ValidationError)
	return deduped


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
	validated_package_ids = _validate_required_package_ids(package_ids)

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
		package_ids=validated_package_ids,
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
	vehicle_color: Optional[str] = None,
	license_plate: Optional[str] = None,
	vin: Optional[str] = None,
	vin_or_plate: Optional[str] = None,
	notes: Optional[str] = None,
	waiter_preference: Optional[str] = None,
) -> Dict[str, Any]:
	"""Submit a provisional self-serve booking for an authenticated caller."""
	_ensure_authenticated_permission("Schedule Entry", "create")
	validated_package_ids = _validate_required_package_ids(package_ids)
	parsed_year = _parse_optional_int(vehicle_year, "vehicle_year")

	return create_provisional_booking(
		package_ids=validated_package_ids,
		target_date=target_date,
		customer_arrival_time=customer_arrival_time,
		customer_name=customer_name,
		phone=phone,
		email=email,
		vehicle_model=vehicle_model,
		vehicle_year=parsed_year,
		vehicle_color=vehicle_color,
		license_plate=license_plate,
		vin=vin,
		vin_or_plate=vin_or_plate,
		notes=notes,
		waiter_preference=waiter_preference,
	)
