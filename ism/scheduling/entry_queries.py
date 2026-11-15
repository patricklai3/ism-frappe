# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import frappe
from frappe.utils import add_days, getdate

from ism.scheduling.capacity import INACTIVE_WORK_STATUSES, NON_BLOCKING_ENTRY_STATUSES
from ism.scheduling.time_utils import (
	calculate_working_span,
	format_timedelta,
	normalize_date,
	to_timedelta,
)

DEFAULT_BLOCKING_FIELDS: Tuple[str, ...] = (
	"name",
	"repair_order",
	"service_bay",
	"assigned_technician",
	"scheduled_date",
	"scheduled_time",
	"estimated_duration",
	"completion_date",
	"completion_time",
	"status",
)


def span_datetimes(span: Dict[str, Any]) -> Tuple[datetime, datetime]:
	"""Convert a calculate_working_span result into continuous (start_dt, end_dt) bounds."""
	start_d = getdate(span["start_date"])
	start_t = to_timedelta(span["start_time"])
	end_d = getdate(span["completion_date"])
	end_t = to_timedelta(span["completion_time"])
	return (
		datetime.combine(start_d, (datetime.min + start_t).time()),
		datetime.combine(end_d, (datetime.min + end_t).time()),
	)


def entry_datetimes(
	entry: Dict[str, Any],
	is_holiday_fn: Optional[Callable[[date], bool]] = None,
) -> Tuple[datetime, datetime]:
	"""Return continuous (start_dt, end_dt) bounds for a Schedule Entry record."""
	start_d = getdate(entry.get("scheduled_date"))
	start_t = to_timedelta(entry.get("scheduled_time"))
	comp_date_raw = entry.get("completion_date")
	comp_time_raw = entry.get("completion_time")
	has_comp_time = comp_time_raw is not None and str(comp_time_raw).strip() != ""

	if comp_date_raw and has_comp_time:
		end_d = getdate(comp_date_raw)
		end_t = to_timedelta(comp_time_raw)
	else:
		span = calculate_working_span(
			entry.get("scheduled_date"),
			entry.get("scheduled_time"),
			entry.get("estimated_duration") or 60,
			is_holiday_fn=is_holiday_fn,
		)
		end_d = getdate(span["completion_date"])
		end_t = to_timedelta(span["completion_time"])

	return (
		datetime.combine(start_d, (datetime.min + start_t).time()),
		datetime.combine(end_d, (datetime.min + end_t).time()),
	)


def fetch_leaves_by_employee(
	employees: Sequence[str],
	min_date: Union[str, date, datetime],
	max_date: Union[str, date, datetime],
) -> Dict[str, List[Dict[str, Any]]]:
	"""Batch fetch approved Leave Applications overlapping [min_date, max_date] for given employees."""
	min_str = normalize_date(min_date) or str(min_date or "")
	max_str = normalize_date(max_date) or str(max_date or "")
	if not employees or not min_str or not max_str or not frappe.db.exists("DocType", "Leave Application"):
		return {}

	leaves = frappe.get_all(
		"Leave Application",
		filters=[
			["employee", "in", list(employees)],
			["status", "=", "Approved"],
			["docstatus", "=", 1],
			["from_date", "<=", max_str],
			["to_date", ">=", min_str],
		],
		fields=["employee", "from_date", "to_date", "half_day", "half_day_date"],
	)
	leaves_by_emp: Dict[str, List[Dict[str, Any]]] = {}
	for leave in leaves:
		leaves_by_emp.setdefault(leave["employee"], []).append(leave)
	return leaves_by_emp


def is_employee_on_leave_for_date(
	emp_leaves: Sequence[Dict[str, Any]],
	target_date: Union[str, date, datetime],
) -> Dict[str, Any]:
	"""Check whether any approved leave in emp_leaves covers target_date (preferring full-day)."""
	target_str = normalize_date(target_date) or str(target_date or "")
	if not target_str:
		return {"on_leave": False, "half_day": False}

	half_day_hit = False
	for leave in emp_leaves:
		from_str = normalize_date(leave.get("from_date")) or str(leave.get("from_date") or "")
		to_str = normalize_date(leave.get("to_date")) or str(leave.get("to_date") or "")
		if from_str and to_str and not (from_str <= target_str <= to_str):
			continue
		if not bool(leave.get("half_day")):
			return {"on_leave": True, "half_day": False}
		half_day_raw = leave.get("half_day_date")
		half_day_str = (normalize_date(half_day_raw) or str(half_day_raw)) if half_day_raw else None
		if half_day_str == target_str:
			half_day_hit = True

	if half_day_hit:
		return {"on_leave": True, "half_day": True}
	return {"on_leave": False, "half_day": False}


def intervals_overlap(start_a: Any, end_a: Any, start_b: Any, end_b: Any) -> bool:
	"""Return True when two half-open intervals [start_a, end_a) and [start_b, end_b) intersect."""
	return max(start_a, start_b) < min(end_a, end_b)


def build_holiday_checker(
	holiday_list: Optional[str],
	min_date: Optional[Union[str, date, datetime]] = None,
) -> Callable[[Union[str, date, datetime]], bool]:
	"""Return a holiday-check predicate backed by a single batched Holiday table load."""
	if not holiday_list or not frappe.db.exists("DocType", "Holiday"):
		return lambda _d: False

	filters: List[Any] = [["parent", "=", holiday_list]]
	if min_date:
		min_str = normalize_date(min_date) or str(min_date)
		filters.append(["holiday_date", ">=", min_str])

	rows = frappe.get_all("Holiday", filters=filters, pluck="holiday_date")
	holiday_dates: Set[str] = {str(d) for d in rows if d}

	def _is_holiday(target_date: Union[str, date, datetime]) -> bool:
		d_str = normalize_date(target_date) or str(getdate(target_date))
		return d_str in holiday_dates

	return _is_holiday


def fetch_blocking_entries(
	start_date: Union[str, date, datetime],
	end_date: Union[str, date, datetime],
	*,
	service_bay: Optional[str] = None,
	service_bays: Optional[Iterable[str]] = None,
	assigned_technician: Optional[str] = None,
	assigned_technicians: Optional[Iterable[str]] = None,
	exclude_entry: Optional[str] = None,
	fields: Optional[Sequence[str]] = None,
	lookback_days: int = 30,
	statuses: Sequence[str] = NON_BLOCKING_ENTRY_STATUSES,
) -> List[Dict[str, Any]]:
	"""Fetch non-blocking Schedule Entry rows potentially overlapping [start_date, end_date]."""
	start_dt = getdate(normalize_date(start_date) or start_date)
	end_dt = getdate(normalize_date(end_date) or end_date)
	start_str = str(start_dt)
	end_str = str(end_dt)
	lookback_str = str(add_days(start_dt, -abs(int(lookback_days))))

	bay_list = list(service_bays) if service_bays is not None else None
	if bay_list is not None and not bay_list:
		return []
	tech_list = list(assigned_technicians) if assigned_technicians is not None else None
	if tech_list is not None and not tech_list:
		return []

	select_fields = list(fields or DEFAULT_BLOCKING_FIELDS)
	se = frappe.qb.DocType("Schedule Entry")
	query = (
		frappe.qb.from_(se)
		.select(*(getattr(se, fname) for fname in select_fields))
		.where(se.scheduled_date <= end_str)
		.where(
			(se.completion_date >= start_str)
			| (se.completion_date.isnull() & (se.scheduled_date >= lookback_str))
		)
		.where(se.status.notin(list(statuses)))
	)

	if service_bay:
		query = query.where(se.service_bay == service_bay)
	elif bay_list is not None:
		query = query.where(se.service_bay.isin(bay_list))

	if assigned_technician:
		query = query.where(se.assigned_technician == assigned_technician)
	elif tech_list is not None:
		query = query.where(se.assigned_technician.isin(tech_list))

	if exclude_entry:
		query = query.where(se.name != exclude_entry)

	return query.run(as_dict=True)


def find_conflicting_bay_entries(
	service_bay: str,
	scheduled_date: Union[str, date, datetime],
	scheduled_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
	fields: Optional[Sequence[str]] = None,
	is_holiday_fn: Optional[Callable[[date], bool]] = None,
) -> List[Dict[str, Any]]:
	"""Return blocking Schedule Entry rows whose continuous span overlaps a proposed bay window."""
	span = calculate_working_span(
		scheduled_date,
		scheduled_time,
		duration_minutes,
		is_holiday_fn=is_holiday_fn,
	)
	prop_start_dt, prop_end_dt = span_datetimes(span)
	entries = fetch_blocking_entries(
		span["start_date"],
		span["completion_date"],
		service_bay=service_bay,
		exclude_entry=exclude_entry,
		fields=fields,
	)
	conflicts: List[Dict[str, Any]] = []
	for entry in entries:
		ex_start_dt, ex_end_dt = entry_datetimes(entry, is_holiday_fn=is_holiday_fn)
		if intervals_overlap(prop_start_dt, prop_end_dt, ex_start_dt, ex_end_dt):
			conflicts.append(entry)
	return conflicts


def fetch_queue_entries_for_date(
	target_date: Union[str, date, datetime],
	*,
	employee: Optional[str] = None,
	fields: Optional[Sequence[str]] = None,
	lookback_days: int = 30,
	is_holiday_fn: Optional[Callable[[date], bool]] = None,
) -> List[Dict[str, Any]]:
	"""Fetch active Schedule Entry rows covering target_date, projecting overnight spills onto the day."""
	target_str = normalize_date(target_date) or str(getdate(target_date))
	target_dt = getdate(target_str)
	lookback_str = str(add_days(target_dt, -abs(int(lookback_days))))

	required_fields = [
		"name",
		"assigned_technician",
		"scheduled_date",
		"scheduled_time",
		"completion_date",
		"completion_time",
		"estimated_duration",
		"service_bay",
		"customer",
		"repair_vehicle",
		"items_summary",
		"status",
		"repair_order",
	]
	select_fields = list(dict.fromkeys([*(fields or ()), *required_fields]))

	filters: List[Any] = [
		["scheduled_date", "<=", target_str],
		["scheduled_date", ">=", lookback_str],
		["status", "not in", list(INACTIVE_WORK_STATUSES)],
	]
	if employee:
		filters.append(["assigned_technician", "=", employee])

	or_filters: List[Any] = [
		["completion_date", ">=", target_str],
		["completion_date", "is", "not set"],
	]

	rows = frappe.get_list(
		"Schedule Entry",
		filters=filters,
		or_filters=or_filters,
		fields=select_fields,
		order_by="scheduled_date asc, scheduled_time asc",
	)

	projected: List[Dict[str, Any]] = []
	for row in rows:
		sched_str = normalize_date(row.get("scheduled_date")) or str(row.get("scheduled_date") or "")
		dur = row.get("estimated_duration") or 0
		if dur <= 0:
			if sched_str != target_str:
				continue
			item = dict(row)
			start_fmt = format_timedelta(to_timedelta(row.get("scheduled_time")))
			item["scheduled_time"] = start_fmt
			item["estimated_duration"] = 0
			item["_day_end_time"] = start_fmt
			projected.append(item)
			continue

		span = calculate_working_span(
			row.get("scheduled_date"),
			row.get("scheduled_time"),
			dur,
			is_holiday_fn=is_holiday_fn,
		)
		day_segs = [seg for seg in span["segments"] if seg["date"] == target_str]
		if not day_segs:
			continue

		total_seg_dur = sum(float(seg["duration"]) for seg in day_segs)
		rounded_dur = round(total_seg_dur)
		day_dur: Union[int, float] = (
			int(rounded_dur) if abs(total_seg_dur - rounded_dur) < 1e-6 else round(total_seg_dur, 2)
		)
		item = dict(row)
		item["scheduled_time"] = day_segs[0]["start_time"]
		item["estimated_duration"] = day_dur
		item["_day_end_time"] = day_segs[-1]["end_time"]
		projected.append(item)

	projected.sort(key=lambda r: (to_timedelta(r.get("scheduled_time")), r.get("name") or ""))
	return projected
