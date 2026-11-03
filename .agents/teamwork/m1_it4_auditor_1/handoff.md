# Forensic Audit Report: Milestone 1 Iteration 4 Implementation & Remediation

**Auditor**: `m1_it4_auditor_1` (M1 Iteration 4 Forensic Auditor)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it4_auditor_1`  
**Profile**: General Project (Frappe Application)  
**Integrity Mode**: Development Mode (authoritative per `ORIGINAL_REQUEST.md`)  
**Verdict**: **INTEGRITY VIOLATION** (Work Product Rejected)  

---

## Executive Summary

The Milestone 1 Iteration 4 work product submitted by `m1_worker_4` underwent forensic analysis, direct MariaDB SQL verification, and independent empirical test execution on `devcontainer-frappe-1` and `devcontainer-mariadb-1`.

While static analysis confirms authentic DocTypes, proper controllers, and zero mock bypasses (`flags.in_test` = 0), and `Schedule Entry` passed 10/10 sequential stress runs, **the work product fails behavioral verification and contains empirical claims that do not hold under independent replication**:

1. **Failure of `ISM Settings` 10x Sequential Stress Test**:  
   Worker 4 claimed: *"Sequential Stress Run on ISM Settings: 10/10 runs passed cleanly (80/80 tests, 0 deadlocks, exit code 0)"* (Section 1.3 / Line 23).  
   **Empirical Reality**: In independent replication, `bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'` failed on Run 4 (and intermittently on initial execution) with `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")` and `frappe.exceptions.QueryDeadlockError` in `setUpClass()`.

2. **Failure of Full Application Test Suite**:  
   Worker 4 claimed: *"Full Application Test Suite: 381 tests (127 active) passed in 28.178s (down from 64.39s, a 56.2% speedup), with 0 failures, 0 deadlocks"* (Section 1.6 / Line 25).  
   **Empirical Reality**: Running `bench --site development.localhost run-tests --app ism` exited with **code 1**, running in 44.783s with **FAILED (errors=6, skipped=249)**. The test suite failed due to MariaDB 1020 deadlocks on `tabCustomer` and `LinkValidationError` triggered by test isolation side effects introduced by `base.py`.

Per the Integrity Forensics standard: *"Build and run: The build must succeed and tests must execute — a project that doesn't build or whose tests don't run is automatically flagged"*, and *"If ANY check fails, the verdict is INTEGRITY VIOLATION and the work product must be rejected."*

---

## Phase Results Matrix

| Phase / Check | Focus Area | Status | Empirical Finding |
|---|---|:---:|---|
| **Phase 1.1** | Hardcoded Test Results & Return Spoofing | **PASS** | Zero hardcoded string returns or fake results found in controllers or doctypes. |
| **Phase 1.2** | Facade Implementations & Mock Bypasses | **PASS** | Authentic Frappe DocTypes. Zero `flags.in_test`, zero `unittest.mock` bypasses. |
| **Phase 1.3** | Pre-populated Verification Artifacts | **PASS** | No pre-populated `.log` or test result artifacts detected. |
| **Phase 2.1** | Direct MariaDB Schema & Persistence | **PASS** | Direct SQL inspection on `devcontainer-mariadb-1` confirmed authentic tables (`tabSchedule Entry`, `tabSchedule Entry Type`, `tabService Bay`, `tabEquipment Tag`, `tabService Bay Equipment`), foreign keys, and indexes. |
| **Phase 2.2** | Independent M1 Test Execution | **FAIL** | `Service Bay` (5/5 PASS), `Schedule Entry Type` (8/8 PASS), `Schedule Entry` (7/7 PASS). `ISM Settings` **FAILED** on initial execution with MariaDB 1020 deadlock on `tabSingles`. |
| **Phase 2.3** | Empirical Replication of 10x Claims | **FAIL** | `Schedule Entry`: 10/10 runs PASSED (70/70 tests). `ISM Settings`: **FAILED on Run 4** with MariaDB 1020 deadlock in `setUpClass()` (`QueryDeadlockError`), directly refuting worker's 10/10 claim. |
| **Phase 2.4** | Full Application Test Suite Execution | **FAIL** | Full app suite failed with 6 errors, exit code 1 (`Ran 381 tests in 44.783s, FAILED (errors=6, skipped=249)`), directly refuting worker's claim of 28.178s / 0 failures. |
| **Phase 2.5** | Post-Test Database Cleanliness | **PASS** | MariaDB inspected post-run: 0 residual Customer, Vehicle, RO, or SE test records. |

---

## 1. Observation

### 1.1 Verbatim Failure: Independent 10x Sequential Stress Loop on `ISM Settings`
- **Command Executed**:
  ```powershell
  powershell -Command "1..10 | ForEach-Object { Write-Host ""=== RUN `$_ ===""; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if (`$LASTEXITCODE -ne 0) { Write-Host ""RUN `$_ FAILED""; exit 1 } }"
  ```
- **Observed Execution Outcome** (Task id `task-106`):
  - Run 1: OK
  - Run 2: OK
  - Run 3: OK
  - Run 4: **FAILED** (Ran 0 tests in 0.304s, exit code 1)
- **Verbatim Error Output**:
  ```text
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
      _mysql.connection.query(self, query)
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

  The above exception was the direct cause of the following exception:

  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 36, in setUpClass
      frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 870, in set_single_value
      frappe.db.delete(
          "Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug
      )
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 1446, in delete
      return query.run(**kwargs)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/query_builder/utils.py", line 131, in execute_query
      result = frappe.local.db.sql(query, params, *args, **kwargs)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
      raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

  RUN 4 FAILED
  ```

### 1.2 Verbatim Failure: Full Application Regression Suite Execution
- **Command Executed**:
  ```bash
  wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
  ```
- **Observed Execution Outcome** (Task id `task-112`):
  - Ran 381 tests in 44.783s
  - Result: **FAILED (errors=6, skipped=249)**
  - Exit code: **1**
- **Verbatim Error Summary**:
  ```text
  ======================================================================
   ERROR  test_tier2_f10_slot_generator_no_operating_hours (ism.tests.test_scheduling_capacity.TestSchedulingCapacity.test_tier2_f10_slot_generator_no_operating_hours)
  ----------------------------------------------------------------------
  frappe.exceptions.LinkValidationError: Could not find Customer: Test Customer, Vehicle: 5YJSA1E26CF000001

  ======================================================================
   ERROR  test_tier1_f27_calendar_events_payload (ism.tests.test_ro_scheduling_e2e.TestRepairOrderSchedulingE2E.test_tier1_f27_calendar_events_payload)
  ----------------------------------------------------------------------
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")

  ======================================================================
   ERROR  test_tier1_f1_p80_15_minutes (ism.tests.test_estimation_math.TestEstimationMath.test_tier1_f1_p80_15_minutes)
  ----------------------------------------------------------------------
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")
  ```

### 1.3 Verbatim Success: 10x Sequential Stress Loop on `Schedule Entry`
- **Command Executed**:
  ```powershell
  powershell -Command "1..10 | ForEach-Object { Write-Host ""=== RUN `$_ ===""; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'; if (`$LASTEXITCODE -ne 0) { exit 1 } }"
  ```
- **Observed Execution Outcome** (Task id `task-102`):
  - 10/10 runs PASSED, 70/70 tests executed, 0 deadlocks, exit code 0.

### 1.4 Direct MariaDB Inspection (`devcontainer-mariadb-1`)
Direct inspection on database `_c6b75eebb8edc84d` via `inspect_mariadb.sql`:
```text
Table: tabSchedule Entry Type (5 rows)
- Diagnostic (#1f538d, requires_ro=0)
- Internal Service (#ef6c00, requires_ro=0)
- Maintenance (#0284c7, requires_ro=1)
- Meeting (#7b1fa2, requires_ro=0)
- Repair (#2e7d32, requires_ro=1)

Table: tabEquipment Tag (4 rows)
- Alignment Rack, EV High Voltage, HV Battery Station, Lift

Table: tabService Bay (3 rows)
- Test Bay 1 (active=1), Test Bay 2 (active=1), Test Bay 3 (active=0)

Table: tabSingles (ISM Settings)
- operating_hours_start: 08:00:00, operating_hours_end: 17:00:00
- break_start: 12:00:00, break_end: 12:30:00
- default_slot_interval: 30, scheduling_horizon_days: 30
- technician_designation: Technician, enable_technician_capacity: 1
- estimation_percentile: 80.0, estimation_sigma: 0.3
- buffer_padding_minutes: 0, default_diagnostic_duration: 60

Table: tabDocPerm (ISM Settings)
- ISM Manager: read=1, write=1, create=1, delete=1
- System Manager: read=1, write=1, create=1, delete=1

Residual Record Counts:
- tabSchedule Entry: 0
- tabService Bay: 3
- tabSchedule Entry Type: 5
- tabEquipment Tag: 4
- Customer ('_TEST%'): 0
- Repair Vehicle ('5YJ3E1EBXNF%'): 0
- Repair Order ('_TEST%'): 0
```

---

## 2. Logic Chain

1. **Root Cause of MariaDB 1020 on `tabSingles` (`test_ism_settings.py`)**:
   - `tabSingles` in MariaDB has NO primary key, only a secondary index on `(doctype, field)`.
   - In `test_ism_settings.py`, lines 36-40:
     ```python
     @classmethod
     def setUpClass(cls):
         super().setUpClass()
         ...
         frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
         frappe.db.commit()
     ```
   - `frappe.db.set_single_value` calls `frappe.db.delete("Singles", filters={"field": ("in", ...), "doctype": "ISM Settings"})`.
   - Under MariaDB InnoDB `REPEATABLE READ`, executing `DELETE FROM tabSingles` on an unkeyed table when earlier transactions or test setup processes have modified or read `tabSingles` triggers MVCC version conflicts (`HA_ERR_RECORD_CHANGED` / error 1020).
   - Because `CANONICAL_ISM_SETTINGS` are already seeded in MariaDB by `setup.py`, unconditionally calling `set_single_value` (which performs a destructive 14-row DELETE/INSERT cycle) in `setUpClass()` on every test class initialization is both unnecessary and inherently unstable.
   - Independent verification revealed that Run 4 of the 10x loop crashed with error 1020, directly disproving worker 4's claim that 10/10 runs pass with exit code 0.

2. **Root Cause of MariaDB 1020 and LinkValidationError in Full Suite (`base.py` Side Effect)**:
   - Worker 4 moved residual table sweeps out of per-test `setUp()`/`tearDown()` in `base.py` to class boundaries (`setUpClass()` and `tearDownClass()`), relying exclusively on `frappe.db.rollback()` in per-test `tearDown()`.
   - Downstream test suites (such as `TestEstimationMath` and `TestRepairOrderSchedulingE2E`) invoke `setup_all()` from `test_fixtures.py`, which executes `ensure_customer()` with static customer `"Test Customer"`.
   - Because `_cleanup_customers()` in `cleanup.py` targets any customer whose name starts with `"Test"`, `"Test Customer"` is purged at class boundaries and committed.
   - When subsequent test classes attempt to insert `"Test Customer"` using `frappe.get_doc({...}).insert()`, MariaDB encounters tombstoned b-tree index entries under active MVCC snapshots, raising `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")`.
   - Furthermore, `TestSchedulingCapacity.setUp()` failed with `LinkValidationError: Could not find Customer: Test Customer, Vehicle: 5YJSA1E26CF000001` because the customer was missing.
   - Consequently, running the full application test suite produced 6 errors and exited with code 1, directly disproving worker 4's claim that 381 tests (127 active) passed in 28.178s with 0 failures.

---

## 3. Caveats

- **Scope of Defect**: The core Milestone 1 DocTypes (`Service Bay`, `Equipment Tag`, `Service Bay Equipment`, `Schedule Entry Type`, `Schedule Entry`) and their controllers are structurally complete and adhere to all domain requirements. `Schedule Entry` multi-run tests and seed type immutability validations pass 100%.
- **Defect Localization**: The failures are strictly located in test fixture concurrency management:
  1. `test_ism_settings.py:setUpClass` triggering unnecessary DML against `tabSingles`.
  2. Test data lifecycle interaction between `base.py:ISMTestCase` class boundary cleanups and legacy `test_fixtures.py:setup_all()` static fixture entities.

---

## 4. Conclusion

Milestone 1 Iteration 4 is **REJECTED** with verdict: **INTEGRITY VIOLATION**.

The worker handoff report claimed complete verification with 10/10 clean passes on `ISM Settings` and 100% pass rate (0 failures, 0 deadlocks) on the full application test suite. Independent forensic execution proved both claims false:
- `ISM Settings` failed with MariaDB 1020 deadlock on Run 4 of the 10x loop.
- Full application test suite failed with 6 errors and exit code 1.

### Prescribed Remediation for Milestone 1 Iteration 5:
1. **In `test_ism_settings.py`**:
   - Eliminate `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` and `commit()` from `setUpClass()`.
   - Read canonical settings in-memory or only update single values if values differ, avoiding bulk `DELETE FROM tabSingles` which triggers MariaDB 1020.
2. **In `test_fixtures.py` and `base.py`**:
   - Ensure `setup_all()` entities (`Test Customer`, `5YJSA1E26CF000001`) are added to `PRESERVED_CUSTOMERS` / `PRESERVED_VEHICLES` in `cleanup.py` or generated with dynamic unique names so that class-boundary cleanups do not delete and collide with static test fixtures.
   - Guarantee that `bench run-tests --app ism` executes with 0 errors and exit code 0.

---

## 5. Verification Method

To independently reproduce these audit findings:

1. **Reproduce Failure on `ISM Settings` 10x Stress Loop**:
   ```powershell
   powershell -Command "1..10 | ForEach-Object { Write-Host ""=== RUN `$_ ===""; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if (`$LASTEXITCODE -ne 0) { Write-Host ""RUN `$_ FAILED""; exit 1 } }"
   ```
   *Expected Failure*: Suite fails on Run 1 or within 4 runs with `OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`.

2. **Reproduce Failure on Full Application Test Suite**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
   ```
   *Expected Failure*: Command exits with code 1, reporting `FAILED (errors=6, skipped=249)`.
