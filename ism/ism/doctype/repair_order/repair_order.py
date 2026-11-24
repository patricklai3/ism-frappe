# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, now_datetime, time_diff_in_hours

from ism.ism.doctype.repair_order.authorization import (
	_match_service_decision,
	authorize_repair_order_jobs,
	issue_approval_token,
	sync_approval_status_to_services,
	validate_authorization_guards,
)
from ism.ism.doctype.repair_order.billing import (
	process_ro_payment_and_release,
	record_ro_deposit,
)
from ism.ism.doctype.repair_order.fulfillment import (
	check_ro_parts_stock_availability,
	complete_linked_schedule_entries,
	handle_cancel_check,
	handle_stock_update_event,
	link_ro_vehicle_to_customer,
	park_ro_vehicle,
	pick_up_ro_vehicle,
	query_deferred_services_for_vehicle,
	query_filtered_vehicles,
	query_vehicle_customers,
	sync_completed_deferred_services,
	sync_declined_to_deferred_services,
	sync_ro_schedule_entries,
	toggle_ro_service_completion,
	update_parts_waiting_status,
)
from ism.ism.doctype.repair_order.pricing import (
	_get_default_labor_rate,
	add_deferred_services_to_ro,
	add_service_package_to_ro,
	add_supplement_to_ro,
	build_service_package_row_fields,
	calculate_service_totals,
	calculate_taxes_and_totals,
	duplicate_service_row_opposite_side,
	fetch_service_package_details,
	get_or_create_default_tax_template,
	save_service_row_as_package,
)
from ism.ism.naming import generate_safe_series_name
from ism.utils.vehicle_display import (
	format_vehicle_display,
	validate_repair_vehicle_ready_for_check_in,
)


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
			self.company = (
				frappe.defaults.get_user_default("Company")
				or frappe.db.get_single_value("Global Defaults", "default_company")
			)
		if not self.check_in_date:
			self.check_in_date = now_datetime()
		if self.tax_rate is None:
			self.tax_rate = 13.0
		if not self.taxes_and_charges and frappe.utils.flt(self.tax_rate) != 0.0:
			self.taxes_and_charges = get_or_create_default_tax_template(self.company)
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
		self._update_scheduling_capability_flags()

	def _update_scheduling_capability_flags(self):
		"""Compute is_mobile_capable across all active services for scheduling."""
		if not self.services:
			self.is_mobile_capable = 0
			return
		self.is_mobile_capable = 1 if all(cint(getattr(s, "is_mobile_capable", 0)) for s in self.services) else 0

	def on_update(self):
		self.link_vehicle_to_customer()
		self._sync_declined_to_deferred_services()
		self._sync_completed_deferred_services()

	def on_update_after_submit(self):
		self._sync_declined_to_deferred_services()
		self._sync_completed_deferred_services()

	def _validate_check_in(self):
		if not self.check_in_date:
			self.check_in_date = self.creation or now_datetime()
		if self.vehicle_released and not self.checkout_date:
			self.checkout_date = now_datetime()

	def link_vehicle_to_customer(self):
		"""Link repair order customer to vehicle if not already associated."""
		link_ro_vehicle_to_customer(self)

	def _sync_approval_status_to_services(self):
		sync_approval_status_to_services(self)

	def _check_parts_stock_availability(self):
		return check_ro_parts_stock_availability(self)

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
		update_parts_waiting_status(self)

	def on_trash(self):
		if "System Manager" not in frappe.get_roles() and frappe.session.user != "Administrator":
			frappe.throw(
				_("Only Administrators and System Managers are allowed to delete Repair Orders."),
				frappe.PermissionError,
			)
		if frappe.db.exists("DocType", "Shop Board Thread"):
			linked_threads = frappe.get_all(
				"Shop Board Thread",
				filters={"repair_order": self.name},
				pluck="name",
			)
			for t_name in linked_threads:
				if not frappe.db.exists("Shop Board Event", {"thread": t_name}):
					frappe.delete_doc("Shop Board Thread", t_name, force=True, ignore_permissions=True)

	def submit(self):
		"""Save for non-submittable Repair Order to preserve API compatibility."""
		self.check_permission("write")
		self.save()
		return self

	def cancel(self):
		"""Handle cancellation for non-submittable Repair Order."""
		self.check_permission("write")
		self._cancelling_via_save = True
		try:
			self.on_cancel()
			self.save()
		finally:
			self._cancelling_via_save = False
		return self

	def on_cancel(self):
		handle_cancel_check(self, persist_direct=not getattr(self, "_cancelling_via_save", False))

	@frappe.whitelist()
	def cancel_repair_order(self):
		self.check_permission("write")
		self._cancelling_via_save = True
		try:
			self.on_cancel()
			self.save()
		finally:
			self._cancelling_via_save = False

	def _validate_vehicle_and_customer(self):
		if not self.customer:
			frappe.throw(_("Customer is mandatory for Repair Order."), frappe.MandatoryError)
		if not self.vehicle:
			frappe.throw(_("Vehicle is mandatory for Repair Order."), frappe.MandatoryError)
		if not frappe.db.exists("Repair Vehicle", self.vehicle):
			frappe.throw(_("Repair Vehicle {0} does not exist.").format(self.vehicle))
		if self.status == "Checked In" or (self.is_new() and self.status != "Cancelled"):
			validate_repair_vehicle_ready_for_check_in(self.vehicle)
		self.vehicle_display = format_vehicle_display(self.vehicle)[:140]

	def _validate_odometer(self):
		if self.odometer_in is None or self.odometer_in == "":
			frappe.throw(_("Odometer In is mandatory for Repair Order."), frappe.MandatoryError)
		if cint(self.odometer_in) < 0:
			frappe.throw(_("Odometer In cannot be negative."))
		if cint(self.odometer_out) > 0 and cint(self.odometer_out) < cint(self.odometer_in):
			frappe.throw(
				_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(
					self.odometer_out, self.odometer_in
				)
			)

	@frappe.whitelist()
	def sync_schedule_entries(self, exclude_entry=None):
		"""Sync informational schedule entries child table with linked Schedule Entry records."""
		sync_ro_schedule_entries(self, exclude_entry=exclude_entry)

	def _calculate_service_totals(self):
		calculate_service_totals(self)

	def _calculate_taxes_and_totals(self):
		calculate_taxes_and_totals(self)

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
		sync_declined_to_deferred_services(self)

	def _sync_completed_deferred_services(self):
		sync_completed_deferred_services(self)

	def _validate_authorization_guards(self):
		validate_authorization_guards(self)

	@frappe.whitelist()
	def pick_up_vehicle(self):
		"""Technician pulls vehicle from lot into service bay."""
		return pick_up_ro_vehicle(self)

	@frappe.whitelist()
	def park_vehicle(self, odometer_out=None, require_odometer=False, temporary=False):
		"""Technician parks vehicle back in lot."""
		return park_ro_vehicle(
			self,
			odometer_out=odometer_out,
			require_odometer=require_odometer,
			temporary=temporary,
		)

	@frappe.whitelist()
	def toggle_service_completion(self, service_row_id=None, is_completed=None, **kwargs):
		"""Toggle individual service completion with timestamping."""
		return toggle_ro_service_completion(
			self,
			service_row_id=service_row_id,
			is_completed=is_completed,
			**kwargs,
		)

	@frappe.whitelist()
	def generate_approval_token(self):
		"""Generate token for remote customer signature portal."""
		return issue_approval_token(self)

	@frappe.whitelist()
	def authorize_jobs(
		self,
		approvals=None,
		channel="Remote Web Link",
		signer_name=None,
		signature_image=None,
		notes=None,
		**kwargs,
	):
		"""Apply per-service customer decisions (Approved/Declined) and audit evidence."""
		return authorize_repair_order_jobs(
			self,
			approvals=approvals,
			channel=channel,
			signer_name=signer_name,
			signature_image=signature_image,
			notes=notes,
			**kwargs,
		)

	@frappe.whitelist()
	def add_service_package(self, service_package=None, **kwargs):
		"""Expand canned service package into new Service row(s) with nested Parts."""
		return add_service_package_to_ro(self, service_package=service_package, **kwargs)

	@frappe.whitelist()
	def duplicate_service_for_opposite_side(self, service_row_id=None, **kwargs):
		"""Duplicate a service row for the opposite side (e.g. LH -> RH or RH -> LH)."""
		return duplicate_service_row_opposite_side(self, service_row_id=service_row_id, **kwargs)

	@frappe.whitelist()
	def add_supplement(
		self,
		service_title=None,
		category="Maintenance",
		labor_hours=1.0,
		labor_rate=None,
		parts=None,
		notes=None,
		job_title=None,
		**kwargs,
	):
		"""Add a supplemental repair service to an existing Repair Order."""
		return add_supplement_to_ro(
			self,
			service_title=service_title,
			category=category,
			labor_hours=labor_hours,
			labor_rate=labor_rate,
			parts=parts,
			notes=notes,
			job_title=job_title,
			**kwargs,
		)

	@frappe.whitelist()
	def add_deferred_services(self, deferred_items=None, **kwargs):
		"""Add one or more previously deferred services to this Repair Order."""
		return add_deferred_services_to_ro(self, deferred_items=deferred_items, **kwargs)

	@frappe.whitelist()
	def save_service_as_package(self, service_row_id=None, package_name=None, **kwargs):
		"""Save a one-off repair service and its nested parts as a reusable Service Package."""
		return save_service_row_as_package(
			self,
			service_row_id=service_row_id,
			package_name=package_name,
			**kwargs,
		)

	@frappe.whitelist()
	def record_deposit(self, amount, payment_method="Debit / Interac", reference_no=None, **kwargs):
		"""Record an upfront customer deposit Payment Entry against this Repair Order."""
		return record_ro_deposit(
			self,
			amount=amount,
			payment_method=payment_method,
			reference_no=reference_no,
			**kwargs,
		)

	@frappe.whitelist()
	def process_payment_and_release(
		self,
		payment_method="Debit / Interac",
		paid_amount=None,
		odometer_out=None,
		payments=None,
		phone_key_removed_confirmed=False,
		**kwargs,
	):
		"""Finalize billing audit, create Sales Invoice and Payment Entry, and release vehicle."""
		return process_ro_payment_and_release(
			self,
			payment_method=payment_method,
			paid_amount=paid_amount,
			odometer_out=odometer_out,
			payments=payments,
			phone_key_removed_confirmed=phone_key_removed_confirmed,
			**kwargs,
		)


@frappe.whitelist()
def get_service_package_details(service_package, side=None):
	"""Return package info and its parts list so the client can automatically populate."""
	return fetch_service_package_details(service_package, side=side)


@frappe.whitelist()
def get_filtered_vehicles(doctype, txt, searchfield, start, page_len, filters, as_dict=False, **kwargs):
	return query_filtered_vehicles(
		doctype=doctype,
		txt=txt,
		searchfield=searchfield,
		start=start,
		page_len=page_len,
		filters=filters,
		as_dict=as_dict,
		**kwargs,
	)


@frappe.whitelist()
def get_deferred_services_for_vehicle(vehicle: str):
	return query_deferred_services_for_vehicle(vehicle=vehicle)


@frappe.whitelist()
def get_vehicle_customers(vehicle: str):
	"""Return list of all customers linked to this vehicle."""
	return query_vehicle_customers(vehicle=vehicle)


def on_stock_update_event(doc, method=None):
	"""Re-evaluate stock on Repair Orders in 'Waiting on Parts' when incoming inventory arrives."""
	handle_stock_update_event(doc, method=method)


__all__ = [
	"RepairOrder",
	"_get_default_labor_rate",
	"_match_service_decision",
	"build_service_package_row_fields",
	"complete_linked_schedule_entries",
	"get_deferred_services_for_vehicle",
	"get_filtered_vehicles",
	"get_or_create_default_tax_template",
	"get_service_package_details",
	"get_vehicle_customers",
	"on_stock_update_event",
]
