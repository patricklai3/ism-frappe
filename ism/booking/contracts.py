# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from dataclasses import asdict, dataclass
from typing import Any

import frappe
from frappe.utils import cint

BOOKING_ROUTE: str = "/book"
PORTAL_ROUTE: str = "/portal"
BOOKING_OTP_PURPOSE: str = "booking_verify"
BOOKING_SESSION_AUTH_METHOD: str = "booking_otp"
BOOKING_SESSION_SCOPE_TYPE: str = "booking"
BOOKING_SOURCE: str = "Website Self-Serve"

ALLOWED_VEHICLE_MODELS: tuple[str, ...] = (
	"Model 3",
	"Model Y",
	"Model S",
	"Model X",
	"Cybertruck",
	"Roadster",
)
VEHICLE_MIN_YEAR: int = 2008
MIN_FORM_FILL_SECONDS: int = 2
MAX_FORM_NONCE_AGE_SECONDS: int = 4 * 3600
BOOKING_READ_IP_RATE_LIMIT_PER_HOUR: int = 60
BOOKING_IDEMPOTENCY_TTL_SECONDS: int = 600
HONEYPOT_FIELD_NAME: str = "website_url"

DEFAULT_BOOKING_TITLE: str = "Book Your Service Appointment"
DEFAULT_BOOKING_INTRO: str = (
	"Select your vehicle and service package to view live drop-off times and book online."
)
DEFAULT_MAX_PACKAGES_PER_BOOKING: int = 5
DEFAULT_IP_RATE_LIMIT_PER_HOUR: int = 15
DEFAULT_PHONE_RATE_LIMIT_PER_DAY: int = 3
DEFAULT_MAX_CONCURRENT_BOOKINGS_PER_PHONE: int = 2

# Canonical structured error codes returned by ism.booking & ism.api.booking
ERR_BOOKING_DISABLED: str = "BOOKING_DISABLED"
ERR_SMS_UNCONFIGURED: str = "SMS_UNCONFIGURED"
ERR_BOT_DETECTED: str = "BOT_DETECTED"
ERR_SUBMISSION_TOO_FAST: str = "SUBMISSION_TOO_FAST"
ERR_FORM_EXPIRED: str = "FORM_EXPIRED"
ERR_RATE_LIMITED: str = "RATE_LIMITED"
ERR_PHONE_DAILY_LIMIT: str = "PHONE_DAILY_LIMIT"
ERR_DUPLICATE_BOOKING: str = "DUPLICATE_BOOKING"
ERR_VEHICLE_CONFLICT: str = "VEHICLE_CONFLICT"
ERR_SLOT_UNAVAILABLE: str = "SLOT_UNAVAILABLE"
ERR_EMAIL_REQUIRED: str = "EMAIL_REQUIRED"
ERR_MAX_PACKAGES_EXCEEDED: str = "MAX_PACKAGES_EXCEEDED"

__all__ = [
	"ALLOWED_VEHICLE_MODELS",
	"BOOKING_IDEMPOTENCY_TTL_SECONDS",
	"BOOKING_OTP_PURPOSE",
	"BOOKING_READ_IP_RATE_LIMIT_PER_HOUR",
	"BOOKING_ROUTE",
	"BOOKING_SESSION_AUTH_METHOD",
	"BOOKING_SESSION_SCOPE_TYPE",
	"BOOKING_SOURCE",
	"DEFAULT_BOOKING_INTRO",
	"DEFAULT_BOOKING_TITLE",
	"DEFAULT_IP_RATE_LIMIT_PER_HOUR",
	"DEFAULT_MAX_CONCURRENT_BOOKINGS_PER_PHONE",
	"DEFAULT_MAX_PACKAGES_PER_BOOKING",
	"DEFAULT_PHONE_RATE_LIMIT_PER_DAY",
	"ERR_BOOKING_DISABLED",
	"ERR_BOT_DETECTED",
	"ERR_DUPLICATE_BOOKING",
	"ERR_EMAIL_REQUIRED",
	"ERR_FORM_EXPIRED",
	"ERR_MAX_PACKAGES_EXCEEDED",
	"ERR_PHONE_DAILY_LIMIT",
	"ERR_RATE_LIMITED",
	"ERR_SLOT_UNAVAILABLE",
	"ERR_SMS_UNCONFIGURED",
	"ERR_SUBMISSION_TOO_FAST",
	"ERR_VEHICLE_CONFLICT",
	"HONEYPOT_FIELD_NAME",
	"MAX_FORM_NONCE_AGE_SECONDS",
	"MIN_FORM_FILL_SECONDS",
	"PORTAL_ROUTE",
	"VEHICLE_MIN_YEAR",
	"BookingAntiAbuseResult",
	"BookingConfigSnapshot",
	"BookingConfirmationPayload",
	"get_booking_settings",
	"get_shop_branding",
	"is_booking_enabled",
	"resolve_booking_pin_state",
	"resolve_booking_portal_link",
]


@dataclass(frozen=True)
class BookingConfigSnapshot:
	enable_self_serve_booking: bool
	title: str
	intro: str
	max_packages_per_booking: int
	require_email: bool
	ip_rate_limit_per_hour: int
	phone_rate_limit_per_day: int
	honeypot_enabled: bool
	max_concurrent_bookings_per_phone: int = DEFAULT_MAX_CONCURRENT_BOOKINGS_PER_PHONE

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class BookingAntiAbuseResult:
	allowed: bool
	error_code: str | None = None
	error: str | None = None
	http_status: int = 200
	retry_after_seconds: int = 0

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class BookingConfirmationPayload:
	success: bool
	booking_reference: str
	appointment: dict[str, Any]
	packages: list[dict[str, Any]]
	vehicle_display: str
	waiter_preference: str
	portal_handoff: dict[str, Any]
	notifications: dict[str, Any]
	shop: dict[str, Any]
	pin_to_drive: dict[str, Any] | None = None
	vehicle_info: dict[str, Any] | None = None

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


def _clamp_int(raw: Any, *, default: int, minimum: int, maximum: int) -> int:
	if raw is None or raw == "":
		return default
	val = cint(raw)
	if val < minimum:
		return minimum
	if val > maximum:
		return maximum
	return val


def get_booking_settings(settings: Any = None) -> BookingConfigSnapshot:
	"""Read sb_self_serve_booking fields from ISM Settings with safe bounds."""
	cfg = settings if settings is not None else frappe.get_single("ISM Settings")

	title = str(cfg.get("self_serve_booking_title") or "").strip() or DEFAULT_BOOKING_TITLE
	intro = str(cfg.get("self_serve_booking_intro") or "").strip() or DEFAULT_BOOKING_INTRO

	raw_honeypot = cfg.get("self_serve_honeypot_enabled")
	honeypot_enabled = True if raw_honeypot is None else bool(cint(raw_honeypot))

	return BookingConfigSnapshot(
		enable_self_serve_booking=bool(cint(cfg.get("enable_self_serve_booking") or 0)),
		title=title[:140],
		intro=intro,
		max_packages_per_booking=_clamp_int(
			cfg.get("self_serve_max_packages_per_booking"),
			default=DEFAULT_MAX_PACKAGES_PER_BOOKING,
			minimum=1,
			maximum=10,
		),
		require_email=bool(cint(cfg.get("self_serve_require_email") or 0)),
		ip_rate_limit_per_hour=_clamp_int(
			cfg.get("self_serve_booking_ip_rate_limit_per_hour"),
			default=DEFAULT_IP_RATE_LIMIT_PER_HOUR,
			minimum=1,
			maximum=500,
		),
		phone_rate_limit_per_day=_clamp_int(
			cfg.get("self_serve_booking_phone_rate_limit_per_day"),
			default=DEFAULT_PHONE_RATE_LIMIT_PER_DAY,
			minimum=1,
			maximum=20,
		),
		honeypot_enabled=honeypot_enabled,
		max_concurrent_bookings_per_phone=_clamp_int(
			cfg.get("max_concurrent_bookings_per_phone"),
			default=DEFAULT_MAX_CONCURRENT_BOOKINGS_PER_PHONE,
			minimum=1,
			maximum=10,
		),
	)


def is_booking_enabled(settings: Any = None) -> tuple[bool, str | None]:
	"""Return (True, None) only when enable_self_serve_booking == 1; else (False, BOOKING_DISABLED)."""
	cfg = settings if isinstance(settings, BookingConfigSnapshot) else get_booking_settings(settings)
	if cfg.enable_self_serve_booking:
		return True, None
	return False, ERR_BOOKING_DISABLED


def get_shop_branding() -> dict[str, str]:
	"""Resolve customer-safe shop branding from default ERPNext Company without internal fields."""
	try:
		company_name = frappe.defaults.get_global_default("company")
		company = None
		if company_name:
			company = frappe.db.get_value(
				"Company",
				company_name,
				["company_name", "phone_no", "email"],
				as_dict=True,
			)
		if not company:
			rows = frappe.get_all(
				"Company",
				fields=["company_name", "phone_no", "email"],
				order_by="creation asc",
				limit=1,
			)
			company = rows[0] if rows else None

		if not company:
			return {"name": "", "phone": "", "email": ""}

		return {
			"name": str(company.get("company_name") or "").strip(),
			"phone": str(company.get("phone_no") or "").strip(),
			"email": str(company.get("email") or "").strip(),
		}
	except Exception:
		return {"name": "", "phone": "", "email": ""}


def resolve_booking_portal_link(schedule_entry: str) -> tuple[str, bool]:
	"""Resolve a direct booking portal link via soft P4 import, falling back cleanly to /portal."""
	fallback_url = frappe.utils.get_url(PORTAL_ROUTE)
	try:
		from ism.portal.auth import create_booking_portal_link
	except ImportError:
		return fallback_url, False

	try:
		raw_link = create_booking_portal_link(schedule_entry)
		if isinstance(raw_link, dict):
			raw_link = raw_link.get("url") or raw_link.get("portal_url")

		if not isinstance(raw_link, str) or not raw_link.strip():
			return fallback_url, False

		cleaned = raw_link.strip()
		if cleaned.startswith(("http://", "https://")):
			return cleaned, True
		return frappe.utils.get_url(cleaned), True
	except Exception:
		frappe.log_error(
			title="Booking Portal Link Resolution Failed",
			message=frappe.get_traceback(),
		)
		return fallback_url, False


def resolve_booking_pin_state(schedule_entry: str) -> dict[str, Any] | None:
	"""Resolve customer-safe PIN-to-Drive prompt state via soft P4 import for appointment confirmation."""
	try:
		from ism.portal.pin_vault import get_schedule_entry_pin_customer_state
	except ImportError:
		return None

	try:
		state = get_schedule_entry_pin_customer_state(schedule_entry)
		return dict(state) if isinstance(state, dict) else None
	except Exception:
		frappe.log_error(
			title="Booking PIN State Resolution Failed",
			message=frappe.get_traceback(),
		)
		return None
