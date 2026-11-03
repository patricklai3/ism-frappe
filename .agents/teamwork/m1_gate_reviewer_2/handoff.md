# Milestone 1 Gate Review & Adversarial Stress-Test Report

**Reviewer**: `m1_gate_reviewer_2` (Roles: Reviewer, Adversarial Critic)  
**Date**: 2026-10-27  
**Working Directory**: `d:\ism\.agents\teamwork\m1_gate_reviewer_2`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Review Target**: Milestone 1 Remediation & Verification (`m1_worker_5/handoff.md`)  
**Verdict**: **REQUEST_CHANGES**

---

## Review Summary

While individual Milestone 1 DocType stress loops pass cleanly in isolation (`ISM Settings` 10x loop: 10/10 PASS; `Schedule Entry` 10x loop: 10/10 PASS; `Service Bay`: 5/5 PASS; `Schedule Entry Type`: 8/8 PASS) and static master records (`Test Customer`, `5YJSA1E26CF000001`) are protected, the **full application test suite (`bench run-tests --app ism`) consistently crashes with exit code 1** due to a recurring transaction deadlock (`OperationalError: 1020`) on table `tabSingles` in `TestISMSettings.setUpClass`.

Worker 5's conditional baseline check (`divergent` check) succeeded when executed immediately after `test_ism_settings.py` had already set `buffer_padding_minutes = 0`, but whenever the full suite runs, preceding tests (`test_scheduling_capacity.py` and `test_estimation_math.py` via `test_fixtures.py:ensure_ism_settings()`) set `buffer_padding_minutes = 10` and commit to `tabSingles`. `TestISMSettings.setUpClass` detects this divergence and calls `frappe.db.set_single_value("ISM Settings", divergent)` without a transaction restart retry loop, triggering an unhandled MariaDB error 1020 deadlock and aborting the test run.

---

## Findings

### [Critical] Finding 1: Full Application Test Suite Crashes on `tabSingles` MariaDB Error 1020 Deadlock in `TestISMSettings.setUpClass`

- **What**: Executing `bench --site development.localhost run-tests --app ism` crashes with exit code 1 (`FAILED (errors=1, skipped=254)`) due to `frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")` in `TestISMSettings.setUpClass`.
- **Where**: `ism/ism/doctype/ism_settings/test_ism_settings.py`, lines 35–64:
  ```python
  current_settings = frappe.db.get_singles_dict("ISM Settings")
  divergent = {}
  for fieldname, expected_val in CANONICAL_ISM_SETTINGS.items():
      ...
  if divergent:
      frappe.db.set_single_value("ISM Settings", divergent)
      frappe.clear_cache(doctype="ISM Settings")
      if hasattr(frappe.db, "value_cache"):
          frappe.db.value_cache.clear()
      frappe.db.commit()
  ```
- **Why**: In full suite execution, `ism/tests/test_scheduling_capacity.py` and `ism/tests/test_estimation_math.py` execute before `test_ism_settings.py`. In their `setUp()`, `setup_all()` calls `ensure_ism_settings()`, which sets `buffer_padding_minutes = 10` (line 303 of `ism/tests/test_fixtures.py`) and commits. When `TestISMSettings.setUpClass` runs subsequently, `current_settings` has `'buffer_padding_minutes': '10'`, whereas `CANONICAL_ISM_SETTINGS` defines `0`. This populates `divergent = {'buffer_padding_minutes': 0}`. The resulting `set_single_value` executes `DELETE FROM tabSingles WHERE field IN ('buffer_padding_minutes', 'modified', 'modified_by') AND doctype='ISM Settings'`. Under MariaDB 11.8 snapshot isolation with background `bench worker` activity, this un-retried delete raises `ER_CHECKREAD (error 1020)`. Unlike `base.py` where Worker 5 wrapped entity creation in 3-attempt restart loops, `TestISMSettings.setUpClass` has no retry loop, causing an unhandled crash.
- **Suggestion**:
  1. In `test_ism_settings.py:setUpClass`, wrap `frappe.db.set_single_value("ISM Settings", divergent)` in a 3-attempt restart retry loop catching MariaDB error 1020 / `QueryDeadlockError` with `frappe.db.rollback()`.
  2. In `ism/tests/test_fixtures.py:ensure_ism_settings()`, change line 303 from `"buffer_padding_minutes": 10` to `"buffer_padding_minutes": 0` to align fixture defaults with canonical settings (`setup.py:146` and `CANONICAL_ISM_SETTINGS`), eliminating baseline divergence churn.

---

### [Major] Finding 2: Residual Service Bay Test Records in Database Not Purged by Cleanup Routine

- **What**: MariaDB database inspection reveals residual `Service Bay` records named `TEMP_BAY` and `TEMP_BAY_MULTI` remaining in the database after test suite execution.
- **Where**: `ism/tests/cleanup.py`, lines 468–485 (`_cleanup_service_bays()`):
  ```python
  all_bays = frappe.get_all("Service Bay", fields=["name", "bay_name"])
  for bay in all_bays:
      name = bay.name or bay.bay_name or ""
      if name in ("Test Bay 1", "Test Bay 2", "Test Bay 3"):
          continue
      if (
          any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS"))
          or "Test" in name
          or "TEST" in name
      ):
          ...
  ```
- **Why**: `_cleanup_service_bays()` only filters for prefixes `_TEST`, `TEST-`, `Test Unit Bay`, `STRESS` or names containing `Test`/`TEST`. Temporary bays named `TEMP_BAY` or `TEMP_BAY_MULTI` bypass this filter and remain as persistent clutter.
- **Suggestion**: Add `"TEMP"` and `"TEMP_BAY"` to the prefix exclusion list in `_cleanup_service_bays()`, or delete all service bays where `name not in ("Test Bay 1", "Test Bay 2", "Test Bay 3")` and `not is_preserved(name)`.

---

### [Minor] Finding 3: Default Parameter Discrepancy Between `test_fixtures.py` and `setup.py`

- **What**: `test_fixtures.py` defaults `buffer_padding_minutes: 10`, while `setup.py` defaults `buffer_padding_minutes: 0` and `test_ism_settings.py` expects `0`.
- **Where**: `ism/tests/test_fixtures.py:303` vs `ism/setup.py:146` vs `ism/ism/doctype/ism_settings/test_ism_settings.py:19`.
- **Why**: This discrepancy creates unnecessary state oscillation across test suites that share the database connection.
- **Suggestion**: Unify default `buffer_padding_minutes` to `0` across all files.

---

## 1. Observation

### 1.1 Direct Tool Execution Results

#### Verification A: 10x Sequential Stress Loop on `ISM Settings`
- **Command**:
  ```powershell
  1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if ($LASTEXITCODE -ne 0) { exit 1 } }
  ```
- **Observed Result**: 10/10 runs PASSED, 8 tests per run (80 tests total), 0 errors, 0 deadlocks, exit code 0.

#### Verification B: 10x Sequential Stress Loop on `Schedule Entry`
- **Command**:
  ```powershell
  1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'; if ($LASTEXITCODE -ne 0) { exit 1 } }
  ```
- **Observed Result**: 10/10 runs PASSED, 7 tests per run (70 tests total), 0 errors, 0 deadlocks, exit code 0.

#### Verification C: Isolated Milestone 1 Test Suites
- `bench --site development.localhost run-tests --app ism --doctype "Service Bay"`: Ran 5 tests in 2.603s, **OK**, exit code 0.
- `bench --site development.localhost run-tests --app ism --doctype "Schedule Entry Type"`: Ran 8 tests in 2.958s, **OK**, exit code 0.

#### Verification D: Full Application Test Suite (`bench run-tests --app ism`) — Crash Reproduced
- **Command**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Run 1 Result (task-92)**:
  ```text
  Ran 373 tests in 52.877s
  FAILED (errors=1, skipped=254)
  ERROR setUpClass (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings)
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  Exit code: 1
  ```
- **Run 2 Result (task-102, independent reproduction)**:
  ```text
  Ran 373 tests in 35.989s
  FAILED (errors=1, skipped=254)
  ERROR setUpClass (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings)
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  Exit code: 1
  ```

#### Verification E: Static Master Fixture Preservation
- **Command**:
  ```python
  import frappe
  frappe.init('development.localhost')
  frappe.connect()
  print('Cust:', frappe.db.get_value('Customer', 'Test Customer', ['name', 'customer_name']),
        'Veh:', frappe.db.get_value('Repair Vehicle', '5YJSA1E26CF000001', ['name', 'vin', 'customer']))
  ```
- **Output**:
  ```text
  Cust: ('Test Customer', 'Test Customer') Veh: ('5YJSA1E26CF000001', '5YJSA1E26CF000001', 'Test Customer')
  ```
- **Status**: Intact and correctly linked.

#### Verification F: Core Frappe Framework Modifications Check
- Checked `/workspace/development/frappe-bench/apps/frappe` and `/workspace/development/frappe-bench/apps/erpnext`.
- Existing modifications in `frappe/public/js/frappe/form/form.js` (2026-10-11) and `frappe/utils/background_jobs.py` (2026-07-08) predate this project. Zero core framework modifications were introduced in Milestone 1.

---

## 2. Logic Chain

1. **Deadlock Mechanism**:
   - In `test_fixtures.py:ensure_ism_settings()`, `buffer_padding_minutes` defaults to `10`.
   - `test_fixtures.py:setup_all()` calls `ensure_ism_settings()` and executes `frappe.db.commit()`.
   - When the full test suite runs, `ism/tests/test_scheduling_capacity.py` and `ism/tests/test_estimation_math.py` run before `test_ism_settings.py`. Their `setUp()` invokes `setup_all()`, committing `buffer_padding_minutes = 10` into table `tabSingles`.
   - When `TestISMSettings.setUpClass` subsequently executes, `current_settings` reads `buffer_padding_minutes = 10`.
   - Because `CANONICAL_ISM_SETTINGS` specifies `0`, `divergent` evaluates to `{'buffer_padding_minutes': 0}`.
   - `TestISMSettings.setUpClass` calls `frappe.db.set_single_value("ISM Settings", divergent)`.
   - `set_single_value` issues `DELETE FROM tabSingles WHERE field IN ('buffer_padding_minutes', 'modified', 'modified_by') AND doctype='ISM Settings'`.
   - Under MariaDB 11.8 with `REPEATABLE READ` and asynchronous `bench worker` execution, this uncommitted delete encounters row version divergence, throwing `OperationalError: 1020 ("Record has changed since last read in table 'tabSingles'; try restarting transaction")`.
   - Because `TestISMSettings.setUpClass` contains no retry loop, `QueryDeadlockError` aborts the class.

2. **Divergence From Worker 5 Handoff**:
   - Worker 5 reported `Ran 381 tests in 28.786s, OK (skipped=254), exit code 0`.
   - When `TestISMSettings` fails in `setUpClass`, all 8 of its tests are bypassed, reducing executed tests from 381 to 373 (`381 - 8 = 373`).
   - Worker 5's test ran after `test_ism_settings.py` had run individually, leaving `buffer_padding_minutes = 0` in the database, which allowed `divergent` to be empty and skipped the write. However, under a full run starting from standard test conditions, the crash is 100% reproducible.

3. **Integrity Evaluation**:
   - No hardcoded test results, facade implementations, or shortcuts were found in source code.
   - However, the claimed verification of full test suite stability was self-certifying under an unrepresentative baseline state. The gate review requires genuine green verification across the entire suite.

---

## 3. Caveats

- Isolated single-doctype test runs (`--doctype 'ISM Settings'`) pass 10/10 because `divergent` is empty when the database already holds canonical values. The deadlock only surfaces during full test suite runs (`--app ism`) where other test suites alter settings.
- The 2 modified files in core Frappe (`form.js` and `background_jobs.py`) have filesystem timestamps months prior to the project and are not related to M1.

---

## 4. Conclusion

The Milestone 1 implementation is structurally sound in its data models, controller validations, deletion protections, and permissions. However, because **`bench run-tests --app ism` fails with exit code 1**, Milestone 1 cannot be approved at this gate.

**Verdict: REQUEST_CHANGES**

Required remediations:
1. Implement a 3-attempt restart loop on MariaDB error 1020 in `test_ism_settings.py:setUpClass`.
2. Align `test_fixtures.py:ensure_ism_settings()` default `buffer_padding_minutes` to `0`.
3. Add `TEMP%` cleanup filter to `ism/tests/cleanup.py:_cleanup_service_bays()`.

---

## 5. Verification Method

To reproduce this finding on `devcontainer-frappe-1`:

```bash
# 1. Dirty the setting as occurs during test_fixtures execution
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); frappe.db.set_single_value('ISM Settings', 'buffer_padding_minutes', 10); frappe.db.commit()"

# 2. Run the full application test suite
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
```

*Expected Result*: Exit code 1, `FAILED (errors=1, skipped=254)` with `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")` in `TestISMSettings.setUpClass`.
