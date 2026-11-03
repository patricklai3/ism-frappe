# Copyright (c) 2026, patricklai3 and contributors
# Adversarial Stress and Verification Suite for Milestone 3 Gate (Technician Pool & Concurrency)
# Role: Empirical Challenger 2 (m3_gate_challenger_2)

import sys
import traceback
from datetime import timedelta
import frappe
from frappe.utils import add_days, today

if not frappe.db:
	frappe.init("development.localhost")
	frappe.connect()

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
from ism.api.scheduling import (
	effective_end_time,
	check_bay_availability,
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
	check_employee_leave,
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


def run_all_stress_tests():
	print("Starting Adversarial Stress & Verification Suite for Milestone 3 Gate (Technician Pool & Concurrency)...")
	emp_map = setup_all()
	bay1 = f"{PREFIX}Bay 1"
	bay2 = f"{PREFIX}Bay 2"
	tech1_id = emp_map.get("tech_active_1")
	tech2_id = emp_map.get("tech_active_2")
	tech3_id = emp_map.get("tech_inactive")

	test_ro = create_test_repair_order(
		customer=CUSTOMER,
		vehicle=VEHICLE_VIN,
		odometer_in=45000,
	)

	try:
		test_pool_exhaustion_and_concurrency(test_ro, bay1, bay2, tech1_id, tech2_id)
		test_master_toggle_bypass_and_gating(test_ro, bay1, bay2, tech1_id, tech2_id)
		test_hrms_leave_application_stress(test_ro, bay1, bay2, tech1_id, tech2_id)
		test_soft_warning_policy(test_ro, bay1, bay2, tech1_id, tech2_id)
		test_queue_and_overview_stress(test_ro, bay1, bay2, tech1_id, tech2_id)
		test_database_hygiene()
	finally:
		teardown_transactional()
		if test_ro and frappe.db.exists("Repair Order", test_ro.name):
			try:
				frappe.delete_doc("Repair Order", test_ro.name, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

	print("\n" + "=" * 70)
	print(f"ADVERSARIAL STRESS SUITE COMPLETED: {runner.passed} PASSED, {runner.failed} FAILED")
	print("=" * 70)

	if runner.failed > 0:
		print("CRITICAL: Adversarial stress failures detected!")
		sys.exit(1)
	else:
		print("ALL ADVERSARIAL STRESS TESTS PASSED SUCCESSFULLY!")
		sys.exit(0)


# ==============================================================================
# SECTION 1: Technician Pool Exhaustion & Concurrency
# ==============================================================================
def test_pool_exhaustion_and_concurrency(test_ro, bay1, bay2, tech1_id, tech2_id):
	print("\n" + "=" * 70)
	print("SECTION 1: Technician Pool Exhaustion & Concurrency Stress")
	print("=" * 70)

	target_date = add_days(today(), 20)
	ensure_ism_settings(enable_technician_capacity=1)

	# 1.1 Initial Pool Availability (2 active on-duty techs)
	pool_initial = get_technician_pool_availability(target_date, "09:00:00", 60)
	runner.record(
		"1.1 Initial Pool Availability",
		pool_initial["total_technicians"] >= 2 and pool_initial["on_duty"] >= 2 and pool_initial["occupied"] == 0 and pool_initial["available"] >= 2 and pool_initial["is_available"],
		f"total={pool_initial['total_technicians']}, on_duty={pool_initial['on_duty']}, avail={pool_initial['available']}",
	)

	# 1.2 Partial Pool Utilization (1 occupied)
	se1 = create_test_schedule_entry(
		repair_order=test_ro.name,
		scheduled_date=target_date,
		scheduled_time="09:00:00",
		estimated_duration=60,
		service_bay=bay1,
		assigned_technician=tech1_id,
	)
	pool_after_1 = get_technician_pool_availability(target_date, "09:00:00", 60)
	runner.record(
		"1.2 Partial Pool Utilization (1 occupied)",
		pool_after_1["occupied"] == 1 and pool_after_1["available"] == pool_after_1["on_duty"] - 1 and pool_after_1["is_available"],
		f"occupied={pool_after_1['occupied']}, available={pool_after_1['available']}",
	)

	# 1.3 Full Pool Exhaustion (2 occupied)
	se2 = create_test_schedule_entry(
		repair_order=test_ro.name,
		scheduled_date=target_date,
		scheduled_time="09:00:00",
		estimated_duration=60,
		service_bay=bay2,
		assigned_technician=tech2_id,
	)
	pool_exhausted = get_technician_pool_availability(target_date, "09:00:00", 60)
	runner.record(
		"1.3 Full Pool Exhaustion",
		pool_exhausted["occupied"] == pool_exhausted["on_duty"] and pool_exhausted["available"] == 0 and not pool_exhausted["is_available"],
		f"occupied={pool_exhausted['occupied']}, available={pool_exhausted['available']}, is_avail={pool_exhausted['is_available']}",
	)

	# 1.4 Booking Rejection on Pool Exhaustion
	rejected = False
	extra_bay_name = f"{PREFIX}Bay Exhaustion Test"
	try:
		if not frappe.db.exists("Service Bay", extra_bay_name):
			frappe.get_doc({
				"doctype": "Service Bay",
				"bay_name": extra_bay_name,
				"is_active": 1,
			}).insert(ignore_permissions=True)

		se3_doc = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": test_ro.name,
			"scheduled_date": target_date,
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"service_bay": extra_bay_name,
			"status": "Scheduled",
		})
		se3_doc.insert(ignore_permissions=True)
	except frappe.ValidationError as e:
		rejected = True
		details = str(e)
	finally:
		if frappe.db.exists("Schedule Entry", {"service_bay": extra_bay_name}):
			frappe.delete_doc("Schedule Entry", {"service_bay": extra_bay_name}, force=True)
		if frappe.db.exists("Service Bay", extra_bay_name):
			frappe.delete_doc("Service Bay", extra_bay_name, force=True)

	runner.record(
		"1.4 Booking Rejection on Pool Exhaustion",
		rejected,
		f"Blocked over-capacity insert: {details if rejected else 'FAILED TO BLOCK'}",
	)

	# 1.5 auto_assign_bay Rejection on Pool Exhaustion
	auto_assign_rejected = False
	try:
		auto_assign_bay(target_date, "09:00:00", 60)
	except frappe.ValidationError as e:
		auto_assign_rejected = True
		auto_assign_msg = str(e)

	runner.record(
		"1.5 auto_assign_bay Rejection on Pool Exhaustion",
		auto_assign_rejected,
		f"auto_assign_bay blocked when pool exhausted: {auto_assign_msg if auto_assign_rejected else 'ALLOWED'}",
	)

	# 1.6 Lunch extension in pool occupancy:
	frappe.delete_doc("Schedule Entry", se1.name, force=True)
	frappe.delete_doc("Schedule Entry", se2.name, force=True)

	# Schedule Tech 1 from 11:00 with 120m duration (lunch 12:00-12:30 -> effective end 13:30)
	se_lunch = create_test_schedule_entry(
		repair_order=test_ro.name,
		scheduled_date=target_date,
		scheduled_time="11:00:00",
		estimated_duration=120,
		service_bay=bay1,
		assigned_technician=tech1_id,
	)

	# At 13:00, Tech 1 should STILL be occupied due to lunch extension
	chk_1300 = check_technician_availability(tech1_id, target_date, "13:00:00", 30)
	runner.record(
		"1.6 Lunch Extension Keeps Tech Occupied at 13:00",
		not chk_1300["is_available"] and chk_1300["reason"] == "overlap",
		f"reason={chk_1300.get('reason')}, conflicts={len(chk_1300.get('conflicts', []))}",
	)

	# At 13:30, Tech 1 should be FREE
	chk_1330 = check_technician_availability(tech1_id, target_date, "13:30:00", 30)
	runner.record(
		"1.6 Tech Free Immediately After Extended End Time (13:30)",
		chk_1330["is_available"],
		f"is_available={chk_1330['is_available']}",
	)

	# 1.7 Self-Exclusion on Reschedule (exclude_entry)
	pool_resched = get_technician_pool_availability(target_date, "11:00:00", 60, exclude_entry=se_lunch.name)
	runner.record(
		"1.7 Self-Exclusion on Reschedule (exclude_entry)",
		pool_resched["occupied"] == 0,
		f"occupied with exclude={pool_resched['occupied']} (expected 0)",
	)

	# 1.8 Cancelled Entry Releases Technician Capacity
	se_lunch.status = "Cancelled"
	se_lunch.save(ignore_permissions=True)
	pool_cancelled = get_technician_pool_availability(target_date, "11:00:00", 60)
	runner.record(
		"1.8 Cancelled Entry Releases Technician Capacity",
		pool_cancelled["occupied"] == 0,
		f"occupied after cancellation={pool_cancelled['occupied']} (expected 0)",
	)

	frappe.delete_doc("Schedule Entry", se_lunch.name, force=True)


# ==============================================================================
# SECTION 2: Master Capacity Toggle (enable_technician_capacity) Stress
# ==============================================================================
def test_master_toggle_bypass_and_gating(test_ro, bay1, bay2, tech1_id, tech2_id):
	print("\n" + "=" * 70)
	print("SECTION 2: Master Capacity Toggle Stress")
	print("=" * 70)

	target_date = add_days(today(), 21)

	# 2.1 Disable toggle: enable_technician_capacity = 0
	ensure_ism_settings(enable_technician_capacity=0)

	# 2.2 Pool availability returns 999 bypass capacity
	pool_bypassed = get_technician_pool_availability(target_date, "10:00:00", 60)
	runner.record(
		"2.1 Pool Capacity Bypass When Toggle Disabled (0)",
		pool_bypassed["total_technicians"] == 999 and pool_bypassed["available"] == 999 and pool_bypassed["is_available"],
		f"total={pool_bypassed['total_technicians']}, avail={pool_bypassed['available']}",
	)

	# 2.3 Slot generator shows available_technicians=None and bottleneck=None
	slots_bypassed = get_available_slots(target_date, 60)
	runner.record(
		"2.2 Slot Generator Bypass Mode",
		len(slots_bypassed) > 0 and slots_bypassed[0]["available_technicians"] is None and slots_bypassed[0]["bottleneck"] is None,
		f"first slot avail_techs={slots_bypassed[0]['available_technicians'] if slots_bypassed else 'N/A'}",
	)

	# 2.4 Re-enable toggle: enable_technician_capacity = 1
	ensure_ism_settings(enable_technician_capacity=1)
	pool_enabled = get_technician_pool_availability(target_date, "10:00:00", 60)
	runner.record(
		"2.3 Strict Gating Restored When Toggle Enabled (1)",
		pool_enabled["total_technicians"] < 999 and pool_enabled["on_duty"] >= 2,
		f"total={pool_enabled['total_technicians']}, on_duty={pool_enabled['on_duty']}",
	)

	slots_enabled = get_available_slots(target_date, 60)
	runner.record(
		"2.4 Slot Generator Capacity Mode Restored",
		len(slots_enabled) > 0 and slots_enabled[0]["available_technicians"] is not None and slots_enabled[0]["bottleneck"] is not None,
		f"avail_techs={slots_enabled[0]['available_technicians']}, bottleneck={slots_enabled[0]['bottleneck']}",
	)


# ==============================================================================
# SECTION 3: HRMS Leave Application Integration Stress
# ==============================================================================
def test_hrms_leave_application_stress(test_ro, bay1, bay2, tech1_id, tech2_id):
	print("\n" + "=" * 70)
	print("SECTION 3: HRMS Leave Application Integration Stress")
	print("=" * 70)

	ensure_ism_settings(enable_technician_capacity=1)
	company = frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed"
	leave_type = frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave"

	# 3.1 Multi-day leave span (5 days: D+25 to D+29)
	span_start = add_days(today(), 25)
	span_mid = add_days(today(), 27)
	span_end = add_days(today(), 29)
	span_before = add_days(today(), 24)
	span_after = add_days(today(), 30)

	leave_multi = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": tech1_id,
		"leave_type": leave_type,
		"from_date": span_start,
		"to_date": span_end,
		"status": "Approved",
		"company": company,
	})
	leave_multi.flags.ignore_validate = True
	leave_multi.flags.ignore_mandatory = True
	leave_multi.insert(ignore_permissions=True)
	frappe.db.set_value("Leave Application", leave_multi.name, "docstatus", 1)
	frappe.db.set_value("Leave Application", leave_multi.name, "status", "Approved")

	# Check before span: not on leave
	chk_before = check_technician_availability(tech1_id, span_before, "09:00:00", 60)
	runner.record("3.1 Multi-day Span: Day Before is Free", chk_before["is_available"] and not chk_before["on_leave"])

	# Check start day: on leave
	chk_start = check_technician_availability(tech1_id, span_start, "09:00:00", 60)
	runner.record("3.1 Multi-day Span: Start Day is Blocked", not chk_start["is_available"] and chk_start["on_leave"])

	# Check mid day: on leave
	chk_mid = check_technician_availability(tech1_id, span_mid, "09:00:00", 60)
	runner.record("3.1 Multi-day Span: Mid Day is Blocked", not chk_mid["is_available"] and chk_mid["on_leave"])

	# Check end day: on leave
	chk_end = check_technician_availability(tech1_id, span_end, "09:00:00", 60)
	runner.record("3.1 Multi-day Span: End Day is Blocked", not chk_end["is_available"] and chk_end["on_leave"])

	# Check after span: not on leave
	chk_after = check_technician_availability(tech1_id, span_after, "09:00:00", 60)
	runner.record("3.1 Multi-day Span: Day After is Free", chk_after["is_available"] and not chk_after["on_leave"])

	# Pool capacity should be reduced by 1 on span_mid
	pool_mid = get_technician_pool_availability(span_mid, "09:00:00", 60)
	runner.record(
		"3.1 Multi-day Span Reduces Pool on_duty by 1",
		pool_mid["on_leave"] >= 1,
		f"on_leave={pool_mid['on_leave']}, on_duty={pool_mid['on_duty']}",
	)

	# Clean multi-day leave
	frappe.db.set_value("Leave Application", leave_multi.name, "docstatus", 2)
	frappe.delete_doc("Leave Application", leave_multi.name, force=True)

	# 3.2 Half-day leave
	half_day_target = add_days(today(), 32)
	leave_half = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": tech2_id,
		"leave_type": leave_type,
		"from_date": half_day_target,
		"to_date": half_day_target,
		"half_day": 1,
		"half_day_date": half_day_target,
		"status": "Approved",
		"company": company,
	})
	leave_half.flags.ignore_validate = True
	leave_half.flags.ignore_mandatory = True
	leave_half.insert(ignore_permissions=True)
	frappe.db.set_value("Leave Application", leave_half.name, "docstatus", 1)
	frappe.db.set_value("Leave Application", leave_half.name, "status", "Approved")

	chk_half = check_technician_availability(tech2_id, half_day_target, "09:00:00", 60)
	runner.record(
		"3.2 Half-day Leave Marks On-Leave & Flags Half-Day",
		not chk_half["is_available"] and chk_half["on_leave"] and chk_half["half_day"],
		f"on_leave={chk_half['on_leave']}, half_day={chk_half['half_day']}",
	)

	chk_half_other = check_technician_availability(tech2_id, add_days(today(), 33), "09:00:00", 60)
	runner.record("3.2 Half-day Leave Free on Other Dates", chk_half_other["is_available"] and not chk_half_other["on_leave"])

	frappe.db.set_value("Leave Application", leave_half.name, "docstatus", 2)
	frappe.delete_doc("Leave Application", leave_half.name, force=True)

	# 3.3 Open / Pending Leave (docstatus=0, status='Open') MUST NOT block
	pending_date = add_days(today(), 35)
	leave_pending = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": tech1_id,
		"leave_type": leave_type,
		"from_date": pending_date,
		"to_date": pending_date,
		"status": "Open",
		"docstatus": 0,
		"company": company,
	})
	leave_pending.flags.ignore_validate = True
	leave_pending.flags.ignore_mandatory = True
	leave_pending.insert(ignore_permissions=True)

	chk_pending = check_technician_availability(tech1_id, pending_date, "09:00:00", 60)
	runner.record(
		"3.3 Open/Pending Leave Does Not Block Technician",
		chk_pending["is_available"] and not chk_pending["on_leave"],
		f"is_available={chk_pending['is_available']}, on_leave={chk_pending['on_leave']}",
	)

	frappe.delete_doc("Leave Application", leave_pending.name, force=True)

	# 3.4 Rejected Leave MUST NOT block
	rejected_date = add_days(today(), 36)
	leave_rejected = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": tech1_id,
		"leave_type": leave_type,
		"from_date": rejected_date,
		"to_date": rejected_date,
		"status": "Rejected",
		"company": company,
	})
	leave_rejected.flags.ignore_validate = True
	leave_rejected.flags.ignore_mandatory = True
	leave_rejected.insert(ignore_permissions=True)
	frappe.db.set_value("Leave Application", leave_rejected.name, "status", "Rejected")
	frappe.db.set_value("Leave Application", leave_rejected.name, "docstatus", 1)

	chk_rejected = check_technician_availability(tech1_id, rejected_date, "09:00:00", 60)
	runner.record(
		"3.4 Rejected Leave Does Not Block Technician",
		chk_rejected["is_available"] and not chk_rejected["on_leave"],
		f"is_available={chk_rejected['is_available']}, on_leave={chk_rejected['on_leave']}",
	)

	frappe.db.set_value("Leave Application", leave_rejected.name, "docstatus", 2)
	frappe.delete_doc("Leave Application", leave_rejected.name, force=True)

	# 3.5 Cancelled Leave (docstatus=2) MUST NOT block
	cancelled_date = add_days(today(), 37)
	leave_cancelled = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": tech1_id,
		"leave_type": leave_type,
		"from_date": cancelled_date,
		"to_date": cancelled_date,
		"status": "Approved",
		"company": company,
	})
	leave_cancelled.flags.ignore_validate = True
	leave_cancelled.flags.ignore_mandatory = True
	leave_cancelled.insert(ignore_permissions=True)
	frappe.db.set_value("Leave Application", leave_cancelled.name, "status", "Cancelled")
	frappe.db.set_value("Leave Application", leave_cancelled.name, "docstatus", 2)

	chk_cancelled = check_technician_availability(tech1_id, cancelled_date, "09:00:00", 60)
	runner.record(
		"3.5 Cancelled Leave Does Not Block Technician",
		chk_cancelled["is_available"] and not chk_cancelled["on_leave"],
		f"is_available={chk_cancelled['is_available']}, on_leave={chk_cancelled['on_leave']}",
	)

	frappe.delete_doc("Leave Application", leave_cancelled.name, force=True)


# ==============================================================================
# SECTION 4: Soft Warning Non-Blocking Policy Stress
# ==============================================================================
def test_soft_warning_policy(test_ro, bay1, bay2, tech1_id, tech2_id):
	print("\n" + "=" * 70)
	print("SECTION 4: Soft Warning Non-Blocking Policy Stress")
	print("=" * 70)

	target_date = add_days(today(), 40)
	company = frappe.db.get_single_value("Global Defaults", "default_company") or "Lightspeed"
	leave_type = frappe.db.get_value("Leave Type", {}, "name") or "Casual Leave"

	# 4.1 Technician on leave: saving Schedule Entry issues msgprint warning, does NOT block save
	leave = frappe.get_doc({
		"doctype": "Leave Application",
		"employee": tech1_id,
		"leave_type": leave_type,
		"from_date": target_date,
		"to_date": target_date,
		"status": "Approved",
		"company": company,
	})
	leave.flags.ignore_validate = True
	leave.flags.ignore_mandatory = True
	leave.insert(ignore_permissions=True)
	frappe.db.set_value("Leave Application", leave.name, "docstatus", 1)
	frappe.db.set_value("Leave Application", leave.name, "status", "Approved")

	saved_on_leave = False
	se_leave_name = None
	try:
		se_leave = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": test_ro.name,
			"scheduled_date": target_date,
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"service_bay": bay1,
			"assigned_technician": tech1_id,
			"status": "Scheduled",
		})
		se_leave.insert(ignore_permissions=True)
		saved_on_leave = True
		se_leave_name = se_leave.name
	except Exception as e:
		print("Unexpected error saving on leave:", e)

	runner.record(
		"4.1 Soft Warning: Technician on Leave Does Not Block Save",
		saved_on_leave,
		f"Document saved with name: {se_leave_name if saved_on_leave else 'FAILED TO SAVE'}",
	)

	# 4.2 Technician with overlap: saving second Schedule Entry with same technician issues msgprint, does NOT block save
	saved_overlap = False
	se_overlap_name = None
	try:
		se_overlap = frappe.get_doc({
			"doctype": "Schedule Entry",
			"repair_order": test_ro.name,
			"scheduled_date": target_date,
			"scheduled_time": "14:30:00",
			"estimated_duration": 60,
			"service_bay": bay2,
			"assigned_technician": tech1_id,
			"status": "Scheduled",
		})
		se_overlap.insert(ignore_permissions=True)
		saved_overlap = True
		se_overlap_name = se_overlap.name
	except Exception as e:
		print("Unexpected error saving overlap:", e)

	runner.record(
		"4.2 Soft Warning: Technician Overlap Does Not Block Save",
		saved_overlap,
		f"Document saved with name: {se_overlap_name if saved_overlap else 'FAILED TO SAVE'}",
	)

	# Clean up
	if saved_on_leave:
		frappe.delete_doc("Schedule Entry", se_leave_name, force=True)
	if saved_overlap:
		frappe.delete_doc("Schedule Entry", se_overlap_name, force=True)
	frappe.db.set_value("Leave Application", leave.name, "docstatus", 2)
	frappe.delete_doc("Leave Application", leave.name, force=True)


# ==============================================================================
# SECTION 5: Technician Queue & Shop-Wide Daily Overview Stress
# ==============================================================================
def test_queue_and_overview_stress(test_ro, bay1, bay2, tech1_id, tech2_id):
	print("\n" + "=" * 70)
	print("SECTION 5: Technician Queue & Daily Overview Stress")
	print("=" * 70)

	target_date = add_days(today(), 45)

	# 5.1 Empty queue behavior
	q_empty = get_technician_queue(tech1_id, target_date)
	runner.record(
		"5.1 Empty Queue Returns 0 Jobs and 0 Utilization",
		q_empty["total_jobs"] == 0 and q_empty["total_minutes"] == 0 and q_empty["utilization_percent"] == 0.0,
		f"jobs={q_empty['total_jobs']}, util={q_empty['utilization_percent']}%",
	)

	# 5.2 Scheduled job queue with lunch gap calculation
	# Shop hours: 08:00 - 17:00, lunch 12:00-12:30. Operating minutes = 540 - 30 = 510 minutes.
	# Book 08:00 - 10:00 (120m) and 13:00 - 15:00 (120m)
	se_q1 = create_test_schedule_entry(
		repair_order=test_ro.name,
		scheduled_date=target_date,
		scheduled_time="08:00:00",
		estimated_duration=120,
		service_bay=bay1,
		assigned_technician=tech1_id,
	)
	se_q2 = create_test_schedule_entry(
		repair_order=test_ro.name,
		scheduled_date=target_date,
		scheduled_time="13:00:00",
		estimated_duration=120,
		service_bay=bay1,
		assigned_technician=tech1_id,
	)

	q_active = get_technician_queue(tech1_id, target_date)
	expected_util = round((240 / 510) * 100, 1)  # ~47.1%
	runner.record(
		"5.2 Queue Correctly Reports Jobs, Minutes, and Utilization %",
		q_active["total_jobs"] == 2 and q_active["total_minutes"] == 240 and abs(q_active["utilization_percent"] - expected_util) <= 0.2,
		f"jobs={q_active['total_jobs']}, minutes={q_active['total_minutes']}, util={q_active['utilization_percent']}% (expected {expected_util}%)",
	)

	# 5.3 Daily Overview aggregates
	overview = get_daily_technician_overview(target_date)
	runner.record(
		"5.3 Daily Overview Aggregates Scheduled Hours & Technicians",
		overview["total_scheduled_hours"] == 4.0 and len(overview["technicians"]) >= 2 and "bottleneck" in overview,
		f"hours={overview['total_scheduled_hours']}, techs={len(overview['technicians'])}, bottleneck={overview['bottleneck']}",
	)

	# Clean up
	frappe.delete_doc("Schedule Entry", se_q1.name, force=True)
	frappe.delete_doc("Schedule Entry", se_q2.name, force=True)


# ==============================================================================
# SECTION 6: Database Hygiene & State Invariance
# ==============================================================================
def test_database_hygiene():
	print("\n" + "=" * 70)
	print("SECTION 6: Database Hygiene & State Invariance")
	print("=" * 70)

	teardown_transactional()
	frappe.db.commit()

	se_count = frappe.db.count("Schedule Entry")
	runner.record(
		"6.1 Schedule Entry Table Count is 0",
		se_count == 0,
		f"Schedule Entry count: {se_count}",
	)

	vehicles = frappe.get_all("Repair Vehicle", pluck="name")
	non_seed_vehicles = [v for v in vehicles if v not in ("5YJSA1E26CF000001", "LRW3E1FA9PC445566")]
	runner.record(
		"6.2 Non-seed Repair Vehicle Count is 0",
		len(non_seed_vehicles) == 0,
		f"Non-seed vehicles found: {non_seed_vehicles}",
	)


if __name__ == "__main__":
	run_all_stress_tests()
