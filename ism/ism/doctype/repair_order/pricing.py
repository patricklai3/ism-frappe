# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.api.parts import _resolve_default_warehouse as get_default_warehouse


def _get_default_labor_rate() -> float:
	"""Return default shop labor rate from ISM Settings or 140.0 CAD fallback."""
	try:
		if frappe.get_meta("ISM Settings").has_field("default_labor_rate"):
			return flt(frappe.db.get_single_value("ISM Settings", "default_labor_rate")) or 140.0
	except Exception:
		pass
	return 140.0


def get_or_create_default_tax_template(company=None):
	"""Return the default 13% HST Sales Taxes and Charges Template for the company."""
	from ism.setup import get_or_create_default_sales_tax_template

	return get_or_create_default_sales_tax_template(company)


def _expand_package_parts(
	pkg: Any,
	target_side: str,
	warehouse: Optional[str],
	fallback_to_standard_rate: bool = True,
) -> tuple[List[Dict[str, Any]], float]:
	"""Expand Service Package items matching target_side into normalized RO part dicts."""
	parts_list: List[Dict[str, Any]] = []
	parts_total = 0.0
	for row in getattr(pkg, "items", None) or []:
		row_side = getattr(row, "side", None) or "Both"
		if target_side not in ("Both", "None") and row_side not in ("Both", target_side):
			continue
		item_doc = frappe.get_cached_doc("Item", row.item_code)
		qty = flt(row.qty) or 1.0
		row_rate = getattr(row, "rate", None)
		if row_rate is None and fallback_to_standard_rate:
			rate = flt(getattr(item_doc, "standard_rate", 0))
		else:
			rate = flt(row_rate)
		amt = flt(qty * rate, 2)
		parts_total += amt
		parts_list.append({
			"item_code": row.item_code,
			"item_name": row.item_name or item_doc.item_name,
			"qty": qty,
			"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
			"rate": rate,
			"amount": amt,
			"warehouse": getattr(row, "warehouse", None) or warehouse,
		})
	return parts_list, flt(parts_total, 2)


def build_service_package_row_fields(
	pkg: Any,
	company: Optional[str] = None,
	cur_side: Optional[str] = None,
	cur_title: Optional[str] = None,
	cur_code: Optional[str] = None,
	default_labor_rate: Optional[float] = None,
	advisor_notes: str = "",
	is_supplement: int = 0,
	warehouse: Optional[str] = None,
	fallback_to_standard_rate: bool = True,
) -> Dict[str, Any]:
	"""Expand a Service Package document into a canonical Repair Order Service row dict."""
	from ism.utils.tesla_manual import detect_service_side

	pkg_title = getattr(pkg, "package_name", None) or pkg.name
	pkg_code = getattr(pkg, "correction_code", None)
	rh_code = getattr(pkg, "rh_correction_code", None)
	is_symm = cint(getattr(pkg, "is_symmetrical", 0))

	if cur_side is None:
		base_side = detect_service_side(pkg_title, pkg_code, rh_code)
		cur_side = "LH" if is_symm and base_side != "RH" else ("RH" if is_symm and base_side == "RH" else "None")

	cur_title = cur_title or pkg_title
	cur_code = pkg_code if cur_code is None else cur_code

	labor_hours = flt(getattr(pkg, "standard_labor_hours", 0))
	is_flat = cint(getattr(pkg, "is_flat_rate", 0))
	if is_flat:
		labor_rate = flt(getattr(pkg, "hourly_rate", 0))
		labor_amount = flt(getattr(pkg, "service_rate", 0), 2)
	else:
		fallback_rate = flt(default_labor_rate) if default_labor_rate else _get_default_labor_rate()
		labor_rate = flt(getattr(pkg, "hourly_rate", 0)) or fallback_rate
		labor_amount = flt(labor_hours * labor_rate, 2)

	resolved_wh = warehouse if warehouse is not None else get_default_warehouse(company)
	parts_list, parts_total = _expand_package_parts(
		pkg,
		target_side=cur_side,
		warehouse=resolved_wh,
		fallback_to_standard_rate=fallback_to_standard_rate,
	)

	return {
		"service_package": pkg.name,
		"correction_code": cur_code,
		"service_title": cur_title,
		"service_position": cur_side,
		"status": "Estimating",
		"is_flat_rate": is_flat,
		"labor_hours": labor_hours,
		"labor_rate": labor_rate,
		"labor_amount": labor_amount,
		"parts": json.dumps(parts_list),
		"parts_total": parts_total,
		"total_amount": flt(labor_amount + parts_total, 2),
		"is_supplement": cint(is_supplement),
		"is_mobile_capable": cint(getattr(pkg, "is_mobile_capable", 0)),
		"tesla_manual_url": getattr(pkg, "tesla_manual_url", None),
		"advisor_notes": advisor_notes or "",
	}


def _parse_parts_list(raw_parts: Any) -> List[Dict[str, Any]]:
	"""Parse JSON-encoded or list-based service parts cleanly."""
	if isinstance(raw_parts, str):
		try:
			parsed = json.loads(raw_parts) if raw_parts else []
		except Exception:
			return []
	elif isinstance(raw_parts, list):
		parsed = raw_parts
	else:
		return []
	return [p for p in parsed if isinstance(p, dict)]


def calculate_service_totals(ro) -> None:
	"""Calculate labor amounts, parts totals, and service totals with batched Warehouse validation."""
	total_billed_hours = 0.0
	default_wh = get_default_warehouse(ro.company)
	default_rate = None

	parsed_by_row: List[tuple[Any, List[Dict[str, Any]]]] = []
	candidate_warehouses: set[str] = set()
	for s in ro.services:
		parts = _parse_parts_list(s.parts)
		parsed_by_row.append((s, parts))
		for p in parts:
			wh = p.get("warehouse")
			if wh and isinstance(wh, str):
				candidate_warehouses.add(wh)

	valid_warehouses: set[str] = set()
	if candidate_warehouses:
		wh_filters: Dict[str, Any] = {"is_group": 0, "name": ["in", list(candidate_warehouses)]}
		if ro.company:
			wh_filters["company"] = ro.company
		valid_warehouses = set(frappe.get_all("Warehouse", filters=wh_filters, pluck="name"))

	for s, parts in parsed_by_row:
		if not s.service_title:
			s.service_title = s.service_package or _("Custom Service")

		is_flat = cint(getattr(s, "is_flat_rate", 0))
		if not is_flat and s.service_package:
			pkg_flat = frappe.get_cached_value("Service Package", s.service_package, "is_flat_rate")
			if pkg_flat:
				is_flat = 1
				s.is_flat_rate = 1

		labor_hours = flt(s.labor_hours)
		if is_flat:
			if s.labor_amount is None or (flt(s.labor_amount) == 0 and s.service_package):
				pkg_service_rate = frappe.get_cached_value("Service Package", s.service_package, "service_rate")
				if pkg_service_rate is not None and pkg_service_rate != "":
					s.labor_amount = flt(pkg_service_rate, 2)
			s.labor_amount = flt(s.labor_amount, 2)
		else:
			if labor_hours and (s.labor_rate is None or s.labor_rate == ""):
				if default_rate is None:
					default_rate = _get_default_labor_rate()
				s.labor_rate = default_rate
			s.labor_amount = flt(labor_hours * flt(s.labor_rate), 2)

		parts_total = 0.0
		cleaned_parts = []
		for p in parts:
			qty = flt(p.get("qty", 1.0)) or 1.0
			rate = flt(p.get("rate", 0.0))
			amt = flt(qty * rate, 2)
			parts_total += amt
			part_wh = p.get("warehouse")
			valid_wh = part_wh if (part_wh and part_wh in valid_warehouses) else default_wh
			code = p.get("item_code", "")
			cleaned_parts.append({
				"item_code": code,
				"item_name": p.get("item_name", code),
				"component_slot": p.get("component_slot") or p.get("item_name", code),
				"part_source": p.get("part_source", ""),
				"part_condition": p.get("part_condition", ""),
				"oem_part_number": p.get("oem_part_number", ""),
				"warranty_period": p.get("warranty_period", ""),
				"side": p.get("side", ""),
				"qty": qty,
				"uom": p.get("uom", "Nos"),
				"rate": rate,
				"amount": amt,
				"warehouse": valid_wh,
			})

		s.parts = json.dumps(cleaned_parts)
		s.parts_total = flt(parts_total, 2)
		s.total_amount = flt(s.labor_amount + s.parts_total, 2)

		if s.status == "Approved":
			total_billed_hours += labor_hours

	ro.total_billed_hours = total_billed_hours


def calculate_taxes_and_totals(ro) -> None:
	"""Calculate Repair Order net, tax, and grand totals without overwriting explicit tax_rate=0."""
	has_approved = any(s.status == "Approved" for s in ro.services)
	if has_approved:
		net = sum(flt(s.total_amount) for s in ro.services if s.status == "Approved")
	else:
		net = sum(flt(s.total_amount) for s in ro.services if s.status != "Declined")

	ro.net_total = flt(net, 2)

	explicit_tax = ro.tax_rate is not None and str(ro.tax_rate).strip() != ""
	explicit_zero_tax = explicit_tax and flt(ro.tax_rate) == 0.0
	if explicit_zero_tax:
		ro.taxes_and_charges = None
	elif not ro.taxes_and_charges:
		ro.taxes_and_charges = get_or_create_default_tax_template(ro.company)

	if not explicit_tax:
		if ro.taxes_and_charges:
			template_rate = frappe.db.get_value(
				"Sales Taxes and Charges",
				{"parent": ro.taxes_and_charges},
				"rate",
			)
			tax_rate = flt(template_rate) if template_rate is not None else 13.0
		else:
			tax_rate = 13.0
		ro.tax_rate = tax_rate
	else:
		tax_rate = flt(ro.tax_rate)

	ro.total_taxes_and_charges = flt(net * (tax_rate / 100.0), 2)
	ro.grand_total = flt(ro.net_total + ro.total_taxes_and_charges, 2)
	ro.outstanding_amount = flt(ro.grand_total - flt(ro.paid_amount), 2)


def _has_prior_authorization(ro) -> bool:
	"""Return True if the Repair Order already has customer authorization in place."""
	return bool(
		ro.authorized_at
		or any(s.status == "Approved" for s in ro.services)
		or ro.approval_status in ["Approved", "Partially Approved"]
		or ro.status in ["Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid"]
	)


def _update_supplement_approval_status(ro) -> None:
	"""Set approval_status cleanly after adding a supplemental service."""
	if any(s.status == "Approved" for s in ro.services):
		ro.approval_status = "Partially Approved"
	else:
		ro.approval_status = "Pending Approval"


def add_service_package_to_ro(ro, service_package=None, **kwargs) -> Dict[str, Any]:
	"""Expand canned service package into new Service row(s) with nested Parts."""
	from ism.utils.tesla_manual import detect_service_side, swap_side_in_title

	ro.check_permission("write")
	if isinstance(service_package, dict):
		kwargs.update(service_package)
		service_package = service_package.get("service_package")
	service_package = service_package or kwargs.get("service_package")
	if not service_package or not isinstance(service_package, str):
		frappe.throw(_("Valid Service Package identifier is required."), frappe.ValidationError)

	frappe.has_permission("Service Package", "read", doc=service_package, throw=True)
	pkg = frappe.get_doc("Service Package", service_package)
	is_supp = 1 if (cint(kwargs.get("is_supplement", 0)) or _has_prior_authorization(ro)) else 0

	side_param = str(kwargs.get("side") or "").strip().lower()
	is_symm = cint(getattr(pkg, "is_symmetrical", 0))
	pkg_title = pkg.package_name
	pkg_code = getattr(pkg, "correction_code", None)
	raw_rh_code = getattr(pkg, "rh_correction_code", None)

	base_side = detect_service_side(pkg_title, pkg_code, raw_rh_code)
	if base_side == "RH":
		rh_title = pkg_title
		lh_title = swap_side_in_title(pkg_title, "LH")
		rh_code = pkg_code
		lh_code = getattr(pkg, "lh_correction_code", None) or pkg_code
	else:
		lh_title = pkg_title
		rh_title = swap_side_in_title(pkg_title, "RH")
		lh_code = pkg_code
		rh_code = raw_rh_code or pkg_code

	sides_to_add = []
	if side_param == "both" and is_symm:
		sides_to_add.append({"side": "LH", "title": lh_title, "code": lh_code})
		sides_to_add.append({"side": "RH", "title": rh_title, "code": rh_code})
	elif side_param == "rh" and is_symm:
		sides_to_add.append({"side": "RH", "title": rh_title, "code": rh_code})
	elif side_param == "lh" and is_symm:
		sides_to_add.append({"side": "LH", "title": lh_title, "code": lh_code})
	else:
		pos = "LH" if is_symm and base_side != "RH" else ("RH" if is_symm and base_side == "RH" else "None")
		sides_to_add.append({"side": pos, "title": pkg_title, "code": pkg_code})

	default_wh = get_default_warehouse(ro.company)
	default_rate = _get_default_labor_rate()
	added_titles = []
	for entry in sides_to_add:
		cur_side = entry["side"]
		cur_title = entry["title"]
		cur_code = entry["code"]
		if any(
			(s.service_title and s.service_title.strip().lower() == cur_title.strip().lower())
			or (
				pkg.name
				and s.service_package == pkg.name
				and getattr(s, "service_position", "") == cur_side
				and cur_side not in ("None", "Both", "")
			)
			for s in ro.services
		):
			continue

		row_fields = build_service_package_row_fields(
			pkg=pkg,
			company=ro.company,
			cur_side=cur_side,
			cur_title=cur_title,
			cur_code=cur_code,
			default_labor_rate=default_rate,
			advisor_notes=kwargs.get("advisor_notes", "") or "",
			is_supplement=is_supp,
			warehouse=default_wh,
		)
		ro.append("services", row_fields)
		added_titles.append(cur_title)

	if is_supp:
		_update_supplement_approval_status(ro)

	ro.save()
	return {"status": "success", "service": ", ".join(added_titles)}


def duplicate_service_row_opposite_side(ro, service_row_id=None, **kwargs) -> Dict[str, Any]:
	"""Duplicate a service row for the opposite side (e.g. LH -> RH or RH -> LH)."""
	from ism.utils.tesla_manual import detect_service_side, swap_side_in_title

	ro.check_permission("write")
	if not service_row_id:
		frappe.throw(_("Service Row ID is required."))

	target = next(
		(s for s in ro.services if s.name == service_row_id or str(getattr(s, "idx", "")) == str(service_row_id)),
		None,
	)
	if not target:
		frappe.throw(_("Service row '{0}' not found on this Repair Order.").format(service_row_id))

	raw_pos = getattr(target, "service_position", None)
	curr_pos = raw_pos if raw_pos in ("LH", "RH") else detect_service_side(target.service_title)
	if curr_pos not in ("LH", "RH"):
		frappe.throw(_("Service '{0}' is not a symmetrical LH/RH operation.").format(target.service_title))

	opposite_pos = "RH" if curr_pos == "LH" else "LH"
	new_title = swap_side_in_title(target.service_title, opposite_pos)

	for s in ro.services:
		if (s.service_title and s.service_title.strip().lower() == new_title.strip().lower()) or (
			target.service_package
			and s.service_package == target.service_package
			and getattr(s, "service_position", "") == opposite_pos
		):
			frappe.throw(_("Opposite side service '{0}' is already present on this Repair Order.").format(new_title))

	pkg = None
	if target.service_package and frappe.db.exists("Service Package", target.service_package):
		pkg = frappe.get_doc("Service Package", target.service_package)

	new_code = target.correction_code
	if opposite_pos == "RH" and pkg and getattr(pkg, "rh_correction_code", None):
		new_code = pkg.rh_correction_code
	elif opposite_pos == "LH" and pkg and getattr(pkg, "correction_code", None):
		new_code = pkg.correction_code

	default_wh = get_default_warehouse(ro.company)
	if pkg and pkg.items:
		parts_list, parts_total = _expand_package_parts(
			pkg,
			target_side=opposite_pos,
			warehouse=default_wh,
			fallback_to_standard_rate=False,
		)
	else:
		parts_list = []
		parts_total = 0.0
		for p in _parse_parts_list(target.parts):
			p_copy = dict(p)
			p_copy["item_name"] = swap_side_in_title(p_copy.get("item_name", ""), opposite_pos)
			parts_list.append(p_copy)
			parts_total += flt(p_copy.get("amount", 0))
		parts_total = flt(parts_total, 2)

	is_supp = 1 if (_has_prior_authorization(ro) or getattr(target, "is_supplement", 0)) else 0
	new_service = ro.append("services", {
		"service_package": target.service_package,
		"correction_code": new_code,
		"service_title": new_title,
		"service_position": opposite_pos,
		"status": "Estimating",
		"is_flat_rate": target.is_flat_rate,
		"labor_hours": target.labor_hours,
		"labor_rate": target.labor_rate,
		"labor_amount": target.labor_amount,
		"parts": json.dumps(parts_list),
		"parts_total": parts_total,
		"total_amount": flt(flt(target.labor_amount) + parts_total, 2),
		"is_supplement": is_supp,
		"is_mobile_capable": target.is_mobile_capable,
		"tesla_manual_url": target.tesla_manual_url,
		"advisor_notes": "",
	})

	if is_supp:
		_update_supplement_approval_status(ro)

	ro.save()
	return {
		"status": "success",
		"service_row": new_service.as_dict() if hasattr(new_service, "as_dict") else None,
		"service_title": new_title,
		"service_position": opposite_pos,
	}


def add_supplement_to_ro(
	ro,
	service_title=None,
	category="Maintenance",
	labor_hours=1.0,
	labor_rate=None,
	parts=None,
	notes=None,
	job_title=None,
	**kwargs,
) -> Dict[str, Any]:
	"""Add a supplemental repair service to an existing Repair Order."""
	ro.check_permission("write")
	if isinstance(service_title, dict):
		category = service_title.get("category", category)
		labor_hours = service_title.get("labor_hours", labor_hours)
		labor_rate = service_title.get("labor_rate", labor_rate)
		parts = service_title.get("parts", parts)
		notes = service_title.get("notes", notes)
		job_title = service_title.get("job_title", job_title)
		service_title = service_title.get("service_title")
	title = service_title or job_title or kwargs.get("service_title") or kwargs.get("job_title") or "Supplemental Service"
	hours = flt(labor_hours)
	rate = flt(labor_rate) if (labor_rate is not None and str(labor_rate).strip() != "") else _get_default_labor_rate()
	labor_amount = flt(hours * rate, 2)

	parts_list: List[Dict[str, Any]] = []
	parts_total = 0.0
	if parts:
		if isinstance(parts, str):
			parts = json.loads(parts)
		default_wh = get_default_warehouse(ro.company)
		item_codes = [p.get("item_code") for p in parts if isinstance(p, dict) and p.get("item_code")]
		wh_names = [p.get("warehouse") for p in parts if isinstance(p, dict) and p.get("warehouse")]
		valid_items = (
			set(frappe.get_all("Item", filters={"name": ["in", item_codes]}, pluck="name"))
			if item_codes
			else set()
		)
		valid_whs = (
			set(frappe.get_all("Warehouse", filters={"name": ["in", wh_names]}, pluck="name"))
			if wh_names
			else set()
		)
		for p in parts:
			if not isinstance(p, dict):
				continue
			item_code = p.get("item_code")
			if item_code and item_code in valid_items:
				item_doc = frappe.get_cached_doc("Item", item_code)
				qty = flt(p.get("qty", 1.0)) or 1.0
				raw_rate = p.get("rate")
				part_rate = flt(raw_rate) if raw_rate is not None else flt(item_doc.standard_rate or 0.0)
				amt = flt(qty * part_rate, 2)
				parts_total += amt
				part_wh = p.get("warehouse")
				parts_list.append({
					"item_code": item_code,
					"item_name": item_doc.item_name,
					"qty": qty,
					"uom": getattr(item_doc, "stock_uom", "Nos"),
					"rate": part_rate,
					"amount": amt,
					"warehouse": part_wh if (part_wh and part_wh in valid_whs) else default_wh,
				})

	ro.append("services", {
		"service_title": title,
		"status": "Estimating",
		"labor_hours": hours,
		"labor_rate": rate,
		"labor_amount": labor_amount,
		"parts": json.dumps(parts_list),
		"parts_total": parts_total,
		"total_amount": flt(labor_amount + parts_total, 2),
		"is_supplement": 1,
		"advisor_notes": notes or "",
	})

	_update_supplement_approval_status(ro)
	ro.save()
	return {"status": "success", "service_title": title}


def add_deferred_services_to_ro(ro, deferred_items=None, **kwargs) -> Dict[str, Any]:
	"""Add one or more previously deferred services to this Repair Order."""
	ro.check_permission("write")
	if isinstance(deferred_items, str):
		try:
			deferred_items = json.loads(deferred_items)
		except Exception:
			deferred_items = [deferred_items]
	elif not deferred_items and kwargs.get("services"):
		deferred_items = kwargs.get("services")
		if isinstance(deferred_items, str):
			try:
				deferred_items = json.loads(deferred_items)
			except Exception:
				deferred_items = [deferred_items]

	if not deferred_items:
		frappe.throw(_("No deferred services specified to add."))

	is_supp = 1 if _has_prior_authorization(ro) else 0
	default_wh = get_default_warehouse(ro.company)
	default_rate = _get_default_labor_rate()
	existing_titles = {s.service_title.strip().lower() for s in ro.services if s.service_title}

	parsed_requests: List[tuple[str, float]] = []
	candidate_titles: List[str] = []
	for item in deferred_items:
		if isinstance(item, str):
			title = item.strip()
			est_amount = 0.0
		elif isinstance(item, dict):
			title = (item.get("job_title") or item.get("service_title") or "").strip()
			raw_est = item.get("estimated_amount")
			est_amount = flt(raw_est if raw_est is not None else item.get("estimated_cost", 0.0))
		else:
			continue
		if not title or title.lower() in existing_titles:
			continue
		existing_titles.add(title.lower())
		parsed_requests.append((title, est_amount))
		candidate_titles.append(title)

	pkg_by_title: Dict[str, str] = {}
	if candidate_titles:
		for pkg_row in frappe.get_all(
			"Service Package",
			filters={"package_name": ["in", candidate_titles]},
			fields=["name", "package_name"],
		):
			pkg_by_title[pkg_row.package_name] = pkg_row.name
		missing_titles = [t for t in candidate_titles if t not in pkg_by_title]
		if missing_titles:
			for pkg_name in frappe.get_all(
				"Service Package",
				filters={"name": ["in", missing_titles]},
				pluck="name",
			):
				pkg_by_title[pkg_name] = pkg_name

	added_services: List[str] = []
	for title, est_amount in parsed_requests:
		pkg_name = pkg_by_title.get(title)
		if pkg_name:
			pkg = frappe.get_doc("Service Package", pkg_name)
			row_fields = build_service_package_row_fields(
				pkg=pkg,
				company=ro.company,
				cur_side="None",
				cur_title=pkg.package_name,
				cur_code=getattr(pkg, "correction_code", None),
				default_labor_rate=default_rate,
				is_supplement=is_supp,
				warehouse=default_wh,
				fallback_to_standard_rate=False,
			)
			ro.append("services", row_fields)
		else:
			ro.append("services", {
				"service_title": title,
				"status": "Estimating",
				"is_flat_rate": 1,
				"labor_hours": 0.0,
				"labor_rate": 0.0,
				"labor_amount": est_amount,
				"parts": json.dumps([]),
				"parts_total": 0.0,
				"total_amount": est_amount,
				"is_supplement": is_supp,
				"advisor_notes": "",
			})
		added_services.append(title)

	if not added_services:
		return {
			"status": "noop",
			"message": _("Selected services are already present on this Repair Order."),
			"added_count": 0,
		}

	if is_supp:
		_update_supplement_approval_status(ro)

	ro.save()
	return {"status": "success", "added_count": len(added_services), "added_services": added_services}


def save_service_row_as_package(ro, service_row_id=None, package_name=None, **kwargs) -> Dict[str, Any]:
	"""Save a one-off repair service and its nested parts as a reusable Service Package."""
	ro.check_permission("write")
	frappe.has_permission("Service Package", "create", throw=True)
	if isinstance(service_row_id, dict):
		package_name = service_row_id.get("package_name", package_name)
		service_row_id = service_row_id.get("service_row_id")
	service_row_id = service_row_id or kwargs.get("service_row_id")
	package_name = package_name or kwargs.get("package_name")

	target = next(
		(
			s
			for s in ro.services
			if str(s.name) == str(service_row_id)
			or str(s.idx) == str(service_row_id)
			or str(s.service_title) == str(service_row_id)
		),
		None,
	)
	if not target:
		frappe.throw(_("Service row {0} not found.").format(service_row_id))

	pkg_title = package_name or target.service_title
	if frappe.db.exists("Service Package", pkg_title):
		frappe.throw(_("A Service Package named '{0}' already exists.").format(pkg_title))

	parts = _parse_parts_list(target.parts)
	veh_model = frappe.db.get_value("Repair Vehicle", ro.vehicle, "model") if ro.vehicle else ""
	app_model = getattr(target, "applicable_model", None) or veh_model or ""

	pkg = frappe.get_doc({
		"doctype": "Service Package",
		"package_name": pkg_title,
		"applicable_model": app_model,
		"correction_code": getattr(target, "correction_code", None),
		"is_flat_rate": cint(getattr(target, "is_flat_rate", 0)),
		"is_mobile_capable": cint(getattr(target, "is_mobile_capable", 0)),
		"standard_labor_hours": flt(target.labor_hours),
		"hourly_rate": flt(target.labor_rate),
		"service_rate": flt(target.labor_amount),
		"tesla_manual_url": getattr(target, "tesla_manual_url", None),
		"items": [
			{
				"item_code": p.get("item_code"),
				"item_name": p.get("item_name"),
				"qty": flt(p.get("qty", 1.0)),
				"rate": flt(p.get("rate", 0.0)),
			}
			for p in parts
			if p.get("item_code")
		],
	})
	pkg.insert()

	target.service_package = pkg.name
	if target.name and not str(target.name).startswith("new-"):
		frappe.db.set_value("Repair Order Service", target.name, "service_package", pkg.name, update_modified=False)

	return {
		"status": "success",
		"package_name": pkg.name,
		"message": _("Service Package '{0}' created successfully!").format(pkg.name),
	}


def fetch_service_package_details(service_package: str, side: Optional[str] = None) -> Dict[str, Any]:
	"""Return package info and its parts list with batched stock lookup."""
	from ism.api.parts import format_slot_label, get_items_available_stock_batch, parse_slot_number
	from ism.utils.tesla_manual import (
		detect_service_side,
		get_category_for_correction_code,
		parse_tesla_manual_url,
		swap_side_in_title,
	)

	frappe.has_permission("Service Package", "read", throw=True)
	if not service_package:
		return {}
	if not isinstance(service_package, str):
		frappe.throw(_("service_package must be a string."), frappe.ValidationError)
	if side is not None and not isinstance(side, str):
		frappe.throw(_("side must be a string."), frappe.ValidationError)

	pkg = frappe.get_doc("Service Package", service_package)
	pkg.check_permission("read")
	default_wh = get_default_warehouse()
	default_rate = _get_default_labor_rate()

	is_symm = cint(getattr(pkg, "is_symmetrical", 0))
	rh_code = getattr(pkg, "rh_correction_code", None)
	target_side = str(side or "").strip().upper()

	title = pkg.package_name
	code = getattr(pkg, "correction_code", None)
	pos = "None"
	if is_symm:
		pos = "RH" if target_side == "RH" else "LH"
		base_side = detect_service_side(pkg.package_name, code, rh_code)
		if pos == "RH":
			title = swap_side_in_title(pkg.package_name, "RH") if base_side == "LH" else pkg.package_name
			if rh_code:
				code = rh_code
		elif pos == "LH":
			title = swap_side_in_title(pkg.package_name, "LH") if base_side == "RH" else pkg.package_name
			if getattr(pkg, "correction_code", None):
				code = pkg.correction_code

	matching_rows = []
	item_codes = []
	for row in pkg.items:
		row_side = getattr(row, "side", None) or "Both"
		if not is_symm or target_side not in ("LH", "RH") or row_side in ("Both", target_side):
			matching_rows.append(row)
			if row.item_code:
				item_codes.append(row.item_code)

	stock_map = get_items_available_stock_batch(item_codes, default_wh) if item_codes else {}

	slots_seen: set[int] = set()
	parts = []
	unassigned_parts = []
	for row in matching_rows:
		row_side = getattr(row, "side", None) or "Both"
		s_num = cint(getattr(row, "slot_number", None))
		if s_num == 0 and getattr(row, "component_slot", None):
			s_num = parse_slot_number(row.component_slot)

		slot = format_slot_label(s_num)
		is_def = cint(getattr(row, "is_default", 1))
		item_doc = frappe.get_cached_doc("Item", row.item_code)
		qty = flt(row.qty) or 1.0
		rate = flt(row.rate) if row.rate is not None else flt(item_doc.standard_rate)
		stock_qty = flt(stock_map.get(row.item_code, {}).get("actual_qty", 0.0))

		part_info = {
			"slot_number": s_num,
			"component_slot": slot,
			"item_code": row.item_code,
			"item_name": row.item_name or item_doc.item_name,
			"part_source": getattr(item_doc, "part_source", "OEM") or "OEM",
			"part_condition": getattr(item_doc, "part_condition", "New") or "New",
			"oem_part_number": getattr(item_doc, "oem_part_number", "") or "",
			"revision": getattr(item_doc, "revision", "") or "",
			"warranty_period": (
				getattr(item_doc, "warranty_period", "12 Months / 20,000 km") or "12 Months / 20,000 km"
			),
			"qty": qty,
			"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
			"rate": rate,
			"amount": flt(qty * rate, 2),
			"warehouse": default_wh,
			"side": row_side,
			"stock_qty": stock_qty,
		}

		if s_num == 0:
			unassigned_parts.append(part_info)
			continue
		if s_num in slots_seen and not is_def:
			continue
		if s_num in slots_seen and is_def:
			parts = [p for p in parts if p.get("slot_number") != s_num]

		parts.append(part_info)
		slots_seen.add(s_num)

	labor_hours = flt(pkg.standard_labor_hours)
	is_flat = cint(pkg.is_flat_rate)
	if is_flat:
		hourly_rate = flt(pkg.hourly_rate)
		service_rate = flt(pkg.service_rate, 2)
	else:
		hourly_rate = flt(pkg.hourly_rate) or default_rate
		service_rate = flt(pkg.service_rate) if flt(pkg.service_rate) else flt(labor_hours * hourly_rate, 2)

	pkg_cat = getattr(pkg, "category", None)
	if not pkg_cat and getattr(pkg, "correction_code", None):
		try:
			cat, _subcat = get_category_for_correction_code(pkg.correction_code)
			if cat:
				pkg_cat = cat
		except Exception:
			pass

	app_model = getattr(pkg, "applicable_model", None)
	app_gen = getattr(pkg, "applicable_generation", None)
	if not app_model and getattr(pkg, "tesla_manual_url", None):
		try:
			u_meta = parse_tesla_manual_url(pkg.tesla_manual_url)
			app_model = u_meta.get("applicable_model") or ""
			if not app_gen:
				app_gen = u_meta.get("matched_generation") or ""
		except Exception:
			pass

	return {
		"package_name": title,
		"applicable_model": app_model or "",
		"applicable_generation": app_gen or "",
		"correction_code": code,
		"service_position": pos,
		"is_symmetrical": is_symm,
		"rh_correction_code": rh_code,
		"frt": getattr(pkg, "frt", None) or labor_hours,
		"tesla_manual_url": getattr(pkg, "tesla_manual_url", None),
		"category": pkg_cat or "Maintenance",
		"description": "",
		"is_flat_rate": is_flat,
		"is_mobile_capable": cint(getattr(pkg, "is_mobile_capable", 0)),
		"standard_labor_hours": labor_hours,
		"labor_hours": labor_hours,
		"hourly_rate": hourly_rate,
		"service_rate": service_rate,
		"parts": parts,
		"unassigned_parts": unassigned_parts,
		"parts_total": sum(p["amount"] for p in parts),
	}
