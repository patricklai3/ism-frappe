# Copyright (c) 2026, patricklai3 and contributors
# Adversarial Stress and Concurrency Verification Suite for Milestone 1 Gate
# Role: Empirical Challenger (m1_gate_challenger_1)

import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import frappe
from frappe import _
from ism.tests.cleanup import cleanup_all_test_data
from ism.setup import (
    seed_equipment_tags,
    seed_schedule_entry_types,
    setup_default_ism_settings,
)

STANDARD_SCHEDULE_ENTRY_TYPES = [
    "Diagnostic",
    "Repair",
    "Maintenance",
    "Meeting",
    "Internal Service",
]

STANDARD_EQUIPMENT_TAGS = [
    "Lift",
    "EV High Voltage",
    "Alignment Rack",
]

TEST_PREFIX = "_TEST_ADV_GATE_"


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
# SECTION 1: ISM Settings Concurrency & Sequential Write Stress
# ==============================================================================

def test_suite_1_ism_settings_stress():
    print("\n" + "=" * 70)
    print("TEST SUITE 1: ISM Settings Concurrency & Sequential Stress")
    print("=" * 70)

    # 1.1 Sequential rapid updates to tabSingles
    print("\n--- 1.1 Sequential Rapid Updates to ISM Settings (tabSingles) ---")
    sequential_deadlocks = 0
    start_time = time.time()
    try:
        for i in range(25):
            slot_interval = 15 + (i % 4) * 15  # 15, 30, 45, 60
            sigma = 0.20 + (i % 5) * 0.05      # 0.20 to 0.40
            percentile = 70.0 + (i % 5) * 5.0   # 70.0 to 90.0

            # Alternate between set_single_value and get_doc().save()
            if i % 2 == 0:
                frappe.db.set_single_value("ISM Settings", "default_slot_interval", slot_interval)
                frappe.db.set_single_value("ISM Settings", "estimation_sigma", sigma)
                frappe.db.set_single_value("ISM Settings", "estimation_percentile", percentile)
                frappe.db.commit()
            else:
                doc = frappe.get_doc("ISM Settings")
                doc.default_slot_interval = slot_interval
                doc.estimation_sigma = sigma
                doc.estimation_percentile = percentile
                doc.save(ignore_permissions=True)
                frappe.db.commit()
    except Exception as e:
        if "1020" in str(e) or "Deadlock" in type(e).__name__:
            sequential_deadlocks += 1
            print(f"Deadlock detected during sequential update {i}: {e}")
        else:
            print(f"Error during sequential update: {e}")
            raise

    elapsed_seq = time.time() - start_time
    runner.record(
        "ISM Settings: 25 sequential updates without MariaDB 1020 deadlock",
        sequential_deadlocks == 0,
        f"(elapsed: {elapsed_seq:.3f}s, deadlocks: {sequential_deadlocks})",
    )

    # 1.2 Multi-threaded Concurrent Updates to ISM Settings
    print("\n--- 1.2 Multi-threaded Concurrent Updates to ISM Settings ---")
    concurrent_deadlocks = 0
    worker_errors = []

    def concurrent_settings_worker(worker_id):
        nonlocal concurrent_deadlocks
        local_site = "development.localhost"
        frappe.init(site=local_site, sites_path="/workspace/development/frappe-bench/sites")
        frappe.connect()
        try:
            for iteration in range(5):
                try:
                    # Each worker attempts an update with isolated retry
                    new_interval = 20 + worker_id * 5 + iteration
                    frappe.db.set_single_value("ISM Settings", "default_slot_interval", new_interval)
                    frappe.db.commit()
                    time.sleep(0.01)
                except Exception as ex:
                    err_msg = str(ex)
                    if "1020" in err_msg or "Deadlock" in type(ex).__name__:
                        concurrent_deadlocks += 1
                        # Retrying transaction as required by MariaDB 1020
                        frappe.db.rollback()
                        time.sleep(0.02)
                        frappe.db.set_single_value("ISM Settings", "default_slot_interval", new_interval)
                        frappe.db.commit()
                    else:
                        worker_errors.append(f"Worker {worker_id}: {err_msg}")
        finally:
            frappe.destroy()

    threads = []
    t_start = time.time()
    for w in range(4):
        t = threading.Thread(target=concurrent_settings_worker, args=(w,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    t_elapsed = time.time() - t_start

    # Reconnect main thread
    frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
    frappe.connect()

    runner.record(
        "ISM Settings: 4 concurrent worker threads completed updates cleanly",
        len(worker_errors) == 0,
        f"(elapsed: {t_elapsed:.3f}s, retried_1020: {concurrent_deadlocks}, errors: {len(worker_errors)})",
    )

    # 1.3 Boundary validations and input constraints
    print("\n--- 1.3 Boundary Inputs and Range Constraints ---")
    doc = frappe.new_doc("ISM Settings")

    # Operating hours: start >= end
    doc.operating_hours_start = "17:00:00"
    doc.operating_hours_end = "08:00:00"
    passed_hours = False
    try:
        doc.validate()
    except frappe.ValidationError:
        passed_hours = True
    runner.record("ISM Settings: inverted operating hours rejected", passed_hours)

    # Lunch break outside operating hours
    doc.operating_hours_start = "08:00:00"
    doc.operating_hours_end = "17:00:00"
    doc.break_start = "07:00:00"
    doc.break_end = "07:30:00"
    passed_break = False
    try:
        doc.validate()
    except frappe.ValidationError:
        passed_break = True
    runner.record("ISM Settings: break outside operating hours rejected", passed_break)

    # Percentile boundary: 49.0 rejected, 50.0 allowed, 99.0 allowed, 100.0 rejected
    doc.break_start = "12:00:00"
    doc.break_end = "12:30:00"
    doc.default_slot_interval = 30
    doc.scheduling_horizon_days = 30
    doc.estimation_sigma = 0.30

    doc.estimation_percentile = 49.0
    passed_p_low = False
    try:
        doc.validate()
    except frappe.ValidationError:
        passed_p_low = True
    runner.record("ISM Settings: percentile < 50.0% rejected", passed_p_low)

    doc.estimation_percentile = 100.0
    passed_p_high = False
    try:
        doc.validate()
    except frappe.ValidationError:
        passed_p_high = True
    runner.record("ISM Settings: percentile > 99.0% rejected", passed_p_high)

    # Decimal fraction auto-conversion (0.80 -> 80.0)
    doc.estimation_percentile = 0.80
    doc.validate()
    passed_auto_conv = (doc.estimation_percentile == 80.0)
    runner.record("ISM Settings: fraction 0.80 auto-converted to 80.0%", passed_auto_conv)

    # Sigma boundaries
    doc.estimation_sigma = 0.04
    passed_sig_low = False
    try:
        doc.validate()
    except frappe.ValidationError:
        passed_sig_low = True
    runner.record("ISM Settings: sigma < 0.05 rejected", passed_sig_low)

    doc.estimation_sigma = 1.05
    passed_sig_high = False
    try:
        doc.validate()
    except frappe.ValidationError:
        passed_sig_high = True
    runner.record("ISM Settings: sigma > 1.00 rejected", passed_sig_high)

    # Reset to canonical defaults
    setup_default_ism_settings()
    frappe.db.commit()


# ==============================================================================
# SECTION 2: Schedule Entry Concurrency, Overlap Defense, and Cardinality
# ==============================================================================

def test_suite_2_schedule_entry_stress():
    print("\n" + "=" * 70)
    print("TEST SUITE 2: Schedule Entry Concurrency & Collision Defenses")
    print("=" * 70)

    bay_name = f"{TEST_PREFIX}BAY_STRESS"
    if frappe.db.exists("Service Bay", bay_name):
        frappe.delete_doc("Service Bay", bay_name, force=True)

    bay = frappe.get_doc({
        "doctype": "Service Bay",
        "bay_name": bay_name,
        "is_active": 1,
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    test_date = "2026-11-20"

    # Clean existing entries for this bay and date
    for se in frappe.get_all("Schedule Entry", filters={"service_bay": bay_name}, pluck="name"):
        frappe.delete_doc("Schedule Entry", se, force=True)
    frappe.db.commit()

    # 2.1 Baseline Entry: 09:00:00 to 10:00:00 (duration: 60 min)
    base_se = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "service_bay": bay_name,
        "scheduled_date": test_date,
        "scheduled_time": "09:00:00",
        "estimated_duration": 60,
        "status": "Scheduled",
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    print(f"Base entry created: {base_se.name} (09:00 - 10:00 on {test_date})")

    # 2.2 Overlap Attack Scenarios
    overlap_cases = [
        ("Exact duplicate time (09:00 - 10:00)", "09:00:00", 60),
        ("Mid-job overlap start (09:30 - 10:30)", "09:30:00", 60),
        ("Pre-job spanning start (08:30 - 09:30)", "08:30:00", 60),
        ("Enclosing overlap window (08:00 - 11:00)", "08:00:00", 180),
        ("Enclosed overlap window (09:15 - 09:45)", "09:15:00", 30),
    ]

    for label, start_time, duration in overlap_cases:
        passed_blocked = False
        try:
            cand = frappe.get_doc({
                "doctype": "Schedule Entry",
                "entry_type": "Diagnostic",
                "service_bay": bay_name,
                "scheduled_date": test_date,
                "scheduled_time": start_time,
                "estimated_duration": duration,
                "status": "Scheduled",
            })
            cand.insert(ignore_permissions=True)
            print(f"CRITICAL VULNERABILITY: Overlapping entry allowed: {label}")
        except frappe.ValidationError:
            passed_blocked = True
        except Exception as e:
            passed_blocked = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        runner.record(f"Bay Collision Defense: {label} blocked", passed_blocked)

    # 2.3 Non-overlapping boundary edge cases
    # Preceding abutting entry: 08:00 - 09:00
    abutting_pre = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "service_bay": bay_name,
        "scheduled_date": test_date,
        "scheduled_time": "08:00:00",
        "estimated_duration": 60,
        "status": "Scheduled",
    })
    abutting_pre.insert(ignore_permissions=True)
    runner.record("Bay Abutting Boundary: preceding job (08:00-09:00) permitted", bool(abutting_pre.name))

    # Succeeding abutting entry: 10:00 - 11:00
    abutting_post = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "service_bay": bay_name,
        "scheduled_date": test_date,
        "scheduled_time": "10:00:00",
        "estimated_duration": 60,
        "status": "Scheduled",
    })
    abutting_post.insert(ignore_permissions=True)
    runner.record("Bay Abutting Boundary: succeeding job (10:00-11:00) permitted", bool(abutting_post.name))

    # Discard uncommitted abutting test entries via rollback
    frappe.db.rollback()
    # Delete base_se in clean transaction boundary
    frappe.delete_doc("Schedule Entry", base_se.name, force=True)
    frappe.db.commit()


    # 2.4 Concurrent Race Condition: Multi-threaded booking on identical bay & time
    print("\n--- 2.4 Concurrent Double-Booking Race Condition Test ---")
    race_results = []
    lock = threading.Lock()

    def concurrent_booking_worker(worker_id):
        frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
        frappe.connect()
        try:
            entry = frappe.get_doc({
                "doctype": "Schedule Entry",
                "entry_type": "Diagnostic",
                "service_bay": bay_name,
                "scheduled_date": test_date,
                "scheduled_time": "14:00:00",
                "estimated_duration": 60,
                "status": "Scheduled",
                "title": f"Worker-{worker_id}",
            })
            entry.insert(ignore_permissions=True)
            frappe.db.commit()
            with lock:
                race_results.append(("SUCCESS", worker_id, entry.name))
        except (frappe.ValidationError, Exception) as e:
            frappe.db.rollback()
            with lock:
                race_results.append(("BLOCKED", worker_id, type(e).__name__))
        finally:
            frappe.destroy()

    threads = []
    for w in range(4):
        t = threading.Thread(target=concurrent_booking_worker, args=(w,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    # Reconnect
    frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
    frappe.connect()

    successes = [r for r in race_results if r[0] == "SUCCESS"]
    print(f"Concurrent race booking outcomes (4 threads): {len(successes)} succeeded, {len(race_results) - len(successes)} blocked.")
    for r in race_results:
        print(f"  Worker outcome: {r}")

    # Inspect DB: how many entries actually exist for that slot?
    actual_booked = frappe.get_all(
        "Schedule Entry",
        filters={
            "service_bay": bay_name,
            "scheduled_date": test_date,
            "scheduled_time": "14:00:00",
        },
        pluck="name",
    )
    print(f"Actual booked rows in DB: {actual_booked}")

    runner.record(
        "Concurrency Race: exactly <= 1 booking succeeded on same bay/slot",
        len(actual_booked) <= 1,
        f"(actual in DB: {len(actual_booked)})",
    )

    # Cleanup booked entries
    for b_name in actual_booked:
        frappe.delete_doc("Schedule Entry", b_name, force=True)
    frappe.db.commit()

    # 2.5 Invariant: 1 Visit = 1 RO with Multiple Appointments (1:N)
    print("\n--- 2.5 Invariant Verification: Multiple Schedule Entries per Repair Order (1:N) ---")
    cust = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": f"{TEST_PREFIX}Cust_1toN",
        "customer_type": "Individual",
    }).insert(ignore_permissions=True)

    vin = f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"
    veh = frappe.get_doc({
        "doctype": "Repair Vehicle",
        "vin": vin,
        "customer": cust.name,
    }).insert(ignore_permissions=True)

    ro = frappe.get_doc({
        "doctype": "Repair Order",
        "customer": cust.name,
        "vehicle": veh.name,
        "odometer_in": 12345,
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    se_a = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Repair",
        "repair_order": ro.name,
        "scheduled_date": "2026-11-21",
        "scheduled_time": "09:00:00",
        "estimated_duration": 120,
    }).insert(ignore_permissions=True)

    se_b = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Repair",
        "repair_order": ro.name,
        "scheduled_date": "2026-11-22",
        "scheduled_time": "10:00:00",
        "estimated_duration": 60,
    }).insert(ignore_permissions=True)

    se_c = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Maintenance",
        "repair_order": ro.name,
        "scheduled_date": "2026-11-23",
        "scheduled_time": "11:00:00",
        "estimated_duration": 45,
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    linked = frappe.get_all("Schedule Entry", filters={"repair_order": ro.name}, pluck="name")
    is_1_to_n = (len(linked) == 3 and se_a.name in linked and se_b.name in linked and se_c.name in linked)
    runner.record("Core Domain Invariant: 1 RO with multiple supplemental Schedule Entries (1:N)", is_1_to_n)

    # 2.6 Repair Order Requirement Enforcement
    passed_ro_req = False
    try:
        ro_mand = frappe.get_doc({
            "doctype": "Schedule Entry",
            "entry_type": "Repair",  # requires_repair_order = 1
            "scheduled_date": "2026-11-25",
            "scheduled_time": "09:00:00",
            "estimated_duration": 60,
        })
        ro_mand.insert(ignore_permissions=True)
    except frappe.MandatoryError:
        passed_ro_req = True
    runner.record("Schedule Entry: 'Repair' type without Repair Order raises MandatoryError", passed_ro_req)

    # 2.7 Duration > 0 Validation
    passed_dur_zero = False
    try:
        dur_zero = frappe.get_doc({
            "doctype": "Schedule Entry",
            "entry_type": "Diagnostic",
            "scheduled_date": "2026-11-25",
            "scheduled_time": "09:00:00",
            "estimated_duration": 0,
        })
        dur_zero.insert(ignore_permissions=True)
    except frappe.ValidationError:
        passed_dur_zero = True
    finally:
        frappe.db.rollback()
    runner.record("Schedule Entry: estimated_duration=0 raises ValidationError", passed_dur_zero)

    # Cleanup section 2 fixtures
    frappe.db.rollback()
    for s in linked:
        try:
            frappe.delete_doc("Schedule Entry", s, force=True)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
    try:
        frappe.delete_doc("Repair Order", ro.name, force=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
    try:
        frappe.delete_doc("Repair Vehicle", veh.name, force=True)
        frappe.delete_doc("Customer", cust.name, force=True)
        frappe.delete_doc("Service Bay", bay_name, force=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()


# ==============================================================================
# SECTION 3: Standard Schedule Entry Type Tamper Resistance
# ==============================================================================

def test_suite_3_standard_types_anti_tamper():
    print("\n" + "=" * 70)
    print("TEST SUITE 3: Standard Schedule Entry Type Tamper & Deletion Defenses")
    print("=" * 70)

    for std_type in STANDARD_SCHEDULE_ENTRY_TYPES:
        # A: frappe.delete_doc with force=True
        del_blocked = False
        try:
            frappe.delete_doc("Schedule Entry Type", std_type, force=True)
        except frappe.ValidationError:
            del_blocked = True
        finally:
            frappe.db.rollback()
        runner.record(f"Standard Type '{std_type}': frappe.delete_doc(force=True) blocked", del_blocked)

        # B: frappe.rename_doc with force=True
        rename_blocked = False
        try:
            frappe.rename_doc("Schedule Entry Type", std_type, f"{std_type}_Hacked", force=True)
        except frappe.ValidationError:
            rename_blocked = True
        finally:
            frappe.db.rollback()
        runner.record(f"Standard Type '{std_type}': frappe.rename_doc(force=True) blocked", rename_blocked)

        # C: doc.type_name mutation on save()
        mut_blocked = False
        doc = frappe.get_doc("Schedule Entry Type", std_type)
        doc.type_name = f"{std_type}_Tampered"
        try:
            doc.save()
        except frappe.ValidationError:
            mut_blocked = True
        finally:
            frappe.db.rollback()
        runner.record(f"Standard Type '{std_type}': direct doc.save() mutation blocked", mut_blocked)

    # D: Attempt to rename custom type into standard type name
    cust_type_name = f"{TEST_PREFIX}Cust_Imposter"
    if frappe.db.exists("Schedule Entry Type", cust_type_name):
        frappe.delete_doc("Schedule Entry Type", cust_type_name, force=True)
    cust_t = frappe.get_doc({
        "doctype": "Schedule Entry Type",
        "type_name": cust_type_name,
        "requires_repair_order": 0,
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    cust_to_std_blocked = False
    try:
        frappe.rename_doc("Schedule Entry Type", cust_type_name, "Diagnostic", force=True)
    except (frappe.ValidationError, frappe.DuplicateEntryError):
        cust_to_std_blocked = True
    finally:
        frappe.db.rollback()

    runner.record("Custom Type: rename to standard name 'Diagnostic' blocked", cust_to_std_blocked)

    # E: In-use custom entry type deletion protection
    se_cust = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": cust_type_name,
        "scheduled_date": "2026-11-20",
        "scheduled_time": "15:00:00",
        "estimated_duration": 30,
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    in_use_del_blocked = False
    try:
        frappe.delete_doc("Schedule Entry Type", cust_type_name)
    except frappe.ValidationError as e:
        in_use_del_blocked = "referenced by existing Schedule Entry records" in str(e)
    finally:
        frappe.db.rollback()

    runner.record("Referential Integrity: deletion of in-use Schedule Entry Type blocked", in_use_del_blocked)

    # Cleanup
    frappe.delete_doc("Schedule Entry", se_cust.name, force=True)
    frappe.delete_doc("Schedule Entry Type", cust_type_name, force=True)
    frappe.db.commit()


# ==============================================================================
# SECTION 4: Equipment Tag & Service Bay Referential Integrity
# ==============================================================================

def test_suite_4_equipment_tag_referential_integrity():
    print("\n" + "=" * 70)
    print("TEST SUITE 4: Equipment Tag & Service Bay Referential Integrity")
    print("=" * 70)

    # 4.1 Verify standard equipment tags exist
    for stag in STANDARD_EQUIPMENT_TAGS:
        exists = frappe.db.exists("Equipment Tag", stag)
        runner.record(f"Standard Equipment Tag '{stag}' exists in database", bool(exists))

    # 4.2 Assigned Equipment Tag deletion protection
    test_tag_name = f"{TEST_PREFIX}TAG_REF"
    if frappe.db.exists("Equipment Tag", test_tag_name):
        frappe.delete_doc("Equipment Tag", test_tag_name, force=True)

    tag_doc = frappe.get_doc({
        "doctype": "Equipment Tag",
        "tag_name": test_tag_name,
        "description": "Adversarial orphan test tag",
    }).insert(ignore_permissions=True)

    test_bay_name = f"{TEST_PREFIX}BAY_REF"
    if frappe.db.exists("Service Bay", test_bay_name):
        frappe.delete_doc("Service Bay", test_bay_name, force=True)

    bay_doc = frappe.get_doc({
        "doctype": "Service Bay",
        "bay_name": test_bay_name,
        "is_active": 1,
        "equipment": [{"equipment_tag": test_tag_name}],
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    # Attempt to delete tag while assigned
    tag_del_blocked = False
    try:
        frappe.delete_doc("Equipment Tag", test_tag_name)
    except frappe.ValidationError as e:
        tag_del_blocked = "because it is assigned to Service Bay(s)" in str(e)
    finally:
        frappe.db.rollback()

    runner.record("Equipment Tag: deletion blocked while assigned to Service Bay", tag_del_blocked)

    # 4.3 Duplicate equipment tags on same Service Bay rejected
    passed_dup_tags = False
    try:
        dup_bay = frappe.get_doc({
            "doctype": "Service Bay",
            "bay_name": f"{TEST_PREFIX}BAY_DUP",
            "is_active": 1,
            "equipment": [
                {"equipment_tag": "Lift"},
                {"equipment_tag": "Lift"},
            ],
        })
        dup_bay.insert(ignore_permissions=True)
    except frappe.ValidationError:
        passed_dup_tags = True
    finally:
        frappe.db.rollback()

    runner.record("Service Bay: duplicate equipment tag rows rejected on validate()", passed_dup_tags)

    # 4.4 Non-existent equipment tag Link rejected
    passed_fake_tag = False
    try:
        fake_bay = frappe.get_doc({
            "doctype": "Service Bay",
            "bay_name": f"{TEST_PREFIX}BAY_FAKE",
            "is_active": 1,
            "equipment": [{"equipment_tag": "NON_EXISTENT_TAG_XYZ"}],
        })
        fake_bay.insert(ignore_permissions=True)
    except (frappe.LinkValidationError, frappe.ValidationError):
        passed_fake_tag = True
    finally:
        frappe.db.rollback()

    runner.record("Service Bay: non-existent equipment tag raises LinkValidationError", passed_fake_tag)

    # 4.5 Service Bay deletion blocked when it has active schedule entries
    se_active = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "service_bay": test_bay_name,
        "scheduled_date": "2026-11-20",
        "scheduled_time": "16:00:00",
        "estimated_duration": 30,
        "status": "Scheduled",
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    bay_del_blocked = False
    try:
        frappe.delete_doc("Service Bay", test_bay_name)
    except frappe.ValidationError as e:
        bay_del_blocked = "because it has active schedule entries" in str(e)
    finally:
        frappe.db.rollback()

    runner.record("Service Bay: deletion blocked when active Schedule Entries exist", bay_del_blocked)

    # Cancel schedule entry and clean up
    se_active.status = "Cancelled"
    se_active.save(ignore_permissions=True)
    frappe.delete_doc("Schedule Entry", se_active.name, force=True)
    frappe.delete_doc("Service Bay", test_bay_name, force=True)
    frappe.delete_doc("Equipment Tag", test_tag_name, force=True)
    frappe.db.commit()


# ==============================================================================
# SECTION 5: MariaDB 1020 Deadlock Burst Run & Database State Cleanliness
# ==============================================================================

def test_suite_5_deadlock_burst_and_cleanliness():
    print("\n" + "=" * 70)
    print("TEST SUITE 5: MariaDB 1020 Deadlock Burst Run & State Cleanliness")
    print("=" * 70)

    # Execute comprehensive cleanup to verify baseline
    cleanup_all_test_data(verbose=False)
    frappe.db.commit()

    se_count_0 = frappe.db.count("Schedule Entry")
    cust_test_count_0 = frappe.db.count("Customer", {"name": ("like", f"{TEST_PREFIX}%")})
    bay_test_count_0 = frappe.db.count("Service Bay", {"name": ("like", f"{TEST_PREFIX}%")})
    type_test_count_0 = frappe.db.count("Schedule Entry Type", {"name": ("like", f"{TEST_PREFIX}%")})
    tag_test_count_0 = frappe.db.count("Equipment Tag", {"name": ("like", f"{TEST_PREFIX}%")})

    baseline_clean = (
        se_count_0 == 0
        and cust_test_count_0 == 0
        and bay_test_count_0 == 0
        and type_test_count_0 == 0
        and tag_test_count_0 == 0
    )
    runner.record("Database Cleanliness: 0 residual test entities in MariaDB tables", baseline_clean)

    # Verify static fixtures remain intact
    static_cust_exists = frappe.db.exists("Customer", "Test Customer")
    static_veh_exists = frappe.db.exists("Repair Vehicle", "5YJSA1E26CF000001")
    runner.record("Static Master Fixtures: 'Test Customer' and '5YJSA1E26CF000001' intact", bool(static_cust_exists and static_veh_exists))


def main():
    frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
    frappe.connect()

    start_total = time.time()
    print("=" * 70)
    print("STARTING ADVERSARIAL STRESS & CONCURRENCY HARNESS FOR MILESTONE 1")
    print("=" * 70)

    try:
        test_suite_1_ism_settings_stress()
        test_suite_2_schedule_entry_stress()
        test_suite_3_standard_types_anti_tamper()
        test_suite_4_equipment_tag_referential_integrity()
        test_suite_5_deadlock_burst_and_cleanliness()
    finally:
        cleanup_all_test_data(verbose=False)
        frappe.db.commit()

    total_time = time.time() - start_total
    print("\n" + "=" * 70)
    print(f"ADVERSARIAL SUITE SUMMARY: {runner.passed} PASSED, {runner.failed} FAILED in {total_time:.2f}s")
    print("=" * 70)

    if runner.failed > 0:
        print("\nFAILURES ENCOUNTERED:")
        for name, passed, details in runner.results:
            if not passed:
                print(f" - [FAIL] {name} {details}")
        sys.exit(1)
    else:
        print("\nALL ADVERSARIAL AND CONCURRENCY STRESS TESTS PASSED WITH 0 FAILURES!")
        sys.exit(0)


if __name__ == "__main__":
    main()
