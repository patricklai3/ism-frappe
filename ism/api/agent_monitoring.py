# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, flt, get_url, getdate, now_datetime, today

from ism.api._validation import (
	get_active_company,
	optional_str,
	parse_json_arg,
	require_int,
	require_str,
)
from ism.api.agent_serializers import build_agent_envelope, build_ro_urls
from ism.api.parts_stock import (
	_get_items_stock_qty_map,
	_parse_parts_json,
	_resolve_default_warehouse,
)
from ism.api.technician_availability import (
	get_active_technicians,
	get_daily_technician_overview,
)
from ism.scheduling.capacity import INACTIVE_WORK_STATUSES, load_bay_tags_map
from ism.scheduling.entry_queries import build_holiday_checker
from ism.scheduling.estimation_light import DEFAULT_SIGMA, estimate_total_duration
from ism.scheduling.time_utils import get_shop_settings, normalize_date, normalize_time

_ACTIVE_RO_STATUSES: Tuple[str, ...] = (
	"Checked In",
	"Draft Estimate",
	"Pending Authorization",
	"Authorized - In Progress",
	"Waiting on Parts",
	"Work Completed",
)
_APPROVED_RO_STATES = ("Approved", "Partially Approved")
_VALID_HEALTH_GRADES = ("Healthy", "Watch", "Critical")


def _fmt_dt(val: Any) -> Optional[str]:
	if val is None or val == "":
		return None
	text = str(val).strip()
	return text or None


def _fmt_time(val: Any) -> Optional[str]:
	if val is None or val == "":
		return None
	return normalize_time(val)


def _resolve_next_open_date(target_date: str, holiday_list: Optional[str]) -> str:
	start_d = getdate(target_date)
	is_holiday_fn = build_holiday_checker(holiday_list, target_date)
	for offset in range(1, 15):
		cand = add_days(start_d, offset)
		if not is_holiday_fn(cand):
			return str(cand)
	return str(add_days(start_d, 1))


def _load_ism_monitoring_settings() -> Dict[str, Any]:
	shop = get_shop_settings()
	raw_pct = 80.0
	sigma = DEFAULT_SIGMA
	buffer_minutes = 0
	default_labor_rate = 140.0
	expiry_hours = 48

	if frappe.db.exists("DocType", "ISM Settings"):
		try:
			doc = frappe.get_single("ISM Settings")
			raw_pct = flt(getattr(doc, "estimation_percentile", 80.0) or 80.0)
			sigma = flt(getattr(doc, "estimation_sigma", DEFAULT_SIGMA) or DEFAULT_SIGMA)
			buffer_minutes = cint(getattr(doc, "buffer_padding_minutes", 0) or 0)
			default_labor_rate = flt(getattr(doc, "default_labor_rate", 140.0) or 140.0)
			expiry_hours = cint(getattr(doc, "authorization_token_expiry_hours", 48) or 48)
		except frappe.DoesNotExistError:
			pass

	percentile = raw_pct / 100.0 if raw_pct > 1.0 else raw_pct
	percentile = min(max(percentile, 0.50), 0.99)
	sigma = min(max(sigma, 0.01), 2.0)

	return {
		"operating_hours_start": _fmt_time(shop.get("operating_hours_start")) or "08:00:00",
		"operating_hours_end": _fmt_time(shop.get("operating_hours_end")) or "17:00:00",
		"break_start": _fmt_time(shop.get("break_start")),
		"break_end": _fmt_time(shop.get("break_end")),
		"default_slot_interval": cint(shop.get("default_slot_interval") or 30),
		"holiday_list": shop.get("holiday_list"),
		"estimation_percentile": percentile,
		"estimation_sigma": sigma,
		"buffer_padding_minutes": max(0, buffer_minutes),
		"default_labor_rate": default_labor_rate,
		"authorization_token_expiry_hours": max(1, expiry_hours),
	}


def _compute_batch_parts_summaries(
	active_ros: List[Dict[str, Any]],
	ro_required_parts: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, Any]]:
	"""Compute FIFO stock availability and open PO coverage for active approved ROs in batch."""
	active_ro_names = {r["name"] for r in active_ros}
	fifo_ros = frappe.get_all(
		"Repair Order",
		filters={
			"docstatus": ["<", 2],
			"status": ["not in", ["Closed / Released", "Cancelled"]],
			"approval_status": ["in", list(_APPROVED_RO_STATES)],
		},
		fields=["name", "company", "creation"],
		order_by="creation asc, name asc",
	)

	fifo_ro_names = [r["name"] for r in fifo_ros]
	missing_ro_names = [n for n in fifo_ro_names if n not in ro_required_parts]
	if missing_ro_names:
		extra_svcs = frappe.get_all(
			"Repair Order Service",
			filters={
				"parent": ["in", missing_ro_names],
				"parenttype": "Repair Order",
				"status": "Approved",
			},
			fields=["parent", "parts"],
		)
		for svc in extra_svcs:
			req_map = ro_required_parts.setdefault(svc["parent"], {})
			for p in _parse_parts_json(svc.get("parts")):
				if not isinstance(p, dict):
					continue
				if str(p.get("part_source") or "").strip() == "Customer Supplied":
					continue
				code = p.get("item_code")
				if code and isinstance(code, str):
					req_map[code] = req_map.get(code, 0.0) + flt(p.get("qty", 1.0))

	all_codes: List[str] = list(
		dict.fromkeys(
			code
			for req_map in ro_required_parts.values()
			for code in req_map
		)
	)
	stock_item_meta: Dict[str, str] = {}
	if all_codes:
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", all_codes], "is_stock_item": 1},
			fields=["name", "item_name"],
		):
			stock_item_meta[row["name"]] = row.get("item_name") or row["name"]

	valid_codes = sorted(stock_item_meta.keys())
	wh_by_company: Dict[str, Optional[str]] = {}
	actual_by_wh: Dict[Optional[str], Dict[str, float]] = {}

	def _warehouse_for(company: Optional[str]) -> Optional[str]:
		if company not in wh_by_company:
			wh_by_company[company or ""] = _resolve_default_warehouse(company)
		return wh_by_company[company or ""]

	if valid_codes:
		for comp in {r.get("company") for r in active_ros + fifo_ros}:
			wh = _warehouse_for(comp)
			if wh not in actual_by_wh:
				actual_by_wh[wh] = {
					k: flt(v)
					for k, v in _get_items_stock_qty_map(valid_codes, warehouse=wh).items()
				}

	# Batch-fetch open unreceived Purchase Order quantities linked to active ROs.
	open_po_qty_map: Dict[Tuple[str, str], float] = {}
	if active_ro_names and valid_codes and frappe.db.has_column("Purchase Order", "repair_order"):
		pos = frappe.get_all(
			"Purchase Order",
			filters={
				"docstatus": 1,
				"status": ["not in", ["Closed", "Cancelled"]],
				"repair_order": ["in", list(active_ro_names)],
			},
			fields=["name", "repair_order"],
		)
		po_to_ro = {po["name"]: po["repair_order"] for po in pos if po.get("repair_order")}
		if po_to_ro:
			po_items = frappe.get_all(
				"Purchase Order Item",
				filters={
					"parent": ["in", list(po_to_ro.keys())],
					"parenttype": "Purchase Order",
					"item_code": ["in", valid_codes],
				},
				fields=["parent", "item_code", "qty", "received_qty"],
			)
			for poi in po_items:
				ro_id = po_to_ro.get(poi["parent"])
				code = poi.get("item_code")
				rem = max(0.0, flt(poi.get("qty")) - flt(poi.get("received_qty")))
				if ro_id and code and rem > 0:
					key = (ro_id, code)
					open_po_qty_map[key] = open_po_qty_map.get(key, 0.0) + rem

	# Walk approved ROs in strict FIFO order, deducting stock commitments per warehouse.
	remaining_stock_by_wh: Dict[Optional[str], Dict[str, float]] = {
		wh: dict(stock_map) for wh, stock_map in actual_by_wh.items()
	}
	fifo_available_by_ro: Dict[str, Dict[str, float]] = {}

	for cro in fifo_ros:
		ro_id = cro["name"]
		wh = _warehouse_for(cro.get("company"))
		wh_stock = remaining_stock_by_wh.setdefault(wh, {})
		req_map = ro_required_parts.get(ro_id, {})
		ro_avail: Dict[str, float] = {}
		for code, req_qty in req_map.items():
			if code not in stock_item_meta:
				continue
			avail_now = max(0.0, flt(wh_stock.get(code, 0.0)))
			ro_avail[code] = avail_now
			wh_stock[code] = max(0.0, avail_now - flt(req_qty))
		if ro_id in active_ro_names:
			fifo_available_by_ro[ro_id] = ro_avail

	summaries: Dict[str, Dict[str, Any]] = {}
	for ro in active_ros:
		ro_id = ro["name"]
		is_approved = ro.get("approval_status") in _APPROVED_RO_STATES
		req_map = ro_required_parts.get(ro_id, {})
		stock_reqs = {code: flt(qty) for code, qty in req_map.items() if code in stock_item_meta and flt(qty) > 0}
		if not is_approved or not stock_reqs:
			summaries[ro_id] = {
				"required_count": len(stock_reqs),
				"deficit_items": [],
				"has_stock_deficit": False,
				"has_uncovered_deficit": False,
				"all_parts_ready": True,
			}
			continue

		wh = _warehouse_for(ro.get("company"))
		ro_avail_map = fifo_available_by_ro.get(ro_id, {})
		deficit_items: List[Dict[str, Any]] = []
		has_stock_deficit = False
		has_uncovered_deficit = False

		for code, req_qty in sorted(stock_reqs.items()):
			if ro_id in fifo_available_by_ro:
				avail_qty = flt(ro_avail_map.get(code, 0.0))
			else:
				avail_qty = max(0.0, flt(remaining_stock_by_wh.get(wh, {}).get(code, 0.0)))
			open_po_qty = flt(open_po_qty_map.get((ro_id, code), 0.0))
			shortfall = max(0.0, req_qty - avail_qty)
			uncovered = max(0.0, shortfall - open_po_qty)
			if shortfall > 1e-6:
				has_stock_deficit = True
				if uncovered > 1e-6:
					has_uncovered_deficit = True
				deficit_items.append({
					"item_code": code,
					"item_name": stock_item_meta.get(code, code),
					"required_qty": round(req_qty, 3),
					"available_qty": round(avail_qty, 3),
					"open_po_qty": round(open_po_qty, 3),
					"shortfall_qty": round(shortfall, 3),
					"uncovered_qty": round(uncovered, 3),
				})

		summaries[ro_id] = {
			"required_count": len(stock_reqs),
			"deficit_items": deficit_items,
			"has_stock_deficit": has_stock_deficit,
			"has_uncovered_deficit": has_uncovered_deficit,
			"all_parts_ready": not has_stock_deficit,
		}

	return summaries


@frappe.whitelist()
def get_cockpit_snapshot(
	date: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return batched live shop telemetry, dual-resource schedule, action overlays, and cached scores."""
	for dt in ("Service Bay", "Schedule Entry", "Repair Order", "Shop Action Item", "Shop Health Snapshot"):
		frappe.has_permission(dt, "read", throw=True)

	raw_date = optional_str(date, "Date")
	target_date = normalize_date(raw_date) if raw_date else today()
	settings = _load_ism_monitoring_settings()
	next_open_date = _resolve_next_open_date(target_date, settings.get("holiday_list"))
	base_url = get_url()
	server_now = now_datetime()

	bays_raw = frappe.get_list(
		"Service Bay",
		filters={"is_active": 1},
		fields=["name", "bay_name", "description"],
		order_by="name asc",
	)
	bay_tags_map = load_bay_tags_map([b["name"] for b in bays_raw])
	bays = [
		{
			"name": b["name"],
			"bay_name": b.get("bay_name") or b["name"],
			"description": b.get("description"),
			"equipment_tags": bay_tags_map.get(b["name"], []),
			"desk_url": f"{base_url}/app/service-bay/{quote(b['name'], safe='')}",
		}
		for b in bays_raw
	]

	tech_overview = get_daily_technician_overview(target_date)
	next_open_techs = get_active_technicians(next_open_date)

	raw_entries = frappe.get_list(
		"Schedule Entry",
		filters={"scheduled_date": ["in", [target_date, next_open_date]], "status": ["!=", "Cancelled"]},
		fields=[
			"name",
			"entry_type",
			"status",
			"scheduled_date",
			"scheduled_time",
			"estimated_duration",
			"completion_date",
			"completion_time",
			"repair_order",
			"customer",
			"provisional_customer_name",
			"repair_vehicle",
			"provisional_vehicle_info",
			"service_bay",
			"assigned_technician",
			"items_summary",
		],
		order_by="scheduled_date asc, scheduled_time asc",
	)

	active_ros_raw = frappe.get_list(
		"Repair Order",
		filters={"status": ["in", list(_ACTIVE_RO_STATUSES)], "docstatus": ["<", 2]},
		fields=[
			"name",
			"company",
			"customer",
			"vehicle",
			"status",
			"approval_status",
			"approval_token_expiry",
			"vehicle_location",
			"picked_up_at",
			"parked_at",
			"authorized_at",
			"grand_total",
			"outstanding_amount",
			"payment_status",
			"vehicle_released",
			"manual_parts_hold",
			"progress_percent",
			"creation",
			"modified",
		],
		order_by="creation asc, name asc",
	)

	active_ro_names = [r["name"] for r in active_ros_raw]
	ro_services_meta: Dict[str, Dict[str, Any]] = {
		name: {
			"total_services": 0,
			"approved_services": 0,
			"completed_approved_services": 0,
			"unscheduled_approved_services": 0,
			"p80_duration_minutes": 0,
			"_approved_frts": [],
			"_all_frts": [],
		}
		for name in active_ro_names
	}
	ro_required_parts: Dict[str, Dict[str, float]] = {}

	if active_ro_names:
		has_se_col = frappe.db.has_column("Repair Order Service", "schedule_entry")
		svc_fields = ["name", "parent", "service_title", "status", "labor_hours", "is_completed", "parts"]
		if has_se_col:
			svc_fields.append("schedule_entry")
		svc_rows = frappe.get_all(
			"Repair Order Service",
			filters={"parent": ["in", active_ro_names], "parenttype": "Repair Order"},
			fields=svc_fields,
		)
		linked_se_names = list(
			{s.get("schedule_entry") for s in svc_rows if has_se_col and s.get("schedule_entry")}
		)
		linked_se_status: Dict[str, str] = {}
		if linked_se_names:
			for row in frappe.get_all(
				"Schedule Entry",
				filters={"name": ["in", linked_se_names]},
				fields=["name", "status"],
			):
				linked_se_status[row["name"]] = row.get("status") or ""

		for svc in svc_rows:
			ro_id = svc["parent"]
			meta = ro_services_meta[ro_id]
			meta["total_services"] += 1
			frt_min = max(0.0, flt(svc.get("labor_hours")) * 60.0)
			status_val = svc.get("status")
			if status_val != "Declined" and frt_min > 0:
				meta["_all_frts"].append({"flat_rate_minutes": frt_min, "sigma": settings["estimation_sigma"]})
			if status_val == "Approved":
				meta["approved_services"] += 1
				if cint(svc.get("is_completed")):
					meta["completed_approved_services"] += 1
				if frt_min > 0:
					meta["_approved_frts"].append({"flat_rate_minutes": frt_min, "sigma": settings["estimation_sigma"]})
				se_name = svc.get("schedule_entry") if has_se_col else None
				if not se_name or linked_se_status.get(se_name) in INACTIVE_WORK_STATUSES:
					meta["unscheduled_approved_services"] += 1

				req_map = ro_required_parts.setdefault(ro_id, {})
				for p in _parse_parts_json(svc.get("parts")):
					if not isinstance(p, dict):
						continue
					if str(p.get("part_source") or "").strip() == "Customer Supplied":
						continue
					code = p.get("item_code")
					if code and isinstance(code, str):
						req_map[code] = req_map.get(code, 0.0) + flt(p.get("qty", 1.0))

		for ro_id, meta in ro_services_meta.items():
			approved_frts = meta.pop("_approved_frts", [])
			all_frts = meta.pop("_all_frts", [])
			frts = approved_frts or all_frts
			if frts:
				p_pure = estimate_total_duration(
					frts,
					sigma=settings["estimation_sigma"],
					percentile=settings["estimation_percentile"],
				)
				meta["p80_duration_minutes"] = int(p_pure + settings["buffer_padding_minutes"])
			else:
				meta["p80_duration_minutes"] = 0

	# Active or future booking lookup for active ROs.
	ro_has_future_booking: set[str] = set()
	if active_ro_names:
		for row in frappe.get_all(
			"Schedule Entry",
			filters={
				"repair_order": ["in", active_ro_names],
				"scheduled_date": [">=", target_date],
				"status": ["not in", ["Cancelled", "No Show", "Completed"]],
			},
			fields=["repair_order"],
		):
			if row.get("repair_order"):
				ro_has_future_booking.add(row["repair_order"])

	parts_summaries = _compute_batch_parts_summaries(active_ros_raw, ro_required_parts)

	ro_p80_map: Dict[str, int] = {}
	active_repair_orders: List[Dict[str, Any]] = []
	expiry_hours = settings["authorization_token_expiry_hours"]
	for ro in active_ros_raw:
		ro_id = ro["name"]
		svc_meta = ro_services_meta.get(ro_id, {})
		p80_min = cint(svc_meta.get("p80_duration_minutes", 0))
		ro_p80_map[ro_id] = p80_min
		sent_at = (
			add_to_date(ro["approval_token_expiry"], hours=-expiry_hours)
			if ro.get("approval_token_expiry")
			else ro.get("modified")
		)
		urls = build_ro_urls(ro_id)
		active_repair_orders.append({
			"name": ro_id,
			"company": ro.get("company"),
			"customer": ro.get("customer"),
			"vehicle": ro.get("vehicle"),
			"status": ro.get("status"),
			"approval_status": ro.get("approval_status"),
			"vehicle_location": ro.get("vehicle_location"),
			"picked_up_at": _fmt_dt(ro.get("picked_up_at")),
			"parked_at": _fmt_dt(ro.get("parked_at")),
			"authorized_at": _fmt_dt(ro.get("authorized_at")),
			"approval_sent_at": _fmt_dt(sent_at),
			"grand_total": flt(ro.get("grand_total"), 2),
			"outstanding_amount": flt(ro.get("outstanding_amount"), 2),
			"payment_status": ro.get("payment_status"),
			"vehicle_released": cint(ro.get("vehicle_released")),
			"manual_parts_hold": cint(ro.get("manual_parts_hold")),
			"progress_percent": flt(ro.get("progress_percent"), 1),
			"creation": _fmt_dt(ro.get("creation")),
			"modified": _fmt_dt(ro.get("modified")),
			"total_services": svc_meta.get("total_services", 0),
			"approved_services": svc_meta.get("approved_services", 0),
			"completed_approved_services": svc_meta.get("completed_approved_services", 0),
			"unscheduled_approved_services": svc_meta.get("unscheduled_approved_services", 0),
			"has_active_or_future_booking": ro_id in ro_has_future_booking,
			"p80_duration_minutes": p80_min,
			"parts_summary": parts_summaries.get(ro_id, {}),
			"desk_url": urls["desk"],
		})

	schedule_entries: List[Dict[str, Any]] = []
	for entry in raw_entries:
		ro_id = entry.get("repair_order")
		schedule_entries.append({
			"name": entry["name"],
			"entry_type": entry.get("entry_type"),
			"status": entry.get("status"),
			"scheduled_date": str(entry.get("scheduled_date")) if entry.get("scheduled_date") else None,
			"scheduled_time": _fmt_time(entry.get("scheduled_time")),
			"estimated_duration": cint(entry.get("estimated_duration") or 0),
			"completion_date": str(entry.get("completion_date")) if entry.get("completion_date") else None,
			"completion_time": _fmt_time(entry.get("completion_time")),
			"repair_order": ro_id,
			"customer": entry.get("customer") or entry.get("provisional_customer_name"),
			"vehicle": entry.get("repair_vehicle") or entry.get("provisional_vehicle_info"),
			"service_bay": entry.get("service_bay"),
			"assigned_technician": entry.get("assigned_technician"),
			"items_summary": entry.get("items_summary"),
			"p80_duration_minutes": ro_p80_map.get(ro_id, 0) if ro_id else 0,
			"desk_url": f"{base_url}/app/schedule-entry/{quote(entry['name'], safe='')}",
		})

	raw_actions = frappe.get_list(
		"Shop Action Item",
		filters={"status": ["in", ["Open", "In Progress", "Dismissed"]]},
		fields=[
			"name",
			"title",
			"category",
			"priority",
			"status",
			"source",
			"dedup_key",
			"snoozed_until",
			"reference_doctype",
			"reference_name",
			"item_code",
			"details",
			"reported_by",
			"resolved_by",
			"resolved_at",
			"creation",
			"modified",
		],
		order_by="modified desc",
		limit_page_length=200,
	)
	action_items = [
		{
			**row,
			"snoozed_until": _fmt_dt(row.get("snoozed_until")),
			"resolved_at": _fmt_dt(row.get("resolved_at")),
			"creation": _fmt_dt(row.get("creation")),
			"modified": _fmt_dt(row.get("modified")),
			"desk_url": f"{base_url}/app/shop-action-item/{quote(row['name'], safe='')}",
		}
		for row in raw_actions
		if row.get("status") in ("Open", "In Progress") or bool(row.get("dedup_key"))
	]

	raw_snapshots = frappe.get_list(
		"Shop Health Snapshot",
		fields=[
			"name",
			"metric_key",
			"period_date",
			"score",
			"grade",
			"verdict",
			"report_url",
			"details_json",
			"evaluated_by",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=100,
	)
	health_snapshots: Dict[str, Dict[str, Any]] = {}
	for snap in raw_snapshots:
		key = snap.get("metric_key")
		if not key:
			continue
		if key not in health_snapshots:
			parsed_details = None
			if snap.get("details_json"):
				try:
					parsed_details = json.loads(snap["details_json"])
				except (TypeError, ValueError):
					parsed_details = None
			health_snapshots[key] = {
				"name": snap["name"],
				"metric_key": key,
				"period_date": str(snap.get("period_date")) if snap.get("period_date") else None,
				"score": cint(snap.get("score")),
				"grade": snap.get("grade"),
				"verdict": snap.get("verdict"),
				"report_url": snap.get("report_url"),
				"details": parsed_details,
				"evaluated_by": snap.get("evaluated_by"),
				"creation": _fmt_dt(snap.get("creation")),
				"previous_score": None,
				"delta": None,
			}
		elif health_snapshots[key]["previous_score"] is None:
			prev = cint(snap.get("score"))
			health_snapshots[key]["previous_score"] = prev
			health_snapshots[key]["delta"] = health_snapshots[key]["score"] - prev

	urls = {
		"desk_root": base_url,
		"calendar": f"{base_url}/app/schedule-entry/view/calendar/default",
		"schedule_entries": f"{base_url}/app/schedule-entry",
		"repair_orders": f"{base_url}/app/repair-order",
		"pending_authorization": f"{base_url}/app/repair-order?status=Pending+Authorization",
		"work_completed": f"{base_url}/app/repair-order?status=Work+Completed",
		"pnl_report": f"{base_url}/app/query-report/Profit%20and%20Loss%20Statement",
		"action_items": f"{base_url}/app/shop-action-item",
		"health_snapshots": f"{base_url}/app/shop-health-snapshot",
	}

	return build_agent_envelope(
		success=True,
		urls=urls,
		data={
			"date": target_date,
			"next_open_date": next_open_date,
			"server_now": _fmt_dt(server_now),
			"settings": settings,
			"bays": bays,
			"technician_overview": tech_overview,
			"next_open_technicians": next_open_techs,
			"schedule_entries": schedule_entries,
			"active_repair_orders": active_repair_orders,
			"action_items": action_items,
			"health_snapshots": health_snapshots,
		},
		message=_("Cockpit snapshot retrieved for {0}.").format(target_date),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Cockpit Snapshot ({target_date})\n"
			f"- **Active Bays**: `{len(bays)}` | **Active ROs**: `{len(active_repair_orders)}`\n"
			f"- **Schedule Entries (Today + Next Open)**: `{len(schedule_entries)}`"
		),
	)


@frappe.whitelist()
def get_pnl_digest_inputs(
	from_date: Optional[str] = None,
	to_date: Optional[str] = None,
	company: Optional[str] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Return server-computed P&L, labor rate, parts margin, and unbilled WIP metrics for agent scoring."""
	frappe.has_permission("Repair Order", "read", throw=True)
	frappe.has_permission("Sales Invoice", "read", throw=True)

	raw_to = optional_str(to_date, "to_date")
	raw_from = optional_str(from_date, "from_date")
	end_date = normalize_date(raw_to) if raw_to else today()
	start_date = normalize_date(raw_from) if raw_from else end_date
	if start_date > end_date:
		frappe.throw(_("from_date ({0}) cannot be after to_date ({1}).").format(start_date, end_date), frappe.ValidationError)

	resolved_company = get_active_company(optional_str(company, "Company"), check_permission=True)

	invoices = frappe.get_list(
		"Sales Invoice",
		filters={
			"docstatus": 1,
			"company": resolved_company,
			"posting_date": ["between", [start_date, end_date]],
		},
		fields=["name", "posting_date", "net_total", "grand_total", "outstanding_amount"],
	)
	si_names = [inv["name"] for inv in invoices]
	net_revenue = round(sum(flt(inv.get("net_total")) for inv in invoices), 2)
	grand_total_billed = round(sum(flt(inv.get("grand_total")) for inv in invoices), 2)
	outstanding_receivables = round(sum(flt(inv.get("outstanding_amount")) for inv in invoices), 2)

	labor_revenue = 0.0
	parts_revenue = 0.0
	other_revenue = 0.0
	billed_labor_hours = 0.0
	parts_cogs = 0.0

	if si_names:
		si_items = frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": ["in", si_names], "parenttype": "Sales Invoice"},
			fields=["item_code", "qty", "base_net_amount", "net_amount", "incoming_rate"],
		)
		codes = list({row["item_code"] for row in si_items if row.get("item_code")})
		item_meta: Dict[str, Dict[str, Any]] = {}
		if codes:
			item_fields = ["name", "is_stock_item", "valuation_rate"]
			if frappe.db.has_column("Item", "is_vehicle_part"):
				item_fields.append("is_vehicle_part")
			for row in frappe.get_all("Item", filters={"name": ["in", codes]}, fields=item_fields):
				item_meta[row["name"]] = row

		for row in si_items:
			code = row.get("item_code") or ""
			meta = item_meta.get(code, {})
			amount = flt(row.get("base_net_amount")) or flt(row.get("net_amount"))
			qty = flt(row.get("qty"))
			is_stock = cint(meta.get("is_stock_item")) == 1
			is_part = is_stock or cint(meta.get("is_vehicle_part")) == 1
			is_labor = (code == "LABOR-STD") or (not is_stock and ("labor" in code.lower() or "labour" in code.lower()))
			if is_labor:
				labor_revenue += amount
				billed_labor_hours += qty
			elif is_part:
				parts_revenue += amount
				unit_cost = flt(row.get("incoming_rate")) or flt(meta.get("valuation_rate"))
				parts_cogs += unit_cost * qty
			else:
				other_revenue += amount

	labor_revenue = round(labor_revenue, 2)
	parts_revenue = round(parts_revenue, 2)
	other_revenue = round(other_revenue, 2)
	billed_labor_hours = round(billed_labor_hours, 2)
	parts_cogs = round(parts_cogs, 2)
	effective_labor_rate = round(labor_revenue / billed_labor_hours, 2) if billed_labor_hours > 0 else 0.0
	parts_gross_profit = round(parts_revenue - parts_cogs, 2)
	parts_gross_margin_pct = round((parts_gross_profit / parts_revenue) * 100.0, 2) if parts_revenue > 0 else None

	gl_income = 0.0
	gl_expense = 0.0
	gl_available = False
	if frappe.db.exists("DocType", "GL Entry") and frappe.has_permission("GL Entry", "read"):
		gl_rows = frappe.get_all(
			"GL Entry",
			filters={
				"company": resolved_company,
				"posting_date": ["between", [start_date, end_date]],
				"is_cancelled": 0,
			},
			fields=["account", "debit", "credit"],
		)
		if gl_rows:
			accounts = list({r["account"] for r in gl_rows if r.get("account")})
			acct_root = (
				{
					a["name"]: a.get("root_type")
					for a in frappe.get_all("Account", filters={"name": ["in", accounts]}, fields=["name", "root_type"])
				}
				if accounts
				else {}
			)
			for gl in gl_rows:
				rt = acct_root.get(gl.get("account"))
				if rt == "Income":
					gl_income += flt(gl.get("credit")) - flt(gl.get("debit"))
					gl_available = True
				elif rt == "Expense":
					gl_expense += flt(gl.get("debit")) - flt(gl.get("credit"))
					gl_available = True

	gl_income = round(gl_income, 2)
	gl_expense = round(gl_expense, 2)
	gl_net_profit = round(gl_income - gl_expense, 2) if gl_available else None

	unbilled_ros = frappe.get_list(
		"Repair Order",
		filters={"company": resolved_company, "status": "Work Completed", "docstatus": ["<", 2]},
		fields=["name", "grand_total", "outstanding_amount"],
	)
	unbilled_total = round(sum(flt(r.get("grand_total")) for r in unbilled_ros), 2)

	pnl_url = f"{get_url()}/app/query-report/Profit%20and%20Loss%20Statement"
	data = {
		"company": resolved_company,
		"from_date": start_date,
		"to_date": end_date,
		"invoice_count": len(invoices),
		"net_revenue": net_revenue,
		"grand_total_billed": grand_total_billed,
		"outstanding_receivables": outstanding_receivables,
		"labor_revenue": labor_revenue,
		"parts_revenue": parts_revenue,
		"other_revenue": other_revenue,
		"billed_labor_hours": billed_labor_hours,
		"effective_labor_rate": effective_labor_rate,
		"parts_cogs": parts_cogs,
		"parts_gross_profit": parts_gross_profit,
		"parts_gross_margin_pct": parts_gross_margin_pct,
		"gl_available": gl_available,
		"gl_income": gl_income if gl_available else None,
		"gl_expense": gl_expense if gl_available else None,
		"gl_net_profit": gl_net_profit,
		"unbilled_completed_count": len(unbilled_ros),
		"unbilled_completed_total": unbilled_total,
	}
	return build_agent_envelope(
		success=True,
		urls={"pnl_report": pnl_url},
		data=data,
		message=_("P&L digest inputs computed for {0} to {1}.").format(start_date, end_date),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### P&L Digest ({start_date}..{end_date})\n"
			f"- **Invoices**: `{len(invoices)}` | **Net Revenue**: `${net_revenue:.2f}`\n"
			f"- **ELR**: `${effective_labor_rate:.2f}/hr` | **Unbilled Completed WIP**: `${unbilled_total:.2f}`"
		),
	)


@frappe.whitelist()
def publish_health_snapshot(
	metric_key: str,
	score: Any,
	verdict: str,
	grade: Optional[str] = None,
	period_date: Optional[str] = None,
	report_url: Optional[str] = None,
	details: Optional[Union[Dict[str, Any], str]] = None,
	include_summary: Any = False,
) -> Dict[str, Any]:
	"""Publish a synced 0-100 Shop Health Snapshot for manager cockpits."""
	frappe.has_permission("Shop Health Snapshot", "create", throw=True)
	frappe.has_permission("Shop Health Snapshot", "read", throw=True)

	clean_key = require_str(metric_key, "metric_key")
	score_int = require_int(score, "score", min_val=0, max_val=100)
	clean_verdict = require_str(verdict, "verdict")
	raw_grade = optional_str(grade, "grade")
	if raw_grade is None:
		clean_grade = "Healthy" if score_int >= 80 else ("Watch" if score_int >= 60 else "Critical")
	else:
		if raw_grade not in _VALID_HEALTH_GRADES:
			frappe.throw(
				_("Grade must be one of: {0}.").format(", ".join(_VALID_HEALTH_GRADES)),
				frappe.ValidationError,
			)
		clean_grade = raw_grade

	raw_date = optional_str(period_date, "period_date")
	clean_date = normalize_date(raw_date) if raw_date else today()
	clean_url = optional_str(report_url, "report_url")

	details_payload = None
	if details is not None and not (isinstance(details, str) and not details.strip()):
		details_payload = parse_json_arg(details, label="details")
		if not isinstance(details_payload, (dict, list)):
			frappe.throw(_("Details must be a JSON object or array."), frappe.ValidationError)

	prior_rows = frappe.get_list(
		"Shop Health Snapshot",
		filters={"metric_key": clean_key},
		fields=["name", "score"],
		order_by="creation desc",
		limit_page_length=1,
	)
	previous_score = cint(prior_rows[0]["score"]) if prior_rows else None

	doc = frappe.new_doc("Shop Health Snapshot")
	doc.metric_key = clean_key
	doc.period_date = clean_date
	doc.score = score_int
	doc.grade = clean_grade
	doc.verdict = clean_verdict
	doc.report_url = clean_url
	doc.details_json = json.dumps(details_payload) if details_payload is not None else None
	doc.insert()
	doc.check_permission("read")

	delta = (doc.score - previous_score) if previous_score is not None else None
	base_url = get_url()
	desk_url = f"{base_url}/app/shop-health-snapshot/{quote(doc.name, safe='')}"
	return build_agent_envelope(
		success=True,
		urls={"desk": desk_url, **({"report": clean_url} if clean_url else {})},
		data={
			"name": doc.name,
			"metric_key": doc.metric_key,
			"period_date": str(doc.period_date),
			"score": doc.score,
			"grade": doc.grade,
			"verdict": doc.verdict,
			"report_url": doc.report_url,
			"previous_score": previous_score,
			"delta": delta,
			"evaluated_by": doc.evaluated_by,
		},
		message=_("Published {0} health snapshot ({1}/100, {2}).").format(
			doc.metric_key, doc.score, doc.grade
		),
		include_summary=include_summary,
		summary_builder=lambda: (
			f"### Health Score Published: `{doc.metric_key}`\n"
			f"- **Score**: `{doc.score}/100` ({doc.grade})\n"
			f"- **Verdict**: {doc.verdict}"
		),
	)
