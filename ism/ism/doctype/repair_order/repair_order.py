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
	escape_html,
	flt,
	get_datetime,
	now_datetime,
	time_diff_in_hours,
	today,
)


def _get_default_labor_rate():
	try:
		if frappe.db.has_column("ISM Settings", "default_labor_rate"):
			return flt(frappe.db.get_single_value("ISM Settings", "default_labor_rate")) or 120.0
	except Exception:
		pass
	return 120.0


def get_or_create_default_tax_template(company=None):
	"""
	Return the default 13% HST template for the company.
	If not present, create the HST 13% account and 13% HST template and return it.
	"""
	company = company or frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company") or "I"
	abbr = frappe.db.get_value("Company", company, "abbr") or company

	# Look for an existing default template
	default_tmpl = frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "is_default": 1},
		"name"
	)
	if default_tmpl and ("13" in default_tmpl or "HST" in default_tmpl):
		return default_tmpl

	# Check for any template with 13% or HST in title/name
	hst_tmpl = frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "title": ["like", "%HST%"]},
		"name"
	) or frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "name": ["like", "%13%HST%"]},
		"name"
	)
	if hst_tmpl:
		return hst_tmpl

	# Create Account & Template if needed
	duties_and_taxes = (
		frappe.db.get_value("Account", {"account_name": "Duties and Taxes", "company": company}, "name")
		or f"Duties and Taxes - {abbr}"
	)
	account_name = f"HST 13% - {abbr}"
	if not frappe.db.exists("Account", account_name):
		if frappe.db.exists("Account", duties_and_taxes):
			try:
				acc = frappe.get_doc({
					"doctype": "Account",
					"account_name": "HST 13%",
					"company": company,
					"parent_account": duties_and_taxes,
					"account_type": "Tax",
					"currency": frappe.db.get_value("Company", company, "default_currency") or "CAD",
				})
				acc.insert(ignore_permissions=True)
			except Exception:
				pass

	template_name = f"13% HST - {abbr}"
	if not frappe.db.exists("Sales Taxes and Charges Template", template_name):
		tax_head = account_name if frappe.db.exists("Account", account_name) else duties_and_taxes
		try:
			tmpl = frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": "13% HST",
				"company": company,
				"is_default": 1,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": tax_head,
						"rate": 13.0,
						"description": "HST @ 13.0%",
					}
				],
			})
			tmpl.insert(ignore_permissions=True)
			for other in frappe.get_all(
				"Sales Taxes and Charges Template",
				filters={"company": company, "is_default": 1, "name": ["!=", template_name]},
			):
				frappe.db.set_value("Sales Taxes and Charges Template", other.name, "is_default", 0)
			return tmpl.name
		except Exception:
			pass

	return template_name if frappe.db.exists("Sales Taxes and Charges Template", template_name) else default_tmpl


def _render_parts_html(parts):
	"""Render a compact, styled HTML table of parts nested under a service."""
	if not parts:
		return '<div class="text-muted" style="padding: 6px 0; font-size: 12px;"><i>No parts required for this service.</i></div>'

	rows = ""
	for p in parts:
		item_code = escape_html(str(p.get("item_code", "")))
		item_name = escape_html(str(p.get("item_name", p.get("item_code", ""))))
		qty = flt(p.get("qty", 1.0))
		uom = escape_html(str(p.get("uom", "Nos")))
		rate = flt(p.get("rate", 0.0))
		amt = flt(p.get("amount", qty * rate), 2)
		rows += f"""
		<tr>
			<td><b>{item_code}</b></td>
			<td>{item_name}</td>
			<td style="text-align: right;">{qty} {uom}</td>
			<td style="text-align: right;">${rate:.2f}</td>
			<td style="text-align: right;"><b>${amt:.2f}</b></td>
		</tr>
		"""

	return f"""
	<table class="table table-bordered table-condensed" style="margin-top: 5px; font-size: 12px; background: #fafbfc;">
		<thead>
			<tr style="background: #eef2f7;">
				<th>Part Code</th>
				<th>Description</th>
				<th style="text-align: right;">Qty</th>
				<th style="text-align: right;">Rate</th>
				<th style="text-align: right;">Amount</th>
			</tr>
		</thead>
		<tbody>
			{rows}
		</tbody>
	</table>
	"""


def _match_service_decision(service, approvals):
	"""
	Match customer approval decision from approvals dict against a service by:
	- service.name (child row name, e.g. '0t4pqp90cs')
	- service.idx (1, 2, ...)
	- str(service.idx)
	- service.service_title
	- service.service_package
	"""
	if not approvals or not isinstance(approvals, dict):
		return None

	idx_str = str(service.idx or "")
	name_str = str(service.name or "")
	title_str = str(service.service_title or "").strip()
	pkg_str = str(service.service_package or "").strip()

	keys = [
		name_str,
		idx_str,
		title_str,
		title_str.lower(),
		pkg_str,
		pkg_str.lower(),
		f"Service {idx_str}",
		f"service-{idx_str}",
		f"JOB-{idx_str}",
	]

	for k in keys:
		if k:
			if k in approvals:
				return approvals[k]
			if str(k) in approvals:
				return approvals[str(k)]

	return None


class RepairOrder(Document):
	def before_insert(self):
		if not self.price_list:
			self.price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
		if not self.check_in_date:
			self.check_in_date = frappe.utils.now_datetime()
		if not self.taxes_and_charges:
			self.taxes_and_charges = get_or_create_default_tax_template(self.company)
		if not flt(self.tax_rate):
			self.tax_rate = 13.0
		if not self.approval_status or self.approval_status == "Draft":
			self.approval_status = "Estimating"

	def validate(self):
		self._validate_check_in()
		self._sync_approval_status_to_services()
		self._validate_vehicle_and_customer()
		self._validate_odometer()
		self._calculate_service_totals()
		self._calculate_taxes_and_totals()
		self._update_progress_and_job_timing()
		self._validate_authorization_guards()
		self._sync_declined_to_deferred_services()
		self._sync_completed_deferred_services()
		self._update_scheduling_capability_flags()

	def _update_scheduling_capability_flags(self):
		"""Compute is_mobile_capable across all active services for scheduling."""
		if not self.services:
			self.is_mobile_capable = 0
			return

		self.is_mobile_capable = 1 if all(cint(getattr(s, "is_mobile_capable", 0)) for s in self.services) else 0

	def on_update(self):
		self.link_vehicle_to_customer()

	def _validate_check_in(self):
		if not self.check_in_date:
			self.check_in_date = self.creation or frappe.utils.now_datetime()
		if self.vehicle_released and not self.checkout_date:
			self.checkout_date = frappe.utils.now_datetime()

	def link_vehicle_to_customer(self):
		"""Link repair order customer to vehicle if not already associated."""
		if not (self.vehicle and self.customer):
			return

		# Check primary customer first (fast indexed lookup)
		if frappe.db.get_value("Repair Vehicle", self.vehicle, "customer") == self.customer:
			return

		# Check child customers table
		if frappe.db.exists("Repair Vehicle Customer", {"parent": self.vehicle, "customer": self.customer}):
			return

		if frappe.db.exists("Repair Vehicle", self.vehicle):
			vehicle_doc = frappe.get_doc("Repair Vehicle", self.vehicle)
			vehicle_doc.add_customer(self.customer)

	def _sync_approval_status_to_services(self):
		"""
		If approval_status is set to Approved or Partially Approved (e.g. manually by advisor),
		synchronize non-declined services so they become Approved.
		"""
		has_approved = any(s.status == "Approved" for s in self.services)
		has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in self.services)

		# Normalize Draft to Estimating if not approved
		if not self.approval_status or self.approval_status == "Draft":
			if not has_approved:
				self.approval_status = "Estimating"

		for s in self.services:
			if s.status == "Draft":
				s.status = "Estimating"

		# If there are pending services (e.g. newly added supplement), approval_status cannot be "Approved"
		# unless the user explicitly changed approval_status to "Approved" on the form
		if has_approved and has_pending and not self.has_value_changed("approval_status"):
			self.approval_status = "Partially Approved"
			return

		if self.approval_status == "Approved":
			for s in self.services:
				if s.status != "Declined":
					s.status = "Approved"
		elif self.approval_status == "Partially Approved":
			if not has_approved and self.services:
				for s in self.services:
					if s.status != "Declined":
						s.status = "Approved"
						break

		if self.approval_status in ["Approved", "Partially Approved"]:
			if self.status in ["Draft Estimate", "Pending Authorization", "", None]:
				self.status = "Authorized - In Progress"

	def on_trash(self):
		if "System Manager" not in frappe.get_roles() and frappe.session.user != "Administrator":
			frappe.throw(
				_("Only Administrators and System Managers are allowed to delete Repair Orders."),
				frappe.PermissionError,
			)

	def submit(self):
		"""Save for non-submittable Repair Order to preserve API compatibility."""
		self.save(ignore_permissions=True)
		return self

	def cancel(self):
		"""Handle cancellation for non-submittable Repair Order."""
		self.on_cancel()
		self.save(ignore_permissions=True)
		return self

	def on_cancel(self):
		if self.sales_invoice:
			si_docstatus = frappe.db.get_value("Sales Invoice", self.sales_invoice, "docstatus")
			if si_docstatus == 1:
				frappe.throw(
					_("Cannot cancel Repair Order while active Sales Invoice {0} exists. Please cancel the Sales Invoice first.").format(
						self.sales_invoice
					)
				)
		self.status = "Cancelled"
		self.db_set("status", "Cancelled")

	@frappe.whitelist()
	def cancel_repair_order(self):
		self.on_cancel()
		self.save(ignore_permissions=True)

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

	def _validate_odometer(self):
		if self.odometer_in is not None and self.odometer_in < 0:
			frappe.throw(_("Odometer In cannot be negative."))

		if cint(self.odometer_out) > 0:
			if self.odometer_in is not None and cint(self.odometer_out) < cint(self.odometer_in):
				frappe.throw(_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(
					self.odometer_out, self.odometer_in
				))

	def _calculate_service_totals(self):
		"""
		Calculate labor amounts, parts totals, and combined service total for each service row.
		"""
		total_billed_hours = 0.0
		default_wh = frappe.db.get_single_value("Stock Settings", "default_warehouse") or "Stores - ISM"

		for s in self.services:
			# Auto-assign service title if missing
			if not s.service_title:
				s.service_title = s.service_package or _("Custom Service")

			# Check if flat rate from service row or linked package
			is_flat = cint(getattr(s, "is_flat_rate", 0))
			if not is_flat and s.service_package:
				pkg_flat = frappe.db.get_value("Service Package", s.service_package, "is_flat_rate")
				if pkg_flat:
					is_flat = 1
					s.is_flat_rate = 1

			labor_hours = flt(s.labor_hours)
			if is_flat:
				if s.labor_amount is None or (flt(s.labor_amount) == 0 and s.service_package):
					pkg_service_rate = frappe.db.get_value("Service Package", s.service_package, "service_rate")
					if pkg_service_rate is not None and pkg_service_rate != "":
						s.labor_amount = flt(pkg_service_rate, 2)
				s.labor_amount = flt(s.labor_amount, 2)
			else:
				if labor_hours and (s.labor_rate is None or s.labor_rate == ""):
					s.labor_rate = _get_default_labor_rate()
				labor_rate = flt(s.labor_rate)
				s.labor_amount = flt(labor_hours * labor_rate, 2)

			# Parse parts list
			parts = s.parts
			if isinstance(parts, str):
				try:
					parts = json.loads(parts) if parts else []
				except Exception:
					parts = []
			elif not isinstance(parts, list):
				parts = []

			# Calculate parts totals and format items
			parts_total = 0.0
			cleaned_parts = []
			for p in parts:
				if not isinstance(p, dict):
					continue
				qty = flt(p.get("qty", 1.0)) or 1.0
				rate = flt(p.get("rate", 0.0))
				amt = flt(qty * rate, 2)
				parts_total += amt
				cleaned_parts.append({
					"item_code": p.get("item_code", ""),
					"item_name": p.get("item_name", p.get("item_code", "")),
					"qty": qty,
					"uom": p.get("uom", "Nos"),
					"rate": rate,
					"amount": amt,
					"warehouse": p.get("warehouse") or default_wh,
				})

			s.parts = json.dumps(cleaned_parts)
			s.parts_total = flt(parts_total, 2)
			s.total_amount = flt(s.labor_amount + s.parts_total, 2)

			# Render clean HTML preview of parts inside service row
			s.parts_html = _render_parts_html(cleaned_parts)

			if s.status == "Approved":
				total_billed_hours += labor_hours

		self.total_billed_hours = total_billed_hours

	def _calculate_taxes_and_totals(self):
		has_approved = any(s.status == "Approved" for s in self.services)
		if has_approved:
			net = sum(flt(s.total_amount) for s in self.services if s.status == "Approved")
		else:
			net = sum(flt(s.total_amount) for s in self.services if s.status != "Declined")

		self.net_total = flt(net, 2)

		if not self.taxes_and_charges:
			self.taxes_and_charges = get_or_create_default_tax_template(self.company)

		tax_rate = flt(self.tax_rate)
		if not tax_rate:
			if self.taxes_and_charges:
				tax_rate = flt(frappe.db.get_value(
					"Sales Taxes and Charges",
					{"parent": self.taxes_and_charges},
					"rate"
				) or 13.0)
			else:
				tax_rate = 13.0
			self.tax_rate = tax_rate

		self.total_taxes_and_charges = flt(net * (tax_rate / 100.0), 2)
		self.grand_total = flt(self.net_total + self.total_taxes_and_charges, 2)
		self.outstanding_amount = flt(self.grand_total - flt(self.paid_amount), 2)

	def _update_progress_and_job_timing(self):
		approved_services = [s for s in self.services if s.status == "Approved"]
		if approved_services:
			completed_services = [s for s in approved_services if s.is_completed]
			self.progress_percent = flt((len(completed_services) / len(approved_services)) * 100.0, 1)
		else:
			self.progress_percent = 0.0

		for s in self.services:
			if s.is_completed and not s.completed_at:
				s.completed_at = now_datetime()
				s.completed_by = frappe.session.user
				if self.picked_up_at:
					s.actual_duration_hours = flt(time_diff_in_hours(s.completed_at, self.picked_up_at), 2)

	def _sync_declined_to_deferred_services(self):
		"""Log declined services into Vehicle Deferred Service for future follow-up."""
		if not self.vehicle:
			return

		declined = [s for s in self.services if s.status == "Declined"]
		if not declined:
			return

		veh_doc = None
		for s in declined:
			exists = frappe.db.exists(
				"Vehicle Deferred Service",
				{
					"parent": self.vehicle,
					"repair_order": self.name,
					"job_title": s.service_title,
				},
			)
			if not exists:
				if not veh_doc:
					veh_doc = frappe.get_doc("Repair Vehicle", self.vehicle)
				reason = (s.advisor_notes or "").strip()
				if not reason or reason.startswith("Correction code") or "NOTE: Unless otherwise explicitly stated" in reason:
					reason = _("Declined during customer authorization")
				veh_doc.append("deferred_services", {
					"job_title": s.service_title,
					"decline_date": today(),
					"estimated_amount": s.total_amount,
					"repair_order": self.name,
					"decline_reason": reason,
				})

		if veh_doc:
			veh_doc.save(ignore_permissions=True)

	def _sync_completed_deferred_services(self):
		"""Mark previously deferred services on this vehicle as Completed Later once completed."""
		if not self.vehicle:
			return

		completed_titles = [s.service_title for s in self.services if s.is_completed and s.service_title]
		if not completed_titles:
			return

		for title in completed_titles:
			frappe.db.sql("""
				UPDATE `tabVehicle Deferred Service`
				SET status = 'Completed Later'
				WHERE parent = %s
				  AND job_title = %s
				  AND status = 'Open Recommendation'
			""", (self.vehicle, title))

	def _validate_authorization_guards(self):
		"""
		Enforce strict authorization blocks:
		1. Status progression: cannot advance beyond draft/pending without customer approval.
		2. Vehicle location: cannot enter bay or be released without customer approval.
		3. Service completion: individual services cannot be marked completed unless approved.
		"""
		has_approved = any(s.status == "Approved" for s in self.services)
		has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in self.services)
		if has_pending and self.approval_status == "Approved":
			self.approval_status = "Partially Approved" if has_approved else "Pending Approval"

		# Auto-cascade if approval_status is Approved and no pending/estimating services
		if self.approval_status == "Approved":
			if not self.services:
				has_approved = True
			else:
				for s in self.services:
					if s.status != "Declined":
						s.status = "Approved"
						has_approved = True
		elif self.approval_status == "Partially Approved":
			if not has_approved and self.services:
				for s in self.services:
					if s.status not in ["Declined", "Estimating", "Draft", "Pending Approval"]:
						s.status = "Approved"
						has_approved = True
						break

		# Guard 1: Status progression block
		restricted_statuses = [
			"Authorized - In Progress",
			"Waiting on Parts",
			"Work Completed",
			"Billed / Paid",
			"Closed / Released",
		]
		if self.status in restricted_statuses:
			if not has_approved:
				frappe.throw(
					_(
						"Cannot advance Repair Order to '{0}' without customer authorization. "
						"Please obtain customer approval before proceeding with repair work."
					).format(self.status)
				)

		# Guard 2: Vehicle movement / bay location block
		restricted_locations = [
			"In Bay / Being Serviced",
			"Completed / Parked in Lot",
			"Released to Customer",
		]
		if self.vehicle_location in restricted_locations:
			if not has_approved:
				frappe.throw(
					_(
						"Vehicle cannot be moved to '{0}' without customer authorization on file. "
						"Please obtain customer approval first."
					).format(self.vehicle_location)
				)

		# Guard 3: Service completion block
		for s in self.services:
			if cint(s.is_completed) and s.status != "Approved":
				frappe.throw(
					_(
						"Service '{0}' cannot be completed because its status is '{1}'. "
						"Only Approved services can be completed by technicians."
					).format(s.service_title, s.status)
				)

	@frappe.whitelist()
	def pick_up_vehicle(self):
		"""Technician pulls vehicle from lot into service bay."""
		has_approved = any(s.status == "Approved" for s in self.services)
		if self.approval_status == "Approved":
			has_approved = True
		if self.approval_status not in ["Approved", "Partially Approved"] or not has_approved:
			frappe.throw(
				_(
					"Vehicle cannot be picked up into the bay without customer authorization on file. "
					"Please obtain customer approval first."
				)
			)
		self.picked_up_at = now_datetime()
		self.picked_up_by = frappe.session.user
		self.vehicle_location = "In Bay / Being Serviced"
		if self.status in ["Draft Estimate", "Pending Authorization"]:
			self.status = "Authorized - In Progress"

		if self.docstatus == 1:
			self.db_set("vehicle_location", self.vehicle_location)
			self.db_set("picked_up_at", self.picked_up_at)
			self.db_set("picked_up_by", self.picked_up_by)
			self.db_set("status", self.status)

		self.save(ignore_permissions=True)
		return {"status": "success", "vehicle_location": self.vehicle_location, "picked_up_at": self.picked_up_at}

	@frappe.whitelist()
	def park_vehicle(self):
		"""Technician parks vehicle back in lot."""
		self.parked_at = now_datetime()
		self.parked_by = frappe.session.user
		if self.picked_up_at:
			self.total_bay_hours = flt(time_diff_in_hours(self.parked_at, self.picked_up_at), 2)

		has_approved = any(s.status == "Approved" for s in self.services)
		has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in self.services)
		approved_services = [s for s in self.services if s.status == "Approved"]
		all_approved_completed = bool(has_approved and not has_pending and all(s.is_completed for s in approved_services))

		if all_approved_completed:
			self.vehicle_location = "Completed / Parked in Lot"
			self.status = "Work Completed"
		else:
			self.vehicle_location = "In Customer Lot"
			if self.status not in ["Authorized - In Progress", "Waiting on Parts"]:
				self.status = "Authorized - In Progress"

		if self.docstatus == 1:
			self.db_set("vehicle_location", self.vehicle_location)
			self.db_set("parked_at", self.parked_at)
			self.db_set("parked_by", self.parked_by)
			self.db_set("status", self.status)
			self.db_set("total_bay_hours", self.total_bay_hours)

		self.save(ignore_permissions=True)
		return {"status": "success", "vehicle_location": self.vehicle_location, "total_bay_hours": self.total_bay_hours}

	@frappe.whitelist()
	def toggle_service_completion(self, service_row_id=None, is_completed=None, **kwargs):
		"""Toggle individual service completion with timestamping."""
		if isinstance(service_row_id, dict):
			is_completed = service_row_id.get("is_completed", is_completed)
			service_row_id = service_row_id.get("service_row_id") or service_row_id.get("job_id")
		service_row_id = service_row_id or kwargs.get("service_row_id")
		if is_completed is None:
			is_completed = kwargs.get("is_completed", 0)

		target_service = None
		for s in self.services:
			if str(s.name) == str(service_row_id) or str(s.idx) == str(service_row_id) or str(s.service_title) == str(service_row_id):
				target_service = s
				break

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
			if self.picked_up_at:
				target_service.actual_duration_hours = flt(time_diff_in_hours(target_service.completed_at, self.picked_up_at), 2)
		else:
			target_service.completed_at = None
			target_service.completed_by = None
			target_service.actual_duration_hours = 0.0

		if target_service.name and not str(target_service.name).startswith("new-"):
			frappe.db.set_value("Repair Order Service", target_service.name, {
				"is_completed": target_service.is_completed,
				"completed_at": target_service.completed_at,
				"completed_by": target_service.completed_by,
				"actual_duration_hours": target_service.actual_duration_hours,
			}, update_modified=False)

		self._update_progress_and_job_timing()

		has_approved = any(s.status == "Approved" for s in self.services)
		has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in self.services)
		approved_services = [s for s in self.services if s.status == "Approved"]
		all_approved_completed = bool(has_approved and not has_pending and all(s.is_completed for s in approved_services))

		if all_approved_completed:
			if self.vehicle_location == "Completed / Parked in Lot":
				self.status = "Work Completed"
			else:
				# Keep status Authorized - In Progress until vehicle is moved/parked back in lot
				if self.status not in ["Authorized - In Progress", "Waiting on Parts"]:
					self.status = "Authorized - In Progress"
		elif self.status == "Work Completed" and not all_approved_completed:
			self.status = "Authorized - In Progress"
			if self.vehicle_location == "Completed / Parked in Lot":
				self.vehicle_location = "In Bay / Being Serviced" if self.picked_up_at else "In Customer Lot"

		if self.docstatus == 1:
			self.db_set("progress_percent", self.progress_percent)
			self.db_set("status", self.status)
			self.db_set("vehicle_location", self.vehicle_location)

		self.save(ignore_permissions=True)
		return {"status": "success", "progress_percent": self.progress_percent}

	# Backward compatibility alias
	@frappe.whitelist()
	def toggle_job_completion(self, job_id=None, is_completed=None, **kwargs):
		return self.toggle_service_completion(service_row_id=job_id, is_completed=is_completed, **kwargs)

	@frappe.whitelist()
	def generate_approval_token(self):
		"""Generate token for remote customer signature portal."""
		self.approval_token = secrets.token_urlsafe(32)
		self.approval_token_expiry = add_days(now_datetime(), 2)

		# Advance uncommunicated draft/estimating services to Pending Approval upon generating/sending approval link
		for s in self.services:
			if s.status in ["Draft", "Estimating"]:
				s.status = "Pending Approval"
				if s.name and not str(s.name).startswith("new-"):
					frappe.db.set_value("Repair Order Service", s.name, "status", "Pending Approval", update_modified=False)

		has_approved = any(s.status == "Approved" for s in self.services)
		if has_approved:
			self.approval_status = "Partially Approved"
		else:
			self.approval_status = "Pending Approval"
		self.save(ignore_permissions=True)
		return {"approval_token": self.approval_token, "expiry": self.approval_token_expiry}

	@frappe.whitelist()
	def authorize_jobs(self, approvals=None, channel="Remote Web Link", signer_name=None, signature_image=None, notes=None, **kwargs):
		"""
		Apply per-service customer decisions (Approved/Declined) and audit evidence.
		approvals: {"1": "Approved", "Brake Service": "Declined"}
		"""
		# Defensive unwrapping in case Frappe or caller passed wrapped dict in approvals
		if isinstance(approvals, dict) and ("approvals" in approvals or "channel" in approvals or "signer_name" in approvals):
			channel = approvals.get("channel") or channel
			signer_name = approvals.get("signer_name") or signer_name
			signature_image = approvals.get("signature_image") or signature_image
			notes = approvals.get("notes") or notes
			approvals = approvals.get("approvals")

		if isinstance(approvals, str):
			try:
				approvals = json.loads(approvals)
			except Exception:
				approvals = {}

		newly_approved = []
		newly_declined = []
		newly_authorized_amount = 0.0

		if not approvals:
			for s in self.services:
				if s.status != "Declined":
					if s.status != "Approved":
						newly_approved.append(s)
						newly_authorized_amount += flt(s.total_amount)
					s.status = "Approved"
		else:
			for s in self.services:
				# Prior authorized or declined services cannot be altered or revoked via customer portal
				if s.status in ["Approved", "Declined"]:
					continue

				decision = _match_service_decision(s, approvals)
				if decision is None and len(self.services) == 1 and len(approvals) == 1:
					decision = list(approvals.values())[0]

				if decision in ["Approved", "Declined"]:
					s.status = decision
					if decision == "Approved":
						newly_approved.append(s)
						newly_authorized_amount += flt(s.total_amount)
					elif decision == "Declined":
						newly_declined.append(s)
						s.is_completed = 0
						s.completed_at = None
						s.completed_by = None

		approved = [s for s in self.services if s.status == "Approved"]
		declined = [s for s in self.services if s.status == "Declined"]
		pending = [s for s in self.services if s.status in ["Draft", "Estimating", "Pending Approval"]]

		if approved and not pending and not declined:
			self.approval_status = "Approved"
		elif approved:
			self.approval_status = "Partially Approved"
		elif declined and not pending:
			self.approval_status = "Declined"
		else:
			self.approval_status = "Pending Approval"

		if self.approval_status in ["Approved", "Partially Approved"]:
			if self.status in ["Draft Estimate", "Pending Authorization", "", None]:
				self.status = "Authorized - In Progress"

		current_timestamp = now_datetime()

		# Backfill initial historical authorization log if table was empty but prior approval exists
		if (not getattr(self, "approval_logs", None) or len(self.approval_logs) == 0) and self.authorized_at and self.authorized_by:
			prior_approved = [s for s in self.services if s.status == "Approved" and s not in newly_approved]
			if prior_approved:
				self.append("approval_logs", {
					"approval_datetime": self.authorized_at,
					"signer_name": self.authorized_by,
					"channel": "Remote Web Link",
					"authorized_amount": flt(sum(flt(s.total_amount) for s in prior_approved), 2),
					"approved_services": ", ".join(f"{s.service_title} (${flt(s.total_amount):,.2f})" for s in prior_approved),
					"declined_services": ", ".join(s.service_title for s in self.services if s.status == "Declined" and s not in newly_declined) or _("None"),
					"signature_image": self.signature_image,
					"notes": _("Initial customer authorization on file"),
				})

		if newly_approved or newly_declined:
			self.authorized_by = signer_name or frappe.session.user
			self.authorized_at = current_timestamp

			if signature_image:
				if signature_image.startswith("data:image"):
					from frappe.utils.file_manager import save_file

					file_doc = save_file(
						fname=f"sig_{self.name}_{int(now_datetime().timestamp())}.png",
						content=signature_image,
						dt=self.doctype,
						dn=self.name,
						decode=True,
						is_private=0,
						df="signature_image",
					)
					self.signature_image = file_doc.file_url
				else:
					self.signature_image = signature_image

			if notes:
				self.verbal_notes = notes

			audit_payload = f"{self.name}|{self.authorized_by}|{self.authorized_at}|{json.dumps(approvals or {}, sort_keys=True)}"
			self.audit_hash = hashlib.sha256(audit_payload.encode()).hexdigest()

			approved_summary = ", ".join(f"{s.service_title} (${flt(s.total_amount):,.2f})" for s in newly_approved) or _("None")
			declined_summary = ", ".join(s.service_title for s in newly_declined) or _("None")

			valid_channels = [
				"Remote Web Link",
				"In-Person Digital",
				"In-Person Signature",
				"Phone Verbal",
				"Verbal Approval",
				"Written Work Order",
				"Advisor Manual",
			]
			channel_to_record = channel if channel in valid_channels else "In-Person Signature"

			self.append("approval_logs", {
				"approval_datetime": current_timestamp,
				"signer_name": self.authorized_by,
				"channel": channel_to_record,
				"authorized_amount": flt(newly_authorized_amount, 2),
				"approved_services": approved_summary,
				"declined_services": declined_summary,
				"signature_image": self.signature_image,
				"ip_address": kwargs.get("ip_address"),
				"user_agent": kwargs.get("user_agent"),
				"notes": notes or self.verbal_notes,
				"audit_hash": self.audit_hash,
			})

		for s in self.services:
			if s.name and not str(s.name).startswith("new-"):
				frappe.db.set_value("Repair Order Service", s.name, {
					"status": s.status,
					"is_completed": s.is_completed,
				}, update_modified=False)

		if self.docstatus == 1:
			self.db_set("approval_status", self.approval_status)
			self.db_set("status", self.status)
			self.db_set("authorized_by", self.authorized_by)
			self.db_set("authorized_at", self.authorized_at)
			self.db_set("audit_hash", self.audit_hash)
			if self.signature_image:
				self.db_set("signature_image", self.signature_image)
			if self.verbal_notes:
				self.db_set("verbal_notes", self.verbal_notes)

		self.save(ignore_permissions=True)
		return {
			"status": "success",
			"approval_status": self.approval_status,
			"order_status": self.status,
		}

	@frappe.whitelist()
	def add_service_package(self, service_package=None, **kwargs):
		"""Expand canned service package into a new Service row with nested Parts."""
		if isinstance(service_package, dict):
			service_package = service_package.get("service_package")
		service_package = service_package or kwargs.get("service_package")
		pkg = frappe.get_doc("Service Package", service_package)
		has_prior_authorization = bool(
			self.authorized_at
			or any(s.status == "Approved" for s in self.services)
			or self.approval_status in ["Approved", "Partially Approved"]
			or self.status in ["Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid"]
		)
		is_supp = 1 if (cint(kwargs.get("is_supplement", 0)) or has_prior_authorization) else 0

		parts_list = []
		parts_total = 0.0
		for row in pkg.items:
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

		labor_hours = flt(pkg.standard_labor_hours)
		is_flat = cint(pkg.is_flat_rate)
		if is_flat:
			labor_rate = flt(pkg.hourly_rate)
			labor_amount = flt(pkg.service_rate, 2)
		else:
			labor_rate = flt(pkg.hourly_rate) or _get_default_labor_rate()
			labor_amount = flt(labor_hours * labor_rate, 2)

		self.append("services", {
			"service_package": pkg.name,
			"correction_code": getattr(pkg, "correction_code", None),
			"service_title": pkg.package_name,
			"status": "Estimating",
			"is_flat_rate": is_flat,
			"labor_hours": labor_hours,
			"labor_rate": labor_rate,
			"labor_amount": labor_amount,
			"parts": json.dumps(parts_list),
			"parts_total": parts_total,
			"total_amount": flt(labor_amount + parts_total, 2),
			"parts_html": _render_parts_html(parts_list),
			"is_supplement": is_supp,
			"is_mobile_capable": cint(getattr(pkg, "is_mobile_capable", 0)),
			"tesla_manual_url": getattr(pkg, "tesla_manual_url", None),
			"advisor_notes": kwargs.get("advisor_notes", "") or "",
		})

		if is_supp:
			if any(s.status == "Approved" for s in self.services):
				self.approval_status = "Partially Approved"
			else:
				self.approval_status = "Pending Approval"

		self.save(ignore_permissions=True)
		return {"status": "success", "service": pkg.package_name}

	@frappe.whitelist()
	def add_supplement(self, service_title=None, category="Maintenance", labor_hours=1.0, labor_rate=120.0, parts=None, notes=None, job_title=None, **kwargs):
		"""Add a supplemental repair service to an existing Repair Order."""
		if isinstance(service_title, dict):
			category = service_title.get("category", category)
			labor_hours = service_title.get("labor_hours", labor_hours)
			labor_rate = service_title.get("labor_rate", labor_rate)
			parts = service_title.get("parts", parts)
			notes = service_title.get("notes", notes)
			job_title = service_title.get("job_title", job_title)
			service_title = service_title.get("service_title")
		title = service_title or job_title or kwargs.get("service_title") or kwargs.get("job_title") or "Supplemental Service"
		hours = flt(labor_hours)
		rate = flt(labor_rate) or 120.0
		labor_amount = flt(hours * rate, 2)

		parts_list = []
		parts_total = 0.0
		if parts:
			if isinstance(parts, str):
				parts = json.loads(parts)
			for p in parts:
				item_code = p.get("item_code")
				if item_code and frappe.db.exists("Item", item_code):
					item_doc = frappe.get_cached_doc("Item", item_code)
					qty = flt(p.get("qty", 1.0)) or 1.0
					part_rate = flt(p.get("rate", item_doc.standard_rate or 0.0))
					amt = flt(qty * part_rate, 2)
					parts_total += amt
					parts_list.append({
						"item_code": item_code,
						"item_name": item_doc.item_name,
						"qty": qty,
						"uom": getattr(item_doc, "stock_uom", "Nos"),
						"rate": part_rate,
						"amount": amt,
						"warehouse": p.get("warehouse") or frappe.db.get_single_value("Stock Settings", "default_warehouse") or "Stores - ISM",
					})

		self.append("services", {
			"service_title": title,
			"status": "Estimating",
			"labor_hours": hours,
			"labor_rate": rate,
			"labor_amount": labor_amount,
			"parts": json.dumps(parts_list),
			"parts_total": parts_total,
			"total_amount": flt(labor_amount + parts_total, 2),
			"parts_html": _render_parts_html(parts_list),
			"is_supplement": 1,
			"advisor_notes": notes or "",
		})

		if any(s.status == "Approved" for s in self.services):
			self.approval_status = "Partially Approved"
		else:
			self.approval_status = "Pending Approval"

		self.save(ignore_permissions=True)
		return {"status": "success", "service_title": title}

	@frappe.whitelist()
	def add_deferred_services(self, deferred_items=None, **kwargs):
		"""
		Add one or more previously deferred services to this Repair Order.
		deferred_items can be a list of job titles, dicts, or a JSON string.
		"""
		if isinstance(deferred_items, str):
			try:
				deferred_items = json.loads(deferred_items)
			except Exception:
				deferred_items = [deferred_items]
		elif not deferred_items and kwargs.get("services"):
			deferred_items = kwargs.get("services")
			if isinstance(deferred_items, str):
				try:
					deferred_items = json.loads(deferred_items)
				except Exception:
					deferred_items = [deferred_items]

		if not deferred_items:
			frappe.throw(_("No deferred services specified to add."))

		has_prior_authorization = bool(
			self.authorized_at
			or any(s.status == "Approved" for s in self.services)
			or self.approval_status in ["Approved", "Partially Approved"]
			or self.status in ["Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid"]
		)
		is_supp = 1 if has_prior_authorization else 0

		added_services = []
		for item in deferred_items:
			if isinstance(item, str):
				title = item.strip()
				est_amount = 0.0
			elif isinstance(item, dict):
				title = (item.get("job_title") or item.get("service_title") or "").strip()
				est_amount = flt(item.get("estimated_amount") if item.get("estimated_amount") is not None else item.get("estimated_cost", 0.0))
			else:
				continue

			if not title:
				continue

			# Avoid duplicate if exact service title already exists on this order
			if any(s.service_title and s.service_title.strip().lower() == title.lower() for s in self.services):
				continue

			# Check if there is an existing Service Package for this title
			pkg_name = frappe.db.get_value("Service Package", {"package_name": title}, "name")
			if not pkg_name and frappe.db.exists("Service Package", title):
				pkg_name = title

			if pkg_name:
				pkg = frappe.get_doc("Service Package", pkg_name)
				parts_list = []
				parts_total = 0.0
				default_wh = frappe.db.get_single_value("Stock Settings", "default_warehouse") or "Stores - ISM"
				for row in pkg.items:
					item_doc = frappe.get_cached_doc("Item", row.item_code)
					qty = flt(row.qty) or 1.0
					part_rate = flt(row.rate)
					amt = flt(qty * part_rate, 2)
					parts_total += amt
					parts_list.append({
						"item_code": row.item_code,
						"item_name": row.item_name or item_doc.item_name,
						"qty": qty,
						"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
						"rate": part_rate,
						"amount": amt,
						"warehouse": getattr(row, "warehouse", None) or default_wh,
					})

				labor_hours = flt(pkg.standard_labor_hours)
				is_flat = cint(pkg.is_flat_rate)
				if is_flat:
					labor_rate = flt(pkg.hourly_rate)
					labor_amount = flt(pkg.service_rate, 2)
				else:
					labor_rate = flt(pkg.hourly_rate) or _get_default_labor_rate()
					labor_amount = flt(labor_hours * labor_rate, 2)

				self.append("services", {
					"service_package": pkg.name,
					"correction_code": getattr(pkg, "correction_code", None),
					"service_title": pkg.package_name,
					"status": "Estimating",
					"is_flat_rate": is_flat,
					"labor_hours": labor_hours,
					"labor_rate": labor_rate,
					"labor_amount": labor_amount,
					"parts": json.dumps(parts_list),
					"parts_total": parts_total,
					"total_amount": flt(labor_amount + parts_total, 2),
					"parts_html": _render_parts_html(parts_list),
					"is_supplement": is_supp,
					"is_mobile_capable": cint(getattr(pkg, "is_mobile_capable", 0)),
					"tesla_manual_url": getattr(pkg, "tesla_manual_url", None),
					"advisor_notes": "",
				})
				added_services.append(title)
			else:
				self.append("services", {
					"service_title": title,
					"status": "Estimating",
					"is_flat_rate": 1,
					"labor_hours": 0.0,
					"labor_rate": 0.0,
					"labor_amount": est_amount,
					"parts": json.dumps([]),
					"parts_total": 0.0,
					"total_amount": est_amount,
					"parts_html": "",
					"is_supplement": is_supp,
					"advisor_notes": "",
				})
				added_services.append(title)

		if not added_services:
			return {
				"status": "noop",
				"message": _("Selected services are already present on this Repair Order."),
				"added_count": 0,
			}

		if is_supp:
			if any(s.status == "Approved" for s in self.services):
				self.approval_status = "Partially Approved"
			else:
				self.approval_status = "Pending Approval"

		self._calculate_service_totals()
		self._calculate_taxes_and_totals()
		self.save(ignore_permissions=True)
		return {"status": "success", "added_count": len(added_services), "added_services": added_services}

	@frappe.whitelist()
	def save_service_as_package(self, service_row_id=None, package_name=None, **kwargs):
		"""
		Save a one-off repair service and its nested parts as a reusable Service Package.
		"""
		if isinstance(service_row_id, dict):
			package_name = service_row_id.get("package_name", package_name)
			service_row_id = service_row_id.get("service_row_id")
		service_row_id = service_row_id or kwargs.get("service_row_id")
		package_name = package_name or kwargs.get("package_name")

		target = None
		for s in self.services:
			if str(s.name) == str(service_row_id) or str(s.idx) == str(service_row_id) or str(s.service_title) == str(service_row_id):
				target = s
				break

		if not target:
			frappe.throw(_("Service row {0} not found.").format(service_row_id))

		pkg_title = package_name or target.service_title
		if frappe.db.exists("Service Package", pkg_title):
			frappe.throw(_("A Service Package named '{0}' already exists.").format(pkg_title))

		parts = target.parts
		if isinstance(parts, str):
			try:
				parts = json.loads(parts) if parts else []
			except Exception:
				parts = []

		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": pkg_title,
			"correction_code": getattr(target, "correction_code", None),
			"is_flat_rate": cint(getattr(target, "is_flat_rate", 0)),
			"is_mobile_capable": cint(getattr(target, "is_mobile_capable", 0)),
			"standard_labor_hours": flt(target.labor_hours),
			"hourly_rate": flt(target.labor_rate),
			"service_rate": flt(target.labor_amount),
			"tesla_manual_url": getattr(target, "tesla_manual_url", None),
			"items": [
				{
					"item_code": p.get("item_code"),
					"item_name": p.get("item_name"),
					"qty": flt(p.get("qty", 1.0)),
					"rate": flt(p.get("rate", 0.0)),
				}
				for p in parts
				if p.get("item_code")
			]
		})
		pkg.insert(ignore_permissions=True)

		target.service_package = pkg.name
		if target.name and not str(target.name).startswith("new-"):
			frappe.db.set_value("Repair Order Service", target.name, "service_package", pkg.name, update_modified=False)

		return {
			"status": "success",
			"package_name": pkg.name,
			"message": _("Service Package '{0}' created successfully!").format(pkg.name)
		}

	@frappe.whitelist()
	def process_payment_and_release(self, payment_method="Debit / Interac", paid_amount=None, odometer_out=None, **kwargs):
		"""
		Finalize billing audit:
		1. Validates odometer out
		2. Creates background ERPNext Sales Invoice with update_stock=1
		3. Creates matching Payment Entry
		4. Marks vehicle released, stamps vehicle last service specs, and submits RO
		"""
		if isinstance(payment_method, dict):
			paid_amount = payment_method.get("paid_amount", paid_amount)
			odometer_out = payment_method.get("odometer_out", odometer_out)
			payment_method = payment_method.get("payment_method") or "Debit / Interac"
		payment_method = kwargs.get("payment_method") or payment_method
		if paid_amount is None and "paid_amount" in kwargs:
			paid_amount = kwargs.get("paid_amount")
		if odometer_out is None and "odometer_out" in kwargs:
			odometer_out = kwargs.get("odometer_out")
		if odometer_out is not None:
			self.odometer_out = cint(odometer_out)
		self._validate_odometer()

		approved_services = [s for s in self.services if s.status == "Approved"]
		if not approved_services:
			frappe.throw(_("Cannot process payment without approved services."))

		has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in self.services)
		if has_pending:
			frappe.throw(
				_(
					"Cannot close or release Repair Order while services are still in Estimating, Draft, or Pending Approval. "
					"Please obtain customer authorization or decline pending supplements before final billing and release."
				)
			)

		abbr = frappe.db.get_value("Company", self.company, "abbr") or "I"
		default_income_account = frappe.db.get_value(
			"Company", self.company, "default_income_account"
		) or f"Sales - {abbr}"
		default_cost_center = frappe.db.get_value(
			"Company", self.company, "cost_center"
		) or f"Main - {abbr}"
		default_warehouse = (
			frappe.db.get_value("Warehouse", {"company": self.company, "warehouse_name": "Stores"}, "name")
			or frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 0}, "name")
			or (frappe.db.get_single_value("Stock Settings", "default_warehouse") if frappe.db.get_value("Warehouse", frappe.db.get_single_value("Stock Settings", "default_warehouse"), "company") == self.company else None)
			or f"Stores - {abbr}"
		)

		tax_template = self.taxes_and_charges or get_or_create_default_tax_template(self.company)
		self.taxes_and_charges = tax_template

		si = frappe.new_doc("Sales Invoice")
		si.customer = self.customer
		si.company = self.company
		si.posting_date = today()
		si.currency = self.currency or "CAD"
		si.update_stock = 1
		si.disable_rounded_total = 1
		si.taxes_and_charges = tax_template

		# Add line items: labor and all nested parts from approved services
		for s in approved_services:
			# 1. Labor line
			if flt(s.labor_hours) > 0 or flt(s.labor_amount) > 0:
				default_labor = "LABOR-STD"
				if not frappe.db.exists("Item", default_labor):
					if not frappe.db.exists("UOM", "Hour"):
						frappe.get_doc({"doctype": "UOM", "uom_name": "Hour", "must_be_whole_number": 0}).insert(ignore_permissions=True)
					labor_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "Services"
					frappe.get_doc({
						"doctype": "Item",
						"item_code": default_labor,
						"item_name": "Standard Labor",
						"item_group": labor_group,
						"is_stock_item": 0,
						"stock_uom": "Hour",
					}).insert(ignore_permissions=True)
				if cint(getattr(s, "is_flat_rate", 0)):
					si.append("items", {
						"item_code": default_labor,
						"item_name": f"Labor: {s.service_title}",
						"description": s.advisor_notes or s.service_title,
						"qty": 1.0,
						"uom": "Unit" if frappe.db.exists("UOM", "Unit") else "Nos",
						"rate": flt(s.labor_amount),
						"income_account": default_income_account,
						"cost_center": default_cost_center,
					})
				else:
					si.append("items", {
						"item_code": default_labor,
						"item_name": f"Labor: {s.service_title}",
						"description": s.advisor_notes or s.service_title,
						"qty": flt(s.labor_hours) or 1.0,
						"uom": "Hour",
						"rate": flt(s.labor_rate),
						"income_account": default_income_account,
						"cost_center": default_cost_center,
					})

			# 2. Nested parts
			parts = s.parts
			if isinstance(parts, str):
				try:
					parts = json.loads(parts) if parts else []
				except Exception:
					parts = []
			for p in parts:
				item_code = p.get("item_code")
				if item_code and frappe.db.exists("Item", item_code):
					item_doc = frappe.get_cached_doc("Item", item_code)
					si.append("items", {
						"item_code": item_code,
						"item_name": p.get("item_name") or item_doc.item_name,
						"description": p.get("item_name") or item_doc.item_name,
						"qty": flt(p.get("qty", 1.0)),
						"uom": p.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
						"rate": flt(p.get("rate", 0.0)),
						"warehouse": (
							p.get("warehouse")
							if p.get("warehouse")
							and frappe.db.get_value("Warehouse", p.get("warehouse"), "company") == self.company
							else default_warehouse
						),
						"income_account": default_income_account,
						"cost_center": default_cost_center,
					})

		si.set_missing_values()
		if tax_template:
			si.set_taxes()

		# Guard: Ensure 13% HST tax row is present on Sales Invoice
		if not si.taxes:
			abbr = frappe.db.get_value("Company", self.company, "abbr") or "I"
			duties_and_taxes = (
				frappe.db.get_value("Account", {"account_name": "Duties and Taxes", "company": self.company}, "name")
				or f"Duties and Taxes - {abbr}"
			)
			account_head = (
				frappe.db.get_value("Account", {"account_name": ["in", ["HST 13%", "HST"]], "company": self.company}, "name")
				or (f"HST 13% - {abbr}" if frappe.db.exists("Account", f"HST 13% - {abbr}") else duties_and_taxes)
			)
			rate = flt(self.tax_rate) or 13.0
			si.append("taxes", {
				"charge_type": "On Net Total",
				"account_head": account_head,
				"description": f"HST @ {rate}%",
				"rate": rate,
			})

		si.calculate_taxes_and_totals()
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()

		self.sales_invoice = si.name

		# Process Payment Entry
		pay_amt = flt(paid_amount) if paid_amount is not None else flt(self.grand_total)
		if pay_amt > 0:
			pe = frappe.new_doc("Payment Entry")
			pe.payment_type = "Receive"
			pe.party_type = "Customer"
			pe.party = self.customer
			pe.company = self.company
			pe.paid_amount = pay_amt
			pe.received_amount = pay_amt

			mop = payment_method or "Debit / Interac"
			if not frappe.db.exists("Mode of Payment", mop):
				if frappe.db.exists("Mode of Payment", "Cash"):
					mop = "Cash"
				else:
					mop = frappe.db.get_value("Mode of Payment", {}, "name")
			pe.mode_of_payment = mop
			pe.paid_to = (
				frappe.db.get_value(
					"Mode of Payment Account",
					{"parent": pe.mode_of_payment, "company": self.company},
					"default_account",
				)
				or frappe.db.get_value("Company", self.company, "default_bank_account")
				or frappe.db.get_value("Company", self.company, "default_cash_account")
				or f"Cash - {frappe.db.get_value('Company', self.company, 'abbr') or 'I'}"
			)
			pe.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name": si.name,
				"total_amount": si.grand_total,
				"outstanding_amount": si.outstanding_amount,
				"allocated_amount": min(pay_amt, si.outstanding_amount),
			})
			pe.flags.ignore_permissions = True
			pe.insert()
			pe.submit()

			self.paid_amount = pay_amt
			self.outstanding_amount = flt(self.grand_total - pay_amt, 2)
			self.payment_status = "Paid" if self.outstanding_amount <= 0 else "Partially Paid"

		# Complete vehicle handover
		self.vehicle_location = "Released to Customer"
		self.vehicle_released = 1
		self.status = "Closed / Released"
		self.checkout_date = frappe.utils.now_datetime()

		# Update vehicle specs
		if self.vehicle:
			veh_doc = frappe.get_doc("Repair Vehicle", self.vehicle)
			veh_doc.last_odometer = self.odometer_out or self.odometer_in
			veh_doc.last_service_date = today()
			veh_doc.last_repair_order = self.name
			veh_doc.save(ignore_permissions=True)

		self.save(ignore_permissions=True)
		return {
			"status": "success",
			"sales_invoice": self.sales_invoice,
			"payment_status": self.payment_status,
			"vehicle_released": self.vehicle_released,
			"checkout_date": self.checkout_date,
		}

	@frappe.whitelist()
	def create_material_request(self):
		"""Create background ERPNext Material Request for all parts in approved services."""
		approved_services = [s for s in self.services if s.status == "Approved"]
		if not approved_services:
			frappe.throw(_("No approved services found on this order."))

		parts_to_order = []
		for s in approved_services:
			parts = s.parts
			if isinstance(parts, str):
				try:
					parts = json.loads(parts) if parts else []
				except Exception:
					parts = []
			for p in parts:
				item_code = p.get("item_code")
				if item_code and frappe.db.exists("Item", item_code):
					parts_to_order.append(p)

		if not parts_to_order:
			frappe.throw(_("No inventory parts found in approved services."))

		mr = frappe.new_doc("Material Request")
		mr.material_request_type = "Material Transfer"
		mr.company = self.company
		mr.transaction_date = today()
		mr.schedule_date = today()

		for part in parts_to_order:
			item_doc = frappe.get_cached_doc("Item", part.get("item_code"))
			mr.append("items", {
				"item_code": part.get("item_code"),
				"item_name": part.get("item_name") or item_doc.item_name,
				"description": part.get("item_name") or item_doc.item_name,
				"qty": flt(part.get("qty", 1.0)),
				"uom": part.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
				"warehouse": part.get("warehouse") or frappe.db.get_single_value("Stock Settings", "default_warehouse"),
				"schedule_date": today(),
			})

		mr.flags.ignore_permissions = True
		mr.insert()
		return {"material_request": mr.name}


# -------------------------------------------------------------
# Module-level Whitelisted RPC Wrappers
# -------------------------------------------------------------

def _extract_docname_from_request():
	if frappe.form_dict.get("docname"):
		return frappe.form_dict.get("docname")
	if frappe.form_dict.get("doc"):
		doc_data = frappe.form_dict.get("doc")
		if isinstance(doc_data, str):
			doc_data = json.loads(doc_data)
		return doc_data.get("name") if isinstance(doc_data, dict) else getattr(doc_data, "name", None)
	if frappe.form_dict.get("docs"):
		docs_data = frappe.form_dict.get("docs")
		if isinstance(docs_data, str):
			docs_data = json.loads(docs_data)
		if isinstance(docs_data, dict):
			return docs_data.get("name")
		elif isinstance(docs_data, list) and docs_data:
			return docs_data[0].get("name")
	frappe.throw(_("Repair Order name is required."))


@frappe.whitelist()
def get_service_package_details(service_package):
	"""Return package info and its parts list so the client can automatically populate."""
	if not service_package:
		return {}

	pkg = frappe.get_doc("Service Package", service_package)
	default_wh = frappe.db.get_single_value("Stock Settings", "default_warehouse") or "Stores - ISM"
	default_rate = _get_default_labor_rate()

	parts = []
	for row in pkg.items:
		item_doc = frappe.get_cached_doc("Item", row.item_code)
		qty = flt(row.qty) or 1.0
		rate = flt(row.rate)
		parts.append({
			"item_code": row.item_code,
			"item_name": row.item_name or item_doc.item_name,
			"qty": qty,
			"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
			"rate": rate,
			"amount": flt(qty * rate, 2),
			"warehouse": default_wh,
		})

	labor_hours = flt(pkg.standard_labor_hours)
	is_flat = cint(pkg.is_flat_rate)
	if is_flat:
		hourly_rate = flt(pkg.hourly_rate)
		service_rate = flt(pkg.service_rate, 2)
	else:
		hourly_rate = flt(pkg.hourly_rate) or default_rate
		service_rate = flt(pkg.service_rate) if flt(pkg.service_rate) else flt(labor_hours * hourly_rate, 2)

	pkg_cat = getattr(pkg, "category", None)
	if not pkg_cat and getattr(pkg, "correction_code", None):
		try:
			from ism.utils.tesla_manual import get_category_for_correction_code
			cat, _ = get_category_for_correction_code(pkg.correction_code)
			if cat:
				pkg_cat = cat
		except Exception:
			pass

	return {
		"package_name": pkg.package_name,
		"correction_code": getattr(pkg, "correction_code", None),
		"frt": getattr(pkg, "frt", None) or labor_hours,
		"tesla_manual_url": getattr(pkg, "tesla_manual_url", None),
		"category": pkg_cat or "Maintenance",
		"description": "",
		"is_flat_rate": is_flat,
		"is_mobile_capable": cint(getattr(pkg, "is_mobile_capable", 0)),
		"standard_labor_hours": labor_hours,
		"labor_hours": labor_hours,
		"hourly_rate": hourly_rate,
		"service_rate": service_rate,
		"parts": parts,
		"parts_total": sum(p["amount"] for p in parts),
	}


@frappe.whitelist()
def save_service_as_package(service_row_id=None, package_name=None, docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.save_service_as_package(service_row_id=service_row_id, package_name=package_name, **kwargs)


@frappe.whitelist()
def add_deferred_services(docname=None, deferred_items=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.add_deferred_services(deferred_items=deferred_items, **kwargs)


@frappe.whitelist()
def toggle_service_completion(service_row_id=None, is_completed=None, docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.toggle_service_completion(service_row_id=service_row_id, is_completed=is_completed, **kwargs)


@frappe.whitelist()
def toggle_job_completion(job_id=None, is_completed=None, docname=None, **kwargs):
	return toggle_service_completion(service_row_id=job_id, is_completed=is_completed, docname=docname, **kwargs)


@frappe.whitelist()
def authorize_jobs(docname=None, approvals=None, channel="Remote Web Link", signer_name=None, signature_image=None, notes=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.authorize_jobs(
		approvals=approvals,
		channel=channel,
		signer_name=signer_name,
		signature_image=signature_image,
		notes=notes,
		**kwargs,
	)


@frappe.whitelist()
def pick_up_vehicle(docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.pick_up_vehicle()


@frappe.whitelist()
def park_vehicle(docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.park_vehicle()


@frappe.whitelist()
def add_service_package(service_package=None, docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.add_service_package(service_package=service_package, **kwargs)


@frappe.whitelist()
def add_supplement(service_title=None, category="Maintenance", labor_hours=1.0, labor_rate=120.0, notes=None, parts=None, docname=None, job_title=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.add_supplement(service_title=service_title, category=category, labor_hours=labor_hours, labor_rate=labor_rate, parts=parts, notes=notes, job_title=job_title, **kwargs)


@frappe.whitelist()
def process_payment_and_release(payment_method="Debit / Interac", paid_amount=None, odometer_out=None, docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.process_payment_and_release(payment_method=payment_method, paid_amount=paid_amount, odometer_out=odometer_out, **kwargs)


@frappe.whitelist()
def generate_approval_token(docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.generate_approval_token()


@frappe.whitelist()
def create_material_request(docname=None, **kwargs):
	if not docname:
		docname = _extract_docname_from_request()
	ro = frappe.get_doc("Repair Order", docname)
	return ro.create_material_request()


@frappe.whitelist()
def get_filtered_vehicles(doctype, txt, searchfield, start, page_len, filters, as_dict=False, **kwargs):
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	txt_pattern = f"%{txt}%"
	start_offset = cint(start)
	limit_len = cint(page_len) or 20

	v = frappe.qb.DocType("Repair Vehicle")
	customer = filters.get("customer") if filters else None

	if customer:
		vc = frappe.qb.DocType("Repair Vehicle Customer")
		query = (
			frappe.qb.from_(v)
			.left_join(vc)
			.on(vc.parent == v.name)
			.select(v.name, v.license_plate, v.manufacturer, v.model, v.model_year)
			.distinct()
			.where(
				((v.customer == customer) | (vc.customer == customer))
				& (
					(v.name.like(txt_pattern))
					| (v.license_plate.like(txt_pattern))
					| (v.vin.like(txt_pattern))
					| (v.model.like(txt_pattern))
				)
			)
			.orderby(v.name)
			.offset(start_offset)
			.limit(limit_len)
		)
		return query.run(as_dict=as_dict)

	query = (
		frappe.qb.from_(v)
		.select(v.name, v.license_plate, v.manufacturer, v.model, v.model_year)
		.where(
			(v.name.like(txt_pattern))
			| (v.license_plate.like(txt_pattern))
			| (v.vin.like(txt_pattern))
			| (v.model.like(txt_pattern))
		)
		.orderby(v.name)
		.offset(start_offset)
		.limit(limit_len)
	)
	return query.run(as_dict=as_dict)


@frappe.whitelist()
def get_deferred_services_for_vehicle(vehicle: str):
	if not vehicle:
		return []

	services = frappe.get_all(
		"Vehicle Deferred Service",
		filters={
			"parent": vehicle,
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
		# Compatibility aliases for legacy or alternate field names
		s["service_title"] = s.get("job_title") or ""
		s["service_description"] = s.get("job_title") or ""
		s["estimated_cost"] = flt(s.get("estimated_amount") or 0.0)
		s["estimated_amount"] = flt(s.get("estimated_amount") or 0.0)

		# Sanitize residual Tesla manual scraped boilerplate if present
		reason = (s.get("decline_reason") or "").strip()
		if reason.startswith("Correction code") or "NOTE: Unless otherwise explicitly stated" in reason:
			s["decline_reason"] = _("Declined during customer authorization")

	return services


@frappe.whitelist()
def get_vehicle_customers(vehicle: str):
	"""Return list of all customers linked to this vehicle."""
	if not isinstance(vehicle, str) or not vehicle.strip():
		return []

	frappe.has_permission("Repair Vehicle", "read", throw=True)

	if not frappe.db.exists("Repair Vehicle", vehicle):
		return []

	vehicle_doc = frappe.get_doc("Repair Vehicle", vehicle)
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
		for c in frappe.get_all(
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
