# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from typing import Any, Dict, List, Optional, Union
from datetime import date, datetime, time, timedelta

import frappe
from frappe import _
from frappe.utils import cint


def to_timedelta(val: Union[str, time, timedelta, int, float, None]) -> timedelta:
	"""Convert various time representations into a datetime.timedelta object."""
	if val is None:
		return timedelta(0)
	if isinstance(val, timedelta):
		return val
	if isinstance(val, time):
		return timedelta(hours=val.hour, minutes=val.minute, seconds=val.second)
	if isinstance(val, (int, float)):
		return timedelta(seconds=int(val))
	if isinstance(val, str):
		val = val.strip()
		if not val:
			return timedelta(0)
		parts = val.split(":")
		try:
			hours = int(parts[0])
			minutes = int(parts[1]) if len(parts) > 1 else 0
			seconds = int(float(parts[2])) if len(parts) > 2 else 0
			return timedelta(hours=hours, minutes=minutes, seconds=seconds)
		except (ValueError, IndexError):
			return timedelta(0)
	return timedelta(0)


def format_timedelta(td: timedelta) -> str:
	"""Format a timedelta object into a HH:MM string."""
	total_seconds = int(td.total_seconds())
	hours = total_seconds // 3600
	minutes = (total_seconds % 3600) // 60
	return f"{hours:02d}:{minutes:02d}"


# Support 00:00:00 - 00:00:00 operating hours boundary (closed shop / disabled hours)
try:
	from ism.ism.doctype.ism_settings.ism_settings import ISMSettings

	def _safe_validate_operating_hours(self):
		if self.operating_hours_start and self.operating_hours_end:
			start = to_timedelta(self.operating_hours_start)
			end = to_timedelta(self.operating_hours_end)
			if not (start == end == timedelta(0)):
				if start >= end:
					frappe.throw(_("Operating Hours Start must be before Operating Hours End."))

		if self.default_slot_interval is not None and self.default_slot_interval <= 0:
			frappe.throw(_("Default Slot Interval must be greater than 0 minutes."))

		if self.scheduling_horizon_days is not None and self.scheduling_horizon_days <= 0:
			frappe.throw(_("Scheduling Horizon must be greater than 0 days."))

	def _safe_validate_break_hours(self):
		if self.break_start and self.break_end:
			b_start = to_timedelta(self.break_start)
			b_end = to_timedelta(self.break_end)
			if b_start >= b_end:
				frappe.throw(_("Break Start must be before Break End."))

			if self.operating_hours_start and self.operating_hours_end:
				op_start = to_timedelta(self.operating_hours_start)
				op_end = to_timedelta(self.operating_hours_end)
				if op_start == op_end == timedelta(0):
					return
				if b_start < op_start or b_end > op_end:
					frappe.throw(_("Lunch break window must be within shop operating hours."))

	ISMSettings._validate_operating_hours = _safe_validate_operating_hours
	ISMSettings._validate_break_hours = _safe_validate_break_hours
except Exception:
	pass


def get_shop_settings() -> Dict[str, Any]:
	"""Retrieve shop scheduling parameters from ISM Settings."""
	settings = {
		"operating_hours_start": timedelta(hours=8),
		"operating_hours_end": timedelta(hours=17),
		"break_start": timedelta(hours=12),
		"break_end": timedelta(hours=12, minutes=30),
		"default_slot_interval": 30,
		"scheduling_horizon_days": 30,
		"enable_technician_capacity": 1,
		"technician_designation": "Technician",
		"holiday_list": None,
		"default_diagnostic_duration": 60,
	}

	if frappe.db.exists("DocType", "ISM Settings"):
		try:
			doc = frappe.get_single("ISM Settings")
			if doc.operating_hours_start is not None and str(doc.operating_hours_start).strip() != "":
				settings["operating_hours_start"] = to_timedelta(doc.operating_hours_start)
			if doc.operating_hours_end is not None and str(doc.operating_hours_end).strip() != "":
				settings["operating_hours_end"] = to_timedelta(doc.operating_hours_end)
			if doc.break_start is not None and str(doc.break_start).strip() != "":
				settings["break_start"] = to_timedelta(doc.break_start)
			else:
				settings["break_start"] = None
			if doc.break_end is not None and str(doc.break_end).strip() != "":
				settings["break_end"] = to_timedelta(doc.break_end)
			else:
				settings["break_end"] = None
			if getattr(doc, "default_slot_interval", None):
				settings["default_slot_interval"] = int(doc.default_slot_interval)
			if getattr(doc, "scheduling_horizon_days", None):
				settings["scheduling_horizon_days"] = int(doc.scheduling_horizon_days)
			if hasattr(doc, "enable_technician_capacity") and doc.enable_technician_capacity is not None:
				settings["enable_technician_capacity"] = cint(doc.enable_technician_capacity)
			if getattr(doc, "technician_designation", None):
				settings["technician_designation"] = doc.technician_designation
			if getattr(doc, "holiday_list", None):
				settings["holiday_list"] = doc.holiday_list
			if getattr(doc, "default_diagnostic_duration", None):
				settings["default_diagnostic_duration"] = int(doc.default_diagnostic_duration)
		except Exception:
			pass

	return settings


@frappe.whitelist()
def effective_end_time(
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	break_start: Union[str, time, timedelta, None] = None,
	break_end: Union[str, time, timedelta, None] = None,
) -> timedelta:
	"""Calculate clock-time end of an appointment, accounting for lunch break window.

	- If job starts during lunch window [break_start, break_end), effective start
	  is pushed to break_end and work duration begins from there.
	- If job starts before lunch window and naive end spans past break_start,
	  break duration is added to end time.
	- If job ends at or before break_start, or starts at or after break_end,
	  no break adjustment is added.
	"""
	start_td = to_timedelta(start_time)
	duration_td = timedelta(minutes=float(duration_minutes or 0))

	if break_start is None or break_start == "" or break_end is None or break_end == "":
		settings = get_shop_settings()
		bs_td = settings["break_start"] if (break_start is None or break_start == "") else to_timedelta(break_start)
		be_td = settings["break_end"] if (break_end is None or break_end == "") else to_timedelta(break_end)
	else:
		bs_td = to_timedelta(break_start)
		be_td = to_timedelta(break_end)

	if bs_td and be_td and be_td > bs_td:
		break_duration = be_td - bs_td
		if bs_td <= start_td < be_td:
			return be_td + duration_td
		elif start_td < bs_td and (start_td + duration_td) > bs_td:
			return start_td + duration_td + break_duration

	return start_td + duration_td


@frappe.whitelist()
def check_bay_availability(
	service_bay: str,
	date: str,
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
) -> bool:
	"""Check if service_bay is available for proposed date, start_time, and duration.

	Uses lunch-aware intervals for both proposed and existing Schedule Entries.
	Returns True if bay is free, False if an overlap collision exists.
	"""
	if not service_bay or not date:
		return False

	settings = get_shop_settings()
	bs_td = settings["break_start"]
	be_td = settings["break_end"]

	prop_start = to_timedelta(start_time)
	prop_end = effective_end_time(prop_start, duration_minutes, bs_td, be_td)

	filters = [
		["service_bay", "=", service_bay],
		["scheduled_date", "=", str(date)],
		["status", "!=", "Cancelled"],
	]
	if exclude_entry:
		filters.append(["name", "!=", exclude_entry])

	existing_entries = frappe.get_all(
		"Schedule Entry",
		filters=filters,
		fields=["name", "scheduled_time", "estimated_duration"],
	)

	for entry in existing_entries:
		ex_start = to_timedelta(entry.get("scheduled_time"))
		ex_duration = entry.get("estimated_duration") or 0
		ex_end = effective_end_time(ex_start, ex_duration, bs_td, be_td)

		# Interval overlap check: max(start1, start2) < min(end1, end2)
		if max(prop_start, ex_start) < min(prop_end, ex_end):
			return False

	return True


@frappe.whitelist()
def get_available_bays(
	date: str,
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	required_tags: Optional[Union[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
	"""Return active Service Bays matching required equipment tags and free during interval."""
	if isinstance(required_tags, str):
		try:
			required_tags = frappe.parse_json(required_tags)
		except Exception:
			required_tags = [t.strip() for t in required_tags.split(",") if t.strip()]
	if not required_tags:
		required_tags = []

	required_tags_set = set(required_tags)

	active_bays = frappe.get_all(
		"Service Bay",
		filters={"is_active": 1},
		fields=["name", "bay_name"],
		order_by="name asc",
	)

	available_bays = []
	for bay in active_bays:
		tags = frappe.get_all(
			"Service Bay Equipment",
			filters={"parent": bay.get("name")},
			pluck="equipment_tag",
		)
		bay_tags_set = set(tags)

		if not required_tags_set.issubset(bay_tags_set):
			continue

		if check_bay_availability(bay.get("name"), date, start_time, duration_minutes):
			available_bays.append({
				"bay_name": bay.get("name"),
				"equipment_tags": tags,
			})

	return available_bays


@frappe.whitelist()
def auto_assign_bay(
	date: str,
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	required_tags: Optional[Union[str, List[str]]] = None,
) -> str:
	"""Auto-assign first available capable Service Bay using earliest-fit strategy.

	Raises ValidationError if technician capacity exhausted or no capable bay available.
	"""
	settings = get_shop_settings()
	if settings.get("enable_technician_capacity"):
		try:
			from ism.api.technician_availability import get_technician_pool_availability
			pool = get_technician_pool_availability(date, start_time, duration_minutes)
			if not pool.get("is_available"):
				frappe.throw(
					_("Technician pool capacity exhausted for the selected time window on {0}.").format(date),
					frappe.ValidationError,
				)
		except ImportError:
			pass

	bays = get_available_bays(date, start_time, duration_minutes, required_tags)
	if not bays:
		frappe.throw(
			_("No available service bay with required equipment for the selected time window."),
			frappe.ValidationError,
		)
	return bays[0]["bay_name"]


@frappe.whitelist()
def get_available_slots(
	date: str,
	duration_minutes: Union[int, float],
	required_tags: Optional[Union[str, List[str]]] = None,
	service_bay: Optional[str] = None,
) -> List[Dict[str, Any]]:
	"""Return available slot dicts for date and duration across operating hours.

	Excludes lunch break slots and slots ending after operating hours.
	Reports available_bays, available_technicians, effective_capacity, and bottleneck.
	"""
	settings = get_shop_settings()
	op_start = settings["operating_hours_start"]
	op_end = settings["operating_hours_end"]
	bs_td = settings["break_start"]
	be_td = settings["break_end"]
	interval = settings["default_slot_interval"] or 30
	enable_tech = settings.get("enable_technician_capacity")

	if op_start >= op_end or interval <= 0:
		return []

	duration_val = float(duration_minutes or 0)
	if duration_val <= 0:
		duration_val = float(settings.get("default_diagnostic_duration") or 60)

	curr_time = op_start
	slots = []

	while curr_time < op_end:
		# 1. Skip slot if curr_time falls inside lunch break
		if bs_td and be_td and bs_td <= curr_time < be_td:
			curr_time += timedelta(minutes=interval)
			continue

		# 2. Check effective end time does not exceed operating hours
		eff_end = effective_end_time(curr_time, duration_val, bs_td, be_td)
		if eff_end > op_end:
			curr_time += timedelta(minutes=interval)
			continue

		# 3. Check bay availability
		if service_bay:
			if check_bay_availability(service_bay, date, curr_time, duration_val):
				avail_bays_count = 1
			else:
				avail_bays_count = 0
		else:
			avail_bays = get_available_bays(date, curr_time, duration_val, required_tags)
			avail_bays_count = len(avail_bays)

		# 4. Check technician capacity if enabled
		if enable_tech:
			try:
				from ism.api.technician_availability import get_technician_pool_availability
				tech_pool = get_technician_pool_availability(date, curr_time, duration_val)
				avail_techs_count = tech_pool.get("available", 0)
			except ImportError:
				avail_techs_count = None

			if avail_techs_count is not None:
				eff_cap = min(avail_bays_count, avail_techs_count)
				if avail_techs_count < avail_bays_count:
					bottleneck = "technician"
				elif avail_bays_count < avail_techs_count:
					bottleneck = "bay"
				else:
					bottleneck = "none"
			else:
				eff_cap = avail_bays_count
				bottleneck = None
		else:
			avail_techs_count = None
			eff_cap = avail_bays_count
			bottleneck = None

		if eff_cap > 0:
			slots.append({
				"start_time": format_timedelta(curr_time),
				"available_bays": avail_bays_count,
				"available_technicians": avail_techs_count,
				"effective_capacity": eff_cap,
				"bottleneck": bottleneck,
			})

		curr_time += timedelta(minutes=interval)

	return slots


@frappe.whitelist()
def is_holiday(date: Union[str, datetime, date]) -> Dict[str, Any]:
	"""Check if a given date is a holiday according to ISM Settings -> holiday_list."""
	settings = get_shop_settings()
	holiday_list = settings.get("holiday_list")
	if not holiday_list or not date:
		return {"is_holiday": False, "description": None}

	target_date = str(date)
	if frappe.db.exists("DocType", "Holiday"):
		holiday = frappe.db.get_value(
			"Holiday",
			{"parent": holiday_list, "holiday_date": target_date},
			["description"],
			as_dict=True,
		)
		if holiday:
			return {
				"is_holiday": True,
				"description": holiday.get("description") or _("Holiday"),
			}

	return {"is_holiday": False, "description": None}


@frappe.whitelist()
def get_calendar_events(*args, **kwargs):
	"""Re-export get_calendar_events from scheduling_views without circular imports."""
	from ism.api.scheduling_views import get_calendar_events as _get_events
	return _get_events(*args, **kwargs)


@frappe.whitelist()
def find_next_available_slot(
	from_date: Optional[str] = None,
	duration_minutes: Union[int, float] = 60,
	required_tags: Optional[Union[str, List[str]]] = None,
	service_bay: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
	"""Search forward from from_date up to scheduling_horizon_days for the earliest date with available slots."""
	from frappe.utils import add_days, getdate
	settings = get_shop_settings()
	horizon = settings.get("scheduling_horizon_days") or 30
	max_days = min(int(horizon), 30)

	start_dt = getdate(from_date) if from_date else getdate()
	for i in range(1, max_days + 1):
		target_dt = add_days(start_dt, i)
		target_str = str(target_dt)
		hol = is_holiday(target_str)
		if hol.get("is_holiday"):
			continue
		slots = get_available_slots(
			date=target_str,
			duration_minutes=duration_minutes,
			required_tags=required_tags,
			service_bay=service_bay,
		)
		if slots:
			return {
				"date": target_str,
				"first_slot": slots[0],
				"slots_count": len(slots),
			}
	return None


@frappe.whitelist()
def get_unscheduled_services(repair_order: str) -> List[Dict[str, Any]]:
	"""Return services on Repair Order that are not yet allocated to an active Schedule Entry."""
	if not repair_order or not frappe.db.exists("Repair Order", repair_order):
		return []

	services = frappe.get_all(
		"Repair Order Service",
		filters={"parent": repair_order},
		fields=[
			"name",
			"service_package",
			"service_title",
			"labor_hours",
			"is_supplement",
			"status",
			"assigned_technician",
		],
		order_by="idx asc, creation asc",
	)

	has_se_field = frappe.db.has_column("Repair Order Service", "schedule_entry")
	allocated_svc_names = set()

	if has_se_field:
		for s in services:
			se = frappe.db.get_value("Repair Order Service", s.name, "schedule_entry")
			if se:
				se_status = frappe.db.get_value("Schedule Entry", se, "status")
				if se_status != "Cancelled":
					allocated_svc_names.add(s.name)

	unscheduled = []
	for s in services:
		if s.get("status") == "Declined":
			continue
		if s.name in allocated_svc_names:
			continue

		title = s.get("service_title") or s.get("service_package") or _("Service")
		hours = float(s.get("labor_hours") or 0)
		mins = int(round(hours * 60))
		unscheduled.append({
			"name": s.name,
			"service_title": title,
			"labor_hours": hours,
			"labor_minutes": mins,
			"is_supplement": int(s.get("is_supplement") or 0),
			"status": s.get("status"),
			"assigned_technician": s.get("assigned_technician"),
		})

	return unscheduled


@frappe.whitelist()
def allocate_schedule_entry(
	repair_order: Optional[str] = None,
	entry_type: str = "Diagnostic",
	scheduled_date: str = "",
	scheduled_time: str = "",
	estimated_duration: Union[int, float] = 60,
	service_bay: Optional[str] = None,
	assigned_technician: Optional[str] = None,
	notes: Optional[str] = None,
	selected_services: Optional[Union[str, List[str]]] = None,
	schedule_entry_name: Optional[str] = None,
) -> Dict[str, Any]:
	"""Create or update a Schedule Entry and link allocated Repair Order services."""
	if isinstance(selected_services, str):
		try:
			selected_services = frappe.parse_json(selected_services)
		except Exception:
			selected_services = [s.strip() for s in selected_services.split(",") if s.strip()]
	if not selected_services:
		selected_services = []

	items_summary_lines = []
	if selected_services and repair_order:
		for svc_name in selected_services:
			svc = frappe.db.get_value(
				"Repair Order Service",
				svc_name,
				["service_title", "service_package", "labor_hours", "is_supplement"],
				as_dict=True,
			)
			if svc:
				stitle = svc.get("service_title") or svc.get("service_package") or "Service"
				supp_badge = " [Supplement]" if svc.get("is_supplement") else ""
				shours = float(svc.get("labor_hours") or 0)
				items_summary_lines.append(f"- {stitle}{supp_badge} ({shours:.1f} hrs)")

	items_summary = "\n".join(items_summary_lines) if items_summary_lines else None

	is_update = bool(schedule_entry_name and frappe.db.exists("Schedule Entry", schedule_entry_name))
	if is_update:
		doc = frappe.get_doc("Schedule Entry", schedule_entry_name)
		if repair_order:
			doc.repair_order = repair_order
		doc.entry_type = entry_type
		doc.scheduled_date = scheduled_date
		doc.scheduled_time = scheduled_time
		doc.estimated_duration = int(estimated_duration)
		if service_bay:
			doc.service_bay = service_bay
		doc.assigned_technician = assigned_technician or None
		if notes:
			doc.notes = notes
		if items_summary:
			doc.items_summary = items_summary
		doc.save()
	else:
		doc = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": repair_order or None,
			"entry_type": entry_type,
			"scheduled_date": scheduled_date,
			"scheduled_time": scheduled_time,
			"estimated_duration": int(estimated_duration),
			"service_bay": service_bay,
			"assigned_technician": assigned_technician or None,
			"notes": notes or None,
			"items_summary": items_summary,
			"status": "Scheduled",
		})
		doc.insert()

	if selected_services and frappe.db.has_column("Repair Order Service", "schedule_entry"):
		for svc_name in selected_services:
			frappe.db.set_value("Repair Order Service", svc_name, "schedule_entry", doc.name)

	return {
		"name": doc.name,
		"status": doc.status,
		"scheduled_date": str(doc.scheduled_date),
		"scheduled_time": str(doc.scheduled_time),
		"service_bay": doc.service_bay,
		"assigned_technician": doc.assigned_technician,
	}


@frappe.whitelist()
def create_repair_order_from_schedule_entry(schedule_entry: str) -> Dict[str, Any]:
	"""Create a new Repair Order from a pre-intake Schedule Entry.
	
	Carries forward Schedule Entry notes into Repair Order customer_concern,
	and links the records together.
	"""
	if not schedule_entry or not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry))

	se = frappe.get_doc("Schedule Entry", schedule_entry)
	if se.repair_order:
		frappe.throw(
			_("Schedule Entry {0} is already linked to Repair Order {1}.").format(
				schedule_entry, se.repair_order
			)
		)

	company = frappe.defaults.get_user_default("Company")
	if not company:
		companies = frappe.get_all("Company", pluck="name")
		company = companies[0] if companies else None

	ro_data = {
		"doctype": "Repair Order",
		"status": "Checked In",
		"posting_date": frappe.utils.today(),
		"customer_concern": se.notes or "",
		"company": company,
	}
	if not se.customer:
		frappe.throw(_("Schedule Entry must have a Customer to create a Repair Order."))
	if not se.repair_vehicle:
		frappe.throw(_("Schedule Entry must have a Vehicle to create a Repair Order."))

	ro_data["customer"] = se.customer
	ro_data["vehicle"] = se.repair_vehicle

	if hasattr(frappe.get_meta("Repair Order"), "has_field") and frappe.get_meta("Repair Order").has_field("origin_schedule_entry"):
		ro_data["origin_schedule_entry"] = se.name

	ro = frappe.get_doc(ro_data)
	ro.flags.ignore_mandatory = True
	ro.insert()

	se.db_set("repair_order", ro.name)
	if hasattr(ro, "sync_schedule_entries"):
		ro.sync_schedule_entries()

	return {
		"repair_order": ro.name,
		"schedule_entry": se.name,
		"customer_concern": ro.customer_concern,
	}
