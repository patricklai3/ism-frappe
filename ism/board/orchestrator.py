# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Lightweight Active-RO Shop Board AI orchestrator (STT + Action Item Feed + natural-language follow-up)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import frappe

from ism.board.ai_providers import BoardAIProvider, get_ai_provider, normalize_ai_result
from ism.board.contracts import BoardContractError, parse_ro_from_thread_key
from ism.board.media_storage import (
	DEFAULT_MAX_AUDIO_MB,
	DEFAULT_MAX_IMAGE_MB,
	fetch_event_media_bytes,
)

MAX_TOTAL_MEDIA_BYTES = 20 * 1024 * 1024  # 20 MB aggregate multimodal cap per capture
MAX_AI_ERROR_LENGTH = 280
MAX_RECENT_MESSAGES = 4
MAX_RECENT_MESSAGE_CHARS = 250

_SECRET_SCRUB_RE = re.compile(
	r"(?:key|token|bearer|authorization)\s*[:=]\s*\S+|\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]+",
	re.IGNORECASE,
)
_SHORT_ACK_SET: frozenset[str] = frozenset(
	{
		"ok",
		"okay",
		"got it",
		"thanks",
		"thank you",
		"copy",
		"copy that",
		"roger",
		"k",
		"kk",
		"yep",
		"yes",
		"no",
		"done",
		"thx",
		"np",
		"all good",
		"sounds good",
		"will do",
		"noted",
	}
)


@contextmanager
def _as_system_user():
	"""Temporarily elevate to Administrator for verified internal RO context & event updates."""
	prev_user = getattr(frappe.session, "user", None) or "Administrator"
	if prev_user == "Administrator":
		yield prev_user
		return
	frappe.set_user("Administrator")
	try:
		yield prev_user
	finally:
		frappe.set_user(prev_user)


def _is_mock_ai_provider() -> bool:
	try:
		raw = frappe.db.get_single_value("ISM Settings", "board_ai_provider")
	except Exception:
		raw = None
	return str(raw or "").strip().lower() in ("mock", "mock (deterministic test)", "deterministic")


def _safe_json_loads(raw: Any, default: Any) -> Any:
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(str(raw))
	except (TypeError, ValueError):
		return default


def _is_truthy_flag(val: Any) -> bool:
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, float)):
		return val != 0
	if isinstance(val, str):
		return val.strip().lower() in ("1", "true", "yes", "y", "on")
	return False


def _safe_error_text(exc: Exception) -> str:
	raw = f"{type(exc).__name__}: {exc}".strip()
	cleaned = _SECRET_SCRUB_RE.sub("[REDACTED]", raw)
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	if len(cleaned) > MAX_AI_ERROR_LENGTH:
		cleaned = cleaned[: MAX_AI_ERROR_LENGTH - 1].rstrip() + "…"
	return cleaned or "Unexpected Board AI error."


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


def should_skip_ai_for_capture(
	text: str | None,
	intent_tag: str | None,
	attachments: list[dict[str, Any]] | None,
	metadata: dict[str, Any] | None,
	*,
	actor_type: str | None = None,
) -> bool:
	"""Zero-cost pre-filter skipping AI when capture needs neither STT nor action extraction."""
	meta = metadata if isinstance(metadata, dict) else {}
	effective_actor = str(actor_type or meta.get("actor_type") or "").strip().upper()
	if effective_actor in ("AI", "SYSTEM"):
		return True

	if _is_truthy_flag(meta.get("skip_ai")):
		return True

	items = attachments if isinstance(attachments, list) else []
	has_audio = any(
		isinstance(a, dict)
		and str(a.get("content_type") or a.get("mime_type") or "").strip().lower().startswith("audio/")
		for a in items
	)
	if has_audio:
		return False

	norm_intent = str(intent_tag or meta.get("intent_tag") or "").strip().upper()
	if norm_intent == "FYI":
		return True

	has_any_attachment = len(items) > 0
	if not has_any_attachment:
		cleaned = re.sub(r"[.!?,;:]+$", "", " ".join(str(text or "").split())).strip().lower()
		if not cleaned or cleaned in _SHORT_ACK_SET:
			return True

	return False


def gather_ro_comms_context(thread_doc: Any, event_doc: Any) -> dict[str, Any]:
	"""Gather compact RO header + up to 4 recent thread messages (~150 tokens, zero heavy ERP queries)."""
	thread_key = getattr(thread_doc, "name", None) or getattr(event_doc, "thread", "")
	ro_name = (
		getattr(event_doc, "repair_order", None)
		or getattr(thread_doc, "repair_order", None)
		or parse_ro_from_thread_key(thread_key)
		or ""
	)
	metadata = _safe_json_loads(getattr(event_doc, "metadata_json", None), {})
	if not isinstance(metadata, dict):
		metadata = {}

	context: dict[str, Any] = {
		"ro_name": ro_name,
		"ro_status": None,
		"approval_status": None,
		"vehicle": getattr(thread_doc, "vehicle", None),
		"customer": getattr(thread_doc, "customer", None),
		"bay": getattr(thread_doc, "bay", None),
		"assigned_technician": getattr(thread_doc, "assigned_technician", None),
		"service_count": 0,
		"odometer_in": None,
		"odometer_out": None,
		"intent_tag": getattr(event_doc, "intent_tag", None) or "",
		"actor_role": getattr(event_doc, "actor_type", None) or "",
		"event_metadata": metadata,
		"recent_messages": [],
	}

	with _as_system_user():
		if ro_name and frappe.db.exists("Repair Order", ro_name):
			ro = frappe.db.get_value(
				"Repair Order",
				ro_name,
				[
					"name",
					"status",
					"approval_status",
					"vehicle",
					"customer",
					"odometer_in",
					"odometer_out",
				],
				as_dict=True,
			)
			if ro:
				context["ro_status"] = ro.get("status")
				context["approval_status"] = ro.get("approval_status")
				context["customer"] = ro.get("customer") or context["customer"]
				context["odometer_in"] = ro.get("odometer_in")
				context["odometer_out"] = ro.get("odometer_out")

				vehicle_name = ro.get("vehicle") or context["vehicle"]
				if vehicle_name and frappe.db.exists("Repair Vehicle", vehicle_name):
					veh = frappe.db.get_value(
						"Repair Vehicle",
						vehicle_name,
						["model_year", "manufacturer", "model"],
						as_dict=True,
					)
					if veh:
						parts = [
							str(veh.get("model_year") or "").strip(),
							str(veh.get("manufacturer") or "").strip(),
							str(veh.get("model") or "").strip(),
						]
						label = " ".join(p for p in parts if p)
						context["vehicle"] = label or vehicle_name
					else:
						context["vehicle"] = vehicle_name
				else:
					context["vehicle"] = vehicle_name

			try:
				context["service_count"] = frappe.db.count(
					"Repair Order Service",
					{"parent": ro_name, "parenttype": "Repair Order"},
				)
			except Exception:
				context["service_count"] = 0

		# Last 4 TEXT / MEDIA_CAPTURE messages on this thread before event_doc
		ev_name = getattr(event_doc, "name", None)
		ev_ts = getattr(event_doc, "event_timestamp", None)
		ev_creation = getattr(event_doc, "creation", None)

		if thread_key:
			if ev_name and ev_ts is not None and ev_creation is not None:
				recent_rows = frappe.db.sql(
					"""
					SELECT actor_type, body_text, transcript
					FROM `tabShop Board Event`
					WHERE thread = %s
						AND name != %s
						AND payload_type IN ('TEXT', 'MEDIA_CAPTURE')
						AND (event_timestamp, creation, name) < (%s, %s, %s)
					ORDER BY event_timestamp DESC, creation DESC, name DESC
					LIMIT %s
					""",
					(thread_key, ev_name, ev_ts, ev_creation, ev_name, MAX_RECENT_MESSAGES),
					as_dict=True,
				)
			else:
				recent_rows = frappe.db.sql(
					"""
					SELECT actor_type, body_text, transcript
					FROM `tabShop Board Event`
					WHERE thread = %s
						AND name != %s
						AND payload_type IN ('TEXT', 'MEDIA_CAPTURE')
					ORDER BY event_timestamp DESC, creation DESC, name DESC
					LIMIT %s
					""",
					(thread_key, ev_name or "", MAX_RECENT_MESSAGES),
					as_dict=True,
				)
			recent_rows.reverse()
			recent_msgs: list[dict[str, str]] = []
			for row in recent_rows:
				raw_txt = (row.get("transcript") or row.get("body_text") or "").strip()
				if not raw_txt:
					continue
				recent_msgs.append(
					{
						"actor_type": str(row.get("actor_type") or ""),
						"text": raw_txt[:MAX_RECENT_MESSAGE_CHARS],
					}
				)
			context["recent_messages"] = recent_msgs

	return context


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

	max_audio_bytes = int(
		float(getattr(cfg, "media_max_audio_mb", DEFAULT_MAX_AUDIO_MB) or DEFAULT_MAX_AUDIO_MB) * 1024 * 1024
	)
	max_image_bytes = int(
		float(getattr(cfg, "media_max_image_mb", DEFAULT_MAX_IMAGE_MB) or DEFAULT_MAX_IMAGE_MB) * 1024 * 1024
	)

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


def enqueue_board_event(event_name: str) -> dict[str, Any]:
	"""Enqueue a ``Shop Board Event`` for lightweight AI analysis (synchronous under ``in_test`` or Mock provider)."""
	if not isinstance(event_name, str) or not event_name.strip():
		raise BoardContractError("event_name must be a non-empty string.")
	clean_name = event_name.strip()

	with _as_system_user():
		event_doc = frappe.get_doc("Shop Board Event", clean_name)
		thread_doc = frappe.get_doc("Shop Board Thread", event_doc.thread)

		attachments = _safe_json_loads(getattr(event_doc, "media_attachments", None), [])
		metadata = _safe_json_loads(getattr(event_doc, "metadata_json", None), {})
		if (
			str(getattr(event_doc, "actor_type", "") or "").strip().upper() in ("AI", "SYSTEM")
			or should_skip_ai_for_capture(
				getattr(event_doc, "body_text", None),
				getattr(event_doc, "intent_tag", None),
				attachments if isinstance(attachments, list) else [],
				metadata if isinstance(metadata, dict) else {},
				actor_type=getattr(event_doc, "actor_type", None),
			)
		):
			event_doc.db_set({"ai_status": "NONE", "ai_error": ""}, update_modified=True)
			return {
				"event_name": clean_name,
				"thread_key": thread_doc.name,
				"ai_status": "NONE",
				"queued": False,
				"synchronous": False,
				"skipped": True,
			}

		event_doc.db_set({"ai_status": "QUEUED", "ai_error": ""}, update_modified=True)
		_publish_ai_status(
			event_doc=event_doc,
			thread_key=thread_doc.name,
			thread_type=thread_doc.thread_type,
			repair_order=event_doc.repair_order or thread_doc.repair_order,
			ai_status="QUEUED",
			ai_error=None,
		)

		if getattr(frappe.flags, "in_test", False) or _is_mock_ai_provider():
			result = process_board_event(clean_name)
			result["queued"] = False
			result["synchronous"] = True
			return result

		frappe.enqueue(
			"ism.board.orchestrator.process_board_event",
			queue="default",
			enqueue_after_commit=True,
			event_name=clean_name,
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
	*,
	provider: BoardAIProvider | None = None,
	result_dispatcher: Callable[[Any, dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
	"""Process a ``Shop Board Event``: compact RO context + media -> STT / follow-up / Action Items."""
	if not isinstance(event_name, str) or not event_name.strip():
		raise BoardContractError("event_name must be a non-empty string.")
	clean_name = event_name.strip()

	with _as_system_user():
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
			context = gather_ro_comms_context(thread_doc, event_doc)
			media_blobs = _load_event_media_blobs(event_doc)

			raw_result = active_provider.analyze_capture(
				context=context,
				text=event_doc.body_text,
				media_blobs=media_blobs,
			)
			result = normalize_ai_result(raw_result)

			if result["transcript"]:
				event_doc.db_set({"transcript": result["transcript"]}, update_modified=True)
				event_doc.transcript = result["transcript"]

			if result_dispatcher is not None:
				dispatch_fn = result_dispatcher
			else:
				from ism.board.gateways import dispatch_board_ai_result

				dispatch_fn = dispatch_board_ai_result

			dispatch_result = dispatch_fn(event_doc, result)

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
				"outcome": dispatch_result.get("outcome") if isinstance(dispatch_result, dict) else None,
				"ai_result": result,
				"dispatch_result": dispatch_result,
			}
		except Exception as exc:
			safe_err = _safe_error_text(exc)
			event_doc.db_set({"ai_status": "FAILED", "ai_error": safe_err}, update_modified=True)
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
					title=f"Shop Board AI Failed: {event_doc.name}",
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
	"MAX_RECENT_MESSAGES",
	"MAX_RECENT_MESSAGE_CHARS",
	"MAX_TOTAL_MEDIA_BYTES",
	"enqueue_board_event",
	"gather_ro_comms_context",
	"process_board_event",
	"should_skip_ai_for_capture",
]
