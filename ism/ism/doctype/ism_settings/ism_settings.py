# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

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
