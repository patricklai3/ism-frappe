"""Server-side state-aware triage, gauge, and swimlane engine for /cockpit."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

import frappe

from ism.ism.doctype.shop_action_item.clearing import CLEARING_CONDITION_DESCRIPTIONS

DEAD_LIFT_MINUTES_THRESHOLD = 30
COLD_QUOTE_MINUTES_THRESHOLD = 120
SCORE_STALE_HOURS_THRESHOLD = 24

_PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
_NON_BLOCKING_STATUSES = frozenset({"Cancelled", "No Show", "Completed"})


def _parse_dt(val: Any) -> datetime | None:
	if not val or not isinstance(val, str):
		return None
	text = val.strip()
	if not text:
		return None
	text = text.replace("T", " ")
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	try:
		return datetime.fromisoformat(text)
	except ValueError:
		return None


def _parse_clock_minutes(val: Any, default_minutes: int) -> int:
	if not val or not isinstance(val, str):
		return default_minutes
	parts = val.strip().split(":")
	try:
		hours = int(parts[0])
		minutes = int(parts[1]) if len(parts) > 1 else 0
		return max(0, min(24 * 60, hours * 60 + minutes))
	except (TypeError, ValueError):
		return default_minutes


def _minutes_to_clock(minutes: int) -> str:
	clamped = max(0, min(24 * 60, int(minutes)))
	return f"{clamped // 60:02d}:{clamped % 60:02d}"


def _minutes_between(older_val: Any, ref_dt: datetime) -> int:
	dt = _parse_dt(older_val)
	if dt is None:
		return 0
	return max(0, int((ref_dt - dt).total_seconds() // 60))


def _hours_between(older_val: Any, ref_dt: datetime) -> float:
	dt = _parse_dt(older_val)
	if dt is None:
		return 9999.0
	return max(0.0, (ref_dt - dt).total_seconds() / 3600.0)


def _format_age(minutes: int) -> str:
	m = max(0, int(minutes))
	if m < 60:
		return f"{m}m"
	hours, rem = divmod(m, 60)
	if hours < 24:
		return f"{hours}h {rem}m" if rem else f"{hours}h"
	days, rem_h = divmod(hours, 24)
	return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def _format_cad(amount: Any) -> str:
	try:
		val = float(amount or 0.0)
	except (TypeError, ValueError):
		val = 0.0
	return f"${val:,.2f}"


def _grade_color(grade: str) -> str:
	if grade == "Critical":
		return "red"
	if grade in ("Watch", "Stale"):
		return "amber"
	return "green"


def _effective_span_end_minutes(
	start_min: int,
	duration_min: int,
	break_s: int | None,
	break_e: int | None,
) -> int:
	dur = max(15, int(duration_min or 60))
	if break_s is not None and break_e is not None and break_e > break_s:
		break_dur = break_e - break_s
		if break_s <= start_min < break_e:
			return break_e + dur
		if start_min < break_s < start_min + dur:
			return start_min + dur + break_dur
	return start_min + dur


def _interval_overlap_minutes(a1: int, a2: int, b1: int, b2: int) -> int:
	if a2 <= a1 or b2 <= b1:
		return 0
	return max(0, min(a2, b2) - max(a1, b1))


def _union_busy_minutes(
	intervals: list[tuple[int, int]],
	win_start: int,
	win_end: int,
	break_s: int | None,
	break_e: int | None,
) -> int:
	clipped = sorted((max(win_start, s), min(win_end, e)) for s, e in intervals if e > win_start and s < win_end)
	if not clipped:
		return 0
	merged: list[tuple[int, int]] = [clipped[0]]
	for s, e in clipped[1:]:
		prev_s, prev_e = merged[-1]
		if s <= prev_e:
			merged[-1] = (prev_s, max(prev_e, e))
		else:
			merged.append((s, e))
	total = 0
	for s, e in merged:
		if e <= s:
			continue
		seg_min = e - s
		if break_s is not None and break_e is not None and break_e > break_s:
			seg_min -= _interval_overlap_minutes(s, e, break_s, break_e)
		total += max(0, seg_min)
	return total


def _active_window_minutes(
	win_start: int,
	win_end: int,
	break_s: int | None,
	break_e: int | None,
) -> int:
	if win_end <= win_start:
		return 0
	total = win_end - win_start
	if break_s is not None and break_e is not None and break_e > break_s:
		total -= _interval_overlap_minutes(win_start, win_end, break_s, break_e)
	return max(0, total)


def _metric_title(metric_key: str) -> str:
	if metric_key == "pnl_health":
		return "Nightly P&L Health"
	if metric_key == "tomorrow_readiness":
		return "Tomorrow Readiness"
	if metric_key == "employee_performance":
		return "Employee Performance"
	return metric_key.replace("_", " ").title()


def _load_shop_board_threads_map() -> dict[str, dict[str, Any]]:
	"""Defensively load active-RO Shop Board Thread summaries; returns {} if DocType/table is absent."""
	try:
		if not getattr(frappe, "db", None) or not frappe.db.table_exists("Shop Board Thread"):
			return {}
		rows = frappe.get_all(
			"Shop Board Thread",
			filters={"thread_type": "REPAIR_ORDER"},
			fields=[
				"thread_key",
				"thread_type",
				"title",
				"repair_order",
				"board_status",
				"blocked_reason",
				"blocked_since",
				"open_action_count",
				"last_event_at",
				"last_event_preview",
			],
			limit_page_length=500,
		)
	except Exception:
		return {}
	return {str(r["thread_key"]): r for r in rows if r.get("thread_key")}


def _thread_enrichment_for_key(
	thread_key: str | None,
	thread_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	if not thread_key:
		return {
			"thread_key": None,
			"board_url": None,
			"board_status": None,
			"open_action_count": 0,
		}
	info = thread_map.get(thread_key) or {}
	return {
		"thread_key": thread_key,
		"board_url": f"/board?thread={thread_key}",
		"board_status": info.get("board_status"),
		"open_action_count": int(info.get("open_action_count") or 0),
	}


def build_cockpit_view(
	snapshot_envelope: dict[str, Any],
	*,
	dead_lift_minutes: int = DEAD_LIFT_MINUTES_THRESHOLD,
	cold_quote_minutes: int = COLD_QUOTE_MINUTES_THRESHOLD,
	stale_hours: int = SCORE_STALE_HOURS_THRESHOLD,
	thread_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
	"""Compute state-aware Action Queue, 7 cockpit gauges, and coupled Bay/Tech swimlane."""
	data = snapshot_envelope.get("data") if "data" in snapshot_envelope else snapshot_envelope
	if not isinstance(data, dict):
		data = {}
	urls = dict(snapshot_envelope.get("urls")) if isinstance(snapshot_envelope.get("urls"), dict) else {}
	urls.setdefault("board", "/board")
	urls.setdefault("cockpit", "/cockpit")

	resolved_threads = _load_shop_board_threads_map() if thread_map is None else thread_map

	target_date = str(data.get("date") or "")
	next_open_date = str(data.get("next_open_date") or "")
	server_now_str = str(data.get("server_now") or "")
	ref_now = _parse_dt(server_now_str) or datetime.now()
	server_date = ref_now.strftime("%Y-%m-%d")
	is_viewing_today = (not target_date) or (target_date == server_date)
	now_min = ref_now.hour * 60 + ref_now.minute

	settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
	op_start = _parse_clock_minutes(settings.get("operating_hours_start"), 8 * 60)
	op_end = _parse_clock_minutes(settings.get("operating_hours_end"), 17 * 60)
	if op_end <= op_start:
		op_end = op_start + 9 * 60
	break_s = _parse_clock_minutes(settings.get("break_start"), -1) if settings.get("break_start") else None
	break_e = _parse_clock_minutes(settings.get("break_end"), -1) if settings.get("break_end") else None
	if break_s is not None and break_e is not None and break_e <= break_s:
		break_s, break_e = None, None

	bays = data.get("bays") if isinstance(data.get("bays"), list) else []
	tech_overview = data.get("technician_overview") if isinstance(data.get("technician_overview"), dict) else {}
	techs = tech_overview.get("technicians") if isinstance(tech_overview.get("technicians"), list) else []
	next_open_techs = data.get("next_open_technicians") if isinstance(data.get("next_open_technicians"), list) else []
	schedule_entries = data.get("schedule_entries") if isinstance(data.get("schedule_entries"), list) else []
	active_ros = data.get("active_repair_orders") if isinstance(data.get("active_repair_orders"), list) else []
	raw_action_items = data.get("action_items") if isinstance(data.get("action_items"), list) else []
	health_snapshots = data.get("health_snapshots") if isinstance(data.get("health_snapshots"), dict) else {}

	# 1. Sync overlay maps from Shop Action Item records
	snoozed_keys: dict[str, str] = {}
	dismissed_keys: set[str] = set()
	open_synced_items: list[dict[str, Any]] = []

	for row in raw_action_items:
		if not isinstance(row, dict):
			continue
		d_key = str(row.get("dedup_key") or "").strip()
		status = str(row.get("status") or "")
		source = str(row.get("source") or "Manual")
		snooze_raw = row.get("snoozed_until")
		snooze_dt = _parse_dt(snooze_raw)

		if d_key and snooze_dt and snooze_dt > ref_now:
			snoozed_keys[d_key] = str(snooze_raw)
		if d_key and status == "Dismissed":
			dismissed_keys.add(d_key)
		if status in ("Open", "In Progress") and source != "Rule Snooze":
			open_synced_items.append(row)

	# Index today's schedule allocations and RO links
	today_entries = [
		e for e in schedule_entries if isinstance(e, dict) and str(e.get("scheduled_date") or "") == target_date
	]
	next_entries = [
		e for e in schedule_entries if isinstance(e, dict) and str(e.get("scheduled_date") or "") == next_open_date
	]

	ro_active_bay: dict[str, str] = {}
	ro_today_booked: set[str] = set()
	for entry in today_entries:
		ro_id = entry.get("repair_order")
		st = str(entry.get("status") or "")
		if ro_id and st not in ("Cancelled", "No Show"):
			ro_today_booked.add(str(ro_id))
		if ro_id and st == "In Progress" and entry.get("service_bay"):
			ro_active_bay[str(ro_id)] = str(entry["service_bay"])

	# 2. Derive live state-aware action items & gauge facts
	derived_items: list[dict[str, Any]] = []
	dead_lift_items_raw: list[dict[str, Any]] = []
	dead_lift_ros: set[str] = set()
	pending_auth_ros: list[dict[str, Any]] = []
	unbilled_wip_ros: list[dict[str, Any]] = []

	for ro in active_ros:
		if not isinstance(ro, dict):
			continue
		ro_name = str(ro.get("name") or "")
		if not ro_name:
			continue
		ro_status = str(ro.get("status") or "")
		approval_status = str(ro.get("approval_status") or "")
		customer = str(ro.get("customer") or "Customer")
		vehicle = str(ro.get("vehicle") or "Vehicle")
		desk_url = str(ro.get("desk_url") or "")
		parts_summary = ro.get("parts_summary") if isinstance(ro.get("parts_summary"), dict) else {}
		in_bay = (ro.get("vehicle_location") == "In Bay / Being Serviced") or (ro_name in ro_active_bay)

		# Rule 1: Build & Send Quote
		if ro_status in ("Checked In", "Draft Estimate"):
			age_min = _minutes_between(ro.get("creation"), ref_now)
			svc_count = int(ro.get("total_services") or 0)
			derived_items.append({
				"id": f"quote_needed:{ro_name}",
				"dedup_key": f"quote_needed:{ro_name}",
				"kind": "derived",
				"rule": "quote_needed",
				"priority": "High",
				"title": f"Build & Send Quote — {ro_name}",
				"subtitle": f"{customer} · {vehicle} · {svc_count} service(s) ({ro_status})",
				"age_minutes": age_min,
				"age_display": _format_age(age_min),
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
				"deep_link": desk_url,
				"can_snooze": True,
				"can_dismiss": False,
			})

		# Rule 2: Follow Up Cold Quote (Pending Authorization)
		if ro_status == "Pending Authorization":
			wait_min = _minutes_between(ro.get("approval_sent_at") or ro.get("modified"), ref_now)
			pending_auth_ros.append({"ro": ro, "wait_minutes": wait_min})
			if wait_min >= cold_quote_minutes:
				derived_items.append({
					"id": f"quote_cold:{ro_name}",
					"dedup_key": f"quote_cold:{ro_name}",
					"kind": "derived",
					"rule": "quote_cold",
					"priority": "High" if wait_min >= cold_quote_minutes * 2 else "Medium",
					"title": f"Follow Up Quote — {ro_name}",
					"subtitle": (
						f"{customer} · {vehicle} · {_format_cad(ro.get('grand_total'))}"
						f" waiting {_format_age(wait_min)}"
					),
					"age_minutes": wait_min,
					"age_display": _format_age(wait_min),
					"reference_doctype": "Repair Order",
					"reference_name": ro_name,
					"deep_link": desk_url,
					"can_snooze": True,
					"can_dismiss": False,
				})

		# Rule 3: Order Missing Parts (persistent while uncovered deficit remains)
		if (
			approval_status in ("Approved", "Partially Approved")
			and ro_status not in ("Work Completed", "Billed / Paid", "Closed / Released", "Cancelled")
			and parts_summary.get("has_uncovered_deficit")
		):
			deficits = parts_summary.get("deficit_items") or []
			uncovered_labels = [
				f"{d.get('item_code')} ×{float(d.get('uncovered_qty') or 0):g}"
				for d in deficits
				if float(d.get("uncovered_qty") or 0) > 0
			]
			parts_text = ", ".join(uncovered_labels[:3])
			if len(uncovered_labels) > 3:
				parts_text += f" (+{len(uncovered_labels) - 3} more)"
			age_min = _minutes_between(ro.get("authorized_at") or ro.get("modified"), ref_now)
			prio = "Critical" if (in_bay or ro_name in ro_today_booked) else "High"
			derived_items.append({
				"id": f"parts_deficit:{ro_name}",
				"dedup_key": f"parts_deficit:{ro_name}",
				"kind": "derived",
				"rule": "parts_deficit",
				"priority": prio,
				"title": f"Order Missing Parts — {ro_name}",
				"subtitle": f"{customer} · {vehicle} · Need: {parts_text or 'stock part'}",
				"age_minutes": age_min,
				"age_display": _format_age(age_min),
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
				"deep_link": desk_url,
				"can_snooze": True,
				"can_dismiss": False,
			})

		# Rule 4: Parts Ready — Resume RO
		if (
			ro_status == "Waiting on Parts"
			and parts_summary.get("all_parts_ready")
			and not int(ro.get("manual_parts_hold") or 0)
		):
			age_min = _minutes_between(ro.get("modified"), ref_now)
			derived_items.append({
				"id": f"parts_ready:{ro_name}",
				"dedup_key": f"parts_ready:{ro_name}",
				"kind": "derived",
				"rule": "parts_ready",
				"priority": "High",
				"title": f"Parts Ready: Resume {ro_name}",
				"subtitle": f"{customer} · {vehicle} · Stock covers approved parts",
				"age_minutes": age_min,
				"age_display": _format_age(age_min),
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
				"deep_link": desk_url,
				"can_snooze": True,
				"can_dismiss": False,
			})

		# Rule 5: Dead Lift (> 30m in active bay while waiting on parts or approval)
		if in_bay and ro_status in ("Waiting on Parts", "Pending Authorization"):
			picked_dt = _parse_dt(ro.get("picked_up_at"))
			mod_dt = _parse_dt(ro.get("modified"))
			if picked_dt and mod_dt:
				stall_anchor = picked_dt if picked_dt >= mod_dt else mod_dt
			else:
				stall_anchor = picked_dt or mod_dt
			stalled_min = max(0, int((ref_now - stall_anchor).total_seconds() // 60)) if stall_anchor else 0
			if stalled_min >= dead_lift_minutes:
				bay_label = ro_active_bay.get(ro_name) or "Bay"
				dead_lift_ros.add(ro_name)
				dl_item = {
					"id": f"dead_lift:{ro_name}",
					"dedup_key": f"dead_lift:{ro_name}",
					"kind": "derived",
					"rule": "dead_lift",
					"priority": "Critical",
					"title": f"Dead Lift ({_format_age(stalled_min)}) — Park {ro_name}",
					"subtitle": f"{bay_label} blocked · {vehicle} · {ro_status}",
					"age_minutes": stalled_min,
					"age_display": _format_age(stalled_min),
					"reference_doctype": "Repair Order",
					"reference_name": ro_name,
					"deep_link": desk_url,
					"can_snooze": True,
					"can_dismiss": False,
				}
				dead_lift_items_raw.append(dl_item)
				derived_items.append(dl_item)

		# Rule 6: Schedule Approved RO
		if (
			approval_status in ("Approved", "Partially Approved")
			and ro_status
			not in (
				"Authorized - In Progress",
				"Work Completed",
				"Billed / Paid",
				"Closed / Released",
				"Cancelled",
			)
			and int(ro.get("unscheduled_approved_services") or 0) > 0
			and not ro.get("has_active_or_future_booking")
		):
			age_min = _minutes_between(ro.get("authorized_at") or ro.get("modified"), ref_now)
			unsched = int(ro.get("unscheduled_approved_services") or 0)
			derived_items.append({
				"id": f"unscheduled_ro:{ro_name}",
				"dedup_key": f"unscheduled_ro:{ro_name}",
				"kind": "derived",
				"rule": "unscheduled_ro",
				"priority": "Medium",
				"title": f"Schedule Approved RO — {ro_name}",
				"subtitle": f"{customer} · {vehicle} · {unsched} unscheduled service(s)",
				"age_minutes": age_min,
				"age_display": _format_age(age_min),
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
				"deep_link": desk_url,
				"can_snooze": True,
				"can_dismiss": False,
			})

		# Rule 7: Checkout & Release Completed Work
		if ro_status == "Work Completed":
			outstanding = float(ro.get("outstanding_amount") or 0.0)
			released = int(ro.get("vehicle_released") or 0)
			if outstanding > 0.01 or not released:
				unbilled_wip_ros.append(ro)
				age_min = _minutes_between(ro.get("modified"), ref_now)
				derived_items.append({
					"id": f"checkout_ready:{ro_name}",
					"dedup_key": f"checkout_ready:{ro_name}",
					"kind": "derived",
					"rule": "checkout_ready",
					"priority": "Medium",
					"title": f"Checkout & Release — {ro_name}",
					"subtitle": (
						f"{customer} · {vehicle} · Balance {_format_cad(outstanding or ro.get('grand_total'))}"
					),
					"age_minutes": age_min,
					"age_display": _format_age(age_min),
					"reference_doctype": "Repair Order",
					"reference_name": ro_name,
					"deep_link": desk_url,
					"can_snooze": True,
					"can_dismiss": False,
				})

	emitted_derived_keys = {it["dedup_key"] for it in derived_items}
	all_candidates = list(derived_items)

	# Rule 8: Synced irregularities, Board AI action cards, & manual Shop Action Items
	for row in open_synced_items:
		d_key = str(row.get("dedup_key") or "").strip() or f"sai:{row['name']}"
		if d_key in emitted_derived_keys:
			continue
		age_min = _minutes_between(row.get("creation"), ref_now)
		prio = str(row.get("priority") or "High")
		if prio not in _PRIORITY_ORDER:
			prio = "High"
		ref_dt = row.get("reference_doctype")
		ref_nm = row.get("reference_name")
		subtitle = str(row.get("details") or "").strip()
		if not subtitle:
			subtitle = f"{row.get('category') or 'General'}" + (f" · {ref_nm}" if ref_nm else "")

		ctype = str(row.get("clearing_condition_type") or "manual").strip() or "manual"
		clearing_desc = ""
		raw_cc_json = row.get("clearing_condition_json")
		if isinstance(raw_cc_json, dict):
			clearing_desc = str(raw_cc_json.get("description") or "").strip()
		elif isinstance(raw_cc_json, str) and raw_cc_json.strip():
			try:
				parsed_cc = json.loads(raw_cc_json)
				if isinstance(parsed_cc, dict):
					clearing_desc = str(parsed_cc.get("description") or "").strip()
			except Exception:
				clearing_desc = ""
		if not clearing_desc:
			clearing_desc = CLEARING_CONDITION_DESCRIPTIONS.get(ctype, "")

		row_name = str(row.get("name") or "")
		all_candidates.append({
			"id": row_name or d_key,
			"action_item_name": row_name,
			"action_item": row_name,
			"dedup_key": d_key,
			"kind": "synced",
			"rule": "synced_action",
			"source": str(row.get("source") or "Manual"),
			"board_event": row.get("board_event") or None,
			"clearing_condition_type": ctype,
			"clearing_condition_description": clearing_desc,
			"priority": prio,
			"title": str(row.get("title") or row_name),
			"subtitle": subtitle,
			"age_minutes": age_min,
			"age_display": _format_age(age_min),
			"reference_doctype": ref_dt,
			"reference_name": ref_nm,
			"deep_link": str(row.get("desk_url") or urls.get("action_items") or ""),
			"can_snooze": True,
			"can_dismiss": True,
		})

	action_queue: list[dict[str, Any]] = []
	suppressed_count = 0
	for item in all_candidates:
		d_key = item["dedup_key"]
		if d_key in snoozed_keys:
			suppressed_count += 1
			continue
		if item["kind"] == "synced" and d_key in dismissed_keys:
			suppressed_count += 1
			continue
		t_key = item.get("thread_key")
		if not t_key and item.get("reference_doctype") == "Repair Order" and item.get("reference_name"):
			t_key = f"RO:{item['reference_name']}"
		item.update(_thread_enrichment_for_key(t_key, resolved_threads))
		action_queue.append(item)

	action_queue.sort(
		key=lambda it: (
			_PRIORITY_ORDER.get(it["priority"], 9),
			-int(it.get("age_minutes") or 0),
			str(it.get("id") or ""),
		)
	)

	# 3. Coupled Dual-Resource Capacity & Today Swimlane (strictly Internal Capacity Start T)
	if is_viewing_today and op_start <= now_min < op_end:
		win_start = now_min
		win_end = min(op_end, now_min + 240)
		window_label = "Next 4h"
	else:
		win_start = op_start
		win_end = op_end
		window_label = "Today" if is_viewing_today else target_date

	window_net_min = max(1, _active_window_minutes(win_start, win_end, break_s, break_e))
	full_day_net_min = max(1, _active_window_minutes(op_start, op_end, break_s, break_e))
	total_span_min = max(60, op_end - op_start)

	parsed_today_entries: list[dict[str, Any]] = []
	for entry in today_entries:
		st = str(entry.get("status") or "")
		# Seam 4 invariant: ALWAYS use Internal Capacity Start T (`scheduled_time`), never customer arrival time.
		start_m = _parse_clock_minutes(entry.get("scheduled_time"), op_start)
		est_dur = int(entry.get("estimated_duration") or 60)
		comp_date = str(entry.get("completion_date") or "")
		comp_time = entry.get("completion_time")
		if comp_date and comp_time:
			if comp_date > target_date:
				end_m = op_end
			elif comp_date < target_date:
				end_m = start_m + max(15, est_dur)
			else:
				end_m = max(start_m + 15, _parse_clock_minutes(comp_time, start_m + est_dur))
		else:
			end_m = _effective_span_end_minutes(start_m, est_dur, break_s, break_e)

		p80_min = int(entry.get("p80_duration_minutes") or 0)
		p80_end_m = (
			_effective_span_end_minutes(start_m, p80_min, break_s, break_e) if p80_min > est_dur else end_m
		)
		parsed_today_entries.append({
			"raw": entry,
			"name": str(entry.get("name") or ""),
			"status": st,
			"bay": entry.get("service_bay"),
			"tech": entry.get("assigned_technician"),
			"ro": entry.get("repair_order"),
			"start_min": start_m,
			"end_min": end_m,
			"p80_end_min": p80_end_m,
		})

	# Detect spillover risk on non-completed/non-cancelled/non-no-show entries
	spillover_entry_names: set[str] = set()
	active_parsed = [p for p in parsed_today_entries if p["status"] not in _NON_BLOCKING_STATUSES]

	for idx, p in enumerate(active_parsed):
		if is_viewing_today and p["status"] == "In Progress" and now_min > p["end_min"]:
			spillover_entry_names.add(p["name"])
			continue
		if p["p80_end_min"] > p["end_min"]:
			if p["p80_end_min"] > op_end:
				spillover_entry_names.add(p["name"])
				continue
			for jdx, other in enumerate(active_parsed):
				if idx == jdx:
					continue
				same_bay = bool(p["bay"] and other["bay"] == p["bay"])
				same_tech = bool(p["tech"] and other["tech"] == p["tech"])
				if (
					(same_bay or same_tech)
					and other["start_min"] >= p["start_min"]
					and other["start_min"] < p["p80_end_min"]
				):
					spillover_entry_names.add(p["name"])
					break

	# Accumulate busy intervals per bay & tech
	bay_intervals: dict[str, list[tuple[int, int]]] = {}
	tech_intervals: dict[str, list[tuple[int, int]]] = {}
	for p in active_parsed:
		if p["bay"]:
			bay_intervals.setdefault(str(p["bay"]), []).append((p["start_min"], p["end_min"]))
		if p["tech"]:
			tech_intervals.setdefault(str(p["tech"]), []).append((p["start_min"], p["end_min"]))

	bay_busy_total = sum(
		_union_busy_minutes(bay_intervals.get(str(b["name"]), []), win_start, win_end, break_s, break_e)
		for b in bays
	)
	total_bay_cap_min = len(bays) * window_net_min
	bay_util_pct = round(min(100.0, (bay_busy_total / total_bay_cap_min) * 100.0), 1) if total_bay_cap_min > 0 else 0.0
	free_bay_hours = round(max(0.0, (total_bay_cap_min - bay_busy_total) / 60.0), 1)

	on_duty_techs = [t for t in techs if isinstance(t, dict) and not t.get("on_leave")]
	tech_busy_total = sum(
		_union_busy_minutes(tech_intervals.get(str(t["employee"]), []), win_start, win_end, break_s, break_e)
		for t in on_duty_techs
	)
	total_tech_cap_min = len(on_duty_techs) * window_net_min
	tech_util_pct = (
		round(min(100.0, (tech_busy_total / total_tech_cap_min) * 100.0), 1)
		if total_tech_cap_min > 0
		else 0.0
	)
	free_tech_hours = round(max(0.0, (total_tech_cap_min - tech_busy_total) / 60.0), 1)
	effective_bookable_hours = round(min(free_bay_hours, free_tech_hours), 1)

	# Tag utilization breakdown
	tag_stats: dict[str, dict[str, Any]] = {}
	for tag_name in ("Alignment Rack", "HV Diagnostic", "Lift"):
		tagged_bays = [b for b in bays if tag_name in (b.get("equipment_tags") or [])]
		if not tagged_bays:
			continue
		t_cap = len(tagged_bays) * window_net_min
		t_busy = sum(
			_union_busy_minutes(bay_intervals.get(str(b["name"]), []), win_start, win_end, break_s, break_e)
			for b in tagged_bays
		)
		tag_stats[tag_name] = {
			"bays": len(tagged_bays),
			"utilization_pct": round(min(100.0, (t_busy / t_cap) * 100.0), 1) if t_cap > 0 else 0.0,
		}

	if not bays or not on_duty_techs:
		bottleneck = "No Capacity"
	elif (
		tag_stats.get("Alignment Rack", {}).get("utilization_pct", 0.0) >= 85.0
		and min(bay_util_pct, tech_util_pct) < 85.0
	):
		bottleneck = "Alignment-Bound"
	elif (
		tag_stats.get("HV Diagnostic", {}).get("utilization_pct", 0.0) >= 85.0
		and min(bay_util_pct, tech_util_pct) < 85.0
	):
		bottleneck = "HV-Bound"
	elif (free_tech_hours == 0 and free_bay_hours > 0) or (tech_util_pct - bay_util_pct >= 15.0):
		bottleneck = "Tech-Bound"
	elif (free_bay_hours == 0 and free_tech_hours > 0) or (bay_util_pct - tech_util_pct >= 15.0):
		bottleneck = "Bay-Bound"
	elif max(bay_util_pct, tech_util_pct) >= 85.0:
		bottleneck = "Saturated (Coupled)"
	else:
		bottleneck = "Balanced"

	def _build_block(p: dict[str, Any], counterpart_label: str) -> dict[str, Any]:
		raw = p["raw"]
		clamped_s = max(op_start, min(op_end - 5, p["start_min"]))
		clamped_e = max(clamped_s + 10, min(op_end, p["end_min"]))
		left_pct = round(((clamped_s - op_start) / total_span_min) * 100.0, 2)
		width_pct = round(max(2.5, min(100.0 - left_pct, ((clamped_e - clamped_s) / total_span_min) * 100.0)), 2)
		ro_id = p["ro"]
		st = p["status"]
		if ro_id and str(ro_id) in dead_lift_ros:
			state = "dead_lift"
		elif p["name"] in spillover_entry_names:
			state = "overrun"
		elif st == "Completed":
			state = "completed"
		elif st == "In Progress":
			state = "active"
		else:
			state = "scheduled"

		main_label = str(ro_id or raw.get("entry_type") or p["name"])
		veh = str(raw.get("vehicle") or raw.get("customer") or "").strip()
		sublabel_parts = [
			part
			for part in (
				veh,
				counterpart_label,
				f"{_minutes_to_clock(p['start_min'])}–{_minutes_to_clock(p['end_min'])}",
			)
			if part
		]
		t_key = f"RO:{ro_id}" if ro_id else None
		block = {
			"name": p["name"],
			"repair_order": ro_id,
			"status": st,
			"state": state,
			"label": main_label,
			"sublabel": " · ".join(sublabel_parts),
			"start_clock": _minutes_to_clock(p["start_min"]),
			"end_clock": _minutes_to_clock(p["end_min"]),
			"left_pct": left_pct,
			"width_pct": width_pct,
			"deep_link": str(raw.get("desk_url") or ""),
		}
		block.update(_thread_enrichment_for_key(t_key, resolved_threads))
		return block

	tech_name_map = {
		str(t.get("employee") or ""): str(t.get("employee_name") or t.get("employee") or "")
		for t in techs
		if isinstance(t, dict)
	}
	bay_lanes: list[dict[str, Any]] = []
	for b in bays:
		b_id = str(b["name"])
		b_entries = [p for p in parsed_today_entries if p["bay"] == b_id and p["status"] not in ("Cancelled", "No Show")]
		b_busy = _union_busy_minutes(bay_intervals.get(b_id, []), op_start, op_end, break_s, break_e)
		bay_lanes.append({
			"id": b_id,
			"label": str(b.get("bay_name") or b_id),
			"tags": list(b.get("equipment_tags") or []),
			"utilization_pct": round(min(100.0, (b_busy / full_day_net_min) * 100.0), 1),
			"desk_url": str(b.get("desk_url") or ""),
			"blocks": [
				_build_block(p, tech_name_map.get(str(p["tech"] or ""), str(p["tech"] or "Unassigned Tech")))
				for p in b_entries
			],
		})

	tech_lanes: list[dict[str, Any]] = []
	for t in techs:
		if not isinstance(t, dict):
			continue
		t_id = str(t.get("employee") or "")
		t_entries = [p for p in parsed_today_entries if p["tech"] == t_id and p["status"] not in ("Cancelled", "No Show")]
		tech_lanes.append({
			"id": t_id,
			"label": str(t.get("employee_name") or t_id),
			"on_leave": bool(t.get("on_leave")),
			"utilization_pct": round(float(t.get("utilization_pct") or t.get("utilization_percent") or 0.0), 1),
			"blocks": [_build_block(p, str(p["bay"] or "No Bay")) for p in t_entries],
		})

	unslotted_bay_ros = []
	for ro in active_ros:
		if (
			isinstance(ro, dict)
			and ro.get("vehicle_location") == "In Bay / Being Serviced"
			and str(ro.get("name") or "") not in ro_active_bay
		):
			ro_id = str(ro["name"])
			row_data = {
				"name": ro_id,
				"status": str(ro.get("status") or ""),
				"vehicle": str(ro.get("vehicle") or ""),
				"desk_url": str(ro.get("desk_url") or ""),
				"is_dead_lift": ro_id in dead_lift_ros,
			}
			row_data.update(_thread_enrichment_for_key(f"RO:{ro_id}", resolved_threads))
			unslotted_bay_ros.append(row_data)

	break_band = None
	if break_s is not None and break_e is not None and op_start <= break_s < break_e <= op_end:
		break_band = {
			"left_pct": round(((break_s - op_start) / total_span_min) * 100.0, 2),
			"width_pct": round(((break_e - break_s) / total_span_min) * 100.0, 2),
			"label": f"Break {_minutes_to_clock(break_s)}–{_minutes_to_clock(break_e)}",
		}

	now_marker_pct = None
	if is_viewing_today and op_start <= now_min <= op_end:
		now_marker_pct = round(((now_min - op_start) / total_span_min) * 100.0, 2)

	swimlane = {
		"date": target_date,
		"op_start_clock": _minutes_to_clock(op_start),
		"op_end_clock": _minutes_to_clock(op_end),
		"hours": [
			{
				"label": _minutes_to_clock(m),
				"left_pct": round(((m - op_start) / total_span_min) * 100.0, 2),
			}
			for m in range(op_start, op_end + 1, 60)
		],
		"break_band": break_band,
		"now_marker_pct": now_marker_pct,
		"bay_lanes": bay_lanes,
		"tech_lanes": tech_lanes,
		"unslotted_bay_ros": unslotted_bay_ros,
	}

	# 4. Build the 7 Cockpit Gauges + dynamic supplemental score cards
	cap_grade = (
		"Critical"
		if bottleneck == "No Capacity" or max(bay_util_pct, tech_util_pct) >= 92.0
		else ("Watch" if bottleneck != "Balanced" or max(bay_util_pct, tech_util_pct) >= 75.0 else "Healthy")
	)
	cap_pct = round(max(bay_util_pct, tech_util_pct), 1)

	max_dead_min = max((int(it["age_minutes"]) for it in dead_lift_items_raw), default=0)
	dead_count = len(dead_lift_items_raw)
	dead_grade = "Critical" if dead_count > 0 else "Healthy"

	pending_count = len(pending_auth_ros)
	pending_total = sum(float(p["ro"].get("grand_total") or 0.0) for p in pending_auth_ros)
	max_pending_wait = max((int(p["wait_minutes"]) for p in pending_auth_ros), default=0)
	auth_grade = (
		"Critical"
		if max_pending_wait >= cold_quote_minutes * 2
		else ("Watch" if max_pending_wait >= cold_quote_minutes else "Healthy")
	)

	spill_count = len(spillover_entry_names)
	spill_grade = "Critical" if spill_count >= 2 else ("Watch" if spill_count == 1 else "Healthy")

	wip_count = len(unbilled_wip_ros)
	wip_total = sum(float(r.get("outstanding_amount") or r.get("grand_total") or 0.0) for r in unbilled_wip_ros)
	wip_grade = "Watch" if wip_count > 0 else "Healthy"

	pnl_snap = health_snapshots.get("pnl_health")
	pnl_stale = True
	if isinstance(pnl_snap, dict):
		pnl_age_h = _hours_between(pnl_snap.get("creation"), ref_now)
		pnl_stale = pnl_age_h > stale_hours
		pnl_score = int(pnl_snap.get("score") or 0)
		pnl_grade = str(pnl_snap.get("grade") or "Healthy")
		pnl_gauge = {
			"key": "pnl_health",
			"title": "Nightly P&L Health",
			"value": f"{pnl_score}/100",
			"numeric_value": pnl_score,
			"max_value": 100,
			"grade": "Stale" if pnl_stale else pnl_grade,
			"color": "amber" if pnl_stale else _grade_color(pnl_grade),
			"delta": pnl_snap.get("delta"),
			"is_stale": pnl_stale,
			"driver": str(pnl_snap.get("verdict") or "P&L snapshot available"),
			"period_date": pnl_snap.get("period_date"),
			"link": str(pnl_snap.get("report_url") or urls.get("pnl_report") or ""),
		}
	else:
		pnl_gauge = {
			"key": "pnl_health",
			"title": "Nightly P&L Health",
			"value": "—/100",
			"numeric_value": None,
			"max_value": 100,
			"grade": "Stale",
			"color": "amber",
			"delta": None,
			"is_stale": True,
			"driver": "Needs evening P&L analysis run",
			"period_date": None,
			"link": str(urls.get("pnl_report") or ""),
		}

	tmrw_snap = health_snapshots.get("tomorrow_readiness")
	use_tmrw_snap = False
	if isinstance(tmrw_snap, dict):
		tmrw_age_h = _hours_between(tmrw_snap.get("creation"), ref_now)
		snap_period = str(tmrw_snap.get("period_date") or "")
		if tmrw_age_h <= stale_hours and snap_period in (next_open_date, target_date):
			use_tmrw_snap = True

	if use_tmrw_snap and isinstance(tmrw_snap, dict):
		tmrw_score = int(tmrw_snap.get("score") or 0)
		tmrw_grade = str(tmrw_snap.get("grade") or "Healthy")
		tmrw_gauge = {
			"key": "tomorrow_readiness",
			"title": "Tomorrow Readiness",
			"value": f"{tmrw_score}/100",
			"numeric_value": tmrw_score,
			"max_value": 100,
			"grade": tmrw_grade,
			"color": _grade_color(tmrw_grade),
			"delta": tmrw_snap.get("delta"),
			"is_stale": False,
			"driver": str(tmrw_snap.get("verdict") or ""),
			"period_date": tmrw_snap.get("period_date"),
			"link": str(tmrw_snap.get("report_url") or urls.get("calendar") or ""),
		}
	else:
		ro_by_name = {str(r["name"]): r for r in active_ros if isinstance(r, dict) and r.get("name")}
		next_active = [e for e in next_entries if str(e.get("status") or "") not in ("Cancelled", "No Show")]
		next_on_duty = {
			str(t.get("employee") or "")
			for t in next_open_techs
			if isinstance(t, dict) and not t.get("on_leave")
		}
		uncovered_bookings = 0
		awaiting_po_bookings = 0
		tech_gap_bookings = 0
		for e in next_active:
			ro_doc = ro_by_name.get(str(e.get("repair_order") or ""))
			ps = (ro_doc.get("parts_summary") or {}) if isinstance(ro_doc, dict) else {}
			if ps.get("has_uncovered_deficit"):
				uncovered_bookings += 1
			elif ps.get("has_stock_deficit"):
				awaiting_po_bookings += 1
			assigned_t = str(e.get("assigned_technician") or "")
			if assigned_t and assigned_t not in next_on_duty:
				tech_gap_bookings += 1

		penalty = (
			min(60, uncovered_bookings * 20)
			+ min(20, awaiting_po_bookings * 10)
			+ min(30, tech_gap_bookings * 15)
		)
		if next_active and not next_on_duty:
			penalty += 25
		live_score = max(0, min(100, 100 - penalty))
		live_grade = "Healthy" if live_score >= 80 else ("Watch" if live_score >= 60 else "Critical")
		tmrw_driver = (
			f"{len(next_active)} booked ({next_open_date}) · {uncovered_bookings} uncovered parts"
			if next_active
			else f"No bookings yet for {next_open_date}"
		)
		tmrw_gauge = {
			"key": "tomorrow_readiness",
			"title": "Tomorrow Readiness",
			"value": f"{live_score}/100",
			"numeric_value": live_score,
			"max_value": 100,
			"grade": live_grade,
			"color": _grade_color(live_grade),
			"delta": None,
			"is_stale": False,
			"driver": tmrw_driver,
			"period_date": next_open_date,
			"link": str(urls.get("calendar") or ""),
		}

	gauges: list[dict[str, Any]] = [
		{
			"key": "effective_capacity",
			"title": f"Effective Capacity ({window_label})",
			"value": f"{effective_bookable_hours}h free",
			"numeric_value": cap_pct,
			"max_value": 100,
			"grade": cap_grade,
			"color": _grade_color(cap_grade),
			"delta": None,
			"is_stale": False,
			"driver": f"Bay {bay_util_pct:.0f}% · Tech {tech_util_pct:.0f}% · {bottleneck}",
			"link": str(urls.get("calendar") or ""),
		},
		{
			"key": "dead_lift",
			"title": "Dead-Lift Bays",
			"value": f"{dead_count} blocked" if dead_count else "0 blocked",
			"numeric_value": min(100, dead_count * 35),
			"max_value": 100,
			"grade": dead_grade,
			"color": _grade_color(dead_grade),
			"delta": None,
			"is_stale": False,
			"driver": (
				f"Max stall {_format_age(max_dead_min)} (> {dead_lift_minutes}m rule)"
				if dead_count
				else "All lifts productive"
			),
			"link": str(urls.get("repair_orders") or ""),
		},
		{
			"key": "approval_velocity",
			"title": "Pending Approvals",
			"value": f"{pending_count} ({_format_cad(pending_total)})",
			"numeric_value": (
				min(100, int((max_pending_wait / max(1, cold_quote_minutes * 2)) * 100)) if pending_count else 0
			),
			"max_value": 100,
			"grade": auth_grade,
			"color": _grade_color(auth_grade),
			"delta": None,
			"is_stale": False,
			"driver": f"Oldest quote waiting {_format_age(max_pending_wait)}" if pending_count else "No quotes waiting",
			"link": str(urls.get("pending_authorization") or urls.get("repair_orders") or ""),
		},
		{
			"key": "spillover_risk",
			"title": "Promise / P80 Risk",
			"value": f"{spill_count} at risk" if spill_count else "0 at risk",
			"numeric_value": min(100, spill_count * 40),
			"max_value": 100,
			"grade": spill_grade,
			"color": _grade_color(spill_grade),
			"delta": None,
			"is_stale": False,
			"driver": (
				f"{spill_count} booking(s) exceed P80 or end time" if spill_count else "All bookings within P80 buffer"
			),
			"link": str(urls.get("schedule_entries") or urls.get("calendar") or ""),
		},
		{
			"key": "unbilled_wip",
			"title": "Unbilled Completed WIP",
			"value": f"{wip_count} ({_format_cad(wip_total)})",
			"numeric_value": min(100, wip_count * 25),
			"max_value": 100,
			"grade": wip_grade,
			"color": _grade_color(wip_grade),
			"delta": None,
			"is_stale": False,
			"driver": f"{wip_count} completed RO(s) ready for checkout" if wip_count else "All completed work billed",
			"link": str(urls.get("work_completed") or urls.get("repair_orders") or ""),
		},
		pnl_gauge,
		tmrw_gauge,
	]

	supplemental_gauges: list[dict[str, Any]] = []
	for key, snap in sorted(health_snapshots.items()):
		if key in ("pnl_health", "tomorrow_readiness") or not isinstance(snap, dict):
			continue
		age_h = _hours_between(snap.get("creation"), ref_now)
		is_stale = age_h > stale_hours
		score = int(snap.get("score") or 0)
		grade = str(snap.get("grade") or "Healthy")
		card = {
			"key": key,
			"title": _metric_title(key),
			"value": f"{score}/100",
			"numeric_value": score,
			"max_value": 100,
			"grade": "Stale" if is_stale else grade,
			"color": "amber" if is_stale else _grade_color(grade),
			"delta": snap.get("delta"),
			"is_stale": is_stale,
			"driver": str(snap.get("verdict") or ""),
			"period_date": snap.get("period_date"),
			"link": str(snap.get("report_url") or urls.get("health_snapshots") or ""),
		}
		gauges.append(card)
		supplemental_gauges.append(card)

	active_ro_rows = []
	for ro in active_ros:
		if not isinstance(ro, dict) or not ro.get("name"):
			continue
		ro_id = str(ro["name"])
		row_copy = dict(ro)
		row_copy.update(_thread_enrichment_for_key(f"RO:{ro_id}", resolved_threads))
		active_ro_rows.append(row_copy)

	return {
		"date": target_date,
		"next_open_date": next_open_date,
		"server_now": server_now_str,
		"capacity": {
			"window_label": window_label,
			"active_bays": len(bays),
			"on_duty_techs": len(on_duty_techs),
			"total_techs": len(techs),
			"bay_utilization_pct": bay_util_pct,
			"tech_utilization_pct": tech_util_pct,
			"free_bay_hours": free_bay_hours,
			"free_tech_hours": free_tech_hours,
			"effective_bookable_hours": effective_bookable_hours,
			"bottleneck": bottleneck,
			"tag_saturation": tag_stats,
		},
		"gauges": gauges,
		"supplemental_gauges": supplemental_gauges,
		"action_queue": action_queue,
		"action_counts": {
			"total": len(action_queue),
			"critical": sum(1 for it in action_queue if it["priority"] == "Critical"),
			"high": sum(1 for it in action_queue if it["priority"] == "High"),
			"medium": sum(1 for it in action_queue if it["priority"] == "Medium"),
			"suppressed": suppressed_count,
		},
		"swimlane": swimlane,
		"ro_rows": active_ro_rows,
		"urls": urls,
	}


def get_cockpit_view(date: str | None = None) -> dict[str, Any]:
	"""Run lightweight clearing sweep, fetch live cockpit snapshot, and build the enriched cockpit view."""
	try:
		from ism.ism.doctype.shop_action_item.clearing import sweep_open_action_items

		sweep_open_action_items()
	except Exception:
		pass

	from ism.api.agent_monitoring import get_cockpit_snapshot

	snapshot = get_cockpit_snapshot(date=date)
	return build_cockpit_view(snapshot)
