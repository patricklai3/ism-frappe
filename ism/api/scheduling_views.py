# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from datetime import date

import frappe
from frappe import _
from frappe.utils import getdate

from ism.scheduling.time_utils import calculate_working_span, to_timedelta

STATUS_COLORS = {
	"Draft": "#9e9e9e",
	"Scheduled": "#2196f3",
	"Confirmed": "#4caf50",
	"In Progress": "#ff9800",
	"Completed": "#00bcd4",
	"Cancelled": "#f44336",
	"No Show": "#795548",
}

_ALLOWED_FILTER_FIELDS = frozenset(
	{"status", "service_bay", "assigned_technician", "entry_type", "repair_order"}
)
_ALLOWED_FILTER_OPERATORS = frozenset({"=", "!=", "in", "not in"})
_ALLOWED_STATUSES = frozenset(STATUS_COLORS.keys())


def _validate_filter_value(field: str, op: str, value):
	"""Validate a calendar filter value against permitted scalar/list shapes."""
	if op in ("in", "not in"):
		if not isinstance(value, (list, tuple)) or not value:
			frappe.throw(_("Calendar filter '{0}' with operator '{1}' requires a non-empty list.").format(field, op))
		cleaned = []
		for item in value:
			if not isinstance(item, str) or not item.strip() or len(item.strip()) > 140:
				frappe.throw(_("Invalid calendar filter value for '{0}'.").format(field))
			item_str = item.strip()
			if field == "status" and item_str not in _ALLOWED_STATUSES:
				frappe.throw(_("Invalid Schedule Entry status filter '{0}'.").format(item_str))
			cleaned.append(item_str)
		return cleaned

	if not isinstance(value, str) or not value.strip() or len(value.strip()) > 140:
		frappe.throw(_("Invalid calendar filter value for '{0}'.").format(field))
	val_str = value.strip()
	if field == "status" and val_str not in _ALLOWED_STATUSES:
		frappe.throw(_("Invalid Schedule Entry status filter '{0}'.").format(val_str))
	return val_str


def _build_calendar_query_filters(start_date: date, end_date: date, filters) -> tuple[list, list]:
	"""Build allowlisted SQL filters + completion_date lower-bound or_filters for calendar view."""
	if isinstance(filters, str):
		stripped = filters.strip()
		if not stripped:
			filters = {}
		else:
			try:
				filters = json.loads(stripped)
			except Exception:
				frappe.throw(_("Invalid calendar filters JSON."))
	elif filters is None:
		filters = {}

	query_filters = [
		["scheduled_date", "<=", end_date],
	]
	has_status_filter = False

	if isinstance(filters, dict):
		unknown = set(filters.keys()) - _ALLOWED_FILTER_FIELDS
		if unknown:
			frappe.throw(
				_("Disallowed calendar filter fields: {0}").format(", ".join(sorted(unknown)))
			)

		raw_status = filters.get("status")
		if isinstance(raw_status, str) and not raw_status.strip():
			raw_status = None
		if raw_status is not None:
			status_val = _validate_filter_value("status", "=", raw_status)
			query_filters.append(["status", "=", status_val])
			has_status_filter = True

		for fld in ("service_bay", "assigned_technician", "entry_type", "repair_order"):
			raw_val = filters.get(fld)
			if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
				continue
			query_filters.append([fld, "=", _validate_filter_value(fld, "=", raw_val)])
	elif isinstance(filters, list):
		for entry in filters:
			if not isinstance(entry, (list, tuple)):
				frappe.throw(_("Invalid calendar filter clause."))
			if len(entry) == 4:
				doctype_name, field, op, value = entry
				if doctype_name != "Schedule Entry":
					frappe.throw(_("Disallowed doctype in calendar filter: {0}").format(doctype_name))
			elif len(entry) == 3:
				field, op, value = entry
			else:
				frappe.throw(_("Invalid calendar filter clause length."))

			if not isinstance(field, str) or field not in _ALLOWED_FILTER_FIELDS:
				frappe.throw(_("Disallowed calendar filter field: {0}").format(field))
			if not isinstance(op, str) or op not in _ALLOWED_FILTER_OPERATORS:
				frappe.throw(_("Disallowed calendar filter operator: {0}").format(op))

			validated_val = _validate_filter_value(field, op, value)
			query_filters.append([field, op, validated_val])
			if field == "status":
				has_status_filter = True
	else:
		frappe.throw(_("Calendar filters must be a dict, list, or JSON string."))

	if not has_status_filter:
		query_filters.append(["status", "!=", "Cancelled"])

	# Push lower date bound into SQL; allow rows missing completion_date to fall back to span calculation
	or_filters = [
		["completion_date", ">=", start_date],
		["completion_date", "is", "not set"],
	]
	return query_filters, or_filters


@frappe.whitelist()
def get_calendar_events(start, end, filters=None):
	"""
	Return structured events payload for FullCalendar / Desk Calendar view.
	Accepts start date, end date, and optional allowlisted filters (JSON string, dict, or list).
	"""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	if not start or not end:
		frappe.throw(_("Start and end dates are required."))

	start_date = getdate(start)
	end_date = getdate(end)
	query_filters, or_filters = _build_calendar_query_filters(start_date, end_date, filters)

	entries = frappe.get_list(
		"Schedule Entry",
		filters=query_filters,
		or_filters=or_filters,
		fields=[
			"name",
			"title",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
			"completion_date",
			"completion_time",
			"status",
			"service_bay",
			"assigned_technician",
			"repair_order",
			"entry_type",
			"customer",
			"provisional_customer_name",
			"repair_vehicle",
			"provisional_vehicle_info",
		],
		order_by="scheduled_date asc, scheduled_time asc",
	)

	events = []
	for entry in entries:
		start_td = to_timedelta(entry.scheduled_time)
		duration = entry.estimated_duration or 60
		sched_date_str = str(entry.scheduled_date)
		start_str = f"{sched_date_str} {start_td!s:>08}"

		if entry.get("completion_date") and entry.get("completion_time"):
			comp_date = getdate(entry.completion_date)
			comp_td = to_timedelta(entry.completion_time)
			comp_date_str = str(entry.completion_date)
		else:
			sp = calculate_working_span(entry.scheduled_date, entry.scheduled_time, duration)
			comp_date = getdate(sp["completion_date"])
			comp_td = to_timedelta(sp["completion_time"])
			comp_date_str = sp["completion_date"]

		if comp_date < start_date:
			continue
		end_str = f"{comp_date_str} {comp_td!s:>08}"

		color = STATUS_COLORS.get(entry.status, "#2196f3")
		title = entry.title or f"{entry.name} ({entry.entry_type or 'Appointment'})"

		events.append({
			"name": entry.name,
			"title": title,
			"start": start_str,
			"end": end_str,
			"allDay": 0,
			"color": color,
			"status": entry.status,
			"service_bay": entry.service_bay,
			"assigned_technician": entry.assigned_technician,
			"repair_order": entry.repair_order,
			"entry_type": entry.entry_type,
		})

	return events
