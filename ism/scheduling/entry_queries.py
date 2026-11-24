# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, time, timedelta
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

import frappe
from frappe.utils import add_days, cint, getdate

from ism.scheduling.capacity import INACTIVE_WORK_STATUSES, NON_BLOCKING_ENTRY_STATUSES
from ism.scheduling.time_utils import (
	calculate_working_span,
	format_timedelta,
	get_shop_settings,
	normalize_date,
	to_timedelta,
)
from ism.utils.vehicle_display import normalize_license_plate, normalize_vehicle_color

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

VEHICLE_CONFLICT_FIELDS: Tuple[str, ...] = (
	"name",
	"repair_order",
	"customer",
	"repair_vehicle",
	"provisional_customer_name",
	"provisional_phone",
	"provisional_email",
	"provisional_vehicle_info",
	"provisional_vehicle_model",
	"provisional_vehicle_year",
	"provisional_vehicle_color",
	"provisional_license_plate",
	"provisional_vin_or_plate",
	"service_bay",
	"assigned_technician",
	"scheduled_date",
	"scheduled_time",
	"estimated_duration",
	"completion_date",
	"completion_time",
	"status",
)

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_NON_DIGIT_RE = re.compile(r"\D+")
_GENERIC_CUSTOMER_NAMES = frozenset({"guest", "walk-in", "walk in", "unknown", "customer"})


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


def _get_entry_attr(entry: Any, fieldname: str) -> Any:
	if isinstance(entry, Mapping):
		return entry.get(fieldname)
	return getattr(entry, fieldname, None)


def _clean_text(value: Any) -> Optional[str]:
	if value is None or isinstance(value, (bool, list, dict, tuple, set)):
		return None
	text = " ".join(str(value).split())
	if not text or text.lower() in ("none", "null"):
		return None
	return text


def _canonical_alnum(value: Any) -> Optional[str]:
	text = _clean_text(value)
	if not text:
		return None
	cleaned = _NON_ALNUM_RE.sub("", text.upper())
	return cleaned or None


def _is_canonical_vin(alnum: Optional[str]) -> bool:
	return bool(alnum and len(alnum) == 17 and alnum.isalnum())


def _canonical_phone(value: Any) -> Optional[str]:
	text = _clean_text(value)
	if not text:
		return None
	try:
		from ism.comms.contracts import normalize_phone_e164

		normalized = normalize_phone_e164(text)
		if normalized:
			return normalized
	except Exception:
		pass

	if re.search(r"[A-Za-z]", text):
		return None
	digits = _NON_DIGIT_RE.sub("", text)
	if len(digits) == 10:
		return f"+1{digits}"
	if len(digits) == 11 and digits.startswith("1"):
		return f"+{digits}"
	if 8 <= len(digits) <= 15 and not digits.startswith("0"):
		return f"+{digits}"
	return None


def _clean_year(value: Any) -> Optional[int]:
	if value is None or isinstance(value, bool):
		return None
	yr = cint(value)
	return yr if yr > 0 else None


def load_ro_meta_map(ro_names: Iterable[Optional[str]]) -> Dict[str, Dict[str, Any]]:
	"""Batch-load Repair Order customer and vehicle links."""
	unique = list(dict.fromkeys(n for raw in ro_names if (n := _clean_text(raw))))
	if not unique:
		return {}
	rows = frappe.get_all(
		"Repair Order",
		filters={"name": ["in", unique]},
		fields=["name", "customer", "vehicle"],
		ignore_permissions=True,
	)
	return {r["name"]: r for r in rows if r.get("name")}


def load_vehicle_meta_map(vehicle_names: Iterable[Optional[str]]) -> Dict[str, Dict[str, Any]]:
	"""Batch-load Repair Vehicle identifying attributes."""
	unique = list(dict.fromkeys(n for raw in vehicle_names if (n := _clean_text(raw))))
	if not unique:
		return {}
	rows = frappe.get_all(
		"Repair Vehicle",
		filters={"name": ["in", unique]},
		fields=["name", "vin", "license_plate", "model", "color", "model_year", "customer"],
		ignore_permissions=True,
	)
	return {r["name"]: r for r in rows if r.get("name")}


def load_customer_phones_map(customers: Iterable[Optional[str]]) -> Dict[str, Set[str]]:
	"""Batch-load normalized phone numbers for Customers (Customer.mobile_no + linked Contacts)."""
	unique = list(dict.fromkeys(c for raw in customers if (c := _clean_text(raw))))
	if not unique:
		return {}

	phones_by_customer: Dict[str, Set[str]] = {c: set() for c in unique}
	try:
		cust_rows = frappe.get_all(
			"Customer",
			filters={"name": ["in", unique]},
			fields=["name", "mobile_no"],
			ignore_permissions=True,
		)
		for row in cust_rows:
			cname = row.get("name")
			phone = _canonical_phone(row.get("mobile_no"))
			if cname and phone:
				phones_by_customer.setdefault(cname, set()).add(phone)

		if frappe.db.exists("DocType", "Dynamic Link"):
			links = frappe.get_all(
				"Dynamic Link",
				filters={
					"parenttype": "Contact",
					"link_doctype": "Customer",
					"link_name": ["in", unique],
				},
				fields=["parent", "link_name"],
				ignore_permissions=True,
			)
			contact_to_customers: Dict[str, List[str]] = {}
			for link in links:
				contact_name = link.get("parent")
				cust_name = link.get("link_name")
				if contact_name and cust_name:
					contact_to_customers.setdefault(contact_name, []).append(cust_name)

			if contact_to_customers:
				contact_names = list(contact_to_customers.keys())
				contact_rows = frappe.get_all(
					"Contact",
					filters={"name": ["in", contact_names]},
					fields=["name", "mobile_no", "phone"],
					ignore_permissions=True,
				)
				for crow in contact_rows:
					cname = crow.get("name")
					for field in ("mobile_no", "phone"):
						phone = _canonical_phone(crow.get(field))
						if phone:
							for cust_name in contact_to_customers.get(cname, ()):
								phones_by_customer.setdefault(cust_name, set()).add(phone)

				if frappe.db.exists("DocType", "Contact Phone"):
					cp_rows = frappe.get_all(
						"Contact Phone",
						filters={"parent": ["in", contact_names]},
						fields=["parent", "phone"],
						ignore_permissions=True,
					)
					for cprow in cp_rows:
						phone = _canonical_phone(cprow.get("phone"))
						if phone:
							for cust_name in contact_to_customers.get(cprow.get("parent"), ()):
								phones_by_customer.setdefault(cust_name, set()).add(phone)
	except Exception:
		pass

	return phones_by_customer


def extract_vehicle_identity(
	entry: Any,
	*,
	vehicle_meta_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
	ro_meta_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
	customer_phones_map: Optional[Mapping[str, Set[str]]] = None,
) -> Dict[str, Any]:
	"""Extract canonical vehicle & phone identity from a Schedule Entry doc or dict."""
	if isinstance(entry, Mapping) and entry.get("_is_vehicle_identity"):
		return dict(entry)

	ro_name = _clean_text(_get_entry_attr(entry, "repair_order"))
	repair_vehicle = _clean_text(_get_entry_attr(entry, "repair_vehicle")) or _clean_text(
		_get_entry_attr(entry, "vehicle")
	)
	customer = _clean_text(_get_entry_attr(entry, "customer"))

	if ro_name and (not repair_vehicle or not customer):
		ro_meta = (ro_meta_map or {}).get(ro_name)
		if ro_meta is None and ro_meta_map is None and frappe.db.exists("Repair Order", ro_name):
			ro_meta = frappe.db.get_value(
				"Repair Order", ro_name, ["customer", "vehicle"], as_dict=True
			) or {}
		if ro_meta:
			if not repair_vehicle:
				repair_vehicle = _clean_text(ro_meta.get("vehicle"))
			if not customer:
				customer = _clean_text(ro_meta.get("customer"))

	veh_meta: Optional[Mapping[str, Any]] = None
	if repair_vehicle:
		veh_meta = (vehicle_meta_map or {}).get(repair_vehicle)
		if veh_meta is None and vehicle_meta_map is None and frappe.db.exists("Repair Vehicle", repair_vehicle):
			veh_meta = frappe.db.get_value(
				"Repair Vehicle",
				repair_vehicle,
				["name", "vin", "license_plate", "model", "color", "model_year", "customer"],
				as_dict=True,
			) or {}
		if veh_meta and not customer:
			customer = _clean_text(veh_meta.get("customer"))

	vins: Set[str] = set()
	plates: Set[str] = set()
	raw_plates: Set[str] = set()

	def _record_vin_or_plate(raw_val: Any, *, force_kind: Optional[str] = None) -> None:
		alnum = _canonical_alnum(raw_val)
		if not alnum:
			return
		if force_kind == "vin":
			vins.add(alnum)
			return
		if force_kind == "plate":
			if len(alnum) >= 2:
				plates.add(alnum)
				norm_p = normalize_license_plate(raw_val)
				if norm_p:
					raw_plates.add(norm_p)
				raw_plates.add(alnum)
			return
		if _is_canonical_vin(alnum):
			vins.add(alnum)
		elif len(alnum) >= 2:
			plates.add(alnum)
			norm_p = normalize_license_plate(raw_val)
			if norm_p:
				raw_plates.add(norm_p)
			raw_plates.add(alnum)

	if veh_meta:
		_record_vin_or_plate(veh_meta.get("vin"), force_kind="vin")
		_record_vin_or_plate(veh_meta.get("license_plate"), force_kind="plate")
	elif repair_vehicle and _is_canonical_vin(_canonical_alnum(repair_vehicle)):
		_record_vin_or_plate(repair_vehicle, force_kind="vin")

	_record_vin_or_plate(_get_entry_attr(entry, "vin"), force_kind="vin")
	_record_vin_or_plate(_get_entry_attr(entry, "license_plate"), force_kind="plate")
	_record_vin_or_plate(_get_entry_attr(entry, "provisional_license_plate"), force_kind="plate")
	_record_vin_or_plate(_get_entry_attr(entry, "provisional_vin_or_plate"))
	_record_vin_or_plate(_get_entry_attr(entry, "vin_or_plate"))

	prov_phone = _canonical_phone(
		_get_entry_attr(entry, "provisional_phone") or _get_entry_attr(entry, "phone")
	)
	all_phones: Set[str] = set()
	if prov_phone:
		all_phones.add(prov_phone)
	if customer and customer_phones_map is not None:
		all_phones.update(customer_phones_map.get(customer, ()))

	email = _clean_text(_get_entry_attr(entry, "provisional_email") or _get_entry_attr(entry, "email"))
	norm_email = email.lower() if email else None

	strong_owners: Set[str] = set()
	if customer:
		strong_owners.add(f"cust:{customer.lower()}")
	for phone in all_phones:
		strong_owners.add(f"phone:{phone}")
	if norm_email:
		strong_owners.add(f"email:{norm_email}")

	prov_cust = _clean_text(
		_get_entry_attr(entry, "provisional_customer_name") or _get_entry_attr(entry, "customer_name")
	)
	fallback_owner: Optional[str] = None
	if prov_cust and prov_cust.lower() not in _GENERIC_CUSTOMER_NAMES:
		fallback_owner = prov_cust.lower()

	raw_model = (
		(veh_meta.get("model") if veh_meta else None)
		or _get_entry_attr(entry, "provisional_vehicle_model")
		or _get_entry_attr(entry, "vehicle_model")
	)
	norm_model = _clean_text(raw_model)
	model_key = norm_model.lower() if norm_model else None

	raw_color = (
		(veh_meta.get("color") if veh_meta else None)
		or _get_entry_attr(entry, "provisional_vehicle_color")
		or _get_entry_attr(entry, "vehicle_color")
	)
	norm_color = normalize_vehicle_color(raw_color)
	color_key = norm_color.lower() if norm_color else None

	year_val = _clean_year(
		(veh_meta.get("model_year") if veh_meta else None)
		or _get_entry_attr(entry, "provisional_vehicle_year")
		or _get_entry_attr(entry, "vehicle_year")
	)

	raw_info_text = _clean_text(_get_entry_attr(entry, "provisional_vehicle_info"))
	raw_info_key = raw_info_text.lower() if raw_info_text else None

	has_owner = bool(strong_owners or fallback_owner)
	has_conflict_identity = bool(
		repair_vehicle
		or ro_name
		or vins
		or plates
		or prov_phone
		or (has_owner and (model_key or raw_info_key))
	)

	return {
		"_is_vehicle_identity": True,
		"repair_vehicle": repair_vehicle,
		"repair_order": ro_name,
		"customer": customer,
		"vins": vins,
		"plates": plates,
		"raw_plates": raw_plates,
		"provisional_phone": prov_phone,
		"all_phones": all_phones,
		"strong_owners": strong_owners,
		"fallback_owner": fallback_owner,
		"model": model_key,
		"color": color_key,
		"year": year_val,
		"raw_info": raw_info_key,
		"has_conflict_identity": has_conflict_identity,
	}


def peak_concurrent_overlaps(
	prop_start: Any,
	prop_end: Any,
	overlapping_intervals: Sequence[Tuple[Any, Any]],
) -> int:
	"""Return peak simultaneous count of intervals overlapping [prop_start, prop_end), including proposed."""
	events: List[Tuple[Any, int]] = []
	for ex_start, ex_end in overlapping_intervals:
		clamped_start = max(prop_start, ex_start)
		clamped_end = min(prop_end, ex_end)
		if clamped_start < clamped_end:
			events.append((clamped_start, 1))
			events.append((clamped_end, -1))
	if not events:
		return 1
	# Half-open intervals: process ends (-1) before starts (+1) at equal timestamps.
	events.sort(key=lambda item: (item[0], item[1]))
	curr = 0
	peak = 0
	for _ts, delta in events:
		curr += delta
		if curr > peak:
			peak = curr
	return peak + 1


def match_booking_conflict(id_a: Mapping[str, Any], id_b: Mapping[str, Any]) -> Optional[str]:
	"""Return conflict category ('vehicle' or 'phone') if two identities conflict, else None.

	Precedence:
	1. Same linked repair_vehicle or same repair_order -> 'vehicle' (always hard-blocked).
	2. Shared canonical VIN or license plate (linked <-> linked, linked <-> provisional, provisional <-> provisional) -> 'vehicle' (always hard-blocked).
	3. Unplated same-vehicle match (when hard IDs do not contradict): shared owner anchor (customer, phone, email, or fallback name) + compatible vehicle descriptor (matching model with non-contradicting color/year, or identical provisional_vehicle_info) -> 'vehicle' (always hard-blocked).
	4. Shared normalized phone when at least one side carries a provisional/booking phone -> 'phone' (subject to ISM Settings max_concurrent_bookings_per_phone, default 2).
	"""
	if not id_a.get("has_conflict_identity") or not id_b.get("has_conflict_identity"):
		return None

	# Tier 1: Linked Repair Vehicle or Repair Order
	if id_a.get("repair_vehicle") and id_b.get("repair_vehicle") and id_a["repair_vehicle"] == id_b["repair_vehicle"]:
		return "vehicle"
	if id_a.get("repair_order") and id_b.get("repair_order") and id_a["repair_order"] == id_b["repair_order"]:
		return "vehicle"

	# Tier 2: Hard Vehicle Identifiers (VIN & License Plate)
	if id_a.get("vins") and id_b.get("vins") and (id_a["vins"] & id_b["vins"]):
		return "vehicle"
	if id_a.get("plates") and id_b.get("plates") and (id_a["plates"] & id_b["plates"]):
		return "vehicle"

	# Tier 3 (Unplated same-vehicle check, skipped if both sides have contradicting hard vehicle IDs)
	has_contradicting_hard_id = bool(
		(id_a.get("repair_vehicle") and id_b.get("repair_vehicle"))
		or (id_a.get("vins") and id_b.get("vins"))
		or (id_a.get("plates") and id_b.get("plates"))
	)
	if not has_contradicting_hard_id:
		strong_a = id_a.get("strong_owners") or set()
		strong_b = id_b.get("strong_owners") or set()
		shared_owner = bool(strong_a & strong_b)
		if not shared_owner and not strong_a and not strong_b:
			fb_a = id_a.get("fallback_owner")
			fb_b = id_b.get("fallback_owner")
			shared_owner = bool(fb_a and fb_b and fb_a == fb_b)

		if shared_owner:
			model_a = id_a.get("model")
			model_b = id_b.get("model")
			if model_a and model_b:
				if model_a == model_b:
					color_a = id_a.get("color")
					color_b = id_b.get("color")
					year_a = id_a.get("year")
					year_b = id_b.get("year")
					color_ok = not (color_a and color_b and color_a != color_b)
					year_ok = not (year_a and year_b and year_a != year_b)
					if color_ok and year_ok:
						return "vehicle"
			else:
				raw_a = id_a.get("raw_info")
				raw_b = id_b.get("raw_info")
				if raw_a and raw_b and raw_a == raw_b:
					return "vehicle"

	# Tier 4: Shared normalized phone (subject to configurable max_concurrent_bookings_per_phone)
	phone_a = id_a.get("provisional_phone")
	phone_b = id_b.get("provisional_phone")
	if phone_a and phone_b and phone_a == phone_b:
		return "phone"
	if phone_a and phone_a in (id_b.get("all_phones") or ()):
		return "phone"
	if phone_b and phone_b in (id_a.get("all_phones") or ()):
		return "phone"

	return None


def lock_vehicle_rows_for_identity(identity: Mapping[str, Any]) -> List[str]:
	"""Acquire deterministic FOR UPDATE row locks on matching Repair Vehicle records."""
	candidate_names: Set[str] = set()
	rv = identity.get("repair_vehicle")
	if rv:
		candidate_names.add(rv)

	vins = list(identity.get("vins") or ())
	raw_plates = list(identity.get("raw_plates") or ())

	conditions: List[str] = []
	params: List[Any] = []
	if candidate_names:
		conditions.append("`name` IN %s")
		params.append(tuple(sorted(candidate_names)))
	if vins:
		conditions.append("`vin` IN %s")
		params.append(tuple(sorted(vins)))
	if raw_plates:
		conditions.append("`license_plate` IN %s")
		params.append(tuple(sorted(raw_plates)))

	if not conditions:
		return []

	where_sql = " OR ".join(conditions)
	rows = frappe.db.sql(
		f"""
		SELECT `name`
		FROM `tabRepair Vehicle`
		WHERE {where_sql}
		ORDER BY `name` ASC
		FOR UPDATE
		""",
		tuple(params),
		as_dict=True,
	)
	return [r["name"] for r in rows if r.get("name")]


def _fetch_blocking_entries_for_conflict(
	start_date: Union[str, date, datetime],
	end_date: Union[str, date, datetime],
	*,
	exclude_entry: Optional[str] = None,
	lookback_days: int = 30,
	use_current_read: bool = False,
) -> List[Dict[str, Any]]:
	if not use_current_read:
		return fetch_blocking_entries(
			start_date,
			end_date,
			exclude_entry=exclude_entry,
			fields=VEHICLE_CONFLICT_FIELDS,
			lookback_days=lookback_days,
		)

	start_dt = getdate(normalize_date(start_date) or start_date)
	end_dt = getdate(normalize_date(end_date) or end_date)
	start_str = str(start_dt)
	end_str = str(end_dt)
	lookback_str = str(add_days(start_dt, -abs(int(lookback_days))))

	select_cols = ", ".join(f"`{fname}`" for fname in VEHICLE_CONFLICT_FIELDS)
	conditions = [
		"`scheduled_date` <= %s",
		"(`completion_date` >= %s OR (`completion_date` IS NULL AND `scheduled_date` >= %s))",
		"`status` NOT IN %s",
	]
	params: List[Any] = [end_str, start_str, lookback_str, tuple(NON_BLOCKING_ENTRY_STATUSES)]
	if exclude_entry:
		conditions.append("`name` != %s")
		params.append(exclude_entry)

	where_sql = " AND ".join(conditions)
	return frappe.db.sql(
		f"""
		SELECT {select_cols}
		FROM `tabSchedule Entry`
		WHERE {where_sql}
		ORDER BY `name` ASC
		LOCK IN SHARE MODE
		""",
		tuple(params),
		as_dict=True,
	)


def resolve_conflict_identities_batch(
	target_entry: Any,
	candidate_entries: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
	"""Batch-load RO, Vehicle, and Customer Phone metadata once for target + candidate entries."""
	all_entries: List[Any] = [target_entry, *candidate_entries]

	ro_names: List[Optional[str]] = [
		_get_entry_attr(e, "repair_order")
		for e in all_entries
		if _get_entry_attr(e, "repair_order")
		and (not _get_entry_attr(e, "repair_vehicle") or not _get_entry_attr(e, "customer"))
	]
	ro_meta_map = load_ro_meta_map(ro_names)

	vehicle_names: List[Optional[str]] = []
	customer_names: List[Optional[str]] = []
	any_provisional_phone = False

	for e in all_entries:
		if isinstance(e, Mapping) and e.get("_is_vehicle_identity"):
			if e.get("repair_vehicle"):
				vehicle_names.append(e["repair_vehicle"])
			if e.get("customer"):
				customer_names.append(e["customer"])
			if e.get("provisional_phone"):
				any_provisional_phone = True
			continue

		rv = _clean_text(_get_entry_attr(e, "repair_vehicle")) or _clean_text(_get_entry_attr(e, "vehicle"))
		cust = _clean_text(_get_entry_attr(e, "customer"))
		ro_n = _clean_text(_get_entry_attr(e, "repair_order"))
		if ro_n and ro_n in ro_meta_map:
			rv = rv or _clean_text(ro_meta_map[ro_n].get("vehicle"))
			cust = cust or _clean_text(ro_meta_map[ro_n].get("customer"))
		if rv:
			vehicle_names.append(rv)
		if cust:
			customer_names.append(cust)
		if _canonical_phone(_get_entry_attr(e, "provisional_phone") or _get_entry_attr(e, "phone")):
			any_provisional_phone = True

	vehicle_meta_map = load_vehicle_meta_map(vehicle_names)
	for vm in vehicle_meta_map.values():
		if vm.get("customer"):
			customer_names.append(vm["customer"])

	customer_phones_map = load_customer_phones_map(customer_names) if any_provisional_phone else {}

	target_id = extract_vehicle_identity(
		target_entry,
		vehicle_meta_map=vehicle_meta_map,
		ro_meta_map=ro_meta_map,
		customer_phones_map=customer_phones_map,
	)
	if (
		isinstance(target_entry, Mapping)
		and target_entry.get("_is_vehicle_identity")
		and customer_phones_map
		and target_id.get("customer")
	):
		target_phones = set(target_id.get("all_phones") or ())
		target_phones.update(customer_phones_map.get(target_id["customer"], ()))
		target_id["all_phones"] = target_phones

	candidate_ids = [
		extract_vehicle_identity(
			cand,
			vehicle_meta_map=vehicle_meta_map,
			ro_meta_map=ro_meta_map,
			customer_phones_map=customer_phones_map,
		)
		for cand in candidate_entries
	]
	return target_id, candidate_ids


def find_conflicting_vehicle_entries(
	target_entry: Any,
	scheduled_date: Union[str, date, datetime],
	scheduled_time: Union[str, time, timedelta],
	duration_minutes: Union[int, float],
	*,
	exclude_entry: Optional[str] = None,
	is_holiday_fn: Optional[Callable[[date], bool]] = None,
	use_current_read: bool = False,
	max_concurrent_per_phone: Optional[int] = None,
) -> List[Dict[str, Any]]:
	"""Return active blocking Schedule Entry rows overlapping a proposed window for the same vehicle or phone.

	Same-vehicle overlaps are always returned (hard limit of 1). Phone-only overlaps (different vehicles
	or blank vehicle info under the same phone number) are returned only when peak simultaneous bookings
	for that phone would exceed max_concurrent_per_phone (from ISM Settings, default 2).
	"""
	target_preview = extract_vehicle_identity(target_entry)
	if not target_preview.get("has_conflict_identity"):
		return []

	span = calculate_working_span(
		scheduled_date,
		scheduled_time,
		duration_minutes,
		is_holiday_fn=is_holiday_fn,
	)
	prop_start_dt, prop_end_dt = span_datetimes(span)

	entries = _fetch_blocking_entries_for_conflict(
		span["start_date"],
		span["completion_date"],
		exclude_entry=exclude_entry,
		use_current_read=use_current_read,
	)
	if not entries:
		return []

	target_id, candidate_ids = resolve_conflict_identities_batch(target_preview, entries)
	if not target_id.get("has_conflict_identity"):
		return []

	if max_concurrent_per_phone is None:
		phone_limit = max(1, cint(get_shop_settings().get("max_concurrent_bookings_per_phone") or 2))
	else:
		phone_limit = max(1, cint(max_concurrent_per_phone))

	vehicle_conflicts: List[Dict[str, Any]] = []
	phone_overlapping_entries: List[Dict[str, Any]] = []
	phone_overlapping_spans: List[Tuple[datetime, datetime]] = []

	for entry, cand_id in zip(entries, candidate_ids):
		match_kind = match_booking_conflict(target_id, cand_id)
		if not match_kind:
			continue
		ex_start_dt, ex_end_dt = entry_datetimes(entry, is_holiday_fn=is_holiday_fn)
		if not intervals_overlap(prop_start_dt, prop_end_dt, ex_start_dt, ex_end_dt):
			continue
		item = dict(entry)
		item["_conflict_match_kind"] = match_kind
		if match_kind == "vehicle":
			vehicle_conflicts.append(item)
		else:
			phone_overlapping_entries.append(item)
			phone_overlapping_spans.append((ex_start_dt, ex_end_dt))

	if vehicle_conflicts:
		return vehicle_conflicts

	if phone_overlapping_entries:
		if peak_concurrent_overlaps(prop_start_dt, prop_end_dt, phone_overlapping_spans) > phone_limit:
			return phone_overlapping_entries

	return []


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
