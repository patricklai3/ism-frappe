#!/usr/bin/env python3
"""
M1 Challenger 3: Empirical Stress Harness for Milestone 1 Data Models & Collision Logic.

Target DocTypes:
- Schedule Entry
- Service Bay
- Schedule Entry Type
- Repair Order (1:N cardinality integration)

Focus Areas Tested:
1. Schedule Entry positive duration enforcement (<=0, negative, strings, None, updates)
2. Collision detection fallback in ScheduleEntry._check_bay_availability_fallback:
   - exact abutting appointments before/after
   - 1-minute overlap before/after
   - nested intervals & enclosing supersets
   - exact identical intervals
   - parallel bays & different dates
   - self-update (no false collision)
   - cancelled entry rebooking
3. populate_from_repair_order on creation and update (before_save):
   - customer, vehicle, and services summary formatting
   - update linking RO to existing appointment
   - preservation of explicit overrides
4. Multiple appointments per Repair Order (1:N cardinality invariant):
   - 4 appointments linked to single RO
   - independent mutations and cancellations
5. ServiceBay.on_trash deletion guards:
   - blocked by active statuses: Draft, Scheduled, In Progress
   - allowed when inactive: Completed, Cancelled
   - mixed active/inactive state resolution
6. ScheduleEntryType.on_trash deletion guards:
   - protected standard types (Diagnostic, Repair, Maintenance, Meeting, Internal Service)
   - protected custom types when referenced by Schedule Entry
   - allowed deletion when unreferenced
"""

import sys
import traceback
from datetime import datetime, timedelta

import frappe
from frappe.utils import today, add_days, to_timedelta, cint

PREFIX = "STRESS-C3-"


class EmpiricalTestRunner:
	def __init__(self):
		self.passed = 0
		self.failed = 0
		self.tests = []
		self.tracked_docs = []

	def track(self, doc):
		self.tracked_docs.append((doc.doctype, doc.name))
		return doc

	def assert_true(self, condition, message=""):
		if not condition:
			raise AssertionError(f"Expected True, got False. {message}")

	def assert_false(self, condition, message=""):
		if condition:
			raise AssertionError(f"Expected False, got True. {message}")

	def assert_equal(self, actual, expected, message=""):
		if actual != expected:
			raise AssertionError(f"Expected {expected!r}, got {actual!r}. {message}")

	def assert_raises(self, exc_class, func, *args, **kwargs):
		try:
			func(*args, **kwargs)
		except exc_class:
			frappe.db.rollback()
			return
		except Exception as e:
			frappe.db.rollback()
			exc_names = exc_class.__name__ if hasattr(exc_class, "__name__") else str(exc_class)
			raise AssertionError(f"Expected {exc_names}, but caught {type(e).__name__}: {e}")
		frappe.db.rollback()
		exc_names = exc_class.__name__ if hasattr(exc_class, "__name__") else str(exc_class)
		raise AssertionError(f"Expected {exc_names}, but no exception was raised.")

	def test(self, name):
		def decorator(func):
			def wrapper(*args, **kwargs):
				print(f"[*] Running: {name} ...", end=" ", flush=True)
				try:
					func(*args, **kwargs)
					frappe.db.commit()
					print("\033[92mPASS\033[0m")
					self.passed += 1
					self.tests.append((name, "PASS", None))
				except Exception as e:
					frappe.db.rollback()
					print(f"\033[91mFAIL\033[0m: {e}")
					traceback.print_exc(file=sys.stdout)
					self.failed += 1
					self.tests.append((name, "FAIL", str(e)))
			return wrapper
		return decorator

	def cleanup(self):
		# Clean up any Schedule Entries linked to PREFIX bays
		for b in frappe.get_all("Service Bay", filters={"name": ["like", f"{PREFIX}%"]}, pluck="name"):
			for se in frappe.get_all("Schedule Entry", filters={"service_bay": b}, pluck="name"):
				try:
					frappe.delete_doc("Schedule Entry", se, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.commit()

		# Clean up any Schedule Entries linked to PREFIX repair orders
		for r in frappe.get_all("Repair Order", filters={"customer": ["like", f"{PREFIX}%"]}, pluck="name"):
			for se in frappe.get_all("Schedule Entry", filters={"repair_order": r}, pluck="name"):
				try:
					frappe.delete_doc("Schedule Entry", se, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.commit()

		# Clean up any Schedule Entries linked to PREFIX types
		for t in frappe.get_all("Schedule Entry Type", filters={"name": ["like", f"{PREFIX}%"]}, pluck="name"):
			for se in frappe.get_all("Schedule Entry", filters={"entry_type": t}, pluck="name"):
				try:
					frappe.delete_doc("Schedule Entry", se, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.commit()

		# Clean up Repair Orders
		for ro in frappe.get_all("Repair Order", filters={"customer": ["like", f"{PREFIX}%"]}, pluck="name"):
			try:
				frappe.delete_doc("Repair Order", ro, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

		# Clean up Repair Vehicles (by customer)
		for v in frappe.get_all("Repair Vehicle", filters={"customer": ["like", f"{PREFIX}%"]}, pluck="name"):
			try:
				frappe.delete_doc("Repair Vehicle", v, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

		# Clean up Service Bays
		for b in frappe.get_all("Service Bay", filters={"name": ["like", f"{PREFIX}%"]}, pluck="name"):
			try:
				frappe.delete_doc("Service Bay", b, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

		# Clean up Schedule Entry Types
		for t in frappe.get_all("Schedule Entry Type", filters={"name": ["like", f"{PREFIX}%"]}, pluck="name"):
			try:
				frappe.delete_doc("Schedule Entry Type", t, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

		# Clean up Customers
		for c in frappe.get_all("Customer", filters={"name": ["like", f"{PREFIX}%"]}, pluck="name"):
			try:
				frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()


runner = EmpiricalTestRunner()


# ===========================================================================
# Section 1: Positive Duration Enforcement
# ===========================================================================

@runner.test("1.1 Schedule Entry duration zero rejected")
def test_1_1():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 0,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("1.2 Schedule Entry negative duration (-1) rejected")
def test_1_2():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": -1,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("1.3 Schedule Entry large negative duration (-60) rejected")
def test_1_3():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": -60,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("1.4 Schedule Entry None duration behavior on insert (default applied) and update (rejected)")
def test_1_4():
	# On insert with None: DocType default 60 is applied by Frappe
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"scheduled_date": today(),
		"scheduled_time": "09:00:00",
		"estimated_duration": None,
	}).insert(ignore_permissions=True))
	runner.assert_equal(se.estimated_duration, 60)

	# On update with None: validate_estimated_duration throws ValidationError
	se.estimated_duration = None
	runner.assert_raises(frappe.ValidationError, se.save, ignore_permissions=True)


@runner.test("1.5 Schedule Entry empty string duration rejected")
def test_1_5():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": "",
		}).insert(ignore_permissions=True)
	runner.assert_raises((frappe.ValidationError, frappe.MandatoryError), make_entry)


@runner.test("1.6 Schedule Entry string zero ('0') rejected")
def test_1_6():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": "0",
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("1.7 Schedule Entry string negative ('-15') rejected")
def test_1_7():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": "-15",
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("1.8 Schedule Entry non-numeric string rejected")
def test_1_8():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": "invalid_duration",
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("1.9 Schedule Entry strictly positive durations (1 and '45') permitted")
def test_1_9():
	se1 = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"scheduled_date": today(),
		"scheduled_time": "09:00:00",
		"estimated_duration": 1,
	}).insert(ignore_permissions=True))
	runner.assert_equal(se1.estimated_duration, 1)

	se2 = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"scheduled_date": today(),
		"scheduled_time": "09:02:00",
		"estimated_duration": "45",
	}).insert(ignore_permissions=True))
	runner.assert_equal(cint(se2.estimated_duration), 45)


@runner.test("1.10 Schedule Entry update to 0 or negative duration blocked on save")
def test_1_10():
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"scheduled_date": today(),
		"scheduled_time": "09:50:00",
		"estimated_duration": 30,
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	# Update to 0
	se.estimated_duration = 0
	runner.assert_raises(frappe.ValidationError, se.save, ignore_permissions=True)

	# Update to -10
	se.reload()
	se.estimated_duration = -10
	runner.assert_raises(frappe.ValidationError, se.save, ignore_permissions=True)


# ===========================================================================
# Section 2: Bay Availability Collision Edge Cases (_check_bay_availability_fallback)
# ===========================================================================

BAY_A = f"{PREFIX}Bay-Collision-A"
BAY_B = f"{PREFIX}Bay-Collision-B"
COLLISION_DATE = "2027-02-15"


@runner.test("2.0 Setup bays and base appointment for collision tests")
def test_2_0():
	runner.track(frappe.get_doc({
		"doctype": "Service Bay",
		"bay_name": BAY_A,
		"is_active": 1,
	}).insert(ignore_permissions=True))

	runner.track(frappe.get_doc({
		"doctype": "Service Bay",
		"bay_name": BAY_B,
		"is_active": 1,
	}).insert(ignore_permissions=True))

	# Base entry: 09:00:00 to 10:00:00 (60 mins) on Bay A
	runner.base_entry = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_A,
		"scheduled_date": COLLISION_DATE,
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
		"status": "Scheduled",
	}).insert(ignore_permissions=True))
	frappe.db.commit()


@runner.test("2.1 Collision exact abutting BEFORE (08:00 - 09:00) allowed")
def test_2_1():
	# Ends at 09:00:00, exact start of base entry
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_A,
		"scheduled_date": COLLISION_DATE,
		"scheduled_time": "08:00:00",
		"estimated_duration": 60,
	}).insert(ignore_permissions=True))
	runner.assert_true(se.name)


@runner.test("2.2 Collision exact abutting AFTER (10:00 - 11:00) allowed")
def test_2_2():
	# Starts at 10:00:00, exact end of base entry
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_A,
		"scheduled_date": COLLISION_DATE,
		"scheduled_time": "10:00:00",
		"estimated_duration": 60,
	}).insert(ignore_permissions=True))
	runner.assert_true(se.name)


@runner.test("2.3 Collision 1-minute overlap BEFORE (08:30 - 09:01) blocked")
def test_2_3():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": BAY_A,
			"scheduled_date": COLLISION_DATE,
			"scheduled_time": "08:30:00",
			"estimated_duration": 31,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("2.4 Collision 1-minute overlap AFTER (09:59 - 10:30) blocked")
def test_2_4():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": BAY_A,
			"scheduled_date": COLLISION_DATE,
			"scheduled_time": "09:59:00",
			"estimated_duration": 31,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("2.5 Collision strictly nested inside (09:15 - 09:45) blocked")
def test_2_5():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": BAY_A,
			"scheduled_date": COLLISION_DATE,
			"scheduled_time": "09:15:00",
			"estimated_duration": 30,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("2.6 Collision enclosing superset (08:45 - 10:15) blocked")
def test_2_6():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": BAY_A,
			"scheduled_date": COLLISION_DATE,
			"scheduled_time": "08:45:00",
			"estimated_duration": 90,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("2.7 Collision exact identical interval (09:00 - 10:00) blocked")
def test_2_7():
	def make_entry():
		frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": BAY_A,
			"scheduled_date": COLLISION_DATE,
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
		}).insert(ignore_permissions=True)
	runner.assert_raises(frappe.ValidationError, make_entry)


@runner.test("2.8 Parallel booking on different bay at identical time allowed")
def test_2_8():
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_B,
		"scheduled_date": COLLISION_DATE,
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
	}).insert(ignore_permissions=True))
	runner.assert_true(se.name)


@runner.test("2.9 Booking on same bay at identical time on different date allowed")
def test_2_9():
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_A,
		"scheduled_date": "2027-02-16",
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
	}).insert(ignore_permissions=True))
	runner.assert_true(se.name)


@runner.test("2.10 Self-update does not trigger self-collision")
def test_2_10():
	runner.base_entry.reload()
	runner.base_entry.notes = "Empirical update verification note"
	runner.base_entry.save(ignore_permissions=True)
	runner.assert_equal(runner.base_entry.notes, "Empirical update verification note")


@runner.test("2.11 Cancelled entry allows rebooking on same bay and time")
def test_2_11():
	runner.base_entry.status = "Cancelled"
	runner.base_entry.save(ignore_permissions=True)
	frappe.db.commit()

	rebook = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_A,
		"scheduled_date": COLLISION_DATE,
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
	}).insert(ignore_permissions=True))
	runner.assert_true(rebook.name)


# ===========================================================================
# Section 3: populate_from_repair_order on Insert & Update (before_save)
# ===========================================================================

@runner.test("3.0 Setup Customer, Tesla Vehicle, and Repair Order with services")
def test_3_0():
	runner.cust_pop = runner.track(frappe.get_doc({
		"doctype": "Customer",
		"customer_name": f"{PREFIX}Cust-Pop",
		"customer_type": "Individual",
		"customer_group": "Individual",
		"territory": "All Territories",
	}).insert(ignore_permissions=True))

	runner.veh_pop = runner.track(frappe.get_doc({
		"doctype": "Repair Vehicle",
		"customer": runner.cust_pop.name,
		"vin": "5YJ3E1EBXNF654321",
		"license_plate": "C3-POP-1",
		"make": "Tesla",
		"model": "Model Y",
		"model_year": 2023,
	}).insert(ignore_permissions=True))

	runner.ro_pop = runner.track(frappe.get_doc({
		"doctype": "Repair Order",
		"customer": runner.cust_pop.name,
		"vehicle": runner.veh_pop.name,
		"odometer_in": 10000,
		"status": "Draft Estimate",
		"services": [
			{"service_title": "Cabin Air Filter Replacement", "labor_hours": 0.5},
			{"service_title": "Brake Fluid Flush", "labor_hours": 1.0},
		],
	}).insert(ignore_permissions=True))
	frappe.db.commit()


@runner.test("3.1 populate_from_repair_order on creation populates customer, vehicle, items summary")
def test_3_1():
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Repair",
		"repair_order": runner.ro_pop.name,
		"scheduled_date": today(),
		"scheduled_time": "11:00:00",
		"estimated_duration": 90,
	}).insert(ignore_permissions=True))

	runner.assert_equal(se.customer, runner.cust_pop.name)
	runner.assert_equal(se.repair_vehicle, runner.veh_pop.name)
	runner.assert_true("Cabin Air Filter Replacement (0.5 hrs)" in (se.items_summary or ""))
	runner.assert_true("Brake Fluid Flush (1.0 hrs)" in (se.items_summary or ""))
	runner.assert_true(runner.cust_pop.name in se.title and ("Model" in se.title or "Tesla" in se.title))


@runner.test("3.2 populate_from_repair_order on update (before_save) populates unlinked entry")
def test_3_2():
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"scheduled_date": today(),
		"scheduled_time": "13:00:00",
		"estimated_duration": 60,
		"provisional_customer_name": "Provisional Client 99",
	}).insert(ignore_permissions=True))

	runner.assert_false(se.customer)
	runner.assert_false(se.repair_vehicle)
	runner.assert_false(se.items_summary)

	# Link RO on update
	se.repair_order = runner.ro_pop.name
	se.save(ignore_permissions=True)
	se.reload()

	runner.assert_equal(se.customer, runner.cust_pop.name)
	runner.assert_equal(se.repair_vehicle, runner.veh_pop.name)
	runner.assert_true("Cabin Air Filter Replacement (0.5 hrs)" in (se.items_summary or ""))
	runner.assert_true(runner.cust_pop.name in se.title)


@runner.test("3.3 populate_from_repair_order preserves pre-existing explicit overrides")
def test_3_3():
	cust_override = runner.track(frappe.get_doc({
		"doctype": "Customer",
		"customer_name": f"{PREFIX}Cust-Override",
		"customer_type": "Individual",
		"customer_group": "Individual",
		"territory": "All Territories",
	}).insert(ignore_permissions=True))

	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Repair",
		"repair_order": runner.ro_pop.name,
		"customer": cust_override.name,
		"items_summary": "Manual Custom Summary",
		"scheduled_date": today(),
		"scheduled_time": "15:00:00",
		"estimated_duration": 60,
	}).insert(ignore_permissions=True))

	runner.assert_equal(se.customer, cust_override.name)
	runner.assert_equal(se.repair_vehicle, runner.veh_pop.name)
	runner.assert_equal(se.items_summary, "Manual Custom Summary")


# ===========================================================================
# Section 4: Multiple Appointments per Repair Order (1:N Cardinality)
# ===========================================================================

@runner.test("4.1 Booking 4 distinct appointments on a single Repair Order succeeds")
def test_4_1():
	runner.ro_multi = runner.track(frappe.get_doc({
		"doctype": "Repair Order",
		"customer": runner.cust_pop.name,
		"vehicle": runner.veh_pop.name,
		"odometer_in": 12000,
		"status": "Draft Estimate",
	}).insert(ignore_permissions=True))

	runner.se_m1 = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"repair_order": runner.ro_multi.name,
		"scheduled_date": "2027-03-01",
		"scheduled_time": "08:00:00",
		"estimated_duration": 60,
		"notes": "Session 1: Initial Diagnosis",
	}).insert(ignore_permissions=True))

	runner.se_m2 = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Repair",
		"repair_order": runner.ro_multi.name,
		"scheduled_date": "2027-03-01",
		"scheduled_time": "13:00:00",
		"estimated_duration": 180,
		"notes": "Session 2: Heavy Mechanical Teardown",
	}).insert(ignore_permissions=True))

	runner.se_m3 = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Repair",
		"repair_order": runner.ro_multi.name,
		"scheduled_date": "2027-03-02",
		"scheduled_time": "09:00:00",
		"estimated_duration": 120,
		"notes": "Session 3: Supplemental Parts Assembly",
	}).insert(ignore_permissions=True))

	runner.se_m4 = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Maintenance",
		"repair_order": runner.ro_multi.name,
		"scheduled_date": "2027-03-02",
		"scheduled_time": "14:00:00",
		"estimated_duration": 60,
		"notes": "Session 4: Calibration and Road Test",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	linked = frappe.get_all("Schedule Entry", filters={"repair_order": runner.ro_multi.name}, pluck="name")
	runner.assert_equal(len(linked), 4)
	names = {runner.se_m1.name, runner.se_m2.name, runner.se_m3.name, runner.se_m4.name}
	runner.assert_equal(len(names), 4)
	for n in names:
		runner.assert_true(n in linked)


@runner.test("4.2 Independent mutation across multiple appointments on same Repair Order")
def test_4_2():
	runner.se_m2.estimated_duration = 240
	runner.se_m2.status = "In Progress"
	runner.se_m2.save(ignore_permissions=True)
	frappe.db.commit()

	runner.se_m1.reload()
	runner.se_m3.reload()
	runner.se_m4.reload()

	runner.assert_equal(runner.se_m1.estimated_duration, 60)
	runner.assert_equal(runner.se_m1.status, "Draft")
	runner.assert_equal(runner.se_m3.estimated_duration, 120)
	runner.assert_equal(runner.se_m3.status, "Draft")
	runner.assert_equal(runner.se_m4.estimated_duration, 60)
	runner.assert_equal(runner.se_m4.status, "Draft")


@runner.test("4.3 Independent cancellation across multiple appointments on same Repair Order")
def test_4_3():
	runner.se_m3.status = "Cancelled"
	runner.se_m3.save(ignore_permissions=True)
	frappe.db.commit()

	active_linked = frappe.get_all("Schedule Entry", filters={
		"repair_order": runner.ro_multi.name,
		"status": ["!=", "Cancelled"],
	}, pluck="name")

	runner.assert_equal(len(active_linked), 3)
	runner.assert_false(runner.se_m3.name in active_linked)


# ===========================================================================
# Section 5: ServiceBay.on_trash Deletion Guards
# ===========================================================================

BAY_DEL = f"{PREFIX}Bay-Del"


def recreate_bay_del():
	# First delete any schedule entries linked to this bay
	for se in frappe.get_all("Schedule Entry", filters={"service_bay": BAY_DEL}, pluck="name"):
		try:
			frappe.delete_doc("Schedule Entry", se, force=True, ignore_permissions=True)
		except Exception:
			pass
	if frappe.db.exists("Service Bay", BAY_DEL):
		try:
			frappe.delete_doc("Service Bay", BAY_DEL, force=True, ignore_permissions=True)
		except Exception:
			pass
	frappe.db.commit()
	runner.track(frappe.get_doc({
		"doctype": "Service Bay",
		"bay_name": BAY_DEL,
		"is_active": 1,
	}).insert(ignore_permissions=True))
	frappe.db.commit()


@runner.test("5.1 Service Bay deletion blocked by active 'Draft' schedule entry")
def test_5_1():
	recreate_bay_del()
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-01",
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
		"status": "Draft",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	runner.assert_raises(frappe.ValidationError, frappe.delete_doc, "Service Bay", BAY_DEL, force=True)
	runner.assert_true(frappe.db.exists("Service Bay", BAY_DEL))


@runner.test("5.2 Service Bay deletion blocked by active 'Scheduled' schedule entry")
def test_5_2():
	recreate_bay_del()
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-01",
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
		"status": "Scheduled",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	runner.assert_raises(frappe.ValidationError, frappe.delete_doc, "Service Bay", BAY_DEL, force=True)
	runner.assert_true(frappe.db.exists("Service Bay", BAY_DEL))


@runner.test("5.3 Service Bay deletion blocked by active 'In Progress' schedule entry")
def test_5_3():
	recreate_bay_del()
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-01",
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
		"status": "In Progress",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	runner.assert_raises(frappe.ValidationError, frappe.delete_doc, "Service Bay", BAY_DEL, force=True)
	runner.assert_true(frappe.db.exists("Service Bay", BAY_DEL))


@runner.test("5.4 Service Bay deletion allowed when entries are 'Completed'")
def test_5_4():
	recreate_bay_del()
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-01",
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
		"status": "Completed",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	# Verify on_trash hook passes directly
	bay_doc = frappe.get_doc("Service Bay", BAY_DEL)
	bay_doc.run_method("on_trash")

	# Delete with force=True (bypasses generic FK, exercises on_trash)
	frappe.delete_doc("Service Bay", BAY_DEL, force=True, ignore_permissions=True)
	runner.assert_false(frappe.db.exists("Service Bay", BAY_DEL))


@runner.test("5.5 Service Bay deletion allowed when entries are 'Cancelled'")
def test_5_5():
	recreate_bay_del()
	se = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-01",
		"scheduled_time": "09:00:00",
		"estimated_duration": 60,
		"status": "Cancelled",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	bay_doc = frappe.get_doc("Service Bay", BAY_DEL)
	bay_doc.run_method("on_trash")

	frappe.delete_doc("Service Bay", BAY_DEL, force=True, ignore_permissions=True)
	runner.assert_false(frappe.db.exists("Service Bay", BAY_DEL))


@runner.test("5.6 Service Bay mixed status resolution (Completed + Cancelled + In Progress)")
def test_5_6():
	recreate_bay_del()
	se_c = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-02",
		"scheduled_time": "08:00:00",
		"estimated_duration": 60,
		"status": "Completed",
	}).insert(ignore_permissions=True))

	se_x = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-02",
		"scheduled_time": "10:00:00",
		"estimated_duration": 60,
		"status": "Cancelled",
	}).insert(ignore_permissions=True))

	se_p = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": "Diagnostic",
		"service_bay": BAY_DEL,
		"scheduled_date": "2027-04-02",
		"scheduled_time": "13:00:00",
		"estimated_duration": 60,
		"status": "In Progress",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	# Blocked while se_p is In Progress
	runner.assert_raises(frappe.ValidationError, frappe.delete_doc, "Service Bay", BAY_DEL, force=True)

	# Transition se_p to Completed -> Now all are Completed/Cancelled -> Allowed!
	se_p.reload()
	se_p.status = "Completed"
	se_p.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.delete_doc("Service Bay", BAY_DEL, force=True, ignore_permissions=True)
	runner.assert_false(frappe.db.exists("Service Bay", BAY_DEL))


# ===========================================================================
# Section 6: ScheduleEntryType Deletion Protection
# ===========================================================================

@runner.test("6.1 Standard Schedule Entry Types are strictly protected from deletion")
def test_6_1():
	std_types = ["Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service"]
	for st in std_types:
		runner.assert_raises(frappe.ValidationError, frappe.delete_doc, "Schedule Entry Type", st, force=True)
		runner.assert_true(frappe.db.exists("Schedule Entry Type", st), f"Type {st} should still exist")


CUSTOM_TYPE = f"{PREFIX}CustomType"


@runner.test("6.2 Custom Schedule Entry Type deletion blocked while linked to Schedule Entry")
def test_6_2():
	if frappe.db.exists("Schedule Entry Type", CUSTOM_TYPE):
		frappe.delete_doc("Schedule Entry Type", CUSTOM_TYPE, force=True, ignore_permissions=True)

	cust_type = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry Type",
		"type_name": CUSTOM_TYPE,
		"requires_repair_order": 0,
		"color": "#336699",
	}).insert(ignore_permissions=True))

	runner.se_linked_custom = runner.track(frappe.get_doc({
		"doctype": "Schedule Entry",
		"entry_type": CUSTOM_TYPE,
		"scheduled_date": "2027-05-01",
		"scheduled_time": "10:00:00",
		"estimated_duration": 60,
		"status": "Scheduled",
	}).insert(ignore_permissions=True))
	frappe.db.commit()

	runner.assert_raises(frappe.ValidationError, frappe.delete_doc, "Schedule Entry Type", CUSTOM_TYPE, force=True)
	runner.assert_true(frappe.db.exists("Schedule Entry Type", CUSTOM_TYPE))


@runner.test("6.3 Custom Schedule Entry Type deletion allowed after referencing entry deleted")
def test_6_3():
	# Delete referencing entry
	frappe.delete_doc("Schedule Entry", runner.se_linked_custom.name, force=True, ignore_permissions=True)
	frappe.db.commit()

	# Now delete custom type
	frappe.delete_doc("Schedule Entry Type", CUSTOM_TYPE, force=True, ignore_permissions=True)
	runner.assert_false(frappe.db.exists("Schedule Entry Type", CUSTOM_TYPE))


# ===========================================================================
# Main Execution Entrypoint
# ===========================================================================

def main():
	print("=" * 70)
	print("STARTING M1 CHALLENGER 3 EMPIRICAL STRESS TEST EXECUTION")
	print("=" * 70)

	runner.cleanup()

	# Section 1
	test_1_1()
	test_1_2()
	test_1_3()
	test_1_4()
	test_1_5()
	test_1_6()
	test_1_7()
	test_1_8()
	test_1_9()
	test_1_10()

	# Section 2
	test_2_0()
	test_2_1()
	test_2_2()
	test_2_3()
	test_2_4()
	test_2_5()
	test_2_6()
	test_2_7()
	test_2_8()
	test_2_9()
	test_2_10()
	test_2_11()

	# Section 3
	test_3_0()
	test_3_1()
	test_3_2()
	test_3_3()

	# Section 4
	test_4_1()
	test_4_2()
	test_4_3()

	# Section 5
	test_5_1()
	test_5_2()
	test_5_3()
	test_5_4()
	test_5_5()
	test_5_6()

	# Section 6
	test_6_1()
	test_6_2()
	test_6_3()

	# Cleanup
	runner.cleanup()

	print("\n" + "=" * 70)
	total = runner.passed + runner.failed
	print(f"TOTAL TESTS:  {total}")
	print(f"PASSED:       {runner.passed}")
	print(f"FAILED:       {runner.failed}")

	if runner.failed == 0:
		print("\033[92mVERDICT: APPROVE\033[0m")
		print("=" * 70)
		return 0
	else:
		print("\033[91mVERDICT: CHALLENGE_FAILED\033[0m")
		print("=" * 70)
		return 1


if __name__ == "__main__":
	frappe.init("development.localhost")
	frappe.connect()
	frappe.set_user("Administrator")
	sys.exit(main())
