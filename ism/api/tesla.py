# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import urllib.request
import urllib.error
import frappe
from frappe.utils import flt, cint
from ism.utils.tesla_manual import (
	parse_tesla_procedure_html,
	parse_tesla_manual_url,
	get_tesla_manual_url_for_vehicle,
	validate_vehicle_compatibility,
	get_category_for_correction_code,
)
from ism.ism.doctype.repair_order.repair_order import _get_default_labor_rate


@frappe.whitelist()
def get_active_repair_orders():
	"""Return recent draft/open Repair Orders with vehicle details for the extension and desk UI."""
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
	if not csrf_token and hasattr(frappe.sessions, "get_csrf_token"):
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
	csrf_token = getattr(frappe.local.session.data, "csrf_token", None) if hasattr(frappe.local, "session") else None
	if not csrf_token and hasattr(frappe.sessions, "get_csrf_token"):
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
	if repair_order and not vehicle:
		vehicle = frappe.db.get_value("Repair Order", repair_order, "vehicle")

	if not vehicle:
		return "https://service.tesla.com/docs/"

	vdoc = frappe.get_doc("Repair Vehicle", vehicle)
	return get_tesla_manual_url_for_vehicle(vdoc)


@frappe.whitelist()
def fetch_and_parse_tesla_procedure(url):
	"""Fetch HTML of a Tesla service manual procedure and parse its metadata server-side."""
	if not url:
		frappe.throw("Tesla procedure URL is required.")

	req = urllib.request.Request(
		url,
		headers={
			"User-Agent": (
				"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
				"AppleWebKit/537.36 (KHTML, like Gecko) "
				"Chrome/124.0.0.0 Safari/537.36"
			),
			"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
		},
	)

	try:
		with urllib.request.urlopen(req, timeout=12) as response:
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
	title = title or "Tesla Service Procedure"
	frt = flt(proc_data.get("frt"), 2)

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

	# 4. Check for duplicate on the current Repair Order
	existing_service = None
	if correction_code:
		for s in ro.services:
			if str(getattr(s, "correction_code", "") or "").strip() == correction_code:
				existing_service = s
				break

	if not existing_service and pkg and pkg.name:
		for s in ro.services:
			if s.service_package == pkg.name:
				existing_service = s
				break

	if existing_service and not duplicate_action:
		code_display = correction_code or pkg.name or "this procedure"
		return {
			"status": "duplicate_warning",
			"requires_duplicate_choice": True,
			"message": f"Correction code {code_display} is already on this Repair Order. Update existing row or Add as additional job?",
			"correction_code": correction_code,
			"existing_service_title": existing_service.service_title,
			"existing_idx": existing_service.idx,
			"procedure_data": proc_data,
		}

	# 5. Populate labor rates and append or update
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

	service_category = proc_data.get("category") or getattr(pkg, "category", None)
	if not service_category and correction_code:
		c, _ = get_category_for_correction_code(correction_code)
		if c:
			service_category = c
	service_category = service_category or "Maintenance"

	action_taken = "added"
	if existing_service and duplicate_action == "update":
		action_taken = "updated"
		existing_service.service_title = title
		existing_service.labor_hours = labor_hours
		existing_service.labor_rate = hourly_rate
		existing_service.labor_amount = labor_amount
		existing_service.total_amount = flt(labor_amount + flt(existing_service.parts_total), 2)
		existing_service.tesla_manual_url = proc_url
		existing_service.is_mobile_capable = cint(proc_data.get("is_mobile_capable", getattr(pkg, "is_mobile_capable", 0)))
		if kwargs.get("advisor_notes"):
			existing_service.advisor_notes = kwargs.get("advisor_notes")
		target_service = existing_service
	else:
		target_service = ro.append("services", {
			"service_package": pkg.name,
			"correction_code": correction_code,
			"service_title": title,
			"status": "Estimating",
			"is_flat_rate": 0,
			"labor_hours": labor_hours,
			"labor_rate": hourly_rate,
			"labor_amount": labor_amount,
			"parts_total": 0.0,
			"total_amount": labor_amount,
			"is_supplement": is_supp,
			"is_mobile_capable": cint(proc_data.get("is_mobile_capable", getattr(pkg, "is_mobile_capable", 0))),
			"tesla_manual_url": proc_url,
			"advisor_notes": kwargs.get("advisor_notes", "") or "",
		})

	if is_supp and any(s.status == "Approved" for s in ro.services):
		ro.approval_status = "Partially Approved"

	# Update order-level scheduling flags
	if ro.services:
		ro.is_mobile_capable = 1 if all(cint(s.is_mobile_capable) for s in ro.services) else 0

	# Save Repair Order
	ro.flags.ignore_validate_update_after_submit = True
	ro.save(ignore_permissions=True)

	# Broadcast realtime notification
	frappe.publish_realtime(
		"repair_order_updated",
		{
			"docname": ro.name,
			"service_title": title,
			"correction_code": correction_code,
			"frt": frt,
			"action": action_taken,
			"is_mobile_capable": target_service.is_mobile_capable,
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
		"is_mobile_capable": target_service.is_mobile_capable,
		"service_row": target_service.as_dict() if hasattr(target_service, "as_dict") else None,
	}


def _upsert_service_package(proc_data, proc_url):
	"""Upsert Service Package record based on correction code or package name."""
	correction_code = str(proc_data.get("correction_code") or "").strip()
	title = str(proc_data.get("title") or "Tesla Service Procedure").strip()
	if correction_code:
		import re
		title = re.sub(rf"^{re.escape(correction_code)}\s*[-–—:]?\s*", "", title).strip()
	title = title or "Tesla Service Procedure"

	frt = flt(proc_data.get("frt"), 2)

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
		"tesla_manual_url": proc_url,
		"doc_guid": proc_data.get("doc_guid") or "",
	})
	pkg.insert(ignore_permissions=True)
	return pkg
