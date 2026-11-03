# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

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
}


class TestISMSettings(ISMTestCase):
	"""Unit tests for ISM Settings validations, default initialization, and transactional hygiene."""

	def setUp(self):
		super().setUp()
		# Clear any pending transaction locks and in-memory caches
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()

		# Establish deterministic canonical baseline state
		frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
		frappe.clear_cache(doctype="ISM Settings")
		frappe.db.commit()

	def tearDown(self):
		# Discard in-flight test transaction state
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()

		# Forcefully restore canonical baseline defaults without exception suppression
		frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
		frappe.clear_cache(doctype="ISM Settings")
		frappe.db.commit()
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
		# Apply custom values via set_single_value (no lock contention, no commit)
		frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)
		frappe.db.set_single_value("ISM Settings", "enable_technician_capacity", 0)
		frappe.clear_cache(doctype="ISM Settings")

		# Re-run setup_default_ism_settings
		setup_default_ism_settings()

		# Assert customizations were preserved
		reloaded = frappe.get_single("ISM Settings")
		self.assertEqual(reloaded.default_slot_interval, 45)
		self.assertEqual(reloaded.enable_technician_capacity, 0)

		# Discard uncommitted mutations via transaction rollback
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
