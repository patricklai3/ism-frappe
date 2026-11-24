# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Dual-path Customer Portal authentication, booking links, and anti-IDOR scope guards.

Implements Project 4 Wave 2A:
- Path 1: Direct tokenized RO approval & P5 booking link exchanges (zero OTP friction)
- Path 2: Public SMS-only OTP login (strictly zero email fallback, non-enumerating)
- Path 3: Returning-device persistent cookie restore and server-side sign-out revocation
- Scope & lifecycle guards enforcing strict ownership isolation with zero existence oracle
"""

from __future__ import annotations

import hmac
import secrets
from datetime import time as dt_time
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, get_time, get_url, now_datetime

from ism.comms.contracts import (
	OTPChallengeResult,
	PortalSessionInfo,
	get_client_ip,
	mask_phone_e164,
	normalize_phone_e164,
)
from ism.comms.otp import (
	_OTP_COOLDOWN_PREFIX,
	_OTP_IP_RL_PREFIX,
	_OTP_PHONE_RL_PREFIX,
	_acquire_resend_cooldown,
	_cache_or_none,
	_check_and_increment_hourly_counter,
	_decrement_counter_best_effort,
	_get_default_country_code,
	_get_otp_config,
	_hash_phone,
	clear_comms_rate_limits,
	request_sms_otp,
	verify_sms_otp,
)
from ism.comms.sessions import (
	issue_portal_session,
	revoke_portal_session,
	rotate_portal_session,
	verify_portal_session,
)
from ism.portal.contracts import (
	PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS,
	PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS,
	PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS,
	RO_CLOSED_LOCKED_STATUSES,
	hash_portal_token,
)
from ism.utils.demo_mode import is_demo_mode

__all__ = [
	"assert_ro_mutating_unlocked",
	"assert_session_owns_ro",
	"assert_session_owns_schedule_entry",
	"clear_portal_auth_rate_limits",
	"create_booking_portal_link",
	"exchange_booking_token",
	"exchange_ro_approval_token",
	"get_active_portal_session",
	"get_public_portal_branding",
	"is_ro_mutating_locked",
	"list_accessible_visit_keys",
	"request_portal_login_otp",
	"require_portal_session",
	"resolve_customer_phone_e164",
	"resolve_subjects_for_phone",
	"sign_out_portal_session",
	"verify_portal_login_otp",
]

_PORTAL_AUTH_RL_PREFIX = "ism:portal_auth_rl"
_PORTAL_NOOP_OTP_PREFIX = "ism:portal:noop_otp"
_PORTAL_DEMO_OTP_PREFIX = "ism:portal:demo_otp"
_DEMO_PORTAL_CHALLENGES: dict[str, str] = {}
_LINK_EXCHANGE_WINDOW_SEC = 60
_LINK_EXCHANGE_IP_LIMIT = 30
_LINK_EXCHANGE_TOKEN_LIMIT = 30

_INVALID_LINK_MSG = "This portal link is invalid or has expired."
_INVALID_OTP_MSG = "Invalid or expired verification code."
_PERMISSION_DENIED_MSG = "You do not have permission to access this visit."


def _get_settings() -> Any:
	return frappe.get_cached_doc("ISM Settings")


def _try_normalize_phone(raw_phone: Any, default_country_code: str = "+1") -> str | None:
	if not isinstance(raw_phone, str):
		return None
	cleaned = raw_phone.strip()
	if not cleaned or "@" in cleaned:
		return None
	try:
		return normalize_phone_e164(cleaned, default_country_code=default_country_code)
	except ValueError:
		return None


def _phone_digit_suffix(phone_e164: str) -> tuple[int, str]:
	digits = "".join(ch for ch in phone_e164 if ch.isdigit())
	suffix = digits[-10:] if len(digits) >= 10 else digits
	return len(suffix), suffix


def _enforce_link_exchange_rate_limit(
	endpoint: str,
	raw_token: Any,
	*,
	client_ip: str | None = None,
) -> None:
	resolved_ip = get_client_ip(client_ip)[:140]
	ip_key = f"{_PORTAL_AUTH_RL_PREFIX}:{endpoint}:ip:{resolved_ip}"
	ip_ok, _ip_ttl = _check_and_increment_hourly_counter(
		ip_key,
		_LINK_EXCHANGE_IP_LIMIT,
		window_sec=_LINK_EXCHANGE_WINDOW_SEC,
	)
	if not ip_ok:
		frappe.throw(
			_("Too many requests. Please wait a moment and try again."),
			frappe.TooManyRequestsError,
		)

	if isinstance(raw_token, str) and raw_token.strip():
		tok_hash = hash_portal_token(raw_token)[:24]
		tok_key = f"{_PORTAL_AUTH_RL_PREFIX}:{endpoint}:tok:{tok_hash}"
		tok_ok, _tok_ttl = _check_and_increment_hourly_counter(
			tok_key,
			_LINK_EXCHANGE_TOKEN_LIMIT,
			window_sec=_LINK_EXCHANGE_WINDOW_SEC,
		)
		if not tok_ok:
			frappe.throw(
				_("Too many requests. Please wait a moment and try again."),
				frappe.TooManyRequestsError,
			)


def _format_time_12h(raw_value: Any) -> str:
	if raw_value is None or raw_value == "":
		return ""
	try:
		parsed: dt_time = get_time(raw_value)
	except Exception:
		return ""
	hour_12 = parsed.hour % 12 or 12
	meridiem = "AM" if parsed.hour < 12 else "PM"
	return f"{hour_12}:{parsed.minute:02d} {meridiem}"


def _format_operating_hours_fallback(settings: Any) -> str:
	start_str = _format_time_12h(getattr(settings, "operating_hours_start", None))
	end_str = _format_time_12h(getattr(settings, "operating_hours_end", None))
	if start_str and end_str:
		return f"{start_str} - {end_str}"
	return ""


def _get_contact_phones(contact_name: str) -> list[str]:
	candidates: list[str] = []
	row = frappe.db.get_value("Contact", contact_name, ["mobile_no", "phone"], as_dict=True)
	if row:
		if row.get("mobile_no"):
			candidates.append(row["mobile_no"])
		if row.get("phone"):
			candidates.append(row["phone"])

	child_rows = frappe.db.sql(
		"""
		SELECT phone
		FROM `tabContact Phone`
		WHERE parent = %s AND phone IS NOT NULL AND phone != ''
		ORDER BY IFNULL(is_primary_mobile_no, 0) DESC, IFNULL(is_primary_phone, 0) DESC, idx ASC
		""",
		(contact_name,),
		as_dict=True,
	)
	for child in child_rows:
		if child.get("phone"):
			candidates.append(child["phone"])

	return candidates


def _get_phone_cooldown_ttl(phone_hash: str, default_cooldown_sec: int) -> int:
	"""Return remaining resend-cooldown TTL (> 0) without triggering double-prefixing on cache.exists()."""
	cache = _cache_or_none()
	if cache is None:
		return 0

	cache_key = f"{_OTP_COOLDOWN_PREFIX}:{phone_hash}"
	full_key = cache.make_key(cache_key) if hasattr(cache, "make_key") else cache_key
	if hasattr(cache, "ttl"):
		ttl = cint(cache.ttl(full_key))
		if ttl > 0:
			return ttl
		if ttl == -1:
			if hasattr(cache, "expire"):
				cache.expire(full_key, default_cooldown_sec)
			return max(1, default_cooldown_sec)
		return 0

	return max(1, default_cooldown_sec) if cache.get_value(cache_key) else 0


def _generic_otp_verify_failure(challenge_id: str | None = None) -> dict[str, Any]:
	return {
		"verified": False,
		"challenge_id": challenge_id or None,
		"session": None,
		"error": _(_INVALID_OTP_MSG),
		"error_code": "INVALID_CODE",
	}


def clear_portal_auth_rate_limits() -> None:
	"""Clear portal auth link-exchange, noop OTP, and P1 comms rate-limit keys (for hermetic tests)."""
	clear_comms_rate_limits()
	cache = _cache_or_none()
	if cache is None:
		return
	for prefix in (_PORTAL_AUTH_RL_PREFIX, _PORTAL_NOOP_OTP_PREFIX):
		try:
			if hasattr(cache, "delete_keys"):
				cache.delete_keys(f"{prefix}:*")
		except Exception:
			pass


def create_booking_portal_link(schedule_entry: str) -> str:
	"""FROZEN EXPORT FOR PROJECT 5 (plan_p5_website_self_serve_booking.md).

	Generates a 256-bit cryptographically random token for `schedule_entry`, persists ONLY
	SHA-256(token) + expiry (portal_booking_token_ttl_days) on `Schedule Entry` under FOR UPDATE lock,
	and returns `<site_url>/portal?booking_token=<raw_token>`.
	"""
	if not isinstance(schedule_entry, str) or not schedule_entry.strip():
		frappe.throw(_("Schedule Entry is required."), frappe.ValidationError)

	se_name = schedule_entry.strip()
	locked_rows = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabSchedule Entry`
			WHERE name = %s
			FOR UPDATE
			""",
			(se_name,),
		)
	)
	if not locked_rows:
		frappe.throw(_("Schedule Entry {0} not found.").format(se_name), frappe.DoesNotExistError)

	settings = _get_settings()
	ttl_days = cint(getattr(settings, "portal_booking_token_ttl_days", None) or 0)
	if ttl_days < PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS or ttl_days > PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS:
		ttl_days = PORTAL_BOOKING_TOKEN_DEFAULT_TTL_DAYS

	raw_token = secrets.token_urlsafe(32)
	token_hash = hash_portal_token(raw_token)
	expires_at = add_to_date(now_datetime(), days=ttl_days)

	frappe.db.set_value(
		"Schedule Entry",
		se_name,
		{
			"portal_booking_token_hash": token_hash,
			"portal_booking_token_expires_at": expires_at,
		},
		update_modified=False,
	)

	base_url = get_url().rstrip("/")
	return f"{base_url}/portal?booking_token={raw_token}"


def get_public_portal_branding() -> dict[str, Any]:
	"""Return strictly non-sensitive public branding for Path 2 unauthenticated landing.

	Contains ZERO customer, vehicle, RO, or schedule PII.
	"""
	settings = _get_settings()
	raw_enabled = getattr(settings, "enable_customer_portal", None)
	portal_enabled = True if raw_enabled is None else bool(cint(raw_enabled))

	default_company = (
		frappe.defaults.get_global_default("company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or frappe.db.get_value("Company", {}, "name")
	)
	company_row = (
		frappe.db.get_value("Company", default_company, ["company_name", "phone_no"], as_dict=True)
		if default_company
		else None
	)

	company_name = (
		(getattr(settings, "portal_brand_name", None) or "").strip()
		or ((company_row.get("company_name") or "").strip() if company_row else "")
		or (default_company or "").strip()
		or "ISM Automotive"
	)
	support_phone = (
		(getattr(settings, "portal_support_phone", None) or "").strip()
		or ((company_row.get("phone_no") or "").strip() if company_row else "")
	)
	pickup_hours_text = (
		(getattr(settings, "portal_pickup_hours_text", None) or "").strip()
		or _format_operating_hours_fallback(settings)
	)
	otp_cfg = _get_otp_config(settings)

	return {
		"portal_enabled": portal_enabled,
		"demo_mode": is_demo_mode(settings),
		"company_name": company_name,
		"support_phone": support_phone,
		"pickup_hours_text": pickup_hours_text,
		"otp_code_length": otp_cfg["code_length"],
		"otp_resend_cooldown_seconds": otp_cfg["cooldown_seconds"],
	}


def resolve_customer_phone_e164(
	customer: str | None = None,
	*,
	contact_person: str | None = None,
	fallback_phone: str | None = None,
) -> str | None:
	"""Resolve and E.164-normalize a customer's mobile phone from Contact, Customer, or fallback_phone."""
	settings = _get_settings()
	default_cc = _get_default_country_code(settings)

	checked_contacts: set[str] = set()

	if isinstance(contact_person, str) and contact_person.strip():
		clean_contact = contact_person.strip()
		checked_contacts.add(clean_contact)
		for candidate in _get_contact_phones(clean_contact):
			normalized = _try_normalize_phone(candidate, default_cc)
			if normalized:
				return normalized

	if isinstance(customer, str) and customer.strip():
		clean_customer = customer.strip()
		cust_row = frappe.db.get_value(
			"Customer",
			clean_customer,
			["mobile_no", "customer_primary_contact"],
			as_dict=True,
		)
		if cust_row:
			normalized = _try_normalize_phone(cust_row.get("mobile_no"), default_cc)
			if normalized:
				return normalized

			primary_contact = (cust_row.get("customer_primary_contact") or "").strip()
			if primary_contact and primary_contact not in checked_contacts:
				checked_contacts.add(primary_contact)
				for candidate in _get_contact_phones(primary_contact):
					normalized = _try_normalize_phone(candidate, default_cc)
					if normalized:
						return normalized

		linked_contacts = frappe.db.sql(
			"""
			SELECT c.name
			FROM `tabContact` c
			INNER JOIN `tabDynamic Link` dl
				ON dl.parent = c.name
				AND dl.parenttype = 'Contact'
				AND dl.parentfield = 'links'
				AND dl.link_doctype = 'Customer'
				AND dl.link_name = %s
			ORDER BY IFNULL(c.is_primary_contact, 0) DESC, c.modified DESC
			""",
			(clean_customer,),
			as_dict=True,
		)
		for row in linked_contacts:
			cname = (row.get("name") or "").strip()
			if not cname or cname in checked_contacts:
				continue
			checked_contacts.add(cname)
			for candidate in _get_contact_phones(cname):
				normalized = _try_normalize_phone(candidate, default_cc)
				if normalized:
					return normalized

	return _try_normalize_phone(fallback_phone, default_cc)


def resolve_subjects_for_phone(phone_e164: str) -> dict[str, list[str]]:
	"""Find all Customer IDs and provisional Schedule Entry IDs matching `phone_e164`.

	Matches normalized E.164 across Customer.mobile_no, Dynamic-Link Contact (mobile_no, phone,
	Contact Phone), primary Customer Contacts, and Schedule Entry.provisional_phone by filtering
	last-10-digit SQL candidates and confirming exact normalize_phone_e164() equality in Python.
	"""
	settings = _get_settings()
	default_cc = _get_default_country_code(settings)
	target_e164 = _try_normalize_phone(phone_e164, default_cc)
	if not target_e164:
		return {"customers": [], "schedule_entries": []}

	suffix_len, suffix = _phone_digit_suffix(target_e164)
	if not suffix:
		return {"customers": [], "schedule_entries": []}

	matched_customers: set[str] = set()
	matched_schedule_entries: set[str] = set()

	# 1. Customer.mobile_no
	customer_rows = frappe.db.sql(
		"""
		SELECT name, mobile_no
		FROM `tabCustomer`
		WHERE mobile_no IS NOT NULL
			AND mobile_no != ''
			AND RIGHT(REGEXP_REPLACE(mobile_no, '[^0-9]', ''), %s) = %s
		""",
		(suffix_len, suffix),
		as_dict=True,
	)
	for row in customer_rows:
		if _try_normalize_phone(row.get("mobile_no"), default_cc) == target_e164:
			matched_customers.add(row["name"])

	# 2. Dynamic-Link Contact (mobile_no / phone)
	contact_rows = frappe.db.sql(
		"""
		SELECT dl.link_name AS customer, c.mobile_no, c.phone
		FROM `tabContact` c
		INNER JOIN `tabDynamic Link` dl
			ON dl.parent = c.name
			AND dl.parenttype = 'Contact'
			AND dl.parentfield = 'links'
			AND dl.link_doctype = 'Customer'
		INNER JOIN `tabCustomer` cust
			ON cust.name = dl.link_name
		WHERE (
			(c.mobile_no IS NOT NULL AND c.mobile_no != ''
				AND RIGHT(REGEXP_REPLACE(c.mobile_no, '[^0-9]', ''), %s) = %s)
			OR (c.phone IS NOT NULL AND c.phone != ''
				AND RIGHT(REGEXP_REPLACE(c.phone, '[^0-9]', ''), %s) = %s)
		)
		""",
		(suffix_len, suffix, suffix_len, suffix),
		as_dict=True,
	)
	for row in contact_rows:
		if (
			_try_normalize_phone(row.get("mobile_no"), default_cc) == target_e164
			or _try_normalize_phone(row.get("phone"), default_cc) == target_e164
		):
			matched_customers.add(row["customer"])

	# 3. Dynamic-Link Contact Phone child table
	contact_phone_rows = frappe.db.sql(
		"""
		SELECT dl.link_name AS customer, cp.phone
		FROM `tabContact Phone` cp
		INNER JOIN `tabDynamic Link` dl
			ON dl.parent = cp.parent
			AND dl.parenttype = 'Contact'
			AND dl.parentfield = 'links'
			AND dl.link_doctype = 'Customer'
		INNER JOIN `tabCustomer` cust
			ON cust.name = dl.link_name
		WHERE cp.phone IS NOT NULL
			AND cp.phone != ''
			AND RIGHT(REGEXP_REPLACE(cp.phone, '[^0-9]', ''), %s) = %s
		""",
		(suffix_len, suffix),
		as_dict=True,
	)
	for row in contact_phone_rows:
		if _try_normalize_phone(row.get("phone"), default_cc) == target_e164:
			matched_customers.add(row["customer"])

	# 4. Customer.customer_primary_contact (even when Dynamic Link is absent)
	primary_contact_rows = frappe.db.sql(
		"""
		SELECT cust.name AS customer, c.mobile_no, c.phone
		FROM `tabCustomer` cust
		INNER JOIN `tabContact` c
			ON c.name = cust.customer_primary_contact
		WHERE (
			(c.mobile_no IS NOT NULL AND c.mobile_no != ''
				AND RIGHT(REGEXP_REPLACE(c.mobile_no, '[^0-9]', ''), %s) = %s)
			OR (c.phone IS NOT NULL AND c.phone != ''
				AND RIGHT(REGEXP_REPLACE(c.phone, '[^0-9]', ''), %s) = %s)
		)
		""",
		(suffix_len, suffix, suffix_len, suffix),
		as_dict=True,
	)
	for row in primary_contact_rows:
		if (
			_try_normalize_phone(row.get("mobile_no"), default_cc) == target_e164
			or _try_normalize_phone(row.get("phone"), default_cc) == target_e164
		):
			matched_customers.add(row["customer"])

	primary_cp_rows = frappe.db.sql(
		"""
		SELECT cust.name AS customer, cp.phone
		FROM `tabCustomer` cust
		INNER JOIN `tabContact Phone` cp
			ON cp.parent = cust.customer_primary_contact
		WHERE cp.phone IS NOT NULL
			AND cp.phone != ''
			AND RIGHT(REGEXP_REPLACE(cp.phone, '[^0-9]', ''), %s) = %s
		""",
		(suffix_len, suffix),
		as_dict=True,
	)
	for row in primary_cp_rows:
		if _try_normalize_phone(row.get("phone"), default_cc) == target_e164:
			matched_customers.add(row["customer"])

	# 5. Schedule Entry.provisional_phone
	se_rows = frappe.db.sql(
		"""
		SELECT name, provisional_phone
		FROM `tabSchedule Entry`
		WHERE provisional_phone IS NOT NULL
			AND provisional_phone != ''
			AND RIGHT(REGEXP_REPLACE(provisional_phone, '[^0-9]', ''), %s) = %s
		""",
		(suffix_len, suffix),
		as_dict=True,
	)
	for row in se_rows:
		if _try_normalize_phone(row.get("provisional_phone"), default_cc) == target_e164:
			matched_schedule_entries.add(row["name"])

	return {
		"customers": sorted(matched_customers),
		"schedule_entries": sorted(matched_schedule_entries),
	}


def exchange_ro_approval_token(
	raw_token: str,
	*,
	client_ip: str | None = None,
	user_agent: str | None = None,
	set_response_cookie: bool = True,
) -> dict[str, Any]:
	"""Path 1 (Zero OTP Friction): Exchange Repair Order.approval_token into persistent session cookie."""
	_enforce_link_exchange_rate_limit("ro_token", raw_token, client_ip=client_ip)

	if not isinstance(raw_token, str) or not raw_token.strip():
		return {"valid": False, "error": _(_INVALID_LINK_MSG)}

	clean_token = raw_token.strip()
	row = frappe.db.get_value(
		"Repair Order",
		{"approval_token": clean_token},
		["name", "customer", "contact_person", "approval_token", "approval_token_expiry"],
		as_dict=True,
	)
	if not row or not hmac.compare_digest(clean_token, (row.get("approval_token") or "").strip()):
		return {"valid": False, "error": _(_INVALID_LINK_MSG)}

	if row.get("approval_token_expiry") and now_datetime() > get_datetime(row["approval_token_expiry"]):
		return {"valid": False, "error": _(_INVALID_LINK_MSG)}

	resolved_phone = resolve_customer_phone_e164(
		row.get("customer"),
		contact_person=row.get("contact_person"),
	)
	session_info = issue_portal_session(
		auth_method="magic_link",
		scope_type="repair_order",
		customer=row.get("customer"),
		phone_e164=resolved_phone,
		repair_order=row["name"],
		source_magic_token=clean_token,
		client_ip=client_ip,
		user_agent=user_agent,
		set_response_cookie=set_response_cookie,
	)
	return {
		"valid": True,
		"session": session_info,
		"repair_order": row["name"],
	}


def exchange_booking_token(
	raw_token: str,
	*,
	client_ip: str | None = None,
	user_agent: str | None = None,
	set_response_cookie: bool = True,
) -> dict[str, Any]:
	"""Path 1 (Zero OTP Friction for P5 booking links): Exchange hashed booking token into session cookie."""
	_enforce_link_exchange_rate_limit("booking_token", raw_token, client_ip=client_ip)

	if not isinstance(raw_token, str) or not raw_token.strip():
		return {"valid": False, "error": _(_INVALID_LINK_MSG)}

	clean_token = raw_token.strip()
	token_hash = hash_portal_token(clean_token)
	row = frappe.db.get_value(
		"Schedule Entry",
		{"portal_booking_token_hash": token_hash},
		[
			"name",
			"customer",
			"repair_order",
			"provisional_phone",
			"portal_booking_token_hash",
			"portal_booking_token_expires_at",
		],
		as_dict=True,
	)
	if not row or not hmac.compare_digest(token_hash, (row.get("portal_booking_token_hash") or "").strip()):
		return {"valid": False, "error": _(_INVALID_LINK_MSG)}

	expires_at = row.get("portal_booking_token_expires_at")
	if not expires_at or now_datetime() > get_datetime(expires_at):
		return {"valid": False, "error": _(_INVALID_LINK_MSG)}

	resolved_phone = resolve_customer_phone_e164(
		row.get("customer"),
		fallback_phone=row.get("provisional_phone"),
	)
	session_info = issue_portal_session(
		auth_method="magic_link",
		scope_type="booking",
		customer=row.get("customer"),
		phone_e164=resolved_phone,
		repair_order=row.get("repair_order") or None,
		schedule_entry=row["name"],
		source_magic_token=clean_token,
		client_ip=client_ip,
		user_agent=user_agent,
		set_response_cookie=set_response_cookie,
	)
	return {
		"valid": True,
		"session": session_info,
		"schedule_entry": row["name"],
		"repair_order": row.get("repair_order") or None,
	}


def request_portal_login_otp(
	raw_phone: str,
	*,
	client_ip: str | None = None,
	user_agent: str | None = None,
) -> dict[str, Any]:
	"""Path 2 (Public Entry / Cross-Device Re-Entry — SMS ONLY, ZERO Email Fallback).

	Rejects non-string/email inputs, enforces uniform per-IP/per-phone/cooldown buckets,
	delegates known phones to P1 request_sms_otp, and returns an indistinguishable synthetic
	challenge for unknown phones with zero SMS dispatched.
	"""
	if not isinstance(raw_phone, str) or not raw_phone.strip():
		frappe.throw(_("Please enter a valid mobile phone number."), frappe.ValidationError)

	if "@" in raw_phone:
		frappe.throw(
			_("Portal sign-in uses SMS verification only. Email addresses are not supported."),
			frappe.ValidationError,
		)

	settings = _get_settings()
	default_cc = _get_default_country_code(settings)
	try:
		phone_e164 = normalize_phone_e164(raw_phone, default_country_code=default_cc)
	except ValueError:
		frappe.throw(_("Please enter a valid mobile phone number."), frappe.ValidationError)

	if is_demo_mode(settings):
		challenge_id = f"demo-{secrets.token_urlsafe(12)}"
		_DEMO_PORTAL_CHALLENGES[challenge_id] = phone_e164
		cache = _cache_or_none()
		if cache is not None:
			cache.set_value(f"{_PORTAL_DEMO_OTP_PREFIX}:{challenge_id}", phone_e164, expires_in_sec=300)
		payload = OTPChallengeResult(
			success=True,
			challenge_id=challenge_id,
			phone_masked=mask_phone_e164(phone_e164),
			expires_in_seconds=300,
			resend_available_in_seconds=5,
		).to_dict()
		payload["demo_mode"] = True
		payload["demo_code"] = "000000"
		payload["message"] = _("Demo Mode: Enter 000000 to sign in (SMS bypassed).")
		return payload

	subjects = resolve_subjects_for_phone(phone_e164)
	if subjects["customers"] or subjects["schedule_entries"]:
		result = request_sms_otp(
			phone_e164,
			purpose="portal_login",
			client_ip=client_ip,
			user_agent=user_agent,
		)
		return result.to_dict()

	# Unknown phone number: consume identical rate-limit & cooldown buckets, send ZERO SMS,
	# and return the identical uniform opaque challenge payload (anti-enumeration).
	cfg = _get_otp_config(settings)
	phone_masked = mask_phone_e164(phone_e164)
	phone_hash = _hash_phone(phone_e164)
	resolved_ip = get_client_ip(client_ip)[:140]

	existing_cd_ttl = _get_phone_cooldown_ttl(phone_hash, cfg["cooldown_seconds"])
	if existing_cd_ttl > 0:
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=existing_cd_ttl,
			error=_("Please wait before requesting another verification code."),
			error_code="COOLDOWN_ACTIVE",
		).to_dict()

	ip_rl_key = f"{_OTP_IP_RL_PREFIX}:{resolved_ip}"
	ip_ok, ip_ttl = _check_and_increment_hourly_counter(ip_rl_key, cfg["ip_limit"])
	if not ip_ok:
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=ip_ttl,
			error=_("Too many verification code requests from this IP address. Please try again later."),
			error_code="RATE_LIMITED",
		).to_dict()

	phone_rl_key = f"{_OTP_PHONE_RL_PREFIX}:{phone_hash}"
	phone_ok, phone_ttl = _check_and_increment_hourly_counter(phone_rl_key, cfg["phone_limit"])
	if not phone_ok:
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=phone_ttl,
			error=_("Too many verification code requests for this phone number. Please try again later."),
			error_code="RATE_LIMITED",
		).to_dict()

	cooldown_ok, cooldown_ttl = _acquire_resend_cooldown(phone_hash, cfg["cooldown_seconds"])
	if not cooldown_ok:
		_decrement_counter_best_effort(phone_rl_key)
		_decrement_counter_best_effort(ip_rl_key)
		return OTPChallengeResult(
			success=False,
			phone_masked=phone_masked,
			resend_available_in_seconds=cooldown_ttl,
			error=_("Please wait before requesting another verification code."),
			error_code="COOLDOWN_ACTIVE",
		).to_dict()

	challenge_id = secrets.token_urlsafe(24)
	cache = _cache_or_none()
	if cache is not None:
		cache.set_value(
			f"{_PORTAL_NOOP_OTP_PREFIX}:{challenge_id}",
			{
				"phone_hash": phone_hash,
				"attempts": 0,
				"max_attempts": cfg["max_attempts"],
			},
			expires_in_sec=cfg["ttl_seconds"],
		)

	return OTPChallengeResult(
		success=True,
		challenge_id=challenge_id,
		phone_masked=phone_masked,
		expires_in_seconds=cfg["ttl_seconds"],
		resend_available_in_seconds=cfg["cooldown_seconds"],
	).to_dict()


def verify_portal_login_otp(
	challenge_id: str,
	code: str,
	*,
	raw_phone: str | None = None,
	client_ip: str | None = None,
	user_agent: str | None = None,
	upgrade_existing_session: bool = False,
	set_response_cookie: bool = True,
) -> dict[str, Any]:
	"""Verify SMS OTP challenge and issue or rotate a customer-scoped persistent portal session."""
	clean_challenge_id = challenge_id.strip() if isinstance(challenge_id, str) else ""
	clean_code = code.strip() if isinstance(code, str) else ""
	if not clean_challenge_id or not clean_code:
		return _generic_otp_verify_failure(clean_challenge_id)

	settings = _get_settings()
	default_cc = _get_default_country_code(settings)

	normalized_phone: str | None = None
	if raw_phone is not None:
		normalized_phone = _try_normalize_phone(raw_phone, default_cc)
		if not normalized_phone:
			return _generic_otp_verify_failure(clean_challenge_id)

	# Handle synthetic non-enumerating noop challenges identically to invalid real codes.
	noop_key = f"{_PORTAL_NOOP_OTP_PREFIX}:{clean_challenge_id}"
	cache = _cache_or_none()
	noop_marker = cache.get_value(noop_key) if cache is not None else None
	if noop_marker is not None:
		if isinstance(noop_marker, dict):
			attempts = cint(noop_marker.get("attempts") or 0) + 1
			max_attempts = cint(noop_marker.get("max_attempts") or 5)
			if attempts >= max_attempts:
				cache.delete_value(noop_key)
			else:
				noop_marker["attempts"] = attempts
				cfg = _get_otp_config(settings)
				cache.set_value(noop_key, noop_marker, expires_in_sec=cfg["ttl_seconds"])
		return _generic_otp_verify_failure(clean_challenge_id)

	source_challenge_id: str | None = None
	if is_demo_mode(settings) and clean_challenge_id.startswith("demo-"):
		demo_key = f"{_PORTAL_DEMO_OTP_PREFIX}:{clean_challenge_id}"
		demo_phone = cache.get_value(demo_key) if cache is not None else None
		if not demo_phone:
			demo_phone = _DEMO_PORTAL_CHALLENGES.get(clean_challenge_id)
		if not demo_phone or not clean_code.isdigit() or not (4 <= len(clean_code) <= 8):
			return _generic_otp_verify_failure(clean_challenge_id)
		if normalized_phone and normalized_phone != demo_phone:
			return _generic_otp_verify_failure(clean_challenge_id)
		if cache is not None:
			cache.delete_value(demo_key)
		_DEMO_PORTAL_CHALLENGES.pop(clean_challenge_id, None)
		verified_phone = str(demo_phone)
	else:
		verify_result = verify_sms_otp(
			clean_challenge_id,
			clean_code,
			phone=normalized_phone,
			purpose="portal_login",
			client_ip=client_ip,
			consume=True,
		)
		if not verify_result.verified:
			if verify_result.error_code == "LOCKED_OUT":
				return {
					"verified": False,
					"challenge_id": verify_result.challenge_id or clean_challenge_id,
					"session": None,
					"locked_until": verify_result.locked_until,
					"error": verify_result.error,
					"error_code": "LOCKED_OUT",
				}
			return _generic_otp_verify_failure(verify_result.challenge_id or clean_challenge_id)

		verified_phone = verify_result.phone_e164
		source_challenge_id = verify_result.challenge_id

	subjects = resolve_subjects_for_phone(verified_phone or "")
	primary_customer = subjects["customers"][0] if subjects["customers"] else None
	primary_schedule_entry = (
		subjects["schedule_entries"][0]
		if (not primary_customer and subjects["schedule_entries"])
		else None
	)

	session_info: PortalSessionInfo
	if upgrade_existing_session:
		existing = verify_portal_session(touch=False)
		if existing is not None and existing.phone_e164 == verified_phone:
			session_info = rotate_portal_session(
				customer=primary_customer,
				scope_type="customer",
				set_response_cookie=set_response_cookie,
			)
		else:
			if existing is not None:
				revoke_portal_session(
					session_id=existing.session_id,
					reason="Upgraded via SMS OTP",
					clear_response_cookie=False,
				)
			session_info = issue_portal_session(
				auth_method="sms_otp",
				scope_type="customer",
				customer=primary_customer,
				phone_e164=verified_phone,
				schedule_entry=primary_schedule_entry,
				source_otp_challenge=source_challenge_id,
				client_ip=client_ip,
				user_agent=user_agent,
				set_response_cookie=set_response_cookie,
			)
	else:
		session_info = issue_portal_session(
			auth_method="sms_otp",
			scope_type="customer",
			customer=primary_customer,
			phone_e164=verified_phone,
			schedule_entry=primary_schedule_entry,
			source_otp_challenge=source_challenge_id,
			client_ip=client_ip,
			user_agent=user_agent,
			set_response_cookie=set_response_cookie,
		)

	return {
		"verified": True,
		"challenge_id": source_challenge_id or clean_challenge_id,
		"session": session_info,
		"customers": subjects["customers"],
		"schedule_entries": subjects["schedule_entries"],
	}


def get_active_portal_session(
	*,
	touch: bool = True,
	cookie_value: str | None = None,
) -> PortalSessionInfo | None:
	"""Path 3 (Returning Device Continuity): Resolve active portal session from cookie."""
	return verify_portal_session(cookie_value=cookie_value, touch=touch)


def require_portal_session(
	*,
	touch: bool = True,
	cookie_value: str | None = None,
) -> PortalSessionInfo:
	"""Resolve active portal session or raise frappe.AuthenticationError."""
	session = get_active_portal_session(touch=touch, cookie_value=cookie_value)
	if session is None:
		frappe.throw(
			_("Please sign in to access the Customer Portal."),
			frappe.AuthenticationError,
		)
	return session


def sign_out_portal_session(
	*,
	reason: str = "Customer signed out on device",
	cookie_value: str | None = None,
	session_id: str | None = None,
) -> dict[str, Any]:
	"""Revoke active portal session server-side and clear the response cookie."""
	revoked = revoke_portal_session(
		cookie_value=cookie_value,
		session_id=session_id,
		reason=reason,
		clear_response_cookie=True,
	)
	return {"signed_out": True, "revoked": bool(revoked)}


def list_accessible_visit_keys(session: PortalSessionInfo) -> dict[str, list[str]]:
	"""Compute authoritative {'repair_orders': [...], 'schedule_entries': [...]} for `session.scope_type`.

	- repair_order: ONLY [session.repair_order] + Schedule Entries where repair_order == session.repair_order.
	- booking: ONLY [session.schedule_entry] + converted [se.repair_order] if se.repair_order is set.
	- customer: All ROs for matched Customer IDs + all Schedule Entries linked to those Customers,
	  those ROs, or matching provisional_phone == session.phone_e164.
	"""
	if not isinstance(session, PortalSessionInfo) or session.status != "Active":
		return {"repair_orders": [], "schedule_entries": []}

	if session.scope_type == "repair_order":
		ro_name = (session.repair_order or "").strip()
		if not ro_name or not frappe.db.exists("Repair Order", ro_name):
			return {"repair_orders": [], "schedule_entries": []}
		se_names = frappe.get_all(
			"Schedule Entry",
			filters={"repair_order": ro_name},
			pluck="name",
			order_by="name asc",
		)
		return {
			"repair_orders": [ro_name],
			"schedule_entries": sorted(set(se_names)),
		}

	if session.scope_type == "booking":
		se_name = (session.schedule_entry or "").strip()
		if not se_name:
			return {"repair_orders": [], "schedule_entries": []}
		se_row = frappe.db.get_value(
			"Schedule Entry",
			se_name,
			["name", "repair_order"],
			as_dict=True,
		)
		if not se_row:
			return {"repair_orders": [], "schedule_entries": []}
		repair_orders: list[str] = []
		converted_ro = (se_row.get("repair_order") or "").strip()
		if converted_ro and frappe.db.exists("Repair Order", converted_ro):
			repair_orders.append(converted_ro)
		return {
			"repair_orders": repair_orders,
			"schedule_entries": [se_row["name"]],
		}

	if session.scope_type == "customer":
		matched_customers: set[str] = set()
		matched_se: set[str] = set()
		matched_ro: set[str] = set()

		session_customer = (session.customer or "").strip()
		if session_customer and frappe.db.exists("Customer", session_customer):
			matched_customers.add(session_customer)

		if session.phone_e164:
			subjects = resolve_subjects_for_phone(session.phone_e164)
			matched_customers.update(subjects["customers"])
			matched_se.update(subjects["schedule_entries"])

		if matched_customers:
			customer_ros = frappe.get_all(
				"Repair Order",
				filters={"customer": ["in", sorted(matched_customers)]},
				pluck="name",
			)
			matched_ro.update(customer_ros)

			customer_ses = frappe.get_all(
				"Schedule Entry",
				filters={"customer": ["in", sorted(matched_customers)]},
				pluck="name",
			)
			matched_se.update(customer_ses)

		# Follow any provisional Schedule Entry already converted to a Repair Order.
		if matched_se:
			converted_ros = frappe.get_all(
				"Schedule Entry",
				filters={
					"name": ["in", sorted(matched_se)],
					"repair_order": ["is", "set"],
				},
				pluck="repair_order",
			)
			for ro_name in converted_ros:
				if ro_name and frappe.db.exists("Repair Order", ro_name):
					matched_ro.add(ro_name)

		# Include Schedule Entries linked to any accessible Repair Order.
		if matched_ro:
			ro_ses = frappe.get_all(
				"Schedule Entry",
				filters={"repair_order": ["in", sorted(matched_ro)]},
				pluck="name",
			)
			matched_se.update(ro_ses)

		return {
			"repair_orders": sorted(matched_ro),
			"schedule_entries": sorted(matched_se),
		}

	return {"repair_orders": [], "schedule_entries": []}


def assert_session_owns_ro(session: PortalSessionInfo, repair_order: str) -> None:
	"""Enforce RO ownership for `session`; raise uniform frappe.PermissionError on mismatch."""
	if not isinstance(repair_order, str) or not repair_order.strip():
		frappe.throw(_(_PERMISSION_DENIED_MSG), frappe.PermissionError)

	clean_ro = repair_order.strip()
	accessible = list_accessible_visit_keys(session)
	if clean_ro not in accessible["repair_orders"]:
		frappe.throw(_(_PERMISSION_DENIED_MSG), frappe.PermissionError)


def assert_session_owns_schedule_entry(session: PortalSessionInfo, schedule_entry: str) -> None:
	"""Enforce Schedule Entry ownership for `session`; raise uniform frappe.PermissionError on mismatch."""
	if not isinstance(schedule_entry, str) or not schedule_entry.strip():
		frappe.throw(_(_PERMISSION_DENIED_MSG), frappe.PermissionError)

	clean_se = schedule_entry.strip()
	accessible = list_accessible_visit_keys(session)
	if clean_se not in accessible["schedule_entries"]:
		frappe.throw(_(_PERMISSION_DENIED_MSG), frappe.PermissionError)


def is_ro_mutating_locked(ro_status: str | None) -> bool:
	"""Return True when ro_status is in RO_CLOSED_LOCKED_STATUSES ('Closed / Released', 'Cancelled')."""
	return isinstance(ro_status, str) and ro_status.strip() in RO_CLOSED_LOCKED_STATUSES


def assert_ro_mutating_unlocked(ro_status: str | None) -> None:
	"""Raise frappe.ValidationError when the Repair Order is locked against portal mutations."""
	if is_ro_mutating_locked(ro_status):
		frappe.throw(
			_("This repair order is closed and can no longer be modified from the portal."),
			frappe.ValidationError,
		)
