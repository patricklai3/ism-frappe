# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt


def validate_sales_invoice(doc, method=None):
	"""Sync automotive jobs and vehicle data from linked Sales Order if missing."""
	# If custom_jobs is empty, attempt to pull from linked Sales Order
	if not doc.get("custom_jobs"):
		so_names = {it.sales_order for it in (doc.get("items") or []) if it.sales_order}
		if so_names:
			existing_titles = set()
			for so_name in so_names:
				if not frappe.db.exists("Sales Order", so_name):
					continue
				so = frappe.get_doc("Sales Order", so_name)
				if not doc.custom_vehicle and so.custom_vehicle:
					doc.custom_vehicle = so.custom_vehicle
				if not doc.custom_vehicle_check_in and so.custom_vehicle_check_in:
					doc.custom_vehicle_check_in = so.custom_vehicle_check_in
				if not doc.custom_odometer_in and so.custom_odometer_in:
					doc.custom_odometer_in = so.custom_odometer_in

				for so_job in so.get("custom_jobs") or []:
					if so_job.status == "Approved" and so_job.job_title not in existing_titles:
						existing_titles.add(so_job.job_title)
						doc.append("custom_jobs", {
							"job_title": so_job.job_title,
							"job_category": so_job.job_category,
							"service_package": so_job.service_package,
							"status": so_job.status,
							"approval_channel": so_job.approval_channel,
							"estimated_total": so_job.estimated_total,
							"authorized_amount": so_job.authorized_amount,
							"authorized_by": so_job.authorized_by,
							"authorized_at": so_job.authorized_at,
							"advisor": so_job.advisor,
							"signature_image": so_job.signature_image,
							"is_supplement": so_job.is_supplement,
						})


def validate_sales_invoice_authorization(doc, method=None):
	"""Strict Gate: Ensure every billed item links to an authorized job within cost tolerance."""
	jobs = doc.get("custom_jobs") or []
	if not jobs:
		# If no jobs defined, bypass automotive authorization gate
		return

	approved_jobs = {j.name: j for j in jobs if j.status == "Approved"}
	approved_job_titles = {j.job_title: j for j in jobs if j.status == "Approved"}

	settings = frappe.get_single("ISM Settings")
	tolerance_perc = flt(settings.cost_tolerance_percentage) or 10.0
	tolerance_flat = flt(settings.cost_tolerance_flat_amount) or 20.0

	# Map billed amounts per job
	job_billed_totals = {}

	for it in doc.get("items") or []:
		job_key = it.custom_job or it.custom_job_title
		if not job_key:
			frappe.throw(
				_(
					"Cannot submit Sales Invoice: Billed item '{0}' is not linked to any repair job. "
					"Every billed line must link to a customer-authorized repair job."
				).format(it.item_code or it.item_name)
			)

		matched_job = approved_jobs.get(job_key) or approved_job_titles.get(job_key)
		if not matched_job:
			frappe.throw(
				_(
					"Cannot submit Sales Invoice: Item '{0}' is linked to unauthorized or unapproved job '{1}'. "
					"Only customer-approved jobs can be billed."
				).format(it.item_code, job_key)
			)

		job_name = matched_job.name
		job_billed_totals[job_name] = job_billed_totals.get(job_name, 0.0) + flt(it.amount)

	# Verify tolerance per job
	for job_name, matched_job in approved_jobs.items():
		billed_total = job_billed_totals.get(job_name, 0.0)
		authorized_cap = flt(matched_job.authorized_amount) or flt(matched_job.estimated_total)

		delta = billed_total - authorized_cap
		if delta > 0.001:
			perc_buffer = authorized_cap * (tolerance_perc / 100.0)
			permitted_buffer = min(perc_buffer, tolerance_flat) if tolerance_flat > 0 else perc_buffer

			if delta > permitted_buffer:
				frappe.throw(
					_(
						"Cannot submit Sales Invoice: Billed total for job '{0}' (${1:,.2f}) exceeds the customer-authorized limit "
						"(${2:,.2f}) plus permitted tolerance (${3:,.2f}). A supplemental customer authorization is required."
					).format(matched_job.job_title, billed_total, authorized_cap, permitted_buffer)
				)
