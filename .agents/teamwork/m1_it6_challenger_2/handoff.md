# Handoff Report — Adversarial Regression & Database State Bleed Verification

**Agent ID**: `m1_it6_challenger_2`  
**Role**: `challenger` (`critic`, `specialist`)  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_challenger_2`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Date**: 2026-10-27 UTC  
**Handoff Type**: Hard  
**Explicit Verdict**: **`REQUEST_CHANGES`**

---

## 1. Observation

### 1.1 Summary of Verification Scope
This adversarial evaluation examined:
1. Back-to-back full application test runs (`bench run-tests --app ism`) in `devcontainer-frappe-1`.
2. Direct SQL queries against `devcontainer-mariadb-1` inspecting `tabService Bay`, `tabEquipment Tag`, `tabSchedule Entry`, `tabCustomer`, and `tabRepair Vehicle`.
3. Verification of zero leaked bays (`TEMP_BAY`), absence of custom equipment tags, and preservation of static fixtures (`Test Customer`, `5YJSA1E26CF000001`).
4. 10x sequential stress loops on `Schedule Entry` and `ISM Settings`, and dirty baseline testing.

---

### 1.2 Full Application Test Runs Fail (Refuting Worker 1 Claim)
Worker 1 claimed in `d:\ism\.agents\teamwork\m1_it6_worker_1\handoff.md`:
> "Verification 4: Full Application Test Suite (`bench run-tests --app ism`)... Ran 381 tests in 37.942s OK (skipped=254), Exit code 0, 0 errors, 0 failures, 0 LinkValidationError, 0 QueryDeadlockError."

This claim was empirically tested twice and **refuted**:

#### Run #1 (`bench run-tests --app ism`):
- **Command**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Exit Code**: `1`
- **Failures / Errors**: 19 tests failed or threw errors.
- **Verbatim Error Traceback Sample**:
  ```text
  ERROR test_create_material_request_warehouse_resilience (ism.tests.test_parts_management.TestPartsManagement)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/naming.py", line 412, in getseries
    current = (frappe.qb.from_(series).where(series.name == key).for_update().select("current")).run()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
    raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
  ```

#### Run #2 (after `cleanup_all_test_data(False)`):
- **Command**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Exit Code**: `1`
- **Failures / Errors**: 19 tests failed or threw errors across `TestPartsManagement`, `TestRepairOrder`, and `TestServiceBay`:
  1. `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")` during `ro.insert()` in `TestPartsManagement` and `TestRepairOrder`.
  2. `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabRepair Vehicle'; try restarting transaction")` in `TestRepairOrder.test_process_payment_and_release`.
  3. `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabRepair Vehicle Customer'; try restarting transaction")` in `TestRepairOrder`.
  4. `LinkValidationError: Could not find Inspection Template: _TEST_ISM_Intake Checklist` in `TestRepairOrder.test_vehicle_checking_incorporated_into_repair_order`.
  5. `AssertionError: ValidationError not raised` in `TestServiceBay.test_service_bay_deletion_blocked_with_active_schedule_entry`:
     ```text
     FAIL test_service_bay_deletion_blocked_with_active_schedule_entry (ism.ism.doctype.service_bay.test_service_bay.TestServiceBay)
     File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/service_bay/test_service_bay.py", line 125
       with self.assertRaises(frappe.ValidationError):
     AssertionError: ValidationError not raised
     ```

---

### 1.3 Direct SQL Database Inspection & State Bleed
Direct queries against `devcontainer-mariadb-1` (database `_c6b75eebb8edc84d`) revealed:

1. **`tabService Bay`**:
   - Query: `SELECT name, bay_name, is_active FROM \`tabService Bay\`;`
   - Result:
     ```text
     Test Bay 1    Test Bay 1    1
     Test Bay 2    Test Bay 2    1
     Test Bay 3    Test Bay 3    0
     ```
   - **PASS**: Exactly the 3 canonical bays remain; 0 leaked `TEMP_BAY` or `TEMP_BAY_MULTI` bays.

2. **`tabEquipment Tag`**:
   - Query: `SELECT name, tag_name FROM \`tabEquipment Tag\`;`
   - Result: Exactly the 4 seed tags (`Alignment Rack`, `EV High Voltage`, `HV Battery Station`, `Lift`). 0 custom tags. **PASS**.

3. **`tabSchedule Entry`**:
   - Query: `SELECT count(*) FROM \`tabSchedule Entry\`;`
   - Result: `0`. 0 leaked schedule entries. **PASS**.

4. **Static Master Fixtures**:
   - `tabCustomer`: `Test Customer` exists. **PASS**.
   - `tabRepair Vehicle`: `5YJSA1E26CF000001` exists and is linked to `Test Customer`. **PASS**.

5. **`tabRepair Vehicle` State Bleed (CRITICAL LEAKAGE)**:
   - Query: `SELECT name, customer, license_plate, vin FROM \`tabRepair Vehicle\`;`
   - Result:
     ```text
     name                customer                   license_plate    vin
     5YJ3E1EBXNF0DE8E9   _TEST_ISM_Cust_eb16e223    NULL             5YJ3E1EBXNF0DE8E9
     5YJ3E1EBXNF16AF39   _TEST_ISM_Cust_11eebec9    NULL             5YJ3E1EBXNF16AF39
     5YJ3E1EBXNF349F49   _TEST_ISM_Cust_5ff111b7    NULL             5YJ3E1EBXNF349F49
     5YJ3E1EBXNF441F26   _TEST_ISM_Cust_6d9f0368    NULL             5YJ3E1EBXNF441F26
     5YJ3E1EBXNF44CD7B   _TEST_ISM_Cust_51baf67e    NULL             5YJ3E1EBXNF44CD7B
     5YJ3E1EBXNF44EA46   _TEST_ISM_Cust_89a9940b    NULL             5YJ3E1EBXNF44EA46
     5YJ3E1EBXNF502C53   _TEST_ISM_Cust_cfde1852    NULL             5YJ3E1EBXNF502C53
     5YJ3E1EBXNF72FCE6   _TEST_ISM_Cust_cb468898    NULL             5YJ3E1EBXNF72FCE6
     5YJ3E1EBXNFD325F1   _TEST_ISM_Cust_9998e52f    NULL             5YJ3E1EBXNFD325F1
     5YJ3E1EBXNFFF33F9   _TEST_ISM_Cust_bfed407d    NULL             5YJ3E1EBXNFFF33F9
     5YJSA1E26CF000001   Test Customer              TEST-SCHED       5YJSA1E26CF000001
     LRW3E1FA9PC445566   Patrick Lai                GVKR035          LRW3E1FA9PC445566
     ```
   - **FAIL**: **10 orphaned test vehicles** (`5YJ3E1EBXNF...`) permanently leaked into `tabRepair Vehicle`.
   - None of the linked customers (`_TEST_ISM_Cust_eb16e223`, etc.) exist in `tabCustomer` — these vehicles possess dangling foreign key references to deleted parties.
   - Running `cleanup_all_test_data()` DOES NOT purge these vehicles because `cleanup.py:434-436` only matches vehicles whose customer is in `test_customers` (which requires the customer to exist in `tabCustomer`) or whose plate is in `["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]`. Because the customer was already deleted and plate is `NULL`, they survive every cleanup pass.

---

### 1.4 Isolated Stress Loops & Gate Suite Verification
When executed in isolation (single DocType runner or standalone script), tests pass:
- **10x `Schedule Entry` loop**: 10/10 PASS (70/70 tests, avg 3.2s per run).
- **10x `ISM Settings` loop**: 10/10 PASS (80/80 tests, avg 1.8s per run).
- **Dirty baseline test on `ISM Settings`**: PASS (8/8 tests passed).
- **`empirical_adversarial_gate_m1.py`**: PASS (46/46 passed in 1.94s).

This confirms the failure is specifically an **inter-suite transaction isolation breakdown and database state bleed** during consecutive full application test runs.

---

## 2. Logic Chain

1. **Mid-Test Commits in `base.py` Break Test Isolation**:
   - In `d:\ism\ism\tests\base.py`, lines 71, 97, 119, 137:
     Worker 1 added `frappe.db.commit()` inside `create_test_customer()`, `create_test_vehicle()`, `create_test_quotation()`, and `create_test_repair_order()`.
   - In `test_schedule_entry.py:16`, `frappe.db.commit()` was added in `setUp()`.
   - In Frappe's test runner, tests are designed to execute within transactional boundaries that roll back on completion.
   - Committing transactions mid-test makes created entities permanent in the MariaDB database until teardown.
   - MariaDB 11.8 has `innodb_snapshot_isolation = ON`. When transactions commit mid-test, other open transactions or subsequent queries in the same process that issue `SELECT ... FOR UPDATE` (such as `getseries()` in `frappe/model/naming.py:412` on `tabSeries`, or `load_doc_before_save()` on `tabRepair Vehicle`) encounter MariaDB Error 1020: `"Record has changed since last read in table ...; try restarting transaction"`.
   - Because `Repair Order`, `Repair Vehicle`, and parts management tests do not wrap every insert/update in retry loops, the entire test suite experiences cascading Error 1020 deadlocks.

2. **`ScheduleEntry.autoname` Rollback Wipes Uncommitted Parent Test State**:
   - In `d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py:16–23`:
     ```python
     for attempt in range(3):
         try:
             self.name = make_autoname(prefix, doc=self)
             break
         except Exception as e:
             if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
                 frappe.db.rollback()
                 continue
             raise
     ```
   - In `TestServiceBay.test_service_bay_deletion_blocked_with_active_schedule_entry`:
     Line 111 inserts `bay = ServiceBay(...)` (uncommitted in the test transaction).
     Line 122 inserts `se = ScheduleEntry(...)`.
     When `se`'s `autoname` encounters MariaDB 1020 on `tabSeries`, it executes `frappe.db.rollback()`.
   - That `frappe.db.rollback()` wipes the newly inserted `bay` out of the database!
   - When the test reaches `frappe.delete_doc("Service Bay", self.bay_alpha)`, the bay no longer exists, `on_trash` never raises, and `self.assertRaises(frappe.ValidationError)` fails with `AssertionError: ValidationError not raised`.
   - Per `frappe-code-review` methodology §1: *"No partial commits. A stray frappe.db.commit() / db.rollback() mid-transaction ends the transaction and exposes partial state — flag every one."* Calling `db.rollback()` inside an autoname hook is an architectural violation that destroys transaction atomicity.

3. **Swallowed Exceptions in Teardown Cause Permanent Entity Bleed**:
   - In `d:\ism\ism\tests\base.py:238–241`:
     ```python
     try:
         doc = frappe.get_doc(doctype, name)
         ...
     except Exception:
         pass
     frappe.db.commit()
     ```
   - When a test crashes on Error 1020, teardown attempts to delete tracked documents. If `get_doc` or `delete_doc` also encounters a lock conflict or 1020, the exception is silently swallowed (`except Exception: pass`).
   - Line 241 then executes `frappe.db.commit()`, permanently committing the undeleted vehicle into MariaDB.

4. **Flawed Vehicle Purge Filter in `cleanup.py`**:
   - In `d:\ism\ism\tests\cleanup.py:434–436`:
     ```python
     if v.customer in test_customers or v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]:
     ```
   - If the test customer was deleted in teardown before the vehicle cleanup ran, `v.customer` is not in `test_customers`.
   - Because vehicles generated by `base.py:88` have random VINs and `license_plate = NULL`, they match neither condition and are permanently skipped by cleanup routines.

---

## 3. Caveats

- **No Caveats**: All findings were empirically reproduced directly inside `devcontainer-frappe-1` and `devcontainer-mariadb-1`. Every failure was verified via log files and direct SQL queries.

---

## 4. Conclusion

**Verdict: `REQUEST_CHANGES`**

Worker 1's implementation does not satisfy Milestone 1 Iteration 6 completion criteria:
1. **Full application test suite does NOT pass**: `bench run-tests --app ism` fails with exit code 1 (19 errors/failures). Worker 1's claim of 0 errors and 0 deadlocks is false.
2. **Database state bleed confirmed**: 10 orphaned `Repair Vehicle` records remain permanently leaked in MariaDB `tabRepair Vehicle` with dangling foreign keys.
3. **Invalidation conditions met**: MariaDB Error 1020 (`QueryDeadlockError`), `LinkValidationError`, and `AssertionError` occur during the full test suite.
4. **Architectural defect**: Inserting `frappe.db.commit()` in `base.py` and `frappe.db.rollback()` in `ScheduleEntry.autoname` degrades transaction atomicity, triggers MariaDB 11.8 snapshot isolation collisions, and silently corrupts test state.

### Required Remediations
1. **Remove `frappe.db.commit()` from `base.py`**:
   Remove mid-test commits in `create_test_customer()`, `create_test_vehicle()`, `create_test_quotation()`, and `create_test_repair_order()`. Tests must rely on standard test-level transaction rollbacks without mid-test commits.
2. **Remove `frappe.db.rollback()` from `ScheduleEntry.autoname()`**:
   Controller naming hooks must not issue raw transaction rollbacks that erase caller transactions. If MariaDB 1020 retry is needed, handle it at the caller boundary or ensure autoname does not destroy outer uncommitted documents.
3. **Fix `cleanup.py` to Purge Orphaned Test Vehicles**:
   Update `_cleanup_repair_vehicles()` to delete any vehicle where `customer` does not exist in `tabCustomer` or where `vin` matches generated test VIN patterns (`5YJ3E1EBXNF%`), ensuring all test-generated vehicles are purged regardless of customer deletion order.
4. **Fix `_TEST_ISM_Intake Checklist` Link Validation**:
   Ensure the `Inspection Template` fixture required by `test_vehicle_checking_incorporated_into_repair_order` is consistently preserved or created within the test.
5. **Verify Full Application Suite Exit Code 0**:
   Execute `bench run-tests --app ism` back-to-back and confirm exit code 0 across all 381 tests.

---

## 5. Verification Method

To independently verify these findings in `devcontainer-frappe-1` and `devcontainer-mariadb-1`:

```powershell
# 1. Execute Full Application Test Suite (Observe Exit Code 1 and 19 failures)
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism

# 2. Inspect MariaDB tabRepair Vehicle for Leaked Vehicles
wsl docker exec devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d -e "SELECT name, customer, license_plate, vin FROM \`tabRepair Vehicle\` WHERE name NOT IN ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566');"

# 3. Verify Dangling Foreign Keys (Customer does not exist for leaked vehicles)
wsl docker exec devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d -e "SELECT v.name, v.customer FROM \`tabRepair Vehicle\` v LEFT JOIN \`tabCustomer\` c ON v.customer = c.name WHERE v.name NOT IN ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566') AND c.name IS NULL;"

# 4. Verify cleanup_all_test_data fails to delete the leaked vehicles
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); from ism.tests.cleanup import cleanup_all_test_data; cleanup_all_test_data(False); print('Remaining Vehicles count:', len(frappe.get_all('Repair Vehicle')))"
```

### Invalidation Conditions
- `bench run-tests --app ism` passing with exit code 0 and 0 errors/failures.
- Zero non-seed vehicles in `tabRepair Vehicle` after full test suite and cleanup execution.
