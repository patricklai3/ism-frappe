# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import csv
import io
import json
import re
import frappe
from frappe import _
from frappe.utils import cint, flt, today

LHRH_PATTERN = re.compile(
	r"\b(LH|RH)\b|\bLeft\s+Hand\b(?!\s*Drive)|\bRight\s+Hand\b(?!\s*Drive)",
	re.IGNORECASE,
)
LH_ONLY_PATTERN = re.compile(r"\bLH\b|\bLeft\s+Hand\b(?!\s*Drive)", re.IGNORECASE)
RH_ONLY_PATTERN = re.compile(r"\bRH\b|\bRight\s+Hand\b(?!\s*Drive)", re.IGNORECASE)


def format_item_code(part_source="OEM", part_number="", part_condition="New"):
	"""Generate strictly standardized Item Code: [Source]-[PartNumber]-[Condition].
	e.g. OEM-1044321-00-J-NEW, OEM-1044321-00-J-USED, AFT-RK623000-NEW
	"""
	src = "OEM" if str(part_source).upper().startswith("OEM") else "AFT"

	cond_str = str(part_condition).upper().strip()
	if "USE" in cond_str:
		cond = "USED"
	elif any(tok in cond_str for tok in ("REC", "RECON", "REB", "RCN", "REM")):
		cond = "RCN"
	else:
		cond = "NEW"

	# Sanitize part number: keep alphanumerics, hyphens, and underscores
	clean_part = re.sub(r"[^A-Za-z0-9_\-]", "", str(part_number).strip().upper())
	if not clean_part:
		clean_part = "UNKNOWN"

	return f"{src}-{clean_part}-{cond}"


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
	part_number = _require_str(part_number, "Part Number")
	part_name = _optional_str(part_name, "Part Name")
	part_source_str = _optional_str(part_source, "Part Source") or "OEM"
	part_condition_str = _optional_str(part_condition, "Part Condition") or "New"
	oem_part_number = _optional_str(oem_part_number, "OEM Part Number")
	revision = _optional_str(revision, "Revision")
	brand = _optional_str(brand, "Brand")
	warranty_period = _optional_str(warranty_period, "Warranty Period") or "12 Months / 20,000 km"
	stock_uom = _optional_str(stock_uom, "Stock UOM")
	component_slot = _optional_str(component_slot, "Component Slot")
	service_package = _optional_str(service_package, "Service Package")
	side = _optional_str(side, "Side") or "Both"

	part_source = _normalize_part_source(part_source_str)
	part_condition = _normalize_part_condition(part_condition_str)
	item_code = format_item_code(part_source, part_number, part_condition)

	if not oem_part_number and part_source == "OEM":
		oem_part_number = part_number

	if not part_name:
		part_name = f"{part_number} ({part_source} {part_condition})"

	unit_rate = flt(unit_rate)
	valuation_rate = flt(valuation_rate)
	item_qty = _coerce_item_qty(qty)

	packages_to_link = _parse_service_package_names(service_packages, primary=service_package)
	item_exists = bool(frappe.db.exists("Item", item_code))
	if item_exists:
		item = frappe.get_doc("Item", item_code)
		item.check_permission("read")
	else:
		frappe.has_permission("Item", "create", throw=True)
	if packages_to_link:
		frappe.has_permission("Service Package", "write", throw=True)

	item_group = "Parts" if frappe.db.exists("Item Group", "Parts") else (
		frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
	)
	uom_name = stock_uom or "Nos"
	if not frappe.db.exists("UOM", uom_name):
		uom_name = "Nos" if frappe.db.exists("UOM", "Nos") else "Unit"

	_ensure_item_custom_fields()

	if not item_exists:
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
		item.standard_rate = unit_rate
		item.valuation_rate = valuation_rate
		item.description = f"{part_name} [{part_source} - {part_condition}]"
		item.is_vehicle_part = 1
		item.part_source = part_source
		item.part_condition = part_condition
		item.oem_part_number = oem_part_number or ""
		item.revision = revision or ""
		if brand and frappe.db.exists("Brand", brand):
			item.brand = brand
		item.warranty_period = warranty_period or "12 Months / 20,000 km"
		item.insert()
	else:
		if _backfill_existing_item_fields(item, unit_rate, valuation_rate, oem_part_number, revision, warranty_period):
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
	)

	if packages_to_link:
		_link_item_to_packages(
			item_code=item_code,
			package_names=packages_to_link,
			target_slot_num=target_slot_num,
			qty=item_qty,
			rate=unit_rate or flt(item.standard_rate),
			side=side,
			is_default=is_default,
			part_name=part_name,
			part_number=part_number,
		)

	stock_qty = _get_item_stock_qty(item_code)

	return {
		"item_code": item.item_code,
		"item_name": item.item_name,
		"part_number": part_number,
		"part_source": part_source,
		"part_condition": part_condition,
		"oem_part_number": item.oem_part_number or oem_part_number or "",
		"revision": item.revision or revision or "",
		"brand": item.brand or brand or "",
		"warranty_period": item.warranty_period or warranty_period,
		"unit_rate": flt(item.standard_rate) or unit_rate,
		"valuation_rate": flt(item.valuation_rate) or valuation_rate,
		"stock_uom": item.stock_uom,
		"stock_qty": stock_qty,
		"slot_number": target_slot_num,
		"component_slot": slot_label,
		"side": resolved_side,
	}


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
	service_package = _optional_str(service_package, "Service Package")
	component_slot = _optional_str(component_slot, "Component Slot")
	default_source = _optional_str(default_source, "Default Source") or "OEM"
	default_condition = _optional_str(default_condition, "Default Condition") or "New"
	side = _optional_str(side, "Side") or "Both"
	supplier = _optional_str(supplier, "Supplier")
	if raw_text is not None and not isinstance(raw_text, str):
		frappe.throw(_("Import raw text must be a string."))

	if _parse_service_package_names(service_packages, primary=service_package):
		frappe.has_permission("Service Package", "write", throw=True)
	if supplier:
		sup_ptype = "create" if not frappe.db.exists("Supplier", supplier) else "read"
		frappe.has_permission("Supplier", sup_ptype, throw=True)
		frappe.has_permission("Item", "write", throw=True)

	structured_records, legacy_text = _coerce_bulk_import_inputs(items, raw_text)

	created_items = []
	errors = []

	if structured_records is not None:
		if not structured_records:
			frappe.throw(_("No import items or text provided."))
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
			)
	else:
		if not legacy_text or not legacy_text.strip():
			frappe.throw(_("No import items or text provided."))
		for line_idx, raw_line, record in _parse_raw_import_lines(legacy_text, default_source, default_condition):
			try:
				res = _import_part_record(
					record=record,
					service_package=service_package,
					service_packages=service_packages,
					slot_number=slot_number,
					component_slot=component_slot,
					default_source=default_source,
					default_condition=default_condition,
					side=side,
					is_first=(len(created_items) == 0),
					supplier=supplier,
				)
				created_items.append(res)
			except Exception as exc:
				errors.append({"line": line_idx, "text": raw_line, "error": str(exc)})

	return {
		"created": created_items,
		"errors": errors,
		"total_created": len(created_items),
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
	service_package = _optional_str(service_package, "Service Package")
	component_slot = _optional_str(component_slot, "Component Slot")
	oem_part_number = _optional_str(oem_part_number, "OEM Part Number")
	item_code = _optional_str(item_code, "Item Code")

	items = {}

	target_slot_num = None
	if slot_number is not None and str(slot_number).strip() != "":
		if isinstance(slot_number, (dict, list, tuple, set)):
			frappe.throw(_("Slot number must be a scalar value."))
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
						"rate": flt(row.rate),
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
			data["rate"] = data.get("rate") or flt(it_doc.standard_rate)

		data["stock_qty"] = flt(stock_map.get(ic, 0.0))
		results.append(data)

	def sort_key(x):
		unassigned_rank = 1 if x.get("is_unassigned") else 0
		default_rank = 0 if x.get("is_default") else 1
		source_rank = 0 if x.get("part_source") == "OEM" else 1
		cond_rank = 0 if x.get("part_condition") == "New" else (1 if x.get("part_condition") == "Reconditioned" else 2)
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
	rate=0.0,
	side="Both",
	is_default=0,
):
	"""Link an item to multiple Service Packages as a compatible part.
	The primary package receives slot_number (default 1); secondary packages get Unassigned (0).
	"""
	item_code = _require_str(item_code, "Item Code")
	component_slot = _optional_str(component_slot, "Component Slot")
	side = _optional_str(side, "Side") or "Both"
	frappe.has_permission("Item", "read", throw=True)
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item '{0}' not found.").format(item_code))
	frappe.get_cached_doc("Item", item_code).check_permission("read")
	frappe.has_permission("Service Package", "write", throw=True)

	package_names = _parse_service_package_names(service_packages)
	target_slot_num = _resolve_target_slot(slot_number=slot_number, component_slot=component_slot)
	linked_packages = _link_item_to_packages(
		item_code=item_code,
		package_names=package_names,
		target_slot_num=target_slot_num,
		qty=qty,
		rate=rate,
		side=side,
		is_default=is_default,
	)

	return {
		"status": "success",
		"linked_packages": linked_packages,
		"linked_count": len(linked_packages),
	}


@frappe.whitelist()
def search_service_packages(query="", limit=25):
	"""Search for Service Packages by title, correction code, model, or category."""
	frappe.has_permission("Service Package", "read", throw=True)
	query = _optional_str(query, "Search Query") or ""
	or_filters = None
	if query:
		or_filters = [
			["package_name", "like", f"%{query}%"],
			["correction_code", "like", f"%{query}%"],
			["applicable_model", "like", f"%{query}%"],
		]

	packages = frappe.get_list(
		"Service Package",
		fields=[
			"name",
			"package_name",
			"applicable_model",
			"applicable_generation",
			"correction_code",
			"standard_labor_hours",
			"hourly_rate",
			"service_rate",
			"is_symmetrical",
			"is_flat_rate",
			"tesla_manual_url",
		],
		or_filters=or_filters,
		limit=cint(limit) or 25,
		order_by="modified desc",
	)

	from ism.utils.tesla_manual import parse_tesla_manual_url

	for pkg in packages:
		if not pkg.get("applicable_model") and pkg.get("tesla_manual_url"):
			url_meta = parse_tesla_manual_url(pkg["tesla_manual_url"])
			pkg["applicable_model"] = url_meta.get("applicable_model") or ""
			if not pkg.get("applicable_generation"):
				pkg["applicable_generation"] = url_meta.get("matched_generation") or ""

	return packages


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
	pkg_name = _require_str(package_name, "Service Package Name")
	correction_code = _optional_str(correction_code, "Correction Code")
	applicable_model = _optional_str(applicable_model, "Applicable Model")
	applicable_generation = _optional_str(applicable_generation, "Applicable Generation")

	if frappe.db.exists("Service Package", pkg_name):
		pkg = frappe.get_doc("Service Package", pkg_name)
		pkg.check_permission("read")
		if applicable_model and not pkg.applicable_model:
			pkg.check_permission("write")
			pkg.applicable_model = applicable_model
			pkg.save()
		return pkg.as_dict()

	frappe.has_permission("Service Package", "create", throw=True)
	hours = flt(standard_labor_hours) or 1.0
	hr_rate = flt(hourly_rate) or 120.0
	pkg = frappe.get_doc({
		"doctype": "Service Package",
		"package_name": pkg_name,
		"applicable_model": applicable_model or "",
		"applicable_generation": applicable_generation or "",
		"correction_code": correction_code or None,
		"standard_labor_hours": hours,
		"hourly_rate": hr_rate,
		"service_rate": flt(hours * hr_rate, 2),
		"is_flat_rate": cint(is_flat_rate),
	})
	pkg.insert()
	return pkg.as_dict()


@frappe.whitelist()
def ensure_supplier_and_link_to_item(supplier_name, item_code=None, supplier_part_no=None):
	"""Ensure a Supplier exists, and link it to Item's supplier_items child table and default_supplier."""
	s_name = _optional_str(supplier_name, "Supplier")
	if not s_name:
		return None
	item_code = _optional_str(item_code, "Item Code")
	supplier_part_no = _optional_str(supplier_part_no, "Supplier Part No")

	# Validate Item permissions before creating Supplier so failed Item writes never leave orphan suppliers.
	item_doc = None
	changed = False
	if item_code and frappe.db.exists("Item", item_code):
		item_doc = frappe.get_doc("Item", item_code)
		item_doc.check_permission("read")
		existing_suppliers = [getattr(row, "supplier", None) for row in getattr(item_doc, "supplier_items", [])]
		if s_name not in existing_suppliers:
			item_doc.append("supplier_items", {
				"supplier": s_name,
				"supplier_part_no": supplier_part_no or "",
			})
			changed = True

		if hasattr(item_doc, "item_defaults"):
			for d in item_doc.item_defaults:
				if not getattr(d, "default_supplier", None):
					d.default_supplier = s_name
					changed = True

		if changed:
			item_doc.check_permission("write")

	if not frappe.db.exists("Supplier", s_name):
		frappe.has_permission("Supplier", "create", throw=True)
		sup_group = frappe.db.get_single_value("Buying Settings", "supplier_group") or "All Supplier Groups"
		if not frappe.db.exists("Supplier Group", sup_group):
			first_group = frappe.db.get_value("Supplier Group", {}, "name")
			sup_group = first_group or "All Supplier Groups"

		sup = frappe.new_doc("Supplier")
		sup.supplier_name = s_name
		sup.supplier_group = sup_group
		sup.insert()
	else:
		frappe.has_permission("Supplier", "read", doc=s_name, throw=True)

	if item_doc and changed:
		item_doc.save()

	return s_name


@frappe.whitelist()
def get_repair_order_parts_stock(ro_name=None, items=None, company=None):
	"""Whitelisted API to fetch live FIFO stock and commitment data for repair order parts."""
	frappe.has_permission("Item", "read", throw=True)
	frappe.has_permission("Repair Order", "read", throw=True)
	ro_name = _optional_str(ro_name, "Repair Order")
	company = _optional_str(company, "Company")

	target_items = []
	if items:
		if isinstance(items, str):
			try:
				items = json.loads(items)
			except Exception:
				items = [s.strip() for s in items.split(",") if s.strip()]
		if isinstance(items, (list, tuple, set)):
			for entry in items:
				if isinstance(entry, (dict, list, tuple, set)):
					frappe.throw(_("Invalid items list entry."))
				val = str(entry or "").strip()
				if val and val not in target_items:
					target_items.append(val)
		elif items is not None:
			frappe.throw(_("Invalid items parameter."))

	ro = None
	if ro_name and frappe.db.exists("Repair Order", ro_name):
		ro = frappe.get_doc("Repair Order", ro_name)
		ro.check_permission("read")

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
	item_ro_qty = {code: {} for code in codes}

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


@frappe.whitelist()
def get_item_procurement_info(item_code, company=None, ro_name=None):
	"""Query stock levels, known suppliers from Item Supplier child table, and default supplier for an Item."""
	frappe.has_permission("Item", "read", throw=True)
	item_code = _optional_str(item_code, "Item Code")
	company = _optional_str(company, "Company")
	ro_name = _optional_str(ro_name, "Repair Order")
	if ro_name:
		frappe.has_permission("Repair Order", "read", throw=True)
		if frappe.db.exists("Repair Order", ro_name):
			frappe.has_permission("Repair Order", "read", doc=ro_name, throw=True)

	if not item_code or not frappe.db.exists("Item", item_code):
		return {}

	item_doc = frappe.get_cached_doc("Item", item_code)
	item_doc.check_permission("read")
	wh = _resolve_default_warehouse(company)
	avail_info = get_item_available_stock(item_code, wh, ro_name)
	sup_info = _get_items_supplier_map([item_code])[item_code]

	return {
		"item_code": item_code,
		"item_name": item_doc.item_name,
		"stock_qty": flt(avail_info["available_qty"]),
		"actual_qty": flt(avail_info["actual_qty"]),
		"committed_qty": flt(avail_info["committed_qty"]),
		"warehouse": wh,
		"default_supplier": sup_info["default_supplier"],
		"known_suppliers": sup_info["known_suppliers"],
		"rate": flt(getattr(item_doc, "standard_rate", 0.0) or getattr(item_doc, "valuation_rate", 0.0)),
		"uom": getattr(item_doc, "stock_uom", "Nos"),
	}


@frappe.whitelist()
def get_repair_order_procurement_data(docname):
	"""Gather all required parts from approved services of a Repair Order with current stock & supplier details."""
	frappe.has_permission("Repair Order", "read", throw=True)
	frappe.has_permission("Item", "read", throw=True)
	docname = _optional_str(docname, "Repair Order")
	if not docname or not frappe.db.exists("Repair Order", docname):
		frappe.throw(_("Repair Order {0} not found").format(docname))

	ro = frappe.get_doc("Repair Order", docname)
	ro.check_permission("read")
	approved_services = [s for s in (ro.services or []) if s.status == "Approved"]
	if not approved_services:
		return {
			"repair_order": docname,
			"company": ro.company,
			"parts": [],
			"message": _("No approved services found on this Repair Order."),
		}

	raw_entries = []
	candidate_codes = []
	seen_codes = set()
	for svc in approved_services:
		for p in _parse_parts_json(svc.parts):
			if not isinstance(p, dict):
				continue
			code = p.get("item_code")
			if not code or not isinstance(code, str):
				continue
			raw_entries.append((code, p))
			if code not in seen_codes:
				seen_codes.add(code)
				candidate_codes.append(code)

	if not candidate_codes:
		return {
			"repair_order": docname,
			"company": ro.company,
			"warehouse": _resolve_default_warehouse(ro.company),
			"parts": [],
		}

	wh = _resolve_default_warehouse(ro.company)
	item_rows = frappe.get_list(
		"Item",
		filters={"name": ["in", candidate_codes]},
		fields=["name", "item_name", "standard_rate", "stock_uom"],
	)
	item_meta_map = {row.name: row for row in item_rows}
	valid_codes = [c for c in candidate_codes if c in item_meta_map]

	stock_map = get_items_available_stock_batch(
		valid_codes,
		warehouse=wh,
		ro_name=docname,
		ro_data=_extract_ro_metadata(ro),
	)
	supplier_map = _get_items_supplier_map(valid_codes)

	items_map = {}
	for code, part in raw_entries:
		if code not in item_meta_map:
			continue
		item_meta = item_meta_map[code]
		req_qty = flt(part.get("qty", 1.0))
		if code not in items_map:
			avail_info = stock_map.get(code, {})
			sup_info = supplier_map.get(code, {})
			items_map[code] = {
				"item_code": code,
				"item_name": part.get("item_name") or item_meta.item_name,
				"qty": 0.0,
				"stock_qty": flt(avail_info.get("available_qty", 0.0)),
				"actual_qty": flt(avail_info.get("actual_qty", 0.0)),
				"committed_qty": flt(avail_info.get("committed_qty", 0.0)),
				"rate": flt(part.get("rate") or getattr(item_meta, "standard_rate", 0.0)),
				"uom": part.get("uom") or getattr(item_meta, "stock_uom", "Nos"),
				"default_supplier": sup_info.get("default_supplier"),
				"known_suppliers": sup_info.get("known_suppliers", []),
				"order_mode": "Purchase Order",
			}
		items_map[code]["qty"] += req_qty

	parts_list = list(items_map.values())
	for p in parts_list:
		p["is_deficit"] = 1 if p["stock_qty"] < p["qty"] else 0

	return {
		"repair_order": docname,
		"company": ro.company,
		"warehouse": wh,
		"parts": parts_list,
	}


@frappe.whitelist()
def submit_procurement_orders(repair_order, parts_data, submit=True):
	"""Create (and optionally submit) Purchase Orders (grouped by supplier) and RFQs for a Repair Order."""
	frappe.has_permission("Repair Order", "write", throw=True)
	if isinstance(submit, str):
		should_submit = submit.strip().lower() in ("1", "true", "yes")
	else:
		should_submit = bool(submit)

	repair_order = _optional_str(repair_order, "Repair Order")
	if not repair_order or not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(_("Repair Order {0} not found").format(repair_order))

	ro = frappe.get_doc("Repair Order", repair_order)
	ro.check_permission("write")
	if isinstance(parts_data, str):
		try:
			parts_data = json.loads(parts_data)
		except Exception:
			frappe.throw(_("Invalid parts data format."))

	if not isinstance(parts_data, list) or not parts_data:
		frappe.throw(_("No parts selected for procurement."))

	company = ro.company or frappe.defaults.get_user_default("Company")
	target_wh = _resolve_default_warehouse(company)

	po_parts_by_supplier = {}
	rfq_parts = []
	rfq_suppliers = set()
	supplier_links = []

	for item in parts_data:
		if not isinstance(item, dict):
			frappe.throw(_("Invalid parts data format."))
		code = _optional_str(item.get("item_code"), "Item Code")
		if not code or not frappe.db.exists("Item", code):
			continue

		sup = _optional_str(item.get("supplier"), "Supplier") or ""
		mode = _optional_str(item.get("order_mode"), "Order Mode") or "Purchase Order"
		sup_part_no = _optional_str(item.get("supplier_part_no"), "Supplier Part No")

		if mode == "Request for Quotation":
			rfq_parts.append(item)
			if sup:
				rfq_suppliers.add(sup)
		else:
			if not sup:
				frappe.throw(_("Supplier is required for Purchase Order on item {0}.").format(code))
			po_parts_by_supplier.setdefault(sup, []).append(item)

		if sup:
			supplier_links.append((sup, code, sup_part_no))

	# Enforce Purchase Order / RFQ permissions before creating or linking suppliers.
	if po_parts_by_supplier:
		frappe.has_permission("Purchase Order", "create", throw=True)
		if should_submit:
			frappe.has_permission("Purchase Order", "submit", throw=True)
	if rfq_parts:
		frappe.has_permission("Request for Quotation", "create", throw=True)
		if should_submit:
			frappe.has_permission("Request for Quotation", "submit", throw=True)

	for sup, code, sup_part_no in supplier_links:
		ensure_supplier_and_link_to_item(sup, code, sup_part_no)

	from ism.setup import get_or_create_default_purchase_tax_template

	po_tax_template = get_or_create_default_purchase_tax_template(company)

	created_pos = [
		_create_purchase_order(ro.name, company, sup, items, target_wh, po_tax_template, should_submit)
		for sup, items in po_parts_by_supplier.items()
	]
	created_rfqs = []
	if rfq_parts:
		created_rfqs.append(_create_rfq(ro.name, company, rfq_suppliers, rfq_parts, target_wh, should_submit))

	return {
		"status": "success",
		"submitted": should_submit,
		"purchase_orders": created_pos,
		"rfqs": created_rfqs,
	}


@frappe.whitelist()
def get_default_warehouse(company=None):
	"""Dynamically resolve a valid, existing non-group warehouse.
	If company is specified, the warehouse MUST belong to that company.
	"""
	company = _optional_str(company, "Company")
	_require_any_read_permission(("Warehouse", "Item", "Repair Order"), primary_doctype="Warehouse")
	return _resolve_default_warehouse(company)


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


def resolve_part_side(side="Both", service_package=None, part_name=None, part_number=None):
	"""Resolve Side / Variant for Service Package Item.
	Must strictly return one of 'Both', 'LH', 'RH'. Never 'None'.
	"""
	side_str = str(side or "").strip()
	if side_str.upper() == "LH":
		candidate_side = "LH"
	elif side_str.upper() == "RH":
		candidate_side = "RH"
	else:
		candidate_side = "Both"

	part_ident = f"{part_number or ''} {part_name or ''}".strip()
	part_detected_side = detect_part_side(part_ident)

	pkg_has_lhrh = False
	if service_package:
		if has_lhrh_designation(service_package):
			pkg_has_lhrh = True
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


# ---------------------------------------------------------------------------
# Internal helpers & compatibility utilities
# ---------------------------------------------------------------------------


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


def _get_item_stock_qty(item_code, warehouse=None):
	"""Get current actual stock quantity for a single item (retained for repair_order.py compatibility)."""
	if not item_code:
		return 0.0
	return flt(_get_items_stock_qty_map([item_code], warehouse).get(item_code, 0.0))


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
		pass
	return stock_map


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

	filters = {
		"docstatus": ["<", 2],
		"status": ["not in", ["Closed / Released", "Cancelled"]],
		"approval_status": ["in", ["Approved", "Partially Approved"]],
	}
	if ro_name:
		filters["name"] = ["!=", ro_name]

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
		try:
			parsed = json.loads(raw_parts)
			return parsed if isinstance(parsed, list) else []
		except Exception:
			return []
	return raw_parts if isinstance(raw_parts, list) else []


def _get_items_supplier_map(item_codes):
	"""Batch load known suppliers and default supplier for a list of Item codes."""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	supplier_map = {code: {"known_suppliers": [], "default_supplier": None} for code in codes}
	if not codes:
		return supplier_map

	supplier_rows = frappe.get_all(
		"Item Supplier",
		filters={"parent": ["in", codes], "parenttype": "Item"},
		fields=["parent", "supplier", "idx"],
		order_by="parent asc, idx asc",
	)
	for row in supplier_rows:
		parent = row.get("parent")
		sup = row.get("supplier")
		if parent in supplier_map and sup and sup not in supplier_map[parent]["known_suppliers"]:
			supplier_map[parent]["known_suppliers"].append(sup)

	default_rows = frappe.get_all(
		"Item Default",
		filters={"parent": ["in", codes], "parenttype": "Item"},
		fields=["parent", "default_supplier", "idx"],
		order_by="parent asc, idx asc",
	)
	for row in default_rows:
		parent = row.get("parent")
		sup = row.get("default_supplier")
		if parent in supplier_map and sup and not supplier_map[parent]["default_supplier"]:
			supplier_map[parent]["default_supplier"] = sup

	for info in supplier_map.values():
		if not info["default_supplier"] and info["known_suppliers"]:
			info["default_supplier"] = info["known_suppliers"][0]

	return supplier_map


def _normalize_part_source(part_source):
	return "OEM" if str(part_source or "").upper().startswith("OEM") else "Aftermarket"


def _normalize_part_condition(part_condition):
	cond_norm = str(part_condition or "").capitalize().strip()
	if "Use" in cond_norm:
		return "Used"
	if any(tok in cond_norm for tok in ("Rec", "Rcn", "Reb", "Rem")):
		return "Reconditioned"
	return "New"


def _coerce_item_qty(qty):
	val = flt(qty)
	return val if val > 0 else 1.0


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


def _optional_str(value, field_label):
	if value is None or value == "":
		return None
	if not isinstance(value, str):
		frappe.throw(_("{0} must be a string.").format(field_label))
	return value.strip() or None


def _require_str(value, field_label):
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("{0} is required.").format(field_label))
	return value.strip()


def _require_any_read_permission(doctypes, primary_doctype=None):
	if any(frappe.has_permission(dt, "read") for dt in doctypes):
		return
	frappe.has_permission(primary_doctype or doctypes[0], "read", throw=True)


def _resolve_default_warehouse(company=None):
	"""Internal warehouse lookup after caller has enforced boundary permissions."""
	if company:
		try:
			stock_wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
			if stock_wh and frappe.db.exists("Warehouse", {"name": stock_wh, "company": company, "is_group": 0}):
				return stock_wh
		except Exception:
			pass
		wh = _find_company_warehouse(company)
		if wh:
			return wh

	try:
		wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
		if wh and frappe.db.exists("Warehouse", {"name": wh, "is_group": 0}):
			return wh
	except Exception:
		pass

	default_company = frappe.db.get_single_value("Global Defaults", "default_company")
	wh = _find_company_warehouse(default_company)
	if wh:
		return wh

	return frappe.db.get_value("Warehouse", {"is_group": 0}, "name") or "Stores"


def _parse_service_package_names(service_packages=None, primary=None):
	packages = []
	if service_packages:
		if isinstance(service_packages, str):
			try:
				parsed = json.loads(service_packages)
				if isinstance(parsed, list):
					packages.extend(parsed)
				else:
					packages.extend([s.strip() for s in service_packages.split(",") if s.strip()])
			except Exception:
				packages.extend([s.strip() for s in service_packages.split(",") if s.strip()])
		elif isinstance(service_packages, (list, tuple, set)):
			packages.extend(list(service_packages))
		else:
			frappe.throw(_("Service Packages must be a list or string."))

	cleaned = []
	for pkg_name in packages:
		if isinstance(pkg_name, (dict, list, tuple, set)):
			frappe.throw(_("Service Package name must be a string."))
		name = str(pkg_name).strip()
		if name and name not in cleaned:
			cleaned.append(name)

	if primary is not None and primary != "":
		if not isinstance(primary, str):
			frappe.throw(_("Service Package must be a string."))
		primary_name = primary.strip()
		if primary_name and primary_name not in cleaned:
			cleaned.append(primary_name)

	return cleaned


def _resolve_target_slot(slot_number=None, component_slot=None):
	if slot_number is not None and str(slot_number).strip() != "":
		return cint(slot_number)
	if component_slot:
		return parse_slot_number(component_slot)
	return 1


def _link_item_to_packages(
	item_code,
	package_names,
	target_slot_num=1,
	qty=1.0,
	rate=0.0,
	side="Both",
	is_default=0,
	part_name=None,
	part_number=None,
):
	linked = []
	for idx, pkg_name in enumerate(package_names):
		if not pkg_name or not frappe.db.exists("Service Package", pkg_name):
			continue
		# Primary package gets target slot & default flag; secondary packages default to Unassigned (0).
		pkg_slot = target_slot_num if idx == 0 else 0
		pkg_side = resolve_part_side(
			side=side,
			service_package=pkg_name,
			part_name=part_name,
			part_number=part_number or item_code,
		)
		_attach_part_to_service_package(
			service_package=pkg_name,
			item_code=item_code,
			slot_number=pkg_slot,
			is_default=is_default if idx == 0 else 0,
			qty=qty,
			rate=rate,
			side=pkg_side,
		)
		linked.append(pkg_name)
	return linked


def _attach_part_to_service_package(
	service_package,
	item_code,
	slot_number=1,
	is_default=0,
	qty=1.0,
	rate=0.0,
	side="Both",
):
	"""Upsert an item link row on Service Package without relying on removed modal handlers."""
	_ensure_item_custom_fields()

	s_num = cint(slot_number)
	slot_label = format_slot_label(s_num)
	pkg = frappe.get_doc("Service Package", service_package)
	pkg.check_permission("write")
	item_doc = frappe.get_cached_doc("Item", item_code)

	existing = None
	for row in (pkg.items or []):
		row_snum = cint(getattr(row, "slot_number", None))
		if row_snum == 0 and getattr(row, "component_slot", None):
			row_snum = parse_slot_number(row.component_slot)
		if row.item_code == item_code and (s_num == 0 or row_snum == s_num or row.component_slot == slot_label):
			existing = row
			break

	if is_default and s_num > 0:
		for row in (pkg.items or []):
			row_snum = cint(getattr(row, "slot_number", None))
			if row_snum == 0 and getattr(row, "component_slot", None):
				row_snum = parse_slot_number(row.component_slot)
			if row_snum == s_num and row.item_code != item_code:
				row.is_default = 0

	resolved_side = resolve_part_side(
		side=side,
		service_package=service_package,
		part_name=getattr(item_doc, "item_name", None),
		part_number=getattr(item_doc, "item_code", None),
	)

	if existing:
		existing.slot_number = s_num
		existing.component_slot = slot_label
		if is_default:
			existing.is_default = 1
		if qty:
			existing.qty = flt(qty)
		if rate:
			existing.rate = flt(rate)
		existing.side = resolved_side
	else:
		pkg.append("items", {
			"slot_number": s_num,
			"component_slot": slot_label,
			"item_code": item_code,
			"item_name": item_doc.item_name,
			"is_default": cint(is_default),
			"part_source": getattr(item_doc, "part_source", "OEM") or "OEM",
			"part_condition": getattr(item_doc, "part_condition", "New") or "New",
			"oem_part_number": getattr(item_doc, "oem_part_number", "") or "",
			"warranty_period": getattr(item_doc, "warranty_period", "12 Months / 20,000 km") or "12 Months / 20,000 km",
			"qty": flt(qty) or 1.0,
			"rate": flt(rate) or flt(item_doc.standard_rate),
			"side": resolved_side,
		})

	pkg.save()


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
			try:
				parsed = json.loads(stripped)
			except Exception:
				frappe.throw(_("Invalid JSON items payload."))
			if isinstance(parsed, list):
				return parsed, None
			if isinstance(parsed, dict):
				inner = parsed.get("items")
				return (inner if isinstance(inner, list) else [parsed]), None
			frappe.throw(_("Invalid JSON items payload."))
		if not raw_text:
			return None, items
	return None, raw_text


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
):
	if not isinstance(record, dict):
		errors.append({"index": idx, "item": record, "error": _("Each import item must be a JSON object.")})
		return
	try:
		res = _import_part_record(
			record=record,
			service_package=service_package,
			service_packages=service_packages,
			slot_number=slot_number,
			component_slot=component_slot,
			default_source=default_source,
			default_condition=default_condition,
			side=side,
			is_first=(len(created_items) == 0),
			supplier=supplier,
		)
		created_items.append(res)
	except Exception as exc:
		errors.append({"index": idx, "item": record, "error": str(exc)})


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
		if line_idx == 1 and parts and any(h in parts[0].lower() for h in ("part", "code", "item", "sku")):
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
):
	part_num = _scalar_str(
		record.get("part_number")
		or record.get("part_no")
		or record.get("sku")
		or record.get("code"),
		"Part Number",
	)
	if not part_num:
		frappe.throw(_("Part Number is required."))

	part_name = _scalar_str(
		record.get("part_name")
		or record.get("item_name")
		or record.get("description")
		or record.get("name"),
		"Part Name",
	) or part_num
	source = _scalar_str(record.get("part_source") or record.get("source"), "Part Source") or default_source
	condition = _scalar_str(record.get("part_condition") or record.get("condition"), "Part Condition") or default_condition

	unit_rate = _first_present_flt(record, ("unit_rate", "rate", "standard_rate", "price"), 0.0)
	valuation_rate = _first_present_flt(record, ("valuation_rate", "cost", "cost_price"), 0.0)
	qty = _coerce_item_qty(_first_present_flt(record, ("qty", "quantity"), 1.0))

	revision = _scalar_str(record.get("revision") or record.get("rev"), "Revision") or ""
	oem_num = _scalar_str(
		record.get("oem_part_number") or record.get("oem_part_no") or record.get("oem"),
		"OEM Part Number",
	) or (part_num if source.upper().startswith("OEM") else "")

	row_slot_num = record.get("slot_number") if record.get("slot_number") is not None else slot_number
	row_comp_slot = record.get("component_slot") or component_slot
	row_side = record.get("side") or side
	row_is_default = cint(record["is_default"]) if record.get("is_default") is not None else (1 if is_first else 0)

	res = quick_create_part(
		part_number=part_num,
		part_name=part_name,
		part_source=source,
		part_condition=condition,
		oem_part_number=oem_num,
		revision=revision,
		brand=_scalar_str(record.get("brand"), "Brand"),
		warranty_period=_scalar_str(record.get("warranty_period") or record.get("warranty"), "Warranty Period")
		or "12 Months / 20,000 km",
		unit_rate=unit_rate,
		valuation_rate=valuation_rate,
		stock_uom=_scalar_str(record.get("stock_uom") or record.get("uom"), "Stock UOM") or "Nos",
		slot_number=row_slot_num,
		component_slot=row_comp_slot,
		service_package=service_package,
		service_packages=service_packages,
		side=row_side,
		is_default=row_is_default,
		qty=qty,
	)
	res["qty"] = qty

	row_supplier = _scalar_str(record.get("supplier") or supplier, "Supplier") or ""
	if row_supplier:
		supplier_part_no = _scalar_str(
			record.get("supplier_part_no") or record.get("supplier_sku"),
			"Supplier Part No",
		) or part_num
		ensure_supplier_and_link_to_item(row_supplier, res["item_code"], supplier_part_no)
		res["supplier"] = row_supplier

	return res


def _scalar_str(value, field_label):
	if value is None or value == "":
		return None
	if isinstance(value, (dict, list, tuple, set, bool)):
		frappe.throw(_("{0} must be a scalar value.").format(field_label))
	return str(value).strip() or None


def _first_present_flt(record, keys, default=0.0):
	for key in keys:
		val = record.get(key)
		if val is not None and val != "":
			return flt(val)
	return default


def _create_purchase_order(ro_name, company, supplier, items, target_wh, po_tax_template, should_submit):
	po = frappe.new_doc("Purchase Order")
	po.company = company
	po.supplier = supplier
	po.transaction_date = today()
	po.schedule_date = today()

	if hasattr(po, "repair_order") or frappe.db.has_column("Purchase Order", "repair_order"):
		po.repair_order = ro_name

	for it in items:
		item_doc = frappe.get_cached_doc("Item", it["item_code"])
		rate = flt(
			it.get("rate")
			or getattr(item_doc, "last_purchase_rate", 0.0)
			or getattr(item_doc, "valuation_rate", 0.0)
			or getattr(item_doc, "standard_rate", 0.0)
		)
		po.append("items", {
			"item_code": it["item_code"],
			"item_name": it.get("item_name") or item_doc.item_name,
			"qty": flt(it.get("qty", 1.0)),
			"rate": rate,
			"uom": it.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
			"stock_uom": getattr(item_doc, "stock_uom", "Nos"),
			"conversion_factor": 1.0,
			"warehouse": target_wh,
			"schedule_date": today(),
		})

	if po_tax_template:
		po.taxes_and_charges = po_tax_template
		from erpnext.controllers.accounts_controller import get_taxes_and_charges
		taxes = get_taxes_and_charges("Purchase Taxes and Charges Template", po_tax_template)
		po.set("taxes", [])
		for tax_row in taxes:
			po.append("taxes", tax_row)

	po.set_missing_values()
	po.calculate_taxes_and_totals()
	po.insert()
	if should_submit:
		po.submit()
	return po.name


def _create_rfq(ro_name, company, rfq_suppliers, rfq_parts, target_wh, should_submit):
	rfq = frappe.new_doc("Request for Quotation")
	rfq.company = company
	rfq.transaction_date = today()

	if hasattr(rfq, "repair_order") or frappe.db.has_column("Request for Quotation", "repair_order"):
		rfq.repair_order = ro_name

	for sup in rfq_suppliers:
		rfq.append("suppliers", {"supplier": sup})

	for it in rfq_parts:
		item_doc = frappe.get_cached_doc("Item", it["item_code"])
		rfq.append("items", {
			"item_code": it["item_code"],
			"item_name": it.get("item_name") or item_doc.item_name,
			"qty": flt(it.get("qty", 1.0)),
			"uom": it.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
			"stock_uom": getattr(item_doc, "stock_uom", "Nos"),
			"conversion_factor": 1.0,
			"warehouse": target_wh,
			"schedule_date": today(),
		})

	rfq.set_missing_values()
	rfq.insert()
	if should_submit:
		rfq.submit()
	return rfq.name


def _find_company_warehouse(company):
	"""Return default, Stores-matching, or any non-group Warehouse for a company."""
	if not company:
		return None
	try:
		co_wh = frappe.db.get_value("Company", company, "default_warehouse")
		if co_wh and frappe.db.exists("Warehouse", {"name": co_wh, "company": company, "is_group": 0}):
			return co_wh
	except Exception:
		pass
	stores_wh = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 0, "warehouse_name": ["like", "%Store%"]}, "name"
	)
	if stores_wh:
		return stores_wh
	return frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
