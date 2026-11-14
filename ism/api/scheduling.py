# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from typing import Any, Dict, List, Optional, Union
from datetime import date, datetime, time, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate

from ism.scheduling.capacity import (
	INACTIVE_WORK_STATUSES,
	NON_BLOCKING_ENTRY_STATUSES,
	ShopCapacityWindow,
	load_bay_tags_map,
	parse_required_tags as _parse_required_tags,
	resolve_candidate_bays,
)
from ism.scheduling.time_utils import (
	calculate_working_span as _calculate_working_span,
	effective_end_time as _effective_end_time,
	format_timedelta,
	get_shop_settings,
	to_timedelta,
)


def _ensure_schedule_read_permission(include_bays: bool = False) -> None:
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if include_bays:
		frappe.has_permission("Service Bay", "read", throw=True)


@frappe.whitelist()
def effective_end_time(
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	break_start: Union[str, time, timedelta, None] = None,
	break_end: Union[str, time, timedelta, None] = None,
) -> timedelta:
	"""Calculate clock-time end of an appointment, accounting for lunch break window."""
	_ensure_schedule_read_permission()
	return _effective_end_time(start_time, duration_minutes, break_start, break_end)


@frappe.whitelist()
def calculate_working_span(
	start_date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
) -> Dict[str, Any]:
	"""Calculate active working segments and completion datetime across lunch, overnight, and holidays."""
	_ensure_schedule_read_permission()
	return _calculate_working_span(
		start_date,
		start_time,
		duration_minutes,
		is_holiday_fn=lambda d: bool(is_holiday(d).get("is_holiday")),
	)


def _span_datetimes(span: Dict[str, Any]) -> tuple[datetime, datetime]:
	s_d = getdate(span["start_date"])
	s_t = to_timedelta(span["start_time"])
	e_d = getdate(span["completion_date"])
	e_t = to_timedelta(span["completion_time"])
	return (
		datetime.combine(s_d, (datetime.min + s_t).time()),
		datetime.combine(e_d, (datetime.min + e_t).time()),
	)


def _entry_datetimes(entry: Dict[str, Any]) -> tuple[datetime, datetime]:
	ex_s_d = getdate(entry.get("scheduled_date"))
	ex_s_t = to_timedelta(entry.get("scheduled_time"))
	ex_c_d = getdate(entry["completion_date"]) if entry.get("completion_date") else None
	ex_c_t = to_timedelta(entry["completion_time"]) if entry.get("completion_time") else None
	if not ex_c_d or not ex_c_t:
		ex_span = calculate_working_span(
			entry.get("scheduled_date"),
			entry.get("scheduled_time"),
			entry.get("estimated_duration") or 60,
		)
		ex_c_d = getdate(ex_span["completion_date"])
		ex_c_t = to_timedelta(ex_span["completion_time"])
	return (
		datetime.combine(ex_s_d, (datetime.min + ex_s_t).time()),
		datetime.combine(ex_c_d, (datetime.min + ex_c_t).time()),
	)


@frappe.whitelist()
def check_bay_availability(
	service_bay: str,
	date: str,
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
) -> bool:
	"""Check if service_bay is available for proposed date, start_time, and duration.

	Uses multi-day continuous span awareness across lunch breaks and overnight holds.
	Returns True if bay is free, False if an overlap collision exists.
	"""
	_ensure_schedule_read_permission(include_bays=True)
	if not service_bay or not isinstance(service_bay, str) or not date:
		return False
	if exclude_entry is not None and not isinstance(exclude_entry, str):
		frappe.throw(_("Invalid exclude_entry identifier."), frappe.ValidationError)

	span = calculate_working_span(date, start_time, duration_minutes)
	prop_start_dt, prop_end_dt = _span_datetimes(span)
	p_start_d = getdate(span["start_date"])
	p_end_d = getdate(span["completion_date"])
	lookback_date = add_days(p_start_d, -30)

	filters = [
		["service_bay", "=", service_bay],
		["scheduled_date", "<=", str(p_end_d)],
		["scheduled_date", ">=", str(lookback_date)],
		["status", "not in", list(NON_BLOCKING_ENTRY_STATUSES)],
	]
	if exclude_entry:
		filters.append(["name", "!=", exclude_entry])

	# Shop-wide overlap check intentionally scans all non-blocking entries after read perm verification.
	existing_entries = frappe.get_all(
		"Schedule Entry",
		filters=filters,
		fields=[
			"name",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
			"completion_date",
			"completion_time",
		],
	)

	for entry in existing_entries:
		ex_start_dt, ex_end_dt = _entry_datetimes(entry)
		if max(prop_start_dt, ex_start_dt) < min(prop_end_dt, ex_end_dt):
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
	_ensure_schedule_read_permission(include_bays=True)
	if not date:
		return []

	required_tags_set = set(_parse_required_tags(required_tags))
	candidate_bays, tags_by_bay = resolve_candidate_bays(None, required_tags_set)
	if not candidate_bays:
		return []

	span = calculate_working_span(date, start_time, duration_minutes)
	prop_start_dt, prop_end_dt = _span_datetimes(span)
	p_start_d = getdate(span["start_date"])
	p_end_d = getdate(span["completion_date"])
	lookback_date = add_days(p_start_d, -30)

	entries = frappe.get_all(
		"Schedule Entry",
		filters=[
			["service_bay", "in", candidate_bays],
			["scheduled_date", "<=", str(p_end_d)],
			["scheduled_date", ">=", str(lookback_date)],
			["status", "not in", list(NON_BLOCKING_ENTRY_STATUSES)],
		],
		fields=[
			"service_bay",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
			"completion_date",
			"completion_time",
		],
	)
	occupied_bays = set()
	for entry in entries:
		bay = entry.get("service_bay")
		if not bay or bay in occupied_bays:
			continue
		ex_start_dt, ex_end_dt = _entry_datetimes(entry)
		if max(prop_start_dt, ex_start_dt) < min(prop_end_dt, ex_end_dt):
			occupied_bays.add(bay)

	return [
		{"bay_name": bay_name, "equipment_tags": tags_by_bay.get(bay_name, [])}
		for bay_name in candidate_bays
		if bay_name not in occupied_bays
	]


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
	_ensure_schedule_read_permission(include_bays=True)
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


def _resolve_candidate_bays(
	service_bay: Optional[str],
	required_tags_set: set[str],
) -> List[str]:
	bays, _tags = resolve_candidate_bays(service_bay, required_tags_set)
	return bays


def _normalize_optional_tech(technician: Optional[str]) -> Optional[str]:
	if not technician:
		return None
	text = str(technician).strip()
	if not text or text.lower() == "none":
		return None
	return text


def _slot_technician_capacity(
	date: str,
	curr_time: timedelta,
	duration_val: float,
	target_tech: Optional[str],
	enable_tech: Any,
	avail_bays_count: int,
) -> tuple[Optional[int], int, Optional[str], bool, Optional[str], Optional[str]]:
	tech_available = True
	tech_reason: Optional[str] = None
	conflict_entry: Optional[str] = None
	avail_techs_count: Optional[int] = None

	if target_tech:
		try:
			from ism.api.technician_availability import check_technician_availability

			tech_check = check_technician_availability(
				employee=target_tech,
				date=date,
				start_time=curr_time,
				duration_minutes=duration_val,
			)
			if tech_check.get("is_available"):
				avail_techs_count = 1
			else:
				avail_techs_count = 0
				tech_available = False
				if tech_check.get("on_leave"):
					tech_reason = _("On Leave")
				elif tech_check.get("conflicts"):
					conflicts = tech_check["conflicts"]
					conflict_entry = conflicts[0].get("name") if conflicts else None
					tech_reason = _("Tech Busy")
				else:
					tech_reason = _("Tech Unavailable")
		except ImportError:
			avail_techs_count = None
	elif enable_tech:
		try:
			from ism.api.technician_availability import get_technician_pool_availability

			tech_pool = get_technician_pool_availability(date, curr_time, duration_val)
			avail_techs_count = tech_pool.get("available", 0)
			if avail_techs_count == 0:
				tech_available = False
				tech_reason = _("Techs Full")
		except ImportError:
			avail_techs_count = None

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


@frappe.whitelist()
def get_available_slots(
	date: str,
	duration_minutes: Union[int, float],
	required_tags: Optional[Union[str, List[str]]] = None,
	service_bay: Optional[str] = None,
	max_slots: Optional[int] = None,
	technician: Optional[str] = None,
	assigned_technician: Optional[str] = None,
	include_all: Union[int, bool] = 0,
) -> List[Dict[str, Any]]:
	"""Return available slot dicts for date and duration across operating hours.

	Supports multi-day working spans, technician filtering, and include_all unavailable reasons.
	"""
	_ensure_schedule_read_permission(include_bays=True)
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

	target_tech = _normalize_optional_tech(technician or assigned_technician)
	show_all = bool(cint(include_all))
	slot_limit = cint(max_slots) if max_slots else 0

	slots: List[Dict[str, Any]] = []
	curr_time = op_start

	while curr_time < op_end:
		if bs_td and be_td and bs_td <= curr_time < be_td:
			if show_all:
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
					"completion_date": str(date),
					"completion_time": format_timedelta(be_td),
				})
				if slot_limit and len(slots) >= slot_limit:
					break
			curr_time += timedelta(minutes=interval)
			continue

		span = calculate_working_span(date, curr_time, duration_val)
		comp_date = span["completion_date"]
		comp_time = span["completion_time"]
		spans_days = span["spans_days"]
		is_multi = span["is_multi_day"]

		bay_available = True
		bay_reason = None
		avail_bays: List[Dict[str, Any]] = []
		if service_bay:
			if check_bay_availability(service_bay, date, curr_time, duration_val):
				avail_bays_count = 1
			else:
				avail_bays_count = 0
				bay_available = False
				bay_reason = _("Bay Busy")
		else:
			avail_bays = get_available_bays(date, curr_time, duration_val, required_tags)
			avail_bays_count = len(avail_bays)
			if avail_bays_count == 0:
				bay_available = False
				bay_reason = _("Bays Full")

		avail_techs_count, eff_cap, bottleneck, tech_available, tech_reason, conflict_entry = (
			_slot_technician_capacity(date, curr_time, duration_val, target_tech, enable_tech, avail_bays_count)
		)

		is_slot_available = bool(eff_cap and eff_cap > 0 and bay_available and tech_available)
		if is_slot_available:
			if is_multi:
				day_diff = spans_days - 1
				badge_label = f"+{day_diff} Day ({eff_cap} open)" if day_diff == 1 else f"+{day_diff} Days ({eff_cap} open)"
			else:
				badge_label = f"{eff_cap} open"
			unavailable_reason = None
		elif not bay_available and not tech_available:
			badge_label = _("Bay & Tech Busy")
			unavailable_reason = _("Neither the selected bay nor the technician is available.")
		elif not tech_available:
			badge_label = tech_reason or _("Tech Busy")
			unavailable_reason = (
				_("Technician {0} is unavailable ({1}).").format(target_tech, badge_label)
				if target_tech
				else _("No technicians available in shop pool.")
			)
		elif not bay_available:
			badge_label = bay_reason or _("Bay Busy")
			unavailable_reason = (
				_("Service Bay {0} is occupied.").format(service_bay)
				if service_bay
				else _("No capable service bays available.")
			)
		else:
			badge_label = _("Full")
			unavailable_reason = _("Capacity full.")

		if show_all or is_slot_available:
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
				"completion_date": str(comp_date),
				"completion_time": str(comp_time),
				"service_bay": (avail_bays[0]["bay_name"] if avail_bays and not service_bay else service_bay),
			})
			if slot_limit and len(slots) >= slot_limit:
				break

		curr_time += timedelta(minutes=interval)

	return slots


@frappe.whitelist()
def is_holiday(date: Union[str, datetime, date]) -> Dict[str, Any]:
	"""Check if a given date is a holiday according to ISM Settings -> holiday_list."""
	_ensure_schedule_read_permission()
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
	_ensure_schedule_read_permission()
	from ism.api.scheduling_views import get_calendar_events as _get_events

	return _get_events(*args, **kwargs)


@frappe.whitelist()
def find_next_available_slot(
	from_date: Optional[str] = None,
	duration_minutes: Union[int, float] = 60,
	required_tags: Optional[Union[str, List[str]]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	assigned_technician: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
	"""Search forward from from_date up to scheduling_horizon_days for the earliest date with available slots."""
	_ensure_schedule_read_permission(include_bays=True)
	settings = get_shop_settings()
	horizon = settings.get("scheduling_horizon_days") or 30
	max_days = min(int(horizon), 30)

	start_dt = getdate(from_date) if from_date else getdate()
	target_tech = _normalize_optional_tech(technician or assigned_technician)

	if target_tech:
		for offset in range(1, max_days + 1):
			target_dt = add_days(start_dt, offset)
			if is_holiday(target_dt).get("is_holiday"):
				continue
			slots = get_available_slots(
				date=str(target_dt),
				duration_minutes=duration_minutes,
				required_tags=required_tags,
				service_bay=service_bay,
				technician=target_tech,
			)
			if slots:
				return {
					"date": str(target_dt),
					"first_slot": slots[0],
					"slots_count": len(slots),
				}
		return None

	window_start = add_days(start_dt, 1)
	window_end = add_days(start_dt, max_days)

	window = ShopCapacityWindow(
		start_date=window_start,
		end_date=window_end,
		required_tags=required_tags,
		service_bay=service_bay,
		skip_holidays=True,
	)

	for target_dt in window.iter_dates():
		slots = window.slots_for_date(target_dt, duration_minutes)
		if slots:
			return {
				"date": str(target_dt),
				"first_slot": slots[0],
				"slots_count": len(slots),
			}
	return None


@frappe.whitelist()
def get_unscheduled_services(repair_order: str) -> List[Dict[str, Any]]:
	"""Return services on Repair Order that are not yet allocated to an active Schedule Entry."""
	_ensure_schedule_read_permission()
	frappe.has_permission("Repair Order", "read", throw=True)
	if not repair_order or not isinstance(repair_order, str) or not frappe.db.exists("Repair Order", repair_order):
		return []
	frappe.has_permission("Repair Order", "read", doc=repair_order, throw=True)

	has_se_field = frappe.db.has_column("Repair Order Service", "schedule_entry")
	fields = [
		"name",
		"service_package",
		"service_title",
		"labor_hours",
		"is_supplement",
		"status",
		"assigned_technician",
	]
	if has_se_field:
		fields.append("schedule_entry")

	services = frappe.get_all(
		"Repair Order Service",
		filters={"parent": repair_order},
		fields=fields,
		order_by="idx asc, creation asc",
	)

	linked_se_names = list({s.get("schedule_entry") for s in services if has_se_field and s.get("schedule_entry")})
	se_status_map: Dict[str, str] = {}
	if linked_se_names:
		for row in frappe.get_all(
			"Schedule Entry",
			filters={"name": ["in", linked_se_names]},
			fields=["name", "status"],
		):
			se_status_map[row["name"]] = row.get("status") or ""

	unscheduled = []
	for s in services:
		if s.get("status") == "Declined":
			continue

		se_name = s.get("schedule_entry") if has_se_field else None
		if se_name and se_status_map.get(se_name) not in INACTIVE_WORK_STATUSES:
			continue

		title = s.get("service_title") or s.get("service_package") or _("Service")
		hours = float(s.get("labor_hours") or 0)
		mins = int(round(hours * 60))
		unscheduled.append({
			"name": s["name"],
			"service_title": title,
			"labor_hours": hours,
			"labor_minutes": mins,
			"is_supplement": int(s.get("is_supplement") or 0),
			"status": s.get("status"),
			"assigned_technician": s.get("assigned_technician"),
		})

	return unscheduled


def _format_services_summary(selected_services: List[str], repair_order: Optional[str]) -> Optional[str]:
	if not (selected_services and repair_order):
		return None

	rows = frappe.get_all(
		"Repair Order Service",
		filters={"parent": repair_order, "name": ["in", selected_services]},
		fields=["name", "service_title", "service_package", "labor_hours", "is_supplement"],
		order_by="idx asc",
	)
	by_name = {r["name"]: r for r in rows}
	lines = []
	for svc_name in selected_services:
		svc = by_name.get(svc_name)
		if not svc:
			continue
		stitle = svc.get("service_title") or svc.get("service_package") or "Service"
		supp_badge = " [Supplement]" if svc.get("is_supplement") else ""
		shours = float(svc.get("labor_hours") or 0)
		lines.append(f"- {stitle}{supp_badge} ({shours:.1f} hrs)")
	return "\n".join(lines) if lines else None


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
	provisional_customer_name: Optional[str] = None,
	provisional_vehicle_info: Optional[str] = None,
	allow_technician_overlap: Union[int, bool] = 0,
) -> Dict[str, Any]:
	"""Create or update a Schedule Entry and link allocated Repair Order services."""
	if schedule_entry_name is not None and not isinstance(schedule_entry_name, str):
		frappe.throw(_("Invalid Schedule Entry identifier."), frappe.ValidationError)
	if repair_order is not None and not isinstance(repair_order, str):
		frappe.throw(_("Invalid Repair Order identifier."), frappe.ValidationError)

	target_tech = _normalize_optional_tech(assigned_technician)
	if target_tech and scheduled_date and scheduled_time and not cint(allow_technician_overlap):
		try:
			from ism.api.technician_availability import check_technician_availability

			tech_check = check_technician_availability(
				employee=target_tech,
				date=scheduled_date,
				start_time=scheduled_time,
				duration_minutes=estimated_duration,
				exclude_entry=schedule_entry_name,
			)
			if tech_check.get("on_leave"):
				frappe.throw(
					_("Technician '{0}' is on approved leave during this work window.").format(target_tech),
					frappe.ValidationError,
				)
			if tech_check.get("conflicts"):
				conflict_names = ", ".join(c.get("name") for c in tech_check["conflicts"] if c.get("name"))
				frappe.throw(
					_(
						"Technician '{0}' already has an overlapping allocation ({1}) during this work window. "
						"Confirm double-booking to proceed."
					).format(target_tech, conflict_names),
					frappe.ValidationError,
				)
		except ImportError:
			pass

	if isinstance(selected_services, str):
		try:
			selected_services = frappe.parse_json(selected_services)
		except Exception:
			selected_services = [s.strip() for s in selected_services.split(",") if s.strip()]
	if not selected_services:
		selected_services = []

	is_update = bool(schedule_entry_name and frappe.db.exists("Schedule Entry", schedule_entry_name))
	if is_update:
		frappe.has_permission("Schedule Entry", "write", throw=True)
		doc = frappe.get_doc("Schedule Entry", schedule_entry_name)
		doc.check_permission("write")
	else:
		frappe.has_permission("Schedule Entry", "create", throw=True)

	if repair_order:
		if not frappe.db.exists("Repair Order", repair_order):
			frappe.throw(_("Repair Order {0} does not exist.").format(repair_order), frappe.DoesNotExistError)
		frappe.has_permission("Repair Order", "read", doc=repair_order, throw=True)
		if selected_services:
			frappe.has_permission("Repair Order", "write", doc=repair_order, throw=True)

	items_summary = _format_services_summary(selected_services, repair_order)

	if is_update:
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
		if provisional_customer_name is not None:
			doc.provisional_customer_name = provisional_customer_name
		if provisional_vehicle_info is not None:
			doc.provisional_vehicle_info = provisional_vehicle_info
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
			"provisional_customer_name": provisional_customer_name or None,
			"provisional_vehicle_info": provisional_vehicle_info or None,
			"notes": notes or None,
			"items_summary": items_summary,
			"status": "Scheduled",
		})
		doc.insert()

	if selected_services and repair_order and frappe.db.has_column("Repair Order Service", "schedule_entry"):
		frappe.db.set_value(
			"Repair Order Service",
			{"parent": repair_order, "name": ["in", selected_services]},
			"schedule_entry",
			doc.name,
			update_modified=False,
		)

	result: Dict[str, Any] = {
		"name": doc.name,
		"status": doc.status,
		"scheduled_date": str(doc.scheduled_date),
		"scheduled_time": str(doc.scheduled_time),
		"completion_date": str(getattr(doc, "completion_date", None) or doc.scheduled_date),
		"completion_time": str(getattr(doc, "completion_time", None) or doc.scheduled_time),
		"service_bay": doc.service_bay,
		"assigned_technician": doc.assigned_technician,
	}
	warnings = getattr(doc.flags, "scheduling_warnings", None) or []
	if warnings:
		result["warnings"] = list(warnings)
	return result


@frappe.whitelist()
def create_repair_order_from_schedule_entry(schedule_entry: str) -> Dict[str, Any]:
	"""Create a new Repair Order from a pre-intake Schedule Entry.
	
	Carries forward Schedule Entry notes into Repair Order customer_concern,
	and links the records together.
	"""
	frappe.has_permission("Schedule Entry", "write", throw=True)
	frappe.has_permission("Repair Order", "create", throw=True)
	if not schedule_entry or not isinstance(schedule_entry, str) or not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry), frappe.DoesNotExistError)

	se = frappe.get_doc("Schedule Entry", schedule_entry)
	se.check_permission("write")
	if se.repair_order:
		frappe.throw(
			_("Schedule Entry {0} is already linked to Repair Order {1}.").format(
				schedule_entry, se.repair_order
			)
		)

	company = (
		frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	if not company:
		companies = frappe.get_list("Company", pluck="name", limit=1)
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
