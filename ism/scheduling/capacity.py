# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import frappe
from frappe.utils import add_days, cint, getdate

from ism.scheduling.time_utils import effective_end_time, format_timedelta, get_shop_settings, to_timedelta

# Entries in these states release both bay and technician capacity.
NON_BLOCKING_ENTRY_STATUSES: Tuple[str, ...] = ("Cancelled", "Completed", "No Show")

# Entries in these states never represent worked technician hours on the shop floor.
INACTIVE_WORK_STATUSES: Tuple[str, ...] = ("Cancelled", "No Show")


def parse_required_tags(required_tags: Optional[Union[str, List[str]]]) -> List[str]:
	"""Parse required equipment tags from JSON string, CSV string, or iterable."""
	if isinstance(required_tags, str):
		try:
			parsed = frappe.parse_json(required_tags)
			return list(parsed) if parsed else []
		except Exception:
			return [t.strip() for t in required_tags.split(",") if t.strip()]
	return list(required_tags) if required_tags else []


def load_bay_tags_map(bay_names: List[str]) -> Dict[str, List[str]]:
	"""Fetch Service Bay Equipment tags grouped by Service Bay in a single query."""
	if not bay_names:
		return {}
	rows = frappe.get_all(
		"Service Bay Equipment",
		filters={"parent": ["in", bay_names]},
		fields=["parent", "equipment_tag"],
	)
	tags_by_bay: Dict[str, List[str]] = {name: [] for name in bay_names}
	for row in rows:
		parent = row.get("parent")
		tag = row.get("equipment_tag")
		if parent and tag:
			tags_by_bay.setdefault(parent, []).append(tag)
	return tags_by_bay


def resolve_candidate_bays(
	service_bay: Optional[str],
	required_tags_set: Set[str],
) -> Tuple[List[str], Dict[str, List[str]]]:
	"""Return matching bay IDs and their equipment tags without N+1 queries."""
	if service_bay:
		return [service_bay], load_bay_tags_map([service_bay])

	active_bays = frappe.get_all(
		"Service Bay",
		filters={"is_active": 1},
		pluck="name",
		order_by="name asc",
	)
	if not active_bays:
		return [], {}

	tags_by_bay = load_bay_tags_map(list(active_bays))
	# Prefer dedicated single-purpose bays over multi-tag bays to preserve scarce equipment capacity.
	matching = sorted(
		(b for b in active_bays if required_tags_set.issubset(set(tags_by_bay.get(b, [])))),
		key=lambda b: (len(tags_by_bay.get(b, [])), b),
	)
	return matching, {b: tags_by_bay.get(b, []) for b in matching}


class ShopCapacityWindow:
	"""Batch-loaded scheduling snapshot over a date range for fast multi-day slot search."""

	def __init__(
		self,
		start_date: Union[str, date, datetime],
		end_date: Union[str, date, datetime],
		required_tags: Optional[Union[str, List[str]]] = None,
		service_bay: Optional[str] = None,
		skip_holidays: bool = False,
	) -> None:
		self.settings = get_shop_settings()
		self.start_dt = getdate(start_date)
		self.end_dt = getdate(end_date)
		self.op_start: timedelta = self.settings["operating_hours_start"]
		self.op_end: timedelta = self.settings["operating_hours_end"]
		self.bs_td: Optional[timedelta] = self.settings["break_start"]
		self.be_td: Optional[timedelta] = self.settings["break_end"]
		self.interval: int = int(self.settings["default_slot_interval"] or 30)
		self.enable_tech: int = cint(self.settings.get("enable_technician_capacity"))
		self.skip_holidays: bool = bool(skip_holidays)

		self.candidate_bays: List[str] = []
		self.bay_tags: Dict[str, List[str]] = {}
		self.active_techs: List[Dict[str, Any]] = []
		self.tech_names: Dict[str, str] = {}
		self.holidays: Dict[str, str] = {}
		self._leaves_by_emp: Dict[str, List[Dict[str, Any]]] = {}
		self._entries_by_date: Dict[str, List[Tuple[timedelta, timedelta, Optional[str], Optional[str]]]] = {}

		if self.op_start >= self.op_end or self.interval <= 0 or self.end_dt < self.start_dt:
			return

		required_tags_set = set(parse_required_tags(required_tags))
		self.candidate_bays, self.bay_tags = resolve_candidate_bays(service_bay, required_tags_set)
		if not self.candidate_bays:
			return

		start_str = str(self.start_dt)
		end_str = str(self.end_dt)

		if self.skip_holidays:
			self.holidays = self._load_holidays(start_str, end_str)

		if self.enable_tech:
			self.active_techs = self._load_active_techs()
			if not self.active_techs:
				return
			self.tech_names = {t["name"]: (t.get("employee_name") or t["name"]) for t in self.active_techs}
			self._leaves_by_emp = self._load_leaves(start_str, end_str)

		self._entries_by_date = self._load_blocking_entries(start_str, end_str)

	def _load_holidays(self, start_str: str, end_str: str) -> Dict[str, str]:
		holiday_list = self.settings.get("holiday_list")
		if not holiday_list or not frappe.db.exists("DocType", "Holiday"):
			return {}
		rows = frappe.get_all(
			"Holiday",
			filters=[
				["parent", "=", holiday_list],
				["holiday_date", ">=", start_str],
				["holiday_date", "<=", end_str],
			],
			fields=["holiday_date", "description"],
		)
		return {str(r["holiday_date"]): (r.get("description") or "Holiday") for r in rows if r.get("holiday_date")}

	def _load_active_techs(self) -> List[Dict[str, Any]]:
		designation = self.settings.get("technician_designation") or "Technician"
		return frappe.get_all(
			"Employee",
			filters={"designation": designation, "status": "Active"},
			fields=["name", "employee_name", "designation"],
			order_by="employee_name asc, name asc",
		)

	def _load_leaves(self, start_str: str, end_str: str) -> Dict[str, List[Dict[str, Any]]]:
		if not frappe.db.exists("DocType", "Leave Application"):
			return {}
		emp_ids = [t["name"] for t in self.active_techs]
		if not emp_ids:
			return {}
		leaves = frappe.get_all(
			"Leave Application",
			filters=[
				["employee", "in", emp_ids],
				["status", "=", "Approved"],
				["docstatus", "=", 1],
				["from_date", "<=", end_str],
				["to_date", ">=", start_str],
			],
			fields=["employee", "from_date", "to_date", "half_day", "half_day_date"],
		)
		grouped: Dict[str, List[Dict[str, Any]]] = {}
		for row in leaves:
			grouped.setdefault(row["employee"], []).append(row)
		return grouped

	def _load_blocking_entries(
		self,
		start_str: str,
		end_str: str,
	) -> Dict[str, List[Tuple[timedelta, timedelta, Optional[str], Optional[str]]]]:
		entries = frappe.get_all(
			"Schedule Entry",
			filters=[
				["scheduled_date", ">=", start_str],
				["scheduled_date", "<=", end_str],
				["status", "not in", list(NON_BLOCKING_ENTRY_STATUSES)],
			],
			fields=["scheduled_date", "service_bay", "assigned_technician", "scheduled_time", "estimated_duration"],
		)
		by_date: Dict[str, List[Tuple[timedelta, timedelta, Optional[str], Optional[str]]]] = {}
		for entry in entries:
			dt_str = str(entry.get("scheduled_date"))
			ex_start = to_timedelta(entry.get("scheduled_time"))
			ex_duration = entry.get("estimated_duration") or 0
			ex_end = effective_end_time(ex_start, ex_duration, self.bs_td, self.be_td)
			by_date.setdefault(dt_str, []).append((
				ex_start,
				ex_end,
				entry.get("service_bay"),
				entry.get("assigned_technician"),
			))
		return by_date

	def is_holiday(self, target_date: Union[str, date, datetime]) -> bool:
		return str(getdate(target_date)) in self.holidays

	def _on_duty_tech_ids(self, target_str: str) -> Optional[List[str]]:
		if not self.enable_tech:
			return None
		on_duty: List[str] = []
		for emp in self.active_techs:
			emp_id = emp["name"]
			leaves = self._leaves_by_emp.get(emp_id, ())
			on_leave = False
			for lv in leaves:
				if lv.get("half_day"):
					if str(lv.get("half_day_date") or "") == target_str:
						on_leave = True
						break
				else:
					from_str = str(lv.get("from_date") or "")
					to_str = str(lv.get("to_date") or "")
					if from_str <= target_str <= to_str:
						on_leave = True
						break
			if not on_leave:
				on_duty.append(emp_id)
		return on_duty

	def slots_for_date(
		self,
		target_date: Union[str, date, datetime],
		duration_minutes: Union[int, float],
		max_slots: Optional[int] = None,
		min_start_time: Optional[Union[str, timedelta]] = None,
	) -> List[Dict[str, Any]]:
		"""Compute available slots for a single date from preloaded capacity data."""
		if self.op_start >= self.op_end or self.interval <= 0 or not self.candidate_bays:
			return []

		target_str = str(getdate(target_date))
		if self.skip_holidays and target_str in self.holidays:
			return []

		on_duty_techs = self._on_duty_tech_ids(target_str)
		if on_duty_techs is not None and not on_duty_techs:
			return []

		duration_val = float(duration_minutes or 0)
		if duration_val <= 0:
			duration_val = float(self.settings.get("default_diagnostic_duration") or 60)

		min_td = to_timedelta(min_start_time) if min_start_time is not None else None
		day_intervals = self._entries_by_date.get(target_str, ())
		slot_limit = cint(max_slots) if max_slots else 0
		step = timedelta(minutes=self.interval)
		curr_time = self.op_start
		slots: List[Dict[str, Any]] = []

		while curr_time < self.op_end:
			if min_td is not None and curr_time < min_td:
				curr_time += step
				continue
			if self.bs_td and self.be_td and self.bs_td <= curr_time < self.be_td:
				curr_time += step
				continue

			eff_end = effective_end_time(curr_time, duration_val, self.bs_td, self.be_td)
			if eff_end > self.op_end:
				curr_time += step
				continue

			occupied_bays: Set[str] = set()
			occupied_techs: Set[str] = set()
			for ex_start, ex_end, ex_bay, ex_tech in day_intervals:
				if max(curr_time, ex_start) < min(eff_end, ex_end):
					if ex_bay:
						occupied_bays.add(ex_bay)
					if ex_tech:
						occupied_techs.add(ex_tech)

			avail_bay_ids = [b for b in self.candidate_bays if b not in occupied_bays]
			avail_bays_count = len(avail_bay_ids)

			avail_tech_ids: Optional[List[str]] = None
			if on_duty_techs is not None:
				avail_tech_ids = [t for t in on_duty_techs if t not in occupied_techs]
				avail_techs_count: Optional[int] = len(avail_tech_ids)
				eff_cap = min(avail_bays_count, avail_techs_count)
				if avail_techs_count < avail_bays_count:
					bottleneck: Optional[str] = "technician"
				elif avail_bays_count < avail_techs_count:
					bottleneck = "bay"
				else:
					bottleneck = "none"
			else:
				avail_techs_count = None
				eff_cap = avail_bays_count
				bottleneck = None

			if eff_cap > 0:
				slot_info: Dict[str, Any] = {
					"start_time": format_timedelta(curr_time),
					"available_bays": avail_bays_count,
					"available_bay_ids": avail_bay_ids,
					"available_technicians": avail_techs_count,
					"effective_capacity": eff_cap,
					"bottleneck": bottleneck,
				}
				if avail_tech_ids is not None:
					slot_info["available_technician_ids"] = avail_tech_ids
				slots.append(slot_info)
				if slot_limit and len(slots) >= slot_limit:
					break

			curr_time += step

		return slots

	def iter_dates(self) -> Iterable[date]:
		curr = self.start_dt
		while curr <= self.end_dt:
			yield curr
			curr = add_days(curr, 1)
