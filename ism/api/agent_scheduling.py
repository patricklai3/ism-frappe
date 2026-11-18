# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import inspect
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_url, getdate, today

from ism.api._validation import (
	coerce_bool,
	optional_flt,
	optional_int,
	optional_str,
	parse_json_arg,
	require_int,
	require_str,
)
from ism.api.agent_quotes import _estimate_duration_minutes, _get_ro_doc, create_quote
from ism.api.agent_serializers import (
	build_agent_envelope,
	build_appointment_summary,
	build_estimate_summary,
	build_floor_summary,
	build_ro_urls,
	build_slots_summary,
)
from ism.api.estimation_service import estimate_repair_order, get_total_estimate
from ism.api.scheduling import (
	allocate_schedule_entry,
	check_bay_availability,
	create_repair_order_from_schedule_entry,
	get_unscheduled_services as _get_unscheduled_services_impl,
	resolve_service_bay_identifier,
	resolve_technician_identifier,
)
from ism.api.technician_availability import (
	get_active_technicians,
	get_daily_technician_overview,
)
from ism.scheduling.capacity import NON_BLOCKING_ENTRY_STATUSES, ShopCapacityWindow, load_bay_tags_map
from ism.scheduling.time_utils import effective_end_time, get_shop_settings, normalize_date, normalize_time

_VALID_SCHEDULE_ENTRY_STATUSES = (
	"Draft",
	"Scheduled",
	"Confirmed",
	"In Progress",
	"Completed",
	"No Show",
	"Cancelled",
)


def _build_conflict_summary(heading: str, conflict: Dict[str, Any]) -> str:
	lines = [
		f"### {heading}",
		f"- **Reason**: {conflict.get('message')}",
		f"- **Requested**: `{conflict.get('requested_date')}` `{conflict.get('requested_time')}`",
	]
	alts = conflict.get("alternative_slots") or []
	if alts:
		lines.append("- **Nearest Open Alternatives**:")
		for alt in alts[:5]:
			lines.append(f"  - `{alt.get('date')}` `{alt.get('start_time')}` (Bay: `{alt.get('service_bay')}`)")
	return "\n".join(lines)


def _infer_schedule_entry_type(ro: Any, entry_type: Optional[str] = None) -> str:
	if entry_type:
		return entry_type
	services = getattr(ro, "services", None) or []
	has_repair_work = any(
		getattr(s, "service_package", None)
		or not any(
			tok in (getattr(s, "service_title", None) or "").lower()
			for tok in ("diagnos", "inspect", "check")
		)
		for s in services
	)
	if has_repair_work and frappe.db.exists("Schedule Entry Type", "Repair"):
		return "Repair"
	return "Diagnostic"


def _build_ambiguous_bay_availability_message(
	service_bay: str,
	scheduled_date: str,
	start_time: str,
	estimated_duration: Optional[int],
	repair_order: Optional[str],
) -> Optional[str]:
	"""Annotate ambiguous bay candidates with equipment tags and [Available]/[Busy] status."""
	try:
		if estimated_duration:
			dur = max(15, int(estimated_duration))
		elif repair_order and frappe.db.exists("Repair Order", repair_order):
			ro = _get_ro_doc(repair_order, ptype="read")
			dur = _estimate_duration_minutes(ro)
		else:
			settings = get_shop_settings()
			dur = max(15, int(settings.get("default_diagnostic_duration") or 60))

		lowered = service_bay.strip().lower()
		bays = frappe.get_all(
			"Service Bay",
			fields=["name", "bay_name", "is_active"],
			order_by="is_active desc, name asc",
		)
		partial_matches = [
			b
			for b in bays
			if lowered in (b.get("name") or "").lower() or lowered in (b.get("bay_name") or "").lower()
		]
		active_partial = [b for b in partial_matches if cint(b.get("is_active"))]
		candidates = (active_partial or partial_matches)[:5]
		if len(candidates) <= 1:
			return None

		active_names = [c["name"] for c in candidates if cint(c.get("is_active"))]
		tags_by_bay = load_bay_tags_map(active_names)
		formatted: List[str] = []
		for c in candidates:
			bay_id = c["name"]
			tags = sorted(tags_by_bay.get(bay_id) or [])
			label = f"{bay_id} [{', '.join(tags)}]" if tags else bay_id
			if cint(c.get("is_active")):
				is_free = check_bay_availability(bay_id, scheduled_date, start_time, dur)
				label += " [Available]" if is_free else " [Busy]"
			formatted.append(label)

		return _("Ambiguous Service Bay '{0}'. Matching bays: {1}.").format(
			service_bay.strip(), ", ".join(formatted)
		)
	except Exception:
		return None


def _resolve_bay_for_booking(
	service_bay: str,
	scheduled_date: str,
	start_time: str,
	estimated_duration: Optional[int],
	repair_order: Optional[str],
) -> Optional[str]:
	try:
		return resolve_service_bay_identifier(service_bay)
	except frappe.ValidationError as err:
		if "Ambiguous Service Bay" not in str(err) or not scheduled_date or not start_time:
			raise
		annotated_msg = _build_ambiguous_bay_availability_message(
			service_bay,
			scheduled_date=scheduled_date,
			start_time=start_time,
			estimated_duration=estimated_duration,
			repair_order=repair_order,
		)
		if not annotated_msg:
			raise
		frappe.throw(annotated_msg, frappe.ValidationError)


def _schedule_entry_urls(se_name: str, repair_order: Optional[str] = None) -> Dict[str, str]:
	base_url = get_url()
	encoded_se = quote(str(se_name), safe="")
	urls = {
		"desk": f"{base_url}/app/schedule-entry/{encoded_se}",
		"calendar": f"{base_url}/app/schedule-entry/view/calendar",
	}
	if repair_order:
		urls.update(build_ro_urls(repair_order))
		urls["desk"] = f"{base_url}/app/schedule-entry/{encoded_se}"
	return urls


def _serialize_schedule_entry(doc: Any) -> Dict[str, Any]:
	payload: Dict[str, Any] = {
		"name": doc.name,
		"status": doc.status,
		"entry_type": doc.entry_type,
		"repair_order": doc.repair_order,
		"scheduled_date": str(doc.scheduled_date),
		"scheduled_time": str(doc.scheduled_time),
		"estimated_duration": int(doc.estimated_duration or 0),
		"service_bay": doc.service_bay,
		"assigned_technician": doc.assigned_technician,
	}
	warnings = getattr(doc.flags, "scheduling_warnings", None) or []
	if warnings:
		payload["warnings"] = list(warnings)
	return payload


def _parse_equipment_tags(equipment_tags: Any) -> Optional[List[str]]:
	if equipment_tags is None:
		return None
	if isinstance(equipment_tags, str):
		stripped = equipment_tags.strip()
		if not stripped:
			return []
		if stripped.startswith(("[", "{")):
			parsed = parse_json_arg(stripped, [], label="equipment_tags")
			if not isinstance(parsed, list):
				frappe.throw(_("equipment_tags JSON must be a list."), frappe.ValidationError)
			return [optional_str(t, "Equipment Tag") for t in parsed if optional_str(t, "Equipment Tag")]
		return [t.strip() for t in stripped.split(",") if t.strip()]
	if isinstance(equipment_tags, (list, tuple, set)):
		cleaned: List[str] = []
		for t in equipment_tags:
			tag = optional_str(t, "Equipment Tag")
			if tag:
				cleaned.append(tag)
		return cleaned
	frappe.throw(_("equipment_tags must be a list or string."), frappe.ValidationError)


@frappe.whitelist()
def find_available_slots(
	repair_order: Optional[str] = None,
	duration_minutes: Optional[int] = None,
	start_date: Optional[str] = None,
	target_date: Optional[str] = None,
	days_ahead: int = 3,
	equipment_tags: Optional[Union[List[str], str]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	include_summary: Any = False,
	include_resources: Any = False,
	max_slots: int = 15,
	include_past: Any = False,
	min_start_time: Optional[str] = None,
) -> Dict[str, Any]:
	"""Query dual-resource capacity for available booking slots across shop operating hours."""
	import ism.api.agent as agent_mod

	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)
	repair_order = optional_str(repair_order, "Repair Order")
	raw_bay = optional_str(service_bay, "Service Bay")
	raw_tech = optional_str(technician, "Technician")
	raw_start = optional_str(start_date, "Start Date")
	raw_target = optional_str(target_date, "Target Date")
	raw_min_time = optional_str(min_start_time, "Minimum Start Time")

	span = optional_int(days_ahead, "Days Ahead", min_val=1, max_val=60, default=3) or 3
	cap_limit = optional_int(max_slots, "Max Slots", min_val=1, max_val=100, default=15) or 15
	explicit_dur = optional_int(duration_minutes, "Duration Minutes", min_val=1, default=None)
	parsed_tags = _parse_equipment_tags(equipment_tags)

	resolved_bay = resolve_service_bay_identifier(raw_bay) if raw_bay else None
	resolved_tech = resolve_technician_identifier(raw_tech) if raw_tech else None

	dur = 60
	estimate_meta: Optional[Dict[str, Any]] = None
	if explicit_dur is not None:
		dur = max(15, explicit_dur)
		if repair_order:
			_get_ro_doc(repair_order, ptype="read")
	elif repair_order:
		_get_ro_doc(repair_order, ptype="read")
		estimate_meta = estimate_repair_order(repair_order)
		dur = estimate_meta.get("total_duration_minutes", 60)

	norm_start = normalize_date(raw_start) if raw_start else None
	norm_target = normalize_date(raw_target) if raw_target else None
	norm_min_time = normalize_time(raw_min_time) if raw_min_time else None

	anchor = norm_start or norm_target or today()
	start_dt = getdate(anchor)
	end_dt = add_days(start_dt, span - 1)
	show_resources = coerce_bool(include_resources)
	allow_past = coerce_bool(include_past)
	now_dt = agent_mod.now_datetime()
	today_dt = getdate(now_dt)
	now_time_str = now_dt.strftime("%H:%M:%S")

	window = ShopCapacityWindow(
		start_date=start_dt,
		end_date=end_dt,
		required_tags=parsed_tags,
		service_bay=resolved_bay,
		skip_holidays=True,
	)

	results: List[Dict[str, Any]] = []
	for cur_dt in window.iter_dates():
		date_str = str(cur_dt)
		day_min_time: Optional[str] = None
		if norm_min_time and cur_dt == start_dt:
			day_min_time = norm_min_time
		elif not allow_past and cur_dt == today_dt:
			day_min_time = now_time_str

		remaining = (cap_limit - len(results)) if (cap_limit > 0 and not resolved_tech) else None
		day_slots = window.slots_for_date(
			cur_dt,
			dur,
			max_slots=remaining,
			min_start_time=day_min_time,
		)
		for slot in day_slots or []:
			if resolved_tech and "available_technician_ids" in slot:
				if resolved_tech not in (slot.get("available_technician_ids") or []):
					continue
			slot_row: Dict[str, Any] = {
				"date": date_str,
				"start_time": slot.get("start_time"),
				"available_bays": slot.get("available_bays"),
				"available_technicians": slot.get("available_technicians"),
				"effective_capacity": slot.get("effective_capacity"),
				"bottleneck": slot.get("bottleneck"),
			}
			if "available_bay_ids" in slot:
				slot_row["available_bay_ids"] = slot["available_bay_ids"]
			if (show_resources or resolved_tech) and "available_technician_ids" in slot:
				slot_row["available_technician_ids"] = slot["available_technician_ids"]
			if resolved_tech and "available_technician_ids" in slot:
				slot_row["matched_technician"] = resolved_tech
			results.append(slot_row)
			if cap_limit > 0 and len(results) >= cap_limit:
				break
		if cap_limit > 0 and len(results) >= cap_limit:
			break

	data: Dict[str, Any] = {
		"duration_minutes": dur,
		"slots": results,
		"resources": {
			"bays": {b: window.bay_tags.get(b, []) for b in window.candidate_bays},
			"technicians": dict(window.tech_names),
		},
	}
	if resolved_bay:
		data["service_bay"] = resolved_bay
	if resolved_tech:
		data["technician"] = resolved_tech
	if window.holidays:
		data["skipped_holidays"] = sorted(window.holidays.keys())
	if estimate_meta:
		data["duration_breakdown"] = {
			"p_pure": estimate_meta.get("p_pure"),
			"buffer_padding": estimate_meta.get("buffer_padding"),
			"operations_count": estimate_meta.get("operations_count"),
		}

	return build_agent_envelope(
		success=True,
		data=data,
		message=_("Found {0} available slots.").format(len(results)),
		include_summary=include_summary,
		summary_builder=lambda: build_slots_summary(results, dur),
	)


@frappe.whitelist()
def book_appointment(
	repair_order: Optional[str] = None,
	scheduled_date: str = "",
	start_time: str = "",
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	entry_type: Optional[str] = None,
	notes: Optional[str] = None,
	estimated_duration: Optional[int] = None,
	provisional_customer_name: Optional[str] = None,
	provisional_vehicle_info: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Allocate a linked or standalone Schedule Entry and broadcast ambient realtime update."""
	import ism.api.agent as agent_mod

	frappe.has_permission("Schedule Entry", "create", throw=True)
	repair_order = optional_str(repair_order, "Repair Order")
	service_bay = optional_str(service_bay, "Service Bay")
	technician = optional_str(technician, "Technician")
	entry_type = optional_str(entry_type, "Entry Type")
	notes = optional_str(notes, "Notes")
	provisional_customer_name = optional_str(provisional_customer_name, "Provisional Customer Name")
	provisional_vehicle_info = optional_str(provisional_vehicle_info, "Provisional Vehicle Info")
	raw_date = optional_str(scheduled_date, "Scheduled Date") or ""
	raw_time = optional_str(start_time, "Start Time") or ""
	norm_date = normalize_date(raw_date) or raw_date
	norm_time = normalize_time(raw_time) or raw_time
	explicit_dur = optional_int(estimated_duration, "Estimated Duration", min_val=1, default=None)

	if service_bay:
		frappe.has_permission("Service Bay", "read", throw=True)
		service_bay = agent_mod._resolve_bay_for_booking(
			service_bay,
			scheduled_date=norm_date,
			start_time=norm_time,
			estimated_duration=explicit_dur,
			repair_order=repair_order,
		)
	if technician:
		technician = resolve_technician_identifier(technician)
	ro = None
	if repair_order:
		ro = _get_ro_doc(repair_order, ptype="read")
		dur = max(15, explicit_dur) if explicit_dur is not None else _estimate_duration_minutes(ro)
		resolved_entry_type = _infer_schedule_entry_type(ro, entry_type=entry_type)
		booking_notes = notes if notes is not None else ro.customer_concern
		summary_subject = f"**Repair Order**: `{ro.name}` ({ro.customer})"
	else:
		resolved_entry_type = entry_type or "Diagnostic"
		if frappe.db.exists("Schedule Entry Type", resolved_entry_type):
			requires_ro = frappe.db.get_value(
				"Schedule Entry Type", resolved_entry_type, "requires_repair_order"
			)
			if cint(requires_ro):
				frappe.throw(
					_("Schedule Entry of type '{0}' requires a linked Repair Order.").format(resolved_entry_type),
					frappe.MandatoryError,
				)
		settings = get_shop_settings()
		dur = max(15, explicit_dur if explicit_dur is not None else int(settings.get("default_diagnostic_duration") or 60))
		booking_notes = notes
		customer_label = provisional_customer_name or "Walk-In Diagnostic"
		summary_subject = f"**Provisional Customer**: {customer_label}"
		if provisional_vehicle_info:
			summary_subject += f" ({provisional_vehicle_info})"

	# ScheduleEntry.on_update already publishes ism_calendar_update once on save/insert.
	entry_dict = allocate_schedule_entry(
		repair_order=ro.name if ro else None,
		entry_type=resolved_entry_type,
		scheduled_date=norm_date,
		scheduled_time=norm_time,
		estimated_duration=dur,
		service_bay=service_bay,
		assigned_technician=technician,
		notes=booking_notes,
		provisional_customer_name=provisional_customer_name if not ro else None,
		provisional_vehicle_info=provisional_vehicle_info if not ro else None,
	)
	if entry_dict.get("status") == "conflict" or entry_dict.get("conflict"):
		return build_agent_envelope(
			success=False,
			urls=entry_dict.get("urls") or {"calendar": f"{get_url()}/app/schedule-entry/view/calendar"},
			data=entry_dict,
			message=entry_dict.get("message") or _("Scheduling conflict detected."),
			error=entry_dict.get("message") or _("Scheduling conflict detected."),
			include_summary=include_summary,
			summary_builder=lambda: _build_conflict_summary("Appointment Conflict", entry_dict),
		)

	entry_dict["entry_type"] = resolved_entry_type
	entry_dict["estimated_duration"] = dur

	base_url = get_url()
	cal_url = f"{base_url}/app/schedule-entry/view/calendar"
	se_name = entry_dict.get("name")
	desk_url = f"{base_url}/app/schedule-entry/{quote(str(se_name), safe='')}"

	return build_agent_envelope(
		success=True,
		urls={"calendar": cal_url, "desk": desk_url},
		data=entry_dict,
		message=_("Appointment booked successfully."),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Appointment Confirmed: `{se_name}`\n"
			f"{summary_subject}\n"
			f"**Date**: `{norm_date}` | **Time**: `{norm_time}` ({dur} min)\n"
			f"**Bay**: `{entry_dict.get('service_bay')}` | "
			f"**Technician**: `{entry_dict.get('assigned_technician') or 'Unassigned'}`\n\n"
			f"[View Calendar]({cal_url}) | [Inspect Entry]({desk_url})"
		),
	)


@frappe.whitelist()
def reschedule_appointment(
	schedule_entry: str,
	scheduled_date: Optional[str] = None,
	start_time: Optional[str] = None,
	duration_minutes: Optional[int] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	notes: Optional[str] = None,
	include_summary: Any = False,
	clear_bay: Any = False,
) -> Dict[str, Any]:
	"""Move an existing Schedule Entry to a new date, time, bay, or technician with capacity checks."""
	frappe.has_permission("Schedule Entry", "write", throw=True)
	schedule_entry = require_str(schedule_entry, "Schedule Entry")
	scheduled_date = optional_str(scheduled_date, "Scheduled Date")
	start_time = optional_str(start_time, "Start Time")
	clean_bay = optional_str(service_bay, "Service Bay")
	clean_tech = optional_str(technician, "Technician")
	notes_str = optional_str(notes, "Notes")
	explicit_dur = optional_int(duration_minutes, "Duration Minutes", min_val=1, default=None)
	explicit_clear_bay = coerce_bool(clear_bay)
	if explicit_clear_bay and clean_bay:
		frappe.throw(
			_("Cannot specify both a non-empty service_bay and clear_bay=True."),
			frappe.ValidationError,
		)

	if not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry), frappe.DoesNotExistError)

	doc = frappe.get_doc("Schedule Entry", schedule_entry)
	doc.check_permission("write")
	if doc.status in ("Completed", "Cancelled"):
		frappe.throw(
			_("Cannot reschedule Schedule Entry {0} in '{1}' state.").format(doc.name, doc.status),
			frappe.ValidationError,
		)

	target_date = normalize_date(scheduled_date) if scheduled_date else str(doc.scheduled_date)
	target_time = normalize_time(start_time) if start_time else str(doc.scheduled_time)
	target_dur = max(15, explicit_dur) if explicit_dur is not None else int(doc.estimated_duration or 60)

	should_clear_bay = explicit_clear_bay or (service_bay is not None and not clean_bay)
	if should_clear_bay:
		target_bay: Optional[str] = ""
	elif clean_bay:
		frappe.has_permission("Service Bay", "read", throw=True)
		target_bay = resolve_service_bay_identifier(clean_bay)
	else:
		target_bay = doc.service_bay

	if technician is not None:
		target_tech = resolve_technician_identifier(clean_tech) if clean_tech else None
	else:
		target_tech = doc.assigned_technician

	merged_notes = (f"{doc.notes}\n{notes_str}".strip() if doc.notes else notes_str) if notes_str else None

	alloc_kwargs: Dict[str, Any] = {
		"repair_order": doc.repair_order,
		"entry_type": doc.entry_type or "Diagnostic",
		"scheduled_date": target_date,
		"scheduled_time": target_time,
		"estimated_duration": target_dur,
		"service_bay": target_bay,
		"assigned_technician": target_tech,
		"notes": merged_notes,
		"schedule_entry_name": doc.name,
	}
	if should_clear_bay and "clear_bay" in inspect.signature(allocate_schedule_entry).parameters:
		alloc_kwargs["clear_bay"] = True

	alloc_res = allocate_schedule_entry(**alloc_kwargs)
	if alloc_res.get("status") == "conflict" or alloc_res.get("conflict"):
		return build_agent_envelope(
			success=False,
			urls=alloc_res.get("urls") or _schedule_entry_urls(doc.name, doc.repair_order),
			data=alloc_res,
			message=alloc_res.get("message") or _("Reschedule conflict detected."),
			error=alloc_res.get("message") or _("Reschedule conflict detected."),
			include_summary=include_summary,
			summary_builder=lambda: _build_conflict_summary("Reschedule Conflict", alloc_res),
		)

	doc.reload()
	if should_clear_bay and doc.service_bay:
		doc.service_bay = None
		doc.save()
	entry_data = _serialize_schedule_entry(doc)
	urls = _schedule_entry_urls(doc.name, doc.repair_order)
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=entry_data,
		message=_("Rescheduled appointment {0}.").format(doc.name),
		include_summary=include_summary,
		summary_builder=lambda: build_appointment_summary("Appointment Rescheduled", entry_data, urls),
	)


@frappe.whitelist()
def intake_and_find_slots(
	customer_name: Optional[str] = None,
	vehicle_identifier: Optional[str] = None,
	customer: Optional[str] = None,
	vehicle: Optional[str] = None,
	services: Optional[Union[List[Any], str]] = None,
	parts: Optional[Union[List[Any], str]] = None,
	notes: Optional[str] = None,
	concern: Optional[str] = None,
	customer_phone: Optional[str] = None,
	customer_email: Optional[str] = None,
	company: Optional[str] = None,
	odometer_in: Optional[int] = None,
	start_date: Optional[str] = None,
	target_date: Optional[str] = None,
	days_ahead: int = 3,
	duration_minutes: Optional[int] = None,
	equipment_tags: Optional[Union[List[str], str]] = None,
	service_bay: Optional[str] = None,
	technician: Optional[str] = None,
	max_slots: int = 10,
	include_past: Any = False,
	min_start_time: Optional[str] = None,
	include_resources: Any = False,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Composite intake endpoint: create Draft Estimate quote and return candidate booking slots."""
	frappe.has_permission("Repair Order", "create", throw=True)
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)

	span = optional_int(days_ahead, "Days Ahead", min_val=1, max_val=60, default=3) or 3
	cap_limit = optional_int(max_slots, "Max Slots", min_val=1, max_val=100, default=10) or 10
	explicit_dur = optional_int(duration_minutes, "Duration Minutes", min_val=1, default=None)

	quote_res = create_quote(
		customer_name=customer_name,
		vehicle_identifier=vehicle_identifier,
		customer=customer,
		vehicle=vehicle,
		services=services,
		parts=parts,
		notes=notes,
		concern=concern,
		customer_phone=customer_phone,
		customer_email=customer_email,
		company=company,
		odometer_in=odometer_in,
		include_summary=False,
	)
	ro_data = quote_res["data"]
	ro_name = ro_data["name"]
	dur = explicit_dur if explicit_dur is not None else (ro_data.get("total_duration_minutes") or 60)

	slots_res = find_available_slots(
		repair_order=ro_name,
		duration_minutes=dur,
		start_date=start_date,
		target_date=target_date,
		days_ahead=span,
		equipment_tags=equipment_tags,
		service_bay=service_bay,
		technician=technician,
		include_resources=include_resources,
		max_slots=cap_limit,
		include_past=include_past,
		min_start_time=min_start_time,
		include_summary=False,
	)
	slots_data = slots_res["data"]
	combined_data: Dict[str, Any] = {
		"name": ro_name,
		"repair_order": ro_data,
		"duration_minutes": slots_data["duration_minutes"],
		"slots": slots_data["slots"],
		"resources": slots_data["resources"],
	}
	if "service_bay" in slots_data:
		combined_data["service_bay"] = slots_data["service_bay"]
	if "technician" in slots_data:
		combined_data["technician"] = slots_data["technician"]
	if "skipped_holidays" in slots_data:
		combined_data["skipped_holidays"] = slots_data["skipped_holidays"]
	if "duration_breakdown" in slots_data:
		combined_data["duration_breakdown"] = slots_data["duration_breakdown"]

	urls = dict(quote_res.get("urls") or {})
	urls["calendar"] = f"{get_url()}/app/schedule-entry/view/calendar"

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=combined_data,
		message=_("Created quote {0} and found {1} available slot(s).").format(
			ro_name, len(slots_data["slots"])
		),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Intake Quote `{ro_name}` & Candidate Slots\n"
			f"- **Estimated Duration**: {slots_data['duration_minutes']} min\n"
			f"- **Available Slots Found**: {len(slots_data['slots'])}\n\n"
			f"{build_slots_summary(slots_data['slots'], slots_data['duration_minutes'])}"
		),
	)


@frappe.whitelist()
def update_appointment_status(
	schedule_entry: str,
	status: str,
	notes: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Transition a Schedule Entry status and push a single ambient calendar refresh."""
	frappe.has_permission("Schedule Entry", "write", throw=True)
	schedule_entry = require_str(schedule_entry, "Schedule Entry")
	status = require_str(status, "Status")
	notes_str = optional_str(notes, "Notes")
	if not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry), frappe.DoesNotExistError)

	if status not in _VALID_SCHEDULE_ENTRY_STATUSES:
		frappe.throw(
			_("Invalid Schedule Entry status '{0}'. Allowed values: {1}.").format(
				status, ", ".join(_VALID_SCHEDULE_ENTRY_STATUSES)
			),
			frappe.ValidationError,
		)

	doc = frappe.get_doc("Schedule Entry", schedule_entry)
	doc.check_permission("write")
	doc.status = status
	if notes_str:
		doc.notes = f"{doc.notes}\n{notes_str}".strip() if doc.notes else notes_str
	doc.save()

	entry_data = _serialize_schedule_entry(doc)
	urls = _schedule_entry_urls(doc.name, doc.repair_order)
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=entry_data,
		message=_("Updated Schedule Entry {0} to {1}.").format(doc.name, doc.status),
		include_summary=include_summary,
		summary_builder=lambda: build_appointment_summary("Appointment Updated", entry_data, urls),
	)


@frappe.whitelist()
def create_ro_from_appointment(
	schedule_entry: str,
	odometer_in: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Convert a pre-intake or diagnostic Schedule Entry into a Checked In Repair Order."""
	frappe.has_permission("Schedule Entry", "write", throw=True)
	frappe.has_permission("Repair Order", "create", throw=True)
	schedule_entry = require_str(schedule_entry, "Schedule Entry")
	if not frappe.db.exists("Schedule Entry", schedule_entry):
		frappe.throw(_("Schedule Entry {0} does not exist.").format(schedule_entry), frappe.DoesNotExistError)
	frappe.has_permission("Schedule Entry", "write", doc=schedule_entry, throw=True)
	validated_odometer_in = require_int(odometer_in, "Odometer In", min_val=0)

	res = create_repair_order_from_schedule_entry(schedule_entry, odometer_in=validated_odometer_in)
	ro_name = res["repair_order"]
	urls = build_ro_urls(ro_name)
	urls["calendar"] = f"{get_url()}/app/schedule-entry/view/calendar"
	return build_agent_envelope(
		success=True,
		urls=urls,
		data=res,
		message=_("Created Repair Order {0} from Schedule Entry {1}.").format(ro_name, schedule_entry),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Repair Order Created: `{ro_name}`\n"
			f"- **Schedule Entry**: `{schedule_entry}`\n"
			f"- **Customer Concern**: {res.get('customer_concern') or 'None'}\n\n"
			f"[Open in Desk]({urls['desk']})"
		),
	)


@frappe.whitelist()
def estimate_duration(
	repair_order: Optional[str] = None,
	operations: Optional[Union[List[Any], str]] = None,
	percentile: Optional[float] = None,
	sigma: Optional[float] = None,
	buffer_minutes: Optional[int] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Estimate probabilistic shop duration (log-normal + Fenton-Wilkinson) for an RO or operations list."""
	repair_order = optional_str(repair_order, "Repair Order")
	if not repair_order and not operations:
		frappe.throw(_("Provide either repair_order or operations."), frappe.ValidationError)

	kwargs: Dict[str, Any] = {}
	pct_val = optional_flt(percentile, "Percentile", positive=True, default=None)
	if pct_val is not None:
		kwargs["percentile"] = pct_val
	sig_val = optional_flt(sigma, "Sigma", non_negative=True, default=None)
	if sig_val is not None:
		kwargs["sigma"] = sig_val
	buf_val = optional_int(buffer_minutes, "Buffer Minutes", min_val=0, default=None)
	if buf_val is not None:
		kwargs["buffer_padding_minutes"] = buf_val

	urls: Dict[str, str] = {}
	if repair_order:
		_get_ro_doc(repair_order, ptype="read")
		est = estimate_repair_order(repair_order, **kwargs)
		urls = build_ro_urls(repair_order)
	else:
		frappe.has_permission("Item", "read", throw=True)
		parsed_ops = parse_json_arg(operations, [], label="operations") if isinstance(operations, str) else operations
		est = get_total_estimate(parsed_ops, **kwargs)

	return build_agent_envelope(
		success=True,
		urls=urls,
		data=est,
		message=_("Estimated shop duration: {0} minutes.").format(est.get("total_duration_minutes", 0)),
		include_summary=include_summary,
		summary_builder=lambda: build_estimate_summary(est),
	)


@frappe.whitelist()
def get_shop_floor_status(date: Optional[str] = None, include_summary: Any = False) -> Dict[str, Any]:
	"""Executive query summarizing bay occupancy, active tech pool, and active Repair Orders."""
	frappe.has_permission("Service Bay", "read", throw=True)
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Repair Order", "read", throw=True)
	raw_date = optional_str(date, "Date")
	target_date = normalize_date(raw_date) if raw_date else today()

	bays = frappe.get_list("Service Bay", filters={"is_active": 1}, fields=["name", "description"])
	techs = get_active_technicians(target_date)
	active_techs = [t for t in techs if not t.get("on_leave")]

	entries = frappe.get_list(
		"Schedule Entry",
		filters={"scheduled_date": target_date, "status": ["!=", "Cancelled"]},
		fields=[
			"name",
			"repair_order",
			"service_bay",
			"scheduled_time",
			"completion_time",
			"estimated_duration",
			"assigned_technician",
			"status",
		],
		order_by="scheduled_time asc, name asc",
	)

	active_ros = frappe.get_list(
		"Repair Order",
		filters={"status": ["in", ["Authorized - In Progress", "Waiting on Parts", "Pending Authorization"]]},
		fields=["name", "customer", "vehicle", "status", "approval_status", "vehicle_location"],
	)

	schedule_entries = [
		{
			"name": e.get("name"),
			"repair_order": e.get("repair_order"),
			"service_bay": e.get("service_bay"),
			"scheduled_time": normalize_time(e.get("scheduled_time")) if e.get("scheduled_time") is not None else None,
			"end_time": (
				normalize_time(e.get("completion_time"))
				if e.get("completion_time") is not None
				else (
					normalize_time(effective_end_time(e.get("scheduled_time"), e.get("estimated_duration") or 60))
					if e.get("scheduled_time") is not None
					else None
				)
			),
			"estimated_duration": cint(e.get("estimated_duration") or 0),
			"assigned_technician": e.get("assigned_technician"),
			"status": e.get("status"),
		}
		for e in sorted(
			entries,
			key=lambda row: (
				1 if row.get("status") in NON_BLOCKING_ENTRY_STATUSES else 0,
				str(row.get("scheduled_time") or ""),
				str(row.get("name") or ""),
			),
		)
	]

	summary = {
		"active_bays": len(bays),
		"active_technicians": len(active_techs),
		"active_repair_orders": len(active_ros),
		"scheduled_appointments": len(entries),
		"completed_appointments": len([e for e in entries if e.get("status") == "Completed"]),
		"active_allocations": len([e for e in entries if e.get("status") not in NON_BLOCKING_ENTRY_STATUSES]),
	}

	return build_agent_envelope(
		success=True,
		data={
			"date": target_date,
			"summary": summary,
			"schedule_entries": schedule_entries,
			"active_repair_orders": active_ros,
		},
		message=_("Shop floor telemetry retrieved for {0}.").format(target_date),
		include_summary=include_summary,
		summary_builder=lambda: build_floor_summary(summary, active_ros),
	)


@frappe.whitelist()
def get_tech_overview(date: Optional[str] = None, include_summary: Any = False) -> Dict[str, Any]:
	"""Return daily technician workload, shift windows, and bottleneck analysis."""
	frappe.has_permission("Schedule Entry", "read", throw=True)
	frappe.has_permission("Service Bay", "read", throw=True)
	raw_date = optional_str(date, "Date")
	target_date = normalize_date(raw_date) if raw_date else today()
	overview = get_daily_technician_overview(target_date)
	return build_agent_envelope(
		success=True,
		data=overview if isinstance(overview, dict) else {"date": target_date, "overview": overview},
		message=_("Technician overview retrieved for {0}.").format(target_date),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Technician Overview ({target_date})\n"
			f"- **Total Scheduled Hours**: `{overview.get('total_scheduled_hours', 0) if isinstance(overview, dict) else 0}`\n"
			f"- **Bottleneck**: `{overview.get('bottleneck', 'None') if isinstance(overview, dict) else 'None'}`"
		),
	)


@frappe.whitelist()
def get_unscheduled_services(repair_order: str, include_summary: Any = False) -> Dict[str, Any]:
	"""Return approved services on a Repair Order that lack a scheduled bay allocation."""
	ro = _get_ro_doc(repair_order, ptype="read")
	repair_order = ro.name
	frappe.has_permission("Schedule Entry", "read", throw=True)
	rows = _get_unscheduled_services_impl(repair_order)
	return build_agent_envelope(
		success=True,
		urls=build_ro_urls(repair_order),
		data={
			"repair_order": repair_order,
			"unscheduled_count": len(rows),
			"services": rows,
		},
		message=_("Found {0} unscheduled service(s).").format(len(rows)),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Unscheduled Services for #{repair_order}\n"
			f"- **Pending Scheduling**: `{len(rows)}`"
		),
	)
