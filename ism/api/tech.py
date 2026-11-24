# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Whitelisted Technician Execution REST API facade for /tech.

Strict invariants:
- Assignment & readiness isolation enforced via ``ism.permissions`` on every endpoint.
- ZERO pricing, currency, labor rate, parts total, or grand total fields exposed anywhere.
- Schedule Entry -> Repair Order superseding: any Schedule Entry with ``repair_order``
  populated is excluded from ``list_my_jobs`` and redirects in ``get_tech_schedule_entry``.
- Mandatory ``odometer_out >= odometer_in`` on every lot park (final and temporary).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, flt

from ism.api._validation import (
	coerce_bool,
	optional_int,
	optional_str,
	require_str,
)
from ism.board.contracts import CAPTURE_LOCKED_RO_STATUSES, ro_thread_key
from ism.board.gateways import _as_system_user, resolve_actor_role
from ism.permissions import (
	TECH_VISIBLE_APPROVAL_STATUSES,
	assert_can_access_ro,
	assert_can_access_schedule_entry,
	get_eligible_ro_names_for_employee,
	get_employee_for_user,
	is_ro_visible_to_user,
)
from ism.utils.tesla_manual import get_tesla_manual_details_for_vehicle
from ism.utils.vehicle_display import build_vehicle_display_dict

ACTIVE_RO_STATUSES: tuple[str, ...] = ("Authorized - In Progress",)
COMPLETED_RO_STATUSES: tuple[str, ...] = ("Work Completed", "Billed / Paid")
QUEUE_RO_STATUSES: tuple[str, ...] = ACTIVE_RO_STATUSES + COMPLETED_RO_STATUSES

ACTIVE_SE_STATUSES: tuple[str, ...] = ("Scheduled", "Confirmed", "In Progress")
COMPLETED_SE_STATUSES: tuple[str, ...] = ("Completed",)
QUEUE_SE_STATUSES: tuple[str, ...] = ACTIVE_SE_STATUSES + COMPLETED_SE_STATUSES


def _parse_json_safe(raw: Any, default: Any) -> Any:
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	if isinstance(raw, str):
		try:
			return json.loads(raw)
		except Exception:
			return default
	return default


def _format_vehicle_from_row(veh: dict[str, Any] | None) -> dict[str, Any]:
	if not isinstance(veh, dict):
		empty_info = build_vehicle_display_dict(None)
		return {
			"vehicle_display": "",
			"vehicle_compact_display": "",
			"vehicle_color": "",
			"license_plate": "",
			"vin": "",
			"model_year": "",
			"manufacturer": "",
			"model": "",
			"trim": "",
			"drivetrain": "",
			"vehicle_generation": "",
			"recommended_manual_url": "",
			"vehicle_info": empty_info,
		}
	v_info = build_vehicle_display_dict(veh)
	vehicle_gen = str(veh.get("vehicle_generation") or "").strip()
	manual_url = str(veh.get("recommended_manual_url") or "").strip()
	if (not vehicle_gen or not manual_url) and (veh.get("model") or vehicle_gen):
		details = get_tesla_manual_details_for_vehicle(veh)
		vehicle_gen = vehicle_gen or str(details.get("vehicle_generation") or "").strip()
		manual_url = manual_url or str(details.get("recommended_manual_url") or "").strip()
	return {
		"vehicle_display": v_info.get("display") or str(veh.get("display_title") or "").strip(),
		"vehicle_compact_display": v_info.get("compact_display") or "",
		"vehicle_color": v_info.get("color") or "",
		"license_plate": v_info.get("license_plate") or "",
		"vin": v_info.get("vin") or "",
		"model_year": str(v_info.get("model_year") or ""),
		"manufacturer": str(v_info.get("manufacturer") or ""),
		"model": str(v_info.get("model") or ""),
		"trim": str(veh.get("trim") or "").strip(),
		"drivetrain": str(veh.get("drivetrain") or "").strip(),
		"vehicle_generation": vehicle_gen,
		"recommended_manual_url": manual_url,
		"vehicle_info": v_info,
	}


def _format_provisional_vehicle(se: dict[str, Any] | Any) -> dict[str, Any]:
	get = se.get if isinstance(se, dict) else lambda k, d=None: getattr(se, k, d)
	year = get("provisional_vehicle_year")
	model = str(get("provisional_vehicle_model") or "").strip()
	color = str(get("provisional_vehicle_color") or "").strip()
	plate = str(get("provisional_license_plate") or "").strip()
	info = str(get("provisional_vehicle_info") or "").strip()
	vin_or_plate = str(get("provisional_vin_or_plate") or "").strip()
	generation = str(get("provisional_vehicle_generation") or "").strip()

	v_info = build_vehicle_display_dict(
		{
			"provisional_vehicle_year": year,
			"provisional_vehicle_model": model,
			"provisional_vehicle_color": color,
			"provisional_license_plate": plate,
			"provisional_vin_or_plate": vin_or_plate,
		},
		fallback=info,
	)
	manual_url = ""
	if model or generation:
		details = get_tesla_manual_details_for_vehicle(
			{
				"model": model,
				"model_year": year,
				"vehicle_generation": generation,
			}
		)
		generation = generation or str(details.get("vehicle_generation") or "").strip()
		manual_url = str(details.get("recommended_manual_url") or "").strip()

	has_real_fields = any(
		[
			v_info.get("model_year"),
			v_info.get("color"),
			v_info.get("model"),
			v_info.get("license_plate"),
			v_info.get("vin"),
			info,
		]
	)
	display = v_info.get("display") if has_real_fields else ""
	compact = v_info.get("compact_display") if has_real_fields else ""
	return {
		"vehicle_display": display or "",
		"vehicle_compact_display": compact or "",
		"vehicle_color": v_info.get("color") or "",
		"license_plate": v_info.get("license_plate") or "",
		"vin": v_info.get("vin") or "",
		"model_year": str(v_info.get("model_year") or ""),
		"manufacturer": str(v_info.get("manufacturer") or ""),
		"model": str(v_info.get("model") or ""),
		"trim": "",
		"drivetrain": "",
		"vehicle_generation": generation,
		"recommended_manual_url": manual_url,
		"vehicle_info": v_info,
	}


def _compute_ro_lifecycle_state(
	status: str | None,
	vehicle_location: str | None,
	approved_count: int,
	completed_count: int,
	pending_count: int = 0,
) -> str:
	clean_status = (status or "").strip()
	clean_loc = (vehicle_location or "").strip()
	if clean_status in ("Work Completed", "Billed / Paid", "Closed / Released") or clean_loc == "Completed / Parked in Lot":
		return "COMPLETED"
	if clean_loc == "In Bay / Being Serviced":
		if approved_count > 0 and completed_count >= approved_count and pending_count == 0:
			return "READY_FOR_DROPOFF"
		return "IN_BAY_WORKING"
	return "READY_FOR_PICKUP"


def _compute_capture_lock(ro_status: str | None) -> tuple[bool, str]:
	status = (ro_status or "").strip()
	if status in CAPTURE_LOCKED_RO_STATUSES:
		return False, f"Repair Order is {status} — thread is read-only."
	return True, ""


def _format_bin_qty(qty: Any) -> str:
	val = flt(qty, 2)
	return str(int(val)) if val == int(val) else f"{val:g}"


def _parse_service_parts_no_price(
	raw_parts: Any,
	bin_stock_map: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
	"""Extract strictly price-free part descriptors with bin location metadata for technicians."""
	parsed = _parse_json_safe(raw_parts, [])
	if not isinstance(parsed, list):
		return []
	stock_map = bin_stock_map if isinstance(bin_stock_map, dict) else {}
	result: list[dict[str, Any]] = []
	for entry in parsed:
		if not isinstance(entry, dict):
			continue
		item_code = str(entry.get("item_code") or "").strip()
		item_name = str(entry.get("item_name") or item_code).strip()
		if not item_code and not item_name:
			continue

		qty = flt(entry.get("qty") if entry.get("qty") is not None else 1, 2)
		part_source = str(entry.get("part_source") or "OEM").strip() or "OEM"
		explicit_bin = str(entry.get("bin_location") or "").strip().upper()

		stock_info = stock_map.get(item_code) or {}
		default_bin_location = str(stock_info.get("default_bin_location") or "").strip() or None
		raw_bins = stock_info.get("bins") or []
		bins: list[dict[str, Any]] = []
		for b in raw_bins:
			if not isinstance(b, dict):
				continue
			b_code = str(b.get("bin_location") or "").strip()
			if not b_code:
				continue
			bins.append(
				{
					"bin_location": b_code,
					"qty": flt(b.get("qty"), 2),
					"is_default": bool(b.get("is_default")),
				}
			)

		if part_source == "Customer Supplied":
			bin_status = "CUSTOMER_SUPPLIED"
			bin_display = "Customer Supplied"
			primary_bin = None
			bin_locations: list[dict[str, Any]] = []
		else:
			pinned_bin = next((b for b in bins if b["bin_location"] == explicit_bin), None) if explicit_bin else None
			if pinned_bin is not None:
				bin_status = "IN_STOCK"
				primary_bin = pinned_bin["bin_location"]
				bin_display = primary_bin
				bin_locations = [pinned_bin]
			elif len(bins) == 1:
				bin_status = "IN_STOCK"
				primary_bin = bins[0]["bin_location"]
				bin_display = primary_bin
				bin_locations = bins
			elif len(bins) > 1:
				bin_status = "MULTI_BIN"
				primary_bin = bins[0]["bin_location"]
				bin_display = " · ".join(
					f"{b['bin_location']} (×{_format_bin_qty(b['qty'])})" for b in bins
				)
				bin_locations = bins
			elif default_bin_location:
				bin_status = "DEFAULT_ONLY"
				primary_bin = None
				bin_display = f"Home: {default_bin_location}"
				bin_locations = []
			else:
				bin_status = "UNASSIGNED"
				primary_bin = None
				bin_display = "No Bin"
				bin_locations = []

		result.append(
			{
				"item_code": item_code,
				"item_name": item_name,
				"qty": qty,
				"part_source": part_source,
				"primary_bin": primary_bin,
				"default_bin_location": default_bin_location,
				"bin_locations": bin_locations,
				"bin_status": bin_status,
				"bin_display": bin_display,
			}
		)
	return result


def _parse_booked_packages_no_price(raw_json: Any) -> list[dict[str, str]]:
	"""Extract strictly price-free booked package titles for pre-RO diagnostic Schedule Entries."""
	parsed = _parse_json_safe(raw_json, [])
	if not isinstance(parsed, list):
		return []
	packages: list[dict[str, str]] = []
	for item in parsed:
		title = ""
		if isinstance(item, str):
			title = item.strip()
		elif isinstance(item, dict):
			title = str(
				item.get("title")
				or item.get("package_name")
				or item.get("service_title")
				or item.get("name")
				or ""
			).strip()
		if title:
			packages.append({"title": title})
	return packages


def _parse_required_odometer(odometer_out: Any, odometer_in: Any) -> int:
	"""Validate mandatory integer odometer_out >= odometer_in for any customer-lot park."""
	if odometer_out is None or isinstance(odometer_out, bool):
		frappe.throw(
			_("Odometer Out is required before parking the vehicle in the customer lot."),
			frappe.MandatoryError,
		)
	raw_str = str(odometer_out).strip()
	if not raw_str:
		frappe.throw(
			_("Odometer Out is required before parking the vehicle in the customer lot."),
			frappe.MandatoryError,
		)
	try:
		numeric = float(raw_str)
	except (TypeError, ValueError):
		frappe.throw(
			_("Odometer Out must be a valid whole number."),
			frappe.ValidationError,
		)
	if not numeric.is_integer() or numeric < 0:
		frappe.throw(
			_("Odometer Out must be a non-negative whole number."),
			frappe.ValidationError,
		)
	parsed = int(numeric)
	min_odo = cint(odometer_in or 0)
	if parsed < min_odo:
		frappe.throw(
			_("Odometer Out ({0}) must be greater than or equal to Odometer In ({1}).").format(
				parsed, min_odo
			),
			frappe.ValidationError,
		)
	return parsed


def _get_ro_pin_state_safe(repair_order: str | None) -> dict[str, Any]:
	absent_state = {
		"pin_feature_installed": False,
		"status": "Not Prompted",
		"has_pin": False,
		"can_reveal": False,
	}
	if not repair_order:
		return absent_state
	try:
		from ism.portal.pin_vault import get_ro_pin_state
	except ImportError:
		return absent_state
	state = get_ro_pin_state(repair_order)
	if not isinstance(state, dict):
		return absent_state
	result = dict(state)
	result.pop("pin", None)
	result.pop("plaintext_pin", None)
	result.setdefault("repair_order", repair_order)
	result.setdefault("pin_feature_installed", True)
	result.setdefault("status", "Not Prompted")
	result.setdefault("has_pin", False)
	result.setdefault("can_reveal", False)
	return result


def _get_se_pin_state_safe(schedule_entry: str | None) -> dict[str, Any]:
	absent_state = {
		"pin_feature_installed": False,
		"status": "Not Prompted",
		"has_pin": False,
		"can_reveal": False,
	}
	if not schedule_entry:
		return absent_state
	try:
		from ism.portal.pin_vault import get_schedule_entry_pin_state
	except ImportError:
		return absent_state
	state = get_schedule_entry_pin_state(schedule_entry)
	if not isinstance(state, dict):
		return absent_state
	result = dict(state)
	result.pop("pin", None)
	result.pop("plaintext_pin", None)
	result.setdefault("schedule_entry", schedule_entry)
	result.setdefault("pin_feature_installed", True)
	result.setdefault("status", "Not Prompted")
	result.setdefault("has_pin", False)
	result.setdefault("can_reveal", False)
	return result


def _matches_query(card: dict[str, Any], needle: str) -> bool:
	q = needle.lower()
	haystacks = (
		card.get("id"),
		card.get("vehicle_display"),
		card.get("vehicle_compact_display"),
		card.get("vehicle_color"),
		card.get("model_year"),
		card.get("model"),
		card.get("vehicle_generation"),
		card.get("license_plate"),
		card.get("vin"),
		card.get("customer_name"),
		card.get("bay"),
		card.get("entry_type"),
	)
	return any(q in str(h).lower() for h in haystacks if h)


def _fetch_ro_schedule_map(ro_names: list[str]) -> dict[str, dict[str, Any]]:
	if not ro_names:
		return {}
	rows = frappe.get_all(
		"Schedule Entry",
		filters={
			"repair_order": ["in", ro_names],
			"status": ["not in", ["Cancelled", "No Show"]],
		},
		fields=[
			"repair_order",
			"service_bay",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
		],
		order_by="scheduled_date asc, scheduled_time asc",
	)
	mapping: dict[str, dict[str, Any]] = {}
	for r in rows:
		ro_id = r.get("repair_order")
		if ro_id and ro_id not in mapping:
			mapping[ro_id] = r
	return mapping


def _fetch_ro_service_counts(ro_names: list[str]) -> dict[str, tuple[int, int, int]]:
	if not ro_names:
		return {}
	rows = frappe.get_all(
		"Repair Order Service",
		filters={
			"parent": ["in", ro_names],
			"parenttype": "Repair Order",
		},
		fields=["parent", "status", "is_completed"],
	)
	counts: dict[str, list[int]] = {}
	for r in rows:
		parent = r.get("parent")
		if not parent:
			continue
		bucket = counts.setdefault(parent, [0, 0, 0])
		row_status = (r.get("status") or "").strip()
		if row_status == "Approved":
			bucket[0] += 1
			if cint(r.get("is_completed")):
				bucket[1] += 1
		elif row_status in ("", "Draft", "Estimating", "Pending Approval"):
			bucket[2] += 1
	return {k: (v[0], v[1], v[2]) for k, v in counts.items()}


def _fetch_thread_and_message_maps(ro_names: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
	if not ro_names:
		return {}, {}
	thread_keys = [ro_thread_key(ro_id) for ro_id in ro_names]
	thread_rows = frappe.get_all(
		"Shop Board Thread",
		filters={"thread_key": ["in", thread_keys]},
		fields=[
			"thread_key",
			"repair_order",
			"board_status",
			"blocked_reason",
			"blocked_since",
			"open_action_count",
			"last_event_preview",
			"last_event_at",
		],
	)
	thread_map = {str(r["repair_order"]): r for r in thread_rows if r.get("repair_order")}

	placeholders = ", ".join(["%s"] * len(thread_keys))
	msg_rows = frappe.db.sql(
		f"""
		SELECT thread, COUNT(*) AS cnt
		FROM `tabShop Board Event`
		WHERE thread IN ({placeholders})
		  AND IFNULL(payload_type, '') != 'STATE_MUTATION'
		GROUP BY thread
		""",
		tuple(thread_keys),
		as_dict=True,
	)
	msg_map = {str(r["thread"]): cint(r["cnt"]) for r in msg_rows if r.get("thread")}
	return thread_map, msg_map


def _fetch_vehicle_map(vehicle_names: list[str]) -> dict[str, dict[str, Any]]:
	unique_names = list(dict.fromkeys(v for v in vehicle_names if v))
	if not unique_names:
		return {}
	rows = frappe.get_all(
		"Repair Vehicle",
		filters={"name": ["in", unique_names]},
		fields=[
			"name",
			"display_title",
			"color",
			"model_year",
			"manufacturer",
			"model",
			"license_plate",
			"vin",
			"trim",
			"drivetrain",
			"drive_unit",
			"manufactured_month",
			"vehicle_generation",
			"recommended_manual_url",
		],
	)
	return {str(r["name"]): _format_vehicle_from_row(r) for r in rows if r.get("name")}


def _fetch_customer_names(customer_ids: list[str]) -> dict[str, str]:
	unique_ids = list(dict.fromkeys(c for c in customer_ids if c))
	if not unique_ids:
		return {}
	rows = frappe.get_all(
		"Customer",
		filters={"name": ["in", unique_ids]},
		fields=["name", "customer_name"],
	)
	return {str(r["name"]): str(r.get("customer_name") or r["name"]) for r in rows if r.get("name")}


# ---------------------------------------------------------------------------
# Whitelisted Technician Workbench Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_my_jobs(
	include_completed: Any = False,
	query: str | None = None,
	technician: str | None = None,
	limit: int = 50,
) -> dict[str, Any]:
	"""Return strictly scoped, non-overlapping Repair Orders and unlinked Schedule Entries for /tech."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	show_completed = coerce_bool(include_completed, default=False)
	safe_limit = optional_int(limit, "limit", min_val=1, max_val=200, default=50)
	clean_query = optional_str(query, "query")
	clean_tech = optional_str(technician, "technician") if actor_role == "MANAGER" else None

	emp = get_employee_for_user(caller_user)
	if actor_role == "TECH" and not emp:
		return {
			"ok": True,
			"actor_role": "TECH",
			"employee_linked": False,
			"tech_id": None,
			"active_count": 0,
			"completed_count": 0,
			"jobs": [],
		}

	with _as_system_user():
		# 1. Eligible Repair Orders (approved, not waiting on parts, not manual_parts_hold)
		ro_filters: dict[str, Any] = {
			"approval_status": ["in", list(TECH_VISIBLE_APPROVAL_STATUSES)],
			"status": ["in", list(QUEUE_RO_STATUSES)],
			"manual_parts_hold": ["in", [0, None]],
		}
		if actor_role == "TECH":
			eligible_ro_names = sorted(get_eligible_ro_names_for_employee(emp))
			ro_rows = (
				frappe.get_all(
					"Repair Order",
					filters={**ro_filters, "name": ["in", eligible_ro_names]},
					fields=[
						"name",
						"status",
						"vehicle_location",
						"vehicle",
						"customer",
						"odometer_in",
						"odometer_out",
						"progress_percent",
						"picked_up_at",
						"parked_at",
						"pin_to_drive_status",
						"modified",
					],
				)
				if eligible_ro_names
				else []
			)
		else:
			if clean_tech:
				eligible_ro_names = sorted(get_eligible_ro_names_for_employee(clean_tech))
				ro_rows = (
					frappe.get_all(
						"Repair Order",
						filters={**ro_filters, "name": ["in", eligible_ro_names]},
						fields=[
							"name",
							"status",
							"vehicle_location",
							"vehicle",
							"customer",
							"odometer_in",
							"odometer_out",
							"progress_percent",
							"picked_up_at",
							"parked_at",
							"pin_to_drive_status",
							"modified",
						],
					)
					if eligible_ro_names
					else []
				)
			else:
				ro_rows = frappe.get_all(
					"Repair Order",
					filters=ro_filters,
					fields=[
						"name",
						"status",
						"vehicle_location",
						"vehicle",
						"customer",
						"odometer_in",
						"odometer_out",
						"progress_percent",
						"picked_up_at",
						"parked_at",
						"pin_to_drive_status",
						"modified",
					],
					limit_page_length=500,
				)

		# 2. Assigned UNLINKED Schedule Entries (IFNULL(repair_order, '') = '' — superseded SEs never returned!)
		se_conditions = ["IFNULL(repair_order, '') = ''", "status IN %s"]
		se_values: list[Any] = [QUEUE_SE_STATUSES]
		if actor_role == "TECH":
			se_conditions.append("assigned_technician = %s")
			se_values.append(emp)
		elif clean_tech:
			se_conditions.append("assigned_technician = %s")
			se_values.append(clean_tech)

		se_where = " AND ".join(se_conditions)
		se_rows = frappe.db.sql(
			f"""
			SELECT name, status, entry_type, waiter_preference, repair_vehicle, customer,
			       provisional_customer_name, provisional_vehicle_info, provisional_vehicle_model,
			       provisional_vehicle_year, provisional_vehicle_color, provisional_license_plate,
			       provisional_vehicle_generation, provisional_vin_or_plate, service_bay,
			       scheduled_date, scheduled_time, estimated_duration, pin_to_drive_status, modified
			FROM `tabSchedule Entry`
			WHERE {se_where}
			ORDER BY scheduled_date ASC, scheduled_time ASC
			LIMIT 500
			""",
			tuple(se_values),
			as_dict=True,
		)

		active_ro_rows = [r for r in ro_rows if r.get("status") in ACTIVE_RO_STATUSES]
		completed_ro_rows = [r for r in ro_rows if r.get("status") in COMPLETED_RO_STATUSES]
		active_se_rows = [r for r in se_rows if r.get("status") in ACTIVE_SE_STATUSES]
		completed_se_rows = [r for r in se_rows if r.get("status") in COMPLETED_SE_STATUSES]

		active_count = len(active_ro_rows) + len(active_se_rows)
		completed_count = len(completed_ro_rows) + len(completed_se_rows)

		target_ro_rows = completed_ro_rows if show_completed else active_ro_rows
		target_se_rows = completed_se_rows if show_completed else active_se_rows

		ro_names = [str(r["name"]) for r in target_ro_rows if r.get("name")]
		vehicle_ids = [r.get("vehicle") for r in target_ro_rows if r.get("vehicle")] + [
			r.get("repair_vehicle") for r in target_se_rows if r.get("repair_vehicle")
		]
		customer_ids = [r.get("customer") for r in target_ro_rows if r.get("customer")] + [
			r.get("customer") for r in target_se_rows if r.get("customer")
		]

		vehicle_map = _fetch_vehicle_map(vehicle_ids)
		customer_map = _fetch_customer_names(customer_ids)
		ro_schedule_map = _fetch_ro_schedule_map(ro_names)
		ro_service_counts = _fetch_ro_service_counts(ro_names)
		thread_map, msg_map = _fetch_thread_and_message_maps(ro_names)

	jobs: list[dict[str, Any]] = []

	for ro in target_ro_rows:
		ro_id = str(ro["name"])
		v_meta = vehicle_map.get(str(ro.get("vehicle") or ""), _format_vehicle_from_row(None))
		sched = ro_schedule_map.get(ro_id) or {}
		approved_count, completed_count_svc, pending_count_svc = ro_service_counts.get(ro_id, (0, 0, 0))
		lifecycle_state = _compute_ro_lifecycle_state(
			ro.get("status"),
			ro.get("vehicle_location"),
			approved_count,
			completed_count_svc,
			pending_count=pending_count_svc,
		)
		t_key = ro_thread_key(ro_id)
		t_row = thread_map.get(ro_id) or {}
		card = {
			"kind": "REPAIR_ORDER",
			"id": ro_id,
			"status": ro.get("status") or "",
			"lifecycle_state": lifecycle_state,
			"vehicle_location": ro.get("vehicle_location") or "In Customer Lot",
			"vehicle_display": v_meta["vehicle_display"],
			"vehicle_compact_display": v_meta["vehicle_compact_display"],
			"vehicle_color": v_meta["vehicle_color"],
			"license_plate": v_meta["license_plate"],
			"vin": v_meta["vin"],
			"model_year": v_meta["model_year"],
			"manufacturer": v_meta["manufacturer"],
			"model": v_meta["model"],
			"vehicle_generation": v_meta["vehicle_generation"],
			"recommended_manual_url": v_meta["recommended_manual_url"],
			"vehicle_info": v_meta["vehicle_info"],
			"customer_name": customer_map.get(str(ro.get("customer") or ""), str(ro.get("customer") or "")),
			"bay": sched.get("service_bay") or "",
			"scheduled_date": str(sched["scheduled_date"]) if sched.get("scheduled_date") else None,
			"scheduled_time": str(sched["scheduled_time"]) if sched.get("scheduled_time") else None,
			"estimated_duration": cint(sched.get("estimated_duration") or 0) or None,
			"odometer_in": cint(ro.get("odometer_in")) if ro.get("odometer_in") is not None else None,
			"odometer_out": cint(ro.get("odometer_out")) if ro.get("odometer_out") is not None else None,
			"approved_count": approved_count,
			"completed_count": completed_count_svc,
			"progress_percent": flt(ro.get("progress_percent"), 1),
			"picked_up_at": str(ro["picked_up_at"]) if ro.get("picked_up_at") else None,
			"parked_at": str(ro["parked_at"]) if ro.get("parked_at") else None,
			"has_pin": bool(ro.get("pin_to_drive_status") == "Provided"),
			"thread_key": t_key,
			"board_status": t_row.get("board_status") or "ACTIVE",
			"open_exception_count": cint(t_row.get("open_action_count") or 0),
			"board_message_count": cint(msg_map.get(t_key, 0)),
			"_sort_modified": str(ro.get("parked_at") or ro.get("modified") or ""),
		}
		if not clean_query or _matches_query(card, clean_query):
			jobs.append(card)

	for se in target_se_rows:
		se_id = str(se["name"])
		if se.get("repair_vehicle") and str(se["repair_vehicle"]) in vehicle_map:
			v_meta = vehicle_map[str(se["repair_vehicle"])]
		else:
			v_meta = _format_provisional_vehicle(se)

		cust_name = (
			customer_map.get(str(se.get("customer") or ""))
			or str(se.get("provisional_customer_name") or "").strip()
			or str(se.get("customer") or "").strip()
		)
		card = {
			"kind": "SCHEDULE_ENTRY",
			"id": se_id,
			"status": se.get("status") or "",
			"entry_type": se.get("entry_type") or "",
			"waiter_preference": se.get("waiter_preference") or "",
			"vehicle_display": v_meta["vehicle_display"],
			"vehicle_compact_display": v_meta["vehicle_compact_display"],
			"vehicle_color": v_meta["vehicle_color"],
			"license_plate": v_meta["license_plate"],
			"vin": v_meta["vin"],
			"model_year": v_meta["model_year"],
			"manufacturer": v_meta["manufacturer"],
			"model": v_meta["model"],
			"vehicle_generation": v_meta["vehicle_generation"],
			"recommended_manual_url": v_meta["recommended_manual_url"],
			"vehicle_info": v_meta["vehicle_info"],
			"customer_name": cust_name,
			"bay": se.get("service_bay") or "",
			"scheduled_date": str(se["scheduled_date"]) if se.get("scheduled_date") else None,
			"scheduled_time": str(se["scheduled_time"]) if se.get("scheduled_time") else None,
			"estimated_duration": cint(se.get("estimated_duration") or 0) or None,
			"has_pin": bool(se.get("pin_to_drive_status") == "Provided"),
			"_sort_modified": str(se.get("modified") or ""),
		}
		if not clean_query or _matches_query(card, clean_query):
			jobs.append(card)

	if show_completed:
		jobs.sort(key=lambda c: (c.get("_sort_modified") or "", c.get("id") or ""), reverse=True)
	else:
		def _active_sort_key(c: dict[str, Any]) -> tuple[int, str, str, str]:
			if c["kind"] == "REPAIR_ORDER":
				ls = c.get("lifecycle_state")
				prio = 0 if ls in ("IN_BAY_WORKING", "READY_FOR_DROPOFF") else 1
			else:
				prio = 2
			return (
				prio,
				c.get("scheduled_date") or "9999-12-31",
				c.get("scheduled_time") or "23:59:59",
				c.get("id") or "",
			)

		jobs.sort(key=_active_sort_key)

	for c in jobs:
		c.pop("_sort_modified", None)

	return {
		"ok": True,
		"actor_role": actor_role,
		"employee_linked": bool(emp),
		"tech_id": emp,
		"active_count": active_count,
		"completed_count": completed_count,
		"jobs": jobs[:safe_limit],
	}


@frappe.whitelist()
def get_tech_repair_order(repair_order: str) -> dict[str, Any]:
	"""Return price-free Layer 2B execution detail for an assigned, approved, ready Repair Order."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	clean_ro = require_str(repair_order, "repair_order")
	assert_can_access_ro(clean_ro, user=caller_user)

	ro = frappe.get_doc("Repair Order", clean_ro)
	ro.check_permission("read")

	with _as_system_user():
		veh_row = (
			frappe.db.get_value(
				"Repair Vehicle",
				ro.vehicle,
				[
					"name",
					"display_title",
					"color",
					"model_year",
					"manufacturer",
					"model",
					"license_plate",
					"vin",
					"trim",
					"drivetrain",
					"drive_unit",
					"manufactured_month",
					"vehicle_generation",
					"recommended_manual_url",
				],
				as_dict=True,
			)
			if ro.vehicle
			else None
		)
		v_meta = _format_vehicle_from_row(veh_row)

		customer_name = (
			frappe.db.get_value("Customer", ro.customer, "customer_name")
			if ro.customer
			else None
		) or (ro.customer or "")

		sched = _fetch_ro_schedule_map([ro.name]).get(ro.name) or {}

		t_key = ro_thread_key(ro.name)
		t_row = (
			frappe.db.get_value(
				"Shop Board Thread",
				t_key,
				[
					"board_status",
					"blocked_reason",
					"blocked_since",
					"open_action_count",
					"last_event_preview",
					"last_event_at",
				],
				as_dict=True,
			)
			if frappe.db.exists("Shop Board Thread", t_key)
			else None
		)
		msg_count = cint(
			frappe.db.sql(
				"""
				SELECT COUNT(*)
				FROM `tabShop Board Event`
				WHERE thread = %s
				  AND IFNULL(payload_type, '') != 'STATE_MUTATION'
				""",
				(t_key,),
			)[0][0]
		)

		unique_part_codes: list[str] = []
		seen_part_codes: set[str] = set()
		for row in getattr(ro, "services", []) or []:
			if (row.status or "").strip() != "Approved":
				continue
			for entry in _parse_json_safe(getattr(row, "parts", None), []):
				if not isinstance(entry, dict):
					continue
				code = str(entry.get("item_code") or "").strip()
				if code and code not in seen_part_codes:
					seen_part_codes.add(code)
					unique_part_codes.append(code)

		bin_stock_map: dict[str, dict[str, Any]] = {}
		if unique_part_codes:
			try:
				from ism.api.parts_stock import _resolve_default_warehouse, get_items_bin_stock_batch
			except ImportError:
				_resolve_default_warehouse = None
				get_items_bin_stock_batch = None
			if get_items_bin_stock_batch is not None:
				wh = _resolve_default_warehouse(getattr(ro, "company", None)) if _resolve_default_warehouse else None
				bin_stock_map = get_items_bin_stock_batch(unique_part_codes, warehouse=wh) or {}

	approved_services: list[dict[str, Any]] = []
	pending_services: list[dict[str, Any]] = []
	declined_count = 0

	for row in getattr(ro, "services", []) or []:
		row_status = (row.status or "").strip()
		if row_status == "Approved":
			approved_services.append(
				{
					"name": row.name,
					"idx": cint(row.idx),
					"service_title": row.service_title or row.service_package or row.name,
					"service_position": row.service_position or "",
					"correction_code": row.correction_code or "",
					"labor_hours": flt(row.labor_hours, 2),
					"is_completed": cint(row.is_completed),
					"completed_at": str(row.completed_at) if getattr(row, "completed_at", None) else None,
					"completed_by": getattr(row, "completed_by", None) or None,
					"is_supplement": cint(getattr(row, "is_supplement", 0)),
					"advisor_notes": getattr(row, "advisor_notes", None) or "",
					"applicable_generation": getattr(row, "applicable_generation", None) or "",
					"tesla_manual_url": getattr(row, "tesla_manual_url", None) or "",
					"parts": _parse_service_parts_no_price(getattr(row, "parts", None), bin_stock_map=bin_stock_map),
				}
			)
		elif row_status == "Declined":
			declined_count += 1
		else:
			pending_services.append(
				{
					"name": row.name,
					"idx": cint(row.idx),
					"service_title": row.service_title or row.service_package or row.name,
					"service_position": row.service_position or "",
					"labor_hours": flt(row.labor_hours, 2),
					"status": row_status or "Draft",
					"is_supplement": cint(getattr(row, "is_supplement", 0)),
				}
			)

	approved_count = len(approved_services)
	completed_count = sum(1 for s in approved_services if cint(s["is_completed"]))
	lifecycle_state = _compute_ro_lifecycle_state(
		ro.status,
		ro.vehicle_location,
		approved_count,
		completed_count,
		pending_count=len(pending_services),
	)

	inspection_items: list[dict[str, Any]] = []
	for item in getattr(ro, "inspection_items", []) or []:
		inspection_items.append(
			{
				"name": item.name,
				"idx": cint(item.idx),
				"inspection_description": item.inspection_description or "",
				"status": item.status or "Pending",
				"condition": getattr(item, "condition", None) or "",
				"notes": getattr(item, "notes", None) or "",
				"inspected_by": getattr(item, "inspected_by", None) or None,
				"inspected_at": str(item.inspected_at) if getattr(item, "inspected_at", None) else None,
			}
		)

	can_post, read_only_reason = _compute_capture_lock(ro.status)
	exception_summary = {
		"thread_key": t_key,
		"board_status": (t_row.board_status if t_row else None) or "ACTIVE",
		"blocked_reason": (t_row.blocked_reason if t_row else None) or "",
		"blocked_since": str(t_row.blocked_since) if (t_row and t_row.blocked_since) else None,
		"open_action_count": cint(t_row.open_action_count) if t_row else 0,
		"message_count": msg_count,
		"last_message_preview": (t_row.last_event_preview if t_row else None) or "",
		"last_message_at": str(t_row.last_event_at) if (t_row and t_row.last_event_at) else None,
		"can_post": can_post,
		"read_only_reason": read_only_reason,
	}

	pin_state = _get_ro_pin_state_safe(ro.name)

	return {
		"ok": True,
		"actor_role": actor_role,
		"repair_order": {
			"name": ro.name,
			"status": ro.status or "",
			"approval_status": ro.approval_status or "",
			"lifecycle_state": lifecycle_state,
			"vehicle_location": ro.vehicle_location or "In Customer Lot",
			"vehicle_display": v_meta["vehicle_display"],
			"vehicle_compact_display": v_meta["vehicle_compact_display"],
			"vehicle_color": v_meta["vehicle_color"],
			"license_plate": v_meta["license_plate"],
			"vin": v_meta["vin"],
			"model_year": v_meta["model_year"],
			"manufacturer": v_meta["manufacturer"],
			"model": v_meta["model"],
			"trim": v_meta["trim"],
			"drivetrain": v_meta["drivetrain"],
			"vehicle_generation": v_meta["vehicle_generation"],
			"recommended_manual_url": v_meta["recommended_manual_url"],
			"vehicle_info": v_meta["vehicle_info"],
			"customer_name": customer_name,
			"bay": sched.get("service_bay") or "",
			"scheduled_date": str(sched["scheduled_date"]) if sched.get("scheduled_date") else None,
			"scheduled_time": str(sched["scheduled_time"]) if sched.get("scheduled_time") else None,
			"estimated_duration": cint(sched.get("estimated_duration") or 0) or None,
			"odometer_in": cint(ro.odometer_in) if ro.odometer_in is not None else None,
			"odometer_out": cint(ro.odometer_out) if ro.odometer_out is not None else None,
			"picked_up_at": str(ro.picked_up_at) if ro.picked_up_at else None,
			"picked_up_by": ro.picked_up_by or None,
			"parked_at": str(ro.parked_at) if ro.parked_at else None,
			"parked_by": ro.parked_by or None,
			"total_bay_hours": flt(ro.total_bay_hours, 2),
			"progress_percent": flt(ro.progress_percent, 1),
			"approved_count": approved_count,
			"completed_count": completed_count,
			"customer_concern": ro.customer_concern or "",
			"services": approved_services,
			"pending_services": pending_services,
			"declined_count": declined_count,
			"inspection_items": inspection_items,
			"desk_url": f"/app/repair-order/{quote(ro.name, safe='')}",
		},
		"pin_state": pin_state,
		"exception_summary": exception_summary,
	}


@frappe.whitelist()
def get_tech_schedule_entry(schedule_entry: str) -> dict[str, Any]:
	"""Return Layer 2A pre-RO diagnostic detail for an assigned Schedule Entry, or superseding RO notice."""
	actor_role = resolve_actor_role()
	caller_user = frappe.session.user
	clean_se = require_str(schedule_entry, "schedule_entry")
	assert_can_access_schedule_entry(clean_se, user=caller_user)

	se = frappe.get_doc("Schedule Entry", clean_se)
	se.check_permission("read")

	if se.repair_order:
		return {
			"ok": True,
			"actor_role": actor_role,
			"superseded_by_ro": se.repair_order,
			"ro_eligible": bool(is_ro_visible_to_user(se.repair_order, user=caller_user)),
			"schedule_entry": {
				"name": se.name,
				"status": se.status or "",
				"repair_order": se.repair_order,
			},
		}

	with _as_system_user():
		veh_row = (
			frappe.db.get_value(
				"Repair Vehicle",
				se.repair_vehicle,
				[
					"name",
					"display_title",
					"color",
					"model_year",
					"manufacturer",
					"model",
					"license_plate",
					"vin",
					"trim",
					"drivetrain",
					"drive_unit",
					"manufactured_month",
					"vehicle_generation",
					"recommended_manual_url",
				],
				as_dict=True,
			)
			if se.repair_vehicle
			else None
		)
		v_meta = _format_vehicle_from_row(veh_row) if veh_row else _format_provisional_vehicle(se)

		customer_name = (
			(frappe.db.get_value("Customer", se.customer, "customer_name") if se.customer else None)
			or str(getattr(se, "provisional_customer_name", "") or "").strip()
			or str(se.customer or "").strip()
		)

	pin_state = _get_se_pin_state_safe(se.name)

	return {
		"ok": True,
		"actor_role": actor_role,
		"superseded_by_ro": None,
		"ro_eligible": False,
		"schedule_entry": {
			"name": se.name,
			"entry_type": se.entry_type or "",
			"status": se.status or "",
			"waiter_preference": getattr(se, "waiter_preference", None) or "",
			"vehicle_display": v_meta["vehicle_display"],
			"vehicle_compact_display": v_meta["vehicle_compact_display"],
			"vehicle_color": v_meta["vehicle_color"],
			"license_plate": v_meta["license_plate"],
			"vin": v_meta["vin"],
			"model_year": v_meta["model_year"],
			"manufacturer": v_meta["manufacturer"],
			"model": v_meta["model"],
			"vehicle_generation": v_meta["vehicle_generation"],
			"recommended_manual_url": v_meta["recommended_manual_url"],
			"vehicle_info": v_meta["vehicle_info"],
			"customer_name": customer_name,
			"bay": se.service_bay or "",
			"scheduled_date": str(se.scheduled_date) if se.scheduled_date else None,
			"scheduled_time": str(se.scheduled_time) if se.scheduled_time else None,
			"estimated_duration": cint(se.estimated_duration or 0) or None,
			"notes": se.notes or "",
			"items_summary": getattr(se, "items_summary", None) or "",
			"booked_packages": _parse_booked_packages_no_price(getattr(se, "booked_packages_json", None)),
			"desk_url": f"/app/schedule-entry/{quote(se.name, safe='')}",
		},
		"pin_state": pin_state,
	}


@frappe.whitelist()
def pick_up_vehicle(repair_order: str) -> dict[str, Any]:
	"""Move an assigned, approved Repair Order vehicle from customer lot into the bay."""
	resolve_actor_role()
	clean_ro = require_str(repair_order, "repair_order")
	assert_can_access_ro(clean_ro, user=frappe.session.user)

	ro = frappe.get_doc("Repair Order", clean_ro)
	ro.check_permission("write")
	ro.pick_up_vehicle()
	return get_tech_repair_order(clean_ro)


@frappe.whitelist()
def toggle_service_item(
	repair_order: str,
	service_row_id: str,
	is_completed: Any,
) -> dict[str, Any]:
	"""Toggle completion state of an approved service row on an assigned Repair Order."""
	resolve_actor_role()
	clean_ro = require_str(repair_order, "repair_order")
	clean_row = require_str(service_row_id, "service_row_id")
	assert_can_access_ro(clean_ro, user=frappe.session.user)

	ro = frappe.get_doc("Repair Order", clean_ro)
	ro.check_permission("write")
	completed_flag = 1 if coerce_bool(is_completed, default=False) else 0
	ro.toggle_service_completion(service_row_id=clean_row, is_completed=completed_flag)
	return get_tech_repair_order(clean_ro)


@frappe.whitelist()
def drop_off_vehicle(
	repair_order: str,
	odometer_out: Any = None,
	temporary: Any = False,
) -> dict[str, Any]:
	"""Park vehicle back in customer lot (final or temporary). Strictly requires odometer_out >= odometer_in."""
	resolve_actor_role()
	clean_ro = require_str(repair_order, "repair_order")
	assert_can_access_ro(clean_ro, user=frappe.session.user)

	ro = frappe.get_doc("Repair Order", clean_ro)
	ro.check_permission("write")
	parsed_odo = _parse_required_odometer(odometer_out, ro.odometer_in)
	# Both final drop-off and temporary mid-job park enforce require_odometer=True
	_is_temporary = coerce_bool(temporary, default=False)
	ro.park_vehicle(odometer_out=parsed_odo, require_odometer=True, temporary=_is_temporary)
	return get_tech_repair_order(clean_ro)


@frappe.whitelist()
def update_inspection_item(
	repair_order: str,
	item_row_id: str,
	status: str,
	notes: str | None = None,
) -> dict[str, Any]:
	"""Update an intake inspection checklist item on an assigned Repair Order."""
	resolve_actor_role()
	clean_ro = require_str(repair_order, "repair_order")
	clean_row = require_str(item_row_id, "item_row_id")
	clean_status = require_str(status, "status")
	clean_notes = optional_str(notes, "notes")
	assert_can_access_ro(clean_ro, user=frappe.session.user)

	from ism.api.inspection import record_check_in_inspection

	record_check_in_inspection(
		repair_order=clean_ro,
		items=[{"name": clean_row, "status": clean_status, "notes": clean_notes}],
	)
	return get_tech_repair_order(clean_ro)


@frappe.whitelist()
def reveal_schedule_entry_pin(schedule_entry: str) -> dict[str, Any]:
	"""Staff-only audited reveal of a Schedule Entry PIN-to-Drive for assigned technicians/managers."""
	resolve_actor_role()
	clean_se = require_str(schedule_entry, "schedule_entry")
	assert_can_access_schedule_entry(clean_se, user=frappe.session.user)

	try:
		from ism.portal.pin_vault import get_schedule_entry_pin_for_staff
	except ImportError:
		return {
			"ok": True,
			"available": False,
			"pin_feature_installed": False,
			"schedule_entry": clean_se,
		}

	res = get_schedule_entry_pin_for_staff(clean_se, channel="Tech Workbench")
	if isinstance(res, dict):
		return {
			"ok": True,
			"available": True,
			"pin_feature_installed": True,
			**res,
		}
	return {
		"ok": True,
		"available": False,
		"pin_feature_installed": True,
		"schedule_entry": clean_se,
	}


__all__ = [
	"drop_off_vehicle",
	"get_tech_repair_order",
	"get_tech_schedule_entry",
	"list_my_jobs",
	"pick_up_vehicle",
	"reveal_schedule_entry_pin",
	"toggle_service_item",
	"update_inspection_item",
]
