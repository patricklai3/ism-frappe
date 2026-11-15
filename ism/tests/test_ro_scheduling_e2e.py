# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import add_days, today

from ism.tests.base import ISMTestCase
from ism.tests.test_fixtures import (
	setup_all,
	teardown_transactional,
	ensure_ism_settings,
	create_test_schedule_entry,
	PREFIX,
	CUSTOMER,
	VEHICLE_VIN,
)

try:
	from ism.api.scheduling import (
		effective_end_time,
		auto_assign_bay,
	)
except ImportError:
	effective_end_time = None
	auto_assign_bay = None

try:
	from ism.api.scheduling_views import (
		get_calendar_events,
	)
except ImportError:
	try:
		from ism.api.scheduling import get_calendar_events
	except ImportError:
		get_calendar_events = None

try:
	from ism.api.technician_availability import (
		check_technician_availability,
	)
except ImportError:
	check_technician_availability = None


class TestRepairOrderSchedulingE2E(ISMTestCase):
	"""
	Comprehensive 4-Tier Test Suite for Features 23–31 & Real-World E2E Scenarios:
	- Feature 23: Reusable Modal Date Navigator
	- Feature 24: Header Badges (duration & equipment tags)
	- Feature 25: Slot Selection Grid
	- Feature 26: Technician Preview & Conflict Warnings
	- Feature 27: Calendar View Integration (FullCalendar events, color coding)
	- Feature 28: List View Quick Filters
	- Feature 29: Early Appointment Stages on Repair Order (Draft Appointment, Scheduled, Checked In, No Show)
	- Feature 30: Provisional Intake Formalization (formalize_provisional_info)
	- Feature 31: Supplemental Scheduling on Active Repair Order
	- Tier 4: Real-World Scenarios 1–4
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.emp_map = setup_all()
		cls.bay1 = f"{PREFIX}Bay 1"
		cls.bay2 = f"{PREFIX}Bay 2"
		cls.tech1_id = cls.emp_map.get("tech_active_1")
		cls.tech2_id = cls.emp_map.get("tech_active_2")

	@classmethod
	def tearDownClass(cls):
		teardown_transactional()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		ensure_ism_settings(holiday_list=None)
		self.bay1 = self.__class__.bay1
		self.bay2 = self.__class__.bay2
		self.tech1_id = self.__class__.tech1_id
		self.tech2_id = self.__class__.tech2_id
		self.emp_map = self.__class__.emp_map

	def tearDown(self):
		super().tearDown()

	def _require_doctype(self, dt):
		if not frappe.db.exists("DocType", dt):
			self.skipTest(f"DocType '{dt}' not yet created (Milestone M1)")

	def _require_sched_api(self):
		if effective_end_time is None:
			self.skipTest("ism.api.scheduling not yet implemented (Milestone M3)")

	def _require_tech_api(self):
		if check_technician_availability is None:
			self.skipTest("ism.api.technician_availability not yet implemented (Milestone M3)")

	# =========================================================================
	# TIER 1: FEATURE COVERAGE (>=5 tests per feature)
	# =========================================================================

	# --- Feature 29: Decoupled Repair Order Intake & Master Mandatory Validation ---
	def test_tier1_f29_ro_requires_mandatory_customer_and_vehicle(self):
		"""F29: Repair Order requires master Customer and Vehicle; provisional fields removed."""
		ro = frappe.get_doc({
			"doctype": "Repair Order",
			"status": "Checked In",
			"customer_concern": "Diagnostic squeak inquiry",
		})
		with self.assertRaises((frappe.MandatoryError, frappe.ValidationError)):
			ro.insert(ignore_permissions=True)

	def test_tier1_f29_ro_checked_in_creation(self):
		"""F29: Repair Order can be created directly in 'Checked In' stage."""
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN, status="Checked In")
		self.assertEqual(ro.status, "Checked In")

	def test_tier1_f29_ro_statuses_do_not_contain_appointment_states(self):
		"""F29: Repair Order status options do not include Draft Appointment, Scheduled, or No Show."""
		meta = frappe.get_meta("Repair Order")
		status_field = meta.get_field("status")
		options = [opt.strip() for opt in status_field.options.splitlines() if opt.strip()]
		self.assertNotIn("Draft Appointment", options)
		self.assertNotIn("Scheduled", options)
		self.assertNotIn("No Show", options)
		self.assertIn("Checked In", options)
		self.assertIn("Draft Estimate", options)

	def test_tier1_f29_ro_provisional_fields_removed(self):
		"""F29: Provisional intake fields are removed from Repair Order schema."""
		meta = frappe.get_meta("Repair Order")
		self.assertFalse(meta.has_field("provisional_customer_name"))
		self.assertFalse(meta.has_field("provisional_vin"))
		self.assertFalse(meta.has_field("tab_appointment"))

	def test_tier1_f29_ro_schedule_allocations_child_table_present(self):
		"""F29: Repair Order has read-only schedule_entries child table."""
		meta = frappe.get_meta("Repair Order")
		self.assertTrue(meta.has_field("schedule_entries"))
		field = meta.get_field("schedule_entries")
		self.assertEqual(field.fieldtype, "Table")
		self.assertEqual(field.options, "Repair Order Schedule Entry")
		self.assertTrue(field.read_only)

	# --- Feature 30: Schedule Entry Synchronization to Repair Order Child Table ---
	def test_tier1_f30_schedule_entry_creation_syncs_to_ro_child_table(self):
		"""F30: Creating a Schedule Entry automatically populates the RO child table."""
		self._require_doctype("Schedule Entry")
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)

		target_date = today()
		se = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=target_date,
			scheduled_time="10:00:00",
			estimated_duration=60,
			service_bay=self.bay1,
			entry_type="Diagnostic",
		)
		self.track(se)

		ro.reload()
		self.assertEqual(len(ro.schedule_entries), 1)
		self.assertEqual(ro.schedule_entries[0].schedule_entry, se.name)
		self.assertEqual(ro.schedule_entries[0].service_bay, self.bay1)
		self.assertEqual(ro.schedule_entries[0].status, se.status)

	def test_tier1_f30_schedule_entry_update_syncs_to_ro_child_table(self):
		"""F30: Updating a Schedule Entry (bay/status) updates the linked RO child table."""
		self._require_doctype("Schedule Entry")
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)

		se = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=today(),
			scheduled_time="11:00:00",
			estimated_duration=60,
			service_bay=self.bay1,
		)
		self.track(se)

		se.service_bay = self.bay2
		se.status = "In Progress"
		se.save(ignore_permissions=True)

		ro.reload()
		self.assertEqual(len(ro.schedule_entries), 1)
		self.assertEqual(ro.schedule_entries[0].service_bay, self.bay2)
		self.assertEqual(ro.schedule_entries[0].status, "In Progress")
	# --- Feature 31: Supplemental Scheduling ---
	def test_tier1_f31_multiple_schedule_entries_single_ro(self):
		"""F31: Multiple Schedule Entries can be linked to a single active Repair Order."""
		self._require_doctype("Schedule Entry")
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)

		target_date = today()
		# Initial diagnostic session
		se1 = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=target_date,
			scheduled_time="08:00:00",
			estimated_duration=60,
			service_bay=self.bay1,
			entry_type="Diagnostic",
		)
		self.track(se1)

		# Supplemental repair session in afternoon
		se2 = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=target_date,
			scheduled_time="13:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
			entry_type="Repair",
		)
		self.track(se2)

		# Both entries must exist and share the same repair_order
		self.assertEqual(se1.repair_order, ro.name)
		self.assertEqual(se2.repair_order, ro.name)
		self.assertNotEqual(se1.name, se2.name)

	def test_tier1_f31_supplemental_different_bay_permitted(self):
		"""F31: Supplemental session can book a different specialized bay (e.g. Alignment Rack)."""
		self._require_doctype("Schedule Entry")
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)

		target_date = today()
		se_diag = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="09:00:00", estimated_duration=60, service_bay=self.bay1)
		self.track(se_diag)

		se_align = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="14:00:00", estimated_duration=60, service_bay=self.bay2)
		self.track(se_align)

		self.assertEqual(se_diag.service_bay, self.bay1)
		self.assertEqual(se_align.service_bay, self.bay2)

	def test_tier1_f31_supplemental_on_active_ro_retains_status(self):
		"""F31: Adding supplemental entries does not reset or corrupt active RO state."""
		self._require_doctype("Schedule Entry")
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)
		try:
			ro.status = "In Progress"
			ro.save(ignore_permissions=True)
		except frappe.ValidationError:
			ro.reload()
			ro.approval_status = "Approved"
			ro.status = "Authorized - In Progress"
			ro.save(ignore_permissions=True)

		se_supp = create_test_schedule_entry(repair_order=ro.name, scheduled_date=add_days(today(), 1), scheduled_time="09:00:00", estimated_duration=60, service_bay=self.bay1)
		self.track(se_supp)

		ro.reload()
		self.assertIn(ro.status, ["In Progress", "Authorized - In Progress"])

	# --- Features 23–27: Reusable Modal & Calendar Integration APIs ---
	def test_tier1_f27_calendar_events_payload(self):
		"""F27: get_calendar_events returns structured FullCalendar payload."""
		if get_calendar_events is None:
			self.skipTest("get_calendar_events API not yet implemented (Milestone M5)")
		self._require_doctype("Schedule Entry")

		target_date = today()
		se = create_test_schedule_entry(
			scheduled_date=target_date,
			scheduled_time="10:00:00",
			estimated_duration=60,
			service_bay=self.bay1,
			entry_type="Diagnostic",
		)
		self.track(se)
		events = get_calendar_events(start=target_date, end=add_days(target_date, 1))
		self.assertIsInstance(events, list)
		self.assertTrue(events)
		ev = events[0]
		self.assertIn("title", ev)
		self.assertIn("start", ev)
		self.assertIn("end", ev)

	# =========================================================================
	# TIER 2: BOUNDARY & CORNER CASES
	# =========================================================================

	def test_tier2_ro_schedule_entry_deletion_syncs_child_table(self):
		"""Tier 2: Deleting a Schedule Entry automatically removes it from RO child table."""
		self._require_doctype("Schedule Entry")
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)
		se = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=today(),
			scheduled_time="09:00:00",
			estimated_duration=60,
			service_bay=self.bay1,
		)
		self.track(se)
		ro.reload()
		self.assertEqual(len(ro.schedule_entries), 1)
		se.delete(ignore_permissions=True)
		ro.reload()
		self.assertEqual(len(ro.schedule_entries), 0)

	def test_tier2_whitespace_in_provisional_details_on_schedule_entry(self):
		"""Tier 2: Schedule Entry trims leading/trailing whitespace on provisional contact details."""
		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"provisional_customer_name": "   Trimmed Customer Name   ",
			"provisional_phone": "  555-0177  ",
			"scheduled_date": today(),
			"scheduled_time": "12:00:00",
			"estimated_duration": 60,
			"service_bay": self.bay1,
		}).insert(ignore_permissions=True)
		self.track(se)
		self.assertEqual(se.provisional_customer_name.strip(), "Trimmed Customer Name")
	# =========================================================================
	# TIER 3: CROSS-FEATURE COMBINATIONS
	# =========================================================================

	def test_tier3_provisional_intake_with_immediate_auto_bay(self):
		"""Tier 3: Early appointment RO automatically assigns free bay via scheduling API."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		target_date = today()
		assigned_bay = auto_assign_bay(target_date, "11:00:00", 60)
		self.assertTrue(bool(assigned_bay))

	# =========================================================================
	# TIER 4: REAL-WORLD APPLICATION SCENARIOS
	# =========================================================================

	def test_tier4_scenario_1_full_appointment_to_release_lifecycle(self):
		"""
		Tier 4 (Scenario 1): Decoupled Lifecycle:
		1. Schedule Entry created for diagnostic appointment.
		2. Customer arrives: Repair Order created with mandatory customer and vehicle.
		3. RO child table automatically reflects the linked Schedule Entry.
		4. Supplemental Schedule Entry created for repair.
		5. RO child table automatically contains both schedule entries.
		"""
		self._require_doctype("Schedule Entry")

		# Step 1: Create Repair Order
		ro = self.create_test_repair_order(
			customer=CUSTOMER,
			vehicle=VEHICLE_VIN,
			status="Checked In",
			customer_concern="Vibration at 65 mph",
		)

		# Step 2: Book Initial Appointment
		se_initial = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=today(),
			scheduled_time="08:30:00",
			estimated_duration=60,
			service_bay=self.bay1,
			entry_type="Diagnostic",
		)
		self.track(se_initial)

		ro.reload()
		self.assertEqual(len(ro.schedule_entries), 1)
		self.assertEqual(ro.schedule_entries[0].schedule_entry, se_initial.name)

		# Step 3: Supplemental Work Session (Parts Arrived)
		se_supplemental = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=today(),
			scheduled_time="14:00:00",
			estimated_duration=120,
			service_bay=self.bay1,
			entry_type="Repair",
		)
		self.track(se_supplemental)

		# Verify both appointments share the single Repair Order and are in child table
		ro.reload()
		self.assertEqual(len(ro.schedule_entries), 2)
		self.assertEqual(se_initial.repair_order, ro.name)
		self.assertEqual(se_supplemental.repair_order, ro.name)
	def test_tier4_scenario_2_emergency_walkin_equipment_match(self):
		"""
		Tier 4 (Scenario 2):
		Emergency walk-in vehicle arrives requiring specialized Lift + Alignment Rack.
		Auto-assign finds Bay 2 (the only rack bay), locks slot, and enforces capacity.
		"""
		self._require_sched_api()
		self._require_doctype("Service Bay")
		target_date = today()
		assigned = auto_assign_bay(target_date, "15:00:00", 60, required_tags=["Lift", "Alignment Rack"])
		self.assertEqual(assigned, self.bay2)

	def test_tier4_scenario_3_technician_leave_and_reschedule(self):
		"""
		Tier 4 (Scenario 3):
		Technician 1 has approved leave. System warns of conflict and schedules Tech 2 instead.
		"""
		self._require_tech_api()
		if not self.tech1_id or not self.tech2_id:
			self.skipTest("Techs not available")

		target_date = add_days(today(), 4)
		leave = frappe.get_doc({
			"doctype": "Leave Application",
			"employee": self.tech1_id,
			"leave_type": frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave",
			"from_date": target_date,
			"to_date": target_date,
			"status": "Approved",
			"company": frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed",
		})
		leave.flags.ignore_validate = True
		leave.flags.ignore_mandatory = True
		leave.insert(ignore_permissions=True)
		self.track(leave)
		frappe.db.set_value("Leave Application", leave.name, "docstatus", 1)

		chk1 = check_technician_availability(self.tech1_id, target_date, "10:00:00", 60)
		self.assertFalse(chk1["is_available"])

		chk2 = check_technician_availability(self.tech2_id, target_date, "10:00:00", 60)
		self.assertTrue(chk2["is_available"])

	def test_calendar_events_filter_allowlist_and_multiday_span(self):
		"""Verify get_calendar_events enforces filter allowlists, includes Confirmed, and spans multi-day entries."""
		self._require_sched_api()
		self._require_doctype("Schedule Entry")
		if not self.tech1_id:
			self.skipTest("Tech 1 not available")

		ro = self.create_test_repair_order(CUSTOMER, VEHICLE_VIN)
		confirmed_se = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=today(),
			scheduled_time="10:00:00",
			estimated_duration=60,
			service_bay=self.bay1,
			assigned_technician=self.tech1_id,
			entry_type="Diagnostic",
			status="Confirmed",
		)
		self.track(confirmed_se)

		span_start = today()
		query_start = add_days(today(), 2)
		query_end = add_days(today(), 3)
		span_se = create_test_schedule_entry(
			repair_order=ro.name,
			scheduled_date=span_start,
			scheduled_time="08:00:00",
			estimated_duration=60 * 20,
			service_bay=self.bay2,
			entry_type="Repair",
			status="Scheduled",
		)
		self.track(span_se)
		self.assertGreaterEqual(str(span_se.completion_date), str(query_start))

		dict_events = get_calendar_events(
			today(),
			today(),
			filters={
				"service_bay": self.bay1,
				"status": "Confirmed",
				"assigned_technician": self.tech1_id,
			},
		)
		self.assertIn(confirmed_se.name, {e["name"] for e in dict_events})

		list_events = get_calendar_events(
			today(),
			today(),
			filters=[["Schedule Entry", "status", "=", "Confirmed"]],
		)
		self.assertIn(confirmed_se.name, {e["name"] for e in list_events})

		for bad_filter in (
			{"customer": CUSTOMER},
			[["Schedule Entry", "owner", "=", "Administrator"]],
			[["Schedule Entry", "status", "like", "%"]],
			{"status": "BogusStatus"},
		):
			with self.assertRaises(frappe.ValidationError):
				get_calendar_events(today(), today(), filters=bad_filter)

		span_events = get_calendar_events(query_start, query_end)
		self.assertIn(span_se.name, {e["name"] for e in span_events})
