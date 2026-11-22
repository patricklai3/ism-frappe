# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Lightweight AI provider adapters for Active-RO Shop Board (STT + Action Item Feed + natural-language follow-up).

Provides:
- ``BoardAIProvider`` protocol
- ``BOARD_AI_SCHEMA`` (Gemini-v1beta-safe schema with zero empty OBJECT nodes)
- ``normalize_ai_result`` normalizer
- ``GeminiRestProvider`` (pure HTTPS ``requests``, default ``gemini-3.6-flash`` + ``thinkingLevel="LOW"``)
- ``OpenAICompatibleProvider`` (pure HTTPS ``requests``)
- ``MockBoardAIProvider`` (deterministic offline provider covering STT, 2-turn follow-up, and action extraction)
- ``get_ai_provider`` factory driven by ``ISM Settings``
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Protocol, runtime_checkable

import frappe
import requests

from ism.board.contracts import (
	ACTION_CARD_CATEGORIES,
	ACTION_CARD_PRIORITIES,
	SUGGESTED_ACTION_TO_CLEARING_CONDITION,
	BoardContractError,
)
from ism.ism.doctype.shop_action_item.clearing import (
	CLEARING_CONDITION_DESCRIPTIONS,
	CLEARING_CONDITION_TYPES,
)

DEFAULT_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1"
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_GEMINI_THINKING_LEVEL = "LOW"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_HTTP_TIMEOUT_SECONDS = 90
MAX_ACTION_ITEMS = 5

_GEMINI_THINKING_LEVEL_MODEL_RE = re.compile(
	r"^(?:models/)?gemini-(?:[3-9]|\d{2,})(?:\.\d+)?-(?!.*(?:embedding|tts|image))",
	re.IGNORECASE,
)


def _supports_gemini_thinking_level(model: str | None) -> bool:
	normalized = (model or "").strip().lower()
	return bool(normalized and _GEMINI_THINKING_LEVEL_MODEL_RE.match(normalized))


PROVIDER_DISABLED = "Disabled"
PROVIDER_MOCK = "Mock (Deterministic Test)"
PROVIDER_GEMINI = "Gemini REST API"
PROVIDER_OPENAI = "OpenAI Compatible"

BOARD_AI_SCHEMA: dict[str, Any] = {
	"type": "OBJECT",
	"properties": {
		"transcript": {"type": "STRING"},
		"summary": {"type": "STRING"},
		"followup_question": {"type": "STRING", "nullable": True},
		"action_items": {
			"type": "ARRAY",
			"items": {
				"type": "OBJECT",
				"properties": {
					"title": {"type": "STRING"},
					"details": {"type": "STRING"},
					"category": {"type": "STRING"},
					"priority": {"type": "STRING"},
					"suggested_action": {"type": "STRING"},
					"clearing_condition_type": {"type": "STRING"},
					"clearing_condition_description": {"type": "STRING"},
					"part_names": {"type": "ARRAY", "items": {"type": "STRING"}},
					"side": {"type": "STRING", "nullable": True},
					"odometer": {"type": "INTEGER", "nullable": True},
				},
				"required": ["title", "details", "category", "priority"],
			},
		},
	},
	"required": ["transcript", "summary", "action_items"],
}

_BOARD_AI_SYSTEM_PROMPT = (
	"You are the lightweight Shop Board AI assistant for an independent Tesla repair shop. "
	"You receive compact active Repair Order context, up to 4 recent thread messages, and the latest "
	"technician/manager text, voice audio, or photo capture. "
	"Return strict JSON matching the schema: "
	"(1) If audio is attached, put an accurate speech-to-text transcript in 'transcript' (else empty string). "
	"(2) If an actionable floor report is missing a critical detail (e.g. left vs right side on a symmetrical part, "
	"or odometer-out reading when finishing a job and odometer_out is unset), set 'followup_question' to a short, "
	"natural chat question for the technician and return 'action_items': []. Never fabricate side or odometer. "
	"(3) If the message is a reply answering an earlier follow-up question in recent_messages, combine the prior "
	"finding with the reply and emit the complete action item(s) with 'followup_question': null. "
	"(4) Otherwise emit 0 to 5 concise, actionable Shop Action Items for the manager/advisor (return 'action_items': [] "
	"for pure FYI or routine chatter). "
	"Allowed categories: "
	+ ", ".join(ACTION_CARD_CATEGORIES)
	+ ". Allowed priorities: "
	+ ", ".join(ACTION_CARD_PRIORITIES)
	+ ". Allowed clearing_condition_type values: "
	+ ", ".join(CLEARING_CONDITION_TYPES)
	+ "."
)

_CATEGORY_LOOKUP: dict[str, str] = {c.lower(): c for c in ACTION_CARD_CATEGORIES}
_PRIORITY_LOOKUP: dict[str, str] = {p.lower(): p for p in ACTION_CARD_PRIORITIES}

# Deterministic regexes for MockBoardAIProvider
_SYMMETRICAL_PART_RE = re.compile(
	r"\b(upper control arm|lower control arm|control arm|bushing|strut|shock|caliper|"
	r"wheel bearing|bearing|tie rod|knuckle|axle|half shaft|ball joint|sway bar link|"
	r"cv boot|wheel hub|headlight|taillight|mirror|fender|door)\b",
	re.IGNORECASE,
)
_SIDE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
	(re.compile(r"\b(?:front\s+left|left\s+front|fl\b|driver(?:'s)?\s+front)\b", re.IGNORECASE), "Front Left"),
	(re.compile(r"\b(?:front\s+right|right\s+front|fr\b|passenger(?:'s)?\s+front)\b", re.IGNORECASE), "Front Right"),
	(re.compile(r"\b(?:rear\s+left|left\s+rear|rl\b|driver(?:'s)?\s+rear)\b", re.IGNORECASE), "Rear Left"),
	(re.compile(r"\b(?:rear\s+right|right\s+rear|rr\b|passenger(?:'s)?\s+rear)\b", re.IGNORECASE), "Rear Right"),
	(re.compile(r"\b(?:both\s+sides?|both|left\s+and\s+right|l\s*&\s*r|lh\s*&\s*rh)\b", re.IGNORECASE), "Both"),
	(re.compile(r"\b(?:left\s+side|lh\b|driver(?:'s)?\s+side|driver\b|left\b)\b", re.IGNORECASE), "Left"),
	(re.compile(r"\b(?:right\s+side|rh\b|passenger(?:'s)?\s+side|passenger\b|right\b)\b", re.IGNORECASE), "Right"),
)
_ODOMETER_RE = re.compile(
	r"(?:odometer|odo|mileage)\s*(?:out|in|is|at|reading|:|#|=)?\s*(\d{1,3}(?:,\d{3})+|\d{3,7})\b"
	r"|\b(\d{4,7})\s*(?:km|kms|mi|miles)\b",
	re.IGNORECASE,
)
_BARE_ODO_RE = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d{4,7})\s*(?:km|kms|mi|miles)?\.?\s*$", re.IGNORECASE)
_JOB_COMPLETE_RE = re.compile(
	r"\b(?:finished\s+(?:the\s+)?(?:job|work|repair)|work\s+completed|job(?:'s|\s+is)?\s+(?:done|complete|completed|finished)|"
	r"completed\s+(?:all\s+)?(?:the\s+)?(?:work|job|repair|services)|ready\s+for\s+(?:pickup|checkout)|all\s+done\s+with\s+(?:the\s+)?(?:job|repair|work))\b",
	re.IGNORECASE,
)
_PARTS_SHORTAGE_RE = re.compile(
	r"\b(?:out\s+of\s+stock|shortage|short\s+on|missing\s+parts?|need(?:s)?\s+to\s+order|"
	r"(?<!repair\s)order\s+(?:more\s+|new\s+|replacement\s+|a\s+|the\s+|some\s+)?"
	r"(?:parts?|brake\s+pads?|pads?|brake\s+rotors?|rotors?|cabin\s+(?:air\s+)?filters?|filters?|"
	r"control\s+arms?|arms?|bushings?|calipers?|struts?|bearings?|bolts?|kits?|sensors?|fluid|coolant|wipers?|battery)|"
	r"backorder(?:ed)?|waiting\s+on\s+parts|parts\s+hold|no\s+stock|no\s+more\s+in\s+stock|procure|purchase\s+order)\b",
	re.IGNORECASE,
)
_SUPPLEMENT_RE = re.compile(
	r"\b(?:supplement|additional\s+(?:work|repair|service)|add\s+(?:to\s+)?(?:the\s+)?quote|needs?\s+(?:a\s+)?quote|"
	r"cracked|torn|leaking|worn\s+out|badly\s+worn|damaged|bent|needs?\s+replacement|recommend\s+replacing)\b",
	re.IGNORECASE,
)
_CUSTOMER_COMMS_RE = re.compile(
	r"\b(?:call\s+(?:the\s+)?customer|notify\s+(?:the\s+)?customer|update\s+(?:the\s+)?customer|"
	r"text\s+(?:the\s+)?customer|sms\s+(?:the\s+)?customer|email\s+(?:the\s+)?customer|"
	r"let\s+(?:the\s+)?customer\s+know|customer\s+(?:update|call|approval)|reach\s+out\s+to\s+(?:the\s+)?customer)\b",
	re.IGNORECASE,
)
_SCHEDULE_DELAY_RE = re.compile(
	r"\b(?:reschedule|behind\s+schedule|extra\s+\d+\s*(?:h|hr|hrs|hour|hours|min|mins)|"
	r"need(?:s)?\s+(?:another|extra)\s+\d*\s*(?:hour|hours|hr|hrs)|push\s+back|move\s+(?:the\s+)?(?:slot|appointment))\b",
	re.IGNORECASE,
)
_BLOCKER_RE = re.compile(
	r"\b(?:seized|stuck|blocker|blocked|stripped|broken\s+bolt|sheared|cannot\s+remove|safety\s+issue|discrepancy)\b",
	re.IGNORECASE,
)
_PART_EXTRACT_RE = re.compile(
	r"\b((?:upper|lower)?\s*(?:control arm(?:\s+bushing)?|bushing|brake pads?|brake rotors?|cabin air filters?|cabin filters?|"
	r"air filters?|strut|caliper|wheel bearing|tie rod|knuckle|axle|ball joint|sway bar link|coolant|wiper blades?|12v battery|sensor|bolt kit))\b",
	re.IGNORECASE,
)


@runtime_checkable
class BoardAIProvider(Protocol):
	"""Protocol implemented by all lightweight Active-RO Shop Board AI providers."""

	def analyze_capture(
		self,
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
	) -> dict[str, Any]:
		"""Analyze RO context + capture text/media into transcript, optional followup_question, and 0..N action items."""
		...


def _clamp_category(raw: Any) -> str:
	if isinstance(raw, str):
		cleaned = raw.strip()
		if cleaned in ACTION_CARD_CATEGORIES:
			return cleaned
		matched = _CATEGORY_LOOKUP.get(cleaned.lower())
		if matched:
			return matched
	return "General"


def _clamp_priority(raw: Any) -> str:
	if isinstance(raw, str):
		cleaned = raw.strip()
		if cleaned in ACTION_CARD_PRIORITIES:
			return cleaned
		matched = _PRIORITY_LOOKUP.get(cleaned.lower())
		if matched:
			return matched
	return "Medium"


def _clamp_clearing_type(raw_ctype: Any, suggested_action: str) -> str:
	if isinstance(raw_ctype, str) and raw_ctype.strip() in CLEARING_CONDITION_TYPES:
		return raw_ctype.strip()
	if suggested_action:
		mapped = SUGGESTED_ACTION_TO_CLEARING_CONDITION.get(suggested_action.strip().upper())
		if mapped and mapped in CLEARING_CONDITION_TYPES:
			return mapped
	return "manual"


def _parse_optional_int(val: Any) -> int | None:
	if val is None or val == "" or isinstance(val, bool):
		return None
	try:
		num = int(str(val).replace(",", "").strip())
		return num if num >= 0 else None
	except (TypeError, ValueError):
		return None


def normalize_ai_result(raw: dict[str, Any] | None) -> dict[str, Any]:
	"""Normalize any provider result into the frozen lightweight Board AI output contract."""
	data = raw if isinstance(raw, dict) else {}

	transcript = str(data.get("transcript") or "").strip()
	summary = str(data.get("summary") or "").strip()

	raw_followup = data.get("followup_question")
	followup_question: str | None = None
	if isinstance(raw_followup, str) and raw_followup.strip():
		followup_question = raw_followup.strip()

	raw_items = data.get("action_items")
	normalized_items: list[dict[str, Any]] = []
	if isinstance(raw_items, list):
		for entry in raw_items:
			if len(normalized_items) >= MAX_ACTION_ITEMS:
				break
			if not isinstance(entry, dict):
				continue
			title = str(entry.get("title") or "").strip()
			if not title:
				continue
			details = str(entry.get("details") or "").strip()
			category = _clamp_category(entry.get("category"))
			priority = _clamp_priority(entry.get("priority"))
			suggested_action = str(entry.get("suggested_action") or "").strip()
			clearing_type = _clamp_clearing_type(entry.get("clearing_condition_type"), suggested_action)
			raw_desc = str(entry.get("clearing_condition_description") or "").strip()
			clearing_desc = raw_desc or CLEARING_CONDITION_DESCRIPTIONS.get(clearing_type, "")

			raw_parts = entry.get("part_names")
			part_names = (
				[str(p).strip() for p in raw_parts if isinstance(p, str) and p.strip()]
				if isinstance(raw_parts, list)
				else []
			)

			raw_side = entry.get("side")
			side = raw_side.strip() if isinstance(raw_side, str) and raw_side.strip() else None
			odometer = _parse_optional_int(entry.get("odometer"))

			normalized_items.append(
				{
					"title": title[:140],
					"details": details,
					"category": category,
					"priority": priority,
					"suggested_action": suggested_action or "manual_review",
					"clearing_condition_type": clearing_type,
					"clearing_condition_description": clearing_desc,
					"part_names": part_names,
					"side": side,
					"odometer": odometer,
				}
			)

	return {
		"transcript": transcript,
		"summary": summary,
		"followup_question": followup_question,
		"action_items": normalized_items,
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


def _build_capture_prompt(context: dict[str, Any], text: str | None) -> str:
	prompt_payload = {
		"capture_text": (text or "").strip(),
		"context": context or {},
	}
	return json.dumps(prompt_payload, default=str, ensure_ascii=False)


class GeminiRestProvider:
	"""Pure HTTPS Gemini REST adapter (default gemini-3.6-flash + thinkingLevel LOW)."""

	def __init__(
		self,
		*,
		api_key: str,
		endpoint: str | None = None,
		model: str | None = None,
		orchestrator_model: str | None = None,
		thinking_level: str | None = DEFAULT_GEMINI_THINKING_LEVEL,
		timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
	) -> None:
		self.api_key = (api_key or "").strip()
		self.endpoint = (endpoint or DEFAULT_GEMINI_ENDPOINT).strip().rstrip("/")
		self.model = (model or orchestrator_model or DEFAULT_GEMINI_MODEL).strip()
		self.thinking_level = (thinking_level or "").strip().upper() or None
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
		generation_config: dict[str, Any] = {
			"temperature": 0.1,
			"responseMimeType": "application/json",
			"responseSchema": response_schema,
		}
		if self.thinking_level and _supports_gemini_thinking_level(model):
			generation_config["thinkingConfig"] = {"thinkingLevel": self.thinking_level}

		body = {
			"systemInstruction": {"parts": [{"text": system_instruction}]},
			"contents": [{"role": "user", "parts": parts}],
			"generationConfig": generation_config,
		}

		resp = requests.post(url, headers=headers, json=body, timeout=self.timeout_seconds)
		resp.raise_for_status()
		payload = resp.json()

		candidates = payload.get("candidates") or []
		if not candidates:
			raise ValueError("Gemini API returned no candidates.")
		content_parts = (candidates[0].get("content") or {}).get("parts") or []
		text_chunks = [
			p.get("text", "")
			for p in content_parts
			if isinstance(p, dict) and p.get("text") and not p.get("thought")
		]
		if not text_chunks:
			raise ValueError("Gemini API response contained no text part.")
		return _extract_json_object("\n".join(text_chunks))

	def analyze_capture(
		self,
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
	) -> dict[str, Any]:
		parts: list[dict[str, Any]] = [{"text": _build_capture_prompt(context, text)}]
		for blob in media_blobs or []:
			mime_type, b64 = _encode_media_b64(blob)
			if b64:
				parts.append({"inline_data": {"mime_type": mime_type, "data": b64}})

		raw = self._generate_content(
			model=self.model,
			system_instruction=_BOARD_AI_SYSTEM_PROMPT,
			parts=parts,
			response_schema=BOARD_AI_SCHEMA,
		)
		return normalize_ai_result(raw)


class OpenAICompatibleProvider:
	"""Pure HTTPS OpenAI-compatible multimodal adapter (single analyze_capture call)."""

	def __init__(
		self,
		*,
		api_key: str,
		endpoint: str | None = None,
		model: str | None = None,
		orchestrator_model: str | None = None,
		timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
	) -> None:
		self.api_key = (api_key or "").strip()
		self.endpoint = (endpoint or DEFAULT_OPENAI_ENDPOINT).strip().rstrip("/")
		self.model = (model or orchestrator_model or DEFAULT_OPENAI_MODEL).strip()
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
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
	) -> dict[str, Any]:
		content_items: list[dict[str, Any]] = [{"type": "text", "text": _build_capture_prompt(context, text)}]
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
			model=self.model,
			system_prompt=_BOARD_AI_SYSTEM_PROMPT,
			user_content=content_items,
		)
		return normalize_ai_result(raw)


def _detect_side(text: str) -> str | None:
	if not text:
		return None
	for pattern, label in _SIDE_PATTERNS:
		if pattern.search(text):
			return label
	return None


def _detect_odometer(text: str, *, allow_bare: bool = False) -> int | None:
	if not text:
		return None
	m = _ODOMETER_RE.search(text)
	if m:
		raw_num = m.group(1) or m.group(2)
		return _parse_optional_int(raw_num)
	if allow_bare:
		bm = _BARE_ODO_RE.match(text)
		if bm:
			return _parse_optional_int(bm.group(1))
	return None


def _extract_part_names(text: str) -> list[str]:
	if not text:
		return []
	found: list[str] = []
	for m in _PART_EXTRACT_RE.finditer(text):
		cleaned = " ".join(m.group(1).strip().split())
		if cleaned and cleaned.lower() not in {x.lower() for x in found}:
			found.append(cleaned)
	return found


class MockBoardAIProvider:
	"""Deterministic offline provider for Active-RO STT, natural-language follow-up, and Action Item Feed."""

	SIDE_FOLLOWUP_QUESTION = "Is that the front left (driver) or front right (passenger) side, or both?"
	ODOMETER_FOLLOWUP_QUESTION = "What is the odometer-out reading so we can mark work completed?"

	def analyze_capture(
		self,
		context: dict[str, Any],
		text: str | None,
		media_blobs: list[dict[str, Any]],
	) -> dict[str, Any]:
		ctx = context if isinstance(context, dict) else {}
		metadata = ctx.get("event_metadata") if isinstance(ctx.get("event_metadata"), dict) else {}
		intent_tag = str(ctx.get("intent_tag") or "").strip().upper()
		ro_name = str(ctx.get("ro_name") or "").strip()
		recent_messages = ctx.get("recent_messages") if isinstance(ctx.get("recent_messages"), list) else []

		has_audio = any(
			isinstance(b, dict) and str(b.get("content_type") or "").lower().startswith("audio/")
			for b in (media_blobs or [])
		)
		mock_transcript = str(metadata.get("mock_transcript") or "").strip()
		raw_text = (text or "").strip()

		# 1. STT transcript handling
		transcript = ""
		if mock_transcript:
			transcript = mock_transcript
		elif has_audio:
			transcript = raw_text or "Voice note recorded on active Repair Order."

		effective_text = " ".join(part for part in (raw_text, transcript) if part).strip()
		if raw_text and transcript and raw_text.lower() == transcript.lower():
			effective_text = raw_text

		if not effective_text:
			return normalize_ai_result({"transcript": transcript, "summary": "", "action_items": []})

		# 2. Check if this capture is answering an immediately preceding AI follow-up question in recent_messages
		prior_ai_msg = None
		prior_human_msg = None
		if recent_messages:
			last_msg = recent_messages[-1] if isinstance(recent_messages[-1], dict) else {}
			if str(last_msg.get("actor_type") or "").upper() == "AI":
				prior_ai_msg = str(last_msg.get("text") or "").strip() or None
				for msg in reversed(recent_messages[:-1]):
					if not isinstance(msg, dict):
						continue
					actor = str(msg.get("actor_type") or "").upper()
					m_text = str(msg.get("text") or "").strip()
					if actor in ("TECH", "MANAGER") and m_text:
						prior_human_msg = m_text
						break

		if prior_ai_msg and prior_human_msg:
			prior_ai_lower = prior_ai_msg.lower()
			# Follow-up reply for ambiguous side
			if any(tok in prior_ai_lower for tok in ("left", "right", "side", "driver", "passenger")):
				answered_side = _detect_side(effective_text)
				if answered_side:
					combined_text = f"{prior_human_msg} ({answered_side})"
					parts = _extract_part_names(prior_human_msg) or _extract_part_names(combined_text)
					part_label = parts[0] if parts else "suspension component"
					title = f"Add supplement: {answered_side} {part_label}"
					if ro_name:
						title = f"{title} ({ro_name})"
					return normalize_ai_result(
						{
							"transcript": transcript,
							"summary": f"Completed finding with side '{answered_side}' from follow-up reply.",
							"followup_question": None,
							"action_items": [
								{
									"title": title,
									"details": f"Technician reported: {prior_human_msg}. Confirmed side: {answered_side}.",
									"category": "Quote / Supplement",
									"priority": "High",
									"suggested_action": "add_supplement",
									"clearing_condition_type": "ro_has_supplement_or_service",
									"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS[
										"ro_has_supplement_or_service"
									],
									"part_names": parts,
									"side": answered_side,
									"odometer": None,
								}
							],
						}
					)

			# Follow-up reply for missing odometer-out
			if "odometer" in prior_ai_lower:
				answered_odo = _detect_odometer(effective_text, allow_bare=True)
				if answered_odo is not None:
					title = f"Mark work completed (odo {answered_odo:,})"
					if ro_name:
						title = f"Mark {ro_name} work completed (odo {answered_odo:,})"
					return normalize_ai_result(
						{
							"transcript": transcript,
							"summary": f"Job completion confirmed with odometer-out {answered_odo:,}.",
							"followup_question": None,
							"action_items": [
								{
									"title": title,
									"details": f"Technician reported: {prior_human_msg}. Confirmed odometer-out: {answered_odo:,}.",
									"category": "General",
									"priority": "Medium",
									"suggested_action": "complete_job",
									"clearing_condition_type": "ro_status_in",
									"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS["ro_status_in"],
									"part_names": [],
									"side": None,
									"odometer": answered_odo,
								}
							],
						}
					)

		# 3. Check for ambiguous side on a symmetrical part (check BOTH current text AND non-AI recent_messages)
		recent_human_blob = " ".join(
			str(m.get("text") or "")
			for m in recent_messages
			if isinstance(m, dict) and str(m.get("actor_type") or "").upper() in ("TECH", "MANAGER")
		)
		current_side = _detect_side(effective_text)
		recent_side = _detect_side(recent_human_blob)
		resolved_side = current_side or recent_side

		if _SYMMETRICAL_PART_RE.search(effective_text) and not resolved_side:
			# Ambiguous symmetrical part finding -> ask concise natural-language follow-up question
			if intent_tag != "FYI":
				return normalize_ai_result(
					{
						"transcript": transcript,
						"summary": "Side missing for symmetrical component finding; asking technician.",
						"followup_question": self.SIDE_FOLLOWUP_QUESTION,
						"action_items": [],
					}
				)

		# 4. Check for job completion missing odometer_out
		has_job_complete = bool(_JOB_COMPLETE_RE.search(effective_text))
		detected_odo = _detect_odometer(effective_text, allow_bare=False)
		ctx_odo_out = _parse_optional_int(ctx.get("odometer_out"))
		effective_odo = detected_odo if detected_odo is not None else ctx_odo_out

		if has_job_complete and effective_odo is None:
			return normalize_ai_result(
				{
					"transcript": transcript,
					"summary": "Odometer-out reading missing for job completion; asking technician.",
					"followup_question": self.ODOMETER_FOLLOWUP_QUESTION,
					"action_items": [],
				}
			)

		# 5. Pure FYI / non-actionable chatter check
		has_parts_shortage = bool(_PARTS_SHORTAGE_RE.search(effective_text))
		has_supplement = bool(_SUPPLEMENT_RE.search(effective_text)) or intent_tag == "SUPPLEMENT"
		has_customer_comms = bool(_CUSTOMER_COMMS_RE.search(effective_text))
		has_schedule_delay = bool(_SCHEDULE_DELAY_RE.search(effective_text))
		has_blocker = bool(_BLOCKER_RE.search(effective_text)) or intent_tag == "BLOCKER"

		if intent_tag == "FYI" and not (
			has_parts_shortage or has_supplement or has_customer_comms or has_job_complete or has_blocker
		):
			return normalize_ai_result(
				{
					"transcript": transcript,
					"summary": "FYI update logged — no action item required.",
					"followup_question": None,
					"action_items": [],
				}
			)

		# 6. Extract 0..N structured action items (supports multi-issue captures!)
		action_items: list[dict[str, Any]] = []
		parts = _extract_part_names(effective_text)

		if has_supplement:
			part_label = parts[0] if parts else "additional repair"
			side_prefix = f"{resolved_side} " if resolved_side else ""
			title = f"Add supplement: {side_prefix}{part_label}".strip()
			if ro_name:
				title = f"{title} ({ro_name})"
			action_items.append(
				{
					"title": title,
					"details": effective_text,
					"category": "Quote / Supplement",
					"priority": "High",
					"suggested_action": "add_supplement",
					"clearing_condition_type": "ro_has_supplement_or_service",
					"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS[
						"ro_has_supplement_or_service"
					],
					"part_names": parts,
					"side": resolved_side,
					"odometer": None,
				}
			)

		if has_parts_shortage:
			parts_label = ", ".join(parts[:2]) if parts else "required parts"
			title = f"Order missing parts: {parts_label}"
			if ro_name:
				title = f"{title} ({ro_name})"
			action_items.append(
				{
					"title": title,
					"details": effective_text,
					"category": "Inventory / Parts",
					"priority": "High",
					"suggested_action": "order_parts",
					"clearing_condition_type": "po_submitted_or_received",
					"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS[
						"po_submitted_or_received"
					],
					"part_names": parts,
					"side": resolved_side,
					"odometer": None,
				}
			)

		if has_customer_comms:
			title = f"Update customer on {ro_name}" if ro_name else "Send customer update"
			action_items.append(
				{
					"title": title,
					"details": effective_text,
					"category": "Customer Comms",
					"priority": "Medium",
					"suggested_action": "notify_customer",
					"clearing_condition_type": "ro_sms_or_email_sent",
					"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS["ro_sms_or_email_sent"],
					"part_names": [],
					"side": None,
					"odometer": None,
				}
			)

		if has_job_complete and effective_odo is not None:
			title = (
				f"Mark {ro_name} work completed (odo {effective_odo:,})"
				if ro_name
				else f"Mark work completed (odo {effective_odo:,})"
			)
			action_items.append(
				{
					"title": title,
					"details": effective_text,
					"category": "General",
					"priority": "Medium",
					"suggested_action": "complete_job",
					"clearing_condition_type": "ro_status_in",
					"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS["ro_status_in"],
					"part_names": [],
					"side": None,
					"odometer": effective_odo,
				}
			)

		if has_schedule_delay:
			title = f"Update schedule for {ro_name}" if ro_name else "Update RO schedule"
			action_items.append(
				{
					"title": title,
					"details": effective_text,
					"category": "Shop Floor Discrepancy",
					"priority": "High",
					"suggested_action": "reschedule",
					"clearing_condition_type": "ro_schedule_updated",
					"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS["ro_schedule_updated"],
					"part_names": [],
					"side": None,
					"odometer": None,
				}
			)

		if has_blocker and not has_schedule_delay:
			title = f"Shop floor blocker on {ro_name}" if ro_name else "Resolve shop floor blocker"
			action_items.append(
				{
					"title": title,
					"details": effective_text,
					"category": "Shop Floor Discrepancy",
					"priority": "High",
					"suggested_action": "manual_review",
					"clearing_condition_type": "manual",
					"clearing_condition_description": CLEARING_CONDITION_DESCRIPTIONS["manual"],
					"part_names": parts,
					"side": resolved_side,
					"odometer": None,
				}
			)

		summary = (
			f"Extracted {len(action_items)} action item(s) from floor update."
			if action_items
			else "Routine update logged — no action required."
		)
		return normalize_ai_result(
			{
				"transcript": transcript,
				"summary": summary,
				"followup_question": None,
				"action_items": action_items,
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
) -> BoardAIProvider | None:
	"""Resolve the configured ``BoardAIProvider`` from ``ISM Settings`` (or ``None`` if Disabled)."""
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
			return MockBoardAIProvider()

		api_key = _read_setting_password(cfg, "board_ai_api_key")
		endpoint = str(getattr(cfg, "board_ai_endpoint", "") or "").strip() or None
		configured_model = (
			str(getattr(cfg, "board_ai_model", "") or "").strip()
			or str(getattr(cfg, "board_ai_orchestrator_model", "") or "").strip()
			or None
		)
	finally:
		if elevated:
			frappe.set_user(prev_user)

	if lowered in ("gemini", "gemini rest api", "gemini_rest", "google gemini (rest)", "google gemini"):
		return GeminiRestProvider(
			api_key=api_key,
			endpoint=endpoint or DEFAULT_GEMINI_ENDPOINT,
			model=configured_model or DEFAULT_GEMINI_MODEL,
		)

	if lowered in ("openai", "openai compatible", "openai_compatible", "openai-compatible"):
		return OpenAICompatibleProvider(
			api_key=api_key,
			endpoint=endpoint or DEFAULT_OPENAI_ENDPOINT,
			model=configured_model or DEFAULT_OPENAI_MODEL,
		)

	raise BoardContractError(f"Unsupported board_ai_provider: {norm!r}.")


__all__ = [
	"BOARD_AI_SCHEMA",
	"DEFAULT_GEMINI_ENDPOINT",
	"DEFAULT_GEMINI_MODEL",
	"DEFAULT_GEMINI_THINKING_LEVEL",
	"DEFAULT_HTTP_TIMEOUT_SECONDS",
	"DEFAULT_OPENAI_ENDPOINT",
	"DEFAULT_OPENAI_MODEL",
	"MAX_ACTION_ITEMS",
	"PROVIDER_DISABLED",
	"PROVIDER_GEMINI",
	"PROVIDER_MOCK",
	"PROVIDER_OPENAI",
	"BoardAIProvider",
	"GeminiRestProvider",
	"MockBoardAIProvider",
	"OpenAICompatibleProvider",
	"_supports_gemini_thinking_level",
	"get_ai_provider",
	"normalize_ai_result",
]
