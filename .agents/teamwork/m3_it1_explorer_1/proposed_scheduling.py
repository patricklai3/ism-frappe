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

	if break_start is None or break_end is None:
		settings = get_shop_settings()
		bs_td = settings["break_start"] if break_start is None else to_timedelta(break_start)
		be_td = settings["break_end"] if break_end is None else to_timedelta(break_end)
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
		bay_name = bay.get("bay_name") or bay.get("name")
		tags = frappe.get_all(
			"Service Bay Equipment",
			filters={"parent": bay.get("name")},
			pluck="equipment_tag",
		)
		bay_tags_set = set(tags)

		if not required_tags_set.issubset(bay_tags_set):
			continue

		if check_bay_availability(bay_name, date, start_time, duration_minutes):
			available_bays.append({
				"bay_name": bay_name,
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
