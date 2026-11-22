# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Decoupled structured clearing-condition engine for Shop Action Items.

NEVER imports ``ism.board``. Evaluates deterministic ERP predicates on
``Repair Order``, ``Schedule Entry``, ``Purchase Order``, ``Purchase Receipt``,
``SMS Audit Log``, and ``Communication`` events and auto-resolves satisfied
``Shop Action Item`` records so their linked board cards clear silently via
``Shop Action Item.on_update``.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

import frappe
from frappe.utils import cint, get_datetime, now_datetime

CLEARING_CONDITION_TYPES: tuple[str, ...] = (
	"manual",
	"ro_status_in",
	"ro_approval_status_in",
	"ro_has_supplement_or_service",
	"ro_parts_covered",
	"ro_parts_hold_cleared",
	"ro_schedule_updated",
	"ro_sms_or_email_sent",
	"po_submitted_or_received",
	"schedule_converted_to_ro",
	"ro_closed",
)

AUTO_CLOSE_RO_STATUSES: tuple[str, ...] = (
	"Closed / Released",
	"Cancelled",
)

_TERMINAL_SCHEDULE_STATUSES: tuple[str, ...] = (
	"Cancelled",
	"Completed",
	"No Show",
)

_VALID_OUTBOUND_SMS_STATUSES: tuple[str, ...] = (
	"Sent",
	"Delivered",
	"Queued",
	"Mock",
)

CLEARING_CONDITION_DESCRIPTIONS: dict[str, str] = {
	"manual": "Manual resolution (or when RO closes)",
	"ro_status_in": "When Repair Order status advances",
	"ro_approval_status_in": "When customer approval status updates",
	"ro_has_supplement_or_service": "When supplement or service is added / sent for approval",
	"ro_parts_covered": "When required parts are in stock or ordered",
	"ro_parts_hold_cleared": "When parts hold is cleared",
	"ro_schedule_updated": "When Repair Order schedule is updated",
	"ro_sms_or_email_sent": "When customer SMS or email is sent",
	"po_submitted_or_received": "When Purchase Order is submitted or received",
	"schedule_converted_to_ro": "When booking is converted to a Repair Order",
	"ro_closed": "When Repair Order closes or is cancelled",
}


def _clearing_schema_ready() -> bool:
	"""Return True once Shop Action Item clearing columns exist in the database."""
	try:
		return bool(
			frappe.db.exists("DocType", "Shop Action Item")
			and frappe.db.has_column("Shop Action Item", "clearing_condition_type")
			and frappe.db.has_column("Shop Action Item", "auto_cleared")
		)
	except Exception:
		return False


def _parse_json_dict(raw: Any) -> dict[str, Any]:
	if isinstance(raw, dict):
		return dict(raw)
	if isinstance(raw, str) and raw.strip():
		try:
			parsed = json.loads(raw)
			if isinstance(parsed, dict):
				return parsed
		except Exception:
			return {}
	return {}


def _parse_ts(value: Any) -> datetime | None:
	if not value:
		return None
	try:
		dt = value if isinstance(value, datetime) else get_datetime(value)
		if dt is None:
			return None
		if dt.tzinfo is not None:
			return dt.replace(tzinfo=None)
		return dt
	except Exception:
		return None


def _ts_gte(candidate: Any, baseline: datetime | None) -> bool:
	cand_dt = _parse_ts(candidate)
	if cand_dt is None:
		return False
	if baseline is None:
		return True
	return cand_dt >= baseline


def _ts_gt(candidate: Any, baseline: datetime | None) -> bool:
	cand_dt = _parse_ts(candidate)
	if cand_dt is None:
		return False
	if baseline is None:
		return True
	return cand_dt > baseline


def _get_po_ro_column(doctype: str) -> str | None:
	try:
		if not frappe.db.exists("DocType", doctype):
			return None
		for col in ("repair_order", "custom_repair_order"):
			if frappe.db.has_column(doctype, col):
				return col
	except Exception:
		return None
	return None


def _get_submitted_pos_for_ro(ro_name: str) -> list[dict[str, Any]]:
	if not ro_name:
		return []
	col = _get_po_ro_column("Purchase Order")
	if not col:
		return []
	try:
		return frappe.get_all(
			"Purchase Order",
			filters={col: ro_name, "docstatus": 1},
			fields=["name", "creation", "modified"],
		)
	except Exception:
		return []


def _get_submitted_prs_for_ro(ro_name: str) -> list[dict[str, Any]]:
	if not ro_name or not frappe.db.exists("DocType", "Purchase Receipt"):
		return []
	prs_by_name: dict[str, dict[str, Any]] = {}
	try:
		pr_col = _get_po_ro_column("Purchase Receipt")
		if pr_col:
			for row in frappe.get_all(
				"Purchase Receipt",
				filters={pr_col: ro_name, "docstatus": 1},
				fields=["name", "creation", "modified"],
			):
				prs_by_name[row["name"]] = row

		po_col = _get_po_ro_column("Purchase Order")
		if po_col and frappe.db.exists("DocType", "Purchase Receipt Item"):
			po_names = frappe.get_all(
				"Purchase Order",
				filters={po_col: ro_name, "docstatus": ["in", [0, 1]]},
				pluck="name",
			)
			if po_names:
				pr_names = list(
					dict.fromkeys(
						frappe.get_all(
							"Purchase Receipt Item",
							filters={"purchase_order": ["in", po_names], "docstatus": 1},
							pluck="parent",
						)
					)
				)
				if pr_names:
					for row in frappe.get_all(
						"Purchase Receipt",
						filters={"name": ["in", pr_names], "docstatus": 1},
						fields=["name", "creation", "modified"],
					):
						prs_by_name[row["name"]] = row
	except Exception:
		return []
	return list(prs_by_name.values())


def _extract_target_item_codes(item_doc: Any, params: dict[str, Any]) -> list[str]:
	codes: list[str] = []
	item_code = getattr(item_doc, "item_code", None)
	if isinstance(item_code, str) and item_code.strip():
		codes.append(item_code.strip())
	raw_codes = params.get("item_codes") or params.get("part_codes")
	if isinstance(raw_codes, (list, tuple)):
		for code in raw_codes:
			if isinstance(code, str) and code.strip():
				codes.append(code.strip())
	elif isinstance(raw_codes, str) and raw_codes.strip():
		codes.append(raw_codes.strip())
	return list(dict.fromkeys(codes))


def _has_submitted_po_or_pr_for_items(
	item_codes: list[str],
	baseline_ts: datetime | None,
) -> tuple[bool, str]:
	if not item_codes:
		return False, ""
	for parent_dt, child_dt in (
		("Purchase Order", "Purchase Order Item"),
		("Purchase Receipt", "Purchase Receipt Item"),
	):
		try:
			if not frappe.db.exists("DocType", parent_dt) or not frappe.db.exists("DocType", child_dt):
				continue
			parents = list(
				dict.fromkeys(
					frappe.get_all(
						child_dt,
						filters={"item_code": ["in", item_codes], "docstatus": 1},
						pluck="parent",
						limit=50,
					)
				)
			)
			if not parents:
				continue
			rows = frappe.get_all(
				parent_dt,
				filters={"name": ["in", parents], "docstatus": 1},
				fields=["name", "creation", "modified"],
			)
			for row in rows:
				if baseline_ts is None or _ts_gte(row.get("creation"), baseline_ts) or _ts_gt(
					row.get("modified"), baseline_ts
				):
					return True, f"{parent_dt} {row['name']} submitted for item(s) {', '.join(item_codes)}"
		except Exception:
			continue
	return False, ""


def _evaluate_po_or_pr_activity(
	ro_name: str | None,
	item_doc: Any,
	params: dict[str, Any],
	baseline_ts: datetime | None,
) -> tuple[bool, str]:
	baseline_po_count = params.get("baseline_po_count")
	baseline_pr_count = params.get("baseline_pr_count")

	if ro_name:
		pos = _get_submitted_pos_for_ro(ro_name)
		if baseline_po_count is not None:
			if len(pos) > cint(baseline_po_count):
				return True, f"Purchase Order submitted for {ro_name}"
		else:
			for po in pos:
				if _ts_gte(po.get("creation"), baseline_ts) or _ts_gt(po.get("modified"), baseline_ts):
					return True, f"Purchase Order {po['name']} submitted for {ro_name}"

		prs = _get_submitted_prs_for_ro(ro_name)
		if baseline_pr_count is not None:
			if len(prs) > cint(baseline_pr_count):
				return True, f"Purchase Receipt submitted for {ro_name}"
		else:
			for pr in prs:
				if _ts_gte(pr.get("creation"), baseline_ts) or _ts_gt(pr.get("modified"), baseline_ts):
					return True, f"Purchase Receipt {pr['name']} submitted for {ro_name}"

	item_codes = _extract_target_item_codes(item_doc, params)
	if item_codes:
		matched, reason = _has_submitted_po_or_pr_for_items(item_codes, baseline_ts)
		if matched:
			return True, reason

	return False, ""


def _ro_approved_stock_status(ro_doc: Any) -> tuple[bool, bool]:
	"""Return (has_approved_stock_items, all_available)."""
	approved_services = [
		s for s in (getattr(ro_doc, "services", None) or []) if getattr(s, "status", None) == "Approved"
	]
	if not approved_services:
		return False, True

	raw_codes: set[str] = set()
	for svc in approved_services:
		parts = getattr(svc, "parts", None)
		if isinstance(parts, str):
			try:
				parts = json.loads(parts) if parts else []
			except Exception:
				parts = []
		for part in parts or []:
			if isinstance(part, dict) and part.get("item_code"):
				raw_codes.add(str(part["item_code"]))

	if not raw_codes:
		return False, True

	try:
		stock_items = set(
			frappe.get_all(
				"Item",
				filters={"name": ["in", list(raw_codes)], "is_stock_item": 1},
				pluck="name",
			)
		)
	except Exception:
		return False, True

	if not stock_items:
		return False, True

	try:
		from ism.ism.doctype.repair_order.fulfillment import check_ro_parts_stock_availability

		all_avail, _deficits = check_ro_parts_stock_availability(ro_doc)
		return True, bool(all_avail)
	except Exception:
		return True, False


def _get_ro_schedule_entries(ro_name: str, *, include_cancelled: bool = False) -> list[dict[str, Any]]:
	if not ro_name or not frappe.db.exists("DocType", "Schedule Entry"):
		return []
	try:
		filters: dict[str, Any] = {"repair_order": ro_name}
		if not include_cancelled:
			filters["status"] = ["!=", "Cancelled"]
		return frappe.get_all(
			"Schedule Entry",
			filters=filters,
			fields=["name", "status", "creation", "modified"],
			order_by="modified desc",
		)
	except Exception:
		return []


def _get_outbound_sms_for_ro(ro_name: str) -> list[dict[str, Any]]:
	if not ro_name or not frappe.db.exists("DocType", "SMS Audit Log"):
		return []
	try:
		return frappe.get_all(
			"SMS Audit Log",
			filters={
				"direction": "Outbound",
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
				"status": ["in", list(_VALID_OUTBOUND_SMS_STATUSES)],
			},
			fields=["name", "status", "creation", "sent_at"],
			order_by="creation desc",
		)
	except Exception:
		return []


def _get_outbound_comms_for_ro(ro_name: str) -> list[dict[str, Any]]:
	if not ro_name or not frappe.db.exists("DocType", "Communication"):
		return []
	try:
		return frappe.get_all(
			"Communication",
			filters={
				"reference_doctype": "Repair Order",
				"reference_name": ro_name,
				"sent_or_received": "Sent",
			},
			fields=["name", "creation"],
			order_by="creation desc",
		)
	except Exception:
		return []


def build_clearing_condition_for_ro(
	condition_type: str | None,
	ro_name: str | None,
	*,
	overrides: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
	"""Snapshot baseline RO state so clearing predicates never false-trigger on pre-existing data."""
	ctype = (condition_type or "manual").strip() if isinstance(condition_type, str) else "manual"
	if ctype not in CLEARING_CONDITION_TYPES:
		ctype = "manual"

	baseline_ts = now_datetime().strftime("%Y-%m-%d %H:%M:%S.%f")
	params: dict[str, Any] = {
		"baseline_timestamp": baseline_ts,
		"description": CLEARING_CONDITION_DESCRIPTIONS.get(ctype, ""),
	}

	ro_exists = bool(ro_name and frappe.db.exists("Repair Order", ro_name))
	if ro_exists and ro_name:
		if ctype == "ro_has_supplement_or_service":
			try:
				params["baseline_service_count"] = frappe.db.count(
					"Repair Order Service",
					{"parent": ro_name, "parenttype": "Repair Order"},
				)
			except Exception:
				params["baseline_service_count"] = 0
			params["baseline_approval_status"] = frappe.db.get_value(
				"Repair Order", ro_name, "approval_status"
			)

		elif ctype in ("po_submitted_or_received", "ro_parts_covered"):
			params["baseline_po_count"] = len(_get_submitted_pos_for_ro(ro_name))
			params["baseline_pr_count"] = len(_get_submitted_prs_for_ro(ro_name))
			if ctype == "ro_parts_covered":
				try:
					ro_doc = frappe.get_doc("Repair Order", ro_name)
					has_stock, all_avail = _ro_approved_stock_status(ro_doc)
					params["baseline_has_stock_parts"] = has_stock
					params["baseline_had_parts_deficit"] = bool(has_stock and not all_avail)
				except Exception:
					pass

		elif ctype == "ro_status_in":
			params["statuses"] = ["Work Completed", "Billed / Paid", "Closed / Released", "Cancelled"]

		elif ctype == "ro_approval_status_in":
			params["approval_statuses"] = ["Pending Approval", "Approved", "Partially Approved"]

		elif ctype == "ro_schedule_updated":
			entries = _get_ro_schedule_entries(ro_name, include_cancelled=False)
			params["baseline_schedule_count"] = len(entries)
			params["baseline_schedule_modified"] = (
				str(entries[0]["modified"]) if entries and entries[0].get("modified") else None
			)

		elif ctype == "ro_sms_or_email_sent":
			params["baseline_sms_count"] = len(_get_outbound_sms_for_ro(ro_name))
			params["baseline_comm_count"] = len(_get_outbound_comms_for_ro(ro_name))

		elif ctype == "ro_closed":
			params["statuses"] = list(AUTO_CLOSE_RO_STATUSES)

	if isinstance(overrides, dict):
		for key, val in overrides.items():
			if val is not None:
				params[key] = val

	return ctype, params


def evaluate_clearing_condition(
	item_doc: Any,
	*,
	ro_doc: Any = None,
	se_doc: Any = None,
) -> tuple[bool, str]:
	"""Pure deterministic predicate evaluator. Returns ``(should_clear, reason)``."""
	if item_doc is None:
		return False, ""
	if getattr(item_doc, "source", None) == "Rule Snooze":
		return False, ""

	params = _parse_json_dict(getattr(item_doc, "clearing_condition_json", None))
	ctype = (getattr(item_doc, "clearing_condition_type", None) or "manual").strip() or "manual"
	ref_dt = getattr(item_doc, "reference_doctype", None)
	ref_name = getattr(item_doc, "reference_name", None)
	baseline_ts = _parse_ts(params.get("baseline_timestamp") or getattr(item_doc, "creation", None))

	if ro_doc is None and ref_dt == "Repair Order" and ref_name:
		if frappe.db.exists("Repair Order", ref_name):
			ro_doc = frappe.get_doc("Repair Order", ref_name)

	if se_doc is None and ref_dt == "Schedule Entry" and ref_name:
		if frappe.db.exists("DocType", "Schedule Entry") and frappe.db.exists("Schedule Entry", ref_name):
			se_doc = frappe.get_doc("Schedule Entry", ref_name)

	# Universal close/cancel auto-clear for any RO-linked item unless explicitly opted out.
	if ro_doc is not None and getattr(ro_doc, "status", None) in AUTO_CLOSE_RO_STATUSES:
		if not bool(params.get("keep_open_on_close")):
			return True, f"Repair Order {ro_doc.name} reached status '{ro_doc.status}'"

	if ctype == "manual":
		return False, ""

	if ctype == "ro_closed":
		target_statuses = params.get("statuses") or list(AUTO_CLOSE_RO_STATUSES)
		if ro_doc is not None and getattr(ro_doc, "status", None) in target_statuses:
			return True, f"Repair Order {ro_doc.name} reached status '{ro_doc.status}'"
		return False, ""

	if ctype == "ro_status_in":
		target_statuses = params.get("statuses") or []
		if ro_doc is not None and target_statuses and getattr(ro_doc, "status", None) in target_statuses:
			return True, f"Repair Order {ro_doc.name} status reached '{ro_doc.status}'"
		return False, ""

	if ctype == "ro_approval_status_in":
		target_approvals = params.get("approval_statuses") or [
			"Pending Approval",
			"Approved",
			"Partially Approved",
		]
		if ro_doc is not None and getattr(ro_doc, "approval_status", None) in target_approvals:
			return True, f"Repair Order {ro_doc.name} approval status reached '{ro_doc.approval_status}'"
		return False, ""

	if ctype == "ro_has_supplement_or_service":
		if ro_doc is None:
			return False, ""
		services = list(getattr(ro_doc, "services", None) or [])
		baseline_count = params.get("baseline_service_count")
		keywords = [
			str(k).strip().lower()
			for k in (params.get("keywords") or [])
			if isinstance(k, str) and k.strip()
		]

		if baseline_count is not None:
			if len(services) > cint(baseline_count):
				return (
					True,
					f"Service or supplement added to {ro_doc.name} ({len(services)} > {cint(baseline_count)})",
				)
		elif services and not keywords:
			return True, f"Service present on {ro_doc.name}"

		if keywords:
			for svc in services:
				title_blob = " ".join(
					str(getattr(svc, f, "") or "") for f in ("service_title", "advisor_notes", "correction_code")
				).lower()
				if any(kw in title_blob for kw in keywords):
					if baseline_count is None or _ts_gt(getattr(svc, "modified", None), baseline_ts):
						return True, f"Matching service '{getattr(svc, 'service_title', '')}' found on {ro_doc.name}"

		cur_approval = getattr(ro_doc, "approval_status", None)
		if cur_approval in ("Pending Approval", "Approved", "Partially Approved"):
			if "baseline_approval_status" in params:
				if cur_approval != params.get("baseline_approval_status"):
					return True, f"Repair Order {ro_doc.name} approval status advanced to '{cur_approval}'"
			elif baseline_ts is not None and _ts_gt(getattr(ro_doc, "modified", None), baseline_ts):
				return True, f"Repair Order {ro_doc.name} approval status is '{cur_approval}'"

		return False, ""

	if ctype == "ro_parts_hold_cleared":
		if ro_doc is None:
			return False, ""
		if cint(getattr(ro_doc, "manual_parts_hold", 0)) == 0 and getattr(ro_doc, "status", None) != "Waiting on Parts":
			return True, f"Parts hold cleared on {ro_doc.name} (status: '{ro_doc.status}')"
		return False, ""

	if ctype == "po_submitted_or_received":
		ro_name = ro_doc.name if ro_doc is not None else (ref_name if ref_dt == "Repair Order" else None)
		return _evaluate_po_or_pr_activity(ro_name, item_doc, params, baseline_ts)

	if ctype == "ro_parts_covered":
		ro_name = ro_doc.name if ro_doc is not None else (ref_name if ref_dt == "Repair Order" else None)
		po_ok, po_reason = _evaluate_po_or_pr_activity(ro_name, item_doc, params, baseline_ts)
		if po_ok:
			return True, po_reason
		if ro_doc is None:
			return False, ""
		has_stock, all_avail = _ro_approved_stock_status(ro_doc)
		baseline_had_deficit = params.get("baseline_had_parts_deficit")
		if baseline_had_deficit is True:
			if all_avail:
				return True, f"All required parts are now covered in stock for {ro_doc.name}"
		elif baseline_had_deficit is None and has_stock and all_avail:
			return True, f"All required parts are covered in stock for {ro_doc.name}"
		return False, ""

	if ctype == "ro_schedule_updated":
		ro_name = ro_doc.name if ro_doc is not None else (ref_name if ref_dt == "Repair Order" else None)
		if not ro_name:
			return False, ""
		target_statuses = params.get("statuses") or []
		entries = _get_ro_schedule_entries(ro_name, include_cancelled=bool(target_statuses))
		if target_statuses:
			for ent in entries:
				if ent.get("status") in target_statuses:
					return True, f"Schedule Entry {ent['name']} reached status '{ent['status']}'"
		non_cancelled = [e for e in entries if e.get("status") != "Cancelled"]
		baseline_sched_count = params.get("baseline_schedule_count")
		baseline_sched_mod = _parse_ts(params.get("baseline_schedule_modified"))
		if baseline_sched_count is not None and len(non_cancelled) > cint(baseline_sched_count):
			return True, f"New Schedule Entry created for {ro_name}"
		for ent in non_cancelled:
			if baseline_sched_mod is not None:
				if _ts_gt(ent.get("modified"), baseline_sched_mod):
					return True, f"Schedule Entry {ent['name']} updated for {ro_name}"
			elif baseline_ts is not None and _ts_gte(ent.get("modified"), baseline_ts):
				return True, f"Schedule Entry {ent['name']} updated for {ro_name}"
		return False, ""

	if ctype == "ro_sms_or_email_sent":
		ro_name = ro_doc.name if ro_doc is not None else (ref_name if ref_dt == "Repair Order" else None)
		if not ro_name:
			return False, ""
		sms_rows = _get_outbound_sms_for_ro(ro_name)
		baseline_sms_count = params.get("baseline_sms_count")
		if baseline_sms_count is not None:
			if len(sms_rows) > cint(baseline_sms_count):
				return True, f"Outbound SMS sent for {ro_name}"
		else:
			for row in sms_rows:
				if _ts_gte(row.get("creation") or row.get("sent_at"), baseline_ts):
					return True, f"Outbound SMS {row['name']} sent for {ro_name}"

		comm_rows = _get_outbound_comms_for_ro(ro_name)
		baseline_comm_count = params.get("baseline_comm_count")
		if baseline_comm_count is not None:
			if len(comm_rows) > cint(baseline_comm_count):
				return True, f"Outbound email/communication sent for {ro_name}"
		else:
			for row in comm_rows:
				if _ts_gte(row.get("creation"), baseline_ts):
					return True, f"Outbound communication {row['name']} sent for {ro_name}"
		return False, ""

	if ctype == "schedule_converted_to_ro":
		if se_doc is None:
			return False, ""
		if getattr(se_doc, "repair_order", None):
			return True, f"Schedule Entry {se_doc.name} converted to Repair Order {se_doc.repair_order}"
		if getattr(se_doc, "status", None) in _TERMINAL_SCHEDULE_STATUSES:
			return True, f"Schedule Entry {se_doc.name} reached status '{se_doc.status}'"
		return False, ""

	return False, ""


def _auto_clear_item(item_doc: Any, reason: str) -> bool:
	"""Mark a Shop Action Item Resolved (auto_cleared=1) and save so on_update hooks fire."""
	try:
		if item_doc.status not in ("Open", "In Progress"):
			return False
		item_doc.status = "Resolved"
		item_doc.auto_cleared = 1
		item_doc.resolution_notes = f"Auto-cleared: {reason}"
		item_doc.resolved_by = frappe.session.user or "Administrator"
		item_doc.resolved_at = now_datetime()
		item_doc.save(ignore_permissions=True)
		return True
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Shop Action Item Auto-Clear Failed ({getattr(item_doc, 'name', '')})",
		)
		return False


def evaluate_and_clear_action_items_for_reference(
	reference_doctype: str | None,
	reference_name: str | None,
	*,
	doc: Any = None,
) -> list[str]:
	"""Evaluate all open/in-progress Shop Action Items for a reference and auto-clear satisfied ones."""
	if not reference_doctype or not reference_name or not _clearing_schema_ready():
		return []

	in_progress = getattr(frappe.flags, "_clearing_refs_in_progress", None)
	if in_progress is None:
		in_progress = set()
		frappe.flags._clearing_refs_in_progress = in_progress

	ref_key = (reference_doctype, reference_name)
	if ref_key in in_progress:
		return []

	in_progress.add(ref_key)
	cleared: list[str] = []
	try:
		rows = frappe.get_all(
			"Shop Action Item",
			filters={
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"status": ["in", ["Open", "In Progress"]],
			},
			fields=["name", "source"],
			order_by="creation asc",
		)
		if not rows:
			return []

		ro_doc = doc if (doc is not None and reference_doctype == "Repair Order") else None
		se_doc = doc if (doc is not None and reference_doctype == "Schedule Entry") else None

		for row in rows:
			if row.get("source") == "Rule Snooze":
				continue
			try:
				item_doc = frappe.get_doc("Shop Action Item", row["name"])
				if item_doc.status not in ("Open", "In Progress") or item_doc.source == "Rule Snooze":
					continue
				should_clear, reason = evaluate_clearing_condition(
					item_doc,
					ro_doc=ro_doc,
					se_doc=se_doc,
				)
				if should_clear and _auto_clear_item(item_doc, reason):
					cleared.append(item_doc.name)
			except Exception:
				frappe.log_error(
					message=frappe.get_traceback(),
					title=f"Shop Action Item Clearing Eval Failed ({row.get('name', '')})",
				)
	finally:
		in_progress.discard(ref_key)

	return cleared


def sweep_open_action_items(
	*,
	repair_order: str | None = None,
	limit: int = 100,
) -> list[str]:
	"""Lightweight read-time safety sweep for open/in-progress Shop Action Items."""
	if not _clearing_schema_ready():
		return []

	filters: dict[str, Any] = {"status": ["in", ["Open", "In Progress"]]}
	if repair_order:
		filters["reference_doctype"] = "Repair Order"
		filters["reference_name"] = repair_order

	safe_limit = max(1, min(cint(limit) or 100, 500))
	rows = frappe.get_all(
		"Shop Action Item",
		filters=filters,
		fields=["name", "source", "reference_doctype", "reference_name"],
		order_by="creation asc",
		limit_page_length=safe_limit,
	)
	if not rows:
		return []

	ro_cache: dict[str, Any] = {}
	se_cache: dict[str, Any] = {}
	cleared: list[str] = []

	for row in rows:
		if row.get("source") == "Rule Snooze":
			continue
		ref_dt = row.get("reference_doctype")
		ref_name = row.get("reference_name")
		if not ref_dt or not ref_name:
			continue

		ro_doc = None
		se_doc = None
		if ref_dt == "Repair Order":
			if ref_name not in ro_cache:
				ro_cache[ref_name] = (
					frappe.get_doc("Repair Order", ref_name)
					if frappe.db.exists("Repair Order", ref_name)
					else None
				)
			ro_doc = ro_cache[ref_name]
		elif ref_dt == "Schedule Entry":
			if ref_name not in se_cache:
				se_cache[ref_name] = (
					frappe.get_doc("Schedule Entry", ref_name)
					if frappe.db.exists("DocType", "Schedule Entry")
					and frappe.db.exists("Schedule Entry", ref_name)
					else None
				)
			se_doc = se_cache[ref_name]

		try:
			item_doc = frappe.get_doc("Shop Action Item", row["name"])
			if item_doc.status not in ("Open", "In Progress") or item_doc.source == "Rule Snooze":
				continue
			should_clear, reason = evaluate_clearing_condition(
				item_doc,
				ro_doc=ro_doc,
				se_doc=se_doc,
			)
			if should_clear and _auto_clear_item(item_doc, reason):
				cleared.append(item_doc.name)
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Shop Action Item Sweep Failed ({row.get('name', '')})",
			)

	return cleared


def _collect_item_matched_ros_for_procurement(doc: Any) -> set[str]:
	"""Find Repair Orders with open procurement-linked action items matching items on a PO/PR."""
	ro_names: set[str] = set()
	if not _clearing_schema_ready():
		return ro_names
	item_codes = sorted(
		{
			str(row.item_code).strip()
			for row in (getattr(doc, "items", None) or [])
			if getattr(row, "item_code", None)
		}
	)
	if not item_codes:
		return ro_names
	try:
		open_items = frappe.get_all(
			"Shop Action Item",
			filters={
				"status": ["in", ["Open", "In Progress"]],
				"reference_doctype": "Repair Order",
				"clearing_condition_type": ["in", ["po_submitted_or_received", "ro_parts_covered"]],
			},
			fields=["reference_name", "item_code", "clearing_condition_json"],
			limit_page_length=200,
		)
		item_set = set(item_codes)
		for row in open_items:
			ref_name = row.get("reference_name")
			if not ref_name:
				continue
			if row.get("item_code") in item_set:
				ro_names.add(ref_name)
				continue
			params = _parse_json_dict(row.get("clearing_condition_json"))
			target_codes = set(params.get("item_codes") or params.get("part_codes") or [])
			if target_codes & item_set:
				ro_names.add(ref_name)
	except Exception:
		pass
	return ro_names


def on_repair_order_update_clear_actions(doc, method=None) -> None:
	"""Auto-clear satisfied Shop Action Items when a Repair Order updates."""
	_ = method
	try:
		if not doc or not getattr(doc, "name", None):
			return
		evaluate_and_clear_action_items_for_reference("Repair Order", doc.name, doc=doc)
		origin_se = getattr(doc, "origin_schedule_entry", None)
		if origin_se:
			evaluate_and_clear_action_items_for_reference("Schedule Entry", origin_se)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Clearing Hook Error (Repair Order {getattr(doc, 'name', '')})",
		)


def on_schedule_entry_update_clear_actions(doc, method=None) -> None:
	"""Auto-clear satisfied Shop Action Items when a Schedule Entry updates."""
	_ = method
	try:
		if not doc or not getattr(doc, "name", None):
			return
		evaluate_and_clear_action_items_for_reference("Schedule Entry", doc.name, doc=doc)
		ro_name = getattr(doc, "repair_order", None)
		if ro_name:
			evaluate_and_clear_action_items_for_reference("Repair Order", ro_name)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Clearing Hook Error (Schedule Entry {getattr(doc, 'name', '')})",
		)


def on_purchase_order_submit_clear_actions(doc, method=None) -> None:
	"""Auto-clear parts/PO Shop Action Items when a Purchase Order is submitted."""
	_ = method
	try:
		if not doc or not getattr(doc, "name", None):
			return
		ro_names: set[str] = set()
		for field in ("repair_order", "custom_repair_order"):
			val = getattr(doc, field, None)
			if val:
				ro_names.add(val)
		ro_names.update(_collect_item_matched_ros_for_procurement(doc))
		for ro_name in sorted(ro_names):
			evaluate_and_clear_action_items_for_reference("Repair Order", ro_name)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Clearing Hook Error (Purchase Order {getattr(doc, 'name', '')})",
		)


def on_purchase_receipt_submit_clear_actions(doc, method=None) -> None:
	"""Auto-clear parts/PO Shop Action Items when a Purchase Receipt is submitted."""
	_ = method
	try:
		if not doc or not getattr(doc, "name", None):
			return
		ro_names: set[str] = set()
		for field in ("repair_order", "custom_repair_order"):
			val = getattr(doc, field, None)
			if val:
				ro_names.add(val)

		po_col = _get_po_ro_column("Purchase Order")
		if po_col:
			linked_pos = sorted(
				{
					str(row.purchase_order).strip()
					for row in (getattr(doc, "items", None) or [])
					if getattr(row, "purchase_order", None)
				}
			)
			if linked_pos:
				for ro_val in frappe.get_all(
					"Purchase Order",
					filters={"name": ["in", linked_pos]},
					pluck=po_col,
				):
					if ro_val:
						ro_names.add(ro_val)

		ro_names.update(_collect_item_matched_ros_for_procurement(doc))
		for ro_name in sorted(ro_names):
			evaluate_and_clear_action_items_for_reference("Repair Order", ro_name)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Clearing Hook Error (Purchase Receipt {getattr(doc, 'name', '')})",
		)


def on_sms_audit_after_insert_clear_actions(doc, method=None) -> None:
	"""Auto-clear customer comms Shop Action Items when an outbound SMS is logged."""
	_ = method
	try:
		if not doc:
			return
		if (
			getattr(doc, "direction", None) == "Outbound"
			and getattr(doc, "reference_doctype", None) == "Repair Order"
			and getattr(doc, "reference_name", None)
		):
			evaluate_and_clear_action_items_for_reference("Repair Order", doc.reference_name)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Clearing Hook Error (SMS Audit Log {getattr(doc, 'name', '')})",
		)


def on_communication_after_insert_clear_actions(doc, method=None) -> None:
	"""Auto-clear customer comms Shop Action Items when an outbound Communication is logged."""
	_ = method
	try:
		if not doc:
			return
		if (
			getattr(doc, "sent_or_received", None) == "Sent"
			and getattr(doc, "reference_doctype", None) == "Repair Order"
			and getattr(doc, "reference_name", None)
		):
			evaluate_and_clear_action_items_for_reference("Repair Order", doc.reference_name)
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Clearing Hook Error (Communication {getattr(doc, 'name', '')})",
		)


__all__ = [
	"AUTO_CLOSE_RO_STATUSES",
	"CLEARING_CONDITION_DESCRIPTIONS",
	"CLEARING_CONDITION_TYPES",
	"build_clearing_condition_for_ro",
	"evaluate_and_clear_action_items_for_reference",
	"evaluate_clearing_condition",
	"on_communication_after_insert_clear_actions",
	"on_purchase_order_submit_clear_actions",
	"on_purchase_receipt_submit_clear_actions",
	"on_repair_order_update_clear_actions",
	"on_schedule_entry_update_clear_actions",
	"on_sms_audit_after_insert_clear_actions",
	"sweep_open_action_items",
]
