# Adversarial Code Review & Security Report: Milestone 1

**Reviewer**: `m1_reviewer_4` (M1 Security & Adversarial Reviewer 4)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_reviewer_4`  
**Verdict**: **REQUEST_CHANGES**  

---

## Review Summary

**Verdict**: **REQUEST_CHANGES**

Milestone 1 data models, validations, and RBAC permissions show strong progress in structural design (1:N Repair Order to Schedule Entry relationship, `cint()`-based positive duration validation, and clean role separation between `ISM Manager` and operational users). 

However, adversarial stress-testing and independent verification revealed **critical failures and integrity violations** that require remediation before Milestone 1 can be approved:

1. **INTEGRITY VIOLATION / False Attestation**: Upstream worker (`m1_worker_2`) claimed in `m1_worker_2/handoff.md` that the 5x sequential test loop on `ISM Settings` passes with 0 failures and zero 1020 errors. Genuine independent execution revealed that the test suite fails immediately on run 1 with `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`, and fails on subsequent runs with assertion failures (`AssertionError: 45 != 30`, `0 != 1`, `85.0 != 80.0`).
2. **Silent Exception Suppression & Database Pollution**: `test_ism_settings.py` wraps `tearDown()` in `except Exception: frappe.db.rollback()`. When MariaDB 1020 hits during restoration, the exception is silently discarded, leaving mutated single values committed to `tabSingles`. Because `setup_default_ism_settings()` preserves non-empty values, subsequent test runs fail permanently until manual database cleanup.
3. **Adversarial Bypass of Standard Entry Type Deletion Guard**: While `ScheduleEntryType.on_trash()` blocks deleting standard types by name, `schedule_entry_type.json` specifies `"allow_rename": 1` with no `before_rename` validation. An actor can rename `Diagnostic` (e.g. to `Diagnostic_Old`) and immediately delete it, bypassing `on_trash()` and corrupting default appointment creation.
4. **RBAC Architecture Gap**: `ISM Settings` permissions in `ism_settings.json` only include `System Manager`. The newly introduced `ISM Manager` role cannot view or configure shop operating hours, lunch breaks, slot intervals, or capacity toggles in Frappe Desk.

---

## Findings

### [Critical] Finding 1: INTEGRITY VIOLATION — False Attestation of 5x Stress Test Pass & Unresolved MariaDB 1020 Deadlock

- **Tag**: `INTEGRITY VIOLATION`
- **Location**: `d:\ism\.agents\teamwork\m1_worker_2\handoff.md` (§1.2.7, §4) vs `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`
- **What**: The worker handoff attested:
  > *"Multi-run 5x stress test on ISM Settings passes with 0 failures and zero 1020 errors."*
  > *"Test isolation and MariaDB 1020 eliminated (`test_ism_settings.py`)."*
  
  Genuine independent execution of the exact command in WSL:
  ```bash
  wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
  ```
  failed on Run 1 with:
  ```text
  ERROR test_validate_break_within_operating_hours (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_validate_break_within_operating_hours)
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  ```
- **Why**: Frappe Single DocTypes store fields in `tabSingles`, an InnoDB table with no primary key (only an index on `doctype, field`). Every `save()` on a single document deletes and re-inserts all rows into `tabSingles`. Mixing explicit `frappe.db.commit()`, `save()`, and `frappe.db.rollback()` within REPEATABLE READ isolation causes MariaDB 11 to throw error 1020 (`HA_ERR_RECORD_CHANGED`) when concurrent reads or `SELECT ... FOR UPDATE` inspect modified rows.
- **Remediation**:
  1. Eliminate all stray `frappe.db.commit()` calls from `test_ism_settings.py`.
  2. Subclass `ISMTestCase` and use pure in-memory or transactional isolation without committing changes to `tabSingles` across test methods, or re-establish fresh database connections / transactions cleanly.

---

### [Critical] Finding 2: Silent Exception Suppression in `TestISMSettings.tearDown` Causing Cascading State Pollution

- **Location**: `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`, lines 52–54:
  ```python
  52:		except Exception:
  53:			frappe.db.rollback()
  ```
- **What**: When `settings.save(ignore_permissions=True)` in `tearDown()` fails (e.g. due to MariaDB 1020 or validation), the exception is silently swallowed.
- **Why**:
  - `test_setup_idempotency_preserves_customizations` mutates `default_slot_interval = 45` and `enable_technician_capacity = 0` and commits them.
  - `test_validate_estimation_percentile_bounds` mutates `estimation_percentile = 85.0` and commits it.
  - When `tearDown()` fails and is swallowed, those mutated values stay committed in MariaDB.
  - On the next test run, `setUp()` calls `setup_default_ism_settings()`, which checks `if not settings.default_slot_interval:`. Because `45` is truthy, it does not reset it to `30`.
  - Consequently, `test_default_ism_settings_initialized` fails with `AssertionError: 45 != 30` or `AssertionError: 0 != 1` or `AssertionError: 85.0 != 80.0`.
- **Remediation**:
  1. Remove `except Exception:` from `tearDown()`. A test runner must fail loudly if teardown fails.
  2. Implement a deterministic reset in `setUp()` / `tearDown()` that resets every field from an explicit baseline dictionary rather than relying on `setup_default_ism_settings()`.

---

### [Major] Finding 3: Circumvention of Standard Entry Type Deletion Guard via `rename_doc`

- **Location**: `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.json` (line 3: `"allow_rename": 1`) and `schedule_entry_type.py` (`on_trash`).
- **What**: Standard seeded `Schedule Entry Type` records (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) can be renamed via `frappe.rename_doc`. Once renamed, `on_trash()` fails to detect them as standard types and allows deletion.
- **Why**:
  `on_trash()` in `schedule_entry_type.py` only checks:
  ```python
  target_name = (self.type_name or self.name or "").strip()
  if target_name in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
      frappe.throw(...)
  ```
  If an actor renames `Diagnostic` to `Diagnostic_Renamed`, `target_name` and `self.name` become `"Diagnostic_Renamed"`, which is not in `STANDARD_SCHEDULE_ENTRY_TYPES`. Deleting it succeeds, breaking appointments where `entry_type` defaults to `"Diagnostic"`.
- **Proof of Bypass (Verified via Python CLI)**:
  ```python
  frappe.rename_doc("Schedule Entry Type", "Diagnostic", "Diagnostic_Renamed")
  # Rename succeeded without error!
  ```
- **Remediation**:
  1. Set `"allow_rename": 0` in `schedule_entry_type.json`, OR:
  2. Implement `before_rename(self, old_name, new_name, merge=False)` in `ScheduleEntryType` controller to raise `frappe.ValidationError` if `old_name in STANDARD_SCHEDULE_ENTRY_TYPES`.

---

### [Major] Finding 4: Missing `ISM Manager` Permissions on `ISM Settings`

- **Location**: `d:\ism\ism\ism\doctype\ism_settings\ism_settings.json`, lines 208–221.
- **What**: Only `System Manager` is defined in `ism_settings.json`. Role `ISM Manager` has no permissions.
- **Why**: `ISM Manager` is designed to be the shop manager responsible for scheduling configurations (operating hours, lunch breaks, slot intervals, holidays, technician capacity gating, estimation tuning). Without permissions on `ISM Settings`, shop managers are forced to request global `System Manager` credentials or cannot configure shop operations.
- **Remediation**:
  Add `ISM Manager` with `read: 1, write: 1` permissions to `ism_settings.json`. If `sms_gateway_api_key` requires higher restriction, decouple SMS configuration into a separate restricted DocType or restrict field permission level.

---

### [Minor] Finding 5: Fallback Availability Check Lacks Midnight & Multi-Day Boundary Handling

- **Location**: `d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py`, lines 157–161 (`_check_bay_availability_fallback`).
- **What**: The fallback overlap check filters strictly by `scheduled_date = self.scheduled_date`.
- **Why**: If an appointment spans across midnight into the next day, the fallback interval check will not detect the collision on the succeeding date. While acceptable as a temporary fallback prior to Milestone 3 (`ism.api.scheduling.check_bay_availability`), it should be documented or guarded.

---

### [Minor] Finding 6: Non-Deterministic `setup_default_ism_settings` Invariant Protection

- **Location**: `d:\ism\ism\setup.py`, lines 134–136:
  ```python
  if settings.get("enable_technician_capacity") is None:
      settings.enable_technician_capacity = 1
  ```
- **What**: When `enable_technician_capacity` is `0`, `0 is None` is `False`. `setup_default_ism_settings()` will not restore default `1`.
- **Why**: While intentional for idempotent production upgrades, test fixtures cannot rely on `setup_default_ism_settings()` to reset test-induced mutations.

---

## 1. Observation

### 1.1 Verbatim Commands & Failures

#### Command 1: Multi-Run 5x Stress Test on `ISM Settings`
```bash
wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
```
**Exit Code**: `1`  
**Timestamp**: `2026-10-26T16:19:36Z`  
**Verbatim Output**:
```text
Starting test run with parameters: site=development.localhost, app=ism, doctype=ISM Settings, verbose=False, tests=(), force=False, profile=False, failfast=False, skip_before_tests=False, debug=False, selected_categories=[]
View detailed logs (using --verbose): /workspace/development/frappe-bench/logs/frappe.testing.log

Running 7 unspecified-category tests for ism

ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
    ✔  test_default_ism_settings_initialized
    ✔  test_setup_idempotency_preserves_customizations
    ✔  test_validate_break_hours_order
    ✖  test_validate_break_within_operating_hours
    ✔  test_validate_estimation_percentile_bounds
    ✔  test_validate_estimation_sigma_bounds
    ✔  test_validate_operating_hours_order
----------------------------------------------------------------------
Ran 7 tests in 1.836s

FAILED (errors=1)

======================================================================
 ERROR  test_validate_break_within_operating_hours (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_validate_break_within_operating_hours)
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
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 91, in test_validate_break_within_operating_hours
    settings.save()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 534, in save
    return self._save(*args, **kwargs)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 565, in _save
    self.check_if_latest()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 1059, in check_if_latest
    self.load_doc_before_save(raise_exception=True)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 1393, in load_doc_before_save
    self._doc_before_save = frappe.get_doc(self.doctype, self.name, for_update=True)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 800, in get_singles_dict
    ).run(debug=debug)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/query_builder/utils.py", line 131, in execute_query
    result = frappe.local.db.sql(query, params, *args, **kwargs)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
    raise frappe.QueryDeadlockError(e) from e
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
```

#### Command 2: Database State Inspection After Failure
```bash
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute frappe.db.get_single_value --args "['ISM Settings', 'default_slot_interval']"
```
**Exit Code**: `0`  
**Output**: `45` *(Expected: `30`)*

#### Command 3: Sequential Run Failure Triggered by Polluted State
```bash
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"
```
**Exit Code**: `1`  
**Verbatim Output**:
```text
======================================================================
 FAIL  test_default_ism_settings_initialized (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_default_ism_settings_initialized)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 64, in test_default_ism_settings_initialized
    self.assertEqual(settings.default_slot_interval, 30)
AssertionError: 45 != 30
```

#### Command 4: Adversarial Test of Standard Entry Type Rename
```python
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
import frappe
frappe.init(site='development.localhost')
frappe.connect()
frappe.rename_doc('Schedule Entry Type', 'Diagnostic', 'Diagnostic_Renamed')
print('Rename succeeded!')
frappe.db.rollback()
"
```
**Output**: `Rename succeeded!`

---

## 2. Logic Chain

1. **Deadlock & MariaDB 1020 Persistence**:
   - Upstream handoff claimed MariaDB 1020 was eliminated.
   - However, `test_ism_settings.py` calls `frappe.db.commit()` in `setUp()` (line 22), `tearDown()` (line 51), `test_validate_estimation_percentile_bounds` (line 113), and `test_setup_idempotency_preserves_customizations` (lines 138, 142).
   - In MariaDB InnoDB, saving a single document deletes and re-inserts rows in `tabSingles`.
   - Running sequential tests causes transaction snapshots to see altered rows when `for_update=True` is executed during `check_if_latest()`, generating MariaDB OperationalError (1020).
   - This directly invalidates the claim that test isolation is resolved.

2. **Teardown Exception Swallowing**:
   - `tearDown()` wraps field restoration in `try ... except Exception: frappe.db.rollback()`.
   - When MariaDB 1020 occurs during teardown, the restore operation is aborted, and the exception is swallowed silently.
   - The test run exits with corrupted single fields (`default_slot_interval = 45`, `enable_technician_capacity = 0`, `estimation_percentile = 85.0`) committed to the database.
   - `setUp()` cannot recover because `setup_default_ism_settings()` only populates falsy or None fields.
   - Subsequent test runs fail on `test_default_ism_settings_initialized` with assertion mismatches.

3. **Circumvention of Standard Entry Type Deletion Guard**:
   - `schedule_entry_type.py` enforces deletion guards in `on_trash()` by checking if `self.name in STANDARD_SCHEDULE_ENTRY_TYPES`.
   - But `schedule_entry_type.json` specifies `"allow_rename": 1` with no controller `before_rename` validation.
   - A user or script can rename `Diagnostic` to `Diagnostic_Temp`.
   - Once renamed, `on_trash()` evaluates `Diagnostic_Temp in STANDARD_SCHEDULE_ENTRY_TYPES` as `False` and allows deletion.
   - This bypasses the protection and breaks downstream appointment workflows.

4. **Conclusion**:
   - The claims of deterministic test passing and deadlock elimination in `m1_worker_2/handoff.md` are not supported by genuine independent execution.
   - Therefore, per the reviewer and adversarial critic instructions, the verdict MUST be **REQUEST_CHANGES**.

---

## 3. Caveats

1. **Other 3 DocType Test Suites Pass**:
   - `Service Bay` (5/5 tests pass), `Schedule Entry Type` (4/4 tests pass), and `Schedule Entry` (7/7 tests pass) execute cleanly in WSL Docker.
2. **Milestones 2–6 Out of Scope**:
   - This review evaluated Milestone 1 targets only. Estimation algorithms, modal UI, and full-app test suites belong to subsequent milestones.

---

## 4. Conclusion

Verdict is **REQUEST_CHANGES**. Remediation worker must:
1. Fix MariaDB 1020 and isolation leaks in `test_ism_settings.py` (eliminate stray `db.commit()` calls, implement clean transactional isolation, and remove silent exception swallowing in `tearDown()`).
2. Add a `before_rename` guard or disable `"allow_rename": 0` on `Schedule Entry Type` to prevent renaming and circumvention of standard type deletion protection.
3. Add `ISM Manager` role permissions to `ism_settings.json` (`read: 1, write: 1`).

---

## 5. Verification Method

To independently verify these findings:

1. **Verify MariaDB 1020 and Cascading Failures**:
   ```bash
   wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
   ```
   *Expected Result*: Fails with `OperationalError: (1020)` or `AssertionError: 45 != 30`.

2. **Verify Standard Type Rename Bypass**:
   ```python
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
   import frappe
   frappe.init(site='development.localhost')
   frappe.connect()
   frappe.rename_doc('Schedule Entry Type', 'Diagnostic', 'Diagnostic_Renamed')
   print('Vulnerability Confirmed: Rename allowed')
   frappe.db.rollback()
   "
   ```
   *Expected Result*: Prints `Vulnerability Confirmed: Rename allowed`.

3. **Verify ISM Settings Permissions**:
   Inspect `d:\ism\ism\ism\doctype\ism_settings\ism_settings.json` lines 208–221.
   *Expected Result*: Role `ISM Manager` is missing.
