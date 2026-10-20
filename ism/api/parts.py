# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import csv
import io
import json
import re
import frappe
from frappe import _
from frappe.utils import flt, cint, today


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


LHRH_PATTERN = re.compile(r'\b(LH|RH)\b|\bLeft\s+Hand\b(?!\s*Drive)|\bRight\s+Hand\b(?!\s*Drive)', re.IGNORECASE)
LH_ONLY_PATTERN = re.compile(r'\bLH\b|\bLeft\s+Hand\b(?!\s*Drive)', re.IGNORECASE)
RH_ONLY_PATTERN = re.compile(r'\bRH\b|\bRight\s+Hand\b(?!\s*Drive)', re.IGNORECASE)


def has_lhrh_designation(val):
	"""Check if text contains explicit LH, RH, Left Hand, or Right Hand designation.
	Phrases like "driver's side" or "passenger side" do NOT qualify.
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
	Must strictly return one of "Both", "LH", "RH". Never "None".
	If neither service nor part has Left Hand / Right Hand designation, returns "Both".
	"""
	side_str = str(side or "").strip()
	if side_str.lower() in ("none", "null", "both", ""):
		candidate_side = "Both"
	elif side_str.upper() == "LH":
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
			is_symm = frappe.db.get_value("Service Package", service_package, "is_symmetrical")
			if is_symm:
				pkg_has_lhrh = True

	# If neither service nor part has LH/RH designation, side is always Both
	if not pkg_has_lhrh and not part_detected_side:
		return "Both"

	if part_detected_side:
		return part_detected_side

	if pkg_has_lhrh and candidate_side in ("LH", "RH"):
		return candidate_side

	return "Both"



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


def get_repair_order_committed_qty(item_code, warehouse=None, ro_name=None):
	"""Calculate total quantity of item_code committed to active approved Repair Orders
	that take priority over ro_name.

	Priority:
	- If ro_name is an active approved RO, priority is FIFO by (creation, name).
	- If ro_name is unapproved/draft/estimating/new, ALL active approved ROs have priority.
	"""
	if not item_code:
		return 0.0, []

	ro_creation = None
	is_ro_approved = False
	if ro_name and frappe.db.exists("Repair Order", ro_name):
		ro_data = frappe.db.get_value(
			"Repair Order",
			ro_name,
			["creation", "approval_status", "status", "docstatus"],
			as_dict=True,
		)
		if ro_data and cint(ro_data.docstatus) < 2:
			ro_creation = ro_data.creation
			is_ro_approved = (
				ro_data.approval_status in ("Approved", "Partially Approved")
				and ro_data.status not in ("Closed / Released", "Cancelled")
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

	if not prior_ros:
		return 0.0, []

	services = frappe.get_all(
		"Repair Order Service",
		filters={"parent": ["in", list(prior_ros.keys())], "status": "Approved"},
		fields=["parent", "parts"],
	)

	committed_qty = 0.0
	details_map = {}

	for s in services:
		parts = s.parts
		if isinstance(parts, str):
			try:
				parts = json.loads(parts) if parts else []
			except Exception:
				parts = []
		for p in parts or []:
			if p.get("item_code") == item_code:
				part_wh = p.get("warehouse")
				if not part_wh and s.parent in prior_ros:
					part_wh = get_default_warehouse(prior_ros[s.parent].company)
				if warehouse and part_wh and part_wh != warehouse:
					continue
				p_qty = flt(p.get("qty", 1.0))
				committed_qty += p_qty
				if s.parent not in details_map:
					details_map[s.parent] = 0.0
				details_map[s.parent] += p_qty

	details = [{"repair_order": ro, "qty": q} for ro, q in details_map.items()]
	return committed_qty, details


def get_item_available_stock(item_code, warehouse=None, ro_name=None):
	"""Get live available stock for a Repair Order after deducting earlier commitments."""
	actual_qty = _get_item_stock_qty(item_code, warehouse)
	committed_qty, details = get_repair_order_committed_qty(item_code, warehouse, ro_name)
	available_qty = max(0.0, flt(actual_qty) - flt(committed_qty))
	return {
		"item_code": item_code,
		"warehouse": warehouse,
		"actual_qty": flt(actual_qty),
		"committed_qty": flt(committed_qty),
		"available_qty": flt(available_qty),
		"committed_orders": details,
	}


@frappe.whitelist()
def get_repair_order_parts_stock(ro_name=None, items=None, company=None):
	"""Whitelisted API to fetch live stock and commitment data for repair order parts."""
	target_items = []
	if items:
		if isinstance(items, str):
			try:
				items = json.loads(items)
			except Exception:
				items = [s.strip() for s in items.split(",") if s.strip()]
		if isinstance(items, (list, tuple, set)):
			target_items = [str(x).strip() for x in items if str(x).strip()]

	target_wh = get_default_warehouse(company)

	if not target_items and ro_name and frappe.db.exists("Repair Order", ro_name):
		ro = frappe.get_doc("Repair Order", ro_name)
		if not target_wh:
			target_wh = get_default_warehouse(ro.company)
		for s in ro.services:
			parts = s.parts
			if isinstance(parts, str):
				try:
					parts = json.loads(parts) if parts else []
				except Exception:
					parts = []
			for p in parts or []:
				c = p.get("item_code")
				if c and c not in target_items:
					target_items.append(c)

	result = {}
	for code in target_items:
		result[code] = get_item_available_stock(code, target_wh, ro_name)

	return result




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

	# Ensure automotive compliance custom fields exist on Item
	if not frappe.db.has_column("Item", "part_source"):
		from ism.setup import setup_item_custom_fields
		setup_item_custom_fields()

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
	primary_pkg = packages_to_link[0] if packages_to_link else service_package
	resolved_side = resolve_part_side(
		side=side,
		service_package=primary_pkg,
		part_name=part_name,
		part_number=part_number,
	)

	for idx, pkg_name in enumerate(packages_to_link):
		if pkg_name and frappe.db.exists("Service Package", pkg_name):
			# Primary package gets the specified slot; subsequent packages accommodate it as Unassigned (0)
			pkg_slot = target_slot_num if idx == 0 else 0
			pkg_side = resolve_part_side(
				side=side,
				service_package=pkg_name,
				part_name=part_name,
				part_number=part_number,
			)
			save_part_to_service_package(
				service_package=pkg_name,
				item_code=item_code,
				slot_number=pkg_slot,
				component_slot=format_slot_label(pkg_slot),
				is_default=is_default if idx == 0 else 0,
				qty=1.0,
				rate=unit_rate or flt(item.standard_rate),
				side=pkg_side,
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
def bulk_import_parts(raw_text, service_package=None, service_packages=None, slot_number=None, component_slot=None, default_source="OEM", default_condition="New", side="Both"):
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
				side=side,
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

	# Ensure automotive compliance custom fields exist on Item before saving link
	if not frappe.db.has_column("Item", "part_source"):
		from ism.setup import setup_item_custom_fields
		setup_item_custom_fields()

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

	pkg.flags.ignore_permissions = True
	pkg.save()
	return {
		"status": "success",
		"slot_number": s_num,
		"slot_label": slot_label,
		"side": resolved_side,
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


@frappe.whitelist()
def ensure_supplier_and_link_to_item(supplier_name, item_code=None, supplier_part_no=None):
	"""Ensure a Supplier exists, and link it to Item's supplier_items child table and default_supplier."""
	if not supplier_name or not str(supplier_name).strip():
		return None

	s_name = str(supplier_name).strip()
	if not frappe.db.exists("Supplier", s_name):
		# Determine default supplier group
		sup_group = frappe.db.get_single_value("Buying Settings", "supplier_group") or "All Supplier Groups"
		if not frappe.db.exists("Supplier Group", sup_group):
			first_group = frappe.db.get_value("Supplier Group", {}, "name")
			sup_group = first_group or "All Supplier Groups"

		sup = frappe.new_doc("Supplier")
		sup.supplier_name = s_name
		sup.supplier_group = sup_group
		sup.flags.ignore_permissions = True
		sup.insert()

	if item_code and frappe.db.exists("Item", item_code):
		item_doc = frappe.get_doc("Item", item_code)
		existing_suppliers = [getattr(row, "supplier", None) for row in getattr(item_doc, "supplier_items", [])]
		changed = False
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
			item_doc.flags.ignore_permissions = True
			item_doc.save()

	return s_name


@frappe.whitelist()
def get_item_procurement_info(item_code, company=None, ro_name=None):
	"""Query stock levels, known suppliers from Item Supplier child table, and default supplier for an Item."""
	if not item_code or not frappe.db.exists("Item", item_code):
		return {}

	item_doc = frappe.get_cached_doc("Item", item_code)
	wh = get_default_warehouse(company)
	avail_info = get_item_available_stock(item_code, wh, ro_name)

	known_suppliers = []
	for row in getattr(item_doc, "supplier_items", []):
		if row.supplier and row.supplier not in known_suppliers:
			known_suppliers.append(row.supplier)

	default_sup = None
	if hasattr(item_doc, "item_defaults"):
		for d in item_doc.item_defaults:
			if getattr(d, "default_supplier", None):
				default_sup = d.default_supplier
				break
	if not default_sup:
		default_sup = known_suppliers[0] if known_suppliers else None

	return {
		"item_code": item_code,
		"item_name": item_doc.item_name,
		"stock_qty": flt(avail_info["available_qty"]),
		"actual_qty": flt(avail_info["actual_qty"]),
		"committed_qty": flt(avail_info["committed_qty"]),
		"warehouse": wh,
		"default_supplier": default_sup,
		"known_suppliers": known_suppliers,
		"rate": flt(getattr(item_doc, "standard_rate", 0.0) or getattr(item_doc, "valuation_rate", 0.0)),
		"uom": getattr(item_doc, "stock_uom", "Nos"),
	}


@frappe.whitelist()
def get_repair_order_procurement_data(docname):
	"""Gather all required parts from approved services of a Repair Order with current stock & supplier details."""
	if not docname or not frappe.db.exists("Repair Order", docname):
		frappe.throw(_("Repair Order {0} not found").format(docname))

	ro = frappe.get_doc("Repair Order", docname)
	approved_services = [s for s in ro.services if s.status == "Approved"]
	if not approved_services:
		return {
			"repair_order": docname,
			"company": ro.company,
			"parts": [],
			"message": _("No approved services found on this Repair Order."),
		}

	# Aggregate quantities by item_code
	items_map = {}
	wh = get_default_warehouse(ro.company)

	for s in approved_services:
		parts = s.parts
		if isinstance(parts, str):
			try:
				parts = json.loads(parts) if parts else []
			except Exception:
				parts = []
		for p in parts or []:
			code = p.get("item_code")
			if not code or not frappe.db.exists("Item", code):
				continue
			req_qty = flt(p.get("qty", 1.0))
			if code not in items_map:
				item_doc = frappe.get_cached_doc("Item", code)
				avail_info = get_item_available_stock(code, wh, docname)
				known_sups = [r.supplier for r in getattr(item_doc, "supplier_items", []) if r.supplier]
				def_sup = None
				if hasattr(item_doc, "item_defaults"):
					for d in item_doc.item_defaults:
						if getattr(d, "default_supplier", None):
							def_sup = d.default_supplier
							break
				if not def_sup:
					def_sup = known_sups[0] if known_sups else None
				items_map[code] = {
					"item_code": code,
					"item_name": p.get("item_name") or item_doc.item_name,
					"qty": 0.0,
					"stock_qty": flt(avail_info["available_qty"]),
					"actual_qty": flt(avail_info["actual_qty"]),
					"committed_qty": flt(avail_info["committed_qty"]),
					"rate": flt(p.get("rate") or getattr(item_doc, "standard_rate", 0.0)),
					"uom": p.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
					"default_supplier": def_sup,
					"known_suppliers": known_sups,
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
def submit_procurement_orders(repair_order, parts_data):
	"""Directly create and submit Purchase Orders (grouped by supplier) and RFQs for a Repair Order."""
	if not repair_order or not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(_("Repair Order {0} not found").format(repair_order))

	ro = frappe.get_doc("Repair Order", repair_order)
	if isinstance(parts_data, str):
		try:
			parts_data = json.loads(parts_data)
		except Exception:
			frappe.throw(_("Invalid parts data format."))

	if not parts_data:
		frappe.throw(_("No parts selected for procurement."))

	company = ro.company or frappe.defaults.get_user_default("Company")
	target_wh = get_default_warehouse(company)

	po_parts_by_supplier = {}
	rfq_parts = []
	rfq_suppliers = set()

	for item in parts_data:
		code = item.get("item_code")
		if not code or not frappe.db.exists("Item", code):
			continue

		sup = str(item.get("supplier") or "").strip()
		mode = str(item.get("order_mode") or "Purchase Order").strip()

		# Ensure supplier exists and is linked to Item
		if sup:
			ensure_supplier_and_link_to_item(sup, code, item.get("supplier_part_no"))

		if mode == "Request for Quotation":
			rfq_parts.append(item)
			if sup:
				rfq_suppliers.add(sup)
		else:
			if not sup:
				frappe.throw(_("Supplier is required for Purchase Order on item {0}.").format(code))
			if sup not in po_parts_by_supplier:
				po_parts_by_supplier[sup] = []
			po_parts_by_supplier[sup].append(item)

	created_pos = []
	created_rfqs = []

	# 1. Create and submit Purchase Orders grouped by supplier
	from ism.setup import get_or_create_default_purchase_tax_template
	po_tax_template = get_or_create_default_purchase_tax_template(company)

	for sup, items in po_parts_by_supplier.items():
		po = frappe.new_doc("Purchase Order")
		po.company = company
		po.supplier = sup
		po.transaction_date = today()
		po.schedule_date = today()

		if hasattr(po, "repair_order") or frappe.db.has_column("Purchase Order", "repair_order"):
			po.repair_order = ro.name

		for it in items:
			item_doc = frappe.get_cached_doc("Item", it["item_code"])
			rate = flt(it.get("rate") or getattr(item_doc, "standard_rate", 0.0) or getattr(item_doc, "valuation_rate", 0.0))
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
			for t in taxes:
				po.append("taxes", t)

		po.set_missing_values()
		po.calculate_taxes_and_totals()
		po.flags.ignore_permissions = True
		po.insert()
		po.submit()
		created_pos.append(po.name)

	# 2. Create and submit Request for Quotations
	if rfq_parts:
		rfq = frappe.new_doc("Request for Quotation")
		rfq.company = company
		rfq.transaction_date = today()

		if hasattr(rfq, "repair_order") or frappe.db.has_column("Request for Quotation", "repair_order"):
			rfq.repair_order = ro.name

		for s in rfq_suppliers:
			rfq.append("suppliers", {"supplier": s})

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
		rfq.flags.ignore_permissions = True
		rfq.insert()
		rfq.submit()
		created_rfqs.append(rfq.name)

	return {
		"status": "success",
		"purchase_orders": created_pos,
		"rfqs": created_rfqs,
	}


