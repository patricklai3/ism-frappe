# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from ism.setup import setup_default_ism_settings
from ism.tests.base import ISMTestCase

CANONICAL_ISM_SETTINGS = {
	"operating_hours_start": "08:00:00",
	"operating_hours_end": "17:00:00",
	"break_start": "12:00:00",
	"break_end": "12:30:00",
	"default_slot_interval": 30,
	"scheduling_horizon_days": 30,
	"technician_designation": "Technician",
	"enable_technician_capacity": 1,
	"estimation_percentile": 80.0,
	"estimation_sigma": 0.30,
	"buffer_padding_minutes": 0,
	"default_diagnostic_duration": 60,
	"default_labor_rate": 140.0,
	"shop_supplies_percentage": 0.0,
	"cost_tolerance_flat_amount": 20.0,
	"sms_gateway_provider": "Disabled",
}


class TestISMSettings(ISMTestCase):
	"""Unit tests for ISM Settings validations, default initialization, and transactional hygiene."""

	def setUp(self):
		super().setUp()
		# Clear any pending transaction locks and in-memory caches without DB churn
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()

	def tearDown(self):
		# Discard in-flight test transaction state via rollback (zero tabSingles churn)
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()
		super().tearDown()

	def test_default_ism_settings_initialized(self):
		"""Verify canonical default values for all scheduling and estimation fields."""
		settings = frappe.get_single("ISM Settings")
		self.assertIsNotNone(settings)
		self.assertEqual(str(settings.operating_hours_start), "08:00:00")
		self.assertEqual(str(settings.operating_hours_end), "17:00:00")
		self.assertEqual(str(settings.break_start), "12:00:00")
		self.assertEqual(str(settings.break_end), "12:30:00")
		self.assertEqual(settings.default_slot_interval, 30)
		self.assertEqual(settings.scheduling_horizon_days, 30)
		self.assertEqual(settings.technician_designation, "Technician")
		self.assertEqual(settings.enable_technician_capacity, 1)
		self.assertEqual(float(settings.estimation_percentile), 80.0)
		self.assertAlmostEqual(float(settings.estimation_sigma), 0.30, places=2)
		self.assertGreaterEqual(int(settings.buffer_padding_minutes or 0), 0)
		self.assertEqual(settings.default_diagnostic_duration, 60)
		self.assertAlmostEqual(float(settings.default_labor_rate), 140.0, places=2)
		self.assertAlmostEqual(float(settings.shop_supplies_percentage or 0.0), 0.0, places=2)
		self.assertAlmostEqual(float(settings.cost_tolerance_flat_amount), 20.0, places=2)
		self.assertEqual(settings.sms_gateway_provider, "Disabled")

	def test_validate_operating_hours_order(self):
		"""Verify validation error when operating_hours_start >= operating_hours_end."""
		settings = frappe.new_doc("ISM Settings")
		settings.operating_hours_start = "18:00:00"
		settings.operating_hours_end = "17:00:00"
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		# Verify slot interval and horizon must be positive
		settings.operating_hours_start = "08:00:00"
		settings.default_slot_interval = 0
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		settings.default_slot_interval = 30
		settings.scheduling_horizon_days = 0
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_validate_break_hours_order(self):
		"""Verify validation error when break_start >= break_end."""
		settings = frappe.new_doc("ISM Settings")
		settings.operating_hours_start = "08:00:00"
		settings.operating_hours_end = "17:00:00"
		settings.break_start = "13:00:00"
		settings.break_end = "12:30:00"
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_validate_break_within_operating_hours(self):
		"""Verify validation error when lunch break extends outside shop operating hours."""
		settings = frappe.new_doc("ISM Settings")
		settings.operating_hours_start = "08:00:00"
		settings.operating_hours_end = "17:00:00"

		# Break starts before operating hours
		settings.break_start = "07:00:00"
		settings.break_end = "07:30:00"
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		# Break ends after operating hours
		settings.break_start = "17:00:00"
		settings.break_end = "17:30:00"
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_validate_estimation_percentile_bounds(self):
		"""Verify bounds checking and fraction auto-conversion for estimation_percentile."""
		settings = frappe.new_doc("ISM Settings")

		# Lower bound (< 50%)
		settings.estimation_percentile = 40.0
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		# Upper bound (> 99%)
		settings.estimation_percentile = 100.0
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		# Auto-conversion from decimal fraction (0.85 -> 85.0)
		settings.estimation_percentile = 0.85
		settings.validate()
		self.assertEqual(settings.estimation_percentile, 85.0)

	def test_validate_estimation_sigma_bounds(self):
		"""Verify bounds checking for estimation_sigma (0.05 - 1.00)."""
		settings = frappe.new_doc("ISM Settings")

		# Lower bound (< 0.05)
		settings.estimation_sigma = 0.02
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		# Upper bound (> 1.00)
		settings.estimation_sigma = 1.5
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		# Negative buffer padding
		settings.estimation_sigma = 0.30
		settings.buffer_padding_minutes = -5
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		# Zero diagnostic duration
		settings.buffer_padding_minutes = 0
		settings.default_diagnostic_duration = 0
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

	def test_setup_idempotency_preserves_customizations(self):
		"""Verify setup_default_ism_settings preserves existing customizations without committing state."""
		for attempt in range(3):
			try:
				# Apply custom values via atomic dictionary set_single_value in uncommitted transaction
				frappe.db.set_single_value("ISM Settings", {
					"default_slot_interval": 45,
					"enable_technician_capacity": 0,
				})
				frappe.clear_cache(doctype="ISM Settings")
				if hasattr(frappe.db, "value_cache"):
					frappe.db.value_cache.clear()
				break
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					frappe.db.rollback()
					frappe.clear_cache(doctype="ISM Settings")
					if hasattr(frappe.db, "value_cache"):
						frappe.db.value_cache.clear()
					continue
				raise

		try:
			# Re-run setup_default_ism_settings
			setup_default_ism_settings()

			# Assert customizations were preserved
			reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(reloaded.default_slot_interval, 45)
			self.assertEqual(reloaded.enable_technician_capacity, 0)
		finally:
			# Discard uncommitted mutations via transaction rollback
			frappe.db.rollback()
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()

	def test_ism_manager_permissions(self):
		"""Verify that a user with only ISM Manager role has read and write permissions on ISM Settings."""
		docperm_rows = frappe.db.get_all(
			"DocPerm",
			filters={"parent": "ISM Settings", "role": "ISM Manager"},
			fields=["read", "write"],
		)
		self.assertTrue(docperm_rows, "ISM Settings DocType schema must grant ISM Manager permissions")
		self.assertTrue(
			any(int(row.get("read") or 0) == 1 and int(row.get("write") or 0) == 1 for row in docperm_rows),
			"ISM Settings DocPerm schema must grant ISM Manager read and write access",
		)

		# Guard against corrupted partial Custom DocPerm overrides (missing System Manager)
		# left on a shared test site by legacy scripts or uncommitted test teardown.
		if frappe.db.exists("Custom DocPerm", {"parent": "ISM Settings"}) and not frappe.db.exists(
			"Custom DocPerm", {"parent": "ISM Settings", "role": "System Manager"}
		):
			frappe.db.delete("Custom DocPerm", {"parent": "ISM Settings"})
			frappe.clear_cache(doctype="ISM Settings")

		user_email = f"sim_ism_mgr_{frappe.generate_hash(length=6)}@example.com"
		if frappe.db.exists("User", user_email):
			frappe.delete_doc("User", user_email, force=True)

		user = frappe.get_doc({
			"doctype": "User",
			"email": user_email,
			"first_name": "Sim ISM Manager",
			"roles": [{"role": "ISM Manager"}],
		}).insert(ignore_permissions=True)
		self.track(user)

		self.assertTrue(
			frappe.has_permission("ISM Settings", "read", user=user_email),
			"ISM Manager role should have read permission on ISM Settings",
		)
		self.assertTrue(
			frappe.has_permission("ISM Settings", "write", user=user_email),
			"ISM Manager role should have write permission on ISM Settings",
		)

	def test_validate_default_labor_rate_positive(self):
		"""Verify default_labor_rate must be positive when set."""
		settings = frappe.new_doc("ISM Settings")
		settings.default_labor_rate = 0
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		settings.default_labor_rate = -50.0
		with self.assertRaises(frappe.ValidationError):
			settings.validate()

		settings.default_labor_rate = 165.0
		settings.validate()

	def test_sms_gateway_provider_options_and_twilio_migration(self):
		"""Verify sms_gateway_provider only offers supported options and coerces legacy Twilio."""
		meta = frappe.get_meta("ISM Settings")
		provider_field = meta.get_field("sms_gateway_provider")
		self.assertIsNotNone(provider_field)
		options = [opt.strip() for opt in (provider_field.options or "").splitlines() if opt.strip()]
		self.assertEqual(
			options,
			["Disabled", "Telnyx", "Custom HTTP Webhook", "Mock (Deterministic Test)"],
		)

		settings = frappe.new_doc("ISM Settings")
		settings.sms_gateway_provider = "Twilio"
		settings.before_validate()
		settings._validate_selects()
		settings.validate()
		self.assertEqual(settings.sms_gateway_provider, "Disabled")

		for valid_provider in ("Telnyx", "Custom HTTP Webhook", "Mock (Deterministic Test)"):
			settings.sms_gateway_provider = valid_provider
			settings.before_validate()
			settings._validate_selects()
			settings.validate()
			self.assertEqual(settings.sms_gateway_provider, valid_provider)

	def test_comms_and_otp_settings_defaults_and_bounds(self):
		"""Verify seeded comms/OTP/session defaults and validation bounds on ISM Settings."""
		settings = frappe.get_single("ISM Settings")
		self.assertEqual(settings.sms_default_country_code, "+1")
		self.assertEqual(int(settings.telnyx_webhook_tolerance_seconds), 300)
		self.assertEqual(int(settings.sms_enforce_opt_out), 1)
		self.assertTrue(settings.sms_help_reply_text)
		self.assertEqual(int(settings.otp_code_length), 6)
		self.assertEqual(int(settings.otp_ttl_seconds), 300)
		self.assertEqual(int(settings.otp_resend_cooldown_seconds), 60)
		self.assertEqual(int(settings.otp_max_verify_attempts), 5)
		self.assertEqual(int(settings.otp_lockout_seconds), 900)
		self.assertEqual(int(settings.otp_phone_rate_limit_per_hour), 5)
		self.assertEqual(int(settings.otp_ip_rate_limit_per_hour), 20)
		self.assertEqual(settings.portal_session_cookie_name, "ism_portal_session")
		self.assertEqual(int(settings.portal_session_ttl_days), 30)
		self.assertEqual(settings.portal_session_cookie_samesite, "Lax")
		self.assertEqual(int(settings.portal_session_secure_cookie), 1)
		self.assertEqual(int(settings.transactional_email_rate_limit_per_hour), 5)
		self.assertEqual(int(settings.transactional_email_ip_rate_limit_per_hour), 20)
		self.assertTrue(settings.get_password("portal_session_signing_secret", raise_exception=False))

		invalid_cases = [
			("sms_default_country_code", "1"),
			("sms_default_country_code", "+1234"),
			("telnyx_webhook_tolerance_seconds", 10),
			("telnyx_webhook_tolerance_seconds", 7200),
			("otp_code_length", 3),
			("otp_code_length", 9),
			("otp_ttl_seconds", 30),
			("otp_ttl_seconds", 1200),
			("otp_resend_cooldown_seconds", 5),
			("otp_resend_cooldown_seconds", 600),
			("otp_max_verify_attempts", 0),
			("otp_max_verify_attempts", 11),
			("otp_lockout_seconds", 30),
			("otp_lockout_seconds", 100000),
			("otp_phone_rate_limit_per_hour", 0),
			("otp_ip_rate_limit_per_hour", 0),
			("portal_session_cookie_name", "ab"),
			("portal_session_cookie_name", "bad cookie!"),
			("portal_session_ttl_days", 0),
			("portal_session_ttl_days", 91),
			("transactional_email_rate_limit_per_hour", 0),
			("transactional_email_ip_rate_limit_per_hour", 0),
		]
		for fieldname, bad_val in invalid_cases:
			doc = frappe.new_doc("ISM Settings")
			setattr(doc, fieldname, bad_val)
			with self.subTest(fieldname=fieldname, bad_val=bad_val):
				with self.assertRaises(frappe.ValidationError):
					doc.validate()

	def test_closed_shop_zero_hours_allowed(self):
		"""Verify 00:00:00-00:00:00 closed-shop operating hours validate natively on ISMSettings."""
		doc = frappe.get_single("ISM Settings")
		doc.operating_hours_start = "00:00:00"
		doc.operating_hours_end = "00:00:00"
		doc.validate()

	def test_self_serve_booking_settings_defaults_and_bounds(self):
		"""Verify all 8 sb_self_serve_booking defaults, trimming, and validation bounds."""
		settings = frappe.get_single("ISM Settings")
		self.assertEqual(int(settings.enable_self_serve_booking), 0)
		self.assertEqual(settings.self_serve_booking_title, "Book Your Service Appointment")
		self.assertEqual(
			settings.self_serve_booking_intro,
			"Select your vehicle and service package to view live drop-off times and book online.",
		)
		self.assertEqual(int(settings.self_serve_max_packages_per_booking), 5)
		self.assertEqual(int(settings.self_serve_require_email), 0)
		self.assertEqual(int(settings.self_serve_booking_ip_rate_limit_per_hour), 15)
		self.assertEqual(int(settings.self_serve_booking_phone_rate_limit_per_day), 3)
		self.assertEqual(int(settings.self_serve_honeypot_enabled), 1)

		trimmed = frappe.new_doc("ISM Settings")
		trimmed.self_serve_booking_title = "  Book Online  "
		trimmed.self_serve_booking_intro = "  Fast drop-off booking.  "
		trimmed.validate()
		self.assertEqual(trimmed.self_serve_booking_title, "Book Online")
		self.assertEqual(trimmed.self_serve_booking_intro, "Fast drop-off booking.")

		invalid_cases = [
			("self_serve_max_packages_per_booking", 0),
			("self_serve_max_packages_per_booking", 11),
			("self_serve_booking_ip_rate_limit_per_hour", 0),
			("self_serve_booking_ip_rate_limit_per_hour", 501),
			("self_serve_booking_phone_rate_limit_per_day", 0),
			("self_serve_booking_phone_rate_limit_per_day", 21),
			("self_serve_booking_title", "x" * 141),
		]
		for fieldname, bad_val in invalid_cases:
			doc = frappe.new_doc("ISM Settings")
			setattr(doc, fieldname, bad_val)
			with self.subTest(fieldname=fieldname, bad_val=bad_val):
				with self.assertRaises(frappe.ValidationError):
					doc.validate()

	def test_setup_preserves_explicit_zero_self_serve_booking_flags(self):
		"""Verify setup_default_ism_settings preserves explicit 0 for booking and honeypot flags."""
		from ism.setup_tasks.seed import setup_default_ism_settings

		settings = frappe.get_single("ISM Settings")
		settings.enable_self_serve_booking = 0
		settings.self_serve_honeypot_enabled = 0
		settings.save(ignore_permissions=True)

		setup_default_ism_settings()

		reloaded = frappe.get_single("ISM Settings")
		self.assertEqual(int(reloaded.enable_self_serve_booking), 0)
		self.assertEqual(int(reloaded.self_serve_honeypot_enabled), 0)

	def test_self_serve_booking_warns_when_sms_unconfigured(self):
		"""Verify enabling self-serve booking warns (non-blocking) when SMS is unconfigured."""
		field = frappe.get_meta("ISM Settings").get_field("enable_self_serve_booking")
		self.assertIsNotNone(field)
		self.assertIn("SMS Gateway Provider", field.description or "")
		self.assertIn("Allow Self-Serve Booking", field.description or "")

		disabled_doc = frappe.new_doc("ISM Settings")
		disabled_doc.enable_self_serve_booking = 1
		disabled_doc.sms_gateway_provider = "Disabled"
		with patch("frappe.msgprint") as mock_msgprint:
			disabled_doc.validate()
		mock_msgprint.assert_called_once()
		msg = str(mock_msgprint.call_args.args[0])
		self.assertIn("/book", msg)
		self.assertIn("SMS", msg)
		self.assertEqual(mock_msgprint.call_args.kwargs.get("indicator"), "orange")

		telnyx_incomplete = frappe.new_doc("ISM Settings")
		telnyx_incomplete.enable_self_serve_booking = 1
		telnyx_incomplete.sms_gateway_provider = "Telnyx"
		telnyx_incomplete.telnyx_api_key = ""
		with patch("frappe.msgprint") as mock_msgprint:
			telnyx_incomplete.validate()
		mock_msgprint.assert_called_once()
		self.assertIn("Telnyx API Key", str(mock_msgprint.call_args.args[0]))

		configured_doc = frappe.new_doc("ISM Settings")
		configured_doc.enable_self_serve_booking = 1
		configured_doc.sms_gateway_provider = "Mock (Deterministic Test)"
		with patch("frappe.msgprint") as mock_msgprint:
			configured_doc.validate()
		mock_msgprint.assert_not_called()

		off_doc = frappe.new_doc("ISM Settings")
		off_doc.enable_self_serve_booking = 0
		off_doc.sms_gateway_provider = "Disabled"
		with patch("frappe.msgprint") as mock_msgprint:
			off_doc.validate()
		mock_msgprint.assert_not_called()
