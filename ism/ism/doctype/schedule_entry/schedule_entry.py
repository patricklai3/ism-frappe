# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from ism.ism.naming import generate_safe_series_name
from ism.scheduling.capacity import NON_BLOCKING_ENTRY_STATUSES
from ism.scheduling.time_utils import effective_end_time


def sync_repair_order_schedule_entries(repair_order_name: str, exclude_entry: str = None):
	"""Sync the informational Repair Order Schedule Entry child rows without saving the parent RO."""
	if not repair_order_name or not frappe.db.exists("Repair Order", repair_order_name):
		return
	try:
		filters = {"repair_order": repair_order_name}
		if exclude_entry:
			filters["name"] = ["!=", exclude_entry]
		entries = frappe.get_all(
			"Schedule Entry",
			filters=filters,
			fields=[
				"name",
				"entry_type",
				"scheduled_date",
				"scheduled_time",
				"estimated_duration",
				"service_bay",
				"assigned_technician",
				"status",
			],
			order_by="scheduled_date asc, scheduled_time asc",
		)
		frappe.db.delete(
			"Repair Order Schedule Entry",
			{"parent": repair_order_name, "parenttype": "Repair Order"},
		)
		for idx, entry in enumerate(entries, start=1):
			child = frappe.get_doc({
				"doctype": "Repair Order Schedule Entry",
				"parent": repair_order_name,
				"parenttype": "Repair Order",
				"parentfield": "schedule_entries",
				"idx": idx,
				"schedule_entry": entry.name,
				"entry_type": entry.entry_type,
				"scheduled_date": entry.scheduled_date,
				"scheduled_time": entry.scheduled_time,
				"estimated_duration": entry.estimated_duration,
				"service_bay": entry.service_bay,
				"assigned_technician": entry.assigned_technician,
				"status": entry.status,
			})
			child.db_insert()
	except Exception as e:
		frappe.log_error(f"Failed to sync schedule entries to Repair Order {repair_order_name}: {e}")


class ScheduleEntry(Document):
	def autoname(self):
		"""Assign naming series SE-.##### with safe series fallback on MariaDB 1020 contention without rollback."""
		from frappe.model.naming import make_autoname

		prefix = self.naming_series or "SE-.#####"
		try:
			self.name = make_autoname(prefix, doc=self)
		except Exception as e:
			if "1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError):
				self.name = generate_safe_series_name(self.doctype, prefix, doc=self)
			else:
				raise

	def get_display_customer(self) -> str:
		"""Returns linked Customer name, provisional customer name, or fallback 'Guest'."""
		if self.customer:
			return frappe.db.get_value("Customer", self.customer, "customer_name") or self.customer
		if self.provisional_customer_name:
			return self.provisional_customer_name
		return "Guest"

	def get_display_vehicle(self) -> str:
		"""Returns linked Repair Vehicle title/model/plate, or provisional vehicle info."""
		if self.repair_vehicle:
			v = frappe.db.get_value(
				"Repair Vehicle",
				self.repair_vehicle,
				["model_year", "manufacturer", "model", "license_plate", "vin"],
				as_dict=True,
			)
			if v:
				parts = [str(v.get(k)) for k in ["model_year", "model"] if v.get(k)]
				if parts:
					plate = v.get("license_plate")
					if plate:
						return f"{' '.join(parts)} ({plate})"
					return " ".join(parts)
				if v.get("vin"):
					return v.get("vin")
			return self.repair_vehicle
		if self.provisional_vehicle_info:
			return self.provisional_vehicle_info
		return ""

	def set_title(self):
		"""Set dynamic human-readable title for Desk header, calendar, and link fields."""
		customer = self.get_display_customer()
		vehicle = self.get_display_vehicle()
		if vehicle:
			self.title = f"{customer} | {vehicle}"
		else:
			entry_type_str = self.entry_type or "Appointment"
			self.title = f"{customer} ({entry_type_str})"

	def before_insert(self):
		self.populate_from_repair_order()
		self.set_title()

	def before_save(self):
		self._prev_repair_order = (
			frappe.db.get_value("Schedule Entry", self.name, "repair_order")
			if self.name and frappe.db.exists("Schedule Entry", self.name)
			else None
		)
		self.populate_from_repair_order()
		self.set_title()

	def on_update(self):
		prev_ro = getattr(self, "_prev_repair_order", None)
		if prev_ro and prev_ro != self.repair_order:
			sync_repair_order_schedule_entries(prev_ro)
		if self.repair_order:
			sync_repair_order_schedule_entries(self.repair_order)
		frappe.publish_realtime(
			"ism_calendar_update",
			{"schedule_entry": self.name, "bay": self.service_bay, "date": str(self.scheduled_date)},
		)

	def on_cancel(self):
		if self.repair_order:
			sync_repair_order_schedule_entries(self.repair_order)
		frappe.publish_realtime(
			"ism_calendar_update",
			{"schedule_entry": self.name, "bay": self.service_bay, "date": str(self.scheduled_date)},
		)

	def on_trash(self):
		if self.repair_order:
			sync_repair_order_schedule_entries(self.repair_order, exclude_entry=self.name)
		frappe.publish_realtime(
			"ism_calendar_update",
			{"schedule_entry": self.name, "bay": self.service_bay, "date": str(self.scheduled_date)},
		)

	@property
	def end_time(self) -> timedelta:
		"""Calculate lunch-aware effective end time as timedelta."""
		if not self.scheduled_time:
			return timedelta(0)
		return effective_end_time(self.scheduled_time, self.estimated_duration or 60)

	def get_effective_end_time(self) -> timedelta:
		"""Returns lunch-aware effective end time."""
		return self.end_time

	def validate(self):
		self.flags.scheduling_warnings = []
		self.validate_estimated_duration()
		self.validate_repair_order_requirement()
		self.validate_bay_availability()
		self.validate_technician_pool()
		self.validate_technician_assignment()

	def validate_estimated_duration(self):
		"""Enforce strictly positive appointment duration."""
		if cint(self.estimated_duration) <= 0:
			frappe.throw(
				_("Estimated Duration must be greater than 0 minutes."),
				frappe.ValidationError,
			)

	def populate_from_repair_order(self):
		"""Auto-populate customer, vehicle, and services summary from linked Repair Order."""
		if not self.repair_order:
			return

		ro_meta = frappe.db.get_value(
			"Repair Order",
			self.repair_order,
			["customer", "vehicle"],
			as_dict=True,
		)
		if not ro_meta:
			return

		if not self.customer and ro_meta.get("customer"):
			self.customer = ro_meta["customer"]

		if not self.repair_vehicle and ro_meta.get("vehicle"):
			self.repair_vehicle = ro_meta["vehicle"]

		if not self.items_summary:
			svc_rows = frappe.get_all(
				"Repair Order Service",
				filters={"parent": self.repair_order},
				fields=["service_title", "service_package", "labor_hours"],
				order_by="idx asc",
			)
			summary_lines = []
			for s in svc_rows:
				title = s.get("service_title") or s.get("service_package") or "Service"
				hrs = s.get("labor_hours") or 0
				summary_lines.append(f"• {title} ({hrs} hrs)")
			if summary_lines:
				self.items_summary = "\n".join(summary_lines)

	def validate_repair_order_requirement(self):
		"""Enforce linked Repair Order if required by Schedule Entry Type."""
		if not self.entry_type:
			return

		if not frappe.db.exists("Schedule Entry Type", self.entry_type):
			return

		requires_ro = frappe.db.get_value("Schedule Entry Type", self.entry_type, "requires_repair_order")
		if requires_ro and not self.repair_order:
			frappe.throw(
				_("Schedule Entry of type '{0}' requires a linked Repair Order.").format(self.entry_type),
				frappe.MandatoryError,
			)

	def validate_bay_availability(self):
		"""Validate physical bay availability for requested time window."""
		if not self.service_bay or not self.scheduled_date or not self.scheduled_time:
			return

		if self.status in NON_BLOCKING_ENTRY_STATUSES:
			return

		from ism.api.scheduling import check_bay_availability

		duration = self.estimated_duration or 60
		is_available = check_bay_availability(
			service_bay=self.service_bay,
			date=str(self.scheduled_date),
			start_time=self.scheduled_time,
			duration_minutes=duration,
			exclude_entry=self.name,
		)

		if not is_available:
			frappe.throw(
				_("Service Bay '{0}' is already occupied during the requested time window on {1}.").format(
					self.service_bay, self.scheduled_date
				),
				frappe.ValidationError,
			)

	def validate_technician_pool(self):
		"""Validate technician pool capacity gating."""
		if (
			not self.scheduled_date
			or not self.scheduled_time
			or self.status in NON_BLOCKING_ENTRY_STATUSES
		):
			return

		from ism.api.technician_availability import get_technician_pool_availability
		from ism.scheduling.time_utils import get_shop_settings

		settings = get_shop_settings()
		if not settings.get("enable_technician_capacity"):
			return

		pool = get_technician_pool_availability(
			date=str(self.scheduled_date),
			start_time=self.scheduled_time,
			duration_minutes=self.estimated_duration or 60,
			exclude_entry=self.name,
		)

		if not pool.get("is_available"):
			frappe.throw(
				_("Technician pool capacity exhausted for the requested time window on {0}.").format(
					self.scheduled_date
				),
				frappe.ValidationError,
			)

	def validate_technician_assignment(self):
		"""Collect non-blocking soft warnings if assigned technician has leave or overlap."""
		if (
			not self.assigned_technician
			or not self.scheduled_date
			or not self.scheduled_time
			or self.status in NON_BLOCKING_ENTRY_STATUSES
		):
			return

		from ism.api.technician_availability import check_technician_availability

		check = check_technician_availability(
			employee=self.assigned_technician,
			date=str(self.scheduled_date),
			start_time=self.scheduled_time,
			duration_minutes=self.estimated_duration or 60,
			exclude_entry=self.name,
		)

		warnings = getattr(self.flags, "scheduling_warnings", None)
		if warnings is None:
			warnings = []
			self.flags.scheduling_warnings = warnings

		if check.get("on_leave"):
			warnings.append(
				_("Assigned technician '{0}' is on approved leave on {1}.").format(
					self.assigned_technician, self.scheduled_date
				)
			)
		elif check.get("conflicts"):
			warnings.append(
				_("Assigned technician '{0}' has an overlapping Schedule Entry during this time window.").format(
					self.assigned_technician
				)
			)
