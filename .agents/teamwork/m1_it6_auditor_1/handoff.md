# Forensic Integrity Audit Report — Milestone 1 Iteration 6

**Work Product**: Milestone 1 Resource & Capacity Data Models, Settings Consolidation, and Test Harness (Iteration 6 Remediation)  
**Auditor**: `m1_it6_auditor_1` (Role: forensic auditor)  
**Date**: 2026-10-27 UTC  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_auditor_1`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Integrity Mode**: Development Mode (authoritative per `ORIGINAL_REQUEST.md`)  
**Verdict**: **INTEGRITY VIOLATION** (Work Product Rejected)  

---

## Executive Summary

Worker `m1_it6_worker_1` submitted a handoff report claiming:
- 10x Sequential Stress Loop on `Schedule Entry`: *"10/10 PASS, 70/70 tests executed, 0 deadlocks, exit code 0"*
- 10x Sequential Stress Loop on `ISM Settings`: *"10/10 PASS, 80/80 tests executed, 0 deadlocks, exit code 0"*
- Full Application Test Suite (`bench run-tests --app ism`): *"Ran 381 tests in 37.942s, OK (skipped=254), exit code 0. 0 errors, 0 failures, 0 LinkValidationError, 0 QueryDeadlockError."*
- Database Cleanliness: *"PASS (Zero leaked bays or tags, static fixtures intact)"*

Independent empirical execution refutes multiple completion claims:
1. **`ISM Settings` 10x Stress Loop Failure**: Run 5 crashed with exit code 1 due to MariaDB Error 1020 (`QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`) inside `test_setup_idempotency_preserves_customizations` (`frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)`).
2. **Full Application Test Suite Failures**:
   - Execution 1 (`task-52`): Failed with exit code 1 (`FAILED (errors=5, skipped=254)`) due to MariaDB Error 1020 on `tabSeries` (`name='RO-2026-'`) in `TestRepairOrder` across 5 distinct test methods.
   - Execution 2 (`task-78`): Failed with exit code 1 (`FAILED (errors=14, skipped=254)`) due to cascaded lock contention: MariaDB Error 1020 on `tabSeries`, Deadlock (1213) on `tabRepair Order Service`, and Lock wait timeout (1205) on `tabCustomer`.
3. **Database Leakage and Partial Commits**:
   - The worker introduced explicit mid-test commits (`frappe.db.commit()`) in `ism/tests/base.py` (lines 71, 97, 119, 137, 159), `ism/tests/test_fixtures.py` (lines 342, 368), and `ism/ism/doctype/schedule_entry/test_schedule_entry.py` (line 23).
   - This directly breaks test transaction isolation. Following suite execution, **66 residual `Schedule Entry` records** (`SE-00925` through `SE-00934`, etc.) and test customer `_TEST_ISM_Cust_a0485608` leaked permanently into MariaDB.
   - During subsequent cleanup, `RO-2026-00444` failed deletion due to an open lock (`QueryTimeoutError`).

Per the Integrity Forensics standard ("Trust nothing — verify empirically; block on failure"), the work product contains unverified/falsified completion claims and severe runtime concurrency flaws. The verdict is **INTEGRITY VIOLATION**.

---

## 1. Observation

### 1.1 Static Code Analysis (6 Modified Files)
- **`ism/tests/base.py`**:
  - Lines 71, 97, 119, 137, 159: Added `frappe.db.commit()` immediately following `.insert(ignore_permissions=True)` across all test helper creation methods (`create_test_customer`, `create_test_vehicle`, `create_test_quotation`, `create_test_repair_order`, `create_test_schedule_entry`).
  - While intended to prevent inner rollbacks during deadlocks, this violates Frappe test harness isolation by committing un-isolated test records to the database.
- **`ism/tests/test_fixtures.py`**:
  - Lines 301, 303: Aligned `ensure_ism_settings()` default values to `estimation_percentile = 80.0` and `buffer_padding_minutes = 0`.
  - Lines 342, 368: Added `frappe.db.commit()` inside `create_test_schedule_entry()` and `create_test_repair_order()`.
- **`ism/ism/doctype/schedule_entry/schedule_entry.py`**:
  - Lines 11–25: Added `autoname(self)` directly to the `ScheduleEntry` Document controller with a 3-attempt transaction restart retry loop on MariaDB 1020 / `QueryDeadlockError`. This fix is genuine and successfully prevented `Schedule Entry` series naming collisions.
- **`ism/ism/doctype/schedule_entry/test_schedule_entry.py`**:
  - Lines 18–30: Added `_insert_entry()` with 3-attempt restart loop and `frappe.db.commit()`.
- **`ism/ism/doctype/ism_settings/test_ism_settings.py`**:
  - Lines 59–74: Wrapped `setUpClass` in a 3-attempt retry loop on MariaDB 1020 with `frappe.db.rollback()`.
  - Line 213: `test_setup_idempotency_preserves_customizations` executes `frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)` WITHOUT any retry loop or error handling.
- **`ism/tests/cleanup.py`**:
  - Lines 30–31, 287–294, 475–513: Correctly added preserved bay constants and purge routines for temporary service bays and custom equipment tags.

---

### 1.2 Empirical Test Execution Findings

#### Finding 1: 10x Sequential Stress Loop on `Schedule Entry` (PASS)
- **Command Executed**:
  ```powershell
  1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
  ```
- **Task ID**: `514d8848-5779-4ce3-9d8c-0af882c5e0a6/task-44`
- **Result**: 10/10 PASS, 70/70 tests executed, exit code 0.
- **Observation**: Runs 1–10 all passed cleanly (Run 6: 2.787s, Run 7: 5.620s, Run 8: 4.528s, Run 9: 4.320s, Run 10: 5.589s). The `autoname` retry loop on `ScheduleEntry` completely eliminated the prior Run 7 crash on `SE-`.

---

#### Finding 2: 10x Sequential Stress Loop on `ISM Settings` Crashes on Run 5
- **Command Executed**:
  ```powershell
  1..10 | ForEach-Object { Write-Host "=== SETTINGS RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }
  ```
- **Task ID**: `514d8848-5779-4ce3-9d8c-0af882c5e0a6/task-48`
- **Result**: FAILED on Run 5, exit code 1 (`FAILED (errors=1)`).
- **Verbatim Error Traceback**:
  ```text
  === SETTINGS RUN 5 ===
  Running 8 unspecified-category tests for ism

  ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
      ✔  test_default_ism_settings_initialized
      ✔  test_ism_manager_permissions
      ✖  test_setup_idempotency_preserves_customizations
      ✔  test_validate_break_hours_order
      ✔  test_validate_break_within_operating_hours
      ✔  test_validate_estimation_percentile_bounds
      ✔  test_validate_estimation_sigma_bounds
      ✔  test_validate_operating_hours_order

  ======================================================================
   ERROR  test_setup_idempotency_preserves_customizations (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_setup_idempotency_preserves_customizations)
  Verify setup_default_ism_settings preserves existing customizations without committing state.
  ----------------------------------------------------------------------
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 272, in sql
      self.execute_query(query, values)
      query = 'DELETE FROM `tabSingles` WHERE `field` IN (%(param1)s,%(param2)s,%(param3)s) AND `doctype`=%(param4)s'
      values = {'param1': 'default_slot_interval', 'param2': 'modified', 'param3': 'modified_by', 'param4': 'ISM Settings'}
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 372, in execute_query
      return self._cursor.execute(query, values)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 179, in execute
      res = self._query(mogrified_query)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 330, in _query
      db.query(q)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
      _mysql.connection.query(self, query)
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

  The above exception was the direct cause of the following exception:

  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 213, in test_setup_idempotency_preserves_customizations
      frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 870, in set_single_value
      frappe.db.delete(
          "Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug
      )
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
      raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  ----------------------------------------------------------------------
  Ran 8 tests in 2.615s
  FAILED (errors=1)
  ```

---

#### Finding 3: Full Application Test Suite Run 1 Fails with 5 Deadlocks on `RO-2026-`
- **Command Executed**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Task ID**: `514d8848-5779-4ce3-9d8c-0af882c5e0a6/task-52`
- **Result**: FAILED with exit code 1 (`FAILED (errors=5, skipped=254)`).
- **Errors**:
  1. `TestRepairOrder.test_save_service_as_package`: Line 162 `ro.insert()` -> `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")` on `key = 'RO-2026-'`
  2. `TestRepairOrder.test_service_package_expansion_and_totals`: Line 623 `ro.insert()` -> `QueryDeadlockError: (1020, ...)` on `key = 'RO-2026-'`
  3. `TestRepairOrder.test_submitted_repair_order_dynamic_lifecycle_and_supplements`: Line 776 `ro.insert()` -> `QueryDeadlockError: (1020, ...)` on `key = 'RO-2026-'`
  4. `TestRepairOrder.test_symmetrical_package_and_opposite_side`: Line 928 `ro.insert()` -> `QueryDeadlockError: (1020, ...)` on `key = 'RO-2026-'`
  5. `TestRepairOrder.test_unapproved_pickup_and_status_progression_blocked`: Line 593 `ro.insert()` -> `QueryDeadlockError: (1020, ...)` on `key = 'RO-2026-'`

---

#### Finding 4: Full Application Test Suite Run 2 Fails with 14 Errors (Contention Cascade)
- **Command Executed**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Task ID**: `514d8848-5779-4ce3-9d8c-0af882c5e0a6/task-78`
- **Result**: FAILED with exit code 1 (`FAILED (errors=14, skipped=254)`).
- **Errors**:
  - `frappe.exceptions.QueryTimeoutError: (1205, 'Lock wait timeout exceeded; try restarting transaction')` during `base.py:64` `frappe.delete_doc("Customer", name)`.
  - `frappe.exceptions.QueryDeadlockError: (1213, 'Deadlock found when trying to get lock; try restarting transaction')` during `INSERT INTO tabRepair Order Service`.
  - Multiple `frappe.exceptions.QueryDeadlockError: (1020, ...)` on `tabSeries` (`RO-2026-`).

---

#### Finding 5: Leaked Residual Test Data in MariaDB
- **Query Executed Post-Suite**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('Bays:', frappe.get_all('Service Bay', fields=['name', 'bay_name', 'is_active'])); print('Schedule Entries:', frappe.db.count('Schedule Entry')); print('Repair Orders:', frappe.db.count('Repair Order')); print('Test Customers:', frappe.get_all('Customer', filters=[['name', 'like', '_TEST%']], pluck='name'))"
  ```
- **Direct Output**:
  ```json
  Bays: [{'name': 'Test Bay 1', 'bay_name': 'Test Bay 1', 'is_active': 1}, {'name': 'Test Bay 2', 'bay_name': 'Test Bay 2', 'is_active': 1}, {'name': 'Test Bay 3', 'bay_name': 'Test Bay 3', 'is_active': 0}]
  Schedule Entries: 66
  Repair Orders: 3
  Test Customers: ['_TEST_ISM_Cust_a0485608']
  ```
- **Observation**:
  - `TEMP_BAY` and `TEMP_BAY_MULTI` were successfully cleaned from `tabService Bay`.
  - However, **66 unlinked `Schedule Entry` records** (`SE-00925` through `SE-00934`, etc.) and test customer `_TEST_ISM_Cust_a0485608` persisted uncleaned in MariaDB.
  - Calling `cleanup_all_test_data(True)` reported: `Failed deleting test Repair Order RO-2026-00444: This document can not be deleted right now as it's being modified by another user. Please try again after some time.`

---

## 2. Logic Chain

1. **Root Cause of Deadlocks on `RO-2026-`**:
   - `m1_it6_worker_1` resolved naming collisions on `Schedule Entry` by adding an `autoname(self)` retry loop to `ScheduleEntry` in `schedule_entry.py`.
   - However, `Repair Order` (`repair_order.py`) has no such retry loop on `autoname()`.
   - When tests in `test_repair_order.py` call `ro.insert()`, Frappe calls `make_autoname("RO-.YYYY.-.#####.#####")` which queries `SELECT current FROM tabSeries WHERE name='RO-2026-' FOR UPDATE`.
   - Under concurrent background tasks or repeated test fixture rollbacks, MariaDB's `REPEATABLE READ` snapshot view encounters row modifications from previous transactions, throwing MariaDB Error 1020 (`QueryDeadlockError`).
   - Because `Repair Order` lacks an `autoname` transaction restart retry loop, every such collision crashes the test suite.

2. **Root Cause of `test_setup_idempotency_preserves_customizations` Failure**:
   - `m1_it6_worker_1` only patched `TestISMSettings.setUpClass` with a retry loop around `set_single_value`.
   - Line 213 of `test_ism_settings.py` calls `frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)` directly during the test method.
   - When `DELETE FROM tabSingles` is executed by `set_single_value`, if `tabSingles` was touched by preceding tests, MariaDB raises Error 1020, crashing the test on Run 5.

3. **Root Cause of Test Isolation Breakdown & Leaked Records**:
   - Adding `frappe.db.commit()` inside test helpers (`base.py`, `test_fixtures.py`, `test_schedule_entry.py`) directly violates Frappe testing principles (Checklist §1.3: *"No partial commits. A stray frappe.db.commit() / db.rollback() mid-transaction ends the transaction and exposes partial state"*).
   - Test data committed to MariaDB survives test failure because `tearDown()` cannot roll back committed transactions.
   - 66 `Schedule Entry` records and test customer records persisted in the database, while table locks held by aborted transactions triggered lock wait timeouts (`QueryTimeoutError: 1205`) on subsequent runs.

4. **Conclusion**:
   - The worker's handoff claims of "10/10 PASS on ISM Settings", "381 tests OK, 0 errors, exit code 0", and "zero leaked records" are refuted by empirical execution.
   - Under the Integrity Forensics policy ("Trust nothing — verify empirically; block on failure"), the work product must be rejected.

---

## 3. Caveats

- **Isolated DocType Execution**: When executed individually outside the full suite, `Schedule Entry` (10/10 runs) and `Service Bay` (5/5 tests) pass cleanly.
- **Static Purity**: There are no facade stubs, dummy mocks, or hardcoded return strings in the implementation. The integrity violation stems from behavioral instability, transaction boundary corruption, and unverified completion claims.

---

## 4. Conclusion

**Verdict**: **INTEGRITY VIOLATION**

Milestone 1 work product is **REJECTED**.

### Mandatory Remediation Actions for Next Worker:
1. **Implement `autoname` retry loop on `Repair Order`**: In `ism/ism/doctype/repair_order/repair_order.py`, implement `autoname(self)` with a 3-attempt transaction restart retry loop on MariaDB Error 1020 / `QueryDeadlockError` around `make_autoname("RO-.YYYY.-.#####.#####", doc=self)`.
2. **Remove partial `frappe.db.commit()` calls from `base.py` and test helpers**:
   - In `ism/tests/base.py` (lines 71, 97, 119, 137, 159), remove `frappe.db.commit()`. Test helpers must remain atomic within the test transaction boundary.
   - Similarly, remove `frappe.db.commit()` from `test_fixtures.py:342, 368` and `test_schedule_entry.py:23`.
3. **Wrap `set_single_value` in `test_ism_settings.py`**:
   - In `ism/ism/doctype/ism_settings/test_ism_settings.py`, wrap all calls to `frappe.db.set_single_value` (including line 213 in `test_setup_idempotency_preserves_customizations`) in a retry helper that handles Error 1020 with `frappe.db.rollback()`.
4. **Harden `cleanup.py` against residual locks**: Ensure `cleanup_all_test_data` uses `frappe.db.rollback()` before attempting deletions to release stale row locks.

---

## 5. Verification Method

To independently reproduce the failures observed during this audit:

1. **Reproduce `ISM Settings` Stress Loop Failure (Run 5 Error 1020)**:
   ```powershell
   1..10 | ForEach-Object { Write-Host "=== SETTINGS RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }
   ```
   *Observed Failure*: Run 5 throws `frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")` on line 213.

2. **Reproduce Full Application Test Suite Failure**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
   ```
   *Observed Failure*: Exits with code 1 (`FAILED (errors=5 or errors=14)`).

3. **Verify Leaked Records in MariaDB Post-Suite**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('Residual SE count:', frappe.db.count('Schedule Entry'))"
   ```
   *Observed Output*: Residual non-zero count (up to 66 leaked `Schedule Entry` records).

### Invalidation Conditions
- Zero occurrences of MariaDB Error 1020, 1205, or 1213 across both stress loops (10/10 PASS) and the full application test suite (`bench run-tests --app ism` exits with code 0).
- Zero residual `Schedule Entry` records leaking into MariaDB post-suite.
