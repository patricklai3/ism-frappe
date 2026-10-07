# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe


def validate_quotation(doc, method=None):
	"""Ensure vehicle has an active check-in record and is not checked out before submitting."""
	if not doc.custom_vehicle:
		return

	check_in = None
	if doc.custom_vehicle_check_in:
		check_in = frappe.db.get_value(
			"Vehicle Check-in",
			doc.custom_vehicle_check_in,
			["status", "vehicle", "customer"],
			as_dict=True,
		)
		if not check_in:
			frappe.throw(frappe._("Linked Vehicle Check-in {0} does not exist.").format(doc.custom_vehicle_check_in))

		if check_in.vehicle != doc.custom_vehicle:
			frappe.throw(
				frappe._("Vehicle Check-in {0} belongs to vehicle {1}, not {2}.").format(
					doc.custom_vehicle_check_in, check_in.vehicle, doc.custom_vehicle
				)
			)

	if doc.docstatus == 1:
		if not doc.custom_vehicle_check_in:
			active_check_in = frappe.db.get_value(
				"Vehicle Check-in",
				{"vehicle": doc.custom_vehicle, "status": "Checked In"},
				"name",
			)
			if active_check_in:
				doc.custom_vehicle_check_in = active_check_in
			else:
				frappe.throw(
					frappe._(
						"Cannot submit Quotation for vehicle {0}: The vehicle must have an active Vehicle Check-in record and cannot be checked out."
					).format(doc.custom_vehicle)
				)
		else:
			status = check_in.status if check_in else frappe.db.get_value("Vehicle Check-in", doc.custom_vehicle_check_in, "status")
			if status != "Checked In":
				frappe.throw(
					frappe._(
						"Cannot submit Quotation: Vehicle Check-in {0} is not active (status: {1}). An active check-in is required."
					).format(doc.custom_vehicle_check_in, status or "Unknown")
				)


@frappe.whitelist()
def get_active_checked_in_vehicles(
	doctype=None, txt=None, searchfield=None, start=0, page_len=20, filters=None, as_dict=False, **kwargs
):
	"""Custom query to filter vehicles that currently have an active check-in."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters) or {}
	filters = filters or {}
	customer = filters.get("customer")

	v = frappe.qb.DocType("Repair Vehicle")
	ci = frappe.qb.DocType("Vehicle Check-in")
	rvc = frappe.qb.DocType("Repair Vehicle Customer")

	query = (
		frappe.qb.from_(v)
		.inner_join(ci)
		.on(ci.vehicle == v.name)
		.left_join(rvc)
		.on(rvc.parent == v.name)
		.select(v.name, v.model, v.license_plate, v.customer)
		.distinct()
		.where(ci.status == "Checked In")
		.orderby(v.creation, order=frappe.qb.desc)
	)

	if customer and isinstance(customer, str):
		query = query.where((v.customer == customer) | (rvc.customer == customer))

	if txt and isinstance(txt, str) and txt.strip():
		like_term = f"%{txt.strip()}%"
		query = query.where(
			(v.name.like(like_term)) | (v.license_plate.like(like_term)) | (v.model.like(like_term))
		)

	start_val = frappe.utils.cint(start)
	page_len_val = frappe.utils.cint(page_len) or 20

	query = query.offset(start_val).limit(page_len_val)

	return query.run(as_dict=as_dict)


@frappe.whitelist()
def get_active_check_in_for_vehicle(vehicle: str):
	"""Return the active check-in details for a given vehicle."""
	if not isinstance(vehicle, str) or not vehicle.strip():
		frappe.throw(frappe._("Invalid vehicle parameter"), frappe.ValidationError)

	frappe.has_permission("Vehicle Check-in", "read", throw=True)

	check_in = frappe.db.get_value(
		"Vehicle Check-in",
		{"vehicle": vehicle.strip(), "status": "Checked In"},
		["name", "customer", "intake_mileage", "customer_concern"],
		as_dict=True,
	)
	return check_in
