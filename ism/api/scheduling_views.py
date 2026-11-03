# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from datetime import timedelta
import frappe
from frappe import _
from frappe.utils import getdate, to_timedelta

STATUS_COLORS = {
	"Draft": "#9e9e9e",
	"Scheduled": "#2196f3",
	"Confirmed": "#4caf50",
	"In Progress": "#ff9800",
	"Completed": "#00bcd4",
	"Cancelled": "#f44336",
	"No Show": "#795548",
}


@frappe.whitelist()
def get_calendar_events(start, end, filters=None):
	"""
	Return structured events payload for FullCalendar / Desk Calendar view.
	Accepts start date, end date, and optional filters (JSON string, dict, or list).
	"""
	if isinstance(filters, str):
		try:
			filters = json.loads(filters)
		except Exception:
			filters = {}
	elif filters is None:
		filters = {}

	query_filters = [
		["scheduled_date", ">=", getdate(start)],
		["scheduled_date", "<=", getdate(end)],
	]

	# Default to excluding Cancelled unless explicitly requested
	if isinstance(filters, dict):
		if "status" in filters:
			query_filters.append(["status", "=", filters["status"]])
		else:
			query_filters.append(["status", "!=", "Cancelled"])

		for fld in ["service_bay", "assigned_technician", "entry_type", "repair_order"]:
			if filters.get(fld):
				query_filters.append([fld, "=", filters[fld]])
	elif isinstance(filters, list):
		query_filters.extend(filters)

	entries = frappe.get_all(
		"Schedule Entry",
		filters=query_filters,
		fields=[
			"name",
			"title",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
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
		try:
			from ism.api.scheduling import effective_end_time
			end_td = effective_end_time(entry.scheduled_time, duration)
		except Exception:
			end_td = start_td + timedelta(minutes=duration)

		sched_date_str = str(entry.scheduled_date)
		start_str = f"{sched_date_str} {str(start_td):>08}"
		end_str = f"{sched_date_str} {str(end_td):>08}"

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
