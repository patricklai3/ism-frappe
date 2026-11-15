# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Optional, Union

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate

DEFAULT_SHOP_SETTINGS: Dict[str, Any] = {
	"operating_hours_start": timedelta(hours=8),
	"operating_hours_end": timedelta(hours=17),
	"break_start": timedelta(hours=12),
	"break_end": timedelta(hours=12, minutes=30),
	"default_slot_interval": 30,
	"scheduling_horizon_days": 30,
	"enable_technician_capacity": 1,
	"technician_designation": "Technician",
	"holiday_list": None,
	"default_diagnostic_duration": 60,
}


_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TIME_PATTERN = re.compile(
	r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$",
)


def normalize_date(val: Union[str, date, datetime, None]) -> Optional[str]:
	"""Validate and normalize an ISO YYYY-MM-DD date (or date/datetime object) into YYYY-MM-DD."""
	if val is None:
		return None
	if isinstance(val, (date, datetime)):
		return str(getdate(val))
	if not isinstance(val, str):
		frappe.throw(_("Invalid date value: '{0}'.").format(val), frappe.ValidationError)

	text = val.strip()
	if not text:
		return None

	if not _ISO_DATE_PATTERN.match(text):
		frappe.throw(
			_("Invalid date format: '{0}'. Expected ISO YYYY-MM-DD.").format(val),
			frappe.ValidationError,
		)

	try:
		return date.fromisoformat(text).isoformat()
	except ValueError:
		frappe.throw(
			_("Invalid date format: '{0}'. Expected ISO YYYY-MM-DD.").format(val),
			frappe.ValidationError,
		)


def normalize_time(val: Union[str, time, timedelta, int, float, None]) -> Optional[str]:
	"""Validate and normalize a 24-hour time string (HH:MM or HH:MM:SS) into HH:MM:SS."""
	if val is None:
		return None
	if isinstance(val, timedelta):
		total_seconds = max(0, int(val.total_seconds()))
		hours = (total_seconds // 3600) % 24
		minutes = (total_seconds % 3600) // 60
		seconds = total_seconds % 60
		return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
	if isinstance(val, time):
		return f"{val.hour:02d}:{val.minute:02d}:{val.second:02d}"
	if isinstance(val, (int, float)):
		total_seconds = max(0, int(val))
		hours = (total_seconds // 3600) % 24
		minutes = (total_seconds % 3600) // 60
		seconds = total_seconds % 60
		return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
	if not isinstance(val, str):
		frappe.throw(_("Invalid time value: '{0}'.").format(val), frappe.ValidationError)

	text = val.strip()
	if not text:
		return None

	match = _TIME_PATTERN.match(text)
	if not match:
		frappe.throw(
			_("Invalid time format: '{0}'. Expected 24-hour HH:MM or HH:MM:SS.").format(val),
			frappe.ValidationError,
		)

	hour = int(match.group(1))
	minute = int(match.group(2))
	second = int(match.group(3) or 0)

	if hour < 0 or hour > 23 or minute < 0 or minute > 59 or second < 0 or second > 59:
		frappe.throw(
			_("Invalid 24-hour time: '{0}'. Expected HH:MM or HH:MM:SS.").format(val),
			frappe.ValidationError,
		)

	return f"{hour:02d}:{minute:02d}:{second:02d}"


def to_timedelta(val: Union[str, time, timedelta, int, float, None]) -> timedelta:
	"""Convert time strings, datetime.time, numeric seconds, or timedelta into timedelta."""
	if val is None:
		return timedelta(0)
	if isinstance(val, timedelta):
		return val
	if isinstance(val, time):
		return timedelta(hours=val.hour, minutes=val.minute, seconds=val.second)
	if isinstance(val, (int, float)):
		return timedelta(seconds=int(val))
	if isinstance(val, str):
		text = val.strip()
		if not text:
			return timedelta(0)
		parts = text.split(":")
		try:
			hours = int(parts[0])
			minutes = int(parts[1]) if len(parts) > 1 else 0
			seconds = int(float(parts[2])) if len(parts) > 2 else 0
			return timedelta(hours=hours, minutes=minutes, seconds=seconds)
		except (ValueError, IndexError):
			return timedelta(0)
	return timedelta(0)


def format_timedelta(td: timedelta) -> str:
	"""Format a timedelta into an HH:MM clock string."""
	total_seconds = int(td.total_seconds())
	hours = total_seconds // 3600
	minutes = (total_seconds % 3600) // 60
	return f"{hours:02d}:{minutes:02d}"


SHOP_SETTINGS_CACHE_KEY = "ism:shop_settings"
HOLIDAY_LIST_CACHE_PREFIX = "ism:holiday_list:"


def _get_cache():
	if getattr(getattr(frappe, "flags", None), "in_test", False):
		return None
	cache_fn = getattr(frappe, "cache", None)
	if not callable(cache_fn):
		return None
	try:
		return cache_fn()
	except Exception:
		return None


def invalidate_shop_settings_cache(doc=None, method=None) -> None:
	"""Clear cached ISM Settings when ISM Settings is updated."""
	cache = _get_cache()
	if cache is not None:
		try:
			cache.delete_value(SHOP_SETTINGS_CACHE_KEY)
		except Exception:
			pass


def invalidate_holiday_cache(doc=None, method=None) -> None:
	"""Clear cached Holiday List dates when a Holiday List is updated or deleted."""
	cache = _get_cache()
	if cache is None:
		return
	try:
		name = getattr(doc, "name", None) if doc is not None else None
		if name:
			cache.delete_value(f"{HOLIDAY_LIST_CACHE_PREFIX}{name}")
		elif hasattr(cache, "delete_keys"):
			cache.delete_keys(HOLIDAY_LIST_CACHE_PREFIX)
	except Exception:
		pass


def _has_time_value(val: Any) -> bool:
	return val is not None and str(val).strip() != ""


def get_shop_settings() -> Dict[str, Any]:
	"""Read cached shop scheduling defaults and normalized timedeltas from ISM Settings."""
	cache = _get_cache()
	if cache is not None:
		try:
			cached = cache.get_value(SHOP_SETTINGS_CACHE_KEY)
			if isinstance(cached, dict):
				return dict(cached)
		except Exception:
			pass

	settings = dict(DEFAULT_SHOP_SETTINGS)

	if not frappe.db.exists("DocType", "ISM Settings"):
		return settings

	try:
		doc = frappe.get_single("ISM Settings")
	except Exception:
		return settings

	if _has_time_value(doc.operating_hours_start):
		settings["operating_hours_start"] = to_timedelta(doc.operating_hours_start)
	if _has_time_value(doc.operating_hours_end):
		settings["operating_hours_end"] = to_timedelta(doc.operating_hours_end)

	settings["break_start"] = to_timedelta(doc.break_start) if _has_time_value(doc.break_start) else None
	settings["break_end"] = to_timedelta(doc.break_end) if _has_time_value(doc.break_end) else None

	if getattr(doc, "default_slot_interval", None):
		settings["default_slot_interval"] = int(doc.default_slot_interval)
	if getattr(doc, "scheduling_horizon_days", None):
		settings["scheduling_horizon_days"] = int(doc.scheduling_horizon_days)
	if hasattr(doc, "enable_technician_capacity") and doc.enable_technician_capacity is not None:
		settings["enable_technician_capacity"] = cint(doc.enable_technician_capacity)
	if getattr(doc, "technician_designation", None):
		settings["technician_designation"] = doc.technician_designation
	if getattr(doc, "holiday_list", None):
		settings["holiday_list"] = doc.holiday_list
	if getattr(doc, "default_diagnostic_duration", None):
		settings["default_diagnostic_duration"] = int(doc.default_diagnostic_duration)

	if cache is not None:
		try:
			cache.set_value(SHOP_SETTINGS_CACHE_KEY, dict(settings), expires_in_sec=300)
		except Exception:
			pass

	return settings


def effective_end_time(
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	break_start: Union[str, time, timedelta, None] = None,
	break_end: Union[str, time, timedelta, None] = None,
) -> timedelta:
	"""Return clock end time for a duration, pushing across the shop break window if needed."""
	start_td = to_timedelta(start_time)
	duration_td = timedelta(minutes=float(duration_minutes or 0))

	if break_start is None or break_start == "" or break_end is None or break_end == "":
		settings = get_shop_settings()
		bs_td = settings["break_start"] if (break_start is None or break_start == "") else to_timedelta(break_start)
		be_td = settings["break_end"] if (break_end is None or break_end == "") else to_timedelta(break_end)
	else:
		bs_td = to_timedelta(break_start)
		be_td = to_timedelta(break_end)

	if bs_td and be_td and be_td > bs_td:
		break_duration = be_td - bs_td
		if bs_td <= start_td < be_td:
			return be_td + duration_td
		if start_td < bs_td and (start_td + duration_td) > bs_td:
			return start_td + duration_td + break_duration

	return start_td + duration_td


def _default_is_holiday(target_date: date, holiday_list: Optional[str]) -> bool:
	if not holiday_list:
		return False
	cache = _get_cache()
	cache_key = f"{HOLIDAY_LIST_CACHE_PREFIX}{holiday_list}"
	if cache is not None:
		try:
			cached_dates = cache.get_value(cache_key)
			if isinstance(cached_dates, (list, set, tuple)):
				return str(target_date) in cached_dates
		except Exception:
			pass

	if not frappe.db.exists("DocType", "Holiday"):
		return False

	rows = frappe.get_all(
		"Holiday",
		filters={"parent": holiday_list},
		fields=["holiday_date"],
	)
	holiday_dates = sorted({str(r["holiday_date"]) for r in rows if r.get("holiday_date")})
	if cache is not None:
		try:
			cache.set_value(cache_key, holiday_dates, expires_in_sec=3600)
		except Exception:
			pass
	return str(target_date) in holiday_dates


def calculate_working_span(
	start_date: Union[str, date, datetime],
	start_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	is_holiday_fn: Optional[Callable[[date], bool]] = None,
) -> Dict[str, Any]:
	"""Calculate active working segments and completion datetime across lunch, overnight, and holidays."""
	settings = get_shop_settings()
	op_start = settings["operating_hours_start"]
	op_end = settings["operating_hours_end"]
	bs_td = settings["break_start"]
	be_td = settings["break_end"]

	s_date = getdate(start_date)
	s_time = to_timedelta(start_time)
	duration_val = float(duration_minutes or 0)
	if duration_val <= 0:
		duration_val = float(settings.get("default_diagnostic_duration") or 60)

	if not op_start or not op_end or op_start >= op_end:
		end_td = s_time + timedelta(minutes=duration_val)
		end_clock = format_timedelta(end_td)
		start_clock = format_timedelta(s_time)
		return {
			"start_date": str(s_date),
			"start_time": start_clock,
			"completion_date": str(s_date),
			"completion_time": end_clock,
			"spans_days": 1,
			"is_multi_day": False,
			"spans_lunch": False,
			"segments": [{
				"date": str(s_date),
				"start_time": start_clock,
				"end_time": end_clock,
				"duration": duration_val,
			}],
			"bay_window": {
				"start_date": str(s_date),
				"start_time": start_clock,
				"end_date": str(s_date),
				"end_time": end_clock,
			},
		}

	curr_date = s_date
	curr_time = s_time
	if curr_time < op_start:
		curr_time = op_start
	elif curr_time >= op_end:
		curr_date = add_days(curr_date, 1)
		curr_time = op_start

	holiday_list = settings.get("holiday_list")
	has_lunch = bool(bs_td and be_td and op_start < bs_td < be_td < op_end)
	rem_minutes = duration_val
	segments = []
	spans_lunch = False
	days_checked = 0

	while rem_minutes > 0 and days_checked < 60:
		days_checked += 1
		is_hol = is_holiday_fn(curr_date) if is_holiday_fn else _default_is_holiday(curr_date, holiday_list)
		if is_hol:
			curr_date = add_days(curr_date, 1)
			curr_time = op_start
			continue

		if has_lunch and bs_td <= curr_time < be_td:
			curr_time = be_td
			spans_lunch = True

		if has_lunch and curr_time < bs_td:
			avail_morning = (bs_td - curr_time).total_seconds() / 60.0
			if avail_morning > 0:
				if rem_minutes <= avail_morning:
					seg_end = curr_time + timedelta(minutes=rem_minutes)
					segments.append({
						"date": str(curr_date),
						"start_time": format_timedelta(curr_time),
						"end_time": format_timedelta(seg_end),
						"duration": rem_minutes,
					})
					curr_time = seg_end
					rem_minutes = 0
					break
				segments.append({
					"date": str(curr_date),
					"start_time": format_timedelta(curr_time),
					"end_time": format_timedelta(bs_td),
					"duration": avail_morning,
				})
				rem_minutes -= avail_morning
				curr_time = be_td
				spans_lunch = True

		if rem_minutes > 0 and curr_time < op_end:
			avail_afternoon = (op_end - curr_time).total_seconds() / 60.0
			if avail_afternoon > 0:
				if rem_minutes <= avail_afternoon:
					seg_end = curr_time + timedelta(minutes=rem_minutes)
					segments.append({
						"date": str(curr_date),
						"start_time": format_timedelta(curr_time),
						"end_time": format_timedelta(seg_end),
						"duration": rem_minutes,
					})
					curr_time = seg_end
					rem_minutes = 0
					break
				segments.append({
					"date": str(curr_date),
					"start_time": format_timedelta(curr_time),
					"end_time": format_timedelta(op_end),
					"duration": avail_afternoon,
				})
				rem_minutes -= avail_afternoon
				curr_date = add_days(curr_date, 1)
				curr_time = op_start
				continue

		curr_date = add_days(curr_date, 1)
		curr_time = op_start

	spans_days = (curr_date - s_date).days + 1
	is_multi = spans_days > 1 or curr_date > s_date
	start_clock = format_timedelta(s_time)
	comp_clock = format_timedelta(curr_time)

	return {
		"start_date": str(s_date),
		"start_time": start_clock,
		"completion_date": str(curr_date),
		"completion_time": comp_clock,
		"spans_days": spans_days,
		"is_multi_day": is_multi,
		"spans_lunch": spans_lunch,
		"segments": segments,
		"bay_window": {
			"start_date": str(s_date),
			"start_time": start_clock,
			"end_date": str(curr_date),
			"end_time": comp_clock,
		},
	}
