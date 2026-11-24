# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
import re
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.api._validation import (
	coerce_bool,
	escape_like,
	optional_flt,
	optional_str,
	parse_json_arg,
	require_str,
	scalar_str,
)

_BIN_DIM_CACHE_KEY = "ism:bin_dimension_meta:v2"
_SAFE_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_BIN_TYPES = ("Standard", "Overflow", "Receiving", "Bulk", "Hazmat")


def _require_any_read_permission(doctypes, primary_doctype=None):
	if any(frappe.has_permission(dt, "read") for dt in doctypes):
		return
	frappe.has_permission(primary_doctype or doctypes[0], "read", throw=True)


def _find_company_warehouse(company):
	"""Return default, Shop Store/Stores-matching, or active non-group Warehouse for a company."""
	if not company:
		return None
	if frappe.db.has_column("Company", "default_warehouse"):
		co_wh = frappe.db.get_value("Company", company, "default_warehouse")
		if co_wh and frappe.db.exists(
			"Warehouse", {"name": co_wh, "company": company, "is_group": 0, "disabled": 0}
		):
			return co_wh
	for pattern in ("%Shop Store%", "%Store%"):
		stores_wh = frappe.db.get_value(
			"Warehouse",
			{"company": company, "is_group": 0, "disabled": 0, "warehouse_name": ["like", pattern]},
			"name",
		)
		if stores_wh:
			return stores_wh
	active_wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0, "disabled": 0}, "name")
	if active_wh:
		return active_wh
	return frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")


def _get_stock_settings_default_warehouse():
	if frappe.db.exists("DocType", "Stock Settings") and frappe.get_meta("Stock Settings").has_field("default_warehouse"):
		return frappe.db.get_single_value("Stock Settings", "default_warehouse")
	return None


def _resolve_default_warehouse(company=None):
	"""Internal warehouse lookup after caller has enforced boundary permissions."""
	if company:
		stock_wh = _get_stock_settings_default_warehouse()
		if stock_wh and frappe.db.exists(
			"Warehouse", {"name": stock_wh, "company": company, "is_group": 0, "disabled": 0}
		):
			return stock_wh
		wh = _find_company_warehouse(company)
		if wh:
			return wh

	stock_wh = _get_stock_settings_default_warehouse()
	if stock_wh and frappe.db.exists("Warehouse", {"name": stock_wh, "is_group": 0, "disabled": 0}):
		return stock_wh

	default_company = frappe.db.get_single_value("Global Defaults", "default_company")
	wh = _find_company_warehouse(default_company)
	if wh:
		return wh

	return (
		frappe.db.get_value("Warehouse", {"is_group": 0, "disabled": 0}, "name")
		or frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
		or "Stores"
	)


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


def clear_bin_dimension_meta_cache() -> None:
	"""Invalidate cached Bin Location Inventory Dimension metadata."""
	try:
		frappe.cache.delete_value(_BIN_DIM_CACHE_KEY)
	except Exception:
		pass


def _normalize_bin_code(
	value: Any,
	label: str = "Bin Location",
	*,
	required: bool = False,
) -> Optional[str]:
	"""Validate and normalize a Storage Bin code to stripped uppercase."""
	cleaned = require_str(value, label) if required else optional_str(value, label)
	return cleaned.upper() if cleaned else None


def _validate_storage_bin_exists(
	bin_code: str,
	warehouse: Optional[str] = None,
	*,
	require_active: bool = True,
) -> dict[str, Any]:
	"""Validate that a Storage Bin exists, is active (when required), and matches warehouse if specified."""
	code = _normalize_bin_code(bin_code, "Bin Location", required=True)
	row = frappe.db.get_value(
		"Storage Bin",
		code,
		["name", "bin_code", "warehouse", "bin_type", "is_active", "zone", "aisle", "rack", "shelf", "description"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Storage Bin '{0}' does not exist.").format(code), frappe.DoesNotExistError)
	if require_active and cint(row.get("is_active")) != 1:
		frappe.throw(_("Storage Bin '{0}' is inactive.").format(code), frappe.ValidationError)
	if warehouse and row.get("warehouse") and row.get("warehouse") != warehouse:
		frappe.throw(
			_("Storage Bin '{0}' belongs to warehouse '{1}', not '{2}'.").format(
				code, row.get("warehouse"), warehouse
			),
			frappe.ValidationError,
		)
	return row


def get_bin_dimension_meta(use_cache: bool = True) -> dict[str, Any]:
	"""Return active ERPNext Inventory Dimension metadata for Storage Bin, verifying SLE column presence."""
	disabled_meta: dict[str, Any] = {
		"enabled": False,
		"name": None,
		"source_fieldname": None,
		"target_fieldname": None,
		"sle_fieldname": None,
		"validate_negative_stock": False,
	}

	if use_cache:
		try:
			cached = frappe.cache.get_value(_BIN_DIM_CACHE_KEY)
		except Exception:
			cached = None
		if (
			isinstance(cached, dict)
			and cached.get("enabled")
			and cached.get("sle_fieldname")
			and _SAFE_SQL_IDENT_RE.fullmatch(str(cached["sle_fieldname"]))
			and frappe.db.has_column("Stock Ledger Entry", cached["sle_fieldname"])
		):
			return dict(cached)

	if not frappe.db.exists("DocType", "Inventory Dimension"):
		return disabled_meta

	has_disabled_col = frappe.db.has_column("Inventory Dimension", "disabled")
	ref_filters: dict[str, Any] = {"reference_document": "Storage Bin"}
	name_filters: dict[str, Any] = {"dimension_name": "Bin Location"}
	if has_disabled_col:
		ref_filters["disabled"] = 0
		name_filters["disabled"] = 0

	row = frappe.db.get_value(
		"Inventory Dimension",
		ref_filters,
		["name", "dimension_name", "source_fieldname", "target_fieldname", "validate_negative_stock"],
		as_dict=True,
	) or frappe.db.get_value(
		"Inventory Dimension",
		name_filters,
		["name", "dimension_name", "source_fieldname", "target_fieldname", "validate_negative_stock"],
		as_dict=True,
	)
	if not row:
		return disabled_meta

	source_fn = (row.get("source_fieldname") or "bin_location").strip()
	raw_target_fn = (row.get("target_fieldname") or "").strip()
	to_source_fn = f"to_{source_fn}"
	if (not raw_target_fn or raw_target_fn == source_fn) and frappe.db.has_column(
		"Stock Entry Detail", to_source_fn
	):
		target_fn = to_source_fn
	else:
		target_fn = raw_target_fn or to_source_fn
	sle_fn = source_fn

	if not _SAFE_SQL_IDENT_RE.fullmatch(sle_fn) or not frappe.db.has_column("Stock Ledger Entry", sle_fn):
		return {
			"enabled": False,
			"name": row.get("name"),
			"source_fieldname": source_fn,
			"target_fieldname": target_fn,
			"sle_fieldname": sle_fn,
			"validate_negative_stock": bool(cint(row.get("validate_negative_stock"))),
		}

	meta = {
		"enabled": True,
		"name": row.get("name"),
		"source_fieldname": source_fn,
		"target_fieldname": target_fn,
		"sle_fieldname": sle_fn,
		"validate_negative_stock": bool(cint(row.get("validate_negative_stock"))),
	}
	if use_cache:
		try:
			frappe.cache.set_value(_BIN_DIM_CACHE_KEY, meta)
		except Exception:
			pass
	return meta


def get_items_bin_stock_batch(
	item_codes: list[str],
	warehouse: str | None = None,
	*,
	_actual_qty_map: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
	"""Return per-item live bin stock breakdown + default_bin_location in one batched SLE query."""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	if not codes:
		return {}

	default_bin_map: dict[str, Optional[str]] = {}
	if frappe.db.has_column("Item", "default_bin_location"):
		item_rows = frappe.get_all(
			"Item",
			filters={"name": ["in", codes]},
			fields=["name", "default_bin_location"],
		)
		for row in item_rows:
			raw_bin = row.get("default_bin_location")
			default_bin_map[row["name"]] = str(raw_bin).strip().upper() if raw_bin and str(raw_bin).strip() else None

	results: dict[str, dict[str, Any]] = {
		code: {
			"item_code": code,
			"warehouse": warehouse,
			"default_bin_location": default_bin_map.get(code),
			"bins": [],
			"primary_bin": None,
			"unbinned_qty": 0.0,
			"total_binned_qty": 0.0,
		}
		for code in codes
	}

	bin_qty_map = _actual_qty_map if _actual_qty_map is not None else _get_items_stock_qty_map(codes, warehouse)

	dim_meta = get_bin_dimension_meta()
	if not dim_meta.get("enabled"):
		for code in codes:
			qty = flt(bin_qty_map.get(code, 0.0), 6)
			if qty > 1e-6:
				results[code]["unbinned_qty"] = qty
		return results

	sle_col = dim_meta["sle_fieldname"]
	params: list[Any] = [tuple(codes)]
	wh_clause = ""
	if warehouse:
		wh_clause = " AND warehouse = %s"
		params.append(warehouse)

	sle_rows = frappe.db.sql(
		f"""
		SELECT item_code, IFNULL(`{sle_col}`, '') AS bin_location, SUM(actual_qty) AS qty
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled = 0
		  AND item_code IN %s
		  {wh_clause}
		GROUP BY item_code, IFNULL(`{sle_col}`, '')
		HAVING SUM(actual_qty) > 0.000001
		""",
		tuple(params),
		as_dict=True,
	)

	for row in sle_rows:
		code = row.get("item_code")
		if code not in results:
			continue
		qty = flt(row.get("qty"), 6)
		raw_bin = (row.get("bin_location") or "").strip().upper()
		if raw_bin:
			home_bin = results[code]["default_bin_location"]
			results[code]["bins"].append(
				{
					"bin_location": raw_bin,
					"qty": qty,
					"is_default": bool(home_bin and raw_bin == home_bin),
				}
			)

	for code, entry in results.items():
		entry["bins"].sort(
			key=lambda b: (
				0 if b.get("is_default") else 1,
				-flt(b.get("qty")),
				str(b.get("bin_location") or ""),
			)
		)
		entry["primary_bin"] = entry["bins"][0]["bin_location"] if entry["bins"] else None
		entry["total_binned_qty"] = flt(sum(flt(b.get("qty")) for b in entry["bins"]), 6)
		# Reconcile unbinned stock against authoritative Bin.actual_qty so Stock Reconciliation
		# opening balances (SLE actual_qty=0, qty_after_transaction=N) are preserved accurately.
		entry["unbinned_qty"] = max(
			0.0,
			flt(flt(bin_qty_map.get(code, 0.0)) - entry["total_binned_qty"], 6),
		)

	return results


def allocate_item_bins(
	item_code: str,
	required_qty: float,
	bin_info: dict[str, Any] | None,
	explicit_bin: str | None = None,
) -> list[tuple[str | None, float]]:
	"""Deterministically allocate required_qty across bins: pinned bin OR Home Bin -> overflow -> legacy unbinned."""
	req = flt(required_qty, 6)
	if req <= 0:
		return []

	info = bin_info or {}
	bins = info.get("bins") or []
	pinned = _normalize_bin_code(explicit_bin, "Bin Location", required=False)

	if pinned:
		avail = 0.0
		for b in bins:
			if str(b.get("bin_location") or "").strip().upper() == pinned:
				avail = flt(b.get("qty"), 6)
				break
		if avail < req - 1e-6:
			shortfall = flt(req - avail, 3)
			frappe.throw(
				_("Insufficient stock for {0} in Bin '{1}': required {2}, available {3}, shortfall {4}.").format(
					item_code, pinned, flt(req, 3), flt(avail, 3), shortfall
				),
				frappe.ValidationError,
			)
		return [(pinned, req)]

	remaining = req
	slices: list[tuple[str | None, float]] = []
	for b in bins:
		b_code = str(b.get("bin_location") or "").strip().upper()
		b_qty = flt(b.get("qty"), 6)
		if not b_code or b_qty <= 1e-6:
			continue
		take = min(remaining, b_qty)
		if take > 1e-6:
			slices.append((b_code, flt(take, 6)))
			remaining = flt(remaining - take, 6)
		if remaining <= 1e-6:
			break

	if remaining > 1e-6:
		unbinned = flt(info.get("unbinned_qty", 0.0), 6)
		if unbinned > 1e-6:
			take = min(remaining, unbinned)
			slices.append((None, flt(take, 6)))
			remaining = flt(remaining - take, 6)

	if remaining > 1e-6:
		avail_total = flt(req - remaining, 3)
		frappe.throw(
			_("Insufficient bin stock for {0}: required {1}, available {2}, shortfall {3}.").format(
				item_code, flt(req, 3), avail_total, flt(remaining, 3)
			),
			frappe.ValidationError,
		)

	return slices


def consume_allocated_bin_slices(
	working_bin_info: dict[str, Any],
	slices: list[tuple[str | None, float]],
) -> None:
	"""Deduct allocated (bin_or_None, qty) slices from a mutable working copy of bin_info in place."""
	bins = working_bin_info.get("bins") or []
	for alloc_bin, alloc_qty in slices:
		qty = flt(alloc_qty, 6)
		if qty <= 0:
			continue
		if alloc_bin is None:
			working_bin_info["unbinned_qty"] = max(
				0.0, flt(flt(working_bin_info.get("unbinned_qty", 0.0)) - qty, 6)
			)
		else:
			target = str(alloc_bin).strip().upper()
			for entry in bins:
				if str(entry.get("bin_location") or "").strip().upper() == target:
					entry["qty"] = max(0.0, flt(flt(entry.get("qty")) - qty, 6))
					break

	active_bins = [b for b in bins if flt(b.get("qty")) > 1e-6]
	working_bin_info["bins"] = active_bins
	working_bin_info["primary_bin"] = active_bins[0]["bin_location"] if active_bins else None
	working_bin_info["total_binned_qty"] = flt(sum(flt(b.get("qty")) for b in active_bins), 6)


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
	bin_map = get_items_bin_stock_batch(codes, warehouse, _actual_qty_map=actual_map)

	results = {}
	for code in codes:
		actual_qty = flt(actual_map.get(code, 0.0))
		committed_qty, details = committed_map.get(code, (0.0, []))
		available_qty = max(0.0, actual_qty - flt(committed_qty))
		bin_info = bin_map.get(code) or {}
		results[code] = {
			"item_code": code,
			"warehouse": warehouse,
			"actual_qty": actual_qty,
			"committed_qty": flt(committed_qty),
			"available_qty": flt(available_qty),
			"committed_orders": details,
			"bin_locations": bin_info.get("bins", []),
			"primary_bin": bin_info.get("primary_bin"),
			"default_bin_location": bin_info.get("default_bin_location"),
			"unbinned_qty": flt(bin_info.get("unbinned_qty", 0.0)),
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
			"bin_locations": [],
			"primary_bin": None,
			"default_bin_location": None,
			"unbinned_qty": 0.0,
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


def _validate_non_group_warehouse(warehouse: str) -> dict[str, Any]:
	wh_meta = frappe.db.get_value(
		"Warehouse",
		warehouse,
		["name", "company", "is_group", "disabled"],
		as_dict=True,
	)
	if not wh_meta:
		frappe.throw(_("Warehouse '{0}' does not exist.").format(warehouse), frappe.DoesNotExistError)
	if cint(wh_meta.get("is_group")) == 1:
		frappe.throw(_("Warehouse '{0}' cannot be a group warehouse.").format(warehouse), frappe.ValidationError)
	return wh_meta


def _is_bin_required_on_receive() -> bool:
	if not frappe.db.exists("DocType", "ISM Settings"):
		return False
	meta = frappe.get_meta("ISM Settings")
	if not meta.has_field("require_bin_on_receive"):
		return False
	return bool(cint(frappe.db.get_single_value("ISM Settings", "require_bin_on_receive")))


def _parse_stock_movement_items(
	items: Any = None,
	item_code: Optional[str] = None,
	qty: Any = None,
	*,
	allow_single_args: bool = False,
) -> list[dict[str, Any]]:
	"""Parse and validate item movement rows from list/JSON or single item_code + qty arguments."""
	has_items = (
		items is not None
		and items != []
		and items != {}
		and (not isinstance(items, str) or bool(items.strip()))
	)
	single_code = optional_str(item_code, "Item Code") if allow_single_args else None

	if allow_single_args and has_items and single_code:
		frappe.throw(
			_("Provide either items or item_code/qty, not both."),
			frappe.ValidationError,
		)

	raw_list: list[Any]
	if has_items:
		parsed = parse_json_arg(items, label="Items") if isinstance(items, str) else items
		if isinstance(parsed, dict):
			raw_list = [parsed]
		elif isinstance(parsed, list):
			raw_list = parsed
		else:
			frappe.throw(_("Items must be a list of objects."), frappe.ValidationError)
	elif allow_single_args and single_code:
		parsed_qty = optional_flt(qty, f"Quantity for {single_code}", positive=True, default=None)
		if parsed_qty is None:
			frappe.throw(
				_("Positive qty is required when item_code is specified."),
				frappe.MandatoryError,
			)
		raw_list = [{"item_code": single_code, "qty": parsed_qty}]
	else:
		frappe.throw(_("At least one item is required."), frappe.MandatoryError)

	if not raw_list:
		frappe.throw(_("At least one item is required."), frappe.MandatoryError)

	normalized: list[dict[str, Any]] = []
	for idx, entry in enumerate(raw_list, start=1):
		if not isinstance(entry, dict):
			frappe.throw(_("Item entry #{0} must be a JSON object.").format(idx), frappe.ValidationError)
		code = require_str(entry.get("item_code"), f"Item Code (row {idx})")
		row_qty = optional_flt(entry.get("qty"), f"Quantity for {code}", positive=True, default=None)
		if row_qty is None:
			frappe.throw(_("Positive qty is required for item '{0}'.").format(code), frappe.MandatoryError)
		row_dict = dict(entry)
		row_dict["item_code"] = code
		row_dict["qty"] = flt(row_qty, 6)
		normalized.append(row_dict)
	return normalized


@frappe.whitelist()
def list_bin_locations(
	warehouse: Optional[str] = None,
	zone: Optional[str] = None,
	query: Optional[str] = None,
	include_stock: Any = False,
) -> dict[str, Any]:
	"""List active Storage Bins with optional warehouse, zone, search query, and live SLE stock breakdown."""
	frappe.has_permission("Storage Bin", "read", throw=True)
	warehouse = optional_str(warehouse, "Warehouse")
	zone = optional_str(zone, "Zone")
	query = optional_str(query, "Query")
	want_stock = coerce_bool(include_stock, default=False)

	if want_stock:
		frappe.has_permission("Item", "read", throw=True)

	filters: dict[str, Any] = {"is_active": 1}
	if warehouse:
		filters["warehouse"] = warehouse
	if zone:
		filters["zone"] = zone

	or_filters = None
	if query:
		escaped = escape_like(query)
		or_filters = [
			["bin_code", "like", f"%{escaped}%"],
			["zone", "like", f"%{escaped}%"],
			["aisle", "like", f"%{escaped}%"],
			["rack", "like", f"%{escaped}%"],
			["shelf", "like", f"%{escaped}%"],
			["description", "like", f"%{escaped}%"],
		]

	rows = frappe.get_list(
		"Storage Bin",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"bin_code",
			"warehouse",
			"bin_type",
			"is_active",
			"zone",
			"aisle",
			"rack",
			"shelf",
			"description",
		],
		order_by="bin_code asc",
		limit_page_length=500,
	)

	if want_stock:
		dim_meta = get_bin_dimension_meta()
		stock_by_bin: dict[str, list[dict[str, Any]]] = {}
		if rows and dim_meta.get("enabled"):
			sle_col = dim_meta["sle_fieldname"]
			bin_names = [r["name"] for r in rows]
			params: list[Any] = [tuple(bin_names)]
			wh_clause = ""
			if warehouse:
				wh_clause = " AND warehouse = %s"
				params.append(warehouse)
			sle_rows = frappe.db.sql(
				f"""
				SELECT `{sle_col}` AS bin_location, item_code, SUM(actual_qty) AS qty
				FROM `tabStock Ledger Entry`
				WHERE is_cancelled = 0
				  AND `{sle_col}` IN %s
				  {wh_clause}
				GROUP BY `{sle_col}`, item_code
				HAVING SUM(actual_qty) > 0.000001
				ORDER BY `{sle_col}` ASC, item_code ASC
				""",
				tuple(params),
				as_dict=True,
			)
			for srow in sle_rows:
				b_key = str(srow.get("bin_location") or "").strip().upper()
				if b_key:
					stock_by_bin.setdefault(b_key, []).append(
						{"item_code": srow["item_code"], "qty": flt(srow["qty"], 6)}
					)

		for row in rows:
			items_in_bin = stock_by_bin.get(row["name"], [])
			row["stock_items"] = items_in_bin
			row["sku_count"] = len(items_in_bin)
			row["total_qty"] = flt(sum(flt(x["qty"]) for x in items_in_bin), 6)

	return {
		"warehouse": warehouse,
		"zone": zone,
		"query": query,
		"include_stock": want_stock,
		"count": len(rows),
		"bins": rows,
	}


@frappe.whitelist()
def create_bin_location(
	bin_code: str,
	warehouse: Optional[str] = None,
	zone: Optional[str] = None,
	aisle: Optional[str] = None,
	rack: Optional[str] = None,
	shelf: Optional[str] = None,
	bin_type: str = "Standard",
	description: Optional[str] = None,
) -> dict[str, Any]:
	"""Create a new Storage Bin master record with uppercase bin_code normalization."""
	frappe.has_permission("Storage Bin", "create", throw=True)
	code = _normalize_bin_code(bin_code, "Bin Code", required=True)
	resolved_wh = optional_str(warehouse, "Warehouse") or _resolve_default_warehouse()
	_validate_non_group_warehouse(resolved_wh)

	resolved_type = optional_str(bin_type, "Bin Type") or "Standard"
	if resolved_type not in _VALID_BIN_TYPES:
		frappe.throw(
			_("Invalid Bin Type '{0}'. Allowed values: {1}.").format(
				resolved_type, ", ".join(_VALID_BIN_TYPES)
			),
			frappe.ValidationError,
		)

	if frappe.db.exists("Storage Bin", code):
		frappe.throw(_("Storage Bin '{0}' already exists.").format(code), frappe.ValidationError)

	doc = frappe.get_doc(
		{
			"doctype": "Storage Bin",
			"bin_code": code,
			"warehouse": resolved_wh,
			"bin_type": resolved_type,
			"is_active": 1,
			"zone": optional_str(zone, "Zone") or "",
			"aisle": optional_str(aisle, "Aisle") or "",
			"rack": optional_str(rack, "Rack") or "",
			"shelf": optional_str(shelf, "Shelf") or "",
			"description": optional_str(description, "Description") or "",
		}
	)
	doc.check_permission("create")
	doc.insert()

	return {
		"name": doc.name,
		"bin_code": doc.bin_code,
		"warehouse": doc.warehouse,
		"bin_type": doc.bin_type,
		"is_active": cint(doc.is_active),
		"zone": doc.zone or "",
		"aisle": doc.aisle or "",
		"rack": doc.rack or "",
		"shelf": doc.shelf or "",
		"description": doc.description or "",
	}


@frappe.whitelist()
def set_item_default_bin(
	item_code: str,
	bin_location: Optional[str] = None,
) -> dict[str, Any]:
	"""Assign or clear an Item's Home Bin (Item.default_bin_location)."""
	frappe.has_permission("Item", "write", throw=True)
	code = require_str(item_code, "Item Code")
	if not frappe.db.exists("Item", code):
		frappe.throw(_("Item '{0}' not found.").format(code), frappe.DoesNotExistError)

	target_bin = _normalize_bin_code(bin_location, "Bin Location", required=False)
	if target_bin:
		frappe.has_permission("Storage Bin", "read", throw=True)
		_validate_storage_bin_exists(target_bin, require_active=True)

	if not frappe.db.has_column("Item", "default_bin_location"):
		from ism.setup_tasks.fields import setup_item_custom_fields

		setup_item_custom_fields()

	item_doc = frappe.get_doc("Item", code)
	item_doc.check_permission("write")
	item_doc.default_bin_location = target_bin or ""
	item_doc.save()

	return {
		"item_code": item_doc.name,
		"item_name": item_doc.item_name,
		"default_bin_location": target_bin,
	}


@frappe.whitelist()
def transfer_bin_stock(
	from_bin: str,
	to_bin: str,
	items: Any = None,
	item_code: Optional[str] = None,
	qty: Any = None,
	warehouse: Optional[str] = None,
	notes: Optional[str] = None,
) -> dict[str, Any]:
	"""Transfer stock between two Storage Bins within the same warehouse via Stock Entry (Material Transfer)."""
	frappe.has_permission("Stock Entry", "create", throw=True)
	frappe.has_permission("Stock Entry", "submit", throw=True)
	frappe.has_permission("Storage Bin", "read", throw=True)

	dim_meta = get_bin_dimension_meta()
	if not dim_meta.get("enabled"):
		frappe.throw(
			_("Bin Location Inventory Dimension is not configured or enabled."),
			frappe.ValidationError,
		)

	src_bin = _normalize_bin_code(from_bin, "From Bin", required=True)
	dst_bin = _normalize_bin_code(to_bin, "To Bin", required=True)
	if src_bin == dst_bin:
		frappe.throw(
			_("Source bin '{0}' and target bin '{1}' must be different.").format(src_bin, dst_bin),
			frappe.ValidationError,
		)

	src_doc = _validate_storage_bin_exists(src_bin, require_active=True)
	dst_doc = _validate_storage_bin_exists(dst_bin, require_active=True)

	explicit_wh = optional_str(warehouse, "Warehouse")
	resolved_wh = explicit_wh or src_doc.get("warehouse") or dst_doc.get("warehouse") or _resolve_default_warehouse()
	wh_meta = _validate_non_group_warehouse(resolved_wh)
	_validate_storage_bin_exists(src_bin, warehouse=resolved_wh, require_active=True)
	_validate_storage_bin_exists(dst_bin, warehouse=resolved_wh, require_active=True)

	parsed_items = _parse_stock_movement_items(items=items, item_code=item_code, qty=qty, allow_single_args=True)
	unique_codes = list(dict.fromkeys(r["item_code"] for r in parsed_items))
	item_rows = frappe.get_all(
		"Item",
		filters={"name": ["in", unique_codes]},
		fields=["name", "is_stock_item"],
	)
	item_map = {r["name"]: r for r in item_rows}
	for code in unique_codes:
		if code not in item_map:
			frappe.throw(_("Item '{0}' not found.").format(code), frappe.DoesNotExistError)
		if cint(item_map[code].get("is_stock_item")) != 1:
			frappe.throw(_("Item '{0}' is not a stock item.").format(code), frappe.ValidationError)

	required_by_code: dict[str, float] = {}
	for row in parsed_items:
		required_by_code[row["item_code"]] = flt(required_by_code.get(row["item_code"], 0.0) + row["qty"], 6)

	bin_stock_map = get_items_bin_stock_batch(unique_codes, warehouse=resolved_wh)
	for code, req_qty in required_by_code.items():
		allocate_item_bins(code, req_qty, bin_stock_map.get(code), explicit_bin=src_bin)

	clean_notes = optional_str(notes, "Notes")
	source_fn = dim_meta["source_fieldname"]
	target_fn = dim_meta["target_fieldname"]

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer"
	se.purpose = "Material Transfer"
	se.company = wh_meta["company"]
	if clean_notes:
		se.remarks = clean_notes

	for row in parsed_items:
		se_row: dict[str, Any] = {
			"item_code": row["item_code"],
			"qty": row["qty"],
			"s_warehouse": resolved_wh,
			"t_warehouse": resolved_wh,
			source_fn: src_bin,
		}
		if target_fn:
			se_row[target_fn] = dst_bin
		se.append("items", se_row)

	se.check_permission("create")
	se.insert()
	se.check_permission("submit")
	se.submit()

	return {
		"stock_entry": se.name,
		"docstatus": se.docstatus,
		"stock_entry_type": se.stock_entry_type,
		"company": se.company,
		"warehouse": resolved_wh,
		"from_bin": src_bin,
		"to_bin": dst_bin,
		"notes": clean_notes,
		"items": [
			{"item_code": r.item_code, "qty": flt(r.qty, 6)}
			for r in (se.items or [])
		],
	}


@frappe.whitelist()
def adjust_bin_stock(
	purpose: str,
	items: Any,
	bin_location: Optional[str] = None,
	warehouse: Optional[str] = None,
	reason: Optional[str] = None,
) -> dict[str, Any]:
	"""Post a Stock Entry Material Receipt or Material Issue targeting specific Storage Bin(s)."""
	frappe.has_permission("Stock Entry", "create", throw=True)
	frappe.has_permission("Stock Entry", "submit", throw=True)

	raw_purpose = require_str(purpose, "Purpose").lower()
	if raw_purpose in ("receipt", "material receipt"):
		resolved_purpose = "Material Receipt"
	elif raw_purpose in ("issue", "material issue"):
		resolved_purpose = "Material Issue"
	else:
		frappe.throw(
			_("Purpose must be 'Material Receipt' ('receipt') or 'Material Issue' ('issue')."),
			frappe.ValidationError,
		)

	dim_meta = get_bin_dimension_meta()
	if not dim_meta.get("enabled"):
		frappe.throw(
			_("Bin Location Inventory Dimension is not configured or enabled."),
			frappe.ValidationError,
		)

	uniform_bin = _normalize_bin_code(bin_location, "Bin Location", required=False)
	explicit_wh = optional_str(warehouse, "Warehouse")
	clean_reason = optional_str(reason, "Reason")

	uniform_bin_doc = None
	if uniform_bin:
		frappe.has_permission("Storage Bin", "read", throw=True)
		uniform_bin_doc = _validate_storage_bin_exists(uniform_bin, require_active=True)

	resolved_wh = explicit_wh or (uniform_bin_doc.get("warehouse") if uniform_bin_doc else None) or _resolve_default_warehouse()
	wh_meta = _validate_non_group_warehouse(resolved_wh)
	if uniform_bin:
		_validate_storage_bin_exists(uniform_bin, warehouse=resolved_wh, require_active=True)

	parsed_items = _parse_stock_movement_items(items=items, allow_single_args=False)
	unique_codes = list(dict.fromkeys(r["item_code"] for r in parsed_items))
	item_fields = ["name", "is_stock_item", "valuation_rate", "standard_rate"]
	has_default_bin_col = frappe.db.has_column("Item", "default_bin_location")
	if has_default_bin_col:
		item_fields.append("default_bin_location")

	item_rows = frappe.get_all("Item", filters={"name": ["in", unique_codes]}, fields=item_fields)
	item_map = {r["name"]: r for r in item_rows}
	for code in unique_codes:
		if code not in item_map:
			frappe.throw(_("Item '{0}' not found.").format(code), frappe.DoesNotExistError)
		if cint(item_map[code].get("is_stock_item")) != 1:
			frappe.throw(_("Item '{0}' is not a stock item.").format(code), frappe.ValidationError)

	require_bin = _is_bin_required_on_receive()
	resolved_rows: list[dict[str, Any]] = []
	validated_bins: set[str] = {uniform_bin} if uniform_bin else set()

	for row in parsed_items:
		code = row["item_code"]
		item_meta = item_map[code]
		item_default_bin = (
			str(item_meta.get("default_bin_location")).strip().upper()
			if has_default_bin_col and item_meta.get("default_bin_location")
			else None
		)
		row_bin = (
			_normalize_bin_code(row.get("bin_location"), "Bin Location", required=False)
			or uniform_bin
			or item_default_bin
		)
		if row_bin and row_bin not in validated_bins:
			frappe.has_permission("Storage Bin", "read", throw=True)
			_validate_storage_bin_exists(row_bin, warehouse=resolved_wh, require_active=True)
			validated_bins.add(row_bin)
		if not row_bin and resolved_purpose == "Material Receipt" and require_bin:
			frappe.throw(
				_("Bin Location is required when receiving stock item '{0}'.").format(code),
				frappe.MandatoryError,
			)
		raw_rate = row.get("rate")
		if raw_rate is None:
			raw_rate = row.get("basic_rate")
		if raw_rate is None:
			raw_rate = row.get("valuation_rate")
		explicit_rate = optional_flt(raw_rate, f"Rate for {code}", non_negative=True, default=None)
		resolved_rows.append(
			{
				"item_code": code,
				"qty": row["qty"],
				"bin_location": row_bin,
				"rate": explicit_rate,
				"item_meta": item_meta,
			}
		)

	if resolved_purpose == "Material Issue":
		bin_stock_map = get_items_bin_stock_batch(unique_codes, warehouse=resolved_wh)
		req_by_item_bin: dict[tuple[str, Optional[str]], float] = {}
		for r in resolved_rows:
			key = (r["item_code"], r["bin_location"])
			req_by_item_bin[key] = flt(req_by_item_bin.get(key, 0.0) + r["qty"], 6)
		for (code, target_bin), req_qty in req_by_item_bin.items():
			info = bin_stock_map.get(code) or {}
			if target_bin:
				allocate_item_bins(code, req_qty, info, explicit_bin=target_bin)
			else:
				unbinned = flt(info.get("unbinned_qty", 0.0), 6)
				if unbinned < req_qty - 1e-6:
					frappe.throw(
						_("Insufficient unbinned stock for {0}: required {1}, available {2}.").format(
							code, flt(req_qty, 3), flt(unbinned, 3)
						),
						frappe.ValidationError,
					)

	source_fn = dim_meta["source_fieldname"]
	target_fn = dim_meta["target_fieldname"]

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = resolved_purpose
	se.purpose = resolved_purpose
	se.company = wh_meta["company"]
	if clean_reason:
		se.remarks = clean_reason

	for r in resolved_rows:
		if resolved_purpose == "Material Receipt":
			se_row: dict[str, Any] = {
				"item_code": r["item_code"],
				"qty": r["qty"],
				"t_warehouse": resolved_wh,
			}
			rate_val = r["rate"]
			if rate_val is None or rate_val <= 0:
				fallback_rate = flt(r["item_meta"].get("valuation_rate")) or flt(r["item_meta"].get("standard_rate"))
				if fallback_rate > 0:
					se_row["basic_rate"] = fallback_rate
				else:
					se_row["allow_zero_valuation_rate"] = 1
			else:
				se_row["basic_rate"] = rate_val
			if r["bin_location"]:
				if target_fn:
					se_row[target_fn] = r["bin_location"]
				if source_fn:
					se_row[source_fn] = r["bin_location"]
		else:
			se_row = {
				"item_code": r["item_code"],
				"qty": r["qty"],
				"s_warehouse": resolved_wh,
			}
			if r["bin_location"] and source_fn:
				se_row[source_fn] = r["bin_location"]
		se.append("items", se_row)

	se.check_permission("create")
	se.insert()
	se.check_permission("submit")
	se.submit()

	return {
		"stock_entry": se.name,
		"docstatus": se.docstatus,
		"stock_entry_type": se.stock_entry_type,
		"purpose": resolved_purpose,
		"company": se.company,
		"warehouse": resolved_wh,
		"reason": clean_reason,
		"items": [
			{
				"item_code": r["item_code"],
				"qty": flt(r["qty"], 6),
				"bin_location": r["bin_location"],
			}
			for r in resolved_rows
		],
	}
