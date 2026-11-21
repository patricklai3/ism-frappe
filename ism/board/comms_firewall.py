# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Context Firewall for customer-facing Shop Board communications (Seam 4 aware).

Guarantees zero internal leakage (supplier costs, margin %, Tesla OEM prices,
``override_notes``, ``verbal_notes``, raw transcripts, bay/tech IDs, shop slang,
and Seam 4 arrival buffer wording) in customer SMS, Email, and Quote drafts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import frappe

from ism.board.contracts import BoardContractError

ALLOWED_COMMS_CHANNELS: tuple[str, ...] = ("sms", "email")

DEFAULT_SHOP_NAME = "Independent Service Motors"

# Strict patterns that must NEVER appear in customer-facing communications.
_FORBIDDEN_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
	(
		"internal_cost_or_margin",
		re.compile(
			r"\b(?:supplier\s+cost|our\s+cost|wholesale\s+cost|internal\s+cost|cost\s+floor|cost_plus|"
			r"margin\s*[:=]?\s*\d|markup\s*[:=]?\s*\d|\bmargin\b|\bmarkup\b|"
			r"tesla\s+oem(?:\s+(?:price|list|ref(?:erence)?))?|oem\s+list\s+price|oem\s+price|"
			r"baseline_offset|internal_only|cost\s*[:=]\s*\$?\d|cost\s+\$\d)\b",
			re.IGNORECASE,
		),
	),
	(
		"internal_field_label",
		re.compile(
			r"\b(?:override_notes|verbal_notes|advisor_notes|manual_parts_hold|"
			r"has_shop_floor_override|shop_floor_override|raw\s+transcript|"
			r"audio\s+transcript|transcript\s*:|board_event|BE-\d{4}-\d+)\b",
			re.IGNORECASE,
		),
	),
	(
		"internal_bay_or_tech_id",
		re.compile(
			r"\b(?:BAY[-_ ]?\d+|Bay\s+\d+|TECH[-_ ]?\d+|HR-EMP-\d+|EMP[-_ ]?\d+|"
			r"tech_id|bay_id|lift\s*#?\s*\d+)\b",
			re.IGNORECASE,
		),
	),
	(
		"arrival_buffer_disclosure",
		re.compile(
			r"\b(?:buffer|leeway|grace\s+period|arrival\s+offset|internal\s+slot|"
			r"internal\s+capacity|arrive\s+\d+\s*min(?:ute)?s?\s+early|"
			r"extra\s+cushion|check-?in\s+buffer)\b",
			re.IGNORECASE,
		),
	),
	(
		"shop_slang_or_profanity",
		re.compile(
			r"\b(?:fuck(?:ed|ing)?|shit(?:ty)?|crap(?:py)?|busted\s+ass|clusterfuck|"
			r"ripoff|rip-off|upsell|milk\s+the\s+customer|screwed\s+up|junk\s+part|"
			r"idiot|stupid\s+customer)\b",
			re.IGNORECASE,
		),
	),
)

# Patterns scrubbed from free-text summaries when building SanitizedCustomerFactPayload.
_SUMMARY_SCRUB_PATTERNS: tuple[re.Pattern[str], ...] = (
	re.compile(r"\[(?:INTERNAL|NOTE|OVERRIDE|COST|MARGIN|OEM|BAY|TECH)[^\]]*\]", re.IGNORECASE),
	re.compile(
		r"\b(?:supplier\s+cost|our\s+cost|wholesale\s+cost|internal\s+cost|cost\s+floor|"
		r"tesla\s+oem(?:\s+(?:price|list|ref(?:erence)?))?|oem\s+price|margin|markup|"
		r"override_notes|verbal_notes|advisor_notes)\s*[:=]?\s*[^.;,\n]*",
		re.IGNORECASE,
	),
	re.compile(r"\b(?:BAY[-_ ]?\d+|Bay\s+\d+|TECH[-_ ]?\d+|HR-EMP-\d+|EMP[-_ ]?\d+)\b", re.IGNORECASE),
	re.compile(
		r"\b(?:with\s+(?:a\s+)?\d+-?min(?:ute)?\s+)?(?:buffer|leeway|grace\s+period|arrival\s+offset|"
		r"arrive\s+\d+\s*min(?:ute)?s?\s+early)\b",
		re.IGNORECASE,
	),
	re.compile(
		r"\b(?:fuck(?:ed|ing)?|shit(?:ty)?|crap(?:py)?|busted\s+ass|clusterfuck|"
		r"ripoff|rip-off|upsell|screwed\s+up)\b",
		re.IGNORECASE,
	),
)

_SAFE_PHOTO_REF_RE = re.compile(r"^(?:m_[A-Za-z0-9_-]{4,64}|https?://[^\s<>\"']{4,512})$")
_SAFE_URL_RE = re.compile(r"^(?:https?://[^\s<>\"']{4,1024}|/[^\s<>\"']{1,512})$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class CommsFirewallViolationError(frappe.ValidationError, ValueError):
	"""Raised when outbound customer communication violates the Context Firewall."""


def resolve_customer_arrival_time(internal_start: Any) -> str | None:
	"""Resolve customer-facing arrival time via Seam 4 soft import of Project 3 arrival_policy.

	When Project 3 (``ism.scheduling.arrival_policy``) is installed, converts internal
	capacity start ``T`` into customer arrival time ``T - offset`` (e.g. ``08:00`` -> ``07:45``)
	with zero buffer or leeway wording. When Project 3 is absent, falls back cleanly
	to raw ``internal_start``.
	"""
	if internal_start is None:
		return None

	raw_str = str(internal_start).strip()
	if not raw_str:
		return ""

	try:
		from ism.scheduling.arrival_policy import to_customer_arrival_time
	except ImportError:
		to_customer_arrival_time = None

	if callable(to_customer_arrival_time):
		resolved = to_customer_arrival_time(internal_start)
		return _normalize_time_display(resolved)

	return _normalize_time_display(raw_str)


def _normalize_time_display(value: Any) -> str:
	if value is None:
		return ""
	text = str(value).strip()
	match = _HHMM_RE.match(text)
	if match:
		hour = int(match.group(1))
		minute = int(match.group(2))
		return f"{hour:02d}:{minute:02d}"
	return text


def _scrub_customer_text(text: Any, *, max_len: int = 500) -> str:
	if text is None:
		return ""
	cleaned = str(text)
	for pattern in _SUMMARY_SCRUB_PATTERNS:
		cleaned = pattern.sub("", cleaned)
	cleaned = re.sub(r"\s*[,;:]\s*([.;,]|$)", r"\1", cleaned)
	cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
	if len(cleaned) > max_len:
		cleaned = cleaned[: max_len - 1].rstrip() + "…"
	return cleaned


def _extract_customer_display_name(ro_doc: Any) -> str:
	if ro_doc is None:
		return "Valued Customer"

	raw_name = None
	if isinstance(ro_doc, dict):
		raw_name = (
			ro_doc.get("customer_first_name")
			or ro_doc.get("customer_display_name")
			or ro_doc.get("customer_name")
			or ro_doc.get("customer")
		)
	else:
		raw_name = getattr(ro_doc, "customer_name", None) or getattr(ro_doc, "customer", None)
		if raw_name and getattr(ro_doc, "customer", None) == raw_name:
			try:
				looked_up = frappe.db.get_value("Customer", raw_name, "customer_name")
				if looked_up:
					raw_name = looked_up
			except Exception:
				pass

	cleaned = _scrub_customer_text(raw_name, max_len=80)
	if not cleaned:
		return "Valued Customer"
	return cleaned


def _extract_vehicle_summary(ro_doc: Any) -> str:
	if ro_doc is None:
		return "your vehicle"

	if isinstance(ro_doc, dict):
		explicit = ro_doc.get("vehicle_summary")
		if explicit:
			return _scrub_customer_text(explicit, max_len=100) or "your vehicle"
		year = ro_doc.get("model_year") or ro_doc.get("vehicle_year")
		make = ro_doc.get("manufacturer") or ro_doc.get("make")
		model = ro_doc.get("model") or ro_doc.get("vehicle_model")
		parts = [str(p).strip() for p in (year, make, model) if p and str(p).strip()]
		if parts:
			return _scrub_customer_text(" ".join(parts), max_len=100) or "your vehicle"
		vehicle_link = ro_doc.get("vehicle")
	else:
		vehicle_link = getattr(ro_doc, "vehicle", None)

	if vehicle_link:
		try:
			vmeta = frappe.db.get_value(
				"Repair Vehicle",
				vehicle_link,
				["model_year", "manufacturer", "model"],
				as_dict=True,
			)
			if vmeta:
				parts = [
					str(vmeta.get(k)).strip()
					for k in ("model_year", "manufacturer", "model")
					if vmeta.get(k) and str(vmeta.get(k)).strip()
				]
				if parts:
					return _scrub_customer_text(" ".join(parts), max_len=100) or "your vehicle"
		except Exception:
			pass

	return "your vehicle"


def _extract_reference_id(ro_doc: Any) -> str:
	if ro_doc is None:
		return ""
	if isinstance(ro_doc, dict):
		raw = ro_doc.get("name") or ro_doc.get("reference_id") or ro_doc.get("booking_reference") or ""
	else:
		raw = getattr(ro_doc, "name", None) or ""
	return _scrub_customer_text(raw, max_len=64)


def _extract_currency(ro_doc: Any) -> str:
	raw = None
	if isinstance(ro_doc, dict):
		raw = ro_doc.get("currency")
	elif ro_doc is not None:
		raw = getattr(ro_doc, "currency", None)
	cleaned = str(raw or "CAD").strip().upper()
	return cleaned if re.match(r"^[A-Z]{3}$", cleaned) else "CAD"


def _sanitize_approved_items(
	ro_doc: Any,
	approved_items: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> list[dict[str, Any]]:
	"""Strictly allowlist customer-facing item descriptions and customer sell prices ONLY."""
	source_rows: list[Any]
	if approved_items is not None:
		source_rows = list(approved_items)
	elif ro_doc is not None and not isinstance(ro_doc, dict):
		source_rows = list(getattr(ro_doc, "services", None) or [])
	elif isinstance(ro_doc, dict) and isinstance(ro_doc.get("services"), list):
		source_rows = list(ro_doc["services"])
	else:
		source_rows = []

	sanitized: list[dict[str, Any]] = []
	for row in source_rows:
		if isinstance(row, dict):
			raw_desc = (
				row.get("description")
				or row.get("service_title")
				or row.get("title")
				or row.get("item_name")
				or ""
			)
			raw_price = row.get("price")
			if raw_price is None:
				raw_price = row.get("sell_price")
			if raw_price is None:
				raw_price = row.get("total_amount")
			if raw_price is None:
				raw_price = row.get("customer_price")
			if raw_price is None:
				raw_price = row.get("amount")
			raw_qty = row.get("qty")
		else:
			raw_desc = (
				getattr(row, "service_title", None)
				or getattr(row, "description", None)
				or getattr(row, "item_name", None)
				or ""
			)
			raw_price = getattr(row, "total_amount", None)
			if raw_price is None:
				raw_price = getattr(row, "sell_price", None)
			raw_qty = getattr(row, "qty", None)

		desc = _scrub_customer_text(raw_desc, max_len=140)
		if not desc:
			continue

		item_entry: dict[str, Any] = {"description": desc}
		if raw_price is not None and raw_price != "":
			try:
				price_val = round(float(raw_price), 2)
				if price_val >= 0:
					item_entry["price"] = price_val
			except (TypeError, ValueError):
				pass
		if raw_qty is not None and raw_qty != "":
			try:
				qty_val = float(raw_qty)
				if qty_val > 0:
					item_entry["qty"] = int(qty_val) if qty_val.is_integer() else round(qty_val, 2)
			except (TypeError, ValueError):
				pass

		sanitized.append(item_entry)

	return sanitized


def _sanitize_photo_refs(clean_photo_refs: Any) -> list[str]:
	if not clean_photo_refs:
		return []
	if isinstance(clean_photo_refs, str):
		candidates = [clean_photo_refs]
	elif isinstance(clean_photo_refs, (list, tuple)):
		candidates = list(clean_photo_refs)
	else:
		return []

	refs: list[str] = []
	for entry in candidates:
		raw_ref = entry.get("file_ref") if isinstance(entry, dict) else entry
		if not isinstance(raw_ref, str):
			continue
		cleaned = raw_ref.strip()
		if cleaned and _SAFE_PHOTO_REF_RE.match(cleaned) and cleaned not in refs:
			refs.append(cleaned)
	return refs


def _sanitize_url(url: Any) -> str | None:
	if not isinstance(url, str):
		return None
	cleaned = url.strip()
	if cleaned and _SAFE_URL_RE.match(cleaned):
		return cleaned
	return None


def _resolve_eta_window(eta_window: Any, internal_start_time: Any) -> str | None:
	cleaned_eta = _scrub_customer_text(eta_window, max_len=120) if eta_window else ""
	if internal_start_time is None or str(internal_start_time).strip() == "":
		return cleaned_eta or None

	raw_internal = _normalize_time_display(internal_start_time)
	customer_time = resolve_customer_arrival_time(internal_start_time) or ""
	if not customer_time:
		return cleaned_eta or None

	if not cleaned_eta:
		return customer_time

	if raw_internal and raw_internal in cleaned_eta:
		return cleaned_eta.replace(raw_internal, customer_time)

	raw_orig = str(internal_start_time).strip()
	if raw_orig and raw_orig in cleaned_eta:
		return cleaned_eta.replace(raw_orig, customer_time)

	if customer_time in cleaned_eta:
		return cleaned_eta

	return f"{cleaned_eta} at {customer_time}"


@dataclass(frozen=True)
class SanitizedCustomerFactPayload:
	"""Strict allowlist payload for customer-facing communication generation.

	Physically excludes raw board chat history, audio transcripts, shop slang,
	bay/tech IDs, supplier costs, margin %, Tesla OEM prices, ``override_notes``,
	and ``verbal_notes``.
	"""

	customer_name: str
	vehicle_summary: str
	reference_id: str
	finding_summary: str
	approved_items: list[dict[str, Any]] = field(default_factory=list)
	clean_photo_refs: list[str] = field(default_factory=list)
	approval_url: str | None = None
	eta_window: str | None = None
	currency: str = "CAD"
	shop_name: str = DEFAULT_SHOP_NAME

	def to_dict(self) -> dict[str, Any]:
		"""Return strictly allowlisted JSON-serializable customer facts."""
		return {
			"customer_name": self.customer_name,
			"vehicle_summary": self.vehicle_summary,
			"reference_id": self.reference_id,
			"finding_summary": self.finding_summary,
			"approved_items": [dict(item) for item in self.approved_items],
			"clean_photo_refs": list(self.clean_photo_refs),
			"approval_url": self.approval_url,
			"eta_window": self.eta_window,
			"currency": self.currency,
			"shop_name": self.shop_name,
		}

	def facts_hash(self) -> str:
		"""Deterministic SHA-256 hash of the allowlisted customer facts."""
		canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
		return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sanitize_for_customer_comms(
	ro_doc: Any,
	finding_summary: str | None,
	approved_items: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
	clean_photo_refs: list[Any] | tuple[Any, ...] | None = None,
	approval_url: str | None = None,
	eta_window: str | None = None,
	*,
	internal_start_time: Any = None,
) -> SanitizedCustomerFactPayload:
	"""Build a strictly allowlisted ``SanitizedCustomerFactPayload`` from ERP inputs."""
	customer_name = _extract_customer_display_name(ro_doc)
	vehicle_summary = _extract_vehicle_summary(ro_doc)
	reference_id = _extract_reference_id(ro_doc)
	clean_summary = _scrub_customer_text(finding_summary, max_len=400)
	clean_items = _sanitize_approved_items(ro_doc, approved_items)
	clean_photos = _sanitize_photo_refs(clean_photo_refs)
	clean_url = _sanitize_url(approval_url)
	resolved_eta = _resolve_eta_window(eta_window, internal_start_time)
	currency = _extract_currency(ro_doc)

	shop_name = DEFAULT_SHOP_NAME
	try:
		settings = frappe.get_cached_doc("ISM Settings")
		brand = getattr(settings, "portal_brand_name", None)
		if isinstance(brand, str) and brand.strip():
			shop_name = _scrub_customer_text(brand, max_len=80) or DEFAULT_SHOP_NAME
	except Exception:
		pass

	return SanitizedCustomerFactPayload(
		customer_name=customer_name,
		vehicle_summary=vehicle_summary,
		reference_id=reference_id,
		finding_summary=clean_summary,
		approved_items=clean_items,
		clean_photo_refs=clean_photos,
		approval_url=clean_url,
		eta_window=resolved_eta,
		currency=currency,
		shop_name=shop_name,
	)


def validate_customer_message_clean(
	text: str,
	*,
	forbidden_values: list[str] | tuple[str, ...] | None = None,
) -> None:
	"""Post-generation leak guard verifying zero forbidden internal tokens/values leak."""
	if not text:
		return

	for label, pattern in _FORBIDDEN_OUTPUT_PATTERNS:
		match = pattern.search(text)
		if match:
			raise CommsFirewallViolationError(
				f"Context Firewall blocked customer message ({label}): matched forbidden token {match.group(0)!r}."
			)

	if forbidden_values:
		lowered = text.lower()
		for raw_secret in forbidden_values:
			if not isinstance(raw_secret, str):
				continue
			secret = raw_secret.strip()
			if len(secret) >= 3 and secret.lower() in lowered:
				raise CommsFirewallViolationError(
					"Context Firewall blocked customer message: forbidden internal value detected."
				)


def _render_deterministic_customer_message(
	payload: SanitizedCustomerFactPayload,
	channel: str,
) -> dict[str, str]:
	"""Clean fallback customer message formatter when AI provider is Disabled."""
	first_name = payload.customer_name.split()[0] if payload.customer_name else "there"
	ref_part = f" ({payload.reference_id})" if payload.reference_id else ""

	item_parts: list[str] = []
	for item in payload.approved_items:
		desc = item.get("description") or ""
		price = item.get("price")
		if desc and price is not None:
			item_parts.append(f"{desc}: ${float(price):.2f}")
		elif desc:
			item_parts.append(desc)

	if channel == "email":
		subject = (
			f"Service Update for your {payload.vehicle_summary}{ref_part}"
			if payload.vehicle_summary != "your vehicle"
			else f"Service Update{ref_part}"
		)
		lines = [f"Hi {first_name},", ""]
		if payload.finding_summary:
			lines.append(f"Update for {payload.vehicle_summary}{ref_part}: {payload.finding_summary}.")
		else:
			lines.append(f"Here is an update regarding {payload.vehicle_summary}{ref_part}.")
		if item_parts:
			lines.append("")
			lines.append("Recommended / quoted items:")
			for line in item_parts:
				lines.append(f"- {line}")
		if payload.eta_window:
			lines.append("")
			lines.append(f"Appointment / arrival time: {payload.eta_window}.")
		if payload.approval_url:
			lines.append("")
			lines.append(f"Review and approve online: {payload.approval_url}")
		lines.append("")
		lines.append(f"Thank you,\n{payload.shop_name}")
		return {"subject": subject, "body": "\n".join(lines).strip()}

	# SMS channel
	segments: list[str] = [f"Hi {first_name}, update on {payload.vehicle_summary}{ref_part}:"]
	if payload.finding_summary:
		segments.append(payload.finding_summary.rstrip(".") + ".")
	if item_parts:
		segments.append("Quoted: " + "; ".join(item_parts) + ".")
	if payload.eta_window:
		segments.append(f"Scheduled for {payload.eta_window}.")
	if payload.approval_url:
		segments.append(f"Approve here: {payload.approval_url}")
	return {"subject": "", "body": " ".join(segments).strip()}


def draft_customer_message(
	sanitized_payload: SanitizedCustomerFactPayload,
	channel: str = "sms",
	provider: Any = None,
	*,
	forbidden_values: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
	"""Draft a customer-facing SMS or Email in an isolated single-turn call.

	Enforces strict ``SanitizedCustomerFactPayload`` input and runs a post-generation
	leak guard verifying zero forbidden internal tokens or values appear in the output.
	"""
	if not isinstance(sanitized_payload, SanitizedCustomerFactPayload):
		raise BoardContractError(
			"draft_customer_message requires a SanitizedCustomerFactPayload instance."
		)

	norm_channel = str(channel or "sms").strip().lower()
	if norm_channel not in ALLOWED_COMMS_CHANNELS:
		raise BoardContractError(
			f"Unsupported customer comms channel {channel!r}; expected one of {ALLOWED_COMMS_CHANNELS}."
		)

	active_provider = provider
	if active_provider is None:
		from ism.board.ai_providers import get_ai_provider

		active_provider = get_ai_provider()

	if active_provider is not None:
		raw_result = active_provider.draft_customer_comms(sanitized_payload, channel=norm_channel)
		if not isinstance(raw_result, dict):
			raise BoardContractError("Provider draft_customer_comms must return a dict.")
		subject = str(raw_result.get("subject") or "").strip() if norm_channel == "email" else ""
		body = str(raw_result.get("body") or "").strip()
	else:
		rendered = _render_deterministic_customer_message(sanitized_payload, norm_channel)
		subject = rendered["subject"]
		body = rendered["body"]

	if not body:
		rendered = _render_deterministic_customer_message(sanitized_payload, norm_channel)
		body = rendered["body"]
		if norm_channel == "email" and not subject:
			subject = rendered["subject"]

	validate_customer_message_clean(subject, forbidden_values=forbidden_values)
	validate_customer_message_clean(body, forbidden_values=forbidden_values)

	return {
		"channel": norm_channel,
		"subject": subject,
		"body": body,
		"sanitized_facts_hash": sanitized_payload.facts_hash(),
		"clean_photo_refs": list(sanitized_payload.clean_photo_refs),
	}


__all__ = [
	"ALLOWED_COMMS_CHANNELS",
	"DEFAULT_SHOP_NAME",
	"CommsFirewallViolationError",
	"SanitizedCustomerFactPayload",
	"draft_customer_message",
	"resolve_customer_arrival_time",
	"sanitize_for_customer_comms",
	"validate_customer_message_clean",
]
