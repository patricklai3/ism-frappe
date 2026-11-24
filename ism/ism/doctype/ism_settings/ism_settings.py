# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from ism.comms.providers import get_sms_unconfigured_reason
from ism.payments.contracts import (
	SQUARE_CURRENCY,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_PRODUCTION,
	SQUARE_ENV_SANDBOX,
	SQUARE_ENVIRONMENTS,
	assert_cad_currency,
)
from ism.portal.contracts import (
	PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS,
	PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS,
)
from ism.scheduling.time_utils import to_timedelta

_DEFAULT_COUNTRY_CODE_RE = re.compile(r"^\+\d{1,3}$")
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


class ISMSettings(Document):
	def before_validate(self):
		self._normalize_sms_settings()

	def validate(self):
		self._validate_operating_hours()
		self._validate_break_hours()
		self._validate_estimation_parameters()
		self._validate_pricing_parameters()
		self._validate_comms_and_otp_settings()
		self._validate_square_settings()
		self._validate_customer_portal_parameters()
		self._validate_self_serve_booking_settings()

	def _normalize_sms_settings(self):
		# Twilio support was removed; coerce legacy values so existing sites save cleanly.
		if not self.sms_gateway_provider or self.sms_gateway_provider == "Twilio":
			self.sms_gateway_provider = "Disabled"

	def _validate_operating_hours(self):
		if self.operating_hours_start and self.operating_hours_end:
			start = to_timedelta(self.operating_hours_start)
			end = to_timedelta(self.operating_hours_end)
			# 00:00:00 - 00:00:00 marks the shop closed for scheduling tests and off-days.
			if not (start == end == to_timedelta("00:00:00")) and start >= end:
				frappe.throw(_("Operating Hours Start must be before Operating Hours End."))

		if self.default_slot_interval is not None and self.default_slot_interval <= 0:
			frappe.throw(_("Default Slot Interval must be greater than 0 minutes."))

		if self.scheduling_horizon_days is not None and self.scheduling_horizon_days <= 0:
			frappe.throw(_("Scheduling Horizon must be greater than 0 days."))

	def _validate_break_hours(self):
		if self.break_start and self.break_end:
			b_start = to_timedelta(self.break_start)
			b_end = to_timedelta(self.break_end)
			if b_start >= b_end:
				frappe.throw(_("Break Start must be before Break End."))

			if self.operating_hours_start and self.operating_hours_end:
				op_start = to_timedelta(self.operating_hours_start)
				op_end = to_timedelta(self.operating_hours_end)
				if op_start == op_end == to_timedelta("00:00:00"):
					return
				if b_start < op_start or b_end > op_end:
					frappe.throw(_("Lunch break window must be within shop operating hours."))

	def _validate_estimation_parameters(self):
		if self.estimation_percentile is not None:
			# Auto-convert decimal fraction (e.g. 0.80) to percentage (80.0)
			if 0 < self.estimation_percentile <= 1.0:
				self.estimation_percentile = self.estimation_percentile * 100.0

			if self.estimation_percentile < 50.0 or self.estimation_percentile > 99.0:
				frappe.throw(_("Estimation Percentile must be between 50% and 99%."))

		if self.estimation_sigma is not None:
			if self.estimation_sigma < 0.05 or self.estimation_sigma > 1.0:
				frappe.throw(_("Estimation Sigma must be between 0.05 and 1.00."))

		if self.buffer_padding_minutes is not None and self.buffer_padding_minutes < 0:
			frappe.throw(_("Buffer Padding cannot be negative."))

		if self.default_diagnostic_duration is not None and self.default_diagnostic_duration <= 0:
			frappe.throw(_("Default Diagnostic Duration must be greater than 0 minutes."))

	def _validate_pricing_parameters(self):
		if self.default_labor_rate is not None and flt(self.default_labor_rate) <= 0:
			frappe.throw(_("Default Labor Rate must be greater than 0."))

	def _validate_comms_and_otp_settings(self):
		if self.sms_default_country_code is not None and str(self.sms_default_country_code).strip():
			cc = str(self.sms_default_country_code).strip()
			self.sms_default_country_code = cc
			if not _DEFAULT_COUNTRY_CODE_RE.match(cc):
				frappe.throw(_("Default Country Code must match +<1-3 digits> (e.g. +1)."))

		if self.telnyx_webhook_tolerance_seconds is not None:
			tol = cint(self.telnyx_webhook_tolerance_seconds)
			if tol < 30 or tol > 3600:
				frappe.throw(_("Webhook Replay Tolerance must be between 30 and 3600 seconds."))

		if self.otp_code_length is not None:
			length = cint(self.otp_code_length)
			if length < 4 or length > 8:
				frappe.throw(_("OTP Code Length must be between 4 and 8 digits."))

		if self.otp_ttl_seconds is not None:
			ttl = cint(self.otp_ttl_seconds)
			if ttl < 60 or ttl > 900:
				frappe.throw(_("OTP Expiry TTL must be between 60 and 900 seconds."))

		if self.otp_resend_cooldown_seconds is not None:
			cooldown = cint(self.otp_resend_cooldown_seconds)
			if cooldown < 15 or cooldown > 300:
				frappe.throw(_("OTP Resend Cooldown must be between 15 and 300 seconds."))

		if self.otp_max_verify_attempts is not None:
			max_attempts = cint(self.otp_max_verify_attempts)
			if max_attempts < 1 or max_attempts > 10:
				frappe.throw(_("OTP Max Verification Attempts must be between 1 and 10."))

		if self.otp_lockout_seconds is not None:
			lockout = cint(self.otp_lockout_seconds)
			if lockout < 60 or lockout > 86400:
				frappe.throw(_("OTP Lockout Window must be between 60 and 86400 seconds."))

		if self.otp_phone_rate_limit_per_hour is not None and cint(self.otp_phone_rate_limit_per_hour) <= 0:
			frappe.throw(_("OTP Per-Phone Rate Limit must be greater than 0."))

		if self.otp_ip_rate_limit_per_hour is not None and cint(self.otp_ip_rate_limit_per_hour) <= 0:
			frappe.throw(_("OTP Per-IP Rate Limit must be greater than 0."))

		if self.portal_session_cookie_name is not None and str(self.portal_session_cookie_name).strip():
			cookie_name = str(self.portal_session_cookie_name).strip()
			self.portal_session_cookie_name = cookie_name
			if not _COOKIE_NAME_RE.match(cookie_name):
				frappe.throw(
					_("Portal Session Cookie Name must be 3-64 characters using only letters, digits, '_' or '-'.")
				)

		if self.portal_session_ttl_days is not None:
			ttl_days = cint(self.portal_session_ttl_days)
			if ttl_days < 1 or ttl_days > 90:
				frappe.throw(_("Portal Session TTL must be between 1 and 90 days."))

		if (
			self.transactional_email_rate_limit_per_hour is not None
			and cint(self.transactional_email_rate_limit_per_hour) <= 0
		):
			frappe.throw(_("Transactional Email Per-Recipient Limit must be greater than 0."))

		if (
			self.transactional_email_ip_rate_limit_per_hour is not None
			and cint(self.transactional_email_ip_rate_limit_per_hour) <= 0
		):
			frappe.throw(_("Transactional Email Per-IP Limit must be greater than 0."))

	def _validate_square_settings(self):
		if not self.square_environment:
			self.square_environment = SQUARE_ENV_DISABLED
		if self.square_environment not in SQUARE_ENVIRONMENTS:
			frappe.throw(
				_("Invalid Square Environment: {0}.").format(self.square_environment),
				frappe.ValidationError,
			)

		if not self.square_currency:
			self.square_currency = SQUARE_CURRENCY
		assert_cad_currency(self.square_currency, context="ISM Settings Square")

		if self.square_bank_account:
			acc = frappe.db.get_value(
				"Account",
				self.square_bank_account,
				["name", "is_group", "disabled", "account_type", "account_currency"],
				as_dict=True,
			)
			if not acc:
				frappe.throw(
					_("Square Bank Account {0} does not exist.").format(self.square_bank_account),
					frappe.ValidationError,
				)
			if acc.is_group:
				frappe.throw(
					_("Square Bank Account {0} must be a leaf account (not a group account).").format(
						self.square_bank_account
					),
					frappe.ValidationError,
				)
			if acc.disabled:
				frappe.throw(
					_("Square Bank Account {0} is disabled.").format(self.square_bank_account),
					frappe.ValidationError,
				)
			if acc.account_type != "Bank":
				frappe.throw(
					_("Square Bank Account {0} must have Account Type 'Bank' (got '{1}').").format(
						self.square_bank_account, acc.account_type or ""
					),
					frappe.ValidationError,
				)
			if acc.account_currency and acc.account_currency != SQUARE_CURRENCY:
				frappe.throw(
					_("Square Bank Account {0} must have CAD currency (got '{1}').").format(
						self.square_bank_account, acc.account_currency
					),
					frappe.ValidationError,
				)

		if self.square_environment in (SQUARE_ENV_SANDBOX, SQUARE_ENV_PRODUCTION):
			missing = []
			if not (self.square_application_id or "").strip():
				missing.append("Square Application ID")
			if not (self.square_location_id or "").strip():
				missing.append("Square Location ID")
			has_token = bool((self.square_access_token or "").strip()) or bool(
				self.get_password("square_access_token", raise_exception=False)
			)
			if not has_token:
				missing.append("Square Access Token")
			if not (self.square_mode_of_payment or "").strip():
				missing.append("Square Mode of Payment")
			if missing:
				frappe.throw(
					_("Square {0} mode requires: {1}.").format(
						self.square_environment, ", ".join(missing)
					),
					frappe.ValidationError,
				)

			webhook_url = (self.square_webhook_notification_url or "").strip()
			if self.square_environment == SQUARE_ENV_PRODUCTION and webhook_url:
				if not webhook_url.lower().startswith("https://"):
					frappe.throw(
						_("Square Webhook Notification URL must start with https:// in Production."),
						frappe.ValidationError,
					)

	def _validate_customer_portal_parameters(self):
		if self.portal_booking_token_ttl_days is not None:
			ttl_days = cint(self.portal_booking_token_ttl_days)
			if ttl_days < PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS or ttl_days > PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS:
				frappe.throw(
					_("Booking Portal Link TTL must be between {0} and {1} days.").format(
						PORTAL_BOOKING_TOKEN_TTL_MIN_DAYS, PORTAL_BOOKING_TOKEN_TTL_MAX_DAYS
					),
					frappe.ValidationError,
				)

	def _validate_self_serve_booking_settings(self):
		if self.self_serve_booking_title is not None:
			title = str(self.self_serve_booking_title).strip()
			self.self_serve_booking_title = title
			if len(title) > 140:
				frappe.throw(_("Self-Serve Booking Title cannot exceed 140 characters."))

		if self.self_serve_booking_intro is not None:
			self.self_serve_booking_intro = str(self.self_serve_booking_intro).strip()

		if self.self_serve_max_packages_per_booking is not None:
			max_packages = cint(self.self_serve_max_packages_per_booking)
			if max_packages < 1 or max_packages > 10:
				frappe.throw(_("Max Packages Per Booking must be between 1 and 10."))

		if self.self_serve_booking_ip_rate_limit_per_hour is not None:
			ip_limit = cint(self.self_serve_booking_ip_rate_limit_per_hour)
			if ip_limit < 1 or ip_limit > 500:
				frappe.throw(_("Self-Serve Booking IP Rate Limit must be between 1 and 500 per hour."))

		if self.self_serve_booking_phone_rate_limit_per_day is not None:
			phone_limit = cint(self.self_serve_booking_phone_rate_limit_per_day)
			if phone_limit < 1 or phone_limit > 20:
				frappe.throw(_("Self-Serve Booking Phone Rate Limit must be between 1 and 20 per day."))

		if getattr(self, "max_concurrent_bookings_per_phone", None) is not None and str(
			self.max_concurrent_bookings_per_phone
		).strip() != "":
			concurrent_limit = cint(self.max_concurrent_bookings_per_phone)
			if concurrent_limit < 1 or concurrent_limit > 10:
				frappe.throw(
					_("Max Concurrent Appointments Per Phone must be between 1 and 10.")
				)

		if (
			cint(self.enable_self_serve_booking) == 1
			and not (frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch)
			and not getattr(self.flags, "ignore_self_serve_sms_warning", False)
			and getattr(frappe.session, "user", None) != "Guest"
		):
			sms_reason = get_sms_unconfigured_reason(self)
			if sms_reason:
				frappe.msgprint(
					_(
						"Self-Serve Booking is enabled, but /book will remain unavailable until an SMS Gateway Provider is configured: {0}"
					).format(sms_reason),
					title=_("Self-Serve Booking Requires SMS"),
					indicator="orange",
				)
