# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from typing import Any

import frappe
import requests
from frappe import _
from frappe.utils import cint, now_datetime

from ism.comms.contracts import (
	SUPPORTED_SMS_PROVIDERS,
	TELNYX_OPT_OUT_ERROR_CODES,
	SMSDispatchRequest,
	SMSDispatchResult,
	SMSProvider,
	is_valid_sms_webhook_url,
	normalize_phone_e164,
	redact_otp_body,
)
from ism.comms.telnyx import TelnyxSMSProvider, get_settings_password

_MOCK_OUTBOX_KEY = "ism:comms:mock_outbox"
_MOCK_SEQ_KEY = "ism:comms:mock_outbox_seq"
_MOCK_NEXT_ERROR_KEY = "ism:comms:mock_next_error"
_MOCK_OUTBOX_TTL_SEC = 3600
_MAX_MESSAGE_PREVIEW_LEN = 500
_MAX_ERROR_MESSAGE_LEN = 500

_VALID_AUDIT_MESSAGE_TYPES: frozenset[str] = frozenset(
	{
		"Notification",
		"Approval Link",
		"Ready For Pickup",
		"OTP",
		"Booking Confirmation",
		"Reschedule Confirmation",
		"Compliance Reply",
		"Inbound Keyword",
		"Other",
	}
)
_VALID_AUDIT_STATUSES: frozenset[str] = frozenset(
	{
		"Queued",
		"Sent",
		"Delivered",
		"Failed",
		"Blocked - Opted Out",
		"Blocked - Unconfigured",
		"Received",
	}
)

_IN_PROCESS_MOCK_OUTBOX: list[dict[str, Any]] = []
_IN_PROCESS_MOCK_SEQ: int = 0
_IN_PROCESS_NEXT_ERROR: dict[str, str] | None = None


def _cache_or_none():
	try:
		return frappe.cache()
	except Exception:
		return None


def _next_mock_sequence() -> int:
	global _IN_PROCESS_MOCK_SEQ
	cache = _cache_or_none()
	if cache is not None:
		try:
			seq_key = cache.make_key(_MOCK_SEQ_KEY) if hasattr(cache, "make_key") else _MOCK_SEQ_KEY
			if hasattr(cache, "incr"):
				seq = int(cache.incr(seq_key))
				_IN_PROCESS_MOCK_SEQ = seq
				return seq
		except Exception:
			pass
	_IN_PROCESS_MOCK_SEQ += 1
	return _IN_PROCESS_MOCK_SEQ


def get_mock_sms_outbox() -> list[dict[str, Any]]:
	"""Return deterministic mock SMS outbox records (Redis-backed with in-process fallback)."""
	cache = _cache_or_none()
	if cache is not None:
		try:
			cached = cache.get_value(_MOCK_OUTBOX_KEY)
			if isinstance(cached, list):
				return list(cached)
		except Exception:
			pass
	return list(_IN_PROCESS_MOCK_OUTBOX)


def clear_mock_sms_outbox() -> None:
	"""Clear mock SMS outbox, sequence counter, and any queued forced mock error."""
	global _IN_PROCESS_MOCK_SEQ, _IN_PROCESS_NEXT_ERROR
	_IN_PROCESS_MOCK_OUTBOX.clear()
	_IN_PROCESS_MOCK_SEQ = 0
	_IN_PROCESS_NEXT_ERROR = None

	cache = _cache_or_none()
	if cache is not None:
		try:
			cache.delete_value(_MOCK_OUTBOX_KEY)
			cache.delete_value(_MOCK_NEXT_ERROR_KEY)
			if hasattr(cache, "make_key") and hasattr(cache, "delete"):
				cache.delete(cache.make_key(_MOCK_SEQ_KEY))
			else:
				cache.delete_value(_MOCK_SEQ_KEY)
		except Exception:
			pass


def set_mock_sms_next_error(error_code: str | None, error_message: str | None = None) -> None:
	"""Queue a one-shot deterministic error for the next MockSMSProvider.send() call."""
	global _IN_PROCESS_NEXT_ERROR
	if not error_code:
		clear_mock_sms_next_error()
		return

	payload = {
		"error_code": str(error_code).strip(),
		"error_message": (error_message or f"Mock SMS error {error_code}").strip(),
	}
	_IN_PROCESS_NEXT_ERROR = payload

	cache = _cache_or_none()
	if cache is not None:
		try:
			cache.set_value(_MOCK_NEXT_ERROR_KEY, payload, expires_in_sec=_MOCK_OUTBOX_TTL_SEC)
		except Exception:
			pass


def clear_mock_sms_next_error() -> None:
	"""Clear any queued one-shot mock SMS error."""
	global _IN_PROCESS_NEXT_ERROR
	_IN_PROCESS_NEXT_ERROR = None
	cache = _cache_or_none()
	if cache is not None:
		try:
			cache.delete_value(_MOCK_NEXT_ERROR_KEY)
		except Exception:
			pass


def _pop_mock_sms_next_error() -> dict[str, str] | None:
	global _IN_PROCESS_NEXT_ERROR
	payload = None
	cache = _cache_or_none()
	if cache is not None:
		try:
			cached = cache.get_value(_MOCK_NEXT_ERROR_KEY)
			if isinstance(cached, dict) and cached.get("error_code"):
				payload = cached
				cache.delete_value(_MOCK_NEXT_ERROR_KEY)
		except Exception:
			pass
	if payload is None and _IN_PROCESS_NEXT_ERROR is not None:
		payload = dict(_IN_PROCESS_NEXT_ERROR)
	_IN_PROCESS_NEXT_ERROR = None
	return payload


class DisabledSMSProvider:
	"""No-op provider returned when SMS dispatch is disabled."""

	provider_name: str = "Disabled"

	def validate_config(self, settings: Any) -> str | None:
		return _("SMS Gateway is disabled in ISM Settings. Please enable and configure a provider.")

	def send(self, phone_e164: str, body: str, settings: Any, *, http_post: Any = None) -> dict[str, Any]:
		return {
			"success": False,
			"message_id": None,
			"status": "Blocked - Unconfigured",
			"error": self.validate_config(settings),
			"error_code": "UNCONFIGURED",
			"opted_out": False,
		}


class CustomWebhookSMSProvider:
	"""Backward-compatible generic HTTPS JSON webhook SMS transport."""

	provider_name: str = "Custom HTTP Webhook"

	def validate_config(self, settings: Any) -> str | None:
		endpoint = (getattr(settings, "sms_gateway_endpoint", None) or "").strip()
		if not is_valid_sms_webhook_url(endpoint):
			return _("Valid HTTPS SMS Gateway Endpoint URL is required (HTTP allowed only for loopback).")
		return None

	def send(self, phone_e164: str, body: str, settings: Any, *, http_post: Any = None) -> dict[str, Any]:
		endpoint = (getattr(settings, "sms_gateway_endpoint", None) or "").strip()
		headers = {"Content-Type": "application/json"}
		api_key = get_settings_password(settings, "sms_gateway_api_key")
		if api_key:
			headers["Authorization"] = f"Bearer {api_key}"

		payload = {
			"to": phone_e164,
			"from": (getattr(settings, "sms_sender_id", None) or "ISM").strip(),
			"body": body,
		}

		post_fn = http_post or requests.post
		try:
			response = post_fn(
				endpoint,
				headers=headers,
				data=json.dumps(payload),
				timeout=10,
				allow_redirects=False,
			)
			if response.status_code in (200, 201, 202):
				try:
					res_data = response.json()
					msg_id = (
						(res_data.get("message_id") or res_data.get("id"))
						if isinstance(res_data, dict)
						else None
					) or str(response.status_code)
				except Exception:
					msg_id = str(response.status_code)
				return {
					"success": True,
					"message_id": str(msg_id),
					"status": "Sent",
					"error": None,
					"error_code": None,
					"opted_out": False,
				}

			frappe.log_error(
				title="ISM SMS Webhook Failure",
				message=f"HTTP {response.status_code}: {str(getattr(response, 'text', ''))[:200]}",
			)
			return {
				"success": False,
				"message_id": None,
				"status": "Failed",
				"error": f"HTTP {response.status_code} from SMS gateway.",
				"error_code": f"HTTP_{response.status_code}",
				"opted_out": False,
			}
		except Exception:
			frappe.log_error(
				title="ISM SMS Dispatch Exception",
				message=frappe.get_traceback(),
			)
			return {
				"success": False,
				"message_id": None,
				"status": "Failed",
				"error": _("SMS dispatch failed due to a gateway communication error."),
				"error_code": "NETWORK_ERROR",
				"opted_out": False,
			}


class MockSMSProvider:
	"""Deterministic offline SMS provider backed by a Redis + in-process outbox."""

	provider_name: str = "Mock (Deterministic Test)"

	def validate_config(self, settings: Any) -> str | None:
		return None

	def send(self, phone_e164: str, body: str, settings: Any, *, http_post: Any = None) -> dict[str, Any]:
		queued_err = _pop_mock_sms_next_error()
		if queued_err is not None:
			err_code = str(queued_err.get("error_code") or "MOCK_ERROR")
			err_msg = str(queued_err.get("error_message") or f"Mock SMS error {err_code}")
			is_opt_out = err_code in TELNYX_OPT_OUT_ERROR_CODES
			if is_opt_out:
				try:
					upsert_sms_opt_out(
						phone_e164,
						is_opted_out=True,
						source="Carrier Opt-Out Error",
						notes=err_msg[:_MAX_ERROR_MESSAGE_LEN],
					)
				except Exception:
					frappe.log_error(
						title="ISM Mock Opt-Out Upsert Failure",
						message=frappe.get_traceback(),
					)
			return {
				"success": False,
				"message_id": None,
				"status": "Failed",
				"error": err_msg,
				"error_code": err_code,
				"opted_out": is_opt_out,
			}

		seq = _next_mock_sequence()
		message_id = f"mock_sms_{seq:06d}"
		sender_id = (getattr(settings, "sms_sender_id", None) or "ISM").strip()
		record = {
			"message_id": message_id,
			"phone_e164": phone_e164,
			"body": body,
			"sender_id": sender_id,
			"sent_at": now_datetime().isoformat(),
		}

		_IN_PROCESS_MOCK_OUTBOX.append(record)
		cache = _cache_or_none()
		if cache is not None:
			try:
				outbox = cache.get_value(_MOCK_OUTBOX_KEY)
				updated = list(outbox) if isinstance(outbox, list) else []
				updated.append(record)
				cache.set_value(_MOCK_OUTBOX_KEY, updated, expires_in_sec=_MOCK_OUTBOX_TTL_SEC)
			except Exception:
				pass

		return {
			"success": True,
			"message_id": message_id,
			"status": "Sent",
			"error": None,
			"error_code": None,
			"opted_out": False,
		}


_PROVIDERS: dict[str, SMSProvider] = {
	"Disabled": DisabledSMSProvider(),
	"Telnyx": TelnyxSMSProvider(),
	"Custom HTTP Webhook": CustomWebhookSMSProvider(),
	"Mock (Deterministic Test)": MockSMSProvider(),
}


def get_sms_provider(provider_name: str | None) -> SMSProvider | None:
	"""Resolve a configured SMS provider adapter by name."""
	key = (provider_name or "Disabled").strip()
	return _PROVIDERS.get(key)


def get_sms_unconfigured_reason(settings: Any = None) -> str | None:
	"""Return a human-readable reason if SMS dispatch is disabled or misconfigured."""
	if settings is None:
		settings = frappe.get_single("ISM Settings")

	provider_name = (getattr(settings, "sms_gateway_provider", None) or "Disabled").strip()
	provider = get_sms_provider(provider_name)
	if provider is None:
		return _("Unsupported SMS provider: {0}").format(provider_name)
	return provider.validate_config(settings)


def _find_customer_for_phone(phone_e164: str) -> str | None:
	"""Best-effort indexed lookup matching a Customer by exact or common NA phone formats."""
	candidates = [phone_e164]
	if phone_e164.startswith("+1") and len(phone_e164) == 12:
		digits = phone_e164[2:]
		candidates.extend(
			[
				digits,
				f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
				f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
			]
		)
	try:
		rows = frappe.get_all(
			"Customer",
			filters={"mobile_no": ["in", candidates]},
			pluck="name",
			limit_page_length=1,
		)
		return rows[0] if rows else None
	except Exception:
		return None


def is_phone_opted_out(phone_e164: str, *, default_country_code: str = "+1") -> bool:
	"""Return True if the normalized E.164 phone number is currently opted out."""
	if not isinstance(phone_e164, str) or not phone_e164.strip():
		return False
	try:
		normalized = normalize_phone_e164(phone_e164, default_country_code=default_country_code)
	except ValueError:
		return False
	return cint(frappe.db.get_value("SMS Opt Out", normalized, "is_opted_out") or 0) == 1


def upsert_sms_opt_out(
	phone_e164: str,
	*,
	is_opted_out: bool,
	source: str,
	last_keyword: str | None = None,
	event_id: str | None = None,
	notes: str | None = None,
	customer: str | None = None,
	default_country_code: str = "+1",
) -> str:
	"""Idempotently upsert an SMS Opt Out record under a row lock."""
	normalized = normalize_phone_e164(phone_e164, default_country_code=default_country_code)
	resolved_customer = customer or _find_customer_for_phone(normalized)
	now = now_datetime()

	locked = frappe.db.sql(
		"""
		SELECT name
		FROM `tabSMS Opt Out`
		WHERE name = %s
		FOR UPDATE
		""",
		(normalized,),
		as_dict=True,
	)

	if locked:
		doc = frappe.get_doc("SMS Opt Out", normalized)
		doc.is_opted_out = 1 if is_opted_out else 0
		doc.source = source
		if last_keyword is not None:
			doc.last_keyword = last_keyword
		if event_id is not None:
			doc.last_event_id = event_id
		if notes is not None:
			doc.notes = notes[:_MAX_ERROR_MESSAGE_LEN]
		if is_opted_out:
			doc.opted_out_at = now
		else:
			doc.opted_in_at = now
		if resolved_customer and not doc.customer:
			doc.customer = resolved_customer
		doc.save(ignore_permissions=True)
		return doc.name

	doc_fields: dict[str, Any] = {
		"doctype": "SMS Opt Out",
		"phone_e164": normalized,
		"is_opted_out": 1 if is_opted_out else 0,
		"source": source,
		"last_keyword": last_keyword,
		"last_event_id": event_id,
		"customer": resolved_customer,
		"notes": notes[:_MAX_ERROR_MESSAGE_LEN] if notes else None,
	}
	if is_opted_out:
		doc_fields["opted_out_at"] = now
	else:
		doc_fields["opted_in_at"] = now

	doc = frappe.get_doc(doc_fields).insert(ignore_permissions=True)
	return doc.name


def _write_sms_audit_log(
	*,
	provider_name: str,
	request: SMSDispatchRequest,
	phone_e164: str,
	sender_id: str | None,
	status: str,
	message_id: str | None = None,
	error_code: str | None = None,
	error_message: str | None = None,
	sent_at: Any = None,
) -> str | None:
	"""Persist an SMS Audit Log row safely with mandatory OTP redaction."""
	try:
		audit_provider = provider_name if provider_name in SUPPORTED_SMS_PROVIDERS else "Disabled"
		msg_type = request.message_type if request.message_type in _VALID_AUDIT_MESSAGE_TYPES else "Other"
		audit_status = status if status in _VALID_AUDIT_STATUSES else "Failed"

		should_redact = bool(request.redact_body or msg_type == "OTP")
		if should_redact:
			preview = redact_otp_body(request.body, preview_override=request.redacted_preview)
		else:
			preview = (request.redacted_preview or request.body or "").strip()
		if len(preview) > _MAX_MESSAGE_PREVIEW_LEN:
			preview = preview[:_MAX_MESSAGE_PREVIEW_LEN]

		doc_data: dict[str, Any] = {
			"doctype": "SMS Audit Log",
			"direction": "Outbound",
			"provider": audit_provider,
			"message_type": msg_type,
			"phone_e164": phone_e164,
			"sender_id": (sender_id or "").strip() or None,
			"provider_message_id": message_id,
			"status": audit_status,
			"is_redacted": 1 if should_redact else 0,
			"message_preview": preview,
			"reference_doctype": request.reference_doctype,
			"reference_name": request.reference_name,
			"error_code": error_code,
			"error_message": error_message[:_MAX_ERROR_MESSAGE_LEN] if error_message else None,
			"sent_at": sent_at,
		}
		audit_doc = frappe.get_doc(doc_data).insert(ignore_permissions=True)
		return audit_doc.name
	except Exception:
		frappe.log_error(
			title="ISM SMS Audit Log Persistence Failure",
			message=frappe.get_traceback(),
		)
		return None


def send_sms(
	request: SMSDispatchRequest,
	*,
	settings: Any = None,
	http_post: Any = None,
) -> SMSDispatchResult:
	"""Route an outbound SMS through normalization, opt-out compliance, provider transport, and redacted audit logging."""
	target_phone = request.to_phone.strip() if isinstance(request.to_phone, str) else ""
	body_text = request.body.strip() if isinstance(request.body, str) else ""

	if settings is None:
		settings = frappe.get_single("ISM Settings")

	provider_name = (getattr(settings, "sms_gateway_provider", None) or "Disabled").strip()
	sender_id = (getattr(settings, "sms_sender_id", None) or "ISM").strip()
	default_cc = (getattr(settings, "sms_default_country_code", None) or "+1").strip() or "+1"

	if not target_phone or not body_text:
		return SMSDispatchResult(
			success=False,
			provider=provider_name,
			status="Failed",
			error=_("Phone number and message are required."),
			error_code="INVALID_INPUT",
		)

	try:
		phone_e164 = normalize_phone_e164(target_phone, default_country_code=default_cc)
	except ValueError as exc:
		if provider_name in ("Telnyx", "Mock (Deterministic Test)"):
			return SMSDispatchResult(
				success=False,
				provider=provider_name,
				status="Failed",
				error=str(exc),
				error_code="INVALID_PHONE",
			)
		phone_e164 = target_phone

	unconfigured_reason = get_sms_unconfigured_reason(settings)
	if unconfigured_reason:
		audit_name = _write_sms_audit_log(
			provider_name=provider_name,
			request=request,
			phone_e164=phone_e164,
			sender_id=sender_id,
			status="Blocked - Unconfigured",
			error_code="UNCONFIGURED",
			error_message=unconfigured_reason,
		)
		return SMSDispatchResult(
			success=False,
			provider=provider_name,
			status="Blocked - Unconfigured",
			audit_log=audit_name,
			phone_e164=phone_e164,
			error=unconfigured_reason,
			error_code="UNCONFIGURED",
		)

	enforce_opt_out = bool(cint(getattr(settings, "sms_enforce_opt_out", 1)))
	if request.respect_opt_out and enforce_opt_out and is_phone_opted_out(
		phone_e164, default_country_code=default_cc
	):
		opt_out_error = _("SMS delivery is opted out for this number. Text START to re-enable SMS alerts.")
		audit_name = _write_sms_audit_log(
			provider_name=provider_name,
			request=request,
			phone_e164=phone_e164,
			sender_id=sender_id,
			status="Blocked - Opted Out",
			error_code="OPTED_OUT",
			error_message=opt_out_error,
		)
		return SMSDispatchResult(
			success=False,
			provider=provider_name,
			status="Blocked - Opted Out",
			audit_log=audit_name,
			phone_e164=phone_e164,
			error=opt_out_error,
			error_code="OPTED_OUT",
			opted_out=True,
		)

	provider = get_sms_provider(provider_name)
	if provider is None:
		err = _("Unsupported SMS provider: {0}").format(provider_name)
		return SMSDispatchResult(
			success=False,
			provider=provider_name,
			status="Blocked - Unconfigured",
			phone_e164=phone_e164,
			error=err,
			error_code="UNCONFIGURED",
		)

	dispatch_res = provider.send(phone_e164, body_text, settings, http_post=http_post)
	success = bool(dispatch_res.get("success"))
	res_status = str(dispatch_res.get("status") or ("Sent" if success else "Failed"))
	message_id = dispatch_res.get("message_id")
	error_msg = dispatch_res.get("error")
	error_code = dispatch_res.get("error_code")
	opted_out = bool(dispatch_res.get("opted_out", False))

	audit_name = _write_sms_audit_log(
		provider_name=provider_name,
		request=request,
		phone_e164=phone_e164,
		sender_id=sender_id,
		status=res_status,
		message_id=message_id,
		error_code=error_code,
		error_message=error_msg,
		sent_at=now_datetime() if success else None,
	)

	return SMSDispatchResult(
		success=success,
		provider=provider_name,
		status=res_status,
		message_id=message_id,
		audit_log=audit_name,
		phone_e164=phone_e164,
		error=error_msg,
		error_code=error_code,
		opted_out=opted_out,
	)
