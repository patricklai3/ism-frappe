# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Multimodal AI provider adapters for the Shop OS Blackboard Orchestrator.

Provides:
- ``MultimodalProvider`` protocol
- ``GeminiRestProvider`` (pure HTTPS ``requests``, zero extra pip packages)
- ``OpenAICompatibleProvider`` (pure HTTPS ``requests``)
- ``MockMultimodalProvider`` (deterministic offline provider covering all P1 & P2 flows)
- ``get_ai_provider`` factory driven by ``ISM Settings``
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Protocol, runtime_checkable

import frappe
import requests

from ism.board.comms_firewall import (
	SanitizedCustomerFactPayload,
	draft_customer_message,
	resolve_customer_arrival_time,
	sanitize_for_customer_comms,
)
from ism.board.contracts import ALL_ACTIONS, BoardContractError
from ism.board.pricing import calculate_part_price

DEFAULT_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_HTTP_TIMEOUT_SECONDS = 45

PROVIDER_DISABLED = "Disabled"
PROVIDER_MOCK = "Mock (Deterministic Test)"
PROVIDER_GEMINI = "Gemini REST API"
PROVIDER_OPENAI = "OpenAI Compatible"

DEFAULT_PAYMENT_OPTIONS: tuple[str, ...] = (
	"Debit / Interac",
	"Credit Card",
	"E-Transfer",
	"Cash",
)

DECISION_SCHEMA: dict[str, Any] = {
	"type": "OBJECT",
	"properties": {
		"intent": {"type": "STRING"},
		"confidence": {"type": "NUMBER"},
		"audio_clarity_score": {"type": "NUMBER"},
		"transcript": {"type": "STRING"},
		"summary": {"type": "STRING"},
		"action": {"type": "STRING", "nullable": True},
		"action_payload": {"type": "OBJECT"},
		"missing_fields": {"type": "ARRAY", "items": {"type": "STRING"}},
		"clarification": {"type": "OBJECT", "nullable": True},
		"customer_comms": {"type": "OBJECT", "nullable": True},
		"followup_actions": {"type": "ARRAY", "items": {"type": "OBJECT"}},
	},
	"required": [
		"intent",
		"confidence",
		"audio_clarity_score",
		"transcript",
		"summary",
		"action",
		"action_payload",
		"missing_fields",
		"clarification",
		"customer_comms",
		"followup_actions",
	],
}

COMMS_RESPONSE_SCHEMA: dict[str, Any] = {
	"type": "OBJECT",
	"properties": {
		"subject": {"type": "STRING"},
		"body": {"type": "STRING"},
	},
	"required": ["subject", "body"],
}

_ORCHESTRATOR_SYSTEM_PROMPT = (
	"You are the bounded Shop OS Blackboard AI Orchestrator for an independent Tesla repair shop. "
	"Analyze the technician/advisor capture and ERP context, and return strict JSON matching the schema. "
	"Allowed actions: "
	+ ", ".join(ALL_ACTIONS)
	+ ". Never fabricate odometer_in, odometer_out, payment_method, bill_no, or ambiguous LH/RH vehicle side; "
	"when any mandatory field is missing or audio clarity is low (<0.75), populate missing_fields and clarification. "
	"Keep customer_comms strictly free of supplier costs, margins, Tesla OEM reference prices, bay/tech IDs, "
	"raw transcripts, slang, and arrival buffer wording."
)

_COMMS_FIREWALL_SYSTEM_PROMPT = (
	"You are drafting a polite, concise customer-facing message for an automotive service shop. "
	"Use ONLY the sanitized JSON facts provided. Never mention internal costs, supplier prices, "
	"margin percentages, Tesla OEM reference prices, bay numbers, technician IDs, internal notes, "
	"slang, or appointment buffer/leeway wording. Return JSON with 'subject' and 'body'."
)

_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.IGNORECASE)
_ODOMETER_RE = re.compile(
	r"(?:odometer|odo|mileage|km|mi)\s*(?:is|at|reading|:|#|=)?\s*(\d{1,3}(?:,\d{3})+|\d{3,7})\b"
	r"|\b(\d{4,7})\s*(?:km|kms|mi|miles)\b",
	re.IGNORECASE,
)
_DOLLAR_RE = re.compile(r"\$\s*(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)|\b(\d{2,6}(?:\.\d{1,2})?)\s*(?:cad|dollars?)\b", re.IGNORECASE)
_BILL_NO_RE = re.compile(
	r"(?:bill\s*(?:no|num|number|#)?|invoice\s*(?:no|num|number|#)?|inv\s*#?)\s*[:#-]?\s*"
	r"((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{2,24})\b",
	re.IGNORECASE,
)
_PO_NO_RE = re.compile(r"\b((?:PUR-ORD|PO)-[A-Z0-9-]{3,32})\b", re.IGNORECASE)
_EXTRA_HOURS_RE = re.compile(r"(?:extra|another|need|more|delay(?:ed)?\s*(?:by)?)\s*(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", re.IGNORECASE)
_EXTRA_MINS_RE = re.compile(r"(?:extra|another|need|more|delay(?:ed)?\s*(?:by)?)\s*(\d+)\s*(?:m|min|mins|minute|minutes)\b", re.IGNORECASE)


@runtime_checkable
class MultimodalProvider(Protocol):
	"""Protocol implemented by all Shop Board multimodal AI providers."""

	def analyze_capture(
		self,
		thread_type: str,
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
		clarification_patch: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		"""Analyze capture text + media bytes + ERP context into a normalized decision dict."""
		...

	def draft_customer_comms(
		self,
		sanitized_payload: SanitizedCustomerFactPayload,
		channel: str = "sms",
	) -> dict[str, Any]:
		"""Draft an isolated customer-facing SMS or Email from strictly sanitized facts."""
		...


def normalize_decision(raw: dict[str, Any] | None) -> dict[str, Any]:
	"""Normalize any provider decision dict into the frozen 11-key orchestrator schema."""
	data = raw if isinstance(raw, dict) else {}

	def _clamp_score(val: Any, default: float) -> float:
		try:
			num = float(val)
		except (TypeError, ValueError):
			return default
		return round(max(0.0, min(1.0, num)), 4)

	action = data.get("action")
	if isinstance(action, str):
		action = action.strip() or None
	else:
		action = None
	if action is not None and action not in ALL_ACTIONS:
		action = None

	action_payload = data.get("action_payload")
	if not isinstance(action_payload, dict):
		action_payload = {}

	raw_missing = data.get("missing_fields")
	missing_fields = (
		[str(item).strip() for item in raw_missing if str(item).strip()]
		if isinstance(raw_missing, list)
		else []
	)

	clarification = data.get("clarification")
	if not isinstance(clarification, dict):
		clarification = None

	customer_comms = data.get("customer_comms")
	if not isinstance(customer_comms, dict):
		customer_comms = None

	raw_followups = data.get("followup_actions")
	followup_actions: list[dict[str, Any]] = []
	if isinstance(raw_followups, list):
		for entry in raw_followups:
			if not isinstance(entry, dict):
				continue
			f_action = entry.get("action")
			if isinstance(f_action, str) and f_action.strip() in ALL_ACTIONS:
				f_payload = entry.get("action_payload")
				followup_actions.append(
					{
						"action": f_action.strip(),
						"action_payload": dict(f_payload) if isinstance(f_payload, dict) else {},
					}
				)

	return {
		"intent": str(data.get("intent") or "general_update").strip(),
		"confidence": _clamp_score(data.get("confidence"), 0.0),
		"audio_clarity_score": _clamp_score(data.get("audio_clarity_score"), 1.0),
		"transcript": str(data.get("transcript") or "").strip(),
		"summary": str(data.get("summary") or "").strip(),
		"action": action,
		"action_payload": dict(action_payload),
		"missing_fields": missing_fields,
		"clarification": dict(clarification) if clarification is not None else None,
		"customer_comms": dict(customer_comms) if customer_comms is not None else None,
		"followup_actions": followup_actions,
	}


def _extract_json_object(raw_text: str) -> dict[str, Any]:
	text = (raw_text or "").strip()
	if text.startswith("```"):
		text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
		text = re.sub(r"\s*```$", "", text)
	parsed = json.loads(text)
	if not isinstance(parsed, dict):
		raise ValueError("AI provider JSON response must be an object.")
	return parsed


def _encode_media_b64(blob: dict[str, Any]) -> tuple[str, str]:
	content_type = str(blob.get("content_type") or "application/octet-stream").strip()
	raw_data = blob.get("data")
	if isinstance(raw_data, str):
		return content_type, raw_data
	if isinstance(raw_data, (bytes, bytearray)):
		return content_type, base64.b64encode(bytes(raw_data)).decode("ascii")
	return content_type, ""


def _build_capture_prompt(
	thread_type: str,
	context: dict[str, Any],
	text: str | None,
	clarification_patch: dict[str, Any] | None,
) -> str:
	prompt_payload = {
		"thread_type": thread_type,
		"capture_text": (text or "").strip(),
		"clarification_patch": clarification_patch or {},
		"context": context or {},
	}
	return json.dumps(prompt_payload, default=str, ensure_ascii=False)


class GeminiRestProvider:
	"""Pure HTTPS Gemini REST multimodal adapter (zero extra pip dependencies)."""

	def __init__(
		self,
		*,
		api_key: str,
		endpoint: str | None = None,
		orchestrator_model: str | None = None,
		comms_model: str | None = None,
		timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
	) -> None:
		self.api_key = (api_key or "").strip()
		self.endpoint = (endpoint or DEFAULT_GEMINI_ENDPOINT).strip().rstrip("/")
		self.orchestrator_model = (orchestrator_model or DEFAULT_GEMINI_MODEL).strip()
		self.comms_model = (comms_model or self.orchestrator_model or DEFAULT_GEMINI_MODEL).strip()
		self.timeout_seconds = max(5, int(timeout_seconds or DEFAULT_HTTP_TIMEOUT_SECONDS))

	def _generate_content(
		self,
		*,
		model: str,
		system_instruction: str,
		parts: list[dict[str, Any]],
		response_schema: dict[str, Any],
	) -> dict[str, Any]:
		if not self.api_key:
			raise BoardContractError("Gemini REST API key is not configured in ISM Settings.")

		url = f"{self.endpoint}/models/{model}:generateContent"
		headers = {
			"Content-Type": "application/json",
			"x-goog-api-key": self.api_key,
		}
		body = {
			"systemInstruction": {"parts": [{"text": system_instruction}]},
			"contents": [{"role": "user", "parts": parts}],
			"generationConfig": {
				"temperature": 0.1,
				"responseMimeType": "application/json",
				"responseSchema": response_schema,
			},
		}

		resp = requests.post(url, headers=headers, json=body, timeout=self.timeout_seconds)
		resp.raise_for_status()
		payload = resp.json()

		candidates = payload.get("candidates") or []
		if not candidates:
			raise ValueError("Gemini API returned no candidates.")
		content_parts = (candidates[0].get("content") or {}).get("parts") or []
		text_chunks = [p.get("text", "") for p in content_parts if isinstance(p, dict) and p.get("text")]
		if not text_chunks:
			raise ValueError("Gemini API response contained no text part.")
		return _extract_json_object("\n".join(text_chunks))

	def analyze_capture(
		self,
		thread_type: str,
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
		clarification_patch: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		parts: list[dict[str, Any]] = [
			{"text": _build_capture_prompt(thread_type, context, text, clarification_patch)}
		]
		for blob in media_blobs or []:
			mime_type, b64 = _encode_media_b64(blob)
			if b64:
				parts.append({"inline_data": {"mime_type": mime_type, "data": b64}})

		raw = self._generate_content(
			model=self.orchestrator_model,
			system_instruction=_ORCHESTRATOR_SYSTEM_PROMPT,
			parts=parts,
			response_schema=DECISION_SCHEMA,
		)
		return normalize_decision(raw)

	def draft_customer_comms(
		self,
		sanitized_payload: SanitizedCustomerFactPayload,
		channel: str = "sms",
	) -> dict[str, Any]:
		if not isinstance(sanitized_payload, SanitizedCustomerFactPayload):
			raise BoardContractError(
				"draft_customer_comms requires a SanitizedCustomerFactPayload instance."
			)
		prompt = json.dumps(
			{
				"channel": channel,
				"facts": sanitized_payload.to_dict(),
			},
			ensure_ascii=False,
		)
		raw = self._generate_content(
			model=self.comms_model,
			system_instruction=_COMMS_FIREWALL_SYSTEM_PROMPT,
			parts=[{"text": prompt}],
			response_schema=COMMS_RESPONSE_SCHEMA,
		)
		return {
			"channel": channel,
			"subject": str(raw.get("subject") or "").strip(),
			"body": str(raw.get("body") or "").strip(),
		}


class OpenAICompatibleProvider:
	"""Pure HTTPS OpenAI-compatible multimodal adapter (zero extra pip dependencies)."""

	def __init__(
		self,
		*,
		api_key: str,
		endpoint: str | None = None,
		orchestrator_model: str | None = None,
		comms_model: str | None = None,
		timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
	) -> None:
		self.api_key = (api_key or "").strip()
		self.endpoint = (endpoint or DEFAULT_OPENAI_ENDPOINT).strip().rstrip("/")
		self.orchestrator_model = (orchestrator_model or DEFAULT_OPENAI_MODEL).strip()
		self.comms_model = (comms_model or self.orchestrator_model or DEFAULT_OPENAI_MODEL).strip()
		self.timeout_seconds = max(5, int(timeout_seconds or DEFAULT_HTTP_TIMEOUT_SECONDS))

	def _chat_completion(
		self,
		*,
		model: str,
		system_prompt: str,
		user_content: Any,
	) -> dict[str, Any]:
		if not self.api_key:
			raise BoardContractError("OpenAI-compatible API key is not configured in ISM Settings.")

		url = f"{self.endpoint}/chat/completions"
		headers = {
			"Content-Type": "application/json",
			"Authorization": f"Bearer {self.api_key}",
		}
		body = {
			"model": model,
			"temperature": 0.1,
			"response_format": {"type": "json_object"},
			"messages": [
				{"role": "system", "content": system_prompt},
				{"role": "user", "content": user_content},
			],
		}

		resp = requests.post(url, headers=headers, json=body, timeout=self.timeout_seconds)
		resp.raise_for_status()
		payload = resp.json()
		choices = payload.get("choices") or []
		if not choices:
			raise ValueError("OpenAI-compatible API returned no choices.")
		content = (choices[0].get("message") or {}).get("content") or ""
		return _extract_json_object(content)

	def analyze_capture(
		self,
		thread_type: str,
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
		clarification_patch: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		content_items: list[dict[str, Any]] = [
			{"type": "text", "text": _build_capture_prompt(thread_type, context, text, clarification_patch)}
		]
		for blob in media_blobs or []:
			mime_type, b64 = _encode_media_b64(blob)
			if not b64:
				continue
			if mime_type.startswith("image/"):
				content_items.append(
					{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}
				)
			elif mime_type.startswith("audio/"):
				audio_fmt = mime_type.split("/", 1)[1].split(";", 1)[0].replace("x-m4a", "mp4")
				content_items.append(
					{"type": "input_audio", "input_audio": {"data": b64, "format": audio_fmt}}
				)

		raw = self._chat_completion(
			model=self.orchestrator_model,
			system_prompt=_ORCHESTRATOR_SYSTEM_PROMPT,
			user_content=content_items,
		)
		return normalize_decision(raw)

	def draft_customer_comms(
		self,
		sanitized_payload: SanitizedCustomerFactPayload,
		channel: str = "sms",
	) -> dict[str, Any]:
		if not isinstance(sanitized_payload, SanitizedCustomerFactPayload):
			raise BoardContractError(
				"draft_customer_comms requires a SanitizedCustomerFactPayload instance."
			)
		prompt = json.dumps(
			{
				"channel": channel,
				"facts": sanitized_payload.to_dict(),
			},
			ensure_ascii=False,
		)
		raw = self._chat_completion(
			model=self.comms_model,
			system_prompt=_COMMS_FIREWALL_SYSTEM_PROMPT,
			user_content=prompt,
		)
		return {
			"channel": channel,
			"subject": str(raw.get("subject") or "").strip(),
			"body": str(raw.get("body") or "").strip(),
		}


def _extract_payment_method(text: str, patch: dict[str, Any]) -> str | None:
	raw = patch.get("payment_method")
	if isinstance(raw, str) and raw.strip():
		return raw.strip()
	lowered = text.lower()
	if "interac" in lowered or "debit" in lowered:
		return "Debit / Interac"
	if "credit" in lowered or "visa" in lowered or "mastercard" in lowered or "amex" in lowered:
		return "Credit Card"
	if "e-transfer" in lowered or "etransfer" in lowered or "e transfer" in lowered:
		return "E-Transfer"
	if re.search(r"\bcash\b", lowered):
		return "Cash"
	return None


def _extract_odometer(text: str, patch: dict[str, Any], metadata: dict[str, Any]) -> int | None:
	for key in ("odometer_out", "odometer_in", "odometer", "value"):
		val = patch.get(key)
		if val is not None and str(val).strip().isdigit():
			return int(str(val).strip())
	for key in ("extracted_odometer", "odometer_out", "odometer_in", "odometer"):
		val = metadata.get(key)
		if val is not None and str(val).strip().isdigit():
			return int(str(val).strip())
	match = _ODOMETER_RE.search(text or "")
	if match:
		raw = (match.group(1) or match.group(2) or "").replace(",", "")
		if raw.isdigit():
			return int(raw)
	return None


def _extract_side(text: str, patch: dict[str, Any]) -> str | None:
	raw = patch.get("side") or patch.get("selected_option") or patch.get("service_position")
	if isinstance(raw, str) and raw.strip():
		norm = raw.strip().lower()
		if norm in ("front left", "left", "lh", "driver", "driver side"):
			return "Front Left"
		if norm in ("front right", "right", "rh", "passenger", "passenger side"):
			return "Front Right"
		if norm in ("both", "both sides", "lh/rh", "pair"):
			return "Both"

	lowered = (text or "").lower()
	if re.search(r"\bboth\s+sides?\b|\blh\s*(?:and|&|/)\s*rh\b", lowered):
		return "Both"
	if re.search(r"\bfront\s+left\b|\bleft\s+front\b|\blh\b|\bdriver(?:'s)?\s+side\b", lowered):
		return "Front Left"
	if re.search(r"\bfront\s+right\b|\bright\s+front\b|\brh\b|\bpassenger(?:'s)?\s+side\b", lowered):
		return "Front Right"
	return None


def _extract_amount(text: str, patch: dict[str, Any]) -> float | None:
	raw = patch.get("amount")
	if raw is not None:
		try:
			val = float(raw)
			if val > 0:
				return round(val, 2)
		except (TypeError, ValueError):
			pass
	match = _DOLLAR_RE.search(text or "")
	if match:
		num_str = (match.group(1) or match.group(2) or "").replace(",", "")
		try:
			val = float(num_str)
			if val > 0:
				return round(val, 2)
		except ValueError:
			pass
	return None


class MockMultimodalProvider:
	"""Deterministic offline provider covering all Phase 1 and Phase 2 Shop OS scenarios."""

	def draft_customer_comms(
		self,
		sanitized_payload: SanitizedCustomerFactPayload,
		channel: str = "sms",
	) -> dict[str, Any]:
		if not isinstance(sanitized_payload, SanitizedCustomerFactPayload):
			raise BoardContractError(
				"draft_customer_comms requires a SanitizedCustomerFactPayload instance."
			)
		first_name = (
			sanitized_payload.customer_name.split()[0]
			if sanitized_payload.customer_name
			else "there"
		)
		ref_part = f" ({sanitized_payload.reference_id})" if sanitized_payload.reference_id else ""
		vehicle = sanitized_payload.vehicle_summary or "your vehicle"

		item_summaries: list[str] = []
		for item in sanitized_payload.approved_items:
			desc = item.get("description") or ""
			price = item.get("price")
			if desc and price is not None:
				item_summaries.append(f"{desc} (${float(price):.2f} {sanitized_payload.currency})")
			elif desc:
				item_summaries.append(desc)

		if channel == "email":
			subject = f"Service Update for {vehicle}{ref_part}"
			lines = [f"Hi {first_name},", ""]
			if sanitized_payload.finding_summary:
				lines.append(
					f"Here is an update on {vehicle}{ref_part}: {sanitized_payload.finding_summary.rstrip('.')}."
				)
			if item_summaries:
				lines.append("")
				lines.append("Estimate summary:")
				for entry in item_summaries:
					lines.append(f"- {entry}")
			if sanitized_payload.eta_window:
				lines.append("")
				lines.append(f"Arrival time: {sanitized_payload.eta_window}.")
			if sanitized_payload.approval_url:
				lines.append("")
				lines.append(f"Review and approve online: {sanitized_payload.approval_url}")
			lines.append("")
			lines.append(f"Best regards,\n{sanitized_payload.shop_name}")
			return {"channel": "email", "subject": subject, "body": "\n".join(lines).strip()}

		parts = [f"Hi {first_name}, update for {vehicle}{ref_part}:"]
		if sanitized_payload.finding_summary:
			parts.append(sanitized_payload.finding_summary.rstrip(".") + ".")
		if item_summaries:
			parts.append("Estimated: " + ", ".join(item_summaries) + ".")
		if sanitized_payload.eta_window:
			parts.append(f"Appointment time: {sanitized_payload.eta_window}.")
		if sanitized_payload.approval_url:
			parts.append(f"Approve: {sanitized_payload.approval_url}")
		return {"channel": "sms", "subject": "", "body": " ".join(parts).strip()}

	def analyze_capture(
		self,
		thread_type: str,
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
		clarification_patch: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		ctx = context or {}
		patch = clarification_patch or {}
		metadata = ctx.get("event_metadata") or {}
		raw_text = (text or "").strip()
		if not raw_text and metadata.get("mock_transcript"):
			raw_text = str(metadata["mock_transcript"]).strip()
		lowered = raw_text.lower()
		intent_tag = re.sub(r"[\s_-]+", " ", str(ctx.get("intent_tag") or "").strip().lower()).strip()
		norm_thread_type = str(thread_type or "REPAIR_ORDER").strip().upper()

		# 1. Noisy audio detection (applies across all thread types unless already clarified)
		is_noisy = bool(
			metadata.get("simulate_noisy_audio")
			or metadata.get("noisy_audio")
			or "noisy" in lowered
			or "inaudible" in lowered
			or "garbled" in lowered
			or any("noisy" in str(b.get("filename") or "").lower() for b in (media_blobs or []))
		)
		if is_noisy and not patch.get("selected_option") and not patch.get("confirmed_action"):
			return normalize_decision(
				{
					"intent": "low_clarity_audio",
					"confidence": 0.60,
					"audio_clarity_score": 0.60,
					"transcript": raw_text or "[unintelligible shop noise]",
					"summary": "Audio clarity too low (0.60); confirmation required before any update.",
					"action": None,
					"action_payload": {},
					"missing_fields": ["audio_clarity"],
					"clarification": {
						"prompt": "Audio was noisy — please pick the intended action or re-record.",
						"action_type": "LOG_RO_NOTE",
						"target_role": "TECH",
						"options": [
							{"label": "Log as FYI Note", "value": "LOG_RO_NOTE"},
							{"label": "Flag Parts Hold", "value": "SET_PARTS_HOLD"},
							{"label": "Add Supplement Finding", "value": "ADD_SUPPLEMENT_DRAFT"},
						],
					},
					"customer_comms": None,
					"followup_actions": [],
				}
			)

		if norm_thread_type == "INTAKE":
			return self._analyze_intake(ctx, raw_text, lowered, intent_tag, patch, metadata)

		if norm_thread_type == "AP_RECEIVING":
			return self._analyze_ap_receiving(ctx, raw_text, lowered, intent_tag, patch, metadata)

		return self._analyze_repair_order(ctx, raw_text, lowered, intent_tag, media_blobs, patch, metadata)

	def _analyze_repair_order(
		self,
		ctx: dict[str, Any],
		raw_text: str,
		lowered: str,
		intent_tag: str,
		media_blobs: list[dict[str, Any]],
		patch: dict[str, Any],
		metadata: dict[str, Any],
	) -> dict[str, Any]:
		ro_ctx = ctx.get("repair_order") or {}
		ro_name = ro_ctx.get("name") or ctx.get("repair_order_name") or ""
		clean_photo_refs = [
			str(m.get("file_ref"))
			for m in (ctx.get("media_attachments") or [])
			if isinstance(m, dict)
			and str(m.get("content_type") or "").startswith("image/")
			and m.get("file_ref")
		]

		# A. FYI / Silent RO note
		if (
			intent_tag == "fyi"
			or lowered.startswith("fyi")
			or re.search(r"\bfyi\b", lowered)
			or patch.get("selected_option") == "LOG_RO_NOTE"
		):
			note_text = re.sub(r"^\s*fyi\s*[:,-]?\s*", "", raw_text, flags=re.IGNORECASE).strip() or raw_text
			return normalize_decision(
				{
					"intent": "fyi_note",
					"confidence": 0.96,
					"audio_clarity_score": 0.98,
					"transcript": raw_text,
					"summary": f"Logged note on {ro_name or 'RO'}: {note_text[:90]}",
					"action": "LOG_RO_NOTE",
					"action_payload": {
						"repair_order": ro_name,
						"note": note_text or "Technician FYI update logged.",
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": None,
					"followup_actions": [],
				}
			)

		# B. Deposit capture ("paid $500 deposit")
		if "deposit" in lowered:
			amount = _extract_amount(raw_text, patch)
			payment_method = _extract_payment_method(raw_text, patch)
			if amount is None:
				return normalize_decision(
					{
						"intent": "record_deposit",
						"confidence": 0.78,
						"audio_clarity_score": 0.96,
						"transcript": raw_text,
						"summary": "Deposit amount needed before recording payment.",
						"action": "RECORD_DEPOSIT",
						"action_payload": {"repair_order": ro_name},
						"missing_fields": ["amount"],
						"clarification": {
							"prompt": "Enter deposit amount (CAD) received from customer.",
							"input_field": "amount",
							"input_type": "number",
							"action_type": "RECORD_DEPOSIT",
							"target_role": "MANAGER",
							"options": [],
						},
						"customer_comms": None,
						"followup_actions": [],
					}
				)
			return normalize_decision(
				{
					"intent": "record_deposit",
					"confidence": 0.96,
					"audio_clarity_score": 0.97,
					"transcript": raw_text,
					"summary": f"Record ${amount:.2f} CAD deposit on {ro_name or 'RO'}",
					"action": "RECORD_DEPOSIT",
					"action_payload": {
						"repair_order": ro_name,
						"amount": amount,
						"payment_method": payment_method,
						"payment_options": list(DEFAULT_PAYMENT_OPTIONS),
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": None,
					"followup_actions": [],
				}
			)

		# C. Delay / Reschedule ("seized bolt, need extra 2 hours")
		if (
			any(kw in lowered for kw in ("seized", "delay", "reschedule", "extra 2 hour", "extra hour", "behind schedule"))
			or intent_tag == "blocker"
			and "hour" in lowered
		):
			extra_minutes = 120
			h_match = _EXTRA_HOURS_RE.search(raw_text)
			m_match = _EXTRA_MINS_RE.search(raw_text)
			if h_match:
				extra_minutes = max(15, int(round(float(h_match.group(1)) * 60)))
			elif m_match:
				extra_minutes = max(15, int(m_match.group(1)))

			slots = list(ctx.get("candidate_slots") or [])
			active_schedules = list(ctx.get("active_schedule_entries") or [])
			schedule_entry_name = active_schedules[0].get("name") if active_schedules else None
			first_slot = slots[0] if slots else None
			internal_start = first_slot.get("start_time") if isinstance(first_slot, dict) else None
			slot_date = first_slot.get("date") if isinstance(first_slot, dict) else None

			# Seam 4: format customer arrival time using resolve_customer_arrival_time (zero buffer wording)
			customer_arrival = (
				resolve_customer_arrival_time(internal_start) if internal_start else None
			)
			eta_window = (
				f"{slot_date} at {customer_arrival}"
				if slot_date and customer_arrival
				else customer_arrival
			)
			sanitized = sanitize_for_customer_comms(
				ro_ctx,
				finding_summary="We encountered additional mechanical work on your vehicle and updated your appointment schedule",
				approved_items=[],
				clean_photo_refs=clean_photo_refs,
				eta_window=slot_date,
				internal_start_time=internal_start,
			)
			comms_draft = draft_customer_message(sanitized, channel="sms", provider=self)

			return normalize_decision(
				{
					"intent": "reschedule_delay",
					"confidence": 0.94,
					"audio_clarity_score": 0.96,
					"transcript": raw_text,
					"summary": f"Job delayed (+{extra_minutes}m) — select updated schedule slot",
					"action": "RESCHEDULE_APPOINTMENT",
					"action_payload": {
						"repair_order": ro_name,
						"schedule_entry": schedule_entry_name,
						"extra_minutes": extra_minutes,
						"reason": raw_text or "Additional repair time required",
						"candidate_slots": slots,
						"customer_arrival_time": customer_arrival,
						"eta_window": eta_window,
						"optional_customer_sms": comms_draft["body"],
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": comms_draft,
					"followup_actions": [],
				}
			)

		# D. Checkout with / without odometer ("done, odometer 64210" / "ready for checkout")
		if (
			intent_tag == "checkout"
			or any(kw in lowered for kw in ("checkout", "check out", "bill & release", "bill and release", "odometer", "done"))
		):
			odometer_out = _extract_odometer(raw_text, patch, metadata)
			payment_method = _extract_payment_method(raw_text, patch)
			if odometer_out is None:
				return normalize_decision(
					{
						"intent": "checkout_repair_order",
						"confidence": 0.78,
						"audio_clarity_score": 0.96,
						"transcript": raw_text,
						"summary": "Final odometer reading required before checkout & invoice release.",
						"action": "CHECKOUT_REPAIR_ORDER",
						"action_payload": {
							"repair_order": ro_name,
							"payment_options": list(DEFAULT_PAYMENT_OPTIONS),
						},
						"missing_fields": ["odometer_out"],
						"clarification": {
							"prompt": "Enter final odometer reading (odometer_out) to complete checkout.",
							"input_field": "odometer_out",
							"input_type": "number",
							"action_type": "CHECKOUT_REPAIR_ORDER",
							"target_role": "TECH",
							"options": [],
						},
						"customer_comms": None,
						"followup_actions": [],
					}
				)
			return normalize_decision(
				{
					"intent": "checkout_repair_order",
					"confidence": 0.96,
					"audio_clarity_score": 0.98,
					"transcript": raw_text,
					"summary": f"Checkout & release {ro_name or 'RO'} (Odometer: {odometer_out:,})",
					"action": "CHECKOUT_REPAIR_ORDER",
					"action_payload": {
						"repair_order": ro_name,
						"odometer_out": odometer_out,
						"payment_method": payment_method,
						"payment_options": list(DEFAULT_PAYMENT_OPTIONS),
						"grand_total": ro_ctx.get("grand_total", 0.0),
						"deposit_amount": ro_ctx.get("deposit_amount", 0.0),
						"outstanding_amount": ro_ctx.get("outstanding_amount", ro_ctx.get("grand_total", 0.0)),
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": None,
					"followup_actions": [],
				}
			)

		# E. Ambiguous finding ("control arm bushing torn") + post-clarification multi-action flow
		if any(kw in lowered for kw in ("control arm", "bushing", "torn", "supplement", "ball joint", "strut")):
			side = _extract_side(raw_text, patch)
			if not side:
				return normalize_decision(
					{
						"intent": "supplement_finding",
						"confidence": 0.74,
						"audio_clarity_score": 0.96,
						"transcript": raw_text,
						"summary": "Torn control arm bushing found — confirm vehicle side before adding supplement.",
						"action": "ADD_SUPPLEMENT_DRAFT",
						"action_payload": {
							"repair_order": ro_name,
							"finding": "Control arm bushing torn",
						},
						"missing_fields": ["side"],
						"clarification": {
							"prompt": "Which side has the torn control arm bushing?",
							"field": "side",
							"action_type": "ADD_SUPPLEMENT_DRAFT",
							"target_role": "TECH",
							"options": [
								{"label": "Front Left", "value": "Front Left"},
								{"label": "Front Right", "value": "Front Right"},
								{"label": "Both", "value": "Both"},
							],
						},
						"customer_comms": None,
						"followup_actions": [],
					}
				)

			# Post-clarification: price part internally, build ADD_SUPPLEMENT_DRAFT + DRAFT_PURCHASE_ORDER + sanitized DISPATCH_CUSTOMER_SMS
			qty = 2 if side == "Both" else 1
			pricing_result = calculate_part_price(
				tesla_oem_price=240.0,
				cost=110.0,
				part_source="Aftermarket",
				part_condition="New",
				part_name=f"Front Control Arm ({side})",
			)
			unit_sell = pricing_result["sell_price"]
			labor_hours = 1.5 * qty
			labor_rate = 165.0
			est_line_total = round((unit_sell * qty) + (labor_hours * labor_rate), 2)
			service_title = f"Replace Front Control Arm ({side})"

			sanitized_payload = sanitize_for_customer_comms(
				ro_ctx,
				finding_summary=f"During inspection we found a torn {side.lower()} control arm bushing requiring replacement",
				approved_items=[{"description": service_title, "price": est_line_total}],
				clean_photo_refs=clean_photo_refs,
			)
			comms_draft = draft_customer_message(sanitized_payload, channel="sms", provider=self)

			part_label = f"Front Control Arm ({side})"
			matched_item_code: str | None = None
			matched_supplier: str | None = None
			parts_stock = ctx.get("parts_stock") or {}
			stock_rows = parts_stock.get("parts") or parts_stock.get("items") or []
			if isinstance(stock_rows, list):
				for row in stock_rows:
					if not isinstance(row, dict):
						continue
					row_name = str(row.get("item_name") or row.get("part_name") or row.get("item_code") or "").lower()
					if "control arm" in row_name and row.get("item_code"):
						matched_item_code = str(row["item_code"]).strip()
						if row.get("supplier") or row.get("default_supplier"):
							matched_supplier = str(row.get("supplier") or row.get("default_supplier")).strip()
						break

			supp_part_entry: dict[str, Any] = {
				"part_name": part_label,
				"qty": qty,
				"rate": unit_sell,
				"sell_price": unit_sell,
				"internal_cost": pricing_result["cost"],
				"pricing_regime": pricing_result["pricing_regime"],
			}
			po_part_entry: dict[str, Any] = {
				"item_name": part_label,
				"part_name": part_label,
				"qty": qty,
				"rate": pricing_result["cost"],
			}
			if matched_item_code:
				supp_part_entry["item_code"] = matched_item_code
				po_part_entry["item_code"] = matched_item_code
			if matched_supplier:
				po_part_entry["supplier"] = matched_supplier

			return normalize_decision(
				{
					"intent": "supplement_finding_resolved",
					"confidence": 0.96,
					"audio_clarity_score": 0.97,
					"transcript": raw_text,
					"summary": f"Add supplement '{service_title}', draft PO, and prepare customer SMS",
					"action": "ADD_SUPPLEMENT_DRAFT",
					"action_payload": {
						"repair_order": ro_name,
						"service_title": service_title,
						"service_position": side,
						"side": side,
						"duplicate_opposite_side": side == "Both",
						"labor_hours": labor_hours,
						"parts": [supp_part_entry],
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": comms_draft,
					"followup_actions": [
						{
							"action": "DRAFT_PURCHASE_ORDER",
							"action_payload": {
								"repair_order": ro_name,
								"parts_data": [po_part_entry],
								"notes": f"Supplement draft PO for {service_title}",
							},
						},
						{
							"action": "DISPATCH_CUSTOMER_SMS",
							"action_payload": {
								"repair_order": ro_name,
								"channel": "sms",
								"message": comms_draft["body"],
								"sanitized_facts_hash": comms_draft["sanitized_facts_hash"],
								"clean_photo_refs": comms_draft["clean_photo_refs"],
							},
						},
					],
				}
			)

		# Default fallback: safe RO note
		return normalize_decision(
			{
				"intent": "general_ro_note",
				"confidence": 0.92,
				"audio_clarity_score": 0.97,
				"transcript": raw_text,
				"summary": f"Log floor update on {ro_name or 'RO'}",
				"action": "LOG_RO_NOTE",
				"action_payload": {
					"repair_order": ro_name,
					"note": raw_text or "Shop floor update recorded.",
				},
				"missing_fields": [],
				"clarification": None,
				"customer_comms": None,
				"followup_actions": [],
			}
		)

	def _analyze_intake(
		self,
		ctx: dict[str, Any],
		raw_text: str,
		lowered: str,
		intent_tag: str,
		patch: dict[str, Any],
		metadata: dict[str, Any],
	) -> dict[str, Any]:
		# Walk-in appointment conversion
		if intent_tag == "walk in" or any(kw in lowered for kw in ("walk-in", "walk in", "convert appointment", "arrived for appointment")):
			odometer_in = _extract_odometer(raw_text, patch, metadata)
			pending_appts = list(ctx.get("open_appointments") or [])
			matched_appt = pending_appts[0] if pending_appts else {}
			if odometer_in is None:
				return normalize_decision(
					{
						"intent": "convert_appointment_to_ro",
						"confidence": 0.78,
						"audio_clarity_score": 0.96,
						"transcript": raw_text,
						"summary": "Odometer-in reading required to convert appointment to Repair Order.",
						"action": "CONVERT_APPOINTMENT_TO_RO",
						"action_payload": {"schedule_entry": matched_appt.get("name")},
						"missing_fields": ["odometer_in"],
						"clarification": {
							"prompt": "Enter arrival odometer reading (odometer_in) to open Repair Order.",
							"input_field": "odometer_in",
							"input_type": "number",
							"action_type": "CONVERT_APPOINTMENT_TO_RO",
							"target_role": "MANAGER",
							"options": [],
						},
						"customer_comms": None,
						"followup_actions": [],
					}
				)
			return normalize_decision(
				{
					"intent": "convert_appointment_to_ro",
					"confidence": 0.95,
					"audio_clarity_score": 0.98,
					"transcript": raw_text,
					"summary": f"Convert appointment {matched_appt.get('name') or ''} to Repair Order (odo {odometer_in:,})",
					"action": "CONVERT_APPOINTMENT_TO_RO",
					"action_payload": {
						"schedule_entry": matched_appt.get("name"),
						"odometer_in": odometer_in,
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": None,
					"followup_actions": [],
				}
			)

		# Default #Intake: quote + booking with VIN decode, matched vehicles, service packages, real open slots
		vin_match = _VIN_RE.search(raw_text)
		vin = vin_match.group(1).upper() if vin_match else (ctx.get("decoded_vin") or {}).get("vin")
		decoded = ctx.get("decoded_vin") or {}
		matched_vehicles = list(ctx.get("matched_vehicles") or [])
		packages = list(ctx.get("service_packages") or [])
		raw_slots = list(ctx.get("candidate_slots") or [])

		# Enrich candidate slots with Seam 4 customer arrival display while keeping internal start_time (T) intact
		candidate_slots: list[dict[str, Any]] = []
		for slot in raw_slots:
			if not isinstance(slot, dict):
				continue
			enriched = dict(slot)
			if not enriched.get("scheduled_date") and enriched.get("date"):
				enriched["scheduled_date"] = enriched["date"]
			if not enriched.get("service_bay"):
				avail_bays = enriched.get("available_bay_ids")
				enriched["service_bay"] = enriched.get("bay") or (
					avail_bays[0] if isinstance(avail_bays, list) and avail_bays else None
				)
			if slot.get("start_time"):
				enriched["customer_arrival_time"] = resolve_customer_arrival_time(slot["start_time"])
			candidate_slots.append(enriched)

		matched_pkg = None
		for pkg in packages:
			pkg_name = str(pkg.get("package_name") or pkg.get("name") or "")
			if pkg_name and any(tok in lowered for tok in pkg_name.lower().split() if len(tok) >= 4):
				matched_pkg = pkg
				break
		if matched_pkg is None and packages:
			matched_pkg = packages[0]

		quoted_service = (
			(matched_pkg.get("package_name") or matched_pkg.get("name"))
			if matched_pkg
			else "Diagnostic & Inspection"
		)
		labor_hours = float(matched_pkg.get("standard_labor_hours") or 1.0) if matched_pkg else 1.0
		service_rate = float(matched_pkg.get("service_rate") or 165.0) if matched_pkg else 165.0
		estimated_total = round(service_rate if service_rate > 0 else labor_hours * 165.0, 2)

		vehicle_summary = " ".join(
			str(decoded.get(k))
			for k in ("model_year", "manufacturer", "model")
			if decoded.get(k)
		).strip() or "Tesla Vehicle"

		customer = matched_vehicles[0].get("customer") if matched_vehicles else None
		customer_name = (
			matched_vehicles[0].get("customer_name")
			if matched_vehicles and matched_vehicles[0].get("customer_name")
			else customer or "Walk-In Customer"
		)

		first_slot = candidate_slots[0] if candidate_slots else None
		sanitized = sanitize_for_customer_comms(
			{
				"customer_name": customer_name,
				"vehicle_summary": vehicle_summary,
				"reference_id": "New Booking",
			},
			finding_summary=f"We have prepared your quote for {quoted_service}",
			approved_items=[{"description": quoted_service, "price": estimated_total}],
			eta_window=first_slot.get("date") if first_slot else None,
			internal_start_time=first_slot.get("start_time") if first_slot else None,
		)
		comms_draft = draft_customer_message(sanitized, channel="sms", provider=self)

		intake_odometer = _extract_odometer(raw_text, patch, metadata)
		intake_services = (
			[{"service_package": matched_pkg["name"]}]
			if matched_pkg and matched_pkg.get("name")
			else [{"service_title": quoted_service, "labor_hours": labor_hours}]
		)
		intake_payload: dict[str, Any] = {
			"vin": vin,
			"decoded_vehicle": decoded,
			"vehicle": matched_vehicles[0].get("name") if matched_vehicles else None,
			"customer": customer,
			"customer_name": customer_name,
			"service_package": matched_pkg.get("name") if matched_pkg else None,
			"services": intake_services,
			"requested_services": [quoted_service],
			"odometer_in": intake_odometer if intake_odometer is not None else 0,
			"notes": raw_text or quoted_service,
			"estimated_total": estimated_total,
			"candidate_slots": candidate_slots,
		}
		if not vin and not matched_vehicles:
			intake_payload["provisional"] = True
			intake_payload["provisional_vehicle_info"] = vehicle_summary

		return normalize_decision(
			{
				"intent": "intake_quote_and_book",
				"confidence": 0.95,
				"audio_clarity_score": 0.98,
				"transcript": raw_text,
				"summary": f"Intake quote & booking for {vehicle_summary} ({quoted_service}, ${estimated_total:.2f})",
				"action": "INTAKE_QUOTE_AND_BOOK",
				"action_payload": intake_payload,
				"missing_fields": [],
				"clarification": None,
				"customer_comms": comms_draft,
				"followup_actions": [],
			}
		)

	def _analyze_ap_receiving(
		self,
		ctx: dict[str, Any],
		raw_text: str,
		lowered: str,
		intent_tag: str,
		patch: dict[str, Any],
		metadata: dict[str, Any],
	) -> dict[str, Any]:
		open_pos = list(ctx.get("open_purchase_orders") or [])

		# Match PO by explicit PO number, supplier name, or item match
		matched_po: dict[str, Any] | None = None
		po_match = _PO_NO_RE.search(raw_text)
		explicit_po = (
			patch.get("purchase_order")
			or metadata.get("purchase_order")
			or (po_match.group(1).upper() if po_match else None)
		)
		if explicit_po:
			for po in open_pos:
				if str(po.get("name") or "").upper() == str(explicit_po).upper():
					matched_po = po
					break
		if matched_po is None:
			for po in open_pos:
				supplier = str(po.get("supplier_name") or po.get("supplier") or "").lower()
				if supplier and supplier in lowered:
					matched_po = po
					break
		if matched_po is None and open_pos:
			matched_po = open_pos[0]

		po_name = matched_po.get("name") if matched_po else explicit_po
		supplier = (matched_po.get("supplier") if matched_po else None) or metadata.get("supplier") or ""
		items = list(matched_po.get("items") or []) if matched_po else []
		grand_total = float(matched_po.get("grand_total") or 0.0) if matched_po else (_extract_amount(raw_text, patch) or 0.0)

		bill_match = _BILL_NO_RE.search(raw_text)
		bill_no = (
			patch.get("bill_no")
			or metadata.get("bill_no")
			or (bill_match.group(1).upper() if bill_match else None)
		)
		bill_date = patch.get("bill_date") or metadata.get("bill_date") or str(frappe.utils.today())

		# Pay purchase
		if intent_tag == "paid" or any(kw in lowered for kw in ("paid vendor", "pay vendor", "pay bill", "paid invoice")):
			amount = _extract_amount(raw_text, patch) or grand_total
			return normalize_decision(
				{
					"intent": "ap_pay_purchase",
					"confidence": 0.93,
					"audio_clarity_score": 0.97,
					"transcript": raw_text,
					"summary": f"Record vendor payment for {supplier or po_name or 'AP bill'} (${amount:.2f})",
					"action": "PAY_PURCHASE",
					"action_payload": {
						"purchase_order": po_name,
						"supplier": supplier,
						"bill_no": bill_no,
						"amount": amount,
						"paid_amount": amount,
						"payment_method": _extract_payment_method(raw_text, patch),
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": None,
					"followup_actions": [],
				}
			)

		is_invoice = (
			intent_tag == "vendor bill"
			or "invoice" in lowered
			or "vendor bill" in lowered
			or "bill" in lowered
			or bool(bill_no)
		)
		is_packing_slip = (
			intent_tag == "packing slip"
			or "packing slip" in lowered
			or "box arrived" in lowered
			or "received" in lowered
			or "shipment" in lowered
		)

		if is_invoice:
			if not bill_no:
				return normalize_decision(
					{
						"intent": "ap_vendor_invoice",
						"confidence": 0.78,
						"audio_clarity_score": 0.96,
						"transcript": raw_text,
						"summary": f"Vendor invoice matched to {po_name or 'PO'} — enter supplier bill number.",
						"action": "POST_PURCHASE_INVOICE",
						"action_payload": {
							"purchase_order": po_name,
							"supplier": supplier,
							"items": items,
							"parts_data": items,
							"grand_total": grand_total,
						},
						"missing_fields": ["bill_no"],
						"clarification": {
							"prompt": "Enter the vendor invoice / bill number (bill_no) to post Purchase Invoice.",
							"input_field": "bill_no",
							"input_type": "text",
							"action_type": "POST_PURCHASE_INVOICE",
							"target_role": "MANAGER",
							"options": [],
						},
						"customer_comms": None,
						"followup_actions": [],
					}
				)

			return normalize_decision(
				{
					"intent": "ap_vendor_invoice",
					"confidence": 0.95,
					"audio_clarity_score": 0.98,
					"transcript": raw_text,
					"summary": f"Post Purchase Invoice {bill_no} for {supplier or 'vendor'} (PO {po_name or 'unmatched'})",
					"action": "POST_PURCHASE_INVOICE",
					"action_payload": {
						"purchase_order": po_name,
						"supplier": supplier,
						"bill_no": bill_no,
						"bill_date": bill_date,
						"items": items,
						"parts_data": items,
						"grand_total": grand_total,
						"update_stock": bool(is_packing_slip),
					},
					"missing_fields": [],
					"clarification": None,
					"customer_comms": None,
					"followup_actions": [],
				}
			)

		# Default AP Receiving: packing slip -> RECEIVE_PO
		return normalize_decision(
			{
				"intent": "ap_receive_po",
				"confidence": 0.95,
				"audio_clarity_score": 0.98,
				"transcript": raw_text,
				"summary": f"Receive Purchase Order {po_name or '(select PO)'} from {supplier or 'vendor'}",
				"action": "RECEIVE_PO",
				"action_payload": {
					"purchase_order": po_name,
					"supplier": supplier,
					"items": items,
					"parts_data": items,
					"grand_total": grand_total,
				},
				"missing_fields": [],
				"clarification": None,
				"customer_comms": None,
				"followup_actions": [],
			}
		)


def _read_setting_password(settings: Any, fieldname: str) -> str:
	if settings is None:
		return ""
	get_pw = getattr(settings, "get_password", None)
	if callable(get_pw):
		try:
			val = get_pw(fieldname, raise_exception=False)
			if val:
				return str(val).strip()
		except Exception:
			pass
	raw = getattr(settings, fieldname, None)
	return str(raw).strip() if raw else ""


def get_ai_provider(
	settings: Any = None,
	*,
	provider_override: str | None = None,
) -> MultimodalProvider | None:
	"""Resolve the configured ``MultimodalProvider`` from ``ISM Settings`` (or ``None`` if Disabled)."""
	prev_user = getattr(frappe.session, "user", None) or "Administrator"
	elevated = prev_user != "Administrator"
	if elevated:
		frappe.set_user("Administrator")
	try:
		cfg = settings
		if cfg is None:
			try:
				cfg = frappe.get_cached_doc("ISM Settings")
			except Exception:
				cfg = None

		raw_provider = (
			provider_override
			if provider_override is not None
			else (getattr(cfg, "board_ai_provider", None) if cfg is not None else None)
		)
		norm = str(raw_provider or PROVIDER_DISABLED).strip()
		lowered = norm.lower()

		if not norm or lowered == "disabled":
			return None

		if lowered in ("mock", "mock (deterministic test)", "deterministic"):
			return MockMultimodalProvider()

		api_key = _read_setting_password(cfg, "board_ai_api_key")
		endpoint = str(getattr(cfg, "board_ai_endpoint", "") or "").strip() or None
		orchestrator_model = str(getattr(cfg, "board_ai_orchestrator_model", "") or "").strip() or None
		comms_model = str(getattr(cfg, "board_ai_comms_model", "") or "").strip() or None
	finally:
		if elevated:
			frappe.set_user(prev_user)

	if lowered in ("gemini", "gemini rest api", "gemini_rest"):
		return GeminiRestProvider(
			api_key=api_key,
			endpoint=endpoint or DEFAULT_GEMINI_ENDPOINT,
			orchestrator_model=orchestrator_model or DEFAULT_GEMINI_MODEL,
			comms_model=comms_model or orchestrator_model or DEFAULT_GEMINI_MODEL,
		)

	if lowered in ("openai", "openai compatible", "openai_compatible"):
		return OpenAICompatibleProvider(
			api_key=api_key,
			endpoint=endpoint or DEFAULT_OPENAI_ENDPOINT,
			orchestrator_model=orchestrator_model or DEFAULT_OPENAI_MODEL,
			comms_model=comms_model or orchestrator_model or DEFAULT_OPENAI_MODEL,
		)

	raise BoardContractError(f"Unsupported board_ai_provider: {norm!r}.")


__all__ = [
	"COMMS_RESPONSE_SCHEMA",
	"DECISION_SCHEMA",
	"DEFAULT_GEMINI_ENDPOINT",
	"DEFAULT_GEMINI_MODEL",
	"DEFAULT_OPENAI_ENDPOINT",
	"DEFAULT_OPENAI_MODEL",
	"DEFAULT_PAYMENT_OPTIONS",
	"PROVIDER_DISABLED",
	"PROVIDER_GEMINI",
	"PROVIDER_MOCK",
	"PROVIDER_OPENAI",
	"GeminiRestProvider",
	"MockMultimodalProvider",
	"MultimodalProvider",
	"OpenAICompatibleProvider",
	"get_ai_provider",
	"normalize_decision",
]
