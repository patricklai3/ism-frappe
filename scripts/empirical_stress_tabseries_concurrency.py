# Copyright (c) 2026, patricklai3 and contributors
# Empirical Stress & Concurrency Challenge Harness for Milestone 1 Iteration 6
# Targets: tabSeries MariaDB 1020 race conditions, concurrent Schedule Entry creation, sequence monotonicity

import os
import sys
import time
import multiprocessing as mp
from typing import List, Dict, Any

# Ensure we can import frappe
import frappe
from frappe import _

SITE = "development.localhost"
TEST_PREFIX = "ADV_STRESS_"


def worker_create_entries(worker_id: int, count: int, bay: str, base_date: str, result_queue: mp.Queue):
    """Worker process: connects to Frappe, rapidly creates `count` Schedule Entry records concurrently."""
    try:
        frappe.init(site=SITE)
        frappe.connect()

        created_names = []
        errors = []

        times = [
            f"{h:02d}:00:00" for h in range(8, 8 + count)
        ]

        for i in range(count):
            sched_time = times[i % len(times)]
            doc_data = {
                "doctype": "Schedule Entry",
                "service_bay": bay,
                "scheduled_date": base_date,
                "scheduled_time": sched_time,
                "estimated_duration": 45,
                "entry_type": "Internal Service",  # Internal Service does not require Repair Order
                "status": "Scheduled",
                "provisional_customer_name": f"{TEST_PREFIX}Worker_{worker_id}_Cust_{i}",
                "provisional_contact_phone": "555-0199",
                "provisional_vehicle_info": f"{TEST_PREFIX}Vehicle_{worker_id}_{i}",
                "notes": f"Concurrency stress worker {worker_id} iteration {i}",
            }
            try:
                doc = frappe.get_doc(doc_data).insert(ignore_permissions=True)
                frappe.db.commit()
                created_names.append(doc.name)
            except Exception as e:
                frappe.db.rollback()
                err_msg = f"Worker {worker_id} failed on insert {i}: {type(e).__name__}: {str(e)}"
                errors.append(err_msg)

        frappe.destroy()
        result_queue.put({
            "worker_id": worker_id,
            "created": created_names,
            "errors": errors,
            "success": len(errors) == 0,
        })
    except Exception as exc:
        result_queue.put({
            "worker_id": worker_id,
            "created": [],
            "errors": [f"Fatal worker exception: {type(exc).__name__}: {str(exc)}"],
            "success": False,
        })


def test_concurrent_tabseries_hammer(num_workers: int = 4, entries_per_worker: int = 10) -> bool:
    """Stress test tabSeries with concurrent worker processes creating Schedule Entry records."""
    print(f"\n========================================================")
    print(f"TEST 1: Concurrent tabSeries Hammer ({num_workers} processes x {entries_per_worker} entries = {num_workers * entries_per_worker} total)")
    print(f"========================================================")

    bays = ["Test Bay 1", "Test Bay 2"]
    dates = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]

    result_queue = mp.Queue()
    processes: List[mp.Process] = []

    start_time = time.time()
    for w_id in range(num_workers):
        bay = bays[(w_id // len(dates)) % len(bays)]
        date = dates[w_id % len(dates)]
        p = mp.Process(
            target=worker_create_entries,
            args=(w_id, entries_per_worker, bay, date, result_queue),
        )
        processes.append(p)
        p.start()

    for p in processes:
        p.join(timeout=60)

    elapsed = time.time() - start_time
    print(f"All worker processes finished in {elapsed:.2f}s")

    all_results = []
    while not result_queue.empty():
        all_results.append(result_queue.get())

    total_created = []
    total_errors = []

    for r in all_results:
        w_id = r["worker_id"]
        created = r["created"]
        errors = r["errors"]
        print(f"Worker {w_id}: {len(created)}/{entries_per_worker} created, {len(errors)} errors")
        if errors:
            for err in errors:
                print(f"   [ERROR] {err}")
        total_created.extend(created)
        total_errors.extend(errors)

    # Validate results
    expected_total = num_workers * entries_per_worker
    unique_names = set(total_created)

    print(f"\nTotal entries requested: {expected_total}")
    print(f"Total entries created: {len(total_created)}")
    print(f"Total unique names: {len(unique_names)}")
    print(f"Total errors encountered: {len(total_errors)}")

    if len(total_errors) > 0:
        print(f"[FAIL] Concurrency stress test encountered {len(total_errors)} errors!")
        return False

    if len(total_created) != expected_total:
        print(f"[FAIL] Expected {expected_total} created, got {len(total_created)}")
        return False

    if len(unique_names) != len(total_created):
        print(f"[FAIL] Duplicate naming series detected! {len(total_created) - len(unique_names)} duplicates.")
        return False

    print(f"[PASS] All {expected_total} Schedule Entries created concurrently with 0 deadlocks and 0 duplicate names.")
    return True


def test_rapid_fire_sequential(count: int = 30) -> bool:
    """Rapid sequential bursts without pausing."""
    print(f"\n========================================================")
    print(f"TEST 2: Rapid-Fire Sequential Burst ({count} entries)")
    print(f"========================================================")

    created_names = []
    errors = []
    start_time = time.time()

    for i in range(count):
        # vary date and hour to avoid bay collision
        day = (i // 8) + 1
        hour = (i % 8) + 8
        date_str = f"2026-12-{day:02d}"
        time_str = f"{hour:02d}:00:00"
        doc_data = {
            "doctype": "Schedule Entry",
            "service_bay": "Test Bay 1",
            "scheduled_date": date_str,
            "scheduled_time": time_str,
            "estimated_duration": 30,
            "entry_type": "Internal Service",
            "status": "Scheduled",
            "provisional_customer_name": f"{TEST_PREFIX}Rapid_{i}",
            "notes": f"Rapid fire sequential test #{i}",
        }
        try:
            doc = frappe.get_doc(doc_data).insert(ignore_permissions=True)
            frappe.db.commit()
            created_names.append(doc.name)
        except Exception as e:
            frappe.db.rollback()
            errors.append(f"Seq burst #{i} failed: {type(e).__name__}: {str(e)}")

    elapsed = time.time() - start_time
    print(f"Sequential burst of {count} entries completed in {elapsed:.2f}s (avg {elapsed/count*1000:.1f}ms/entry)")
    print(f"Created: {len(created_names)}, Errors: {len(errors)}")

    if errors:
        for err in errors:
            print(f"   [ERROR] {err}")
        return False

    if len(set(created_names)) != count:
        print("[FAIL] Duplicate names in sequential burst!")
        return False

    print(f"[PASS] Rapid-fire sequential burst passed without errors.")
    return True


def test_transaction_rollback_recovery() -> bool:
    """Test that a transaction rollback during naming or creation does not corrupt tabSeries."""
    print(f"\n========================================================")
    print(f"TEST 3: Transaction Rollback & tabSeries Recovery")
    print(f"========================================================")

    try:
        # Create an entry inside an explicit uncommitted transaction, then roll back
        doc = frappe.get_doc({
            "doctype": "Schedule Entry",
            "service_bay": "Test Bay 1",
            "scheduled_date": "2026-11-20",
            "scheduled_time": "09:00:00",
            "estimated_duration": 30,
            "entry_type": "Internal Service",
            "status": "Scheduled",
            "provisional_customer_name": f"{TEST_PREFIX}Aborted",
        }).insert(ignore_permissions=True)
        aborted_name = doc.name
        print(f"Inserted entry {aborted_name} (uncommitted)")

        # Explicitly rollback
        frappe.db.rollback()
        print(f"Rolled back transaction. Verifying {aborted_name} does NOT exist in DB...")
        exists = frappe.db.exists("Schedule Entry", aborted_name)
        if exists:
            print(f"[FAIL] Entry {aborted_name} still exists after rollback!")
            return False

        # Now insert next entry and verify it gets created cleanly
        next_doc = frappe.get_doc({
            "doctype": "Schedule Entry",
            "service_bay": "Test Bay 1",
            "scheduled_date": "2026-11-20",
            "scheduled_time": "09:00:00",
            "estimated_duration": 30,
            "entry_type": "Internal Service",
            "status": "Scheduled",
            "provisional_customer_name": f"{TEST_PREFIX}PostRollback",
        }).insert(ignore_permissions=True)
        frappe.db.commit()
        print(f"Next entry created successfully: {next_doc.name}")

        print("[PASS] Rollback recovery verified.")
        return True
    except Exception as e:
        frappe.db.rollback()
        print(f"[FAIL] Rollback recovery exception: {type(e).__name__}: {e}")
        return False


def cleanup_stress_data():
    """Purge all stress test data."""
    print(f"\n--- Cleaning up stress test data ---")
    try:
        frappe.init(site=SITE)
        frappe.connect()

        from ism.tests.cleanup import cleanup_all_test_data
        cleanup_all_test_data(False)
        frappe.db.commit()
        remaining = frappe.db.count('Schedule Entry')
        print(f"cleanup_all_test_data() executed successfully. Remaining Schedule Entries: {remaining}")
        frappe.destroy()
    except Exception as e:
        print(f"Cleanup error: {e}")


def main():
    print("Starting adversarial concurrency harness...")

    test1_ok = test_concurrent_tabseries_hammer(num_workers=8, entries_per_worker=15)

    frappe.init(site=SITE)
    frappe.connect()

    test2_ok = test_rapid_fire_sequential(count=50)
    test3_ok = test_transaction_rollback_recovery()

    cleanup_stress_data()

    all_ok = test1_ok and test2_ok and test3_ok
    print(f"\n========================================================")
    print(f"ADVERSARIAL CONCURRENCY SUMMARY: {'ALL TESTS PASSED' if all_ok else 'SOME TESTS FAILED'}")
    print(f"========================================================")

    if not all_ok:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
