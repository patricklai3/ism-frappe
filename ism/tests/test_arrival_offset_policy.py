# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, time, timedelta

import frappe

from ism.scheduling.arrival_policy import (
	DEFAULT_ARRIVAL_OFFSET_MINUTES,
	MAX_ARRIVAL_OFFSET_MINUTES,
	format_customer_appointment_display,
	format_customer_arrival_time,
	get_arrival_offset_minutes,
	to_customer_arrival_datetime,
	to_customer_arrival_time,
	to_internal_slot_datetime,
	to_internal_slot_start,
	to_public_slot_projection,
)
from ism.scheduling.time_utils import get_shop_settings, invalidate_shop_settings_cache
from ism.setup_tasks.seed import setup_default_ism_settings
from ism.tests.base import ISMTestCase

_FORBIDDEN_CUSTOMER_WORDS = ("buffer", "leeway", "early", "grace")


class TestArrivalOffsetPolicy(ISMTestCase):
	"""Unit and settings-integration tests for Seam 4 customer arrival offset policy."""

	def setUp(self):
		super().setUp()
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		invalidate_shop_settings_cache()
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		invalidate_shop_settings_cache()
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()
		super().tearDown()

	def _set_policy_settings(self, **kwargs):
		doc = frappe.get_single("ISM Settings")
		for key, value in kwargs.items():
			doc.set(key, value)
		doc.save(ignore_permissions=True)
		invalidate_shop_settings_cache()

	def test_opening_slot_offset_and_round_trip(self):
		"""Opening slot 08:00:00 translates uniformly to 07:45:00 and round-trips bijectively."""
		self.assertEqual(DEFAULT_ARRIVAL_OFFSET_MINUTES, 15)
		self.assertEqual(MAX_ARRIVAL_OFFSET_MINUTES, 120)

		self.assertEqual(to_customer_arrival_time("08:00:00", 15), "07:45:00")
		self.assertEqual(to_customer_arrival_time("08:00", 15), "07:45:00")
		self.assertEqual(to_customer_arrival_time(time(8, 0, 0), 15), "07:45:00")
		self.assertEqual(to_customer_arrival_time(timedelta(hours=8), 15), "07:45:00")

		self.assertEqual(to_internal_slot_start("07:45:00", 15), "08:00:00")
		self.assertEqual(to_internal_slot_start("07:45", 15), "08:00:00")
		self.assertEqual(to_internal_slot_start(time(7, 45), 15), "08:00:00")
		self.assertEqual(to_internal_slot_start(timedelta(hours=7, minutes=45), 15), "08:00:00")

		self.assertEqual(format_customer_arrival_time("08:00:00", 15), "7:45 AM")
		self.assertEqual(format_customer_arrival_time("08:00:00", 15, use_12h=False), "07:45")

	def test_post_lunch_noon_midnight_and_custom_offsets(self):
		"""Post-lunch 12:30 -> 12:15, noon/midnight 12h formatting, offset 0 identity, and custom offsets."""
		self.assertEqual(to_customer_arrival_time("12:30:00", 15), "12:15:00")
		self.assertEqual(to_internal_slot_start("12:15:00", 15), "12:30:00")
		self.assertEqual(format_customer_arrival_time("12:30:00", 15), "12:15 PM")
		self.assertEqual(format_customer_arrival_time("12:30:00", 15, use_12h=False), "12:15")

		self.assertEqual(format_customer_arrival_time("12:15:00", 15), "12:00 PM")
		self.assertEqual(format_customer_arrival_time("00:45:00", 15), "12:30 AM")
		self.assertEqual(format_customer_arrival_time("00:15:00", 15), "12:00 AM")

		# Offset 0 identity
		self.assertEqual(to_customer_arrival_time("08:00:00", 0), "08:00:00")
		self.assertEqual(to_internal_slot_start("08:00:00", 0), "08:00:00")
		self.assertEqual(format_customer_arrival_time("08:00:00", 0), "8:00 AM")
		self.assertEqual(format_customer_arrival_time("12:00:00", 0), "12:00 PM")

		# Custom offsets 20 and 30
		self.assertEqual(to_customer_arrival_time("08:00:00", 20), "07:40:00")
		self.assertEqual(to_internal_slot_start("07:40:00", 20), "08:00:00")
		self.assertEqual(to_customer_arrival_time("08:00:00", 30), "07:30:00")
		self.assertEqual(to_internal_slot_start("07:30:00", 30), "08:00:00")
		self.assertEqual(format_customer_arrival_time("13:30:00", 30), "1:00 PM")

	def test_datetime_helpers_and_appointment_display(self):
		"""Date-aware conversions and customer appointment display omit internal fields and forbidden words."""
		arrival_dt = to_customer_arrival_datetime("2026-11-23", "08:00:00", 15)
		self.assertEqual(arrival_dt, datetime(2026, 11, 23, 7, 45, 0))

		internal_dt = to_internal_slot_datetime(date(2026, 11, 23), "07:45:00", 15)
		self.assertEqual(internal_dt, datetime(2026, 11, 23, 8, 0, 0))

		display = format_customer_appointment_display("2026-11-23", "08:00:00", 15)
		self.assertEqual(
			display,
			{
				"appointment_date": "2026-11-23",
				"customer_arrival_time": "07:45:00",
				"customer_arrival_hhmm": "07:45",
				"display_time": "7:45 AM",
				"display_date": "Mon, Nov 23, 2026",
				"display_datetime": "Mon, Nov 23, 2026 at 7:45 AM",
			},
		)
		self.assertNotIn("internal_start_time", display)
		self.assertNotIn("offset_minutes", display)
		serialized = " ".join(str(v).lower() for v in display.values())
		for word in _FORBIDDEN_CUSTOMER_WORDS:
			self.assertNotIn(word, serialized)

	def test_invalid_offsets_and_midnight_boundaries(self):
		"""Reject booleans, negative offsets, offsets > 120, and midnight underflow/overflow."""
		for bad_offset in (True, False, -1, -15, 121, 240, 15.5, "15"):
			with self.assertRaises(frappe.ValidationError):
				to_customer_arrival_time("08:00:00", bad_offset)
			with self.assertRaises(frappe.ValidationError):
				to_internal_slot_start("07:45:00", bad_offset)

		# Midnight underflow (T - offset < 00:00:00)
		with self.assertRaises(frappe.ValidationError):
			to_customer_arrival_time("00:10:00", 15)
		with self.assertRaises(frappe.ValidationError):
			to_customer_arrival_datetime("2026-11-23", "00:00:00", 1)

		# Midnight overflow (T_cust + offset >= 24:00:00)
		with self.assertRaises(frappe.ValidationError):
			to_internal_slot_start("23:50:00", 15)
		with self.assertRaises(frappe.ValidationError):
			to_internal_slot_start("23:45:00", 15)
		with self.assertRaises(frappe.ValidationError):
			to_internal_slot_datetime("2026-11-23", "23:55:00", 10)

		# Boundary 120-minute offset succeeds cleanly
		self.assertEqual(to_customer_arrival_time("08:00:00", 120), "06:00:00")
		self.assertEqual(to_internal_slot_start("06:00:00", 120), "08:00:00")

	def test_settings_integration_and_seed_preserves_zero_overrides(self):
		"""setup_default_ism_settings and get_shop_settings preserve explicit 0 overrides."""
		self._set_policy_settings(
			customer_arrival_offset_minutes=0,
			reschedule_cutoff_hours=0,
			max_self_reschedules=0,
			self_serve_min_lead_time_minutes=0,
			enable_waiter_preference=0,
		)

		setup_default_ism_settings()
		invalidate_shop_settings_cache()

		doc = frappe.get_single("ISM Settings")
		self.assertEqual(doc.customer_arrival_offset_minutes, 0)
		self.assertEqual(doc.reschedule_cutoff_hours, 0)
		self.assertEqual(doc.max_self_reschedules, 0)
		self.assertEqual(doc.self_serve_min_lead_time_minutes, 0)
		self.assertEqual(doc.enable_waiter_preference, 0)

		settings = get_shop_settings()
		self.assertEqual(settings["customer_arrival_offset_minutes"], 0)
		self.assertEqual(settings["reschedule_cutoff_hours"], 0)
		self.assertEqual(settings["max_self_reschedules"], 0)
		self.assertEqual(settings["self_serve_min_lead_time_minutes"], 0)
		self.assertEqual(settings["enable_waiter_preference"], 0)
		self.assertEqual(get_arrival_offset_minutes(), 0)
		self.assertEqual(to_customer_arrival_time("08:00:00"), "08:00:00")

		# Custom non-zero setting propagates through get_arrival_offset_minutes()
		self._set_policy_settings(customer_arrival_offset_minutes=20)
		self.assertEqual(get_arrival_offset_minutes(), 20)
		self.assertEqual(to_customer_arrival_time("08:00:00"), "07:40:00")

	def test_to_public_slot_projection_allowlist_and_zero_leakage(self):
		"""to_public_slot_projection returns exact allowlisted keys and strips all internal fields."""
		internal_slot = {
			"start_time": "08:00",
			"available_bays": 2,
			"available_bay_ids": ["BAY-MAIN-LIFT", "BAY-ALIGN"],
			"available_technicians": 2,
			"available_technician_ids": ["EMP-MARCUS", "EMP-ELENA"],
			"service_bay": "BAY-MAIN-LIFT",
			"bottleneck": "bay",
			"conflict_entry": "SE-0001",
			"effective_capacity": 2,
		}

		projected = to_public_slot_projection("2026-11-23", internal_slot, offset_minutes=15)
		self.assertEqual(
			set(projected.keys()),
			{"date", "customer_arrival_time", "customer_arrival_hhmm", "display_time"},
		)
		self.assertEqual(
			projected,
			{
				"date": "2026-11-23",
				"customer_arrival_time": "07:45:00",
				"customer_arrival_hhmm": "07:45",
				"display_time": "7:45 AM",
			},
		)

		serialized = repr(projected).lower()
		for leaked in (
			"start_time",
			"available_bays",
			"available_bay_ids",
			"available_technicians",
			"available_technician_ids",
			"service_bay",
			"bottleneck",
			"conflict_entry",
			"effective_capacity",
			"bay-main-lift",
			"emp-marcus",
			*_FORBIDDEN_CUSTOMER_WORDS,
		):
			self.assertNotIn(leaked, serialized)
