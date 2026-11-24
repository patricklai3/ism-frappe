# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import frappe
from frappe.query_builder.functions import Coalesce
from frappe.utils import add_days, cint, getdate

from ism.scheduling.time_utils import (
	calculate_working_span,
	effective_end_time,
	format_timedelta,
	get_shop_settings,
	to_timedelta,
)

# Entries in these states release both bay and technician capacity.
NON_BLOCKING_ENTRY_STATUSES: Tuple[str, ...] = ("Cancelled", "Completed", "No Show")

# Entries in these states never represent worked technician hours on the shop floor.
INACTIVE_WORK_STATUSES: Tuple[str, ...] = ("Cancelled", "No Show")


def parse_required_tags(required_tags: Optional[Union[str, List[str]]]) -> List[str]:
	"""Parse required equipment tags from JSON string, CSV string, or iterable."""
	if isinstance(required_tags, str):
		try:
			parsed = frappe.parse_json(required_tags)
			if isinstance(parsed, (list, tuple, set)):
				return [str(t).strip() for t in parsed if str(t).strip()]
			if parsed:
				return [str(parsed).strip()]
			return []
		except (ValueError, TypeError):
			return [t.strip() for t in required_tags.split(",") if t.strip()]
	if isinstance(required_tags, (list, tuple, set)):
		return [str(t).strip() for t in required_tags if str(t).strip()]
	return list(required_tags) if required_tags else []


def format_required_tags(tags: Optional[Union[str, Iterable[str]]]) -> str:
	"""Format required equipment tags as a sorted, deduplicated comma-separated string."""
	return ", ".join(sorted(set(parse_required_tags(tags))))


def load_bay_tags_map(bay_names: List[str]) -> Dict[str, List[str]]:
	"""Fetch Service Bay Equipment tags grouped by Service Bay in a single query."""
	if not bay_names:
		return {}
	rows = frappe.get_all(
		"Service Bay Equipment",
		filters={"parent": ["in", bay_names], "parenttype": "Service Bay"},
		fields=["parent", "equipment_tag"],
	)
	tags_by_bay: Dict[str, List[str]] = {name: [] for name in bay_names}
	seen_by_bay: Dict[str, Set[str]] = {name: set() for name in bay_names}
	for row in rows:
		parent = row.get("parent")
		tag = (row.get("equipment_tag") or "").strip()
		if parent and tag and tag not in seen_by_bay.setdefault(parent, set()):
			seen_by_bay[parent].add(tag)
			tags_by_bay.setdefault(parent, []).append(tag)
	return tags_by_bay


def load_package_tags_map(package_names: Sequence[str]) -> Dict[str, List[str]]:
	"""Fetch Service Package Equipment tags grouped by Service Package in a single query."""
	clean_names = [n for n in dict.fromkeys(package_names or ()) if n]
	if not clean_names or not frappe.db.exists("DocType", "Service Package Equipment"):
		return {name: [] for name in clean_names}
	rows = frappe.get_all(
		"Service Package Equipment",
		filters={"parent": ["in", clean_names], "parenttype": "Service Package"},
		fields=["parent", "equipment_tag"],
		ignore_permissions=True,
	)
	tags_by_pkg: Dict[str, Set[str]] = {name: set() for name in clean_names}
	for row in rows:
		parent = row.get("parent")
		tag = (row.get("equipment_tag") or "").strip()
		if parent and tag:
			tags_by_pkg.setdefault(parent, set()).add(tag)
	return {name: sorted(tags_by_pkg.get(name, set())) for name in clean_names}


def get_required_tags_for_packages(package_names: Sequence[str]) -> List[str]:
	"""Return sorted deduplicated union of equipment tags across Service Packages."""
	tags_map = load_package_tags_map(package_names)
	union_tags: Set[str] = set()
	for tags in tags_map.values():
		union_tags.update(tags)
	return sorted(union_tags)


def get_required_tags_for_repair_order(repair_order: Any) -> List[str]:
	"""Derive sorted deduplicated required equipment tags for a Repair Order or RO name."""
	if not repair_order:
		return []

	if isinstance(repair_order, str):
		if not frappe.db.exists("Repair Order", repair_order):
			return []
		ro = frappe.get_doc("Repair Order", repair_order)
	else:
		ro = repair_order

	get_field = (lambda obj, key: obj.get(key)) if isinstance(ro, dict) else (lambda obj, key: getattr(obj, key, None))
	tags: Set[str] = set(parse_required_tags(get_field(ro, "required_equipment_tags")))
	fallback_packages: List[str] = []

	for row in get_field(ro, "services") or ():
		row_get = (lambda r, k: r.get(k)) if isinstance(row, dict) else (lambda r, k: getattr(r, k, None))
		row_tags = parse_required_tags(row_get(row, "required_equipment_tags"))
		if row_tags:
			tags.update(row_tags)
		else:
			pkg_name = row_get(row, "service_package")
			if pkg_name:
				fallback_packages.append(str(pkg_name).strip())

	if fallback_packages:
		tags.update(get_required_tags_for_packages(fallback_packages))

	return sorted(tags)


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
		exclude_entry: Optional[str] = None,
		vehicle_identity: Optional[Any] = None,
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
		self.exclude_entry: Optional[str] = exclude_entry
		self.vehicle_identity: Optional[Any] = vehicle_identity

		self.candidate_bays: List[str] = []
		self.bay_tags: Dict[str, List[str]] = {}
		self.active_techs: List[Dict[str, Any]] = []
		self.tech_names: Dict[str, str] = {}
		self.holidays: Dict[str, str] = {}
		self._leaves_by_emp: Dict[str, List[Dict[str, Any]]] = {}
		self._entries_by_date: Dict[str, List[Tuple[timedelta, timedelta, Optional[str], Optional[str]]]] = {}
		self._vehicle_busy_by_date: Dict[str, List[Tuple[timedelta, timedelta]]] = {}
		self._phone_busy_by_date: Dict[str, List[Tuple[timedelta, timedelta]]] = {}
		self._max_phone_concurrent: int = max(
			1, cint(self.settings.get("max_concurrent_bookings_per_phone") or 2)
		)

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
		se = frappe.qb.DocType("Schedule Entry")
		select_cols = [
			se.scheduled_date,
			se.scheduled_time,
			se.completion_date,
			se.completion_time,
			se.estimated_duration,
			se.service_bay,
			se.assigned_technician,
		]
		track_vehicle = False
		target_preview = None
		if self.vehicle_identity is not None:
			from ism.scheduling.entry_queries import (
				extract_vehicle_identity,
				match_booking_conflict,
				resolve_conflict_identities_batch,
			)

			target_preview = extract_vehicle_identity(self.vehicle_identity)
			if target_preview.get("has_conflict_identity"):
				track_vehicle = True
				select_cols.extend([
					se.name,
					se.repair_order,
					se.customer,
					se.repair_vehicle,
					se.provisional_customer_name,
					se.provisional_phone,
					se.provisional_email,
					se.provisional_vehicle_info,
					se.provisional_vehicle_model,
					se.provisional_vehicle_year,
					se.provisional_vehicle_color,
					se.provisional_license_plate,
					se.provisional_vin_or_plate,
				])

		query = (
			frappe.qb.from_(se)
			.select(*select_cols)
			.where(se.scheduled_date <= end_str)
			.where(Coalesce(se.completion_date, se.scheduled_date) >= start_str)
			.where(se.status.notin(list(NON_BLOCKING_ENTRY_STATUSES)))
		)
		if self.exclude_entry:
			query = query.where(se.name != self.exclude_entry)
		entries = query.run(as_dict=True)

		match_kinds: List[Optional[str]] = [None] * len(entries)
		if track_vehicle and entries:
			target_id, cand_ids = resolve_conflict_identities_batch(target_preview, entries)
			match_kinds = [match_booking_conflict(target_id, cid) for cid in cand_ids]

		by_date: Dict[str, List[Tuple[timedelta, timedelta, Optional[str], Optional[str]]]] = {}
		vehicle_busy: Dict[str, List[Tuple[timedelta, timedelta]]] = {}
		phone_busy: Dict[str, List[Tuple[timedelta, timedelta]]] = {}
		for idx, entry in enumerate(entries):
			if not entry.get("scheduled_date"):
				continue
			s_dt = getdate(entry["scheduled_date"])
			ex_start = to_timedelta(entry.get("scheduled_time"))
			ex_duration = entry.get("estimated_duration") or 0
			comp_date_raw = entry.get("completion_date")
			comp_time_raw = entry.get("completion_time")

			if comp_date_raw and comp_time_raw is not None and str(comp_time_raw).strip() != "":
				c_dt = getdate(comp_date_raw)
				c_td = to_timedelta(comp_time_raw)
				if c_dt == s_dt and c_td <= ex_start:
					c_td = effective_end_time(ex_start, ex_duration, self.bs_td, self.be_td)
			else:
				eff_end = effective_end_time(ex_start, ex_duration, self.bs_td, self.be_td)
				if self.op_start >= self.op_end or eff_end <= self.op_end:
					c_dt = s_dt
					c_td = eff_end
				else:
					span = calculate_working_span(
						s_dt,
						ex_start,
						ex_duration,
						is_holiday_fn=self.is_holiday if self.skip_holidays else None,
					)
					c_dt = getdate(span["completion_date"])
					c_td = to_timedelta(span["completion_time"])

			bay = entry.get("service_bay")
			tech = entry.get("assigned_technician")
			conflict_kind = match_kinds[idx]
			curr_dt = max(s_dt, self.start_dt)
			last_dt = min(c_dt, self.end_dt)
			while curr_dt <= last_dt:
				dt_key = str(curr_dt)
				if not (self.skip_holidays and dt_key in self.holidays):
					day_start = ex_start if curr_dt == s_dt else self.op_start
					day_end = c_td if curr_dt == c_dt else self.op_end
					if day_end > day_start:
						by_date.setdefault(dt_key, []).append((day_start, day_end, bay, tech))
						if conflict_kind == "vehicle":
							vehicle_busy.setdefault(dt_key, []).append((day_start, day_end))
						elif conflict_kind == "phone":
							phone_busy.setdefault(dt_key, []).append((day_start, day_end))
				curr_dt = add_days(curr_dt, 1)
		self._vehicle_busy_by_date = vehicle_busy
		self._phone_busy_by_date = phone_busy
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
		vehicle_busy = self._vehicle_busy_by_date.get(target_str, ())
		phone_busy = self._phone_busy_by_date.get(target_str, ())
		peak_fn = None
		if phone_busy:
			from ism.scheduling.entry_queries import peak_concurrent_overlaps as peak_fn
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

			if vehicle_busy and any(
				max(curr_time, v_start) < min(eff_end, v_end) for v_start, v_end in vehicle_busy
			):
				curr_time += step
				continue

			if phone_busy and peak_fn is not None and peak_fn(curr_time, eff_end, phone_busy) > self._max_phone_concurrent:
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
