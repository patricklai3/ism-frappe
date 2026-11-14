# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import hashlib
import inspect
import json
import secrets
from typing import Any, Dict, List, Optional
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
from ism.api.parts import _resolve_default_warehouse as get_default_warehouse
from ism.ism.naming import generate_safe_series_name


def _get_default_labor_rate():
	try:
		if frappe.get_meta("ISM Settings").has_field("default_labor_rate"):
			return flt(frappe.db.get_single_value("ISM Settings", "default_labor_rate")) or 140.0
	except Exception:
		pass
	return 140.0


def _is_public_approval_portal_call() -> bool:
	"""Return True only when invoked directly from the token-verified guest approval endpoint."""
	for frame_info in inspect.stack():
		if frame_info.function == "submit_customer_approval" and frame_info.filename.replace("\\", "/").endswith(
			"ism/api/approval.py"
		):
			return True
	return False


def build_service_package_row_fields(
	pkg: Any,
	company: Optional[str] = None,
	cur_side: Optional[str] = None,
	cur_title: Optional[str] = None,
	cur_code: Optional[str] = None,
	default_labor_rate: Optional[float] = None,
	advisor_notes: str = "",
	is_supplement: int = 0,
) -> Dict[str, Any]:
	"""Expand a Service Package document into a canonical Repair Order Service row dict."""
	from ism.utils.tesla_manual import detect_service_side

	pkg_title = getattr(pkg, "package_name", None) or pkg.name
	pkg_code = getattr(pkg, "correction_code", None)
	rh_code = getattr(pkg, "rh_correction_code", None)
	is_symm = cint(getattr(pkg, "is_symmetrical", 0))

	if cur_side is None:
		base_side = detect_service_side(pkg_title, pkg_code, rh_code)
		cur_side = "LH" if is_symm and base_side != "RH" else ("RH" if is_symm and base_side == "RH" else "None")

	cur_title = cur_title or pkg_title
	cur_code = pkg_code if cur_code is None else cur_code

	labor_hours = flt(getattr(pkg, "standard_labor_hours", 0))
	is_flat = cint(getattr(pkg, "is_flat_rate", 0))
	if is_flat:
		labor_rate = flt(getattr(pkg, "hourly_rate", 0))
		labor_amount = flt(getattr(pkg, "service_rate", 0), 2)
	else:
		fallback_rate = flt(default_labor_rate) if default_labor_rate else _get_default_labor_rate()
		labor_rate = flt(getattr(pkg, "hourly_rate", 0)) or fallback_rate
		labor_amount = flt(labor_hours * labor_rate, 2)

	parts_list = []
	parts_total = 0.0
	warehouse = get_default_warehouse(company)
	for row in getattr(pkg, "items", None) or []:
		row_side = getattr(row, "side", None) or "Both"
		if cur_side in ("Both", "None") or row_side in ("Both", cur_side):
			item_doc = frappe.get_cached_doc("Item", row.item_code)
			qty = flt(row.qty) or 1.0
			rate = flt(getattr(item_doc, "standard_rate", 0)) if getattr(row, "rate", None) is None else flt(row.rate)
			amt = flt(qty * rate, 2)
			parts_total += amt
			parts_list.append({
				"item_code": row.item_code,
				"item_name": row.item_name or item_doc.item_name,
				"qty": qty,
				"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
				"rate": rate,
				"amount": amt,
				"warehouse": warehouse,
			})

	return {
		"service_package": pkg.name,
		"correction_code": cur_code,
		"service_title": cur_title,
		"service_position": cur_side,
		"status": "Estimating",
		"is_flat_rate": is_flat,
		"labor_hours": labor_hours,
		"labor_rate": labor_rate,
		"labor_amount": labor_amount,
		"parts": json.dumps(parts_list),
		"parts_total": parts_total,
		"total_amount": flt(labor_amount + parts_total, 2),
		"is_supplement": cint(is_supplement),
		"is_mobile_capable": cint(getattr(pkg, "is_mobile_capable", 0)),
		"tesla_manual_url": getattr(pkg, "tesla_manual_url", None),
		"advisor_notes": advisor_notes or "",
	}


def get_or_create_default_tax_template(company=None):
	"""Return the default 13% HST Sales Taxes and Charges Template for the company."""
	from ism.setup import get_or_create_default_sales_tax_template
	return get_or_create_default_sales_tax_template(company)


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
	def autoname(self):
		"""Assign naming series RO-.YYYY.-.##### with safe series fallback on MariaDB 1020 contention without rollback."""
		from frappe.model.naming import make_autoname

		prefix = self.naming_series or "RO-.YYYY.-.#####"
		try:
			self.name = make_autoname(prefix, doc=self)
		except Exception as e:
			if "1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError):
				self.name = generate_safe_series_name(self.doctype, prefix, doc=self)
			else:
				raise

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

		self._update_parts_waiting_status()

	def _check_parts_stock_availability(self):
		"""Check stock availability for all parts in approved services. Returns (is_all_available, deficit_items)."""
		approved_services = [s for s in self.services if s.status == "Approved"]
		if not approved_services:
			return True, []

		from ism.api.parts import get_item_available_stock
		wh = get_default_warehouse(self.company)
		deficit_items = []
		item_reqs = {}
		for s in approved_services:
			parts = s.parts
			if isinstance(parts, str):
				try:
					parts = json.loads(parts) if parts else []
				except Exception:
					parts = []
			for p in parts or []:
				code = p.get("item_code")
				if code and frappe.db.exists("Item", code):
					is_stock = frappe.db.get_value("Item", code, "is_stock_item")
					if cint(is_stock):
						item_reqs[code] = item_reqs.get(code, 0.0) + flt(p.get("qty", 1.0))

		for code, req_qty in item_reqs.items():
			avail_info = get_item_available_stock(code, wh, self.name)
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

	@frappe.whitelist()
	def check_parts_stock_availability(self):
		self.check_permission("read")
		all_avail, deficit = self._check_parts_stock_availability()
		updated = False
		if self.status == "Waiting on Parts" and all_avail:
			self.check_permission("write")
			self.status = "Authorized - In Progress"
			self.save()
			updated = True
		return {"all_available": all_avail, "deficit_items": deficit, "status": self.status, "updated": updated}

	def _update_parts_waiting_status(self):
		"""Update status between 'Waiting on Parts' and 'Authorized - In Progress' based on inventory."""
		if getattr(self, "has_shop_floor_override", 0) or getattr(self, "manual_parts_hold", 0):
			return
		if self.approval_status in ["Approved", "Partially Approved"]:
			if self.status in ["Draft Estimate", "Pending Authorization", "Waiting on Parts", "Authorized - In Progress", "", None]:
				all_available, _ = self._check_parts_stock_availability()
				if not all_available:
					self.status = "Waiting on Parts"
				else:
					self.status = "Authorized - In Progress"

	def on_trash(self):
		if "System Manager" not in frappe.get_roles() and frappe.session.user != "Administrator":
			frappe.throw(
				_("Only Administrators and System Managers are allowed to delete Repair Orders."),
				frappe.PermissionError,
			)

	def submit(self):
		"""Save for non-submittable Repair Order to preserve API compatibility."""
		self.check_permission("write")
		self.save()
		return self

	def cancel(self):
		"""Handle cancellation for non-submittable Repair Order."""
		self.check_permission("write")
		self.on_cancel()
		self.save()
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
		self.check_permission("write")
		self.on_cancel()
		self.save()

	def _validate_vehicle_and_customer(self):
		if not self.customer:
			frappe.throw(_("Customer is mandatory for Repair Order."), frappe.MandatoryError)
		if not self.vehicle:
			frappe.throw(_("Vehicle is mandatory for Repair Order."), frappe.MandatoryError)

		# Ensure vehicle exists (customer linking happens in on_update after validation succeeds)
		if not frappe.db.exists("Repair Vehicle", self.vehicle):
			frappe.throw(_("Repair Vehicle {0} does not exist.").format(self.vehicle))

	def _validate_odometer(self):
		if self.status != "Checked In":
			if self.odometer_in is None:
				frappe.throw(_("Odometer In is mandatory for Repair Order."), frappe.MandatoryError)

		if self.odometer_in is not None and self.odometer_in < 0:
			frappe.throw(_("Odometer In cannot be negative."))

		if cint(self.odometer_out) > 0:
			if self.odometer_in is not None and cint(self.odometer_out) < cint(self.odometer_in):
				frappe.throw(_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(
					self.odometer_out, self.odometer_in
				))

	@frappe.whitelist()
	def sync_schedule_entries(self, exclude_entry=None):
		"""Sync informational schedule entries child table with linked Schedule Entry records."""
		if not getattr(self.flags, "ignore_permissions", False):
			self.check_permission("read")
		if not self.name or not frappe.db.exists("Repair Order", self.name):
			return
		from ism.ism.doctype.schedule_entry.schedule_entry import sync_repair_order_schedule_entries

		sync_repair_order_schedule_entries(self.name, exclude_entry=exclude_entry)
		rows = frappe.get_all(
			"Repair Order Schedule Entry",
			filters={"parent": self.name, "parenttype": "Repair Order"},
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
		self.set("schedule_entries", rows)

	def _calculate_service_totals(self):
		"""
		Calculate labor amounts, parts totals, and combined service total for each service row.
		"""
		total_billed_hours = 0.0
		default_wh = get_default_warehouse(self.company)

		for s in self.services:
			# Auto-assign service title if missing
			if not s.service_title:
				s.service_title = s.service_package or _("Custom Service")

			# Check if flat rate from service row or linked package
			is_flat = cint(getattr(s, "is_flat_rate", 0))
			if not is_flat and s.service_package:
				pkg_flat = frappe.get_cached_value("Service Package", s.service_package, "is_flat_rate")
				if pkg_flat:
					is_flat = 1
					s.is_flat_rate = 1

			labor_hours = flt(s.labor_hours)
			if is_flat:
				if s.labor_amount is None or (flt(s.labor_amount) == 0 and s.service_package):
					pkg_service_rate = frappe.get_cached_value("Service Package", s.service_package, "service_rate")
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
				wh_filters = {"is_group": 0}
				if self.company:
					wh_filters["company"] = self.company
				part_wh = p.get("warehouse")
				valid_wh = part_wh if (part_wh and frappe.db.exists("Warehouse", {**wh_filters, "name": part_wh})) else default_wh

				code = p.get("item_code", "")
				cleaned_parts.append({
					"item_code": code,
					"item_name": p.get("item_name", code),
					"component_slot": p.get("component_slot") or p.get("item_name", code),
					"part_source": p.get("part_source", ""),
					"part_condition": p.get("part_condition", ""),
					"oem_part_number": p.get("oem_part_number", ""),
					"warranty_period": p.get("warranty_period", ""),
					"side": p.get("side", ""),
					"qty": qty,
					"uom": p.get("uom", "Nos"),
					"rate": rate,
					"amount": amt,
					"warehouse": valid_wh,
				})

			s.parts = json.dumps(cleaned_parts)
			s.parts_total = flt(parts_total, 2)
			s.total_amount = flt(s.labor_amount + s.parts_total, 2)

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
		self.check_permission("write")
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

		if self.status == "Waiting on Parts":
			all_avail, deficit = self._check_parts_stock_availability()
			if not all_avail:
				frappe.throw(
					_(
						"Cannot pick up vehicle into bay: Required parts have zero or insufficient stock in system. "
						"Please procure and receive all required parts into inventory first."
					)
				)

		self.picked_up_at = now_datetime()
		self.picked_up_by = frappe.session.user
		self.vehicle_location = "In Bay / Being Serviced"
		if self.status in ["Draft Estimate", "Pending Authorization", "Waiting on Parts"]:
			self.status = "Authorized - In Progress"

		if self.docstatus == 1:
			self.db_set("vehicle_location", self.vehicle_location)
			self.db_set("picked_up_at", self.picked_up_at)
			self.db_set("picked_up_by", self.picked_up_by)
			self.db_set("status", self.status)

		self.save()
		return {"status": "success", "vehicle_location": self.vehicle_location, "picked_up_at": self.picked_up_at}

	@frappe.whitelist()
	def park_vehicle(self):
		"""Technician parks vehicle back in lot."""
		self.check_permission("write")
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

		self.save()
		return {"status": "success", "vehicle_location": self.vehicle_location, "total_bay_hours": self.total_bay_hours}

	@frappe.whitelist()
	def toggle_service_completion(self, service_row_id=None, is_completed=None, **kwargs):
		"""Toggle individual service completion with timestamping."""
		self.check_permission("write")
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

		self.save()
		return {"status": "success", "progress_percent": self.progress_percent}

	@frappe.whitelist()
	def generate_approval_token(self):
		"""Generate token for remote customer signature portal."""
		self.check_permission("write")
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
		self.save()
		return {"approval_token": self.approval_token, "expiry": self.approval_token_expiry}

	@frappe.whitelist()
	def authorize_jobs(self, approvals=None, channel="Remote Web Link", signer_name=None, signature_image=None, notes=None, **kwargs):
		"""
		Apply per-service customer decisions (Approved/Declined) and audit evidence.
		approvals: {"1": "Approved", "Brake Service": "Declined"}
		"""
		is_public_portal = frappe.session.user == "Guest" and _is_public_approval_portal_call()
		if not getattr(self.flags, "ignore_permissions", False) and not is_public_portal:
			self.check_permission("write")

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

		self._update_parts_waiting_status()

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

		self.save(ignore_permissions=is_public_portal)
		return {
			"status": "success",
			"approval_status": self.approval_status,
			"order_status": self.status,
		}

	@frappe.whitelist()
	def add_service_package(self, service_package=None, **kwargs):
		"""Expand canned service package into new Service row(s) with nested Parts."""
		self.check_permission("write")
		from ism.utils.tesla_manual import swap_side_in_title

		if isinstance(service_package, dict):
			kwargs.update(service_package)
			service_package = service_package.get("service_package")
		service_package = service_package or kwargs.get("service_package")
		if not service_package or not isinstance(service_package, str):
			frappe.throw(_("Valid Service Package identifier is required."), frappe.ValidationError)
		frappe.has_permission("Service Package", "read", doc=service_package, throw=True)
		pkg = frappe.get_doc("Service Package", service_package)
		has_prior_authorization = bool(
			self.authorized_at
			or any(s.status == "Approved" for s in self.services)
			or self.approval_status in ["Approved", "Partially Approved"]
			or self.status in ["Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid"]
		)
		is_supp = 1 if (cint(kwargs.get("is_supplement", 0)) or has_prior_authorization) else 0

		from ism.utils.tesla_manual import swap_side_in_title, detect_service_side

		side_param = str(kwargs.get("side") or "").strip().lower()
		is_symm = cint(getattr(pkg, "is_symmetrical", 0))
		pkg_title = pkg.package_name
		pkg_code = getattr(pkg, "correction_code", None)
		raw_rh_code = getattr(pkg, "rh_correction_code", None)

		base_side = detect_service_side(pkg_title, pkg_code, raw_rh_code)
		if base_side == "RH":
			rh_title = pkg_title
			lh_title = swap_side_in_title(pkg_title, "LH")
			rh_code = pkg_code
			lh_code = getattr(pkg, "lh_correction_code", None) or pkg_code
		else:
			lh_title = pkg_title
			rh_title = swap_side_in_title(pkg_title, "RH")
			lh_code = pkg_code
			rh_code = raw_rh_code or pkg_code

		sides_to_add = []
		if side_param == "both" and is_symm:
			sides_to_add.append({"side": "LH", "title": lh_title, "code": lh_code})
			sides_to_add.append({"side": "RH", "title": rh_title, "code": rh_code})
		elif side_param == "rh" and is_symm:
			sides_to_add.append({"side": "RH", "title": rh_title, "code": rh_code})
		elif side_param == "lh" and is_symm:
			sides_to_add.append({"side": "LH", "title": lh_title, "code": lh_code})
		else:
			pos = "LH" if is_symm and base_side != "RH" else ("RH" if is_symm and base_side == "RH" else "None")
			sides_to_add.append({"side": pos, "title": pkg_title, "code": pkg_code})

		added_titles = []
		for entry in sides_to_add:
			cur_side = entry["side"]
			cur_title = entry["title"]
			cur_code = entry["code"]

			# Skip if this service side is already on this repair order
			if any(
				(s.service_title and s.service_title.strip().lower() == cur_title.strip().lower())
				or (pkg.name and s.service_package == pkg.name and getattr(s, "service_position", "") == cur_side and cur_side not in ("None", "Both", ""))
				for s in self.services
			):
				continue

			row_fields = build_service_package_row_fields(
				pkg=pkg,
				company=self.company,
				cur_side=cur_side,
				cur_title=cur_title,
				cur_code=cur_code,
				advisor_notes=kwargs.get("advisor_notes", "") or "",
				is_supplement=is_supp,
			)
			self.append("services", row_fields)
			added_titles.append(cur_title)

		if is_supp:
			if any(s.status == "Approved" for s in self.services):
				self.approval_status = "Partially Approved"
			else:
				self.approval_status = "Pending Approval"

		self.save()
		return {"status": "success", "service": ", ".join(added_titles)}

	@frappe.whitelist()
	def duplicate_service_for_opposite_side(self, service_row_id=None, **kwargs):
		"""Duplicate a service row for the opposite side (e.g. LH -> RH or RH -> LH)."""
		self.check_permission("write")
		from ism.utils.tesla_manual import swap_side_in_title, detect_service_side

		if not service_row_id:
			frappe.throw(_("Service Row ID is required."))

		target = None
		for s in self.services:
			if s.name == service_row_id or str(getattr(s, "idx", "")) == str(service_row_id):
				target = s
				break

		if not target:
			frappe.throw(_("Service row '{0}' not found on this Repair Order.").format(service_row_id))

		raw_pos = getattr(target, "service_position", None)
		curr_pos = raw_pos if raw_pos in ("LH", "RH") else detect_service_side(target.service_title)
		if curr_pos not in ("LH", "RH"):
			frappe.throw(_("Service '{0}' is not a symmetrical LH/RH operation.").format(target.service_title))

		opposite_pos = "RH" if curr_pos == "LH" else "LH"
		new_title = swap_side_in_title(target.service_title, opposite_pos)

		# Check if opposite side service already exists
		for s in self.services:
			if (s.service_title and s.service_title.strip().lower() == new_title.strip().lower()) or (
				target.service_package and s.service_package == target.service_package and getattr(s, "service_position", "") == opposite_pos
			):
				frappe.throw(_("Opposite side service '{0}' is already present on this Repair Order.").format(new_title))

		pkg = None
		if target.service_package and frappe.db.exists("Service Package", target.service_package):
			pkg = frappe.get_doc("Service Package", target.service_package)

		# Determine opposite correction code
		new_code = target.correction_code
		if opposite_pos == "RH":
			if pkg and getattr(pkg, "rh_correction_code", None):
				new_code = pkg.rh_correction_code
		elif opposite_pos == "LH":
			if pkg and getattr(pkg, "correction_code", None):
				new_code = pkg.correction_code

		# Determine parts for opposite side
		parts_list = []
		parts_total = 0.0
		if pkg and pkg.items:
			for item in pkg.items:
				item_side = getattr(item, "side", None) or "Both"
				if item_side in ("Both", opposite_pos):
					item_doc = frappe.get_cached_doc("Item", item.item_code)
					qty = flt(item.qty) or 1.0
					rate = flt(item.rate)
					amt = flt(qty * rate, 2)
					parts_total += amt
					parts_list.append({
						"item_code": item.item_code,
						"item_name": item.item_name or item_doc.item_name,
						"qty": qty,
						"uom": getattr(item, "uom", None) or item_doc.stock_uom or "Nos",
						"rate": rate,
						"amount": amt,
						"warehouse": get_default_warehouse(self.company),
					})
		else:
			try:
				old_parts = json.loads(target.parts) if isinstance(target.parts, str) else (target.parts or [])
				for p in old_parts:
					p_copy = dict(p)
					p_copy["item_name"] = swap_side_in_title(p_copy.get("item_name", ""), opposite_pos)
					parts_list.append(p_copy)
					parts_total += flt(p_copy.get("amount", 0))
			except Exception:
				parts_list = []

		has_prior_authorization = bool(
			self.authorized_at
			or any(s.status == "Approved" for s in self.services)
			or self.approval_status in ["Approved", "Partially Approved"]
			or self.status in ["Authorized - In Progress", "Waiting on Parts", "Work Completed", "Billed / Paid"]
		)
		is_supp = 1 if (has_prior_authorization or getattr(target, "is_supplement", 0)) else 0

		new_service = self.append("services", {
			"service_package": target.service_package,
			"correction_code": new_code,
			"service_title": new_title,
			"service_position": opposite_pos,
			"status": "Estimating",
			"is_flat_rate": target.is_flat_rate,
			"labor_hours": target.labor_hours,
			"labor_rate": target.labor_rate,
			"labor_amount": target.labor_amount,
			"parts": json.dumps(parts_list),
			"parts_total": parts_total,
			"total_amount": flt(flt(target.labor_amount) + parts_total, 2),
			"is_supplement": is_supp,
			"is_mobile_capable": target.is_mobile_capable,
			"tesla_manual_url": target.tesla_manual_url,
			"advisor_notes": "",
		})

		if is_supp:
			if any(s.status == "Approved" for s in self.services):
				self.approval_status = "Partially Approved"
			else:
				self.approval_status = "Pending Approval"

		self.save()
		return {
			"status": "success",
			"service_row": new_service.as_dict() if hasattr(new_service, "as_dict") else None,
			"service_title": new_title,
			"service_position": opposite_pos,
		}

	@frappe.whitelist()
	def add_supplement(self, service_title=None, category="Maintenance", labor_hours=1.0, labor_rate=None, parts=None, notes=None, job_title=None, **kwargs):
		"""Add a supplemental repair service to an existing Repair Order."""
		self.check_permission("write")
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
		rate = flt(labor_rate) if (labor_rate is not None and str(labor_rate).strip() != "") else _get_default_labor_rate()
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
						"warehouse": (p.get("warehouse") if (p.get("warehouse") and frappe.db.exists("Warehouse", p.get("warehouse"))) else get_default_warehouse(self.company)),
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
			"is_supplement": 1,
			"advisor_notes": notes or "",
		})

		if any(s.status == "Approved" for s in self.services):
			self.approval_status = "Partially Approved"
		else:
			self.approval_status = "Pending Approval"

		self.save()
		return {"status": "success", "service_title": title}

	@frappe.whitelist()
	def add_deferred_services(self, deferred_items=None, **kwargs):
		"""
		Add one or more previously deferred services to this Repair Order.
		deferred_items can be a list of job titles, dicts, or a JSON string.
		"""
		self.check_permission("write")
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
				default_wh = get_default_warehouse(self.company)
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
		self.save()
		return {"status": "success", "added_count": len(added_services), "added_services": added_services}

	@frappe.whitelist()
	def save_service_as_package(self, service_row_id=None, package_name=None, **kwargs):
		"""
		Save a one-off repair service and its nested parts as a reusable Service Package.
		"""
		self.check_permission("write")
		frappe.has_permission("Service Package", "create", throw=True)
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

		veh_model = frappe.db.get_value("Repair Vehicle", self.vehicle, "model") if self.vehicle else ""
		app_model = getattr(target, "applicable_model", None) or veh_model or ""

		pkg = frappe.get_doc({
			"doctype": "Service Package",
			"package_name": pkg_title,
			"applicable_model": app_model,
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
		pkg.insert()

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
		self.check_permission("write")
		frappe.has_permission("Sales Invoice", "create", throw=True)
		frappe.has_permission("Sales Invoice", "submit", throw=True)
		if self.vehicle:
			frappe.has_permission("Repair Vehicle", "write", doc=self.vehicle, throw=True)
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
		if hasattr(si, "repair_order") or frappe.db.has_column("Sales Invoice", "repair_order"):
			si.repair_order = self.name

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
					source = p.get("part_source") or getattr(item_doc, "part_source", "OEM") or "OEM"
					condition = p.get("part_condition") or getattr(item_doc, "part_condition", "New") or "New"
					warranty = p.get("warranty_period") or getattr(item_doc, "warranty_period", "") or ""
					compliance_tag = f"[{source} - {condition}"
					if warranty:
						compliance_tag += f" | Warranty: {warranty}"
					compliance_tag += "]"
					desc = f"{p.get('item_name') or item_doc.item_name} {compliance_tag}"

					si.append("items", {
						"item_code": item_code,
						"item_name": p.get("item_name") or item_doc.item_name,
						"description": desc,
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
		si.insert()
		si.submit()

		self.sales_invoice = si.name

		# Process Payment Entry
		pay_amt = flt(paid_amount) if paid_amount is not None else flt(self.grand_total)
		if pay_amt > 0:
			frappe.has_permission("Payment Entry", "create", throw=True)
			frappe.has_permission("Payment Entry", "submit", throw=True)
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
			veh_doc.save()

		complete_linked_schedule_entries(self.name)
		self.save()
		return {
			"status": "success",
			"sales_invoice": self.sales_invoice,
			"payment_status": self.payment_status,
			"vehicle_released": self.vehicle_released,
			"checkout_date": self.checkout_date,
		}

# -------------------------------------------------------------
# Module-level Standalone RPCs & Lifecycle Utilities
# -------------------------------------------------------------


@frappe.whitelist()
def get_service_package_details(service_package, side=None):
	"""Return package info and its parts list so the client can automatically populate."""
	frappe.has_permission("Service Package", "read", throw=True)
	if not service_package:
		return {}
	if not isinstance(service_package, str):
		frappe.throw(_("service_package must be a string."), frappe.ValidationError)
	if side is not None and not isinstance(side, str):
		frappe.throw(_("side must be a string."), frappe.ValidationError)

	pkg = frappe.get_doc("Service Package", service_package)
	pkg.check_permission("read")
	default_wh = get_default_warehouse()
	default_rate = _get_default_labor_rate()

	from ism.utils.tesla_manual import swap_side_in_title, detect_service_side

	is_symm = cint(getattr(pkg, "is_symmetrical", 0))
	rh_code = getattr(pkg, "rh_correction_code", None)
	target_side = str(side or "").strip().upper()

	title = pkg.package_name
	code = getattr(pkg, "correction_code", None)
	pos = "None"
	if is_symm:
		pos = "RH" if target_side == "RH" else "LH"
		base_side = detect_service_side(pkg.package_name, code, rh_code)
		if pos == "RH":
			title = swap_side_in_title(pkg.package_name, "RH") if base_side == "LH" else pkg.package_name
			if rh_code:
				code = rh_code
		elif pos == "LH":
			title = swap_side_in_title(pkg.package_name, "LH") if base_side == "RH" else pkg.package_name
			if getattr(pkg, "correction_code", None):
				code = pkg.correction_code

	slots_seen = set()
	parts = []
	unassigned_parts = []
	from ism.api.parts import _get_item_stock_qty, parse_slot_number, format_slot_label
	for row in pkg.items:
		row_side = getattr(row, "side", None) or "Both"
		if not is_symm or target_side not in ("LH", "RH") or row_side in ("Both", target_side):
			s_num = cint(getattr(row, "slot_number", None))
			if s_num == 0 and getattr(row, "component_slot", None):
				s_num = parse_slot_number(row.component_slot)

			slot = format_slot_label(s_num)
			is_def = cint(getattr(row, "is_default", 1))

			item_doc = frappe.get_cached_doc("Item", row.item_code)
			qty = flt(row.qty) or 1.0
			rate = flt(row.rate) if flt(row.rate) else flt(item_doc.standard_rate)
			stock_qty = _get_item_stock_qty(row.item_code)

			part_info = {
				"slot_number": s_num,
				"component_slot": slot,
				"item_code": row.item_code,
				"item_name": row.item_name or item_doc.item_name,
				"part_source": getattr(item_doc, "part_source", "OEM") or "OEM",
				"part_condition": getattr(item_doc, "part_condition", "New") or "New",
				"oem_part_number": getattr(item_doc, "oem_part_number", "") or "",
				"revision": getattr(item_doc, "revision", "") or "",
				"warranty_period": getattr(item_doc, "warranty_period", "12 Months / 20,000 km") or "12 Months / 20,000 km",
				"qty": qty,
				"uom": getattr(row, "uom", None) or item_doc.stock_uom or "Nos",
				"rate": rate,
				"amount": flt(qty * rate, 2),
				"warehouse": default_wh,
				"side": row_side,
				"stock_qty": stock_qty,
			}

			if s_num == 0:
				unassigned_parts.append(part_info)
				continue

			# If this component slot was already populated by a default item, skip alternate options for initial load
			if s_num in slots_seen and not is_def:
				continue
			if s_num in slots_seen and is_def:
				parts = [p for p in parts if p.get("slot_number") != s_num]

			parts.append(part_info)
			slots_seen.add(s_num)

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
			cat, _subcat = get_category_for_correction_code(pkg.correction_code)
			if cat:
				pkg_cat = cat
		except Exception:
			pass

	app_model = getattr(pkg, "applicable_model", None)
	app_gen = getattr(pkg, "applicable_generation", None)
	if not app_model and getattr(pkg, "tesla_manual_url", None):
		try:
			from ism.utils.tesla_manual import parse_tesla_manual_url
			u_meta = parse_tesla_manual_url(pkg.tesla_manual_url)
			app_model = u_meta.get("applicable_model") or ""
			if not app_gen:
				app_gen = u_meta.get("matched_generation") or ""
		except Exception:
			pass

	return {
		"package_name": title,
		"applicable_model": app_model or "",
		"applicable_generation": app_gen or "",
		"correction_code": code,
		"service_position": pos,
		"is_symmetrical": is_symm,
		"rh_correction_code": rh_code,
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
		"unassigned_parts": unassigned_parts,
		"parts_total": sum(p["amount"] for p in parts),
	}


def complete_linked_schedule_entries(ro_name):
	"""Mark non-cancelled Schedule Entries for a completed/released Repair Order as Completed."""
	if not ro_name or not frappe.db.exists("DocType", "Schedule Entry"):
		return []
	frappe.has_permission("Schedule Entry", "write", throw=True)
	open_entries = frappe.get_list(
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
	)
	return open_entries


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
	frappe.has_permission("Repair Vehicle", "read", throw=True)
	if not vehicle or not isinstance(vehicle, str) or not vehicle.strip():
		return []
	if not frappe.db.exists("Repair Vehicle", vehicle):
		return []
	frappe.has_permission("Repair Vehicle", "read", doc=vehicle, throw=True)

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


def on_stock_update_event(doc, method=None):
	"""Re-evaluate stock on Repair Orders in 'Waiting on Parts' when incoming inventory arrives."""
	waiting_orders = frappe.get_all(
		"Repair Order",
		filters={"status": "Waiting on Parts"},
		order_by="creation asc, name asc",
		pluck="name",
	)
	for ro_name in waiting_orders:
		try:
			ro = frappe.get_doc("Repair Order", ro_name)
			all_avail, _ = ro._check_parts_stock_availability()
			if all_avail:
				ro.status = "Authorized - In Progress"
				ro.flags.ignore_permissions = True
				ro.save()
		except Exception:
			pass

