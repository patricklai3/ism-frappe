# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from ism.booking.contracts import (
	BOOKING_IDEMPOTENCY_TTL_SECONDS,
	BOOKING_SOURCE,
	ERR_BOT_DETECTED,
	ERR_DUPLICATE_BOOKING,
	ERR_FORM_EXPIRED,
	ERR_PHONE_DAILY_LIMIT,
	ERR_RATE_LIMITED,
	ERR_SUBMISSION_TOO_FAST,
	MAX_FORM_NONCE_AGE_SECONDS,
	MIN_FORM_FILL_SECONDS,
	BookingAntiAbuseResult,
	BookingConfigSnapshot,
	get_booking_settings,
)
from ism.comms.sessions import get_portal_session_signing_secret

_NONCE_VERSION: str = "v1"
_NONCE_DOMAIN_SEPARATOR: bytes = b":booking-nonce-v1"
_BOOKING_CACHE_PREFIX: str = "ism:booking"
_IP_RL_PREFIX: str = "ism:booking:rl:ip"
_PHONE_DAY_RL_PREFIX: str = "ism:booking:rl:phone_day"
_IDEM_PREFIX: str = "ism:booking:idem"
_PHONE_DAY_WINDOW_SECONDS: int = 86400

__all__ = [
	"check_phone_daily_limit_and_duplicate",
	"clear_booking_rate_limits",
	"enforce_booking_ip_rate_limit",
	"get_idempotent_booking_result",
	"issue_booking_form_nonce",
	"record_phone_daily_booking",
	"store_idempotent_booking_result",
	"validate_honeypot_and_nonce",
	"verify_booking_form_nonce",
]


def _cache_or_none() -> Any:
	try:
		return frappe.cache()
	except Exception:
		return None


def _get_nonce_signing_key() -> bytes:
	secret = get_portal_session_signing_secret(create_if_missing=True)
	if not secret:
		raise RuntimeError("Portal session signing secret is unavailable.")
	return secret + _NONCE_DOMAIN_SEPARATOR


def _sign_nonce_payload(payload: str, key: bytes) -> str:
	digest = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest()
	return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _hash_phone_24(phone_e164: str) -> str:
	return hashlib.sha256(phone_e164.encode("utf-8")).hexdigest()[:24]


def _idempotency_cache_key(idempotency_key: str, phone_e164: str, challenge_id: str) -> str:
	raw = f"{idempotency_key}:{phone_e164}:{challenge_id}"
	digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
	return f"{_IDEM_PREFIX}:{digest}"


def _read_redis_int_counter(cache: Any, cache_key: str) -> int:
	if cache is None:
		return 0
	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key
	if hasattr(cache, "incr") and hasattr(cache, "get"):
		try:
			raw = cache.get(full_key)
			if raw is None:
				return 0
			if isinstance(raw, bytes):
				return cint(raw.decode("utf-8", errors="ignore"))
			return cint(raw)
		except Exception:
			return 0
	try:
		return cint(cache.get_value(cache_key) or 0)
	except Exception:
		return 0


def issue_booking_form_nonce(*, now_ts: int | None = None) -> str:
	"""Issue an HMAC-SHA256-signed form-fill timing nonce (v1.<issued_ts>.<rand>.<sig>)."""
	ts = int(now_ts if now_ts is not None else time.time())
	payload = f"{_NONCE_VERSION}.{ts}.{secrets.token_urlsafe(12)}"
	sig = _sign_nonce_payload(payload, _get_nonce_signing_key())
	return f"{payload}.{sig}"


def verify_booking_form_nonce(
	nonce: str | None,
	*,
	now_ts: int | None = None,
	min_elapsed_seconds: int = MIN_FORM_FILL_SECONDS,
) -> BookingAntiAbuseResult:
	"""Verify nonce format, HMAC signature, minimum fill time, and max age."""
	if not isinstance(nonce, str) or not nonce.strip():
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_BOT_DETECTED,
			error="Unable to process request.",
			http_status=400,
		)

	parts = nonce.strip().split(".")
	if len(parts) != 4:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_BOT_DETECTED,
			error="Unable to process request.",
			http_status=400,
		)

	version, ts_str, rand_part, sig_part = parts
	if version != _NONCE_VERSION or not rand_part or not sig_part:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_BOT_DETECTED,
			error="Unable to process request.",
			http_status=400,
		)

	try:
		issued_ts = int(ts_str)
	except ValueError:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_BOT_DETECTED,
			error="Unable to process request.",
			http_status=400,
		)

	payload = f"{version}.{ts_str}.{rand_part}"
	expected_sig = _sign_nonce_payload(payload, _get_nonce_signing_key())
	if not hmac.compare_digest(expected_sig, sig_part):
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_BOT_DETECTED,
			error="Unable to process request.",
			http_status=400,
		)

	current_ts = int(now_ts if now_ts is not None else time.time())
	elapsed = current_ts - issued_ts
	if elapsed < min_elapsed_seconds:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_SUBMISSION_TOO_FAST,
			error="Please take a moment to review your details and try again.",
			http_status=400,
		)

	if elapsed > MAX_FORM_NONCE_AGE_SECONDS:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_FORM_EXPIRED,
			error="Your booking session expired. Please refresh the page and try again.",
			http_status=400,
		)

	return BookingAntiAbuseResult(allowed=True)


def validate_honeypot_and_nonce(
	*,
	honeypot_value: str | None = None,
	form_nonce: str | None = None,
	settings: BookingConfigSnapshot | None = None,
	now_ts: int | None = None,
) -> BookingAntiAbuseResult:
	"""Enforce decoy honeypot trap and signed timing nonce when honeypot protection is enabled."""
	cfg = settings if settings is not None else get_booking_settings()
	if not cfg.honeypot_enabled:
		return BookingAntiAbuseResult(allowed=True)

	if honeypot_value is not None and str(honeypot_value).strip():
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_BOT_DETECTED,
			error="Unable to process request.",
			http_status=400,
		)

	return verify_booking_form_nonce(form_nonce, now_ts=now_ts)


def enforce_booking_ip_rate_limit(
	bucket: str,
	*,
	client_ip: str,
	limit: int,
	window_seconds: int = 3600,
) -> BookingAntiAbuseResult:
	"""Atomically enforce a per-IP Redis rate limit for the given booking bucket."""
	cache = _cache_or_none()
	if cache is None:
		return BookingAntiAbuseResult(allowed=True)

	bucket_clean = (bucket or "default").strip()
	ip_clean = (client_ip or "unknown").strip()
	cache_key = f"{_IP_RL_PREFIX}:{bucket_clean}:{ip_clean}"
	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key

	if hasattr(cache, "incr"):
		current = cint(cache.incr(full_key))
		if current == 1 and hasattr(cache, "expire"):
			cache.expire(full_key, window_seconds)
		ttl = cint(cache.ttl(full_key)) if hasattr(cache, "ttl") else window_seconds
		if ttl <= 0:
			if ttl == -1 and hasattr(cache, "expire"):
				cache.expire(full_key, window_seconds)
			ttl = window_seconds
	else:
		current = cint(cache.get_value(cache_key) or 0) + 1
		cache.set_value(cache_key, current, expires_in_sec=window_seconds)
		ttl = window_seconds

	if current > limit:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_RATE_LIMITED,
			error="Too many booking requests from this network. Please wait and try again.",
			http_status=429,
			retry_after_seconds=max(1, ttl),
		)

	return BookingAntiAbuseResult(allowed=True)


def check_phone_daily_limit_and_duplicate(
	*,
	phone_e164: str,
	target_date: str,
	internal_start_time: str | None = None,
	settings: BookingConfigSnapshot | None = None,
	now_dt: datetime | None = None,
) -> BookingAntiAbuseResult:
	"""Enforce rolling 24h per-phone booking cap (MariaDB + Redis) and same-day active duplicate guard."""
	del internal_start_time  # Any active Scheduled/Confirmed booking on the same target_date is blocked.
	cfg = settings if settings is not None else get_booking_settings()
	current_dt = now_dt if now_dt is not None else now_datetime()
	cutoff_dt = current_dt - timedelta(hours=24)

	db_count = cint(
		frappe.db.count(
			"Schedule Entry",
			filters={
				"booking_source": BOOKING_SOURCE,
				"provisional_phone": phone_e164,
				"status": ["!=", "Cancelled"],
				"creation": [">=", cutoff_dt],
			},
		)
	)

	cache = _cache_or_none()
	phone_key = f"{_PHONE_DAY_RL_PREFIX}:{_hash_phone_24(phone_e164)}"
	redis_count = _read_redis_int_counter(cache, phone_key)

	if max(db_count, redis_count) >= cfg.phone_rate_limit_per_day:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_PHONE_DAILY_LIMIT,
			error=(
				"You have reached the daily online booking limit for this phone number. "
				"Please call the shop or manage your appointments in the Customer Portal."
			),
			http_status=429,
		)

	existing_active = frappe.get_all(
		"Schedule Entry",
		filters={
			"provisional_phone": phone_e164,
			"scheduled_date": target_date,
			"status": ["in", ["Scheduled", "Confirmed"]],
		},
		fields=["name"],
		limit=1,
	)
	if existing_active:
		return BookingAntiAbuseResult(
			allowed=False,
			error_code=ERR_DUPLICATE_BOOKING,
			error=(
				"You already have an active appointment scheduled on this date. "
				"Please visit the Customer Portal to view or reschedule it."
			),
			http_status=409,
		)

	return BookingAntiAbuseResult(allowed=True)


def record_phone_daily_booking(phone_e164: str) -> None:
	"""Atomically increment the rolling 24h per-phone booking Redis counter after commit."""
	cache = _cache_or_none()
	if cache is None:
		return

	cache_key = f"{_PHONE_DAY_RL_PREFIX}:{_hash_phone_24(phone_e164)}"
	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key
	if hasattr(cache, "incr"):
		current = cint(cache.incr(full_key))
		if current == 1 and hasattr(cache, "expire"):
			cache.expire(full_key, _PHONE_DAY_WINDOW_SECONDS)
		elif hasattr(cache, "ttl") and cint(cache.ttl(full_key)) == -1 and hasattr(cache, "expire"):
			cache.expire(full_key, _PHONE_DAY_WINDOW_SECONDS)
	else:
		current = cint(cache.get_value(cache_key) or 0) + 1
		cache.set_value(cache_key, current, expires_in_sec=_PHONE_DAY_WINDOW_SECONDS)


def get_idempotent_booking_result(
	idempotency_key: str | None,
	*,
	phone_e164: str,
	challenge_id: str,
) -> dict[str, Any] | None:
	"""Return cached booking confirmation payload for a repeated mobile submission, if present."""
	if not isinstance(idempotency_key, str) or not idempotency_key.strip():
		return None
	cache = _cache_or_none()
	if cache is None:
		return None

	cache_key = _idempotency_cache_key(idempotency_key.strip(), phone_e164, challenge_id)
	try:
		raw = cache.get_value(cache_key)
	except Exception:
		return None
	if raw is None:
		return None
	if isinstance(raw, dict):
		return raw
	if isinstance(raw, (str, bytes)):
		try:
			parsed = json.loads(raw)
		except (ValueError, TypeError):
			return None
		if isinstance(parsed, dict):
			return parsed
	return None


def store_idempotent_booking_result(
	idempotency_key: str | None,
	*,
	phone_e164: str,
	challenge_id: str,
	payload: dict[str, Any],
) -> None:
	"""Cache a committed booking confirmation payload for 10 minutes to deduplicate double-taps."""
	if not isinstance(idempotency_key, str) or not idempotency_key.strip():
		return
	cache = _cache_or_none()
	if cache is None:
		return

	cache_key = _idempotency_cache_key(idempotency_key.strip(), phone_e164, challenge_id)
	try:
		cache.set_value(
			cache_key,
			json.dumps(payload),
			expires_in_sec=BOOKING_IDEMPOTENCY_TTL_SECONDS,
		)
	except Exception:
		pass


def clear_booking_rate_limits() -> None:
	"""Delete all ism:booking:* Redis keys cleanly for hermetic test isolation."""
	cache = _cache_or_none()
	if cache is None:
		return
	try:
		if hasattr(cache, "delete_keys"):
			cache.delete_keys(f"{_BOOKING_CACHE_PREFIX}:*")
	except Exception:
		pass
