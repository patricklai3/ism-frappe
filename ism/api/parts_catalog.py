# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.api._validation import (
	coerce_bool,
	escape_like,
	normalize_condition,
	optional_flt,
	optional_int,
	optional_str,
	parse_json_arg,
	require_str,
	scalar_str,
)
from ism.api.parts_procurement import _apply_supplier_to_item_doc, _ensure_supplier_exists
from ism.api.parts_stock import _get_item_stock_qty, _get_items_stock_qty_map, _resolve_default_warehouse

LHRH_PATTERN = re.compile(
	r"\b(LH|RH)\b|\bLeft\s+Hand\b(?!\s*Drive)|\bRight\s+Hand\b(?!\s*Drive)",
	re.IGNORECASE,
)
LH_ONLY_PATTERN = re.compile(r"\bLH\b|\bLeft\s+Hand\b(?!\s*Drive)", re.IGNORECASE)
RH_ONLY_PATTERN = re.compile(r"\bRH\b|\bRight\s+Hand\b(?!\s*Drive)", re.IGNORECASE)

_IMPORT_HEADER_TOKENS = frozenset(
	{
		"part",
		"part#",
		"part #",
		"part no",
		"part no.",
		"part number",
		"part_number",
		"part_no",
		"item",
		"item#",
		"item #",
		"item code",
		"item_code",
		"sku",
		"code",
	}
)


def _normalize_part_source(part_source):
	return "OEM" if str(part_source or "").strip().upper().startswith("OEM") else "Aftermarket"


def _normalize_part_condition(part_condition):
	return normalize_condition(part_condition, as_code=False)


def _coerce_item_qty(qty):
	val = optional_flt(qty, "Quantity", non_negative=False, default=1.0)
	return val if val is not None and val > 0 else 1.0


def format_item_code(part_source="OEM", part_number="", part_condition="New"):
	"""Generate strictly standardized Item Code: [Source]-[PartNumber]-[Condition].
	e.g. OEM-1044321-00-J-NEW, OEM-1044321-00-J-USED, AFT-RK623000-NEW
	"""
	src = "OEM" if str(part_source or "").strip().upper().startswith("OEM") else "AFT"
	cond = normalize_condition(part_condition, as_code=True)

	raw_part = "" if part_number is None else str(part_number).strip().upper()
	clean_part = re.sub(r"[^A-Za-z0-9_\-]", "", raw_part) or "UNKNOWN"
	return f"{src}-{clean_part}-{cond}"


def has_lhrh_designation(val):
	"""Check if text contains explicit LH, RH, Left Hand, or Right Hand designation.
	Phrases like 'driver's side' or 'passenger side' do NOT qualify.
	"""
	if not val:
		return False
	return bool(LHRH_PATTERN.search(str(val)))


def detect_part_side(val):
	"""Detect LH, RH, Both, or None from part text."""
	if not val:
		return None
	s = str(val)
	has_lh = bool(LH_ONLY_PATTERN.search(s))
	has_rh = bool(RH_ONLY_PATTERN.search(s))
	if has_lh and has_rh:
		return "Both"
	if has_lh:
		return "LH"
	if has_rh:
		return "RH"
	return None


def resolve_part_side(side="Both", service_package=None, part_name=None, part_number=None, is_symmetrical=None):
	"""Resolve Side / Variant for Service Package Item.
	Must strictly return one of 'Both', 'LH', 'RH'. Never 'None'.
	"""
	side_str = str(side or "").strip().upper()
	if side_str == "LH":
		candidate_side = "LH"
	elif side_str == "RH":
		candidate_side = "RH"
	else:
		candidate_side = "Both"

	part_ident = f"{part_number or ''} {part_name or ''}".strip()
	part_detected_side = detect_part_side(part_ident)

	pkg_has_lhrh = False
	if service_package:
		if has_lhrh_designation(service_package):
			pkg_has_lhrh = True
		elif is_symmetrical is not None:
			pkg_has_lhrh = bool(cint(is_symmetrical))
		elif frappe.db.exists("Service Package", service_package):
			if frappe.db.get_value("Service Package", service_package, "is_symmetrical"):
				pkg_has_lhrh = True

	if not pkg_has_lhrh and not part_detected_side:
		return "Both"
	if part_detected_side:
		return part_detected_side
	if pkg_has_lhrh and candidate_side in ("LH", "RH"):
		return candidate_side
	return "Both"


def parse_slot_number(val):
	"""Extract integer slot number from string or int (e.g. 1, 'Slot 1', 'Unassigned' -> 0).
	Retained as non-whitelisted compatibility helper for repair_order and service_package.
	"""
	if val is None or val == "":
		return 0
	val_str = str(val).strip()
	if val_str.lower() in ("unassigned", "none", "0", ""):
		return 0
	match = re.search(r"slot\s*(\d+)", val_str, re.IGNORECASE)
	if match:
		return cint(match.group(1))
	try:
		num = cint(val_str)
		if num > 0:
			return num
	except Exception:
		pass
	return 0


def format_slot_label(slot_number):
	"""Format standard display label for slot: 'Slot 1', 'Slot 2', or 'Unassigned'.
	Retained as non-whitelisted compatibility helper for repair_order and service_package.
	"""
	s_num = cint(slot_number)
	if s_num <= 0:
		return "Unassigned"
	return f"Slot {s_num}"


def _ensure_item_custom_fields():
	if not frappe.db.has_column("Item", "part_source"):
		from ism.setup import setup_item_custom_fields

		setup_item_custom_fields()


def _backfill_existing_item_fields(item, unit_rate, valuation_rate, oem_part_number, revision, warranty_period):
	updated = False
	if unit_rate and not flt(item.standard_rate):
		item.standard_rate = unit_rate
		updated = True
	if valuation_rate and not flt(item.valuation_rate):
		item.valuation_rate = valuation_rate
		updated = True
	if oem_part_number and not item.oem_part_number:
		item.oem_part_number = oem_part_number
		updated = True
	if revision and not item.revision:
		item.revision = revision
		updated = True
	if warranty_period and not item.warranty_period:
		item.warranty_period = warranty_period
		updated = True
	return updated


def _parse_service_package_names(service_packages=None, primary=None):
	packages = []
	if service_packages:
		if isinstance(service_packages, str):
			stripped = service_packages.strip()
			if stripped.startswith(("[", "{")):
				parsed = parse_json_arg(stripped, [], label="Service Packages")
				if isinstance(parsed, list):
					packages.extend(parsed)
				else:
					frappe.throw(_("Service Packages JSON must be a list."), frappe.ValidationError)
			else:
				packages.extend([s.strip() for s in stripped.split(",") if s.strip()])
		elif isinstance(service_packages, (list, tuple, set)):
			packages.extend(list(service_packages))
		else:
			frappe.throw(_("Service Packages must be a list or string."), frappe.ValidationError)

	cleaned = []
	for pkg_name in packages:
		if isinstance(pkg_name, (dict, list, tuple, set, bool)):
			frappe.throw(_("Service Package name must be a string."), frappe.ValidationError)
		name = str(pkg_name).strip()
		if name and name not in cleaned:
			cleaned.append(name)

	if primary is not None and primary != "":
		if not isinstance(primary, str):
			frappe.throw(_("Service Package must be a string."), frappe.ValidationError)
		primary_name = primary.strip()
		if primary_name and primary_name not in cleaned:
			cleaned.append(primary_name)

	return cleaned


def _resolve_target_slot(slot_number=None, component_slot=None):
	if slot_number is not None and str(slot_number).strip() != "":
		if isinstance(slot_number, (dict, list, tuple, set, bool)):
			frappe.throw(_("Slot number must be an integer."), frappe.ValidationError)
		return cint(slot_number)
	if component_slot:
		return parse_slot_number(component_slot)
	return 1


def _upsert_part_row_on_package_doc(
	pkg: Any,
	item_doc: Any,
	*,
	slot_number: int = 1,
	is_default: int = 0,
	qty: float = 1.0,
	rate: Optional[float] = None,
	side: str = "Both",
	resolved_side: Optional[str] = None,
) -> None:
	s_num = cint(slot_number)
	slot_label = format_slot_label(s_num)

	existing = None
	for row in (pkg.items or []):
		row_snum = cint(getattr(row, "slot_number", None))
		if row_snum == 0 and getattr(row, "component_slot", None):
			row_snum = parse_slot_number(row.component_slot)
		if row.item_code == item_doc.name and (s_num == 0 or row_snum == s_num or row.component_slot == slot_label):
			existing = row
			break

	if is_default and s_num > 0:
		for row in (pkg.items or []):
			row_snum = cint(getattr(row, "slot_number", None))
			if row_snum == 0 and getattr(row, "component_slot", None):
				row_snum = parse_slot_number(row.component_slot)
			if row_snum == s_num and row.item_code != item_doc.name:
				row.is_default = 0

	final_side = resolved_side or resolve_part_side(
		side=side,
		service_package=pkg.name,
		part_name=getattr(item_doc, "item_name", None),
		part_number=getattr(item_doc, "item_code", None) or item_doc.name,
		is_symmetrical=getattr(pkg, "is_symmetrical", None),
	)
	resolved_rate = flt(item_doc.standard_rate) if rate is None else flt(rate)

	if existing:
		existing.slot_number = s_num
		existing.component_slot = slot_label
		if is_default:
			existing.is_default = 1
		if qty:
			existing.qty = flt(qty)
		if rate is not None:
			existing.rate = flt(rate)
		existing.side = final_side
	else:
		pkg.append(
			"items",
			{
				"slot_number": s_num,
				"component_slot": slot_label,
				"item_code": item_doc.name,
				"item_name": item_doc.item_name,
				"is_default": cint(is_default),
				"part_source": getattr(item_doc, "part_source", "OEM") or "OEM",
				"part_condition": getattr(item_doc, "part_condition", "New") or "New",
				"oem_part_number": getattr(item_doc, "oem_part_number", "") or "",
				"warranty_period": getattr(item_doc, "warranty_period", "12 Months / 20,000 km")
				or "12 Months / 20,000 km",
				"qty": flt(qty) or 1.0,
				"rate": resolved_rate,
				"side": final_side,
			},
		)


def _attach_part_to_service_package(
	service_package,
	item_code,
	slot_number=1,
	is_default=0,
	qty=1.0,
	rate=None,
	side="Both",
	*,
	pkg_doc=None,
	item_doc=None,
	save_pkg: bool = True,
	resolved_side: Optional[str] = None,
):
	"""Upsert an item link row on Service Package without relying on removed modal handlers."""
	_ensure_item_custom_fields()

	pkg = pkg_doc or frappe.get_doc("Service Package", service_package)
	if pkg_doc is None:
		pkg.check_permission("write")
	resolved_item = item_doc or frappe.get_cached_doc("Item", item_code)
	if item_doc is None:
		resolved_item.check_permission("read")

	_upsert_part_row_on_package_doc(
		pkg,
		resolved_item,
		slot_number=slot_number,
		is_default=is_default,
		qty=qty,
		rate=rate,
		side=side,
		resolved_side=resolved_side,
	)
	if save_pkg:
		pkg.save()


def _link_item_to_packages(
	item_code,
	package_names,
	target_slot_num=1,
	qty=1.0,
	rate=None,
	side="Both",
	is_default=0,
	part_name=None,
	part_number=None,
	*,
	item_doc=None,
	pkg_cache: Optional[dict[str, Any]] = None,
):
	linked = []
	for idx, pkg_name in enumerate(package_names):
		if not pkg_name:
			continue
		if pkg_cache is not None:
			pkg = pkg_cache.get(pkg_name)
			if pkg is None:
				if not frappe.db.exists("Service Package", pkg_name):
					continue
				pkg = frappe.get_doc("Service Package", pkg_name)
				pkg.check_permission("write")
				pkg_cache[pkg_name] = pkg
			save_now = False
		else:
			if not frappe.db.exists("Service Package", pkg_name):
				continue
			pkg = frappe.get_doc("Service Package", pkg_name)
			pkg.check_permission("write")
			save_now = True

		# Primary package gets target slot & default flag; secondary packages default to Unassigned (0).
		pkg_slot = target_slot_num if idx == 0 else 0
		pkg_side = resolve_part_side(
			side=side,
			service_package=pkg_name,
			part_name=part_name,
			part_number=part_number or item_code,
			is_symmetrical=getattr(pkg, "is_symmetrical", None),
		)
		_attach_part_to_service_package(
			service_package=pkg_name,
			item_code=item_code,
			slot_number=pkg_slot,
			is_default=is_default if idx == 0 else 0,
			qty=qty,
			rate=rate,
			side=pkg_side,
			pkg_doc=pkg,
			item_doc=item_doc,
			save_pkg=save_now,
			resolved_side=pkg_side,
		)
		linked.append(pkg_name)
	return linked


def _upsert_part_core(
	*,
	part_number,
	part_name=None,
	part_source="OEM",
	part_condition="New",
	oem_part_number=None,
	revision=None,
	brand=None,
	warranty_period="12 Months / 20,000 km",
	unit_rate=0.0,
	valuation_rate=0.0,
	stock_uom="Nos",
	slot_number=None,
	component_slot=None,
	service_package=None,
	service_packages=None,
	side="Both",
	is_default=0,
	qty=1.0,
	supplier=None,
	supplier_part_no=None,
	ensured_suppliers: Optional[set[str]] = None,
	pkg_cache: Optional[dict[str, Any]] = None,
	fetch_stock_qty: bool = True,
):
	part_number = require_str(part_number, "Part Number")
	part_name = optional_str(part_name, "Part Name")
	part_source_str = optional_str(part_source, "Part Source") or "OEM"
	part_condition_str = optional_str(part_condition, "Part Condition") or "New"
	oem_part_number = optional_str(oem_part_number, "OEM Part Number")
	revision = optional_str(revision, "Revision")
	brand = optional_str(brand, "Brand")
	warranty_period = optional_str(warranty_period, "Warranty Period") or "12 Months / 20,000 km"
	stock_uom = optional_str(stock_uom, "Stock UOM")
	component_slot = optional_str(component_slot, "Component Slot")
	service_package = optional_str(service_package, "Service Package")
	side = optional_str(side, "Side") or "Both"
	supplier = optional_str(supplier, "Supplier")
	supplier_part_no = optional_str(supplier_part_no, "Supplier Part No")

	norm_source = _normalize_part_source(part_source_str)
	norm_condition = _normalize_part_condition(part_condition_str)
	item_code = format_item_code(norm_source, part_number, norm_condition)

	if not oem_part_number and norm_source == "OEM":
		oem_part_number = part_number
	if not part_name:
		part_name = f"{part_number} ({norm_source} {norm_condition})"

	unit_rate_val = optional_flt(unit_rate, "Unit Rate", non_negative=True, default=0.0) or 0.0
	valuation_rate_val = optional_flt(valuation_rate, "Valuation Rate", non_negative=True, default=0.0) or 0.0
	item_qty = _coerce_item_qty(qty)

	packages_to_link = _parse_service_package_names(service_packages, primary=service_package)
	item_exists = bool(frappe.db.exists("Item", item_code))
	if item_exists:
		item = frappe.get_doc("Item", item_code)
		item.check_permission("read")
	else:
		frappe.has_permission("Item", "create", throw=True)
	if packages_to_link and pkg_cache is None:
		frappe.has_permission("Service Package", "write", throw=True)

	if supplier:
		if ensured_suppliers is None or supplier not in ensured_suppliers:
			_ensure_supplier_exists(supplier)
			if ensured_suppliers is not None:
				ensured_suppliers.add(supplier)

	_ensure_item_custom_fields()

	was_created = not item_exists
	if not item_exists:
		item_group = (
			"Parts"
			if frappe.db.exists("Item Group", "Parts")
			else (frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups")
		)
		uom_name = stock_uom or "Nos"
		if not frappe.db.exists("UOM", uom_name):
			uom_name = "Nos" if frappe.db.exists("UOM", "Nos") else "Unit"

		item = frappe.new_doc("Item")
		item.item_code = item_code
		item.item_name = part_name
		item.item_group = item_group
		item.is_stock_item = 1
		item.is_sales_item = 1
		item.is_purchase_item = 1
		item.include_item_in_manufacturing = 0
		item.stock_uom = uom_name
		item.default_warehouse = _resolve_default_warehouse()
		item.standard_rate = unit_rate_val
		item.valuation_rate = valuation_rate_val
		item.description = f"{part_name} [{norm_source} - {norm_condition}]"
		item.is_vehicle_part = 1
		item.part_source = norm_source
		item.part_condition = norm_condition
		item.oem_part_number = oem_part_number or ""
		item.revision = revision or ""
		if brand and frappe.db.exists("Brand", brand):
			item.brand = brand
		item.warranty_period = warranty_period or "12 Months / 20,000 km"
		if supplier:
			_apply_supplier_to_item_doc(item, supplier, supplier_part_no)

		try:
			item.insert()
		except frappe.DuplicateEntryError:
			was_created = False
			item = frappe.get_doc("Item", item_code)
			item.check_permission("read")
			changed = _backfill_existing_item_fields(
				item, unit_rate_val, valuation_rate_val, oem_part_number, revision, warranty_period
			)
			if supplier and _apply_supplier_to_item_doc(item, supplier, supplier_part_no):
				changed = True
			if changed:
				item.check_permission("write")
				item.save()
	else:
		changed = _backfill_existing_item_fields(
			item, unit_rate_val, valuation_rate_val, oem_part_number, revision, warranty_period
		)
		if supplier and _apply_supplier_to_item_doc(item, supplier, supplier_part_no):
			changed = True
		if changed:
			item.check_permission("write")
			item.save()

	target_slot_num = _resolve_target_slot(slot_number=slot_number, component_slot=component_slot)
	slot_label = format_slot_label(target_slot_num)
	primary_pkg = packages_to_link[0] if packages_to_link else service_package
	resolved_side = resolve_part_side(
		side=side,
		service_package=primary_pkg,
		part_name=part_name,
		part_number=part_number,
		is_symmetrical=getattr(pkg_cache.get(primary_pkg), "is_symmetrical", None)
		if (pkg_cache and primary_pkg in pkg_cache)
		else None,
	)

	if packages_to_link:
		_link_item_to_packages(
			item_code=item_code,
			package_names=packages_to_link,
			target_slot_num=target_slot_num,
			qty=item_qty,
			rate=unit_rate_val or flt(item.standard_rate),
			side=side,
			is_default=is_default,
			part_name=part_name,
			part_number=part_number,
			item_doc=item,
			pkg_cache=pkg_cache,
		)

	stock_qty = _get_item_stock_qty(item_code) if fetch_stock_qty else 0.0
	payload = {
		"item_code": item.item_code,
		"item_name": item.item_name,
		"part_number": part_number,
		"part_source": norm_source,
		"part_condition": norm_condition,
		"oem_part_number": item.oem_part_number or oem_part_number or "",
		"revision": item.revision or revision or "",
		"brand": item.brand or brand or "",
		"warranty_period": item.warranty_period or warranty_period,
		"unit_rate": flt(item.standard_rate) or unit_rate_val,
		"valuation_rate": flt(item.valuation_rate) or valuation_rate_val,
		"stock_uom": item.stock_uom,
		"stock_qty": stock_qty,
		"slot_number": target_slot_num,
		"component_slot": slot_label,
		"side": resolved_side,
	}
	if supplier:
		payload["supplier"] = supplier
	return payload, was_created


@frappe.whitelist()
def quick_create_part(
	part_number,
	part_name=None,
	part_source="OEM",
	part_condition="New",
	oem_part_number=None,
	revision=None,
	brand=None,
	warranty_period="12 Months / 20,000 km",
	unit_rate=0.0,
	valuation_rate=0.0,
	stock_uom="Nos",
	slot_number=None,
	component_slot=None,
	service_package=None,
	service_packages=None,
	side="Both",
	is_default=0,
	qty=1.0,
):
	"""Fast single part creator that ensures standard ERPNext inventory defaults,
	automotive compliance custom fields, and optional auto-linking to Service Packages.
	"""
	payload, _was_created = _upsert_part_core(
		part_number=part_number,
		part_name=part_name,
		part_source=part_source,
		part_condition=part_condition,
		oem_part_number=oem_part_number,
		revision=revision,
		brand=brand,
		warranty_period=warranty_period,
		unit_rate=unit_rate,
		valuation_rate=valuation_rate,
		stock_uom=stock_uom,
		slot_number=slot_number,
		component_slot=component_slot,
		service_package=service_package,
		service_packages=service_packages,
		side=side,
		is_default=is_default,
		qty=qty,
		fetch_stock_qty=True,
	)
	return payload


def _coerce_bulk_import_inputs(items=None, raw_text=None):
	"""Determine whether caller provided structured JSON records or legacy delimited text."""
	if isinstance(items, list):
		return items, None
	if isinstance(items, dict):
		inner = items.get("items")
		if isinstance(inner, list):
			return inner, None
		return [items], None
	if isinstance(items, str):
		stripped = items.strip()
		if stripped.startswith("[") or stripped.startswith("{"):
			parsed = parse_json_arg(stripped, label="Import Items")
			if isinstance(parsed, list):
				return parsed, None
			if isinstance(parsed, dict):
				inner = parsed.get("items")
				return (inner if isinstance(inner, list) else [parsed]), None
			frappe.throw(_("Invalid JSON items payload."), frappe.ValidationError)
		if not raw_text:
			return None, items
	elif items is not None:
		frappe.throw(_("Invalid items payload."), frappe.ValidationError)
	return None, raw_text


def _parse_raw_import_lines(raw_text, default_source, default_condition):
	"""Parse CSV, TSV, semicolon, or pipe-delimited raw catalog lines into canonical dicts."""
	for line_idx, raw_line in enumerate(raw_text.strip().splitlines(), 1):
		cleaned = raw_line.strip()
		if not cleaned:
			continue

		if "\t" in cleaned:
			parts = cleaned.split("\t")
		elif "|" in cleaned:
			parts = [p.strip() for p in cleaned.split("|")]
		elif ";" in cleaned:
			parts = [p.strip() for p in cleaned.split(";")]
		else:
			try:
				parts = next(csv.reader(io.StringIO(cleaned)))
			except Exception:
				parts = [p.strip() for p in cleaned.split(",")]

		parts = [p.strip() for p in parts]
		if line_idx == 1 and parts and parts[0].strip().lower() in _IMPORT_HEADER_TOKENS:
			continue

		part_num = parts[0] if len(parts) > 0 else ""
		if not part_num:
			continue

		yield line_idx, raw_line, {
			"part_number": part_num,
			"part_name": parts[1] if len(parts) > 1 and parts[1] else part_num,
			"part_source": parts[2] if len(parts) > 2 and parts[2] else default_source,
			"part_condition": parts[3] if len(parts) > 3 and parts[3] else default_condition,
			"unit_rate": flt(parts[4]) if len(parts) > 4 and parts[4] else 0.0,
			"valuation_rate": flt(parts[5]) if len(parts) > 5 and parts[5] else 0.0,
			"qty": flt(parts[6]) if len(parts) > 6 and parts[6] else 1.0,
			"revision": parts[7] if len(parts) > 7 and parts[7] else "",
			"oem_part_number": parts[8] if len(parts) > 8 and parts[8] else "",
		}


def _first_present_flt(record, keys, default=0.0):
	for key in keys:
		val = record.get(key)
		if val is not None and val != "":
			parsed = optional_flt(val, key, non_negative=False, default=default)
			return default if parsed is None else parsed
	return default


def _import_part_record(
	record,
	service_package=None,
	service_packages=None,
	slot_number=None,
	component_slot=None,
	default_source="OEM",
	default_condition="New",
	side="Both",
	is_first=False,
	supplier=None,
	*,
	ensured_suppliers: Optional[set[str]] = None,
	pkg_cache: Optional[dict[str, Any]] = None,
	return_created_flag: bool = False,
):
	part_num = scalar_str(
		record.get("part_number")
		or record.get("part_no")
		or record.get("sku")
		or record.get("code"),
		"Part Number",
	)
	if not part_num:
		frappe.throw(_("Part Number is required."), frappe.MandatoryError)

	part_name = (
		scalar_str(
			record.get("part_name")
			or record.get("item_name")
			or record.get("description")
			or record.get("name"),
			"Part Name",
		)
		or part_num
	)
	source = scalar_str(record.get("part_source") or record.get("source"), "Part Source") or default_source
	condition = (
		scalar_str(record.get("part_condition") or record.get("condition"), "Part Condition")
		or default_condition
	)

	unit_rate = _first_present_flt(record, ("unit_rate", "rate", "standard_rate", "price"), 0.0)
	valuation_rate = _first_present_flt(record, ("valuation_rate", "cost", "cost_price"), 0.0)
	qty = _coerce_item_qty(_first_present_flt(record, ("qty", "quantity"), 1.0))

	revision = scalar_str(record.get("revision") or record.get("rev"), "Revision") or ""
	oem_num = scalar_str(
		record.get("oem_part_number") or record.get("oem_part_no") or record.get("oem"),
		"OEM Part Number",
	) or (part_num if source.upper().startswith("OEM") else "")

	row_slot_num = record.get("slot_number") if record.get("slot_number") is not None else slot_number
	row_comp_slot = record.get("component_slot") or component_slot
	row_side = record.get("side") or side
	row_is_default = cint(record["is_default"]) if record.get("is_default") is not None else (1 if is_first else 0)
	row_supplier = scalar_str(record.get("supplier") or supplier, "Supplier") or ""
	supplier_part_no = (
		scalar_str(record.get("supplier_part_no") or record.get("supplier_sku"), "Supplier Part No") or part_num
	)

	res, was_created = _upsert_part_core(
		part_number=part_num,
		part_name=part_name,
		part_source=source,
		part_condition=condition,
		oem_part_number=oem_num,
		revision=revision,
		brand=scalar_str(record.get("brand"), "Brand"),
		warranty_period=scalar_str(record.get("warranty_period") or record.get("warranty"), "Warranty Period")
		or "12 Months / 20,000 km",
		unit_rate=unit_rate,
		valuation_rate=valuation_rate,
		stock_uom=scalar_str(record.get("stock_uom") or record.get("uom"), "Stock UOM") or "Nos",
		slot_number=row_slot_num,
		component_slot=row_comp_slot,
		service_package=service_package,
		service_packages=service_packages,
		side=row_side,
		is_default=row_is_default,
		qty=qty,
		supplier=row_supplier or None,
		supplier_part_no=supplier_part_no,
		ensured_suppliers=ensured_suppliers,
		pkg_cache=pkg_cache,
		fetch_stock_qty=False,
	)
	res["qty"] = qty
	if return_created_flag:
		return res, was_created
	return res


def _process_structured_import_row(
	idx,
	record,
	created_items,
	errors,
	service_package,
	service_packages,
	slot_number,
	component_slot,
	default_source,
	default_condition,
	side,
	supplier,
	*,
	updated_items: Optional[list[Any]] = None,
	ensured_suppliers: Optional[set[str]] = None,
	pkg_cache: Optional[dict[str, Any]] = None,
):
	if not isinstance(record, dict):
		errors.append({"index": idx, "item": record, "error": _("Each import item must be a JSON object.")})
		return
	try:
		is_first = (len(created_items) + len(updated_items or [])) == 0
		res, was_created = _import_part_record(
			record=record,
			service_package=service_package,
			service_packages=service_packages,
			slot_number=slot_number,
			component_slot=component_slot,
			default_source=default_source,
			default_condition=default_condition,
			side=side,
			is_first=is_first,
			supplier=supplier,
			ensured_suppliers=ensured_suppliers,
			pkg_cache=pkg_cache,
			return_created_flag=True,
		)
		if was_created or updated_items is None:
			created_items.append(res)
		else:
			updated_items.append(res)
	except Exception as exc:
		errors.append({"index": idx, "item": record, "error": str(exc)})


@frappe.whitelist()
def bulk_import_parts(
	items=None,
	raw_text=None,
	service_package=None,
	service_packages=None,
	slot_number=None,
	component_slot=None,
	default_source="OEM",
	default_condition="New",
	side="Both",
	supplier=None,
):
	"""Batch-create/update parts from structured JSON items (primary) or delimited text.

	Structured `items` accepts a list[dict], single dict, or JSON string.
	Legacy delimited `raw_text` (or positional string passed as `items`) parses:
	Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#
	"""
	frappe.has_permission("Item", "create", throw=True)
	service_package = optional_str(service_package, "Service Package")
	component_slot = optional_str(component_slot, "Component Slot")
	default_source = optional_str(default_source, "Default Source") or "OEM"
	default_condition = optional_str(default_condition, "Default Condition") or "New"
	side = optional_str(side, "Side") or "Both"
	supplier = optional_str(supplier, "Supplier")
	if raw_text is not None and not isinstance(raw_text, str):
		frappe.throw(_("Import raw text must be a string."), frappe.ValidationError)

	package_names = _parse_service_package_names(service_packages, primary=service_package)
	pkg_cache: dict[str, Any] = {}
	if package_names:
		frappe.has_permission("Service Package", "write", throw=True)
		for pkg_name in package_names:
			if frappe.db.exists("Service Package", pkg_name):
				pkg = frappe.get_doc("Service Package", pkg_name)
				pkg.check_permission("write")
				pkg_cache[pkg_name] = pkg

	ensured_suppliers: set[str] = set()
	if supplier:
		sup_ptype = "create" if not frappe.db.exists("Supplier", supplier) else "read"
		frappe.has_permission("Supplier", sup_ptype, throw=True)
		frappe.has_permission("Item", "write", throw=True)
		_ensure_supplier_exists(supplier)
		ensured_suppliers.add(supplier)

	structured_records, legacy_text = _coerce_bulk_import_inputs(items, raw_text)

	created_items = []
	updated_items = []
	errors = []

	if structured_records is not None:
		if not structured_records:
			frappe.throw(_("No import items or text provided."), frappe.ValidationError)
		for idx, record in enumerate(structured_records, 1):
			_process_structured_import_row(
				idx=idx,
				record=record,
				created_items=created_items,
				errors=errors,
				service_package=service_package,
				service_packages=service_packages,
				slot_number=slot_number,
				component_slot=component_slot,
				default_source=default_source,
				default_condition=default_condition,
				side=side,
				supplier=supplier,
				updated_items=updated_items,
				ensured_suppliers=ensured_suppliers,
				pkg_cache=pkg_cache,
			)
	else:
		if not legacy_text or not legacy_text.strip():
			frappe.throw(_("No import items or text provided."), frappe.ValidationError)
		for line_idx, raw_line, record in _parse_raw_import_lines(legacy_text, default_source, default_condition):
			try:
				res, was_created = _import_part_record(
					record=record,
					service_package=service_package,
					service_packages=service_packages,
					slot_number=slot_number,
					component_slot=component_slot,
					default_source=default_source,
					default_condition=default_condition,
					side=side,
					is_first=((len(created_items) + len(updated_items)) == 0),
					supplier=supplier,
					ensured_suppliers=ensured_suppliers,
					pkg_cache=pkg_cache,
					return_created_flag=True,
				)
				if was_created:
					created_items.append(res)
				else:
					updated_items.append(res)
			except Exception as exc:
				errors.append({"line": line_idx, "text": raw_line, "error": str(exc)})

	for pkg in pkg_cache.values():
		pkg.save()

	all_codes = [entry["item_code"] for entry in (created_items + updated_items) if entry.get("item_code")]
	if all_codes:
		stock_map = _get_items_stock_qty_map(all_codes)
		for entry in (created_items + updated_items):
			code = entry.get("item_code")
			if code in stock_map:
				entry["stock_qty"] = flt(stock_map[code])

	return {
		"created": created_items,
		"updated": updated_items,
		"errors": errors,
		"total_created": len(created_items),
		"total_updated": len(updated_items),
	}


@frappe.whitelist()
def get_component_alternates(
	service_package=None,
	component_slot=None,
	slot_number=None,
	oem_part_number=None,
	item_code=None,
):
	"""Return all compatible items for a component slot, including condition variants and redesigns.
	Also includes unassigned compatible parts from the Service Package and batches stock/item reads.
	"""
	frappe.has_permission("Item", "read", throw=True)
	service_package = optional_str(service_package, "Service Package")
	component_slot = optional_str(component_slot, "Component Slot")
	oem_part_number = optional_str(oem_part_number, "OEM Part Number")
	item_code = optional_str(item_code, "Item Code")

	items = {}

	target_slot_num = None
	if slot_number is not None and str(slot_number).strip() != "":
		if isinstance(slot_number, (dict, list, tuple, set, bool)):
			frappe.throw(_("Slot number must be a scalar value."), frappe.ValidationError)
		target_slot_num = cint(slot_number)
	elif component_slot:
		target_slot_num = parse_slot_number(component_slot)

	target_slot_label = format_slot_label(target_slot_num) if target_slot_num else component_slot

	if service_package:
		frappe.has_permission("Service Package", "read", throw=True)
		if frappe.db.exists("Service Package", service_package):
			pkg = frappe.get_doc("Service Package", service_package)
			pkg.check_permission("read")
			for row in (pkg.items or []):
				if not row.item_code:
					continue
				row_snum = cint(getattr(row, "slot_number", None))
				if row_snum == 0 and getattr(row, "component_slot", None):
					row_snum = parse_slot_number(row.component_slot)

				is_in_slot = (target_slot_num is not None and row_snum == target_slot_num) or (
					component_slot and row.component_slot == component_slot
				)
				is_unassigned = row_snum == 0 or row.component_slot == "Unassigned"

				if is_in_slot or is_unassigned or not component_slot:
					items[row.item_code] = {
						"item_code": row.item_code,
						"item_name": row.item_name or row.item_code,
						"slot_number": row_snum,
						"component_slot": format_slot_label(row_snum),
						"is_unassigned": 1 if is_unassigned and not is_in_slot else 0,
						"is_default": cint(getattr(row, "is_default", 0)),
						"rate": flt(row.rate) if getattr(row, "rate", None) is not None else None,
						"qty": flt(row.qty) or 1.0,
						"side": getattr(row, "side", "Both") or "Both",
					}

	if not oem_part_number and item_code and frappe.db.exists("Item", item_code):
		item_ref = frappe.get_cached_doc("Item", item_code)
		item_ref.check_permission("read")
		oem_part_number = item_ref.oem_part_number

	if oem_part_number:
		matched_items = frappe.get_list(
			"Item",
			filters={"oem_part_number": oem_part_number, "disabled": 0},
			fields=["name", "item_name", "standard_rate"],
		)
		for it in matched_items:
			if it.name not in items:
				items[it.name] = {
					"item_code": it.name,
					"item_name": it.item_name,
					"slot_number": target_slot_num or 0,
					"component_slot": target_slot_label or "",
					"is_unassigned": 0,
					"is_default": 0,
					"rate": flt(it.standard_rate),
					"qty": 1.0,
					"side": "Both",
					"is_catalog": 1,
				}

	if not items:
		return []

	item_codes = list(items.keys())
	item_meta_rows = frappe.get_list(
		"Item",
		filters={"name": ["in", item_codes]},
		fields=[
			"name",
			"item_name",
			"part_source",
			"part_condition",
			"oem_part_number",
			"revision",
			"brand",
			"standard_rate",
			"warranty_period",
			"stock_uom",
		],
	)
	item_meta_map = {row.name: row for row in item_meta_rows}
	stock_map = _get_items_stock_qty_map(item_codes)

	results = []
	for ic, data in items.items():
		it_doc = item_meta_map.get(ic)
		if it_doc:
			data["item_name"] = it_doc.item_name or data.get("item_name")
			data["part_source"] = it_doc.part_source or "OEM"
			data["part_condition"] = it_doc.part_condition or "New"
			data["oem_part_number"] = it_doc.oem_part_number or oem_part_number or ""
			data["revision"] = it_doc.revision or ""
			data["brand"] = it_doc.brand or ""
			data["warranty_period"] = it_doc.warranty_period or "12 Months / 20,000 km"
			data["stock_uom"] = it_doc.stock_uom or "Nos"
			if data.get("rate") is None:
				data["rate"] = flt(it_doc.standard_rate)

		data["stock_qty"] = flt(stock_map.get(ic, 0.0))
		results.append(data)

	def sort_key(x):
		unassigned_rank = 1 if x.get("is_unassigned") else 0
		default_rank = 0 if x.get("is_default") else 1
		source_rank = 0 if x.get("part_source") == "OEM" else 1
		cond_rank = (
			0 if x.get("part_condition") == "New" else (1 if x.get("part_condition") == "Reconditioned" else 2)
		)
		return (unassigned_rank, default_rank, source_rank, cond_rank, x.get("item_code"))

	results.sort(key=sort_key)
	return results


@frappe.whitelist()
def link_part_to_service_packages(
	item_code,
	service_packages=None,
	slot_number=None,
	component_slot=None,
	qty=1.0,
	rate=None,
	side="Both",
	is_default=0,
):
	"""Link an item to multiple Service Packages as a compatible part.
	The primary package receives slot_number (default 1); secondary packages get Unassigned (0).
	"""
	item_code = require_str(item_code, "Item Code")
	component_slot = optional_str(component_slot, "Component Slot")
	side = optional_str(side, "Side") or "Both"
	qty_val = _coerce_item_qty(qty)
	rate_val = optional_flt(rate, "Rate", non_negative=True, default=None)

	frappe.has_permission("Item", "read", throw=True)
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item '{0}' not found.").format(item_code), frappe.DoesNotExistError)
	item_doc = frappe.get_cached_doc("Item", item_code)
	item_doc.check_permission("read")
	frappe.has_permission("Service Package", "write", throw=True)

	package_names = _parse_service_package_names(service_packages)
	target_slot_num = _resolve_target_slot(slot_number=slot_number, component_slot=component_slot)
	pkg_cache: dict[str, Any] = {}
	linked_packages = _link_item_to_packages(
		item_code=item_code,
		package_names=package_names,
		target_slot_num=target_slot_num,
		qty=qty_val,
		rate=rate_val,
		side=side,
		is_default=is_default,
		item_doc=item_doc,
		pkg_cache=pkg_cache,
	)
	for pkg in pkg_cache.values():
		pkg.save()

	return {
		"status": "success",
		"linked_packages": linked_packages,
		"linked_count": len(linked_packages),
	}


@frappe.whitelist()
def search_service_packages(
	query="",
	limit=25,
	applicable_model=None,
	applicable_generation=None,
	vehicle_year=None,
	vehicle=None,
):
	"""Search Service Packages by title, correction code, model, generation, or vehicle."""
	from ism.scheduling.bookable_catalog import is_package_eligible_for_vehicle
	from ism.utils.tesla_manual import parse_tesla_manual_url

	frappe.has_permission("Service Package", "read", throw=True)
	query = optional_str(query, "Search Query") or ""
	model_filter = optional_str(applicable_model, "Applicable Model")
	gen_filter = optional_str(applicable_generation, "Applicable Generation")
	year_filter = optional_int(vehicle_year, "Vehicle Year", min_val=2008, max_val=2100) if vehicle_year not in (None, "") else None
	vehicle_name = optional_str(vehicle, "Repair Vehicle")
	resolved_limit = optional_int(limit, "Limit", min_val=1, max_val=100, default=25)

	veh_month = None
	if vehicle_name and frappe.db.exists("Repair Vehicle", vehicle_name):
		frappe.has_permission("Repair Vehicle", "read", doc=vehicle_name, throw=True)
		v_doc = frappe.get_cached_doc("Repair Vehicle", vehicle_name)
		model_filter = model_filter or getattr(v_doc, "model", None)
		year_filter = year_filter or (cint(getattr(v_doc, "model_year", 0)) or None)
		gen_filter = gen_filter or getattr(v_doc, "vehicle_generation", None)
		veh_month = getattr(v_doc, "manufactured_month", None)

	or_filters = None
	if query:
		escaped = escape_like(query)
		or_filters = [
			["package_name", "like", f"%{escaped}%"],
			["correction_code", "like", f"%{escaped}%"],
			["applicable_model", "like", f"%{escaped}%"],
			["applicable_generation", "like", f"%{escaped}%"],
		]

	has_compat_filter = bool(model_filter or gen_filter or year_filter)
	fetch_limit = max(resolved_limit * 4, 100) if has_compat_filter else resolved_limit

	packages = frappe.get_list(
		"Service Package",
		fields=[
			"name",
			"package_name",
			"applicable_model",
			"applicable_generation",
			"applicable_year_from",
			"applicable_year_to",
			"correction_code",
			"standard_labor_hours",
			"hourly_rate",
			"service_rate",
			"is_symmetrical",
			"is_flat_rate",
			"tesla_manual_url",
		],
		or_filters=or_filters,
		limit=fetch_limit,
		order_by="modified desc",
	)

	pkg_names = [pkg["name"] for pkg in packages]
	compat_by_parent = {}
	if pkg_names:
		compat_rows = frappe.get_all(
			"Service Package Generation",
			filters={"parenttype": "Service Package", "parent": ["in", pkg_names]},
			fields=[
				"parent",
				"applicable_model",
				"generation_name",
				"date_range",
				"year_from",
				"month_from",
				"year_to",
				"month_to",
				"manual_base_url",
			],
		)
		for crow in compat_rows:
			compat_by_parent.setdefault(crow["parent"], []).append(crow)

	results = []
	for pkg in packages:
		pkg["generation_compatibility"] = compat_by_parent.get(pkg["name"], [])
		if (not pkg.get("applicable_model") or not pkg.get("applicable_generation")) and pkg.get("tesla_manual_url"):
			url_meta = parse_tesla_manual_url(pkg["tesla_manual_url"])
			if not pkg.get("applicable_model"):
				pkg["applicable_model"] = url_meta.get("applicable_model") or ""
			if not pkg.get("applicable_generation"):
				pkg["applicable_generation"] = (
					url_meta.get("applicable_generation") or url_meta.get("matched_generation") or ""
				)
		if has_compat_filter and not is_package_eligible_for_vehicle(
			pkg,
			vehicle_model=model_filter,
			vehicle_year=year_filter,
			vehicle_generation=gen_filter,
			manufactured_month=veh_month,
		):
			continue
		results.append(pkg)
		if len(results) >= resolved_limit:
			break

	return results


@frappe.whitelist()
def quick_create_service_package(
	package_name,
	correction_code=None,
	applicable_model=None,
	applicable_generation=None,
	standard_labor_hours=1.0,
	hourly_rate=120.0,
	is_flat_rate=0,
):
	"""Fast creation of a Service Package directly from parts or service tooling."""
	pkg_name = require_str(package_name, "Service Package Name")
	correction_code = optional_str(correction_code, "Correction Code")
	applicable_model = optional_str(applicable_model, "Applicable Model")
	applicable_generation = optional_str(applicable_generation, "Applicable Generation")
	flat_rate_flag = 1 if coerce_bool(is_flat_rate) else 0
	hours_val = optional_flt(standard_labor_hours, "Standard Labor Hours", non_negative=True, default=1.0)
	rate_val = optional_flt(hourly_rate, "Hourly Rate", non_negative=True, default=120.0)
	hours = 1.0 if not hours_val else hours_val
	hr_rate = 120.0 if not rate_val else rate_val

	target_model = (applicable_model or "").strip()
	target_gen = (applicable_generation or "").strip()

	def _find_scoped_package():
		matches = frappe.db.get_all(
			"Service Package",
			filters={"package_name": pkg_name},
			fields=["name", "applicable_model", "applicable_generation"],
		)
		for row in matches:
			if (row.applicable_model or "").strip() == target_model and (row.applicable_generation or "").strip() == target_gen:
				return row["name"]
		return None

	existing_name = _find_scoped_package()
	if existing_name:
		pkg = frappe.get_doc("Service Package", existing_name)
		pkg.check_permission("read")
		dirty = False
		if target_model and not pkg.applicable_model:
			pkg.applicable_model = target_model
			dirty = True
		if target_gen and not pkg.applicable_generation:
			pkg.applicable_generation = target_gen
			dirty = True
		if dirty:
			pkg.check_permission("write")
			pkg.save()
		return pkg.as_dict()

	frappe.has_permission("Service Package", "create", throw=True)
	pkg = frappe.get_doc(
		{
			"doctype": "Service Package",
			"package_name": pkg_name,
			"applicable_model": target_model,
			"applicable_generation": target_gen,
			"correction_code": correction_code or None,
			"standard_labor_hours": hours,
			"hourly_rate": hr_rate,
			"service_rate": flt(hr_rate if flat_rate_flag else hours * hr_rate, 2),
			"is_flat_rate": flat_rate_flag,
		}
	)
	try:
		pkg.insert()
	except frappe.DuplicateEntryError:
		fallback_name = _find_scoped_package() or pkg_name
		pkg = frappe.get_doc("Service Package", fallback_name)
		pkg.check_permission("read")
	return pkg.as_dict()
