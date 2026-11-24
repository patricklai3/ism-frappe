# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import frappe
from frappe.utils import escape_html, now_datetime

from ism.booking.anti_abuse import (
	check_phone_daily_limit_and_duplicate,
	enforce_booking_ip_rate_limit,
	get_idempotent_booking_result,
	issue_booking_form_nonce,
	record_phone_daily_booking,
	store_idempotent_booking_result,
	validate_honeypot_and_nonce,
)
from ism.booking.contracts import (
	ALLOWED_VEHICLE_MODELS,
	BOOKING_OTP_PURPOSE,
	BOOKING_READ_IP_RATE_LIMIT_PER_HOUR,
	BOOKING_SESSION_AUTH_METHOD,
	BOOKING_SESSION_SCOPE_TYPE,
	BOOKING_SOURCE,
	ERR_BOOKING_DISABLED,
	ERR_EMAIL_REQUIRED,
	ERR_MAX_PACKAGES_EXCEEDED,
	ERR_SLOT_UNAVAILABLE,
	ERR_SMS_UNCONFIGURED,
	HONEYPOT_FIELD_NAME,
	PORTAL_ROUTE,
	VEHICLE_MIN_YEAR,
	BookingConfigSnapshot,
	BookingConfirmationPayload,
	get_booking_settings,
	get_shop_branding,
	is_booking_enabled,
	resolve_booking_pin_state,
	resolve_booking_portal_link,
)
from ism.comms.contracts import SMSDispatchRequest, get_client_ip
from ism.comms.email_dispatch import dispatch_transactional_email
from ism.comms.otp import (
	consume_verified_otp_challenge,
	request_sms_otp,
	verify_sms_otp,
)
from ism.comms.providers import get_sms_unconfigured_reason, send_sms
from ism.comms.sessions import issue_portal_session
from ism.scheduling.arrival_policy import (
	to_customer_arrival_time,
	to_internal_slot_start,
)
from ism.scheduling.bookable_catalog import (
	compute_packages_booking_requirements,
	list_bookable_packages,
)
from ism.scheduling.self_serve import (
	create_provisional_booking,
	find_self_serve_slots,
	get_self_serve_policy,
	normalize_provisional_intake,
)
from ism.scheduling.time_utils import normalize_date
from ism.utils.tesla_manual import get_generation_catalog

_COMMIT_SAVEPOINT_NAME: str = "p5_booking_commit"
_DEFAULT_CURRENCY: str = "CAD"
_SLOT_CONFLICT_REASON_CODES: frozenset[str] = frozenset(
	{
		"capacity_conflict",
		"outside_horizon",
		"lead_time_not_met",
		"shop_holiday",
		"outside_operating_hours",
		"invalid_slot_time",
		"break_window",
		"exceeds_operating_hours",
	}
)

__all__ = [
	"get_booking_boot_config",
	"get_public_booking_packages",
	"get_public_booking_slots",
	"initiate_booking_otp",
	"verify_and_commit_booking",
]


def _message_log_len() -> int:
	msg_log = getattr(frappe.local, "message_log", None)
	return len(msg_log) if isinstance(msg_log, list) else 0


def _clear_thrown_messages(prior_len: int) -> None:
	msg_log = getattr(frappe.local, "message_log", None)
	if isinstance(msg_log, list) and len(msg_log) > prior_len:
		del msg_log[prior_len:]


def _error_result(
	*,
	error_code: str,
	error: str,
	http_status: int,
	**extra: Any,
) -> dict[str, Any]:
	payload: dict[str, Any] = {
		"success": False,
		"error_code": error_code,
		"error": error,
		"http_status": http_status,
	}
	for key, val in extra.items():
		if val is not None:
			payload[key] = val
	return payload


def _resolve_user_agent(user_agent: str | None) -> str | None:
	if isinstance(user_agent, str) and user_agent.strip():
		return user_agent.strip()[:255]
	if user_agent is None and getattr(frappe.local, "request", None):
		try:
			header_ua = frappe.get_request_header("User-Agent")
			if isinstance(header_ua, str) and header_ua.strip():
				return header_ua.strip()[:255]
		except Exception:
			return None
	return None


def _rollback_savepoint_safe(savepoint_name: str) -> None:
	try:
		frappe.db.rollback(save_point=savepoint_name)
	except Exception:
		pass


def _release_savepoint_safe(savepoint_name: str) -> None:
	if hasattr(frappe.db, "release_savepoint"):
		try:
			frappe.db.release_savepoint(savepoint_name)
		except Exception:
			pass


@contextmanager
def _booking_commit_user_context() -> Iterator[None]:
	"""Run Schedule Entry validation/insert under Administrator for verified Guest bookings.

	ScheduleEntry.validate() calls check_bay_availability and get_technician_pool_availability,
	which check Schedule Entry / Service Bay read permissions against frappe.session.user even
	when doc.insert(ignore_permissions=True) is used. Restores the exact prior user on exit.
	"""
	prev_user = getattr(frappe.session, "user", None)
	switched = bool(prev_user and prev_user != "Administrator")
	if switched:
		frappe.set_user("Administrator")
	try:
		yield
	finally:
		if switched and prev_user:
			frappe.set_user(prev_user)


def _parse_and_validate_package_ids(
	package_ids: Sequence[str] | str | None,
	*,
	settings: BookingConfigSnapshot,
) -> tuple[list[str] | None, dict[str, Any] | None]:
	if package_ids is None or isinstance(package_ids, (bool, int, float, dict)):
		return None, _error_result(
			error_code="INVALID_INPUT",
			error="At least one service package must be selected.",
			http_status=422,
		)

	raw_items: list[str] = []
	if isinstance(package_ids, str):
		stripped = package_ids.strip()
		if not stripped:
			return None, _error_result(
				error_code="INVALID_INPUT",
				error="At least one service package must be selected.",
				http_status=422,
			)
		if stripped.startswith(("[", "{")):
			try:
				parsed = json.loads(stripped)
			except (ValueError, TypeError):
				return None, _error_result(
					error_code="INVALID_INPUT",
					error="Invalid service package selection.",
					http_status=422,
				)
			if not isinstance(parsed, list):
				return None, _error_result(
					error_code="INVALID_INPUT",
					error="Invalid service package selection.",
					http_status=422,
				)
			for item in parsed:
				if not isinstance(item, str) or not item.strip():
					return None, _error_result(
						error_code="INVALID_INPUT",
						error="Invalid service package selection.",
						http_status=422,
					)
				raw_items.append(item.strip())
		else:
			raw_items = [part.strip() for part in stripped.split(",") if part.strip()]
	elif isinstance(package_ids, (list, tuple)):
		for item in package_ids:
			if not isinstance(item, str) or not item.strip():
				return None, _error_result(
					error_code="INVALID_INPUT",
					error="Invalid service package selection.",
					http_status=422,
				)
			raw_items.append(item.strip())
	else:
		return None, _error_result(
			error_code="INVALID_INPUT",
			error="Invalid service package selection.",
			http_status=422,
		)

	deduped = list(dict.fromkeys(raw_items))
	if not deduped:
		return None, _error_result(
			error_code="INVALID_INPUT",
			error="At least one service package must be selected.",
			http_status=422,
		)

	if len(deduped) > settings.max_packages_per_booking:
		return None, _error_result(
			error_code=ERR_MAX_PACKAGES_EXCEEDED,
			error=(
				f"You can select up to {settings.max_packages_per_booking} "
				"service packages per appointment."
			),
			http_status=422,
		)

	return deduped, None


def _phone_lookup_candidates(phone_e164: str) -> list[str]:
	candidates = [phone_e164]
	if phone_e164.startswith("+1") and len(phone_e164) == 12:
		digits = phone_e164[2:]
		candidates.extend(
			[
				digits,
				f"1{digits}",
				f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
				f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
				f"{digits[:3]}.{digits[3:6]}.{digits[6:]}",
				f"+1 {digits[:3]}-{digits[3:6]}-{digits[6:]}",
				f"1-{digits[:3]}-{digits[3:6]}-{digits[6:]}",
			]
		)
	return list(dict.fromkeys(candidates))


def _find_unique_customer_by_phone(phone_e164: str) -> str | None:
	"""Best-effort internal Customer resolution; returns Customer.name ONLY on unambiguous single match."""
	if not isinstance(phone_e164, str) or not phone_e164.strip():
		return None

	candidates = _phone_lookup_candidates(phone_e164.strip())
	matched_customers: set[str] = set()
	try:
		direct_rows = frappe.get_all(
			"Customer",
			filters={"mobile_no": ["in", candidates]},
			pluck="name",
			limit=2,
			ignore_permissions=True,
		)
		for name in direct_rows:
			if name:
				matched_customers.add(str(name))
				if len(matched_customers) > 1:
					return None

		contact_names: set[str] = set()
		for fieldname in ("mobile_no", "phone"):
			rows = frappe.get_all(
				"Contact",
				filters={fieldname: ["in", candidates]},
				pluck="name",
				limit=10,
				ignore_permissions=True,
			)
			for cname in rows:
				if cname:
					contact_names.add(str(cname))

		if frappe.db.exists("DocType", "Contact Phone"):
			phone_rows = frappe.get_all(
				"Contact Phone",
				filters={"phone": ["in", candidates]},
				pluck="parent",
				limit=10,
				ignore_permissions=True,
			)
			for cname in phone_rows:
				if cname:
					contact_names.add(str(cname))

		if contact_names:
			links = frappe.get_all(
				"Dynamic Link",
				filters={
					"parenttype": "Contact",
					"parent": ["in", list(contact_names)],
					"link_doctype": "Customer",
				},
				pluck="link_name",
				limit=5,
				ignore_permissions=True,
			)
			for cust_name in links:
				if cust_name:
					matched_customers.add(str(cust_name))
					if len(matched_customers) > 1:
						return None
	except Exception:
		return None

	if len(matched_customers) == 1:
		return next(iter(matched_customers))
	return None


def _build_confirmation_email_html(
	*,
	shop_name: str,
	shop_phone: str,
	shop_email: str,
	customer_name: str,
	booking_reference: str,
	display_datetime: str,
	vehicle_display: str,
	packages: Sequence[dict[str, Any]],
	waiter_preference: str,
	direct_portal_url: str,
) -> str:
	safe_shop = escape_html(shop_name)
	safe_shop_phone = escape_html(shop_phone)
	safe_shop_email = escape_html(shop_email)
	safe_customer = escape_html(customer_name)
	safe_ref = escape_html(booking_reference)
	safe_dt = escape_html(display_datetime)
	safe_vehicle = escape_html(vehicle_display)
	safe_waiter = escape_html(waiter_preference)
	safe_url = escape_html(direct_portal_url)

	pkg_items_html: list[str] = []
	for pkg in packages:
		title = escape_html(str(pkg.get("title") or ""))
		price_label = escape_html(str(pkg.get("price_display_label") or ""))
		if price_label:
			pkg_items_html.append(
				f"<li style='margin:6px 0;'><strong>{title}</strong> &mdash; {price_label}</li>"
			)
		else:
			pkg_items_html.append(f"<li style='margin:6px 0;'><strong>{title}</strong></li>")
	packages_list = "".join(pkg_items_html) or "<li>Scheduled Service</li>"

	contact_parts: list[str] = []
	if safe_shop_phone:
		contact_parts.append(f"Phone: {safe_shop_phone}")
	if safe_shop_email:
		contact_parts.append(f"Email: {safe_shop_email}")
	contact_footer = " &bull; ".join(contact_parts)

	return (
		"<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;"
		"max-width:600px;margin:0 auto;color:#1a202c;border:1px solid #e2e8f0;border-radius:8px;"
		"overflow:hidden;'>"
		"<div style='background:#1a202c;color:#ffffff;padding:20px 24px;'>"
		f"<h2 style='margin:0;font-size:20px;'>{safe_shop}</h2>"
		"<p style='margin:6px 0 0;color:#cbd5e0;font-size:14px;'>Appointment Confirmation</p>"
		"</div>"
		"<div style='padding:24px;background:#ffffff;'>"
		f"<p style='margin:0 0 16px;'>Hi {safe_customer},</p>"
		"<p style='margin:0 0 20px;'>Your service appointment is confirmed. Here are your details:</p>"
		"<div style='background:#f7fafc;border:1px solid #e2e8f0;border-radius:6px;padding:16px;"
		"margin-bottom:20px;'>"
		f"<p style='margin:0 0 8px;'><strong>Appointment Time:</strong> {safe_dt}</p>"
		f"<p style='margin:0 0 8px;'><strong>Reference:</strong> {safe_ref}</p>"
		f"<p style='margin:0 0 8px;'><strong>Vehicle:</strong> {safe_vehicle}</p>"
		f"<p style='margin:0;'><strong>Visit Preference:</strong> {safe_waiter}</p>"
		"</div>"
		"<p style='margin:0 0 8px;'><strong>Selected Services:</strong></p>"
		f"<ul style='margin:0 0 24px;padding-left:20px;'>{packages_list}</ul>"
		"<div style='text-align:center;margin:24px 0;'>"
		f"<a href='{safe_url}' style='display:inline-block;background:#2f855a;color:#ffffff;"
		"text-decoration:none;padding:12px 24px;border-radius:6px;font-weight:600;'>"
		"Manage Your Appointment</a>"
		"</div>"
		"<p style='margin:20px 0 0;font-size:13px;color:#4a5568;'>"
		"Need to reschedule or view appointment status? Use the button above to open the Customer Portal."
		"</p>"
		+ (
			f"<p style='margin:12px 0 0;font-size:13px;color:#718096;'>{contact_footer}</p>"
			if contact_footer
			else ""
		)
		+ "</div></div>"
	)


def _map_otp_request_error(otp_res: Any) -> dict[str, Any]:
	code = str(getattr(otp_res, "error_code", None) or "DELIVERY_FAILED")
	if code in ("LOCKED_OUT", "RATE_LIMITED", "COOLDOWN_ACTIVE"):
		http_status = 429
	elif code == "OPTED_OUT":
		http_status = 422
	else:
		http_status = 503

	resend_in = int(getattr(otp_res, "resend_available_in_seconds", 0) or 0)
	return _error_result(
		error_code=code,
		error=str(getattr(otp_res, "error", None) or "Unable to send verification code."),
		http_status=http_status,
		resend_available_in_seconds=resend_in if resend_in > 0 else None,
		retry_after_seconds=resend_in if resend_in > 0 else None,
		locked_until=getattr(otp_res, "locked_until", None),
	)


def _map_otp_verify_error(verify_res: Any) -> dict[str, Any]:
	code = str(getattr(verify_res, "error_code", None) or "OTP_INVALID")
	error_msg = str(getattr(verify_res, "error", None) or "Verification code is invalid.")
	if code == "INVALID_CODE":
		return _error_result(
			error_code="INVALID_CODE",
			error=error_msg,
			http_status=400,
			attempts_remaining=int(getattr(verify_res, "attempts_remaining", 0) or 0),
		)
	if code == "LOCKED_OUT":
		return _error_result(
			error_code="LOCKED_OUT",
			error=error_msg,
			http_status=429,
			attempts_remaining=0,
			locked_until=getattr(verify_res, "locked_until", None),
		)
	if code == "EXPIRED":
		return _error_result(
			error_code="EXPIRED",
			error=error_msg,
			http_status=410,
		)
	if code == "ALREADY_USED":
		return _error_result(
			error_code="OTP_ALREADY_USED",
			error=error_msg,
			http_status=409,
		)
	if code == "PHONE_MISMATCH":
		return _error_result(
			error_code="PHONE_MISMATCH",
			error=error_msg,
			http_status=400,
		)
	if code == "PURPOSE_MISMATCH":
		return _error_result(
			error_code="PURPOSE_MISMATCH",
			error=error_msg,
			http_status=400,
		)
	return _error_result(
		error_code="OTP_INVALID",
		error=error_msg,
		http_status=400,
	)


def _map_otp_consume_error(consume_res: Any) -> dict[str, Any]:
	code = str(getattr(consume_res, "error_code", None) or "OTP_INVALID")
	error_msg = str(getattr(consume_res, "error", None) or "Verification challenge is no longer valid.")
	if code == "ALREADY_CONSUMED":
		return _error_result(
			error_code="OTP_ALREADY_USED",
			error=error_msg,
			http_status=409,
		)
	if code == "GRACE_EXPIRED":
		return _error_result(
			error_code="EXPIRED",
			error=error_msg,
			http_status=410,
		)
	if code == "PHONE_MISMATCH":
		return _error_result(
			error_code="PHONE_MISMATCH",
			error=error_msg,
			http_status=400,
		)
	if code == "PURPOSE_MISMATCH":
		return _error_result(
			error_code="PURPOSE_MISMATCH",
			error=error_msg,
			http_status=400,
		)
	return _error_result(
		error_code="OTP_INVALID",
		error=error_msg,
		http_status=400,
	)


def get_booking_boot_config(
	*,
	client_ip: str | None = None,
	now_ts: int | None = None,
) -> dict[str, Any]:
	"""Return strictly sanitized customer-safe boot configuration for /book."""
	del client_ip  # Reserved for telemetry parity; boot config carries zero PII.
	booking_settings = get_booking_settings()
	p3_policy = get_self_serve_policy()
	branding = get_shop_branding()
	sms_ready = get_sms_unconfigured_reason() is None

	if not booking_settings.enable_self_serve_booking:
		enabled = False
		disabled_reason: str | None = ERR_BOOKING_DISABLED
	elif not sms_ready:
		enabled = False
		disabled_reason = ERR_SMS_UNCONFIGURED
	else:
		enabled = True
		disabled_reason = None

	form_nonce = issue_booking_form_nonce(now_ts=now_ts) if enabled else None

	return {
		"enabled": enabled,
		"disabled_reason": disabled_reason,
		"title": booking_settings.title,
		"intro": booking_settings.intro,
		"shop": branding,
		"policy": {
			"max_packages_per_booking": booking_settings.max_packages_per_booking,
			"require_email": booking_settings.require_email,
			"enable_waiter_preference": bool(p3_policy["enable_waiter_preference"]),
			"horizon_days": int(p3_policy["self_serve_horizon_days"]),
			"min_lead_time_minutes": int(p3_policy["self_serve_min_lead_time_minutes"]),
			"currency": _DEFAULT_CURRENCY,
		},
		"vehicle_models": list(ALLOWED_VEHICLE_MODELS),
		"vehicle_generations": get_generation_catalog(),
		"vehicle_year_min": VEHICLE_MIN_YEAR,
		"vehicle_year_max": now_datetime().year + 1,
		"honeypot_enabled": booking_settings.honeypot_enabled,
		"honeypot_field": HONEYPOT_FIELD_NAME,
		"form_nonce": form_nonce,
		"portal_url": PORTAL_ROUTE,
	}


def get_public_booking_packages(
	*,
	vehicle_model: str | None = None,
	vehicle_year: int | None = None,
	vehicle_generation: str | None = None,
	search: str | None = None,
	client_ip: str | None = None,
) -> dict[str, Any]:
	"""Return customer-safe predictable booking packages guarded by feature toggle and IP quota."""
	booking_settings = get_booking_settings()
	enabled, _disabled_code = is_booking_enabled(booking_settings)
	if not enabled:
		return _error_result(
			error_code=ERR_BOOKING_DISABLED,
			error="Online booking is currently unavailable. Please call the shop to schedule your appointment.",
			http_status=503,
		)

	resolved_ip = get_client_ip(client_ip)
	ip_rl = enforce_booking_ip_rate_limit(
		"read",
		client_ip=resolved_ip,
		limit=BOOKING_READ_IP_RATE_LIMIT_PER_HOUR,
	)
	if not ip_rl.allowed:
		return _error_result(
			error_code=ip_rl.error_code or "RATE_LIMITED",
			error=ip_rl.error or "Too many requests. Please try again later.",
			http_status=ip_rl.http_status,
			retry_after_seconds=ip_rl.retry_after_seconds or None,
		)

	prior_len = _message_log_len()
	try:
		packages = list_bookable_packages(
			vehicle_model=vehicle_model,
			vehicle_year=vehicle_year,
			vehicle_generation=vehicle_generation,
			search=search,
		)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Invalid vehicle filter parameters.",
			http_status=422,
		)

	return {
		"success": True,
		"packages": packages,
		"max_packages_per_booking": booking_settings.max_packages_per_booking,
		"currency": _DEFAULT_CURRENCY,
	}


def get_public_booking_slots(
	*,
	package_ids: Sequence[str] | str,
	start_date: str | None = None,
	days_ahead: int = 14,
	vehicle_model: str | None = None,
	vehicle_year: int | None = None,
	vehicle_generation: str | None = None,
	max_slots_per_day: int | None = None,
	client_ip: str | None = None,
	now_dt: datetime | None = None,
) -> dict[str, Any]:
	"""Return strictly sanitized customer arrival slots for selected self-serve packages."""
	booking_settings = get_booking_settings()
	enabled, _disabled_code = is_booking_enabled(booking_settings)
	if not enabled:
		return _error_result(
			error_code=ERR_BOOKING_DISABLED,
			error="Online booking is currently unavailable. Please call the shop to schedule your appointment.",
			http_status=503,
		)

	resolved_ip = get_client_ip(client_ip)
	ip_rl = enforce_booking_ip_rate_limit(
		"read",
		client_ip=resolved_ip,
		limit=BOOKING_READ_IP_RATE_LIMIT_PER_HOUR,
	)
	if not ip_rl.allowed:
		return _error_result(
			error_code=ip_rl.error_code or "RATE_LIMITED",
			error=ip_rl.error or "Too many requests. Please try again later.",
			http_status=ip_rl.http_status,
			retry_after_seconds=ip_rl.retry_after_seconds or None,
		)

	deduped_ids, pkg_err = _parse_and_validate_package_ids(package_ids, settings=booking_settings)
	if pkg_err is not None or deduped_ids is None:
		return pkg_err or _error_result(
			error_code="INVALID_INPUT",
			error="At least one service package must be selected.",
			http_status=422,
		)

	if isinstance(days_ahead, bool) or not isinstance(days_ahead, int) or days_ahead <= 0:
		return _error_result(
			error_code="INVALID_INPUT",
			error="days_ahead must be a positive integer.",
			http_status=422,
		)
	if max_slots_per_day is not None and (
		isinstance(max_slots_per_day, bool)
		or not isinstance(max_slots_per_day, int)
		or max_slots_per_day <= 0
	):
		return _error_result(
			error_code="INVALID_INPUT",
			error="max_slots_per_day must be a positive integer.",
			http_status=422,
		)

	prior_len = _message_log_len()
	try:
		slot_result = find_self_serve_slots(
			package_ids=deduped_ids,
			start_date=start_date,
			days_ahead=days_ahead,
			vehicle_model=vehicle_model,
			vehicle_year=vehicle_year,
			vehicle_generation=vehicle_generation,
			max_slots_per_day=max_slots_per_day,
			now_dt=now_dt,
		)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Unable to search available slots with the provided parameters.",
			http_status=422,
		)

	return {"success": True, **slot_result}


def initiate_booking_otp(
	*,
	package_ids: Sequence[str] | str,
	target_date: str,
	customer_arrival_time: str,
	customer_name: str,
	phone: str,
	email: str | None = None,
	vehicle_model: str | None = None,
	vehicle_year: int | None = None,
	vehicle_color: str | None = None,
	license_plate: str | None = None,
	vin: str | None = None,
	vehicle_generation: str | None = None,
	vin_or_plate: str | None = None,
	notes: str | None = None,
	waiter_preference: str | None = None,
	honeypot_value: str | None = None,
	form_nonce: str | None = None,
	client_ip: str | None = None,
	user_agent: str | None = None,
	now_dt: datetime | None = None,
	now_ts: int | None = None,
) -> dict[str, Any]:
	"""Run anti-abuse, intake, daily/duplicate, and slot checks before dispatching SMS OTP."""
	booking_settings = get_booking_settings()
	enabled, _disabled_code = is_booking_enabled(booking_settings)
	if not enabled:
		return _error_result(
			error_code=ERR_BOOKING_DISABLED,
			error="Online booking is currently unavailable. Please call the shop to schedule your appointment.",
			http_status=503,
		)

	if get_sms_unconfigured_reason() is not None:
		return _error_result(
			error_code=ERR_SMS_UNCONFIGURED,
			error=(
				"SMS verification is currently unavailable. "
				"Please call the shop to schedule your appointment."
			),
			http_status=503,
		)

	abuse_res = validate_honeypot_and_nonce(
		honeypot_value=honeypot_value,
		form_nonce=form_nonce,
		settings=booking_settings,
		now_ts=now_ts,
	)
	if not abuse_res.allowed:
		return _error_result(
			error_code=abuse_res.error_code or "BOT_DETECTED",
			error=abuse_res.error or "Unable to process request.",
			http_status=abuse_res.http_status,
		)

	resolved_ip = get_client_ip(client_ip)
	resolved_ua = _resolve_user_agent(user_agent)
	ip_rl = enforce_booking_ip_rate_limit(
		"otp",
		client_ip=resolved_ip,
		limit=booking_settings.ip_rate_limit_per_hour,
	)
	if not ip_rl.allowed:
		return _error_result(
			error_code=ip_rl.error_code or "RATE_LIMITED",
			error=ip_rl.error or "Too many booking requests. Please wait and try again.",
			http_status=ip_rl.http_status,
			retry_after_seconds=ip_rl.retry_after_seconds or None,
		)

	deduped_ids, pkg_err = _parse_and_validate_package_ids(package_ids, settings=booking_settings)
	if pkg_err is not None or deduped_ids is None:
		return pkg_err or _error_result(
			error_code="INVALID_INPUT",
			error="At least one service package must be selected.",
			http_status=422,
		)

	prior_len = _message_log_len()
	try:
		normalized = normalize_provisional_intake(
			customer_name=customer_name,
			phone=phone,
			email=email,
			vehicle_model=vehicle_model,
			vehicle_year=vehicle_year,
			vehicle_color=vehicle_color,
			license_plate=license_plate,
			vin=vin,
			vehicle_generation=vehicle_generation,
			vin_or_plate=vin_or_plate,
			notes=notes,
			waiter_preference=waiter_preference,
		)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Invalid customer or vehicle details.",
			http_status=422,
		)

	if booking_settings.require_email and not normalized["email"]:
		return _error_result(
			error_code=ERR_EMAIL_REQUIRED,
			error="Please provide a valid email address to book an appointment.",
			http_status=422,
		)

	prior_len = _message_log_len()
	try:
		compute_packages_booking_requirements(
			deduped_ids,
			vehicle_model=normalized["vehicle_model"],
			vehicle_year=normalized["vehicle_year"],
			vehicle_generation=normalized["vehicle_generation"],
		)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Selected service packages are not available for this vehicle.",
			http_status=422,
		)

	iso_date = normalize_date(target_date)
	if not iso_date:
		return _error_result(
			error_code="INVALID_INPUT",
			error="Please select a valid appointment date.",
			http_status=422,
		)

	prior_len = _message_log_len()
	try:
		internal_start_time = to_internal_slot_start(customer_arrival_time)
		canonical_arrival_time = to_customer_arrival_time(internal_start_time)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Please select a valid appointment time.",
			http_status=422,
		)

	phone_guard = check_phone_daily_limit_and_duplicate(
		phone_e164=normalized["phone"],
		target_date=iso_date,
		internal_start_time=internal_start_time,
		settings=booking_settings,
		now_dt=now_dt,
	)
	if not phone_guard.allowed:
		return _error_result(
			error_code=phone_guard.error_code or "RATE_LIMITED",
			error=phone_guard.error or "Unable to schedule another appointment right now.",
			http_status=phone_guard.http_status,
		)

	prior_len = _message_log_len()
	try:
		slots_check = find_self_serve_slots(
			package_ids=deduped_ids,
			start_date=iso_date,
			days_ahead=1,
			vehicle_model=normalized["vehicle_model"],
			vehicle_year=normalized["vehicle_year"],
			vehicle_generation=normalized["vehicle_generation"],
			now_dt=now_dt,
		)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Unable to verify appointment availability.",
			http_status=422,
		)

	slot_available = False
	for day in slots_check.get("days") or []:
		if day.get("date") != iso_date:
			continue
		for slot in day.get("slots") or []:
			if slot.get("customer_arrival_time") == canonical_arrival_time:
				slot_available = True
				break
		if slot_available:
			break

	if not slot_available:
		return _error_result(
			error_code=ERR_SLOT_UNAVAILABLE,
			error="That appointment time is no longer available. Please select another available time.",
			http_status=409,
		)

	try:
		otp_res = request_sms_otp(
			normalized["phone"],
			purpose=BOOKING_OTP_PURPOSE,
			client_ip=resolved_ip,
			user_agent=resolved_ua,
		)
	except ValueError as exc:
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Please provide a valid mobile phone number.",
			http_status=422,
		)

	if not otp_res.success:
		return _map_otp_request_error(otp_res)

	return {
		"success": True,
		"challenge_id": otp_res.challenge_id,
		"phone_masked": otp_res.phone_masked,
		"expires_in_seconds": otp_res.expires_in_seconds,
		"resend_available_in_seconds": otp_res.resend_available_in_seconds,
	}


def verify_and_commit_booking(
	*,
	challenge_id: str,
	otp_code: str | None = None,
	package_ids: Sequence[str] | str,
	target_date: str,
	customer_arrival_time: str,
	customer_name: str,
	phone: str,
	email: str | None = None,
	vehicle_model: str | None = None,
	vehicle_year: int | None = None,
	vehicle_color: str | None = None,
	license_plate: str | None = None,
	vin: str | None = None,
	vehicle_generation: str | None = None,
	vin_or_plate: str | None = None,
	notes: str | None = None,
	waiter_preference: str | None = None,
	honeypot_value: str | None = None,
	form_nonce: str | None = None,
	idempotency_key: str | None = None,
	client_ip: str | None = None,
	user_agent: str | None = None,
	set_response_cookie: bool = True,
	now_dt: datetime | None = None,
	now_ts: int | None = None,
) -> dict[str, Any]:
	"""Verify SMS OTP, atomically commit provisional Schedule Entry, promote cookie, and send receipts."""
	booking_settings = get_booking_settings()
	enabled, _disabled_code = is_booking_enabled(booking_settings)
	if not enabled:
		return _error_result(
			error_code=ERR_BOOKING_DISABLED,
			error="Online booking is currently unavailable. Please call the shop to schedule your appointment.",
			http_status=503,
		)

	if get_sms_unconfigured_reason() is not None:
		return _error_result(
			error_code=ERR_SMS_UNCONFIGURED,
			error=(
				"SMS verification is currently unavailable. "
				"Please call the shop to schedule your appointment."
			),
			http_status=503,
		)

	abuse_res = validate_honeypot_and_nonce(
		honeypot_value=honeypot_value,
		form_nonce=form_nonce,
		settings=booking_settings,
		now_ts=now_ts,
	)
	if not abuse_res.allowed:
		return _error_result(
			error_code=abuse_res.error_code or "BOT_DETECTED",
			error=abuse_res.error or "Unable to process request.",
			http_status=abuse_res.http_status,
		)

	resolved_ip = get_client_ip(client_ip)
	resolved_ua = _resolve_user_agent(user_agent)
	ip_rl = enforce_booking_ip_rate_limit(
		"commit",
		client_ip=resolved_ip,
		limit=booking_settings.ip_rate_limit_per_hour,
	)
	if not ip_rl.allowed:
		return _error_result(
			error_code=ip_rl.error_code or "RATE_LIMITED",
			error=ip_rl.error or "Too many booking requests. Please wait and try again.",
			http_status=ip_rl.http_status,
			retry_after_seconds=ip_rl.retry_after_seconds or None,
		)

	clean_challenge_id = challenge_id.strip() if isinstance(challenge_id, str) else ""
	if not clean_challenge_id:
		return _error_result(
			error_code="INVALID_INPUT",
			error="Verification challenge is required.",
			http_status=422,
		)

	deduped_ids, pkg_err = _parse_and_validate_package_ids(package_ids, settings=booking_settings)
	if pkg_err is not None or deduped_ids is None:
		return pkg_err or _error_result(
			error_code="INVALID_INPUT",
			error="At least one service package must be selected.",
			http_status=422,
		)

	prior_len = _message_log_len()
	try:
		normalized = normalize_provisional_intake(
			customer_name=customer_name,
			phone=phone,
			email=email,
			vehicle_model=vehicle_model,
			vehicle_year=vehicle_year,
			vehicle_color=vehicle_color,
			license_plate=license_plate,
			vin=vin,
			vehicle_generation=vehicle_generation,
			vin_or_plate=vin_or_plate,
			notes=notes,
			waiter_preference=waiter_preference,
		)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Invalid customer or vehicle details.",
			http_status=422,
		)

	if booking_settings.require_email and not normalized["email"]:
		return _error_result(
			error_code=ERR_EMAIL_REQUIRED,
			error="Please provide a valid email address to book an appointment.",
			http_status=422,
		)

	iso_date = normalize_date(target_date)
	if not iso_date:
		return _error_result(
			error_code="INVALID_INPUT",
			error="Please select a valid appointment date.",
			http_status=422,
		)

	prior_len = _message_log_len()
	try:
		internal_start_time = to_internal_slot_start(customer_arrival_time)
	except frappe.ValidationError as exc:
		_clear_thrown_messages(prior_len)
		return _error_result(
			error_code="INVALID_INPUT",
			error=str(exc) or "Please select a valid appointment time.",
			http_status=422,
		)

	phone_e164 = normalized["phone"]

	# Double-click idempotency replay before re-touching OTP or DB.
	cached_payload = get_idempotent_booking_result(
		idempotency_key,
		phone_e164=phone_e164,
		challenge_id=clean_challenge_id,
	)
	if cached_payload is not None:
		return cached_payload

	# Step 4: Pre-commit SMS OTP verification BEFORE opening the booking savepoint
	# so Verified status survives a slot-conflict savepoint rollback.
	clean_otp = otp_code.strip() if isinstance(otp_code, str) and otp_code.strip() else None
	if clean_otp is not None:
		verify_res = verify_sms_otp(
			clean_challenge_id,
			clean_otp,
			phone=phone_e164,
			purpose=BOOKING_OTP_PURPOSE,
			client_ip=resolved_ip,
			consume=False,
		)
		if not verify_res.verified:
			return _map_otp_verify_error(verify_res)

	# Step 5: Atomic single-use OTP consume + provisional Schedule Entry commit inside savepoint.
	frappe.db.savepoint(_COMMIT_SAVEPOINT_NAME)
	try:
		consume_res = consume_verified_otp_challenge(
			clean_challenge_id,
			expected_purpose=BOOKING_OTP_PURPOSE,
			expected_phone=phone_e164,
		)
		if not consume_res.verified:
			_rollback_savepoint_safe(_COMMIT_SAVEPOINT_NAME)
			return _map_otp_consume_error(consume_res)

		phone_guard = check_phone_daily_limit_and_duplicate(
			phone_e164=phone_e164,
			target_date=iso_date,
			internal_start_time=internal_start_time,
			settings=booking_settings,
			now_dt=now_dt,
		)
		if not phone_guard.allowed:
			_rollback_savepoint_safe(_COMMIT_SAVEPOINT_NAME)
			return _error_result(
				error_code=phone_guard.error_code or "RATE_LIMITED",
				error=phone_guard.error or "Unable to schedule another appointment right now.",
				http_status=phone_guard.http_status,
			)

		prior_len = _message_log_len()
		try:
			with _booking_commit_user_context():
				booking_result = create_provisional_booking(
					package_ids=deduped_ids,
					target_date=iso_date,
					customer_arrival_time=customer_arrival_time,
					customer_name=normalized["customer_name"],
					phone=phone_e164,
					email=normalized["email"],
					vehicle_model=normalized["vehicle_model"],
					vehicle_year=normalized["vehicle_year"],
					vehicle_color=normalized["vehicle_color"],
					license_plate=normalized["license_plate"],
					vin=normalized["vin"],
					vehicle_generation=normalized["vehicle_generation"],
					vin_or_plate=normalized["vin_or_plate"],
					notes=normalized["notes"],
					waiter_preference=normalized["waiter_preference"],
					booking_source=BOOKING_SOURCE,
					customer=None,
					repair_vehicle=None,
					now_dt=now_dt,
					emit_notification=True,
				)
		except frappe.ValidationError as exc:
			_clear_thrown_messages(prior_len)
			_rollback_savepoint_safe(_COMMIT_SAVEPOINT_NAME)
			reason_code = getattr(exc, "reason_code", None)
			if reason_code in _SLOT_CONFLICT_REASON_CODES:
				return _error_result(
					error_code=ERR_SLOT_UNAVAILABLE,
					error="That appointment time was just booked. Please select another available time.",
					http_status=409,
					otp_verified=True,
					challenge_id=clean_challenge_id,
				)
			return _error_result(
				error_code="INVALID_INPUT",
				error=str(exc) or "Unable to complete booking with the provided details.",
				http_status=422,
				otp_verified=True,
				challenge_id=clean_challenge_id,
			)
	except Exception:
		_rollback_savepoint_safe(_COMMIT_SAVEPOINT_NAME)
		raise
	else:
		_release_savepoint_safe(_COMMIT_SAVEPOINT_NAME)

	booking_reference: str = str(booking_result["booking_reference"])
	appointment: dict[str, Any] = dict(booking_result["appointment"])
	packages: list[dict[str, Any]] = list(booking_result.get("packages") or [])
	vehicle_display: str = str(booking_result.get("vehicle_display") or "")
	waiter_pref: str = str(booking_result.get("waiter_preference") or normalized["waiter_preference"])

	# Step 6: Post-commit rate-limit bookkeeping & persistent portal cookie promotion.
	record_phone_daily_booking(phone_e164)

	challenge_doc_name = frappe.db.get_value(
		"Portal OTP Challenge",
		{"challenge_id": clean_challenge_id},
		"name",
	)
	matched_customer = _find_unique_customer_by_phone(phone_e164)

	session_info = issue_portal_session(
		auth_method=BOOKING_SESSION_AUTH_METHOD,
		scope_type=BOOKING_SESSION_SCOPE_TYPE,
		customer=matched_customer,
		phone_e164=phone_e164,
		schedule_entry=booking_reference,
		source_otp_challenge=challenge_doc_name,
		client_ip=resolved_ip,
		user_agent=resolved_ua,
		set_response_cookie=set_response_cookie,
	)

	# Step 7: Non-fatal confirmation SMS & Email using ONLY P3 customer arrival display.
	branding = get_shop_branding()
	shop_name = branding.get("name") or "Our Service Center"
	shop_name_single_line = " ".join(shop_name.splitlines()).strip() or "Our Service Center"
	display_datetime = str(appointment["display_datetime"])
	vehicle_label = vehicle_display or "your vehicle"
	direct_portal_url, has_tokenized_link = resolve_booking_portal_link(booking_reference)

	sms_sent = False
	try:
		sms_body = (
			f"{shop_name_single_line}: Your appointment for {vehicle_label} is confirmed for "
			f"{display_datetime} (Ref: {booking_reference}). "
			f"Manage or reschedule: {direct_portal_url}"
		)
		sms_res = send_sms(
			SMSDispatchRequest(
				to_phone=phone_e164,
				body=sms_body,
				message_type="Booking Confirmation",
				reference_doctype="Schedule Entry",
				reference_name=booking_reference,
				redact_body=False,
				respect_opt_out=True,
			)
		)
		sms_sent = bool(sms_res.success)
	except Exception:
		frappe.log_error(
			title="Booking Confirmation SMS Failed",
			message=frappe.get_traceback(),
		)

	email_sent = False
	recipient_email = normalized.get("email")
	if recipient_email:
		try:
			email_html = _build_confirmation_email_html(
				shop_name=shop_name_single_line,
				shop_phone=branding.get("phone") or "",
				shop_email=branding.get("email") or "",
				customer_name=normalized["customer_name"],
				booking_reference=booking_reference,
				display_datetime=display_datetime,
				vehicle_display=vehicle_label,
				packages=packages,
				waiter_preference=waiter_pref,
				direct_portal_url=direct_portal_url,
			)
			email_res = dispatch_transactional_email(
				recipient=recipient_email,
				subject=f"Appointment Confirmed - {display_datetime} ({shop_name_single_line})",
				html_body=email_html,
				email_type="booking_confirmation",
				reference_doctype="Schedule Entry",
				reference_name=booking_reference,
				client_ip=resolved_ip,
				bypass_rate_limit=False,
			)
			email_sent = bool(email_res.success)
		except Exception:
			frappe.log_error(
				title="Booking Confirmation Email Failed",
				message=frappe.get_traceback(),
			)

	# Step 8: Strictly sanitized response & idempotency cache (zero internal IDs/cookie/OTP).
	pin_state = resolve_booking_pin_state(booking_reference)
	confirmation = BookingConfirmationPayload(
		success=True,
		booking_reference=booking_reference,
		appointment=appointment,
		packages=packages,
		vehicle_display=vehicle_display,
		waiter_preference=waiter_pref,
		portal_handoff={
			"portal_url": PORTAL_ROUTE,
			"session_established": bool(session_info and session_info.status == "Active"),
			"scope_type": BOOKING_SESSION_SCOPE_TYPE,
			"has_tokenized_link": has_tokenized_link,
		},
		notifications={
			"sms_sent": sms_sent,
			"email_sent": email_sent,
			"email_requested": bool(recipient_email),
		},
		shop=branding,
		pin_to_drive=pin_state,
		vehicle_info=booking_result.get("vehicle_info") or normalized.get("vehicle_info"),
	)
	payload = confirmation.to_dict()
	store_idempotent_booking_result(
		idempotency_key,
		phone_e164=phone_e164,
		challenge_id=clean_challenge_id,
		payload=payload,
	)
	return payload
