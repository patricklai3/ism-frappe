# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import csv
import io
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

import frappe
from frappe import _
from frappe.utils import cint, flt, get_url, now_datetime, nowdate, today

from ism.api.approval import get_or_create_approval_token
from ism.api.estimation_service import estimate_repair_order
from ism.api.parts import bulk_import_parts, get_default_warehouse
from ism.api.scheduling import allocate_schedule_entry, get_available_slots
from ism.api.sms import dispatch_sms
from ism.api.technician_availability import get_active_technicians


def _build_agent_envelope(
	success: bool,
	summary_card: str,
	urls: Optional[Dict[str, str]] = None,
	data: Optional[Dict[str, Any]] = None,
	message: Optional[str] = None,
	error: Optional[str] = None,
) -> Dict[str, Any]:
	"""Standardized response envelope for all Agent Facade APIs."""
	payload = {
		"success": success,
		"message": message or ("Operation succeeded" if success else "Operation failed"),
		"summary_card": summary_card,
		"urls": urls or {},
		"data": data or {},
	}
	if error:
		payload["error"] = error
	return payload


def _get_default_company() -> str:
	"""Retrieve active company for shop operations."""
	return (
		frappe.db.get_single_value("Global Defaults", "default_company")
		or frappe.db.get_value("Company", {"is_group": 0}, "name")
		or "Lightspeed"
	)


def _resolve_customer(customer_name: str, phone: Optional[str] = None, email: Optional[str] = None) -> str:
	"""Find an existing Customer or provision a new one safely."""
	if not customer_name:
		customer_name = "Walk-in Customer"

	if frappe.db.exists("Customer", customer_name):
		return customer_name

	existing = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
	if existing:
		return existing

	cust_group = "Individual" if frappe.db.exists("Customer Group", "Individual") else "All Customer Groups"
	territory = "All Territories" if frappe.db.exists("Territory", "All Territories") else frappe.db.get_value("Territory", {}, "name")

	doc = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": customer_name,
		"customer_type": "Individual",
		"customer_group": cust_group,
		"territory": territory,
	}).insert(ignore_permissions=True)
	return doc.name


def _resolve_vehicle(vehicle_identifier: str, customer_name: str) -> str:
	"""Find an existing Repair Vehicle by VIN or Plate, or create one."""
	if not vehicle_identifier:
		vehicle_identifier = f"TEMP{frappe.generate_hash(length=13).upper()}"

	veh = frappe.db.get_value("Repair Vehicle", {"name": vehicle_identifier}, "name")
	if not veh:
		veh = frappe.db.get_value("Repair Vehicle", {"license_plate": vehicle_identifier}, "name")
	if not veh:
		veh = frappe.db.get_value("Repair Vehicle", {"vin": vehicle_identifier}, "name")

	if veh:
		return veh

	is_vin = len(vehicle_identifier) == 17
	vin = vehicle_identifier if is_vin else f"TEMP{frappe.generate_hash(length=13).upper()}"
	plate = "" if is_vin else vehicle_identifier

	v_doc = frappe.get_doc({
		"doctype": "Repair Vehicle",
		"vin": vin,
		"license_plate": plate,
		"customer": customer_name,
		"make": "Tesla",
		"model": "Model 3",
	}).insert(ignore_permissions=True)
	return v_doc.name


@frappe.whitelist()
def create_quote(
	customer_name: str,
	vehicle_identifier: str,
	services: Optional[Union[List[Any], str]] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	customer_phone: Optional[str] = None,
	customer_email: Optional[str] = None,
	company: Optional[str] = None,
	odometer_in: Optional[int] = None,
) -> Dict[str, Any]:
	"""Create a new Repair Order in Draft Estimate state with deterministic totals and estimation."""
	if isinstance(services, str):
		try:
			services = json.loads(services)
		except Exception:
			services = [{"service_title": services}]

	if isinstance(parts, str):
		try:
			parts = json.loads(parts)
		except Exception:
			parts = []

	comp = company or _get_default_company()
	cust = _resolve_customer(customer_name, phone=customer_phone, email=customer_email)
	veh = _resolve_vehicle(vehicle_identifier, cust)

	ro = frappe.new_doc("Repair Order")
	ro.company = comp
	ro.customer = cust
	ro.vehicle = veh
	ro.status = "Draft Estimate"
	ro.approval_status = "Draft"
	ro.customer_concern = notes or ""
	ro.posting_date = today()

	# Resolve odometer_in
	if odometer_in is not None:
		ro.odometer_in = cint(odometer_in)
	else:
		prior_odo = frappe.db.get_value("Repair Order", {"vehicle": veh, "odometer_in": [">", 0]}, "odometer_in", order_by="creation desc")
		ro.odometer_in = cint(prior_odo) if prior_odo else 10000

	default_labor_rate = 140.0
	parsed_services = services or []
	if not parsed_services:
		parsed_services = [{"service_title": "Initial Inspection & Diagnostics", "labor_hours": 1.0}]

	for s in parsed_services:
		if isinstance(s, str):
			s_data = {"service_title": s, "labor_hours": 1.0, "labor_rate": default_labor_rate}
		elif isinstance(s, dict):
			s_data = {
				"service_title": s.get("service_title") or s.get("title") or s.get("name") or "Service Operation",
				"service_package": s.get("service_package"),
				"labor_hours": flt(s.get("labor_hours") or s.get("custom_frt") or 1.0),
				"labor_rate": flt(s.get("labor_rate") or default_labor_rate),
				"is_flat_rate": cint(s.get("is_flat_rate", 0)),
				"service_position": s.get("service_position", "None"),
			}
			if s.get("parts"):
				s_data["parts"] = json.dumps(s["parts"]) if isinstance(s["parts"], (list, dict)) else s["parts"]
		else:
			continue

		# Auto-link Service Package if matched by name or title
		if not s_data.get("service_package"):
			pkg_name = frappe.db.get_value("Service Package", {"name": s_data["service_title"]}, "name")
			if not pkg_name:
				pkg_name = frappe.db.get_value("Service Package", {"package_name": s_data["service_title"]}, "name")
			if pkg_name:
				s_data["service_package"] = pkg_name

		ro.append("services", s_data)

	# If global parts provided, attach to first service
	if parts and ro.services:
		first_s = ro.services[0]
		existing_p = []
		if first_s.parts:
			try:
				existing_p = json.loads(first_s.parts) if isinstance(first_s.parts, str) else first_s.parts
			except Exception:
				existing_p = []
		for p in parts:
			if isinstance(p, dict):
				existing_p.append(p)
		first_s.parts = json.dumps(existing_p)

	ro.insert(ignore_permissions=True)

	# Calculate estimation duration
	try:
		est = estimate_repair_order(ro.name)
		duration_min = est.get("total_duration_minutes", 60)
	except Exception:
		duration_min = int(sum(flt(s.labor_hours) * 60 for s in ro.services)) or 60

	base_url = get_url()
	desk_url = f"{base_url}/app/repair-order/{ro.name}"
	print_url = f"{base_url}/printview?doctype=Repair+Order&name={ro.name}&format=Automotive+Repair+Invoice"

	# Build rich markdown card
	lines = [
		f"### Repair Order Quote #{ro.name}",
		f"**Customer**: `{ro.customer}` | **Vehicle**: `{ro.vehicle}`",
		f"**Status**: `{ro.status}` | **Est. Shop Time**: `{duration_min} mins`",
		"",
		"| Service Operation | Hours | Labor ($) | Parts ($) | Line Total ($) |",
		"| :--- | :---: | :---: | :---: | :---: |",
	]
	for s in ro.services:
		lines.append(
			f"| {s.service_title} | {flt(s.labor_hours):.1f} | ${flt(s.labor_amount):.2f} | ${flt(s.parts_total):.2f} | ${flt(s.total_amount):.2f} |"
		)
	lines.append("")
	lines.append(f"**Subtotal**: `${flt(ro.net_total):,.2f}` | **HST (13%)**: `${flt(ro.total_taxes_and_charges):,.2f}` | **Grand Total**: `${flt(ro.grand_total):,.2f}`")
	lines.append("")
	lines.append(f"[📄 View Legal Print Preview]({print_url})  •  [✏️ Open in Frappe Desk]({desk_url})")

	card = "\n".join(lines)
	urls = {"desk": desk_url, "print": print_url}

	return _build_agent_envelope(
		success=True,
		summary_card=card,
		urls=urls,
		data=ro.as_dict(),
		message=_("Quote {0} created successfully.").format(ro.name),
	)


@frappe.whitelist()
def amend_quote(
	repair_order: str,
	add_services: Optional[Union[List[Any], str]] = None,
	remove_service_indices: Optional[Union[List[int], str]] = None,
	notes: Optional[str] = None,
) -> Dict[str, Any]:
	"""Safely amend lines on an unapproved Repair Order in Draft Estimate state."""
	if not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(_("Repair Order {0} does not exist.").format(repair_order), frappe.DoesNotExistError)

	ro = frappe.get_doc("Repair Order", repair_order)
	if ro.approval_status in ["Approved", "Partially Approved"] or ro.docstatus != 0:
		frappe.throw(_("Cannot amend an approved or submitted Repair Order. Customer re-authorization is legally required."))

	if isinstance(add_services, str):
		try:
			add_services = json.loads(add_services)
		except Exception:
			add_services = [{"service_title": add_services}]

	if isinstance(remove_service_indices, str):
		try:
			remove_service_indices = json.loads(remove_service_indices)
		except Exception:
			remove_service_indices = []

	# Remove services in reverse index order
	if remove_service_indices:
		for idx in sorted([int(i) for i in remove_service_indices], reverse=True):
			if 0 <= idx < len(ro.services):
				ro.services.pop(idx)

	# Add new services
	default_labor_rate = 140.0
	if add_services:
		for s in add_services:
			if isinstance(s, str):
				s_data = {"service_title": s, "labor_hours": 1.0, "labor_rate": default_labor_rate}
			elif isinstance(s, dict):
				s_data = {
					"service_title": s.get("service_title") or s.get("title") or "Service Operation",
					"service_package": s.get("service_package"),
					"labor_hours": flt(s.get("labor_hours") or 1.0),
					"labor_rate": flt(s.get("labor_rate") or default_labor_rate),
					"is_flat_rate": cint(s.get("is_flat_rate", 0)),
					"service_position": s.get("service_position", "None"),
				}
				if s.get("parts"):
					s_data["parts"] = json.dumps(s["parts"]) if isinstance(s["parts"], (list, dict)) else s["parts"]
			else:
				continue
			ro.append("services", s_data)

	if notes:
		ro.customer_concern = notes

	ro.save(ignore_permissions=True)

	base_url = get_url()
	desk_url = f"{base_url}/app/repair-order/{ro.name}"
	print_url = f"{base_url}/printview?doctype=Repair+Order&name={ro.name}&format=Automotive+Repair+Invoice"

	card = (
		f"### Repair Order Quote #{ro.name} (Updated)\n"
		f"**Customer**: `{ro.customer}` | **Vehicle**: `{ro.vehicle}`\n"
		f"**Services Count**: `{len(ro.services)}`\n"
		f"**Subtotal**: `${flt(ro.net_total):,.2f}` | **HST (13%)**: `${flt(ro.total_taxes_and_charges):,.2f}` | **Grand Total**: `${flt(ro.grand_total):,.2f}`\n\n"
		f"[📄 View Legal Print Preview]({print_url})  •  [✏️ Open in Frappe Desk]({desk_url})"
	)
	return _build_agent_envelope(
		success=True,
		summary_card=card,
		urls={"desk": desk_url, "print": print_url},
		data=ro.as_dict(),
		message=_("Quote {0} updated successfully.").format(ro.name),
	)


@frappe.whitelist()
def find_available_slots(
	repair_order: Optional[str] = None,
	duration_minutes: Optional[int] = None,
	start_date: Optional[str] = None,
	days_ahead: int = 5,
	equipment_tags: Optional[Union[List[str], str]] = None,
) -> Dict[str, Any]:
	"""Query dual-resource capacity for available booking slots across shop operating hours."""
	dur = 60
	if duration_minutes:
		dur = max(15, int(duration_minutes))
	elif repair_order and frappe.db.exists("Repair Order", repair_order):
		est = estimate_repair_order(repair_order)
		dur = est.get("total_duration_minutes", 60)

	if isinstance(equipment_tags, str):
		try:
			equipment_tags = json.loads(equipment_tags)
		except Exception:
			equipment_tags = [equipment_tags]

	target_date = start_date or today()
	results = []

	cur_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
	for i in range(max(1, int(days_ahead))):
		date_str = cur_dt.strftime("%Y-%m-%d")
		day_slots = get_available_slots(date=date_str, duration_minutes=dur, required_tags=equipment_tags)
		if day_slots:
			for slot in day_slots[:3]:  # Top 3 slots per day
				results.append({
					"date": date_str,
					"start_time": slot.get("start_time"),
					"available_bays": slot.get("available_bays"),
					"available_technicians": slot.get("available_technicians"),
					"effective_capacity": slot.get("effective_capacity"),
					"bottleneck": slot.get("bottleneck"),
				})
		cur_dt += timedelta(days=1)

	lines = [
		f"### Available Booking Slots ({dur} min duration)",
		"",
		"| Date | Start Time | Available Bays | Available Techs | Bottleneck |",
		"| :--- | :---: | :---: | :---: | :---: |",
	]
	for r in results:
		lines.append(f"| {r.get('date')} | {r.get('start_time')} | {r.get('available_bays')} | {r.get('available_technicians')} | {r.get('bottleneck') or 'None'} |")

	if not results:
		lines.append("| No free slots found in requested range. | — | — | — | Full |")

	card = "\n".join(lines)
	return _build_agent_envelope(
		success=bool(results),
		summary_card=card,
		data={"duration_minutes": dur, "slots": results},
		message=_("Found {0} available slots.").format(len(results)),
	)


@frappe.whitelist()
def book_appointment(
	repair_order: str,
	scheduled_date: str,
	start_time: str,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	notes: Optional[str] = None,
) -> Dict[str, Any]:
	"""Allocate a Schedule Entry for a Repair Order and broadcast ambient realtime update."""
	if not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(_("Repair Order {0} does not exist.").format(repair_order), frappe.DoesNotExistError)

	ro = frappe.get_doc("Repair Order", repair_order)

	# Calculate duration
	try:
		est = estimate_repair_order(ro.name)
		dur = est.get("total_duration_minutes", 60)
	except Exception:
		dur = 60

	entry_dict = allocate_schedule_entry(
		repair_order=ro.name,
		scheduled_date=scheduled_date,
		scheduled_time=start_time,
		estimated_duration=dur,
		service_bay=service_bay,
		assigned_technician=technician,
		notes=notes or ro.customer_concern,
	)

	# Broadcast ambient calendar realtime push
	frappe.publish_realtime(
		"ism_calendar_update",
		{"schedule_entry": entry_dict.get("name"), "bay": entry_dict.get("service_bay"), "date": scheduled_date},
	)

	cal_url = f"{get_url()}/app/schedule-entry/view/calendar"
	se_name = entry_dict.get("name")
	desk_url = f"{get_url()}/app/schedule-entry/{se_name}"

	card = (
		f"### Appointment Confirmed: `{se_name}`\n"
		f"**Repair Order**: `{ro.name}` ({ro.customer})\n"
		f"**Date**: `{scheduled_date}` | **Time**: `{start_time}`\n"
		f"**Bay**: `{entry_dict.get('service_bay')}` | **Technician**: `{entry_dict.get('assigned_technician') or 'Unassigned'}`\n\n"
		f"[📅 View Ambient Shop Calendar]({cal_url})  •  [🔍 Inspect Schedule Entry]({desk_url})"
	)

	return _build_agent_envelope(
		success=True,
		summary_card=card,
		urls={"calendar": cal_url, "desk": desk_url},
		data=entry_dict,
		message=_("Appointment booked successfully."),
	)


@frappe.whitelist()
def send_quote_for_approval(
	repair_order: str,
	communication_medium: str = "sms",
	phone_number: Optional[str] = None,
) -> Dict[str, Any]:
	"""Generate secure approval token and dispatch authorization link to customer."""
	token, approval_url = get_or_create_approval_token(repair_order)
	ro = frappe.get_doc("Repair Order", repair_order)

	phone = phone_number
	if not phone and ro.customer:
		phone = frappe.db.get_value("Customer", ro.customer, "mobile_no")

	dispatch_status = "Generated"
	if communication_medium.lower() == "sms" and phone:
		msg = _("Please review and authorize your repair estimate for {0}: {1}").format(ro.vehicle, approval_url)
		res = dispatch_sms(phone, msg)
		dispatch_status = "Dispatched SMS" if res.get("success") else f"SMS Pending ({res.get('error')})"

	card = (
		f"### Authorization Link Generated for #{ro.name}\n"
		f"**Customer**: `{ro.customer}` | **Expiry**: 48 Hours\n"
		f"**Status**: `{dispatch_status}`\n\n"
		f"**Approval URL**:\n[{approval_url}]({approval_url})\n\n"
		f"When the customer signs, the estimate automatically advances to `Authorized - Ready for Dispatch`."
	)

	return _build_agent_envelope(
		success=True,
		summary_card=card,
		urls={"approval": approval_url},
		data={"token": token, "approval_url": approval_url, "status": dispatch_status},
		message=_("Quote authorization link ready."),
	)


@frappe.whitelist()
def import_supplier_catalog(
	raw_text: Optional[str] = None,
	supplier: Optional[str] = None,
	default_source: str = "Aftermarket",
	default_condition: str = "New",
) -> Dict[str, Any]:
	"""Bulk ingest supplier parts CSV or tabular text directly into Item catalog."""
	if not raw_text:
		frappe.throw(_("CSV text or tabular catalog content is required."))

	result = bulk_import_parts(
		raw_text=raw_text,
		default_source=default_source,
		default_condition=default_condition,
	)

	created_count = len(result.get("created", []))
	updated_count = len(result.get("updated", []))
	error_count = len(result.get("errors", []))

	card = (
		f"### Supplier Catalog Ingestion Complete\n"
		f"**Created Items**: `{created_count}`\n"
		f"**Updated Items**: `{updated_count}`\n"
		f"**Errors**: `{error_count}`\n"
	)
	if error_count > 0:
		card += f"\n> [!WARNING]\n> Some items failed to import: {', '.join(result.get('errors', [])[:3])}"

	return _build_agent_envelope(
		success=True,
		summary_card=card,
		data=result,
		message=_("Ingested {0} parts successfully.").format(created_count + updated_count),
	)


@frappe.whitelist()
def import_tesla_procedure(
	procedure_title: str,
	correction_code: Optional[str] = None,
	labor_hours: float = 1.0,
	model: str = "Model 3",
	parts: Optional[Union[List[Any], str]] = None,
) -> Dict[str, Any]:
	"""Ingest Tesla Service Manual procedure directly into a reusable Service Package."""
	if isinstance(parts, str):
		try:
			parts = json.loads(parts)
		except Exception:
			parts = []

	pkg_name = f"{correction_code} - {procedure_title}" if correction_code else procedure_title
	if frappe.db.exists("Service Package", pkg_name):
		pkg = frappe.get_doc("Service Package", pkg_name)
	else:
		pkg = frappe.new_doc("Service Package")
		pkg.package_name = pkg_name

	pkg.applicable_model = model
	pkg.correction_code = correction_code or ""
	pkg.standard_labor_hours = flt(labor_hours)
	pkg.save(ignore_permissions=True)

	card = (
		f"### Tesla Procedure Ingested: `{pkg.name}`\n"
		f"**Correction Code**: `{correction_code or 'N/A'}` | **Model**: `{model}`\n"
		f"**Flat Rate Time**: `{flt(labor_hours):.2f} hours`\n"
		f"[✏️ Inspect Service Package]({get_url()}/app/service-package/{pkg.name})"
	)

	return _build_agent_envelope(
		success=True,
		summary_card=card,
		urls={"desk": f"{get_url()}/app/service-package/{pkg.name}"},
		data=pkg.as_dict(),
		message=_("Tesla procedure saved as Service Package."),
	)


@frappe.whitelist()
def get_shop_floor_status(date: Optional[str] = None) -> Dict[str, Any]:
	"""Executive dashboard query summarizing bay occupancy, active tech pool, and bottlenecks."""
	target_date = date or today()

	bays = frappe.get_all("Service Bay", filters={"is_active": 1}, fields=["name", "description"])
	techs = get_active_technicians(target_date)
	active_techs = [t for t in techs if not t.get("on_leave")]

	entries = frappe.get_all(
		"Schedule Entry",
		filters={"scheduled_date": target_date},
		fields=["name", "service_bay", "assigned_technician", "scheduled_time", "status", "repair_order"],
	)

	active_ros = frappe.get_all(
		"Repair Order",
		filters={"status": ["in", ["Authorized - In Progress", "Waiting on Parts", "Pending Authorization"]]},
		fields=["name", "customer", "vehicle", "status"],
	)

	lines = [
		f"### Shop Floor Status for `{target_date}`",
		f"**Active Bays**: `{len(bays)}` | **On-Duty Technicians**: `{len(active_techs)}`",
		f"**Scheduled Appointments**: `{len(entries)}` | **Active Shop ROs**: `{len(active_ros)}`",
		"",
		"| Bay | Scheduled Appointments | Status |",
		"| :--- | :---: | :---: |",
	]
	for b in bays:
		b_entries = [e for e in entries if e.get("service_bay") == b.name]
		lines.append(f"| {b.name} | {len(b_entries)} jobs | {'Available' if not b_entries else 'In Use'} |")

	card = "\n".join(lines)
	return _build_agent_envelope(
		success=True,
		summary_card=card,
		data={
			"date": target_date,
			"bays_count": len(bays),
			"technicians_on_duty": len(active_techs),
			"appointments_count": len(entries),
			"active_ros_count": len(active_ros),
		},
	)


@frappe.whitelist()
def update_job_status(
	repair_order: str,
	action: str,
	odometer_out: Optional[int] = None,
	notes: Optional[str] = None,
) -> Dict[str, Any]:
	"""Technician & Advisor 1-tap state transition enforcement with legal authorization locks."""
	if not frappe.db.exists("Repair Order", repair_order):
		frappe.throw(_("Repair Order {0} does not exist.").format(repair_order), frappe.DoesNotExistError)

	ro = frappe.get_doc("Repair Order", repair_order)
	action = action.lower().strip()

	if action == "start":
		if ro.approval_status not in ["Approved", "Partially Approved"]:
			frappe.throw(
				_("Cannot start work on Repair Order {0}: customer authorization is required before bay entry.").format(ro.name),
				frappe.ValidationError,
			)
		ro.status = "Authorized - In Progress"
		ro.has_shop_floor_override = 0
	elif action in ["hold", "parts"]:
		ro.status = "Waiting on Parts"
		ro.has_shop_floor_override = 1
	elif action == "complete":
		ro.status = "Work Completed"
		ro.has_shop_floor_override = 0
		if odometer_out:
			ro.odometer_out = odometer_out
		ro.vehicle_location = "Completed / Parked in Lot"
	else:
		frappe.throw(_("Invalid action {0}. Valid actions are: start, hold, complete.").format(action))

	if notes:
		ro.override_notes = notes

	ro.save(ignore_permissions=True)

	card = (
		f"### Repair Order #{ro.name} Status Updated\n"
		f"**New Status**: `{ro.status}` | **Approval**: `{ro.approval_status}`\n"
		f"**Vehicle Location**: `{ro.vehicle_location or 'Shop Floor'}`"
	)

	return _build_agent_envelope(
		success=True,
		summary_card=card,
		data={"repair_order": ro.name, "status": ro.status, "approval_status": ro.approval_status},
		message=_("Repair Order status updated to {0}.").format(ro.status),
	)
