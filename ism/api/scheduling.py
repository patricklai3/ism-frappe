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
	_ensure_schedule_read_permission(include_bays=True)
	if not service_bay or not isinstance(service_bay, str) or not date:
		return False
	if exclude_entry is not None and not isinstance(exclude_entry, str):
		frappe.throw(_("Invalid exclude_entry identifier."), frappe.ValidationError)

	settings = get_shop_settings()
	bs_td = settings["break_start"]
	be_td = settings["break_end"]

	prop_start = to_timedelta(start_time)
	prop_end = _effective_end_time(prop_start, duration_minutes, bs_td, be_td)

	filters = [
		["service_bay", "=", service_bay],
		["scheduled_date", "=", str(date)],
		["status", "not in", list(NON_BLOCKING_ENTRY_STATUSES)],
	]
	if exclude_entry:
		filters.append(["name", "!=", exclude_entry])

	# Shop-wide overlap check intentionally scans all non-blocking entries after read perm verification.
	existing_entries = frappe.get_all(
		"Schedule Entry",
		filters=filters,
		fields=["name", "scheduled_time", "estimated_duration"],
	)

	for entry in existing_entries:
		ex_start = to_timedelta(entry.get("scheduled_time"))
		ex_duration = entry.get("estimated_duration") or 0
		ex_end = _effective_end_time(ex_start, ex_duration, bs_td, be_td)

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
	_ensure_schedule_read_permission(include_bays=True)
	if not date:
		return []

	required_tags_set = set(_parse_required_tags(required_tags))
	candidate_bays, tags_by_bay = resolve_candidate_bays(None, required_tags_set)
	if not candidate_bays:
		return []

	settings = get_shop_settings()
	bs_td = settings["break_start"]
	be_td = settings["break_end"]
	prop_start = to_timedelta(start_time)
	prop_end = _effective_end_time(prop_start, duration_minutes, bs_td, be_td)

	entries = frappe.get_all(
		"Schedule Entry",
		filters=[
			["service_bay", "in", candidate_bays],
			["scheduled_date", "=", str(date)],
			["status", "not in", list(NON_BLOCKING_ENTRY_STATUSES)],
		],
		fields=["service_bay", "scheduled_time", "estimated_duration"],
	)
	occupied_bays = set()
	for entry in entries:
		bay = entry.get("service_bay")
		if not bay or bay in occupied_bays:
			continue
		ex_start = to_timedelta(entry.get("scheduled_time"))
		ex_end = _effective_end_time(ex_start, entry.get("estimated_duration") or 0, bs_td, be_td)
		if max(prop_start, ex_start) < min(prop_end, ex_end):
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


@frappe.whitelist()
def get_available_slots(
	date: str,
	duration_minutes: Union[int, float],
	required_tags: Optional[Union[str, List[str]]] = None,
	service_bay: Optional[str] = None,
	max_slots: Optional[int] = None,
) -> List[Dict[str, Any]]:
	"""Return available slot dicts for date and duration across operating hours."""
	_ensure_schedule_read_permission(include_bays=True)
	window = ShopCapacityWindow(
		start_date=date,
		end_date=date,
		required_tags=required_tags,
		service_bay=service_bay,
		skip_holidays=False,
	)
	return window.slots_for_date(date, duration_minutes, max_slots=max_slots)


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
) -> Optional[Dict[str, Any]]:
	"""Search forward from from_date up to scheduling_horizon_days for the earliest date with available slots."""
	_ensure_schedule_read_permission(include_bays=True)
	settings = get_shop_settings()
	horizon = settings.get("scheduling_horizon_days") or 30
	max_days = min(int(horizon), 30)

	start_dt = getdate(from_date) if from_date else getdate()
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
) -> Dict[str, Any]:
	"""Create or update a Schedule Entry and link allocated Repair Order services."""
	if schedule_entry_name is not None and not isinstance(schedule_entry_name, str):
		frappe.throw(_("Invalid Schedule Entry identifier."), frappe.ValidationError)
	if repair_order is not None and not isinstance(repair_order, str):
		frappe.throw(_("Invalid Repair Order identifier."), frappe.ValidationError)

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
