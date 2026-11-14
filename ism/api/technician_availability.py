# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from typing import List, Dict, Optional, Union, Any
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Union

import frappe

from ism.scheduling.capacity import INACTIVE_WORK_STATUSES, NON_BLOCKING_ENTRY_STATUSES
from ism.scheduling.time_utils import effective_end_time, format_timedelta, get_shop_settings, to_timedelta


@frappe.whitelist()
def get_active_technicians(date: Optional[Union[str, date, datetime]] = None) -> List[Dict[str, Any]]:
	"""
	Returns active employees matching ISM Settings -> technician_designation.
	If date is provided, includes leave status from approved Leave Applications.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	settings = get_shop_settings()
	designation = settings.get("technician_designation") or "Technician"

	employees = frappe.get_all(
		"Employee",
		filters={
			"designation": designation,
			"status": "Active",
		},
		fields=["name", "employee_name", "designation"],
		order_by="employee_name asc, name asc",
	)

	results = []
	target_date = str(date) if date else None

	# Fetch approved leave applications for target_date if date provided
	leave_map = {}
	if target_date and frappe.db.exists("DocType", "Leave Application"):
		leaves = frappe.get_all(
			"Leave Application",
			filters=[
				["status", "=", "Approved"],
				["docstatus", "=", 1],
				["from_date", "<=", target_date],
				["to_date", ">=", target_date],
			],
			fields=["employee", "half_day", "half_day_date"],
		)
		for l in leaves:
			leave_map[l.employee] = l

	for emp in employees:
		emp_id = emp["name"]
		leave_info = leave_map.get(emp_id)
		is_on_leave = False
		is_half_day = False

		if leave_info:
			is_half_day = bool(leave_info.get("half_day"))
			half_day_date = str(leave_info.get("half_day_date")) if leave_info.get("half_day_date") else None
			if is_half_day:
				if half_day_date == target_date:
					is_on_leave = True
			else:
				is_on_leave = True

		results.append({
			"employee": emp_id,
			"employee_name": emp.get("employee_name") or emp_id,
			"designation": emp.get("designation") or designation,
			"on_leave": is_on_leave,
			"half_day": is_half_day,
		})

	return results


def check_employee_leave(
	employee: str,
	date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
) -> Dict[str, Any]:
	"""
	Internal helper to check if an employee is on leave during a specific slot on date.
	"""
	if not frappe.db.exists("DocType", "Leave Application"):
		return {"on_leave": False, "half_day": False}

	target_date = str(date) if date else None
	if not target_date or not employee:
		return {"on_leave": False, "half_day": False}

	leaves = frappe.get_all(
		"Leave Application",
		filters=[
			["employee", "=", employee],
			["status", "=", "Approved"],
			["docstatus", "=", 1],
			["from_date", "<=", target_date],
			["to_date", ">=", target_date],
		],
		fields=["name", "half_day", "half_day_date"],
	)

	if not leaves:
		return {"on_leave": False, "half_day": False}

	for l in leaves:
		is_half_day = bool(l.get("half_day"))
		half_day_date = str(l.get("half_day_date")) if l.get("half_day_date") else None

		if not is_half_day:
			return {"on_leave": True, "half_day": False}

		if half_day_date == target_date:
			return {"on_leave": True, "half_day": True}

	return {"on_leave": False, "half_day": False}


@frappe.whitelist()
def get_technician_pool_availability(
	date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Pool-level capacity gating function.
	Checks total, on-duty, occupied, and available technicians for the proposed time window.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if exclude_entry is not None and not isinstance(exclude_entry, str):
		frappe.throw(frappe._("Invalid exclude_entry identifier."), frappe.ValidationError)
	settings = get_shop_settings()
	if not settings.get("enable_technician_capacity"):
		return {
			"total_technicians": 999,
			"on_leave": 0,
			"on_duty": 999,
			"occupied": 0,
			"available": 999,
			"is_available": True,
		}

	target_date = str(date)
	technicians = get_active_technicians(date=target_date)
	total_techs = len(technicians)
	if total_techs == 0:
		return {
			"total_technicians": 0,
			"on_leave": 0,
			"on_duty": 0,
			"occupied": 0,
			"available": 0,
			"is_available": False,
		}

	bs_td = settings.get("break_start")
	be_td = settings.get("break_end")
	prop_start = to_timedelta(start_time)
	prop_end = effective_end_time(prop_start, duration_minutes, bs_td, be_td)

	on_leave_count = 0
	on_duty_techs = []

	for tech in technicians:
		emp_id = tech["employee"]
		if tech.get("on_leave"):
			on_leave_count += 1
		else:
			on_duty_techs.append(emp_id)

	on_duty_count = len(on_duty_techs)
	occupied_count = 0

	occupied_techs = set()
	if on_duty_techs:
		filters = [
			["assigned_technician", "in", on_duty_techs],
			["scheduled_date", "=", target_date],
			["status", "not in", list(NON_BLOCKING_ENTRY_STATUSES)],
		]
		if exclude_entry:
			filters.append(["name", "!=", exclude_entry])

		entries = frappe.get_all(
			"Schedule Entry",
			filters=filters,
			fields=["assigned_technician", "scheduled_time", "estimated_duration"],
		)

		for entry in entries:
			ex_start = to_timedelta(entry.get("scheduled_time"))
			ex_duration = entry.get("estimated_duration") or 0
			ex_end = effective_end_time(ex_start, ex_duration, bs_td, be_td)

			if max(prop_start, ex_start) < min(prop_end, ex_end):
				occupied_techs.add(entry.get("assigned_technician"))

		occupied_count = len(occupied_techs)

	available_tech_ids = [t for t in on_duty_techs if t not in occupied_techs]
	available_count = max(0, on_duty_count - occupied_count)

	return {
		"total_technicians": total_techs,
		"on_leave": on_leave_count,
		"on_duty": on_duty_count,
		"occupied": occupied_count,
		"available": available_count,
		"available_technicians": available_tech_ids,
		"is_available": available_count > 0,
	}


@frappe.whitelist()
def check_technician_availability(
	employee: str,
	date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Checks availability of a specific technician for a given date/time window.
	Returns details on leave status, overlaps, and availability.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if not employee or not isinstance(employee, str) or not date:
		return {
			"is_available": False,
			"reason": "invalid_parameters",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}
	if exclude_entry is not None and not isinstance(exclude_entry, str):
		frappe.throw(frappe._("Invalid exclude_entry identifier."), frappe.ValidationError)

	settings = get_shop_settings()
	designation = settings.get("technician_designation") or "Technician"

	emp_doc = frappe.db.get_value(
		"Employee",
		employee,
		["status", "designation"],
		as_dict=True,
	)

	if not emp_doc or emp_doc.status != "Active":
		return {
			"is_available": False,
			"reason": "inactive",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}

	if emp_doc.designation != designation:
		return {
			"is_available": False,
			"reason": "designation_mismatch",
			"on_leave": False,
			"half_day": False,
			"conflicts": [],
		}

	target_date = str(date)

	# Check leave status
	leave_res = check_employee_leave(employee, target_date, start_time, duration_minutes)
	if leave_res["on_leave"]:
		return {
			"is_available": False,
			"reason": "on_leave",
			"on_leave": True,
			"half_day": leave_res["half_day"],
			"conflicts": [],
		}

	# Check overlapping schedule entries
	bs_td = settings.get("break_start")
	be_td = settings.get("break_end")
	prop_start = to_timedelta(start_time)
	prop_end = effective_end_time(prop_start, duration_minutes, bs_td, be_td)

	filters = [
		["assigned_technician", "=", employee],
		["scheduled_date", "=", target_date],
		["status", "not in", list(NON_BLOCKING_ENTRY_STATUSES)],
	]
	if exclude_entry:
		filters.append(["name", "!=", exclude_entry])

	existing_entries = frappe.get_all(
		"Schedule Entry",
		filters=filters,
		fields=["name", "scheduled_time", "estimated_duration", "customer", "service_bay", "repair_order"],
	)

	conflicts = []
	for entry in existing_entries:
		ex_start = to_timedelta(entry.get("scheduled_time"))
		ex_duration = entry.get("estimated_duration") or 0
		ex_end = effective_end_time(ex_start, ex_duration, bs_td, be_td)

		if max(prop_start, ex_start) < min(prop_end, ex_end):
			conflicts.append({
				"name": entry.get("name"),
				"scheduled_time": format_timedelta(ex_start),
				"estimated_duration": ex_duration,
				"effective_end_time": format_timedelta(ex_end),
				"customer": entry.get("customer"),
				"service_bay": entry.get("service_bay"),
				"repair_order": entry.get("repair_order"),
			})

	if conflicts:
		return {
			"is_available": False,
			"reason": "overlap",
			"on_leave": False,
			"half_day": False,
			"conflicts": conflicts,
		}

	return {
		"is_available": True,
		"reason": None,
		"on_leave": False,
		"half_day": False,
		"conflicts": [],
	}


def _build_queue_from_entries(
	employee: str,
	emp_name: str,
	on_leave: bool,
	entries: List[Dict[str, Any]],
	op_start: timedelta,
	op_end: timedelta,
	bs_td: Optional[timedelta],
	be_td: Optional[timedelta],
) -> Dict[str, Any]:
	"""Aggregate timeline metrics and utilization gaps from sorted Schedule Entry rows."""
	formatted_entries = []
	total_minutes = 0
	completed_minutes = 0
	remaining_minutes = 0
	active_job_count = 0
	gaps = []
	last_end_td = op_start

	for entry in entries:
		st_td = to_timedelta(entry.get("scheduled_time"))
		dur = entry.get("estimated_duration") or 0
		eff_end_td = effective_end_time(st_td, dur, bs_td, be_td)

		total_minutes += dur
		if entry.get("status") == "Completed":
			completed_minutes += dur
		else:
			remaining_minutes += dur
			active_job_count += 1

		if st_td > last_end_td:
			gap_dur = int((st_td - last_end_td).total_seconds() // 60)
			if bs_td and be_td and last_end_td <= bs_td and st_td >= be_td:
				gap_dur -= int((be_td - bs_td).total_seconds() // 60)
			if gap_dur > 0:
				gaps.append({
					"after": formatted_entries[-1]["name"] if formatted_entries else "start_of_day",
					"duration_minutes": gap_dur,
					"start": format_timedelta(last_end_td),
				})

		formatted_entries.append({
			"name": entry["name"],
			"scheduled_time": format_timedelta(st_td),
			"estimated_duration": dur,
			"effective_end_time": format_timedelta(eff_end_td),
			"service_bay": entry.get("service_bay"),
			"customer": entry.get("customer"),
			"repair_vehicle": entry.get("repair_vehicle"),
			"items_summary": entry.get("items_summary"),
			"status": entry.get("status"),
			"repair_order": entry.get("repair_order"),
		})

		last_end_td = max(last_end_td, eff_end_td)

	shop_op_minutes = int((op_end - op_start).total_seconds() // 60)
	if bs_td and be_td and be_td > bs_td:
		shop_op_minutes -= int((be_td - bs_td).total_seconds() // 60)

	shop_op_minutes = max(1, shop_op_minutes)
	utilization_pct = round((total_minutes / shop_op_minutes) * 100, 1)

	return {
		"employee": employee,
		"employee_name": emp_name,
		"on_leave": on_leave,
		"timeline": formatted_entries,
		"entries": formatted_entries,
		"total_jobs": len(formatted_entries),
		"active_job_count": active_job_count,
		"total_minutes": total_minutes,
		"completed_minutes": completed_minutes,
		"remaining_minutes": remaining_minutes,
		"utilization_percent": utilization_pct,
		"utilization_pct": utilization_pct,
		"gaps": gaps,
	}


@frappe.whitelist()
def get_technician_queue(employee: str, date: Union[str, date, datetime]) -> Dict[str, Any]:
	"""
	Returns personal work queue for a technician on a specific date.
	Calculates gaps between entries and utilization percentage.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if not employee or not isinstance(employee, str):
		frappe.throw(frappe._("Valid technician identifier is required."), frappe.ValidationError)
	settings = get_shop_settings()
	op_start = settings["operating_hours_start"]
	op_end = settings["operating_hours_end"]
	bs_td = settings.get("break_start")
	be_td = settings.get("break_end")

	target_date = str(date)
	emp_name = frappe.db.get_value("Employee", employee, "employee_name") or employee
	leave_res = check_employee_leave(employee, target_date, op_start, 0)

	entries = frappe.get_list(
		"Schedule Entry",
		filters=[
			["assigned_technician", "=", employee],
			["scheduled_date", "=", target_date],
			["status", "not in", list(INACTIVE_WORK_STATUSES)],
		],
		fields=[
			"name",
			"scheduled_time",
			"estimated_duration",
			"service_bay",
			"customer",
			"repair_vehicle",
			"items_summary",
			"status",
			"repair_order",
		],
		order_by="scheduled_time asc",
	)

	return _build_queue_from_entries(
		employee=employee,
		emp_name=emp_name,
		on_leave=leave_res["on_leave"],
		entries=entries,
		op_start=op_start,
		op_end=op_end,
		bs_td=bs_td,
		be_td=be_td,
	)


@frappe.whitelist()
def get_daily_technician_overview(date: Union[str, date, datetime]) -> Dict[str, Any]:
	"""
	Shop-wide technician dashboard data for managers and overview reporting.
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)
	settings = get_shop_settings()
	op_start = settings["operating_hours_start"]
	op_end = settings["operating_hours_end"]
	bs_td = settings.get("break_start")
	be_td = settings.get("break_end")

	target_date = str(date)

	operating_minutes = int((op_end - op_start).total_seconds() // 60)
	if bs_td and be_td and be_td > bs_td:
		operating_minutes -= int((be_td - bs_td).total_seconds() // 60)
	operating_minutes = max(1, operating_minutes)

	active_techs = get_active_technicians(date=target_date)
	day_entries = frappe.get_list(
		"Schedule Entry",
		filters=[
			["scheduled_date", "=", target_date],
			["status", "not in", list(INACTIVE_WORK_STATUSES)],
		],
		fields=[
			"name",
			"assigned_technician",
			"scheduled_time",
			"estimated_duration",
			"service_bay",
			"customer",
			"repair_vehicle",
			"items_summary",
			"status",
			"repair_order",
		],
		order_by="scheduled_time asc",
	)

	entries_by_tech: Dict[str, List[Dict[str, Any]]] = {}
	unassigned = []
	for entry in day_entries:
		tech_id = entry.get("assigned_technician")
		if tech_id:
			entries_by_tech.setdefault(tech_id, []).append(entry)
		else:
			unassigned.append(entry)

	tech_summaries = []
	total_scheduled_minutes = 0
	total_remaining_minutes = 0
	total_completed_minutes = 0

	for tech in active_techs:
		emp_id = tech["employee"]
		queue = _build_queue_from_entries(
			employee=emp_id,
			emp_name=tech["employee_name"],
			on_leave=tech["on_leave"],
			entries=entries_by_tech.get(emp_id, ()),
			op_start=op_start,
			op_end=op_end,
			bs_td=bs_td,
			be_td=be_td,
		)

		tech_summaries.append({
			"employee": emp_id,
			"employee_name": tech["employee_name"],
			"on_leave": tech["on_leave"],
			"job_count": queue["total_jobs"],
			"active_job_count": queue["active_job_count"],
			"total_minutes": queue["total_minutes"],
			"completed_minutes": queue["completed_minutes"],
			"remaining_minutes": queue["remaining_minutes"],
			"utilization_percent": queue["utilization_percent"],
			"utilization_pct": queue["utilization_pct"],
		})
		total_scheduled_minutes += queue["total_minutes"]
		total_remaining_minutes += queue["remaining_minutes"]
		total_completed_minutes += queue["completed_minutes"]

	unassigned_formatted = []
	for u in unassigned:
		st_td = to_timedelta(u.get("scheduled_time"))
		unassigned_formatted.append({
			"name": u["name"],
			"scheduled_time": format_timedelta(st_td),
			"customer": u.get("customer"),
			"repair_vehicle": u.get("repair_vehicle"),
			"service_bay": u.get("service_bay"),
			"estimated_duration": u.get("estimated_duration") or 0,
			"repair_order": u.get("repair_order"),
		})

	total_bays = frappe.db.count("Service Bay", {"is_active": 1})
	on_duty_count = sum(1 for t in tech_summaries if not t["on_leave"])
	on_leave_count = sum(1 for t in tech_summaries if t["on_leave"])

	bottleneck = "technician" if on_duty_count < total_bays else ("bay" if total_bays < on_duty_count else None)
	total_hours = round(total_scheduled_minutes / 60.0, 1)
	active_hours = round(total_remaining_minutes / 60.0, 1)
	completed_hours = round(total_completed_minutes / 60.0, 1)

	shop_summary = {
		"total_technicians": len(active_techs),
		"on_duty": on_duty_count,
		"on_leave": on_leave_count,
		"total_bays": total_bays,
		"bottleneck": bottleneck,
		"total_scheduled_hours": total_hours,
		"active_scheduled_hours": active_hours,
		"completed_hours": completed_hours,
		"unassigned_count": len(unassigned_formatted),
	}

	return {
		"date": target_date,
		"operating_minutes": operating_minutes,
		"total_scheduled_hours": total_hours,
		"bottleneck": bottleneck,
		"technicians": tech_summaries,
		"unassigned_entries": unassigned_formatted,
		"shop_summary": shop_summary,
	}
