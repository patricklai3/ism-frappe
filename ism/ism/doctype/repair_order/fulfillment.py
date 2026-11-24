# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.database.query import Engine
from frappe.utils import cint, flt, now_datetime, time_diff_in_hours, today

from ism.api._validation import escape_like
from ism.api.parts import _resolve_default_warehouse as get_default_warehouse
from ism.api.parts import get_items_available_stock_batch
from ism.api.sms import dispatch_sms


def link_ro_vehicle_to_customer(ro) -> None:
	"""Link repair order customer to vehicle if not already associated."""
	if not (ro.vehicle and ro.customer):
		return
	if frappe.db.get_value("Repair Vehicle", ro.vehicle, "customer") == ro.customer:
		return
	if frappe.db.exists("Repair Vehicle Customer", {"parent": ro.vehicle, "customer": ro.customer}):
		return
	if frappe.db.exists("Repair Vehicle", ro.vehicle):
		vehicle_doc = frappe.get_doc("Repair Vehicle", ro.vehicle)
		vehicle_doc.add_customer(ro.customer)


def sync_declined_to_deferred_services(ro) -> None:
	"""Log declined services into Vehicle Deferred Service for future follow-up (runs on_update)."""
	if not ro.vehicle:
		return

	declined = [s for s in ro.services if s.status == "Declined"]
	if not declined:
		return

	existing_titles = set(
		frappe.get_all(
			"Vehicle Deferred Service",
			filters={"parent": ro.vehicle, "repair_order": ro.name},
			pluck="job_title",
		)
	)
	to_add = [s for s in declined if s.service_title not in existing_titles]
	if not to_add:
		return

	veh_doc = frappe.get_doc("Repair Vehicle", ro.vehicle)
	for s in to_add:
		reason = (s.advisor_notes or "").strip()
		if not reason or reason.startswith("Correction code") or "NOTE: Unless otherwise explicitly stated" in reason:
			reason = _("Declined during customer authorization")
		veh_doc.append("deferred_services", {
			"job_title": s.service_title,
			"decline_date": today(),
			"estimated_amount": s.total_amount,
			"repair_order": ro.name,
			"decline_reason": reason,
		})
	if not veh_doc.color or not veh_doc.license_plate:
		veh_doc.flags.allow_legacy_missing_intake_fields = True
		veh_doc.flags.ignore_mandatory = True
	veh_doc.save(ignore_permissions=True)


def sync_completed_deferred_services(ro) -> None:
	"""Mark previously deferred services on this vehicle as Completed Later once completed (runs on_update)."""
	if not ro.vehicle:
		return

	completed_titles = sorted({s.service_title for s in ro.services if s.is_completed and s.service_title})
	if not completed_titles:
		return

	vds = frappe.qb.DocType("Vehicle Deferred Service")
	(
		frappe.qb.update(vds)
		.set(vds.status, "Completed Later")
		.where(
			(vds.parent == ro.vehicle)
			& (vds.job_title.isin(completed_titles))
			& (vds.status == "Open Recommendation")
		)
	).run()


def check_ro_parts_stock_availability(ro) -> tuple[bool, List[Dict[str, Any]]]:
	"""Check stock availability for all parts in approved services. Returns (is_all_available, deficit_items)."""
	approved_services = [s for s in ro.services if s.status == "Approved"]
	if not approved_services:
		return True, []

	wh = get_default_warehouse(ro.company)
	deficit_items: List[Dict[str, Any]] = []
	raw_item_reqs: Dict[str, float] = {}
	for s in approved_services:
		parts = s.parts
		if isinstance(parts, str):
			try:
				parts = json.loads(parts) if parts else []
			except Exception:
				parts = []
		for p in parts or []:
			if not isinstance(p, dict):
				continue
			code = p.get("item_code")
			if code:
				raw_item_reqs[code] = raw_item_reqs.get(code, 0.0) + flt(p.get("qty", 1.0))

	if not raw_item_reqs:
		return True, []

	stock_items = set(
		frappe.get_all(
			"Item",
			filters={"name": ["in", list(raw_item_reqs.keys())], "is_stock_item": 1},
			pluck="name",
		)
	)
	item_reqs = {code: qty for code, qty in raw_item_reqs.items() if code in stock_items}
	if not item_reqs:
		return True, []

	avail_map = get_items_available_stock_batch(list(item_reqs.keys()), wh, ro.name)
	for code, req_qty in item_reqs.items():
		avail_info = avail_map.get(code, {})
		avail_qty = flt(avail_info.get("available_qty", 0.0))
		if avail_qty < req_qty:
			deficit_items.append({
				"item_code": code,
				"required_qty": req_qty,
				"available_qty": avail_qty,
				"actual_qty": flt(avail_info.get("actual_qty", 0.0)),
				"committed_qty": flt(avail_info.get("committed_qty", 0.0)),
				"deficit": req_qty - avail_qty,
			})

	return len(deficit_items) == 0, deficit_items


def update_parts_waiting_status(ro) -> None:
	"""Update status between 'Waiting on Parts' and 'Authorized - In Progress' based on inventory."""
	if getattr(ro, "has_shop_floor_override", 0) or getattr(ro, "manual_parts_hold", 0):
		return
	if ro.approval_status in ["Approved", "Partially Approved"]:
		if ro.status in [
			"Draft Estimate",
			"Pending Authorization",
			"Waiting on Parts",
			"Authorized - In Progress",
			"",
			None,
		]:
			all_available, _ = ro._check_parts_stock_availability()
			ro.status = "Authorized - In Progress" if all_available else "Waiting on Parts"


def handle_cancel_check(ro, persist_direct: bool = False) -> None:
	"""Validate cancellation guards and update status consistently."""
	if ro.sales_invoice:
		si_docstatus = frappe.db.get_value("Sales Invoice", ro.sales_invoice, "docstatus")
		if si_docstatus == 1:
			frappe.throw(
				_(
					"Cannot cancel Repair Order while active Sales Invoice {0} exists. "
					"Please cancel the Sales Invoice first."
				).format(ro.sales_invoice)
			)
	ro.status = "Cancelled"
	if persist_direct and ro.name and not ro.is_new() and frappe.db.exists("Repair Order", ro.name):
		ro.db_set("status", "Cancelled")


def sync_ro_schedule_entries(ro, exclude_entry=None) -> None:
	"""Sync informational schedule entries child table with linked Schedule Entry records."""
	if not getattr(ro.flags, "ignore_permissions", False):
		ro.check_permission("read")
	if not ro.name or not frappe.db.exists("Repair Order", ro.name):
		return
	from ism.ism.doctype.schedule_entry.schedule_entry import sync_repair_order_schedule_entries

	sync_repair_order_schedule_entries(ro.name, exclude_entry=exclude_entry)
	rows = frappe.get_all(
		"Repair Order Schedule Entry",
		filters={"parent": ro.name, "parenttype": "Repair Order"},
		fields=[
			"schedule_entry",
			"entry_type",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
			"service_bay",
			"assigned_technician",
			"status",
		],
		order_by="idx asc",
	)
	ro.set("schedule_entries", rows)


def _as_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return False


def _parse_and_validate_odometer_out(ro, odometer_out: Any, *, required: bool) -> Optional[int]:
	"""Validate and parse odometer_out against ro.odometer_in."""
	is_blank = odometer_out is None or (isinstance(odometer_out, str) and not odometer_out.strip())
	if is_blank:
		if required:
			frappe.throw(
				_("Odometer Out is mandatory when parking the vehicle in the lot."),
				frappe.MandatoryError,
			)
		return None

	if isinstance(odometer_out, bool):
		frappe.throw(_("Odometer Out must be a valid integer."), frappe.ValidationError)

	if isinstance(odometer_out, int):
		parsed_odo = odometer_out
	elif isinstance(odometer_out, float):
		if not odometer_out.is_integer():
			frappe.throw(_("Odometer Out must be a whole number."), frappe.ValidationError)
		parsed_odo = int(odometer_out)
	elif isinstance(odometer_out, str):
		raw = odometer_out.strip()
		try:
			num = float(raw)
		except ValueError:
			frappe.throw(_("Odometer Out must be a valid integer."), frappe.ValidationError)
		if not num.is_integer():
			frappe.throw(_("Odometer Out must be a whole number."), frappe.ValidationError)
		parsed_odo = int(num)
	else:
		frappe.throw(_("Odometer Out must be a valid integer."), frappe.ValidationError)

	if parsed_odo < 0:
		frappe.throw(_("Odometer Out cannot be negative."), frappe.ValidationError)

	odo_in = cint(ro.odometer_in or 0)
	if parsed_odo < odo_in:
		frappe.throw(
			_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(parsed_odo, odo_in),
			frappe.ValidationError,
		)

	return parsed_odo


def _publish_ro_execution_updated(ro, action: str) -> None:
	"""Emit targeted ism_ro_execution_updated realtime event to authorized recipients only."""
	from ism.permissions import get_realtime_recipients_for_ro

	approved_services = [s for s in (ro.services or []) if s.status == "Approved"]
	completed_count = sum(1 for s in approved_services if cint(s.is_completed))
	payload = {
		"repair_order": ro.name,
		"action": action,
		"status": ro.status,
		"vehicle_location": ro.vehicle_location,
		"progress_percent": flt(ro.progress_percent or 0.0, 1),
		"approved_count": len(approved_services),
		"completed_count": completed_count,
		"odometer_in": cint(ro.odometer_in or 0),
		"odometer_out": cint(ro.odometer_out or 0) if ro.odometer_out is not None and ro.odometer_out != "" else None,
		"picked_up_at": str(ro.picked_up_at) if ro.picked_up_at else None,
		"parked_at": str(ro.parked_at) if ro.parked_at else None,
		"total_bay_hours": flt(ro.total_bay_hours or 0.0, 2),
	}
	for recipient in get_realtime_recipients_for_ro(ro.name):
		frappe.publish_realtime(
			"ism_ro_execution_updated",
			payload,
			user=recipient,
			after_commit=True,
		)


def pick_up_ro_vehicle(ro) -> Dict[str, Any]:
	"""Technician pulls vehicle from lot into service bay."""
	ro.check_permission("write")
	has_approved = any(s.status == "Approved" for s in ro.services) or ro.approval_status == "Approved"
	if ro.approval_status not in ["Approved", "Partially Approved"] or not has_approved:
		frappe.throw(
			_(
				"Vehicle cannot be picked up into the bay without customer authorization on file. "
				"Please obtain customer approval first."
			)
		)

	if ro.status == "Waiting on Parts":
		all_avail, _stock_details = ro._check_parts_stock_availability()
		if not all_avail:
			frappe.throw(
				_(
					"Cannot pick up vehicle into bay: Required parts have zero or insufficient stock in system. "
					"Please procure and receive all required parts into inventory first."
				)
			)

	ro.picked_up_at = now_datetime()
	ro.picked_up_by = frappe.session.user
	ro.vehicle_location = "In Bay / Being Serviced"
	if ro.status in ["Draft Estimate", "Pending Authorization", "Waiting on Parts"]:
		ro.status = "Authorized - In Progress"

	ro.save()
	_publish_ro_execution_updated(ro, "pick_up")
	return {"status": "success", "vehicle_location": ro.vehicle_location, "picked_up_at": ro.picked_up_at}


def park_ro_vehicle(ro, odometer_out=None, require_odometer=False, temporary=False) -> Dict[str, Any]:
	"""Technician parks vehicle back in lot (final drop-off or temporary mid-job park)."""
	ro.check_permission("write")
	parsed_odo = _parse_and_validate_odometer_out(
		ro,
		odometer_out,
		required=_as_bool(require_odometer),
	)
	is_temporary = _as_bool(temporary)

	prev_status = ro.status
	ro.parked_at = now_datetime()
	ro.parked_by = frappe.session.user
	if parsed_odo is not None:
		ro.odometer_out = parsed_odo
	if ro.picked_up_at:
		ro.total_bay_hours = flt(time_diff_in_hours(ro.parked_at, ro.picked_up_at), 2)

	has_approved = any(s.status == "Approved" for s in ro.services)
	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in ro.services)
	approved_services = [s for s in ro.services if s.status == "Approved"]
	all_approved_completed = bool(
		not is_temporary
		and has_approved
		and not has_pending
		and all(s.is_completed for s in approved_services)
	)

	if all_approved_completed:
		ro.vehicle_location = "Completed / Parked in Lot"
		ro.status = "Work Completed"
	else:
		ro.vehicle_location = "In Customer Lot"
		if ro.status not in ["Authorized - In Progress", "Waiting on Parts"]:
			ro.status = "Authorized - In Progress"

	ro.save()

	if not is_temporary and prev_status != "Work Completed" and ro.status == "Work Completed":
		complete_linked_schedule_entries(ro.name, ignore_permissions=True)
		_maybe_send_ready_for_pickup_sms(ro)
	elif (is_temporary or ro.status != "Work Completed") and parsed_odo is not None:
		ro.add_comment(
			"Comment",
			_("Temporary lot park recorded by {0} (Odometer Out: {1} km).").format(
				frappe.session.user,
				parsed_odo,
			),
		)

	_publish_ro_execution_updated(
		ro,
		"park_complete" if (not is_temporary and ro.status == "Work Completed") else "park_temporary",
	)
	return {
		"status": "success",
		"vehicle_location": ro.vehicle_location,
		"total_bay_hours": ro.total_bay_hours,
		"odometer_out": ro.odometer_out,
		"order_status": ro.status,
	}


def toggle_ro_service_completion(ro, service_row_id=None, is_completed=None, **kwargs) -> Dict[str, Any]:
	"""Toggle individual service completion with timestamping."""
	ro.check_permission("write")
	prev_status = ro.status
	if isinstance(service_row_id, dict):
		is_completed = service_row_id.get("is_completed", is_completed)
		service_row_id = service_row_id.get("service_row_id") or service_row_id.get("job_id")
	service_row_id = service_row_id or kwargs.get("service_row_id")
	if is_completed is None:
		is_completed = kwargs.get("is_completed", 0)

	target_service = next(
		(
			s
			for s in ro.services
			if str(s.name) == str(service_row_id)
			or str(s.idx) == str(service_row_id)
			or str(s.service_title) == str(service_row_id)
		),
		None,
	)
	if not target_service:
		frappe.throw(_("Service {0} not found.").format(service_row_id))

	if cint(is_completed) and target_service.status != "Approved":
		frappe.throw(
			_(
				"Service '{0}' cannot be completed because its status is '{1}'. "
				"Only Approved services can be completed by technicians."
			).format(target_service.service_title, target_service.status)
		)

	target_service.is_completed = 1 if cint(is_completed) else 0
	if target_service.is_completed:
		target_service.completed_at = now_datetime()
		target_service.completed_by = frappe.session.user
		if ro.picked_up_at:
			target_service.actual_duration_hours = flt(time_diff_in_hours(target_service.completed_at, ro.picked_up_at), 2)
	else:
		target_service.completed_at = None
		target_service.completed_by = None
		target_service.actual_duration_hours = 0.0

	ro._update_progress_and_job_timing()

	has_approved = any(s.status == "Approved" for s in ro.services)
	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in ro.services)
	approved_services = [s for s in ro.services if s.status == "Approved"]
	all_approved_completed = bool(has_approved and not has_pending and all(s.is_completed for s in approved_services))

	if all_approved_completed:
		if ro.vehicle_location == "Completed / Parked in Lot":
			ro.status = "Work Completed"
		elif ro.status not in ["Authorized - In Progress", "Waiting on Parts"]:
			ro.status = "Authorized - In Progress"
	elif ro.status == "Work Completed" and not all_approved_completed:
		ro.status = "Authorized - In Progress"
		if ro.vehicle_location == "Completed / Parked in Lot":
			ro.vehicle_location = "In Bay / Being Serviced" if ro.picked_up_at else "In Customer Lot"

	ro.save()
	if prev_status != "Work Completed" and ro.status == "Work Completed":
		complete_linked_schedule_entries(ro.name, ignore_permissions=True)
		_maybe_send_ready_for_pickup_sms(ro)
	_publish_ro_execution_updated(ro, "toggle_service")
	return {"status": "success", "progress_percent": ro.progress_percent}


def complete_linked_schedule_entries(ro_name, ignore_permissions: bool = False) -> List[str]:
	"""Mark non-cancelled Schedule Entries for a completed/released Repair Order as Completed."""
	if not ro_name or not frappe.db.exists("DocType", "Schedule Entry"):
		return []
	if not ignore_permissions:
		frappe.has_permission("Schedule Entry", "write", throw=True)
		open_entries = frappe.get_list(
			"Schedule Entry",
			filters={"repair_order": ro_name, "status": ["in", ["Draft", "Scheduled", "Confirmed", "In Progress"]]},
			pluck="name",
		)
	else:
		open_entries = frappe.get_all(
			"Schedule Entry",
			filters={"repair_order": ro_name, "status": ["in", ["Draft", "Scheduled", "Confirmed", "In Progress"]]},
			pluck="name",
		)
	if not open_entries:
		return []

	frappe.db.set_value("Schedule Entry", {"name": ["in", open_entries]}, "status", "Completed")
	from ism.ism.doctype.schedule_entry.schedule_entry import sync_repair_order_schedule_entries

	sync_repair_order_schedule_entries(ro_name)
	frappe.publish_realtime(
		"ism_calendar_update",
		{"repair_order": ro_name, "schedule_entries": list(open_entries), "action": "bulk_complete"},
		after_commit=True,
	)
	return open_entries


def query_filtered_vehicles(doctype, txt, searchfield, start, page_len, filters, as_dict=False, **kwargs):
	"""Query permitted Repair Vehicles filtered by customer and escaped search text."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	if txt is None:
		txt = ""
	elif not isinstance(txt, str):
		frappe.throw(_("Search text must be a string."), frappe.ValidationError)

	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	if filters is not None and not isinstance(filters, dict):
		frappe.throw(_("Filters must be a JSON object."), frappe.ValidationError)

	customer = filters.get("customer") if filters else None
	if customer is not None:
		if not isinstance(customer, str):
			frappe.throw(_("Customer filter must be a string."), frappe.ValidationError)
		customer = customer.strip() or None

	txt_pattern = f"%{escape_like(txt.strip())}%"
	start_offset = max(0, cint(start))
	limit_len = max(1, min(cint(page_len) or 20, 100))

	v = frappe.qb.DocType("Repair Vehicle")
	search_cond = (
		(v.name.like(txt_pattern))
		| (v.display_title.like(txt_pattern))
		| (v.license_plate.like(txt_pattern))
		| (v.color.like(txt_pattern))
		| (v.vin.like(txt_pattern))
		| (v.model.like(txt_pattern))
		| (v.vehicle_generation.like(txt_pattern))
	)
	engine = Engine()
	engine.get_query("Repair Vehicle", db_query_compat=True)
	perm_cond = engine.get_permission_conditions("Repair Vehicle", v)

	if customer:
		vc = frappe.qb.DocType("Repair Vehicle Customer")
		query = (
			frappe.qb.from_(v)
			.left_join(vc)
			.on(vc.parent == v.name)
			.select(v.name, v.display_title, v.license_plate, v.color, v.manufacturer, v.model, v.model_year, v.vehicle_generation)
			.distinct()
			.where(((v.customer == customer) | (vc.customer == customer)) & search_cond)
		)
	else:
		query = (
			frappe.qb.from_(v)
			.select(v.name, v.display_title, v.license_plate, v.color, v.manufacturer, v.model, v.model_year, v.vehicle_generation)
			.where(search_cond)
		)

	if perm_cond is not None:
		query = query.where(perm_cond)

	return query.orderby(v.name).offset(start_offset).limit(limit_len).run(as_dict=as_dict)


def query_deferred_services_for_vehicle(vehicle: str):
	"""Return open deferred recommendations for a vehicle after checking doc-level read permission."""
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	if not isinstance(vehicle, str):
		frappe.throw(_("Vehicle must be a string."), frappe.ValidationError)
	vehicle_name = vehicle.strip()
	if not vehicle_name or not frappe.db.exists("Repair Vehicle", vehicle_name):
		return []

	vehicle_doc = frappe.get_doc("Repair Vehicle", vehicle_name)
	vehicle_doc.check_permission("read")

	services = frappe.get_all(
		"Vehicle Deferred Service",
		filters={
			"parent": vehicle_name,
			"status": ["in", ["Open Recommendation", ""]],
		},
		fields=[
			"name",
			"job_title",
			"decline_date",
			"repair_order",
			"estimated_amount",
			"status",
			"safety_hazard",
			"decline_reason",
		],
		order_by="decline_date desc",
	)

	for s in services:
		s["service_title"] = s.get("job_title") or ""
		s["service_description"] = s.get("job_title") or ""
		s["estimated_cost"] = flt(s.get("estimated_amount") or 0.0)
		s["estimated_amount"] = flt(s.get("estimated_amount") or 0.0)

		reason = (s.get("decline_reason") or "").strip()
		if reason.startswith("Correction code") or "NOTE: Unless otherwise explicitly stated" in reason:
			s["decline_reason"] = _("Declined during customer authorization")

	return services


def query_vehicle_customers(vehicle: str):
	"""Return list of all customers linked to this vehicle after enforcing read permission."""
	if not isinstance(vehicle, str):
		frappe.throw(_("Vehicle must be a string."), frappe.ValidationError)
	vehicle_name = vehicle.strip()
	if not vehicle_name:
		return []

	frappe.has_permission("Repair Vehicle", "read", throw=True)
	if not frappe.db.exists("Repair Vehicle", vehicle_name):
		return []

	vehicle_doc = frappe.get_doc("Repair Vehicle", vehicle_name)
	vehicle_doc.check_permission("read")

	customer_ids = []
	if vehicle_doc.customer:
		customer_ids.append(vehicle_doc.customer)
	for row in (vehicle_doc.customers or []):
		if row.customer:
			customer_ids.append(row.customer)

	if not customer_ids:
		return []

	customer_names = {
		c.name: c.customer_name
		for c in frappe.get_list(
			"Customer",
			filters={"name": ["in", customer_ids]},
			fields=["name", "customer_name"],
		)
	}

	customers = []
	seen = set()
	if vehicle_doc.customer:
		customers.append({
			"customer": vehicle_doc.customer,
			"customer_name": customer_names.get(vehicle_doc.customer, vehicle_doc.customer),
			"is_primary": 1,
		})
		seen.add(vehicle_doc.customer)

	for row in (vehicle_doc.customers or []):
		if row.customer not in seen:
			customers.append({
				"customer": row.customer,
				"customer_name": row.customer_name or customer_names.get(row.customer, row.customer),
				"is_primary": row.is_primary,
				"relationship": row.relationship,
			})
			seen.add(row.customer)

	return customers


def _ro_uses_any_item(ro_name: str, item_code_set: set[str]) -> bool:
	"""Check whether an RO's Approved services reference any item in item_code_set."""
	service_parts = frappe.get_all(
		"Repair Order Service",
		filters={"parent": ro_name, "parenttype": "Repair Order", "status": "Approved"},
		pluck="parts",
	)
	for raw_parts in service_parts:
		if not raw_parts:
			continue
		try:
			parsed = json.loads(raw_parts) if isinstance(raw_parts, str) else raw_parts
		except Exception:
			continue
		for p in parsed or []:
			if isinstance(p, dict) and p.get("item_code") in item_code_set:
				return True
	return False


def _maybe_send_ready_for_pickup_sms(ro) -> None:
	"""Dispatch optional Vehicle Ready for Pickup SMS when enabled in ISM Settings."""
	try:
		settings = frappe.get_single("ISM Settings")
		if not cint(getattr(settings, "enable_ready_for_pickup_sms", 0)):
			return

		raw_phone = frappe.db.get_value("Customer", ro.customer, "mobile_no") if ro.customer else None
		if not raw_phone and getattr(ro, "contact_person", None):
			raw_phone = frappe.db.get_value("Contact", ro.contact_person, "mobile_no") or frappe.db.get_value(
				"Contact", ro.contact_person, "phone"
			)
		phone = raw_phone.strip() if isinstance(raw_phone, str) and raw_phone.strip() else None
		if not phone:
			return

		from ism.utils.vehicle_display import format_vehicle_display

		vehicle_display = (
			format_vehicle_display(ro.vehicle, fallback=_("your vehicle"))
			if ro.vehicle
			else _("your vehicle")
		)

		customer_name = getattr(ro, "customer_name", None) or ro.customer or _("Customer")
		sms_text = _("Hello {0}, your {1} (Repair Order {2}) is ready for pickup.").format(
			customer_name, vehicle_display, ro.name
		)
		dispatch_sms(
			phone,
			sms_text,
			message_type="Ready For Pickup",
			reference_doctype="Repair Order",
			reference_name=ro.name,
		)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Ready for Pickup SMS Error ({getattr(ro, 'name', '')})",
		)


def _notify_ro_parts_ready(ro) -> None:
	"""Emit realtime alert and create/update a Shop Action Item when stock unlocks an RO."""
	frappe.publish_realtime(
		"ism_ro_parts_ready",
		{
			"repair_order": ro.name,
			"previous_status": "Waiting on Parts",
			"status": ro.status,
			"vehicle": ro.vehicle,
			"customer": ro.customer,
		},
		after_commit=True,
	)

	title = _("Parts Ready for {0}").format(ro.name)
	details = _(
		"All required parts are now in stock for Repair Order {0}. "
		"Status transitioned from Waiting on Parts to Authorized - In Progress."
	).format(ro.name)

	from ism.ism.doctype.shop_action_item.clearing import build_clearing_condition_for_ro

	clearing_type, clearing_params = build_clearing_condition_for_ro(
		"ro_status_in",
		ro.name,
		overrides={
			"statuses": ["Work Completed", "Billed / Paid", "Closed / Released", "Cancelled"],
			"description": "When work completes or RO closes",
		},
	)
	clearing_json = json.dumps(clearing_params)

	existing = frappe.get_all(
		"Shop Action Item",
		filters={
			"reference_doctype": "Repair Order",
			"reference_name": ro.name,
			"category": "Inventory / Parts",
			"status": ["in", ["Open", "In Progress"]],
		},
		order_by="modified desc",
		limit_page_length=1,
		pluck="name",
	)
	if existing:
		item_doc = frappe.get_doc("Shop Action Item", existing[0])
		item_doc.title = title
		item_doc.priority = "High"
		item_doc.details = details
		if not (item_doc.dedup_key or "").strip():
			item_doc.dedup_key = f"parts_ready:{ro.name}"
		item_doc.clearing_condition_type = clearing_type
		item_doc.clearing_condition_json = clearing_json
		item_doc.save(ignore_permissions=True)
	else:
		item_doc = frappe.new_doc("Shop Action Item")
		item_doc.title = title
		item_doc.category = "Inventory / Parts"
		item_doc.priority = "High"
		item_doc.status = "Open"
		item_doc.dedup_key = f"parts_ready:{ro.name}"
		item_doc.reference_doctype = "Repair Order"
		item_doc.reference_name = ro.name
		item_doc.details = details
		item_doc.clearing_condition_type = clearing_type
		item_doc.clearing_condition_json = clearing_json
		item_doc.insert(ignore_permissions=True)


def _process_stock_update_event(item_codes: Optional[List[str]] = None) -> None:
	"""Re-evaluate stock on Repair Orders in 'Waiting on Parts', optionally filtered by item codes."""
	waiting_orders = frappe.get_all(
		"Repair Order",
		filters={"status": "Waiting on Parts"},
		order_by="creation asc, name asc",
		pluck="name",
	)
	if not waiting_orders:
		return

	item_code_set = set(item_codes or [])
	for ro_name in waiting_orders:
		try:
			if item_code_set and not _ro_uses_any_item(ro_name, item_code_set):
				continue
			ro = frappe.get_doc("Repair Order", ro_name)
			all_avail, _ = ro._check_parts_stock_availability()
			if all_avail:
				ro.status = "Authorized - In Progress"
				ro.flags.ignore_permissions = True
				ro.save()
				_notify_ro_parts_ready(ro)
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Repair Order Stock Update Sync Error ({ro_name})",
			)


def handle_stock_update_event(doc, method=None) -> None:
	"""Enqueue (or run synchronously in tests) stock re-evaluation filtered by submitted items."""
	raw_items = getattr(doc, "items", None)
	item_codes: List[str] = []
	if raw_items is not None:
		item_codes = sorted({
			row.item_code
			for row in raw_items
			if getattr(row, "item_code", None)
		})
		if not item_codes:
			return

	try:
		if getattr(frappe.flags, "in_test", False):
			_process_stock_update_event(item_codes=item_codes or None)
		else:
			frappe.enqueue(
				"ism.ism.doctype.repair_order.fulfillment._process_stock_update_event",
				queue="default",
				enqueue_after_commit=True,
				item_codes=item_codes or None,
			)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title="Repair Order Stock Update Enqueue Error",
		)
