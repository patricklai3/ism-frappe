# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime

from ism.scheduling.arrival_policy import (
	format_customer_appointment_display,
	to_customer_arrival_datetime,
	to_internal_slot_start,
	to_public_slot_projection,
)
from ism.scheduling.bookable_catalog import (
	compute_packages_booking_requirements,
	normalize_vehicle_model,
)
from ism.scheduling.capacity import (
	NON_BLOCKING_ENTRY_STATUSES,
	ShopCapacityWindow,
	load_bay_tags_map,
	parse_required_tags,
)
from ism.scheduling.entry_queries import (
	build_holiday_checker,
	entry_datetimes,
	fetch_leaves_by_employee,
	intervals_overlap,
	is_employee_on_leave_for_date,
	span_datetimes,
)
from ism.utils.vehicle_display import (
	build_vehicle_display_dict,
	format_vehicle_display,
	normalize_license_plate,
	normalize_vehicle_color,
)
from ism.scheduling.time_utils import (
	calculate_working_span,
	effective_end_time,
	format_timedelta,
	get_shop_settings,
	normalize_date,
	normalize_time,
	to_timedelta,
)
from ism.utils.demo_mode import is_demo_mode

ALLOWED_RESCHEDULE_ENTRY_STATUSES: Tuple[str, ...] = ("Scheduled", "Confirmed")
ALLOWED_RESCHEDULE_RO_STATUSES: Tuple[str, ...] = ("Draft Estimate", "Pending Authorization")
LOCKED_RO_STATUSES: FrozenSet[str] = frozenset({
	"Checked In",
	"Authorized - In Progress",
	"Waiting on Parts",
	"Work Completed",
	"Billed / Paid",
	"Closed / Released",
	"Cancelled",
})
VALID_BOOKING_SOURCES: Tuple[str, ...] = (
	"Staff / Desk",
	"Website Self-Serve",
	"Customer Portal",
	"Shop OS Intake",
	"Agent / API",
)
VALID_WAITER_PREFERENCES: Tuple[str, ...] = ("Drop-Off", "Waiter", "Not Specified")

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NON_DIGIT_PATTERN = re.compile(r"\D+")
_MAX_SELF_SERVE_SEARCH_DAYS = 60


def _throw_self_serve_error(
	message: str,
	reason_code: str,
	exc_cls=frappe.ValidationError,
) -> None:
	try:
		frappe.throw(message, exc_cls)
	except exc_cls as exc:
		exc.reason_code = reason_code
		raise


def _resolve_now(now_dt: Optional[Union[datetime, str]]) -> datetime:
	if now_dt is None:
		dt = now_datetime()
	elif isinstance(now_dt, datetime):
		dt = now_dt
	else:
		dt = get_datetime(now_dt)
	if dt.tzinfo is not None:
		return dt.replace(tzinfo=None)
	return dt


def _normalize_phone_fallback(phone: str) -> str:
	if not isinstance(phone, str) or not phone.strip():
		frappe.throw(_("A valid phone number is required."), frappe.ValidationError)

	raw = phone.strip()
	if re.search(r"[A-Za-z]", raw):
		frappe.throw(_("Please provide a valid phone number."), frappe.ValidationError)

	has_plus = raw.startswith("+")
	digits = _NON_DIGIT_PATTERN.sub("", raw)
	if has_plus:
		if not (8 <= len(digits) <= 15) or digits.startswith("0"):
			frappe.throw(_("Please provide a valid E.164 phone number."), frappe.ValidationError)
		return f"+{digits}"

	if len(digits) == 10:
		return f"+1{digits}"
	if len(digits) == 11 and digits.startswith("1"):
		return f"+{digits}"

	frappe.throw(_("Please provide a valid 10-digit or E.164 phone number."), frappe.ValidationError)


def _normalize_phone(phone: str) -> str:
	if not isinstance(phone, str) or not phone.strip():
		frappe.throw(_("A valid phone number is required."), frappe.ValidationError)
	try:
		from ism.comms.contracts import normalize_phone_e164
	except ImportError:
		return _normalize_phone_fallback(phone)

	try:
		normalized = normalize_phone_e164(phone)
	except ValueError as exc:
		frappe.throw(_("Please provide a valid phone number: {0}").format(str(exc)), frappe.ValidationError)
	if not normalized:
		frappe.throw(_("Please provide a valid phone number."), frappe.ValidationError)
	return normalized


def _compose_provisional_vehicle_info(
	vehicle_year: Optional[int],
	vehicle_model: Optional[str],
	vin_or_plate: Optional[str] = None,
	*,
	vehicle_color: Optional[str] = None,
	license_plate: Optional[str] = None,
	vin: Optional[str] = None,
) -> Optional[str]:
	resolved_plate = license_plate
	resolved_vin = vin
	if not resolved_plate and vin_or_plate and len(vin_or_plate.strip()) != 17:
		resolved_plate = vin_or_plate
	if not resolved_vin and vin_or_plate and len(vin_or_plate.strip()) == 17:
		resolved_vin = vin_or_plate
	formatted = format_vehicle_display(
		{
			"model_year": vehicle_year,
			"model": vehicle_model,
			"color": vehicle_color,
			"license_plate": resolved_plate,
			"vin": resolved_vin,
		},
		fallback="",
	)
	return formatted or None


def _get_entry_field(entry: Any, fieldname: str, default: Any = None) -> Any:
	if isinstance(entry, dict):
		return entry.get(fieldname, default)
	return getattr(entry, fieldname, default)


def get_required_tags_for_entry(schedule_entry: Any) -> List[str]:
	"""Derive required equipment tags from Schedule Entry booked_packages_json snapshot."""
	raw_json = _get_entry_field(schedule_entry, "booked_packages_json")
	if not raw_json:
		return []
	try:
		parsed = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
	except (TypeError, ValueError):
		return []
	if not isinstance(parsed, list):
		return []

	pkg_ids = [
		str(item.get("package_id")).strip()
		for item in parsed
		if isinstance(item, dict) and item.get("package_id")
	]
	if not pkg_ids:
		return []

	rows = frappe.get_all(
		"Service Package",
		filters={"name": ["in", list(dict.fromkeys(pkg_ids))]},
		fields=["self_serve_required_tags"],
		ignore_permissions=True,
	)
	tags = set()
	for row in rows:
		for tag in parse_required_tags(row.get("self_serve_required_tags")):
			tags.add(tag)
	return sorted(tags)


def get_self_serve_policy() -> Dict[str, Any]:
	"""Return cached self-serve policy snapshot from get_shop_settings()."""
	settings = get_shop_settings()
	return {
		"customer_arrival_offset_minutes": int(settings.get("customer_arrival_offset_minutes", 15)),
		"reschedule_cutoff_hours": int(settings.get("reschedule_cutoff_hours", 24)),
		"max_self_reschedules": int(settings.get("max_self_reschedules", 2)),
		"self_serve_default_entry_type": settings.get("self_serve_default_entry_type") or "Diagnostic",
		"self_serve_min_lead_time_minutes": int(settings.get("self_serve_min_lead_time_minutes", 120)),
		"self_serve_horizon_days": int(settings.get("self_serve_horizon_days", 30)),
		"enable_waiter_preference": 1 if cint(settings.get("enable_waiter_preference", 1)) else 0,
		"operating_hours_start": format_timedelta(settings["operating_hours_start"]),
		"operating_hours_end": format_timedelta(settings["operating_hours_end"]),
		"default_slot_interval": int(settings.get("default_slot_interval") or 30),
		"default_diagnostic_duration": int(settings.get("default_diagnostic_duration") or 60),
	}


def evaluate_reschedule_eligibility(
	schedule_entry: Union[str, Any],
	*,
	now_dt: Optional[datetime] = None,
	use_current_read: bool = False,
) -> Dict[str, Any]:
	"""Evaluate hard lifecycle locks, reschedule count cap, and cutoff window (measured against T_cust)."""
	if isinstance(schedule_entry, str):
		name = schedule_entry.strip()
		if not name or not frappe.db.exists("Schedule Entry", name):
			frappe.throw(_("Appointment '{0}' was not found.").format(schedule_entry), frappe.DoesNotExistError)
		se = frappe.get_doc("Schedule Entry", name)
	elif schedule_entry is not None:
		se = schedule_entry
	else:
		frappe.throw(_("Schedule Entry is required."), frappe.ValidationError)

	now_val = _resolve_now(now_dt)
	policy = get_self_serve_policy()
	offset_minutes = int(policy["customer_arrival_offset_minutes"])
	cutoff_hours = int(policy["reschedule_cutoff_hours"])
	max_self_reschedules = int(policy["max_self_reschedules"])

	scheduled_date = _get_entry_field(se, "scheduled_date")
	scheduled_time = _get_entry_field(se, "scheduled_time")
	current_appointment = format_customer_appointment_display(
		scheduled_date,
		scheduled_time,
		offset_minutes=offset_minutes,
	)
	arrival_dt = to_customer_arrival_datetime(
		scheduled_date,
		scheduled_time,
		offset_minutes=offset_minutes,
	)
	cutoff_deadline_dt = arrival_dt - timedelta(hours=cutoff_hours)
	cutoff_deadline = cutoff_deadline_dt.isoformat()

	reschedule_count = max(0, cint(_get_entry_field(se, "reschedule_count", 0) or 0))
	remaining_reschedules = max(0, max_self_reschedules - reschedule_count)

	status = _get_entry_field(se, "status")
	repair_order = _get_entry_field(se, "repair_order")

	allowed = True
	reason_code: Optional[str] = None
	reason_message: Optional[str] = None

	if status not in ALLOWED_RESCHEDULE_ENTRY_STATUSES:
		allowed = False
		reason_code = "locked_schedule_status"
		reason_message = _(
			"This appointment cannot be rescheduled because its current status is '{0}'. "
			"Please contact the shop for assistance."
		).format(status or "Unknown")
	elif repair_order:
		ro_meta = None
		if use_current_read:
			ro_rows = frappe.db.sql(
				"""
				SELECT `status`, `picked_up_at`, `vehicle_released`
				FROM `tabRepair Order`
				WHERE `name` = %s
				LOCK IN SHARE MODE
				""",
				(repair_order,),
				as_dict=True,
			)
			ro_meta = ro_rows[0] if ro_rows else None
		elif frappe.db.exists("Repair Order", repair_order):
			ro_meta = frappe.db.get_value(
				"Repair Order",
				repair_order,
				["status", "picked_up_at", "vehicle_released"],
				as_dict=True,
			)
		if ro_meta:
			ro_status = ro_meta.get("status")
			ro_locked = (
				ro_status in LOCKED_RO_STATUSES
				or ro_status not in ALLOWED_RESCHEDULE_RO_STATUSES
				or bool(ro_meta.get("picked_up_at"))
				or cint(ro_meta.get("vehicle_released") or 0) == 1
			)
			if ro_locked:
				allowed = False
				reason_code = "locked_repair_order_status"
				reason_message = _(
					"This appointment is already in shop workflow and cannot be rescheduled online. "
					"Please contact the shop for assistance."
				)

	if allowed and max_self_reschedules <= 0:
		allowed = False
		reason_code = "self_reschedule_disabled"
		reason_message = _(
			"Online rescheduling is currently unavailable. Please contact the shop to change your appointment."
		)

	if allowed and reschedule_count >= max_self_reschedules:
		allowed = False
		reason_code = "max_reschedules_reached"
		reason_message = _(
			"This appointment has reached the limit of {0} online reschedules. "
			"Please contact the shop for further changes."
		).format(max_self_reschedules)

	if allowed and (now_val >= arrival_dt or (cutoff_hours > 0 and now_val > cutoff_deadline_dt)):
		allowed = False
		reason_code = "cutoff_window_passed"
		if cutoff_hours > 0:
			reason_message = _(
				"Appointments must be rescheduled at least {0} hours before the scheduled appointment time. "
				"Please contact the shop for assistance."
			).format(cutoff_hours)
		else:
			reason_message = _(
				"This appointment time has already passed. Please contact the shop for assistance."
			)

	return {
		"allowed": allowed,
		"reason_code": reason_code,
		"reason_message": reason_message,
		"reschedule_count": reschedule_count,
		"max_self_reschedules": max_self_reschedules,
		"remaining_reschedules": remaining_reschedules,
		"cutoff_hours": cutoff_hours,
		"cutoff_deadline": cutoff_deadline,
		"current_appointment": current_appointment,
	}


def find_self_serve_slots(
	*,
	package_ids: Optional[Sequence[str]] = None,
	duration_minutes: Optional[int] = None,
	required_tags: Optional[Sequence[str]] = None,
	start_date: Optional[str] = None,
	days_ahead: int = 7,
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[int] = None,
	vehicle_generation: Optional[str] = None,
	exclude_entry: Optional[str] = None,
	max_slots_per_day: Optional[int] = None,
	now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
	"""Search live bay + technician capacity and return strictly sanitized customer-facing slots."""
	now_val = _resolve_now(now_dt)
	today_date = now_val.date()
	settings = get_shop_settings()
	offset_minutes = int(settings.get("customer_arrival_offset_minutes", 15))
	min_lead_minutes = max(0, int(settings.get("self_serve_min_lead_time_minutes", 120)))
	horizon_days = max(1, int(settings.get("self_serve_horizon_days", 30)))
	default_diag_dur = int(settings.get("default_diagnostic_duration") or 60)

	if package_ids is not None:
		reqs = compute_packages_booking_requirements(
			package_ids,
			vehicle_model=vehicle_model,
			vehicle_year=vehicle_year,
			vehicle_generation=vehicle_generation,
		)
		resolved_duration = int(reqs["duration_minutes"])
		resolved_tags = list(reqs["required_tags"])
	else:
		resolved_duration = int(duration_minutes) if duration_minutes and int(duration_minutes) > 0 else default_diag_dur
		resolved_tags = parse_required_tags(list(required_tags) if required_tags else None)

	if start_date is not None and str(start_date).strip() != "":
		norm_start = normalize_date(start_date)
		if not norm_start:
			frappe.throw(_("Invalid start_date."), frappe.ValidationError)
		req_start_dt = date.fromisoformat(norm_start)
	else:
		req_start_dt = today_date

	max_date = today_date + timedelta(days=horizon_days)
	clamped_days = max(1, min(cint(days_ahead or 7), horizon_days, _MAX_SELF_SERVE_SEARCH_DAYS))
	effective_start_dt = max(req_start_dt, today_date)
	effective_end_dt = min(effective_start_dt + timedelta(days=clamped_days - 1), max_date)

	if effective_start_dt > effective_end_dt:
		return {
			"start_date": str(effective_start_dt),
			"end_date": str(max_date),
			"horizon_days": horizon_days,
			"days": [],
			"total_slots": 0,
		}

	slot_limit = cint(max_slots_per_day) if max_slots_per_day else 0
	earliest_arrival_dt = now_val + timedelta(minutes=min_lead_minutes)

	window = ShopCapacityWindow(
		start_date=effective_start_dt,
		end_date=effective_end_dt,
		required_tags=resolved_tags,
		skip_holidays=True,
		exclude_entry=exclude_entry,
	)

	days_out: List[Dict[str, Any]] = []
	total_slots = 0

	for curr_dt in window.iter_dates():
		raw_slots = window.slots_for_date(curr_dt, duration_minutes=resolved_duration)
		day_slots: List[Dict[str, Any]] = []
		for slot in raw_slots:
			arrival_dt = to_customer_arrival_datetime(
				curr_dt,
				slot["start_time"],
				offset_minutes=offset_minutes,
			)
			if arrival_dt < earliest_arrival_dt:
				continue
			day_slots.append(
				to_public_slot_projection(
					curr_dt,
					slot,
					offset_minutes=offset_minutes,
				)
			)
			if slot_limit > 0 and len(day_slots) >= slot_limit:
				break
		if day_slots:
			days_out.append({"date": str(curr_dt), "slots": day_slots})
			total_slots += len(day_slots)

	if (
		total_slots == 0
		and is_demo_mode()
		and (not window.candidate_bays or (window.enable_tech and not window.active_techs))
	):
		op_start: timedelta = settings["operating_hours_start"]
		op_end: timedelta = settings["operating_hours_end"]
		bs_td: Optional[timedelta] = settings["break_start"]
		be_td: Optional[timedelta] = settings["break_end"]
		interval_mins: int = max(15, int(settings.get("default_slot_interval") or 30))
		if op_start < op_end:
			is_holiday = build_holiday_checker(settings)
			step = timedelta(minutes=interval_mins)
			for curr_dt in window.iter_dates():
				if is_holiday(curr_dt):
					continue
				day_slots = []
				curr_time = op_start
				while curr_time < op_end:
					if bs_td and be_td and bs_td <= curr_time < be_td:
						curr_time += step
						continue
					eff_end = effective_end_time(curr_time, resolved_duration, bs_td, be_td)
					if eff_end > op_end:
						curr_time += step
						continue
					start_str = format_timedelta(curr_time)
					arrival_dt = to_customer_arrival_datetime(
						curr_dt,
						start_str,
						offset_minutes=offset_minutes,
					)
					if arrival_dt >= earliest_arrival_dt:
						day_slots.append(
							to_public_slot_projection(
								curr_dt,
								{"start_time": start_str, "effective_capacity": 1},
								offset_minutes=offset_minutes,
							)
						)
						if slot_limit > 0 and len(day_slots) >= slot_limit:
							break
					curr_time += step
				if day_slots:
					days_out.append({"date": str(curr_dt), "slots": day_slots})
					total_slots += len(day_slots)

	return {
		"start_date": str(effective_start_dt),
		"end_date": str(effective_end_dt),
		"horizon_days": horizon_days,
		"days": days_out,
		"total_slots": total_slots,
	}


def _validate_target_slot(
	target_date: Union[str, date, datetime],
	customer_arrival_time: str,
	duration_minutes: int,
	*,
	settings: Dict[str, Any],
	now_val: datetime,
	require_same_day: bool = True,
) -> Tuple[str, str, Callable[[Union[str, date, datetime]], bool]]:
	"""Validate customer-supplied target_date + customer_arrival_time and return (iso_date, internal_start, holiday_fn)."""
	iso_date = normalize_date(target_date)
	if not iso_date:
		_throw_self_serve_error(_("A valid appointment date is required."), "invalid_date")

	offset_minutes = int(settings.get("customer_arrival_offset_minutes", 15))
	internal_start_time = to_internal_slot_start(customer_arrival_time, offset_minutes=offset_minutes)
	arrival_dt = to_customer_arrival_datetime(iso_date, internal_start_time, offset_minutes=offset_minutes)

	target_dt = date.fromisoformat(iso_date)
	today_date = now_val.date()
	horizon_days = max(1, int(settings.get("self_serve_horizon_days", 30)))
	max_date = today_date + timedelta(days=horizon_days)
	if target_dt < today_date or target_dt > max_date:
		_throw_self_serve_error(
			_("Selected appointment date is outside the available booking window."),
			"outside_horizon",
		)

	min_lead_minutes = max(0, int(settings.get("self_serve_min_lead_time_minutes", 120)))
	earliest_arrival_dt = now_val + timedelta(minutes=min_lead_minutes)
	if arrival_dt < earliest_arrival_dt:
		_throw_self_serve_error(
			_("Selected appointment time does not meet the minimum advance notice requirement."),
			"lead_time_not_met",
		)

	holiday_fn = build_holiday_checker(settings.get("holiday_list"), add_days(target_dt, -30))
	if holiday_fn(target_dt):
		_throw_self_serve_error(
			_("The shop is closed on the selected date."),
			"shop_holiday",
		)

	op_start: timedelta = settings["operating_hours_start"]
	op_end: timedelta = settings["operating_hours_end"]
	bs_td: Optional[timedelta] = settings["break_start"]
	be_td: Optional[timedelta] = settings["break_end"]
	interval: int = int(settings.get("default_slot_interval") or 30)

	if op_start >= op_end or interval <= 0:
		_throw_self_serve_error(
			_("Online scheduling is unavailable for the selected date."),
			"outside_operating_hours",
		)

	start_td = to_timedelta(internal_start_time)
	if start_td < op_start or start_td >= op_end:
		_throw_self_serve_error(
			_("Selected appointment time is outside shop operating hours."),
			"outside_operating_hours",
		)

	delta_seconds = int((start_td - op_start).total_seconds())
	if delta_seconds < 0 or delta_seconds % (interval * 60) != 0:
		_throw_self_serve_error(
			_("Selected appointment time is not an available appointment slot."),
			"invalid_slot_time",
		)

	if bs_td is not None and be_td is not None and bs_td <= start_td < be_td:
		_throw_self_serve_error(
			_("Selected appointment time is unavailable during the midday shop break."),
			"break_window",
		)

	if require_same_day:
		eff_end = effective_end_time(start_td, duration_minutes, bs_td, be_td)
		if eff_end > op_end:
			_throw_self_serve_error(
				_("Selected appointment time does not leave enough time within shop hours to complete this service."),
				"exceeds_operating_hours",
			)

	return iso_date, internal_start_time, holiday_fn


def _lock_schedule_entry(schedule_entry: str) -> Dict[str, Any]:
	"""Acquire FOR UPDATE row lock and return full current-read column snapshot of Schedule Entry."""
	rows = frappe.db.sql(
		"SELECT * FROM `tabSchedule Entry` WHERE `name` = %s FOR UPDATE",
		(schedule_entry,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Appointment '{0}' was not found.").format(schedule_entry), frappe.DoesNotExistError)
	return rows[0]


def _lock_active_service_bays() -> List[str]:
	sb = frappe.qb.DocType("Service Bay")
	rows = (
		frappe.qb.from_(sb)
		.select(sb.name)
		.where(sb.is_active == 1)
		.orderby(sb.name)
		.for_update()
		.run(as_dict=True)
	)
	return [r["name"] for r in rows]


def _lock_active_technicians(settings: Dict[str, Any]) -> List[str]:
	if not cint(settings.get("enable_technician_capacity")):
		return []
	designation = settings.get("technician_designation") or "Technician"
	emp = frappe.qb.DocType("Employee")
	rows = (
		frappe.qb.from_(emp)
		.select(emp.name)
		.where(emp.designation == designation)
		.where(emp.status == "Active")
		.orderby(emp.name)
		.for_update()
		.run(as_dict=True)
	)
	return [r["name"] for r in rows]


def _fetch_blocking_entries_current_read(
	start_date: Union[str, date, datetime],
	end_date: Union[str, date, datetime],
	*,
	service_bays: Optional[Sequence[str]] = None,
	assigned_technicians: Optional[Sequence[str]] = None,
	exclude_entry: Optional[str] = None,
	lookback_days: int = 30,
) -> List[Dict[str, Any]]:
	"""Bounded share-mode current-read of blocking Schedule Entry rows so InnoDB REPEATABLE READ sees concurrent commits."""
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

	conditions = [
		"`scheduled_date` >= %s",
		"`scheduled_date` <= %s",
		"(`completion_date` >= %s OR `completion_date` IS NULL)",
		"`status` NOT IN %s",
	]
	params: List[Any] = [lookback_str, end_str, start_str, tuple(NON_BLOCKING_ENTRY_STATUSES)]

	if bay_list is not None:
		conditions.append("`service_bay` IN %s")
		params.append(tuple(bay_list))
	if tech_list is not None:
		conditions.append("`assigned_technician` IN %s")
		params.append(tuple(tech_list))
	if exclude_entry:
		conditions.append("`name` != %s")
		params.append(exclude_entry)

	where_sql = " AND ".join(conditions)
	return frappe.db.sql(
		f"""
		SELECT
			`name`,
			`service_bay`,
			`assigned_technician`,
			`scheduled_date`,
			`scheduled_time`,
			`estimated_duration`,
			`completion_date`,
			`completion_time`,
			`status`
		FROM `tabSchedule Entry`
		WHERE {where_sql}
		ORDER BY `name` ASC
		LOCK IN SHARE MODE
		""",
		tuple(params),
		as_dict=True,
	)


def _select_available_bay_under_lock(
	active_bays: List[str],
	iso_date: str,
	internal_start_time: str,
	duration_minutes: int,
	*,
	required_tags: Sequence[str],
	preferred_bay: Optional[str] = None,
	exclude_entry: Optional[str] = None,
	holiday_fn: Optional[Callable[[Union[str, date, datetime]], bool]] = None,
) -> str:
	if not active_bays:
		_throw_self_serve_error(
			_("No appointment capacity is available for the selected date and time."),
			"capacity_conflict",
		)

	tags_by_bay = load_bay_tags_map(active_bays)
	required_tags_set = set(parse_required_tags(list(required_tags) if required_tags else None))
	capable_bays = sorted(
		(b for b in active_bays if required_tags_set.issubset(set(tags_by_bay.get(b, [])))),
		key=lambda b: (len(tags_by_bay.get(b, [])), b),
	)
	if preferred_bay and preferred_bay in capable_bays:
		capable_bays = [preferred_bay] + [b for b in capable_bays if b != preferred_bay]

	if not capable_bays:
		_throw_self_serve_error(
			_("No appointment capacity is available for the selected date and time."),
			"capacity_conflict",
		)

	span = calculate_working_span(
		iso_date,
		internal_start_time,
		duration_minutes,
		is_holiday_fn=holiday_fn,
	)
	prop_start_dt, prop_end_dt = span_datetimes(span)
	blocking_entries = _fetch_blocking_entries_current_read(
		span["start_date"],
		span["completion_date"],
		service_bays=capable_bays,
		exclude_entry=exclude_entry,
	)
	entries_by_bay: Dict[str, List[Dict[str, Any]]] = {}
	for entry in blocking_entries:
		bay_name = entry.get("service_bay")
		if bay_name:
			entries_by_bay.setdefault(bay_name, []).append(entry)

	for bay in capable_bays:
		has_conflict = False
		for entry in entries_by_bay.get(bay, ()):
			ex_start_dt, ex_end_dt = entry_datetimes(entry, is_holiday_fn=holiday_fn)
			if intervals_overlap(prop_start_dt, prop_end_dt, ex_start_dt, ex_end_dt):
				has_conflict = True
				break
		if not has_conflict:
			return bay

	_throw_self_serve_error(
		_("No appointment capacity is available for the selected date and time."),
		"capacity_conflict",
	)


def _check_technician_capacity_under_lock(
	active_techs: List[str],
	iso_date: str,
	internal_start_time: str,
	duration_minutes: int,
	*,
	settings: Dict[str, Any],
	current_assigned_tech: Optional[str] = None,
	exclude_entry: Optional[str] = None,
	holiday_fn: Optional[Callable[[Union[str, date, datetime]], bool]] = None,
) -> Optional[str]:
	"""Verify technician pool capacity (when enabled) and resolve whether current_assigned_tech stays assigned."""
	enable_tech = cint(settings.get("enable_technician_capacity"))
	if enable_tech and not active_techs:
		_throw_self_serve_error(
			_("No appointment capacity is available for the selected date and time."),
			"capacity_conflict",
		)

	check_techs: List[str] = list(active_techs)
	if not enable_tech and current_assigned_tech:
		emp_status = frappe.db.get_value("Employee", current_assigned_tech, "status")
		if emp_status != "Active":
			return None
		check_techs = [current_assigned_tech]

	if not check_techs:
		return current_assigned_tech

	span = calculate_working_span(
		iso_date,
		internal_start_time,
		duration_minutes,
		is_holiday_fn=holiday_fn,
	)
	seg_dates = [s["date"] for s in span["segments"]] or [iso_date]
	leaves_by_emp = fetch_leaves_by_employee(check_techs, min(seg_dates), max(seg_dates))
	blocking_entries = _fetch_blocking_entries_current_read(
		min(seg_dates),
		max(seg_dates),
		assigned_technicians=check_techs,
		exclude_entry=exclude_entry,
	)

	entry_segs_by_tech: Dict[str, List[Tuple[str, timedelta, timedelta]]] = {}
	for ent in blocking_entries:
		tid = ent.get("assigned_technician")
		if not tid:
			continue
		e_span = calculate_working_span(
			ent.get("scheduled_date"),
			ent.get("scheduled_time"),
			ent.get("estimated_duration") or 0,
			is_holiday_fn=holiday_fn,
		)
		bucket = entry_segs_by_tech.setdefault(tid, [])
		for s in e_span["segments"]:
			bucket.append((s["date"], to_timedelta(s["start_time"]), to_timedelta(s["end_time"])))

	def _is_tech_free_for_seg(emp_id: str, seg_date: str, seg_start: timedelta, seg_end: timedelta) -> bool:
		if is_employee_on_leave_for_date(leaves_by_emp.get(emp_id, ()), seg_date)["on_leave"]:
			return False
		for ex_date, ex_start, ex_end in entry_segs_by_tech.get(emp_id, ()):
			if ex_date == seg_date and intervals_overlap(seg_start, seg_end, ex_start, ex_end):
				return False
		return True

	if enable_tech:
		for prop_seg in span["segments"]:
			seg_date = prop_seg["date"]
			seg_start = to_timedelta(prop_seg["start_time"])
			seg_end = to_timedelta(prop_seg["end_time"])
			free_count = sum(
				1 for emp_id in active_techs if _is_tech_free_for_seg(emp_id, seg_date, seg_start, seg_end)
			)
			if free_count <= 0:
				_throw_self_serve_error(
					_("No appointment capacity is available for the selected date and time."),
					"capacity_conflict",
				)

	if not current_assigned_tech:
		return None
	if enable_tech and current_assigned_tech not in active_techs:
		return None

	for prop_seg in span["segments"]:
		if not _is_tech_free_for_seg(
			current_assigned_tech,
			prop_seg["date"],
			to_timedelta(prop_seg["start_time"]),
			to_timedelta(prop_seg["end_time"]),
		):
			return None

	return current_assigned_tech


def _release_savepoint_safe(savepoint_name: str) -> None:
	if hasattr(frappe.db, "release_savepoint"):
		try:
			frappe.db.release_savepoint(savepoint_name)
		except Exception:
			pass


def _rollback_savepoint_safe(savepoint_name: str) -> None:
	"""Roll back to savepoint without masking the originating exception if DB rolled back the tx."""
	try:
		frappe.db.rollback(save_point=savepoint_name)
	except Exception:
		pass


def _is_snapshot_isolation_conflict(exc: Exception) -> bool:
	"""Return True for MariaDB ER_CHECKREAD (1020) snapshot-isolation conflict on locking read."""
	return isinstance(exc, frappe.QueryDeadlockError) and "1020" in str(exc)


def _can_safely_restart_read_transaction() -> bool:
	"""Return True iff the current DB transaction has zero writes, zero pending callbacks, and tx control enabled."""
	db = getattr(frappe, "db", None)
	if db is None:
		return False
	if getattr(db, "_disable_transaction_control", False):
		return False

	tx_writes = getattr(db, "transaction_writes", None)
	if tx_writes is None or cint(tx_writes) != 0:
		return False

	for cb_attr in ("before_commit", "after_commit", "before_rollback", "after_rollback"):
		cb = getattr(db, cb_attr, None)
		if cb is None:
			continue
		try:
			if len(cb) > 0:
				return False
		except TypeError:
			funcs = getattr(cb, "_functions", None)
			if funcs:
				return False

	for sp_attr in ("_savepoints", "savepoints"):
		if getattr(db, sp_attr, None):
			return False

	return True


def reschedule_self_serve_appointment(
	schedule_entry: str,
	target_date: str,
	customer_arrival_time: str,
	*,
	reason: Optional[str] = None,
	now_dt: Optional[datetime] = None,
	emit_notification: bool = True,
) -> Dict[str, Any]:
	"""Atomically swap an existing Schedule Entry to a new customer arrival slot."""
	if not schedule_entry or not isinstance(schedule_entry, str) or not schedule_entry.strip():
		frappe.throw(_("Schedule Entry is required."), frappe.ValidationError)
	se_name = schedule_entry.strip()

	now_val = _resolve_now(now_dt)
	settings = get_shop_settings()
	policy = get_self_serve_policy()
	offset_minutes = int(policy["customer_arrival_offset_minutes"])
	default_diag_dur = int(settings.get("default_diagnostic_duration") or 60)

	# 1. Translate & pre-check target date/time formatting before acquiring locks.
	iso_date = normalize_date(target_date)
	if not iso_date:
		_throw_self_serve_error(_("A valid appointment date is required."), "invalid_date")
	internal_start_time = to_internal_slot_start(customer_arrival_time, offset_minutes=offset_minutes)

	# 2 & 3. Canonical Lock Order Step 1: Lock target Schedule Entry FOR UPDATE (current read),
	# sync doc, and re-evaluate eligibility (including Repair Order current read) inside the lock.
	# Under MariaDB innodb_snapshot_isolation=ON (ER_CHECKREAD 1020), a prior non-locking read in
	# the same transaction (e.g. has_permission/exists) conflicts with a concurrent commit; restart
	# the pre-mutation transaction once to lock and evaluate the latest committed state cleanly.
	se = None
	eligibility = None
	for attempt in range(2):
		try:
			locked_row = _lock_schedule_entry(se_name)
			se = frappe.get_doc("Schedule Entry", se_name)
			se.update(locked_row)
			se._original_modified = locked_row.get("modified")
			se._doc_before_save = None
			se._last_span_key = None
			eligibility = evaluate_reschedule_eligibility(se, now_dt=now_val, use_current_read=True)
			break
		except Exception as exc:
			if (
				attempt == 0
				and _is_snapshot_isolation_conflict(exc)
				and _can_safely_restart_read_transaction()
			):
				frappe.db.rollback()
				if hasattr(frappe, "clear_document_cache"):
					frappe.clear_document_cache("Schedule Entry", se_name)
				continue
			raise
	if not eligibility["allowed"]:
		_throw_self_serve_error(
			eligibility["reason_message"] or _("This appointment cannot be rescheduled online."),
			eligibility["reason_code"] or "reschedule_not_allowed",
		)

	# 4. Reject no-op move to identical (scheduled_date, scheduled_time).
	prev_date = normalize_date(se.scheduled_date) or str(se.scheduled_date)
	prev_internal_time = normalize_time(se.scheduled_time) or str(se.scheduled_time)
	if prev_date == iso_date and prev_internal_time == internal_start_time:
		_throw_self_serve_error(
			_("The selected date and time is the same as your current appointment."),
			"same_slot",
		)

	# 5. Validate target slot against holiday, horizon, min lead time, operating grid, break, and hours.
	duration = cint(se.estimated_duration) or default_diag_dur
	op_start: timedelta = settings["operating_hours_start"]
	op_end: timedelta = settings["operating_hours_end"]
	bs_td: Optional[timedelta] = settings["break_start"]
	be_td: Optional[timedelta] = settings["break_end"]
	is_already_multiday = bool(
		se.completion_date
		and normalize_date(se.completion_date) != prev_date
	) or (effective_end_time(op_start, duration, bs_td, be_td) > op_end)

	iso_date, internal_start_time, holiday_fn = _validate_target_slot(
		iso_date,
		customer_arrival_time,
		duration,
		settings=settings,
		now_val=now_val,
		require_same_day=not is_already_multiday,
	)

	previous_appointment = format_customer_appointment_display(
		prev_date,
		prev_internal_time,
		offset_minutes=offset_minutes,
	)
	required_tags = get_required_tags_for_entry(se)

	demo_active = is_demo_mode()

	# 6. Canonical Lock Order Steps 2 & 3: Lock active Service Bays, then active Technicians.
	active_bays = _lock_active_service_bays()
	active_techs = _lock_active_technicians(settings)

	# 7. Atomic swap inside savepoint — original slot is NEVER lost on any failure.
	sp = f"ss_resched_{uuid4().hex[:12]}"
	frappe.db.savepoint(sp)
	try:
		if demo_active and not active_bays:
			chosen_bay = None
		else:
			chosen_bay = _select_available_bay_under_lock(
				active_bays,
				iso_date,
				internal_start_time,
				duration,
				required_tags=required_tags,
				preferred_bay=se.service_bay,
				exclude_entry=se.name,
				holiday_fn=holiday_fn,
			)
		if demo_active and cint(settings.get("enable_technician_capacity")) and not active_techs:
			new_assigned_tech = se.assigned_technician
		else:
			new_assigned_tech = _check_technician_capacity_under_lock(
				active_techs,
				iso_date,
				internal_start_time,
				duration,
				settings=settings,
				current_assigned_tech=se.assigned_technician,
				exclude_entry=se.name,
				holiday_fn=holiday_fn,
			)

		se.scheduled_date = iso_date
		se.scheduled_time = internal_start_time
		se.service_bay = chosen_bay
		se.assigned_technician = new_assigned_tech
		se.reschedule_count = max(0, cint(se.reschedule_count or 0)) + 1
		se.last_rescheduled_at = now_val

		if reason and isinstance(reason, str) and reason.strip():
			clean_reason = reason.strip()[:500]
			audit_line = f"[Self-Serve Reschedule {now_val.strftime('%Y-%m-%d %H:%M')}]: {clean_reason}"
			se.notes = f"{se.notes}\n{audit_line}".strip() if se.notes else audit_line

		se.save(ignore_permissions=True)
	except Exception:
		_rollback_savepoint_safe(sp)
		raise
	else:
		_release_savepoint_safe(sp)

	new_appointment = format_customer_appointment_display(
		se.scheduled_date,
		se.scheduled_time,
		offset_minutes=offset_minutes,
	)
	new_count = max(0, cint(se.reschedule_count or 0))
	max_reschedules = int(policy["max_self_reschedules"])

	if emit_notification:
		emit_scheduling_domain_event(
			"self_serve_appointment_rescheduled",
			{
				"schedule_entry": se.name,
				"booking_source": se.booking_source,
				"customer_display": se.get_display_customer(),
				"vehicle_display": se.get_display_vehicle(),
				"service_bay": se.service_bay,
				"previous_scheduled_date": prev_date,
				"previous_internal_start_time": prev_internal_time,
				"previous_customer_arrival_time": previous_appointment["customer_arrival_time"],
				"previous_customer_display_datetime": previous_appointment["display_datetime"],
				"scheduled_date": iso_date,
				"internal_start_time": internal_start_time,
				"customer_arrival_time": new_appointment["customer_arrival_time"],
				"customer_display_datetime": new_appointment["display_datetime"],
				"reschedule_count": new_count,
				"reason": reason.strip() if isinstance(reason, str) and reason.strip() else None,
			},
			create_action_item=True,
		)

	return {
		"schedule_entry": se.name,
		"previous_appointment": previous_appointment,
		"appointment": new_appointment,
		"reschedule_count": new_count,
		"remaining_reschedules": max(0, max_reschedules - new_count),
	}


def normalize_provisional_intake(
	*,
	customer_name: str,
	phone: str,
	email: Optional[str] = None,
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[int] = None,
	vehicle_color: Optional[str] = None,
	license_plate: Optional[str] = None,
	vin: Optional[str] = None,
	vehicle_generation: Optional[str] = None,
	vin_or_plate: Optional[str] = None,
	notes: Optional[str] = None,
	waiter_preference: Optional[str] = None,
) -> Dict[str, Any]:
	"""Normalize and validate provisional customer & vehicle intake payloads."""
	if not isinstance(customer_name, str) or not customer_name.strip():
		frappe.throw(_("Customer name is required."), frappe.ValidationError)
	clean_name = re.sub(r"\s+", " ", customer_name.strip())
	if len(clean_name) < 2 or len(clean_name) > 140:
		frappe.throw(_("Customer name must be between 2 and 140 characters."), frappe.ValidationError)

	clean_phone = _normalize_phone(phone)

	clean_email: Optional[str] = None
	if email is not None and str(email).strip() != "":
		if not isinstance(email, str):
			frappe.throw(_("Please provide a valid email address."), frappe.ValidationError)
		candidate_email = email.strip().lower()
		if len(candidate_email) > 254 or not _EMAIL_PATTERN.match(candidate_email):
			frappe.throw(_("Please provide a valid email address."), frappe.ValidationError)
		clean_email = candidate_email

	clean_model: Optional[str] = None
	if vehicle_model is not None and str(vehicle_model).strip() != "":
		if not isinstance(vehicle_model, str):
			frappe.throw(_("Invalid vehicle model."), frappe.ValidationError)
		clean_model = normalize_vehicle_model(vehicle_model)

	clean_year: Optional[int] = None
	if vehicle_year is not None and str(vehicle_year).strip() != "":
		if isinstance(vehicle_year, bool):
			frappe.throw(_("Vehicle year must be a valid integer between 2008 and 2100."), frappe.ValidationError)
		try:
			parsed_year = int(vehicle_year)
		except (TypeError, ValueError):
			frappe.throw(_("Vehicle year must be a valid integer between 2008 and 2100."), frappe.ValidationError)
		if parsed_year < 2008 or parsed_year > 2100:
			frappe.throw(_("Vehicle year must be between 2008 and 2100."), frappe.ValidationError)
		clean_year = parsed_year

	if vehicle_color is not None and not isinstance(vehicle_color, str):
		frappe.throw(_("Invalid vehicle color."), frappe.ValidationError)
	clean_color = normalize_vehicle_color(vehicle_color)
	if not clean_color:
		frappe.throw(_("Vehicle color is required."), frappe.ValidationError)
	if len(clean_color) > 60:
		frappe.throw(_("Vehicle color cannot exceed 60 characters."), frappe.ValidationError)

	clean_generation: Optional[str] = None
	if vehicle_generation is not None and str(vehicle_generation).strip() != "":
		if not isinstance(vehicle_generation, str):
			frappe.throw(_("Invalid vehicle generation."), frappe.ValidationError)
		clean_generation = re.sub(r"\s+", " ", vehicle_generation.strip())[:140]

	clean_raw_vin_or_plate: Optional[str] = None
	if vin_or_plate is not None and str(vin_or_plate).strip() != "":
		if not isinstance(vin_or_plate, str):
			frappe.throw(_("Invalid VIN or license plate."), frappe.ValidationError)
		candidate_vp = re.sub(r"\s+", " ", vin_or_plate.strip()).upper()
		if len(candidate_vp) > 32:
			frappe.throw(_("VIN or license plate cannot exceed 32 characters."), frappe.ValidationError)
		clean_raw_vin_or_plate = candidate_vp

	if vin is not None and not isinstance(vin, str):
		frappe.throw(_("Invalid VIN."), frappe.ValidationError)
	clean_vin: Optional[str] = None
	if vin is not None and vin.strip():
		candidate_vin = re.sub(r"\s+", "", vin.strip()).upper()
		if len(candidate_vin) > 32:
			frappe.throw(_("VIN cannot exceed 32 characters."), frappe.ValidationError)
		clean_vin = candidate_vin
	elif clean_raw_vin_or_plate and len(clean_raw_vin_or_plate.replace(" ", "")) == 17:
		clean_vin = clean_raw_vin_or_plate.replace(" ", "")

	if license_plate is not None and not isinstance(license_plate, str):
		frappe.throw(_("Invalid license plate."), frappe.ValidationError)
	plate_source = license_plate
	if (not plate_source or not plate_source.strip()) and clean_raw_vin_or_plate:
		if len(clean_raw_vin_or_plate.replace(" ", "")) != 17:
			plate_source = clean_raw_vin_or_plate
	clean_plate = normalize_license_plate(plate_source)
	if clean_plate and (len(clean_plate) < 2 or len(clean_plate) > 20):
		frappe.throw(_("License plate must be between 2 and 20 characters."), frappe.ValidationError)

	clean_vin_or_plate: Optional[str] = clean_vin or clean_plate

	clean_notes: Optional[str] = None
	if notes is not None and str(notes).strip() != "":
		if not isinstance(notes, str):
			frappe.throw(_("Notes must be text."), frappe.ValidationError)
		clean_notes = notes.strip()[:2000]

	settings = get_shop_settings()
	enable_waiter = cint(settings.get("enable_waiter_preference", 1))
	if waiter_preference is None or str(waiter_preference).strip() == "":
		clean_waiter = "Drop-Off"
	else:
		if not isinstance(waiter_preference, str):
			frappe.throw(_("Invalid appointment preference."), frappe.ValidationError)
		clean_waiter = waiter_preference.strip()
		if clean_waiter not in VALID_WAITER_PREFERENCES:
			frappe.throw(
				_("Invalid appointment preference '{0}'.").format(clean_waiter),
				frappe.ValidationError,
			)
		if not enable_waiter and clean_waiter == "Waiter":
			frappe.throw(
				_("Waiting appointments are not currently available; please choose Drop-Off."),
				frappe.ValidationError,
			)

	provisional_vehicle_info = _compose_provisional_vehicle_info(
		clean_year,
		clean_model,
		clean_vin_or_plate,
		vehicle_color=clean_color,
		license_plate=clean_plate,
		vin=clean_vin,
	)
	vehicle_info = build_vehicle_display_dict(
		{
			"model_year": clean_year,
			"model": clean_model,
			"color": clean_color,
			"license_plate": clean_plate,
			"vin": clean_vin,
		},
		fallback=provisional_vehicle_info or "",
	)

	return {
		"customer_name": clean_name,
		"phone": clean_phone,
		"email": clean_email,
		"vehicle_model": clean_model,
		"vehicle_year": clean_year,
		"vehicle_color": clean_color,
		"license_plate": clean_plate,
		"vin": clean_vin,
		"vehicle_generation": clean_generation,
		"vin_or_plate": clean_vin_or_plate,
		"provisional_vehicle_info": provisional_vehicle_info,
		"vehicle_info": vehicle_info,
		"notes": clean_notes,
		"waiter_preference": clean_waiter,
	}


def create_provisional_booking(
	*,
	package_ids: Sequence[str],
	target_date: str,
	customer_arrival_time: str,
	customer_name: str,
	phone: str,
	email: Optional[str] = None,
	vehicle_model: Optional[str] = None,
	vehicle_year: Optional[int] = None,
	vehicle_color: Optional[str] = None,
	license_plate: Optional[str] = None,
	vin: Optional[str] = None,
	vehicle_generation: Optional[str] = None,
	vin_or_plate: Optional[str] = None,
	notes: Optional[str] = None,
	waiter_preference: Optional[str] = None,
	booking_source: str = "Website Self-Serve",
	customer: Optional[str] = None,
	repair_vehicle: Optional[str] = None,
	now_dt: Optional[datetime] = None,
	emit_notification: bool = True,
) -> Dict[str, Any]:
	"""Atomically commit a provisional Schedule Entry for predictable self-serve packages."""
	if booking_source not in VALID_BOOKING_SOURCES:
		frappe.throw(
			_("Invalid booking source '{0}'.").format(booking_source),
			frappe.ValidationError,
		)

	intake = normalize_provisional_intake(
		customer_name=customer_name,
		phone=phone,
		email=email,
		vehicle_model=vehicle_model,
		vehicle_year=vehicle_year,
		vehicle_color=vehicle_color,
		license_plate=license_plate,
		vin=vin,
		vehicle_generation=vehicle_generation,
		vin_or_plate=vin_or_plate,
		notes=notes,
		waiter_preference=waiter_preference,
	)

	reqs = compute_packages_booking_requirements(
		package_ids,
		vehicle_model=intake["vehicle_model"],
		vehicle_year=intake["vehicle_year"],
		vehicle_generation=intake["vehicle_generation"],
	)

	now_val = _resolve_now(now_dt)
	settings = get_shop_settings()
	policy = get_self_serve_policy()
	offset_minutes = int(policy["customer_arrival_offset_minutes"])
	entry_type = policy["self_serve_default_entry_type"] or "Diagnostic"

	if not frappe.db.exists("Schedule Entry Type", entry_type):
		frappe.throw(
			_("Configured self-serve entry type '{0}' does not exist.").format(entry_type),
			frappe.ValidationError,
		)
	requires_ro = cint(frappe.db.get_value("Schedule Entry Type", entry_type, "requires_repair_order") or 0)
	if requires_ro:
		frappe.throw(
			_("Configured self-serve entry type '{0}' requires a pre-existing Repair Order.").format(entry_type),
			frappe.ValidationError,
		)

	duration = int(reqs["duration_minutes"])
	iso_date, internal_start_time, holiday_fn = _validate_target_slot(
		target_date,
		customer_arrival_time,
		duration,
		settings=settings,
		now_val=now_val,
		require_same_day=True,
	)

	demo_active = is_demo_mode()

	# Canonical lock order: active Service Bay ORDER BY name ASC -> active Employee ORDER BY name ASC.
	active_bays = _lock_active_service_bays()
	active_techs = _lock_active_technicians(settings)

	sp = f"ss_book_{uuid4().hex[:12]}"
	frappe.db.savepoint(sp)
	try:
		if demo_active and not active_bays:
			chosen_bay = None
		else:
			chosen_bay = _select_available_bay_under_lock(
				active_bays,
				iso_date,
				internal_start_time,
				duration,
				required_tags=reqs["required_tags"],
				holiday_fn=holiday_fn,
			)
		if not (demo_active and cint(settings.get("enable_technician_capacity")) and not active_techs):
			_check_technician_capacity_under_lock(
				active_techs,
				iso_date,
				internal_start_time,
				duration,
				settings=settings,
				holiday_fn=holiday_fn,
			)

		se_payload: Dict[str, Any] = {
			"doctype": "Schedule Entry",
			"entry_type": entry_type,
			"status": "Scheduled",
			"scheduled_date": iso_date,
			"scheduled_time": internal_start_time,
			"estimated_duration": duration,
			"service_bay": chosen_bay,
			"booking_source": booking_source,
			"waiter_preference": intake["waiter_preference"],
			"reschedule_count": 0,
			"provisional_customer_name": intake["customer_name"],
			"provisional_phone": intake["phone"],
			"provisional_email": intake["email"],
			"provisional_vehicle_model": intake["vehicle_model"],
			"provisional_vehicle_year": intake["vehicle_year"],
			"provisional_vehicle_color": intake["vehicle_color"],
			"provisional_license_plate": intake["license_plate"],
			"provisional_vehicle_generation": intake["vehicle_generation"],
			"provisional_vin_or_plate": intake["vin_or_plate"],
			"provisional_vehicle_info": intake["provisional_vehicle_info"],
			"items_summary": reqs["items_summary"],
			"booked_packages_json": json.dumps(reqs["booked_packages_snapshot"]),
			"notes": intake["notes"],
		}
		if customer:
			se_payload["customer"] = customer
		if repair_vehicle:
			se_payload["repair_vehicle"] = repair_vehicle

		se = frappe.get_doc(se_payload)
		se.insert(ignore_permissions=True)
	except Exception:
		_rollback_savepoint_safe(sp)
		raise
	else:
		_release_savepoint_safe(sp)

	appointment = format_customer_appointment_display(
		se.scheduled_date,
		se.scheduled_time,
		offset_minutes=offset_minutes,
	)

	if emit_notification:
		emit_scheduling_domain_event(
			"self_serve_booking_created",
			{
				"schedule_entry": se.name,
				"booking_source": se.booking_source,
				"customer_display": intake["customer_name"],
				"phone": intake["phone"],
				"email": intake["email"],
				"vehicle_display": intake["provisional_vehicle_info"] or se.get_display_vehicle(),
				"waiter_preference": intake["waiter_preference"],
				"service_bay": se.service_bay,
				"scheduled_date": iso_date,
				"internal_start_time": internal_start_time,
				"customer_arrival_time": appointment["customer_arrival_time"],
				"customer_display_datetime": appointment["display_datetime"],
				"packages": [card["title"] for card in reqs["packages"]],
			},
			create_action_item=True,
		)

	return {
		"booking_reference": se.name,
		"appointment": appointment,
		"packages": reqs["packages"],
		"waiter_preference": intake["waiter_preference"],
		"vehicle_display": intake["provisional_vehicle_info"] or se.get_display_vehicle(),
		"vehicle_info": intake["vehicle_info"],
	}


def _build_action_item_content(event_type: str, payload: Dict[str, Any]) -> Tuple[str, str, str]:
	se_name = str(payload.get("schedule_entry") or "").strip()
	customer_display = str(payload.get("customer_display") or "Customer").strip()
	vehicle_display = str(payload.get("vehicle_display") or "").strip()
	sched_date = str(payload.get("scheduled_date") or "").strip()
	internal_start = str(payload.get("internal_start_time") or "").strip()
	cust_arrival = str(payload.get("customer_arrival_time") or "").strip()
	cust_display_dt = str(payload.get("customer_display_datetime") or "").strip()
	bay = str(payload.get("service_bay") or "").strip()

	custom_dedup = str(payload.get("dedup_key") or "").strip()

	if event_type == "self_serve_booking_created":
		dedup_key = custom_dedup or f"self_serve_booking:{se_name}"
		title = f"New Self-Serve Booking: {customer_display} ({se_name})" if se_name else f"New Self-Serve Booking: {customer_display}"
		lines = [
			f"Schedule Entry: {se_name}",
			f"Source: {payload.get('booking_source') or 'Website Self-Serve'}",
			f"Customer: {customer_display}",
		]
		if payload.get("phone"):
			lines.append(f"Phone: {payload['phone']}")
		if payload.get("email"):
			lines.append(f"Email: {payload['email']}")
		if vehicle_display:
			lines.append(f"Vehicle: {vehicle_display}")
		if payload.get("waiter_preference"):
			lines.append(f"Preference: {payload['waiter_preference']}")
		lines.append(f"Customer Drop-Off: {cust_display_dt or f'{sched_date} {cust_arrival}'}")
		lines.append(f"Internal Bay Start: {sched_date} {internal_start}")
		if bay:
			lines.append(f"Assigned Bay: {bay}")
		packages = payload.get("packages")
		if isinstance(packages, list) and packages:
			lines.append("Services: " + ", ".join(str(p) for p in packages))
		return title[:140], "\n".join(lines), dedup_key

	if event_type == "self_serve_appointment_rescheduled":
		rc = payload.get("reschedule_count", "")
		dedup_key = custom_dedup or f"self_serve_reschedule:{se_name}:{rc}"
		title = f"Appointment Rescheduled: {customer_display} ({se_name})" if se_name else f"Appointment Rescheduled: {customer_display}"
		prev_date = str(payload.get("previous_scheduled_date") or "").strip()
		prev_internal = str(payload.get("previous_internal_start_time") or "").strip()
		prev_cust = str(
			payload.get("previous_customer_display_datetime")
			or f"{prev_date} {payload.get('previous_customer_arrival_time') or ''}".strip()
		)
		lines = [
			f"Schedule Entry: {se_name}",
			f"Customer: {customer_display}",
		]
		if vehicle_display:
			lines.append(f"Vehicle: {vehicle_display}")
		lines.append(f"Previous Customer Drop-Off: {prev_cust}")
		lines.append(f"Previous Internal Bay Start: {prev_date} {prev_internal}")
		lines.append(f"New Customer Drop-Off: {cust_display_dt or f'{sched_date} {cust_arrival}'}")
		lines.append(f"New Internal Bay Start: {sched_date} {internal_start}")
		if bay:
			lines.append(f"Assigned Bay: {bay}")
		lines.append(f"Reschedule Count: {rc}")
		if payload.get("reason"):
			lines.append(f"Customer Reason: {payload['reason']}")
		return title[:140], "\n".join(lines), dedup_key

	dedup_key = custom_dedup or f"scheduling_event:{event_type}:{se_name}"
	title = f"Scheduling Event ({event_type}): {se_name or customer_display}"
	lines = [
		f"Event: {event_type}",
		f"Schedule Entry: {se_name}",
		f"Customer Drop-Off: {cust_display_dt or f'{sched_date} {cust_arrival}'}",
		f"Internal Bay Start: {sched_date} {internal_start}",
	]
	return title[:140], "\n".join(lines), dedup_key


def emit_scheduling_domain_event(
	event_type: str,
	payload: Dict[str, Any],
	*,
	create_action_item: bool = True,
) -> Optional[str]:
	"""Seam 5 decoupled notification emitter (never raises to caller; logs errors cleanly)."""
	action_item_name: Optional[str] = None
	safe_payload = dict(payload or {})
	se_name = str(safe_payload.get("schedule_entry") or "").strip()

	if create_action_item:
		try:
			if frappe.db.exists("DocType", "Shop Action Item"):
				title, details, dedup_key = _build_action_item_content(event_type, safe_payload)
				has_ref = bool(se_name and frappe.db.exists("Schedule Entry", se_name))
				existing_name = frappe.db.get_value(
					"Shop Action Item",
					{"dedup_key": dedup_key, "status": ["in", ["Open", "In Progress"]]},
					"name",
				)
				clearing_type = "schedule_converted_to_ro" if event_type == "self_serve_booking_created" else "manual"
				clearing_json = (
					json.dumps(
						{
							"baseline_timestamp": now_datetime().strftime("%Y-%m-%d %H:%M:%S.%f"),
							"description": "When provisional booking is converted to a Repair Order or cancelled",
						}
					)
					if event_type == "self_serve_booking_created"
					else None
				)
				if existing_name:
					doc = frappe.get_doc("Shop Action Item", existing_name)
					doc.title = title
					doc.category = "General"
					doc.priority = "Medium"
					doc.source = "Agent"
					doc.reference_doctype = "Schedule Entry" if has_ref else None
					doc.reference_name = se_name if has_ref else None
					doc.details = details
					if clearing_type:
						doc.clearing_condition_type = clearing_type
					if clearing_json is not None:
						doc.clearing_condition_json = clearing_json
					doc.save(ignore_permissions=True)
					action_item_name = doc.name
				else:
					doc = frappe.new_doc("Shop Action Item")
					doc.title = title
					doc.category = "General"
					doc.priority = "Medium"
					doc.status = "Open"
					doc.source = "Agent"
					doc.reference_doctype = "Schedule Entry" if has_ref else None
					doc.reference_name = se_name if has_ref else None
					doc.dedup_key = dedup_key
					doc.reported_by = "Administrator"
					doc.details = details
					doc.clearing_condition_type = clearing_type
					if clearing_json is not None:
						doc.clearing_condition_json = clearing_json
					doc.insert(ignore_permissions=True)
					action_item_name = doc.name
		except Exception:
			frappe.log_error(
				title=f"Scheduling Domain Event Action Item Failed: {event_type}",
				message=frappe.get_traceback(),
			)

	event_envelope: Dict[str, Any] = {
		"event_type": event_type,
		"emitted_at": now_datetime().isoformat(),
		"action_item": action_item_name,
		"payload": safe_payload,
	}

	try:
		frappe.publish_realtime(
			"ism_scheduling_domain_event",
			event_envelope,
			after_commit=True,
		)
	except Exception:
		frappe.log_error(
			title=f"Scheduling Domain Event Realtime Failed: {event_type}",
			message=frappe.get_traceback(),
		)

	try:
		hook_targets = frappe.get_hooks("ism_scheduling_domain_events") or ()
		for hook_target in hook_targets:
			try:
				handler = frappe.get_attr(hook_target) if isinstance(hook_target, str) else hook_target
				if callable(handler):
					handler(event_type, event_envelope)
			except Exception:
				frappe.log_error(
					title=f"Scheduling Domain Event Hook Failed: {event_type}",
					message=frappe.get_traceback(),
				)
	except Exception:
		frappe.log_error(
			title=f"Scheduling Domain Event Hooks Resolution Failed: {event_type}",
			message=frappe.get_traceback(),
		)

	return action_item_name
