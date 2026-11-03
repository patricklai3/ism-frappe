# Handoff Report — Milestone 1 Iteration 6 Reviewer 1

**Agent ID**: `m1_it6_reviewer_1`  
**Role**: `reviewer` / `critic` (`teamwork_preview_reviewer`)  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_reviewer_1`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Date**: 2026-10-27 UTC  
**Handoff Type**: Hard  
**Verdict**: **REQUEST_CHANGES**

---

## Executive Summary

Milestone 1 Iteration 6 Remediation Worker (`m1_it6_worker_1`) implemented transactional durability, concurrency retry handling, and test-data hygiene across 6 files. Independent empirical review and containerized test execution confirm significant progress:
- **`Schedule Entry` Concurrency**: Completely resolved. The 10x sequential stress loop passed 10/10 runs (70/70 tests executed, exit code 0). Run 7 (which previously deadlocked on `tabSeries`) now passes reliably due to `ScheduleEntry.autoname(self)` retry handling.
- **Fixture Persistence & Teardown**: `create_test_repair_order` and prerequisite entity commits in `base.py` eliminated `LinkValidationError`. The full application test suite (`bench run-tests --app ism`) passed 381 tests (exit code 0, 0 failures, 0 errors, 254 skipped).
- **Database Cleanliness**: `cleanup.py` successfully purged all temporary service bays (`TEMP_BAY`, `TEMP_BAY_MULTI`) while preserving static fixtures.

**However, a critical concurrency defect remains on `tabSingles` (`ISM Settings`)**:
During independent empirical execution of the 10x sequential stress loop on `ISM Settings` (Task-54), **Run 3 crashed with MariaDB Error 1020 / `QueryDeadlockError`** in `test_setup_idempotency_preserves_customizations` (`test_ism_settings.py:213`). While the worker hardened `setUpClass` against `tabSingles` deadlocks, they left calls to `frappe.db.set_single_value` inside `test_setup_idempotency_preserves_customizations` completely unguarded.

Because stress loop reliability is a mandatory gate requirement and MariaDB Error 1020 was directly reproduced during independent verification, the verdict is **REQUEST_CHANGES**.

---

## 1. Observation

### 1.1 Empirical Verification Test Results

#### Verification 1: 10x Sequential Stress Loop on `Schedule Entry`
- **Command**:
  ```powershell
  1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
  ```
- **Task ID**: `7caa692c-4857-4186-8396-98856ce94e81/task-46`
- **Result**:
  - Runs 1–10: All 10 runs passed (avg ~5.2s per run).
  - Run 7 (previous gate failure point): Ran 7 tests in 5.774s — OK.
  - Overall: 70/70 tests passed, 0 errors, 0 deadlocks, exit code 0.

#### Verification 2: 10x Sequential Stress Loop on `ISM Settings`
- **Command**:
  ```powershell
  1..10 | ForEach-Object { Write-Host "=== SETTINGS RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }
  ```
- **Execution 1 (Task ID: `7caa692c-4857-4186-8396-98856ce94e81/task-54`)**:
  - Run 1: Ran 8 tests in 2.256s — OK
  - Run 2: Ran 8 tests in 2.146s — OK
  - Run 3: **FAILED with exit code 1** (`FAILED (errors=1)`)
  - **Verbatim Error Traceback**:
    ```text
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
        mogrified_query = b"DELETE FROM `tabSingles` WHERE `field` IN ('default_slot_interval','modified','modified_by') AND `doctype`='ISM Settings'"
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
      File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 1446, in delete
        return query.run(**kwargs)
      File "/workspace/development/frappe-bench/apps/frappe/frappe/query_builder/utils.py", line 131, in execute_query
        result = frappe.local.db.sql(query, params, *args, **kwargs)
      File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
        raise frappe.QueryDeadlockError(e) from e
    frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
    ```
- **Execution 2 (Task ID: `7caa692c-4857-4186-8396-98856ce94e81/task-68`)**:
  - Following container transaction rollback, all 10 runs passed (80/80 tests, exit code 0).
  - Demonstrates that the failure is an intermittent concurrency version conflict on `tabSingles`.

#### Verification 3: Dirty Baseline Test on `ISM Settings`
- **Commands**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); frappe.db.set_single_value('ISM Settings', 'buffer_padding_minutes', 15); frappe.db.commit()"
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"
  ```
- **Result**:
  - Ran 8 tests in 2.709s — OK, exit code 0.
  - Reset divergence smoothly in `setUpClass`.

#### Verification 4: Full Application Test Suite (`bench run-tests --app ism`)
- **Command**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Task ID**: `7caa692c-4857-4186-8396-98856ce94e81/task-62`
- **Result**:
  - Ran 381 tests in 61.477s.
  - OK (skipped=254), exit code 0.
  - 0 failures, 0 errors, 0 `LinkValidationError`, 0 `QueryDeadlockError`.

#### Verification 5: Database Cleanliness & Hygiene Verification
- **Command**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); from ism.tests.cleanup import cleanup_all_test_data; cleanup_all_test_data(False); print('Remaining Bays:', frappe.get_all('Service Bay', fields=['name', 'bay_name', 'is_active'])); print('Remaining Equipment Tags:', frappe.get_all('Equipment Tag', pluck='name')); print('Test Customer exists:', frappe.db.exists('Customer', 'Test Customer')); print('Test Vehicle exists:', frappe.db.exists('Repair Vehicle', '5YJSA1E26CF000001'))"
  ```
- **Output**:
  ```text
  Remaining Bays: [{'name': 'Test Bay 1', 'bay_name': 'Test Bay 1', 'is_active': 1}, {'name': 'Test Bay 2', 'bay_name': 'Test Bay 2', 'is_active': 1}, {'name': 'Test Bay 3', 'bay_name': 'Test Bay 3', 'is_active': 0}]
  Remaining Equipment Tags: ['Alignment Rack', 'EV High Voltage', 'HV Battery Station', 'Lift']
  Test Customer exists: Test Customer
  Test Vehicle exists: 5YJSA1E26CF000001
  ```
  - Zero `TEMP_BAY` or `TEMP_BAY_MULTI` rows.
  - Exactly 3 canonical test bays remain.
  - Seed tags and static fixtures intact.

---

### 1.2 Code Inspection Findings

#### [Critical] Finding 1: Unhandled MariaDB Error 1020 in `test_setup_idempotency_preserves_customizations`
- **Location**: `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py:213-214`
- **Code**:
  ```python
  209: 	def test_setup_idempotency_preserves_customizations(self):
  210: 		"""Verify setup_default_ism_settings preserves existing customizations without committing state."""
  211: 		try:
  212: 			# Apply custom values via set_single_value in uncommitted transaction
  213: 			frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)
  214: 			frappe.db.set_single_value("ISM Settings", "enable_technician_capacity", 0)
  215: 			frappe.clear_cache(doctype="ISM Settings")
  ```
- **Problem**:
  In MariaDB under `REPEATABLE READ`, `frappe.db.set_single_value()` performs a `DELETE FROM tabSingles WHERE field IN (...)` followed by an insert. If concurrent transactions (or previous uncommitted test transactions) touch `tabSingles`, MariaDB throws `(1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`, raising `frappe.exceptions.QueryDeadlockError`.
  The worker added a restart retry loop to `setUpClass()` (lines 58–75), but omitted retry protection from lines 213–214. Consequently, whenever `tabSingles` experiences row version contention across successive test invocations, `test_setup_idempotency_preserves_customizations` crashes.
- **Suggestion**:
  Wrap `set_single_value` calls in a 3-attempt restart loop with `frappe.db.rollback()` and cache invalidation before retrying, or provide a helper method `_set_single_value_with_retry(doctype, key, value)`.

---

## 2. Logic Chain

1. **Resolution of `tabSeries` Deadlock**:
   - `ScheduleEntry.autoname(self)` in `schedule_entry.py:11-25` traps MariaDB Error 1020 during `make_autoname()`, issues `frappe.db.rollback()`, and retries up to 3 times.
   - Verification 1 empirical proof: 10/10 runs of the Schedule Entry stress loop completed with 0 deadlocks (70/70 tests passed), confirming that `tabSeries` contention is resolved.

2. **Resolution of `LinkValidationError`**:
   - `base.py:71, 95, 124, 137` commits newly inserted test customers, vehicles, and repair orders immediately.
   - Verification 4 empirical proof: `bench run-tests --app ism` passed all 381 tests with zero `LinkValidationError` in `TestTeslaIntegration` or elsewhere.

3. **Resolution of `TEMP_BAY` Leakage**:
   - `cleanup.py:475-496` enforces `PRESERVED_SERVICE_BAYS = {"Test Bay 1", "Test Bay 2", "Test Bay 3"}` and purges any other bays.
   - Verification 5 empirical proof: Direct database inspection confirmed zero `TEMP_BAY` rows remain.

4. **Persistence of `tabSingles` Error 1020 in `test_ism_settings.py`**:
   - While `setUpClass` handles divergence gracefully, `test_setup_idempotency_preserves_customizations` mutates `tabSingles` directly via `frappe.db.set_single_value("ISM Settings", ...)` without retry logic.
   - Empirical proof from Task-54: Run 3 crashed with MariaDB Error 1020 directly on `test_ism_settings.py:213`.
   - Therefore, the requirement that the test suite survives 10x sequential stress without Error 1020 is not yet satisfied.

---

## 3. Caveats

- **No Integrity Violation / Cheating**: The static source code and test definitions contain genuine business logic, realistic validations, and authentic assertions. No facade mocks, bypass strings, or hardcoded return values were detected.
- **Single Run vs Stress Loop**: In isolated single runs, `test_ism_settings.py` passes 8/8 tests. The failure is strictly a concurrency timing/version contention issue under sequential stress.

---

## 4. Conclusion

**Verdict**: **REQUEST_CHANGES**

The work product demonstrates high quality across 5 of the 6 files, and successfully fixes 4 of the 5 audit issues. However, the remaining unguarded `set_single_value` call in `test_setup_idempotency_preserves_customizations` caused a live crash during the mandatory 10x stress loop on `ISM Settings`.

### Required Fixes:
1. In `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`:
   Wrap `frappe.db.set_single_value` calls in `test_setup_idempotency_preserves_customizations` (lines 213–214) in a 3-attempt restart loop on MariaDB Error 1020 / `QueryDeadlockError` with `frappe.db.rollback()` and cache clearing.
2. Re-run the 10x sequential stress loop on `ISM Settings` to guarantee 10/10 consecutive passes.

---

## 5. Verification Method

To independently reproduce the finding:

```powershell
# 1. Reproduce Error 1020 in ISM Settings Stress Loop:
1..10 | ForEach-Object { Write-Host "=== SETTINGS RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }
```

### Invalidation Conditions
- Any occurrence of MariaDB Error 1020 / `QueryDeadlockError` during test runs.
- Non-zero exit code on the 10x sequential stress loops.
