# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Bounded Frappe RQ multimodal AI orchestrator for Shop OS Blackboard events."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import frappe
from frappe.utils import now_datetime

from ism.board.ai_providers import MultimodalProvider, get_ai_provider, normalize_decision
from ism.board.contracts import BoardContractError
from ism.board.media_storage import (
	DEFAULT_MAX_AUDIO_MB,
	DEFAULT_MAX_IMAGE_MB,
	fetch_event_media_bytes,
)
from ism.board.pricing import load_server_pricing_config
from ism.board.projections import recompute_thread_projection

MAX_TOTAL_MEDIA_BYTES = 20 * 1024 * 1024  # 20 MB aggregate multimodal cap per capture
MAX_AI_ERROR_LENGTH = 280

_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.IGNORECASE)
_DELAY_HINT_RE = re.compile(
	r"\b(?:delay(?:ed)?|seized|reschedule|extra\s+\d|extra\s+hour|behind\s+schedule|push\s+back|move\s+slot)\b",
	re.IGNORECASE,
)
_SECRET_SCRUB_RE = re.compile(r"(?:key|token|bearer|authorization)\s*[:=]\s*\S+", re.IGNORECASE)


def _safe_json_loads(raw: Any, default: Any) -> Any:
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(str(raw))
	except (TypeError, ValueError):
		return default


def _safe_error_text(exc: Exception) -> str:
	raw = f"{type(exc).__name__}: {exc}".strip()
	cleaned = _SECRET_SCRUB_RE.sub("[REDACTED]", raw)
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	if len(cleaned) > MAX_AI_ERROR_LENGTH:
		cleaned = cleaned[: MAX_AI_ERROR_LENGTH - 1].rstrip() + "…"
	return cleaned or "Unexpected AI orchestrator error."


def _publish_ai_status(
	*,
	event_doc: Any,
	thread_key: str,
	thread_type: str,
	repair_order: str | None,
	ai_status: str,
	ai_error: str | None = None,
) -> None:
	frappe.publish_realtime(
		"ism_board_ai_status",
		{
			"event_name": event_doc.name,
			"thread_key": thread_key,
			"thread_type": thread_type,
			"repair_order": repair_order,
			"ai_status": ai_status,
			"ai_error": ai_error,
		},
		doctype="Shop Board Thread",
		docname=thread_key,
		after_commit=True,
	)


def _gather_repair_order_context(
	ro_name: str,
	text: str,
	intent_tag: str,
	clarification_patch: dict[str, Any] | None,
) -> dict[str, Any]:
	from ism.api.agent_serializers import serialize_repair_order_compact

	context: dict[str, Any] = {"repair_order_name": ro_name}
	if not ro_name or not frappe.db.exists("Repair Order", ro_name):
		return context

	ro_doc = frappe.get_doc("Repair Order", ro_name)
	vehicle_meta: dict[str, Any] = {}
	if ro_doc.vehicle and frappe.db.exists("Repair Vehicle", ro_doc.vehicle):
		vehicle_meta = (
			frappe.db.get_value(
				"Repair Vehicle",
				ro_doc.vehicle,
				["vin", "license_plate", "model_year", "manufacturer", "model"],
				as_dict=True,
			)
			or {}
		)

	compact_ro = serialize_repair_order_compact(ro_doc, vehicle_meta=vehicle_meta)
	compact_ro["deposit_amount"] = float(getattr(ro_doc, "deposit_amount", 0.0) or 0.0)
	compact_ro["paid_amount"] = float(getattr(ro_doc, "paid_amount", 0.0) or 0.0)
	compact_ro["outstanding_amount"] = float(
		getattr(ro_doc, "outstanding_amount", compact_ro.get("grand_total", 0.0)) or 0.0
	)
	compact_ro["sales_invoice"] = getattr(ro_doc, "sales_invoice", None)
	compact_ro["payment_status"] = getattr(ro_doc, "payment_status", None)
	compact_ro["manual_parts_hold"] = int(getattr(ro_doc, "manual_parts_hold", 0) or 0)
	if ro_doc.customer:
		cust_name = frappe.db.get_value("Customer", ro_doc.customer, "customer_name")
		if cust_name:
			compact_ro["customer_name"] = cust_name
	if vehicle_meta:
		compact_ro["model_year"] = vehicle_meta.get("model_year")
		compact_ro["manufacturer"] = vehicle_meta.get("manufacturer")
		compact_ro["model"] = vehicle_meta.get("model")

	context["repair_order"] = compact_ro

	# Parts stock check (guarded)
	try:
		from ism.api.agent_procurement import check_parts_stock

		stock_env = check_parts_stock(repair_order=ro_name)
		if isinstance(stock_env, dict):
			context["parts_stock"] = stock_env.get("data", stock_env)
	except Exception:
		context["parts_stock"] = {}

	# Vehicle intelligence (guarded)
	if ro_doc.vehicle:
		try:
			from ism.api.agent_fleet import get_vehicle_intelligence

			intel_env = get_vehicle_intelligence(vehicle=ro_doc.vehicle, history_limit=5)
			if isinstance(intel_env, dict):
				context["vehicle_intelligence"] = intel_env.get("data", intel_env)
		except Exception:
			context["vehicle_intelligence"] = {}

	# Active schedule entries linked to this RO
	try:
		active_schedules = frappe.get_all(
			"Schedule Entry",
			filters={
				"repair_order": ro_name,
				"status": ["not in", ["Cancelled", "Completed"]],
			},
			fields=[
				"name",
				"status",
				"scheduled_date",
				"scheduled_time",
				"estimated_duration",
				"service_bay",
				"assigned_technician",
			],
			order_by="scheduled_date asc, scheduled_time asc",
			limit=5,
		)
		context["active_schedule_entries"] = active_schedules
	except Exception:
		context["active_schedule_entries"] = []

	# Candidate reschedule slots when delay/reschedule intent is present (strictly Internal Capacity T)
	patch_str = json.dumps(clarification_patch or {}, default=str)
	if (
		_DELAY_HINT_RE.search(text or "")
		or _DELAY_HINT_RE.search(patch_str)
		or (intent_tag or "").strip().lower() in ("blocker", "reschedule", "delay")
	):
		try:
			from ism.api.agent_scheduling import find_available_slots

			slots_env = find_available_slots(
				repair_order=ro_name,
				duration_minutes=120,
				days_ahead=5,
				max_slots=5,
			)
			slots_data = slots_env.get("data", {}) if isinstance(slots_env, dict) else {}
			context["candidate_slots"] = list(slots_data.get("slots") or [])
		except Exception:
			context["candidate_slots"] = []
	else:
		context["candidate_slots"] = []

	# Internal server-side pricing config (never passed through Context Firewall)
	try:
		context["pricing_config"] = asdict(load_server_pricing_config())
	except Exception:
		context["pricing_config"] = {}

	return context


def _gather_intake_context(
	text: str,
	metadata: dict[str, Any],
	clarification_patch: dict[str, Any] | None,
) -> dict[str, Any]:
	context: dict[str, Any] = {
		"decoded_vin": {},
		"matched_vehicles": [],
		"service_packages": [],
		"open_appointments": [],
		"candidate_slots": [],
	}

	patch = clarification_patch or {}
	vin = str(patch.get("vin") or metadata.get("vin") or "").strip().upper()
	if not vin:
		vin_match = _VIN_RE.search(text or "")
		if vin_match:
			vin = vin_match.group(1).upper()

	# VIN decode
	if vin:
		try:
			from ism.api.agent_fleet import decode_vehicle_vin

			decoded_env = decode_vehicle_vin(vin=vin)
			if isinstance(decoded_env, dict):
				context["decoded_vin"] = decoded_env.get("data", decoded_env)
		except Exception:
			context["decoded_vin"] = {"vin": vin}

	# Vehicle / customer search
	search_query = vin or str(metadata.get("plate") or metadata.get("customer") or "").strip()
	if search_query:
		try:
			from ism.api.agent_fleet import search_vehicles

			search_env = search_vehicles(query=search_query, limit=5)
			search_data = search_env.get("data", {}) if isinstance(search_env, dict) else {}
			context["matched_vehicles"] = list(search_data.get("vehicles") or [])
		except Exception:
			context["matched_vehicles"] = []

	# Active service packages
	try:
		context["service_packages"] = frappe.get_all(
			"Service Package",
			fields=[
				"name",
				"package_name",
				"applicable_model",
				"standard_labor_hours",
				"service_rate",
				"correction_code",
			],
			limit=30,
		)
	except Exception:
		context["service_packages"] = []

	# Open upcoming appointments without an RO (for walk-in conversion)
	try:
		context["open_appointments"] = frappe.get_all(
			"Schedule Entry",
			filters={
				"repair_order": ["in", ["", None]],
				"status": ["not in", ["Cancelled", "Completed"]],
			},
			fields=[
				"name",
				"title",
				"status",
				"scheduled_date",
				"scheduled_time",
				"customer",
				"provisional_customer_name",
				"repair_vehicle",
				"service_bay",
				"assigned_technician",
			],
			order_by="scheduled_date asc, scheduled_time asc",
			limit=10,
		)
	except Exception:
		context["open_appointments"] = []

	# Real candidate booking slots (Internal Capacity Time T)
	try:
		from ism.api.agent_scheduling import find_available_slots

		slots_env = find_available_slots(duration_minutes=60, days_ahead=7, max_slots=5)
		slots_data = slots_env.get("data", {}) if isinstance(slots_env, dict) else {}
		context["candidate_slots"] = list(slots_data.get("slots") or [])
	except Exception:
		context["candidate_slots"] = []

	try:
		context["pricing_config"] = asdict(load_server_pricing_config())
	except Exception:
		context["pricing_config"] = {}

	return context


def _gather_ap_receiving_context() -> dict[str, Any]:
	context: dict[str, Any] = {
		"open_purchase_orders": [],
		"suppliers": [],
	}

	try:
		open_pos = frappe.get_all(
			"Purchase Order",
			filters={
				"docstatus": 1,
				"status": ["not in", ["Completed", "Closed", "Cancelled"]],
			},
			fields=[
				"name",
				"supplier",
				"supplier_name",
				"transaction_date",
				"grand_total",
				"per_received",
				"per_billed",
				"status",
			],
			order_by="transaction_date desc, creation desc",
			limit=25,
		)
		po_names = [row["name"] for row in open_pos]
		items_by_po: dict[str, list[dict[str, Any]]] = {name: [] for name in po_names}
		if po_names:
			po_items = frappe.get_all(
				"Purchase Order Item",
				filters={"parent": ["in", po_names]},
				fields=[
					"parent",
					"item_code",
					"item_name",
					"qty",
					"received_qty",
					"rate",
					"amount",
					"uom",
				],
				order_by="idx asc",
			)
			for item in po_items:
				parent = item.get("parent")
				if parent in items_by_po:
					items_by_po[parent].append(
						{
							"item_code": item.get("item_code"),
							"item_name": item.get("item_name"),
							"qty": float(item.get("qty") or 0.0),
							"received_qty": float(item.get("received_qty") or 0.0),
							"pending_qty": max(
								0.0, float(item.get("qty") or 0.0) - float(item.get("received_qty") or 0.0)
							),
							"rate": float(item.get("rate") or 0.0),
							"amount": float(item.get("amount") or 0.0),
							"uom": item.get("uom"),
						}
					)

		for po in open_pos:
			po["items"] = items_by_po.get(po["name"], [])
		context["open_purchase_orders"] = open_pos
	except Exception:
		context["open_purchase_orders"] = []

	try:
		suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name"], limit=50)
		context["suppliers"] = suppliers
	except Exception:
		context["suppliers"] = []

	return context


def gather_thread_context(
	thread_doc: Any,
	event_doc: Any,
	clarification_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Gather compact, thread-type-aware ERP context for multimodal AI orchestration."""
	thread_type = str(getattr(thread_doc, "thread_type", None) or getattr(event_doc, "thread_type", None) or "REPAIR_ORDER").strip().upper()
	metadata = _safe_json_loads(getattr(event_doc, "metadata_json", None), {})
	if not isinstance(metadata, dict):
		metadata = {}
	media_attachments = _safe_json_loads(getattr(event_doc, "media_attachments", None), [])
	if not isinstance(media_attachments, list):
		media_attachments = []

	base_context: dict[str, Any] = {
		"thread_key": thread_doc.name,
		"thread_type": thread_type,
		"thread_title": getattr(thread_doc, "title", "") or "",
		"event_name": event_doc.name,
		"intent_tag": getattr(event_doc, "intent_tag", "") or "",
		"event_metadata": metadata,
		"media_attachments": media_attachments,
		"clarification_patch": clarification_patch or {},
	}

	text = str(getattr(event_doc, "body_text", "") or "")

	if thread_type == "REPAIR_ORDER":
		ro_name = getattr(event_doc, "repair_order", None) or getattr(thread_doc, "repair_order", None) or ""
		base_context.update(
			_gather_repair_order_context(
				ro_name=ro_name,
				text=text,
				intent_tag=base_context["intent_tag"],
				clarification_patch=clarification_patch,
			)
		)
	elif thread_type == "INTAKE":
		base_context.update(_gather_intake_context(text=text, metadata=metadata, clarification_patch=clarification_patch))
	elif thread_type == "AP_RECEIVING":
		base_context.update(_gather_ap_receiving_context())

	return base_context


def _load_event_media_blobs(
	event_doc: Any,
	*,
	settings: Any = None,
	max_total_bytes: int = MAX_TOTAL_MEDIA_BYTES,
) -> list[dict[str, Any]]:
	"""Stream media bytes via ``media_storage.fetch_event_media_bytes`` within payload caps."""
	attachments = _safe_json_loads(getattr(event_doc, "media_attachments", None), [])
	if not isinstance(attachments, list) or not attachments:
		return []

	cfg = settings
	if cfg is None:
		try:
			cfg = frappe.get_cached_doc("ISM Settings")
		except Exception:
			cfg = None

	max_audio_bytes = int(float(getattr(cfg, "media_max_audio_mb", DEFAULT_MAX_AUDIO_MB) or DEFAULT_MAX_AUDIO_MB) * 1024 * 1024)
	max_image_bytes = int(float(getattr(cfg, "media_max_image_mb", DEFAULT_MAX_IMAGE_MB) or DEFAULT_MAX_IMAGE_MB) * 1024 * 1024)

	blobs: list[dict[str, Any]] = []
	remaining_budget = max(0, int(max_total_bytes))
	event_media_envelope = {
		"name": event_doc.name,
		"media_refs": attachments,
	}

	for entry in attachments:
		if not isinstance(entry, dict):
			continue
		file_ref = entry.get("file_ref")
		if not isinstance(file_ref, str) or not file_ref.strip():
			continue
		if remaining_budget <= 0:
			break

		content_type = str(entry.get("content_type") or "").lower()
		per_item_cap = max_audio_bytes if content_type.startswith("audio/") else max_image_bytes
		effective_cap = min(per_item_cap, remaining_budget)
		if effective_cap <= 0:
			break

		media_ref, data_bytes = fetch_event_media_bytes(
			event_media_envelope,
			file_ref.strip(),
			user="Administrator",
			roles=("System Manager",),
			max_bytes=effective_cap,
			settings=cfg,
		)
		remaining_budget -= len(data_bytes)
		blobs.append(
			{
				"file_ref": media_ref.get("file_ref") or file_ref.strip(),
				"filename": media_ref.get("filename") or entry.get("filename") or "",
				"content_type": media_ref.get("content_type") or content_type or "application/octet-stream",
				"size_bytes": len(data_bytes),
				"data": data_bytes,
			}
		)

	return blobs


def _append_ai_failure_event(thread_doc: Any, source_event: Any, safe_error: str) -> None:
	try:
		err_event = frappe.get_doc(
			{
				"doctype": "Shop Board Event",
				"thread": thread_doc.name,
				"thread_type": thread_doc.thread_type,
				"repair_order": source_event.repair_order or thread_doc.repair_order,
				"event_timestamp": now_datetime(),
				"actor_type": "SYSTEM",
				"payload_type": "STATE_MUTATION",
				"target_role": "ALL",
				"body_text": f"AI processing failed: {safe_error}",
				"metadata_json": json.dumps(
					{
						"kind": "ai_error",
						"source_event": source_event.name,
						"error": safe_error,
					}
				),
				"resolution_state": "NONE",
				"parent_event": source_event.name,
				"ai_status": "NONE",
			}
		)
		err_event.insert(ignore_permissions=True)
		recompute_thread_projection(thread_doc.name, publish=True)
	except Exception:
		pass


def enqueue_board_event(
	event_name: str,
	clarification_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Enqueue a ``Shop Board Event`` for bounded AI orchestration (synchronous under ``in_test``)."""
	if not isinstance(event_name, str) or not event_name.strip():
		raise BoardContractError("event_name must be a non-empty string.")
	clean_name = event_name.strip()

	event_doc = frappe.get_doc("Shop Board Event", clean_name)
	thread_doc = frappe.get_doc("Shop Board Thread", event_doc.thread)

	event_doc.db_set({"ai_status": "QUEUED", "ai_error": ""}, update_modified=True)
	_publish_ai_status(
		event_doc=event_doc,
		thread_key=thread_doc.name,
		thread_type=thread_doc.thread_type,
		repair_order=event_doc.repair_order or thread_doc.repair_order,
		ai_status="QUEUED",
		ai_error=None,
	)

	if getattr(frappe.flags, "in_test", False):
		result = process_board_event(clean_name, clarification_patch=clarification_patch)
		result["queued"] = False
		result["synchronous"] = True
		return result

	frappe.enqueue(
		"ism.board.orchestrator.process_board_event",
		queue="default",
		enqueue_after_commit=True,
		event_name=clean_name,
		clarification_patch=clarification_patch,
		job_name=f"ism_board_event:{clean_name}",
	)
	return {
		"event_name": clean_name,
		"thread_key": thread_doc.name,
		"ai_status": "QUEUED",
		"queued": True,
		"synchronous": False,
	}


def process_board_event(
	event_name: str,
	clarification_patch: dict[str, Any] | None = None,
	*,
	provider: MultimodalProvider | None = None,
	decision_dispatcher: Callable[[Any, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
	"""Process a ``Shop Board Event`` with thread-aware context, media bytes, and AI decision routing."""
	if not isinstance(event_name, str) or not event_name.strip():
		raise BoardContractError("event_name must be a non-empty string.")
	clean_name = event_name.strip()

	event_doc = frappe.get_doc("Shop Board Event", clean_name)
	thread_doc = frappe.get_doc("Shop Board Thread", event_doc.thread)
	ro_name = event_doc.repair_order or thread_doc.repair_order

	active_provider = provider if provider is not None else get_ai_provider()
	if active_provider is None:
		event_doc.db_set({"ai_status": "NONE", "ai_error": ""}, update_modified=True)
		return {
			"event_name": event_doc.name,
			"thread_key": thread_doc.name,
			"ai_status": "NONE",
			"skipped": True,
			"reason": "Board AI provider is disabled.",
		}

	event_doc.db_set({"ai_status": "PROCESSING", "ai_error": ""}, update_modified=True)
	_publish_ai_status(
		event_doc=event_doc,
		thread_key=thread_doc.name,
		thread_type=thread_doc.thread_type,
		repair_order=ro_name,
		ai_status="PROCESSING",
		ai_error=None,
	)

	try:
		context = gather_thread_context(thread_doc, event_doc, clarification_patch=clarification_patch)
		media_blobs = _load_event_media_blobs(event_doc)

		raw_decision = active_provider.analyze_capture(
			thread_type=thread_doc.thread_type,
			context=context,
			text=event_doc.body_text,
			media_blobs=media_blobs,
			clarification_patch=clarification_patch,
		)
		decision = normalize_decision(raw_decision)

		# Persist transcript and confidence_score on the event (mutable fields)
		persist_updates: dict[str, Any] = {
			"confidence_score": decision["confidence"],
		}
		if decision["transcript"]:
			persist_updates["transcript"] = decision["transcript"]
		event_doc.db_set(persist_updates, update_modified=True)
		event_doc.confidence_score = decision["confidence"]
		if decision["transcript"]:
			event_doc.transcript = decision["transcript"]

		if decision_dispatcher is not None:
			dispatch_fn = decision_dispatcher
		else:
			from ism.board.gateways import dispatch_orchestrator_decision

			dispatch_fn = dispatch_orchestrator_decision

		dispatch_result = dispatch_fn(event_doc, decision)

		event_doc.db_set({"ai_status": "COMPLETED", "ai_error": ""}, update_modified=True)
		_publish_ai_status(
			event_doc=event_doc,
			thread_key=thread_doc.name,
			thread_type=thread_doc.thread_type,
			repair_order=ro_name,
			ai_status="COMPLETED",
			ai_error=None,
		)
		return {
			"event_name": event_doc.name,
			"thread_key": thread_doc.name,
			"ai_status": "COMPLETED",
			"decision": decision,
			"dispatch_result": dispatch_result,
		}
	except Exception as exc:
		safe_err = _safe_error_text(exc)
		event_doc.db_set({"ai_status": "FAILED", "ai_error": safe_err}, update_modified=True)
		_append_ai_failure_event(thread_doc, event_doc, safe_err)
		_publish_ai_status(
			event_doc=event_doc,
			thread_key=thread_doc.name,
			thread_type=thread_doc.thread_type,
			repair_order=ro_name,
			ai_status="FAILED",
			ai_error=safe_err,
		)
		try:
			frappe.log_error(
				title=f"Shop Board AI Orchestrator Failed: {event_doc.name}",
				message=safe_err,
			)
		except Exception:
			pass
		return {
			"event_name": event_doc.name,
			"thread_key": thread_doc.name,
			"ai_status": "FAILED",
			"ai_error": safe_err,
		}


__all__ = [
	"MAX_AI_ERROR_LENGTH",
	"MAX_TOTAL_MEDIA_BYTES",
	"enqueue_board_event",
	"gather_thread_context",
	"process_board_event",
]
