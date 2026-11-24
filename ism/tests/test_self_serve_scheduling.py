# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import date, datetime, time, timedelta
import json
import threading
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, today

from ism.api.scheduling import create_repair_order_from_schedule_entry
from ism.api.self_serve_scheduling import (
	_validate_required_package_ids,
	execute_self_serve_reschedule,
	get_bookable_packages,
	get_reschedule_policy_and_slots,
	get_self_serve_slots,
	submit_provisional_booking,
)
from ism.scheduling.capacity import ShopCapacityWindow
from ism.scheduling.self_serve import (
	ALLOWED_RESCHEDULE_ENTRY_STATUSES,
	ALLOWED_RESCHEDULE_RO_STATUSES,
	LOCKED_RO_STATUSES,
	VALID_BOOKING_SOURCES,
	VALID_WAITER_PREFERENCES,
	create_provisional_booking,
	emit_scheduling_domain_event,
	evaluate_reschedule_eligibility,
	find_self_serve_slots,
	get_self_serve_policy,
	normalize_provisional_intake,
	reschedule_self_serve_appointment,
)
from ism.scheduling.slot_search import find_next_available_slot_batch, search_day_slots
from ism.scheduling.time_utils import invalidate_shop_settings_cache
from ism.tests.base import ISMTestCase, TEST_PREFIX

_FORBIDDEN_CUSTOMER_WORDS = ("buffer", "leeway", "early", "grace")
_FORBIDDEN_INTERNAL_KEYS = {
	"start_time",
	"internal_start_time",
	"offset_minutes",
	"customer_arrival_offset_minutes",
	"service_bay",
	"available_bays",
	"available_bay_ids",
	"available_technicians",
	"available_technician_ids",
	"bottleneck",
	"conflict_entry",
	"correction_code",
	"rh_correction_code",
	"hourly_rate",
	"service_rate",
	"tesla_manual_url",
	"doc_guid",
}


def _collect_keys_and_strings(obj, keys_out, strings_out):
	if isinstance(obj, dict):
		for k, v in obj.items():
			keys_out.add(str(k))
			_collect_keys_and_strings(v, keys_out, strings_out)
	elif isinstance(obj, (list, tuple, set)):
		for item in obj:
			_collect_keys_and_strings(item, keys_out, strings_out)
	elif isinstance(obj, str):
		strings_out.append(obj)


class TestSelfServeScheduling(ISMTestCase):
	"""Comprehensive tests for Project 3 Wave 2 self-serve scheduling, rescheduling, and provisional booking."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.manifest = self.get_manifest()
		self.bay1 = self.manifest.bays["main_lift"]
		self.bay2 = self.manifest.bays["alignment"]
		self.bay_hv = self.manifest.bays["hv_diagnostics"]
		self.bay_tire = self.manifest.bays["tire_quick"]
		self.active_bays = [self.bay1, self.bay2, self.bay_hv, self.bay_tire]
		self.tech1 = self.manifest.techs["marcus"]
		self.tech2 = self.manifest.techs["elena"]
		self.customer = self.manifest.customers["retail_1"]
		self.vehicle = self.manifest.vehicles["veh_model3_liam"]

		self._set_ism_settings(
			operating_hours_start="08:00:00",
			operating_hours_end="17:00:00",
			break_start="12:00:00",
			break_end="12:30:00",
			default_slot_interval=30,
			default_diagnostic_duration=60,
			enable_technician_capacity=1,
			technician_designation="Technician",
			holiday_list=None,
			customer_arrival_offset_minutes=15,
			reschedule_cutoff_hours=24,
			max_self_reschedules=2,
			self_serve_default_entry_type="Diagnostic",
			self_serve_min_lead_time_minutes=120,
			self_serve_horizon_days=30,
			enable_waiter_preference=1,
			buffer_padding_minutes=0,
		)

		self.base_date = getdate(add_days(today(), 5))
		self.now_anchor = datetime.combine(getdate(today()), time(9, 0, 0))
		self.bookable_pkg = self._create_bookable_package(
			"SelfServe_CabinFilter",
			customer_title="Cabin Air Filter Service",
			customer_summary="Replace cabin air filter and inspect HVAC intake.",
			applicable_model="Model 3",
			self_serve_duration_minutes=60,
			customer_price_display_mode="Exact Price",
			customer_estimate_min=129.0,
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		invalidate_shop_settings_cache()
		super().tearDown()

	def _set_ism_settings(self, **kwargs):
		if not frappe.db.exists("DocType", "ISM Settings"):
			return
		settings = frappe.get_single("ISM Settings")
		for key, val in kwargs.items():
			setattr(settings, key, val)
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype="ISM Settings")
		invalidate_shop_settings_cache()

	def _create_bookable_package(self, suffix: str, **kwargs):
		pkg_name = f"{TEST_PREFIX}SS_{suffix}"
		if frappe.db.exists("Service Package", pkg_name):
			frappe.delete_doc("Service Package", pkg_name, force=True, ignore_permissions=True)
		payload = {
			"doctype": "Service Package",
			"package_name": pkg_name,
			"applicable_model": "All Models",
			"standard_labor_hours": 1.0,
			"hourly_rate": 140.0,
			"service_rate": 140.0,
			"correction_code": "10101010",
			"allow_self_serve_booking": 1,
			"customer_price_display_mode": "Exact Price",
			"customer_estimate_min": 140.0,
			"self_serve_duration_minutes": 60,
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def _create_equipment_tag(self, tag_name: str):
		if frappe.db.exists("Equipment Tag", tag_name):
			return tag_name
		doc = frappe.get_doc({
			"doctype": "Equipment Tag",
			"tag_name": tag_name,
			"description": "Self-serve test tag",
		}).insert(ignore_permissions=True)
		self.track(doc)
		return doc.name

	def _assert_customer_safe(self, payload):
		keys = set()
		strings = []
		_collect_keys_and_strings(payload, keys, strings)
		leaked_keys = keys & _FORBIDDEN_INTERNAL_KEYS
		self.assertEqual(leaked_keys, set(), f"Leaked internal keys: {leaked_keys}")
		joined = " ".join(strings).lower()
		for word in _FORBIDDEN_CUSTOMER_WORDS:
			self.assertNotIn(word, joined, f"Forbidden customer word '{word}' found in: {joined}")

	def _create_scheduled_entry(self, sched_date, sched_time="09:00:00", duration=60, bay=None, **kwargs):
		payload = {
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"status": "Scheduled",
			"scheduled_date": str(sched_date),
			"scheduled_time": sched_time,
			"estimated_duration": duration,
			"service_bay": bay or self.bay1,
			"provisional_customer_name": "Jordan Rivera",
			"provisional_phone": "+16045550101",
			"booking_source": "Website Self-Serve",
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	# -------------------------------------------------------------------------
	# 1. Schedule Entry schema, normalization, display vehicle & exclude_entry
	# -------------------------------------------------------------------------
	def test_schedule_entry_self_serve_fields_trim_display_and_reschedule_count(self):
		meta = frappe.get_meta("Schedule Entry")
		for fname in (
			"section_break_self_serve",
			"booking_source",
			"waiter_preference",
			"reschedule_count",
			"cb_self_serve_meta",
			"last_rescheduled_at",
			"provisional_vehicle_model",
			"provisional_vehicle_year",
			"provisional_vin_or_plate",
			"booked_packages_json",
		):
			self.assertTrue(meta.has_field(fname), f"Missing Schedule Entry field: {fname}")

		se = self._create_scheduled_entry(
			self.base_date,
			sched_time="08:00:00",
			provisional_customer_name="   Taylor Chen   ",
			provisional_phone="  604-555-0199  ",
			provisional_email="  taylor@example.com  ",
			provisional_vehicle_info="",
			provisional_vehicle_model="  Model Y  ",
			provisional_vehicle_year=2024,
			provisional_vin_or_plate="  EV2024  ",
		)
		self.assertEqual(se.provisional_customer_name, "Taylor Chen")
		self.assertEqual(se.provisional_phone, "604-555-0199")
		self.assertEqual(se.provisional_email, "taylor@example.com")
		self.assertEqual(se.provisional_vehicle_model, "Model Y")
		self.assertEqual(se.provisional_vin_or_plate, "EV2024")
		self.assertEqual(se.reschedule_count, 0)
		self.assertEqual(se.get_display_vehicle(), "2024 Model Y (EV2024)")
		self.assertIn("2024 Model Y (EV2024)", se.title)

		se.reschedule_count = -1
		with self.assertRaises(frappe.ValidationError):
			se.save(ignore_permissions=True)

	def test_capacity_and_slot_search_exclude_entry_passthrough(self):
		target_date = str(self.base_date)
		se = self._create_scheduled_entry(target_date, sched_time="09:00:00", duration=60, bay=self.bay1)

		win_without = ShopCapacityWindow(target_date, target_date, service_bay=self.bay1)
		times_without = [s["start_time"] for s in win_without.slots_for_date(target_date, 60)]
		self.assertNotIn("09:00", times_without)
		self.assertNotIn("09:30", times_without)

		win_with = ShopCapacityWindow(target_date, target_date, service_bay=self.bay1, exclude_entry=se.name)
		times_with = [s["start_time"] for s in win_with.slots_for_date(target_date, 60)]
		self.assertIn("09:00", times_with)
		self.assertIn("09:30", times_with)

		day_without = [s["start_time"] for s in search_day_slots(target_date, 60, service_bay=self.bay1)]
		day_with = [s["start_time"] for s in search_day_slots(target_date, 60, service_bay=self.bay1, exclude_entry=se.name)]
		self.assertNotIn("09:00", day_without)
		self.assertIn("09:00", day_with)

		# Fill entire day on bay1 with one 510-min entry and verify find_next_available_slot_batch excludes it
		full_day_date = add_days(self.base_date, 2)
		full_se = self._create_scheduled_entry(full_day_date, sched_time="08:00:00", duration=510, bay=self.bay1)
		prev_day = add_days(full_day_date, -1)
		batch_excl = find_next_available_slot_batch(
			prev_day,
			3,
			60,
			service_bay=self.bay1,
			exclude_entry=full_se.name,
		)
		self.assertIsNotNone(batch_excl)
		self.assertEqual(batch_excl["date"], str(full_day_date))

	# -------------------------------------------------------------------------
	# 2. Opening-slot 08:00 -> 07:45 parity
	# -------------------------------------------------------------------------
	def test_opening_slot_parity_discovery_and_booking(self):
		target_date = str(self.base_date)
		slots_res = find_self_serve_slots(
			package_ids=[self.bookable_pkg.name],
			start_date=target_date,
			days_ahead=1,
			vehicle_model="Model 3",
			now_dt=self.now_anchor,
		)
		self._assert_customer_safe(slots_res)
		self.assertEqual(len(slots_res["days"]), 1)
		first_slot = slots_res["days"][0]["slots"][0]
		self.assertEqual(first_slot["customer_arrival_time"], "07:45:00")
		self.assertEqual(first_slot["customer_arrival_hhmm"], "07:45")
		self.assertEqual(first_slot["display_time"], "7:45 AM")

		booking = create_provisional_booking(
			package_ids=[self.bookable_pkg.name],
			target_date=target_date,
			customer_arrival_time="07:45",
			customer_name="Alex Morgan",
			phone="(604) 555-0144",
			email="ALEX@Example.COM",
			vehicle_model="tesla model 3",
			vehicle_year=2023,
			vehicle_color="Pearl White",
			license_plate="EV 101",
			vin_or_plate="5yj3e1ea1pf000001",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_customer_safe(booking)
		se = frappe.get_doc("Schedule Entry", booking["booking_reference"])
		self.track(se)

		# Internal DB truth stays anchored strictly to 08:00:00 -> 09:00:00
		self.assertEqual(str(se.scheduled_time), "8:00:00" if str(se.scheduled_time) == "8:00:00" else "08:00:00")
		self.assertEqual(se.scheduled_time, timedelta(hours=8))
		self.assertEqual(se.completion_time, timedelta(hours=9))
		self.assertEqual(booking["appointment"]["customer_arrival_time"], "07:45:00")
		self.assertEqual(booking["appointment"]["customer_arrival_hhmm"], "07:45")
		self.assertEqual(booking["appointment"]["display_time"], "7:45 AM")

	# -------------------------------------------------------------------------
	# 3. Lunch break & holiday boundaries
	# -------------------------------------------------------------------------
	def test_lunch_break_and_holiday_boundaries(self):
		target_date = str(self.base_date)

		# 11:45 customer -> 12:00 internal (inside lunch [12:00, 12:30)) -> rejected
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_provisional_booking(
				package_ids=[self.bookable_pkg.name],
				target_date=target_date,
				customer_arrival_time="11:45",
				customer_name="Sam Lee",
				phone="6045550122",
				vehicle_model="Model 3",
				vehicle_color="Pearl White",
				license_plate="BRK 101",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx.exception, "reason_code", None), "break_window")

		# 12:15 customer -> 12:30 internal (post-lunch) -> succeeds
		res = create_provisional_booking(
			package_ids=[self.bookable_pkg.name],
			target_date=target_date,
			customer_arrival_time="12:15",
			customer_name="Sam Lee",
			phone="6045550122",
			vehicle_model="Model 3",
			vehicle_color="Pearl White",
			license_plate="BRK 102",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		se = frappe.get_doc("Schedule Entry", res["booking_reference"])
		self.track(se)
		self.assertEqual(se.scheduled_time, timedelta(hours=12, minutes=30))
		self.assertEqual(res["appointment"]["customer_arrival_hhmm"], "12:15")

		# Configure Holiday on holiday_date -> 0 slots, reject booking & reschedule
		holiday_date = str(add_days(self.base_date, 3))
		hl = frappe.get_doc({
			"doctype": "Holiday List",
			"holiday_list_name": f"{TEST_PREFIX}SS_Holiday_{holiday_date}",
			"from_date": str(add_days(today(), -1)),
			"to_date": str(add_days(today(), 90)),
			"holidays": [{"holiday_date": holiday_date, "description": "Shop Closed Test"}],
		}).insert(ignore_permissions=True)
		self.track(hl)
		self._set_ism_settings(holiday_list=hl.name)

		holiday_slots = find_self_serve_slots(
			package_ids=[self.bookable_pkg.name],
			start_date=holiday_date,
			days_ahead=1,
			vehicle_model="Model 3",
			now_dt=self.now_anchor,
		)
		self.assertEqual(holiday_slots["total_slots"], 0)
		self.assertEqual(holiday_slots["days"], [])

		with self.assertRaises(frappe.ValidationError) as ctx_book:
			create_provisional_booking(
				package_ids=[self.bookable_pkg.name],
				target_date=holiday_date,
				customer_arrival_time="08:45",
				customer_name="Sam Lee",
				phone="6045550122",
				vehicle_model="Model 3",
				vehicle_color="Pearl White",
				license_plate="HOL 101",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_book.exception, "reason_code", None), "shop_holiday")

		with self.assertRaises(frappe.ValidationError) as ctx_resched:
			reschedule_self_serve_appointment(
				se.name,
				holiday_date,
				"08:45",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_resched.exception, "reason_code", None), "shop_holiday")

	# -------------------------------------------------------------------------
	# 4. Lead-time & horizon enforcement
	# -------------------------------------------------------------------------
	def test_lead_time_and_horizon_enforcement(self):
		self._set_ism_settings(
			self_serve_min_lead_time_minutes=120,
			self_serve_horizon_days=10,
			customer_arrival_offset_minutes=15,
		)
		now_morning = datetime.combine(getdate(today()), time(9, 0, 0))
		today_str = str(now_morning.date())

		# Earliest customer arrival allowed is 11:00 -> internal 11:00 (cust 10:45) omitted, internal 11:30 (cust 11:15) included
		same_day = find_self_serve_slots(
			package_ids=[self.bookable_pkg.name],
			start_date=today_str,
			days_ahead=1,
			vehicle_model="Model 3",
			now_dt=now_morning,
		)
		hhmm_list = [s["customer_arrival_hhmm"] for s in same_day["days"][0]["slots"]]
		self.assertNotIn("10:45", hhmm_list)
		self.assertIn("11:15", hhmm_list)

		with self.assertRaises(frappe.ValidationError) as ctx_lead:
			create_provisional_booking(
				package_ids=[self.bookable_pkg.name],
				target_date=today_str,
				customer_arrival_time="10:45",
				customer_name="Casey Park",
				phone="6045550155",
				vehicle_model="Model 3",
				vehicle_color="Deep Blue",
				license_plate="LED 101",
				now_dt=now_morning,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_lead.exception, "reason_code", None), "lead_time_not_met")

		# Beyond horizon (today + 11 days > horizon 10)
		beyond_date = str(add_days(now_morning.date(), 11))
		beyond_slots = find_self_serve_slots(
			package_ids=[self.bookable_pkg.name],
			start_date=beyond_date,
			days_ahead=3,
			vehicle_model="Model 3",
			now_dt=now_morning,
		)
		self.assertEqual(beyond_slots["total_slots"], 0)

		with self.assertRaises(frappe.ValidationError) as ctx_horizon:
			create_provisional_booking(
				package_ids=[self.bookable_pkg.name],
				target_date=beyond_date,
				customer_arrival_time="08:45",
				customer_name="Casey Park",
				phone="6045550155",
				vehicle_model="Model 3",
				vehicle_color="Deep Blue",
				license_plate="LED 102",
				now_dt=now_morning,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_horizon.exception, "reason_code", None), "outside_horizon")

	# -------------------------------------------------------------------------
	# 5. Reschedule policy, cutoff window, max count, overlapping shift & hard locks
	# -------------------------------------------------------------------------
	def test_reschedule_cutoff_window_and_max_count_and_same_slot(self):
		# Internal 09:00:00 -> T_cust = 08:45:00
		appt_date = getdate(add_days(today(), 4))
		se = self._create_scheduled_entry(appt_date, sched_time="09:00:00", duration=60, bay=self.bay1)

		t_cust = datetime.combine(appt_date, time(8, 45, 0))
		# 24h01m before T_cust -> allowed
		elig_ok = evaluate_reschedule_eligibility(se, now_dt=t_cust - timedelta(hours=24, minutes=1))
		self.assertTrue(elig_ok["allowed"])
		self.assertIsNone(elig_ok["reason_code"])
		self._assert_customer_safe(elig_ok)

		# 23h59m before T_cust -> blocked
		elig_blocked = evaluate_reschedule_eligibility(se, now_dt=t_cust - timedelta(hours=23, minutes=59))
		self.assertFalse(elig_blocked["allowed"])
		self.assertEqual(elig_blocked["reason_code"], "cutoff_window_passed")
		self._assert_customer_safe(elig_blocked)

		# Same-slot no-op rejected without incrementing count
		with self.assertRaises(frappe.ValidationError) as ctx_same:
			reschedule_self_serve_appointment(
				se.name,
				str(appt_date),
				"08:45",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_same.exception, "reason_code", None), "same_slot")
		se.reload()
		self.assertEqual(se.reschedule_count, 0)

		# 1st reschedule: 08:45 (09:00) -> 09:45 (10:00)
		r1 = reschedule_self_serve_appointment(
			se.name,
			str(appt_date),
			"09:45",
			reason="Morning conflict",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_customer_safe(r1)
		self.assertEqual(r1["reschedule_count"], 1)
		self.assertEqual(r1["remaining_reschedules"], 1)
		self.assertEqual(r1["appointment"]["customer_arrival_hhmm"], "09:45")
		se.reload()
		self.assertEqual(se.reschedule_count, 1)
		self.assertIsNotNone(se.last_rescheduled_at)
		self.assertEqual(se.scheduled_time, timedelta(hours=10))

		# 2nd reschedule: 09:45 (10:00) -> 10:45 (11:00)
		r2 = reschedule_self_serve_appointment(
			se.name,
			str(appt_date),
			"10:45",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self.assertEqual(r2["reschedule_count"], 2)
		self.assertEqual(r2["remaining_reschedules"], 0)
		se.reload()
		self.assertEqual(se.reschedule_count, 2)

		# 3rd reschedule blocked
		with self.assertRaises(frappe.ValidationError) as ctx_max:
			reschedule_self_serve_appointment(
				se.name,
				str(appt_date),
				"13:45",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_max.exception, "reason_code", None), "max_reschedules_reached")

		# max_self_reschedules = 0 -> self_reschedule_disabled
		se_fresh = self._create_scheduled_entry(add_days(appt_date, 1), sched_time="09:00:00", bay=self.bay1)
		self._set_ism_settings(max_self_reschedules=0)
		elig_disabled = evaluate_reschedule_eligibility(se_fresh, now_dt=self.now_anchor)
		self.assertFalse(elig_disabled["allowed"])
		self.assertEqual(elig_disabled["reason_code"], "self_reschedule_disabled")

	def test_overlapping_shift_reschedule_with_single_free_bay(self):
		"""SE-1 at 09:00-10:00 shifts to 09:30-10:30 when all other bays are full (uses exclude_entry)."""
		shift_date = str(add_days(self.base_date, 1))
		self._set_ism_settings(enable_technician_capacity=0)

		# Occupy bay2, bay_hv, bay_tire across 08:00-12:00 so ONLY bay1 is open
		for other_bay in (self.bay2, self.bay_hv, self.bay_tire):
			self._create_scheduled_entry(shift_date, sched_time="08:00:00", duration=240, bay=other_bay)

		se1 = self._create_scheduled_entry(shift_date, sched_time="09:00:00", duration=60, bay=self.bay1)

		# Discovery excluding SE-1 must include 09:15 (internal 09:30)
		avail = find_self_serve_slots(
			duration_minutes=60,
			start_date=shift_date,
			days_ahead=1,
			exclude_entry=se1.name,
			now_dt=self.now_anchor,
		)
		hhmm = [s["customer_arrival_hhmm"] for s in avail["days"][0]["slots"]]
		self.assertIn("09:15", hhmm)

		res = reschedule_self_serve_appointment(
			se1.name,
			shift_date,
			"09:15",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_customer_safe(res)
		se1.reload()
		self.assertEqual(se1.scheduled_time, timedelta(hours=9, minutes=30))
		self.assertEqual(se1.service_bay, self.bay1)

	def test_hard_lifecycle_locks_schedule_entry_and_repair_order(self):
		appt_date = add_days(self.base_date, 1)
		se = self._create_scheduled_entry(appt_date, sched_time="09:00:00", bay=self.bay1)

		# Schedule Entry statuses
		for allowed_status in ALLOWED_RESCHEDULE_ENTRY_STATUSES:
			se.db_set("status", allowed_status)
			se.reload()
			res = evaluate_reschedule_eligibility(se, now_dt=self.now_anchor)
			self.assertTrue(res["allowed"], f"Expected {allowed_status} to be allowed")

		for locked_status in ("Draft", "In Progress", "Completed", "No Show", "Cancelled"):
			se.db_set("status", locked_status)
			se.reload()
			res = evaluate_reschedule_eligibility(se, now_dt=self.now_anchor)
			self.assertFalse(res["allowed"], f"Expected {locked_status} to be locked")
			self.assertEqual(res["reason_code"], "locked_schedule_status")

		# Restore SE to Scheduled and attach Repair Order
		se.db_set("status", "Scheduled")
		ro = self.create_test_repair_order(
			customer=self.customer,
			vehicle=self.vehicle,
			odometer_in=25000,
			status="Draft Estimate",
		)
		se.db_set("repair_order", ro.name)
		se.reload()

		for allowed_ro_status in ALLOWED_RESCHEDULE_RO_STATUSES:
			ro.db_set("status", allowed_ro_status)
			res = evaluate_reschedule_eligibility(se, now_dt=self.now_anchor)
			self.assertTrue(res["allowed"], f"Expected RO status {allowed_ro_status} to be allowed")

		for locked_ro_status in LOCKED_RO_STATUSES:
			ro.db_set("status", locked_ro_status)
			res = evaluate_reschedule_eligibility(se, now_dt=self.now_anchor)
			self.assertFalse(res["allowed"], f"Expected RO status {locked_ro_status} to be locked")
			self.assertEqual(res["reason_code"], "locked_repair_order_status")

		# Pre-arrival status but picked_up_at set -> locked
		ro.db_set("status", "Draft Estimate")
		ro.db_set("picked_up_at", self.now_anchor)
		res_pickup = evaluate_reschedule_eligibility(se, now_dt=self.now_anchor)
		self.assertFalse(res_pickup["allowed"])
		self.assertEqual(res_pickup["reason_code"], "locked_repair_order_status")

		# vehicle_released = 1 -> locked
		ro.db_set("picked_up_at", None)
		ro.db_set("vehicle_released", 1)
		res_rel = evaluate_reschedule_eligibility(se, now_dt=self.now_anchor)
		self.assertFalse(res_rel["allowed"])
		self.assertEqual(res_rel["reason_code"], "locked_repair_order_status")

	# -------------------------------------------------------------------------
	# 6. Atomic rollback on swap failure + assigned tech clearing
	# -------------------------------------------------------------------------
	def test_atomic_rollback_preserves_original_slot_on_any_failure(self):
		orig_date = str(self.base_date)
		full_date = str(add_days(self.base_date, 2))
		se = self._create_scheduled_entry(orig_date, sched_time="09:00:00", duration=60, bay=self.bay1)

		# Fill ALL active bays at 10:00-11:00 on full_date (tech capacity off to test bay full cleanly)
		self._set_ism_settings(enable_technician_capacity=0)
		for bay in self.active_bays:
			self._create_scheduled_entry(full_date, sched_time="10:00:00", duration=60, bay=bay)

		# Attempt 1: Target bays full
		with self.assertRaises(frappe.ValidationError) as ctx_full:
			reschedule_self_serve_appointment(
				se.name,
				full_date,
				"09:45",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_full.exception, "reason_code", None), "capacity_conflict")

		# Attempt 2: Off-grid time (e.g. 08:50 -> internal 09:05 not on 30m grid)
		with self.assertRaises(frappe.ValidationError) as ctx_grid:
			reschedule_self_serve_appointment(
				se.name,
				orig_date,
				"08:50",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
		self.assertEqual(getattr(ctx_grid.exception, "reason_code", None), "invalid_slot_time")

		# Attempt 3: Simulated mid-save exception rolls back savepoint completely
		with patch(
			"ism.ism.doctype.schedule_entry.schedule_entry.ScheduleEntry.save",
			side_effect=RuntimeError("Simulated DB failure"),
		):
			with self.assertRaises(RuntimeError):
				reschedule_self_serve_appointment(
					se.name,
					orig_date,
					"13:45",
					now_dt=self.now_anchor,
					emit_notification=False,
				)

		# Reload DB row: 100% unchanged!
		se.reload()
		self.assertEqual(str(se.scheduled_date), orig_date)
		self.assertEqual(se.scheduled_time, timedelta(hours=9))
		self.assertEqual(se.service_bay, self.bay1)
		self.assertEqual(se.reschedule_count, 0)
		self.assertIsNone(se.last_rescheduled_at)

	def test_reschedule_clears_conflicted_technician_when_pool_available(self):
		target_date = str(self.base_date)
		# Tech1 busy at 10:00 on bay2; Tech2 is free -> pool available, Tech1 cleared on SE1
		self._create_scheduled_entry(
			target_date,
			sched_time="10:00:00",
			duration=60,
			bay=self.bay2,
			assigned_technician=self.tech1,
		)
		se1 = self._create_scheduled_entry(
			target_date,
			sched_time="08:00:00",
			duration=60,
			bay=self.bay1,
			assigned_technician=self.tech1,
		)

		res = reschedule_self_serve_appointment(
			se1.name,
			target_date,
			"09:45",
			now_dt=self.now_anchor,
			emit_notification=False,
		)
		self._assert_customer_safe(res)
		se1.reload()
		self.assertEqual(se1.scheduled_time, timedelta(hours=10))
		self.assertIsNone(se1.assigned_technician)

	# -------------------------------------------------------------------------
	# 7. Canonical lock order & concurrent slot-contention race proof
	# -------------------------------------------------------------------------
	def test_canonical_lock_order_on_reschedule(self):
		target_date = str(self.base_date)
		se = self._create_scheduled_entry(target_date, sched_time="08:00:00", duration=60, bay=self.bay1)
		lock_calls = []

		import ism.scheduling.self_serve as ss_mod

		orig_lock_se = ss_mod._lock_schedule_entry
		orig_lock_bays = ss_mod._lock_active_service_bays
		orig_lock_techs = ss_mod._lock_active_technicians

		def wrap_se(name):
			lock_calls.append("Schedule Entry")
			return orig_lock_se(name)

		def wrap_bays():
			lock_calls.append("Service Bay")
			return orig_lock_bays()

		def wrap_techs(settings):
			lock_calls.append("Employee")
			return orig_lock_techs(settings)

		with (
			patch.object(ss_mod, "_lock_schedule_entry", side_effect=wrap_se),
			patch.object(ss_mod, "_lock_active_service_bays", side_effect=wrap_bays),
			patch.object(ss_mod, "_lock_active_technicians", side_effect=wrap_techs),
		):
			reschedule_self_serve_appointment(
				se.name,
				target_date,
				"09:45",
				now_dt=self.now_anchor,
				emit_notification=False,
			)

		self.assertEqual(lock_calls, ["Schedule Entry", "Service Bay", "Employee"])

	def test_concurrent_slot_contention_race_last_open_slot(self):
		"""Two competing threads target the final open 07:45 (08:00) slot: exactly 1 succeeds, 0 double-books."""
		# Roll back uncommitted setUp fixtures before committing race-only rows
		frappe.db.rollback()
		site = frappe.local.site
		race_date = str(add_days(today(), 15))
		unique_tag_name = f"{TEST_PREFIX}RaceOnlyBayTag"
		race_pkg_name = f"{TEST_PREFIX}RacePkg"
		created_se_names = []
		created_pkg = None
		created_tag = None
		added_bay_equip = False

		try:
			# Ensure clean committed settings for worker connections
			self._set_ism_settings(
				operating_hours_start="08:00:00",
				operating_hours_end="17:00:00",
				break_start="12:00:00",
				break_end="12:30:00",
				default_slot_interval=30,
				enable_technician_capacity=1,
				holiday_list=None,
				customer_arrival_offset_minutes=15,
				self_serve_min_lead_time_minutes=120,
				self_serve_horizon_days=30,
			)
			# Create unique equipment tag and attach ONLY to bay1 so exactly 1 bay can accept race_pkg
			if not frappe.db.exists("Equipment Tag", unique_tag_name):
				created_tag = frappe.get_doc({
					"doctype": "Equipment Tag",
					"tag_name": unique_tag_name,
					"description": "Concurrency race test tag",
				}).insert(ignore_permissions=True)

			bay_doc = frappe.get_doc("Service Bay", self.bay1)
			if not any(row.equipment_tag == unique_tag_name for row in (bay_doc.equipment or [])):
				bay_doc.append("equipment", {"equipment_tag": unique_tag_name})
				bay_doc.save(ignore_permissions=True)
				added_bay_equip = True

			created_pkg = frappe.get_doc({
				"doctype": "Service Package",
				"package_name": race_pkg_name,
				"applicable_model": "All Models",
				"standard_labor_hours": 1.0,
				"service_rate": 150.0,
				"allow_self_serve_booking": 1,
				"customer_title": "Race Slot Package",
				"customer_price_display_mode": "Exact Price",
				"customer_estimate_min": 150.0,
				"self_serve_duration_minutes": 60,
				"self_serve_required_tags": unique_tag_name,
			}).insert(ignore_permissions=True)
			frappe.db.commit()

			barrier = threading.Barrier(2)
			results = []
			results_lock = threading.Lock()

			def _worker(idx: int):
				frappe.init(site=site)
				frappe.connect()
				frappe.set_user("Administrator")
				try:
					barrier.wait(timeout=10)
					booking = create_provisional_booking(
						package_ids=[created_pkg.name],
						target_date=race_date,
						customer_arrival_time="07:45",
						customer_name=f"Racer {idx}",
						phone=f"604555019{idx}",
						vehicle_color="Pearl White",
						license_plate=f"RCE 00{idx}",
						now_dt=self.now_anchor,
						emit_notification=False,
					)
					frappe.db.commit()
					with results_lock:
						results.append(("ok", booking["booking_reference"]))
				except Exception as exc:
					frappe.db.rollback()
					with results_lock:
						results.append(("err", exc))
				finally:
					frappe.destroy()

			t1 = threading.Thread(target=_worker, args=(1,))
			t2 = threading.Thread(target=_worker, args=(2,))
			t1.start()
			t2.start()
			t1.join(timeout=30)
			t2.join(timeout=30)

			# Refresh main connection snapshot
			frappe.db.rollback()
			successes = [ref for kind, ref in results if kind == "ok"]
			failures = [exc for kind, exc in results if kind == "err"]
			created_se_names.extend(successes)

			self.assertEqual(len(results), 2, f"Expected 2 thread outcomes, got: {results}")
			self.assertEqual(len(successes), 1, f"Expected exactly 1 winner, got: {results}")
			self.assertEqual(len(failures), 1, f"Expected exactly 1 loser, got: {results}")
			self.assertIsInstance(failures[0], (frappe.ValidationError, frappe.QueryDeadlockError))

			overlapping = frappe.get_all(
				"Schedule Entry",
				filters={
					"service_bay": self.bay1,
					"scheduled_date": race_date,
					"status": ["not in", ["Cancelled", "Completed", "No Show"]],
				},
				pluck="name",
			)
			self.assertEqual(len(overlapping), 1)
			self.assertEqual(overlapping[0], successes[0])
		finally:
			frappe.db.rollback()
			for se_name in created_se_names:
				if frappe.db.exists("Schedule Entry", se_name):
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
			if created_pkg and frappe.db.exists("Service Package", created_pkg.name):
				frappe.delete_doc("Service Package", created_pkg.name, force=True, ignore_permissions=True)
			if added_bay_equip and frappe.db.exists("Service Bay", self.bay1):
				bay_doc = frappe.get_doc("Service Bay", self.bay1)
				bay_doc.equipment = [
					row for row in (bay_doc.equipment or []) if row.equipment_tag != unique_tag_name
				]
				bay_doc.save(ignore_permissions=True)
			if created_tag and frappe.db.exists("Equipment Tag", created_tag.name):
				frappe.delete_doc("Equipment Tag", created_tag.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	# -------------------------------------------------------------------------
	# 8. Provisional intake normalization, Seam 5 events & RO conversion
	# -------------------------------------------------------------------------
	def test_provisional_intake_normalization_and_waiter_policy(self):
		norm = normalize_provisional_intake(
			customer_name="   Jamie   Fox   ",
			phone="(604) 555-0188",
			email="  JAMIE@Example.COM ",
			vehicle_model="tesla model 3",
			vehicle_year=2023,
			vehicle_color="  Pearl   White ",
			license_plate="  ev  1234 ",
			vin_or_plate=" 5yj3e1ea1pf000001 ",
			notes="  Squeak on front right  ",
			waiter_preference="Waiter",
		)
		self.assertEqual(norm["customer_name"], "Jamie Fox")
		self.assertEqual(norm["phone"], "+16045550188")
		self.assertEqual(norm["email"], "jamie@example.com")
		self.assertEqual(norm["vehicle_model"], "Model 3")
		self.assertEqual(norm["vehicle_year"], 2023)
		self.assertEqual(norm["vehicle_color"], "Pearl White")
		self.assertEqual(norm["license_plate"], "EV 1234")
		self.assertEqual(norm["vin_or_plate"], "5YJ3E1EA1PF000001")
		self.assertEqual(norm["provisional_vehicle_info"], "2023 Pearl White Model 3 (EV 1234)")
		self.assertEqual(norm["vehicle_info"]["display"], "2023 Pearl White Model 3 (EV 1234)")
		self.assertEqual(norm["vehicle_info"]["compact_display"], "Pearl White Model 3")
		self.assertEqual(norm["waiter_preference"], "Waiter")

		# Missing color or license_plate rejected
		with self.assertRaises(frappe.ValidationError):
			normalize_provisional_intake(
				customer_name="Jamie Fox",
				phone="6045550188",
				license_plate="EV 1234",
			)
		with self.assertRaises(frappe.ValidationError):
			normalize_provisional_intake(
				customer_name="Jamie Fox",
				phone="6045550188",
				vehicle_color="Pearl White",
			)

		# Disable waiter preference -> Waiter rejected, None defaults to Drop-Off
		self._set_ism_settings(enable_waiter_preference=0)
		with self.assertRaises(frappe.ValidationError):
			normalize_provisional_intake(
				customer_name="Jamie Fox",
				phone="6045550188",
				vehicle_color="Pearl White",
				license_plate="EV 1234",
				waiter_preference="Waiter",
			)
		norm_dropoff = normalize_provisional_intake(
			customer_name="Jamie Fox",
			phone="6045550188",
			vehicle_color="Pearl White",
			license_plate="EV 1234",
		)
		self.assertEqual(norm_dropoff["waiter_preference"], "Drop-Off")

		# Invalid phone & email rejected
		with self.assertRaises(frappe.ValidationError):
			normalize_provisional_intake(
				customer_name="Jamie Fox",
				phone="123",
				vehicle_color="Pearl White",
				license_plate="EV 1234",
			)
		with self.assertRaises(frappe.ValidationError):
			normalize_provisional_intake(
				customer_name="Jamie Fox",
				phone="6045550188",
				email="not-an-email",
				vehicle_color="Pearl White",
				license_plate="EV 1234",
			)

	def test_provisional_booking_seam5_action_item_and_ro_conversion(self):
		# Use manifest customer & vehicle attributes so create_repair_order_from_schedule_entry resolves cleanly
		cust_name = frappe.db.get_value("Customer", self.customer, "customer_name")
		veh = frappe.db.get_value(
			"Repair Vehicle",
			self.vehicle,
			["model_year", "model", "color", "vin", "license_plate"],
			as_dict=True,
		)
		target_date = str(self.base_date)

		hook_calls = []

		def _fake_hook(event_type, envelope):
			hook_calls.append((event_type, envelope))

		real_get_hooks = frappe.get_hooks

		def _scoped_get_hooks(hook=None, default=None, app_name=None):
			if hook == "ism_scheduling_domain_events":
				return [_fake_hook]
			return real_get_hooks(hook=hook, default=default, app_name=app_name)

		with (
			patch("frappe.publish_realtime") as mock_rt,
			patch("frappe.get_hooks", side_effect=_scoped_get_hooks),
		):
			booking = create_provisional_booking(
				package_ids=[self.bookable_pkg.name],
				target_date=target_date,
				customer_arrival_time="07:45",
				customer_name=cust_name,
				phone="604-555-0177",
				email="customer@example.com",
				vehicle_model=veh.model,
				vehicle_year=veh.model_year,
				vehicle_color=veh.color or "Pearl White",
				license_plate=veh.license_plate or "TST 999",
				vin_or_plate=veh.vin or veh.license_plate,
				notes="Check cabin filter odor",
				waiter_preference="Drop-Off",
				now_dt=self.now_anchor,
				emit_notification=True,
			)

		self._assert_customer_safe(booking)
		self.assertIn(veh.color or "Pearl White", booking["vehicle_display"])
		self.assertIn(veh.license_plate or "TST 999", booking["vehicle_display"])
		self.assertEqual(booking["vehicle_info"]["color"], veh.color or "Pearl White")
		self.assertEqual(booking["vehicle_info"]["license_plate"], (veh.license_plate or "TST 999").upper())
		se = frappe.get_doc("Schedule Entry", booking["booking_reference"])
		self.track(se)
		self.assertEqual(se.provisional_vehicle_color, veh.color or "Pearl White")
		self.assertEqual(se.provisional_license_plate, (veh.license_plate or "TST 999").upper())

		self.assertEqual(se.booking_source, "Website Self-Serve")
		self.assertEqual(se.waiter_preference, "Drop-Off")
		self.assertEqual(se.provisional_phone, "+16045550177")
		self.assertIn("Cabin Air Filter Service", se.items_summary)
		parsed_pkgs = json.loads(se.booked_packages_json)
		self.assertEqual(len(parsed_pkgs), 1)
		self.assertEqual(parsed_pkgs[0]["package_id"], self.bookable_pkg.name)

		# Seam 5 Shop Action Item created with dedup_key and BOTH customer & internal times
		dedup_key = f"self_serve_booking:{se.name}"
		sai_name = frappe.db.get_value("Shop Action Item", {"dedup_key": dedup_key}, "name")
		self.assertIsNotNone(sai_name)
		sai = frappe.get_doc("Shop Action Item", sai_name)
		self.track(sai)
		self.assertEqual(sai.category, "General")
		self.assertEqual(sai.source, "Agent")
		self.assertEqual(sai.reference_doctype, "Schedule Entry")
		self.assertEqual(sai.reference_name, se.name)
		self.assertIn("7:45 AM", sai.details)
		self.assertIn("08:00:00", sai.details)

		# Realtime & hook invoked
		rt_events = [call.args[0] for call in mock_rt.call_args_list if call.args]
		self.assertIn("ism_scheduling_domain_event", rt_events)
		self.assertEqual(len(hook_calls), 1)
		self.assertEqual(hook_calls[0][0], "self_serve_booking_created")

		# Deduplication: re-emitting updates existing Shop Action Item rather than creating duplicate
		second_sai = emit_scheduling_domain_event(
			"self_serve_booking_created",
			{
				"schedule_entry": se.name,
				"customer_display": cust_name,
				"scheduled_date": target_date,
				"internal_start_time": "08:00:00",
				"customer_arrival_time": "07:45:00",
				"customer_display_datetime": booking["appointment"]["display_datetime"],
			},
		)
		self.assertEqual(second_sai, sai_name)

		# Error isolation: broken hook never raises
		def _boom(_evt, _env):
			raise RuntimeError("Downstream subscriber failed")

		def _scoped_boom_hooks(hook=None, default=None, app_name=None):
			if hook == "ism_scheduling_domain_events":
				return [_boom]
			return real_get_hooks(hook=hook, default=default, app_name=app_name)

		with patch("frappe.get_hooks", side_effect=_scoped_boom_hooks):
			emit_scheduling_domain_event(
				"self_serve_booking_created",
				{"schedule_entry": se.name},
				create_action_item=False,
			)

		# Seamless advisor conversion via create_repair_order_from_schedule_entry
		ro_res = create_repair_order_from_schedule_entry(schedule_entry=se.name, odometer_in=42000)
		self.assertTrue(ro_res.get("repair_order"))
		self.track("Repair Order", ro_res["repair_order"])
		se.reload()
		self.assertEqual(se.repair_order, ro_res["repair_order"])
		self.assertEqual(se.customer, self.customer)
		self.assertEqual(se.repair_vehicle, self.vehicle)

	# -------------------------------------------------------------------------
	# 9. Public Guest Security & API Endpoint Boundaries
	# -------------------------------------------------------------------------
	def test_guest_and_authenticated_api_security_boundaries(self):
		target_date = str(self.base_date)
		hidden_pkg = self._create_bookable_package(
			"Hidden_Guest_Test",
			allow_self_serve_booking=0,
			customer_title="Internal Only",
		)
		se = self._create_scheduled_entry(target_date, sched_time="09:00:00", bay=self.bay1)

		# Switch to Guest
		frappe.set_user("Guest")

		# 1. get_bookable_packages allowed for Guest, zero internal leaks
		catalog_res = get_bookable_packages(vehicle_model="Model 3")
		self._assert_customer_safe(catalog_res)
		pkg_ids = {p["package_id"] for p in catalog_res["packages"]}
		self.assertIn(self.bookable_pkg.name, pkg_ids)
		self.assertNotIn(hidden_pkg.name, pkg_ids)
		self.assertIn("enable_waiter_preference", catalog_res["policy"])
		self.assertIn("horizon_days", catalog_res["policy"])
		self.assertIn("currency", catalog_res["policy"])

		# 2. get_self_serve_slots allowed for Guest with curated package_ids, capped days_ahead, zero leaks
		slots_res = get_self_serve_slots(
			package_ids=[self.bookable_pkg.name],
			start_date=target_date,
			days_ahead=120,
			vehicle_model="Model 3",
			max_slots_per_day=3,
		)
		self._assert_customer_safe(slots_res)
		self.assertGreater(slots_res["total_slots"], 0)
		span_days = (getdate(slots_res["end_date"]) - getdate(slots_res["start_date"])).days + 1
		self.assertLessEqual(span_days, min(get_self_serve_policy()["self_serve_horizon_days"], 60))

		# Empty package_ids or non-bookable package rejected for Guest
		with self.assertRaises(frappe.ValidationError):
			get_self_serve_slots(package_ids=[], start_date=target_date)
		with self.assertRaises(frappe.ValidationError):
			get_self_serve_slots(package_ids=None, start_date=target_date)
		with self.assertRaises(frappe.ValidationError):
			get_self_serve_slots(package_ids=[hidden_pkg.name], start_date=target_date)

		# 3. Authenticated endpoints MUST throw PermissionError for Guest
		with self.assertRaises(frappe.PermissionError):
			get_reschedule_policy_and_slots(schedule_entry=se.name, start_date=target_date)

		with self.assertRaises(frappe.PermissionError):
			execute_self_serve_reschedule(
				schedule_entry=se.name,
				target_date=target_date,
				customer_arrival_time="10:45",
			)

		with self.assertRaises(frappe.PermissionError):
			submit_provisional_booking(
				package_ids=[self.bookable_pkg.name],
				target_date=target_date,
				customer_arrival_time="07:45",
				customer_name="Guest Attempt",
				phone="6045550100",
				vehicle_model="Model 3",
			)

		# 4. Authenticated user succeeds on authenticated endpoints
		frappe.set_user("Administrator")
		policy_res = get_reschedule_policy_and_slots(
			schedule_entry=se.name,
			start_date=target_date,
			days_ahead=1,
		)
		self._assert_customer_safe(policy_res)
		self.assertTrue(policy_res["eligibility"]["allowed"])
		self.assertGreater(policy_res["slots"]["total_slots"], 0)

		exec_res = execute_self_serve_reschedule(
			schedule_entry=se.name,
			target_date=target_date,
			customer_arrival_time="10:45",
			reason="Advisor assisted",
		)
		self._assert_customer_safe(exec_res)
		self.assertEqual(exec_res["appointment"]["customer_arrival_hhmm"], "10:45")

		book_res = submit_provisional_booking(
			package_ids=[self.bookable_pkg.name],
			target_date=target_date,
			customer_arrival_time="07:45",
			customer_name="Riley Adams",
			phone="6045550133",
			vehicle_model="Model 3",
			vehicle_year=2023,
			vehicle_color="Solid Black",
			license_plate="RLY 133",
		)
		self._assert_customer_safe(book_res)
		self.track("Schedule Entry", book_res["booking_reference"])
		# Track generated Shop Action Item if any
		sai_book = frappe.db.get_value(
			"Shop Action Item",
			{"dedup_key": f"self_serve_booking:{book_res['booking_reference']}"},
			"name",
		)
		if sai_book:
			self.track("Shop Action Item", sai_book)
		sai_resched = frappe.db.get_value(
			"Shop Action Item",
			{"dedup_key": f"self_serve_reschedule:{se.name}:1"},
			"name",
		)
		if sai_resched:
			self.track("Shop Action Item", sai_resched)

	def test_guest_package_ids_bypass_and_direct_empty_list_rejected(self):
		"""Finding #1 regression: Guest get_self_serve_slots and find_self_serve_slots reject all falsy/invalid package_ids."""
		target_date = str(self.base_date)

		self.assertEqual(
			_validate_required_package_ids("[STAGING TEST] Brake Service & Inspection"),
			["[STAGING TEST] Brake Service & Inspection"],
		)
		for bad_raw in ("[bad", '["unterminated', "{bad", "{}", "[]", "[PKG-1, PKG-2]", "[] foo"):
			with self.subTest(raw_package_ids=bad_raw):
				with self.assertRaises(frappe.ValidationError):
					_validate_required_package_ids(bad_raw)

		frappe.set_user("Guest")
		for bad_value in ({}, False, 0, 0.0, "", "   ", "[]", "{}", "{bad", [], [123], [True], None):
			with self.subTest(endpoint_package_ids=bad_value):
				with self.assertRaises(frappe.ValidationError):
					get_self_serve_slots(package_ids=bad_value, start_date=target_date)

		frappe.set_user("Administrator")
		for bad_direct in ([], {}, False, 0, "", "[]"):
			with self.subTest(direct_package_ids=bad_direct):
				with self.assertRaises(frappe.ValidationError):
					find_self_serve_slots(
						package_ids=bad_direct,
						start_date=target_date,
						now_dt=self.now_anchor,
					)

	def test_repeatable_read_snapshot_toctou_reschedule_max_and_ro_checkin(self):
		"""Finding #2 regression: pre-opened REPEATABLE READ snapshot sees concurrent reschedule & RO check-in."""
		frappe.db.rollback()
		site = frappe.local.site
		appt_date = str(add_days(today(), 16))
		created_se_names = []
		created_ro_names = []

		try:
			self._set_ism_settings(
				operating_hours_start="08:00:00",
				operating_hours_end="17:00:00",
				break_start="12:00:00",
				break_end="12:30:00",
				default_slot_interval=30,
				enable_technician_capacity=1,
				holiday_list=None,
				customer_arrival_offset_minutes=15,
				reschedule_cutoff_hours=24,
				max_self_reschedules=2,
				self_serve_min_lead_time_minutes=120,
				self_serve_horizon_days=30,
			)

			# Fixture A: SE already at reschedule_count=1 (max=2)
			se_max = frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"status": "Scheduled",
				"scheduled_date": appt_date,
				"scheduled_time": "09:00:00",
				"estimated_duration": 60,
				"service_bay": self.bay1,
				"provisional_customer_name": "TOCTOU Max User",
				"provisional_phone": "+16045550191",
				"booking_source": "Website Self-Serve",
				"reschedule_count": 1,
			}).insert(ignore_permissions=True)
			created_se_names.append(se_max.name)

			# Fixture B: SE at reschedule_count=0 (concurrent reschedule #1 -> thread 1 performs #2 without TimestampMismatch)
			se_chain = frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"status": "Scheduled",
				"scheduled_date": appt_date,
				"scheduled_time": "10:00:00",
				"estimated_duration": 60,
				"service_bay": self.bay2,
				"provisional_customer_name": "TOCTOU Chain User",
				"provisional_phone": "+16045550192",
				"booking_source": "Website Self-Serve",
				"reschedule_count": 0,
			}).insert(ignore_permissions=True)
			created_se_names.append(se_chain.name)

			# Fixture C: SE linked to Draft Estimate RO that gets Checked In concurrently
			ro_doc = frappe.get_doc({
				"doctype": "Repair Order",
				"customer": self.customer,
				"vehicle": self.vehicle,
				"odometer_in": 31000,
				"status": "Draft Estimate",
				"posting_date": today(),
			}).insert(ignore_permissions=True)
			created_ro_names.append(ro_doc.name)

			se_ro = frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"status": "Scheduled",
				"scheduled_date": appt_date,
				"scheduled_time": "11:00:00",
				"estimated_duration": 60,
				"service_bay": self.bay_hv,
				"repair_order": ro_doc.name,
				"booking_source": "Website Self-Serve",
				"reschedule_count": 0,
			}).insert(ignore_permissions=True)
			created_se_names.append(se_ro.name)
			frappe.db.commit()

			def _run_in_worker(fn):
				out = []

				def _target():
					frappe.init(site=site)
					frappe.connect()
					frappe.set_user("Administrator")
					try:
						out.append(("ok", fn()))
						frappe.db.commit()
					except Exception as exc:
						frappe.db.rollback()
						out.append(("err", exc))
					finally:
						frappe.destroy()

				t = threading.Thread(target=_target)
				t.start()
				t.join(timeout=30)
				self.assertEqual(len(out), 1)
				kind, val = out[0]
				if kind == "err":
					raise val
				return val

			# --- Scenario A: Thread 1 opens REPEATABLE READ snapshot, Thread 2 hits max_self_reschedules=2 ---
			frappe.db.rollback()
			self.assertTrue(frappe.db.exists("Schedule Entry", se_max.name))
			stale_doc_a = frappe.get_doc("Schedule Entry", se_max.name)
			self.assertEqual(stale_doc_a.reschedule_count, 1)

			_run_in_worker(
				lambda: reschedule_self_serve_appointment(
					se_max.name,
					appt_date,
					"12:45",
					now_dt=self.now_anchor,
					emit_notification=False,
				)
			)

			# Thread 1's snapshot still has stale reschedule_count=1, but locked reschedule MUST see 2 -> max_reschedules_reached
			with self.assertRaises(frappe.ValidationError) as ctx_max:
				reschedule_self_serve_appointment(
					se_max.name,
					appt_date,
					"13:45",
					now_dt=self.now_anchor,
					emit_notification=False,
				)
			self.assertEqual(getattr(ctx_max.exception, "reason_code", None), "max_reschedules_reached")

			# --- Scenario B: Thread 1 opens snapshot (count=0), Thread 2 commits count=1 -> Thread 1 cleanly updates to count=2 ---
			frappe.db.rollback()
			stale_doc_b = frappe.get_doc("Schedule Entry", se_chain.name)
			self.assertEqual(stale_doc_b.reschedule_count, 0)

			_run_in_worker(
				lambda: reschedule_self_serve_appointment(
					se_chain.name,
					appt_date,
					"14:15",
					now_dt=self.now_anchor,
					emit_notification=False,
				)
			)

			res_b = reschedule_self_serve_appointment(
				se_chain.name,
				appt_date,
				"15:15",
				now_dt=self.now_anchor,
				emit_notification=False,
			)
			frappe.db.commit()
			self.assertEqual(res_b["reschedule_count"], 2)
			self.assertEqual(res_b["remaining_reschedules"], 0)
			self.assertEqual(res_b["appointment"]["customer_arrival_hhmm"], "15:15")

			# --- Scenario C: Thread 1 opens snapshot seeing Draft Estimate RO, Thread 2 checks in RO -> locked_repair_order_status ---
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value("Repair Order", ro_doc.name, "status"), "Draft Estimate")
			frappe.get_doc("Schedule Entry", se_ro.name)

			_run_in_worker(lambda: frappe.db.set_value("Repair Order", ro_doc.name, "status", "Checked In"))

			with self.assertRaises(frappe.ValidationError) as ctx_ro:
				reschedule_self_serve_appointment(
					se_ro.name,
					appt_date,
					"13:15",
					now_dt=self.now_anchor,
					emit_notification=False,
				)
			self.assertEqual(getattr(ctx_ro.exception, "reason_code", None), "locked_repair_order_status")
		finally:
			frappe.db.rollback()
			for se_name in created_se_names:
				if frappe.db.exists("Schedule Entry", se_name):
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
			for ro_name in created_ro_names:
				if frappe.db.exists("Repair Order", ro_name):
					frappe.delete_doc("Repair Order", ro_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_er_checkread_1020_does_not_rollback_outer_uncommitted_writes_or_callbacks(self):
		"""ER_CHECKREAD (1020) must NOT rollback when outer caller has uncommitted writes or pending callbacks."""
		frappe.db.rollback()
		site = frappe.local.site
		appt_date = str(add_days(today(), 17))
		created_se_names = []
		created_sai_names = []

		try:
			self._set_ism_settings(
				operating_hours_start="08:00:00",
				operating_hours_end="17:00:00",
				break_start="12:00:00",
				break_end="12:30:00",
				default_slot_interval=30,
				enable_technician_capacity=1,
				holiday_list=None,
				customer_arrival_offset_minutes=15,
				reschedule_cutoff_hours=24,
				max_self_reschedules=5,
				self_serve_min_lead_time_minutes=120,
				self_serve_horizon_days=30,
			)

			se_outer = frappe.get_doc({
				"doctype": "Schedule Entry",
				"entry_type": "Diagnostic",
				"status": "Scheduled",
				"scheduled_date": appt_date,
				"scheduled_time": "09:00:00",
				"estimated_duration": 60,
				"service_bay": self.bay1,
				"provisional_customer_name": "Outer Tx User",
				"provisional_phone": "+16045550195",
				"booking_source": "Website Self-Serve",
				"reschedule_count": 0,
			}).insert(ignore_permissions=True)
			created_se_names.append(se_outer.name)

			probe_sai = frappe.get_doc({
				"doctype": "Shop Action Item",
				"title": f"{TEST_PREFIX}OuterTxProbe",
				"category": "General",
				"priority": "Low",
				"status": "Open",
				"source": "Agent",
				"reported_by": "Administrator",
				"details": "initial_state",
			}).insert(ignore_permissions=True)
			created_sai_names.append(probe_sai.name)
			frappe.db.commit()

			def _commit_concurrent_reschedule(target_hhmm: str):
				out = []

				def _worker():
					frappe.init(site=site)
					frappe.connect()
					frappe.set_user("Administrator")
					try:
						reschedule_self_serve_appointment(
							se_outer.name,
							appt_date,
							target_hhmm,
							now_dt=self.now_anchor,
							emit_notification=False,
						)
						frappe.db.commit()
						out.append(("ok", None))
					except Exception as exc:
						frappe.db.rollback()
						out.append(("err", exc))
					finally:
						frappe.destroy()

				t = threading.Thread(target=_worker)
				t.start()
				t.join(timeout=30)
				self.assertEqual(len(out), 1)
				if out[0][0] == "err":
					raise out[0][1]

			# Case 1: Outer caller has an uncommitted write (transaction_writes > 0) before ER_CHECKREAD (1020)
			frappe.db.rollback()
			self.assertEqual(frappe.db.transaction_writes, 0)
			frappe.get_doc("Schedule Entry", se_outer.name)
			frappe.db.set_value("Shop Action Item", probe_sai.name, "details", "outer_otp_consumed")
			self.assertGreater(frappe.db.transaction_writes, 0)

			_commit_concurrent_reschedule("10:45")

			with patch.object(frappe.db, "rollback", wraps=frappe.db.rollback) as rollback_spy:
				with self.assertRaises(frappe.QueryDeadlockError) as ctx_1020:
					reschedule_self_serve_appointment(
						se_outer.name,
						appt_date,
						"13:45",
						now_dt=self.now_anchor,
						emit_notification=False,
					)
				self.assertIn("1020", str(ctx_1020.exception))
				self.assertEqual(rollback_spy.call_count, 0)

			# Outer transaction aborted cleanly by MariaDB 1020 (NOT swallowed to commit reschedule without outer write):
			# neither the outer write nor the second reschedule committed; DB retains worker's 11:00:00 / count=1 state.
			frappe.db.rollback()
			self.assertEqual(
				frappe.db.get_value("Shop Action Item", probe_sai.name, "details"),
				"initial_state",
			)
			se_after_case1 = frappe.db.get_value(
				"Schedule Entry",
				se_outer.name,
				["scheduled_time", "reschedule_count"],
				as_dict=True,
			)
			self.assertEqual(se_after_case1.scheduled_time, timedelta(hours=11))
			self.assertEqual(se_after_case1.reschedule_count, 1)

			# Case 2: transaction_writes == 0 but pending after_commit callback registered -> must NOT rollback
			frappe.db.rollback()
			self.assertEqual(frappe.db.transaction_writes, 0)
			frappe.get_doc("Schedule Entry", se_outer.name)
			sentinel_called = []
			frappe.db.after_commit.add(lambda: sentinel_called.append(True))
			self.assertGreater(len(frappe.db.after_commit), 0)

			_commit_concurrent_reschedule("11:15")

			with patch.object(frappe.db, "rollback", wraps=frappe.db.rollback) as rollback_spy_cb:
				with self.assertRaises(frappe.QueryDeadlockError) as ctx_cb:
					reschedule_self_serve_appointment(
						se_outer.name,
						appt_date,
						"14:45",
						now_dt=self.now_anchor,
						emit_notification=False,
					)
				self.assertIn("1020", str(ctx_cb.exception))
				self.assertEqual(rollback_spy_cb.call_count, 0)

			self.assertGreater(len(frappe.db.after_commit), 0)
			frappe.db.after_commit.reset()
			frappe.db.rollback()
			se_after_case2 = frappe.db.get_value(
				"Schedule Entry",
				se_outer.name,
				["scheduled_time", "reschedule_count"],
				as_dict=True,
			)
			self.assertEqual(se_after_case2.scheduled_time, timedelta(hours=11, minutes=30))
			self.assertEqual(se_after_case2.reschedule_count, 2)

			# Case 3: _disable_transaction_control=True blocks restart even when transaction_writes == 0
			import ism.scheduling.self_serve as ss_mod

			frappe.db.rollback()
			self.assertTrue(ss_mod._can_safely_restart_read_transaction())
			orig_dtc = frappe.db._disable_transaction_control
			try:
				frappe.db._disable_transaction_control = True
				self.assertFalse(ss_mod._can_safely_restart_read_transaction())
			finally:
				frappe.db._disable_transaction_control = orig_dtc
		finally:
			frappe.db.rollback()
			for se_name in created_se_names:
				if frappe.db.exists("Schedule Entry", se_name):
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
			for sai_name in created_sai_names:
				if frappe.db.exists("DocType", "Shop Board Event"):
					prev_board_td = getattr(frappe.flags, "in_board_teardown", False)
					frappe.flags.in_board_teardown = True
					try:
						for ev_name in frappe.get_all(
							"Shop Board Event",
							filters={"idempotency_key": f"action_item:{sai_name}"},
							pluck="name",
						):
							frappe.delete_doc("Shop Board Event", ev_name, force=True, ignore_permissions=True)
					finally:
						frappe.flags.in_board_teardown = prev_board_td
				if frappe.db.exists("Shop Action Item", sai_name):
					frappe.delete_doc("Shop Action Item", sai_name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_constants_exported(self):
		self.assertEqual(ALLOWED_RESCHEDULE_ENTRY_STATUSES, ("Scheduled", "Confirmed"))
		self.assertEqual(ALLOWED_RESCHEDULE_RO_STATUSES, ("Draft Estimate", "Pending Authorization"))
		self.assertIn("Checked In", LOCKED_RO_STATUSES)
		self.assertIn("Website Self-Serve", VALID_BOOKING_SOURCES)
		self.assertEqual(VALID_WAITER_PREFERENCES, ("Drop-Off", "Waiter", "Not Specified"))

	def test_provisional_vehicle_generation_persisted_on_schedule_entry(self):
		norm = normalize_provisional_intake(
			customer_name="Taylor Moss",
			phone="6045550177",
			vehicle_model="Model S",
			vehicle_year=2021,
			vehicle_color="Pearl White",
			license_plate="EV 2021",
			vehicle_generation="Model S Apr 2016 - Jan 2021",
		)
		self.assertEqual(norm["vehicle_generation"], "Model S Apr 2016 - Jan 2021")
		self.assertEqual(norm["provisional_vehicle_info"], "2021 Pearl White Model S (EV 2021)")

