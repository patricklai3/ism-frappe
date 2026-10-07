# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def validate_sales_order(doc, method=None):
	"""Validate that all jobs on active Sales Order (Repair Order) are in Approved status."""
	jobs = doc.get("custom_jobs") or []
	if not jobs:
		return

	for job in jobs:
		if job.status != "Approved":
			frappe.throw(
				_(
					"Cannot save Repair Order (Sales Order): Only Approved jobs can exist on an active Repair Order. "
					"Job '{0}' has status '{1}'."
				).format(job.job_title, job.status)
			)
