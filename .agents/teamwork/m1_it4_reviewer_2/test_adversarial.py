import frappe
from ism.tests.base import ISMTestCase, TEST_PREFIX
from ism.ism.doctype.schedule_entry_type.schedule_entry_type import STANDARD_SCHEDULE_ENTRY_TYPES

def test_mid_test_commit_teardown():
    print("--- Running Test 1: Mid-test commit teardown ---")
    test_case = ISMTestCase()
    test_case.setUp()

    cust = test_case.create_test_customer()
    veh = test_case.create_test_vehicle(customer=cust.name)
    ro = test_case.create_test_repair_order(customer=cust.name, vehicle=veh.name)
    
    bay = frappe.get_doc({
        "doctype": "Service Bay",
        "bay_name": f"{TEST_PREFIX}Adv_Bay_{frappe.generate_hash(length=6)}",
        "is_active": 1,
    }).insert(ignore_permissions=True)
    test_case.track(bay)

    se = frappe.get_doc({
        "doctype": "Schedule Entry",
        "entry_type": "Diagnostic",
        "repair_order": ro.name,
        "service_bay": bay.name,
        "scheduled_date": frappe.utils.today(),
        "scheduled_time": "10:00:00",
        "estimated_duration": 60,
        "status": "Scheduled",
    }).insert(ignore_permissions=True)
    test_case.track(se)

    # Simulate explicit mid-test commit
    frappe.db.commit()
    print("Committed 5 records mid-test successfully.")

    # Verify they exist in DB
    assert frappe.db.exists("Customer", cust.name)
    assert frappe.db.exists("Repair Vehicle", veh.name)
    assert frappe.db.exists("Repair Order", ro.name)
    assert frappe.db.exists("Service Bay", bay.name)
    assert frappe.db.exists("Schedule Entry", se.name)

    # Now execute teardown
    test_case.tearDown()
    print("tearDown executed successfully.")

    # Verify they are all cleanly purged from DB
    assert not frappe.db.exists("Schedule Entry", se.name), f"Residual Schedule Entry: {se.name}"
    assert not frappe.db.exists("Service Bay", bay.name), f"Residual Service Bay: {bay.name}"
    assert not frappe.db.exists("Repair Order", ro.name), f"Residual Repair Order: {ro.name}"
    assert not frappe.db.exists("Repair Vehicle", veh.name), f"Residual Vehicle: {veh.name}"
    assert not frappe.db.exists("Customer", cust.name), f"Residual Customer: {cust.name}"
    print("Test 1 PASSED: All 5 committed records cleanly purged by Phase 2 teardown!")

def test_standard_type_guards():
    print("--- Running Test 2: Standard type rename & delete guards ---")
    for std in STANDARD_SCHEDULE_ENTRY_TYPES:
        doc = frappe.get_doc("Schedule Entry Type", std)
        
        # Test 2a: Deletion blocked
        try:
            doc.delete()
            raise AssertionError(f"Expected ValidationError deleting standard type {std}")
        except frappe.ValidationError:
            pass

        # Test 2b: Rename blocked with force=True
        try:
            frappe.rename_doc("Schedule Entry Type", std, f"{std}_Hacked", force=True)
            raise AssertionError(f"Expected ValidationError renaming standard type {std}")
        except frappe.ValidationError:
            pass

        # Test 2c: Direct type_name mutation blocked
        doc.type_name = f"{std}_Mutated"
        try:
            doc.save()
            raise AssertionError(f"Expected ValidationError mutating type_name on {std}")
        except frappe.ValidationError:
            pass
        frappe.db.rollback()

    # Test 2d: Rename custom type to standard blocked
    custom = frappe.get_doc({
        "doctype": "Schedule Entry Type",
        "type_name": f"{TEST_PREFIX}CustType_{frappe.generate_hash(length=4)}",
        "requires_repair_order": 0,
    }).insert()
    try:
        frappe.rename_doc("Schedule Entry Type", custom.name, "Diagnostic", force=True)
        raise AssertionError("Expected ValidationError renaming custom type to 'Diagnostic'")
    except frappe.ValidationError:
        pass
    finally:
        custom.delete()

    print("Test 2 PASSED: Standard type rename & delete defenses are 100% impenetrable!")

def test_ism_manager_rbac():
    print("--- Running Test 3: ISM Manager RBAC permissions ---")
    user_email = f"test_mgr_rbac_{frappe.generate_hash(length=6)}@example.com"
    if frappe.db.exists("User", user_email):
        frappe.delete_doc("User", user_email, force=True)

    user = frappe.get_doc({
        "doctype": "User",
        "email": user_email,
        "first_name": "Test ISM Manager RBAC",
        "roles": [{"role": "ISM Manager"}],
    }).insert(ignore_permissions=True)

    try:
        doctypes_crud = [
            "Service Bay",
            "Equipment Tag",
            "Schedule Entry Type",
            "Schedule Entry",
        ]
        for dt in doctypes_crud:
            for ptype in ["read", "write", "create", "delete"]:
                has_p = frappe.has_permission(dt, ptype, user=user_email)
                assert has_p, f"ISM Manager missing '{ptype}' permission on {dt}"
            print(f"  Verified full CRUD for ISM Manager on {dt}")

        # ISM Settings is a Single DocType: read, write
        for ptype in ["read", "write"]:
            has_p = frappe.has_permission("ISM Settings", ptype, user=user_email)
            assert has_p, f"ISM Manager missing '{ptype}' permission on ISM Settings"
        print("  Verified read/write for ISM Manager on ISM Settings")

    finally:
        frappe.delete_doc("User", user_email, force=True)

    print("Test 3 PASSED: ISM Manager RBAC is fully intact across all Milestone 1 DocTypes!")

def main():
    frappe.init(site="development.localhost")
    frappe.connect()
    try:
        test_mid_test_commit_teardown()
        test_standard_type_guards()
        test_ism_manager_rbac()
        print("\nALL ADVERSARIAL STRESS TESTS COMPLETED SUCCESSFULLY!")
    finally:
        frappe.destroy()

if __name__ == "__main__":
    main()
