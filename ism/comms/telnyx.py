# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from typing import Any

import frappe
import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from frappe import _
from frappe.utils import cint, now_datetime

from ism.comms.contracts import (
	HELP_KEYWORDS,
	START_KEYWORDS,
	STOP_KEYWORDS,
	SUPPORTED_SMS_PROVIDERS,
	TELNYX_MESSAGES_ENDPOINT,
	TELNYX_OPT_OUT_ERROR_CODES,
	SMSDispatchRequest,
	normalize_phone_e164,
	redact_otp_body,
)

_KEYWORD_CLEAN_RE = re.compile(r"[^A-Z0-9]+")
_MAX_SANITIZED_PAYLOAD_LEN = 4000
_MAX_ERROR_MESSAGE_LEN = 500
_DEFAULT_HELP_REPLY_TEXT = "Reply STOP to unsubscribe from SMS alerts, or call the shop for assistance."

_TELNYX_STATUS_MAP: dict[str, str] = {
	"queued": "Queued",
	"sent": "Sent",
	"sending": "Sent",
	"delivered": "Delivered",
	"delivery_failed": "Failed",
	"sending_failed": "Failed",
	"delivery_unconfirmed": "Failed",
	"failed": "Failed",
}


def get_settings_password(settings: Any, fieldname: str) -> str:
	"""Safely read a Password field from an ISM Settings document or test stub."""
	raw = getattr(settings, fieldname, None)
	if not raw:
		return ""
	if hasattr(settings, "get_password"):
		try:
			value = settings.get_password(fieldname, raise_exception=False)
			if value:
				return str(value).strip()
		except Exception:
			pass
	text = str(raw).strip()
	if text and set(text) == {"*"}:
		return ""
	return text


def _try_normalize_sender_e164(settings: Any) -> str | None:
	sender_raw = (getattr(settings, "sms_sender_id", None) or "").strip()
	if not sender_raw:
		return None
	default_cc = (getattr(settings, "sms_default_country_code", None) or "+1").strip() or "+1"
	try:
		return normalize_phone_e164(sender_raw, default_country_code=default_cc)
	except ValueError:
		return None


def _parse_telnyx_error(response: Any) -> tuple[str, str]:
	"""Extract Telnyx v2 (code, detail) error info from a non-2xx HTTP response."""
	status_code = getattr(response, "status_code", 0)
	error_code = f"HTTP_{status_code}"
	error_detail = f"HTTP {status_code} from Telnyx API."
	try:
		body = response.json()
	except Exception:
		body = None

	if isinstance(body, dict):
		errors = body.get("errors")
		if isinstance(errors, list) and errors and isinstance(errors[0], dict):
			first = errors[0]
			if first.get("code") is not None and str(first.get("code")).strip():
				error_code = str(first["code"]).strip()
			detail = first.get("detail") or first.get("title")
			if isinstance(detail, str) and detail.strip():
				error_detail = detail.strip()

	return error_code, error_detail


def _extract_event_error(inner: dict[str, Any]) -> tuple[str | None, str | None, bool]:
	"""Extract (error_code, error_message, is_opt_out_error) from a Telnyx event payload."""
	error_entries: list[dict[str, Any]] = []
	top_errors = inner.get("errors")
	if isinstance(top_errors, list):
		error_entries.extend(item for item in top_errors if isinstance(item, dict))

	to_entries = inner.get("to")
	if isinstance(to_entries, list):
		for entry in to_entries:
			if isinstance(entry, dict) and isinstance(entry.get("errors"), list):
				error_entries.extend(item for item in entry["errors"] if isinstance(item, dict))

	if not error_entries:
		return None, None, False

	first = error_entries[0]
	code = str(first.get("code")).strip() if first.get("code") is not None else None
	detail = (first.get("detail") or first.get("title") or "").strip() or None
	is_opt_out = any(
		str(item.get("code") or "").strip() in TELNYX_OPT_OUT_ERROR_CODES for item in error_entries
	)
	if is_opt_out and code not in TELNYX_OPT_OUT_ERROR_CODES:
		for item in error_entries:
			item_code = str(item.get("code") or "").strip()
			if item_code in TELNYX_OPT_OUT_ERROR_CODES:
				code = item_code
				detail = (item.get("detail") or item.get("title") or detail or "").strip() or None
				break
	return code, detail, is_opt_out


def _sanitize_webhook_payload(payload: dict[str, Any], *, redact_text: bool = False) -> str:
	"""Serialize a webhook payload safely, redacting message text when required."""
	try:
		copied = json.loads(json.dumps(payload, default=str))
	except Exception:
		return "{}"

	if isinstance(copied, dict):
		data = copied.get("data")
		if isinstance(data, dict):
			inner = data.get("payload")
			if isinstance(inner, dict) and "text" in inner:
				if redact_text:
					inner["text"] = "[REDACTED]"
				elif isinstance(inner["text"], str):
					inner["text"] = inner["text"][:500]
		if redact_text and isinstance(copied.get("text"), str):
			copied["text"] = "[REDACTED]"

	serialized = json.dumps(copied, separators=(",", ":"), sort_keys=True)
	if redact_text:
		serialized = redact_otp_body(serialized)
	if len(serialized) > _MAX_SANITIZED_PAYLOAD_LEN:
		return serialized[:_MAX_SANITIZED_PAYLOAD_LEN]
	return serialized


def _parse_unix_timestamp(timestamp_str: str | None) -> tuple[int, str] | None:
	if not isinstance(timestamp_str, str):
		return None
	cleaned = timestamp_str.strip()
	if not cleaned or not cleaned.isdigit():
		return None
	try:
		ts = int(cleaned)
	except ValueError:
		return None
	if ts <= 0:
		return None
	return ts, cleaned


def _load_ed25519_public_key(public_key_str: str) -> Ed25519PublicKey | None:
	key_text = (public_key_str or "").strip()
	if not key_text:
		return None
	try:
		if "-----BEGIN PUBLIC KEY-----" in key_text:
			loaded = serialization.load_pem_public_key(key_text.encode("utf-8"))
			return loaded if isinstance(loaded, Ed25519PublicKey) else None
		key_bytes = base64.b64decode(key_text, validate=True)
		if len(key_bytes) != 32:
			return None
		return Ed25519PublicKey.from_public_bytes(key_bytes)
	except (ValueError, TypeError, binascii.Error):
		return None


class TelnyxSMSProvider:
	"""Telnyx Messaging API v2 HTTPS transport adapter."""

	provider_name: str = "Telnyx"

	def validate_config(self, settings: Any) -> str | None:
		api_key = get_settings_password(settings, "telnyx_api_key")
		if not api_key:
			return _("Telnyx API Key is required in ISM Settings.")

		profile_id = (getattr(settings, "telnyx_messaging_profile_id", None) or "").strip()
		sender_e164 = _try_normalize_sender_e164(settings)
		if not sender_e164 and not profile_id:
			return _(
				"Telnyx requires either an E.164 Sender ID / From Number or a Telnyx Messaging Profile ID."
			)
		return None

	def send(self, phone_e164: str, body: str, settings: Any, *, http_post: Any = None) -> dict[str, Any]:
		api_key = get_settings_password(settings, "telnyx_api_key")
		profile_id = (getattr(settings, "telnyx_messaging_profile_id", None) or "").strip()
		sender_e164 = _try_normalize_sender_e164(settings)

		headers = {
			"Authorization": f"Bearer {api_key}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		payload: dict[str, Any] = {
			"to": phone_e164,
			"text": body,
			"type": "SMS",
		}
		if sender_e164:
			payload["from"] = sender_e164
		if profile_id:
			payload["messaging_profile_id"] = profile_id

		post_fn = http_post or requests.post
		try:
			response = post_fn(
				TELNYX_MESSAGES_ENDPOINT,
				headers=headers,
				data=json.dumps(payload),
				timeout=10,
				allow_redirects=False,
			)
			if response.status_code in (200, 201, 202):
				msg_id = str(response.status_code)
				try:
					res_data = response.json()
					if isinstance(res_data, dict):
						data_obj = res_data.get("data")
						if isinstance(data_obj, dict) and data_obj.get("id"):
							msg_id = str(data_obj["id"])
						elif res_data.get("id"):
							msg_id = str(res_data["id"])
				except Exception:
					pass
				return {
					"success": True,
					"message_id": msg_id,
					"status": "Queued",
					"error": None,
					"error_code": None,
					"opted_out": False,
				}

			error_code, error_detail = _parse_telnyx_error(response)
			is_opt_out = error_code in TELNYX_OPT_OUT_ERROR_CODES
			if is_opt_out:
				from ism.comms.providers import upsert_sms_opt_out

				try:
					upsert_sms_opt_out(
						phone_e164,
						is_opted_out=True,
						source="Carrier Opt-Out Error",
						notes=error_detail[:_MAX_ERROR_MESSAGE_LEN],
					)
				except Exception:
					frappe.log_error(
						title="ISM Telnyx Opt-Out Upsert Failure",
						message=frappe.get_traceback(),
					)
			else:
				frappe.log_error(
					title="ISM Telnyx SMS Failure",
					message=f"HTTP {response.status_code} ({error_code}): {error_detail[:200]}",
				)

			return {
				"success": False,
				"message_id": None,
				"status": "Failed",
				"error": error_detail,
				"error_code": error_code,
				"opted_out": is_opt_out,
			}
		except Exception:
			frappe.log_error(
				title="ISM Telnyx SMS Dispatch Exception",
				message=frappe.get_traceback(),
			)
			return {
				"success": False,
				"message_id": None,
				"status": "Failed",
				"error": _("SMS dispatch failed due to a Telnyx communication error."),
				"error_code": "NETWORK_ERROR",
				"opted_out": False,
			}


def verify_telnyx_webhook_signature(
	raw_body: bytes,
	signature_b64: str | None,
	timestamp_str: str | None,
	public_key_str: str,
	*,
	tolerance_seconds: int = 300,
	now_ts: int | None = None,
) -> bool:
	"""Verify a Telnyx Ed25519 webhook signature and timestamp replay window (fail-closed)."""
	try:
		if not isinstance(raw_body, (bytes, bytearray)):
			return False
		if not isinstance(signature_b64, str) or not signature_b64.strip():
			return False

		parsed_ts = _parse_unix_timestamp(timestamp_str)
		if parsed_ts is None:
			return False
		ts, ts_clean = parsed_ts

		current_ts = int(time.time()) if now_ts is None else int(now_ts)
		tolerance = max(0, int(tolerance_seconds))
		if abs(current_ts - ts) > tolerance:
			return False

		pub_key = _load_ed25519_public_key(public_key_str)
		if pub_key is None:
			return False

		sig_bytes = base64.b64decode(signature_b64.strip(), validate=True)
		if len(sig_bytes) != 64:
			return False

		signed_payload = f"{ts_clean}|".encode() + bytes(raw_body)
		pub_key.verify(sig_bytes, signed_payload)
		return True
	except (InvalidSignature, ValueError, TypeError, binascii.Error):
		return False
	except Exception:
		return False


def verify_custom_hmac_webhook_signature(
	raw_body: bytes,
	signature_hex: str | None,
	timestamp_str: str | None,
	hmac_secret: str,
	*,
	tolerance_seconds: int = 300,
	now_ts: int | None = None,
) -> bool:
	"""Verify a custom HMAC-SHA256 webhook signature and timestamp replay window (fail-closed)."""
	try:
		if not isinstance(raw_body, (bytes, bytearray)):
			return False
		if not isinstance(signature_hex, str) or not signature_hex.strip():
			return False
		if not isinstance(hmac_secret, str) or not hmac_secret.strip():
			return False

		parsed_ts = _parse_unix_timestamp(timestamp_str)
		if parsed_ts is None:
			return False
		ts, ts_clean = parsed_ts

		current_ts = int(time.time()) if now_ts is None else int(now_ts)
		tolerance = max(0, int(tolerance_seconds))
		if abs(current_ts - ts) > tolerance:
			return False

		provided_sig = signature_hex.strip()
		if provided_sig.lower().startswith("sha256="):
			provided_sig = provided_sig[7:].strip()
		if not provided_sig:
			return False

		signed_payload = f"{ts_clean}|".encode() + bytes(raw_body)
		expected_hex = hmac.new(hmac_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
		return hmac.compare_digest(expected_hex, provided_sig.lower())
	except Exception:
		return False


def verify_webhook_request(
	raw_body: bytes,
	headers: Any,
	settings: Any = None,
	*,
	now_ts: int | None = None,
) -> tuple[bool, str]:
	"""Fail-closed webhook verifier. Enforces Ed25519-only in Telnyx mode (zero silent HMAC downgrade)."""
	if settings is None:
		settings = frappe.get_single("ISM Settings")

	lower_headers: dict[str, str] = {}
	if headers is not None and hasattr(headers, "items"):
		for key, val in headers.items():
			if key is not None and val is not None:
				lower_headers[str(key).lower()] = str(val)

	provider = (getattr(settings, "sms_gateway_provider", None) or "Disabled").strip()
	tolerance = cint(getattr(settings, "telnyx_webhook_tolerance_seconds", 300)) or 300
	ed25519_key = (getattr(settings, "telnyx_webhook_public_key", None) or "").strip()
	hmac_secret = get_settings_password(settings, "sms_webhook_hmac_secret")

	ed25519_sig = lower_headers.get("telnyx-signature-ed25519")
	ed25519_ts = lower_headers.get("telnyx-timestamp")
	hmac_sig = lower_headers.get("x-ism-webhook-signature")
	hmac_ts = lower_headers.get("x-ism-webhook-timestamp")

	if provider == "Telnyx":
		if not ed25519_key:
			return False, "missing_ed25519_key"
		if verify_telnyx_webhook_signature(
			raw_body,
			ed25519_sig,
			ed25519_ts,
			ed25519_key,
			tolerance_seconds=tolerance,
			now_ts=now_ts,
		):
			return True, "ed25519"
		return False, "invalid_ed25519_signature"

	if provider in ("Custom HTTP Webhook", "Mock (Deterministic Test)"):
		if ed25519_sig and ed25519_key:
			if verify_telnyx_webhook_signature(
				raw_body,
				ed25519_sig,
				ed25519_ts,
				ed25519_key,
				tolerance_seconds=tolerance,
				now_ts=now_ts,
			):
				return True, "ed25519"
			return False, "invalid_ed25519_signature"

		if hmac_secret:
			if verify_custom_hmac_webhook_signature(
				raw_body,
				hmac_sig,
				hmac_ts,
				hmac_secret,
				tolerance_seconds=tolerance,
				now_ts=now_ts,
			):
				return True, "hmac_sha256"
			return False, "invalid_hmac_signature"

		if ed25519_key:
			return False, "invalid_ed25519_signature"

		return False, "missing_webhook_credentials"

	return False, "provider_disabled"


def _resolve_next_delivery_status(current_status: str, mapped_status: str | None) -> str:
	"""Enforce monotonic outbound delivery status transitions."""
	if not mapped_status:
		return current_status
	if current_status == "Delivered":
		return "Delivered"
	if current_status == "Sent" and mapped_status == "Queued":
		return "Sent"
	if current_status == "Failed" and mapped_status in ("Queued", "Sent"):
		return "Failed"
	return mapped_status


def _process_outbound_status_event(
	payload: dict[str, Any],
	event_id: str,
	event_type: str,
	inner: dict[str, Any],
) -> dict[str, Any]:
	from ism.comms.providers import upsert_sms_opt_out

	provider_message_id = str(inner.get("id") or "").strip()
	if not provider_message_id:
		return {"action": "ignored", "event_id": event_id, "reason": "missing_message_id"}

	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabSMS Audit Log`
		WHERE provider_message_id = %s
		ORDER BY creation DESC
		LIMIT 1
		FOR UPDATE
		""",
		(provider_message_id,),
		as_dict=True,
	)
	if not rows:
		return {
			"action": "unmatched",
			"event_id": event_id,
			"provider_message_id": provider_message_id,
		}

	audit_doc = frappe.get_doc("SMS Audit Log", rows[0]["name"])
	if event_id and audit_doc.last_webhook_event_id == event_id:
		return {
			"action": "duplicate_ignored",
			"event_id": event_id,
			"audit_log": audit_doc.name,
			"status": audit_doc.status,
		}

	raw_status = ""
	to_entries = inner.get("to")
	if isinstance(to_entries, list) and to_entries and isinstance(to_entries[0], dict):
		raw_status = str(to_entries[0].get("status") or "").strip().lower()
	if not raw_status:
		raw_status = str(inner.get("status") or "").strip().lower()
	if not raw_status and event_type == "message.sent":
		raw_status = "sent"

	mapped_status = _TELNYX_STATUS_MAP.get(raw_status)
	next_status = _resolve_next_delivery_status(audit_doc.status, mapped_status)
	audit_doc.status = next_status

	now = now_datetime()
	if next_status == "Sent" and not audit_doc.sent_at:
		audit_doc.sent_at = now
	if next_status == "Delivered" and not audit_doc.delivered_at:
		audit_doc.delivered_at = now

	err_code, err_detail, is_opt_out = _extract_event_error(inner)
	if err_code:
		audit_doc.error_code = err_code
	if err_detail:
		audit_doc.error_message = err_detail[:_MAX_ERROR_MESSAGE_LEN]

	opt_out_updated = False
	if is_opt_out:
		target_phone = audit_doc.phone_e164
		if not target_phone and isinstance(to_entries, list) and to_entries and isinstance(to_entries[0], dict):
			target_phone = str(to_entries[0].get("phone_number") or "").strip()
		if target_phone:
			upsert_sms_opt_out(
				target_phone,
				is_opted_out=True,
				source="Carrier Opt-Out Error",
				event_id=event_id or None,
				notes=(err_detail or f"Telnyx error {err_code}")[:_MAX_ERROR_MESSAGE_LEN],
			)
			opt_out_updated = True

	if event_id:
		audit_doc.last_webhook_event_id = event_id
	should_redact = audit_doc.message_type == "OTP" or bool(cint(audit_doc.is_redacted))
	audit_doc.webhook_payload_sanitized = _sanitize_webhook_payload(payload, redact_text=should_redact)
	audit_doc.save(ignore_permissions=True)

	return {
		"action": "status_updated",
		"event_id": event_id,
		"audit_log": audit_doc.name,
		"status": audit_doc.status,
		"opt_out_updated": opt_out_updated,
	}


def _process_inbound_message_event(
	payload: dict[str, Any],
	event_id: str,
	inner: dict[str, Any],
) -> dict[str, Any]:
	from ism.comms.providers import send_sms, upsert_sms_opt_out

	if event_id:
		existing = frappe.db.sql(
			"""
			SELECT name
			FROM `tabSMS Audit Log`
			WHERE direction = 'Inbound' AND last_webhook_event_id = %s
			LIMIT 1
			FOR UPDATE
			""",
			(event_id,),
			as_dict=True,
		)
		if existing:
			return {
				"action": "duplicate_ignored",
				"event_id": event_id,
				"audit_log": existing[0]["name"],
			}

	settings = frappe.get_single("ISM Settings")
	default_cc = (getattr(settings, "sms_default_country_code", None) or "+1").strip() or "+1"

	from_obj = inner.get("from")
	raw_from = (
		from_obj.get("phone_number")
		if isinstance(from_obj, dict)
		else (from_obj if isinstance(from_obj, str) else "")
	)
	try:
		phone_e164 = normalize_phone_e164(str(raw_from or ""), default_country_code=default_cc)
	except ValueError:
		return {"action": "ignored", "event_id": event_id, "reason": "invalid_sender_phone"}

	to_phone = None
	to_entries = inner.get("to")
	if isinstance(to_entries, list) and to_entries and isinstance(to_entries[0], dict):
		to_phone = str(to_entries[0].get("phone_number") or "").strip() or None

	raw_text = str(inner.get("text") or "").strip()
	first_token = raw_text.split()[0].upper() if raw_text else ""
	keyword = _KEYWORD_CLEAN_RE.sub("", first_token)

	configured_provider = (getattr(settings, "sms_gateway_provider", None) or "Telnyx").strip()
	log_provider = configured_provider if configured_provider in SUPPORTED_SMS_PROVIDERS else "Telnyx"

	inbound_doc = frappe.get_doc(
		{
			"doctype": "SMS Audit Log",
			"direction": "Inbound",
			"provider": log_provider,
			"message_type": "Inbound Keyword",
			"phone_e164": phone_e164,
			"sender_id": to_phone,
			"provider_message_id": str(inner.get("id") or "").strip() or None,
			"status": "Received",
			"is_redacted": 0,
			"message_preview": raw_text[:500],
			"sent_at": now_datetime(),
			"last_webhook_event_id": event_id or None,
			"webhook_payload_sanitized": _sanitize_webhook_payload(payload, redact_text=False),
		}
	).insert(ignore_permissions=True)

	response_payload: dict[str, Any] = {
		"action": "inbound_logged",
		"event_id": event_id,
		"phone_e164": phone_e164,
		"keyword": keyword,
		"audit_log": inbound_doc.name,
	}

	if keyword in STOP_KEYWORDS:
		upsert_sms_opt_out(
			phone_e164,
			is_opted_out=True,
			source="Telnyx STOP Keyword",
			last_keyword=keyword,
			event_id=event_id or None,
		)
		response_payload["action"] = "opted_out"
	elif keyword in START_KEYWORDS:
		upsert_sms_opt_out(
			phone_e164,
			is_opted_out=False,
			source="Customer Re-Opt-In (START)",
			last_keyword=keyword,
			event_id=event_id or None,
		)
		response_payload["action"] = "opted_in"
	elif keyword in HELP_KEYWORDS:
		help_text = (getattr(settings, "sms_help_reply_text", None) or "").strip() or _DEFAULT_HELP_REPLY_TEXT
		reply_res = send_sms(
			SMSDispatchRequest(
				to_phone=phone_e164,
				body=help_text,
				message_type="Compliance Reply",
				respect_opt_out=False,
			),
			settings=settings,
		)
		response_payload["action"] = "help_replied"
		response_payload["reply_result"] = reply_res.to_dict()

	return response_payload


def process_telnyx_webhook_event(payload: dict[str, Any]) -> dict[str, Any]:
	"""Idempotently process a verified Telnyx v2 webhook event payload."""
	if not isinstance(payload, dict):
		return {"action": "ignored", "reason": "invalid_payload"}

	data = payload.get("data")
	if not isinstance(data, dict):
		return {"action": "ignored", "reason": "missing_data"}

	event_id = str(data.get("id") or "").strip()
	event_type = str(data.get("event_type") or "").strip()
	inner = data.get("payload") if isinstance(data.get("payload"), dict) else {}

	if event_type in ("message.sent", "message.finalized"):
		return _process_outbound_status_event(payload, event_id, event_type, inner)

	if event_type == "message.received":
		return _process_inbound_message_event(payload, event_id, inner)

	return {"action": "ignored", "event_id": event_id, "event_type": event_type}
