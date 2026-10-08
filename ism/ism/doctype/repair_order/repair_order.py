# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import hashlib
import json
import secrets
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
	add_days,
	cint,
	flt,
	get_datetime,
	now_datetime,
	time_diff_in_hours,
	today,
)


class RepairOrder(Document):
	def before_insert(self):
		if not self.price_list:
			self.price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")

	def validate(self):
		self._validate_vehicle_and_customer()
		self._validate_odometer()
		self._calculate_job_and_item_totals()
		self._calculate_taxes_and_totals()
		self._update_progress_and_job_timing()
		self._sync_declined_to_deferred_services()

	def _validate_vehicle_and_customer(self):
		if not self.vehicle or not self.customer:
			return

		# Ensure vehicle exists
		if not frappe.db.exists("Repair Vehicle", self.vehicle):
			frappe.throw(_("Repair Vehicle {0} does not exist.").format(self.vehicle))

		# Link customer to vehicle if not already linked
		is_linked = frappe.db.exists(
			"Repair Vehicle Customer",
			{"parent": self.vehicle, "customer": self.customer},
		)
		if not is_linked:
			veh_doc = frappe.get_doc("Repair Vehicle", self.vehicle)
			veh_doc.append("customers", {"customer": self.customer, "is_primary": 1 if not veh_doc.customers else 0})
			veh_doc.save(ignore_permissions=True)

		if self.vehicle_check_in:
			check_in_veh = frappe.db.get_value("Vehicle Check-in", self.vehicle_check_in, "vehicle")
			if check_in_veh and check_in_veh != self.vehicle:
				frappe.throw(_("Vehicle Check-in {0} belongs to vehicle {1}, not {2}.").format(
					self.vehicle_check_in, check_in_veh, self.vehicle
				))

	def _validate_odometer(self):
		if self.odometer_in is not None and self.odometer_in < 0:
			frappe.throw(_("Odometer In cannot be negative."))

		if cint(self.odometer_out) > 0:
			if self.odometer_in is not None and cint(self.odometer_out) < cint(self.odometer_in):
				frappe.throw(_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(
					self.odometer_out, self.odometer_in
				))

	def _calculate_job_and_item_totals(self):
		# Auto-assign job_id if missing
		for idx, job in enumerate(self.jobs, 1):
			if not job.job_id:
				job.job_id = f"JOB-{idx}"

		# Calculate item amounts and group by job_id
		job_totals = {j.job_id: 0.0 for j in self.jobs}
		total_billed_hours = 0.0

		for item in self.items:
			item.amount = flt(item.qty) * flt(item.rate)
			if item.job_id in job_totals:
				job_totals[item.job_id] += flt(item.amount)

		for job in self.jobs:
			job.estimated_total = job_totals.get(job.job_id, 0.0)
			if job.status == "Approved":
				job.authorized_amount = job.estimated_total
				total_billed_hours += flt(job.estimated_labor_hours)
			elif job.status == "Declined":
				job.authorized_amount = 0.0

		self.total_billed_hours = total_billed_hours

	def _calculate_taxes_and_totals(self):
		# Calculate net total from approved jobs, or all active jobs if draft
		has_approved = any(j.status == "Approved" for j in self.jobs)
		if has_approved:
			net = sum(flt(j.authorized_amount) for j in self.jobs if j.status == "Approved")
		else:
			net = sum(flt(j.estimated_total) for j in self.jobs if j.status != "Declined")

		self.net_total = net

		# Tax template calculation
		tax_rate = flt(self.tax_rate)
		if not tax_rate:
			if self.taxes_and_charges:
				tax_rate = flt(frappe.db.get_value(
					"Sales Taxes and Charges",
					{"parent": self.taxes_and_charges},
					"rate"
				) or 13.0)
			else:
				tax_rate = 13.0  # Standard Ontario HST default
			self.tax_rate = tax_rate

		self.total_taxes_and_charges = flt(net * (tax_rate / 100.0), 2)
		self.grand_total = flt(self.net_total + self.total_taxes_and_charges, 2)
		self.outstanding_amount = flt(self.grand_total - flt(self.paid_amount), 2)

	def _update_progress_and_job_timing(self):
		approved_jobs = [j for j in self.jobs if j.status == "Approved"]
		if approved_jobs:
			completed_jobs = [j for j in approved_jobs if j.is_completed]
			self.progress_percent = flt((len(completed_jobs) / len(approved_jobs)) * 100.0, 1)
		else:
			self.progress_percent = 0.0

		for job in self.jobs:
			if job.is_completed and not job.completed_at:
				job.completed_at = now_datetime()
				job.completed_by = frappe.session.user
				if self.picked_up_at:
					job.actual_duration_hours = flt(time_diff_in_hours(job.completed_at, self.picked_up_at), 2)

	def _sync_declined_to_deferred_services(self):
		"""Log declined jobs into Vehicle Deferred Service for future follow-up."""
		if not self.vehicle:
			return

		for job in self.jobs:
			if job.status == "Declined":
				exists = frappe.db.exists(
					"Vehicle Deferred Service",
					{
						"parent": self.vehicle,
						"repair_order": self.name,
						"job_title": job.job_title,
					},
				)
				if not exists:
					veh_doc = frappe.get_doc("Repair Vehicle", self.vehicle)
					veh_doc.append("deferred_services", {
						"job_title": job.job_title,
						"decline_date": today(),
						"estimated_amount": job.estimated_total,
						"repair_order": self.name,
						"decline_reason": job.advisor_notes or _("Declined during customer authorization"),
					})
					veh_doc.save(ignore_permissions=True)

	@frappe.whitelist()
	def pick_up_vehicle(self):
		"""Technician pulls vehicle from lot into service bay."""
		self.picked_up_at = now_datetime()
		self.picked_up_by = frappe.session.user
		self.vehicle_location = "In Bay / Being Serviced"
		if self.status in ["Draft Estimate", "Pending Authorization"]:
			self.status = "Authorized - In Progress"
		self.save()
		return {"status": "success", "vehicle_location": self.vehicle_location, "picked_up_at": self.picked_up_at}

	@frappe.whitelist()
	def park_vehicle(self):
		"""Technician parks vehicle back in lot after completing work."""
		self.parked_at = now_datetime()
		self.parked_by = frappe.session.user
		self.vehicle_location = "Completed / Parked in Lot"
		self.status = "Work Completed"
		if self.picked_up_at:
			self.total_bay_hours = flt(time_diff_in_hours(self.parked_at, self.picked_up_at), 2)
		self.save()
		return {"status": "success", "vehicle_location": self.vehicle_location, "total_bay_hours": self.total_bay_hours}

	@frappe.whitelist()
	def toggle_job_completion(self, job_id, is_completed):
		"""Toggle individual job completion with timestamping."""
		for job in self.jobs:
			if job.job_id == job_id:
				job.is_completed = 1 if cint(is_completed) else 0
				if job.is_completed:
					job.completed_at = now_datetime()
					job.completed_by = frappe.session.user
					if self.picked_up_at:
						job.actual_duration_hours = flt(time_diff_in_hours(job.completed_at, self.picked_up_at), 2)
				else:
					job.completed_at = None
					job.completed_by = None
					job.actual_duration_hours = 0.0
				break
		self.save()
		return {"status": "success", "progress_percent": self.progress_percent}

	@frappe.whitelist()
	def generate_approval_token(self):
		"""Generate token for remote customer signature portal."""
		self.approval_token = secrets.token_urlsafe(32)
		self.approval_token_expiry = add_days(now_datetime(), 2)
		self.approval_status = "Pending Approval"
		self.save(ignore_permissions=True)
		return {"approval_token": self.approval_token, "expiry": self.approval_token_expiry}

	@frappe.whitelist()
	def authorize_jobs(self, approvals: dict, channel="Remote Web Link", signer_name=None, signature_image=None, notes=None):
		"""
		Apply per-job customer decisions (Approved/Declined) and audit evidence.
		approvals: {"JOB-1": "Approved", "JOB-2": "Declined"}
		"""
		if isinstance(approvals, str):
			approvals = json.loads(approvals)

		all_approved = True
		any_approved = False

		for job in self.jobs:
			decision = approvals.get(job.job_id)
			if decision in ["Approved", "Declined"]:
				job.status = decision
				job.approval_channel = channel
				if decision == "Approved":
					any_approved = True
				else:
					all_approved = False

		if all_approved and any_approved:
			self.approval_status = "Approved"
			self.status = "Authorized - In Progress"
		elif any_approved:
			self.approval_status = "Partially Approved"
			self.status = "Authorized - In Progress"
		else:
			self.approval_status = "Declined"

		self.authorized_by = signer_name or frappe.session.user
		self.authorized_at = now_datetime()
		if signature_image:
			self.signature_image = signature_image
		if notes:
			self.verbal_notes = notes

		# Build SHA-256 audit hash
		audit_payload = f"{self.name}|{self.authorized_by}|{self.authorized_at}|{json.dumps(approvals, sort_keys=True)}"
		self.audit_hash = hashlib.sha256(audit_payload.encode()).hexdigest()

		self.save()
		return {"status": "success", "approval_status": self.approval_status}

	@frappe.whitelist()
	def add_service_package(self, service_package):
		"""Expand canned service package into a new Job and child Items."""
		pkg = frappe.get_doc("Service Package", service_package)
		new_job_id = f"JOB-{len(self.jobs) + 1}"

		self.append("jobs", {
			"job_id": new_job_id,
			"job_title": pkg.package_name,
			"job_category": pkg.category or "Maintenance",
			"service_package": pkg.name,
			"estimated_labor_hours": flt(pkg.standard_labor_hours),
			"status": "Draft",
			"advisor_notes": pkg.description or "",
		})

		for row in pkg.items:
			item_doc = frappe.get_cached_doc("Item", row.item_code)
			self.append("items", {
				"job_id": new_job_id,
				"job_title": pkg.package_name,
				"item_type": "Labor" if row.is_labor else "Part",
				"item_code": row.item_code,
				"item_name": row.item_name or item_doc.item_name,
				"qty": flt(row.qty) or 1.0,
				"uom": getattr(row, "uom", None) or item_doc.stock_uom,
				"rate": flt(row.rate),
				"amount": flt(row.qty or 1.0) * flt(row.rate),
				"status": "Draft",
			})

		self.save()
		return {"status": "success", "job_id": new_job_id}

	@frappe.whitelist()
	def process_payment_and_release(self, payment_method="Debit / Interac", paid_amount=None, odometer_out=None):
		"""
		Finalize billing audit:
		1. Validates odometer out
		2. Creates background ERPNext Sales Invoice with update_stock=1
		3. Creates matching Payment Entry
		4. Marks vehicle released, stamps vehicle last service specs, and submits RO
		"""
		if odometer_out is not None:
			self.odometer_out = cint(odometer_out)

		if not self.odometer_out:
			frappe.throw(_("Odometer Out is mandatory to complete and release the vehicle."))

		if self.odometer_in and self.odometer_out < self.odometer_in:
			frappe.throw(_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(
				self.odometer_out, self.odometer_in
			))

		approved_items = [
			item for item in self.items
			if any(j.job_id == item.job_id and j.status == "Approved" for j in self.jobs)
		]

		if not approved_items:
			frappe.throw(_("Cannot process billing without any approved jobs and items."))

		amount_to_pay = flt(paid_amount) if paid_amount else flt(self.grand_total)

		# 1. Create and submit background Sales Invoice
		si = frappe.new_doc("Sales Invoice")
		si.customer = self.customer
		si.company = self.company
		si.posting_date = today()
		si.due_date = today()
		si.currency = self.currency
		si.update_stock = 1  # Deduct stock items from warehouse

		for row in approved_items:
			si.append("items", {
				"item_code": row.item_code,
				"item_name": row.item_name,
				"description": row.description or row.item_name,
				"qty": row.qty,
				"uom": row.uom,
				"rate": row.rate,
				"amount": row.amount,
				"warehouse": row.warehouse or frappe.db.get_single_value("Stock Settings", "default_warehouse"),
			})

		if self.taxes_and_charges:
			si.taxes_and_charges = self.taxes_and_charges
			si.set_taxes()

		si.flags.ignore_permissions = True
		si.insert()
		si.submit()

		self.sales_invoice = si.name

		target_pay = flt(paid_amount) if paid_amount is not None else flt(si.outstanding_amount)
		allocated_amount = min(target_pay, flt(si.outstanding_amount))

		# 2. Create Payment Entry if amount paid > 0
		if allocated_amount > 0:
			mode_of_payment = payment_method
			# Ensure mode of payment exists or fallback to Cash
			if not frappe.db.exists("Mode of Payment", mode_of_payment):
				mode_of_payment = frappe.db.get_value("Mode of Payment", {}, "name") or "Cash"

			pe = frappe.new_doc("Payment Entry")
			pe.payment_type = "Receive"
			pe.party_type = "Customer"
			pe.party = self.customer
			pe.company = self.company
			pe.posting_date = today()
			pe.mode_of_payment = mode_of_payment
			pe.paid_amount = allocated_amount
			pe.received_amount = allocated_amount
			pe.reference_no = self.name
			pe.reference_date = today()

			# Determine debit/credit accounts
			paid_to_account = frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": mode_of_payment, "company": self.company},
				"default_account"
			) or frappe.get_cached_value("Company", self.company, "default_cash_account")

			pe.paid_to = paid_to_account
			pe.paid_from = si.debit_to

			pe.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"total_amount": si.grand_total,
				"outstanding_amount": si.outstanding_amount,
				"allocated_amount": allocated_amount,
			})

			pe.flags.ignore_permissions = True
			pe.insert()
			pe.submit()

			self.paid_amount = allocated_amount
			self.payment_status = "Paid" if allocated_amount >= flt(si.grand_total) else "Partially Paid"
			self.outstanding_amount = max(0.0, flt(self.grand_total - allocated_amount))
		else:
			self.paid_amount = 0.0
			self.payment_status = "Unpaid"
			self.outstanding_amount = self.grand_total

		# 3. Mark vehicle released and update vehicle specs
		self.vehicle_released = 1
		self.vehicle_location = "Released to Customer"
		self.status = "Billed / Paid"

		# Update vehicle odometer and last service
		if self.vehicle:
			frappe.db.set_value(
				"Repair Vehicle",
				self.vehicle,
				{
					"last_odometer": self.odometer_out,
					"last_service_date": today(),
				},
			)

		self.submit()
		return {
			"status": "success",
			"sales_invoice": self.sales_invoice,
			"paid_amount": self.paid_amount,
			"outstanding_amount": self.outstanding_amount,
			"vehicle_released": self.vehicle_released,
		}

	@frappe.whitelist()
	def create_material_request(self):
		"""Generate Material Request for parts from approved jobs."""
		parts = [
			item for item in self.items
			if item.item_type == "Part" and any(j.job_id == item.job_id and j.status == "Approved" for j in self.jobs)
		]

		if not parts:
			frappe.throw(_("No approved part lines found on this Repair Order."))

		mr = frappe.new_doc("Material Request")
		mr.material_request_type = "Purchase"
		mr.company = self.company
		mr.schedule_date = today()

		for part in parts:
			mr.append("items", {
				"item_code": part.item_code,
				"item_name": part.item_name,
				"description": part.description or part.item_name,
				"qty": part.qty,
				"uom": part.uom,
				"warehouse": part.warehouse or frappe.db.get_single_value("Stock Settings", "default_warehouse"),
				"schedule_date": today(),
			})

		mr.flags.ignore_permissions = True
		mr.insert()
		return {"material_request": mr.name}


@frappe.whitelist()
def get_filtered_vehicles(doctype, txt, searchfield, start, page_len, filters):
	customer = filters.get("customer") if filters else None
	if customer:
		return frappe.db.sql(
			"""
			SELECT v.name, v.license_plate, v.make, v.model, v.year
			FROM `tabRepair Vehicle` v
			JOIN `tabRepair Vehicle Customer` vc ON vc.parent = v.name
			WHERE vc.customer = %(customer)s
			  AND (v.name LIKE %(txt)s OR v.license_plate LIKE %(txt)s OR v.vin LIKE %(txt)s)
			ORDER BY v.name ASC
			LIMIT %(start)s, %(page_len)s
			""",
			{"customer": customer, "txt": f"%{txt}%", "start": cint(start), "page_len": cint(page_len)},
		)
	return frappe.db.sql(
		"""
		SELECT name, license_plate, make, model, year
		FROM `tabRepair Vehicle`
		WHERE name LIKE %(txt)s OR license_plate LIKE %(txt)s OR vin LIKE %(txt)s
		ORDER BY name ASC
		LIMIT %(start)s, %(page_len)s
		""",
		{"txt": f"%{txt}%", "start": cint(start), "page_len": cint(page_len)},
	)


@frappe.whitelist()
def get_deferred_services_for_vehicle(vehicle: str):
	if not vehicle:
		return []
	return frappe.get_all(
		"Vehicle Deferred Service",
		filters={"parent": vehicle},
		fields=["name", "job_title", "decline_date", "estimated_amount", "decline_reason"],
		order_by="decline_date desc",
	)


@frappe.whitelist()
def make_repair_order(source_name: str, target_doc=None):
	"""Generate a draft Repair Order from a Vehicle Check-in document."""
	from frappe.model.mapper import get_mapped_doc

	def set_missing_values(source, target):
		target.status = "Draft Estimate"
		target.vehicle_location = "In Customer Lot"
		target.odometer_in = source.intake_mileage
		target.customer_concern = source.customer_concern
		target.posting_date = today()
		if not target.company:
			target.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
		if not target.currency:
			target.currency = "CAD"

	doc = get_mapped_doc(
		"Vehicle Check-in",
		source_name,
		{
			"Vehicle Check-in": {
				"doctype": "Repair Order",
				"field_map": {
					"customer": "customer",
					"vehicle": "vehicle",
					"name": "vehicle_check_in",
					"intake_mileage": "odometer_in",
					"customer_concern": "customer_concern",
				},
			}
		},
		target_doc,
		set_missing_values,
	)
	return doc
