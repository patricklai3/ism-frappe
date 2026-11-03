#!/usr/bin/env python3
"""
M1 Challenger 2: Empirical Stress Harness for ISM Settings & Seeding Idempotency.

Tests:
1. ISM Settings boundary inputs and invalid ranges:
   - Operating hours (start >= end, start == end, inverted)
   - Break hours (break_start >= break_end, break_start == break_end)
   - Break outside operating hours (starts before op_start, ends after op_end)
   - Default slot interval (<= 0)
   - Scheduling horizon days (<= 0)
   - Estimation percentile (boundaries: 50.0, 99.0; invalid: 49.9, 99.1, 100.0, -10, 0)
   - Auto-conversion of decimal fractions (0.50 -> 50.0, 0.80 -> 80.0, 0.99 -> 99.0, idempotency on repeated save)
   - Estimation sigma (boundaries: 0.05, 1.00; invalid: 0.049, 1.01, 0.0, -0.5)
   - Buffer padding minutes (boundary: 0; invalid: -1)
   - Default diagnostic duration (invalid: 0, -5)
2. Seeding & Migration Idempotency:
   - Repeated execution of after_migrate() and setup_default_ism_settings() (5x cycles)
   - Preservation of custom settings: enable_technician_capacity=0, buffer_padding_minutes=15, default_slot_interval=45, custom hours
   - Preservation of custom equipment tags and custom schedule entry types
   - No duplicate record creation in Equipment Tag, Schedule Entry Type, or Role tables
"""

import sys
import traceback
import frappe
from frappe.utils import to_timedelta


class TestRunner:
	def __init__(self):
		self.passed = 0
		self.failed = 0
		self.tests = []

	def test(self, name):
		def decorator(func):
			def wrapper(*args, **kwargs):
				print(f"[*] Running: {name} ...", end=" ", flush=True)
				try:
					func(*args, **kwargs)
					print("\033[92mPASS\033[0m")
					self.passed += 1
					self.tests.append((name, "PASS", None))
				except Exception as e:
					print(f"\033[91mFAIL\033[0m: {e}")
					traceback.print_exc()
					self.failed += 1
					self.tests.append((name, "FAIL", str(e)))
			return wrapper
		return decorator


runner = TestRunner()


# Snapshot helper to restore state after tests
class SettingsSnapshot:
	def __enter__(self):
		doc = frappe.get_doc("ISM Settings")
		self.data = doc.as_dict()
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		try:
			frappe.db.rollback()
			frappe.clear_cache()
			reloaded = frappe.get_doc("ISM Settings")
			for k, v in self.data.items():
				if not k.startswith("_") and k not in ("modified", "creation", "doctype", "name"):
					setattr(reloaded, k, v)
			reloaded.save(ignore_permissions=True)
			frappe.db.commit()
		except Exception as e:
			frappe.logger().error(f"Error restoring snapshot: {e}")


# ==============================================================================
# SECTION 1: ISM Settings Boundary & Validation Stress Tests
# ==============================================================================

@runner.test("Operating hours: start == end rejected")
def test_op_hours_equal():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.operating_hours_start = "09:00:00"
		settings.operating_hours_end = "09:00:00"
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError when start == end")
		except frappe.ValidationError:
			pass


@runner.test("Operating hours: start > end rejected")
def test_op_hours_inverted():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.operating_hours_start = "17:00:00"
		settings.operating_hours_end = "08:00:00"
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError when start > end")
		except frappe.ValidationError:
			pass


@runner.test("Operating hours: valid range accepted")
def test_op_hours_valid():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.operating_hours_start = "07:00:00"
		settings.operating_hours_end = "19:00:00"
		settings.break_start = "12:00:00"
		settings.break_end = "13:00:00"
		settings.save(ignore_permissions=True)
		reloaded = frappe.get_doc("ISM Settings")
		assert str(reloaded.operating_hours_start) == "07:00:00"
		assert str(reloaded.operating_hours_end) == "19:00:00"


@runner.test("Break hours: break_start == break_end rejected")
def test_break_hours_equal():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.break_start = "12:00:00"
		settings.break_end = "12:00:00"
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError when break_start == break_end")
		except frappe.ValidationError:
			pass


@runner.test("Break hours: break_start > break_end rejected")
def test_break_hours_inverted():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.break_start = "13:00:00"
		settings.break_end = "12:00:00"
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError when break_start > break_end")
		except frappe.ValidationError:
			pass


@runner.test("Break hours: break starting before operating hours rejected")
def test_break_before_op_hours():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.operating_hours_start = "08:00:00"
		settings.operating_hours_end = "17:00:00"
		settings.break_start = "07:30:00"
		settings.break_end = "08:30:00"
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError when break starts before op hours")
		except frappe.ValidationError:
			pass


@runner.test("Break hours: break ending after operating hours rejected")
def test_break_after_op_hours():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.operating_hours_start = "08:00:00"
		settings.operating_hours_end = "17:00:00"
		settings.break_start = "16:30:00"
		settings.break_end = "17:30:00"
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError when break ends after op hours")
		except frappe.ValidationError:
			pass


@runner.test("Slot interval & horizon: zero and negative rejected")
def test_slot_interval_and_horizon_bounds():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.default_slot_interval = 0
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError for slot interval == 0")
		except frappe.ValidationError:
			pass

		settings = frappe.get_doc("ISM Settings")
		settings.default_slot_interval = -10
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError for slot interval < 0")
		except frappe.ValidationError:
			pass

		settings = frappe.get_doc("ISM Settings")
		settings.scheduling_horizon_days = 0
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError for scheduling horizon == 0")
		except frappe.ValidationError:
			pass

		settings = frappe.get_doc("ISM Settings")
		settings.scheduling_horizon_days = -5
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError for scheduling horizon < 0")
		except frappe.ValidationError:
			pass


# ==============================================================================
# SECTION 2: Estimation Percentile Auto-Conversion & Boundary Stress Tests
# ==============================================================================

@runner.test("Percentile: exact boundary values 50.0 and 99.0 accepted")
def test_percentile_boundaries():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 50.0
		settings.save(ignore_permissions=True)
		reloaded = frappe.get_doc("ISM Settings")
		assert reloaded.estimation_percentile == 50.0, f"Expected 50.0, got {reloaded.estimation_percentile}"

		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 99.0
		settings.save(ignore_permissions=True)
		reloaded = frappe.get_doc("ISM Settings")
		assert reloaded.estimation_percentile == 99.0, f"Expected 99.0, got {reloaded.estimation_percentile}"


@runner.test("Percentile: out of bounds (<50.0 or >99.0) rejected")
def test_percentile_out_of_bounds():
	with SettingsSnapshot():
		for bad_val in [49.9, 49.0, 0.0, -10.0, 99.1, 100.0, 120.0]:
			settings = frappe.get_doc("ISM Settings")
			settings.estimation_percentile = bad_val
			try:
				settings.save(ignore_permissions=True)
				raise AssertionError(f"Expected ValidationError for percentile {bad_val}")
			except frappe.ValidationError:
				pass


@runner.test("Percentile: decimal fraction auto-conversion (0.50, 0.80, 0.95, 0.99)")
def test_percentile_decimal_fraction_auto_conversion():
	with SettingsSnapshot():
		test_cases = [
			(0.50, 50.0),
			(0.80, 80.0),
			(0.85, 85.0),
			(0.95, 95.0),
			(0.99, 99.0),
		]
		for decimal_in, expected_pct in test_cases:
			settings = frappe.get_doc("ISM Settings")
			settings.estimation_percentile = decimal_in
			settings.save(ignore_permissions=True)
			reloaded = frappe.get_doc("ISM Settings")
			assert abs(reloaded.estimation_percentile - expected_pct) < 1e-4, \
				f"For input {decimal_in}, expected {expected_pct}, got {reloaded.estimation_percentile}"


@runner.test("Percentile: invalid decimal fractions rejected (0.49, 1.00, <=0)")
def test_percentile_invalid_decimal_fractions():
	with SettingsSnapshot():
		for bad_dec in [0.49, 0.40, 0.01, 1.0, 0.0, -0.80]:
			settings = frappe.get_doc("ISM Settings")
			settings.estimation_percentile = bad_dec
			try:
				settings.save(ignore_permissions=True)
				raise AssertionError(f"Expected ValidationError for decimal percentile {bad_dec}")
			except frappe.ValidationError:
				pass


@runner.test("Percentile: idempotency on repeated save of auto-converted value")
def test_percentile_repeated_save_idempotency():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_percentile = 0.80
		settings.save(ignore_permissions=True)

		reloaded = frappe.get_doc("ISM Settings")
		assert reloaded.estimation_percentile == 80.0

		# Repeated save without re-specifying must NOT multiply by 100 again (e.g. 8000.0)
		reloaded.save(ignore_permissions=True)
		reloaded2 = frappe.get_doc("ISM Settings")
		assert reloaded2.estimation_percentile == 80.0, f"Expected 80.0, got {reloaded2.estimation_percentile}"


@runner.test("Estimation sigma: boundaries (0.05, 1.00) and invalid values")
def test_sigma_boundaries():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.estimation_sigma = 0.05
		settings.save(ignore_permissions=True)
		assert frappe.get_doc("ISM Settings").estimation_sigma == 0.05

		settings = frappe.get_doc("ISM Settings")
		settings.estimation_sigma = 1.00
		settings.save(ignore_permissions=True)
		assert frappe.get_doc("ISM Settings").estimation_sigma == 1.00

		for bad_sigma in [0.049, 0.0, -0.30, 1.01, 2.0]:
			settings = frappe.get_doc("ISM Settings")
			settings.estimation_sigma = bad_sigma
			try:
				settings.save(ignore_permissions=True)
				raise AssertionError(f"Expected ValidationError for sigma {bad_sigma}")
			except frappe.ValidationError:
				pass


@runner.test("Buffer padding & diagnostic duration bounds")
def test_buffer_and_diagnostic_bounds():
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.buffer_padding_minutes = 0
		settings.save(ignore_permissions=True)
		assert frappe.get_doc("ISM Settings").buffer_padding_minutes == 0

		settings = frappe.get_doc("ISM Settings")
		settings.buffer_padding_minutes = -1
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError for negative buffer padding")
		except frappe.ValidationError:
			pass

		settings = frappe.get_doc("ISM Settings")
		settings.default_diagnostic_duration = 0
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError for default_diagnostic_duration == 0")
		except frappe.ValidationError:
			pass

		settings = frappe.get_doc("ISM Settings")
		settings.default_diagnostic_duration = -15
		try:
			settings.save(ignore_permissions=True)
			raise AssertionError("Expected ValidationError for default_diagnostic_duration < 0")
		except frappe.ValidationError:
			pass


# ==============================================================================
# SECTION 3: Seeding & Migration Idempotency Stress Tests
# ==============================================================================

@runner.test("Idempotency: setup_default_ism_settings() preserves custom settings (including enable_technician_capacity=0)")
def test_idempotency_preserves_custom_settings():
	from ism.setup import setup_default_ism_settings
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		# Set custom settings
		settings.enable_technician_capacity = 0
		settings.buffer_padding_minutes = 15
		settings.default_slot_interval = 45
		settings.scheduling_horizon_days = 60
		settings.operating_hours_start = "07:30:00"
		settings.operating_hours_end = "18:00:00"
		settings.break_start = "12:15:00"
		settings.break_end = "12:45:00"
		settings.technician_designation = "Lead Mechanic"
		settings.estimation_percentile = 88.0
		settings.estimation_sigma = 0.45
		settings.default_diagnostic_duration = 90
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		# Run setup_default_ism_settings 5 times in succession
		for i in range(5):
			setup_default_ism_settings()

		reloaded = frappe.get_doc("ISM Settings")
		assert reloaded.enable_technician_capacity == 0, f"Expected 0, got {reloaded.enable_technician_capacity}"
		assert reloaded.buffer_padding_minutes == 15, f"Expected 15, got {reloaded.buffer_padding_minutes}"
		assert reloaded.default_slot_interval == 45, f"Expected 45, got {reloaded.default_slot_interval}"
		assert reloaded.scheduling_horizon_days == 60, f"Expected 60, got {reloaded.scheduling_horizon_days}"
		assert str(reloaded.operating_hours_start) == "07:30:00", f"Expected 07:30:00, got {reloaded.operating_hours_start}"
		assert str(reloaded.operating_hours_end) == "18:00:00", f"Expected 18:00:00, got {reloaded.operating_hours_end}"
		assert str(reloaded.break_start) == "12:15:00", f"Expected 12:15:00, got {reloaded.break_start}"
		assert str(reloaded.break_end) == "12:45:00", f"Expected 12:45:00, got {reloaded.break_end}"
		assert reloaded.technician_designation == "Lead Mechanic", f"Expected Lead Mechanic, got {reloaded.technician_designation}"
		assert reloaded.estimation_percentile == 88.0, f"Expected 88.0, got {reloaded.estimation_percentile}"
		assert abs(reloaded.estimation_sigma - 0.45) < 1e-4, f"Expected 0.45, got {reloaded.estimation_sigma}"
		assert reloaded.default_diagnostic_duration == 90, f"Expected 90, got {reloaded.default_diagnostic_duration}"


@runner.test("Idempotency: buffer_padding_minutes=0 preserved (not reset to default)")
def test_buffer_padding_zero_preserved():
	from ism.setup import setup_default_ism_settings
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.buffer_padding_minutes = 0
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		# Run setup routine
		setup_default_ism_settings()

		reloaded = frappe.get_doc("ISM Settings")
		assert reloaded.buffer_padding_minutes == 0, f"Expected 0, got {reloaded.buffer_padding_minutes}"


@runner.test("Idempotency: seed_equipment_tags() repeated execution causes no duplication or data loss")
def test_seed_equipment_tags_idempotency():
	from ism.setup import seed_equipment_tags, DEFAULT_EQUIPMENT_TAGS

	# Create a custom equipment tag
	custom_tag_name = "TEST Challenger Custom Bay Tag"
	if not frappe.db.exists("Equipment Tag", custom_tag_name):
		custom_tag = frappe.get_doc({
			"doctype": "Equipment Tag",
			"tag_name": custom_tag_name,
			"description": "Custom tag for challenger test",
		}).insert(ignore_permissions=True)
	
	try:
		# Run seed 5 times
		for _ in range(5):
			seed_equipment_tags()

		# Verify all default tags exist and have exactly 1 record
		for tag in DEFAULT_EQUIPMENT_TAGS:
			count = frappe.db.count("Equipment Tag", filters={"tag_name": tag["tag_name"]})
			assert count == 1, f"Expected exactly 1 record for tag {tag['tag_name']}, found {count}"

		# Verify custom tag still exists
		assert frappe.db.exists("Equipment Tag", custom_tag_name), "Custom tag was unexpectedly removed!"
	finally:
		if frappe.db.exists("Equipment Tag", custom_tag_name):
			frappe.delete_doc("Equipment Tag", custom_tag_name, force=True, ignore_permissions=True)
		frappe.db.commit()


@runner.test("Idempotency: seed_schedule_entry_types() repeated execution causes no duplication or data loss")
def test_seed_schedule_entry_types_idempotency():
	from ism.setup import seed_schedule_entry_types, STANDARD_SCHEDULE_ENTRY_TYPES

	# Create a custom schedule entry type
	custom_type_name = "TEST Challenger Custom Entry Type"
	if not frappe.db.exists("Schedule Entry Type", custom_type_name):
		frappe.get_doc({
			"doctype": "Schedule Entry Type",
			"type_name": custom_type_name,
			"requires_repair_order": 0,
			"color": "#123456",
			"description": "Challenger custom schedule entry type",
		}).insert(ignore_permissions=True)

	try:
		# Run seeder 5 times
		for _ in range(5):
			seed_schedule_entry_types()

		# Verify each standard type exists exactly once and retains correct properties
		for item in STANDARD_SCHEDULE_ENTRY_TYPES:
			count = frappe.db.count("Schedule Entry Type", filters={"type_name": item["type_name"]})
			assert count == 1, f"Expected exactly 1 record for {item['type_name']}, got {count}"
			doc = frappe.get_doc("Schedule Entry Type", item["type_name"])
			assert doc.requires_repair_order == item["requires_repair_order"], \
				f"Mismatch in requires_repair_order for {item['type_name']}"
			assert doc.color == item["color"], f"Mismatch in color for {item['type_name']}"

		# Verify custom type remains intact
		assert frappe.db.exists("Schedule Entry Type", custom_type_name), "Custom entry type was unexpectedly removed!"
		custom_doc = frappe.get_doc("Schedule Entry Type", custom_type_name)
		assert custom_doc.color == "#123456"
	finally:
		if frappe.db.exists("Schedule Entry Type", custom_type_name):
			frappe.delete_doc("Schedule Entry Type", custom_type_name, force=True, ignore_permissions=True)
		frappe.db.commit()


@runner.test("Idempotency: ensure_ism_roles() repeated execution causes no duplicate roles")
def test_roles_idempotency():
	from ism.setup import ensure_ism_roles
	for _ in range(5):
		ensure_ism_roles()
	count = frappe.db.count("Role", filters={"role_name": "ISM Manager"})
	assert count == 1, f"Expected 1 ISM Manager role, got {count}"


@runner.test("Migration simulation: read table -> create_custom_fields -> backfill deadlock")
def test_migration_deadlock_simulation():
	from ism.setup import setup_procurement_custom_fields, backfill_service_package_models
	# 1. Active read on tabRepair Order Service
	frappe.db.sql("SELECT name FROM `tabRepair Order Service` LIMIT 1")

	# 2. Schema alteration via create_custom_fields without commit
	setup_procurement_custom_fields()

	# 3. Subsequent query on tabRepair Order Service triggers 1020 deadlock unless commit is present
	try:
		backfill_service_package_models()
		print(" (deadlock did not trigger in this session) ", end="")
	except frappe.QueryDeadlockError:
		print(" (deadlock confirmed without commit) ", end="")
	finally:
		frappe.db.commit()


@runner.test("Full after_migrate() execution under clean transaction (Idempotency check)")
def test_full_after_migrate():
	from ism.setup import after_migrate
	with SettingsSnapshot():
		settings = frappe.get_doc("ISM Settings")
		settings.enable_technician_capacity = 0
		settings.save(ignore_permissions=True)
		frappe.db.commit()

		after_migrate()

		reloaded = frappe.get_doc("ISM Settings")
		assert reloaded.enable_technician_capacity == 0, f"after_migrate() reset enable_technician_capacity to {reloaded.enable_technician_capacity}!"


def main():
	if not frappe.db:
		frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
		frappe.connect()

	print("================================================================================")
	print("M1 Challenger 2: Empirical Stress Harness for ISM Settings & Seeding")
	print("================================================================================")

	test_op_hours_equal()
	test_op_hours_inverted()
	test_op_hours_valid()
	test_break_hours_equal()
	test_break_hours_inverted()
	test_break_before_op_hours()
	test_break_after_op_hours()
	test_slot_interval_and_horizon_bounds()
	test_percentile_boundaries()
	test_percentile_out_of_bounds()
	test_percentile_decimal_fraction_auto_conversion()
	test_percentile_invalid_decimal_fractions()
	test_percentile_repeated_save_idempotency()
	test_sigma_boundaries()
	test_buffer_and_diagnostic_bounds()
	test_idempotency_preserves_custom_settings()
	test_buffer_padding_zero_preserved()
	test_seed_equipment_tags_idempotency()
	test_seed_schedule_entry_types_idempotency()
	test_roles_idempotency()
	test_migration_deadlock_simulation()
	test_full_after_migrate()

	print("================================================================================")
	print(f"Results: {runner.passed} PASSED, {runner.failed} FAILED out of {len(runner.tests)} tests.")
	print("================================================================================")

	for name, status, err in runner.tests:
		if status == "FAIL":
			print(f"FAILED TEST: {name}")
			print(f"  Error: {err}")

	if runner.failed > 0:
		sys.exit(1)
	sys.exit(0)


if __name__ == "__main__":
	main()
