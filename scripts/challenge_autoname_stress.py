# Copyright (c) 2026, patricklai3 and contributors
# Empirical Stress Test Harness for Autoname and Naming Series (Milestone 1 Iteration 7 Gate)
# Evaluates 50 rapid sequential and 50 concurrent Schedule Entry and Repair Order documents
# Verifies whether make_autoname and _generate_safe_series_name produce duplicate keys or throw 1020/deadlock/1062 errors.

import os
import sys
import time
import concurrent.futures
from typing import List, Dict, Any

import frappe

SITE = "development.localhost"
TEST_PREFIX = "CHAL_STRESS_"


def init_frappe():
    frappe.init(site=SITE)
    frappe.connect()


def cleanup_all(cust_name=None, veh_name=None):
    """Clean up all challenge test records from MariaDB."""
    try:
        frappe.db.rollback()

        # Delete any Schedule Entries matching prefix
        prefix_ses = frappe.get_all(
            "Schedule Entry",
            filters=[["provisional_customer_name", "like", f"{TEST_PREFIX}%"]],
            pluck="name"
        )
        for name in prefix_ses:
            try:
                frappe.delete_doc("Schedule Entry", name, force=True, ignore_permissions=True)
            except Exception:
                pass

        # Delete tracked Repair Orders
        prefix_ros = frappe.get_all(
            "Repair Order",
            filters=[["notes", "like", f"%{TEST_PREFIX}%"]],
            pluck="name"
        )
        for name in prefix_ros:
            try:
                frappe.delete_doc("Repair Order", name, force=True, ignore_permissions=True)
            except Exception:
                pass

        # Delete vehicle & customer
        if veh_name and frappe.db.exists("Repair Vehicle", veh_name):
            try:
                frappe.delete_doc("Repair Vehicle", veh_name, force=True, ignore_permissions=True)
            except Exception:
                pass

        prefix_vehs = frappe.get_all(
            "Repair Vehicle",
            filters=[["vin", "like", "5YJ3E1EBXNFCHAL%"]],
            pluck="name"
        )
        for name in prefix_vehs:
            try:
                frappe.delete_doc("Repair Vehicle", name, force=True, ignore_permissions=True)
            except Exception:
                pass

        if cust_name and frappe.db.exists("Customer", cust_name):
            try:
                frappe.delete_doc("Customer", cust_name, force=True, ignore_permissions=True)
            except Exception:
                pass

        prefix_custs = frappe.get_all(
            "Customer",
            filters=[["customer_name", "like", f"{TEST_PREFIX}%"]],
            pluck="name"
        )
        for name in prefix_custs:
            try:
                frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
            except Exception:
                pass

        frappe.db.commit()
    except Exception as e:
        print(f"Warning during cleanup: {e}", file=sys.stderr)
        frappe.db.rollback()


def setup_fixtures():
    """Create shared test Customer and Repair Vehicle."""
    cust_name = f"{TEST_PREFIX}Cust_01"
    if not frappe.db.exists("Customer", cust_name):
        cust = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": cust_name,
            "customer_group": "Commercial",
            "territory": "All Territories",
        }).insert(ignore_permissions=True)
    else:
        cust = frappe.get_doc("Customer", cust_name)

    vin = "5YJ3E1EBXNFCHAL01"
    if not frappe.db.exists("Repair Vehicle", vin):
        veh = frappe.get_doc({
            "doctype": "Repair Vehicle",
            "vin": vin,
            "license_plate": "CHAL-01",
            "customer": cust.name,
            "manufacturer": "Tesla",
            "model": "Model 3",
            "model_year": 2023,
        }).insert(ignore_permissions=True)
    else:
        veh = frappe.get_doc("Repair Vehicle", vin)

    frappe.db.commit()
    return cust.name, veh.name


def test_rapid_sequential_schedule_entry(count=50):
    print(f"\n--- Test 1: Rapid Sequential Creation of {count} Schedule Entry docs ---")
    created = []
    errors = []
    t0 = time.time()
    for i in range(count):
        doc_dict = {
            "doctype": "Schedule Entry",
            "scheduled_date": frappe.utils.today(),
            "scheduled_time": "09:00:00",
            "estimated_duration": 30,
            "entry_type": "Diagnostic",
            "status": "Draft",
            "provisional_customer_name": f"{TEST_PREFIX}SE_SEQ_{i}",
        }
        for attempt in range(3):
            try:
                doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
                frappe.db.commit()
                created.append(doc.name)
                break
            except Exception as e:
                if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                errors.append(f"Seq SE {i}: {type(e).__name__}: {str(e)}")
                break

    elapsed = time.time() - t0
    duplicates = len(created) - len(set(created))
    print(f"Created {len(created)}/{count} Schedule Entry docs in {elapsed:.2f}s (avg {elapsed/count*1000:.1f}ms/doc)")
    print(f"Errors: {len(errors)}, Duplicates: {duplicates}")
    return {
        "count": count,
        "created": len(created),
        "errors": errors,
        "duplicates": duplicates,
        "elapsed": elapsed,
        "passed": len(created) == count and len(errors) == 0 and duplicates == 0,
    }


def test_rapid_sequential_repair_order(cust_name, veh_name, count=50):
    print(f"\n--- Test 2: Rapid Sequential Creation of {count} Repair Order docs ---")
    created = []
    errors = []
    t0 = time.time()
    for i in range(count):
        doc_dict = {
            "doctype": "Repair Order",
            "customer": cust_name,
            "vehicle": veh_name,
            "odometer_in": 10000 + i,
            "notes": f"{TEST_PREFIX}RO_SEQ_{i}",
        }
        for attempt in range(3):
            try:
                doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
                frappe.db.commit()
                created.append(doc.name)
                break
            except Exception as e:
                if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                errors.append(f"Seq RO {i}: {type(e).__name__}: {str(e)}")
                break

    elapsed = time.time() - t0
    duplicates = len(created) - len(set(created))
    print(f"Created {len(created)}/{count} Repair Order docs in {elapsed:.2f}s (avg {elapsed/count*1000:.1f}ms/doc)")
    print(f"Errors: {len(errors)}, Duplicates: {duplicates}")
    if errors:
        print("Sample error:", errors[0])
    return {
        "count": count,
        "created": len(created),
        "errors": errors,
        "duplicates": duplicates,
        "elapsed": elapsed,
        "passed": len(created) == count and len(errors) == 0 and duplicates == 0,
    }


def _worker_se_task(worker_id: int, count_per_worker: int):
    frappe.init(site=SITE)
    frappe.connect()
    worker_created = []
    worker_errors = []
    try:
        for i in range(count_per_worker):
            doc_dict = {
                "doctype": "Schedule Entry",
                "scheduled_date": frappe.utils.today(),
                "scheduled_time": "11:00:00",
                "estimated_duration": 45,
                "entry_type": "Diagnostic",
                "status": "Draft",
                "provisional_customer_name": f"{TEST_PREFIX}SE_CONC_W{worker_id}_{i}",
            }
            try:
                doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
                frappe.db.commit()
                worker_created.append(doc.name)
            except Exception as e:
                frappe.db.rollback()
                worker_errors.append(f"Worker {worker_id} item {i}: {type(e).__name__}: {str(e)}")
    finally:
        frappe.destroy()
    return worker_created, worker_errors


def test_concurrent_schedule_entry(num_threads=5, per_thread=10):
    total = num_threads * per_thread
    print(f"\n--- Test 3: Concurrent Creation of {total} Schedule Entry docs ({num_threads} threads x {per_thread}) ---")
    t0 = time.time()
    created = []
    errors = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [
            executor.submit(_worker_se_task, w_id, per_thread)
            for w_id in range(num_threads)
        ]
        for f in concurrent.futures.as_completed(futures):
            c, e = f.result()
            created.extend(c)
            errors.extend(e)

    # Reconnect main thread
    frappe.init(site=SITE)
    frappe.connect()

    elapsed = time.time() - t0
    duplicates = len(created) - len(set(created))
    print(f"Created {len(created)}/{total} Schedule Entry docs in {elapsed:.2f}s ({len(errors)} errors)")
    print(f"Unique names: {len(set(created))}/{len(created)}, Duplicates: {duplicates}")
    if errors:
        print(f"Sample errors (first 3):")
        for err in errors[:3]:
            print(f"  - {err}")
    return {
        "total": total,
        "created": len(created),
        "errors": errors,
        "duplicates": duplicates,
        "elapsed": elapsed,
        "passed": len(created) == total and len(errors) == 0 and duplicates == 0,
    }


def _worker_ro_task(worker_id: int, count_per_worker: int, cust_name: str, veh_name: str):
    frappe.init(site=SITE)
    frappe.connect()
    worker_created = []
    worker_errors = []
    try:
        for i in range(count_per_worker):
            doc_dict = {
                "doctype": "Repair Order",
                "customer": cust_name,
                "vehicle": veh_name,
                "odometer_in": 20000 + (worker_id * 100) + i,
                "notes": f"{TEST_PREFIX}RO_CONC_W{worker_id}_{i}",
            }
            try:
                doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
                frappe.db.commit()
                worker_created.append(doc.name)
            except Exception as e:
                frappe.db.rollback()
                worker_errors.append(f"Worker {worker_id} item {i}: {type(e).__name__}: {str(e)}")
    finally:
        frappe.destroy()
    return worker_created, worker_errors


def test_concurrent_repair_order(cust_name, veh_name, num_threads=5, per_thread=10):
    total = num_threads * per_thread
    print(f"\n--- Test 4: Concurrent Creation of {total} Repair Order docs ({num_threads} threads x {per_thread}) ---")
    t0 = time.time()
    created = []
    errors = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [
            executor.submit(_worker_ro_task, w_id, per_thread, cust_name, veh_name)
            for w_id in range(num_threads)
        ]
        for f in concurrent.futures.as_completed(futures):
            c, e = f.result()
            created.extend(c)
            errors.extend(e)

    # Reconnect main thread
    frappe.init(site=SITE)
    frappe.connect()

    elapsed = time.time() - t0
    duplicates = len(created) - len(set(created))
    print(f"Created {len(created)}/{total} Repair Order docs in {elapsed:.2f}s ({len(errors)} errors)")
    print(f"Unique names: {len(set(created))}/{len(created)}, Duplicates: {duplicates}")
    if errors:
        print(f"Sample errors (first 3):")
        for err in errors[:3]:
            print(f"  - {err}")
    return {
        "total": total,
        "created": len(created),
        "errors": errors,
        "duplicates": duplicates,
        "elapsed": elapsed,
        "passed": len(created) == total and len(errors) == 0 and duplicates == 0,
    }


def main():
    print("================================================================================")
    print("EMPIRICAL HARNESS: AUTONAME & NAMING SERIES STRESS (Milestone 1 Iteration 7)")
    print("================================================================================")
    init_frappe()

    cust_name = None
    veh_name = None

    try:
        cleanup_all()
        cust_name, veh_name = setup_fixtures()

        # 1. 50 rapid sequential Schedule Entry
        r1 = test_rapid_sequential_schedule_entry(50)

        # 2. 50 rapid sequential Repair Order
        r2 = test_rapid_sequential_repair_order(cust_name, veh_name, 50)

        # Clean up sequential before concurrent
        cleanup_all(cust_name, veh_name)
        cust_name, veh_name = setup_fixtures()

        # 3. 50 concurrent Schedule Entry (5 threads x 10)
        r3 = test_concurrent_schedule_entry(5, 10)

        # Clean up SE before RO concurrent
        cleanup_all(cust_name, veh_name)
        cust_name, veh_name = setup_fixtures()

        # 4. 50 concurrent Repair Order (5 threads x 10)
        r4 = test_concurrent_repair_order(cust_name, veh_name, 5, 10)

        print("\n================================================================================")
        print("EMPIRICAL STRESS TEST RESULTS SUMMARY")
        print("================================================================================")
        print(f"1. Rapid Sequential Schedule Entry (50 docs): {'PASS' if r1['passed'] else 'FAIL'} ({r1['created']}/50 created, {len(r1['errors'])} errors, {r1['duplicates']} duplicates)")
        print(f"2. Rapid Sequential Repair Order (50 docs):   {'PASS' if r2['passed'] else 'FAIL'} ({r2['created']}/50 created, {len(r2['errors'])} errors, {r2['duplicates']} duplicates)")
        print(f"3. Concurrent Schedule Entry (50 docs):       {'PASS' if r3['passed'] else 'FAIL'} ({r3['created']}/50 created, {len(r3['errors'])} errors, {r3['duplicates']} duplicates)")
        print(f"4. Concurrent Repair Order (50 docs):         {'PASS' if r4['passed'] else 'FAIL'} ({r4['created']}/50 created, {len(r4['errors'])} errors, {r4['duplicates']} duplicates)")
        print("================================================================================")

    finally:
        print("\nFinal cleanup of all test artifacts...")
        cleanup_all(cust_name, veh_name)
        remaining_se = frappe.db.count("Schedule Entry", filters=[["provisional_customer_name", "like", f"{TEST_PREFIX}%"]])
        remaining_ro = frappe.db.count("Repair Order", filters=[["notes", "like", f"%{TEST_PREFIX}%"]])
        print(f"Post-cleanup verification: Remaining test SE: {remaining_se}, Remaining test RO: {remaining_ro}")


if __name__ == "__main__":
    main()
