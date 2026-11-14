# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import urllib.error
import urllib.request
from typing import Any, Optional, Union
from urllib.parse import urlparse

import frappe
from frappe.utils import cint, flt

from ism.api.parts import get_default_warehouse, quick_create_part
from ism.ism.doctype.repair_order.repair_order import _get_default_labor_rate
from ism.ism.doctype.repair_vehicle.repair_vehicle import (
	build_vehicle_intelligence,
	build_vehicle_search_results,
	decode_tesla_vin,
	resolve_vehicle_identifier,
)
from ism.utils.tesla_manual import (
	detect_service_side,
	normalize_tesla_procedure,
	parse_tesla_procedure_html,
	swap_side_in_title,
	validate_vehicle_compatibility,
)

_ALLOWED_TESLA_HOSTS = ("service.tesla.com", "service.teslamotors.com")
_VALID_CONFLICT_POLICIES = ("skip", "overwrite", "add", "error")
_LEGACY_CONFLICT_ALIASES = {"update": "overwrite", "overwrite": "overwrite", "add": "add", "skip": "skip", "error": "error"}


@frappe.whitelist()
def fetch_and_parse_tesla_procedure(
	url: str,
	repair_order: Optional[str] = None,
	vehicle: Optional[str] = None,
) -> dict[str, Any]:
	"""Fetch a Tesla service manual procedure server-side and return its normalized metadata."""
	if not (frappe.has_permission("Service Package", "read") or frappe.has_permission("Repair Order", "read")):
		frappe.throw(frappe._("Not permitted to read Tesla service procedures."), frappe.PermissionError)

	url = _require_non_empty_str(url, "Tesla procedure URL")
	repair_order = _optional_str(repair_order, "Repair Order")
	vehicle = _optional_str(vehicle, "Repair Vehicle")

	_validate_tesla_url(url)
	html_content = _fetch_tesla_html(url)

	parsed = parse_tesla_procedure_html(html_content, url=url)
	if not parsed.get("title") and not parsed.get("correction_code"):
		frappe.throw(frappe._("Could not find a valid Tesla procedure on this page. Please verify the URL."))

	target_vehicle = vehicle
	if not target_vehicle and repair_order and frappe.db.exists("Repair Order", repair_order):
		ro_doc = frappe.get_doc("Repair Order", repair_order)
		ro_doc.check_permission("read")
		target_vehicle = ro_doc.vehicle

	if target_vehicle:
		v_doc = frappe.get_doc("Repair Vehicle", target_vehicle)
		v_doc.check_permission("read")
		parsed["compatibility"] = validate_vehicle_compatibility(v_doc, url)

	return parsed


@frappe.whitelist()
def import_tesla_procedure(
	repair_order: str,
	url: Optional[str] = None,
	data: Optional[Union[dict[str, Any], str]] = None,
	on_conflict: str = "skip",
	force: Any = False,
	allow_generation_mismatch: Any = False,
	duplicate_action: Optional[str] = None,
	is_supplement: Any = 0,
	side: Optional[str] = None,
	advisor_notes: Optional[str] = None,
	**kwargs: Any,
) -> dict[str, Any]:
	"""Import a Tesla service procedure onto a Repair Order using declarative conflict/compatibility policies."""
	frappe.has_permission("Repair Order", "write", throw=True)

	repair_order = _require_non_empty_str(repair_order, "Repair Order")
	url = _optional_str(url, "Tesla procedure URL")
	if not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(frappe._("Repair Order '{0}' does not exist.").format(repair_order))

	ro = frappe.get_doc("Repair Order", repair_order)
	ro.check_permission("write")

	conflict_policy = _resolve_conflict_policy(on_conflict, duplicate_action)
	is_forced = _coerce_bool(force) or _coerce_bool(allow_generation_mismatch)

	raw_data = _parse_raw_procedure_data(data)
	if not raw_data and url:
		raw_data = fetch_and_parse_tesla_procedure(url)

	if not raw_data or (not raw_data.get("title") and not raw_data.get("procedure_title") and not raw_data.get("procedure_name") and not raw_data.get("correction_code")):
		frappe.throw(frappe._("Incomplete procedure data. Title or Correction Code required."))

	raw_proc_url = raw_data.get("tesla_manual_url") or raw_data.get("url") or url or ""
	proc_url = _optional_str(raw_proc_url, "Tesla procedure URL") or ""
	proc_data = normalize_tesla_procedure(raw_data, url=proc_url)

	warnings: list[str] = []
	v_doc = None
	if ro.vehicle:
		v_doc = frappe.get_doc("Repair Vehicle", ro.vehicle)
		v_doc.check_permission("read")
	compat = validate_vehicle_compatibility(v_doc, proc_url)
	if not compat.get("is_compatible"):
		msg = compat.get("warning") or frappe._("Vehicle model or generation does not match Tesla procedure.")
		if not is_forced:
			frappe.throw(msg, frappe.ValidationError)
		warnings.append(msg)

	pkg = _upsert_service_package(proc_data, proc_url)
	side_choice = side if side is not None else (kwargs.get("side") or proc_data.get("side") or "")
	operations = _resolve_import_operations(proc_data, pkg, side_choice)

	notes = advisor_notes if advisor_notes is not None else (kwargs.get("advisor_notes") or "")
	supp_flag = is_supplement or proc_data.get("is_supplement") or kwargs.get("is_supplement")
	has_prior_auth = _ro_has_prior_authorization(ro)
	is_supp = 1 if (_coerce_bool(supp_flag) or has_prior_auth) else 0

	hourly_rate = flt(pkg.hourly_rate) or _get_default_labor_rate()
	labor_hours = flt(proc_data.get("frt"), 2) or flt(pkg.standard_labor_hours) or 1.0
	labor_amount = flt(labor_hours * hourly_rate, 2)

	outcome = _apply_operations_to_ro(
		ro=ro,
		pkg=pkg,
		proc_data=proc_data,
		proc_url=proc_url,
		operations=operations,
		conflict_policy=conflict_policy,
		is_supp=is_supp,
		hourly_rate=hourly_rate,
		labor_hours=labor_hours,
		labor_amount=labor_amount,
		advisor_notes=notes,
	)

	if outcome["mutated"]:
		if is_supp and any(s.status == "Approved" for s in ro.services):
			ro.approval_status = "Partially Approved"
		if ro.services:
			ro.is_mobile_capable = 1 if all(cint(s.is_mobile_capable) for s in ro.services) else 0
		ro.flags.ignore_validate_update_after_submit = True
		ro.save()

	affected_rows = outcome["affected_rows"]
	last_row = affected_rows[-1] if affected_rows else outcome["first_skipped_row"]
	action_taken = outcome["action"]
	title = proc_data["title"]
	correction_code = proc_data["correction_code"]

	if outcome["mutated"]:
		frappe.publish_realtime(
			"repair_order_updated",
			{
				"docname": ro.name,
				"service_title": title,
				"correction_code": correction_code,
				"frt": proc_data["frt"],
				"action": action_taken,
				"is_mobile_capable": last_row.is_mobile_capable if last_row else 0,
			},
			doctype="Repair Order",
			docname=ro.name,
		)

	return {
		"status": "skipped" if action_taken == "skipped" else "success",
		"action": action_taken,
		"conflict_policy": conflict_policy,
		"message": (
			f"Procedure '{title}' already exists on {ro.name}; skipped."
			if action_taken == "skipped"
			else f"Successfully {action_taken} '{title}' on {ro.name}"
		),
		"repair_order": ro.name,
		"service_package": pkg.name,
		"correction_code": correction_code,
		"frt": proc_data["frt"],
		"labor_amount": labor_amount,
		"is_mobile_capable": last_row.is_mobile_capable if last_row else 0,
		"service_row": last_row.as_dict() if hasattr(last_row, "as_dict") else None,
		"count_added": outcome["count_added"],
		"count_updated": outcome["count_updated"],
		"count_skipped": outcome["count_skipped"],
		"warnings": warnings,
	}


@frappe.whitelist()
def decode_vehicle_vin(vin: str) -> dict[str, Any]:
	"""Decode a 17-character Tesla VIN strictly offline using bundled WMI and spec tables."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	if not isinstance(vin, str):
		frappe.throw(frappe._("VIN must be a string."), frappe.ValidationError)
	return decode_tesla_vin(vin)


@frappe.whitelist()
def search_vehicles(
	query: Optional[str] = None,
	customer: Optional[str] = None,
	model: Optional[str] = None,
	limit: int = 20,
) -> list[dict[str, Any]]:
	"""Search vehicles across VIN, license plate, model, primary owner, and linked co-owners."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	query = _optional_str(query, "Search query")
	customer = _optional_str(customer, "Customer")
	model = _optional_str(model, "Model")
	return build_vehicle_search_results(query=query, customer=customer, model=model, limit=limit)


@frappe.whitelist()
def get_vehicle_intelligence(vehicle: str, history_limit: int = 20) -> dict[str, Any]:
	"""Return decoded vehicle specs, multi-customer owners, RO history, and deferred service summary."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	vehicle = _require_non_empty_str(vehicle, "Vehicle")
	return build_vehicle_intelligence(vehicle_identifier=vehicle, history_limit=history_limit)


@frappe.whitelist()
def link_vehicle_customer(
	vehicle: str,
	customer: str,
	is_primary: Any = False,
	relationship: Optional[str] = None,
) -> dict[str, Any]:
	"""Link or update a secondary or primary customer owner on a Repair Vehicle."""
	frappe.has_permission("Repair Vehicle", "write", throw=True)
	vehicle = _require_non_empty_str(vehicle, "Vehicle")
	customer = _require_non_empty_str(customer, "Customer")
	relationship = _optional_str(relationship, "Relationship")

	vehicle_name = resolve_vehicle_identifier(vehicle)
	vdoc = frappe.get_doc("Repair Vehicle", vehicle_name)
	vdoc.check_permission("write")
	vdoc.add_customer(
		customer=customer,
		is_primary=_coerce_bool(is_primary),
		relationship=relationship,
	)
	vdoc.reload()
	return {
		"vehicle": vdoc.name,
		"primary_customer": vdoc.customer,
		"owners": [
			{
				"customer": row.customer,
				"customer_name": row.customer_name,
				"is_primary": cint(row.is_primary),
				"relationship": row.relationship or "",
			}
			for row in (vdoc.customers or [])
		],
	}


def _require_non_empty_str(value: Any, label: str) -> str:
	if not isinstance(value, str) or not value.strip():
		frappe.throw(frappe._("{0} must be a non-empty string.").format(label), frappe.ValidationError)
	return value.strip()


def _optional_str(value: Any, label: str) -> Optional[str]:
	if value is None or value == "":
		return None
	if not isinstance(value, str):
		frappe.throw(frappe._("{0} must be a string.").format(label), frappe.ValidationError)
	return value.strip() or None


def _coerce_bool(val: Any) -> bool:
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, float)):
		return bool(val)
	if isinstance(val, str):
		return val.strip().lower() in ("1", "true", "yes", "on")
	return bool(val)


def _resolve_conflict_policy(on_conflict: str, duplicate_action: Optional[str]) -> str:
	if duplicate_action is not None and str(duplicate_action).strip():
		alias = str(duplicate_action).strip().lower()
		if alias in _LEGACY_CONFLICT_ALIASES:
			return _LEGACY_CONFLICT_ALIASES[alias]
		frappe.throw(frappe._("Invalid duplicate_action '{0}'.").format(duplicate_action), frappe.ValidationError)

	policy = str(on_conflict or "skip").strip().lower()
	if policy not in _VALID_CONFLICT_POLICIES:
		frappe.throw(
			frappe._("Invalid on_conflict policy '{0}'. Must be one of: {1}.").format(
				on_conflict, ", ".join(_VALID_CONFLICT_POLICIES)
			),
			frappe.ValidationError,
		)
	return policy


def _validate_tesla_url(url: str) -> None:
	if not url:
		frappe.throw(frappe._("Tesla procedure URL is required."))
	parsed_url = urlparse(str(url).strip())
	if parsed_url.scheme != "https" or parsed_url.netloc not in _ALLOWED_TESLA_HOSTS:
		frappe.throw(frappe._("Invalid Tesla manual domain. URL must be an https:// URL from service.tesla.com."))


def _fetch_tesla_html(url: str) -> str:
	headers = {
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
		"Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
		"Accept-Language": "en-US,en;q=0.9",
		"Referer": "https://service.tesla.com/",
	}
	req = urllib.request.Request(url, headers=headers)
	try:
		with urllib.request.urlopen(req, timeout=15) as response:
			return response.read().decode("utf-8", errors="ignore")
	except Exception:
		try:
			fallback_req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
			with urllib.request.urlopen(fallback_req, timeout=15) as response:
				return response.read().decode("utf-8", errors="ignore")
		except urllib.error.URLError as exc:
			frappe.throw(f"Unable to fetch Tesla service manual page: {exc.reason}")
		except Exception as exc:
			frappe.throw(f"Failed to fetch page: {str(exc)}")


def _parse_raw_procedure_data(data: Optional[Union[dict[str, Any], str]]) -> dict[str, Any]:
	if not data:
		return {}
	if isinstance(data, dict):
		return data
	if isinstance(data, str):
		try:
			decoded = json.loads(data)
			return decoded if isinstance(decoded, dict) else {}
		except Exception:
			return {}
	return {}


def _ro_has_prior_authorization(ro) -> bool:
	return bool(
		ro.authorized_at
		or any(s.status == "Approved" for s in (ro.services or []))
		or ro.approval_status in ("Approved", "Partially Approved")
		or ro.status in ("Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid")
	)


def _resolve_import_operations(proc_data: dict[str, Any], pkg, side_choice: str) -> list[dict[str, str]]:
	title = proc_data["title"]
	correction_code = proc_data["correction_code"]
	side_param = str(side_choice or "").strip().lower()
	is_symmetrical = cint(proc_data.get("is_symmetrical") or (1 if pkg and pkg.is_symmetrical else 0))

	rh_code = proc_data.get("rh_correction_code") or (getattr(pkg, "rh_correction_code", None) if pkg else None) or correction_code
	lh_code = getattr(pkg, "correction_code", None) or correction_code
	base_side = detect_service_side(title, correction_code, rh_code)

	if base_side == "RH":
		rh_title = title
		lh_title = swap_side_in_title(title, "LH")
		rh_code = correction_code
		if pkg and getattr(pkg, "correction_code", None) and pkg.correction_code != rh_code:
			lh_code = pkg.correction_code
	else:
		lh_title = title
		rh_title = swap_side_in_title(title, "RH")
		lh_code = correction_code

	if side_param == "both" and is_symmetrical:
		return [
			{"side": "LH", "title": lh_title, "code": lh_code},
			{"side": "RH", "title": rh_title, "code": rh_code},
		]
	if side_param == "rh" and is_symmetrical:
		return [{"side": "RH", "title": rh_title, "code": rh_code}]
	if side_param == "lh" and is_symmetrical:
		return [{"side": "LH", "title": lh_title, "code": lh_code}]

	pos = "LH" if is_symmetrical and base_side != "RH" else ("RH" if is_symmetrical and base_side == "RH" else "None")
	return [{"side": pos, "title": title, "code": correction_code}]


def _find_matching_ro_service(ro, op_code: str, op_side: str, pkg_name: Optional[str]):
	for row in (ro.services or []):
		s_code = str(getattr(row, "correction_code", "") or "").strip()
		s_pos = str(getattr(row, "service_position", "") or "None").strip()
		s_pkg = str(getattr(row, "service_package", "") or "").strip()

		pos_matches = (s_pos == op_side) or (s_pos in ("None", "") and op_side in ("None", ""))
		code_or_pkg_matches = (op_code and s_code == op_code) or (pkg_name and s_pkg == pkg_name)
		if pos_matches and code_or_pkg_matches:
			return row
	return None


def _apply_operations_to_ro(
	ro,
	pkg,
	proc_data: dict[str, Any],
	proc_url: str,
	operations: list[dict[str, str]],
	conflict_policy: str,
	is_supp: int,
	hourly_rate: float,
	labor_hours: float,
	labor_amount: float,
	advisor_notes: str,
) -> dict[str, Any]:
	affected_rows = []
	first_skipped_row = None
	count_added = 0
	count_updated = 0
	count_skipped = 0

	for op in operations:
		op_side = op["side"]
		op_title = op["title"]
		op_code = op["code"]
		existing_service = _find_matching_ro_service(ro, op_code, op_side, pkg.name if pkg else None)

		if existing_service and conflict_policy == "error":
			code_display = op_code or pkg.name or "this procedure"
			side_str = f" ({op_side})" if op_side and op_side != "None" else ""
			frappe.throw(
				frappe._("Correction code {0}{1} already exists on Repair Order {2}.").format(
					code_display, side_str, ro.name
				),
				frappe.DuplicateEntryError,
			)

		if existing_service and conflict_policy == "skip":
			count_skipped += 1
			if first_skipped_row is None:
				first_skipped_row = existing_service
			continue

		parts_list, parts_total = _extract_parts_for_side(pkg, op_side)
		model_val = getattr(pkg, "applicable_model", None) or proc_data.get("applicable_model", "")
		mobile_val = cint(proc_data.get("is_mobile_capable", getattr(pkg, "is_mobile_capable", 0)))

		if existing_service and conflict_policy == "overwrite":
			count_updated += 1
			existing_service.service_title = op_title
			existing_service.service_position = op_side
			existing_service.correction_code = op_code
			existing_service.labor_hours = labor_hours
			existing_service.labor_rate = hourly_rate
			existing_service.labor_amount = labor_amount
			if parts_list:
				existing_service.parts = json.dumps(parts_list)
				existing_service.parts_total = parts_total
			existing_service.total_amount = flt(labor_amount + flt(existing_service.parts_total), 2)
			existing_service.tesla_manual_url = proc_url
			existing_service.applicable_model = model_val
			existing_service.is_mobile_capable = mobile_val
			if advisor_notes:
				existing_service.advisor_notes = advisor_notes
			affected_rows.append(existing_service)
		else:
			count_added += 1
			target_service = ro.append("services", {
				"service_package": pkg.name,
				"applicable_model": model_val,
				"correction_code": op_code,
				"service_title": op_title,
				"service_position": op_side,
				"status": "Estimating",
				"is_flat_rate": 0,
				"labor_hours": labor_hours,
				"labor_rate": hourly_rate,
				"labor_amount": labor_amount,
				"parts": json.dumps(parts_list),
				"parts_total": parts_total,
				"total_amount": flt(labor_amount + parts_total, 2),
				"is_supplement": is_supp,
				"is_mobile_capable": mobile_val,
				"tesla_manual_url": proc_url,
				"advisor_notes": advisor_notes or "",
			})
			affected_rows.append(target_service)

	if count_added > 0:
		action = "added"
	elif count_updated > 0:
		action = "updated"
	else:
		action = "skipped"

	return {
		"action": action,
		"mutated": (count_added + count_updated) > 0,
		"affected_rows": affected_rows,
		"first_skipped_row": first_skipped_row,
		"count_added": count_added,
		"count_updated": count_updated,
		"count_skipped": count_skipped,
	}


def _extract_parts_for_side(pkg, side: str = "Both"):
	"""Extract nested parts list from Service Package matching specified side."""
	parts_list = []
	parts_total = 0.0
	if not pkg or not getattr(pkg, "items", None):
		return parts_list, parts_total

	for row in pkg.items:
		row_side = getattr(row, "side", None) or "Both"
		if side in ("Both", "None") or row_side in ("Both", side):
			item_doc = frappe.get_cached_doc("Item", row.item_code)
			qty = flt(row.qty) or 1.0
			rate = flt(row.rate) if flt(row.rate) > 0 else flt(getattr(item_doc, "standard_rate", 0))
			amt = flt(qty * rate, 2)
			parts_total += amt
			parts_list.append({
				"item_code": row.item_code,
				"item_name": row.item_name or item_doc.item_name,
				"qty": qty,
				"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
				"rate": rate,
				"amount": amt,
				"warehouse": get_default_warehouse(),
			})

	return parts_list, parts_total


def _upsert_service_package(proc_data, proc_url=""):
	"""Upsert a Service Package and its catalog parts using the unified procedure schema."""
	normalized = normalize_tesla_procedure(proc_data, url=proc_url or "")
	correction_code = normalized["correction_code"]
	title = normalized["title"]
	frt = normalized["frt"]
	model = normalized["applicable_model"]
	generation = normalized["applicable_generation"]
	parts = normalized["parts"]

	existing_pkg_name = None
	if correction_code:
		existing_pkg_name = frappe.db.get_value("Service Package", {"correction_code": correction_code}, "name")
	if not existing_pkg_name and title:
		existing_pkg_name = frappe.db.get_value("Service Package", {"package_name": title}, "name")

	if existing_pkg_name:
		frappe.has_permission("Service Package", "write", throw=True)
		pkg = frappe.get_doc("Service Package", existing_pkg_name)
		pkg.check_permission("write")
		if correction_code and pkg.package_name and pkg.package_name.startswith(correction_code):
			pkg.package_name = title
		if frt > 0:
			pkg.standard_labor_hours = frt
		if normalized["tesla_manual_url"]:
			pkg.tesla_manual_url = normalized["tesla_manual_url"]
		if model:
			pkg.applicable_model = model
		if generation:
			pkg.applicable_generation = generation
		if normalized["doc_guid"]:
			pkg.doc_guid = normalized["doc_guid"]
		pkg.is_mobile_capable = cint(normalized["is_mobile_capable"])
		pkg.is_symmetrical = cint(normalized["is_symmetrical"])
		if normalized["rh_correction_code"]:
			pkg.rh_correction_code = normalized["rh_correction_code"]
		if normalized["category"] and pkg.meta.has_field("category"):
			pkg.category = normalized["category"]
		if normalized["subcategory"] and pkg.meta.has_field("subcategory"):
			pkg.subcategory = normalized["subcategory"]
		_sync_service_package_parts(pkg, parts)
		pkg.save()
		return pkg

	frappe.has_permission("Service Package", "create", throw=True)
	doc_fields = {
		"doctype": "Service Package",
		"package_name": title,
		"applicable_model": model,
		"applicable_generation": generation,
		"correction_code": correction_code,
		"standard_labor_hours": frt or 1.0,
		"is_flat_rate": 0,
		"is_mobile_capable": cint(normalized["is_mobile_capable"]),
		"is_symmetrical": cint(normalized["is_symmetrical"]),
		"rh_correction_code": normalized["rh_correction_code"],
		"tesla_manual_url": normalized["tesla_manual_url"],
		"doc_guid": normalized["doc_guid"],
	}
	pkg = frappe.get_doc(doc_fields)
	if normalized["category"] and pkg.meta.has_field("category"):
		pkg.category = normalized["category"]
	if normalized["subcategory"] and pkg.meta.has_field("subcategory"):
		pkg.subcategory = normalized["subcategory"]
	_sync_service_package_parts(pkg, parts)
	pkg.insert()
	return pkg


def _sync_service_package_parts(pkg, parts: list[dict[str, Any]]) -> None:
	"""Persist canonical normalized parts onto Service Package.items without duplicate rows."""
	if not parts:
		return

	existing_by_key = {}
	for row in (pkg.items or []):
		existing_by_key[(row.item_code, row.side or "Both")] = row

	next_slot = max((cint(getattr(r, "slot_number", 0)) for r in (pkg.items or [])), default=0) + 1
	for part in parts:
		part_number = part["part_number"]
		oem_pn = part.get("oem_part_number") or part_number
		item_code = _resolve_or_create_part_item(part_number, oem_pn, part)
		side = part.get("side") or "Both"
		qty = flt(part.get("qty") or 1.0)
		rate = flt(part.get("rate") or 0.0, 2)
		if rate <= 0:
			rate = flt(frappe.db.get_value("Item", item_code, "standard_rate"), 2)

		existing_row = existing_by_key.get((item_code, side))
		if existing_row:
			existing_row.qty = qty
			if rate > 0:
				existing_row.rate = rate
		else:
			row = pkg.append("items", {
				"slot_number": next_slot,
				"item_code": item_code,
				"qty": qty,
				"rate": rate,
				"side": side,
				"is_default": 1,
			})
			existing_by_key[(item_code, side)] = row
			next_slot += 1


def _resolve_or_create_part_item(part_number: str, oem_pn: str, part: dict[str, Any]) -> str:
	"""Find an existing ERPNext Item by item_code or OEM part number, or create via quick_create_part."""
	if frappe.db.exists("Item", part_number):
		return part_number

	if oem_pn and frappe.db.has_column("Item", "oem_part_number"):
		existing = frappe.db.get_value("Item", {"oem_part_number": oem_pn}, "name")
		if existing:
			return existing

	created = quick_create_part(
		part_number=part_number,
		part_name=part.get("part_name") or part_number,
		part_source=part.get("part_source") or "OEM",
		part_condition=part.get("part_condition") or "New",
		oem_part_number=oem_pn,
		unit_rate=flt(part.get("rate") or 0.0, 2),
		side=part.get("side") or "Both",
	)
	if isinstance(created, dict):
		return created.get("name") or created.get("item_code") or part_number
	return getattr(created, "name", None) or part_number
