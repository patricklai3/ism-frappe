# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import requests

from ism.comms.contracts import (
	_BLOCKED_SMS_HOSTS,
	SUPPORTED_SMS_PROVIDERS,
	SMSDispatchRequest,
	is_valid_sms_webhook_url,
)
from ism.comms.providers import (
	get_sms_unconfigured_reason as _get_sms_unconfigured_reason,
	send_sms,
)

__all__ = [
	"SUPPORTED_SMS_PROVIDERS",
	"_BLOCKED_SMS_HOSTS",
	"_is_valid_sms_webhook_url",
	"dispatch_sms",
	"get_sms_unconfigured_reason",
	"requests",
]


def _is_valid_sms_webhook_url(endpoint: str) -> bool:
	"""Allow HTTPS webhook URLs, permitting plain HTTP only on loopback hosts for local dev/test."""
	return is_valid_sms_webhook_url(endpoint)


def dispatch_sms(
	phone_number: str,
	message: str,
	*,
	message_type: str = "Notification",
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	redact_body: bool = False,
	redacted_preview: str | None = None,
	respect_opt_out: bool = True,
) -> dict:
	"""Dispatch an SMS message using the provider configured in ISM Settings.

	Preserves 2-argument positional compatibility and legacy response keys
	({"success", "message_id", "error"}) while adding structured provider/audit fields.
	"""
	request = SMSDispatchRequest(
		to_phone=phone_number,
		body=message,
		message_type=message_type,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		redact_body=redact_body,
		redacted_preview=redacted_preview,
		respect_opt_out=respect_opt_out,
	)
	result = send_sms(request, http_post=requests.post)
	return {
		"success": result.success,
		"message_id": result.message_id,
		"error": result.error,
		"provider": result.provider,
		"status": result.status,
		"audit_log": result.audit_log,
		"opted_out": result.opted_out,
		"error_code": result.error_code,
	}


def get_sms_unconfigured_reason(settings=None) -> str | None:
	"""Return a human-readable reason if SMS dispatch is disabled or misconfigured."""
	return _get_sms_unconfigured_reason(settings)
