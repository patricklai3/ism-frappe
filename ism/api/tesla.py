# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import urllib.request
import urllib.error
import frappe
import frappe.sessions
from frappe.utils import flt, cint
from ism.utils.tesla_manual import (
	parse_tesla_procedure_html,
	parse_tesla_manual_url,
	get_tesla_manual_url_for_vehicle,
	validate_vehicle_compatibility,
	get_category_for_correction_code,
	get_mobile_capability_from_manifest,
	swap_side_in_title,
	detect_service_side,
)
from ism.ism.doctype.repair_order.repair_order import _get_default_labor_rate, _render_parts_html


@frappe.whitelist()
def get_active_repair_orders():
	"""Return recent draft/open Repair Orders with vehicle details for the extension and desk UI."""
	if hasattr(frappe.local, "allow_cors"):
		frappe.local.allow_cors = "*"

	orders = frappe.db.sql(
		"""
		SELECT 
			ro.name, ro.customer, ro.vehicle, ro.status, ro.posting_date, ro.modified,
			rv.model, rv.model_year, rv.manufactured_month, rv.license_plate, rv.vin
		FROM `tabRepair Order` ro
		LEFT JOIN `tabRepair Vehicle` rv ON ro.vehicle = rv.name
		WHERE ro.docstatus < 2 AND ro.status NOT IN ('Closed', 'Cancelled')
		ORDER BY ro.modified DESC
		LIMIT 20
		""",
		as_dict=True,
	)

	result = []
	for row in orders:
		manual_url = get_tesla_manual_url_for_vehicle(row)
		result.append({
			"name": row.name,
			"customer": row.customer,
			"vehicle": row.vehicle,
			"status": row.status,
			"model": row.model or "",
			"model_year": row.model_year or "",
			"manufactured_month": row.manufactured_month or "",
			"license_plate": row.license_plate or "",
			"vin": row.vin or "",
			"manual_url": manual_url,
		})

	csrf_token = getattr(frappe.local.session.data, "csrf_token", None) if hasattr(frappe.local, "session") else None
	if not csrf_token:
		try:
			csrf_token = frappe.sessions.get_csrf_token()
		except Exception:
			pass

	return {
		"orders": result,
		"csrf_token": csrf_token or "",
	}


@frappe.whitelist()
def get_session_info():
	"""Return active session info and CSRF token for browser extension authorization."""
	if hasattr(frappe.local, "allow_cors"):
		frappe.local.allow_cors = "*"

	csrf_token = getattr(frappe.local.session.data, "csrf_token", None) if hasattr(frappe.local, "session") else None
	if not csrf_token:
		try:
			csrf_token = frappe.sessions.get_csrf_token()
		except Exception:
			pass

	return {
		"csrf_token": csrf_token or "",
		"user": frappe.session.user,
	}


@frappe.whitelist()
def get_vehicle_manual_url(vehicle=None, repair_order=None):
	"""Return the recommended Tesla Service Manual URL for a vehicle or repair order."""
	if hasattr(frappe.local, "allow_cors"):
		frappe.local.allow_cors = "*"

	if repair_order and not vehicle:
		vehicle = frappe.db.get_value("Repair Order", repair_order, "vehicle")

	if not vehicle:
		return "https://service.tesla.com/docs/"

	vdoc = frappe.get_doc("Repair Vehicle", vehicle)
	return get_tesla_manual_url_for_vehicle(vdoc)


@frappe.whitelist()
def fetch_and_parse_tesla_procedure(url):
	"""Fetch HTML of a Tesla service manual procedure and parse its metadata server-side."""
	if hasattr(frappe.local, "allow_cors"):
		frappe.local.allow_cors = "*"

	if not url:
		frappe.throw("Tesla procedure URL is required.")

	headers = {
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
		"Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
		"Accept-Language": "en-US,en;q=0.9",
		"Referer": "https://service.tesla.com/",
	}
	req = urllib.request.Request(url, headers=headers)

	try:
		with urllib.request.urlopen(req, timeout=15) as response:
			html_content = response.read().decode("utf-8", errors="ignore")
	except Exception:
		# Fallback retry with generic browser agent
		try:
			fallback_req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
			with urllib.request.urlopen(fallback_req, timeout=15) as response:
				html_content = response.read().decode("utf-8", errors="ignore")
		except urllib.error.URLError as e:
			frappe.throw(f"Unable to fetch Tesla service manual page: {e.reason}")
		except Exception as e:
			frappe.throw(f"Failed to fetch page: {str(e)}")

	parsed = parse_tesla_procedure_html(html_content, url=url)
	if not parsed.get("title") and not parsed.get("correction_code"):
		frappe.throw("Could not find a valid Tesla procedure on this page. Please verify the URL.")

	return parsed


@frappe.whitelist()
def import_tesla_procedure(repair_order, url=None, data=None, allow_generation_mismatch=False, duplicate_action=None, is_supplement=0, **kwargs):
	"""Import a Tesla service manual procedure into a Repair Order.

	Accepts either:
	- data: JSON string or dict containing extracted procedure fields (from extension)
	- url: Tesla manual URL to fetch and parse server-side
	- duplicate_action: None (warn if exists), 'update' (update existing row), or 'add' (append additional row)
	"""
	if hasattr(frappe.local, "allow_cors"):
		frappe.local.allow_cors = "*"

	if not repair_order:
		frappe.throw("Repair Order is required.")

	if not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(f"Repair Order '{repair_order}' does not exist.")

	ro = frappe.get_doc("Repair Order", repair_order)

	# 1. Parse or retrieve procedure data
	proc_data = {}
	if data:
		if isinstance(data, str):
			try:
				proc_data = json.loads(data)
			except Exception:
				proc_data = {}
		elif isinstance(data, dict):
			proc_data = data

	if not proc_data and url:
		proc_data = fetch_and_parse_tesla_procedure(url)

	if not proc_data or (not proc_data.get("title") and not proc_data.get("correction_code")):
		frappe.throw("Incomplete procedure data. Title or Correction Code required.")

	proc_url = proc_data.get("tesla_manual_url") or url or ""
	correction_code = str(proc_data.get("correction_code") or "").strip()
	title = str(proc_data.get("title") or "Tesla Service Procedure").strip()
	if correction_code:
		import re
		title = re.sub(rf"^{re.escape(correction_code)}\s*[-–—:]?\s*", "", title).strip()
	frt = flt(proc_data.get("frt"), 2)

	# Ensure mobile capability and symmetrical info are detected from manifest if not yet confirmed
	if proc_url:
		dep_info = get_mobile_capability_from_manifest(
			proc_url,
			proc_data.get("doc_guid"),
			correction_code,
		)
		if dep_info:
			if not cint(proc_data.get("is_mobile_capable")) and dep_info.get("is_mobile_capable"):
				proc_data["is_mobile_capable"] = dep_info["is_mobile_capable"]
				proc_data["mobile_service_level"] = dep_info["mobile_service_level"]
			if not proc_data.get("rh_correction_code") and dep_info.get("rh_correction_code"):
				proc_data["rh_correction_code"] = dep_info["rh_correction_code"]
			if not proc_data.get("is_symmetrical") and dep_info.get("is_symmetrical"):
				proc_data["is_symmetrical"] = 1

	if not proc_data.get("is_symmetrical") and title:
		import re
		if re.search(r'\bLH\b|\bRH\b|Left Hand|Right Hand', title, re.IGNORECASE):
			proc_data["is_symmetrical"] = 1

	# 2. Compatibility check with vehicle
	v_doc = frappe.get_doc("Repair Vehicle", ro.vehicle) if ro.vehicle else None
	allow_mismatch = cint(allow_generation_mismatch)
	compat = validate_vehicle_compatibility(v_doc, proc_url)
	if not compat.get("is_compatible") and not allow_mismatch:
		return {
			"status": "warning",
			"requires_confirmation": True,
			"warning": compat.get("warning"),
			"procedure_data": proc_data,
			"vehicle_desc": compat.get("vehicle_desc"),
			"procedure_gen": compat.get("procedure_gen"),
		}

	# 3. Upsert Service Package in catalog
	pkg = _upsert_service_package(proc_data, proc_url)

	# 4. Determine sides/positions to add
	side_param = str(kwargs.get("side") or proc_data.get("side") or "").strip().lower()
	is_symmetrical = cint(proc_data.get("is_symmetrical") or (1 if pkg and pkg.is_symmetrical else 0))
	rh_code = proc_data.get("rh_correction_code") or (getattr(pkg, "rh_correction_code", None) if pkg else None) or correction_code

	operations_to_add = []
	if side_param == "both" and is_symmetrical:
		operations_to_add.append({
			"side": "LH",
			"title": title,
			"code": correction_code,
		})
		operations_to_add.append({
			"side": "RH",
			"title": swap_side_in_title(title, "RH"),
			"code": rh_code,
		})
	elif side_param == "rh" and is_symmetrical:
		operations_to_add.append({
			"side": "RH",
			"title": swap_side_in_title(title, "RH"),
			"code": rh_code,
		})
	else:
		pos = "LH" if is_symmetrical else "None"
		operations_to_add.append({
			"side": pos,
			"title": title,
			"code": correction_code,
		})

	has_prior_authorization = bool(
		ro.authorized_at
		or any(s.status == "Approved" for s in ro.services)
		or ro.approval_status in ["Approved", "Partially Approved"]
		or ro.status in ["Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid"]
	)
	is_supp = 1 if (cint(is_supplement) or cint(proc_data.get("is_supplement", 0)) or cint(kwargs.get("is_supplement", 0)) or has_prior_authorization) else 0
	hourly_rate = flt(pkg.hourly_rate) or _get_default_labor_rate()
	labor_hours = frt or flt(pkg.standard_labor_hours) or 1.0
	labor_amount = flt(labor_hours * hourly_rate, 2)

	action_taken = "added"
	added_rows = []

	for op in operations_to_add:
		op_side = op["side"]
		op_title = op["title"]
		op_code = op["code"]

		# Check for duplicate on the current Repair Order with position awareness
		existing_service = None
		for s in ro.services:
			s_code = str(getattr(s, "correction_code", "") or "").strip()
			s_pos = str(getattr(s, "service_position", "") or "None").strip()
			s_pkg = str(getattr(s, "service_package", "") or "").strip()

			pos_matches = (s_pos == op_side) or (s_pos in ("None", "") and op_side in ("None", ""))
			code_or_pkg_matches = (op_code and s_code == op_code) or (pkg and s_pkg == pkg.name)
			if pos_matches and code_or_pkg_matches:
				existing_service = s
				break

		if existing_service and not duplicate_action:
			code_display = op_code or pkg.name or "this procedure"
			side_str = f" ({op_side})" if op_side and op_side != "None" else ""
			return {
				"status": "duplicate_warning",
				"requires_duplicate_choice": True,
				"message": f"Correction code {code_display}{side_str} is already on this Repair Order. Update existing row or Add as additional job?",
				"correction_code": op_code,
				"existing_service_title": existing_service.service_title,
				"existing_idx": existing_service.idx,
				"procedure_data": proc_data,
			}

		# Extract nested parts for this specific side
		parts_list, parts_total = _extract_parts_for_side(pkg, op_side)

		if existing_service and duplicate_action == "update":
			action_taken = "updated"
			existing_service.service_title = op_title
			existing_service.service_position = op_side
			existing_service.correction_code = op_code
			existing_service.labor_hours = labor_hours
			existing_service.labor_rate = hourly_rate
			existing_service.labor_amount = labor_amount
			if parts_list:
				existing_service.parts = json.dumps(parts_list)
				existing_service.parts_total = parts_total
				existing_service.parts_html = _render_parts_html(parts_list)
			existing_service.total_amount = flt(labor_amount + flt(existing_service.parts_total), 2)
			existing_service.tesla_manual_url = proc_url
			existing_service.is_mobile_capable = cint(proc_data.get("is_mobile_capable", getattr(pkg, "is_mobile_capable", 0)))
			if kwargs.get("advisor_notes"):
				existing_service.advisor_notes = kwargs.get("advisor_notes")
			added_rows.append(existing_service)
		else:
			target_service = ro.append("services", {
				"service_package": pkg.name,
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
				"parts_html": _render_parts_html(parts_list) if parts_list else "",
				"is_supplement": is_supp,
				"is_mobile_capable": cint(proc_data.get("is_mobile_capable", getattr(pkg, "is_mobile_capable", 0))),
				"tesla_manual_url": proc_url,
				"advisor_notes": kwargs.get("advisor_notes", "") or "",
			})
			added_rows.append(target_service)

	if is_supp and any(s.status == "Approved" for s in ro.services):
		ro.approval_status = "Partially Approved"

	# Update order-level scheduling flags
	if ro.services:
		ro.is_mobile_capable = 1 if all(cint(s.is_mobile_capable) for s in ro.services) else 0

	# Save Repair Order
	ro.flags.ignore_validate_update_after_submit = True
	ro.save(ignore_permissions=True)

	last_row = added_rows[-1] if added_rows else None

	# Broadcast realtime notification
	frappe.publish_realtime(
		"repair_order_updated",
		{
			"docname": ro.name,
			"service_title": title,
			"correction_code": correction_code,
			"frt": frt,
			"action": action_taken,
			"is_mobile_capable": last_row.is_mobile_capable if last_row else 0,
		},
		doctype="Repair Order",
		docname=ro.name,
	)

	return {
		"status": "success",
		"action": action_taken,
		"message": f"Successfully {action_taken} '{title}' on {ro.name}",
		"repair_order": ro.name,
		"service_package": pkg.name,
		"correction_code": correction_code,
		"frt": frt,
		"labor_amount": labor_amount,
		"is_mobile_capable": last_row.is_mobile_capable if last_row else 0,
		"service_row": last_row.as_dict() if hasattr(last_row, "as_dict") else None,
		"count_added": len(added_rows),
	}


def _extract_parts_for_side(pkg, side="Both"):
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
			rate = flt(row.rate)
			amt = flt(qty * rate, 2)
			parts_total += amt
			parts_list.append({
				"item_code": row.item_code,
				"item_name": row.item_name or item_doc.item_name,
				"qty": qty,
				"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
				"rate": rate,
				"amount": amt,
				"warehouse": frappe.db.get_single_value("Stock Settings", "default_warehouse") or "Stores - ISM",
			})

	return parts_list, parts_total


def _upsert_service_package(proc_data, proc_url):
	"""Upsert Service Package record based on correction code or package name."""
	correction_code = str(proc_data.get("correction_code") or "").strip()
	title = str(proc_data.get("title") or "Tesla Service Procedure").strip()
	if correction_code:
		import re
		title = re.sub(rf"^{re.escape(correction_code)}\s*[-–—:]?\s*", "", title).strip()
	title = title or "Tesla Service Procedure"

	frt = flt(proc_data.get("frt"), 2)

	# Ensure mobile capability is detected from manifest if not yet confirmed
	if not cint(proc_data.get("is_mobile_capable")) and proc_url:
		dep_info = get_mobile_capability_from_manifest(
			proc_url,
			proc_data.get("doc_guid"),
			correction_code,
		)
		if dep_info and dep_info.get("is_mobile_capable"):
			proc_data["is_mobile_capable"] = dep_info["is_mobile_capable"]
			proc_data["mobile_service_level"] = dep_info["mobile_service_level"]
		if dep_info and dep_info.get("rh_correction_code") and not proc_data.get("rh_correction_code"):
			proc_data["rh_correction_code"] = dep_info["rh_correction_code"]
		if dep_info and dep_info.get("is_symmetrical") and not proc_data.get("is_symmetrical"):
			proc_data["is_symmetrical"] = 1

	if not proc_data.get("is_symmetrical") and title:
		import re
		if re.search(r'\bLH\b|\bRH\b|Left Hand|Right Hand', title, re.IGNORECASE):
			proc_data["is_symmetrical"] = 1

	# Check for existing Service Package with matching correction_code
	existing_pkg_name = None
	if correction_code:
		existing_pkg_name = frappe.db.get_value("Service Package", {"correction_code": correction_code}, "name")

	pkg_name = title

	if not existing_pkg_name and pkg_name:
		existing_pkg_name = frappe.db.get_value("Service Package", {"package_name": pkg_name}, "name")

	# If found, update existing
	if existing_pkg_name:
		pkg = frappe.get_doc("Service Package", existing_pkg_name)
		if correction_code and pkg.package_name and pkg.package_name.startswith(correction_code):
			pkg.package_name = title
		if frt > 0:
			pkg.standard_labor_hours = frt
		if proc_url:
			pkg.tesla_manual_url = proc_url
		if proc_data.get("doc_guid"):
			pkg.doc_guid = proc_data.get("doc_guid")
		if "is_mobile_capable" in proc_data:
			pkg.is_mobile_capable = cint(proc_data.get("is_mobile_capable"))
		if "is_symmetrical" in proc_data:
			pkg.is_symmetrical = cint(proc_data.get("is_symmetrical"))
		if proc_data.get("rh_correction_code"):
			pkg.rh_correction_code = proc_data.get("rh_correction_code")
		pkg.save(ignore_permissions=True)
		return pkg

	# Otherwise create new package
	pkg = frappe.get_doc({
		"doctype": "Service Package",
		"package_name": pkg_name,
		"correction_code": correction_code,
		"standard_labor_hours": frt or 1.0,
		"is_flat_rate": 0,
		"is_mobile_capable": cint(proc_data.get("is_mobile_capable", 0)),
		"is_symmetrical": cint(proc_data.get("is_symmetrical", 0)),
		"rh_correction_code": proc_data.get("rh_correction_code") or "",
		"tesla_manual_url": proc_url,
		"doc_guid": proc_data.get("doc_guid") or "",
	})
	pkg.insert(ignore_permissions=True)
	return pkg
