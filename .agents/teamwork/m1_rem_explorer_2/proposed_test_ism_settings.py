# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.setup import setup_default_ism_settings
from ism.tests.base import ISMTestCase


class TestISMSettings(ISMTestCase):
	"""Unit tests for ISM Settings validations, default initialization, and transactional hygiene."""

	def setUp(self):
		super().setUp()
		# Clear any pending transaction locks and in-memory caches
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()

		# Initialize pristine defaults and commit to establish baseline transaction state
		setup_default_ism_settings()
		frappe.db.commit()

		# Snapshot pristine state of ISM Settings for complete restoration during tearDown
		self._original_settings = frappe.get_doc("ISM Settings").as_dict()

	def tearDown(self):
		try:
			# Discard in-flight test transaction locks
			frappe.db.rollback()
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()

			# Restore all baseline fields from snapshot
			if hasattr(self, "_original_settings") and self._original_settings:
				settings = frappe.get_doc("ISM Settings")
				excluded_keys = {
					"modified",
					"creation",
					"doctype",
					"name",
					"_liked_by",
					"_comments",
					"_assign",
				}
				for k, v in self._original_settings.items():
					if not k.startswith("_") and k not in excluded_keys:
						setattr(settings, k, v)
				settings.save(ignore_permissions=True)
				frappe.db.commit()
		except Exception:
			frappe.db.rollback()
		finally:
			super().tearDown()

	def test_default_ism_settings_initialized(self):
		settings = frappe.get_doc("ISM Settings")
		self.assertIsNotNone(settings)
		self.assertEqual(str(settings.operating_hours_start), "08:00:00")
		self.assertEqual(str(settings.operating_hours_end), "17:00:00")
		self.assertEqual(str(settings.break_start), "12:00:00")
		self.assertEqual(str(settings.break_end), "12:30:00")
		self.assertEqual(settings.default_slot_interval, 30)
		self.assertEqual(settings.scheduling_horizon_days, 30)
		self.assertEqual(settings.technician_designation, "Technician")
		self.assertEqual(settings.enable_technician_capacity, 1)
		self.assertEqual(settings.estimation_percentile, 80.0)
		self.assertAlmostEqual(settings.estimation_sigma, 0.30, places=2)
		self.assertGreaterEqual(settings.buffer_padding_minutes, 0)
		self.assertEqual(settings.default_diagnostic_duration, 60)

	def test_validate_operating_hours_order(self):
		settings = frappe.get_doc("ISM Settings")
		settings.operating_hours_start = "18:00:00"
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_validate_break_hours_order(self):
		settings = frappe.get_doc("ISM Settings")
		settings.break_start = "13:00:00"
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_validate_break_within_operating_hours(self):
		settings = frappe.get_doc("ISM Settings")
		settings.break_start = "07:00:00"
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_validate_estimation_percentile_bounds(self):
		# Test lower bound (< 50%)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 40.0
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

		# Test upper bound (> 99%)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 100.0
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

		# Auto-conversion from decimal fraction (0.85 -> 85.0)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 0.85
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("ISM Settings")
		self.assertEqual(reloaded.estimation_percentile, 85.0)

	def test_validate_estimation_sigma_bounds(self):
		# Test lower bound (< 0.05)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_sigma = 0.02
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

		# Test upper bound (> 1.00)
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_sigma = 1.5
		with self.assertRaises(frappe.ValidationError):
			settings.save()
		frappe.db.rollback()

	def test_setup_idempotency_preserves_customizations(self):
		settings = frappe.get_doc("ISM Settings")
		settings.default_slot_interval = 45
		settings.enable_technician_capacity = 0
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		# Re-run setup_default_ism_settings
		setup_default_ism_settings()
		frappe.db.commit()

		reloaded = frappe.get_doc("ISM Settings")
		self.assertEqual(reloaded.default_slot_interval, 45)
		self.assertEqual(reloaded.enable_technician_capacity, 0)
