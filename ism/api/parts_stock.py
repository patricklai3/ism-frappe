# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.api._validation import optional_str, parse_json_arg, scalar_str


def _require_any_read_permission(doctypes, primary_doctype=None):
	if any(frappe.has_permission(dt, "read") for dt in doctypes):
		return
	frappe.has_permission(primary_doctype or doctypes[0], "read", throw=True)


def _find_company_warehouse(company):
	"""Return default, Stores-matching, or any non-group Warehouse for a company."""
	if not company:
		return None
	if frappe.db.has_column("Company", "default_warehouse"):
		co_wh = frappe.db.get_value("Company", company, "default_warehouse")
		if co_wh and frappe.db.exists("Warehouse", {"name": co_wh, "company": company, "is_group": 0}):
			return co_wh
	stores_wh = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 0, "warehouse_name": ["like", "%Store%"]}, "name"
	)
	if stores_wh:
		return stores_wh
	return frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")


def _get_stock_settings_default_warehouse():
	if frappe.db.exists("DocType", "Stock Settings") and frappe.get_meta("Stock Settings").has_field("default_warehouse"):
		return frappe.db.get_single_value("Stock Settings", "default_warehouse")
	return None


def _resolve_default_warehouse(company=None):
	"""Internal warehouse lookup after caller has enforced boundary permissions."""
	if company:
		stock_wh = _get_stock_settings_default_warehouse()
		if stock_wh and frappe.db.exists("Warehouse", {"name": stock_wh, "company": company, "is_group": 0}):
			return stock_wh
		wh = _find_company_warehouse(company)
		if wh:
			return wh

	stock_wh = _get_stock_settings_default_warehouse()
	if stock_wh and frappe.db.exists("Warehouse", {"name": stock_wh, "is_group": 0}):
		return stock_wh

	default_company = frappe.db.get_single_value("Global Defaults", "default_company")
	wh = _find_company_warehouse(default_company)
	if wh:
		return wh

	return frappe.db.get_value("Warehouse", {"is_group": 0}, "name") or "Stores"


@frappe.whitelist()
def get_default_warehouse(company=None):
	"""Dynamically resolve a valid, existing non-group warehouse.
	If company is specified, the warehouse MUST belong to that company.
	"""
	company = optional_str(company, "Company")
	_require_any_read_permission(("Warehouse", "Item", "Repair Order"), primary_doctype="Warehouse")
	return _resolve_default_warehouse(company)


def _get_items_stock_qty_map(item_codes, warehouse=None):
	"""Batch fetch summed actual_qty from Bin for all requested item_codes."""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	stock_map = {code: 0.0 for code in codes}
	if not codes:
		return stock_map
	try:
		filters = {"item_code": ["in", codes]}
		if warehouse:
			filters["warehouse"] = warehouse
		bins = frappe.get_all("Bin", filters=filters, fields=["item_code", "actual_qty"])
		for row in bins:
			code = row.get("item_code")
			if code in stock_map:
				stock_map[code] += flt(row.get("actual_qty"))
	except Exception:
		frappe.log_error(message=frappe.get_traceback(), title="Parts Stock Query Error")
		raise
	return stock_map


def _get_item_stock_qty(item_code, warehouse=None):
	"""Get current actual stock quantity for a single item (retained for repair_order.py compatibility)."""
	if not item_code:
		return 0.0
	return flt(_get_items_stock_qty_map([item_code], warehouse).get(item_code, 0.0))


def _extract_ro_metadata(ro):
	return {
		"creation": ro.creation,
		"approval_status": ro.approval_status,
		"status": ro.status,
		"docstatus": ro.docstatus,
		"company": ro.company,
	}


def _parse_parts_json(raw_parts):
	if not raw_parts:
		return []
	if isinstance(raw_parts, str):
		stripped = raw_parts.strip()
		if not stripped:
			return []
		try:
			parsed = json.loads(stripped)
			return parsed if isinstance(parsed, list) else []
		except json.JSONDecodeError:
			return []
	return raw_parts if isinstance(raw_parts, list) else []


def _get_prior_approved_ros(ro_name=None, ro_data=None):
	"""Return FIFO-ordered active approved Repair Orders that take priority over ro_name."""
	ro_creation = None
	is_ro_approved = False
	if ro_name:
		if ro_data is None and frappe.db.exists("Repair Order", ro_name):
			ro_data = frappe.db.get_value(
				"Repair Order",
				ro_name,
				["creation", "approval_status", "status", "docstatus", "company"],
				as_dict=True,
			)
		if ro_data and cint(ro_data.get("docstatus")) < 2:
			ro_creation = ro_data.get("creation")
			is_ro_approved = (
				ro_data.get("approval_status") in ("Approved", "Partially Approved")
				and ro_data.get("status") not in ("Closed / Released", "Cancelled")
			)

	filters: dict[str, Any] = {
		"docstatus": ["<", 2],
		"status": ["not in", ["Closed / Released", "Cancelled"]],
		"approval_status": ["in", ["Approved", "Partially Approved"]],
	}
	if ro_name:
		filters["name"] = ["!=", ro_name]
	if is_ro_approved and ro_creation:
		filters["creation"] = ["<=", ro_creation]

	candidate_ros = frappe.get_all(
		"Repair Order",
		filters=filters,
		fields=["name", "creation", "company"],
		order_by="creation asc, name asc",
	)

	prior_ros = {}
	for cro in candidate_ros:
		if is_ro_approved and ro_creation:
			if (cro.creation < ro_creation) or (cro.creation == ro_creation and cro.name < ro_name):
				prior_ros[cro.name] = cro
		else:
			prior_ros[cro.name] = cro
	return prior_ros


def get_repair_order_committed_qty_batch(item_codes, warehouse=None, ro_name=None, ro_data=None):
	"""Batch compute quantities of item_codes committed to active approved Repair Orders
	that have FIFO priority over ro_name.

	Priority:
	- If ro_name is an active approved RO, priority is FIFO by (creation, name).
	- If ro_name is unapproved/draft/estimating/new, ALL active approved ROs have priority.
	"""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	results = {code: (0.0, []) for code in codes}
	if not codes:
		return results

	prior_ros = _get_prior_approved_ros(ro_name=ro_name, ro_data=ro_data)
	if not prior_ros:
		return results

	services = frappe.get_all(
		"Repair Order Service",
		filters={"parent": ["in", list(prior_ros.keys())], "status": "Approved"},
		fields=["parent", "parts"],
	)

	target_set = set(codes)
	company_wh_cache = {}
	item_ro_qty: dict[str, dict[str, float]] = {code: {} for code in codes}

	for svc in services:
		parent_ro = svc.parent
		for part in _parse_parts_json(svc.parts):
			if not isinstance(part, dict):
				continue
			code = part.get("item_code")
			if code not in target_set:
				continue
			if warehouse:
				part_wh = part.get("warehouse")
				if not part_wh and parent_ro in prior_ros:
					company = prior_ros[parent_ro].company
					if company not in company_wh_cache:
						company_wh_cache[company] = _resolve_default_warehouse(company)
					part_wh = company_wh_cache[company]
				if part_wh and part_wh != warehouse:
					continue
			p_qty = flt(part.get("qty", 1.0))
			ro_map = item_ro_qty[code]
			ro_map[parent_ro] = ro_map.get(parent_ro, 0.0) + p_qty

	for code in codes:
		ro_map = item_ro_qty[code]
		if not ro_map:
			continue
		# Emit details in strict FIFO order matching prior_ros (creation asc, name asc).
		details = [{"repair_order": ro_id, "qty": ro_map[ro_id]} for ro_id in prior_ros if ro_id in ro_map]
		results[code] = (sum(d["qty"] for d in details), details)

	return results


def get_repair_order_committed_qty(item_code, warehouse=None, ro_name=None):
	"""Calculate total quantity of item_code committed to earlier active approved Repair Orders."""
	if not item_code:
		return 0.0, []
	return get_repair_order_committed_qty_batch([item_code], warehouse, ro_name)[item_code]


def get_items_available_stock_batch(item_codes, warehouse=None, ro_name=None, ro_data=None):
	"""Batch fetch live available stock for multiple items after deducting earlier FIFO commitments."""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	if not codes:
		return {}

	actual_map = _get_items_stock_qty_map(codes, warehouse)
	committed_map = get_repair_order_committed_qty_batch(codes, warehouse, ro_name, ro_data=ro_data)

	results = {}
	for code in codes:
		actual_qty = flt(actual_map.get(code, 0.0))
		committed_qty, details = committed_map.get(code, (0.0, []))
		available_qty = max(0.0, actual_qty - flt(committed_qty))
		results[code] = {
			"item_code": code,
			"warehouse": warehouse,
			"actual_qty": actual_qty,
			"committed_qty": flt(committed_qty),
			"available_qty": flt(available_qty),
			"committed_orders": details,
		}
	return results


def get_item_available_stock(item_code, warehouse=None, ro_name=None):
	"""Get live available stock for a Repair Order after deducting earlier FIFO commitments."""
	if not item_code:
		return {
			"item_code": item_code,
			"warehouse": warehouse,
			"actual_qty": 0.0,
			"committed_qty": 0.0,
			"available_qty": 0.0,
			"committed_orders": [],
		}
	return get_items_available_stock_batch([item_code], warehouse, ro_name)[item_code]


def _parse_stock_items_arg(items: Any) -> list[str]:
	target_items: list[str] = []
	if items is None:
		return target_items
	if isinstance(items, str):
		stripped = items.strip()
		if not stripped:
			return target_items
		if stripped.startswith(("[", "{")):
			items = parse_json_arg(stripped, [], label="Items")
		else:
			items = [s.strip() for s in stripped.split(",") if s.strip()]
	if isinstance(items, (list, tuple, set)):
		for entry in items:
			if isinstance(entry, dict):
				val = scalar_str(entry.get("item_code"), "Item Code")
			else:
				val = scalar_str(entry, "Item Code")
			if val and val not in target_items:
				target_items.append(val)
		return target_items
	frappe.throw(_("Invalid items parameter."), frappe.ValidationError)


def _get_ro_parts_stock_internal(
	ro_name: Optional[str] = None,
	items: Any = None,
	company: Optional[str] = None,
	ro_doc: Any = None,
):
	frappe.has_permission("Item", "read", throw=True)
	frappe.has_permission("Repair Order", "read", throw=True)
	ro_name = optional_str(ro_name, "Repair Order")
	company = optional_str(company, "Company")
	target_items = _parse_stock_items_arg(items)

	ro = ro_doc
	if ro is None and ro_name:
		if not frappe.db.exists("Repair Order", ro_name):
			frappe.throw(_("Repair Order {0} not found.").format(ro_name), frappe.DoesNotExistError)
		ro = frappe.get_doc("Repair Order", ro_name)
		ro.check_permission("read")
	elif ro is not None:
		ro.check_permission("read")
		ro_name = ro.name

	if company and not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist.").format(company), frappe.DoesNotExistError)

	effective_company = company or (ro.company if ro else None)
	target_wh = _resolve_default_warehouse(effective_company)

	if not target_items and ro:
		seen = set()
		for svc in (ro.services or []):
			for p in _parse_parts_json(svc.parts):
				if isinstance(p, dict):
					code = p.get("item_code")
					if code and code not in seen:
						seen.add(code)
						target_items.append(code)

	ro_data = _extract_ro_metadata(ro) if ro else None
	return get_items_available_stock_batch(target_items, target_wh, ro_name, ro_data=ro_data)


@frappe.whitelist()
def get_repair_order_parts_stock(ro_name=None, items=None, company=None):
	"""Whitelisted API to fetch live FIFO stock and commitment data for repair order parts."""
	return _get_ro_parts_stock_internal(ro_name=ro_name, items=items, company=company)
