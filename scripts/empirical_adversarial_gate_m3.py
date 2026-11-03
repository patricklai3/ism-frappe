# Copyright (c) 2026, patricklai3 and contributors
# Adversarial Stress and Verification Suite for Milestone 3 Gate
# Role: Empirical Challenger (m3_gate_challenger_1)

import sys
import traceback
from datetime import time, timedelta
import frappe
from frappe.utils import today, add_days

from ism.tests.test_fixtures import (
	setup_all,
	teardown_transactional,
	ensure_ism_settings,
	create_test_repair_order,
	create_test_schedule_entry,
	PREFIX,
	CUSTOMER,
	VEHICLE_VIN,
)

from ism.api.scheduling import (
	to_timedelta,
	format_timedelta,
	effective_end_time,
	check_bay_availability,
	get_available_bays,
	auto_assign_bay,
	get_available_slots,
	is_holiday,
	get_shop_settings,
)

from ism.api.technician_availability import (
	get_active_technicians,
	get_technician_pool_availability,
	check_technician_availability,
	get_technician_queue,
	get_daily_technician_overview,
)


class AdversarialTestRunner:
	def __init__(self):
		self.passed = 0
		self.failed = 0
		self.results = []

	def record(self, test_name: str, passed: bool, details: str = ""):
		status = "PASS" if passed else "FAIL"
		print(f"[{status}] {test_name} {details}")
		self.results.append((test_name, passed, details))
		if passed:
			self.passed += 1
		else:
			self.failed += 1
		return passed


runner = AdversarialTestRunner()


# ==============================================================================
# SECTION 1: Lunch Boundary & Effective End Time Adversarial Stress
# ==============================================================================

def test_effective_end_time_stress():
	print("\n" + "=" * 70)
	print("SECTION 1: Lunch Boundary & Effective End Time Adversarial Stress")
	print("=" * 70)

	bs = "12:00:00"
	be = "12:30:00"

	# 1.1 Starting 12:00 (exact lunch start)
	res = effective_end_time("12:00:00", 60, break_start=bs, break_end=be)
	runner.record(
		"1.1 Starting 12:00 exact lunch start pushes to 12:30 + 60m = 13:30",
		res == timedelta(hours=13, minutes=30),
		f"Result: {res}",
	)

	# 1.2 Starting 12:15 (inside lunch window)
	res = effective_end_time("12:15:00", 60, break_start=bs, break_end=be)
	runner.record(
		"1.2 Starting 12:15 inside lunch window pushes to 12:30 + 60m = 13:30",
		res == timedelta(hours=13, minutes=30),
		f"Result: {res}",
	)

	# 1.3 Starting 12:30 (exact lunch end)
	res = effective_end_time("12:30:00", 60, break_start=bs, break_end=be)
	runner.record(
		"1.3 Starting 12:30 exact lunch end begins immediately -> 13:30",
		res == timedelta(hours=13, minutes=30),
		f"Result: {res}",
	)

	# 1.4 Ending exactly at 12:00 (no extension)
	end_exact_pass = True
	cases = [
		("10:00:00", 120),
		("11:00:00", 60),
		("11:30:00", 30),
		("11:45:00", 15),
		("11:59:00", 1),
	]
	for s, d in cases:
		r = effective_end_time(s, d, break_start=bs, break_end=be)
		if r != timedelta(hours=12):
			end_exact_pass = False
			break
	runner.record(
		"1.4 Ending exactly at 12:00 across multiple durations is not extended",
		end_exact_pass,
		f"Tested {len(cases)} start/duration pairs",
	)

	# 1.5 Spanning 11:45 to 12:45 naive (duration 60m) -> 13:15
	res = effective_end_time("11:45:00", 60, break_start=bs, break_end=be)
	runner.record(
		"1.5 Spanning 11:45 to 12:45 adds break duration -> 13:15",
		res == timedelta(hours=13, minutes=15),
		f"Result: {res}",
	)

	# 1.6 High precision 1-second boundary cross: 11:59:59 + 2 sec (naive 12:00:01)
	res_cross = effective_end_time("11:59:59", 2 / 60.0, break_start=bs, break_end=be)
	runner.record(
		"1.6 1-second cross past 12:00 pushes past break to 12:30:01",
		res_cross == timedelta(hours=12, minutes=30, seconds=1),
		f"Result: {res_cross}",
	)

	# 1.7 1-second before boundary: 11:59:58 + 2 sec (naive 12:00:00)
	res_touch = effective_end_time("11:59:58", 2 / 60.0, break_start=bs, break_end=be)
	runner.record(
		"1.7 1-second before boundary reaching exactly 12:00:00 not extended",
		res_touch == timedelta(hours=12),
		f"Result: {res_touch}",
	)

	# 1.8 Starting 12:29:59 (1 second before lunch ends) for 60m
	res_late_lunch = effective_end_time("12:29:59", 60, break_start=bs, break_end=be)
	runner.record(
		"1.8 Starting 12:29:59 pushes to 12:30:00 -> ends 13:30:00",
		res_late_lunch == timedelta(hours=13, minutes=30),
		f"Result: {res_late_lunch}",
	)

	# 1.9 Polymorphic time representation support
	poly_pass = (
		effective_end_time("08:00", 30, bs, be) == timedelta(hours=8, minutes=30)
		and effective_end_time(time(8, 0), 30, bs, be) == timedelta(hours=8, minutes=30)
		and effective_end_time(timedelta(hours=8), 30, bs, be) == timedelta(hours=8, minutes=30)
		and effective_end_time(28800, 30, bs, be) == timedelta(hours=8, minutes=30)
	)
	runner.record("1.9 Polymorphic inputs (str, time, timedelta, int seconds)", poly_pass)

	# 1.10 Inverted or zero break handling
	res_inv = effective_end_time("11:30:00", 60, break_start="12:30:00", break_end="12:00:00")
	res_zero = effective_end_time("11:30:00", 60, break_start="12:00:00", break_end="12:00:00")
	runner.record(
		"1.10 Inverted or zero-duration break window falls back safely to naive end (12:30)",
		res_inv == timedelta(hours=12, minutes=30) and res_zero == timedelta(hours=12, minutes=30),
		f"inv={res_inv}, zero={res_zero}",
	)


# ==============================================================================
# SECTION 2: Bay Availability Interval & Overlap Stress
# ==============================================================================

def test_bay_availability_stress():
	print("\n" + "=" * 70)
	print("SECTION 2: Bay Availability Interval & Overlap Stress")
	print("=" * 70)

	emp_map = setup_all()
	bay1 = f"{PREFIX}Bay 1"
	target_date = today()

	frappe.db.delete("Schedule Entry", {"service_bay": bay1})
	frappe.db.commit()

	ro = create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)

	# Base entry: 08:00 - 10:00 (120m)
	se1 = create_test_schedule_entry(
		repair_order=ro.name,
		scheduled_date=target_date,
		scheduled_time="08:00:00",
		estimated_duration=120,
		service_bay=bay1,
		status="Scheduled",
	)

	# 2.1 Overlap topologies against 08:00 - 10:00
	runner.record(
		"2.1 Partial front overlap (07:30 - 08:30) is blocked",
		check_bay_availability(bay1, target_date, "07:30:00", 60) is False,
	)
	runner.record(
		"2.1 Partial tail overlap (09:30 - 10:30) is blocked",
		check_bay_availability(bay1, target_date, "09:30:00", 60) is False,
	)
	runner.record(
		"2.1 Exact boundary abutment end (10:00 - 11:00) is allowed",
		check_bay_availability(bay1, target_date, "10:00:00", 60) is True,
	)
	runner.record(
		"2.1 Exact boundary abutment start (07:00 - 08:00) is allowed",
		check_bay_availability(bay1, target_date, "07:00:00", 60) is True,
	)
	runner.record(
		"2.1 Internal enclosure (08:30 - 09:30) is blocked",
		check_bay_availability(bay1, target_date, "08:30:00", 60) is False,
	)
	runner.record(
		"2.1 External enclosure (07:00 - 11:00) is blocked",
		check_bay_availability(bay1, target_date, "07:00:00", 240) is False,
	)
	runner.record(
		"2.1 Exact match (08:00 - 10:00) is blocked",
		check_bay_availability(bay1, target_date, "08:00:00", 120) is False,
	)
	runner.record(
		"2.1 Self-exclusion on reschedule allows saving same slot",
		check_bay_availability(bay1, target_date, "08:00:00", 120, exclude_entry=se1.name) is True,
	)

	# Clear bay1 for fragmentation testing
	frappe.db.delete("Schedule Entry", {"service_bay": bay1})
	frappe.db.commit()

	# 2.2 Multi-entry fragmentation setup:
	# e1: 08:00 - 09:00 (60m)
	# e2: 09:30 - 10:30 (60m) -> gap [09:00, 09:30)
	# e3: 11:00 - 12:00 (60m) -> gap [10:30, 11:00)
	# Lunch: [12:00, 12:30)
	# e4: 13:00 - 14:30 (90m)
	# e5: 15:00 - 16:00 (60m) -> gap [14:30, 15:00), gap [16:00, 17:00)
	e1 = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="08:00:00", estimated_duration=60, service_bay=bay1, status="Scheduled")
	e2 = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="09:30:00", estimated_duration=60, service_bay=bay1, status="Scheduled")
	e3 = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="11:00:00", estimated_duration=60, service_bay=bay1, status="Scheduled")
	e4 = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="13:00:00", estimated_duration=90, service_bay=bay1, status="Scheduled")
	e5 = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="15:00:00", estimated_duration=60, service_bay=bay1, status="Scheduled")

	runner.record(
		"2.2 Multi-entry gap 1: 09:00 (30m) fits exactly in [09:00, 09:30)",
		check_bay_availability(bay1, target_date, "09:00:00", 30) is True,
	)
	runner.record(
		"2.2 Multi-entry gap 1: 09:00 (31m) collides with e2 at 09:30",
		check_bay_availability(bay1, target_date, "09:00:00", 31) is False,
	)
	runner.record(
		"2.2 Multi-entry gap 1: 08:59 (30m) collides with e1 at 08:59",
		check_bay_availability(bay1, target_date, "08:59:00", 30) is False,
	)
	runner.record(
		"2.2 Multi-entry gap 2: 10:30 (30m) fits exactly in [10:30, 11:00)",
		check_bay_availability(bay1, target_date, "10:30:00", 30) is True,
	)
	runner.record(
		"2.2 Multi-entry gap 2: 10:30 (31m) collides with e3 at 11:00",
		check_bay_availability(bay1, target_date, "10:30:00", 31) is False,
	)
	runner.record(
		"2.2 Multi-entry lunch gap: 12:00 (30m) shifts to [12:30, 13:00) and fits",
		check_bay_availability(bay1, target_date, "12:00:00", 30) is True,
	)
	runner.record(
		"2.2 Multi-entry lunch gap: 12:00 (31m) shifts to [12:30, 13:01) and collides with e4 at 13:00",
		check_bay_availability(bay1, target_date, "12:00:00", 31) is False,
	)
	runner.record(
		"2.2 Multi-entry gap 4: 14:30 (30m) fits exactly in [14:30, 15:00)",
		check_bay_availability(bay1, target_date, "14:30:00", 30) is True,
	)
	runner.record(
		"2.2 Multi-entry gap 5: 16:00 (60m) fits in [16:00, 17:00)",
		check_bay_availability(bay1, target_date, "16:00:00", 60) is True,
	)

	# 2.3 Cancelled entry dynamic de-allocation
	frappe.db.set_value("Schedule Entry", e2.name, "status", "Cancelled")
	runner.record(
		"2.3 Cancelling e2 opens the merged gap 09:00 - 11:00 (120m)",
		check_bay_availability(bay1, target_date, "09:00:00", 120) is True,
	)

	# 2.4 Date isolation check
	tomorrow = add_days(target_date, 1)
	runner.record(
		"2.4 Booking on tomorrow does not conflict with today",
		check_bay_availability(bay1, tomorrow, "08:00:00", 60) is True,
	)

	teardown_transactional()


# ==============================================================================
# SECTION 3: Available Slots, Operating Hours & Bottlenecks Stress
# ==============================================================================

def test_available_slots_stress():
	print("\n" + "=" * 70)
	print("SECTION 3: Available Slots, Operating Hours & Bottlenecks Stress")
	print("=" * 70)

	emp_map = setup_all()
	bay1 = f"{PREFIX}Bay 1"
	bay2 = f"{PREFIX}Bay 2"
	tech1 = emp_map.get("tech_active_1")
	tech2 = emp_map.get("tech_active_2")
	target_date = today()

	# 3.1 Closed shop / Zero operating hours
	ensure_ism_settings(operating_hours_start="00:00:00", operating_hours_end="00:00:00")
	slots_zero = get_available_slots(target_date, 60)
	runner.record(
		"3.1 Closed shop 00:00:00 - 00:00:00 returns empty slot list",
		slots_zero == [],
		f"slots count={len(slots_zero)}",
	)

	# 3.2 Inverted operating hours
	ensure_ism_settings(operating_hours_start="17:00:00", operating_hours_end="08:00:00")
	slots_inv = get_available_slots(target_date, 60)
	runner.record(
		"3.2 Inverted operating hours 17:00:00 - 08:00:00 returns empty slot list",
		slots_inv == [],
		f"slots count={len(slots_inv)}",
	)

	# 3.3 Zero / negative slot interval
	ensure_ism_settings(operating_hours_start="08:00:00", operating_hours_end="17:00:00", default_slot_interval=0)
	slots_int0 = get_available_slots(target_date, 60)
	runner.record(
		"3.3 Zero slot interval returns empty slot list without looping",
		slots_int0 == [],
		f"slots count={len(slots_int0)}",
	)

	# Restore normal shop configuration: 08:00 - 17:00, lunch 12:00 - 12:30
	ensure_ism_settings(
		operating_hours_start="08:00:00",
		operating_hours_end="17:00:00",
		break_start="12:00:00",
		break_end="12:30:00",
		default_slot_interval=30,
		enable_technician_capacity=1,
	)

	# Clear schedule entries
	frappe.db.delete("Schedule Entry", {"scheduled_date": target_date})
	frappe.db.commit()

	# 3.4 Small slot interval (15 min)
	ensure_ism_settings(default_slot_interval=15)
	slots_15 = get_available_slots(target_date, 60)
	times_15 = [s["start_time"] for s in slots_15]
	runner.record(
		"3.4 Small interval 15m generates fine-grained slots and suppresses lunch slots",
		"08:00" in times_15 and "08:15" in times_15 and "12:00" not in times_15 and "12:15" not in times_15,
		f"Generated {len(slots_15)} slots",
	)

	# 3.5 Large slot interval (120 min)
	ensure_ism_settings(default_slot_interval=120)
	slots_120 = get_available_slots(target_date, 60)
	times_120 = [s["start_time"] for s in slots_120]
	runner.record(
		"3.5 Large interval 120m generates exactly 4 expected slots [08:00, 10:00, 14:00, 16:00]",
		times_120 == ["08:00", "10:00", "14:00", "16:00"],
		f"Times: {times_120}",
	)

	# Restore 30m interval
	ensure_ism_settings(default_slot_interval=30, enable_technician_capacity=1)
	ro = create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)

	# 3.6 Bottleneck String Verification
	# Case A: bays == 2, techs == 2 -> bottleneck == 'none'
	slots = get_available_slots(target_date, 60)
	s_0800 = next(s for s in slots if s["start_time"] == "08:00")
	runner.record(
		"3.6 Bottleneck is 'none' when bays (2) == techs (2)",
		s_0800["bottleneck"] == "none" and s_0800["effective_capacity"] == 2,
		f"bottleneck={s_0800['bottleneck']}, cap={s_0800['effective_capacity']}",
	)

	# Case B: Book 1 technician on a bay-less job -> bays=2, techs=1 -> bottleneck == 'technician'
	se_tech = create_test_schedule_entry(
		repair_order=ro.name,
		scheduled_date=target_date,
		scheduled_time="08:00:00",
		estimated_duration=60,
		assigned_technician=tech1,
		service_bay=None,
	)
	slots = get_available_slots(target_date, 60)
	s_0800 = next(s for s in slots if s["start_time"] == "08:00")
	runner.record(
		"3.6 Bottleneck is 'technician' when available_technicians (1) < available_bays (2)",
		s_0800["bottleneck"] == "technician" and s_0800["effective_capacity"] == 1,
		f"bottleneck={s_0800['bottleneck']}, bays={s_0800['available_bays']}, techs={s_0800['available_technicians']}",
	)
	frappe.db.delete("Schedule Entry", {"name": se_tech.name})

	# Case C: Book 1 bay with no technician -> bays=1, techs=2 -> bottleneck == 'bay'
	se_bay = create_test_schedule_entry(
		repair_order=ro.name,
		scheduled_date=target_date,
		scheduled_time="08:00:00",
		estimated_duration=60,
		service_bay=bay1,
		assigned_technician=None,
	)
	slots = get_available_slots(target_date, 60)
	s_0800 = next(s for s in slots if s["start_time"] == "08:00")
	runner.record(
		"3.6 Bottleneck is 'bay' when available_bays (1) < available_technicians (2)",
		s_0800["bottleneck"] == "bay" and s_0800["effective_capacity"] == 1,
		f"bottleneck={s_0800['bottleneck']}, bays={s_0800['available_bays']}, techs={s_0800['available_technicians']}",
	)
	frappe.db.delete("Schedule Entry", {"name": se_bay.name})

	# Case D: Master capacity toggle disabled -> bottleneck == None, available_technicians == None
	ensure_ism_settings(enable_technician_capacity=0)
	slots_notog = get_available_slots(target_date, 60)
	s_0800 = next(s for s in slots_notog if s["start_time"] == "08:00")
	runner.record(
		"3.6 Bottleneck is None and available_technicians is None when toggle is 0",
		s_0800["bottleneck"] is None and s_0800["available_technicians"] is None and s_0800["effective_capacity"] == 2,
		f"bottleneck={s_0800['bottleneck']}, techs={s_0800['available_technicians']}",
	)

	# Case E: Strict validation that bottleneck strings are never plural across all slots
	ensure_ism_settings(enable_technician_capacity=1)
	all_slots = get_available_slots(target_date, 60)
	valid_values = {"bay", "technician", "none", None}
	all_valid = all(s["bottleneck"] in valid_values for s in all_slots)
	runner.record(
		"3.6 All slots strictly conform to singular bottleneck vocabulary (bay, technician, none, None)",
		all_valid and len(all_slots) > 0,
		f"Verified {len(all_slots)} slots",
	)

	# 3.7 Suppression of slots ending past closing
	# At 16:30 with 60m duration, job ends at 17:30 (past 17:00 closing)
	s_1630_exists = any(s["start_time"] in ("16:30", "16:30:00") for s in all_slots)
	runner.record(
		"3.7 Slot at 16:30 with 60m duration is suppressed (exceeds 17:00 closing)",
		s_1630_exists is False,
	)

	# 3.8 Job duration longer than operating day yields zero slots
	long_slots = get_available_slots(target_date, 600)  # 10 hours
	runner.record(
		"3.8 Job duration exceeding full operating day (10 hours) yields 0 slots",
		len(long_slots) == 0,
		f"Returned {len(long_slots)} slots",
	)

	teardown_transactional()


# ==============================================================================
# SECTION 4: Dual-Resource Auto Assignment & Gating
# ==============================================================================

def test_auto_assign_and_gating():
	print("\n" + "=" * 70)
	print("SECTION 4: Dual-Resource Auto Assignment & Gating Stress")
	print("=" * 70)

	emp_map = setup_all()
	bay1 = f"{PREFIX}Bay 1"
	bay2 = f"{PREFIX}Bay 2"
	tech1 = emp_map.get("tech_active_1")
	tech2 = emp_map.get("tech_active_2")
	target_date = today()

	ensure_ism_settings(enable_technician_capacity=1)
	ro = create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)

	# Clear entries
	frappe.db.delete("Schedule Entry", {"scheduled_date": target_date})
	frappe.db.commit()

	# 4.1 Earliest-fit tie breaker: bay1 and bay2 free -> chooses bay1 (name asc)
	assigned = auto_assign_bay(target_date, "09:00:00", 60)
	runner.record(
		"4.1 Earliest-fit strategy selects first alphabetical active bay (Bay 1)",
		assigned == bay1,
		f"Assigned: {assigned}",
	)

	# 4.2 Equipment tag requirement: bay2 has Alignment Rack, bay1 does not
	assigned_rack = auto_assign_bay(target_date, "09:00:00", 60, required_tags=["Alignment Rack"])
	runner.record(
		"4.2 Equipment tag requirement correctly routes to capable bay (Bay 2)",
		assigned_rack == bay2,
		f"Assigned: {assigned_rack}",
	)

	# 4.3 Unavailable tag throws ValidationError
	threw_missing_tag = False
	try:
		auto_assign_bay(target_date, "09:00:00", 60, required_tags=["Nonexistent Tag 999"])
	except frappe.ValidationError:
		threw_missing_tag = True
	runner.record(
		"4.3 Requesting nonexistent equipment tag raises frappe.ValidationError",
		threw_missing_tag,
	)

	# 4.4 Technician pool gating blocks auto_assign_bay when all techs busy
	se_t1 = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="09:00:00", estimated_duration=60, assigned_technician=tech1, service_bay=bay1)
	se_t2 = create_test_schedule_entry(repair_order=ro.name, scheduled_date=target_date, scheduled_time="09:00:00", estimated_duration=60, assigned_technician=tech2, service_bay=None)
	# Both techs occupied, bay2 is physically free!
	threw_pool_exhausted = False
	try:
		auto_assign_bay(target_date, "09:00:00", 60)
	except frappe.ValidationError:
		threw_pool_exhausted = True
	runner.record(
		"4.4 Auto-assign bay blocked by exhausted technician pool even when physical bay is free",
		threw_pool_exhausted,
	)

	# 4.5 With toggle disabled, auto_assign_bay succeeds despite exhausted tech pool
	ensure_ism_settings(enable_technician_capacity=0)
	assigned_bypass = auto_assign_bay(target_date, "09:00:00", 60)
	runner.record(
		"4.5 Disabling enable_technician_capacity bypasses tech pool exhaustion and assigns free Bay 2",
		assigned_bypass == bay2,
		f"Assigned: {assigned_bypass}",
	)

	teardown_transactional()


# ==============================================================================
# SECTION 5: Database Hygiene Verification
# ==============================================================================

def test_database_hygiene():
	print("\n" + "=" * 70)
	print("SECTION 5: Database Hygiene Verification")
	print("=" * 70)

	se_count = frappe.db.count("Schedule Entry")
	runner.record(
		"5.1 Schedule Entry count is 0 after transactional teardown",
		se_count == 0,
		f"Schedule Entry count: {se_count}",
	)

	non_seed_vehicles = [
		v for v in frappe.get_all("Repair Vehicle", pluck="name")
		if v not in ("5YJSA1E26CF000001", "LRW3E1FA9PC445566")
	]
	runner.record(
		"5.2 Non-seed Repair Vehicle count is 0",
		len(non_seed_vehicles) == 0,
		f"Found non-seed vehicles: {non_seed_vehicles}",
	)


# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if __name__ == "__main__":
	print("Starting Adversarial Stress & Verification Suite for Milestone 3 Gate...")
	frappe.init("development.localhost")
	frappe.connect()
	try:
		test_effective_end_time_stress()
		test_bay_availability_stress()
		test_available_slots_stress()
		test_auto_assign_and_gating()
		test_database_hygiene()
	except Exception as e:
		traceback.print_exc()
		print(f"\nCRITICAL EXCEPTION IN STRESS SUITE: {e}")
		sys.exit(1)

	print("\n" + "=" * 70)
	print(f"ADVERSARIAL STRESS SUITE COMPLETED: {runner.passed} PASSED, {runner.failed} FAILED")
	print("=" * 70)

	if runner.failed > 0:
		print("VERDICT: REQUEST_CHANGES — Adversarial stress tests detected failures.")
		sys.exit(1)
	else:
		print("VERDICT: APPROVE — All adversarial stress tests passed flawlessly.")
		sys.exit(0)
