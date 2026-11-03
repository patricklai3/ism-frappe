# Copyright (c) 2026, patricklai3 and contributors
# Adversarial Challenge Harness for Milestone 1 Iteration 3
# Targets: Schedule Entry Type rename defenses and Schedule Entry data cleanup isolation

import sys
import frappe
from frappe import _
from ism.tests.cleanup import cleanup_all_test_data

STANDARD_SCHEDULE_ENTRY_TYPES = [
    "Diagnostic",
    "Repair",
    "Maintenance",
    "Meeting",
    "Internal Service",
]

TEST_PREFIX = "_TEST_ADV_IT3_"


def log_test(name, passed, details=""):
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name} {details}")
    if not passed:
        return False
    return True


def attack_1_rename_standard_types_with_force():
    """Attack 1: Attempt to rename standard entry types using frappe.rename_doc with force=True."""
    print("\n=== Attack 1: Rename Standard Types via frappe.rename_doc with force=True ===")
    all_passed = True

    for std_type in STANDARD_SCHEDULE_ENTRY_TYPES:
        # Variant A: Standard rename with force=True
        target_name = f"{std_type}_Pwned"
        passed_a = False
        try:
            frappe.rename_doc("Schedule Entry Type", std_type, target_name, force=True)
            print(f"CRITICAL VULNERABILITY: Standard type '{std_type}' was successfully renamed to '{target_name}'!")
        except (frappe.ValidationError, frappe.exceptions.ValidationError) as e:
            passed_a = True
        except Exception as e:
            # Still blocked, but check if ValidationError
            print(f"Raised non-ValidationError: {type(e).__name__}: {e}")
            passed_a = isinstance(e, frappe.ValidationError)

        # Check DB integrity: std_type must exist, target_name must NOT exist
        exists_std = frappe.db.exists("Schedule Entry Type", std_type)
        exists_target = frappe.db.exists("Schedule Entry Type", target_name)
        if not exists_std or exists_target:
            passed_a = False
            print(f"DB State violation: exists({std_type})={exists_std}, exists({target_name})={exists_target}")

        if not log_test(f"Rename '{std_type}' with force=True blocked", passed_a):
            all_passed = False

        # Variant B: Rename passing Document instance doc=... with force=True
        target_doc_name = f"{std_type}_DocPwned"
        passed_b = False
        try:
            from frappe.model.rename_doc import rename_doc as model_rename_doc
            doc = frappe.get_doc("Schedule Entry Type", std_type)
            model_rename_doc(doc=doc, new=target_doc_name, force=True)
            print(f"CRITICAL VULNERABILITY: Standard type '{std_type}' renamed via doc instance!")
        except frappe.ValidationError:
            passed_b = True
        except Exception as e:
            passed_b = isinstance(e, frappe.ValidationError)

        exists_std = frappe.db.exists("Schedule Entry Type", std_type)
        exists_doc_target = frappe.db.exists("Schedule Entry Type", target_doc_name)
        if not exists_std or exists_doc_target:
            passed_b = False

        if not log_test(f"Rename '{std_type}' (doc instance) with force=True blocked", passed_b):
            all_passed = False

        # Variant C: Rename with whitespace around standard name
        target_ws_name = f"{std_type}_WSPwned"
        passed_c = False
        try:
            frappe.rename_doc("Schedule Entry Type", f" {std_type} ", target_ws_name, force=True)
            print(f"CRITICAL VULNERABILITY: Standard type '{std_type}' renamed via whitespace padded old name!")
        except (frappe.ValidationError, frappe.DoesNotExistError):
            passed_c = True
        except Exception as e:
            passed_c = isinstance(e, (frappe.ValidationError, frappe.DoesNotExistError))

        if not log_test(f"Rename ' {std_type} ' with force=True blocked", passed_c):
            all_passed = False

        # Variant D: Merge attempt into another standard type
        other_type = "Repair" if std_type != "Repair" else "Diagnostic"
        passed_d = False
        try:
            frappe.rename_doc("Schedule Entry Type", std_type, other_type, merge=True, force=True)
            print(f"CRITICAL VULNERABILITY: Standard type '{std_type}' merged into '{other_type}'!")
        except frappe.ValidationError:
            passed_d = True
        except Exception as e:
            passed_d = isinstance(e, frappe.ValidationError)

        exists_std = frappe.db.exists("Schedule Entry Type", std_type)
        exists_other = frappe.db.exists("Schedule Entry Type", other_type)
        if not exists_std or not exists_other:
            passed_d = False

        if not log_test(f"Merge '{std_type}' -> '{other_type}' with force=True blocked", passed_d):
            all_passed = False

    return all_passed


def attack_2_rename_custom_type_to_standard():
    """Attack 2: Attempt to rename a custom type into a standard name."""
    print("\n=== Attack 2: Rename Custom Type into Standard Name ===")
    all_passed = True

    custom_name = f"{TEST_PREFIX}Custom_Renamer"
    if frappe.db.exists("Schedule Entry Type", custom_name):
        frappe.delete_doc("Schedule Entry Type", custom_name, force=True)

    custom_doc = frappe.get_doc({
        "doctype": "Schedule Entry Type",
        "type_name": custom_name,
        "requires_repair_order": 0,
        "color": "#999999",
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    for std_type in STANDARD_SCHEDULE_ENTRY_TYPES:
        # Variant A: Direct rename to standard name with force=True
        passed_a = False
        try:
            frappe.rename_doc("Schedule Entry Type", custom_name, std_type, force=True)
            print(f"CRITICAL VULNERABILITY: Custom type renamed to adopt standard name '{std_type}'!")
        except (frappe.ValidationError, frappe.exceptions.DuplicateEntryError):
            passed_a = True
        except Exception as e:
            passed_a = isinstance(e, (frappe.ValidationError, frappe.DuplicateEntryError))

        # Check DB state
        std_doc = frappe.get_doc("Schedule Entry Type", std_type)
        if std_doc.color == "#999999":
            passed_a = False
            print(f"CRITICAL VULNERABILITY: Standard type '{std_type}' was overwritten by custom type attributes!")

        if not log_test(f"Rename custom to '{std_type}' with force=True blocked", passed_a):
            all_passed = False

        # Variant B: Rename to standard name with padding whitespace
        passed_b = False
        try:
            frappe.rename_doc("Schedule Entry Type", custom_name, f"  {std_type}  ", force=True)
            print(f"CRITICAL VULNERABILITY: Custom type renamed to padded standard name '  {std_type}  '!")
        except (frappe.ValidationError, frappe.DuplicateEntryError):
            passed_b = True
        except Exception as e:
            passed_b = isinstance(e, (frappe.ValidationError, frappe.DuplicateEntryError))

        if not log_test(f"Rename custom to '  {std_type}  ' blocked", passed_b):
            all_passed = False

        # Variant C: Merge into standard name
        passed_c = False
        try:
            frappe.rename_doc("Schedule Entry Type", custom_name, std_type, merge=True, force=True)
            print(f"CRITICAL VULNERABILITY: Custom type merged into standard name '{std_type}'!")
        except (frappe.ValidationError, frappe.DuplicateEntryError):
            passed_c = True
        except Exception as e:
            passed_c = isinstance(e, (frappe.ValidationError, frappe.DuplicateEntryError))

        if not log_test(f"Merge custom into standard '{std_type}' blocked", passed_c):
            all_passed = False

    # Cleanup custom doc
    if frappe.db.exists("Schedule Entry Type", custom_name):
        frappe.delete_doc("Schedule Entry Type", custom_name, force=True)
        frappe.db.commit()

    return all_passed


def attack_3_direct_mutation_via_doc_save():
    """Attack 3: Attempt direct mutation of type_name on standard records via doc.save()."""
    print("\n=== Attack 3: Direct Mutation of type_name via doc.save() ===")
    all_passed = True

    for std_type in STANDARD_SCHEDULE_ENTRY_TYPES:
        # Variant A: Standard doc.save()
        passed_a = False
        doc = frappe.get_doc("Schedule Entry Type", std_type)
        doc.type_name = f"{std_type}_Hacked"
        try:
            doc.save()
            print(f"CRITICAL VULNERABILITY: doc.save() allowed type_name mutation on standard type '{std_type}'!")
        except frappe.ValidationError as e:
            passed_a = True
        except Exception as e:
            passed_a = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        # Check DB value
        db_type_name = frappe.db.get_value("Schedule Entry Type", std_type, "type_name")
        if db_type_name != std_type:
            passed_a = False
            print(f"DB corruption: type_name is '{db_type_name}', expected '{std_type}'")

        if not log_test(f"doc.save() mutation on '{std_type}' blocked", passed_a):
            all_passed = False

        # Variant B: doc.save(ignore_permissions=True)
        passed_b = False
        doc = frappe.get_doc("Schedule Entry Type", std_type)
        doc.type_name = f"{std_type}_PrivHacked"
        try:
            doc.save(ignore_permissions=True)
            print(f"CRITICAL VULNERABILITY: doc.save(ignore_permissions=True) allowed mutation on '{std_type}'!")
        except frappe.ValidationError as e:
            passed_b = True
        except Exception as e:
            passed_b = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        if not log_test(f"doc.save(ignore_permissions=True) mutation on '{std_type}' blocked", passed_b):
            all_passed = False

        # Variant C: Mutating to empty string
        passed_c = False
        doc = frappe.get_doc("Schedule Entry Type", std_type)
        doc.type_name = ""
        try:
            doc.save()
            print(f"CRITICAL VULNERABILITY: doc.save() allowed empty type_name on standard type '{std_type}'!")
        except frappe.ValidationError:
            passed_c = True
        except Exception as e:
            passed_c = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        if not log_test(f"doc.save() empty type_name on '{std_type}' blocked", passed_c):
            all_passed = False

        # Variant D: Mutating to spaces only
        passed_d = False
        doc = frappe.get_doc("Schedule Entry Type", std_type)
        doc.type_name = "    "
        try:
            doc.save()
            print(f"CRITICAL VULNERABILITY: doc.save() allowed whitespace type_name on standard type '{std_type}'!")
        except frappe.ValidationError:
            passed_d = True
        except Exception as e:
            passed_d = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        if not log_test(f"doc.save() whitespace type_name on '{std_type}' blocked", passed_d):
            all_passed = False

    return all_passed


def attack_4_deletion_bypass():
    """Attack 4: Attempt deletion bypass by renaming then deleting, or deleting standard types."""
    print("\n=== Attack 4: Deletion Bypass & Standard Type Deletion Protection ===")
    all_passed = True

    # 4.1 Direct delete via doc.delete()
    for std_type in STANDARD_SCHEDULE_ENTRY_TYPES:
        passed_del = False
        doc = frappe.get_doc("Schedule Entry Type", std_type)
        try:
            doc.delete()
            print(f"CRITICAL VULNERABILITY: doc.delete() deleted standard type '{std_type}'!")
        except frappe.ValidationError:
            passed_del = True
        except Exception as e:
            passed_del = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        if not frappe.db.exists("Schedule Entry Type", std_type):
            passed_del = False

        if not log_test(f"doc.delete() on standard '{std_type}' blocked", passed_del):
            all_passed = False

        # 4.2 frappe.delete_doc with force=True
        passed_force_del = False
        try:
            frappe.delete_doc("Schedule Entry Type", std_type, force=True)
            print(f"CRITICAL VULNERABILITY: frappe.delete_doc(force=True) deleted standard type '{std_type}'!")
        except frappe.ValidationError:
            passed_force_del = True
        except Exception as e:
            passed_force_del = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        if not frappe.db.exists("Schedule Entry Type", std_type):
            passed_force_del = False

        if not log_test(f"frappe.delete_doc(force=True) on standard '{std_type}' blocked", passed_force_del):
            all_passed = False

        # 4.3 Rename then delete sequence
        passed_seq = False
        try:
            frappe.rename_doc("Schedule Entry Type", std_type, f"{std_type}_BypassTmp", force=True)
            # If rename didn't throw, try delete
            frappe.delete_doc("Schedule Entry Type", f"{std_type}_BypassTmp", force=True)
            print(f"CRITICAL VULNERABILITY: Standard type '{std_type}' renamed and deleted!")
        except frappe.ValidationError:
            passed_seq = True
        except Exception as e:
            passed_seq = isinstance(e, frappe.ValidationError)
        finally:
            frappe.db.rollback()

        if not frappe.db.exists("Schedule Entry Type", std_type):
            passed_seq = False

        if not log_test(f"Rename-then-delete bypass on '{std_type}' blocked", passed_seq):
            all_passed = False

    frappe.db.commit()

    # 4.4 In-use custom type deletion blocked
    custom_in_use = f"{TEST_PREFIX}InUse_Type"
    if frappe.db.exists("Schedule Entry Type", custom_in_use):
        frappe.delete_doc("Schedule Entry Type", custom_in_use, force=True)

    c_doc = frappe.get_doc({
        "doctype": "Schedule Entry Type",
        "type_name": custom_in_use,
        "requires_repair_order": 0,
    }).insert(ignore_permissions=True)

    se = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": custom_in_use,
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "12:00:00",
        "estimated_duration": 30,
    }).insert(ignore_permissions=True)
    frappe.db.commit()

    passed_in_use = False
    try:
        c_doc.delete()
        print(f"CRITICAL VULNERABILITY: In-use custom entry type was deleted while referenced by Schedule Entry!")
    except frappe.ValidationError as e:
        passed_in_use = "referenced by existing Schedule Entry records" in str(e)
    except Exception as e:
        passed_in_use = False
    finally:
        frappe.db.rollback()

    if not log_test("In-use custom Schedule Entry Type deletion blocked", passed_in_use):
        all_passed = False

    # Cleanup in-use test fixtures
    frappe.delete_doc("Schedule Entry", se.name, force=True, ignore_permissions=True)
    frappe.delete_doc("Schedule Entry Type", custom_in_use, force=True, ignore_permissions=True)
    frappe.db.commit()

    return all_passed


def attack_5_schedule_entry_test_data_isolation():
    """Attack 5: Create uncommitted/aborted Schedule Entry test records, run cleanup_all_test_data(), verify 0 orphaned rows."""
    print("\n=== Attack 5: Schedule Entry Test Data Isolation & Cleanup Harness ===")
    all_passed = True

    # 1. Setup various messy test fixtures simulating abandoned/aborted test runs
    cust_name = f"{TEST_PREFIX}Cust_Cleanup"
    if not frappe.db.exists("Customer", cust_name):
        cust = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": cust_name,
            "customer_type": "Individual",
        }).insert(ignore_permissions=True)
    else:
        cust = frappe.get_doc("Customer", cust_name)

    vin = "5YJSA1E26CF000001"
    if not frappe.db.exists("Repair Vehicle", vin):
        veh = frappe.get_doc({
            "doctype": "Repair Vehicle",
            "vin": vin,
            "make": "Tesla",
            "model": "Model S",
            "year": 2023,
            "license_plate": "TEST-INTAKE",
            "customer": cust.name,
        }).insert(ignore_permissions=True)
    else:
        veh = frappe.get_doc("Repair Vehicle", vin)

    ro = frappe.get_doc({
        "doctype": "Repair Order",
        "customer": cust.name,
        "vehicle": veh.name,
        "odometer_in": 10000,
    }).insert(ignore_permissions=True)

    bay_name = f"{TEST_PREFIX}Bay_Cleanup"
    if not frappe.db.exists("Service Bay", bay_name):
        bay = frappe.get_doc({
            "doctype": "Service Bay",
            "bay_name": bay_name,
            "is_active": 1,
        }).insert(ignore_permissions=True)

    custom_type_name = f"{TEST_PREFIX}Type_Cleanup"
    if not frappe.db.exists("Schedule Entry Type", custom_type_name):
        ctype = frappe.get_doc({
            "doctype": "Schedule Entry Type",
            "type_name": custom_type_name,
            "requires_repair_order": 0,
        }).insert(ignore_permissions=True)

    # Fixture 1: Standard entry on Repair Order
    se1 = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Repair",
        "repair_order": ro.name,
        "service_bay": bay_name,
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "08:00:00",
        "estimated_duration": 60,
    }).insert(ignore_permissions=True)

    # Fixture 2: Supplemental entry on same Repair Order
    se2 = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "repair_order": ro.name,
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "10:00:00",
        "estimated_duration": 90,
    }).insert(ignore_permissions=True)

    # Fixture 3: Standalone provisional entry with known test provisional names
    se3 = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "12:00:00",
        "estimated_duration": 45,
        "provisional_customer_name": "Alice Walker",
        "provisional_phone": "555-0199",
        "provisional_vehicle_info": "2024 Model Y Red",
    }).insert(ignore_permissions=True)

    # Fixture 4: Standalone provisional entry with Provisional Client
    se4 = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "13:00:00",
        "estimated_duration": 45,
        "provisional_customer_name": "Provisional Client",
    }).insert(ignore_permissions=True)

    # Fixture 5: Standalone entry with custom entry type and test bay
    se5 = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": custom_type_name,
        "service_bay": bay_name,
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "14:00:00",
        "estimated_duration": 30,
        "title": f"{TEST_PREFIX}Standalone_Meeting",
    }).insert(ignore_permissions=True)

    # Fixture 6: Unlinked test entry with Guest title
    se6 = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "15:00:00",
        "estimated_duration": 30,
    }).insert(ignore_permissions=True)

    # Fixture 7: Orphaned entry where Repair Order is deleted BEFORE cleanup is run
    ro_orphan = frappe.get_doc({
        "doctype": "Repair Order",
        "customer": cust.name,
        "vehicle": veh.name,
        "odometer_in": 10000,
    }).insert(ignore_permissions=True)

    se7_orphan = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Repair",
        "repair_order": ro_orphan.name,
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "16:00:00",
        "estimated_duration": 60,
    }).insert(ignore_permissions=True)

    # Explicitly delete ro_orphan via direct DB query to simulate dangling FK
    frappe.db.delete("Repair Order", {"name": ro_orphan.name})

    # Explicitly commit everything to simulate an aborted/crashed test that committed
    frappe.db.commit()

    created_ids = [se1.name, se2.name, se3.name, se4.name, se5.name, se6.name, se7_orphan.name]
    print(f"Created {len(created_ids)} test Schedule Entries across diverse states: {created_ids}")

    count_before = frappe.db.count("Schedule Entry")
    print(f"Total Schedule Entry rows in MariaDB before cleanup: {count_before}")
    assert count_before >= len(created_ids), f"Expected >= {len(created_ids)} rows before cleanup"

    # Run cleanup routine
    print("\nExecuting cleanup_all_test_data(verbose=False)...")
    cleanup_all_test_data(verbose=False)

    # Post-cleanup inspection
    total_remaining = frappe.db.count("Schedule Entry")
    raw_sql_remaining = frappe.db.sql("SELECT count(*) FROM `tabSchedule Entry`")[0][0]
    print(f"Total Schedule Entry rows in MariaDB after cleanup: count={total_remaining}, raw_sql={raw_sql_remaining}")

    passed_purge = (total_remaining == 0 and raw_sql_remaining == 0)
    if not log_test("All test and orphaned Schedule Entry records purged to exactly 0", passed_purge):
        all_passed = False
        remaining_names = frappe.get_all("Schedule Entry", pluck="name")
        print(f"Leaked Schedule Entries: {remaining_names}")

    # Check that custom test bay and custom test entry type were also cleaned
    bay_exists = frappe.db.exists("Service Bay", bay_name)
    type_exists = frappe.db.exists("Schedule Entry Type", custom_type_name)
    cust_exists = frappe.db.exists("Customer", cust_name)
    ro_exists = frappe.db.exists("Repair Order", ro.name)

    passed_cascades = (not bay_exists and not type_exists and not cust_exists and not ro_exists)
    if not log_test("Associated test entities (bays, custom types, cust, ro) purged", passed_cascades):
        all_passed = False
        print(f"Residual entities: bay={bay_exists}, type={type_exists}, cust={cust_exists}, ro={ro_exists}")

    # Verify standard types and standard shop bays were PRESERVED
    std_types_preserved = all(frappe.db.exists("Schedule Entry Type", t) for t in STANDARD_SCHEDULE_ENTRY_TYPES)
    bays_preserved = all(frappe.db.exists("Service Bay", b) for b in ["Test Bay 1", "Test Bay 2", "Test Bay 3"])

    passed_preservation = (std_types_preserved and bays_preserved)
    if not log_test("Standard Schedule Entry Types and Shop Bays preserved", passed_preservation):
        all_passed = False
        print(f"Preservation failure: std_types={std_types_preserved}, bays={bays_preserved}")

    # Test idempotency: run cleanup a second time
    try:
        cleanup_all_test_data(verbose=False)
        passed_idempotency = True
    except Exception as e:
        passed_idempotency = False
        print(f"Second cleanup run failed: {e}")

    if not log_test("cleanup_all_test_data() is idempotent", passed_idempotency):
        all_passed = False

    return all_passed


def main():
    frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
    frappe.connect()

    print("=================================================================")
    print("M1 Iteration 3 Adversarial Challenge Suite (Empirical Challenger)")
    print("=================================================================")

    results = {}
    results["Attack 1: Rename Standard Types (force=True)"] = attack_1_rename_standard_types_with_force()
    results["Attack 2: Rename Custom Type to Standard"] = attack_2_rename_custom_type_to_standard()
    results["Attack 3: Direct Mutation via doc.save()"] = attack_3_direct_mutation_via_doc_save()
    results["Attack 4: Deletion Bypass"] = attack_4_deletion_bypass()
    results["Attack 5: Schedule Entry Test Data Isolation"] = attack_5_schedule_entry_test_data_isolation()

    print("\n=================================================================")
    print("Summary of Adversarial Challenge Results:")
    print("=================================================================")
    overall_success = True
    for attack, res in results.items():
        status = "PASSED (Defense Held)" if res else "FAILED (Vulnerability / Leak Found)"
        print(f"- {attack}: {status}")
        if not res:
            overall_success = False

    print("=================================================================")
    verdict = "APPROVE" if overall_success else "CHALLENGE_FAILED"
    print(f"FINAL ADVERSARIAL VERDICT: {verdict}")
    print("=================================================================")

    if not overall_success:
        sys.exit(1)


if __name__ == "__main__":
    main()
