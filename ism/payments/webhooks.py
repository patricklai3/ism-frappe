# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Constant-time Square HMAC-SHA256 webhook verification and payload normalization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from ism.payments.contracts import WEBHOOK_SUPPORTED_EVENTS, WebhookEventNormalized
from ism.payments.square_client import get_square_settings_snapshot


def _clean_str(value: Any) -> str | None:
	if value is None:
		return None
	cleaned = str(value).strip()
	return cleaned or None


def _safe_int(value: Any) -> int | None:
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def compute_square_webhook_signature(
	notification_url: str,
	raw_body: bytes,
	signature_key: str,
) -> str:
	"""Compute canonical Square HMAC-SHA256 base64 signature over (notification_url_utf8 + raw_body)."""
	if not isinstance(notification_url, str) or not notification_url:
		raise ValueError("notification_url must be a non-empty string.")
	if not isinstance(signature_key, str) or not signature_key:
		raise ValueError("signature_key must be a non-empty string.")
	if not isinstance(raw_body, bytes | bytearray):
		raise ValueError("raw_body must be bytes.")

	payload = notification_url.encode("utf-8") + bytes(raw_body)
	digest = hmac.new(signature_key.encode("utf-8"), payload, hashlib.sha256).digest()
	return base64.b64encode(digest).decode("ascii")


def verify_square_webhook_signature(
	raw_body: bytes,
	signature_header: str | None,
	notification_url: str | None = None,
	signature_key: str | None = None,
) -> bool:
	"""Constant-time hmac.compare_digest check against x-square-hmacsha256-signature.

	Falls back to get_square_settings_snapshot() when notification_url or signature_key is omitted.
	Returns False (never raises silently to True) on missing key, missing URL, missing header, or mismatch.
	"""
	if not isinstance(raw_body, bytes | bytearray):
		return False
	if not isinstance(signature_header, str) or not signature_header.strip():
		return False

	resolved_url = notification_url
	resolved_key = signature_key
	if resolved_url is None or resolved_key is None:
		snapshot = get_square_settings_snapshot()
		if resolved_url is None:
			resolved_url = snapshot.get("webhook_notification_url")
		if resolved_key is None:
			resolved_key = snapshot.get("webhook_signature_key")

	if not isinstance(resolved_url, str) or not resolved_url.strip():
		return False
	if not isinstance(resolved_key, str) or not resolved_key.strip():
		return False

	try:
		expected = compute_square_webhook_signature(
			resolved_url.strip(),
			bytes(raw_body),
			resolved_key.strip(),
		)
	except (TypeError, ValueError):
		return False

	return hmac.compare_digest(expected, signature_header.strip())


def _build_sanitized_summary(event: WebhookEventNormalized) -> str:
	parts = [event.get("event_type") or "unknown"]
	if event.get("event_id"):
		parts.append(f"event={event['event_id']}")
	if event.get("square_payment_id"):
		parts.append(f"payment={event['square_payment_id']}")
	if event.get("payment_status"):
		parts.append(f"status={event['payment_status']}")
	if event.get("refund_id"):
		parts.append(f"refund={event['refund_id']}")
	if event.get("refund_status"):
		parts.append(f"refund_status={event['refund_status']}")
	if event.get("refund_amount_cents") is not None:
		parts.append(f"refund_cents={event['refund_amount_cents']}")
	if event.get("dispute_id"):
		parts.append(f"dispute={event['dispute_id']}")
	if event.get("dispute_state"):
		parts.append(f"dispute_state={event['dispute_state']}")
	if event.get("amount_cents") is not None:
		parts.append(f"amount_cents={event['amount_cents']}")
	if event.get("currency"):
		parts.append(f"currency={event['currency']}")
	if event.get("ignored"):
		parts.append("ignored=True")
	return " ".join(parts)


def parse_square_webhook_event(raw_body: bytes) -> WebhookEventNormalized:
	"""Decode Square JSON envelope into a normalized, PII-free WebhookEventNormalized dict.

	Handles payment.created, payment.updated, refund.created, refund.updated,
	dispute.created, dispute.state.updated; tags unknown event types ignored=True.
	Raises ValueError on malformed JSON or non-object payloads.
	"""
	if isinstance(raw_body, bytes | bytearray):
		try:
			decoded = bytes(raw_body).decode("utf-8")
		except UnicodeDecodeError as exc:
			raise ValueError("Malformed UTF-8 in Square webhook payload.") from exc
	elif isinstance(raw_body, str):
		decoded = raw_body
	else:
		raise ValueError("Square webhook payload must be bytes.")

	try:
		payload = json.loads(decoded)
	except json.JSONDecodeError as exc:
		raise ValueError("Malformed JSON in Square webhook payload.") from exc

	if not isinstance(payload, dict):
		raise ValueError("Square webhook payload must be a JSON object.")

	event_id = _clean_str(payload.get("event_id")) or ""
	event_type = _clean_str(payload.get("type")) or ""
	merchant_id = _clean_str(payload.get("merchant_id"))
	created_at = _clean_str(payload.get("created_at"))

	data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
	obj = data.get("object") if isinstance(data.get("object"), dict) else {}

	normalized: WebhookEventNormalized = {
		"event_id": event_id,
		"event_type": event_type,
		"merchant_id": merchant_id,
		"created_at": created_at,
		"square_payment_id": None,
		"reference_id": None,
		"payment_status": None,
		"amount_cents": None,
		"currency": None,
		"receipt_url": None,
		"receipt_number": None,
		"card_brand": None,
		"card_last4": None,
		"refund_id": None,
		"refund_status": None,
		"refund_amount_cents": None,
		"dispute_id": None,
		"dispute_state": None,
		"ignored": event_type not in WEBHOOK_SUPPORTED_EVENTS,
	}

	if event_type in ("payment.created", "payment.updated"):
		payment = obj.get("payment") if isinstance(obj.get("payment"), dict) else {}
		amount_money = payment.get("amount_money") if isinstance(payment.get("amount_money"), dict) else {}
		card_details = payment.get("card_details") if isinstance(payment.get("card_details"), dict) else {}
		card = card_details.get("card") if isinstance(card_details.get("card"), dict) else {}

		normalized["square_payment_id"] = _clean_str(payment.get("id")) or _clean_str(data.get("id"))
		normalized["reference_id"] = _clean_str(payment.get("reference_id"))
		normalized["payment_status"] = _clean_str(payment.get("status"))
		normalized["amount_cents"] = _safe_int(amount_money.get("amount"))
		normalized["currency"] = _clean_str(amount_money.get("currency"))
		normalized["receipt_url"] = _clean_str(payment.get("receipt_url"))
		normalized["receipt_number"] = _clean_str(payment.get("receipt_number"))
		normalized["card_brand"] = _clean_str(card.get("card_brand"))
		normalized["card_last4"] = _clean_str(card.get("last_4"))

	elif event_type in ("refund.created", "refund.updated"):
		refund = obj.get("refund") if isinstance(obj.get("refund"), dict) else {}
		amount_money = refund.get("amount_money") if isinstance(refund.get("amount_money"), dict) else {}

		normalized["square_payment_id"] = _clean_str(refund.get("payment_id"))
		normalized["refund_id"] = _clean_str(refund.get("id")) or _clean_str(data.get("id"))
		normalized["refund_status"] = _clean_str(refund.get("status"))
		normalized["refund_amount_cents"] = _safe_int(amount_money.get("amount"))
		normalized["currency"] = _clean_str(amount_money.get("currency"))

	elif event_type in ("dispute.created", "dispute.state.updated"):
		dispute = obj.get("dispute") if isinstance(obj.get("dispute"), dict) else {}
		disputed_payment = (
			dispute.get("disputed_payment") if isinstance(dispute.get("disputed_payment"), dict) else {}
		)
		amount_money = dispute.get("amount_money") if isinstance(dispute.get("amount_money"), dict) else {}

		normalized["dispute_id"] = (
			_clean_str(dispute.get("dispute_id"))
			or _clean_str(dispute.get("id"))
			or _clean_str(data.get("id"))
		)
		normalized["dispute_state"] = _clean_str(dispute.get("state"))
		normalized["square_payment_id"] = _clean_str(disputed_payment.get("payment_id"))
		normalized["amount_cents"] = _safe_int(amount_money.get("amount"))
		normalized["currency"] = _clean_str(amount_money.get("currency"))

	normalized["sanitized_summary"] = _build_sanitized_summary(normalized)
	return normalized
