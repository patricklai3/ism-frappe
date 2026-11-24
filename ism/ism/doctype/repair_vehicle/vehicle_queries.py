# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from typing import Any, Optional

import frappe
from frappe.desk.reportview import build_match_conditions
from frappe.utils import cint, flt

from ism.api._validation import escape_like
from ism.ism.doctype.repair_vehicle.vin_decoder import decode_tesla_vin, normalize_vin_string
from ism.utils.tesla_manual import get_tesla_manual_details_for_vehicle, get_tesla_manual_url_for_vehicle
from ism.utils.vehicle_display import build_vehicle_display_dict


def _ensure_optional_str(value: Any, label: str) -> Optional[str]:
	"""Validate optional string parameter to reject non-scalar type confusion."""
	if value is None:
		return None
	if not isinstance(value, str):
		frappe.throw(frappe._("{0} must be a string.").format(label), frappe.ValidationError)
	return value.strip() or None


def resolve_vehicle_identifier(vehicle_identifier: str) -> str:
	"""Resolve a Repair Vehicle document name from exact name, normalized VIN, or license plate."""
	if not isinstance(vehicle_identifier, str):
		frappe.throw(
			frappe._("Vehicle identifier (name, VIN, or license plate) must be a string."),
			frappe.ValidationError,
		)
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	ident = vehicle_identifier.strip()
	if not ident:
		frappe.throw(frappe._("Vehicle identifier (name, VIN, or license plate) is required."))

	matched_name = None
	if frappe.db.exists("Repair Vehicle", ident):
		matched_name = ident
	else:
		norm_vin = normalize_vin_string(ident)
		if norm_vin:
			matched_name = frappe.db.get_value("Repair Vehicle", {"vin": norm_vin}, "name")

	if not matched_name:
		by_plate = frappe.db.sql(
			"""
			SELECT name FROM `tabRepair Vehicle`
			WHERE LOWER(REPLACE(IFNULL(license_plate, ''), ' ', '')) = LOWER(REPLACE(%s, ' ', ''))
			LIMIT 1
			""",
			(ident,),
			as_dict=True,
		)
		if by_plate:
			matched_name = by_plate[0].name

	if matched_name:
		frappe.get_cached_doc("Repair Vehicle", matched_name).check_permission("read")
		return matched_name

	frappe.throw(
		frappe._("Repair Vehicle '{0}' not found by name, VIN, or license plate.").format(ident),
		frappe.DoesNotExistError,
	)


def build_vehicle_search_results(
	query: Optional[str] = None,
	customer: Optional[str] = None,
	model: Optional[str] = None,
	generation: Optional[str] = None,
	limit: int = 20,
) -> list[dict[str, Any]]:
	"""Search vehicles across VIN, license plate, model, generation, primary customer, and co-owners."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	query_str = _ensure_optional_str(query, "Search query")
	customer_str = _ensure_optional_str(customer, "Customer")
	model_str = _ensure_optional_str(model, "Model")
	generation_str = _ensure_optional_str(generation, "Generation")

	safe_limit = max(1, min(cint(limit) or 20, 100))
	conditions: list[str] = []
	params: list[Any] = []

	match_cond = build_match_conditions("Repair Vehicle")
	if match_cond:
		conditions.append(f"({match_cond})")

	if customer_str:
		conditions.append(
			"(`tabRepair Vehicle`.customer = %s OR EXISTS ("
			"SELECT 1 FROM `tabRepair Vehicle Customer` rvc "
			"WHERE rvc.parent = `tabRepair Vehicle`.name "
			"AND rvc.parenttype = 'Repair Vehicle' AND rvc.customer = %s"
			"))"
		)
		params.extend([customer_str, customer_str])

	if model_str:
		conditions.append("LOWER(IFNULL(`tabRepair Vehicle`.model, '')) LIKE LOWER(%s)")
		params.append(f"%{escape_like(model_str)}%")

	if generation_str:
		conditions.append("LOWER(IFNULL(`tabRepair Vehicle`.vehicle_generation, '')) LIKE LOWER(%s)")
		params.append(f"%{escape_like(generation_str)}%")

	if query_str:
		escaped_q = escape_like(query_str)
		like_q = f"%{escaped_q}%"
		norm_q = normalize_vin_string(query_str) or query_str
		like_norm = f"%{escape_like(norm_q)}%"
		conditions.append(
			"("
			"`tabRepair Vehicle`.name LIKE %s OR `tabRepair Vehicle`.vin LIKE %s "
			"OR `tabRepair Vehicle`.vin LIKE %s "
			"OR IFNULL(`tabRepair Vehicle`.license_plate, '') LIKE %s "
			"OR IFNULL(`tabRepair Vehicle`.color, '') LIKE %s "
			"OR IFNULL(`tabRepair Vehicle`.display_title, '') LIKE %s "
			"OR IFNULL(`tabRepair Vehicle`.model, '') LIKE %s "
			"OR IFNULL(`tabRepair Vehicle`.vehicle_generation, '') LIKE %s "
			"OR IFNULL(`tabRepair Vehicle`.customer, '') LIKE %s "
			"OR IFNULL(c.customer_name, '') LIKE %s "
			"OR EXISTS ("
			"SELECT 1 FROM `tabRepair Vehicle Customer` rvc "
			"LEFT JOIN `tabCustomer` c2 ON c2.name = rvc.customer "
			"WHERE rvc.parent = `tabRepair Vehicle`.name AND rvc.parenttype = 'Repair Vehicle' "
			"AND (rvc.customer LIKE %s OR IFNULL(c2.customer_name, '') LIKE %s)"
			")"
			")"
		)
		params.extend([like_q, like_q, like_norm, like_q, like_q, like_q, like_q, like_q, like_q, like_q, like_q, like_q])

	where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
	params.append(safe_limit)

	rows = frappe.db.sql(
		f"""
		SELECT
			`tabRepair Vehicle`.name, `tabRepair Vehicle`.vin, `tabRepair Vehicle`.license_plate,
			`tabRepair Vehicle`.color, `tabRepair Vehicle`.display_title,
			`tabRepair Vehicle`.customer, c.customer_name,
			`tabRepair Vehicle`.is_valid_vin, `tabRepair Vehicle`.manufacturer,
			`tabRepair Vehicle`.model, `tabRepair Vehicle`.model_year,
			`tabRepair Vehicle`.body_type, `tabRepair Vehicle`.trim,
			`tabRepair Vehicle`.drivetrain, `tabRepair Vehicle`.battery_type,
			`tabRepair Vehicle`.drive_unit, `tabRepair Vehicle`.autopilot_hardware,
			`tabRepair Vehicle`.manufactured_month, `tabRepair Vehicle`.vehicle_generation,
			`tabRepair Vehicle`.recommended_manual_url, `tabRepair Vehicle`.last_odometer,
			`tabRepair Vehicle`.last_service_date, `tabRepair Vehicle`.next_service_odometer,
			`tabRepair Vehicle`.next_service_date
		FROM `tabRepair Vehicle`
		LEFT JOIN `tabCustomer` c ON c.name = `tabRepair Vehicle`.customer
		{where_sql}
		ORDER BY `tabRepair Vehicle`.modified DESC
		LIMIT %s
		""",
		tuple(params),
		as_dict=True,
	)

	results = []
	for row in rows:
		item = dict(row)
		vinfo = build_vehicle_display_dict(item)
		item["display_title"] = item.get("display_title") or vinfo["display"]
		item["vehicle_display"] = vinfo["display"]
		item["compact_display"] = vinfo["compact_display"]
		details = get_tesla_manual_details_for_vehicle(item)
		item["vehicle_generation"] = item.get("vehicle_generation") or details.get("vehicle_generation") or ""
		item["recommended_manual_url"] = (
			item.get("recommended_manual_url")
			or details.get("recommended_manual_url")
			or get_tesla_manual_url_for_vehicle(item)
		)
		results.append(item)
	return results


def build_vehicle_intelligence(vehicle_identifier: str, history_limit: int = 20) -> dict[str, Any]:
	"""Return comprehensive vehicle profile, VIN decode, multi-customer owners, RO history, and deferred items."""
	vehicle_name = resolve_vehicle_identifier(vehicle_identifier)
	vdoc = frappe.get_doc("Repair Vehicle", vehicle_name)
	vdoc.check_permission("read")
	safe_limit = max(1, min(cint(history_limit) or 20, 100))

	owners = _collect_vehicle_owners(vdoc)
	service_history = _collect_vehicle_service_history(vdoc.name, safe_limit)
	deferred_summary = _summarize_deferred_services(vdoc)
	generation_info = get_tesla_manual_details_for_vehicle(vdoc)
	manual_url = vdoc.recommended_manual_url or generation_info.get("recommended_manual_url") or get_tesla_manual_url_for_vehicle(vdoc)
	vehicle_gen = vdoc.vehicle_generation or generation_info.get("vehicle_generation") or ""
	vinfo = build_vehicle_display_dict(vdoc)

	return {
		"vehicle": {
			"name": vdoc.name,
			"vin": vdoc.vin,
			"is_valid_vin": cint(vdoc.is_valid_vin),
			"license_plate": vdoc.license_plate or "",
			"color": vdoc.color or "",
			"display_title": vdoc.display_title or vinfo["display"],
			"vehicle_display": vinfo["display"],
			"compact_display": vinfo["compact_display"],
			"customer": vdoc.customer or "",
			"manufacturer": vdoc.manufacturer or "",
			"model": vdoc.model or "",
			"model_year": vdoc.model_year or "",
			"body_type": vdoc.body_type or "",
			"trim": vdoc.trim or "",
			"drivetrain": vdoc.drivetrain or "",
			"battery_type": vdoc.battery_type or "",
			"drive_unit": vdoc.drive_unit or "",
			"assembly_plant": vdoc.assembly_plant or "",
			"production_sequence": vdoc.production_sequence or "",
			"autopilot_hardware": vdoc.autopilot_hardware or "",
			"manufactured_month": vdoc.manufactured_month or "",
			"vehicle_generation": vehicle_gen,
			"recommended_manual_url": manual_url,
			"last_odometer": cint(vdoc.last_odometer),
			"last_service_date": str(vdoc.last_service_date) if vdoc.last_service_date else None,
			"next_service_odometer": cint(vdoc.next_service_odometer),
			"next_service_date": str(vdoc.next_service_date) if vdoc.next_service_date else None,
		},
		"vehicle_display": vinfo["display"],
		"compact_display": vinfo["compact_display"],
		"vin_decode": decode_tesla_vin(vdoc.vin, manufactured_month=vdoc.manufactured_month),
		"recommended_manual_url": manual_url,
		"generation_info": generation_info,
		"owners": owners,
		"service_history": service_history,
		"deferred_services": deferred_summary,
	}


def _collect_vehicle_owners(vdoc) -> list[dict[str, Any]]:
	"""Collect primary and co-owner Customer records linked to a Repair Vehicle."""
	customer_ids = []
	for row in (vdoc.customers or []):
		if row.customer and row.customer not in customer_ids:
			customer_ids.append(row.customer)
	if vdoc.customer and vdoc.customer not in customer_ids:
		customer_ids.insert(0, vdoc.customer)

	customer_meta = {}
	if customer_ids:
		for c in frappe.get_all(
			"Customer",
			filters={"name": ["in", customer_ids]},
			fields=["name", "customer_name", "mobile_no", "email_id"],
		):
			customer_meta[c.name] = c

	owners = []
	seen = set()
	for row in (vdoc.customers or []):
		if not row.customer or row.customer in seen:
			continue
		seen.add(row.customer)
		meta = customer_meta.get(row.customer) or {}
		owners.append({
			"customer": row.customer,
			"customer_name": row.customer_name or meta.get("customer_name") or row.customer,
			"is_primary": cint(row.is_primary or (row.customer == vdoc.customer)),
			"relationship": row.relationship or "",
			"mobile_no": meta.get("mobile_no") or "",
			"email_id": meta.get("email_id") or "",
		})

	if vdoc.customer and vdoc.customer not in seen:
		meta = customer_meta.get(vdoc.customer) or {}
		owners.insert(0, {
			"customer": vdoc.customer,
			"customer_name": meta.get("customer_name") or vdoc.customer,
			"is_primary": 1,
			"relationship": "Primary Owner",
			"mobile_no": meta.get("mobile_no") or "",
			"email_id": meta.get("email_id") or "",
		})
	return owners


def _collect_vehicle_service_history(vehicle_name: str, limit: int) -> list[dict[str, Any]]:
	"""Fetch recent Repair Orders and summarized services for a vehicle without N+1 queries."""
	if not frappe.has_permission("Repair Order", "read"):
		return []

	orders = frappe.get_list(
		"Repair Order",
		filters={"vehicle": vehicle_name, "docstatus": ["<", 2]},
		fields=[
			"name", "customer", "status", "approval_status", "posting_date",
			"odometer_in", "odometer_out", "grand_total",
			"is_mobile_capable", "modified",
		],
		order_by="posting_date desc, modified desc",
		limit_page_length=limit,
	)
	if not orders:
		return []

	order_names = [o.name for o in orders]
	services_by_ro: dict[str, list[dict[str, Any]]] = {}
	labor_totals_by_ro: dict[str, float] = {}
	parts_totals_by_ro: dict[str, float] = {}
	for svc in frappe.get_all(
		"Repair Order Service",
		filters={"parent": ["in", order_names], "parenttype": "Repair Order"},
		fields=[
			"parent", "service_title", "correction_code", "service_position",
			"status", "labor_amount", "parts_total", "total_amount",
		],
		order_by="idx asc",
	):
		labor_amt = flt(svc.labor_amount, 2)
		parts_amt = flt(svc.parts_total, 2)
		labor_totals_by_ro[svc.parent] = flt(labor_totals_by_ro.get(svc.parent, 0.0) + labor_amt, 2)
		parts_totals_by_ro[svc.parent] = flt(parts_totals_by_ro.get(svc.parent, 0.0) + parts_amt, 2)
		services_by_ro.setdefault(svc.parent, []).append({
			"service_title": svc.service_title,
			"correction_code": svc.correction_code or "",
			"service_position": svc.service_position or "None",
			"status": svc.status,
			"labor_amount": labor_amt,
			"parts_total": parts_amt,
			"total_amount": flt(svc.total_amount, 2),
		})

	history = []
	for ro in orders:
		history.append({
			"name": ro.name,
			"customer": ro.customer,
			"status": ro.status,
			"approval_status": ro.approval_status or "",
			"posting_date": str(ro.posting_date) if ro.posting_date else None,
			"odometer_in": cint(ro.odometer_in),
			"odometer_out": cint(ro.odometer_out),
			"labor_total": flt(labor_totals_by_ro.get(ro.name, 0.0), 2),
			"parts_total": flt(parts_totals_by_ro.get(ro.name, 0.0), 2),
			"grand_total": flt(ro.grand_total, 2),
			"is_mobile_capable": cint(ro.is_mobile_capable),
			"services": services_by_ro.get(ro.name, []),
		})
	return history


def _summarize_deferred_services(vdoc) -> dict[str, Any]:
	"""Aggregate deferred service items and highlight open safety recommendations."""
	all_items = []
	open_items = []
	safety_hazard_count = 0
	open_estimated_total = 0.0

	for row in (vdoc.deferred_services or []):
		entry = {
			"name": row.name,
			"job_title": row.job_title,
			"decline_date": str(row.decline_date) if row.decline_date else None,
			"repair_order": row.repair_order or "",
			"estimated_amount": flt(row.estimated_amount, 2),
			"status": row.status or "Open Recommendation",
			"safety_hazard": cint(row.safety_hazard),
			"decline_reason": row.decline_reason or "",
		}
		all_items.append(entry)
		if entry["status"] == "Open Recommendation":
			open_items.append(entry)
			open_estimated_total += entry["estimated_amount"]
			if entry["safety_hazard"]:
				safety_hazard_count += 1

	return {
		"open_count": len(open_items),
		"total_count": len(all_items),
		"safety_hazard_count": safety_hazard_count,
		"open_estimated_total": flt(open_estimated_total, 2),
		"open": open_items,
		"all": all_items,
	}


def on_customer_trash(doc, method=None):
	"""Clean up any Repair Vehicle Customer child links when a Customer is deleted."""
	frappe.db.delete("Repair Vehicle Customer", {"customer": doc.name})
