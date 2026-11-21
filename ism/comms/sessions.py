# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac
import inspect
import ipaddress
import re
import secrets
from typing import Any
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from ism.comms.contracts import (
	DEFAULT_PORTAL_COOKIE_NAME,
	SESSION_AUTH_METHODS,
	SESSION_SCOPE_TYPES,
	PortalSessionInfo,
	get_client_ip,
	normalize_phone_e164,
)

__all__ = [
	"LAST_SEEN_THROTTLE_SECONDS",
	"SESSION_COOKIE_VERSION",
	"SESSION_RETENTION_DAYS",
	"cleanup_expired_portal_sessions",
	"clear_portal_session_cookie",
	"get_portal_cookie_name",
	"get_portal_session_signing_secret",
	"issue_portal_session",
	"revoke_all_sessions_for_customer",
	"revoke_all_sessions_for_phone",
	"revoke_portal_session",
	"rotate_portal_session",
	"set_portal_session_cookie",
	"should_use_secure_cookie",
	"verify_portal_session",
]

SESSION_COOKIE_VERSION = "v1"
LAST_SEEN_THROTTLE_SECONDS = 300
SESSION_RETENTION_DAYS = 90
_DEFAULT_SESSION_TTL_DAYS = 30
_ALLOWED_SAMESITE = ("Lax", "Strict")
_URLSAFE_PART_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def _get_settings(settings: Any = None) -> Any:
	return settings if settings is not None else frappe.get_single("ISM Settings")


def get_portal_cookie_name(settings: Any = None) -> str:
	"""Return the configured portal session cookie name, falling back to the default."""
	cfg = _get_settings(settings)
	raw = getattr(cfg, "portal_session_cookie_name", None)
	if isinstance(raw, str):
		cleaned = raw.strip()
		if _COOKIE_NAME_RE.match(cleaned):
			return cleaned
	return DEFAULT_PORTAL_COOKIE_NAME


def get_portal_session_signing_secret(settings: Any = None, *, create_if_missing: bool = True) -> bytes | None:
	"""Resolve portal_session_signing_secret from ISM Settings, lazily generating if blank."""
	cfg = _get_settings(settings)
	secret = cfg.get_password("portal_session_signing_secret", raise_exception=False)
	if isinstance(secret, str) and secret.strip():
		return secret.encode("utf-8")

	if not create_if_missing:
		return None

	generated = secrets.token_urlsafe(48)
	cfg.portal_session_signing_secret = generated
	cfg.save(ignore_permissions=True)
	return generated.encode("utf-8")


def _extract_request_hostname(request: Any) -> str:
	raw_host = getattr(request, "host", None)
	if not raw_host and hasattr(request, "headers"):
		raw_host = request.headers.get("Host")
	if isinstance(raw_host, str) and raw_host.strip():
		parsed = urlparse(f"http://{raw_host.strip()}")
		return (parsed.hostname or "").strip().lower()

	raw_url = getattr(request, "url", None)
	if isinstance(raw_url, str) and raw_url.strip():
		parsed = urlparse(raw_url.strip())
		return (parsed.hostname or "").strip().lower()

	return ""


def _is_loopback_hostname(hostname: str) -> bool:
	if not hostname:
		return False
	if hostname in ("localhost", "localhost.localdomain") or hostname.endswith(".localhost"):
		return True
	try:
		return ipaddress.ip_address(hostname).is_loopback
	except ValueError:
		return False


def _is_loopback_http_request() -> bool:
	request = getattr(frappe.local, "request", None)
	if not request:
		return False

	forwarded_proto = None
	if hasattr(request, "headers") and request.headers is not None:
		forwarded_proto = request.headers.get("X-Forwarded-Proto")
	if not forwarded_proto:
		try:
			forwarded_proto = frappe.get_request_header("X-Forwarded-Proto")
		except Exception:
			forwarded_proto = None
	if isinstance(forwarded_proto, str) and forwarded_proto.strip():
		proto = forwarded_proto.split(",")[0].strip().lower()
		if proto != "http":
			return False

	if getattr(request, "is_secure", False):
		return False

	scheme = (getattr(request, "scheme", "") or "").strip().lower()
	if scheme != "http":
		return False

	hostname = _extract_request_hostname(request)
	return _is_loopback_hostname(hostname)


def should_use_secure_cookie(settings: Any = None) -> bool:
	"""Return True unless portal_session_secure_cookie=0 AND the active request is loopback HTTP."""
	cfg = _get_settings(settings)
	raw_flag = getattr(cfg, "portal_session_secure_cookie", 1)
	if raw_flag is None or cint(raw_flag) != 0:
		return True
	return not _is_loopback_http_request()


def _get_cookie_samesite(settings: Any = None) -> str:
	cfg = _get_settings(settings)
	samesite = getattr(cfg, "portal_session_cookie_samesite", "Lax")
	return samesite if samesite in _ALLOWED_SAMESITE else "Lax"


def _hash_secret(raw_secret: str) -> str:
	return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()


def _short_hash(value: str | None) -> str | None:
	if not isinstance(value, str) or not value.strip():
		return None
	return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:24]


def _sign_payload(payload: str, secret_bytes: bytes) -> str:
	digest = hmac.new(secret_bytes, payload.encode("utf-8"), hashlib.sha256).digest()
	return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_signed_cookie(session_id: str, raw_secret: str, secret_bytes: bytes) -> str:
	payload = f"{SESSION_COOKIE_VERSION}.{session_id}.{raw_secret}"
	sig = _sign_payload(payload, secret_bytes)
	return f"{payload}.{sig}"


def _parse_and_verify_cookie(cookie_value: str | None, settings: Any = None) -> tuple[str, str] | None:
	"""Parse wire token and verify HMAC signature in constant time before any DB access."""
	if not isinstance(cookie_value, str) or not cookie_value:
		return None

	parts = cookie_value.split(".")
	if len(parts) != 4:
		return None

	version, session_id, raw_secret, provided_sig = parts
	if version != SESSION_COOKIE_VERSION:
		return None
	if (
		not _URLSAFE_PART_RE.match(session_id)
		or not _URLSAFE_PART_RE.match(raw_secret)
		or not _URLSAFE_PART_RE.match(provided_sig)
	):
		return None

	secret_bytes = get_portal_session_signing_secret(settings, create_if_missing=False)
	if not secret_bytes:
		return None

	payload = f"{version}.{session_id}.{raw_secret}"
	expected_sig = _sign_payload(payload, secret_bytes)
	if not hmac.compare_digest(provided_sig, expected_sig):
		return None

	return session_id, raw_secret


def _read_request_cookie(settings: Any = None) -> str | None:
	request = getattr(frappe.local, "request", None)
	if not request:
		return None
	cookies = getattr(request, "cookies", None)
	if not cookies:
		return None
	return cookies.get(get_portal_cookie_name(settings))


def _resolve_user_agent(user_agent: str | None) -> str | None:
	if isinstance(user_agent, str) and user_agent.strip():
		return user_agent.strip()
	if user_agent is None and getattr(frappe.local, "request", None):
		try:
			header_ua = frappe.get_request_header("User-Agent")
			if isinstance(header_ua, str) and header_ua.strip():
				return header_ua.strip()
		except Exception:
			return None
	return None


def _clean_optional_str(value: str | None) -> str | None:
	if not isinstance(value, str):
		return None
	cleaned = value.strip()
	return cleaned or None


def _resolve_otp_challenge_link(source_otp_challenge: str | None) -> str | None:
	cleaned = _clean_optional_str(source_otp_challenge)
	if not cleaned:
		return None
	if frappe.db.exists("Portal OTP Challenge", cleaned):
		return cleaned
	matched_name = frappe.db.get_value("Portal OTP Challenge", {"challenge_id": cleaned}, "name")
	return matched_name or cleaned


def _row_to_session_info(row: Any, *, cookie_value: str | None = None) -> PortalSessionInfo:
	return PortalSessionInfo(
		session_id=row.session_id,
		status=row.status,
		auth_method=row.auth_method,
		scope_type=row.scope_type,
		customer=row.customer or None,
		phone_e164=row.phone_e164 or None,
		repair_order=row.scope_repair_order or None,
		schedule_entry=row.scope_schedule_entry or None,
		issued_at=str(row.issued_at) if row.issued_at else None,
		expires_at=str(row.expires_at) if row.expires_at else None,
		cookie_value=cookie_value,
	)


def _create_session_record(
	*,
	auth_method: str,
	scope_type: str,
	customer: str | None,
	phone_e164: str | None,
	repair_order: str | None,
	schedule_entry: str | None,
	source_otp_challenge: str | None,
	source_token_hash: str | None,
	client_ip: str | None,
	user_agent_hash: str | None,
	set_response_cookie: bool,
	settings: Any = None,
) -> PortalSessionInfo:
	cfg = _get_settings(settings)
	ttl_days = cint(getattr(cfg, "portal_session_ttl_days", _DEFAULT_SESSION_TTL_DAYS)) or _DEFAULT_SESSION_TTL_DAYS
	max_age_seconds = ttl_days * 86400

	session_id = secrets.token_urlsafe(24)
	raw_secret = secrets.token_urlsafe(32)
	token_hash = _hash_secret(raw_secret)
	secret_bytes = get_portal_session_signing_secret(cfg, create_if_missing=True)
	cookie_value = _build_signed_cookie(session_id, raw_secret, secret_bytes)

	issued_dt = now_datetime()
	expires_dt = add_to_date(issued_dt, days=ttl_days)
	resolved_ip = get_client_ip(ip_override=client_ip)

	doc = frappe.get_doc({
		"doctype": "Customer Portal Session",
		"session_id": session_id,
		"token_hash": token_hash,
		"customer": customer,
		"phone_e164": phone_e164,
		"auth_method": auth_method,
		"scope_type": scope_type,
		"scope_repair_order": repair_order,
		"scope_schedule_entry": schedule_entry,
		"status": "Active",
		"issued_at": issued_dt,
		"expires_at": expires_dt,
		"last_seen_at": issued_dt,
		"issued_ip": resolved_ip,
		"last_ip": resolved_ip,
		"user_agent_hash": user_agent_hash,
		"source_otp_challenge": source_otp_challenge,
		"source_token_hash": source_token_hash,
	})
	doc.insert(ignore_permissions=True)

	if set_response_cookie:
		set_portal_session_cookie(cookie_value, max_age_seconds=max_age_seconds, settings=cfg)

	return PortalSessionInfo(
		session_id=session_id,
		status="Active",
		auth_method=auth_method,
		scope_type=scope_type,
		customer=customer,
		phone_e164=phone_e164,
		repair_order=repair_order,
		schedule_entry=schedule_entry,
		issued_at=str(issued_dt),
		expires_at=str(expires_dt),
		cookie_value=cookie_value,
	)


def set_portal_session_cookie(
	cookie_value: str,
	*,
	max_age_seconds: int,
	settings: Any = None,
) -> None:
	"""Write the signed portal session cookie via Frappe's CookieManager when available."""
	if not isinstance(cookie_value, str) or not cookie_value:
		raise ValueError("cookie_value must be a non-empty string.")

	cookie_manager = getattr(frappe.local, "cookie_manager", None)
	if not cookie_manager:
		return

	cfg = _get_settings(settings)
	cookie_name = get_portal_cookie_name(cfg)
	is_secure = should_use_secure_cookie(cfg)
	samesite = _get_cookie_samesite(cfg)
	clamped_max_age = max(cint(max_age_seconds), 0)

	cookie_kwargs: dict[str, Any] = {
		"max_age": clamped_max_age,
		"httponly": True,
		"secure": is_secure,
		"samesite": samesite,
	}
	try:
		sig = inspect.signature(cookie_manager.set_cookie)
		if "path" in sig.parameters:
			cookie_kwargs["path"] = "/"
	except (TypeError, ValueError):
		pass

	cookie_manager.set_cookie(cookie_name, cookie_value, **cookie_kwargs)


def clear_portal_session_cookie(*, settings: Any = None) -> None:
	"""Clear the portal session cookie via Frappe's CookieManager when available."""
	cookie_manager = getattr(frappe.local, "cookie_manager", None)
	if not cookie_manager:
		return

	cfg = _get_settings(settings)
	cookie_name = get_portal_cookie_name(cfg)

	pending_cookies = getattr(cookie_manager, "cookies", None)
	if isinstance(pending_cookies, dict):
		pending_cookies.pop(cookie_name, None)

	if hasattr(cookie_manager, "delete_cookie"):
		cookie_manager.delete_cookie(cookie_name)
		return

	cookie_manager.set_cookie(
		cookie_name,
		"",
		max_age=0,
		httponly=True,
		secure=should_use_secure_cookie(cfg),
		samesite=_get_cookie_samesite(cfg),
	)


def issue_portal_session(
	*,
	auth_method: str,
	scope_type: str = "customer",
	customer: str | None = None,
	phone_e164: str | None = None,
	repair_order: str | None = None,
	schedule_entry: str | None = None,
	source_otp_challenge: str | None = None,
	source_magic_token: str | None = None,
	client_ip: str | None = None,
	user_agent: str | None = None,
	set_response_cookie: bool = True,
) -> PortalSessionInfo:
	"""Issue a new persistent signed portal session and persist only SHA-256(raw_secret)."""
	if auth_method not in SESSION_AUTH_METHODS:
		raise ValueError(f"Unsupported auth_method: {auth_method!r}")
	if scope_type not in SESSION_SCOPE_TYPES:
		raise ValueError(f"Unsupported scope_type: {scope_type!r}")

	cfg = _get_settings()
	clean_customer = _clean_optional_str(customer)
	clean_ro = _clean_optional_str(repair_order)
	clean_se = _clean_optional_str(schedule_entry)
	clean_phone = _clean_optional_str(phone_e164)
	if clean_phone:
		default_cc = getattr(cfg, "sms_default_country_code", None) or "+1"
		clean_phone = normalize_phone_e164(clean_phone, default_country_code=default_cc)

	if not any((clean_customer, clean_phone, clean_ro, clean_se)):
		raise ValueError(
			"At least one binding subject (customer, phone_e164, repair_order, or schedule_entry) is required."
		)

	resolved_otp = _resolve_otp_challenge_link(source_otp_challenge)
	resolved_ua = _resolve_user_agent(user_agent)

	return _create_session_record(
		auth_method=auth_method,
		scope_type=scope_type,
		customer=clean_customer,
		phone_e164=clean_phone,
		repair_order=clean_ro,
		schedule_entry=clean_se,
		source_otp_challenge=resolved_otp,
		source_token_hash=_short_hash(source_magic_token),
		client_ip=client_ip,
		user_agent_hash=_short_hash(resolved_ua),
		set_response_cookie=set_response_cookie,
		settings=cfg,
	)


def verify_portal_session(
	cookie_value: str | None = None,
	*,
	touch: bool = True,
	required_scope_type: str | None = None,
) -> PortalSessionInfo | None:
	"""Verify signed portal session cookie against DB token_hash, status, expiry, and optional scope."""
	cfg = _get_settings()
	token = cookie_value if cookie_value is not None else _read_request_cookie(cfg)
	parsed = _parse_and_verify_cookie(token, cfg)
	if not parsed:
		return None

	session_id, raw_secret = parsed
	row = frappe.db.get_value(
		"Customer Portal Session",
		{"session_id": session_id},
		[
			"name",
			"session_id",
			"token_hash",
			"customer",
			"phone_e164",
			"auth_method",
			"scope_type",
			"scope_repair_order",
			"scope_schedule_entry",
			"status",
			"issued_at",
			"expires_at",
			"last_seen_at",
		],
		as_dict=True,
	)
	if not row:
		return None

	presented_hash = _hash_secret(raw_secret)
	if not hmac.compare_digest(presented_hash, row.token_hash or ""):
		return None

	if row.status != "Active":
		return None

	now = now_datetime()
	if now >= get_datetime(row.expires_at):
		frappe.db.set_value("Customer Portal Session", row.name, "status", "Expired", update_modified=False)
		return None

	if required_scope_type is not None and row.scope_type != required_scope_type:
		return None

	if touch:
		last_seen_dt = get_datetime(row.last_seen_at) if row.last_seen_at else None
		if last_seen_dt is None or (now - last_seen_dt).total_seconds() > LAST_SEEN_THROTTLE_SECONDS:
			frappe.db.set_value(
				"Customer Portal Session",
				row.name,
				{"last_seen_at": now, "last_ip": get_client_ip()},
				update_modified=False,
			)

	return _row_to_session_info(row, cookie_value=None)


def rotate_portal_session(
	cookie_value: str | None = None,
	*,
	customer: str | None = None,
	repair_order: str | None = None,
	schedule_entry: str | None = None,
	scope_type: str | None = None,
	set_response_cookie: bool = True,
) -> PortalSessionInfo:
	"""Atomically lock and rotate an active session, returning a fresh signed session cookie."""
	if scope_type is not None and scope_type not in SESSION_SCOPE_TYPES:
		raise ValueError(f"Unsupported scope_type: {scope_type!r}")

	cfg = _get_settings()
	token = cookie_value if cookie_value is not None else _read_request_cookie(cfg)
	parsed = _parse_and_verify_cookie(token, cfg)
	if not parsed:
		frappe.throw(_("Invalid or expired portal session."), frappe.AuthenticationError)

	session_id, raw_secret = parsed
	rows = frappe.db.sql(
		"""
		SELECT
			name,
			session_id,
			token_hash,
			customer,
			phone_e164,
			auth_method,
			scope_type,
			scope_repair_order,
			scope_schedule_entry,
			status,
			expires_at,
			source_otp_challenge,
			source_token_hash,
			user_agent_hash
		FROM `tabCustomer Portal Session`
		WHERE session_id = %s
		FOR UPDATE
		""",
		(session_id,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Invalid or expired portal session."), frappe.AuthenticationError)

	row = rows[0]
	if not hmac.compare_digest(_hash_secret(raw_secret), row.token_hash or ""):
		frappe.throw(_("Invalid or expired portal session."), frappe.AuthenticationError)

	if row.status != "Active":
		frappe.throw(_("Portal session is no longer active."), frappe.AuthenticationError)

	now = now_datetime()
	if now >= get_datetime(row.expires_at):
		frappe.db.set_value("Customer Portal Session", row.name, "status", "Expired", update_modified=False)
		frappe.throw(_("Portal session has expired."), frappe.AuthenticationError)

	frappe.db.set_value(
		"Customer Portal Session",
		row.name,
		{
			"status": "Rotated",
			"revoked_at": now,
			"revoked_reason": "Rotated",
		},
		update_modified=False,
	)

	new_scope_type = scope_type if scope_type is not None else row.scope_type
	new_customer = _clean_optional_str(customer) if customer is not None else (row.customer or None)
	new_ro = _clean_optional_str(repair_order) if repair_order is not None else (row.scope_repair_order or None)
	new_se = (
		_clean_optional_str(schedule_entry)
		if schedule_entry is not None
		else (row.scope_schedule_entry or None)
	)

	resolved_ua = _resolve_user_agent(None)
	ua_hash = _short_hash(resolved_ua) if resolved_ua else (row.user_agent_hash or None)

	return _create_session_record(
		auth_method=row.auth_method,
		scope_type=new_scope_type,
		customer=new_customer,
		phone_e164=row.phone_e164 or None,
		repair_order=new_ro,
		schedule_entry=new_se,
		source_otp_challenge=row.source_otp_challenge or None,
		source_token_hash=row.source_token_hash or None,
		client_ip=None,
		user_agent_hash=ua_hash,
		set_response_cookie=set_response_cookie,
		settings=cfg,
	)


def revoke_portal_session(
	cookie_value: str | None = None,
	*,
	session_id: str | None = None,
	reason: str = "User signed out",
	clear_response_cookie: bool = True,
) -> bool:
	"""Atomically lock and revoke a portal session by session_id or verified cookie."""
	cfg = _get_settings()
	clean_reason = (reason or "User signed out").strip()[:140]
	revoked = False

	clean_sid = _clean_optional_str(session_id)
	if clean_sid:
		rows = frappe.db.sql(
			"""
			SELECT name, status
			FROM `tabCustomer Portal Session`
			WHERE session_id = %s
			FOR UPDATE
			""",
			(clean_sid,),
			as_dict=True,
		)
		if rows and rows[0].status == "Active":
			frappe.db.set_value(
				"Customer Portal Session",
				rows[0].name,
				{
					"status": "Revoked",
					"revoked_at": now_datetime(),
					"revoked_reason": clean_reason,
				},
				update_modified=False,
			)
			revoked = True
	else:
		token = cookie_value if cookie_value is not None else _read_request_cookie(cfg)
		parsed = _parse_and_verify_cookie(token, cfg)
		if parsed:
			parsed_sid, raw_secret = parsed
			rows = frappe.db.sql(
				"""
				SELECT name, status, token_hash
				FROM `tabCustomer Portal Session`
				WHERE session_id = %s
				FOR UPDATE
				""",
				(parsed_sid,),
				as_dict=True,
			)
			if (
				rows
				and rows[0].status == "Active"
				and hmac.compare_digest(_hash_secret(raw_secret), rows[0].token_hash or "")
			):
				frappe.db.set_value(
					"Customer Portal Session",
					rows[0].name,
					{
						"status": "Revoked",
						"revoked_at": now_datetime(),
						"revoked_reason": clean_reason,
					},
					update_modified=False,
				)
				revoked = True

	if clear_response_cookie:
		clear_portal_session_cookie(settings=cfg)

	return revoked


def _bulk_revoke_rows(names: list[str], reason: str) -> int:
	if not names:
		return 0
	now = now_datetime()
	clean_reason = (reason or "Bulk revocation").strip()[:140]
	placeholders = ", ".join(["%s"] * len(names))
	frappe.db.sql(
		f"""
		UPDATE `tabCustomer Portal Session`
		SET status = 'Revoked', revoked_at = %s, revoked_reason = %s
		WHERE name IN ({placeholders})
		""",
		(now, clean_reason, *names),
	)
	return len(names)


def revoke_all_sessions_for_phone(phone_e164: str, *, reason: str = "Bulk revocation") -> int:
	"""Revoke all active portal sessions matching an E.164 phone number."""
	cfg = _get_settings()
	default_cc = getattr(cfg, "sms_default_country_code", None) or "+1"
	normalized_phone = normalize_phone_e164(phone_e164, default_country_code=default_cc)
	names = frappe.db.sql_list(
		"""
		SELECT name
		FROM `tabCustomer Portal Session`
		WHERE phone_e164 = %s AND status = 'Active'
		FOR UPDATE
		""",
		(normalized_phone,),
	)
	return _bulk_revoke_rows(list(names), reason)


def revoke_all_sessions_for_customer(customer: str, *, reason: str = "Bulk revocation") -> int:
	"""Revoke all active portal sessions linked to a Customer."""
	clean_customer = _clean_optional_str(customer)
	if not clean_customer:
		return 0
	names = frappe.db.sql_list(
		"""
		SELECT name
		FROM `tabCustomer Portal Session`
		WHERE customer = %s AND status = 'Active'
		FOR UPDATE
		""",
		(clean_customer,),
	)
	return _bulk_revoke_rows(list(names), reason)


def cleanup_expired_portal_sessions() -> int:
	"""Daily job: mark overdue Active sessions Expired and purge terminal rows older than 90 days."""
	now = now_datetime()
	cutoff = add_to_date(now, days=-SESSION_RETENTION_DAYS)

	overdue_names = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabCustomer Portal Session`
			WHERE status = 'Active' AND expires_at <= %s
			FOR UPDATE
			""",
			(now,),
		)
	)
	if overdue_names:
		placeholders = ", ".join(["%s"] * len(overdue_names))
		frappe.db.sql(
			f"""
			UPDATE `tabCustomer Portal Session`
			SET status = 'Expired'
			WHERE name IN ({placeholders})
			""",
			tuple(overdue_names),
		)

	purge_names = list(
		frappe.db.sql_list(
			"""
			SELECT name
			FROM `tabCustomer Portal Session`
			WHERE (status = 'Expired' AND expires_at < %s)
			   OR (status IN ('Revoked', 'Rotated') AND COALESCE(revoked_at, expires_at) < %s)
			FOR UPDATE
			""",
			(cutoff, cutoff),
		)
	)
	if purge_names:
		placeholders = ", ".join(["%s"] * len(purge_names))
		frappe.db.sql(
			f"""
			DELETE FROM `tabCustomer Portal Session`
			WHERE name IN ({placeholders})
			""",
			tuple(purge_names),
		)

	return len(set(overdue_names) | set(purge_names))
