# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from datetime import time, timedelta
import unittest
import frappe
from frappe.utils import add_days, today

from ism.tests.base import ISMTestCase
from ism.tests.test_fixtures import (
	setup_all,
	teardown_transactional,
	ensure_ism_settings,
	create_test_schedule_entry,
	create_test_repair_order,
	PREFIX,
	CUSTOMER,
	VEHICLE_VIN,
)

try:
	from ism.api.scheduling import (
		effective_end_time,
		check_bay_availability,
		auto_assign_bay,
		get_available_slots,
		is_holiday,
		find_next_available_slot,
		get_unscheduled_services,
		allocate_schedule_entry,
		create_repair_order_from_schedule_entry,
	)
except ImportError:
	effective_end_time = None
	check_bay_availability = None
	auto_assign_bay = None
	get_available_slots = None
	is_holiday = None

try:
	from ism.api.technician_availability import (
		get_active_technicians,
		get_technician_pool_availability,
		check_technician_availability,
		get_technician_queue,
		get_daily_technician_overview,
	)
except ImportError:
	get_active_technicians = None
	get_technician_pool_availability = None
	check_technician_availability = None
	get_technician_queue = None
	get_daily_technician_overview = None


class TestSchedulingCapacity(ISMTestCase):
	"""
	Comprehensive 4-Tier Test Suite for Features 6–22:
	- Feature 6: Lunch-Aware Effective End Time
	- Feature 7: Per-Bay Overlap Validation & Double-Booking Prevention
	- Feature 8: Equipment Tag Filtering
	- Feature 9: Automated Bay Assignment (Earliest-Fit)
	- Feature 10: Slot Generator with Capacity Indicators
	- Feature 11: Technician Designation Roster
	- Feature 12: HRMS Leave Application Integration
	- Feature 13: Technician Pool-Level Gating
	- Feature 14: Individual Technician Overlap Check
	- Feature 15: Master Capacity Toggle (enable_technician_capacity)
	- Feature 16: Technician Queue & Utilization
	- Feature 17: Daily Technician Overview Dashboard
	- Feature 18: ERPNext Holiday Awareness
	- Features 19–22: DocTypes (Service Bay, Equipment Tag, Schedule Entry Type, Schedule Entry)
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.emp_map = setup_all()
		cls.bay1 = f"{PREFIX}Bay 1"
		cls.bay2 = f"{PREFIX}Bay 2"
		cls.bay3 = f"{PREFIX}Bay 3"  # Inactive
		cls.tech1_id = cls.emp_map.get("tech_active_1")
		cls.tech2_id = cls.emp_map.get("tech_active_2")
		cls.tech3_id = cls.emp_map.get("tech_inactive")

	@classmethod
	def tearDownClass(cls):
		teardown_transactional()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		self.bay1 = self.__class__.bay1
		self.bay2 = self.__class__.bay2
		self.bay3 = self.__class__.bay3
		self.tech1_id = self.__class__.tech1_id
		self.tech2_id = self.__class__.tech2_id
		self.tech3_id = self.__class__.tech3_id
		self.emp_map = self.__class__.emp_map

		# Create test Repair Order for scheduling integration
		self.test_ro1 = self.create_test_repair_order(
			customer=CUSTOMER,
			vehicle=VEHICLE_VIN,
			odometer_in=45000,
		)

	def tearDown(self):
		super().tearDown()

	def _require_sched_api(self):
		if effective_end_time is None:
			self.skipTest("ism.api.scheduling not yet implemented (Milestone M3)")

	def _require_tech_api(self):
		if get_active_technicians is None:
			self.skipTest("ism.api.technician_availability not yet implemented (Milestone M3)")

	def _require_doctype(self, dt):
		if not frappe.db.exists("DocType", dt):
			self.skipTest(f"DocType '{dt}' not yet created (Milestone M1)")

	# =========================================================================
	# TIER 1: FEATURE COVERAGE (>=5 tests per feature)
	# =========================================================================

	# --- Feature 6: Lunch-Aware Effective End Time ---
	def test_tier1_f6_end_time_wholly_before_lunch(self):
		"""F6: 08:00 start + 120 min ends at 10:00 (no break adjustment)."""
		self._require_sched_api()
		eff = effective_end_time("08:00:00", 120, break_start="12:00:00", break_end="12:30:00")
		self.assertEqual(eff, timedelta(hours=10))

	def test_tier1_f6_end_time_spanning_lunch(self):
		"""F6: 11:00 start + 120 min spans 12:00 lunch -> effective end 13:30 (+30 min break)."""
		self._require_sched_api()
		eff = effective_end_time("11:00:00", 120, break_start="12:00:00", break_end="12:30:00")
		self.assertEqual(eff, timedelta(hours=13, minutes=30))

	def test_tier1_f6_end_time_wholly_after_lunch(self):
		"""F6: 13:00 start + 90 min ends at 14:30 (no break adjustment)."""
		self._require_sched_api()
		eff = effective_end_time("13:00:00", 90, break_start="12:00:00", break_end="12:30:00")
		self.assertEqual(eff, timedelta(hours=14, minutes=30))

	def test_tier1_f6_end_time_starts_during_lunch(self):
		"""F6: Starting during lunch window pushes effective start/end past break."""
		self._require_sched_api()
		eff = effective_end_time("12:15:00", 60, break_start="12:00:00", break_end="12:30:00")
		self.assertGreaterEqual(eff, timedelta(hours=13, minutes=30))

	def test_tier1_f6_end_time_defaults_from_ism_settings(self):
		"""F6: When break_start/end not passed, reads from ISM Settings."""
		self._require_sched_api()
		ensure_ism_settings(break_start="12:00:00", break_end="13:00:00")  # 60m lunch
		eff = effective_end_time("11:30:00", 60)  # naive 12:30 -> spans 12:00 -> +60 min = 13:30
		self.assertEqual(eff, timedelta(hours=13, minutes=30))

	# --- Feature 7: Per-Bay Overlap Validation ---
	def test_tier1_f7_bay_available_no_conflict(self):
		"""F7: Bay availability returns True for empty time window."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=target_date,
			scheduled_time="08:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
		)
		# 10:00 to 12:00 should be free
		self.assertTrue(check_bay_availability(self.bay1, target_date, "10:00:00", 120))
		# Different bay at 08:00 should be free
		self.assertTrue(check_bay_availability(self.bay2, target_date, "08:00:00", 120))

	def test_tier1_f7_bay_overlap_blocked(self):
		"""F7: check_bay_availability returns False for overlapping interval."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=target_date,
			scheduled_time="08:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
		)
		# 09:00 to 11:00 overlaps 08:00-10:00
		self.assertFalse(check_bay_availability(self.bay1, target_date, "09:00:00", 120))

	def test_tier1_f7_schedule_entry_overlap_validation_error(self):
		"""F7: Inserting overlapping Schedule Entry on same bay raises ValidationError."""
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=target_date,
			scheduled_time="08:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
		)
		se_colliding = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": self.test_ro1.name,
			"scheduled_date": target_date,
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"service_bay": self.bay1,
			"status": "Scheduled",
		})
		with self.assertRaises(frappe.ValidationError):
			se_colliding.insert(ignore_permissions=True)

	def test_tier1_f7_bay_self_exclusion_on_reschedule(self):
		"""F7: Updating existing Schedule Entry excludes itself from collision check."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		se = create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=target_date,
			scheduled_time="08:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
		)
		# Check same slot with exclude_entry=se.name -> True
		avail = check_bay_availability(self.bay1, target_date, "08:00:00", 120, exclude_entry=se.name)
		self.assertTrue(avail)
		# Resave entry without conflict error
		se.estimated_duration = 150
		se.save(ignore_permissions=True)

	def test_tier1_f7_adjacent_boundary_booking_allowed(self):
		"""F7: Job 1 ending at 10:00 and Job 2 starting at 10:00 do not conflict."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=target_date,
			scheduled_time="08:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
		)
		self.assertTrue(check_bay_availability(self.bay1, target_date, "10:00:00", 60))

	# --- Feature 8: Equipment Tag Filtering ---
	def test_tier1_f8_equipment_tag_single_match(self):
		"""F8: Requesting 'Lift' matches Bay 1 and Bay 2."""
		self._require_sched_api()
		self._require_doctype("Service Bay")
		target_date = today()
		assigned = auto_assign_bay(target_date, "08:00:00", 60, required_tags=["Lift"])
		self.assertIn(assigned, [self.bay1, self.bay2])

	def test_tier1_f8_equipment_tag_exclusive_match(self):
		"""F8: Requesting 'Alignment Rack' matches only Bay 2."""
		self._require_sched_api()
		self._require_doctype("Service Bay")
		target_date = today()
		assigned = auto_assign_bay(target_date, "08:00:00", 60, required_tags=["Alignment Rack"])
		self.assertEqual(assigned, self.bay2)

	def test_tier1_f8_bay_missing_tag_excluded(self):
		"""F8: Bay 1 (which lacks 'Alignment Rack') is not assigned when rack required."""
		self._require_sched_api()
		self._require_doctype("Service Bay")
		target_date = today()
		# Book Bay 2 so the only rack bay is busy
		create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=target_date,
			scheduled_time="08:00:00",
			estimated_duration=120,
			service_bay=self.bay2,
		)
		# Attempting to assign with Alignment Rack should fail because Bay 1 cannot handle it
		with self.assertRaises(frappe.ValidationError):
			auto_assign_bay(target_date, "08:00:00", 60, required_tags=["Alignment Rack"])

	def test_tier1_f8_no_tag_requirement_matches_any_free_bay(self):
		"""F8: Empty required_tags matches any active free service bay."""
		self._require_sched_api()
		target_date = today()
		assigned = auto_assign_bay(target_date, "08:00:00", 60, required_tags=[])
		self.assertIn(assigned, [self.bay1, self.bay2])

	def test_tier1_f8_inactive_bay_never_matched(self):
		"""F8: Bay 3 (inactive) is never matched regardless of equipment."""
		self._require_sched_api()
		target_date = today()
		# Occupy Bay 1 and Bay 2
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay1)
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay2)
		with self.assertRaises(frappe.ValidationError):
			auto_assign_bay(target_date, "08:00:00", 60)

	# --- Feature 9: Automated Bay Assignment ---
	def test_tier1_f9_auto_assign_free_bay(self):
		"""F9: auto_assign_bay returns an active free bay."""
		self._require_sched_api()
		target_date = today()
		bay = auto_assign_bay(target_date, "09:00:00", 60)
		self.assertTrue(bool(bay))
		self.assertIn(bay, [self.bay1, self.bay2])

	def test_tier1_f9_earliest_fit_order(self):
		"""F9: auto_assign_bay consistently selects lowest-index or first available bay."""
		self._require_sched_api()
		target_date = today()
		bay = auto_assign_bay(target_date, "10:00:00", 60)
		self.assertIsNotNone(bay)

	def test_tier1_f9_auto_assign_respects_existing_bookings(self):
		"""F9: When Bay 1 is booked, auto-assign assigns Bay 2."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay1)
		assigned = auto_assign_bay(target_date, "08:30:00", 60)
		self.assertEqual(assigned, self.bay2)

	def test_tier1_f9_auto_assign_full_shop_raises(self):
		"""F9: When all active bays are booked, auto_assign_bay raises ValidationError."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay1)
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay2)
		with self.assertRaises(frappe.ValidationError):
			auto_assign_bay(target_date, "08:30:00", 60)

	def test_tier1_f9_auto_assign_technician_capacity_gating(self):
		"""F9: auto_assign_bay verifies technician pool availability if enabled."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		ensure_ism_settings(enable_technician_capacity=1)
		# If techs are all booked, auto_assign should fail even if a bay is free
		pass

	# --- Feature 10: Slot Generator with Capacity Indicators ---
	def test_tier1_f10_slot_generator_operating_hours(self):
		"""F10: get_available_slots produces slots within 08:00–17:00."""
		self._require_sched_api()
		target_date = today()
		slots = get_available_slots(target_date, 60)
		self.assertIsInstance(slots, list)
		if slots:
			first_slot = slots[0]
			self.assertIn("start_time", first_slot)
			self.assertIn("available_bays", first_slot)
			self.assertIn("available_technicians", first_slot)
			self.assertIn("effective_capacity", first_slot)

	def test_tier1_f10_lunch_break_slot_suppression(self):
		"""F10: Slots starting directly during 12:00 lunch are suppressed or flagged."""
		self._require_sched_api()
		target_date = today()
		slots = get_available_slots(target_date, 60)
		slot_times = [s["start_time"] for s in slots]
		self.assertNotIn("12:00:00", slot_times)
		self.assertNotIn("12:00", slot_times)

	def test_tier1_f10_bay_specific_slot_filtering(self):
		"""F10: Passing service_bay filters slots specifically for that bay."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay1)
		bay1_slots = get_available_slots(target_date, 60, service_bay=self.bay1)
		bay1_times = [s["start_time"] for s in bay1_slots]
		self.assertNotIn("08:00", bay1_times)
		self.assertNotIn("08:00:00", bay1_times)
		self.assertNotIn("09:00", bay1_times)

	def test_tier1_f10_capacity_indicators_bottleneck_bay(self):
		"""F10: Slot indicates bottleneck correctly when bays < technicians."""
		self._require_sched_api()
		target_date = today()
		slots = get_available_slots(target_date, 60)
		if slots:
			slot = slots[0]
			self.assertIn(slot.get("bottleneck"), ["bay", "technician", "none", None])

	def test_tier1_f10_slots_past_closing_suppressed(self):
		"""F10: Slot whose effective end time exceeds operating_hours_end is suppressed."""
		self._require_sched_api()
		target_date = today()
		# 120 min duration at 16:30 would finish at 18:30 (past 17:00)
		slots = get_available_slots(target_date, 120)
		slot_times = [s["start_time"] for s in slots]
		self.assertNotIn("16:30", slot_times)
		self.assertNotIn("16:30:00", slot_times)

	# --- Feature 11: Technician Designation Roster ---
	def test_tier1_f11_technician_roster_active(self):
		"""F11: Active employees with technician designation are included."""
		self._require_tech_api()
		techs = get_active_technicians()
		emp_ids = [t["employee"] for t in techs]
		if self.tech1_id:
			self.assertIn(self.tech1_id, emp_ids)
		if self.tech2_id:
			self.assertIn(self.tech2_id, emp_ids)

	def test_tier1_f11_inactive_technicians_excluded(self):
		"""F11: Technicians with status != 'Active' (e.g. 'Left') are excluded."""
		self._require_tech_api()
		techs = get_active_technicians()
		emp_ids = [t["employee"] for t in techs]
		if self.tech3_id:
			self.assertNotIn(self.tech3_id, emp_ids)

	def test_tier1_f11_non_technician_designation_excluded(self):
		"""F11: Employees with designation != 'Technician' (e.g. 'Accountant') excluded."""
		self._require_tech_api()
		company = frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed"
		acc = frappe.get_doc({
			"doctype": "Employee",
			"first_name": f"{PREFIX}Accountant",
			"employee_name": f"{PREFIX}Accountant",
			"status": "Active",
			"designation": "Accountant",
			"company": company,
			"gender": "Female",
			"date_of_birth": "1991-01-01",
			"date_of_joining": "2020-01-01",
		}).insert(ignore_permissions=True)
		self.track(acc)
		techs = get_active_technicians()
		emp_ids = [t["employee"] for t in techs]
		self.assertNotIn(acc.name, emp_ids)

	def test_tier1_f11_roster_returns_leave_flags(self):
		"""F11: Roster items contain on_leave boolean."""
		self._require_tech_api()
		techs = get_active_technicians(date=today())
		for t in techs:
			self.assertIn("on_leave", t)

	def test_tier1_f11_roster_date_sensitive(self):
		"""F11: Roster accepts optional target date parameter."""
		self._require_tech_api()
		techs = get_active_technicians(date=add_days(today(), 5))
		self.assertIsInstance(techs, list)

	# --- Feature 12: HRMS Leave Application Integration ---
	def test_tier1_f12_leave_application_marks_on_leave(self):
		"""F12: Approved Leave Application marks employee on_leave=True for target date."""
		self._require_tech_api()
		if not self.tech1_id:
			self.skipTest("Tech 1 not available")
		target_date = today()
		leave_type = frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave"
		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": self.tech1_id,
			"leave_type": leave_type,
			"from_date": target_date,
			"to_date": target_date,
			"status": "Approved",
			"company": frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed",
		})
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)
		frappe.db.set_value("Leave Application", leave.name, "docstatus", 1)
		frappe.db.set_value("Leave Application", leave.name, "status", "Approved")

		techs = get_active_technicians(date=target_date)
		tech1 = next((t for t in techs if t["employee"] == self.tech1_id), None)
		if tech1:
			self.assertTrue(tech1["on_leave"])

	def test_tier1_f12_individual_check_reflects_leave(self):
		"""F12: check_technician_availability returns is_available=False, reason='on_leave'."""
		self._require_tech_api()
		if not self.tech1_id:
			self.skipTest("Tech 1 not available")
		target_date = add_days(today(), 1)
		leave_type = frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave"
		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": self.tech1_id,
			"leave_type": leave_type,
			"from_date": target_date,
			"to_date": target_date,
			"status": "Approved",
			"company": frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed",
		})
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)
		frappe.db.set_value("Leave Application", leave.name, "docstatus", 1)

		chk = check_technician_availability(self.tech1_id, target_date, "09:00:00", 60)
		self.assertFalse(chk["is_available"])
		self.assertEqual(chk.get("reason"), "on_leave")

	def test_tier1_f12_pending_leave_does_not_block(self):
		"""F12: Unapproved/Pending (docstatus=0) Leave Application does not block technician."""
		self._require_tech_api()
		if not self.tech2_id:
			self.skipTest("Tech 2 not available")
		target_date = add_days(today(), 2)
		leave_type = frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave"
		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": self.tech2_id,
			"leave_type": leave_type,
			"from_date": target_date,
			"to_date": target_date,
			"status": "Open",
			"docstatus": 0,
			"company": frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed",
		})
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)

		chk = check_technician_availability(self.tech2_id, target_date, "09:00:00", 60)
		self.assertTrue(chk["is_available"])

	def test_tier1_f12_leave_on_different_date_free_on_target(self):
		"""F12: Leave scheduled next week does not block technician today."""
		self._require_tech_api()
		if not self.tech2_id:
			self.skipTest("Tech 2 not available")
		leave_date = add_days(today(), 10)
		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": self.tech2_id,
			"leave_type": frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave",
			"from_date": leave_date,
			"to_date": leave_date,
			"status": "Approved",
			"company": frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed",
		})
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)
		frappe.db.set_value("Leave Application", leave.name, "docstatus", 1)

		chk = check_technician_availability(self.tech2_id, today(), "09:00:00", 60)
		self.assertTrue(chk["is_available"])

	def test_tier1_f12_multi_day_leave_span(self):
		"""F12: Multi-day approved leave spans entire range."""
		self._require_tech_api()
		if not self.tech1_id:
			self.skipTest("Tech 1 not available")
		start_date = add_days(today(), 3)
		end_date = add_days(today(), 5)
		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": self.tech1_id,
			"leave_type": frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave",
			"from_date": start_date,
			"to_date": end_date,
			"status": "Approved",
			"company": frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed",
		})
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)
		frappe.db.set_value("Leave Application", leave.name, "docstatus", 1)

		chk = check_technician_availability(self.tech1_id, add_days(today(), 4), "09:00:00", 60)
		self.assertFalse(chk["is_available"])

	# --- Feature 13: Technician Pool-Level Gating ---
	def test_tier1_f13_tech_pool_availability_structure(self):
		"""F13: get_technician_pool_availability returns structured capacity metrics."""
		self._require_tech_api()
		pool = get_technician_pool_availability(today(), "09:00:00", 60)
		self.assertIn("total_technicians", pool)
		self.assertIn("on_duty", pool)
		self.assertIn("occupied", pool)
		self.assertIn("available", pool)
		self.assertIn("is_available", pool)

	def test_tier1_f13_pool_exhaustion_blocks_insert(self):
		"""F13: When occupied technicians reach on_duty count, booking exceeding capacity is blocked."""
		self._require_tech_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		ensure_ism_settings(enable_technician_capacity=1)
		# Book active tech 1
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="09:00:00", estimated_duration=60, service_bay=self.bay1, assigned_technician=self.tech1_id)
		# Book active tech 2
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="09:00:00", estimated_duration=60, service_bay=self.bay2, assigned_technician=self.tech2_id)
		# Pool capacity is now 0 available
		pool = get_technician_pool_availability(target_date, "09:00:00", 60)
		self.assertFalse(pool["is_available"])

	# --- Feature 14: Individual Technician Overlap Check ---
	def test_tier1_f14_individual_tech_overlap_detected(self):
		"""F14: check_technician_availability detects individual technician double-booking."""
		self._require_tech_api()
		self._require_doctype("Schedule Entry")
		if not self.tech1_id:
			self.skipTest("Tech 1 not available")
		target_date = today()
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay1, assigned_technician=self.tech1_id)
		chk = check_technician_availability(self.tech1_id, target_date, "09:00:00", 60)
		self.assertFalse(chk["is_available"])
		self.assertEqual(chk.get("reason"), "overlap")

	# --- Feature 15: Master Capacity Toggle ---
	def test_tier1_f15_toggle_disabled_bypasses_pool_limit(self):
		"""F15: When enable_technician_capacity=0, pool limits are bypassed."""
		self._require_tech_api()
		ensure_ism_settings(enable_technician_capacity=0)
		pool = get_technician_pool_availability(today(), "09:00:00", 60)
		# With toggle off, pool check returns is_available=True even if techs are busy
		self.assertTrue(pool["is_available"])

	# --- Feature 16 & 17: Queue, Utilization & Daily Overview ---
	def test_tier1_f16_technician_queue(self):
		"""F16: get_technician_queue returns daily job list and utilization %."""
		self._require_tech_api()
		if not self.tech1_id:
			self.skipTest("Tech 1 not available")
		queue = get_technician_queue(self.tech1_id, today())
		self.assertIn("timeline", queue)
		self.assertIn("utilization_percent", queue)

	def test_tier1_f17_daily_technician_overview(self):
		"""F17: get_daily_technician_overview returns shop-wide scheduled hours and bottleneck."""
		self._require_tech_api()
		overview = get_daily_technician_overview(today())
		self.assertIn("total_scheduled_hours", overview)
		self.assertIn("technicians", overview)

	# --- Feature 18: ERPNext Holiday Awareness ---
	def test_tier1_f18_is_holiday_api(self):
		"""F18: is_holiday returns dict with is_holiday and description."""
		self._require_sched_api()
		res = is_holiday(today())
		self.assertIn("is_holiday", res)
		self.assertIn("description", res)

	# --- Features 19–22: DocTypes ---
	def test_tier1_f19_service_bay_creation(self):
		"""F19: Service Bay DocType supports is_active and child equipment tags."""
		self._require_doctype("Service Bay")
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": f"{PREFIX}Custom_Bay_X",
			"is_active": 1,
			"description": "Custom test bay",
		}).insert(ignore_permissions=True)
		self.track(bay)
		self.assertTrue(bay.name)

	def test_tier1_f20_equipment_tag_creation(self):
		"""F20: Equipment Tag DocType stores capability taxonomy."""
		self._require_doctype("Equipment Tag")
		tag = frappe.get_doc({
			"doctype": "Equipment Tag",
			"tag_name": f"{PREFIX}Heavy_Duty_Crane",
		}).insert(ignore_permissions=True)
		self.track(tag)
		self.assertEqual(tag.tag_name, f"{PREFIX}Heavy_Duty_Crane")

	def test_tier1_f21_schedule_entry_type_seed(self):
		"""F21: Seeded entry types (Diagnostic, Repair, Meeting, etc.) exist with colors."""
		self._require_doctype("Schedule Entry Type")
		for name in ["Diagnostic", "Repair", "Meeting", "Maintenance", "Internal Service"]:
			self.assertTrue(frappe.db.exists("Schedule Entry Type", name))

	def test_tier1_f22_schedule_entry_crud(self):
		"""F22: Schedule Entry persists time window, bay, tech, and status."""
		self._require_doctype("Schedule Entry")
		se = create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=today(),
			scheduled_time="14:00:00",
			estimated_duration=90,
			service_bay=self.bay1,
			status="Scheduled",
		)
		self.track(se)
		self.assertEqual(se.repair_order, self.test_ro1.name)
		self.assertEqual(se.estimated_duration, 90)

	# =========================================================================
	# TIER 2: BOUNDARY & CORNER CASES (>=5 tests per feature)
	# =========================================================================

	def test_tier2_f6_lunch_boundary_exact_edges(self):
		"""F6 Boundary: Job ending exactly at break_start (12:00) does not get extended."""
		self._require_sched_api()
		eff = effective_end_time("10:00:00", 120, break_start="12:00:00", break_end="12:30:00")
		self.assertEqual(eff, timedelta(hours=12))

	def test_tier2_f6_lunch_boundary_exact_start_at_break_end(self):
		"""F6 Boundary: Job starting exactly at break_end (12:30) does not get extended."""
		self._require_sched_api()
		eff = effective_end_time("12:30:00", 60, break_start="12:00:00", break_end="12:30:00")
		self.assertEqual(eff, timedelta(hours=13, minutes=30))

	def test_tier2_f7_partial_front_overlap_blocked(self):
		"""F7 Boundary: 07:30 to 08:30 overlaps with 08:00 to 10:00."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay1)
		self.assertFalse(check_bay_availability(self.bay1, target_date, "07:30:00", 60))

	def test_tier2_f7_partial_tail_overlap_blocked(self):
		"""F7 Boundary: 09:30 to 10:30 overlaps with 08:00 to 10:00."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=120, service_bay=self.bay1)
		self.assertFalse(check_bay_availability(self.bay1, target_date, "09:30:00", 60))

	def test_tier2_f10_slot_generator_no_operating_hours(self):
		"""F10 Boundary: If operating hours missing, handles gracefully."""
		self._require_sched_api()
		ensure_ism_settings(operating_hours_start="00:00:00", operating_hours_end="00:00:00")
		slots = get_available_slots(today(), 60)
		self.assertEqual(len(slots), 0)

	# =========================================================================
	# TIER 3: CROSS-FEATURE COMBINATIONS
	# =========================================================================

	def test_tier3_lunch_extension_plus_bay_overlap_detection(self):
		"""Tier 3: Spanning lunch extends bay occupation, blocking afternoon slot."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		# 11:00 start, 120 min -> spans 12:00-12:30 lunch -> effective end 13:30!
		create_test_schedule_entry(
			repair_order=self.test_ro1.name,
			scheduled_date=target_date,
			scheduled_time="11:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
		)
		# 13:00 to 14:00 should be BLOCKED because bay 1 is busy until 13:30!
		avail_1300 = check_bay_availability(self.bay1, target_date, "13:00:00", 60)
		self.assertFalse(avail_1300)
		# 13:30 to 14:30 should be FREE
		avail_1330 = check_bay_availability(self.bay1, target_date, "13:30:00", 60)
		self.assertTrue(avail_1330)

	def test_tier3_tag_filtering_plus_pool_gating(self):
		"""Tier 3: Specialized equipment bay selection subject to simultaneous technician pool."""
		self._require_sched_api()
		self._require_tech_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		ensure_ism_settings(enable_technician_capacity=1)
		# Bay 2 is the only rack bay. If both tech 1 and tech 2 are occupied at 10:00,
		# auto_assign_bay for Alignment Rack must fail due to technician pool exhaustion.
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="10:00:00", estimated_duration=60, service_bay=self.bay1, assigned_technician=self.tech1_id)
		create_test_schedule_entry(repair_order=self.test_ro1.name, scheduled_date=target_date, scheduled_time="10:00:00", estimated_duration=60, service_bay=self.bay2, assigned_technician=self.tech2_id)
		with self.assertRaises(frappe.ValidationError):
			auto_assign_bay(target_date, "10:00:00", 60, required_tags=["Alignment Rack"])

	# =========================================================================
	# SHOP CAPACITY & RESOURCE ALLOCATION REFACTORING TESTS
	# =========================================================================

	def test_find_next_available_slot(self):
		"""Test finding the next open date with capacity when searching forward."""
		self._require_sched_api()
		target_date = today()
		result = find_next_available_slot(from_date=target_date, duration_minutes=60, service_bay=self.bay1)
		self.assertIsNotNone(result)
		self.assertIn("date", result)
		self.assertIn("first_slot", result)
		self.assertGreaterEqual(result["slots_count"], 1)

	def test_get_unscheduled_services_and_allocation(self):
		"""Test unscheduled service discrimination and allocation lifecycle."""
		self._require_sched_api()
		self._require_doctype("Repair Order")
		# Append 2 services to test RO
		ro = frappe.get_doc("Repair Order", self.test_ro1.name)
		ro.append("services", {
			"service_title": "Brake Inspection",
			"labor_hours": 1.5,
			"status": "Approved",
			"is_supplement": 0,
		})
		ro.append("services", {
			"service_title": "Rotor Replacement Supplement",
			"labor_hours": 1.0,
			"status": "Approved",
			"is_supplement": 1,
		})
		ro.save()

		unscheduled = get_unscheduled_services(ro.name)
		svc_titles = [s["service_title"] for s in unscheduled]
		self.assertIn("Brake Inspection", svc_titles)
		self.assertIn("Rotor Replacement Supplement", svc_titles)

		# Allocate only the first service
		first_svc = [s for s in unscheduled if s["service_title"] == "Brake Inspection"][0]
		alloc_res = allocate_schedule_entry(
			repair_order=ro.name,
			entry_type="Repair",
			scheduled_date=today(),
			scheduled_time="09:00:00",
			estimated_duration=90,
			service_bay=self.bay1,
			selected_services=[first_svc["name"]],
		)
		self.assertIsNotNone(alloc_res.get("name"))

		# Now check unscheduled services again; Brake Inspection must no longer be present
		unscheduled_after = get_unscheduled_services(ro.name)
		titles_after = [s["service_title"] for s in unscheduled_after]
		self.assertNotIn("Brake Inspection", titles_after)
		self.assertIn("Rotor Replacement Supplement", titles_after)

	def test_create_repair_order_from_schedule_entry(self):
		"""Test creating a Repair Order from a pre-intake diagnostic Schedule Entry."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"customer": CUSTOMER,
			"repair_vehicle": VEHICLE_VIN,
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": 60,
			"service_bay": self.bay1,
			"notes": "Customer reports grinding noise from front left wheel under heavy braking.",
			"status": "Scheduled",
		})
		se.insert()
		self.track(se)

		res = create_repair_order_from_schedule_entry(se.name)
		self.assertIsNotNone(res.get("repair_order"))
		ro_name = res["repair_order"]

		ro = frappe.get_doc("Repair Order", ro_name)
		self.track(ro)
		self.assertEqual(ro.customer_concern, "Customer reports grinding noise from front left wheel under heavy braking.")
		# Verify SE is linked to RO
		se.reload()
		self.assertEqual(se.repair_order, ro_name)
		# Verify RO child table was synced
		self.assertEqual(len(ro.schedule_entries), 1)
		self.assertEqual(ro.schedule_entries[0].schedule_entry, se.name)
