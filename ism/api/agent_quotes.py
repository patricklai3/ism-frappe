# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, flt, get_url, now_datetime, time_diff_in_hours, today

from ism.api._validation import (
	coerce_bool,
	escape_like,
	get_active_company,
	optional_flt,
	optional_str,
	parse_json_arg,
	require_int,
	require_str,
)
from ism.api.agent_serializers import (
	build_agent_envelope,
	build_quote_summary,
	build_ro_urls,
	serialize_repair_order_compact,
)
from ism.api.approval import send_approval_request
from ism.api.estimation_service import estimate_repair_order
from ism.ism.doctype.repair_order.repair_order import (
	_get_default_labor_rate,
	build_service_package_row_fields,
	get_deferred_services_for_vehicle,
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


def _require_str(value: Any, label: str) -> str:
	return require_str(value, label)


def _optional_str(value: Any, label: str) -> Optional[str]:
	return optional_str(value, label)


def _require_non_negative_int(value: Any, label: str) -> int:
	"""Validate and return a mandatory non-negative integer field."""
	return require_int(value, label, min_val=0)


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
	return get_active_company()


def _get_vehicle_meta_for_ro(ro: Any) -> Optional[Dict[str, Any]]:
	from ism.utils.vehicle_display import VEHICLE_DISPLAY_FIELDS

	veh_name = getattr(ro, "vehicle", None)
	if not veh_name:
		return None
	if not frappe.has_permission("Repair Vehicle", "read", doc=veh_name):
		return None
	return frappe.db.get_value("Repair Vehicle", veh_name, list(VEHICLE_DISPLAY_FIELDS), as_dict=True) or {}


def _serialize_ro_compact(ro: Any, vehicle_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
	meta = vehicle_meta if vehicle_meta is not None else _get_vehicle_meta_for_ro(ro)
	data = serialize_repair_order_compact(ro, vehicle_meta=meta)
	for field in ("discount_amount", "shop_supplies_amount", "environmental_fee_amount", "deposit_amount"):
		if hasattr(ro, field) or getattr(ro, field, None) is not None:
			data[field] = flt(getattr(ro, field, 0.0), 2)
	ro_services = getattr(ro, "services", None) or []
	for svc_obj, svc_dict in zip(ro_services, data.get("services") or []):
		if hasattr(svc_obj, "discount_amount") or getattr(svc_obj, "discount_amount", None) is not None:
			svc_dict["discount_amount"] = flt(getattr(svc_obj, "discount_amount", 0.0), 2)
		if hasattr(svc_obj, "advisor_notes") or getattr(svc_obj, "advisor_notes", None) is not None:
			svc_dict["advisor_notes"] = getattr(svc_obj, "advisor_notes", None)
	return data


def _resolve_customer(customer_name: str, phone: Optional[str] = None, email: Optional[str] = None) -> str:
	"""Find an existing Customer or provision a new one with optional contact info."""
	frappe.has_permission("Customer", "read", throw=True)
	if not customer_name:
		customer_name = "Walk-in Customer"

	def _find_and_update_existing() -> Optional[str]:
		existing = (
			customer_name
			if frappe.db.exists("Customer", customer_name)
			else frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
		)
		if not existing:
			return None
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

	existing = _find_and_update_existing()
	if existing:
		return existing

	frappe.has_permission("Customer", "create", throw=True)
	cust_group = "Individual" if frappe.db.exists("Customer Group", "Individual") else "All Customer Groups"
	territory = (
		"All Territories"
		if frappe.db.exists("Territory", "All Territories")
		else frappe.db.get_value("Territory", {}, "name")
	)

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

	try:
		doc = frappe.get_doc(payload).insert()
		return doc.name
	except frappe.DuplicateEntryError:
		retry_existing = _find_and_update_existing()
		if retry_existing:
			return retry_existing
		raise


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
	"""Return compact open deferred services for a vehicle, excluding active (non-declined) titles on ro."""
	existing_titles = {
		str(getattr(s, "service_title", "") or "").strip().lower()
		for s in (getattr(ro, "services", None) or [])
		if getattr(s, "service_title", None)
		and str(getattr(s, "status", "") or "") != "Declined"
	}
	items = []
	for d in get_deferred_services_for_vehicle(vehicle) or []:
		title = str(d.get("job_title") or "").strip()
		if not title or title.lower() in existing_titles:
			continue
		items.append(
			{
				"job_title": title,
				"estimated_amount": flt(d.get("estimated_amount"), 2),
				"decline_date": str(d.get("decline_date") or ""),
				"decline_reason": d.get("decline_reason") or "",
				"safety_hazard": cint(d.get("safety_hazard", 0)),
				"repair_order": d.get("repair_order"),
			}
		)
	return items


def _resolve_vehicle(
	vehicle_identifier: str,
	customer_name: str,
	*,
	vehicle_color: Optional[str] = None,
	license_plate: Optional[str] = None,
) -> str:
	"""Find an existing Repair Vehicle by name, plate, or VIN, or create one safely."""
	from ism.utils.vehicle_display import (
		normalize_license_plate,
		normalize_vehicle_color,
		require_vehicle_intake_fields,
	)

	frappe.has_permission("Repair Vehicle", "read", throw=True)
	cleaned = (vehicle_identifier or "").strip()
	if not cleaned:
		cleaned = f"PLATE-{frappe.generate_hash(length=10).upper()}"

	clean_color = normalize_vehicle_color(vehicle_color)
	clean_plate = normalize_license_plate(license_plate)

	veh = _lookup_vehicle(cleaned)
	if veh:
		frappe.has_permission("Repair Vehicle", "read", doc=veh, throw=True)
		if clean_color or clean_plate:
			v_doc = frappe.get_doc("Repair Vehicle", veh)
			v_doc.check_permission("write")
			changed = False
			if clean_color and clean_color != normalize_vehicle_color(getattr(v_doc, "color", None)):
				v_doc.color = clean_color
				changed = True
			if clean_plate and clean_plate != normalize_license_plate(getattr(v_doc, "license_plate", None)):
				v_doc.license_plate = clean_plate
				changed = True
			if changed:
				v_doc.save()
		return veh

	frappe.has_permission("Repair Vehicle", "create", throw=True)
	is_vin = len(cleaned) == 17
	vin = cleaned.upper() if is_vin else f"PLATE-{frappe.generate_hash(length=10).upper()}"
	plate = clean_plate or ("" if is_vin else normalize_license_plate(cleaned))
	req_color, req_plate = require_vehicle_intake_fields(
		color=clean_color,
		license_plate=plate,
		context="creating a Repair Vehicle",
	)

	try:
		v_doc = frappe.get_doc(
			{
				"doctype": "Repair Vehicle",
				"vin": vin,
				"color": req_color,
				"license_plate": req_plate,
				"customer": customer_name,
				"manufacturer": "Tesla",
				"model": "Model 3",
			}
		).insert()
		return v_doc.name
	except frappe.DuplicateEntryError:
		retry_veh = _lookup_vehicle(cleaned) or _lookup_vehicle(vin)
		if retry_veh:
			frappe.has_permission("Repair Vehicle", "read", doc=retry_veh, throw=True)
			return retry_veh
		raise


def _resolve_and_validate_parts(raw_parts: Any) -> List[Dict[str, Any]]:
	"""Validate part item_codes against Item master (resolving part_number aliases when unambiguous)."""
	frappe.has_permission("Item", "read", throw=True)
	parsed = parse_json_arg(raw_parts, [], label="parts") if isinstance(raw_parts, str) else raw_parts
	if not parsed:
		return []
	if isinstance(parsed, dict):
		parsed = [parsed]
	if not isinstance(parsed, list):
		frappe.throw(_("Service parts must be a list of part dicts."), frappe.ValidationError)

	resolved: List[Dict[str, Any]] = []
	for entry in parsed:
		if not isinstance(entry, dict):
			frappe.throw(_("Each service part entry must be a dict."), frappe.ValidationError)
		part = dict(entry)
		code = str(part.get("item_code") or "").strip()
		if not code:
			frappe.throw(_("Each part entry must include 'item_code'."), frappe.ValidationError)
		if not frappe.db.exists("Item", code):
			matched = frappe.get_list("Item", filters={"oem_part_number": code}, pluck="name", limit=2)
			if not matched:
				escaped_code = escape_like(code.upper())
				matched = frappe.get_list(
					"Item",
					filters={"name": ["like", f"%-{escaped_code}-%"]},
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
		frappe.get_cached_doc("Item", part["item_code"]).check_permission("read")
		resolved.append(part)
	return resolved


def _resolve_service_package_name(
	raw_name: Optional[str],
	vehicle_doc: Any = None,
) -> Optional[str]:
	if not raw_name or not isinstance(raw_name, str):
		return None
	cleaned = raw_name.strip()
	if not cleaned:
		return None
	exact_name = frappe.db.get_value("Service Package", {"name": cleaned}, "name")
	candidates = frappe.db.get_all(
		"Service Package",
		filters={"package_name": cleaned},
		fields=[
			"name",
			"applicable_model",
			"applicable_generation",
			"applicable_year_from",
			"applicable_year_to",
		],
	)
	if exact_name and (not vehicle_doc or len(candidates) <= 1):
		return exact_name
	if not candidates:
		return exact_name
	if len(candidates) == 1:
		return candidates[0]["name"]

	if vehicle_doc is not None:
		from ism.scheduling.bookable_catalog import is_package_eligible_for_vehicle

		v_model = getattr(vehicle_doc, "model", None) or (vehicle_doc.get("model") if isinstance(vehicle_doc, dict) else None)
		v_year = getattr(vehicle_doc, "model_year", None) or (vehicle_doc.get("model_year") if isinstance(vehicle_doc, dict) else None)
		v_gen = getattr(vehicle_doc, "vehicle_generation", None) or (vehicle_doc.get("vehicle_generation") if isinstance(vehicle_doc, dict) else None)
		v_month = getattr(vehicle_doc, "manufactured_month", None) or (vehicle_doc.get("manufactured_month") if isinstance(vehicle_doc, dict) else None)

		pkg_names = [c["name"] for c in candidates]
		compat_by_parent: Dict[str, List[Dict[str, Any]]] = {}
		for crow in frappe.get_all(
			"Service Package Generation",
			filters={"parenttype": "Service Package", "parent": ["in", pkg_names]},
			fields=["parent", "applicable_model", "generation_name", "year_from", "month_from", "year_to", "month_to"],
		):
			compat_by_parent.setdefault(crow["parent"], []).append(crow)

		best_score = -1
		best_name = None
		for cand in candidates:
			cand_with_compat = dict(cand, generation_compatibility=compat_by_parent.get(cand["name"], []))
			if not is_package_eligible_for_vehicle(
				cand_with_compat,
				vehicle_model=v_model,
				vehicle_year=cint(v_year) or None,
				vehicle_generation=v_gen,
				manufactured_month=v_month,
			):
				continue
			c_model = (cand.get("applicable_model") or "").strip()
			c_gen = (cand.get("applicable_generation") or "").strip()
			score = 1
			if c_model and v_model and c_model == v_model:
				score += 2
			if c_gen:
				score += 4
			if v_gen and c_gen == v_gen:
				score += 8
			if score > best_score:
				best_score = score
				best_name = cand["name"]
		if best_name:
			return best_name

	return exact_name or candidates[0]["name"]


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


def _coerce_service_list(raw_services: Any, label: str = "services") -> List[Any]:
	"""Parse services/add_services into a list, handling single dicts cleanly and throwing on invalid JSON."""
	if raw_services is None:
		return []
	if isinstance(raw_services, list):
		return raw_services
	if isinstance(raw_services, dict):
		return [raw_services]
	if isinstance(raw_services, str):
		stripped = raw_services.strip()
		if not stripped:
			return []
		parsed = parse_json_arg(stripped, [], label=label)
		if isinstance(parsed, list):
			return parsed
		if isinstance(parsed, dict):
			return [parsed]
		if isinstance(parsed, str):
			return [{"service_title": parsed}]
		frappe.throw(_("{0} must be a list of service specifications.").format(label), frappe.ValidationError)
	frappe.throw(_("{0} must be a list of service specifications.").format(label), frappe.ValidationError)


def _build_service_row(
	service_input: Any,
	default_labor_rate: float,
	company: Optional[str] = None,
	vehicle_doc: Any = None,
) -> Optional[Dict[str, Any]]:
	"""Normalize a single service specification into a Repair Order Service row dict."""
	if isinstance(service_input, str):
		cleaned_input = service_input.strip()
		if not cleaned_input:
			return None
		pkg_name = _resolve_service_package_name(cleaned_input, vehicle_doc=vehicle_doc)
		if pkg_name:
			frappe.has_permission("Service Package", "read", doc=pkg_name, throw=True)
			pkg = frappe.get_doc("Service Package", pkg_name)
			return build_service_package_row_fields(
				pkg=pkg,
				company=company,
				default_labor_rate=default_labor_rate,
			)
		return {
			"service_title": cleaned_input,
			"labor_hours": 1.0,
			"labor_rate": default_labor_rate,
		}

	if not isinstance(service_input, dict):
		frappe.throw(_("Each service item must be a service name or dictionary."), frappe.ValidationError)

	explicit_title = _first_present(service_input, _SERVICE_TITLE_ALIASES, strip_str=True)
	explicit_hours = _first_present(service_input, _SERVICE_HOURS_ALIASES, strip_str=True)
	pkg_lookup = optional_str(service_input.get("service_package"), "Service Package") or explicit_title
	pkg_name = _resolve_service_package_name(pkg_lookup, vehicle_doc=vehicle_doc)

	if pkg_name:
		frappe.has_permission("Service Package", "read", doc=pkg_name, throw=True)
		pkg = frappe.get_doc("Service Package", pkg_name)
		s_data = build_service_package_row_fields(
			pkg=pkg,
			company=company,
			default_labor_rate=default_labor_rate,
		)
		if explicit_title:
			s_data["service_title"] = explicit_title
		if service_input.get("service_position") is not None:
			s_data["service_position"] = optional_str(service_input["service_position"], "Service Position") or "None"
		if service_input.get("correction_code") is not None:
			s_data["correction_code"] = optional_str(service_input["correction_code"], "Correction Code") or ""

		has_explicit_flat = "is_flat_rate" in service_input and service_input.get("is_flat_rate") is not None
		if has_explicit_flat:
			s_data["is_flat_rate"] = 1 if coerce_bool(service_input["is_flat_rate"]) else 0

		has_explicit_hours = explicit_hours is not None
		if has_explicit_hours:
			s_data["labor_hours"] = optional_flt(explicit_hours, "Labor Hours", non_negative=True, default=0.0)

		has_explicit_rate = service_input.get("labor_rate") is not None
		if has_explicit_rate:
			s_data["labor_rate"] = optional_flt(
				service_input["labor_rate"], "Labor Rate", non_negative=True, default=0.0
			)

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

	hours_val = (
		optional_flt(explicit_hours, "Labor Hours", non_negative=True, default=1.0)
		if explicit_hours is not None
		else 1.0
	)
	explicit_rate = optional_flt(service_input.get("labor_rate"), "Labor Rate", non_negative=True, default=None)
	s_data: Dict[str, Any] = {
		"service_title": str(explicit_title).strip() if explicit_title else "Service Operation",
		"service_package": None,
		"labor_hours": hours_val,
		"labor_rate": explicit_rate if explicit_rate is not None else default_labor_rate,
		"is_flat_rate": 1 if coerce_bool(service_input.get("is_flat_rate", 0)) else 0,
		"service_position": optional_str(service_input.get("service_position"), "Service Position") or "None",
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
	vehicle_color: Optional[str] = None,
	color: Optional[str] = None,
	license_plate: Optional[str] = None,
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
	frappe.has_permission("Repair Order", "create", throw=True)
	cust_in = optional_str(customer_name, "customer_name") or optional_str(customer, "customer")
	veh_in = optional_str(vehicle_identifier, "vehicle_identifier") or optional_str(vehicle, "vehicle")
	color_in = optional_str(vehicle_color, "vehicle_color") or optional_str(color, "color")
	plate_in = optional_str(license_plate, "license_plate")
	notes_str = optional_str(notes, "notes")
	concern_str = optional_str(concern, "concern")
	notes_in = notes_str if notes_str is not None else concern_str
	phone_in = optional_str(customer_phone, "customer_phone")
	email_in = optional_str(customer_email, "customer_email")

	if not cust_in:
		frappe.throw(_("Customer name or customer identifier is required."), frappe.MandatoryError)
	if not veh_in:
		frappe.throw(_("Vehicle identifier is required."), frappe.MandatoryError)

	parsed_services = _coerce_service_list(services, label="services")
	if parts is None:
		parsed_parts: List[Any] = []
	else:
		raw_parsed_parts = parse_json_arg(parts, [], label="parts") if isinstance(parts, str) else parts
		if isinstance(raw_parsed_parts, dict):
			parsed_parts = [raw_parsed_parts]
		elif isinstance(raw_parsed_parts, list):
			parsed_parts = raw_parsed_parts
		elif not raw_parsed_parts:
			parsed_parts = []
		else:
			frappe.throw(_("Parts must be a list of part dicts."), frappe.ValidationError)

	validated_odometer_in = _require_non_negative_int(odometer_in, "Odometer In")
	notes_clean = notes_in or ""
	if not parsed_services and not notes_clean:
		frappe.throw(
			_("At least one service or customer concern/notes is required."),
			frappe.MandatoryError,
		)

	comp = get_active_company(company)
	cust = _resolve_customer(cust_in, phone=phone_in, email=email_in)
	veh = _resolve_vehicle(veh_in, cust, vehicle_color=color_in, license_plate=plate_in)

	ro = frappe.new_doc("Repair Order")
	ro.company = comp
	ro.customer = cust
	ro.vehicle = veh
	ro.status = "Draft Estimate"
	ro.approval_status = "Estimating"
	ro.customer_concern = notes_clean
	ro.posting_date = today()
	ro.odometer_in = validated_odometer_in

	default_labor_rate = _get_default_labor_rate()
	if not parsed_services:
		parsed_services = [{"service_title": "Initial Inspection & Diagnostics", "labor_hours": 1.0}]

	veh_doc = frappe.get_cached_doc("Repair Vehicle", veh) if veh and frappe.db.exists("Repair Vehicle", veh) else None
	for item in parsed_services:
		row = _build_service_row(item, default_labor_rate, company=comp, vehicle_doc=veh_doc)
		if row:
			ro.append("services", row)

	if parsed_parts and ro.services:
		first_s = ro.services[0]
		existing_p = parse_json_arg(first_s.parts, [], label="Service Parts") or []
		if not isinstance(existing_p, list):
			existing_p = []
		existing_p.extend(_resolve_and_validate_parts(parsed_parts))
		first_s.parts = json.dumps(existing_p)

	ro.insert()
	duration_min = _estimate_duration_minutes(ro)
	pending_deferred = _serialize_pending_deferred(veh, ro=ro)

	data = _serialize_ro_compact(ro)
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


def _apply_service_row_updates(ro: Any, update_services: Any) -> None:
	"""Apply validated in-place updates to unapproved service rows on a Repair Order."""
	if update_services in (None, [], ""):
		return
	parsed = parse_json_arg(update_services, [], label="update_services")
	if isinstance(parsed, dict):
		parsed = [parsed]
	if not isinstance(parsed, list):
		frappe.throw(_("update_services must be a list of service update dictionaries."), frappe.ValidationError)

	ro_services = getattr(ro, "services", None) or []
	row_by_name = {getattr(r, "name", None): r for r in ro_services if getattr(r, "name", None)}

	for entry in parsed:
		if not isinstance(entry, dict):
			frappe.throw(_("Each update_services entry must be a dictionary."), frappe.ValidationError)

		row_id = optional_str(entry.get("service_row_id"), "service_row_id")
		raw_idx = entry.get("idx")
		has_idx = raw_idx is not None and not (isinstance(raw_idx, str) and not raw_idx.strip())
		if row_id is not None and has_idx:
			frappe.throw(
				_("Specify either service_row_id or idx in update_services, not both."),
				frappe.ValidationError,
			)
		if row_id is None and not has_idx:
			frappe.throw(
				_("Each update_services entry must specify service_row_id or 1-based idx."),
				frappe.ValidationError,
			)

		if row_id is not None:
			row = row_by_name.get(row_id)
			if row is None:
				frappe.throw(
					_("Service row '{0}' not found on {1}.").format(row_id, ro.name),
					frappe.ValidationError,
				)
		else:
			idx_1based = require_int(raw_idx, "idx", min_val=1)
			if idx_1based > len(ro_services):
				frappe.throw(
					_("Service row idx {0} is out of range (1..{1}).").format(idx_1based, len(ro_services)),
					frappe.ValidationError,
				)
			row = ro_services[idx_1based - 1]

		if getattr(row, "approval_status", None) == "Approved" or getattr(row, "status", None) == "Approved":
			label = getattr(row, "service_title", None) or getattr(row, "name", None) or ""
			frappe.throw(
				_(
					"Cannot amend approved service row '{0}' in-place. Use add_supplement or unapprove/re-authorize first."
				).format(label),
				frappe.ValidationError,
			)

		title_val = _first_present(entry, ("service_title", "title", "job_title"), strip_str=True)
		if title_val is not None:
			row.service_title = require_str(title_val, "Service Title")

		hours_val = _first_present(entry, _SERVICE_HOURS_ALIASES, strip_str=True)
		if hours_val is not None:
			row.labor_hours = optional_flt(hours_val, "Labor Hours", non_negative=True, default=0.0)

		if "labor_rate" in entry and entry["labor_rate"] is not None:
			row.labor_rate = optional_flt(entry["labor_rate"], "Labor Rate", non_negative=True, default=0.0)

		if "discount_amount" in entry and entry["discount_amount"] is not None:
			disc = optional_flt(entry["discount_amount"], "Service Discount Amount", non_negative=True, default=0.0)
			setattr(row, "discount_amount", flt(disc, 2))

		if "parts" in entry and entry["parts"] is not None:
			raw_parts = entry["parts"]
			if raw_parts in ([], ""):
				row.parts = json.dumps([])
				row.parts_total = 0.0
			else:
				validated_parts = _resolve_and_validate_parts(raw_parts)
				row.parts = json.dumps(validated_parts)
				row.parts_total = sum(
					flt(
						p.get("amount") if p.get("amount") is not None else flt(p.get("qty", 1)) * flt(p.get("rate", 0)),
						2,
					)
					for p in validated_parts
				)

		if "advisor_notes" in entry or "notes" in entry:
			raw_note = entry["advisor_notes"] if "advisor_notes" in entry else entry["notes"]
			setattr(row, "advisor_notes", optional_str(raw_note, "Advisor Notes") or "")

		if not cint(getattr(row, "is_flat_rate", 0)):
			row.labor_amount = flt(flt(getattr(row, "labor_hours", 0.0)) * flt(getattr(row, "labor_rate", 0.0)), 2)
		row.total_amount = max(
			0.0,
			flt(
				flt(getattr(row, "labor_amount", 0.0))
				+ flt(getattr(row, "parts_total", 0.0))
				- flt(getattr(row, "discount_amount", 0.0)),
				2,
			),
		)


@frappe.whitelist()
def amend_quote(
	repair_order: str,
	add_services: Optional[Union[List[Any], str]] = None,
	remove_service_indices: Optional[Union[List[int], str]] = None,
	update_services: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	discount_amount: Optional[Union[float, int, str]] = None,
	shop_supplies_amount: Optional[Union[float, int, str]] = None,
	environmental_fee_amount: Optional[Union[float, int, str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Safely amend lines on an unapproved Repair Order in Draft Estimate state."""
	ro = _get_ro_doc(repair_order, ptype="write")
	parsed_remove_raw = parse_json_arg(remove_service_indices, [], label="remove_service_indices")
	validated_remove: List[int] = []
	if parsed_remove_raw not in (None, [], ""):
		if not isinstance(parsed_remove_raw, list):
			frappe.throw(_("remove_service_indices must be a list of integers."), frappe.ValidationError)
		for entry in parsed_remove_raw:
			validated_remove.append(require_int(entry, "Service Index", min_val=0))

	has_updates = update_services not in (None, [], "")
	if (
		ro.docstatus != 0
		or ro.approval_status == "Approved"
		or (ro.approval_status == "Partially Approved" and (not has_updates or bool(validated_remove)))
	):
		frappe.throw(
			_("Cannot amend an approved or submitted Repair Order. Customer re-authorization is legally required."),
			frappe.ValidationError,
		)

	_apply_service_row_updates(ro, update_services)

	parsed_add = _coerce_service_list(add_services, label="add_services")
	notes_str = optional_str(notes, "Notes")

	if validated_remove:
		for idx in sorted(set(validated_remove), reverse=True):
			if 0 <= idx < len(ro.services):
				ro.services.pop(idx)

	default_labor_rate = _get_default_labor_rate()
	if parsed_add:
		veh_doc = (
			frappe.get_cached_doc("Repair Vehicle", ro.vehicle)
			if ro.vehicle and frappe.db.exists("Repair Vehicle", ro.vehicle)
			else None
		)
		for item in parsed_add:
			row = _build_service_row(item, default_labor_rate, company=ro.company, vehicle_doc=veh_doc)
			if row:
				ro.append("services", row)

	if notes_str is not None:
		ro.customer_concern = notes_str

	for field, label, raw_val in (
		("discount_amount", "Discount Amount", discount_amount),
		("shop_supplies_amount", "Shop Supplies Amount", shop_supplies_amount),
		("environmental_fee_amount", "Environmental Fee Amount", environmental_fee_amount),
	):
		if raw_val is not None and not (isinstance(raw_val, str) and not raw_val.strip()):
			val = optional_flt(raw_val, label, non_negative=True, default=0.0)
			setattr(ro, field, flt(val, 2))

	ro.save()
	duration_min = _estimate_duration_minutes(ro)

	data = _serialize_ro_compact(ro)
	data["total_duration_minutes"] = duration_min
	return build_agent_envelope(
		success=True,
		data=data,
		urls=build_ro_urls(ro.name),
		message=_("Quote {0} updated successfully.").format(ro.name),
		include_summary=include_summary,
		summary_builder=lambda: build_quote_summary(ro, duration_min, "Repair Order Quote (Updated)"),
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
	medium = (optional_str(communication_medium, "Communication Medium") or "sms").lower()
	channel = "link" if medium in ("copy link only", "link", "url", "none") else medium

	target_recipient = (
		optional_str(recipient, "Recipient")
		if recipient is not None
		else optional_str(phone_number, "Phone Number")
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
def update_job_status(
	repair_order: str,
	action: str,
	odometer_out: Optional[int] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Technician & Advisor 1-tap state transition enforcement with legal authorization locks."""
	ro = _get_ro_doc(repair_order, ptype="write")
	action_key = require_str(action, "Action").lower()
	notes_str = optional_str(notes, "Notes")
	if action_key not in ("start", "hold", "parts", "park", "complete"):
		frappe.throw(
			_("Invalid action {0}. Valid actions are: start, hold, parts, park, complete.").format(action),
			frappe.ValidationError,
		)

	if ro.approval_status not in ["Approved", "Partially Approved"]:
		frappe.throw(
			_("Cannot transition Repair Order {0}: customer authorization is required first.").format(ro.name),
			frappe.ValidationError,
		)

	parts_deficit: List[Any] = []
	if action_key == "park":
		if odometer_out is not None and not (isinstance(odometer_out, str) and not odometer_out.strip()):
			ro.odometer_out = require_int(odometer_out, "Odometer Out", min_val=0)
		ro.park_vehicle()
		ro.reload()
		if notes_str:
			ro.add_comment("Comment", notes_str)
	else:
		if action_key == "start":
			ro.manual_parts_hold = 0
			ro.status = "Authorized - In Progress"
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
		if action_key == "start":
			if ro.status == "Waiting on Parts":
				_all_avail, parts_deficit = ro._check_parts_stock_availability()
			elif ro.status == "Authorized - In Progress":
				stamp_updates: Dict[str, Any] = {"vehicle_location": "In Bay / Being Serviced"}
				if not ro.picked_up_at:
					stamp_updates["picked_up_at"] = now_datetime()
				if not ro.picked_up_by:
					stamp_updates["picked_up_by"] = frappe.session.user
				ro.db_set(stamp_updates, update_modified=False)
		elif action_key == "complete":
			from ism.ism.doctype.repair_order.repair_order import complete_linked_schedule_entries

			complete_linked_schedule_entries(ro.name)
		if notes_str:
			ro.add_comment("Comment", notes_str)

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


def _parse_checkout_payments(payments: Any) -> Optional[List[Dict[str, Any]]]:
	"""Validate and normalize split-tender payments list for checkout_repair_order."""
	if payments is None or (isinstance(payments, str) and not payments.strip()):
		return None
	parsed = parse_json_arg(payments, None, label="payments") if isinstance(payments, str) else payments
	if not isinstance(parsed, list) or not parsed:
		frappe.throw(_("payments must be a non-empty list of payment tender objects."), frappe.ValidationError)
	normalized: List[Dict[str, Any]] = []
	for idx, entry in enumerate(parsed, start=1):
		if not isinstance(entry, dict):
			frappe.throw(_("Each payments entry (#{0}) must be a dictionary.").format(idx), frappe.ValidationError)
		pm = optional_str(entry.get("payment_method"), f"payments[{idx}].payment_method")
		if not pm:
			frappe.throw(_("payments[{0}].payment_method is required.").format(idx), frappe.MandatoryError)
		amt = optional_flt(entry.get("amount"), f"payments[{idx}].amount", positive=True, default=None)
		if amt is None:
			frappe.throw(_("payments[{0}].amount must be a positive number.").format(idx), frappe.ValidationError)
		item: Dict[str, Any] = {"payment_method": pm, "amount": flt(amt, 2)}
		ref = optional_str(entry.get("reference_no"), f"payments[{idx}].reference_no")
		if ref is not None:
			item["reference_no"] = ref
		normalized.append(item)
	return normalized


@frappe.whitelist()
def record_ro_deposit(
	repair_order: str,
	amount: Union[float, int, str],
	payment_method: str = "Debit / Interac",
	reference_no: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Record a customer deposit payment against a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	deposit_amt = optional_flt(amount, "Amount", positive=True, default=None)
	if deposit_amt is None:
		frappe.throw(_("Amount is required and must be positive."), frappe.MandatoryError)
	method = optional_str(payment_method, "Payment Method") or "Debit / Interac"
	ref_no = optional_str(reference_no, "Reference No")

	deposit_res = ro.record_deposit(
		amount=flt(deposit_amt, 2),
		payment_method=method,
		reference_no=ref_no,
	)
	ro.reload()

	pe_name = None
	if isinstance(deposit_res, dict):
		pe_name = deposit_res.get("payment_entry")
	elif isinstance(deposit_res, str) and deposit_res.strip():
		pe_name = deposit_res.strip()

	base_url = get_url()
	urls = build_ro_urls(ro.name)
	if pe_name:
		urls["payment_entry"] = f"{base_url}/app/payment-entry/{quote(str(pe_name), safe='')}"

	resolved_deposit_amt = flt(
		getattr(ro, "deposit_amount", None)
		or (deposit_res.get("deposit_amount") if isinstance(deposit_res, dict) else 0.0),
		2,
	)

	data: Dict[str, Any] = {
		"repair_order": ro.name,
		"status": ro.status,
		"payment_status": getattr(ro, "payment_status", None),
		"amount": flt(deposit_amt, 2),
		"payment_method": method,
		"reference_no": ref_no,
		"deposit_amount": resolved_deposit_amt,
		"paid_amount": flt(getattr(ro, "paid_amount", 0.0), 2),
		"grand_total": flt(getattr(ro, "grand_total", 0.0), 2),
		"outstanding_amount": flt(getattr(ro, "outstanding_amount", 0.0), 2),
	}
	if pe_name:
		data["payment_entry"] = pe_name

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=data,
		message=_("Recorded deposit of ${0:.2f} ({1}) on {2}.").format(flt(deposit_amt, 2), method, ro.name),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Deposit Recorded: `{ro.name}`\n"
			f"- **Amount**: `${flt(deposit_amt, 2):,.2f}` ({method})\n"
			f"- **Total Deposit**: `${resolved_deposit_amt:,.2f}` | "
			f"**Outstanding**: `${flt(getattr(ro, 'outstanding_amount', 0.0), 2):,.2f}`"
		),
	)


@frappe.whitelist()
def checkout_repair_order(
	repair_order: str,
	payment_method: Optional[str] = None,
	paid_amount: Optional[float] = None,
	odometer_out: Optional[int] = None,
	payments: Optional[Union[List[Any], str]] = None,
	confirm: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Invoice, record payment, complete linked schedule entries, and release a Repair Order."""
	ro = _get_ro_doc(repair_order, ptype="write")
	parsed_payments = _parse_checkout_payments(payments)
	payment_method = optional_str(payment_method, "Payment Method")
	if not payment_method and parsed_payments is None:
		frappe.throw(_("Payment Method is required."), frappe.MandatoryError)
	explicit_paid = optional_flt(paid_amount, "Paid Amount", positive=True, default=None)
	validated_odo_out = _resolve_odometer_out(ro, odometer_out, "release a Repair Order")
	if not coerce_bool(confirm):
		frappe.throw(
			_("Pass confirm=True only after explicit operator confirmation before invoicing and releasing {0}.").format(
				ro.name
			),
			frappe.ValidationError,
		)

	release_kwargs: Dict[str, Any] = {
		"payment_method": payment_method or parsed_payments[0]["payment_method"],
		"paid_amount": explicit_paid,
		"odometer_out": validated_odo_out,
	}
	if parsed_payments is not None:
		release_kwargs["payments"] = parsed_payments
	release_res = ro.process_payment_and_release(**release_kwargs)
	ro.reload()

	pe_name = None
	if ro.sales_invoice and frappe.has_permission("Payment Entry", "read"):
		pe_name = frappe.db.get_value(
			"Payment Entry Reference",
			{"reference_doctype": "Sales Invoice", "reference_name": ro.sales_invoice, "docstatus": 1},
			"parent",
		)
	if isinstance(release_res, dict):
		pe_name = pe_name or release_res.get("payment_entry")

	base_url = get_url()
	urls = build_ro_urls(ro.name)
	if ro.sales_invoice:
		urls["sales_invoice"] = f"{base_url}/app/sales-invoice/{quote(str(ro.sales_invoice), safe='')}"
	if pe_name:
		urls["payment_entry"] = f"{base_url}/app/payment-entry/{quote(str(pe_name), safe='')}"

	data: Dict[str, Any] = {
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
	}
	if isinstance(release_res, dict):
		if "payment_entries" in release_res and release_res["payment_entries"] is not None:
			data["payment_entries"] = release_res["payment_entries"]
		if "deposit_allocated" in release_res and release_res["deposit_allocated"] is not None:
			data["deposit_allocated"] = flt(release_res["deposit_allocated"], 2)

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=data,
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
	if raw is None:
		return []
	if isinstance(raw, list):
		return [str(r).strip() for r in raw if not isinstance(r, (dict, list, tuple, set, bool)) and str(r).strip()]
	if isinstance(raw, str):
		stripped = raw.strip()
		if not stripped:
			return []
		if stripped.startswith(("[", "{")):
			parsed = parse_json_arg(stripped, [], label="service_row_ids")
			if not isinstance(parsed, list):
				frappe.throw(_("service_row_ids JSON must be a list."), frappe.ValidationError)
			return [
				str(r).strip()
				for r in parsed
				if not isinstance(r, (dict, list, tuple, set, bool)) and str(r).strip()
			]
		return [part.strip() for part in stripped.split(",") if part.strip()]
	frappe.throw(_("service_row_ids must be a list or string."), frappe.ValidationError)


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
	matched_rows: List[Any] = []
	for row_id in targets:
		found = None
		for s in ro.services or []:
			if str(s.name) == str(row_id) or str(s.idx) == str(row_id) or str(s.service_title) == str(row_id):
				found = s
				break
		if not found:
			frappe.throw(_("Service {0} not found.").format(row_id), frappe.ValidationError)
		if completed_flag and found.status != "Approved":
			frappe.throw(
				_(
					"Service '{0}' cannot be completed because its status is '{1}'. "
					"Only Approved services can be completed by technicians."
				).format(found.service_title, found.status),
				frappe.ValidationError,
			)
		if found not in matched_rows:
			matched_rows.append(found)

	now_ts = now_datetime() if completed_flag else None
	for target_service in matched_rows:
		target_service.is_completed = completed_flag
		if completed_flag:
			target_service.completed_at = now_ts
			target_service.completed_by = frappe.session.user
			if ro.picked_up_at:
				target_service.actual_duration_hours = flt(
					time_diff_in_hours(target_service.completed_at, ro.picked_up_at), 2
				)
		else:
			target_service.completed_at = None
			target_service.completed_by = None
			target_service.actual_duration_hours = 0.0

		if ro.docstatus == 1 and target_service.name and not str(target_service.name).startswith("new-"):
			frappe.db.set_value(
				"Repair Order Service",
				target_service.name,
				{
					"is_completed": target_service.is_completed,
					"completed_at": target_service.completed_at,
					"completed_by": target_service.completed_by,
					"actual_duration_hours": target_service.actual_duration_hours,
				},
				update_modified=False,
			)

	ro._update_progress_and_job_timing()
	has_approved = any(s.status == "Approved" for s in ro.services)
	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in ro.services)
	approved_services = [s for s in ro.services if s.status == "Approved"]
	all_approved_completed = bool(
		has_approved and not has_pending and all(s.is_completed for s in approved_services)
	)

	if all_approved_completed:
		if ro.vehicle_location == "Completed / Parked in Lot":
			ro.status = "Work Completed"
		elif ro.status not in ["Authorized - In Progress", "Waiting on Parts"]:
			ro.status = "Authorized - In Progress"
	elif ro.status == "Work Completed" and not all_approved_completed:
		ro.status = "Authorized - In Progress"
		if ro.vehicle_location == "Completed / Parked in Lot":
			ro.vehicle_location = "In Bay / Being Serviced" if ro.picked_up_at else "In Customer Lot"

	if ro.docstatus == 1:
		ro.db_set(
			{
				"progress_percent": ro.progress_percent,
				"status": ro.status,
				"vehicle_location": ro.vehicle_location,
			},
			update_modified=False,
		)

	ro.save()
	data = _serialize_ro_compact(ro)
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
	category = optional_str(category, "Category") or "Maintenance"
	hours_val = optional_flt(labor_hours, "Labor Hours", non_negative=True, default=1.0)
	rate_val = optional_flt(labor_rate, "Labor Rate", non_negative=True, default=None)
	notes_str = optional_str(notes, "Notes")

	resolved_parts = _resolve_and_validate_parts(parts) if parts else []
	res = ro.add_supplement(
		service_title=service_title,
		category=category,
		labor_hours=hours_val,
		labor_rate=rate_val,
		parts=resolved_parts,
		notes=notes_str,
	)
	ro.reload()
	data = _serialize_ro_compact(ro)
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
	data = _serialize_ro_compact(ro)
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
	repair_order = optional_str(repair_order, "Repair Order")
	vehicle = optional_str(vehicle, "Repair Vehicle")
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
	urls = (
		build_ro_urls(ro.name)
		if ro
		else {"vehicle": f"{get_url()}/app/repair-vehicle/{quote(str(veh_name), safe='')}"}
	)
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
	if isinstance(deferred_items, str):
		parsed = parse_json_arg(deferred_items, [], label="deferred_items")
	else:
		parsed = deferred_items
	if parsed is not None and not isinstance(parsed, (list, dict)):
		frappe.throw(_("deferred_items must be a list of deferred service items."), frappe.ValidationError)
	if isinstance(parsed, dict):
		parsed = [parsed]
	res = ro.add_deferred_services(deferred_items=parsed)
	ro.reload()
	remaining = _serialize_pending_deferred(ro.vehicle, ro=ro)
	data = _serialize_ro_compact(ro)
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
