# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_url, getdate

from ism.scheduling.capacity import (
	INACTIVE_WORK_STATUSES,
	ShopCapacityWindow,
	load_bay_tags_map,
	parse_required_tags as _parse_required_tags,
	resolve_candidate_bays,
)
from ism.scheduling.entry_queries import (
	build_holiday_checker,
	entry_datetimes,
	fetch_blocking_entries,
	find_conflicting_bay_entries,
	intervals_overlap,
	span_datetimes,
)
from ism.scheduling.slot_search import (
	find_next_available_slot_batch,
	search_day_slots,
)
from ism.scheduling.time_utils import (
	calculate_working_span as _calculate_working_span,
	effective_end_time as _effective_end_time,
	format_timedelta,
	get_shop_settings,
	normalize_date,
	normalize_time,
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
	norm_date = normalize_date(start_date) or start_date
	norm_time = normalize_time(start_time) or start_time
	settings = get_shop_settings()
	holiday_fn = build_holiday_checker(settings.get("holiday_list"), norm_date)
	return _calculate_working_span(
		norm_date,
		norm_time,
		duration_minutes,
		is_holiday_fn=holiday_fn,
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

	service_bay = resolve_service_bay_identifier(service_bay)
	date = normalize_date(date) or date
	start_time = normalize_time(start_time) or start_time
	settings = get_shop_settings()
	holiday_fn = build_holiday_checker(settings.get("holiday_list"), add_days(getdate(date), -30))

	conflicts = find_conflicting_bay_entries(
		service_bay=service_bay,
		scheduled_date=date,
		scheduled_time=start_time,
		duration_minutes=duration_minutes,
		exclude_entry=exclude_entry,
		fields=[
			"name",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
			"completion_date",
			"completion_time",
		],
		is_holiday_fn=holiday_fn,
	)
	return not conflicts


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
	date = normalize_date(date) or date
	start_time = normalize_time(start_time) or start_time

	required_tags_set = set(_parse_required_tags(required_tags))
	candidate_bays, tags_by_bay = resolve_candidate_bays(None, required_tags_set)
	if not candidate_bays:
		return []

	settings = get_shop_settings()
	holiday_fn = build_holiday_checker(settings.get("holiday_list"), add_days(getdate(date), -30))
	span = _calculate_working_span(date, start_time, duration_minutes, is_holiday_fn=holiday_fn)
	prop_start_dt, prop_end_dt = span_datetimes(span)

	entries = fetch_blocking_entries(
		span["start_date"],
		span["completion_date"],
		service_bays=candidate_bays,
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
		ex_start_dt, ex_end_dt = entry_datetimes(entry, is_holiday_fn=holiday_fn)
		if intervals_overlap(prop_start_dt, prop_end_dt, ex_start_dt, ex_end_dt):
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
	date = normalize_date(date) or date
	start_time = normalize_time(start_time) or start_time
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


def _normalize_optional_tech(technician: Optional[str]) -> Optional[str]:
	if technician is None:
		return None
	if not isinstance(technician, str):
		frappe.throw(_("Technician must be a string."), frappe.ValidationError)
	text = technician.strip()
	if not text or text.lower() in ("none", "null", "unassigned"):
		return None
	return text


def _throw_inactive_bay_error(bay_name: str) -> None:
	frappe.throw(
		_("Service Bay '{0}' is inactive / offline and cannot be scheduled.").format(bay_name),
		frappe.ValidationError,
	)


def resolve_service_bay_identifier(
	service_bay: Optional[str],
	*,
	allow_inactive: bool = False,
) -> Optional[str]:
	"""Resolve a Service Bay by exact ID, bay_name, or unambiguous case-insensitive match."""
	if service_bay is None:
		return None
	if not isinstance(service_bay, str):
		frappe.throw(_("Service Bay must be a string."), frappe.ValidationError)
	text = service_bay.strip()
	if not text or text.lower() in ("none", "null"):
		return None

	exact_doc = frappe.db.get_value("Service Bay", text, ["name", "is_active"], as_dict=True)
	if exact_doc:
		if not allow_inactive and not cint(exact_doc.get("is_active")):
			_throw_inactive_bay_error(exact_doc["name"])
		return exact_doc["name"]

	bays = frappe.get_all("Service Bay", fields=["name", "bay_name", "is_active"], order_by="is_active desc, name asc")
	lowered = text.lower()

	# Prefer active bays on exact case-insensitive match, rejecting inactive-only matches unless allowed
	exact_matches = [
		b for b in bays
		if (b.get("name") or "").lower() == lowered or (b.get("bay_name") or "").lower() == lowered
	]
	if exact_matches:
		active_exact = [b for b in exact_matches if cint(b.get("is_active"))]
		if active_exact:
			if len(active_exact) == 1:
				return active_exact[0]["name"]
		elif len(exact_matches) == 1:
			if not allow_inactive:
				_throw_inactive_bay_error(exact_matches[0]["name"])
			return exact_matches[0]["name"]

	partial_matches = [
		b for b in bays
		if lowered in (b.get("name") or "").lower() or lowered in (b.get("bay_name") or "").lower()
	]
	active_partial = [b for b in partial_matches if cint(b.get("is_active"))]
	if not active_partial and partial_matches and not allow_inactive:
		if len(partial_matches) == 1:
			_throw_inactive_bay_error(partial_matches[0]["name"])
		frappe.throw(
			_("Service Bay '{0}' matches only inactive / offline bays and cannot be scheduled.").format(text),
			frappe.ValidationError,
		)
	candidates = active_partial or partial_matches

	if len(candidates) == 1:
		return candidates[0]["name"]
	if len(candidates) > 1:
		top_candidates = candidates[:5]
		active_names = [c["name"] for c in top_candidates if cint(c.get("is_active"))]
		tags_by_bay = load_bay_tags_map(active_names)
		formatted = []
		for c in top_candidates:
			bay_id = c["name"]
			tags = sorted(tags_by_bay.get(bay_id) or [])
			formatted.append(f"{bay_id} [{', '.join(tags)}]" if tags else bay_id)
		opts = ", ".join(formatted)
		frappe.throw(
			_("Ambiguous Service Bay '{0}'. Matching bays: {1}.").format(text, opts),
			frappe.ValidationError,
		)

	available = ", ".join(b["name"] for b in bays if cint(b.get("is_active")))
	msg = _("Service Bay '{0}' does not exist.").format(text)
	if available:
		msg += " " + _("Available bays: {0}.").format(available)
	frappe.throw(msg, frappe.DoesNotExistError)


def resolve_technician_identifier(technician: Optional[str]) -> Optional[str]:
	"""Resolve a technician by Employee ID or employee_name (exact or unambiguous partial)."""
	cleaned = _normalize_optional_tech(technician)
	if not cleaned:
		return None

	if frappe.db.exists("Employee", cleaned):
		return cleaned

	settings = get_shop_settings()
	tech_desig = settings.get("technician_designation")
	employees = frappe.get_all(
		"Employee",
		filters={"status": "Active"},
		fields=["name", "employee_name", "designation"],
		order_by="employee_name asc",
	)
	lowered = cleaned.lower()

	def _prefer_tech_desig(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
		if not tech_desig or len(rows) <= 1:
			return rows
		techs = [r for r in rows if r.get("designation") == tech_desig]
		return techs or rows

	exact = _prefer_tech_desig([e for e in employees if (e.get("employee_name") or "").lower() == lowered])
	if len(exact) == 1:
		return exact[0]["name"]
	if len(exact) > 1:
		opts = ", ".join(f"{e['employee_name']} ({e['name']})" for e in exact[:5])
		frappe.throw(_("Ambiguous Technician '{0}'. Matching employees: {1}.").format(cleaned, opts), frappe.ValidationError)

	partial = _prefer_tech_desig([e for e in employees if lowered in (e.get("employee_name") or "").lower()])
	if len(partial) == 1:
		return partial[0]["name"]
	if len(partial) > 1:
		opts = ", ".join(f"{e['employee_name']} ({e['name']})" for e in partial[:5])
		frappe.throw(_("Ambiguous Technician '{0}'. Matching employees: {1}.").format(cleaned, opts), frappe.ValidationError)

	frappe.throw(_("Technician '{0}' does not exist.").format(cleaned), frappe.DoesNotExistError)


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
	norm_date = normalize_date(date) or date
	resolved_bay = resolve_service_bay_identifier(service_bay)
	resolved_tech = resolve_technician_identifier(technician) if technician is not None else None
	if not resolved_tech and assigned_technician is not None:
		resolved_tech = resolve_technician_identifier(assigned_technician)

	return search_day_slots(
		target_date=norm_date,
		duration_minutes=duration_minutes,
		required_tags=required_tags,
		service_bay=resolved_bay,
		max_slots=max_slots,
		technician=resolved_tech,
		include_all=bool(cint(include_all)),
	)


@frappe.whitelist()
def is_holiday(date: Union[str, datetime, date]) -> Dict[str, Any]:
	"""Check if a given date is a holiday according to ISM Settings -> holiday_list."""
	_ensure_schedule_read_permission()
	settings = get_shop_settings()
	holiday_list = settings.get("holiday_list")
	if not holiday_list or not date:
		return {"is_holiday": False, "description": None}

	target_date = normalize_date(date) if date else None
	if not target_date:
		return {"is_holiday": False, "description": None}
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
	norm_from = normalize_date(from_date) if from_date else None
	resolved_bay = resolve_service_bay_identifier(service_bay)
	resolved_tech = resolve_technician_identifier(technician) if technician is not None else None
	if not resolved_tech and assigned_technician is not None:
		resolved_tech = resolve_technician_identifier(assigned_technician)

	settings = get_shop_settings()
	horizon = settings.get("scheduling_horizon_days") or 30
	max_days = min(int(horizon), 30)
	start_dt = getdate(norm_from) if norm_from else getdate()

	return find_next_available_slot_batch(
		start_dt=start_dt,
		max_days=max_days,
		duration_minutes=duration_minutes,
		required_tags=required_tags,
		service_bay=resolved_bay,
		technician=resolved_tech,
	)


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
		mins = round(hours * 60)
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


def _find_conflicting_bay_entries(
	service_bay: str,
	scheduled_date: str,
	scheduled_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	exclude_entry: Optional[str] = None,
) -> List[Dict[str, Any]]:
	"""Return blocking Schedule Entry records overlapping the proposed bay window."""
	settings = get_shop_settings()
	holiday_fn = build_holiday_checker(
		settings.get("holiday_list"),
		add_days(getdate(scheduled_date), -30),
	)
	entries = find_conflicting_bay_entries(
		service_bay=service_bay,
		scheduled_date=scheduled_date,
		scheduled_time=scheduled_time,
		duration_minutes=duration_minutes,
		exclude_entry=exclude_entry,
		is_holiday_fn=holiday_fn,
	)
	base_url = get_url()
	return [
		{
			"name": entry["name"],
			"repair_order": entry.get("repair_order"),
			"service_bay": entry.get("service_bay"),
			"assigned_technician": entry.get("assigned_technician"),
			"scheduled_date": str(entry.get("scheduled_date")),
			"scheduled_time": str(entry.get("scheduled_time"))[:5],
			"completion_date": str(entry.get("completion_date") or entry.get("scheduled_date")),
			"completion_time": str(entry.get("completion_time") or "")[:5],
			"status": entry.get("status"),
			"desk_url": f"{base_url}/app/schedule-entry/{quote(entry['name'], safe='')}",
		}
		for entry in entries
	]


def _find_nearest_alternative_slots(
	scheduled_date: str,
	scheduled_time: str,
	duration_minutes: Union[int, float],
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	max_alternatives: int = 5,
) -> List[Dict[str, Any]]:
	"""Find nearest open alternative slots on the requested date or subsequent days."""
	try:
		req_dt = getdate(scheduled_date)
	except Exception:
		req_dt = getdate()

	req_hhmm = str(scheduled_time)[:5]
	end_dt = add_days(req_dt, 7)
	window = ShopCapacityWindow(
		start_date=req_dt,
		end_date=end_dt,
		skip_holidays=True,
	)

	alternatives: List[Dict[str, Any]] = []
	for cur_dt in window.iter_dates():
		day_slots = window.slots_for_date(cur_dt, duration_minutes)
		if not day_slots:
			continue
		if cur_dt == req_dt:
			later = [s for s in day_slots if (s.get("start_time") or "") >= req_hhmm]
			earlier = [s for s in day_slots if (s.get("start_time") or "") < req_hhmm]
			ordered = later + earlier
		else:
			ordered = day_slots

		for slot in ordered:
			avail_bays = slot.get("available_bay_ids") or []
			avail_techs = slot.get("available_technician_ids") or []
			if technician and avail_techs and technician not in avail_techs:
				continue
			preferred_bay = service_bay if (service_bay and service_bay in avail_bays) else (avail_bays[0] if avail_bays else None)
			alternatives.append({
				"date": str(cur_dt),
				"start_time": slot.get("start_time"),
				"service_bay": preferred_bay,
				"available_bay_ids": avail_bays,
				"available_technician_ids": avail_techs,
				"effective_capacity": slot.get("effective_capacity"),
			})
			if len(alternatives) >= max_alternatives:
				return alternatives

	# If technician-specific search yielded nothing, retry without technician filter
	if technician and not alternatives:
		return _find_nearest_alternative_slots(
			scheduled_date=scheduled_date,
			scheduled_time=scheduled_time,
			duration_minutes=duration_minutes,
			service_bay=service_bay,
			technician=None,
			max_alternatives=max_alternatives,
		)
	return alternatives


def build_conflict_feedback(
	conflict_type: str,
	message: str,
	scheduled_date: str,
	scheduled_time: str,
	duration_minutes: Union[int, float],
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	repair_order: Optional[str] = None,
	schedule_entry_name: Optional[str] = None,
	conflicting_entries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
	"""Build structured conflict response with deep links, resource IDs, and nearest alternatives."""
	base_url = get_url()
	urls: Dict[str, str] = {
		"calendar": f"{base_url}/app/schedule-entry/view/calendar",
	}
	if repair_order:
		urls["repair_order"] = f"{base_url}/app/repair-order/{quote(repair_order, safe='')}"
	if schedule_entry_name:
		urls["schedule_entry"] = f"{base_url}/app/schedule-entry/{quote(schedule_entry_name, safe='')}"
	if service_bay:
		urls["service_bay"] = f"{base_url}/app/service-bay/{quote(service_bay, safe='')}"
	if technician:
		urls["technician"] = f"{base_url}/app/employee/{quote(technician, safe='')}"

	conflicts = conflicting_entries or []
	for item in conflicts:
		if item.get("name") and "desk_url" not in item:
			item["desk_url"] = f"{base_url}/app/schedule-entry/{quote(item['name'], safe='')}"
	if conflicts and conflicts[0].get("name"):
		urls["conflicting_entry"] = f"{base_url}/app/schedule-entry/{quote(conflicts[0]['name'], safe='')}"

	alternatives = _find_nearest_alternative_slots(
		scheduled_date=scheduled_date,
		scheduled_time=scheduled_time,
		duration_minutes=duration_minutes,
		service_bay=None if conflict_type == "bay_conflict" else service_bay,
		technician=None if conflict_type in ("technician_conflict", "technician_on_leave") else technician,
	)

	return {
		"status": "conflict",
		"conflict": True,
		"conflict_type": conflict_type,
		"message": message,
		"requested_date": str(scheduled_date),
		"requested_time": str(scheduled_time)[:5],
		"service_bay": service_bay,
		"assigned_technician": technician,
		"repair_order": repair_order,
		"resources": {
			"service_bay": service_bay,
			"technician": technician,
			"repair_order": repair_order,
		},
		"conflicting_entries": conflicts,
		"urls": urls,
		"alternative_slots": alternatives,
	}


def _get_shop_break_window_for_start(scheduled_time: str) -> Optional[tuple[timedelta, timedelta]]:
	"""Return (break_start, break_end) if scheduled_time falls inside the configured shop break."""
	settings = get_shop_settings()
	op_start = settings.get("operating_hours_start")
	op_end = settings.get("operating_hours_end")
	bs_td = settings.get("break_start")
	be_td = settings.get("break_end")
	if bs_td and be_td and op_start and op_end and op_start < bs_td < be_td < op_end:
		req_time_td = to_timedelta(scheduled_time)
		if bs_td <= req_time_td < be_td:
			return bs_td, be_td
	return None


def _validate_optional_str(value: Any, field_label: str) -> Optional[str]:
	if value is None:
		return None
	if not isinstance(value, str):
		frappe.throw(_("{0} must be a string.").format(field_label), frappe.ValidationError)
	return value


def _is_truthy_flag(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	if isinstance(value, str):
		lowered = value.strip().lower()
		if lowered in ("1", "true", "yes"):
			return True
		if lowered in ("", "0", "false", "no", "none", "null"):
			return False
	frappe.throw(_("Invalid boolean flag value."), frappe.ValidationError)


def _parse_selected_services(selected_services: Any) -> List[str]:
	if selected_services is None or selected_services == "":
		return []
	if isinstance(selected_services, str):
		stripped = selected_services.strip()
		if not stripped:
			return []
		if stripped.startswith(("[", "{")):
			try:
				parsed = frappe.parse_json(stripped)
			except Exception:
				frappe.throw(_("Invalid selected_services JSON payload."), frappe.ValidationError)
			if not isinstance(parsed, list):
				frappe.throw(_("selected_services must be a list of service IDs."), frappe.ValidationError)
			raw_items: Any = parsed
		else:
			try:
				parsed = frappe.parse_json(stripped)
			except Exception:
				return [s.strip() for s in stripped.split(",") if s.strip()]
			if isinstance(parsed, list):
				raw_items = parsed
			else:
				frappe.throw(_("selected_services must be a list of service IDs."), frappe.ValidationError)
	elif isinstance(selected_services, (list, tuple)):
		raw_items = selected_services
	else:
		frappe.throw(_("selected_services must be a list of service IDs."), frappe.ValidationError)

	result: List[str] = []
	for item in raw_items:
		if not isinstance(item, str) or not item.strip():
			frappe.throw(
				_("Each item in selected_services must be a non-empty string."),
				frappe.ValidationError,
			)
		result.append(item.strip())
	return result


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
	clear_bay: Union[int, bool] = False,
) -> Dict[str, Any]:
	"""Create or update a Schedule Entry and link allocated Repair Order services."""
	clean_entry_name = _validate_optional_str(schedule_entry_name, "Schedule Entry identifier")
	clean_entry_name = clean_entry_name.strip() if clean_entry_name is not None else None
	is_update = bool(clean_entry_name)
	doc = None
	if is_update:
		frappe.has_permission("Schedule Entry", "write", throw=True)
		if not frappe.db.exists("Schedule Entry", clean_entry_name):
			frappe.throw(
				_("Schedule Entry {0} does not exist.").format(clean_entry_name),
				frappe.DoesNotExistError,
			)
		doc = frappe.get_doc("Schedule Entry", clean_entry_name)
		doc.check_permission("write")
	else:
		frappe.has_permission("Schedule Entry", "create", throw=True)

	clean_ro = _validate_optional_str(repair_order, "Repair Order identifier")
	repair_order = (clean_ro.strip() or None) if clean_ro is not None else None
	if not isinstance(entry_type, str) or not entry_type.strip():
		frappe.throw(_("Entry Type must be a non-empty string."), frappe.ValidationError)
	if not isinstance(scheduled_date, str):
		frappe.throw(_("Scheduled Date must be a string."), frappe.ValidationError)
	if not isinstance(scheduled_time, str):
		frappe.throw(_("Scheduled Time must be a string."), frappe.ValidationError)
	notes = _validate_optional_str(notes, "Notes")
	provisional_customer_name = _validate_optional_str(
		provisional_customer_name,
		"Provisional Customer Name",
	)
	provisional_vehicle_info = _validate_optional_str(
		provisional_vehicle_info,
		"Provisional Vehicle Info",
	)
	if service_bay is not None and not isinstance(service_bay, str):
		frappe.throw(_("Service Bay must be a string."), frappe.ValidationError)

	if isinstance(estimated_duration, bool):
		frappe.throw(_("Estimated Duration must be a number."), frappe.ValidationError)
	try:
		duration_int = int(float(estimated_duration))
	except (TypeError, ValueError, OverflowError):
		frappe.throw(_("Estimated Duration must be a number."), frappe.ValidationError)

	parsed_services = _parse_selected_services(selected_services)
	allow_overlap = _is_truthy_flag(allow_technician_overlap)
	clear_bay_flag = _is_truthy_flag(clear_bay)

	if repair_order:
		if not frappe.db.exists("Repair Order", repair_order):
			frappe.throw(_("Repair Order {0} does not exist.").format(repair_order), frappe.DoesNotExistError)
		frappe.has_permission("Repair Order", "read", doc=repair_order, throw=True)
		if parsed_services:
			frappe.has_permission("Repair Order", "write", doc=repair_order, throw=True)

	scheduled_date = normalize_date(scheduled_date) or scheduled_date
	scheduled_time = normalize_time(scheduled_time) or scheduled_time

	explicit_clear_bay = clear_bay_flag or (service_bay is not None and service_bay.strip() == "")
	resolved_bay: Optional[str] = None
	if not explicit_clear_bay and service_bay is not None:
		frappe.has_permission("Service Bay", "read", throw=True)
		resolved_bay = resolve_service_bay_identifier(service_bay)
		if resolved_bay is None:
			explicit_clear_bay = True

	effective_bay = (
		None
		if explicit_clear_bay
		else (resolved_bay if resolved_bay is not None else (doc.service_bay if is_update else None))
	)
	if effective_bay and is_update and resolved_bay is None:
		bay_active = frappe.db.get_value("Service Bay", effective_bay, "is_active")
		if bay_active is not None and not cint(bay_active):
			_throw_inactive_bay_error(effective_bay)

	target_tech = resolve_technician_identifier(assigned_technician)

	tech_check = None
	if target_tech and scheduled_date and scheduled_time and not allow_overlap:
		try:
			from ism.api.technician_availability import check_technician_availability

			tech_check = check_technician_availability(
				employee=target_tech,
				date=scheduled_date,
				start_time=scheduled_time,
				duration_minutes=duration_int,
				exclude_entry=clean_entry_name if is_update else None,
			)
			if tech_check.get("on_leave"):
				return build_conflict_feedback(
					conflict_type="technician_on_leave",
					message=_("Technician '{0}' is on approved leave during this work window.").format(target_tech),
					scheduled_date=scheduled_date,
					scheduled_time=scheduled_time,
					duration_minutes=duration_int,
					service_bay=effective_bay,
					technician=target_tech,
					repair_order=repair_order,
					schedule_entry_name=clean_entry_name,
				)
		except ImportError:
			tech_check = None

	if scheduled_date and scheduled_time:
		break_window = _get_shop_break_window_for_start(scheduled_time)
		if break_window:
			bs_td, be_td = break_window
			return build_conflict_feedback(
				conflict_type="shop_break",
				message=_(
					"Requested start time {0} falls within the shop lunch break ({1}–{2}). "
					"Please choose an available alternative slot."
				).format(str(scheduled_time)[:5], format_timedelta(bs_td), format_timedelta(be_td)),
				scheduled_date=scheduled_date,
				scheduled_time=scheduled_time,
				duration_minutes=duration_int,
				service_bay=effective_bay,
				technician=target_tech,
				repair_order=repair_order,
				schedule_entry_name=clean_entry_name,
			)

	if tech_check and tech_check.get("conflicts"):
		conflict_names = ", ".join(c.get("name") for c in tech_check["conflicts"] if c.get("name"))
		return build_conflict_feedback(
			conflict_type="technician_conflict",
			message=_(
				"Technician '{0}' already has an overlapping allocation ({1}) during this work window. "
				"Confirm double-booking to proceed."
			).format(target_tech, conflict_names),
			scheduled_date=scheduled_date,
			scheduled_time=scheduled_time,
			duration_minutes=duration_int,
			service_bay=effective_bay,
			technician=target_tech,
			repair_order=repair_order,
			schedule_entry_name=clean_entry_name,
			conflicting_entries=list(tech_check["conflicts"]),
		)

	if effective_bay and scheduled_date and scheduled_time:
		bay_conflicts = _find_conflicting_bay_entries(
			service_bay=effective_bay,
			scheduled_date=scheduled_date,
			scheduled_time=scheduled_time,
			duration_minutes=duration_int,
			exclude_entry=clean_entry_name if is_update else None,
		)
		if bay_conflicts:
			c0 = bay_conflicts[0]
			return build_conflict_feedback(
				conflict_type="bay_conflict",
				message=_("Service Bay '{0}' is already occupied by {1} during {2} {3}.").format(
					effective_bay, c0["name"], scheduled_date, str(scheduled_time)[:5]
				),
				scheduled_date=scheduled_date,
				scheduled_time=scheduled_time,
				duration_minutes=duration_int,
				service_bay=effective_bay,
				technician=target_tech,
				repair_order=repair_order,
				schedule_entry_name=clean_entry_name,
				conflicting_entries=bay_conflicts,
			)

	items_summary = _format_services_summary(parsed_services, repair_order)

	try:
		if is_update:
			if repair_order:
				doc.repair_order = repair_order
			doc.entry_type = entry_type
			doc.scheduled_date = scheduled_date
			doc.scheduled_time = scheduled_time
			doc.estimated_duration = duration_int
			if explicit_clear_bay:
				doc.service_bay = None
			elif resolved_bay is not None:
				doc.service_bay = resolved_bay
			doc.assigned_technician = target_tech
			if notes is not None:
				doc.notes = notes or None
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
				"estimated_duration": duration_int,
				"service_bay": effective_bay,
				"assigned_technician": target_tech,
				"provisional_customer_name": provisional_customer_name or None,
				"provisional_vehicle_info": provisional_vehicle_info or None,
				"notes": notes or None,
				"items_summary": items_summary,
				"status": "Scheduled",
			})
			doc.insert()
	except frappe.ValidationError as exc:
		err_msg = str(exc)
		lowered_err = err_msg.lower()
		if any(tok in lowered_err for tok in ("occupied", "capacity", "exhausted", "overlap", "leave", "available")):
			c_type = "bay_conflict" if "bay" in lowered_err else "technician_pool_exhausted"
			return build_conflict_feedback(
				conflict_type=c_type,
				message=err_msg,
				scheduled_date=scheduled_date,
				scheduled_time=scheduled_time,
				duration_minutes=duration_int,
				service_bay=effective_bay,
				technician=target_tech,
				repair_order=repair_order,
				schedule_entry_name=clean_entry_name,
			)
		raise

	if parsed_services and repair_order and frappe.db.has_column("Repair Order Service", "schedule_entry"):
		frappe.db.set_value(
			"Repair Order Service",
			{"parent": repair_order, "name": ["in", parsed_services]},
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


def _resolve_provisional_customer_for_entry(provisional_name: Optional[str]) -> Optional[str]:
	"""Resolve provisional customer name against existing Customer records (exact or case-insensitive)."""
	cleaned = (provisional_name or "").strip()
	if not cleaned:
		return None
	frappe.has_permission("Customer", "read", throw=True)
	exact = (
		frappe.db.get_value("Customer", cleaned, "name")
		or frappe.db.get_value("Customer", {"customer_name": cleaned}, "name")
	)
	if exact:
		frappe.has_permission("Customer", "read", doc=exact, throw=True)
		return exact

	lowered = cleaned.lower()
	candidates = frappe.get_list("Customer", fields=["name", "customer_name"], limit=0)
	matches = [
		row["name"]
		for row in candidates
		if (row.get("name") or "").strip().lower() == lowered
		or (row.get("customer_name") or "").strip().lower() == lowered
	]
	if len(matches) == 1:
		frappe.has_permission("Customer", "read", doc=matches[0], throw=True)
		return matches[0]
	if len(matches) > 1:
		frappe.throw(
			_("Multiple Customers match '{0}': {1}.").format(cleaned, ", ".join(matches[:5])),
			frappe.ValidationError,
		)
	return None


def _resolve_provisional_vehicle_for_entry(customer: str, provisional_info: Optional[str]) -> Optional[str]:
	"""Resolve provisional vehicle info against Repair Vehicles owned by or linked to customer."""
	from ism.ism.doctype.repair_vehicle.vehicle_queries import build_vehicle_search_results

	vehicles = build_vehicle_search_results(customer=customer, limit=50)
	if not vehicles:
		return None

	cleaned = (provisional_info or "").strip()
	if not cleaned:
		return vehicles[0]["name"] if len(vehicles) == 1 else None

	lowered = cleaned.lower()
	for v in vehicles:
		for key in ("name", "vin", "license_plate"):
			val = str(v.get(key) or "").strip().lower()
			if val and val == lowered:
				frappe.has_permission("Repair Vehicle", "read", doc=v["name"], throw=True)
				return v["name"]

	info_tokens = [tok for tok in re.split(r"[^a-z0-9]+", lowered) if tok]
	scored: list[tuple[int, str]] = []
	for v in vehicles:
		haystack_parts = [
			str(v.get("model_year") or ""),
			str(v.get("manufacturer") or ""),
			str(v.get("model") or ""),
			str(v.get("trim") or ""),
			str(v.get("vin") or ""),
			str(v.get("license_plate") or ""),
			str(v.get("name") or ""),
		]
		haystack_tokens = set(re.split(r"[^a-z0-9]+", " ".join(haystack_parts).lower()))
		score = sum(1 for tok in info_tokens if tok in haystack_tokens)
		model_val = str(v.get("model") or "").strip().lower()
		if model_val and model_val in lowered:
			score += 2
		if score > 0:
			scored.append((score, v["name"]))

	if scored:
		max_score = max(s for s, _ in scored)
		top_matches = [name for s, name in scored if s == max_score]
		if len(top_matches) == 1:
			frappe.has_permission("Repair Vehicle", "read", doc=top_matches[0], throw=True)
			return top_matches[0]
		frappe.throw(
			_("Ambiguous Repair Vehicle for Customer '{0}' matching '{1}': {2}.").format(
				customer, cleaned, ", ".join(top_matches[:5])
			),
			frappe.ValidationError,
		)

	if len(vehicles) == 1:
		frappe.has_permission("Repair Vehicle", "read", doc=vehicles[0]["name"], throw=True)
		return vehicles[0]["name"]
	return None


@frappe.whitelist()
def create_repair_order_from_schedule_entry(
	schedule_entry: str,
	odometer_in: Optional[int] = None,
) -> Dict[str, Any]:
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

	if odometer_in is None or (isinstance(odometer_in, str) and not odometer_in.strip()):
		frappe.throw(_("Odometer In (odometer_in) is required to create a Repair Order."), frappe.MandatoryError)
	if isinstance(odometer_in, bool):
		frappe.throw(_("Odometer In must be a valid non-negative integer."), frappe.ValidationError)
	try:
		parsed_odo = int(odometer_in) if not isinstance(odometer_in, float) else (int(odometer_in) if odometer_in.is_integer() else (_ for _ in ()).throw(ValueError()))
	except (TypeError, ValueError):
		frappe.throw(_("Odometer In must be a valid non-negative integer."), frappe.ValidationError)
	if parsed_odo < 0:
		frappe.throw(_("Odometer In cannot be negative."), frappe.ValidationError)

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
		"odometer_in": parsed_odo,
	}
	resolved_updates: Dict[str, Any] = {}
	if not se.customer and getattr(se, "provisional_customer_name", None):
		resolved_customer = _resolve_provisional_customer_for_entry(se.provisional_customer_name)
		if resolved_customer:
			se.customer = resolved_customer
			resolved_updates["customer"] = resolved_customer
	if not se.customer:
		if getattr(se, "provisional_customer_name", None):
			frappe.throw(
				_(
					"Schedule Entry must have a Customer to create a Repair Order. "
					"No existing Customer matched provisional name '{0}'."
				).format(se.provisional_customer_name),
				frappe.ValidationError,
			)
		frappe.throw(_("Schedule Entry must have a Customer to create a Repair Order."), frappe.ValidationError)

	if not se.repair_vehicle:
		resolved_vehicle = _resolve_provisional_vehicle_for_entry(
			se.customer, getattr(se, "provisional_vehicle_info", None)
		)
		if resolved_vehicle:
			se.repair_vehicle = resolved_vehicle
			resolved_updates["repair_vehicle"] = resolved_vehicle
	if not se.repair_vehicle:
		if getattr(se, "provisional_vehicle_info", None):
			frappe.throw(
				_(
					"Schedule Entry must have a Vehicle to create a Repair Order. "
					"No Repair Vehicle for Customer '{0}' matched '{1}'."
				).format(se.customer, se.provisional_vehicle_info),
				frappe.ValidationError,
			)
		frappe.throw(_("Schedule Entry must have a Vehicle to create a Repair Order."), frappe.ValidationError)

	if resolved_updates:
		se.db_set(resolved_updates)

	ro_data["customer"] = se.customer
	ro_data["vehicle"] = se.repair_vehicle

	if hasattr(frappe.get_meta("Repair Order"), "has_field") and frappe.get_meta("Repair Order").has_field("origin_schedule_entry"):
		ro_data["origin_schedule_entry"] = se.name

	ro = frappe.get_doc(ro_data)
	ro.insert()

	se.db_set("repair_order", ro.name)
	if hasattr(ro, "sync_schedule_entries"):
		ro.sync_schedule_entries()

	return {
		"repair_order": ro.name,
		"schedule_entry": se.name,
		"odometer_in": ro.odometer_in,
		"customer_concern": ro.customer_concern,
	}
