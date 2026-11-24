# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import ipaddress
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import frappe

SUPPORTED_SMS_PROVIDERS: tuple[str, ...] = (
	"Disabled",
	"Telnyx",
	"Custom HTTP Webhook",
	"Mock (Deterministic Test)",
)
TELNYX_MESSAGES_ENDPOINT: str = "https://api.telnyx.com/v2/messages"
OTP_PURPOSES: tuple[str, ...] = ("portal_login", "booking_verify", "portal_action")
SESSION_AUTH_METHODS: tuple[str, ...] = ("sms_otp", "magic_link", "booking_otp")
SESSION_SCOPE_TYPES: tuple[str, ...] = ("customer", "repair_order", "booking")
DEFAULT_PORTAL_COOKIE_NAME: str = "ism_portal_session"

STOP_KEYWORDS: frozenset[str] = frozenset(
	{"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPTOUT"}
)
START_KEYWORDS: frozenset[str] = frozenset({"START", "UNSTOP", "YES"})
HELP_KEYWORDS: frozenset[str] = frozenset({"HELP", "INFO"})
MULTIWORD_KEYWORD_CANONICALS: dict[str, str] = {
	"OPT OUT": "OPTOUT",
	"STOP ALL": "STOPALL",
}
TELNYX_OPT_OUT_ERROR_CODES: frozenset[str] = frozenset({"40300", "40008"})

_KEYWORD_NON_ALNUM_SPACE_RE = re.compile(r"[^A-Z0-9\s]+")
_WHITESPACE_RUN_RE = re.compile(r"\s+")

_BLOCKED_SMS_HOSTS: frozenset[str] = frozenset({"metadata.google.internal", "169.254.169.254"})
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_COUNTRY_CODE_RE = re.compile(r"^\+[1-9]\d{0,2}$")
_ALLOWED_PHONE_CHARS_RE = re.compile(r"^[\d\s+\-().]+$")
_NANP_10_RE = re.compile(r"^[2-9]\d{2}[2-9]\d{6}$")
_OTP_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")


@dataclass(frozen=True)
class SMSDispatchRequest:
	to_phone: str
	body: str
	message_type: str = "Notification"
	reference_doctype: str | None = None
	reference_name: str | None = None
	redact_body: bool = False
	redacted_preview: str | None = None
	respect_opt_out: bool = True

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class SMSDispatchResult:
	success: bool
	provider: str
	status: str
	message_id: str | None = None
	audit_log: str | None = None
	phone_e164: str | None = None
	error: str | None = None
	error_code: str | None = None
	opted_out: bool = False

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class OTPChallengeResult:
	success: bool
	challenge_id: str | None = None
	phone_masked: str | None = None
	expires_in_seconds: int = 0
	resend_available_in_seconds: int = 0
	locked_until: str | None = None
	error: str | None = None
	error_code: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class OTPVerifyResult:
	verified: bool
	challenge_id: str | None = None
	phone_e164: str | None = None
	purpose: str | None = None
	attempts_remaining: int = 0
	locked_until: str | None = None
	error: str | None = None
	error_code: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class PortalSessionInfo:
	session_id: str
	status: str
	auth_method: str
	scope_type: str
	customer: str | None = None
	phone_e164: str | None = None
	repair_order: str | None = None
	schedule_entry: str | None = None
	issued_at: str | None = None
	expires_at: str | None = None
	cookie_value: str | None = None

	def to_dict(self, *, include_cookie: bool = False) -> dict[str, Any]:
		data = asdict(self)
		if not include_cookie:
			data.pop("cookie_value", None)
		return data


@dataclass(frozen=True)
class EmailDispatchResult:
	success: bool
	recipient: str
	email_type: str
	rate_limited: bool = False
	retry_after_seconds: int = 0
	error: str | None = None
	error_code: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


class SMSProvider(Protocol):
	provider_name: str

	def validate_config(self, settings: Any) -> str | None: ...

	def send(self, phone_e164: str, body: str, settings: Any, *, http_post: Any = None) -> dict[str, Any]: ...


def normalize_inbound_sms_keyword(raw_text: str | None) -> str:
	"""Normalize full trimmed inbound SMS body for exact TCPA/CTIA keyword matching."""
	trimmed = str(raw_text or "").strip().upper()
	if not trimmed:
		return ""
	cleaned = _KEYWORD_NON_ALNUM_SPACE_RE.sub("", trimmed)
	collapsed = _WHITESPACE_RUN_RE.sub(" ", cleaned).strip()
	if not collapsed:
		return ""
	return MULTIWORD_KEYWORD_CANONICALS.get(collapsed, collapsed)


def normalize_phone_e164(raw_phone: str, default_country_code: str = "+1") -> str:
	"""Normalize a phone number string into strict ITU-T E.164 format."""
	if not isinstance(raw_phone, str):
		raise ValueError("Phone number must be a string.")

	cc = (default_country_code or "+1").strip()
	if not _COUNTRY_CODE_RE.match(cc):
		raise ValueError(f"Invalid default country code: {default_country_code!r}")

	if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_phone):
		raise ValueError("Phone number contains invalid control characters.")

	text = raw_phone.strip()
	if text.lower().startswith("tel:"):
		text = text[4:].strip()

	if not text:
		raise ValueError("Phone number is required.")

	if not _ALLOWED_PHONE_CHARS_RE.match(text):
		raise ValueError("Phone number contains invalid characters.")

	if "+" in text[1:]:
		raise ValueError("Plus sign is only allowed at the start of a phone number.")

	cleaned = re.sub(r"[\s\-().]+", "", text)
	if not cleaned:
		raise ValueError("Phone number is required.")

	if cleaned.startswith("00"):
		candidate = "+" + cleaned[2:]
		if not _E164_RE.match(candidate):
			raise ValueError(f"Invalid international phone number: {raw_phone!r}")
		return candidate

	if cleaned.startswith("+"):
		if not _E164_RE.match(cleaned):
			raise ValueError(f"Invalid E.164 phone number: {raw_phone!r}")
		return cleaned

	if not cleaned.isdigit():
		raise ValueError(f"Invalid phone number: {raw_phone!r}")

	if cc == "+1":
		if len(cleaned) == 10:
			if not _NANP_10_RE.match(cleaned):
				raise ValueError(f"Invalid North American phone number: {raw_phone!r}")
			return f"+1{cleaned}"
		if len(cleaned) == 11 and cleaned.startswith("1"):
			national = cleaned[1:]
			if not _NANP_10_RE.match(national):
				raise ValueError(f"Invalid North American phone number: {raw_phone!r}")
			return f"+1{national}"
		raise ValueError(f"Invalid North American phone number: {raw_phone!r}")

	candidate = f"{cc}{cleaned.lstrip('0')}"
	if not _E164_RE.match(candidate):
		raise ValueError(f"Invalid phone number: {raw_phone!r}")
	return candidate


def mask_phone_e164(phone_e164: str) -> str:
	"""Mask an E.164 phone number, preserving country prefix and last 4 digits."""
	text = (phone_e164 or "").strip()
	if not text:
		return ""
	if len(text) <= 4:
		return "*" * len(text)

	if text.startswith("+"):
		if text.startswith(("+1", "+7")) or len(text) < 8:
			prefix = text[:2]
		else:
			prefix = text[:3]
		hidden_len = max(len(text) - len(prefix) - 4, 2)
		return f"{prefix}{'*' * hidden_len}{text[-4:]}"

	return f"{'*' * (len(text) - 4)}{text[-4:]}"


def redact_otp_body(body: str, preview_override: str | None = None) -> str:
	"""Redact any 4-8 consecutive digit runs from an SMS body or preview override."""
	source = preview_override.strip() if isinstance(preview_override, str) and preview_override.strip() else ""
	if not source:
		source = body.strip() if isinstance(body, str) else ""
	if not source:
		return "[REDACTED]"
	return _OTP_DIGIT_RUN_RE.sub("[REDACTED]", source)


def is_valid_sms_webhook_url(endpoint: str) -> bool:
	"""Allow HTTPS webhook URLs, permitting plain HTTP only on loopback hosts for local dev/test."""
	if not endpoint or any(ch.isspace() for ch in endpoint):
		return False

	parsed = urlparse(endpoint)
	scheme = (parsed.scheme or "").lower()
	hostname = (parsed.hostname or "").strip().lower()
	if scheme not in ("http", "https") or not hostname:
		return False
	if parsed.username or parsed.password:
		return False
	if hostname in _BLOCKED_SMS_HOSTS:
		return False

	is_loopback = hostname in ("localhost", "localhost.localdomain")
	try:
		ip = ipaddress.ip_address(hostname)
	except ValueError:
		ip = None

	if ip is not None:
		if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
			return False
		is_loopback = ip.is_loopback

	if scheme == "http" and not is_loopback:
		return False
	return True


def get_client_ip(ip_override: str | None = None) -> str:
	"""Resolve client IP from request context (ignoring untrusted overrides when HTTP request is active)."""
	if getattr(frappe.local, "request", None):
		raw_ip = (
			getattr(frappe.local, "request_ip", None)
			or frappe.get_request_header("X-Forwarded-For")
			or getattr(frappe.local.request, "remote_addr", None)
		)
	else:
		raw_ip = (
			ip_override.strip()
			if isinstance(ip_override, str) and ip_override.strip()
			else getattr(frappe.local, "request_ip", None)
		)
	if isinstance(raw_ip, str) and raw_ip.strip():
		return raw_ip.split(",")[0].strip()
	return "unknown"
