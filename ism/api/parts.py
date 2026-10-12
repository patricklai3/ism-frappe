# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import csv
import io
import json
import re
import frappe
from frappe import _
from frappe.utils import flt, cint


def format_item_code(part_source="OEM", part_number="", part_condition="New"):
	"""Generate strictly standardized Item Code: [Source]-[PartNumber]-[Condition]
	e.g. OEM-1044321-00-J-NEW, OEM-1044321-00-J-USED, AFT-RK623000-NEW
	"""
	src = "OEM" if str(part_source).upper().startswith("OEM") else "AFT"

	cond_str = str(part_condition).upper().strip()
	if "USE" in cond_str:
		cond = "USED"
	elif "REC" in cond_str or "RECON" in cond_str or "REB" in cond_str or "RCN" in cond_str:
		cond = "RCN"
	else:
		cond = "NEW"

	# Sanitize part number: keep alphanumerics, hyphens, and underscores
	clean_part = re.sub(r"[^A-Za-z0-9_\-]", "", str(part_number).strip().upper())
	if not clean_part:
		clean_part = "UNKNOWN"

	return f"{src}-{clean_part}-{cond}"


def parse_slot_number(val):
	"""Extract integer slot number from string or int (e.g. 1, 'Slot 1', 'slot 2', 'Unassigned' -> 0)."""
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
	"""Format standard display label for slot: 'Slot 1', 'Slot 2', or 'Unassigned'."""
	s_num = cint(slot_number)
	if s_num <= 0:
		return "Unassigned"
	return f"Slot {s_num}"


@frappe.whitelist()
def generate_preview_item_code(part_source="OEM", part_number="", part_condition="New"):
	"""Whitelisted helper for client-side live preview."""
	return format_item_code(part_source, part_number, part_condition)


def _get_item_stock_qty(item_code, warehouse=None):
	"""Get current actual stock quantity for an item across warehouses or in a specific warehouse."""
	if not item_code:
		return 0.0

	try:
		if warehouse:
			val = frappe.db.sql(
				"SELECT SUM(actual_qty) FROM `tabBin` WHERE item_code = %s AND warehouse = %s",
				(item_code, warehouse),
			)[0][0]
		else:
			val = frappe.db.sql(
				"SELECT SUM(actual_qty) FROM `tabBin` WHERE item_code = %s",
				(item_code,),
			)[0][0]
		return flt(val) if val is not None else 0.0
	except Exception:
		return 0.0



@frappe.whitelist()
def get_default_warehouse(company=None):
	"""Dynamically resolve a valid, existing non-group warehouse.
	If company is specified, the warehouse MUST belong to that company.
	"""
	if company:
		# Check if Stock Settings default_warehouse belongs to this company
		stock_wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
		if stock_wh and frappe.db.exists("Warehouse", {"name": stock_wh, "company": company, "is_group": 0}):
			return stock_wh

		# Check default_warehouse in Company doc
		try:
			co_wh = frappe.db.get_value("Company", company, "default_warehouse")
			if co_wh and frappe.db.exists("Warehouse", {"name": co_wh, "company": company, "is_group": 0}):
				return co_wh
		except Exception:
			pass

		# Look for a Stores / Stock warehouse for this company
		stores_wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0, "warehouse_name": ["like", "%Store%"]}, "name")
		if stores_wh:
			return stores_wh

		# Any non-group warehouse for this company
		wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		if wh:
			return wh

	# If no company provided or no warehouse found for specified company:
	wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if wh and frappe.db.exists("Warehouse", {"name": wh, "is_group": 0}):
		return wh

	default_company = frappe.db.get_single_value("Global Defaults", "default_company")
	if default_company:
		wh = frappe.db.get_value("Warehouse", {"company": default_company, "is_group": 0}, "name")
		if wh:
			return wh

	wh = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
	if wh:
		return wh

	return "Stores"


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
):
	"""Fast single part creator that ensures standard ERPNext inventory defaults,
	automotive compliance custom fields, and optional auto-linking to Service Package.
	"""
	if not part_number or not str(part_number).strip():
		frappe.throw(_("Part Number is required."))

	part_number = str(part_number).strip()
	part_source = "OEM" if str(part_source).upper().startswith("OEM") else "Aftermarket"

	cond_norm = str(part_condition).capitalize().strip()
	if "Use" in cond_norm:
		part_condition = "Used"
	elif "Rec" in cond_norm or "Rcn" in cond_norm or "Reb" in cond_norm:
		part_condition = "Reconditioned"
	else:
		part_condition = "New"

	item_code = format_item_code(part_source, part_number, part_condition)

	if not oem_part_number:
		if part_source == "OEM":
			oem_part_number = part_number

	if not part_name:
		part_name = f"{part_number} ({part_source} {part_condition})"

	unit_rate = flt(unit_rate)
	valuation_rate = flt(valuation_rate)

	# Ensure Item Group exists
	item_group = "Parts" if frappe.db.exists("Item Group", "Parts") else (
		frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
	)

	# Ensure UOM exists
	uom_name = stock_uom or "Nos"
	if not frappe.db.exists("UOM", uom_name):
		uom_name = "Nos" if frappe.db.exists("UOM", "Nos") else "Unit"

	# Default warehouse
	default_wh = get_default_warehouse()

	# Check or create Item
	if not frappe.db.exists("Item", item_code):
		item = frappe.new_doc("Item")
		item.item_code = item_code
		item.item_name = part_name
		item.item_group = item_group
		item.is_stock_item = 1
		item.is_sales_item = 1
		item.is_purchase_item = 1
		item.include_item_in_manufacturing = 0
		item.stock_uom = uom_name
		item.default_warehouse = default_wh
		item.standard_rate = unit_rate
		item.valuation_rate = valuation_rate
		item.description = f"{part_name} [{part_source} - {part_condition}]"

		# Custom automotive fields
		item.is_vehicle_part = 1
		item.part_source = part_source
		item.part_condition = part_condition
		item.oem_part_number = oem_part_number or ""
		item.revision = revision or ""
		if brand and frappe.db.exists("Brand", brand):
			item.brand = brand
		item.warranty_period = warranty_period or "12 Months / 20,000 km"

		item.insert(ignore_permissions=True)
	else:
		# Update rates & compliance fields if not set
		item = frappe.get_doc("Item", item_code)
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
		if updated:
			item.save(ignore_permissions=True)

	# Link to Service Packages if requested
	packages_to_link = []
	if service_packages:
		if isinstance(service_packages, str):
			try:
				parsed = json.loads(service_packages)
				if isinstance(parsed, list):
					packages_to_link.extend(parsed)
				else:
					packages_to_link.extend([s.strip() for s in service_packages.split(",") if s.strip()])
			except Exception:
				packages_to_link.extend([s.strip() for s in service_packages.split(",") if s.strip()])
		elif isinstance(service_packages, (list, tuple, set)):
			packages_to_link.extend(list(service_packages))

	if service_package and service_package not in packages_to_link:
		packages_to_link.append(service_package)

	# Resolve slot number
	if slot_number is not None and str(slot_number).strip() != "":
		target_slot_num = cint(slot_number)
	elif component_slot:
		target_slot_num = parse_slot_number(component_slot)
	else:
		target_slot_num = 1

	slot_label = format_slot_label(target_slot_num)

	for idx, pkg_name in enumerate(packages_to_link):
		if pkg_name and frappe.db.exists("Service Package", pkg_name):
			# Primary package gets the specified slot; subsequent packages accommodate it as Unassigned (0)
			pkg_slot = target_slot_num if idx == 0 else 0
			save_part_to_service_package(
				service_package=pkg_name,
				item_code=item_code,
				slot_number=pkg_slot,
				component_slot=format_slot_label(pkg_slot),
				is_default=is_default if idx == 0 else 0,
				qty=1.0,
				rate=unit_rate or flt(item.standard_rate),
				side=side,
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
	}


@frappe.whitelist()
def bulk_import_parts(raw_text, service_package=None, service_packages=None, slot_number=None, component_slot=None, default_source="OEM", default_condition="New"):
	"""Parse pasted CSV, TSV, or pipe-delimited text and batch-create items.
	Expected header/line format:
	Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#
	"""
	if not raw_text or not raw_text.strip():
		frappe.throw(_("No import text provided."))

	lines = raw_text.strip().splitlines()
	created_items = []
	errors = []

	for line_idx, line in enumerate(lines, 1):
		cleaned = line.strip()
		if not cleaned:
			continue

		# Detect delimiter: tab, pipe, comma, semicolon
		if "\t" in cleaned:
			parts = cleaned.split("\t")
		elif "|" in cleaned:
			parts = [p.strip() for p in cleaned.split("|")]
		elif ";" in cleaned:
			parts = [p.strip() for p in cleaned.split(";")]
		else:
			reader = csv.reader(io.StringIO(cleaned))
			try:
				parts = next(reader)
			except Exception:
				parts = [p.strip() for p in cleaned.split(",")]

		parts = [p.strip() for p in parts]

		# Skip probable header row
		if line_idx == 1 and any(h in parts[0].lower() for h in ["part", "code", "item", "sku"]):
			continue

		part_num = parts[0] if len(parts) > 0 else ""
		if not part_num:
			continue

		desc = parts[1] if len(parts) > 1 and parts[1] else part_num
		source = parts[2] if len(parts) > 2 and parts[2] else default_source
		cond = parts[3] if len(parts) > 3 and parts[3] else default_condition
		rate = flt(parts[4]) if len(parts) > 4 and parts[4] else 0.0
		cost = flt(parts[5]) if len(parts) > 5 and parts[5] else 0.0
		qty = flt(parts[6]) if len(parts) > 6 and parts[6] else 1.0
		rev = parts[7] if len(parts) > 7 and parts[7] else ""
		oem_num = parts[8] if len(parts) > 8 and parts[8] else ""

		try:
			res = quick_create_part(
				part_number=part_num,
				part_name=desc,
				part_source=source,
				part_condition=cond,
				oem_part_number=oem_num or (part_num if source.upper().startswith("OEM") else ""),
				revision=rev,
				unit_rate=rate,
				valuation_rate=cost,
				slot_number=slot_number,
				component_slot=component_slot,
				service_package=service_package,
				service_packages=service_packages,
				is_default=1 if len(created_items) == 0 else 0,
			)
			res["qty"] = qty
			created_items.append(res)
		except Exception as e:
			errors.append({"line": line_idx, "text": line, "error": str(e)})

	return {
		"created": created_items,
		"errors": errors,
		"total_created": len(created_items),
	}


@frappe.whitelist()
def get_service_package_slots(service_package):
	"""Return grouped component slots and preview of their current items for a Service Package.
	Used by the Part Importer and desk views to preview existing parts in each slot.
	"""
	if not service_package or not frappe.db.exists("Service Package", service_package):
		return {
			"slots": [],
			"unassigned": [],
			"next_slot_number": 1,
		}

	pkg = frappe.get_doc("Service Package", service_package)
	slots_map = {}
	unassigned = []
	max_slot = 0

	for row in (pkg.items or []):
		if not row.item_code:
			continue

		s_num = cint(getattr(row, "slot_number", None))
		if s_num == 0 and getattr(row, "component_slot", None):
			s_num = parse_slot_number(row.component_slot)

		item_info = {
			"item_code": row.item_code,
			"item_name": row.item_name or row.item_code,
			"part_source": getattr(row, "part_source", "OEM") or "OEM",
			"part_condition": getattr(row, "part_condition", "New") or "New",
			"is_default": cint(getattr(row, "is_default", 0)),
			"rate": flt(row.rate),
			"qty": flt(row.qty) or 1.0,
			"side": getattr(row, "side", "Both") or "Both",
			"stock_qty": _get_item_stock_qty(row.item_code),
		}

		if s_num > 0:
			if s_num > max_slot:
				max_slot = s_num
			if s_num not in slots_map:
				slots_map[s_num] = {
					"slot_number": s_num,
					"slot_label": format_slot_label(s_num),
					"parts": [],
				}
			slots_map[s_num]["parts"].append(item_info)
		else:
			unassigned.append(item_info)

	# Build sorted list of slots
	slots_list = [slots_map[k] for k in sorted(slots_map.keys())]

	return {
		"slots": slots_list,
		"unassigned": unassigned,
		"next_slot_number": max_slot + 1 if max_slot > 0 else 1,
	}


@frappe.whitelist()
def assign_part_to_slot(service_package, item_code, slot_number):
	"""Permanently assign an unassigned (or existing) part in a Service Package to a specific slot number.
	Wipes the part from unassigned status once assigned.
	"""
	if not service_package or not frappe.db.exists("Service Package", service_package):
		frappe.throw(_("Service Package '{0}' not found.").format(service_package))

	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item '{0}' not found.").format(item_code))

	s_num = cint(slot_number)
	if s_num < 0:
		s_num = 0
	slot_label = format_slot_label(s_num)

	pkg = frappe.get_doc("Service Package", service_package)
	found = False

	for row in (pkg.items or []):
		if row.item_code == item_code:
			row.slot_number = s_num
			row.component_slot = slot_label
			found = True
			break

	if not found:
		item_doc = frappe.get_cached_doc("Item", item_code)
		pkg.append("items", {
			"slot_number": s_num,
			"component_slot": slot_label,
			"item_code": item_code,
			"item_name": item_doc.item_name,
			"is_default": 0,
			"part_source": getattr(item_doc, "part_source", "OEM") or "OEM",
			"part_condition": getattr(item_doc, "part_condition", "New") or "New",
			"oem_part_number": getattr(item_doc, "oem_part_number", "") or "",
			"warranty_period": getattr(item_doc, "warranty_period", "12 Months / 20,000 km") or "12 Months / 20,000 km",
			"qty": 1.0,
			"rate": flt(item_doc.standard_rate),
			"side": "Both",
		})

	pkg.flags.ignore_permissions = True
	pkg.save()

	return {
		"status": "success",
		"service_package": service_package,
		"item_code": item_code,
		"slot_number": s_num,
		"slot_label": slot_label,
		"message": _("Permanently assigned {0} to {1} in {2}").format(item_code, slot_label, service_package),
	}


@frappe.whitelist()
def get_component_alternates(service_package=None, component_slot=None, slot_number=None, oem_part_number=None, item_code=None):
	"""Return all compatible items for a component slot, including condition variants and redesigns.
	Also includes unassigned compatible parts from the Service Package so they can be assigned.
	Fetches live warehouse inventory balances.
	"""
	items = {}

	target_slot_num = None
	if slot_number is not None and str(slot_number).strip() != "":
		target_slot_num = cint(slot_number)
	elif component_slot:
		target_slot_num = parse_slot_number(component_slot)

	target_slot_label = format_slot_label(target_slot_num) if target_slot_num else component_slot

	# 1. Look up explicitly saved items in Service Package
	if service_package and frappe.db.exists("Service Package", service_package):
		pkg = frappe.get_doc("Service Package", service_package)
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

			# Include if in this slot or if unassigned
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

	# 2. Look up items sharing oem_part_number
	if not oem_part_number and item_code and frappe.db.exists("Item", item_code):
		oem_part_number = frappe.db.get_value("Item", item_code, "oem_part_number")

	if oem_part_number:
		matched_items = frappe.get_all(
			"Item",
			filters={"oem_part_number": oem_part_number, "disabled": 0},
			fields=["name", "item_name", "part_source", "part_condition", "oem_part_number", "revision", "standard_rate", "warranty_period", "stock_uom"],
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

	# 3. Populate missing metadata & live inventory for each item
	results = []
	for ic, data in items.items():
		it_doc = frappe.db.get_value(
			"Item",
			ic,
			["item_name", "part_source", "part_condition", "oem_part_number", "revision", "brand", "standard_rate", "warranty_period", "stock_uom"],
			as_dict=True,
		)
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

		data["stock_qty"] = _get_item_stock_qty(ic)
		results.append(data)

	# Sort: default first, then assigned before unassigned, then OEM New, then other OEM, then Aftermarket
	def sort_key(x):
		unassigned_rank = 1 if x.get("is_unassigned") else 0
		default_rank = 0 if x.get("is_default") else 1
		source_rank = 0 if x.get("part_source") == "OEM" else 1
		cond_rank = 0 if x.get("part_condition") == "New" else (1 if x.get("part_condition") == "Reconditioned" else 2)
		return (unassigned_rank, default_rank, source_rank, cond_rank, x.get("item_code"))

	results.sort(key=sort_key)
	return results


@frappe.whitelist()
def save_part_to_service_package(
	service_package,
	component_slot=None,
	item_code=None,
	slot_number=None,
	is_default=0,
	qty=1.0,
	rate=0.0,
	side="Both",
	**kwargs,
):
	"""Save an item under a numbered component slot (or Unassigned) in Service Package.
	Backward-compatible with positional or keyword calls.
	"""
	if not service_package or not frappe.db.exists("Service Package", service_package):
		frappe.throw(_("Service Package '{0}' not found.").format(service_package))

	# Handle swapped positional arguments if called as (service_package, component_slot, item_code)
	if not item_code and kwargs.get("item_code"):
		item_code = kwargs.get("item_code")

	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item '{0}' not found.").format(item_code))

	# Resolve slot number
	if slot_number is not None and str(slot_number).strip() != "":
		s_num = cint(slot_number)
	elif component_slot:
		s_num = parse_slot_number(component_slot)
	else:
		s_num = 1

	slot_label = format_slot_label(s_num)

	pkg = frappe.get_doc("Service Package", service_package)
	item_doc = frappe.get_cached_doc("Item", item_code)

	# Check if already present under this slot or package
	existing = None
	for row in (pkg.items or []):
		row_snum = cint(getattr(row, "slot_number", None))
		if row_snum == 0 and getattr(row, "component_slot", None):
			row_snum = parse_slot_number(row.component_slot)
		if row.item_code == item_code and (s_num == 0 or row_snum == s_num or row.component_slot == slot_label):
			existing = row
			break

	if is_default and s_num > 0:
		# Unset other defaults in same slot
		for row in (pkg.items or []):
			row_snum = cint(getattr(row, "slot_number", None))
			if row_snum == 0 and getattr(row, "component_slot", None):
				row_snum = parse_slot_number(row.component_slot)
			if row_snum == s_num and row.item_code != item_code:
				row.is_default = 0

	if existing:
		existing.slot_number = s_num
		existing.component_slot = slot_label
		if is_default:
			existing.is_default = 1
		if qty:
			existing.qty = flt(qty)
		if rate:
			existing.rate = flt(rate)
		if side:
			existing.side = side
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
			"side": side or "Both",
		})

	pkg.flags.ignore_permissions = True
	pkg.save()
	return {
		"status": "success",
		"slot_number": s_num,
		"slot_label": slot_label,
		"message": _("Saved {0} to {1} of Service Package {2}").format(item_code, slot_label, service_package),
	}


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
	The primary package receives the chosen slot_number (or Unassigned if 0).
	All secondary packages receive the part as Unassigned (slot_number = 0),
	so users are not forced to assign slots for multiple packages simultaneously.
	"""
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item '{0}' not found.").format(item_code))

	if isinstance(service_packages, str):
		try:
			parsed = json.loads(service_packages)
			if isinstance(parsed, list):
				service_packages = parsed
			else:
				service_packages = [s.strip() for s in service_packages.split(",") if s.strip()]
		except Exception:
			service_packages = [s.strip() for s in service_packages.split(",") if s.strip()]

	if not isinstance(service_packages, (list, tuple, set)):
		service_packages = [service_packages] if service_packages else []

	target_slot_num = None
	if slot_number is not None and str(slot_number).strip() != "":
		target_slot_num = cint(slot_number)
	elif component_slot:
		target_slot_num = parse_slot_number(component_slot)
	else:
		target_slot_num = 1

	linked_packages = []
	for idx, pkg_name in enumerate(service_packages):
		pkg_name = str(pkg_name).strip()
		if not pkg_name or not frappe.db.exists("Service Package", pkg_name):
			continue

		# Primary package gets the specified slot; secondary packages get Unassigned (0)
		pkg_slot = target_slot_num if idx == 0 else 0

		save_part_to_service_package(
			service_package=pkg_name,
			item_code=item_code,
			slot_number=pkg_slot,
			component_slot=format_slot_label(pkg_slot),
			is_default=is_default if idx == 0 else 0,
			qty=qty,
			rate=rate,
			side=side,
		)
		linked_packages.append(pkg_name)

	return {"status": "success", "linked_packages": linked_packages, "linked_count": len(linked_packages)}


@frappe.whitelist()
def search_service_packages(query="", limit=25):
	"""Search for Service Packages by title, correction code, model, or category."""
	query = str(query or "").strip()
	or_filters = None
	if query:
		or_filters = [
			["package_name", "like", f"%{query}%"],
			["correction_code", "like", f"%{query}%"],
			["applicable_model", "like", f"%{query}%"],
		]

	packages = frappe.get_all(
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
	"""Fast creation of a Service Package directly from the Part Importer dialog."""
	if not package_name or not str(package_name).strip():
		frappe.throw(_("Service Package Name is required."))

	pkg_name = str(package_name).strip()
	if frappe.db.exists("Service Package", pkg_name):
		pkg = frappe.get_doc("Service Package", pkg_name)
		if applicable_model and not pkg.applicable_model:
			pkg.applicable_model = applicable_model
			pkg.save(ignore_permissions=True)
		return pkg.as_dict()

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
	pkg.insert(ignore_permissions=True)
	return pkg.as_dict()

