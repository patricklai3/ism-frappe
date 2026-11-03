# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


def sync_repair_order_schedule_entries(repair_order_name: str, exclude_entry: str = None):
	"""Sync the informational Schedule Entry child table on the linked Repair Order."""
	if not repair_order_name or not frappe.db.exists("Repair Order", repair_order_name):
		return
	try:
		ro = frappe.get_doc("Repair Order", repair_order_name)
		ro.sync_schedule_entries(exclude_entry=exclude_entry)
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
				self.name = self._generate_safe_series_name(prefix)
			else:
				raise

	def _generate_safe_series_name(self, series_pattern: str) -> str:
		import re
		from frappe.utils import now_datetime

		today = now_datetime()
		parts = series_pattern.split(".")
		prefix_parts = []
		digits = 5
		for p in parts:
			if p.startswith("#"):
				digits = len(p)
				break
			elif p == "YYYY":
				prefix_parts.append(today.strftime("%Y"))
			elif p == "YY":
				prefix_parts.append(today.strftime("%y"))
			elif p == "MM":
				prefix_parts.append(today.strftime("%m"))
			elif p == "DD":
				prefix_parts.append(today.strftime("%d"))
			elif hasattr(self, p) and getattr(self, p):
				prefix_parts.append(str(getattr(self, p)))
			else:
				prefix_parts.append(p)
		prefix = "".join(prefix_parts)
		existing = frappe.get_all(
			self.doctype,
			filters=[["name", "like", f"{prefix}%"]],
			order_by="name desc",
			limit=1,
			pluck="name",
		)
		next_num = 1
		if existing and existing[0].startswith(prefix):
			suffix = existing[0][len(prefix):]
			match = re.search(r"(\d+)", suffix)
			if match:
				try:
					next_num = int(match.group(1)) + 1
				except ValueError:
					next_num = 1
		candidate = f"{prefix}{next_num:0{digits}d}"
		while frappe.db.exists(self.doctype, candidate):
			next_num += 1
			candidate = f"{prefix}{next_num:0{digits}d}"
		return candidate

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

	def on_cancel(self):
		if self.repair_order:
			sync_repair_order_schedule_entries(self.repair_order)

	def on_trash(self):
		if self.repair_order:
			sync_repair_order_schedule_entries(self.repair_order, exclude_entry=self.name)

	@property
	def end_time(self) -> timedelta:
		"""Calculate lunch-aware effective end time as timedelta."""
		if not self.scheduled_time:
			return timedelta(0)
		try:
			from ism.api.scheduling import effective_end_time
			return effective_end_time(self.scheduled_time, self.estimated_duration or 60)
		except Exception:
			from frappe.utils import to_timedelta
			return to_timedelta(self.scheduled_time) + timedelta(minutes=self.estimated_duration or 60)

	def get_effective_end_time(self) -> timedelta:
		"""Returns lunch-aware effective end time."""
		return self.end_time

	def validate(self):
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

		if not frappe.db.exists("Repair Order", self.repair_order):
			return

		ro_doc = frappe.get_doc("Repair Order", self.repair_order)

		# 1. Fetch Customer
		if not self.customer and getattr(ro_doc, "customer", None):
			self.customer = ro_doc.customer

		# 2. Fetch Repair Vehicle (field on Repair Order is 'vehicle')
		if not self.repair_vehicle and getattr(ro_doc, "vehicle", None):
			self.repair_vehicle = ro_doc.vehicle

		# 3. Populate services summary if empty
		if not self.items_summary and hasattr(ro_doc, "services") and ro_doc.services:
			summary_lines = []
			for s in ro_doc.services:
				title = getattr(s, "service_title", "") or getattr(s, "service_package", "") or "Service"
				hrs = getattr(s, "labor_hours", 0)
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

		if self.status == "Cancelled":
			return

		try:
			from ism.api.scheduling import check_bay_availability

			duration = self.estimated_duration or 60
			is_available = check_bay_availability(
				service_bay=self.service_bay,
				date=str(self.scheduled_date),
				start_time=self.scheduled_time,
				duration_minutes=duration,
				exclude_entry=self.name,
			)
		except ImportError:
			is_available = self._check_bay_availability_fallback()

		if not is_available:
			frappe.throw(
				_("Service Bay '{0}' is already occupied during the requested time window on {1}.").format(
					self.service_bay, self.scheduled_date
				),
				frappe.ValidationError,
			)

	def _check_bay_availability_fallback(self) -> bool:
		"""Fallback interval overlap check when scheduling API is not yet loaded."""
		from datetime import timedelta
		from frappe.utils import to_timedelta

		prop_start = to_timedelta(self.scheduled_time)
		prop_duration = timedelta(minutes=self.estimated_duration or 60)
		prop_end = prop_start + prop_duration

		filters = [
			["service_bay", "=", self.service_bay],
			["scheduled_date", "=", self.scheduled_date],
			["status", "!=", "Cancelled"],
		]
		if self.name:
			filters.append(["name", "!=", self.name])

		existing_entries = frappe.get_all(
			"Schedule Entry",
			filters=filters,
			fields=["name", "scheduled_time", "estimated_duration"],
		)

		for entry in existing_entries:
			ex_start = to_timedelta(entry.get("scheduled_time"))
			ex_duration = timedelta(minutes=entry.get("estimated_duration") or 60)
			ex_end = ex_start + ex_duration

			# Interval overlap check: max(start1, start2) < min(end1, end2)
			if max(prop_start, ex_start) < min(prop_end, ex_end):
				return False

		return True

	def validate_technician_pool(self):
		"""Validate technician pool capacity gating."""
		if not self.scheduled_date or not self.scheduled_time or self.status == "Cancelled":
			return

		try:
			from ism.api.scheduling import get_shop_settings
			from ism.api.technician_availability import get_technician_pool_availability
		except ImportError:
			return

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
		"""Issue non-blocking soft warnings if assigned technician has leave or overlap."""
		if not self.assigned_technician or not self.scheduled_date or not self.scheduled_time or self.status == "Cancelled":
			return

		try:
			from ism.api.technician_availability import check_technician_availability
		except ImportError:
			return

		check = check_technician_availability(
			employee=self.assigned_technician,
			date=str(self.scheduled_date),
			start_time=self.scheduled_time,
			duration_minutes=self.estimated_duration or 60,
			exclude_entry=self.name,
		)

		if check.get("on_leave"):
			frappe.msgprint(
				_("Warning: Assigned technician '{0}' is on approved leave on {1}.").format(
					self.assigned_technician, self.scheduled_date
				),
				alert=True,
				indicator="red",
			)
		elif check.get("conflicts"):
			frappe.msgprint(
				_("Warning: Assigned technician '{0}' has an overlapping Schedule Entry during this time window.").format(
					self.assigned_technician
				),
				alert=True,
				indicator="orange",
			)
