# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from ism.payments.contracts import (
	SQUARE_CURRENCY,
	SQUARE_ENV_DISABLED,
	SQUARE_ENV_PRODUCTION,
	SQUARE_ENV_SANDBOX,
	SQUARE_ENVIRONMENTS,
	assert_cad_currency,
)
from ism.scheduling.time_utils import to_timedelta


class ISMSettings(Document):
	def before_validate(self):
		self._normalize_sms_settings()

	def validate(self):
		self._validate_operating_hours()
		self._validate_break_hours()
		self._validate_estimation_parameters()
		self._validate_pricing_parameters()
		self._validate_square_settings()

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
