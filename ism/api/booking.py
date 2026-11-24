# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
from typing import Any

import frappe

from ism.api._validation import optional_int, optional_str, require_str
from ism.booking.anti_abuse import validate_honeypot_and_nonce
from ism.booking.contracts import (
	ERR_BOOKING_DISABLED,
	ERR_SMS_UNCONFIGURED,
	VEHICLE_MIN_YEAR,
	get_booking_settings,
	is_booking_enabled,
)
from ism.booking.service import (
	get_booking_boot_config,
	get_public_booking_packages,
	get_public_booking_slots,
	initiate_booking_otp,
	verify_and_commit_booking,
)
from ism.comms.contracts import get_client_ip
from ism.comms.providers import get_sms_unconfigured_reason


def _message_log_len() -> int:
	msg_log = getattr(frappe.local, "message_log", None)
	return len(msg_log) if isinstance(msg_log, list) else 0


def _clear_thrown_messages(prior_len: int) -> None:
	msg_log = getattr(frappe.local, "message_log", None)
	if isinstance(msg_log, list) and len(msg_log) > prior_len:
		del msg_log[prior_len:]


def _finalize_response(result: dict[str, Any]) -> dict[str, Any]:
	payload = dict(result)
	status = payload.pop("http_status", None)
	if status is None:
		status = 200 if payload.get("success", True) else 400
	frappe.local.response["http_status_code"] = int(status)
	return payload


def _validation_error_response(message: str, *, error_code: str = "INVALID_INPUT") -> dict[str, Any]:
	frappe.local.response["http_status_code"] = 422
	return {
		"success": False,
		"error_code": error_code,
		"error": message,
	}


def _internal_error_response() -> dict[str, Any]:
	frappe.log_error(title="Guest Booking API Error", message=frappe.get_traceback())
	frappe.local.response["http_status_code"] = 500
	return {
		"success": False,
		"error_code": "INTERNAL_ERROR",
		"error": "An unexpected error occurred. Please try again or call the shop.",
	}


def _get_request_user_agent() -> str | None:
	if not getattr(frappe.local, "request", None):
		return None
	try:
		ua = frappe.get_request_header("User-Agent")
		return ua.strip()[:255] if isinstance(ua, str) and ua.strip() else None
	except Exception:
		return None


def _parse_package_ids_arg(package_ids: Any) -> list[str]:
	if package_ids is None or isinstance(package_ids, (bool, int, float, dict)):
		frappe.throw("At least one valid service package must be selected.", frappe.ValidationError)

	raw_items: list[str] = []
	if isinstance(package_ids, str):
		stripped = package_ids.strip()
		if not stripped:
			frappe.throw("At least one service package must be selected.", frappe.ValidationError)
		if stripped.startswith("{"):
			frappe.throw("Invalid package_ids parameter.", frappe.ValidationError)
		if stripped.startswith("["):
			try:
				parsed = json.loads(stripped)
			except json.JSONDecodeError:
				close_idx = stripped.find("]")
				prefix_tag = stripped[1:close_idx] if close_idx > 1 else ""
				is_bracket_prefixed_name = (
					close_idx > 1
					and close_idx < len(stripped) - 1
					and bool(prefix_tag.strip())
					and not any(ch in prefix_tag for ch in ('"', "'", ",", "[", "{"))
				)
				if not is_bracket_prefixed_name:
					frappe.throw("Invalid package_ids parameter.", frappe.ValidationError)
				raw_items = [part.strip() for part in stripped.split(",") if part.strip()]
			else:
				if not isinstance(parsed, list):
					frappe.throw("Invalid package_ids parameter.", frappe.ValidationError)
				for item in parsed:
					if not isinstance(item, str) or not item.strip():
						frappe.throw("Invalid package_ids parameter.", frappe.ValidationError)
					raw_items.append(item.strip())
		else:
			raw_items = [part.strip() for part in stripped.split(",") if part.strip()]
	elif isinstance(package_ids, (list, tuple)):
		for item in package_ids:
			if not isinstance(item, str) or not item.strip():
				frappe.throw("Invalid package_ids parameter.", frappe.ValidationError)
			raw_items.append(item.strip())
	else:
		frappe.throw("Invalid package_ids parameter.", frappe.ValidationError)

	deduped = list(dict.fromkeys(raw_items))
	if not deduped:
		frappe.throw("At least one service package must be selected.", frappe.ValidationError)
	return deduped


def _preflight_booking_enabled(*, require_sms: bool = False) -> dict[str, Any] | None:
	booking_settings = get_booking_settings()
	enabled, _code = is_booking_enabled(booking_settings)
	if not enabled:
		return _finalize_response(
			{
				"success": False,
				"error_code": ERR_BOOKING_DISABLED,
				"error": (
					"Online booking is currently unavailable. "
					"Please call the shop to schedule your appointment."
				),
				"http_status": 503,
			}
		)
	if require_sms and get_sms_unconfigured_reason() is not None:
		return _finalize_response(
			{
				"success": False,
				"error_code": ERR_SMS_UNCONFIGURED,
				"error": (
					"SMS verification is currently unavailable. "
					"Please call the shop to schedule your appointment."
				),
				"http_status": 503,
			}
		)
	return None


def _preflight_honeypot(website_url: Any, form_nonce: Any) -> dict[str, Any] | None:
	booking_settings = get_booking_settings()
	hp_val = str(website_url) if website_url is not None else None
	nonce_val = form_nonce if isinstance(form_nonce, str) else None
	abuse_res = validate_honeypot_and_nonce(
		honeypot_value=hp_val,
		form_nonce=nonce_val,
		settings=booking_settings,
	)
	if not abuse_res.allowed:
		return _finalize_response(
			{
				"success": False,
				"error_code": abuse_res.error_code or "BOT_DETECTED",
				"error": abuse_res.error or "Unable to process request.",
				"http_status": abuse_res.http_status,
			}
		)
	return None


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def get_booking_config() -> dict[str, Any]:
	"""Return customer-safe boot configuration and fresh form nonce for /book."""
	try:
		result = get_booking_boot_config(client_ip=get_client_ip())
		frappe.local.response["http_status_code"] = 200
		return result
	except Exception:
		return _internal_error_response()


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def get_booking_packages(
	vehicle_model: Any = None,
	vehicle_year: Any = None,
	vehicle_generation: Any = None,
	search: Any = None,
) -> dict[str, Any]:
	"""Return customer-safe curated predictable packages filtered by vehicle."""
	try:
		blocked = _preflight_booking_enabled(require_sms=False)
		if blocked is not None:
			return blocked

		prior_len = _message_log_len()
		try:
			clean_model = optional_str(vehicle_model, "vehicle_model")
			clean_year = optional_int(vehicle_year, "vehicle_year", min_val=VEHICLE_MIN_YEAR, max_val=2100)
			clean_generation = optional_str(vehicle_generation, "vehicle_generation")
			clean_search = optional_str(search, "search")
		except frappe.ValidationError as exc:
			_clear_thrown_messages(prior_len)
			return _validation_error_response(str(exc) or "Invalid query parameters.")

		result = get_public_booking_packages(
			vehicle_model=clean_model,
			vehicle_year=clean_year,
			vehicle_generation=clean_generation,
			search=clean_search,
			client_ip=get_client_ip(),
		)
		return _finalize_response(result)
	except Exception:
		return _internal_error_response()


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def get_booking_slots(
	package_ids: Any = None,
	start_date: Any = None,
	days_ahead: Any = 14,
	vehicle_model: Any = None,
	vehicle_year: Any = None,
	vehicle_generation: Any = None,
	max_slots_per_day: Any = None,
) -> dict[str, Any]:
	"""Return live capacity-backed customer arrival slots for selected packages."""
	try:
		blocked = _preflight_booking_enabled(require_sms=False)
		if blocked is not None:
			return blocked

		prior_len = _message_log_len()
		try:
			clean_package_ids = _parse_package_ids_arg(package_ids)
			clean_start_date = optional_str(start_date, "start_date")
			clean_days = optional_int(days_ahead, "days_ahead", min_val=1, max_val=60, default=14)
			clean_model = optional_str(vehicle_model, "vehicle_model")
			clean_year = optional_int(vehicle_year, "vehicle_year", min_val=VEHICLE_MIN_YEAR, max_val=2100)
			clean_generation = optional_str(vehicle_generation, "vehicle_generation")
			clean_max_slots = optional_int(max_slots_per_day, "max_slots_per_day", min_val=1, max_val=200)
		except frappe.ValidationError as exc:
			_clear_thrown_messages(prior_len)
			return _validation_error_response(str(exc) or "Invalid slot search parameters.")

		result = get_public_booking_slots(
			package_ids=clean_package_ids,
			start_date=clean_start_date,
			days_ahead=clean_days if clean_days is not None else 14,
			vehicle_model=clean_model,
			vehicle_year=clean_year,
			vehicle_generation=clean_generation,
			max_slots_per_day=clean_max_slots,
			client_ip=get_client_ip(),
		)
		return _finalize_response(result)
	except Exception:
		return _internal_error_response()


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_booking_otp(
	package_ids: Any = None,
	target_date: Any = None,
	customer_arrival_time: Any = None,
	customer_name: Any = None,
	phone: Any = None,
	email: Any = None,
	vehicle_model: Any = None,
	vehicle_year: Any = None,
	vehicle_generation: Any = None,
	vin_or_plate: Any = None,
	notes: Any = None,
	waiter_preference: Any = None,
	website_url: Any = None,
	form_nonce: Any = None,
) -> dict[str, Any]:
	"""Validate booking intake and dispatch a pre-commit SMS OTP verification code."""
	try:
		blocked = _preflight_booking_enabled(require_sms=True)
		if blocked is not None:
			return blocked

		hp_blocked = _preflight_honeypot(website_url, form_nonce)
		if hp_blocked is not None:
			return hp_blocked

		prior_len = _message_log_len()
		try:
			clean_package_ids = _parse_package_ids_arg(package_ids)
			clean_target_date = require_str(target_date, "target_date")
			clean_arrival_time = require_str(customer_arrival_time, "customer_arrival_time")
			clean_name = require_str(customer_name, "customer_name")
			clean_phone = require_str(phone, "phone")
			clean_email = optional_str(email, "email")
			clean_model = optional_str(vehicle_model, "vehicle_model")
			clean_year = optional_int(vehicle_year, "vehicle_year", min_val=VEHICLE_MIN_YEAR, max_val=2100)
			clean_generation = optional_str(vehicle_generation, "vehicle_generation")
			clean_vin_or_plate = optional_str(vin_or_plate, "vin_or_plate")
			clean_notes = optional_str(notes, "notes")
			clean_waiter = optional_str(waiter_preference, "waiter_preference")
			clean_nonce = optional_str(form_nonce, "form_nonce")
			clean_hp = optional_str(website_url, "website_url")
		except frappe.ValidationError as exc:
			_clear_thrown_messages(prior_len)
			return _validation_error_response(str(exc) or "Invalid booking request parameters.")

		result = initiate_booking_otp(
			package_ids=clean_package_ids,
			target_date=clean_target_date,
			customer_arrival_time=clean_arrival_time,
			customer_name=clean_name,
			phone=clean_phone,
			email=clean_email,
			vehicle_model=clean_model,
			vehicle_year=clean_year,
			vehicle_generation=clean_generation,
			vin_or_plate=clean_vin_or_plate,
			notes=clean_notes,
			waiter_preference=clean_waiter,
			honeypot_value=clean_hp,
			form_nonce=clean_nonce,
			client_ip=get_client_ip(),
			user_agent=_get_request_user_agent(),
		)
		return _finalize_response(result)
	except Exception:
		return _internal_error_response()


@frappe.whitelist(allow_guest=True, methods=["POST"])
def confirm_self_serve_booking(
	challenge_id: Any = None,
	otp_code: Any = None,
	package_ids: Any = None,
	target_date: Any = None,
	customer_arrival_time: Any = None,
	customer_name: Any = None,
	phone: Any = None,
	email: Any = None,
	vehicle_model: Any = None,
	vehicle_year: Any = None,
	vehicle_generation: Any = None,
	vin_or_plate: Any = None,
	notes: Any = None,
	waiter_preference: Any = None,
	website_url: Any = None,
	form_nonce: Any = None,
	idempotency_key: Any = None,
) -> dict[str, Any]:
	"""Verify SMS OTP, commit provisional Schedule Entry, set portal cookie, and send confirmations."""
	try:
		blocked = _preflight_booking_enabled(require_sms=True)
		if blocked is not None:
			return blocked

		hp_blocked = _preflight_honeypot(website_url, form_nonce)
		if hp_blocked is not None:
			return hp_blocked

		prior_len = _message_log_len()
		try:
			clean_challenge_id = require_str(challenge_id, "challenge_id")
			clean_otp = optional_str(otp_code, "otp_code")
			clean_package_ids = _parse_package_ids_arg(package_ids)
			clean_target_date = require_str(target_date, "target_date")
			clean_arrival_time = require_str(customer_arrival_time, "customer_arrival_time")
			clean_name = require_str(customer_name, "customer_name")
			clean_phone = require_str(phone, "phone")
			clean_email = optional_str(email, "email")
			clean_model = optional_str(vehicle_model, "vehicle_model")
			clean_year = optional_int(vehicle_year, "vehicle_year", min_val=VEHICLE_MIN_YEAR, max_val=2100)
			clean_generation = optional_str(vehicle_generation, "vehicle_generation")
			clean_vin_or_plate = optional_str(vin_or_plate, "vin_or_plate")
			clean_notes = optional_str(notes, "notes")
			clean_waiter = optional_str(waiter_preference, "waiter_preference")
			clean_nonce = optional_str(form_nonce, "form_nonce")
			clean_hp = optional_str(website_url, "website_url")
			clean_idem = optional_str(idempotency_key, "idempotency_key")
		except frappe.ValidationError as exc:
			_clear_thrown_messages(prior_len)
			return _validation_error_response(str(exc) or "Invalid booking confirmation parameters.")

		result = verify_and_commit_booking(
			challenge_id=clean_challenge_id,
			otp_code=clean_otp,
			package_ids=clean_package_ids,
			target_date=clean_target_date,
			customer_arrival_time=clean_arrival_time,
			customer_name=clean_name,
			phone=clean_phone,
			email=clean_email,
			vehicle_model=clean_model,
			vehicle_year=clean_year,
			vehicle_generation=clean_generation,
			vin_or_plate=clean_vin_or_plate,
			notes=clean_notes,
			waiter_preference=clean_waiter,
			honeypot_value=clean_hp,
			form_nonce=clean_nonce,
			idempotency_key=clean_idem,
			client_ip=get_client_ip(),
			user_agent=_get_request_user_agent(),
			set_response_cookie=True,
		)
		return _finalize_response(result)
	except Exception:
		return _internal_error_response()
