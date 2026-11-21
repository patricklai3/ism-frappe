# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional, Union

import frappe
from frappe import _

from ism.scheduling.time_utils import get_shop_settings, normalize_date, normalize_time

DEFAULT_ARRIVAL_OFFSET_MINUTES: int = 15
MAX_ARRIVAL_OFFSET_MINUTES: int = 120

_SECONDS_PER_DAY: int = 24 * 60 * 60


def _validate_offset_minutes(offset_minutes: Any) -> int:
	if isinstance(offset_minutes, bool) or not isinstance(offset_minutes, int):
		frappe.throw(
			_("Arrival offset must be an integer between 0 and {0} minutes.").format(
				MAX_ARRIVAL_OFFSET_MINUTES
			),
			frappe.ValidationError,
		)
	if offset_minutes < 0 or offset_minutes > MAX_ARRIVAL_OFFSET_MINUTES:
		frappe.throw(
			_("Arrival offset must be between 0 and {0} minutes.").format(MAX_ARRIVAL_OFFSET_MINUTES),
			frappe.ValidationError,
		)
	return offset_minutes


def _resolve_offset_minutes(offset_minutes: Optional[int]) -> int:
	if offset_minutes is None:
		return get_arrival_offset_minutes()
	return _validate_offset_minutes(offset_minutes)


def _normalize_required_date(val: Union[str, date, datetime]) -> str:
	iso_date = normalize_date(val)
	if not iso_date:
		frappe.throw(_("Appointment date is required."), frappe.ValidationError)
	return iso_date


def _parse_time_seconds(val: Union[str, time, timedelta]) -> int:
	if val is None or isinstance(val, bool):
		frappe.throw(_("Time value is required."), frappe.ValidationError)

	if isinstance(val, timedelta):
		total_seconds = int(val.total_seconds())
		if total_seconds < 0 or total_seconds >= _SECONDS_PER_DAY:
			frappe.throw(
				_("Time value must be within 00:00:00 and 23:59:59."),
				frappe.ValidationError,
			)
		return total_seconds

	if isinstance(val, time):
		return val.hour * 3600 + val.minute * 60 + val.second

	if isinstance(val, str):
		normalized = normalize_time(val)
		if not normalized:
			frappe.throw(_("Time value is required."), frappe.ValidationError)
		hours, minutes, seconds = (int(part) for part in normalized.split(":"))
		return hours * 3600 + minutes * 60 + seconds

	frappe.throw(_("Invalid time value: '{0}'.").format(val), frappe.ValidationError)


def _format_seconds_hhmmss(total_seconds: int) -> str:
	hours = total_seconds // 3600
	minutes = (total_seconds % 3600) // 60
	seconds = total_seconds % 60
	return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def get_arrival_offset_minutes() -> int:
	"""Return validated non-negative earlier arrival offset in minutes from cached ISM Settings (default 15)."""
	settings = get_shop_settings()
	raw = settings.get("customer_arrival_offset_minutes", DEFAULT_ARRIVAL_OFFSET_MINUTES)
	if raw is None:
		return DEFAULT_ARRIVAL_OFFSET_MINUTES
	return _validate_offset_minutes(raw)


def to_customer_arrival_time(
	internal_start: Union[str, time, timedelta],
	offset_minutes: Optional[int] = None,
) -> str:
	"""Convert Internal Capacity Start (T) to Customer Arrival Time (T - offset) as 'HH:MM:SS'.

	Applies uniformly to every slot including opening slot (e.g. '08:00:00' -> '07:45:00').
	Raises frappe.ValidationError if offset is out of [0, 120] or T - offset crosses below 00:00:00.
	"""
	offset = _resolve_offset_minutes(offset_minutes)
	start_seconds = _parse_time_seconds(internal_start)
	arrival_seconds = start_seconds - (offset * 60)
	if arrival_seconds < 0:
		frappe.throw(
			_("Customer arrival time cannot cross below 00:00:00."),
			frappe.ValidationError,
		)
	return _format_seconds_hhmmss(arrival_seconds)


def to_internal_slot_start(
	customer_arrival: Union[str, time, timedelta],
	offset_minutes: Optional[int] = None,
) -> str:
	"""Convert Customer Arrival Time (T_cust) back to Internal Capacity Start (T_cust + offset) as 'HH:MM:SS'.

	Exact inverse of to_customer_arrival_time (e.g. '07:45:00' -> '08:00:00').
	Raises frappe.ValidationError if T_cust + offset >= 24:00:00.
	"""
	offset = _resolve_offset_minutes(offset_minutes)
	arrival_seconds = _parse_time_seconds(customer_arrival)
	internal_seconds = arrival_seconds + (offset * 60)
	if internal_seconds >= _SECONDS_PER_DAY:
		frappe.throw(
			_("Internal slot start time cannot reach or exceed 24:00:00."),
			frappe.ValidationError,
		)
	return _format_seconds_hhmmss(internal_seconds)


def to_customer_arrival_datetime(
	scheduled_date: Union[str, date, datetime],
	internal_start: Union[str, time, timedelta],
	offset_minutes: Optional[int] = None,
) -> datetime:
	"""Return date-aware customer arrival datetime = combine(scheduled_date, internal_start) - timedelta(minutes=offset)."""
	iso_date = _normalize_required_date(scheduled_date)
	arrival_hhmmss = to_customer_arrival_time(internal_start, offset_minutes=offset_minutes)
	hours, minutes, seconds = (int(part) for part in arrival_hhmmss.split(":"))
	return datetime.combine(date.fromisoformat(iso_date), time(hours, minutes, seconds))


def to_internal_slot_datetime(
	customer_date: Union[str, date, datetime],
	customer_arrival: Union[str, time, timedelta],
	offset_minutes: Optional[int] = None,
) -> datetime:
	"""Return date-aware internal capacity start datetime = combine(customer_date, customer_arrival) + timedelta(minutes=offset)."""
	iso_date = _normalize_required_date(customer_date)
	internal_hhmmss = to_internal_slot_start(customer_arrival, offset_minutes=offset_minutes)
	hours, minutes, seconds = (int(part) for part in internal_hhmmss.split(":"))
	return datetime.combine(date.fromisoformat(iso_date), time(hours, minutes, seconds))


def format_customer_arrival_time(
	internal_start: Union[str, time, timedelta],
	offset_minutes: Optional[int] = None,
	*,
	use_12h: bool = True,
) -> str:
	"""Format customer arrival time as clean clock string ('7:45 AM' or '07:45') with ZERO buffer/leeway text."""
	arrival_hhmmss = to_customer_arrival_time(internal_start, offset_minutes=offset_minutes)
	hour = int(arrival_hhmmss[0:2])
	minute = int(arrival_hhmmss[3:5])
	if not use_12h:
		return f"{hour:02d}:{minute:02d}"
	period = "AM" if hour < 12 else "PM"
	hour_12 = hour % 12 or 12
	return f"{hour_12}:{minute:02d} {period}"


def format_customer_appointment_display(
	scheduled_date: Union[str, date, datetime],
	internal_start: Union[str, time, timedelta],
	offset_minutes: Optional[int] = None,
	*,
	use_12h: bool = True,
) -> Dict[str, Any]:
	"""Return customer-safe appointment time dict strictly omitting internal_start_time and offset_minutes."""
	iso_date = _normalize_required_date(scheduled_date)
	parsed_date = date.fromisoformat(iso_date)
	arrival_time = to_customer_arrival_time(internal_start, offset_minutes=offset_minutes)
	display_time = format_customer_arrival_time(
		internal_start,
		offset_minutes=offset_minutes,
		use_12h=use_12h,
	)
	display_date = f"{parsed_date.strftime('%a, %b')} {parsed_date.day}, {parsed_date.year}"
	return {
		"appointment_date": iso_date,
		"customer_arrival_time": arrival_time,
		"customer_arrival_hhmm": arrival_time[:5],
		"display_time": display_time,
		"display_date": display_date,
		"display_datetime": f"{display_date} at {display_time}",
	}


def to_public_slot_projection(
	scheduled_date: Union[str, date, datetime],
	internal_slot: Dict[str, Any],
	offset_minutes: Optional[int] = None,
	*,
	use_12h: bool = True,
) -> Dict[str, Any]:
	"""Project an internal capacity slot dict into a strictly sanitized public customer slot dict."""
	if not isinstance(internal_slot, dict) or "start_time" not in internal_slot:
		frappe.throw(_("Internal slot must be a dict containing 'start_time'."), frappe.ValidationError)

	iso_date = _normalize_required_date(scheduled_date)
	raw_start = internal_slot["start_time"]
	arrival_time = to_customer_arrival_time(raw_start, offset_minutes=offset_minutes)
	display_time = format_customer_arrival_time(
		raw_start,
		offset_minutes=offset_minutes,
		use_12h=use_12h,
	)
	return {
		"date": iso_date,
		"customer_arrival_time": arrival_time,
		"customer_arrival_hhmm": arrival_time[:5],
		"display_time": display_time,
	}
