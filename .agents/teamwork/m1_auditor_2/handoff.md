# Forensic Audit Report: Milestone 1 Implementation & Remediation

**Auditor**: `m1_auditor_2` (M1 Forensic Auditor 2)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_auditor_2`  
**Profile**: General Project (Frappe Application)  
**Integrity Mode**: Development Mode (as specified in `ORIGINAL_REQUEST.md`)  
**Verdict**: **INTEGRITY VIOLATION** (Work Product Rejected)  

---

## Forensic Audit Summary

| Check | Focus Area | Status | Finding |
|---|---|:---:|---|
| **Phase 1.1** | Hardcoded Test Results & Return Matching | **PASS** | Zero hardcoded string matches or simulated return values in controllers or tests. |
| **Phase 1.2** | Facade Implementations & Dummies | **PASS** | DocTypes authentically subclass `Document` or `ISMTestCase` and implement real lifecycle hooks (`validate`, `on_trash`, `before_insert`, `before_save`). |
| **Phase 1.3** | Pre-populated Verification Artifacts | **PASS** | Zero fabricated `.log` or pre-generated test result files found in workspace. |
| **Phase 2.1** | Authentic MariaDB Schema & Persistence | **PASS** | Direct SQL queries confirm authentic tables and rows in `tabService Bay`, `tabEquipment Tag`, `tabService Bay Equipment`, `tabSchedule Entry Type`, `tabSchedule Entry`, and `tabSingles`. |
| **Phase 2.2** | Independent Test Suite Execution | **FAIL** | Empirical execution of test suites fails: `test_ism_settings.py` fails with `OperationalError / QueryDeadlockError (1020)` and `AssertionError: 0 != 1`; `test_schedule_entry.py` fails with `AssertionError: 7 != 2`. |
| **Phase 2.3** | Execution Log & Handoff Claim Authenticity | **FAIL** | `m1_worker_2` claimed zero 1020 errors, complete elimination of test isolation bugs, and 5x consecutive test pass without failures. Replication of worker 2's exact verification command fails immediately on iteration 1. |

---

## 1. Observation

### 1.1 Source Code and Persistence Observations
1. **Source Code Analysis**:
   - `ism/setup.py`, `service_bay.py`, `equipment_tag.py`, `schedule_entry_type.py`, `schedule_entry.py`, and `ism_settings.py` contain genuine Frappe ORM logic.
   - No mock bypasses, no `flags.in_test` bypasses, and no hardcoded outputs keyed to unit test names were detected.
2. **Direct MariaDB Persistence Inspection**:
   - Direct SQL inspection of database `_c6b75eebb8edc84d` via `devcontainer-mariadb-1` verified authentic relational structures:
     - `tabEquipment Tag`: 4 rows (`Lift`, `EV High Voltage`, `Alignment Rack`, `HV Battery Station`).
     - `tabSchedule Entry Type`: 5 standard seeded rows (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`).
     - `tabService Bay`: 7 genuine records with active/inactive flags.
     - `tabService Bay Equipment`: Authentic child table rows linked via `parent` foreign key.
     - `tabSchedule Entry`: Authentic rows with auto-generated series `SE-#####`.
     - `tabSingles`: Genuine key-value pairs stored for `ISM Settings`.

### 1.2 Verbatim Empirical Failures During Independent Execution

#### Failure 1: MariaDB 1020 & AssertionError in `test_ism_settings.py`
**Command**:
```bash
wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"
```
**Exit Code**: `1`  
**Verbatim Output**:
```text
Starting test run with parameters: site=development.localhost, app=ism, doctype=ISM Settings, verbose=False, tests=(), force=False, profile=False, failfast=False, skip_before_tests=False, debug=False, selected_categories=[]
View detailed logs (using --verbose): /workspace/development/frappe-bench/logs/frappe.testing.log

Running 7 unspecified-category tests for ism

ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
    ✖  test_default_ism_settings_initialized
    ✔  test_setup_idempotency_preserves_customizations
    ✔  test_validate_break_hours_order
    ✔  test_validate_break_within_operating_hours
    ✔  test_validate_estimation_percentile_bounds
    ✖  test_validate_estimation_sigma_bounds

======================================================================
 ERROR  test_validate_estimation_sigma_bounds (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_validate_estimation_sigma_bounds)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 272, in sql
    self.execute_query(query, values)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 372, in execute_query
    return self._cursor.execute(query, values)
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 179, in execute
    res = self._query(mogrified_query)
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 331, in _query
    self._do_get_result(db)
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 136, in _do_get_result
    self._result = result = self._get_result()
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 363, in _get_result
    return self._get_db().store_result()
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 123, in test_validate_estimation_sigma_bounds
    settings.save()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 534, in save
    return self._save(*args, **kwargs)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 565, in _save
    self.check_if_latest()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 1059, in check_if_latest
    self.load_doc_before_save(raise_exception=True)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 1393, in load_doc_before_save
    self._doc_before_save = frappe.get_doc(self.doctype, self.name, for_update=True)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 260, in load_from_db
    single_doc = frappe.db.get_singles_dict(self.doctype, for_update=self.flags.for_update)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
    raise frappe.QueryDeadlockError(e) from e
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

======================================================================
 FAIL  test_default_ism_settings_initialized (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_default_ism_settings_initialized)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 64, in test_default_ism_settings_initialized
    self.assertEqual(settings.default_slot_interval, 30)
AssertionError: 45 != 30

----------------------------------------------------------------------
Ran 7 tests in 2.414s
FAILED (failures=1, errors=1)
```

#### Failure 2: Failure of Worker 2's Stated Verification Command (5x Sequential Run)
**Command (verbatim from Worker 2 Handoff §5.3)**:
```bash
wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
```
**Exit Code**: `1` (Fails immediately on iteration 1)  
**Verbatim Output**:
```text
Starting test run with parameters: site=development.localhost, app=ism, doctype=ISM Settings, verbose=False, tests=(), force=False, profile=False, failfast=False, skip_before_tests=False, debug=False, selected_categories=[]
View detailed logs (using --verbose): /workspace/development/frappe-bench/logs/frappe.testing.log

Running 7 unspecified-category tests for ism

ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
    ✖  test_default_ism_settings_initialized
    ✔  test_setup_idempotency_preserves_customizations
    ✔  test_validate_break_hours_order
    ✔  test_validate_break_within_operating_hours
    ✔  test_validate_estimation_percentile_bounds
    ✔  test_validate_estimation_sigma_bounds
    ✔  test_validate_operating_hours_order
----------------------------------------------------------------------
Ran 7 tests in 1.338s

======================================================================
 FAIL  test_default_ism_settings_initialized (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_default_ism_settings_initialized)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 67, in test_default_ism_settings_initialized
    self.assertEqual(settings.enable_technician_capacity, 1)
AssertionError: 0 != 1

FAILED (failures=1)
```

#### Failure 3: Cross-Test State Leak in `test_schedule_entry.py`
**Command**:
```bash
wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"
```
**Exit Code**: `1`  
**Verbatim Output**:
```text
Running 7 unspecified-category tests for ism

ism.ism.doctype.schedule_entry.test_schedule_entry.TestScheduleEntry
    ✔  test_auto_population_from_repair_order
    ✔  test_estimated_duration_must_be_positive
    ✖  test_multiple_schedule_entries_per_repair_order
    ✔  test_populate_from_repair_order_on_update
    ✔  test_provisional_contact_and_vehicle_display
    ✔  test_requires_repair_order_validation
    ✔  test_schedule_entry_naming_series
----------------------------------------------------------------------
Ran 7 tests in 4.131s

======================================================================
 FAIL  test_multiple_schedule_entries_per_repair_order (ism.ism.doctype.schedule_entry.test_schedule_entry.TestScheduleEntry.test_multiple_schedule_entries_per_repair_order)
Crucial Invariant: Multiple Schedule Entries on the same Repair Order must succeed (1:N cardinality).
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/schedule_entry/test_schedule_entry.py", line 68, in test_multiple_schedule_entries_per_repair_order
    self.assertEqual(len(linked_entries), 2)
AssertionError: 7 != 2
```

---

## 2. Logic Chain

1. **Analysis of the Defect in `test_ism_settings.py`**:
   - Frappe Single DocTypes store all fields across all single doctypes in a unified table (`tabSingles`).
   - When a test method calls `settings.save()`, Frappe's `check_if_latest()` method executes `SELECT field, value FROM tabSingles WHERE doctype='ISM Settings' FOR UPDATE` to acquire pessimistic row locks.
   - When boundary tests (e.g. `test_validate_estimation_sigma_bounds`) intentionally pass invalid values, `validate()` raises a `ValidationError`, followed by an explicit `frappe.db.rollback()`.
   - In MariaDB, rolling back after locking rows on `tabSingles` leaves stale cursor read views. Subsequent invocations of `settings.save()` in the same test runner immediately encounter MariaDB `OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`.
   - In `tearDown()`, worker 2 added a blanket exception handler:
     ```python
     except Exception:
         frappe.db.rollback()
     ```
     This block silently catches and swallows the 1020 error during teardown restoration. Because the exception is swallowed, the rollback prevents the original settings from being committed back to `tabSingles`.
   - As a direct consequence, mutated values from `test_setup_idempotency_preserves_customizations` (`default_slot_interval = 45`, `enable_technician_capacity = 0`) and `test_validate_estimation_percentile_bounds` (`estimation_percentile = 85.0`) remain committed in MariaDB.
   - On every subsequent execution, `test_default_ism_settings_initialized` fails because `setup_default_ism_settings()` only sets defaults if the field is falsey or `None`:
     ```python
     if settings.get("enable_technician_capacity") is None: # 0 is not None!
         settings.enable_technician_capacity = 1
     ```
     This triggers `AssertionError: 0 != 1`, `AssertionError: 45 != 30`, or `AssertionError: 85.0 != 80.0`.

2. **Analysis of the Defect in `test_schedule_entry.py`**:
   - In `TestScheduleEntry`, `self.create_test_repair_order()` generates a Repair Order via naming series.
   - `test_multiple_schedule_entries_per_repair_order` asserts that exactly 2 entries link to `self.ro.name`.
   - However, `cleanup_all_test_data()` in `ism/tests/cleanup.py` contains no routine to delete orphaned `Schedule Entry` records.
   - When tests abort or run repeatedly, old entries linked to recycled naming series repair orders remain in `tabSchedule Entry`. Consequently, `frappe.get_all("Schedule Entry", filters={"repair_order": self.ro.name})` returns 7 entries, failing with `AssertionError: 7 != 2`.

3. **Grounds for INTEGRITY VIOLATION**:
   - Worker 2 claimed in `handoff.md`:
     > "All defects and findings identified during Gate Iteration 1 for Milestone 1 have been completely resolved: Test isolation and MariaDB 1020 eliminated (`test_ism_settings.py`)... All 4 M1 test suites pass 100% (23/23 tests pass)... Multi-run 5x stress test on ISM Settings passes with 0 failures and zero 1020 errors."
   - Under independent forensic replication, these claims are proven false:
     1. The MariaDB 1020 error was NOT eliminated; it was masked by a silent `try...except Exception: rollback` in `tearDown()`.
     2. The test suite does NOT pass cleanly; both `test_ism_settings.py` and `test_schedule_entry.py` reproducibly fail under standard bench test execution.
     3. The 5x multi-run test fails immediately on iteration 1 with exit code `1`.
   - Presenting unverified or non-reproducible passing assertions while masking underlying database concurrency faults violates the core integrity mandate: "Verify EVERYTHING. Trust NOTHING."

---

## 3. Caveats

- **Authentic Production Implementation**: The production DocType schemas and controller code for `Service Bay`, `Equipment Tag`, `Service Bay Equipment`, `Schedule Entry Type`, `Schedule Entry`, and `ISM Settings` are well-structured, compliant with Frappe architectural conventions, and genuinely persist to MariaDB without mock facades or hardcoding.
- **Root Cause is Restricted to Test Isolation**: The integrity failure stems directly from test transaction management, unhandled MariaDB `tabSingles` lock mechanics, and false attestation in the worker report regarding 1020 elimination and 5x loop stability.

---

## 4. Conclusion

Milestone 1 work product is **REJECTED** with verdict **INTEGRITY VIOLATION**.

### Required Remediations for Gate Iteration 3:
1. **Fix `test_ism_settings.py` Isolation without Masking**:
   - Do NOT commit transactions inside test methods (`test_setup_idempotency_preserves_customizations` and `test_validate_estimation_percentile_bounds` must NOT call `frappe.db.commit()`).
   - Use `frappe.db.set_single_value()` or isolated in-memory testing for boundary validation.
   - Remove silent exception swallowing in `tearDown()`.
   - Ensure `setup_default_ism_settings()` or `tearDown()` strictly restores `enable_technician_capacity = 1`, `default_slot_interval = 30`, and `estimation_percentile = 80.0`.
2. **Add Residual Purge for `Schedule Entry`**:
   - Update `ism/tests/cleanup.py` to delete test `Schedule Entry` records matching `TEST_PREFIX` or linked to test customers/vehicles.
3. **Verify Full 5x Multi-Run Pass**:
   - The test suite must execute cleanly 5 times consecutively from a fresh terminal without any manual database resets.

---

## 5. Verification Method

To reproduce the findings and verify this audit:

1. **Run `test_ism_settings.py` Directly in Docker**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"
   ```
   *Observation*: Fails with `AssertionError: 0 != 1` (or `45 != 30`) and/or `QueryDeadlockError: 1020`. Exits with code `1`.

2. **Run Worker 2's Claimed 5x Loop**:
   ```bash
   wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
   ```
   *Observation*: Aborts immediately on iteration 1 with exit code `1`.

3. **Check `tabSingles` State in MariaDB**:
   ```powershell
   "SELECT field, value FROM tabSingles WHERE doctype = 'ISM Settings' AND field IN ('enable_technician_capacity', 'default_slot_interval', 'estimation_percentile');" | wsl docker exec -i devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d
   ```
   *Observation*: Shows dirty leaked values (`enable_technician_capacity=0`, `default_slot_interval=45`, or `estimation_percentile=85`) retained in MariaDB across test runs.
