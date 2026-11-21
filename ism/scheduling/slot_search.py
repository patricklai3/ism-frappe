# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, timedelta
import math
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate

from ism.scheduling.capacity import parse_required_tags, resolve_candidate_bays
from ism.scheduling.entry_queries import (
	entry_datetimes,
	fetch_blocking_entries,
	fetch_leaves_by_employee,
	intervals_overlap,
	is_employee_on_leave_for_date,
	span_datetimes,
)
from ism.scheduling.time_utils import (
	calculate_working_span,
	format_timedelta,
	get_shop_settings,
	normalize_date,
	to_timedelta,
)


class _SlotSearchContext:
	"""Single-batch scheduling snapshot for get_available_slots and find_next_available_slot."""

	def __init__(
		self,
		start_date: Union[str, date, datetime],
		end_date: Union[str, date, datetime],
		duration_minutes: Union[int, float],
		*,
		required_tags: Optional[Union[str, List[str]]] = None,
		service_bay: Optional[str] = None,
		target_tech: Optional[str] = None,
		exclude_entry: Optional[str] = None,
	) -> None:
		self.settings = get_shop_settings()
		self.op_start: timedelta = self.settings["operating_hours_start"]
		self.op_end: timedelta = self.settings["operating_hours_end"]
		self.bs_td: Optional[timedelta] = self.settings["break_start"]
		self.be_td: Optional[timedelta] = self.settings["break_end"]
		self.interval: int = int(self.settings["default_slot_interval"] or 30)
		self.enable_tech: Any = self.settings.get("enable_technician_capacity")
		self.holiday_list: Optional[str] = self.settings.get("holiday_list")

		duration_val = float(duration_minutes or 0)
		if duration_val <= 0:
			duration_val = float(self.settings.get("default_diagnostic_duration") or 60)
		self.duration_val: float = duration_val

		self.start_dt = getdate(normalize_date(start_date) or start_date)
		self.end_dt = getdate(normalize_date(end_date) or end_date)
		self.service_bay = service_bay
		self.target_tech = target_tech
		self.exclude_entry = exclude_entry

		self.valid_hours: bool = (
			self.op_start < self.op_end and self.interval > 0 and self.end_dt >= self.start_dt
		)
		self._has_holiday_doctype: bool = False
		self._holiday_min: str = str(self.start_dt)
		self._holiday_max: str = str(self.end_dt)
		self.holiday_dates: Set[str] = set()
		self._extra_holidays: Dict[str, bool] = {}

		self.candidate_bays: List[str] = []
		self._candidate_bay_set: Set[str] = set()
		self.target_tech_valid: bool = True
		self.tech_ids: List[str] = []
		self._tech_id_set: Set[str] = set()
		self.leaves_by_emp: Dict[str, List[Dict[str, Any]]] = {}
		self.bay_intervals: Dict[str, List[Tuple[datetime, datetime, str]]] = {}
		self.tech_segments: Dict[str, Dict[str, List[Tuple[timedelta, timedelta, str]]]] = {}

		if not self.valid_hours:
			return

		horizon_end_dt = add_days(self.end_dt, self._compute_horizon_buffer_days())
		self._holiday_max = str(horizon_end_dt)
		self._load_holidays(self._holiday_min, self._holiday_max)

		required_tags_set = set(parse_required_tags(required_tags))
		self.candidate_bays, _tags = resolve_candidate_bays(service_bay, required_tags_set)
		self._candidate_bay_set = set(self.candidate_bays)

		self._load_technicians(self.start_dt, horizon_end_dt)
		self._load_entries(self.start_dt, horizon_end_dt)

	def _compute_horizon_buffer_days(self) -> int:
		op_mins = int((self.op_end - self.op_start).total_seconds() // 60)
		if self.bs_td and self.be_td and self.be_td > self.bs_td:
			op_mins -= int((self.be_td - self.bs_td).total_seconds() // 60)
		daily_mins = max(60, op_mins)
		span_days_est = max(1, math.ceil(self.duration_val / daily_mins))
		return max(14, span_days_est * 3 + 7)

	def _load_holidays(self, start_str: str, end_str: str) -> None:
		if not self.holiday_list:
			return
		self._has_holiday_doctype = bool(frappe.db.exists("DocType", "Holiday"))
		if not self._has_holiday_doctype:
			return
		rows = frappe.get_all(
			"Holiday",
			filters=[
				["parent", "=", self.holiday_list],
				["holiday_date", ">=", start_str],
				["holiday_date", "<=", end_str],
			],
			pluck="holiday_date",
		)
		self.holiday_dates = {str(d) for d in rows if d}

	def is_holiday(self, target_date: Union[str, date, datetime]) -> bool:
		"""Check whether target_date is a configured shop holiday using preloaded snapshot."""
		if not self.holiday_list or not self._has_holiday_doctype:
			return False
		d_str = str(getdate(normalize_date(target_date) or target_date))
		if self._holiday_min <= d_str <= self._holiday_max:
			return d_str in self.holiday_dates
		if d_str not in self._extra_holidays:
			self._extra_holidays[d_str] = bool(
				frappe.db.exists("Holiday", {"parent": self.holiday_list, "holiday_date": d_str})
			)
		return self._extra_holidays[d_str]

	def _load_technicians(self, start_dt: date, horizon_end_dt: date) -> None:
		designation = self.settings.get("technician_designation") or "Technician"
		if self.target_tech:
			emp_doc = frappe.db.get_value(
				"Employee",
				self.target_tech,
				["status", "designation"],
				as_dict=True,
			)
			self.target_tech_valid = bool(
				emp_doc and emp_doc.status == "Active" and emp_doc.designation == designation
			)
			self.tech_ids = [self.target_tech] if self.target_tech_valid else []
		elif self.enable_tech:
			self.tech_ids = frappe.get_all(
				"Employee",
				filters={"designation": designation, "status": "Active"},
				pluck="name",
				order_by="employee_name asc, name asc",
			)
		self._tech_id_set = set(self.tech_ids)
		if self.tech_ids:
			self.leaves_by_emp = fetch_leaves_by_employee(self.tech_ids, start_dt, horizon_end_dt)

	def _load_entries(self, start_dt: date, horizon_end_dt: date) -> None:
		if not self._candidate_bay_set and not self._tech_id_set:
			return
		entries = fetch_blocking_entries(start_dt, horizon_end_dt, exclude_entry=self.exclude_entry)
		for entry in entries:
			bay = entry.get("service_bay")
			if bay and bay in self._candidate_bay_set:
				ex_start_dt, ex_end_dt = entry_datetimes(entry, is_holiday_fn=self.is_holiday)
				self.bay_intervals.setdefault(bay, []).append((ex_start_dt, ex_end_dt, entry["name"]))

			tech = entry.get("assigned_technician")
			if tech and tech in self._tech_id_set:
				ex_dur = entry.get("estimated_duration") or 0
				ex_span = calculate_working_span(
					entry.get("scheduled_date"),
					entry.get("scheduled_time"),
					ex_dur,
					is_holiday_fn=self.is_holiday,
				)
				by_date = self.tech_segments.setdefault(tech, {})
				for seg in ex_span["segments"]:
					by_date.setdefault(seg["date"], []).append((
						to_timedelta(seg["start_time"]),
						to_timedelta(seg["end_time"]),
						entry["name"],
					))

	def _eval_bay_capacity(
		self,
		prop_start_dt: datetime,
		prop_end_dt: datetime,
	) -> Tuple[int, bool, Optional[str], Optional[str]]:
		if self.service_bay:
			is_occupied = any(
				intervals_overlap(prop_start_dt, prop_end_dt, ex_s, ex_e)
				for ex_s, ex_e, _ in self.bay_intervals.get(self.service_bay, ())
			)
			if not is_occupied:
				return 1, True, None, self.service_bay
			return 0, False, _("Bay Busy"), self.service_bay

		avail_bays = [
			bay
			for bay in self.candidate_bays
			if not any(
				intervals_overlap(prop_start_dt, prop_end_dt, ex_s, ex_e)
				for ex_s, ex_e, _ in self.bay_intervals.get(bay, ())
			)
		]
		if not avail_bays:
			return 0, False, _("Bays Full"), None
		return len(avail_bays), True, None, avail_bays[0]

	def _eval_tech_capacity(
		self,
		span: Dict[str, Any],
		avail_bays_count: int,
	) -> Tuple[Optional[int], int, Optional[str], bool, Optional[str], Optional[str]]:
		tech_available = True
		tech_reason: Optional[str] = None
		conflict_entry: Optional[str] = None
		avail_techs_count: Optional[int] = None

		if self.target_tech:
			if not self.target_tech_valid:
				avail_techs_count = 0
				tech_available = False
				tech_reason = _("Tech Unavailable")
			else:
				emp_leaves = self.leaves_by_emp.get(self.target_tech, ())
				on_leave = any(
					is_employee_on_leave_for_date(emp_leaves, str(seg["date"]))["on_leave"]
					for seg in span["segments"]
				)
				if on_leave:
					avail_techs_count = 0
					tech_available = False
					tech_reason = _("On Leave")
				else:
					tech_by_date = self.tech_segments.get(self.target_tech, {})
					for seg in span["segments"]:
						seg_start = to_timedelta(seg["start_time"])
						seg_end = to_timedelta(seg["end_time"])
						for ex_s, ex_e, entry_name in tech_by_date.get(seg["date"], ()):
							if intervals_overlap(seg_start, seg_end, ex_s, ex_e):
								conflict_entry = entry_name
								avail_techs_count = 0
								tech_available = False
								tech_reason = _("Tech Busy")
								break
						if not tech_available:
							break
					if tech_available:
						avail_techs_count = 1
		elif self.enable_tech:
			total_techs = len(self.tech_ids)
			if total_techs == 0:
				avail_techs_count = 0
				tech_available = False
				tech_reason = _("Techs Full")
			else:
				min_available = total_techs
				for seg in span["segments"]:
					seg_date = seg["date"]
					seg_start = to_timedelta(seg["start_time"])
					seg_end = to_timedelta(seg["end_time"])
					avail_in_seg = 0
					for emp_id in self.tech_ids:
						emp_leaves = self.leaves_by_emp.get(emp_id, ())
						if is_employee_on_leave_for_date(emp_leaves, str(seg_date))["on_leave"]:
							continue
						emp_segs = self.tech_segments.get(emp_id, {}).get(seg_date, ())
						has_overlap = any(
							intervals_overlap(seg_start, seg_end, ex_s, ex_e)
							for ex_s, ex_e, _ in emp_segs
						)
						if has_overlap:
							continue
						avail_in_seg += 1
					if avail_in_seg < min_available:
						min_available = avail_in_seg
					if min_available == 0:
						break
				avail_techs_count = min_available
				if avail_techs_count == 0:
					tech_available = False
					tech_reason = _("Techs Full")

		if avail_techs_count is not None:
			eff_cap = min(avail_bays_count, avail_techs_count)
			if avail_techs_count < avail_bays_count:
				bottleneck: Optional[str] = "technician"
			elif avail_bays_count < avail_techs_count:
				bottleneck = "bay"
			else:
				bottleneck = "none"
		else:
			eff_cap = avail_bays_count
			bottleneck = None

		return avail_techs_count, eff_cap, bottleneck, tech_available, tech_reason, conflict_entry

	def slots_for_date(
		self,
		target_date: Union[str, date, datetime],
		*,
		include_all: bool = False,
		max_slots: Optional[int] = None,
	) -> List[Dict[str, Any]]:
		"""Build available (and optional unavailable) slot dicts for one date using preloaded data."""
		if not self.valid_hours:
			return []

		date_str = normalize_date(target_date) or str(target_date)
		slot_limit = cint(max_slots) if max_slots else 0
		step = timedelta(minutes=self.interval)
		curr_time = self.op_start
		slots: List[Dict[str, Any]] = []

		while curr_time < self.op_end:
			if self.bs_td and self.be_td and self.bs_td <= curr_time < self.be_td:
				if include_all:
					slots.append({
						"start_time": format_timedelta(curr_time),
						"available_bays": 0,
						"available_technicians": 0,
						"effective_capacity": 0,
						"bottleneck": "none",
						"is_available": False,
						"badge_label": _("Lunch"),
						"unavailable_reason": _("Shop closed for lunch break."),
						"bay_available": False,
						"technician_available": False,
						"is_multi_day": False,
						"spans_days": 1,
						"spans_lunch": True,
						"completion_date": date_str,
						"completion_time": format_timedelta(self.be_td),
					})
					if slot_limit and len(slots) >= slot_limit:
						break
				curr_time += step
				continue

			span = calculate_working_span(
				date_str,
				curr_time,
				self.duration_val,
				is_holiday_fn=self.is_holiday,
			)
			prop_start_dt, prop_end_dt = span_datetimes(span)
			avail_bays_count, bay_available, bay_reason, chosen_bay = self._eval_bay_capacity(
				prop_start_dt,
				prop_end_dt,
			)
			avail_techs_count, eff_cap, bottleneck, tech_available, tech_reason, conflict_entry = (
				self._eval_tech_capacity(span, avail_bays_count)
			)

			is_slot_available = bool(eff_cap and eff_cap > 0 and bay_available and tech_available)
			spans_days = span["spans_days"]
			is_multi = span["is_multi_day"]

			if is_slot_available:
				if is_multi:
					day_diff = spans_days - 1
					suffix = "Day" if day_diff == 1 else "Days"
					badge_label = f"+{day_diff} {suffix} ({eff_cap} open)"
				else:
					badge_label = f"{eff_cap} open"
				unavailable_reason = None
			elif not bay_available and not tech_available:
				badge_label = _("Bay & Tech Busy")
				unavailable_reason = _("Neither the selected bay nor the technician is available.")
			elif not tech_available:
				badge_label = tech_reason or _("Tech Busy")
				unavailable_reason = (
					_("Technician {0} is unavailable ({1}).").format(self.target_tech, badge_label)
					if self.target_tech
					else _("No technicians available in shop pool.")
				)
			elif not bay_available:
				badge_label = bay_reason or _("Bay Busy")
				unavailable_reason = (
					_("Service Bay {0} is occupied.").format(self.service_bay)
					if self.service_bay
					else _("No capable service bays available.")
				)
			else:
				badge_label = _("Full")
				unavailable_reason = _("Capacity full.")

			if include_all or is_slot_available:
				slots.append({
					"start_time": format_timedelta(curr_time),
					"available_bays": avail_bays_count,
					"available_technicians": avail_techs_count,
					"effective_capacity": eff_cap,
					"bottleneck": bottleneck,
					"is_available": is_slot_available,
					"badge_label": badge_label,
					"unavailable_reason": unavailable_reason,
					"conflict_entry": conflict_entry,
					"bay_available": bay_available,
					"technician_available": tech_available,
					"is_multi_day": is_multi,
					"spans_days": spans_days,
					"spans_lunch": span.get("spans_lunch", False),
					"completion_date": str(span["completion_date"]),
					"completion_time": str(span["completion_time"]),
					"service_bay": chosen_bay,
				})
				if slot_limit and len(slots) >= slot_limit:
					break

			curr_time += step

		return slots


def search_day_slots(
	target_date: Union[str, date, datetime],
	duration_minutes: Union[int, float],
	*,
	required_tags: Optional[Union[str, List[str]]] = None,
	service_bay: Optional[str] = None,
	max_slots: Optional[int] = None,
	technician: Optional[str] = None,
	include_all: bool = False,
	exclude_entry: Optional[str] = None,
) -> List[Dict[str, Any]]:
	"""Return slots for a single date using a single batched snapshot load."""
	ctx = _SlotSearchContext(
		start_date=target_date,
		end_date=target_date,
		duration_minutes=duration_minutes,
		required_tags=required_tags,
		service_bay=service_bay,
		target_tech=technician,
		exclude_entry=exclude_entry,
	)
	return ctx.slots_for_date(target_date, include_all=include_all, max_slots=max_slots)


def find_next_available_slot_batch(
	start_dt: date,
	max_days: int,
	duration_minutes: Union[int, float],
	*,
	required_tags: Optional[Union[str, List[str]]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	exclude_entry: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
	"""Find the earliest non-holiday date after start_dt (up to max_days) with open slots."""
	if max_days <= 0:
		return None

	window_start = add_days(start_dt, 1)
	window_end = add_days(start_dt, max_days)
	ctx = _SlotSearchContext(
		start_date=window_start,
		end_date=window_end,
		duration_minutes=duration_minutes,
		required_tags=required_tags,
		service_bay=service_bay,
		target_tech=technician,
		exclude_entry=exclude_entry,
	)
	if not ctx.valid_hours:
		return None

	for offset in range(1, max_days + 1):
		target_dt = add_days(start_dt, offset)
		if ctx.is_holiday(target_dt):
			continue
		slots = ctx.slots_for_date(target_dt, include_all=False)
		if slots:
			return {
				"date": str(target_dt),
				"first_slot": slots[0],
				"slots_count": len(slots),
			}
	return None
