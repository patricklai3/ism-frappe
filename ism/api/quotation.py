# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from frappe.utils import cint, flt, getdate, nowdate

from ism.api.approval import sync_declined_jobs_to_vehicle


def before_validate_quotation(doc, method=None):
	"""Safeguard quotation totals against NoneType and auto-expand service package items before class validate."""
	for fld in (
		"base_grand_total",
		"base_rounded_total",
		"base_net_total",
		"grand_total",
		"rounded_total",
		"net_total",
		"total",
		"base_total",
	):
		if getattr(doc, fld, None) is None:
			setattr(doc, fld, 0.0)

	# Auto-expand items from service_package if custom_jobs rows are missing linked items
	if doc.get("custom_jobs"):
		existing_jobs = set()
		for it in doc.get("items") or []:
			if it.custom_job:
				existing_jobs.add(it.custom_job)
			if it.custom_job_title:
				existing_jobs.add(it.custom_job_title)

		for job in doc.custom_jobs:
			if job.service_package and (job.name not in existing_jobs and job.job_title not in existing_jobs):
				if frappe.db.exists("Service Package", job.service_package):
					pkg = frappe.get_doc("Service Package", job.service_package)
					if not job.job_title:
						job.job_title = pkg.package_name
					if not job.job_category:
						job.job_category = pkg.category

					# Add labor line if rate/hours > 0
					labor_qty = flt(pkg.standard_labor_hours) if (flt(pkg.standard_labor_hours) > 0 and not pkg.is_flat_rate) else 1.0
					labor_rate = flt(pkg.hourly_rate) if (flt(pkg.hourly_rate) > 0 and not pkg.is_flat_rate) else flt(pkg.service_rate)
					if labor_rate > 0 or flt(pkg.service_rate) > 0:
						amt = labor_qty * labor_rate
						doc.append("items", {
							"item_code": pkg.package_name,
							"item_name": pkg.package_name,
							"description": pkg.description or pkg.package_name,
							"qty": labor_qty,
							"rate": labor_rate,
							"amount": amt,
							"ordered_qty": 0.0,
							"uom": "Hour",
							"conversion_factor": 1.0,
							"custom_job": job.name,
							"custom_job_title": job.job_title,
						})

					# Add parts from package
					pkg_parts = pkg.get("items") or []
					if pkg_parts:
						part_codes = [p.item_code for p in pkg_parts if p.item_code]
						item_info_map = {
							row.name: row
							for row in frappe.get_all(
								"Item",
								filters={"name": ["in", part_codes]},
								fields=["name", "item_name", "standard_rate", "stock_uom"],
							)
						}
						for part in pkg_parts:
							item_data = item_info_map.get(part.item_code)
							p_qty = flt(part.qty) or 1.0
							default_rate = flt(item_data.standard_rate) if item_data else 0.0
							p_rate = flt(part.rate) or default_rate
							p_amt = p_qty * p_rate
							uom = (item_data.stock_uom if item_data else None) or "Nos"
							item_name = part.item_name or (item_data.item_name if item_data else None) or part.item_code
							doc.append("items", {
								"item_code": part.item_code,
								"item_name": item_name,
								"description": item_name,
								"qty": p_qty,
								"rate": p_rate,
								"amount": p_amt,
								"ordered_qty": 0.0,
								"uom": uom,
								"conversion_factor": 1.0,
								"custom_job": job.name,
								"custom_job_title": job.job_title,
							})

	for it in doc.get("items") or []:
		if getattr(it, "ordered_qty", None) is None:
			it.ordered_qty = 0.0
		if getattr(it, "conversion_factor", None) is None:
			it.conversion_factor = 1.0
		if not getattr(it, "uom", None):
			it.uom = "Nos"


def validate_quotation(doc, method=None):
	"""Ensure vehicle has an active check-in record and validate job resolution on submit."""
	# Safeguard for quotation totals against NoneType
	for fld in (
		"base_grand_total",
		"base_rounded_total",
		"base_net_total",
		"grand_total",
		"rounded_total",
		"net_total",
		"total",
		"base_total",
	):
		if getattr(doc, fld, None) is None:
			setattr(doc, fld, 0.0)

	# Auto-calculate estimated_total for custom_jobs based on linked items
	if doc.get("custom_jobs"):
		jobs_by_title = {j.job_title: j.name for j in doc.custom_jobs if j.job_title and j.name}
		for item in doc.get("items") or []:
			if not item.custom_job and item.custom_job_title in jobs_by_title:
				item.custom_job = jobs_by_title[item.custom_job_title]

		job_totals = {}
		for item in doc.get("items") or []:
			if item.custom_job:
				job_totals[item.custom_job] = job_totals.get(item.custom_job, 0.0) + flt(item.amount)
			elif item.custom_job_title:
				job_totals[item.custom_job_title] = job_totals.get(item.custom_job_title, 0.0) + flt(item.amount)

		for job in doc.get("custom_jobs"):
			val = job_totals.get(job.name) if job.name else None
			if val is None and job.job_title:
				val = job_totals.get(job.job_title)
			job.estimated_total = val or 0.0

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

		# Gate 1: All jobs must be resolved (Approved or Declined)
		if doc.get("custom_jobs"):
			for job in doc.get("custom_jobs"):
				if job.status in ("Draft", "Pending Approval"):
					frappe.throw(
						_(
							"Cannot submit Quotation: All repair jobs must be resolved (Approved or Declined) before submitting. "
							"Job '{0}' is still {1}."
						).format(job.job_title, job.status)
					)

			sync_declined_jobs_to_vehicle(doc)


@frappe.whitelist()
def make_sales_order(source_name: str, target_doc=None, args=None):
	"""Overridden Quotation -> Sales Order mapping ensuring only approved jobs transfer."""
	quotation = frappe.get_doc("Quotation", source_name)

	# Expiry validation
	if not frappe.db.get_singles_value("Selling Settings", "allow_sales_order_creation_for_expired_quotation"):
		if quotation.valid_till and (
			quotation.valid_till < quotation.transaction_date or quotation.valid_till < getdate(nowdate())
		):
			frappe.throw(_("Validity period of this quotation has ended."))

	jobs = quotation.get("custom_jobs") or []
	approved_job_ids = set()
	approved_jobs = []

	if jobs:
		unresolved = [j.job_title for j in jobs if j.status in ("Draft", "Pending Approval")]
		if unresolved:
			frappe.throw(
				_(
					"Cannot create Sales Order: All repair jobs must be resolved (Approved or Declined). "
					"Unresolved jobs: {0}"
				).format(", ".join(unresolved))
			)

		for j in jobs:
			if j.status == "Approved":
				approved_job_ids.add(j.name)
				approved_jobs.append(j)

		if not approved_jobs:
			frappe.throw(_("Cannot create Sales Order: At least one repair job must be Approved by the customer."))

		# Sync declined jobs to vehicle deferred services log
		sync_declined_jobs_to_vehicle(quotation)

	def match_item(source_item) -> bool:
		# If quotation has jobs, only allow items that link to an approved job
		if jobs:
			return source_item.custom_job in approved_job_ids
		return not source_item.get("is_free_item")

	def set_missing_values(source, target):
		target.customer = source.party_name
		target.customer_name = source.customer_name
		target.custom_vehicle = source.custom_vehicle
		target.custom_vehicle_check_in = source.custom_vehicle_check_in
		target.custom_odometer_in = source.custom_odometer_in

		# Copy approved jobs to target Sales Order
		if approved_jobs:
			target.set("custom_jobs", [])
			job_row_map = {}
			for j in approved_jobs:
				new_job_id = frappe.generate_hash(length=10)
				new_job = target.append("custom_jobs", {
					"job_title": j.job_title,
					"job_category": j.job_category,
					"service_package": j.service_package,
					"status": "Approved",
					"approval_channel": j.approval_channel,
					"estimated_total": j.estimated_total,
					"authorized_amount": j.authorized_amount,
					"authorized_by": j.authorized_by,
					"authorized_at": j.authorized_at,
					"advisor": j.advisor,
					"signature_image": j.signature_image,
					"is_supplement": j.is_supplement,
				})
				new_job.name = new_job_id
				new_job.set("__islocal", 1)
				job_row_map[j.name] = new_job

			# Re-point item custom_job references to new job row names if needed
			for it in target.get("items") or []:
				if it.custom_job in job_row_map:
					it.custom_job = job_row_map[it.custom_job].name

	doclist = get_mapped_doc(
		"Quotation",
		source_name,
		{
			"Quotation": {
				"doctype": "Sales Order",
				"validation": {"docstatus": ["=", 1]},
				"field_no_map": ["items"],
			},
			"Quotation Item": {
				"doctype": "Sales Order Item",
				"field_map": {"name": "quotation_item", "parent": "against_quotation"},
				"condition": match_item,
			},
			"Sales Taxes and Charges": {"doctype": "Sales Taxes and Charges", "add_if_empty": True},
			"Sales Team": {"doctype": "Sales Team", "add_if_empty": True},
			"Payment Schedule": {"doctype": "Payment Schedule", "add_if_empty": True},
		},
		target_doc,
		set_missing_values,
	)

	return doclist


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


@frappe.whitelist()
def get_service_package_details(package_name: str) -> dict:
	"""Fetch service package details including labor and parts with pricing."""
	if not package_name or not isinstance(package_name, str) or not package_name.strip():
		frappe.throw(_("Invalid package name."))

	frappe.has_permission("Service Package", "read", throw=True)

	clean_name = package_name.strip()
	if not frappe.db.exists("Service Package", clean_name):
		frappe.throw(_("Service Package '{0}' not found.").format(clean_name), frappe.DoesNotExistError)

	pkg = frappe.get_doc("Service Package", clean_name)
	pkg.check_permission("read")

	pkg_items = pkg.get("items") or []
	parts = []
	if pkg_items:
		item_codes = [p.item_code for p in pkg_items if p.item_code]
		item_info_map = {
			row.name: row
			for row in frappe.get_all(
				"Item",
				filters={"name": ["in", item_codes]},
				fields=["name", "item_name", "standard_rate", "stock_uom"],
			)
		}
		for p in pkg_items:
			item_data = item_info_map.get(p.item_code)
			default_rate = flt(item_data.standard_rate) if item_data else 0.0
			rate = flt(p.rate) or default_rate
			uom = (item_data.stock_uom if item_data else None) or "Nos"
			item_name = p.item_name or (item_data.item_name if item_data else None) or p.item_code
			qty = flt(p.qty) or 1.0
			parts.append({
				"item_code": p.item_code,
				"item_name": item_name,
				"qty": qty,
				"rate": rate,
				"amount": qty * rate,
				"uom": uom,
			})

	return {
		"name": pkg.name,
		"package_name": pkg.package_name,
		"category": pkg.category,
		"description": pkg.description or "",
		"standard_labor_hours": flt(pkg.standard_labor_hours),
		"hourly_rate": flt(pkg.hourly_rate),
		"service_rate": flt(pkg.service_rate),
		"is_flat_rate": cint(pkg.is_flat_rate),
		"items": parts,
	}
