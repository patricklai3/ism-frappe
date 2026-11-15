# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_url, getdate, now_datetime, today

from ism.api.agent_serializers import (
	build_agent_envelope,
	build_alternates_summary,
	build_appointment_summary,
	build_catalog_summary,
	build_estimate_summary,
	build_floor_summary,
	build_inspection_summary,
	build_quote_summary,
	build_ro_urls,
	build_slots_summary,
	build_vehicle_intel_summary,
	build_vin_decode_summary,
	coerce_bool,
	normalize_condition,
	parse_json_arg,
	serialize_repair_order_compact,
	serialize_service_package_compact,
)
from ism.api.approval import send_approval_request
from ism.api.estimation_service import estimate_repair_order, get_total_estimate
from ism.api.inspection import (
	apply_inspection_template as _apply_inspection_template_impl,
	get_check_in_inspection as _get_check_in_inspection_impl,
	get_inspection_template as _get_inspection_template_impl,
	list_inspection_templates as _list_inspection_templates_impl,
	record_check_in_inspection as _record_check_in_inspection_impl,
)
from ism.api.parts import (
	bulk_import_parts,
	ensure_supplier_and_link_to_item,
	get_component_alternates as _get_component_alternates_impl,
	get_repair_order_parts_stock,
	quick_create_part,
	submit_procurement_orders as _submit_procurement_orders_impl,
)
from ism.api.scheduling import (
	allocate_schedule_entry,
	check_bay_availability,
	create_repair_order_from_schedule_entry,
	get_unscheduled_services as _get_unscheduled_services_impl,
	resolve_service_bay_identifier,
	resolve_technician_identifier,
)
from ism.scheduling.capacity import NON_BLOCKING_ENTRY_STATUSES, ShopCapacityWindow, load_bay_tags_map
from ism.scheduling.time_utils import get_shop_settings, normalize_date, normalize_time
from ism.api.technician_availability import (
	get_active_technicians,
	get_daily_technician_overview,
)
from ism.api.tesla import (
	decode_vehicle_vin as _decode_vehicle_vin_impl,
	fetch_and_parse_tesla_procedure,
	get_vehicle_intelligence as _get_vehicle_intelligence_impl,
	import_tesla_procedure as _import_tesla_ro_procedure,
	link_vehicle_customer as _link_vehicle_customer_impl,
	search_vehicles as _search_vehicles_impl,
)
from ism.ism.doctype.repair_order.repair_order import (
	_get_default_labor_rate,
	build_service_package_row_fields,
	get_deferred_services_for_vehicle,
)
from ism.ism.doctype.shop_action_item.shop_action_item import (
	create_shop_action_item as _create_shop_action_item_impl,
	resolve_action_item as _resolve_action_item_impl,
)


def _require_str(value: Any, label: str) -> str:
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("{0} must be a valid non-empty string.").format(label), frappe.ValidationError)
	return value.strip()


def _optional_str(value: Any, label: str) -> Optional[str]:
	if value is None:
		return None
	if not isinstance(value, str):
		frappe.throw(_("{0} must be a string.").format(label), frappe.ValidationError)
	cleaned = value.strip()
	return cleaned or None


def _require_non_negative_int(value: Any, label: str) -> int:
	"""Validate and return a mandatory non-negative integer field."""
	if value is None or (isinstance(value, str) and not value.strip()):
		frappe.throw(_("{0} is required.").format(label), frappe.MandatoryError)
	if isinstance(value, bool):
		frappe.throw(_("{0} must be a non-negative integer.").format(label), frappe.ValidationError)
	if isinstance(value, int):
		parsed = value
	elif isinstance(value, float):
		if not value.is_integer():
			frappe.throw(_("{0} must be a non-negative integer.").format(label), frappe.ValidationError)
		parsed = int(value)
	elif isinstance(value, str):
		try:
			parsed = int(value.strip())
		except ValueError:
			frappe.throw(_("{0} must be a non-negative integer.").format(label), frappe.ValidationError)
	else:
		frappe.throw(_("{0} must be a non-negative integer.").format(label), frappe.ValidationError)
	if parsed < 0:
		frappe.throw(_("{0} cannot be negative.").format(label), frappe.ValidationError)
	return parsed


def _resolve_odometer_out(ro: Any, odometer_out: Any, action_label: str) -> int:
	"""Validate odometer_out against explicit input or existing positive RO value."""
	if odometer_out is not None and not (isinstance(odometer_out, str) and not odometer_out.strip()):
		validated = _require_non_negative_int(odometer_out, "Odometer Out")
	elif cint(getattr(ro, "odometer_out", None) or 0) > 0:
		validated = _require_non_negative_int(ro.odometer_out, "Odometer Out")
	else:
		frappe.throw(_("Odometer Out is required to {0}.").format(action_label), frappe.MandatoryError)
	if validated < cint(getattr(ro, "odometer_in", None) or 0):
		frappe.throw(
			_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(
				validated, cint(getattr(ro, "odometer_in", None) or 0)
			),
			frappe.ValidationError,
		)
	return validated


def _get_default_company() -> str:
	"""Retrieve active company for shop operations."""
	return (
		frappe.db.get_single_value("Global Defaults", "default_company")
		or frappe.db.get_value("Company", {"is_group": 0}, "name")
		or "Lightspeed"
	)


def _resolve_customer(customer_name: str, phone: Optional[str] = None, email: Optional[str] = None) -> str:
	"""Find an existing Customer or provision a new one with optional contact info."""
	frappe.has_permission("Customer", "read", throw=True)
	if not customer_name:
		customer_name = "Walk-in Customer"

	existing = (
		customer_name
		if frappe.db.exists("Customer", customer_name)
		else frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
	)
	if existing:
		frappe.has_permission("Customer", "read", doc=existing, throw=True)
		updates: Dict[str, Any] = {}
		if phone and not frappe.db.get_value("Customer", existing, "mobile_no"):
			updates["mobile_no"] = phone
		if email and not frappe.db.get_value("Customer", existing, "email_id"):
			updates["email_id"] = email
		if updates:
			frappe.has_permission("Customer", "write", doc=existing, throw=True)
			frappe.db.set_value("Customer", existing, updates)
		return existing

	frappe.has_permission("Customer", "create", throw=True)
	cust_group = "Individual" if frappe.db.exists("Customer Group", "Individual") else "All Customer Groups"
	territory = "All Territories" if frappe.db.exists("Territory", "All Territories") else frappe.db.get_value("Territory", {}, "name")

	payload: Dict[str, Any] = {
		"doctype": "Customer",
		"customer_name": customer_name,
		"customer_type": "Individual",
		"customer_group": cust_group,
		"territory": territory,
	}
	if phone:
		payload["mobile_no"] = phone
	if email:
		payload["email_id"] = email

	doc = frappe.get_doc(payload).insert()
	return doc.name


def _lookup_vehicle(cleaned: str) -> Optional[str]:
	if not isinstance(cleaned, str) or not cleaned.strip():
		return None
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	cleaned = cleaned.strip()
	return (
		frappe.db.get_value("Repair Vehicle", {"name": cleaned}, "name")
		or frappe.db.get_value("Repair Vehicle", {"license_plate": cleaned}, "name")
		or frappe.db.get_value("Repair Vehicle", {"vin": cleaned}, "name")
	)


def _get_ro_doc(repair_order: str, ptype: str = "read") -> Any:
	repair_order = _require_str(repair_order, "Repair Order")
	frappe.has_permission("Repair Order", ptype, throw=True)
	if not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(_("Repair Order {0} does not exist.").format(repair_order), frappe.DoesNotExistError)
	ro = frappe.get_doc("Repair Order", repair_order)
	ro.check_permission(ptype)
	return ro


def _serialize_pending_deferred(vehicle: str, ro: Any = None) -> List[Dict[str, Any]]:
	"""Return compact open deferred services for a vehicle, excluding titles already on ro."""
	existing_titles = {
		str(getattr(s, "service_title", "") or "").strip().lower()
		for s in (getattr(ro, "services", None) or [])
		if getattr(s, "service_title", None)
	}
	items = []
	for d in get_deferred_services_for_vehicle(vehicle) or []:
		title = str(d.get("job_title") or "").strip()
		if not title or title.lower() in existing_titles:
			continue
		items.append({
			"job_title": title,
			"estimated_amount": flt(d.get("estimated_amount"), 2),
			"decline_date": str(d.get("decline_date") or ""),
			"decline_reason": d.get("decline_reason") or "",
			"safety_hazard": cint(d.get("safety_hazard", 0)),
			"repair_order": d.get("repair_order"),
		})
	return items


def _resolve_vehicle(vehicle_identifier: str, customer_name: str) -> str:
	"""Find an existing Repair Vehicle by name, plate, or VIN, or create one safely."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	cleaned = (vehicle_identifier or "").strip()
	if not cleaned:
		cleaned = f"PLATE-{frappe.generate_hash(length=10).upper()}"

	veh = _lookup_vehicle(cleaned)
	if veh:
		frappe.has_permission("Repair Vehicle", "read", doc=veh, throw=True)
		return veh

	frappe.has_permission("Repair Vehicle", "create", throw=True)
	is_vin = len(cleaned) == 17
	vin = cleaned.upper() if is_vin else f"PLATE-{frappe.generate_hash(length=10).upper()}"
	plate = "" if is_vin else cleaned

	v_doc = frappe.get_doc({
		"doctype": "Repair Vehicle",
		"vin": vin,
		"license_plate": plate,
		"customer": customer_name,
		"manufacturer": "Tesla",
		"model": "Model 3",
	}).insert()
	return v_doc.name


def _resolve_and_validate_parts(raw_parts: Any) -> List[Dict[str, Any]]:
	"""Validate part item_codes against Item master (resolving part_number aliases when unambiguous)."""
	frappe.has_permission("Item", "read", throw=True)
	parsed = parse_json_arg(raw_parts, []) if isinstance(raw_parts, str) else raw_parts
	if not parsed:
		return []
	if not isinstance(parsed, list):
		frappe.throw(_("Service parts must be a list of part dicts."), frappe.ValidationError)

	resolved: List[Dict[str, Any]] = []
	for entry in parsed:
		if not isinstance(entry, dict):
			continue
		part = dict(entry)
		code = str(part.get("item_code") or "").strip()
		if not code:
			frappe.throw(_("Each part entry must include 'item_code'."), frappe.ValidationError)
		if not frappe.db.exists("Item", code):
			matched = frappe.get_list("Item", filters={"oem_part_number": code}, pluck="name", limit=2)
			if not matched:
				matched = frappe.get_list(
					"Item",
					filters={"name": ["like", f"%-{code.upper()}-%"]},
					pluck="name",
					limit=2,
				)
			if len(matched) == 1:
				part["item_code"] = matched[0]
			elif len(matched) > 1:
				frappe.throw(
					_("Multiple Items match part number '{0}': {1}. Specify exact item_code.").format(
						code, ", ".join(matched)
					),
					frappe.ValidationError,
				)
			else:
				frappe.throw(
					_("Item '{0}' does not exist in catalog. Ingest or create it first.").format(code),
					frappe.ValidationError,
				)
		resolved.append(part)
	return resolved


def _resolve_service_package_name(raw_name: Optional[str]) -> Optional[str]:
	if not raw_name:
		return None
	return (
		frappe.db.get_value("Service Package", {"name": raw_name}, "name")
		or frappe.db.get_value("Service Package", {"package_name": raw_name}, "name")
	)


_SERVICE_TITLE_ALIASES: Tuple[str, ...] = (
	"service_title",
	"title",
	"name",
	"service_name",
	"service",
	"operation",
)
_SERVICE_HOURS_ALIASES: Tuple[str, ...] = (
	"labor_hours",
	"custom_frt",
	"hours",
	"frt",
)


def _first_present(mapping: Dict[str, Any], keys: Tuple[str, ...], *, strip_str: bool = False) -> Any:
	for key in keys:
		val = mapping.get(key)
		if isinstance(val, str):
			if strip_str:
				val = val.strip()
			if not val.strip():
				continue
		if val is not None:
			return val
	return None


def _build_service_row(
	service_input: Any,
	default_labor_rate: float,
	company: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
	"""Normalize a single service specification into a Repair Order Service row dict."""
	import json

	if isinstance(service_input, str):
		pkg_name = _resolve_service_package_name(service_input)
		if pkg_name:
			pkg = frappe.get_doc("Service Package", pkg_name)
			return build_service_package_row_fields(
				pkg=pkg,
				company=company,
				default_labor_rate=default_labor_rate,
			)
		return {
			"service_title": service_input,
			"labor_hours": 1.0,
			"labor_rate": default_labor_rate,
		}

	if not isinstance(service_input, dict):
		return None

	explicit_title = _first_present(service_input, _SERVICE_TITLE_ALIASES, strip_str=True)
	explicit_hours = _first_present(service_input, _SERVICE_HOURS_ALIASES, strip_str=True)
	pkg_lookup = service_input.get("service_package") or explicit_title
	pkg_name = _resolve_service_package_name(pkg_lookup)

	if pkg_name:
		pkg = frappe.get_doc("Service Package", pkg_name)
		s_data = build_service_package_row_fields(
			pkg=pkg,
			company=company,
			default_labor_rate=default_labor_rate,
		)
		if explicit_title:
			s_data["service_title"] = explicit_title
		if service_input.get("service_position") is not None:
			s_data["service_position"] = service_input["service_position"]
		if service_input.get("correction_code") is not None:
			s_data["correction_code"] = service_input["correction_code"]

		has_explicit_flat = "is_flat_rate" in service_input and service_input.get("is_flat_rate") is not None
		if has_explicit_flat:
			s_data["is_flat_rate"] = cint(service_input["is_flat_rate"])

		has_explicit_hours = explicit_hours is not None
		if has_explicit_hours:
			s_data["labor_hours"] = flt(explicit_hours)

		has_explicit_rate = service_input.get("labor_rate") is not None
		if has_explicit_rate:
			s_data["labor_rate"] = flt(service_input["labor_rate"])

		if has_explicit_hours or has_explicit_rate or (has_explicit_flat and not s_data.get("is_flat_rate")):
			if not cint(s_data.get("is_flat_rate")):
				s_data["labor_amount"] = flt(flt(s_data["labor_hours"]) * flt(s_data["labor_rate"]), 2)

		if service_input.get("parts"):
			validated_parts = _resolve_and_validate_parts(service_input["parts"])
			s_data["parts"] = json.dumps(validated_parts)
			s_data["parts_total"] = sum(
				flt(p.get("amount") if p.get("amount") is not None else flt(p.get("qty", 1)) * flt(p.get("rate", 0)), 2)
				for p in validated_parts
			)
		s_data["total_amount"] = flt(flt(s_data.get("labor_amount", 0)) + flt(s_data.get("parts_total", 0)), 2)
		return s_data

	s_data: Dict[str, Any] = {
		"service_title": explicit_title or "Service Operation",
		"service_package": None,
		"labor_hours": flt(explicit_hours) if explicit_hours is not None else 1.0,
		"labor_rate": flt(service_input.get("labor_rate") or default_labor_rate),
		"is_flat_rate": cint(service_input.get("is_flat_rate", 0)),
		"service_position": service_input.get("service_position", "None"),
	}
	if service_input.get("parts"):
		validated_parts = _resolve_and_validate_parts(service_input["parts"])
		s_data["parts"] = json.dumps(validated_parts)
	return s_data


def _estimate_duration_minutes(ro: Any) -> int:
	"""Estimate Repair Order duration in minutes with safe fallback."""
	try:
		est = estimate_repair_order(ro.name)
		return int(est.get("total_duration_minutes", 60))
	except Exception:
		return int(sum(flt(s.labor_hours) * 60 for s in ro.services)) or 60


@frappe.whitelist()
def create_quote(
	customer_name: Optional[str] = None,
	vehicle_identifier: Optional[str] = None,
	customer: Optional[str] = None,
	vehicle: Optional[str] = None,
	services: Optional[Union[List[Any], str]] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	concern: Optional[str] = None,
	customer_phone: Optional[str] = None,
	customer_email: Optional[str] = None,
	company: Optional[str] = None,
	odometer_in: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create a new Repair Order in Draft Estimate state with server-computed totals."""
	import json

	frappe.has_permission("Repair Order", "create", throw=True)
	cust_in = _optional_str(customer_name, "customer_name") or _optional_str(customer, "customer")
	veh_in = _optional_str(vehicle_identifier, "vehicle_identifier") or _optional_str(vehicle, "vehicle")
	notes_in = notes or concern

	if not cust_in:
		frappe.throw(_("Customer name or customer identifier is required."))
	if not veh_in:
		frappe.throw(_("Vehicle identifier is required."))

	try:
		parsed_services = parse_json_arg(services, [])
	except Exception:
		parsed_services = [{"service_title": services}]

	try:
		parsed_parts = parse_json_arg(parts, [])
	except Exception:
		parsed_parts = []

	validated_odometer_in = _require_non_negative_int(odometer_in, "Odometer In")
	notes_clean = notes_in.strip() if isinstance(notes_in, str) else ""
	if not parsed_services and not notes_clean:
		frappe.throw(
			_("At least one service or customer concern/notes is required."),
			frappe.MandatoryError,
		)

	comp = company or _get_default_company()
	cust = _resolve_customer(cust_in, phone=customer_phone, email=customer_email)
	veh = _resolve_vehicle(veh_in, cust)

	ro = frappe.new_doc("Repair Order")
	ro.company = comp
	ro.customer = cust
	ro.vehicle = veh
	ro.status = "Draft Estimate"
	ro.approval_status = "Estimating"
	ro.customer_concern = notes_in or ""
	ro.posting_date = today()
	ro.odometer_in = validated_odometer_in

	default_labor_rate = _get_default_labor_rate()
	if not parsed_services:
		parsed_services = [{"service_title": "Initial Inspection & Diagnostics", "labor_hours": 1.0}]

	for item in parsed_services:
		row = _build_service_row(item, default_labor_rate, company=comp)
		if row:
			ro.append("services", row)

	if parsed_parts and ro.services:
		first_s = ro.services[0]
		try:
			existing_p = parse_json_arg(first_s.parts, []) or []
		except Exception:
			existing_p = []
		existing_p.extend(_resolve_and_validate_parts(parsed_parts))
		first_s.parts = json.dumps(existing_p)

	ro.insert()
	duration_min = _estimate_duration_minutes(ro)
	pending_deferred = _serialize_pending_deferred(veh, ro=ro)

	data = serialize_repair_order_compact(ro)
	data["total_duration_minutes"] = duration_min
	data["deferred_count"] = len(pending_deferred)
	msg = _("Quote {0} created successfully.").format(ro.name)
	if pending_deferred:
		data["deferred_services"] = pending_deferred
		msg += " " + _(
			"Found {0} open deferred service(s) for vehicle {1}; prompt user to add them."
		).format(len(pending_deferred), veh)

	return build_agent_envelope(
		success=True,
		data=data,
		urls=build_ro_urls(ro.name),
		message=msg,
		include_summary=include_summary,
		summary_builder=lambda: build_quote_summary(ro, duration_min, deferred_services=pending_deferred),
	)


@frappe.whitelist()
def amend_quote(
	repair_order: str,
	add_services: Optional[Union[List[Any], str]] = None,
	remove_service_indices: Optional[Union[List[int], str]] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Safely amend lines on an unapproved Repair Order in Draft Estimate state."""
	ro = _get_ro_doc(repair_order, ptype="write")
	if ro.approval_status in ["Approved", "Partially Approved"] or ro.docstatus != 0:
		frappe.throw(_("Cannot amend an approved or submitted Repair Order. Customer re-authorization is legally required."))

	try:
		parsed_add = parse_json_arg(add_services, [])
	except Exception:
		parsed_add = [{"service_title": add_services}]

	try:
		parsed_remove = parse_json_arg(remove_service_indices, [])
	except Exception:
		parsed_remove = []

	if parsed_remove:
		for idx in sorted([int(i) for i in parsed_remove], reverse=True):
			if 0 <= idx < len(ro.services):
				ro.services.pop(idx)

	default_labor_rate = _get_default_labor_rate()
	if parsed_add:
		for item in parsed_add:
			row = _build_service_row(item, default_labor_rate, company=ro.company)
			if row:
				ro.append("services", row)

	if notes:
		ro.customer_concern = notes

	ro.save()
	duration_min = _estimate_duration_minutes(ro)

	data = serialize_repair_order_compact(ro)
	data["total_duration_minutes"] = duration_min
	return build_agent_envelope(
		success=True,
		data=data,
		urls=build_ro_urls(ro.name),
		message=_("Quote {0} updated successfully.").format(ro.name),
		include_summary=include_summary,
		summary_builder=lambda: build_quote_summary(ro, duration_min, "Repair Order Quote (Updated)"),
	)


def _build_conflict_summary(heading: str, conflict: Dict[str, Any]) -> str:
	lines = [
		f"### {heading}",
		f"- **Reason**: {conflict.get('message')}",
		f"- **Requested**: `{conflict.get('requested_date')}` `{conflict.get('requested_time')}`",
	]
	alts = conflict.get("alternative_slots") or []
	if alts:
		lines.append("- **Nearest Open Alternatives**:")
		for alt in alts[:5]:
			lines.append(f"  - `{alt.get('date')}` `{alt.get('start_time')}` (Bay: `{alt.get('service_bay')}`)")
	return "\n".join(lines)


@frappe.whitelist()
def find_available_slots(
	repair_order: Optional[str] = None,
	duration_minutes: Optional[int] = None,
	start_date: Optional[str] = None,
	target_date: Optional[str] = None,
	days_ahead: int = 3,
	equipment_tags: Optional[Union[List[str], str]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	include_summary: Any = False,
	include_resources: Any = False,
	max_slots: int = 15,
	include_past: Any = False,
	min_start_time: Optional[str] = None,
) -> Dict[str, Any]:
	"""Query dual-resource capacity for available booking slots across shop operating hours."""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)
	repair_order = _optional_str(repair_order, "Repair Order")
	service_bay = resolve_service_bay_identifier(_optional_str(service_bay, "Service Bay"))
	technician = resolve_technician_identifier(_optional_str(technician, "Technician"))
	dur = 60
	estimate_meta: Optional[Dict[str, Any]] = None
	if duration_minutes:
		dur = max(15, int(duration_minutes))
	elif repair_order and frappe.db.exists("Repair Order", repair_order):
		_get_ro_doc(repair_order, ptype="read")
		estimate_meta = estimate_repair_order(repair_order)
		dur = estimate_meta.get("total_duration_minutes", 60)

	if isinstance(equipment_tags, str):
		try:
			equipment_tags = parse_json_arg(equipment_tags, [])
		except Exception:
			equipment_tags = [equipment_tags]

	norm_start = normalize_date(start_date) if start_date else None
	norm_target = normalize_date(target_date) if target_date else None
	norm_min_time = normalize_time(min_start_time) if min_start_time else None

	anchor = norm_start or norm_target or today()
	start_dt = getdate(anchor)
	span = max(1, int(days_ahead or 1))
	end_dt = add_days(start_dt, span - 1)
	show_resources = coerce_bool(include_resources)
	allow_past = coerce_bool(include_past)
	cap_limit = cint(max_slots)
	now_dt = now_datetime()
	today_dt = getdate(now_dt)
	now_time_str = now_dt.strftime("%H:%M:%S")

	window = ShopCapacityWindow(
		start_date=start_dt,
		end_date=end_dt,
		required_tags=equipment_tags,
		service_bay=service_bay,
		skip_holidays=True,
	)

	results: List[Dict[str, Any]] = []
	for cur_dt in window.iter_dates():
		date_str = str(cur_dt)
		day_min_time: Optional[str] = None
		if norm_min_time and cur_dt == start_dt:
			day_min_time = norm_min_time
		elif not allow_past and cur_dt == today_dt:
			day_min_time = now_time_str

		remaining = (cap_limit - len(results)) if (cap_limit > 0 and not technician) else None
		day_slots = window.slots_for_date(
			cur_dt,
			dur,
			max_slots=remaining,
			min_start_time=day_min_time,
		)
		for slot in day_slots or []:
			if technician and "available_technician_ids" in slot:
				if technician not in (slot.get("available_technician_ids") or []):
					continue
			slot_row: Dict[str, Any] = {
				"date": date_str,
				"start_time": slot.get("start_time"),
				"available_bays": slot.get("available_bays"),
				"available_technicians": slot.get("available_technicians"),
				"effective_capacity": slot.get("effective_capacity"),
				"bottleneck": slot.get("bottleneck"),
			}
			if "available_bay_ids" in slot:
				slot_row["available_bay_ids"] = slot["available_bay_ids"]
			if (show_resources or technician) and "available_technician_ids" in slot:
				slot_row["available_technician_ids"] = slot["available_technician_ids"]
			if technician and "available_technician_ids" in slot:
				slot_row["matched_technician"] = technician
			results.append(slot_row)
			if cap_limit > 0 and len(results) >= cap_limit:
				break
		if cap_limit > 0 and len(results) >= cap_limit:
			break

	data: Dict[str, Any] = {
		"duration_minutes": dur,
		"slots": results,
		"resources": {
			"bays": {b: window.bay_tags.get(b, []) for b in window.candidate_bays},
			"technicians": dict(window.tech_names),
		},
	}
	if service_bay:
		data["service_bay"] = service_bay
	if technician:
		data["technician"] = technician
	if window.holidays:
		data["skipped_holidays"] = sorted(window.holidays.keys())
	if estimate_meta:
		data["duration_breakdown"] = {
			"p_pure": estimate_meta.get("p_pure"),
			"buffer_padding": estimate_meta.get("buffer_padding"),
			"operations_count": estimate_meta.get("operations_count"),
		}

	return build_agent_envelope(
		success=True,
		data=data,
		message=_("Found {0} available slots.").format(len(results)),
		include_summary=include_summary,
		summary_builder=lambda: build_slots_summary(results, dur),
	)


def _infer_schedule_entry_type(ro: Any, entry_type: Optional[str] = None) -> str:
	if entry_type:
		return entry_type
	services = getattr(ro, "services", None) or []
	has_repair_work = any(
		getattr(s, "service_package", None)
		or not any(tok in (getattr(s, "service_title", None) or "").lower() for tok in ("diagnos", "inspect", "check"))
		for s in services
	)
	if has_repair_work and frappe.db.exists("Schedule Entry Type", "Repair"):
		return "Repair"
	return "Diagnostic"


def _build_ambiguous_bay_availability_message(
	service_bay: str,
	scheduled_date: str,
	start_time: str,
	estimated_duration: Optional[int],
	repair_order: Optional[str],
) -> Optional[str]:
	"""Annotate ambiguous bay candidates with equipment tags and [Available]/[Busy] status."""
	try:
		if estimated_duration:
			dur = max(15, int(estimated_duration))
		elif repair_order and frappe.db.exists("Repair Order", repair_order):
			ro = _get_ro_doc(repair_order, ptype="read")
			dur = _estimate_duration_minutes(ro)
		else:
			settings = get_shop_settings()
			dur = max(15, int(settings.get("default_diagnostic_duration") or 60))

		lowered = service_bay.strip().lower()
		bays = frappe.get_all(
			"Service Bay",
			fields=["name", "bay_name", "is_active"],
			order_by="is_active desc, name asc",
		)
		partial_matches = [
			b for b in bays
			if lowered in (b.get("name") or "").lower() or lowered in (b.get("bay_name") or "").lower()
		]
		active_partial = [b for b in partial_matches if cint(b.get("is_active"))]
		candidates = (active_partial or partial_matches)[:5]
		if len(candidates) <= 1:
			return None

		active_names = [c["name"] for c in candidates if cint(c.get("is_active"))]
		tags_by_bay = load_bay_tags_map(active_names)
		formatted: List[str] = []
		for c in candidates:
			bay_id = c["name"]
			tags = sorted(tags_by_bay.get(bay_id) or [])
			label = f"{bay_id} [{', '.join(tags)}]" if tags else bay_id
			if cint(c.get("is_active")):
				is_free = check_bay_availability(bay_id, scheduled_date, start_time, dur)
				label += " [Available]" if is_free else " [Busy]"
			formatted.append(label)

		return _("Ambiguous Service Bay '{0}'. Matching bays: {1}.").format(
			service_bay.strip(), ", ".join(formatted)
		)
	except Exception:
		return None


def _resolve_bay_for_booking(
	service_bay: str,
	scheduled_date: str,
	start_time: str,
	estimated_duration: Optional[int],
	repair_order: Optional[str],
) -> Optional[str]:
	try:
		return resolve_service_bay_identifier(service_bay)
	except frappe.ValidationError as err:
		if "Ambiguous Service Bay" not in str(err) or not scheduled_date or not start_time:
			raise
		annotated_msg = _build_ambiguous_bay_availability_message(
			service_bay,
			scheduled_date=scheduled_date,
			start_time=start_time,
			estimated_duration=estimated_duration,
			repair_order=repair_order,
		)
		if not annotated_msg:
			raise
		frappe.throw(annotated_msg, frappe.ValidationError)


@frappe.whitelist()
def book_appointment(
	repair_order: Optional[str] = None,
	scheduled_date: str = "",
	start_time: str = "",
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	entry_type: Optional[str] = None,
	notes: Optional[str] = None,
	estimated_duration: Optional[int] = None,
	provisional_customer_name: Optional[str] = None,
	provisional_vehicle_info: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Allocate a linked or standalone Schedule Entry and broadcast ambient realtime update."""
	frappe.has_permission("Schedule Entry", "create", throw=True)
	repair_order = _optional_str(repair_order, "Repair Order")
	service_bay = _optional_str(service_bay, "Service Bay")
	technician = _optional_str(technician, "Technician")
	entry_type = _optional_str(entry_type, "Entry Type")
	scheduled_date = normalize_date(scheduled_date) or scheduled_date
	start_time = normalize_time(start_time) or start_time
	if service_bay:
		frappe.has_permission("Service Bay", "read", throw=True)
		service_bay = _resolve_bay_for_booking(
			service_bay,
			scheduled_date=scheduled_date,
			start_time=start_time,
			estimated_duration=estimated_duration,
			repair_order=repair_order,
		)
	if technician:
		technician = resolve_technician_identifier(technician)
	ro = None
	if repair_order:
		ro = _get_ro_doc(repair_order, ptype="read")
		dur = max(15, int(estimated_duration)) if estimated_duration else _estimate_duration_minutes(ro)
		resolved_entry_type = _infer_schedule_entry_type(ro, entry_type=entry_type)
		booking_notes = notes or ro.customer_concern
		summary_subject = f"**Repair Order**: `{ro.name}` ({ro.customer})"
	else:
		resolved_entry_type = entry_type or "Diagnostic"
		if frappe.db.exists("Schedule Entry Type", resolved_entry_type):
			requires_ro = frappe.db.get_value("Schedule Entry Type", resolved_entry_type, "requires_repair_order")
			if cint(requires_ro):
				frappe.throw(
					_("Schedule Entry of type '{0}' requires a linked Repair Order.").format(resolved_entry_type),
					frappe.MandatoryError,
				)
		settings = get_shop_settings()
		dur = max(15, int(estimated_duration or settings.get("default_diagnostic_duration") or 60))
		booking_notes = notes
		customer_label = provisional_customer_name or "Walk-In Diagnostic"
		summary_subject = f"**Provisional Customer**: {customer_label}"
		if provisional_vehicle_info:
			summary_subject += f" ({provisional_vehicle_info})"

	# ScheduleEntry.on_update already publishes ism_calendar_update once on save/insert.
	entry_dict = allocate_schedule_entry(
		repair_order=ro.name if ro else None,
		entry_type=resolved_entry_type,
		scheduled_date=scheduled_date,
		scheduled_time=start_time,
		estimated_duration=dur,
		service_bay=service_bay,
		assigned_technician=technician,
		notes=booking_notes,
		provisional_customer_name=provisional_customer_name if not ro else None,
		provisional_vehicle_info=provisional_vehicle_info if not ro else None,
	)
	if entry_dict.get("status") == "conflict" or entry_dict.get("conflict"):
		return build_agent_envelope(
			success=False,
			urls=entry_dict.get("urls") or {"calendar": f"{get_url()}/app/schedule-entry/view/calendar"},
			data=entry_dict,
			message=entry_dict.get("message") or _("Scheduling conflict detected."),
			error=entry_dict.get("message") or _("Scheduling conflict detected."),
			include_summary=include_summary,
			summary_builder=lambda: _build_conflict_summary("Appointment Conflict", entry_dict),
		)

	entry_dict["entry_type"] = resolved_entry_type
	entry_dict["estimated_duration"] = dur

	base_url = get_url()
	cal_url = f"{base_url}/app/schedule-entry/view/calendar"
	se_name = entry_dict.get("name")
	desk_url = f"{base_url}/app/schedule-entry/{se_name}"

	return build_agent_envelope(
		success=True,
		urls={"calendar": cal_url, "desk": desk_url},
		data=entry_dict,
		message=_("Appointment booked successfully."),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Appointment Confirmed: `{se_name}`\n"
			f"{summary_subject}\n"
			f"**Date**: `{scheduled_date}` | **Time**: `{start_time}` ({dur} min)\n"
			f"**Bay**: `{entry_dict.get('service_bay')}` | "
			f"**Technician**: `{entry_dict.get('assigned_technician') or 'Unassigned'}`\n\n"
			f"[View Calendar]({cal_url}) | [Inspect Entry]({desk_url})"
		),
	)


@frappe.whitelist()
def send_quote_for_approval(
	repair_order: str,
	communication_medium: str = "sms",
	phone_number: Optional[str] = None,
	recipient: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Generate secure approval token and dispatch authorization link via approval service."""
	ro = _get_ro_doc(repair_order, ptype="write")
	repair_order = ro.name
	medium = (_optional_str(communication_medium, "Communication Medium") or "sms").lower()
	channel = "link" if medium in ("copy link only", "link", "url", "none") else medium

	target_recipient = (
		_optional_str(recipient, "Recipient")
		if recipient is not None
		else _optional_str(phone_number, "Phone Number")
	)
	res = send_approval_request(
		repair_order_name=repair_order,
		channel=channel,
		recipient=target_recipient,
	)
	ro.reload()
	approval_url = res.get("approval_url") or ""
	service_rows = [
		{"name": s.name, "service_title": s.service_title, "status": s.status}
		for s in (ro.services or [])
	]
	urls = build_ro_urls(repair_order, approval_url=approval_url)

	actual_channel = res.get("channel", channel)
	delivery_status = res.get("delivery_status") or ("Sent" if actual_channel != "link" else "Generated")
	payload_data: Dict[str, Any] = {
		"repair_order": repair_order,
		"token": ro.approval_token,
		"approval_url": approval_url,
		"expires_at": ro.approval_token_expiry,
		"channel": actual_channel,
		"requested_channel": res.get("requested_channel", channel),
		"recipient": res.get("recipient"),
		"delivery_status": delivery_status,
		"services": service_rows,
	}
	if res.get("warning"):
		payload_data["warning"] = res["warning"]
	if res.get("message_id") is not None:
		payload_data["message_id"] = res["message_id"]

	def _build_approval_summary() -> str:
		lines = [
			f"### Authorization Link Generated for #{repair_order}",
			f"**Channel**: `{actual_channel}` | **Delivery**: `{delivery_status}` | **Expiry**: 48 Hours",
			f"**Approval URL**: [{approval_url}]({approval_url})",
		]
		if payload_data.get("message_id"):
			lines.append(f"**Message ID**: `{payload_data['message_id']}`")
		if payload_data.get("warning"):
			lines.append(f"**Warning**: {payload_data['warning']}")
		return "\n".join(lines)

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=payload_data,
		message=res.get("message") or _("Quote authorization link ready."),
		include_summary=include_summary,
		summary_builder=_build_approval_summary,
	)


@frappe.whitelist()
def import_supplier_catalog(
	items: Optional[Union[List[Dict[str, Any]], Dict[str, Any], str]] = None,
	raw_text: Optional[str] = None,
	supplier: Optional[str] = None,
	default_source: str = "OEM",
	default_condition: str = "New",
	service_package: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Bulk ingest structured catalog items (primary) or CSV/tabular text into Item catalog."""
	frappe.has_permission("Item", "create", throw=True)
	frappe.has_permission("Item", "write", throw=True)
	supplier = _optional_str(supplier, "Supplier")
	service_package = _optional_str(service_package, "Service Package")
	default_source = _optional_str(default_source, "Default Source") or "OEM"
	default_condition = _optional_str(default_condition, "Default Condition") or "New"
	if supplier:
		if frappe.db.exists("Supplier", supplier):
			frappe.has_permission("Supplier", "read", doc=supplier, throw=True)
		else:
			frappe.has_permission("Supplier", "create", throw=True)
	if service_package:
		frappe.has_permission("Service Package", "read", throw=True)
		if not frappe.db.exists("Service Package", service_package):
			frappe.throw(_("Service Package {0} does not exist.").format(service_package), frappe.DoesNotExistError)
		frappe.has_permission("Service Package", "write", doc=service_package, throw=True)

	has_items = (
		items is not None
		and items != []
		and items != {}
		and (not isinstance(items, str) or bool(items.strip()))
	)
	has_raw = bool(raw_text and str(raw_text).strip())
	if not has_items and not has_raw:
		frappe.throw(_("Structured catalog items or CSV/tabular content is required."))

	normalized_cond = normalize_condition(default_condition)
	result = bulk_import_parts(
		items=items,
		raw_text=raw_text,
		service_package=service_package,
		default_source=default_source,
		default_condition=normalized_cond,
		supplier=supplier,
	)

	created = result.get("created", [])
	updated = result.get("updated", [])
	errors = result.get("errors", [])

	if supplier:
		for item_entry in created + updated:
			if isinstance(item_entry, dict) and item_entry.get("supplier"):
				continue
			item_code = item_entry.get("item_code") if isinstance(item_entry, dict) else item_entry
			if item_code and frappe.db.exists("Item", item_code):
				ensure_supplier_and_link_to_item(supplier_name=supplier, item_code=item_code)

	formatted_errors = [
		(err.get("error") or str(err)) if isinstance(err, dict) else str(err)
		for err in errors
	]

	data = {
		"supplier": supplier,
		"created": created,
		"updated": updated,
		"errors": formatted_errors,
		"total_created": len(created),
		"total_updated": len(updated),
		"total_errors": len(formatted_errors),
	}

	return build_agent_envelope(
		success=True,
		data=data,
		message=_("Ingested {0} parts successfully.").format(len(created) + len(updated)),
		include_summary=include_summary,
		summary_builder=lambda: build_catalog_summary(supplier, created, updated, formatted_errors),
	)


@frappe.whitelist()
def get_component_alternates(
	service_package: Optional[str] = None,
	component_slot: Optional[str] = None,
	slot_number: Optional[int] = None,
	oem_part_number: Optional[str] = None,
	item_code: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return compatible OEM, aftermarket, and reconditioned alternates for a component slot or part."""
	frappe.has_permission("Item", "read", throw=True)
	service_package = _optional_str(service_package, "Service Package")
	component_slot = _optional_str(component_slot, "Component Slot")
	oem_part_number = _optional_str(oem_part_number, "OEM Part Number")
	item_code = _optional_str(item_code, "Item Code")
	if service_package:
		frappe.has_permission("Service Package", "read", throw=True)
		if frappe.db.exists("Service Package", service_package):
			frappe.has_permission("Service Package", "read", doc=service_package, throw=True)
	if item_code and frappe.db.exists("Item", item_code):
		frappe.has_permission("Item", "read", doc=item_code, throw=True)

	alternates = _get_component_alternates_impl(
		service_package=service_package,
		component_slot=component_slot,
		slot_number=slot_number,
		oem_part_number=oem_part_number,
		item_code=item_code,
	) or []

	urls: Dict[str, str] = {}
	if service_package and frappe.db.exists("Service Package", service_package):
		urls["desk"] = f"{get_url()}/app/service-package/{service_package}"
	elif item_code and frappe.db.exists("Item", item_code):
		urls["desk"] = f"{get_url()}/app/item/{item_code}"

	parsed_slot_num = cint(slot_number) if slot_number is not None and str(slot_number).strip() != "" else None
	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"service_package": service_package,
			"component_slot": component_slot,
			"slot_number": parsed_slot_num,
			"oem_part_number": oem_part_number,
			"item_code": item_code,
			"count": len(alternates),
			"alternates": alternates,
		},
		message=_("Found {0} component alternate(s).").format(len(alternates)),
		include_summary=include_summary,
		summary_builder=lambda: build_alternates_summary(
			alternates,
			service_package=service_package,
			oem_part_number=oem_part_number,
			item_code=item_code,
		),
	)


def _attach_package_parts(pkg: Any, parts: List[Any]) -> None:
	"""Attach parsed part definitions onto a Service Package's items child table."""
	existing_codes = {getattr(row, "item_code", None) for row in (pkg.items or [])}
	for idx, entry in enumerate(parts, start=1):
		if isinstance(entry, str):
			part_name = entry
			part_number = ""
			qty = 1.0
			source = "OEM"
			condition = "New"
			slot = f"Part {idx}"
			item_code = None
		elif isinstance(entry, dict):
			part_name = entry.get("part_name") or entry.get("item_name") or entry.get("name") or f"Part {idx}"
			part_number = entry.get("part_number") or entry.get("oem_part_number") or ""
			qty = flt(entry.get("qty") or 1.0)
			source = entry.get("part_source") or entry.get("source") or "OEM"
			condition = normalize_condition(entry.get("part_condition") or entry.get("condition") or "New")
			slot = entry.get("component_slot") or part_name
			item_code = _optional_str(entry.get("item_code"), "Item Code")
		else:
			continue

		if not item_code:
			frappe.has_permission("Item", "create", throw=True)
			created = quick_create_part(
				part_name=part_name,
				part_number=part_number or part_name,
				part_source=source,
				part_condition=condition,
				oem_part_number=part_number or None,
			)
			item_code = created.get("item_code") if isinstance(created, dict) else created
		elif frappe.db.exists("Item", item_code):
			frappe.has_permission("Item", "read", doc=item_code, throw=True)

		if item_code and item_code not in existing_codes:
			pkg.append("items", {
				"item_code": item_code,
				"item_name": part_name,
				"oem_part_number": part_number,
				"part_source": source,
				"part_condition": condition,
				"component_slot": slot,
				"qty": qty,
				"is_default": 1,
			})
			existing_codes.add(item_code)


@frappe.whitelist()
def import_tesla_procedure(
	procedure_title: Optional[str] = None,
	correction_code: Optional[str] = None,
	labor_hours: float = 1.0,
	model: str = "Model 3",
	parts: Optional[Union[List[Any], str]] = None,
	repair_order: Optional[str] = None,
	url: Optional[str] = None,
	data: Optional[Union[Dict[str, Any], str]] = None,
	on_conflict: str = "skip",
	force: Any = False,
	side: Optional[str] = None,
	is_supplement: Any = 0,
	advisor_notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Ingest a Tesla procedure into a Service Package (or directly onto a Repair Order)."""
	repair_order = _optional_str(repair_order, "Repair Order")
	procedure_title = _optional_str(procedure_title, "Procedure Title")
	correction_code = _optional_str(correction_code, "Correction Code")
	url = _optional_str(url, "URL")
	side = _optional_str(side, "Side")
	advisor_notes = _optional_str(advisor_notes, "Advisor Notes")
	parsed_data = parse_json_arg(data, None) if isinstance(data, str) else data
	if repair_order:
		_get_ro_doc(repair_order, ptype="write")
		frappe.has_permission("Service Package", "create", throw=True)
		if parsed_data is None and not url and (procedure_title or correction_code):
			parsed_parts = parse_json_arg(parts, []) if isinstance(parts, str) else (parts or [])
			parsed_data = {
				"title": procedure_title or correction_code,
				"correction_code": correction_code or "",
				"frt": flt(labor_hours),
				"applicable_model": model,
				"parts": parsed_parts if isinstance(parsed_parts, list) else [],
			}
		ro_res = _import_tesla_ro_procedure(
			repair_order=repair_order,
			url=url,
			data=parsed_data,
			on_conflict=on_conflict,
			force=coerce_bool(force),
			side=side,
			is_supplement=1 if coerce_bool(is_supplement) else 0,
			advisor_notes=advisor_notes,
		)
		urls = build_ro_urls(repair_order)
		if isinstance(ro_res, dict) and ro_res.get("service_package"):
			urls["service_package"] = f"{get_url()}/app/service-package/{ro_res['service_package']}"
		return build_agent_envelope(
			success=True,
			urls=urls,
			data=ro_res if isinstance(ro_res, dict) else {"result": ro_res},
			message=(
				ro_res.get("message")
				if isinstance(ro_res, dict) and ro_res.get("message")
				else _("Tesla procedure imported onto Repair Order {0}.").format(repair_order)
			),
			include_summary=include_summary,
			summary_builder=lambda: (
				f"### Tesla Procedure Imported onto `{repair_order}`\n"
				f"- **Action**: `{ro_res.get('action')}` | **Policy**: `{ro_res.get('conflict_policy')}`\n"
				f"- **Service Package**: `{ro_res.get('service_package')}` | **Correction Code**: `{ro_res.get('correction_code') or 'N/A'}`\n"
				f"- **FRT**: `{flt(ro_res.get('frt', 0)):.2f} hrs`"
				if isinstance(ro_res, dict)
				else f"Tesla procedure imported onto {repair_order}."
			),
		)

	if not (frappe.has_permission("Service Package", "create") or frappe.has_permission("Service Package", "write")):
		frappe.has_permission("Service Package", "create", throw=True)

	if not procedure_title and not parsed_data and url:
		parsed_data = fetch_and_parse_tesla_procedure(url)

	if isinstance(parsed_data, dict):
		procedure_title = (
			procedure_title
			or _optional_str(
				parsed_data.get("title")
				or parsed_data.get("procedure_title")
				or parsed_data.get("procedure_name"),
				"Procedure Title",
			)
		)
		correction_code = correction_code or _optional_str(parsed_data.get("correction_code"), "Correction Code")
		if parsed_data.get("frt") is not None and flt(labor_hours) == 1.0:
			labor_hours = flt(parsed_data.get("frt"))
		model = parsed_data.get("applicable_model") or parsed_data.get("model") or model
		if parts is None and parsed_data.get("parts") is not None:
			parts = parsed_data.get("parts")

	if not procedure_title:
		frappe.throw(_("procedure_title is required when importing a Service Package."))

	try:
		parsed_parts = parse_json_arg(parts, []) or []
	except Exception:
		parsed_parts = []

	existing_name = (
		(correction_code and frappe.db.get_value("Service Package", {"correction_code": correction_code}, "name"))
		or frappe.db.get_value("Service Package", {"package_name": procedure_title}, "name")
		or frappe.db.get_value("Service Package", {"name": procedure_title}, "name")
	)

	if existing_name:
		pkg = frappe.get_doc("Service Package", existing_name)
		pkg.check_permission("write")
	else:
		frappe.has_permission("Service Package", "create", throw=True)
		pkg = frappe.new_doc("Service Package")
		pkg.package_name = procedure_title
		pkg.check_permission("create")

	pkg.applicable_model = model
	pkg.correction_code = correction_code or ""
	pkg.standard_labor_hours = flt(labor_hours)

	if isinstance(parsed_parts, list) and parsed_parts:
		_attach_package_parts(pkg, parsed_parts)

	pkg.save()

	desk_url = f"{get_url()}/app/service-package/{pkg.name}"
	return build_agent_envelope(
		success=True,
		urls={"desk": desk_url},
		data=serialize_service_package_compact(pkg),
		message=_("Tesla procedure saved as Service Package."),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Tesla Procedure Ingested: `{pkg.name}`\n"
			f"**Correction Code**: `{correction_code or 'N/A'}` | **Model**: `{model}`\n"
			f"**Flat Rate Time**: `{flt(labor_hours):.2f} hours` | **Parts**: `{len(pkg.items or [])}`\n"
			f"[Inspect Service Package]({desk_url})"
		),
	)


@frappe.whitelist()
def get_shop_floor_status(date: Optional[str] = None, include_summary: Any = False) -> Dict[str, Any]:
	"""Executive query summarizing bay occupancy, active tech pool, and active Repair Orders."""
	frappe.has_permission("Service Bay", "read", throw=True)
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Repair Order", "read", throw=True)
	raw_date = _optional_str(date, "Date")
	target_date = normalize_date(raw_date) if raw_date else today()

	bays = frappe.get_list("Service Bay", filters={"is_active": 1}, fields=["name", "description"])
	techs = get_active_technicians(target_date)
	active_techs = [t for t in techs if not t.get("on_leave")]

	entries = frappe.get_list(
		"Schedule Entry",
		filters={"scheduled_date": target_date, "status": ["!=", "Cancelled"]},
		fields=["name", "service_bay", "assigned_technician", "scheduled_time", "status", "repair_order"],
	)

	active_ros = frappe.get_list(
		"Repair Order",
		filters={"status": ["in", ["Authorized - In Progress", "Waiting on Parts", "Pending Authorization"]]},
		fields=["name", "customer", "vehicle", "status", "approval_status", "vehicle_location"],
	)

	summary = {
		"active_bays": len(bays),
		"active_technicians": len(active_techs),
		"active_repair_orders": len(active_ros),
		"scheduled_appointments": len(entries),
		"completed_appointments": len([e for e in entries if e.get("status") == "Completed"]),
		"active_allocations": len([e for e in entries if e.get("status") not in NON_BLOCKING_ENTRY_STATUSES]),
	}

	return build_agent_envelope(
		success=True,
		data={
			"date": target_date,
			"summary": summary,
			"active_repair_orders": active_ros,
		},
		message=_("Shop floor telemetry retrieved for {0}.").format(target_date),
		include_summary=include_summary,
		summary_builder=lambda: build_floor_summary(summary, active_ros),
	)


@frappe.whitelist()
def update_job_status(
	repair_order: str,
	action: str,
	odometer_out: Optional[int] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Technician & Advisor 1-tap state transition enforcement with legal authorization locks."""
	ro = _get_ro_doc(repair_order, ptype="write")
	action_key = (_optional_str(action, "Action") or "").lower()
	if action_key not in ("start", "hold", "parts", "park", "complete"):
		frappe.throw(_("Invalid action {0}. Valid actions are: start, hold, park, complete.").format(action))

	if ro.approval_status not in ["Approved", "Partially Approved"]:
		frappe.throw(
			_("Cannot transition Repair Order {0}: customer authorization is required first.").format(ro.name),
			frappe.ValidationError,
		)

	if action_key == "park":
		if odometer_out:
			ro.odometer_out = cint(odometer_out)
		ro.park_vehicle()
		ro.reload()
		if notes:
			ro.add_comment("Comment", notes)
	else:
		if action_key == "start":
			ro.manual_parts_hold = 0
			ro.status = "Authorized - In Progress"
			all_avail, _def = ro._check_parts_stock_availability()
			if all_avail:
				ro.vehicle_location = "In Bay / Being Serviced"
				if not ro.picked_up_at:
					ro.picked_up_at = frappe.utils.now_datetime()
				if not ro.picked_up_by:
					ro.picked_up_by = frappe.session.user
		elif action_key in ("hold", "parts"):
			ro.manual_parts_hold = 1
			ro.status = "Waiting on Parts"
		elif action_key == "complete":
			validated_odo_out = _resolve_odometer_out(ro, odometer_out, "complete a Repair Order")
			ro.manual_parts_hold = 0
			ro.status = "Work Completed"
			ro.odometer_out = validated_odo_out
			ro.vehicle_location = "Completed / Parked in Lot"

		ro.save()
		if action_key == "complete":
			from ism.ism.doctype.repair_order.repair_order import complete_linked_schedule_entries
			complete_linked_schedule_entries(ro.name)
		if notes:
			ro.add_comment("Comment", notes)

	parts_deficit: List[Any] = []
	if action_key == "start" and ro.status == "Waiting on Parts":
		_all_avail, parts_deficit = ro._check_parts_stock_availability()

	msg = _("Repair Order status updated to {0}.").format(ro.status)
	if action_key == "start" and ro.status == "Waiting on Parts":
		msg = _("Repair Order {0} remains Waiting on Parts due to stock deficit.").format(ro.name)

	data: Dict[str, Any] = {
		"repair_order": ro.name,
		"status": ro.status,
		"approval_status": ro.approval_status,
		"vehicle_location": ro.vehicle_location,
	}
	if parts_deficit:
		data["parts_deficit"] = parts_deficit

	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(ro.name),
		data=data,
		message=msg,
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Repair Order #{ro.name} Status Updated\n"
			f"**New Status**: `{ro.status}` | **Approval**: `{ro.approval_status}`\n"
			f"**Vehicle Location**: `{ro.vehicle_location or 'Shop Floor'}`"
		),
	)


def _collect_required_part_qtys(repair_order: Optional[str]) -> Dict[str, float]:
	"""Sum required part quantities per item_code across a Repair Order's services."""
	req: Dict[str, float] = {}
	repair_order = _optional_str(repair_order, "Repair Order")
	if not repair_order or not frappe.db.exists("Repair Order", repair_order):
		return req
	ro = _get_ro_doc(repair_order, ptype="read")
	for svc in ro.services:
		parts = parse_json_arg(svc.parts, []) or []
		for p in parts:
			if isinstance(p, dict) and p.get("item_code"):
				code = str(p["item_code"]).strip()
				req[code] = req.get(code, 0.0) + flt(p.get("qty", 1.0))
	return req


@frappe.whitelist()
def check_parts_stock(
	repair_order: Optional[str] = None,
	items: Optional[Union[List[Any], str]] = None,
	company: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Check warehouse stock availability and deficits for a Repair Order or item list."""
	repair_order = _optional_str(repair_order, "Repair Order")
	company = _optional_str(company, "Company")
	frappe.has_permission("Item", "read", throw=True)
	if repair_order:
		_get_ro_doc(repair_order, ptype="read")
	parsed_items = parse_json_arg(items, None) if isinstance(items, str) else items
	stock_map = get_repair_order_parts_stock(ro_name=repair_order, items=parsed_items, company=company)
	if not isinstance(stock_map, dict):
		stock_map = {}

	req_qtys = _collect_required_part_qtys(repair_order)
	deficit_items: List[str] = []
	for code, info in stock_map.items():
		if not isinstance(info, dict):
			continue
		req_qty = req_qtys.get(code, 1.0)
		if "deficit" in info:
			deficit = max(0.0, flt(info["deficit"]))
		else:
			deficit = max(0.0, flt(req_qty) - flt(info.get("available_qty", 0.0)))
		info["required_qty"] = req_qty
		info["deficit"] = deficit
		if deficit > 0:
			deficit_items.append(code)

	urls = build_ro_urls(repair_order) if repair_order else {}

	sku_count = len(stock_map)
	sku_word = _("SKU") if sku_count == 1 else _("SKUs")

	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"repair_order": repair_order,
			"all_available": len(deficit_items) == 0,
			"deficit_count": len(deficit_items),
			"items": stock_map,
		},
		message=_("Checked stock for {0} unique part {1}.").format(sku_count, sku_word),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Parts Stock Check ({repair_order or 'Items'})\n"
			f"- **Checked SKUs**: `{sku_count}`\n"
			f"- **Deficits**: `{len(deficit_items)}`"
		),
	)


@frappe.whitelist()
def submit_procurement_orders(
	repair_order: str,
	parts_data: Union[List[Dict[str, Any]], str],
	submit: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create Purchase Orders (Draft by default) and RFQs for missing Repair Order parts."""
	ro = _get_ro_doc(repair_order, ptype="read")
	repair_order = ro.name
	should_submit = coerce_bool(submit)
	frappe.has_permission("Purchase Order", "create", throw=True)
	if should_submit:
		frappe.has_permission("Purchase Order", "submit", throw=True)
	parsed_parts = parse_json_arg(parts_data, [])
	res = _submit_procurement_orders_impl(
		repair_order=repair_order,
		parts_data=parsed_parts,
		submit=should_submit,
	)

	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data={
			"repair_order": repair_order,
			"submitted": should_submit,
			"purchase_orders": res.get("purchase_orders", []),
			"rfqs": res.get("rfqs", []),
		},
		message=_("Created {0} PO(s) and {1} RFQ(s).").format(
			len(res.get("purchase_orders", [])),
			len(res.get("rfqs", [])),
		),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Procurement Orders for #{repair_order}\n"
			f"- **Submitted**: `{should_submit}`\n"
			f"- **Purchase Orders**: `{', '.join(res.get('purchase_orders', [])) or 'None'}`\n"
			f"- **RFQs**: `{', '.join(res.get('rfqs', [])) or 'None'}`"
		),
	)


@frappe.whitelist()
def fetch_tesla_manual(
	url: str,
	repair_order: Optional[str] = None,
	vehicle: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Fetch and parse a Tesla Service Manual procedure URL with optional compatibility check."""
	frappe.has_permission("Service Package", "read", throw=True)
	url = _require_str(url, "URL")
	repair_order = _optional_str(repair_order, "Repair Order")
	vehicle = _optional_str(vehicle, "Repair Vehicle")
	if repair_order:
		_get_ro_doc(repair_order, ptype="read")
	if vehicle and frappe.db.exists("Repair Vehicle", vehicle):
		frappe.has_permission("Repair Vehicle", "read", doc=vehicle, throw=True)
	parsed = fetch_and_parse_tesla_procedure(url, repair_order=repair_order, vehicle=vehicle)
	urls: Dict[str, str] = {"manual": url}
	if repair_order:
		urls.update(build_ro_urls(repair_order))
	elif vehicle and frappe.db.exists("Repair Vehicle", vehicle):
		urls["vehicle"] = f"{get_url()}/app/repair-vehicle/{vehicle}"

	def _build_manual_summary() -> str:
		title = parsed.get("title", "Unknown") if isinstance(parsed, dict) else "Parsed"
		lines = [
			"### Tesla Service Manual Procedure",
			f"- **Title**: `{title}`",
			f"- **URL**: {url}",
		]
		compat = parsed.get("compatibility") if isinstance(parsed, dict) else None
		if isinstance(compat, dict) and compat.get("warning"):
			lines.append(f"- **Compatibility Warning**: {compat['warning']}")
		return "\n".join(lines)

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=parsed if isinstance(parsed, dict) else {"result": parsed},
		message=_("Parsed Tesla Service Manual procedure."),
		include_summary=include_summary,
		summary_builder=_build_manual_summary,
	)


@frappe.whitelist()
def get_tech_overview(date: Optional[str] = None, include_summary: Any = False) -> Dict[str, Any]:
	"""Return daily technician workload, shift windows, and bottleneck analysis."""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)
	raw_date = _optional_str(date, "Date")
	target_date = normalize_date(raw_date) if raw_date else today()
	overview = get_daily_technician_overview(target_date)
	return build_agent_envelope(
		success=True,
		data=overview if isinstance(overview, dict) else {"date": target_date, "overview": overview},
		message=_("Technician overview retrieved for {0}.").format(target_date),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Technician Overview ({target_date})\n"
			f"- **Total Scheduled Hours**: `{overview.get('total_scheduled_hours', 0) if isinstance(overview, dict) else 0}`\n"
			f"- **Bottleneck**: `{overview.get('bottleneck', 'None') if isinstance(overview, dict) else 'None'}`"
		),
	)


@frappe.whitelist()
def get_unscheduled_services(repair_order: str, include_summary: Any = False) -> Dict[str, Any]:
	"""Return approved services on a Repair Order that lack a scheduled bay allocation."""
	ro = _get_ro_doc(repair_order, ptype="read")
	repair_order = ro.name
	frappe.has_permission("Schedule Entry", "read", throw=True)
	rows = _get_unscheduled_services_impl(repair_order)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data={
			"repair_order": repair_order,
			"unscheduled_count": len(rows),
			"services": rows,
		},
		message=_("Found {0} unscheduled service(s).").format(len(rows)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Unscheduled Services for #{repair_order}\n"
			f"- **Pending Scheduling**: `{len(rows)}`"
		),
	)


@frappe.whitelist()
def receive_purchase_order(
	purchase_order: str,
	submit_po: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Submit (when confirmed) and map/submit a Purchase Receipt for a Purchase Order in one call."""
	purchase_order = _require_str(purchase_order, "Purchase Order")
	frappe.has_permission("Purchase Order", "read", throw=True)
	if not frappe.db.exists("Purchase Order", purchase_order):
		frappe.throw(_("Purchase Order {0} does not exist.").format(purchase_order), frappe.DoesNotExistError)

	po = frappe.get_doc("Purchase Order", purchase_order)
	po.check_permission("read")
	if po.docstatus == 0:
		if not coerce_bool(submit_po):
			frappe.throw(
				_("Purchase Order {0} is in Draft. Pass submit_po=True after explicit operator confirmation.").format(
					purchase_order
				),
				frappe.ValidationError,
			)
		po.check_permission("submit")
		po.submit()

	frappe.has_permission("Purchase Receipt", "create", throw=True)
	frappe.has_permission("Purchase Receipt", "submit", throw=True)
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	pr_doc = make_purchase_receipt(source_name=po.name)
	pr_doc.check_permission("create")
	pr_doc.insert()
	pr_doc.check_permission("submit")
	pr_doc.submit()
	po.reload()

	ro_name = getattr(po, "repair_order", None)
	ro_status = None
	if ro_name and frappe.db.exists("Repair Order", ro_name) and frappe.has_permission("Repair Order", "read", doc=ro_name):
		ro_status = frappe.db.get_value("Repair Order", ro_name, "status")
	base_url = get_url()
	urls = build_ro_urls(ro_name) if ro_name else {}
	urls["purchase_order"] = f"{base_url}/app/purchase-order/{po.name}"
	urls["purchase_receipt"] = f"{base_url}/app/purchase-receipt/{pr_doc.name}"

	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"purchase_order": po.name,
			"purchase_receipt": pr_doc.name,
			"docstatus": pr_doc.docstatus,
			"per_received": flt(getattr(po, "per_received", 100.0)),
			"repair_order": ro_name,
			"repair_order_status": ro_status,
			"items": [
				{"item_code": i.item_code, "qty": flt(i.qty), "warehouse": i.warehouse}
				for i in (pr_doc.items or [])
			],
		},
		message=_("Received Purchase Order {0} via Purchase Receipt {1}.").format(po.name, pr_doc.name),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Purchase Receipt Created: `{pr_doc.name}`\n"
			f"- **PO**: `{po.name}` ({flt(getattr(po, 'per_received', 100.0))}% received)\n"
			f"- **Repair Order**: `{ro_name or 'None'}` (`{ro_status or 'N/A'}`)"
		),
	)


@frappe.whitelist()
def checkout_repair_order(
	repair_order: str,
	payment_method: Optional[str] = None,
	paid_amount: Optional[float] = None,
	odometer_out: Optional[int] = None,
	confirm: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Invoice, record payment, complete linked schedule entries, and release a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	payment_method = _optional_str(payment_method, "Payment Method")
	if not payment_method:
		frappe.throw(_("Payment Method is required."), frappe.MandatoryError)
	validated_odo_out = _resolve_odometer_out(ro, odometer_out, "release a Repair Order")
	if not coerce_bool(confirm):
		frappe.throw(
			_("Pass confirm=True only after explicit operator confirmation before invoicing and releasing {0}.").format(
				ro.name
			),
			frappe.ValidationError,
		)

	ro.process_payment_and_release(
		payment_method=payment_method,
		paid_amount=paid_amount,
		odometer_out=validated_odo_out,
	)
	ro.reload()

	pe_name = None
	if ro.sales_invoice and frappe.has_permission("Payment Entry", "read"):
		pe_name = frappe.db.get_value(
			"Payment Entry Reference",
			{"reference_doctype": "Sales Invoice", "reference_name": ro.sales_invoice, "docstatus": 1},
			"parent",
		)

	base_url = get_url()
	urls = build_ro_urls(ro.name)
	if ro.sales_invoice:
		urls["sales_invoice"] = f"{base_url}/app/sales-invoice/{ro.sales_invoice}"
	if pe_name:
		urls["payment_entry"] = f"{base_url}/app/payment-entry/{pe_name}"

	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"repair_order": ro.name,
			"status": ro.status,
			"payment_status": ro.payment_status,
			"sales_invoice": ro.sales_invoice,
			"payment_entry": pe_name,
			"grand_total": flt(ro.grand_total, 2),
			"paid_amount": flt(getattr(ro, "paid_amount", 0.0), 2),
			"outstanding_amount": flt(ro.outstanding_amount, 2),
			"vehicle_released": cint(ro.vehicle_released),
			"odometer_out": ro.odometer_out,
			"checkout_date": ro.checkout_date,
		},
		message=_("Checked out and released {0} (Invoice {1}).").format(ro.name, ro.sales_invoice or "N/A"),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Checked Out: `{ro.name}`\n"
			f"- **Status**: `{ro.status}` | **Payment**: `{ro.payment_status}`\n"
			f"- **Invoice**: `{ro.sales_invoice or 'None'}` | **Payment Entry**: `{pe_name or 'None'}`"
		),
	)


def _normalize_service_row_targets(
	service_row_id: Optional[Union[str, List[str]]],
	service_row_ids: Optional[Union[str, List[str]]],
) -> List[str]:
	raw = service_row_ids if service_row_ids is not None else service_row_id
	if isinstance(raw, list):
		return [str(r).strip() for r in raw if str(r).strip()]
	if isinstance(raw, str):
		stripped = raw.strip()
		if not stripped:
			return []
		if stripped.startswith("["):
			parsed = parse_json_arg(stripped, [])
			return [str(r).strip() for r in (parsed or []) if str(r).strip()]
		return [part.strip() for part in stripped.split(",") if part.strip()]
	return []


@frappe.whitelist()
def toggle_service_completion(
	repair_order: str,
	service_row_id: Optional[Union[str, List[str]]] = None,
	is_completed: Any = True,
	service_row_ids: Optional[Union[List[str], str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Toggle completion status on one or more approved service rows on a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	targets = _normalize_service_row_targets(service_row_id, service_row_ids)
	if not targets:
		frappe.throw(_("At least one service_row_id is required."), frappe.ValidationError)

	completed_flag = 1 if coerce_bool(is_completed) else 0
	for row_id in targets:
		ro.toggle_service_completion(service_row_id=row_id, is_completed=completed_flag)
	ro.reload()
	data = serialize_repair_order_compact(ro)
	data["updated_rows"] = targets

	target_label = targets[0] if len(targets) == 1 else f"{len(targets)} services"
	msg = (
		_("Updated service {0} completion on {1} (Progress: {2}%).").format(
			targets[0], ro.name, flt(ro.progress_percent, 1)
		)
		if len(targets) == 1
		else _("Updated completion for {0} services on {1} (Progress: {2}%).").format(
			len(targets), ro.name, flt(ro.progress_percent, 1)
		)
	)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(ro.name),
		data=data,
		message=msg,
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Service Updated on `{ro.name}`\n"
			f"- **Service(s)**: `{target_label}` | **Completed**: `{bool(completed_flag)}`\n"
			f"- **Progress**: `{flt(ro.progress_percent, 1)}%` | **Status**: `{ro.status}`"
		),
	)


@frappe.whitelist()
def add_supplement(
	repair_order: str,
	service_title: str,
	category: str = "Maintenance",
	labor_hours: float = 1.0,
	labor_rate: Optional[float] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Add a mid-job supplemental finding/operation in Estimating status for customer re-approval."""
	ro = _get_ro_doc(repair_order, ptype="write")
	service_title = _require_str(service_title, "service_title")
	category = _optional_str(category, "Category") or "Maintenance"

	resolved_parts = _resolve_and_validate_parts(parts) if parts else []
	res = ro.add_supplement(
		service_title=service_title,
		category=category,
		labor_hours=flt(labor_hours),
		labor_rate=labor_rate,
		parts=resolved_parts,
		notes=notes,
	)
	ro.reload()
	data = serialize_repair_order_compact(ro)
	data["added_service_title"] = res.get("service_title")
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(ro.name),
		data=data,
		message=_("Supplement '{0}' added to {1}. Send quote for customer approval before starting work.").format(
			res.get("service_title"), ro.name
		),
		include_summary=include_summary,
		summary_builder=lambda: build_quote_summary(
			ro, _estimate_duration_minutes(ro), title_prefix="Repair Order Supplement"
		),
	)


@frappe.whitelist()
def duplicate_service_for_opposite_side(
	repair_order: str,
	service_row_id: str,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Duplicate an LH or RH symmetrical service operation for the opposite vehicle side."""
	ro = _get_ro_doc(repair_order, ptype="write")
	service_row_id = _require_str(service_row_id, "service_row_id")
	res = ro.duplicate_service_for_opposite_side(service_row_id=service_row_id)
	ro.reload()
	data = serialize_repair_order_compact(ro)
	data["added_service"] = {
		"service_title": res.get("service_title"),
		"service_position": res.get("service_position"),
	}
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(ro.name),
		data=data,
		message=_("Added opposite-side service '{0}' ({1}) to {2}.").format(
			res.get("service_title"), res.get("service_position"), ro.name
		),
		include_summary=include_summary,
		summary_builder=lambda: build_quote_summary(ro, _estimate_duration_minutes(ro)),
	)


@frappe.whitelist()
def get_deferred_services(
	repair_order: Optional[str] = None,
	vehicle: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Retrieve open previously declined/deferred service recommendations for a vehicle or Repair Order."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	repair_order = _optional_str(repair_order, "Repair Order")
	vehicle = _optional_str(vehicle, "Repair Vehicle")
	ro = _get_ro_doc(repair_order, ptype="read") if repair_order else None
	veh_name = ro.vehicle if ro else _lookup_vehicle(vehicle or "")
	if veh_name:
		frappe.has_permission("Repair Vehicle", "read", doc=veh_name, throw=True)
	if not veh_name:
		if not vehicle and not repair_order:
			frappe.throw(_("Provide repair_order or vehicle identifier."), frappe.ValidationError)
		return build_agent_envelope(
			success=True,
			data={"vehicle": vehicle, "deferred_count": 0, "deferred_services": []},
			message=_("No existing vehicle record found for {0}.").format(vehicle),
		)

	deferred = _serialize_pending_deferred(veh_name, ro=ro)
	urls = build_ro_urls(ro.name) if ro else {"vehicle": f"{get_url()}/app/repair-vehicle/{veh_name}"}
	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"repair_order": ro.name if ro else None,
			"vehicle": veh_name,
			"deferred_count": len(deferred),
			"deferred_services": deferred,
		},
		message=_("Found {0} open deferred service(s) for vehicle {1}.").format(len(deferred), veh_name),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Open Deferred Services for `{veh_name}` ({len(deferred)})\n"
			+ (
				"\n".join(
					f"- `{d['job_title']}` (${flt(d['estimated_amount']):,.2f})"
					+ (" [Safety Hazard]" if d.get("safety_hazard") else "")
					for d in deferred
				)
				or "_None_"
			)
		),
	)


@frappe.whitelist()
def add_deferred_services(
	repair_order: str,
	deferred_items: Optional[Union[List[Any], str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Add previously deferred service recommendations onto an existing Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	parsed = parse_json_arg(deferred_items, deferred_items) if isinstance(deferred_items, str) else deferred_items
	res = ro.add_deferred_services(deferred_items=parsed)
	ro.reload()
	remaining = _serialize_pending_deferred(ro.vehicle, ro=ro)
	data = serialize_repair_order_compact(ro)
	data["added_count"] = cint(res.get("added_count", 0))
	data["added_services"] = res.get("added_services", [])
	data["remaining_deferred_count"] = len(remaining)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(ro.name),
		data=data,
		message=_("Added {0} deferred service(s) to {1}.").format(data["added_count"], ro.name),
		include_summary=include_summary,
		summary_builder=lambda: build_quote_summary(ro, _estimate_duration_minutes(ro), deferred_services=remaining),
	)


def _action_item_urls(name: str, reference_doctype: Optional[str] = None, reference_name: Optional[str] = None) -> Dict[str, str]:
	urls = {"desk": f"{get_url()}/app/shop-action-item/{name}"}
	if reference_doctype == "Repair Order" and reference_name:
		urls.update(build_ro_urls(reference_name))
		urls["desk"] = f"{get_url()}/app/shop-action-item/{name}"
	return urls


@frappe.whitelist()
def list_shop_action_items(
	status: Optional[str] = "Open",
	reference_doctype: Optional[str] = None,
	reference_name: Optional[str] = None,
	category: Optional[str] = None,
	limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""List Shop Action Items filtered by status, reference document, or category."""
	frappe.has_permission("Shop Action Item", "read", throw=True)
	status = _optional_str(status, "Status")
	reference_doctype = _optional_str(reference_doctype, "Reference DocType")
	reference_name = _optional_str(reference_name, "Reference Name")
	category = _optional_str(category, "Category")
	if reference_doctype:
		if not frappe.db.exists("DocType", reference_doctype):
			frappe.throw(_("Invalid reference_doctype {0}.").format(reference_doctype), frappe.ValidationError)
		frappe.has_permission(reference_doctype, "read", throw=True)
		if reference_name and frappe.db.exists(reference_doctype, reference_name):
			frappe.has_permission(reference_doctype, "read", doc=reference_name, throw=True)

	filters: Dict[str, Any] = {}
	if status and status.lower() != "all":
		filters["status"] = status
	if reference_doctype:
		filters["reference_doctype"] = reference_doctype
	if reference_name:
		filters["reference_name"] = reference_name
	if category:
		filters["category"] = category

	items = frappe.get_list(
		"Shop Action Item",
		filters=filters,
		fields=[
			"name",
			"title",
			"category",
			"priority",
			"status",
			"reference_doctype",
			"reference_name",
			"item_code",
			"reported_by",
			"resolved_by",
			"resolved_at",
		],
		order_by="creation desc",
		limit_page_length=cint(limit) or 20,
	)
	return build_agent_envelope(
		success=True,
		urls={"list": f"{get_url()}/app/shop-action-item"},
		data={"count": len(items), "items": items},
		message=_("Retrieved {0} Shop Action Item(s).").format(len(items)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Shop Action Items ({len(items)})\n"
			+ (
				"\n".join(f"- `{it['name']}` [{it['priority']}/{it['status']}] {it['title']}" for it in items)
				or "_No action items match filter._"
			)
		),
	)


@frappe.whitelist()
def create_shop_action_item(
	title: str,
	category: str = "Shop Floor Discrepancy",
	priority: str = "High",
	reference_doctype: Optional[str] = None,
	reference_name: Optional[str] = None,
	item_code: Optional[str] = None,
	details: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Create a Shop Action Item escalation for shop floor discrepancies, shortages, or safety flags."""
	frappe.has_permission("Shop Action Item", "create", throw=True)
	title = _require_str(title, "title")
	category = _optional_str(category, "Category") or "Shop Floor Discrepancy"
	priority = _optional_str(priority, "Priority") or "High"
	reference_doctype = _optional_str(reference_doctype, "Reference DocType")
	reference_name = _optional_str(reference_name, "Reference Name")
	item_code = _optional_str(item_code, "Item Code")

	name = _create_shop_action_item_impl(
		title=title,
		category=category,
		priority=priority,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		item_code=item_code,
		details=details,
	)
	doc = frappe.get_doc("Shop Action Item", name)
	return build_agent_envelope(
		success=True,
		urls=_action_item_urls(doc.name, doc.reference_doctype, doc.reference_name),
		data={
			"name": doc.name,
			"title": doc.title,
			"category": doc.category,
			"priority": doc.priority,
			"status": doc.status,
			"reference_doctype": doc.reference_doctype,
			"reference_name": doc.reference_name,
			"item_code": doc.item_code,
		},
		message=_("Created Shop Action Item {0} ({1}).").format(doc.name, doc.priority),
		include_summary=include_summary,
		summary_builder=lambda: f"### Shop Action Item Created: `{doc.name}`\n- **Title**: {doc.title}\n- **Priority**: `{doc.priority}`",
	)


@frappe.whitelist()
def resolve_shop_action_item(
	action_item: str,
	resolution_notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Resolve an open Shop Action Item with resolution notes."""
	action_item = _require_str(action_item, "Shop Action Item")
	frappe.has_permission("Shop Action Item", "write", throw=True)
	_resolve_action_item_impl(name=action_item, resolution_notes=resolution_notes)
	doc = frappe.get_doc("Shop Action Item", action_item)
	return build_agent_envelope(
		success=True,
		urls=_action_item_urls(doc.name, doc.reference_doctype, doc.reference_name),
		data={
			"name": doc.name,
			"title": doc.title,
			"status": doc.status,
			"resolution_notes": doc.resolution_notes,
			"resolved_by": doc.resolved_by,
			"resolved_at": doc.resolved_at,
		},
		message=_("Resolved Shop Action Item {0}.").format(doc.name),
		include_summary=include_summary,
		summary_builder=lambda: f"### Resolved Shop Action Item: `{doc.name}`\n- **Notes**: {doc.resolution_notes or 'None'}",
	)


_VALID_SCHEDULE_ENTRY_STATUSES = (
	"Draft",
	"Scheduled",
	"Confirmed",
	"In Progress",
	"Completed",
	"No Show",
	"Cancelled",
)


def _schedule_entry_urls(se_name: str, repair_order: Optional[str] = None) -> Dict[str, str]:
	base_url = get_url()
	urls = {
		"desk": f"{base_url}/app/schedule-entry/{se_name}",
		"calendar": f"{base_url}/app/schedule-entry/view/calendar",
	}
	if repair_order:
		urls.update(build_ro_urls(repair_order))
		urls["desk"] = f"{base_url}/app/schedule-entry/{se_name}"
	return urls


def _serialize_schedule_entry(doc: Any) -> Dict[str, Any]:
	payload: Dict[str, Any] = {
		"name": doc.name,
		"status": doc.status,
		"entry_type": doc.entry_type,
		"repair_order": doc.repair_order,
		"scheduled_date": str(doc.scheduled_date),
		"scheduled_time": str(doc.scheduled_time),
		"estimated_duration": int(doc.estimated_duration or 0),
		"service_bay": doc.service_bay,
		"assigned_technician": doc.assigned_technician,
	}
	warnings = getattr(doc.flags, "scheduling_warnings", None) or []
	if warnings:
		payload["warnings"] = list(warnings)
	return payload


@frappe.whitelist()
def reschedule_appointment(
	schedule_entry: str,
	scheduled_date: Optional[str] = None,
	start_time: Optional[str] = None,
	duration_minutes: Optional[int] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Move an existing Schedule Entry to a new date, time, bay, or technician with capacity checks."""
	frappe.has_permission("Schedule Entry", "write", throw=True)
	schedule_entry = _require_str(schedule_entry, "Schedule Entry")
	scheduled_date = _optional_str(scheduled_date, "Scheduled Date")
	start_time = _optional_str(start_time, "Start Time")
	clean_bay = _optional_str(service_bay, "Service Bay")
	clean_tech = _optional_str(technician, "Technician")
	if not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry), frappe.DoesNotExistError)

	doc = frappe.get_doc("Schedule Entry", schedule_entry)
	doc.check_permission("write")
	if doc.status in ("Completed", "Cancelled"):
		frappe.throw(
			_("Cannot reschedule Schedule Entry {0} in '{1}' state.").format(doc.name, doc.status),
			frappe.ValidationError,
		)

	target_date = normalize_date(scheduled_date) if scheduled_date else str(doc.scheduled_date)
	target_time = normalize_time(start_time) if start_time else str(doc.scheduled_time)
	target_dur = max(15, int(duration_minutes)) if duration_minutes is not None else int(doc.estimated_duration or 60)

	if service_bay is not None:
		if clean_bay:
			frappe.has_permission("Service Bay", "read", throw=True)
			target_bay = resolve_service_bay_identifier(clean_bay)
		else:
			target_bay = None
	else:
		target_bay = doc.service_bay

	if technician is not None:
		target_tech = resolve_technician_identifier(clean_tech) if clean_tech else None
	else:
		target_tech = doc.assigned_technician

	merged_notes = (f"{doc.notes}\n{notes}".strip() if doc.notes else notes) if notes else None

	alloc_res = allocate_schedule_entry(
		repair_order=doc.repair_order,
		entry_type=doc.entry_type or "Diagnostic",
		scheduled_date=target_date,
		scheduled_time=target_time,
		estimated_duration=target_dur,
		service_bay=target_bay,
		assigned_technician=target_tech,
		notes=merged_notes,
		schedule_entry_name=doc.name,
	)
	if alloc_res.get("status") == "conflict" or alloc_res.get("conflict"):
		return build_agent_envelope(
			success=False,
			urls=alloc_res.get("urls") or _schedule_entry_urls(doc.name, doc.repair_order),
			data=alloc_res,
			message=alloc_res.get("message") or _("Reschedule conflict detected."),
			error=alloc_res.get("message") or _("Reschedule conflict detected."),
			include_summary=include_summary,
			summary_builder=lambda: _build_conflict_summary("Reschedule Conflict", alloc_res),
		)

	doc.reload()
	if service_bay is not None and not target_bay and doc.service_bay:
		doc.service_bay = None
		doc.save()
	entry_data = _serialize_schedule_entry(doc)
	urls = _schedule_entry_urls(doc.name, doc.repair_order)
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=entry_data,
		message=_("Rescheduled appointment {0}.").format(doc.name),
		include_summary=include_summary,
		summary_builder=lambda: build_appointment_summary("Appointment Rescheduled", entry_data, urls),
	)


@frappe.whitelist()
def intake_and_find_slots(
	customer_name: Optional[str] = None,
	vehicle_identifier: Optional[str] = None,
	customer: Optional[str] = None,
	vehicle: Optional[str] = None,
	services: Optional[Union[List[Any], str]] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	concern: Optional[str] = None,
	customer_phone: Optional[str] = None,
	customer_email: Optional[str] = None,
	company: Optional[str] = None,
	odometer_in: Optional[int] = None,
	start_date: Optional[str] = None,
	target_date: Optional[str] = None,
	days_ahead: int = 3,
	duration_minutes: Optional[int] = None,
	equipment_tags: Optional[Union[List[str], str]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	max_slots: int = 10,
	include_past: Any = False,
	min_start_time: Optional[str] = None,
	include_resources: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Composite intake endpoint: create Draft Estimate quote and return candidate booking slots."""
	frappe.has_permission("Repair Order", "create", throw=True)
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)

	quote_res = create_quote(
		customer_name=customer_name,
		vehicle_identifier=vehicle_identifier,
		customer=customer,
		vehicle=vehicle,
		services=services,
		parts=parts,
		notes=notes,
		concern=concern,
		customer_phone=customer_phone,
		customer_email=customer_email,
		company=company,
		odometer_in=odometer_in,
		include_summary=False,
	)
	ro_data = quote_res["data"]
	ro_name = ro_data["name"]
	dur = duration_minutes or ro_data.get("total_duration_minutes") or 60

	slots_res = find_available_slots(
		repair_order=ro_name,
		duration_minutes=dur,
		start_date=start_date,
		target_date=target_date,
		days_ahead=days_ahead,
		equipment_tags=equipment_tags,
		service_bay=service_bay,
		technician=technician,
		include_resources=include_resources,
		max_slots=max_slots,
		include_past=include_past,
		min_start_time=min_start_time,
		include_summary=False,
	)
	slots_data = slots_res["data"]
	combined_data: Dict[str, Any] = {
		"name": ro_name,
		"repair_order": ro_data,
		"duration_minutes": slots_data["duration_minutes"],
		"slots": slots_data["slots"],
		"resources": slots_data["resources"],
	}
	if "service_bay" in slots_data:
		combined_data["service_bay"] = slots_data["service_bay"]
	if "technician" in slots_data:
		combined_data["technician"] = slots_data["technician"]
	if "skipped_holidays" in slots_data:
		combined_data["skipped_holidays"] = slots_data["skipped_holidays"]
	if "duration_breakdown" in slots_data:
		combined_data["duration_breakdown"] = slots_data["duration_breakdown"]

	urls = dict(quote_res.get("urls") or {})
	urls["calendar"] = f"{get_url()}/app/schedule-entry/view/calendar"

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=combined_data,
		message=_("Created quote {0} and found {1} available slot(s).").format(
			ro_name, len(slots_data["slots"])
		),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Intake Quote `{ro_name}` & Candidate Slots\n"
			f"- **Estimated Duration**: {slots_data['duration_minutes']} min\n"
			f"- **Available Slots Found**: {len(slots_data['slots'])}\n\n"
			f"{build_slots_summary(slots_data['slots'], slots_data['duration_minutes'])}"
		),
	)


@frappe.whitelist()
def update_appointment_status(
	schedule_entry: str,
	status: str,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Transition a Schedule Entry status and push a single ambient calendar refresh."""
	frappe.has_permission("Schedule Entry", "write", throw=True)
	schedule_entry = _require_str(schedule_entry, "Schedule Entry")
	status = _require_str(status, "Status")
	if not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry), frappe.DoesNotExistError)

	if status not in _VALID_SCHEDULE_ENTRY_STATUSES:
		frappe.throw(
			_("Invalid Schedule Entry status '{0}'. Allowed values: {1}.").format(
				status, ", ".join(_VALID_SCHEDULE_ENTRY_STATUSES)
			),
			frappe.ValidationError,
		)

	doc = frappe.get_doc("Schedule Entry", schedule_entry)
	doc.check_permission("write")
	doc.status = status
	if notes:
		doc.notes = f"{doc.notes}\n{notes}".strip() if doc.notes else notes
	doc.save()

	entry_data = _serialize_schedule_entry(doc)
	urls = _schedule_entry_urls(doc.name, doc.repair_order)
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=entry_data,
		message=_("Updated Schedule Entry {0} to {1}.").format(doc.name, doc.status),
		include_summary=include_summary,
		summary_builder=lambda: build_appointment_summary("Appointment Updated", entry_data, urls),
	)


@frappe.whitelist()
def create_ro_from_appointment(
	schedule_entry: str,
	odometer_in: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Convert a pre-intake or diagnostic Schedule Entry into a Checked In Repair Order."""
	frappe.has_permission("Schedule Entry", "write", throw=True)
	frappe.has_permission("Repair Order", "create", throw=True)
	schedule_entry = _require_str(schedule_entry, "Schedule Entry")
	if not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry), frappe.DoesNotExistError)
	frappe.has_permission("Schedule Entry", "write", doc=schedule_entry, throw=True)
	validated_odometer_in = _require_non_negative_int(odometer_in, "Odometer In")

	res = create_repair_order_from_schedule_entry(schedule_entry, odometer_in=validated_odometer_in)
	ro_name = res["repair_order"]
	urls = build_ro_urls(ro_name)
	urls["calendar"] = f"{get_url()}/app/schedule-entry/view/calendar"
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=res,
		message=_("Created Repair Order {0} from Schedule Entry {1}.").format(ro_name, schedule_entry),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Repair Order Created: `{ro_name}`\n"
			f"- **Schedule Entry**: `{schedule_entry}`\n"
			f"- **Customer Concern**: {res.get('customer_concern') or 'None'}\n\n"
			f"[Open in Desk]({urls['desk']})"
		),
	)


@frappe.whitelist()
def estimate_duration(
	repair_order: Optional[str] = None,
	operations: Optional[Union[List[Any], str]] = None,
	percentile: Optional[float] = None,
	sigma: Optional[float] = None,
	buffer_minutes: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Estimate probabilistic shop duration (log-normal + Fenton-Wilkinson) for an RO or operations list."""
	repair_order = _optional_str(repair_order, "Repair Order")
	if not repair_order and not operations:
		frappe.throw(_("Provide either repair_order or operations."), frappe.ValidationError)

	kwargs: Dict[str, Any] = {}
	if percentile is not None and percentile != "":
		kwargs["percentile"] = float(percentile)
	if sigma is not None and sigma != "":
		kwargs["sigma"] = float(sigma)
	if buffer_minutes is not None and buffer_minutes != "":
		kwargs["buffer_padding_minutes"] = int(buffer_minutes)

	urls: Dict[str, str] = {}
	if repair_order:
		_get_ro_doc(repair_order, ptype="read")
		est = estimate_repair_order(repair_order, **kwargs)
		urls = build_ro_urls(repair_order)
	else:
		frappe.has_permission("Item", "read", throw=True)
		parsed_ops = parse_json_arg(operations, operations) if isinstance(operations, str) else operations
		est = get_total_estimate(parsed_ops, **kwargs)

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=est,
		message=_("Estimated shop duration: {0} minutes.").format(est.get("total_duration_minutes", 0)),
		include_summary=include_summary,
		summary_builder=lambda: build_estimate_summary(est),
	)


@frappe.whitelist()
def decode_vehicle_vin(vin: str, include_summary: Any = False) -> Dict[str, Any]:
	"""Decode a 17-character Tesla VIN offline and return specs and manual URL."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	vin = _optional_str(vin, "VIN") or ""
	decoded = _decode_vehicle_vin_impl(vin=vin)
	has_error = bool(decoded.get("error"))
	urls: Dict[str, str] = {}
	if decoded.get("recommended_manual_url"):
		urls["manual"] = decoded["recommended_manual_url"]
	norm_vin = decoded.get("vin")
	if norm_vin and frappe.db.exists("Repair Vehicle", norm_vin):
		urls["desk"] = f"{get_url()}/app/repair-vehicle/{norm_vin}"

	msg = (
		decoded["error"]
		if has_error
		else _("Decoded Tesla VIN {0} ({1} {2}).").format(
			norm_vin, decoded.get("model_year") or "", decoded.get("model") or ""
		).strip()
	)
	return build_agent_envelope(
		success=not has_error,
		urls=urls,
		data=decoded,
		message=msg,
		error=decoded.get("error"),
		include_summary=include_summary,
		summary_builder=lambda: build_vin_decode_summary(decoded),
	)


@frappe.whitelist()
def search_vehicles(
	query: Optional[str] = None,
	customer: Optional[str] = None,
	model: Optional[str] = None,
	limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Search Repair Vehicles by VIN, license plate, model, or linked customer."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	query = _optional_str(query, "Query")
	customer = _optional_str(customer, "Customer")
	model = _optional_str(model, "Model")
	if customer:
		frappe.has_permission("Customer", "read", throw=True)
		if frappe.db.exists("Customer", customer):
			frappe.has_permission("Customer", "read", doc=customer, throw=True)
	vehicles = _search_vehicles_impl(query=query, customer=customer, model=model, limit=cint(limit) or 20) or []
	return build_agent_envelope(
		success=True,
		urls={"list": f"{get_url()}/app/repair-vehicle"},
		data={"count": len(vehicles), "vehicles": vehicles},
		message=_("Found {0} matching vehicle(s).").format(len(vehicles)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Vehicle Search ({len(vehicles)} match(es))\n"
			+ (
				"\n".join(
					f"- `{v.get('name')}` | {v.get('model_year') or ''} {v.get('model') or ''} "
					f"| Plate: `{v.get('license_plate') or '-'}` | Owner: `{v.get('customer_name') or v.get('customer') or '-'}`"
					for v in vehicles[:15]
				)
				or "_No matching vehicles found._"
			)
		),
	)


@frappe.whitelist()
def get_vehicle_intelligence(
	vehicle: str,
	history_limit: int = 20,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return decoded VIN specs, owners, Repair Order history, and open deferred services for a vehicle."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	frappe.has_permission("Repair Order", "read", throw=True)
	vehicle = _require_str(vehicle, "Repair Vehicle")
	if frappe.db.exists("Repair Vehicle", vehicle):
		frappe.has_permission("Repair Vehicle", "read", doc=vehicle, throw=True)
	intel = _get_vehicle_intelligence_impl(vehicle=vehicle, history_limit=cint(history_limit) or 20)
	veh = intel.get("vehicle") or {}
	veh_name = veh.get("name") or vehicle
	urls: Dict[str, str] = {"desk": f"{get_url()}/app/repair-vehicle/{veh_name}"}
	if intel.get("recommended_manual_url"):
		urls["manual"] = intel["recommended_manual_url"]

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=intel,
		message=_("Retrieved vehicle intelligence for {0}.").format(veh_name),
		include_summary=include_summary,
		summary_builder=lambda: build_vehicle_intel_summary(intel),
	)


@frappe.whitelist()
def link_vehicle_customer(
	vehicle: str,
	customer: str,
	is_primary: Any = False,
	relationship: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Link or update a primary or secondary customer owner on a Repair Vehicle."""
	frappe.has_permission("Repair Vehicle", "write", throw=True)
	frappe.has_permission("Customer", "read", throw=True)
	vehicle = _require_str(vehicle, "Repair Vehicle")
	customer = _require_str(customer, "Customer")
	relationship = _optional_str(relationship, "Relationship")
	if frappe.db.exists("Repair Vehicle", vehicle):
		frappe.has_permission("Repair Vehicle", "write", doc=vehicle, throw=True)
	if frappe.db.exists("Customer", customer):
		frappe.has_permission("Customer", "read", doc=customer, throw=True)
	res = _link_vehicle_customer_impl(
		vehicle=vehicle,
		customer=customer,
		is_primary=coerce_bool(is_primary),
		relationship=relationship,
	)
	veh_name = res.get("vehicle") or vehicle
	urls = {
		"desk": f"{get_url()}/app/repair-vehicle/{veh_name}",
		"customer": f"{get_url()}/app/customer/{customer}",
	}
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=res,
		message=_("Linked customer {0} to vehicle {1}.").format(customer, veh_name),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Customer Linked to Vehicle `{veh_name}`\n"
			f"- **Customer**: `{customer}` | **Primary**: `{res.get('primary_customer')}`\n"
			f"- **Total Owners**: `{len(res.get('owners') or [])}`"
		),
	)


@frappe.whitelist()
def list_inspection_templates(
	include_inactive: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""List vehicle check-in inspection templates with item counts."""
	frappe.has_permission("Inspection Template", "read", throw=True)
	res = _list_inspection_templates_impl(include_inactive=coerce_bool(include_inactive))
	templates = res.get("templates") or []
	return build_agent_envelope(
		success=True,
		urls={"list": f"{get_url()}/app/inspection-template"},
		data=res,
		message=_("Found {0} inspection template(s).").format(res.get("count", len(templates))),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Inspection Templates ({len(templates)})\n"
			+ (
				"\n".join(
					f"- `{t.get('name')}` ({'Active' if t.get('is_active') else 'Inactive'}, {t.get('item_count', 0)} items)"
					for t in templates
				)
				or "_No inspection templates found._"
			)
		),
	)


@frappe.whitelist()
def get_inspection_template(
	template_name: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Retrieve an Inspection Template and its ordered checklist items."""
	frappe.has_permission("Inspection Template", "read", throw=True)
	template_name = _optional_str(template_name, "Inspection Template")
	if template_name and frappe.db.exists("Inspection Template", template_name):
		frappe.has_permission("Inspection Template", "read", doc=template_name, throw=True)
	res = _get_inspection_template_impl(template_name=template_name)
	items = res.get("items") or []
	return build_agent_envelope(
		success=True,
		urls={"desk": f"{get_url()}/app/inspection-template/{res['name']}"},
		data=res,
		message=_("Loaded inspection template {0} ({1} item(s)).").format(res["name"], len(items)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Inspection Template: `{res['name']}` ({len(items)} items)\n"
			+ "\n".join(f"- {it.get('idx')}. {it.get('inspection_description')}" for it in items[:15])
		),
	)


@frappe.whitelist()
def apply_inspection_template(
	repair_order: str,
	template_name: Optional[str] = None,
	replace_existing: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Apply an Inspection Template's checklist items onto a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	repair_order = ro.name
	template_name = _optional_str(template_name, "Inspection Template")
	frappe.has_permission("Inspection Template", "read", throw=True)
	if template_name and frappe.db.exists("Inspection Template", template_name):
		frappe.has_permission("Inspection Template", "read", doc=template_name, throw=True)
	res = _apply_inspection_template_impl(
		repair_order=repair_order,
		template_name=template_name,
		replace_existing=coerce_bool(replace_existing),
	)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data=res,
		message=_("Applied template {0} to {1} ({2} added, {3} skipped).").format(
			res.get("template_name"),
			repair_order,
			res.get("added_count", 0),
			res.get("skipped_count", 0),
		),
		include_summary=include_summary,
		summary_builder=lambda: build_inspection_summary(res, heading="Inspection Template Applied"),
	)


@frappe.whitelist()
def get_check_in_inspection(
	repair_order: str,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return current check-in inspection items and summary counts for a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="read")
	repair_order = ro.name
	res = _get_check_in_inspection_impl(repair_order=repair_order)
	summary = res.get("summary") or {}
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data=res,
		message=_("Inspection for {0}: {1}/{2} items completed.").format(
			repair_order, summary.get("completed", 0), summary.get("total", 0)
		),
		include_summary=include_summary,
		summary_builder=lambda: build_inspection_summary(res),
	)


@frappe.whitelist()
def record_check_in_inspection(
	repair_order: str,
	items: Optional[Union[List[Any], str]] = None,
	odometer_in: Optional[Union[int, float, str]] = None,
	odometer: Optional[Union[int, float, str]] = None,
	customer_concern: Optional[str] = None,
	template_name: Optional[str] = None,
	auto_apply_template: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Record or update check-in intake fields and inspection findings on a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	repair_order = ro.name
	template_name = _optional_str(template_name, "Inspection Template")
	if coerce_bool(auto_apply_template) or template_name:
		frappe.has_permission("Inspection Template", "read", throw=True)
		if template_name and frappe.db.exists("Inspection Template", template_name):
			frappe.has_permission("Inspection Template", "read", doc=template_name, throw=True)
	res = _record_check_in_inspection_impl(
		repair_order=repair_order,
		items=items,
		odometer_in=odometer_in,
		odometer=odometer,
		customer_concern=customer_concern,
		template_name=template_name,
		auto_apply_template=coerce_bool(auto_apply_template),
	)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data=res,
		message=_("Recorded check-in inspection on {0} ({1} updated, {2} added).").format(
			repair_order, res.get("updated_count", 0), res.get("added_count", 0)
		),
		include_summary=include_summary,
		summary_builder=lambda: build_inspection_summary(res, heading="Check-In Inspection Recorded"),
	)
