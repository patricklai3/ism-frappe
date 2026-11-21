# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""SMS-only OTP challenge generation, cryptographic verification, and lockout engine."""

import hashlib
import hmac
import secrets
import time
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from ism.comms.contracts import (
	OTP_PURPOSES,
	OTPChallengeResult,
	OTPVerifyResult,
	SMSDispatchRequest,
	get_client_ip,
	mask_phone_e164,
	normalize_phone_e164,
)
from ism.comms.providers import is_phone_opted_out, send_sms
from ism.comms.sessions import get_portal_session_signing_secret

__all__ = [
	"OTP_CONSUME_GRACE_SECONDS",
	"OTP_RETENTION_DAYS",
	"check_otp_lockout",
	"cleanup_expired_otp_challenges",
	"clear_comms_rate_limits",
	"consume_verified_otp_challenge",
	"get_last_mock_otp",
	"request_sms_otp",
	"verify_sms_otp",
]

OTP_CONSUME_GRACE_SECONDS = 900
OTP_RETENTION_DAYS = 7

_MOCK_PROVIDER_NAME = "Mock (Deterministic Test)"
_OTP_KEY_PREFIX = "ism:comms:otp"
_OTP_COOLDOWN_PREFIX = "ism:comms:otp:cooldown"
_OTP_PHONE_RL_PREFIX = "ism:comms:otp:rl:phone"
_OTP_IP_RL_PREFIX = "ism:comms:otp:rl:ip"
_OTP_LOCK_PREFIX = "ism:comms:otp:lock"
_MOCK_LAST_OTP_PREFIX = "ism:comms:mock_last_otp"
_EMAIL_RL_PREFIX = "ism:comms:email_rl"
_WEBHOOK_RL_PREFIX = "ism:comms:webhook_rl"

_OTP_HOURLY_WINDOW_SEC = 3600
_MOCK_LAST_OTP_TTL_SEC = 300

_DEFAULT_CODE_LENGTH = 6
_DEFAULT_TTL_SECONDS = 300
_DEFAULT_RESEND_COOLDOWN_SECONDS = 60
_DEFAULT_MAX_VERIFY_ATTEMPTS = 5
_DEFAULT_LOCKOUT_SECONDS = 900
_DEFAULT_PHONE_RATE_LIMIT_PER_HOUR = 5
_DEFAULT_IP_RATE_LIMIT_PER_HOUR = 20

_IN_PROCESS_MOCK_LAST_OTP: dict[str, tuple[str, float]] = {}


def _get_settings(settings: Any = None) -> Any:
	return settings if settings is not None else frappe.get_single("ISM Settings")


def _cache_or_none() -> Any:
	try:
		return frappe.cache()
	except Exception:
		return None


def _hash_phone(phone_e164: str) -> str:
	return hashlib.sha256(phone_e164.encode("utf-8")).hexdigest()[:24]


def _get_default_country_code(settings: Any) -> str:
	return (getattr(settings, "sms_default_country_code", None) or "+1").strip() or "+1"


def _get_otp_config(settings: Any) -> dict[str, int]:
	code_length = cint(getattr(settings, "otp_code_length", None) or 0)
	if code_length < 4 or code_length > 8:
		code_length = _DEFAULT_CODE_LENGTH

	ttl_seconds = cint(getattr(settings, "otp_ttl_seconds", None) or 0)
	if ttl_seconds <= 0:
		ttl_seconds = _DEFAULT_TTL_SECONDS

	cooldown_seconds = cint(getattr(settings, "otp_resend_cooldown_seconds", None) or 0)
	if cooldown_seconds <= 0:
		cooldown_seconds = _DEFAULT_RESEND_COOLDOWN_SECONDS

	max_attempts = cint(getattr(settings, "otp_max_verify_attempts", None) or 0)
	if max_attempts <= 0:
		max_attempts = _DEFAULT_MAX_VERIFY_ATTEMPTS

	lockout_seconds = cint(getattr(settings, "otp_lockout_seconds", None) or 0)
	if lockout_seconds <= 0:
		lockout_seconds = _DEFAULT_LOCKOUT_SECONDS

	phone_limit = cint(getattr(settings, "otp_phone_rate_limit_per_hour", None) or 0)
	if phone_limit <= 0:
		phone_limit = _DEFAULT_PHONE_RATE_LIMIT_PER_HOUR

	ip_limit = cint(getattr(settings, "otp_ip_rate_limit_per_hour", None) or 0)
	if ip_limit <= 0:
		ip_limit = _DEFAULT_IP_RATE_LIMIT_PER_HOUR

	return {
		"code_length": code_length,
		"ttl_seconds": ttl_seconds,
		"cooldown_seconds": cooldown_seconds,
		"max_attempts": max_attempts,
		"lockout_seconds": lockout_seconds,
		"phone_limit": phone_limit,
		"ip_limit": ip_limit,
	}


def _get_otp_pepper(settings: Any = None) -> bytes:
	secret = get_portal_session_signing_secret(settings=settings, create_if_missing=True)
	if not secret:
		raise ValueError("Unable to resolve signing secret for OTP hashing.")
	return secret + b":otp-v1"


def _compute_otp_hash(challenge_id: str, code_salt: str, code: str, pepper: bytes) -> str:
	payload = f"{challenge_id}:{code_salt}:{code}".encode()
	return hmac.new(pepper, payload, hashlib.sha256).hexdigest()


def _get_request_user_agent(user_agent: str | None = None) -> str | None:
	if isinstance(user_agent, str) and user_agent.strip():
		return user_agent.strip()[:140]
	if getattr(frappe.local, "request", None):
		header_ua = frappe.get_request_header("User-Agent")
		if isinstance(header_ua, str) and header_ua.strip():
			return header_ua.strip()[:140]
	return None


def _get_active_cooldown_ttl(phone_hash: str, default_cooldown_sec: int) -> int:
	"""Return remaining cooldown TTL (> 0) if the phone is currently in resend cooldown, else 0."""
	cache = _cache_or_none()
	if cache is None:
		return 0

	cache_key = f"{_OTP_COOLDOWN_PREFIX}:{phone_hash}"
	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key

	if hasattr(cache, "ttl") and hasattr(cache, "exists"):
		if not cache.exists(full_key):
			return 0
		ttl = cint(cache.ttl(full_key))
		if ttl <= 0:
			if ttl == -1 and hasattr(cache, "expire"):
				cache.expire(full_key, default_cooldown_sec)
			return max(1, default_cooldown_sec)
		return ttl

	return max(1, default_cooldown_sec) if cache.get_value(cache_key) else 0


def _acquire_resend_cooldown(phone_hash: str, cooldown_sec: int) -> tuple[bool, int]:
	"""Atomically acquire a per-phone resend cooldown key via Redis SET NX EX."""
	cache = _cache_or_none()
	if cache is None:
		return True, 0

	cache_key = f"{_OTP_COOLDOWN_PREFIX}:{phone_hash}"
	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key

	if hasattr(cache, "set"):
		acquired = bool(cache.set(full_key, "1", nx=True, ex=cooldown_sec))
		if acquired:
			return True, 0
		ttl = cint(cache.ttl(full_key)) if hasattr(cache, "ttl") else cooldown_sec
		if ttl <= 0:
			if ttl == -1 and hasattr(cache, "expire"):
				cache.expire(full_key, cooldown_sec)
			ttl = cooldown_sec
		return False, max(1, ttl)

	if cache.get_value(cache_key):
		return False, cooldown_sec
	cache.set_value(cache_key, "1", expires_in_sec=cooldown_sec)
	return True, 0


def _check_and_increment_hourly_counter(
	cache_key: str,
	limit: int,
	window_sec: int = _OTP_HOURLY_WINDOW_SEC,
) -> tuple[bool, int]:
	"""Atomically increment an hourly Redis counter; return (allowed, retry_after_seconds)."""
	cache = _cache_or_none()
	if cache is None:
		return True, 0

	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key
	if hasattr(cache, "incr"):
		current = cint(cache.incr(full_key))
		if current == 1 and hasattr(cache, "expire"):
			cache.expire(full_key, window_sec)
		ttl = cint(cache.ttl(full_key)) if hasattr(cache, "ttl") else window_sec
		if ttl <= 0:
			if ttl == -1 and hasattr(cache, "expire"):
				cache.expire(full_key, window_sec)
			ttl = window_sec
	else:
		current = cint(cache.get_value(cache_key) or 0) + 1
		cache.set_value(cache_key, current, expires_in_sec=window_sec)
		ttl = window_sec

	if current > limit:
		return False, max(1, ttl)
	return True, 0


def _decrement_counter_best_effort(cache_key: str) -> None:
	cache = _cache_or_none()
	if cache is None:
		return
	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key
	try:
		if hasattr(cache, "decr"):
			new_val = cint(cache.decr(full_key))
			if new_val <= 0 and hasattr(cache, "delete"):
				cache.delete(full_key)
		else:
			current = max(0, cint(cache.get_value(cache_key) or 0) - 1)
			if current <= 0:
				cache.delete_value(cache_key)
			else:
				cache.set_value(cache_key, current, expires_in_sec=_OTP_HOURLY_WINDOW_SEC)
	except Exception:
		pass


def _set_lockout_cache(phone_e164: str, locked_until: Any, ttl_seconds: int) -> None:
	cache = _cache_or_none()
	if cache is None:
		return
	cache_key = f"{_OTP_LOCK_PREFIX}:{_hash_phone(phone_e164)}"
	try:
		cache.set_value(cache_key, str(locked_until), expires_in_sec=max(1, cint(ttl_seconds)))
	except Exception:
		pass


def _clear_lockout_cache(phone_e164: str) -> None:
	cache = _cache_or_none()
	if cache is None:
		return
	cache_key = f"{_OTP_LOCK_PREFIX}:{_hash_phone(phone_e164)}"
	try:
		cache.delete_value(cache_key)
	except Exception:
		pass


def _record_mock_last_otp(phone_e164: str, code: str, settings: Any) -> None:
	provider = (getattr(settings, "sms_gateway_provider", None) or "Disabled").strip()
	if provider != _MOCK_PROVIDER_NAME:
		return

	_IN_PROCESS_MOCK_LAST_OTP[phone_e164] = (code, time.monotonic() + _MOCK_LAST_OTP_TTL_SEC)
	cache = _cache_or_none()
	if cache is not None:
		try:
			cache.set_value(
				f"{_MOCK_LAST_OTP_PREFIX}:{phone_e164}",
				code,
				expires_in_sec=_MOCK_LAST_OTP_TTL_SEC,
			)
		except Exception:
			pass


def get_last_mock_otp(phone_e164: str) -> str | None:
	"""Return the last generated OTP code strictly when provider is 'Mock (Deterministic Test)'."""
	settings = _get_settings()
	provider = (getattr(settings, "sms_gateway_provider", None) or "Disabled").strip()
	if provider != _MOCK_PROVIDER_NAME:
		return None

	if not isinstance(phone_e164, str) or not phone_e164.strip() or "@" in phone_e164:
		return None

	default_cc = _get_default_country_code(settings)
	try:
		normalized = normalize_phone_e164(phone_e164, default_country_code=default_cc)
	except ValueError:
		return None

	cache = _cache_or_none()
	if cache is not None:
		try:
			cached = cache.get_value(f"{_MOCK_LAST_OTP_PREFIX}:{normalized}")
			if isinstance(cached, str) and cached.strip():
				return cached.strip()
			return None
		except Exception:
			pass

	entry = _IN_PROCESS_MOCK_LAST_OTP.get(normalized)
	if entry is None:
		return None
	code, expires_monotonic = entry
	if time.monotonic() > expires_monotonic:
		_IN_PROCESS_MOCK_LAST_OTP.pop(normalized, None)
		return None
	return code


def check_otp_lockout(phone_e164: str) -> dict[str, Any]:
	"""Check whether a phone number is currently locked out (DB locked_until is authoritative)."""
	if not isinstance(phone_e164, str) or not phone_e164.strip() or "@" in phone_e164:
		raise ValueError("SMS OTP requires a valid phone number; email addresses are not supported.")

	settings = _get_settings()
	default_cc = _get_default_country_code(settings)
	normalized = normalize_phone_e164(phone_e164, default_country_code=default_cc)
	now = now_datetime()

	rows = frappe.db.sql(
		"""
		SELECT locked_until
		FROM `tabPortal OTP Challenge`
		WHERE phone_e164 = %s
			AND status = 'Locked'
			AND locked_until IS NOT NULL
			AND locked_until > %s
		ORDER BY locked_until DESC
		LIMIT 1
		""",
		(normalized, now),
	)
	if rows and rows[0] and rows[0][0]:
		locked_until_dt = get_datetime(rows[0][0])
		remaining = max(1, int((locked_until_dt - now).total_seconds()))
		_set_lockout_cache(normalized, locked_until_dt, remaining)
		return {
			"locked": True,
			"phone_e164": normalized,
			"locked_until": str(locked_until_dt),
			"retry_after_seconds": remaining,
		}

	_clear_lockout_cache(normalized)
	return {
		"locked": False,
		"phone_e164": normalized,
		"locked_until": None,
		"retry_after_seconds": 0,
	}


def request_sms_otp(
	phone: str,
	*,
	purpose: str = "portal_login",
	client_ip: str | None = None,
	user_agent: str | None = None,
	context_ref_doctype: str | None = None,
	context_ref_name: str | None = None,
) -> OTPChallengeResult:
	"""Create and dispatch an SMS-only OTP challenge with strict throttling and redacted audit logging."""
	if purpose not in OTP_PURPOSES:
		raise ValueError(f"Unsupported OTP purpose: {purpose!r}")

	if not isinstance(phone, str) or not phone.strip():
		raise ValueError("Phone number is required for SMS OTP.")
	if "@" in phone:
		raise ValueError("SMS OTP requires a phone number; email addresses are not supported.")

	settings = _get_settings()
	default_cc = _get_default_country_code(settings)
	phone_e164 = normalize_phone_e164(phone, default_country_code=default_cc)
	phone_masked = mask_phone_e164(phone_e164)
	phone_hash = _hash_phone(phone_e164)
	resolved_ip = get_client_ip(client_ip)[:140]
	resolved_ua = _get_request_user_agent(user_agent)
	cfg = _get_otp_config(settings)

	# 1. Authoritative lockout check before burning any quota.
	lock_info = check_otp_lockout(phone_e164)
	if lock_info["locked"]:
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=lock_info["retry_after_seconds"],
			locked_until=lock_info["locked_until"],
			error=_("Too many failed verification attempts. Please try again later."),
			error_code="LOCKED_OUT",
		)

	# 2. Opt-out check before burning quotas or creating an unsendable challenge.
	if cint(getattr(settings, "sms_enforce_opt_out", 1)) and is_phone_opted_out(
		phone_e164, default_country_code=default_cc
	):
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			error=_(
				"SMS delivery is opted out for this number. Text START to re-enable verification codes."
			),
			error_code="OPTED_OUT",
		)

	# 3. Atomic resend cooldown + per-IP (checked first to prevent cross-phone DoS) and per-phone hourly quotas.
	existing_cd_ttl = _get_active_cooldown_ttl(phone_hash, cfg["cooldown_seconds"])
	if existing_cd_ttl > 0:
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=existing_cd_ttl,
			error=_("Please wait before requesting another verification code."),
			error_code="COOLDOWN_ACTIVE",
		)

	ip_rl_key = f"{_OTP_IP_RL_PREFIX}:{resolved_ip}"
	ip_ok, ip_ttl = _check_and_increment_hourly_counter(ip_rl_key, cfg["ip_limit"])
	if not ip_ok:
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=ip_ttl,
			error=_("Too many verification code requests from this IP address. Please try again later."),
			error_code="RATE_LIMITED",
		)

	phone_rl_key = f"{_OTP_PHONE_RL_PREFIX}:{phone_hash}"
	phone_ok, phone_ttl = _check_and_increment_hourly_counter(phone_rl_key, cfg["phone_limit"])
	if not phone_ok:
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=phone_ttl,
			error=_("Too many verification code requests for this phone number. Please try again later."),
			error_code="RATE_LIMITED",
		)

	cooldown_ok, cooldown_ttl = _acquire_resend_cooldown(phone_hash, cfg["cooldown_seconds"])
	if not cooldown_ok:
		_decrement_counter_best_effort(phone_rl_key)
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=cooldown_ttl,
			error=_("Please wait before requesting another verification code."),
			error_code="COOLDOWN_ACTIVE",
		)

	now = now_datetime()

	# 4. Mark any prior Pending challenges for (phone_e164, purpose) as Superseded.
	prior_pending = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabPortal OTP Challenge`
			WHERE phone_e164 = %s AND purpose = %s AND status = 'Pending'
			FOR UPDATE
			""",
			(phone_e164, purpose),
		)
	)
	if prior_pending:
		placeholders = ", ".join(["%s"] * len(prior_pending))
		frappe.db.sql(
			f"""
			UPDATE `tabPortal OTP Challenge`
			SET status = 'Superseded', modified = %s
			WHERE name IN ({placeholders})
			""",
			(now, *prior_pending),
		)

	# 5. Generate CSPRNG code, per-challenge salt, and salted HMAC-SHA256 hash.
	code_length = cfg["code_length"]
	code = str(secrets.randbelow(10**code_length)).zfill(code_length)
	challenge_id = secrets.token_urlsafe(24)
	code_salt = secrets.token_hex(16)
	pepper = _get_otp_pepper(settings=settings)
	code_hash = _compute_otp_hash(challenge_id, code_salt, code, pepper)
	expires_at = add_to_date(now, seconds=cfg["ttl_seconds"])

	challenge = frappe.get_doc(
		{
			"doctype": "Portal OTP Challenge",
			"challenge_id": challenge_id,
			"purpose": purpose,
			"phone_e164": phone_e164,
			"code_salt": code_salt,
			"code_hash": code_hash,
			"status": "Pending",
			"attempts": 0,
			"max_attempts": cfg["max_attempts"],
			"expires_at": expires_at,
			"request_ip": resolved_ip,
			"user_agent": resolved_ua,
			"context_ref_doctype": context_ref_doctype or None,
			"context_ref_name": context_ref_name or None,
		}
	)
	challenge.insert(ignore_permissions=True)

	ttl_min = max(1, (cfg["ttl_seconds"] + 59) // 60)
	sms_res = send_sms(
		SMSDispatchRequest(
			to_phone=phone_e164,
			body=f"Your verification code is {code}. It expires in {ttl_min} minutes. Do not share this code.",
			message_type="OTP",
			reference_doctype="Portal OTP Challenge",
			reference_name=challenge.name,
			redact_body=True,
			redacted_preview=f"Your verification code is [REDACTED]. It expires in {ttl_min} minutes.",
		),
		settings=settings,
	)

	if not sms_res.success:
		updates: dict[str, Any] = {"status": "Delivery Failed"}
		if sms_res.audit_log:
			updates["sms_audit_log"] = sms_res.audit_log
		frappe.db.set_value("Portal OTP Challenge", challenge.name, updates, update_modified=True)
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			error=sms_res.error or _("Failed to deliver verification code via SMS."),
			error_code=sms_res.error_code or "DELIVERY_FAILED",
		)

	if sms_res.audit_log:
		frappe.db.set_value(
			"Portal OTP Challenge",
			challenge.name,
			"sms_audit_log",
			sms_res.audit_log,
			update_modified=False,
		)

	_record_mock_last_otp(phone_e164, code, settings)

	return OTPChallengeResult(
		success=True,
		challenge_id=challenge_id,
		phone_masked=phone_masked,
		expires_in_seconds=cfg["ttl_seconds"],
		resend_available_in_seconds=cfg["cooldown_seconds"],
	)


def verify_sms_otp(
	challenge_id: str,
	code: str,
	*,
	phone: str | None = None,
	purpose: str | None = None,
	client_ip: str | None = None,
	consume: bool = True,
) -> OTPVerifyResult:
	"""Verify an SMS OTP code with FOR UPDATE locking, constant-time HMAC comparison, and lockout."""
	del client_ip  # Reserved for future verification telemetry while preserving frozen signature.
	clean_challenge_id = challenge_id.strip() if isinstance(challenge_id, str) else ""
	clean_code = code.strip() if isinstance(code, str) else ""

	if not clean_challenge_id:
		return OTPVerifyResult(
			verified=False,
			error=_("Verification challenge is required."),
			error_code="NOT_FOUND",
		)
	if not clean_code:
		return OTPVerifyResult(
			verified=False,
			challenge_id=clean_challenge_id,
			error=_("Verification code is required."),
			error_code="INVALID_CODE",
		)

	settings = _get_settings()
	default_cc = _get_default_country_code(settings)

	normalized_phone: str | None = None
	if phone is not None:
		if not isinstance(phone, str) or not phone.strip() or "@" in phone:
			return OTPVerifyResult(
				verified=False,
				challenge_id=clean_challenge_id,
				error=_("Phone number does not match this verification challenge."),
				error_code="PHONE_MISMATCH",
			)
		try:
			normalized_phone = normalize_phone_e164(phone, default_country_code=default_cc)
		except ValueError:
			return OTPVerifyResult(
				verified=False,
				challenge_id=clean_challenge_id,
				error=_("Phone number does not match this verification challenge."),
				error_code="PHONE_MISMATCH",
			)

	row_names = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabPortal OTP Challenge`
			WHERE challenge_id = %s
			LIMIT 1
			FOR UPDATE
			""",
			(clean_challenge_id,),
		)
	)
	if not row_names:
		return OTPVerifyResult(
			verified=False,
			challenge_id=clean_challenge_id,
			error=_("Verification challenge not found."),
			error_code="NOT_FOUND",
		)

	doc = frappe.get_doc("Portal OTP Challenge", row_names[0])

	if doc.status != "Pending":
		status_error_map = {
			"Expired": ("EXPIRED", _("Verification code has expired. Please request a new code.")),
			"Locked": ("LOCKED_OUT", _("Too many failed verification attempts. Please try again later.")),
			"Superseded": (
				"SUPERSEDED",
				_("This verification code was superseded by a newer request."),
			),
			"Delivery Failed": (
				"DELIVERY_FAILED",
				_("This verification code could not be delivered."),
			),
			"Verified": (
				"ALREADY_USED" if doc.consumed_at else "ALREADY_VERIFIED",
				_("This verification code has already been used."),
			),
		}
		err_code, err_msg = status_error_map.get(
			doc.status,
			("INVALID_STATUS", _("Verification challenge is no longer valid.")),
		)
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			locked_until=str(doc.locked_until) if doc.status == "Locked" and doc.locked_until else None,
			error=err_msg,
			error_code=err_code,
		)

	if purpose is not None and doc.purpose != purpose:
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Verification purpose does not match."),
			error_code="PURPOSE_MISMATCH",
		)

	if normalized_phone is not None and doc.phone_e164 != normalized_phone:
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Phone number does not match this verification challenge."),
			error_code="PHONE_MISMATCH",
		)

	now = now_datetime()
	if now > get_datetime(doc.expires_at):
		frappe.db.set_value("Portal OTP Challenge", doc.name, "status", "Expired", update_modified=True)
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Verification code has expired. Please request a new code."),
			error_code="EXPIRED",
		)

	lock_info = check_otp_lockout(doc.phone_e164)
	if lock_info["locked"]:
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			locked_until=lock_info["locked_until"],
			error=_("Too many failed verification attempts. Please try again later."),
			error_code="LOCKED_OUT",
		)

	pepper = _get_otp_pepper(settings=settings)
	expected_hash = _compute_otp_hash(doc.challenge_id, doc.code_salt, clean_code, pepper)
	stored_hash = (doc.code_hash or "").strip()
	is_match = bool(stored_hash) and hmac.compare_digest(expected_hash, stored_hash)

	cfg = _get_otp_config(settings)
	max_attempts = cint(doc.max_attempts) or cfg["max_attempts"]

	if not is_match:
		new_attempts = cint(doc.attempts) + 1
		if new_attempts >= max_attempts:
			locked_until = add_to_date(now, seconds=cfg["lockout_seconds"])
			frappe.db.set_value(
				"Portal OTP Challenge",
				doc.name,
				{
					"attempts": new_attempts,
					"status": "Locked",
					"locked_until": locked_until,
				},
				update_modified=True,
			)
			_set_lockout_cache(doc.phone_e164, locked_until, cfg["lockout_seconds"])
			return OTPVerifyResult(
				verified=False,
				challenge_id=doc.challenge_id,
				attempts_remaining=0,
				locked_until=str(locked_until),
				error=_("Too many failed verification attempts. Please try again later."),
				error_code="LOCKED_OUT",
			)

		remaining = max_attempts - new_attempts
		frappe.db.set_value("Portal OTP Challenge", doc.name, "attempts", new_attempts, update_modified=True)
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			attempts_remaining=remaining,
			error=_("Invalid verification code."),
			error_code="INVALID_CODE",
		)

	updates: dict[str, Any] = {
		"status": "Verified",
		"verified_at": now,
	}
	if consume:
		updates["consumed_at"] = now
	frappe.db.set_value("Portal OTP Challenge", doc.name, updates, update_modified=True)

	return OTPVerifyResult(
		verified=True,
		challenge_id=doc.challenge_id,
		phone_e164=doc.phone_e164,
		purpose=doc.purpose,
		attempts_remaining=max(0, max_attempts - cint(doc.attempts)),
	)


def consume_verified_otp_challenge(
	challenge_id: str,
	*,
	expected_purpose: str | None = None,
	expected_phone: str | None = None,
	grace_seconds: int = OTP_CONSUME_GRACE_SECONDS,
) -> OTPVerifyResult:
	"""Atomically consume a previously verified (consume=False) OTP challenge at most once."""
	clean_challenge_id = challenge_id.strip() if isinstance(challenge_id, str) else ""
	if not clean_challenge_id:
		return OTPVerifyResult(
			verified=False,
			error=_("Verification challenge is required."),
			error_code="NOT_FOUND",
		)

	settings = _get_settings()
	default_cc = _get_default_country_code(settings)

	normalized_phone: str | None = None
	if expected_phone is not None:
		if not isinstance(expected_phone, str) or not expected_phone.strip() or "@" in expected_phone:
			return OTPVerifyResult(
				verified=False,
				challenge_id=clean_challenge_id,
				error=_("Phone number does not match this verification challenge."),
				error_code="PHONE_MISMATCH",
			)
		try:
			normalized_phone = normalize_phone_e164(expected_phone, default_country_code=default_cc)
		except ValueError:
			return OTPVerifyResult(
				verified=False,
				challenge_id=clean_challenge_id,
				error=_("Phone number does not match this verification challenge."),
				error_code="PHONE_MISMATCH",
			)

	row_names = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabPortal OTP Challenge`
			WHERE challenge_id = %s
			LIMIT 1
			FOR UPDATE
			""",
			(clean_challenge_id,),
		)
	)
	if not row_names:
		return OTPVerifyResult(
			verified=False,
			challenge_id=clean_challenge_id,
			error=_("Verification challenge not found."),
			error_code="NOT_FOUND",
		)

	doc = frappe.get_doc("Portal OTP Challenge", row_names[0])

	if doc.status != "Verified":
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Verification challenge is not verified."),
			error_code="NOT_VERIFIED",
		)

	if doc.consumed_at:
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Verification challenge has already been consumed."),
			error_code="ALREADY_CONSUMED",
		)

	if expected_purpose is not None and doc.purpose != expected_purpose:
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Verification purpose does not match."),
			error_code="PURPOSE_MISMATCH",
		)

	if normalized_phone is not None and doc.phone_e164 != normalized_phone:
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Phone number does not match this verification challenge."),
			error_code="PHONE_MISMATCH",
		)

	now = now_datetime()
	effective_grace = max(1, cint(grace_seconds))
	if not doc.verified_at or now > add_to_date(get_datetime(doc.verified_at), seconds=effective_grace):
		return OTPVerifyResult(
			verified=False,
			challenge_id=doc.challenge_id,
			error=_("Verification grace window has expired. Please request a new code."),
			error_code="GRACE_EXPIRED",
		)

	frappe.db.set_value("Portal OTP Challenge", doc.name, "consumed_at", now, update_modified=True)
	return OTPVerifyResult(
		verified=True,
		challenge_id=doc.challenge_id,
		phone_e164=doc.phone_e164,
		purpose=doc.purpose,
	)


def clear_comms_rate_limits() -> None:
	"""Clear OTP, mock OTP, email, and webhook rate-limit keys from cache (for hermetic tests)."""
	_IN_PROCESS_MOCK_LAST_OTP.clear()
	cache = _cache_or_none()
	if cache is None:
		return
	for prefix in (_OTP_KEY_PREFIX, _MOCK_LAST_OTP_PREFIX, _EMAIL_RL_PREFIX, _WEBHOOK_RL_PREFIX):
		try:
			if hasattr(cache, "delete_keys"):
				cache.delete_keys(f"{prefix}:*")
		except Exception:
			pass


def cleanup_expired_otp_challenges() -> int:
	"""Daily job: mark overdue Pending OTP challenges Expired and purge rows older than 7 days."""
	now = now_datetime()
	cutoff = add_to_date(now, days=-OTP_RETENTION_DAYS)

	overdue_names = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabPortal OTP Challenge`
			WHERE status = 'Pending' AND expires_at <= %s
			FOR UPDATE
			""",
			(now,),
		)
	)
	if overdue_names:
		placeholders = ", ".join(["%s"] * len(overdue_names))
		frappe.db.sql(
			f"""
			UPDATE `tabPortal OTP Challenge`
			SET status = 'Expired', modified = %s
			WHERE name IN ({placeholders})
			""",
			(now, *overdue_names),
		)

	purge_names = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabPortal OTP Challenge`
			WHERE creation < %s
				AND (locked_until IS NULL OR locked_until <= %s)
			FOR UPDATE
			""",
			(cutoff, now),
		)
	)
	if purge_names:
		placeholders = ", ".join(["%s"] * len(purge_names))
		frappe.db.sql(
			f"""
			DELETE FROM `tabPortal OTP Challenge`
			WHERE name IN ({placeholders})
			""",
			tuple(purge_names),
		)

	return len(set(overdue_names) | set(purge_names))
