"""Rate-limited transactional email dispatch wrapper for Project 1 (Comms)."""

from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, validate_email_address

from ism.comms.contracts import EmailDispatchResult, get_client_ip

_EMAIL_RATE_LIMIT_PREFIX = "ism:comms:email_rl"
_EMAIL_RATE_LIMIT_WINDOW_SEC = 3600
_DEFAULT_RECIPIENT_LIMIT_PER_HOUR = 5
_DEFAULT_IP_LIMIT_PER_HOUR = 20


def _get_email_rate_limits() -> tuple[int, int]:
	"""Read per-recipient and per-IP hourly email limits from ISM Settings."""
	try:
		settings = frappe.get_single("ISM Settings")
	except Exception:
		settings = None

	rcpt_limit = cint(getattr(settings, "transactional_email_rate_limit_per_hour", None) or 0)
	ip_limit = cint(getattr(settings, "transactional_email_ip_rate_limit_per_hour", None) or 0)

	return (
		rcpt_limit if rcpt_limit > 0 else _DEFAULT_RECIPIENT_LIMIT_PER_HOUR,
		ip_limit if ip_limit > 0 else _DEFAULT_IP_LIMIT_PER_HOUR,
	)


def _check_and_increment_rate_limit(
	cache_key: str,
	limit: int,
	window_sec: int = _EMAIL_RATE_LIMIT_WINDOW_SEC,
) -> tuple[bool, int]:
	"""Atomically increment a Redis hourly counter; return (allowed, retry_after_seconds)."""
	cache = frappe.cache()
	redis_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key

	if hasattr(cache, "incr"):
		current = cint(cache.incr(redis_key))
		if current == 1 and hasattr(cache, "expire"):
			cache.expire(redis_key, window_sec)
		ttl = cint(cache.ttl(redis_key)) if hasattr(cache, "ttl") else window_sec
		if ttl <= 0:
			if ttl == -1 and hasattr(cache, "expire"):
				cache.expire(redis_key, window_sec)
			ttl = window_sec
	else:
		current = cint(cache.get_value(cache_key) or 0) + 1
		cache.set_value(cache_key, current, expires_in_sec=window_sec)
		ttl = window_sec

	if current > limit:
		return False, ttl
	return True, 0


def clear_email_rate_limits() -> None:
	"""Clear transactional email rate-limit keys from cache (for hermetic test runs)."""
	cache = frappe.cache()
	if hasattr(cache, "delete_keys"):
		cache.delete_keys(f"{_EMAIL_RATE_LIMIT_PREFIX}:*")


def dispatch_transactional_email(
	*,
	recipient: str,
	subject: str,
	html_body: str,
	email_type: str = "transactional",
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	attachments: list[dict[str, Any]] | None = None,
	client_ip: str | None = None,
	bypass_rate_limit: bool = False,
	delayed: bool = False,
) -> EmailDispatchResult:
	"""Validate, rate-limit, and dispatch a transactional email via frappe.sendmail."""
	clean_email_type = (email_type or "transactional").strip() or "transactional"

	if not isinstance(recipient, str) or not recipient.strip():
		return EmailDispatchResult(
			success=False,
			recipient="",
			email_type=clean_email_type,
			error=_("Recipient email address is required."),
			error_code="INVALID_RECIPIENT",
		)

	if "\r" in recipient or "\n" in recipient:
		return EmailDispatchResult(
			success=False,
			recipient="",
			email_type=clean_email_type,
			error=_("Recipient email address contains invalid header characters."),
			error_code="HEADER_INJECTION",
		)

	raw_recipient = recipient.strip()
	if "," in raw_recipient or ";" in raw_recipient:
		return EmailDispatchResult(
			success=False,
			recipient=raw_recipient,
			email_type=clean_email_type,
			error=_("Invalid recipient email address."),
			error_code="INVALID_RECIPIENT",
		)

	validated_recipient = validate_email_address(raw_recipient, throw=False)
	if not validated_recipient:
		return EmailDispatchResult(
			success=False,
			recipient=raw_recipient,
			email_type=clean_email_type,
			error=_("Invalid recipient email address."),
			error_code="INVALID_RECIPIENT",
		)
	clean_recipient = validated_recipient.strip()

	if not isinstance(subject, str) or not subject.strip():
		return EmailDispatchResult(
			success=False,
			recipient=clean_recipient,
			email_type=clean_email_type,
			error=_("Email subject is required."),
			error_code="INVALID_SUBJECT",
		)

	if "\r" in subject or "\n" in subject:
		return EmailDispatchResult(
			success=False,
			recipient=clean_recipient,
			email_type=clean_email_type,
			error=_("Email subject contains invalid header characters."),
			error_code="HEADER_INJECTION",
		)
	clean_subject = subject.strip()

	if not isinstance(html_body, str) or not html_body.strip():
		return EmailDispatchResult(
			success=False,
			recipient=clean_recipient,
			email_type=clean_email_type,
			error=_("Email body is required."),
			error_code="INVALID_BODY",
		)

	if not bypass_rate_limit:
		rcpt_limit, ip_limit = _get_email_rate_limits()
		rcpt_hash = hashlib.sha256(clean_recipient.lower().encode("utf-8")).hexdigest()[:24]
		rcpt_key = f"{_EMAIL_RATE_LIMIT_PREFIX}:rcpt:{rcpt_hash}"
		allowed, retry_after = _check_and_increment_rate_limit(rcpt_key, rcpt_limit)
		if not allowed:
			return EmailDispatchResult(
				success=False,
				recipient=clean_recipient,
				email_type=clean_email_type,
				rate_limited=True,
				retry_after_seconds=retry_after,
				error=_("Email rate limit exceeded for this recipient. Please try again later."),
				error_code="RATE_LIMITED",
			)

		resolved_ip = get_client_ip(client_ip)
		ip_key = f"{_EMAIL_RATE_LIMIT_PREFIX}:ip:{resolved_ip}"
		allowed, retry_after = _check_and_increment_rate_limit(ip_key, ip_limit)
		if not allowed:
			return EmailDispatchResult(
				success=False,
				recipient=clean_recipient,
				email_type=clean_email_type,
				rate_limited=True,
				retry_after_seconds=retry_after,
				error=_("Email rate limit exceeded for this IP address. Please try again later."),
				error_code="RATE_LIMITED",
			)

	try:
		frappe.sendmail(
			recipients=[clean_recipient],
			subject=clean_subject,
			message=html_body,
			attachments=attachments,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			now=not delayed,
		)
	except Exception as exc:
		frappe.log_error(
			title=f"Transactional email dispatch failed ({clean_email_type})",
			message=frappe.get_traceback(),
		)
		return EmailDispatchResult(
			success=False,
			recipient=clean_recipient,
			email_type=clean_email_type,
			error=str(exc) or _("Failed to send transactional email."),
			error_code="SMTP_ERROR",
		)

	return EmailDispatchResult(
		success=True,
		recipient=clean_recipient,
		email_type=clean_email_type,
	)
