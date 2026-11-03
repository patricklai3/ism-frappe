# Forensic Integrity Audit Report — Milestone 1 Gate

**Work Product**: Milestone 1 Resource & Capacity Data Models, Settings Consolidation, and Test Harness  
**Auditor**: `m1_gate_auditor_1` (Role: forensic auditor)  
**Date**: 2026-10-26 / 2026-10-27 UTC  
**Working Directory**: `d:\ism\.agents\teamwork\m1_gate_auditor_1`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Integrity Mode**: Development Mode (authoritative per `ORIGINAL_REQUEST.md`)  
**Verdict**: **INTEGRITY VIOLATION** (Work Product Rejected)  

---

## Executive Summary

`m1_worker_5` submitted a handoff report (`d:\ism\.agents\teamwork\m1_worker_5\handoff.md`) asserting:
- Verdict: *"REMEDIATION COMPLETE — 100% VERIFIED"*
- Claimed 10x Sequential Stress Loop on `Schedule Entry`: *"10/10 PASS, 70 tests executed, 0 deadlocks, exit code 0"*
- Claimed Full Application Test Suite (`bench run-tests --app ism`): *"Ran 381 tests in 28.786s, OK (skipped=254), exit code 0"* replicated in *"29.737s, exit code 0"*.
- Claimed complete elimination of MariaDB Error 1020 (`ER_CHECKREAD`) and pristine post-test database state.

Independent empirical execution in `devcontainer-frappe-1` and schema inspection in `devcontainer-mariadb-1` **refutes these claims**:
1. **Stress Loop Failure**: The 10x sequential stress loop for `Schedule Entry` failed on Run 7 with MariaDB Error 1020 (`frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")`).
2. **Full Application Test Suite Failures**:
   - Execution 1 (Task-130) failed with exit code 1 (`FAILED (errors=1, skipped=254)`) due to MariaDB Error 1020 on `tabSeries` (`name='SE-'`) in `test_tier1_f7_schedule_entry_overlap_validation_error` (`TestSchedulingCapacity`).
   - Execution 2 (Task-146) failed with exit code 1 (`FAILED (errors=2, skipped=254)`) due to:
     a) `frappe.exceptions.LinkValidationError: Could not find Customer: _TEST_ISM_Cust_b5a2ffb1, Vehicle: 5YJ3E1EBXNFB93E83` in `TestTeslaIntegration.test_symmetrical_procedure_import`.
     b) MariaDB Error 1020 (`QueryDeadlockError`) on `tabSingles` (`doctype='ISM Settings'`) during `TestISMSettings.setUpClass`.
3. **Database Leakage**: Post-test MariaDB inspection revealed persistent leaked records (`TEMP_BAY` and `TEMP_BAY_MULTI`) in `tabService Bay` that survived test suite teardown.

Per the Integrity Forensics standard ("Trust nothing — verify empirically; block on failure"), the work product contains unverified/falsified completion claims and severe runtime concurrency flaws. The verdict is **INTEGRITY VIOLATION**.

---

## 1. Observation

### 1.1 Static Code & Schema Verification
- **DocType Schemas**:
  - `Service Bay`, `Equipment Tag`, `Service Bay Equipment`, `Schedule Entry Type`, `Schedule Entry`, and `ISM Settings` are defined as standard DocTypes.
  - MariaDB InnoDB tables (`tabService Bay`, `tabEquipment Tag`, `tabService Bay Equipment`, `tabSchedule Entry Type`, `tabSchedule Entry`) were verified in `devcontainer-mariadb-1`. Indexes and constraints match JSON definitions (`bay_name` UNIQUE, `type_name` UNIQUE, `tag_name` UNIQUE, `scheduled_date` INDEX, `repair_order` INDEX).
  - Seed records in `tabSchedule Entry Type` ("Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service") and `tabEquipment Tag` ("Lift", "EV High Voltage", "Alignment Rack", "HV Battery Station") are genuine.
  - Static fixtures `Test Customer` and `5YJSA1E26CF000001` exist and are properly linked.
- **Controller Logic**:
  - `service_bay.py`, `equipment_tag.py`, `schedule_entry_type.py`, `schedule_entry.py`, and `ism_settings.py` implement genuine domain validations without dummy returns, facade bypasses, or hardcoded return strings.

---

### 1.2 Empirical Test Execution Findings

#### Empirical Finding 1: 10x Sequential Stress Loop on `Schedule Entry` Crashes on Run 7
- **Command Executed**:
  ```powershell
  1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
  ```
- **Task ID**: `903706c5-a2a1-4656-bef9-0573ef8a068a/task-118`
- **Result**: Runs 1–6 succeeded. Run 7 **FAILED** with exit code 1.
- **Verbatim Error Traceback**:
  ```text
  === RUN 7 ===
  Running 7 unspecified-category tests for ism

  ism.ism.doctype.schedule_entry.test_schedule_entry.TestScheduleEntry
      ✔  test_auto_population_from_repair_order
      ✔  test_estimated_duration_must_be_positive
      ✔  test_multiple_schedule_entries_per_repair_order
      ✔  test_populate_from_repair_order_on_update
      ✖  test_provisional_contact_and_vehicle_display
      ✔  test_requires_repair_order_validation
      ✔  test_schedule_entry_naming_series

  ======================================================================
   ERROR  test_provisional_contact_and_vehicle_display (ism.ism.doctype.schedule_entry.test_schedule_entry.TestScheduleEntry.test_provisional_contact_and_vehicle_display)
  Verify display fallbacks for provisional customer and vehicle intake.
  ----------------------------------------------------------------------
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 272, in sql
      self.execute_query(query, values)
      query = 'SELECT `current` FROM `tabSeries` WHERE `name`=%(param1)s FOR UPDATE'
      values = {'param1': 'SE-'}
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 372, in execute_query
      return self._cursor.execute(query, values)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 179, in execute
      res = self._query(mogrified_query)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 330, in _query
      db.query(q)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
      _mysql.connection.query(self, query)
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")

  The above exception was the direct cause of the following exception:

  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/schedule_entry/test_schedule_entry.py", line 163, in test_provisional_contact_and_vehicle_display
      }).insert(ignore_permissions=True)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 458, in insert
      self.set_new_name(set_name=set_name, set_child_names=set_child_names)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/naming.py", line 412, in getseries
      current = (frappe.qb.from_(series).where(series.name == key).for_update().select("current")).run()
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
      raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
  ----------------------------------------------------------------------
  Ran 7 tests in 4.041s
  FAILED (errors=1)
  ```

---

#### Empirical Finding 2: Full Application Test Suite (`bench run-tests --app ism`) Execution 1 Fails
- **Command Executed**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Task ID**: `903706c5-a2a1-4656-bef9-0573ef8a068a/task-130`
- **Result**: Exit code 1 (`FAILED (errors=1, skipped=254)`).
- **Verbatim Error Traceback**:
  ```text
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")

  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/test_scheduling_capacity.py", line 177, in test_tier1_f7_schedule_entry_overlap_validation_error
      create_test_schedule_entry(
          repair_order=self.test_ro1.name,
          date=target_date,
          time_val="08:00:00",
          duration=120,
          service_bay=self.bay1,
      )
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/test_fixtures.py", line 338, in create_test_schedule_entry
      doc = frappe.get_doc(data).insert(ignore_permissions=True)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 458, in insert
      self.set_new_name(set_name=set_name, set_child_names=set_child_names)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/naming.py", line 412, in getseries
      current = (frappe.qb.from_(series).where(series.name == key).for_update().select("current")).run()
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
      raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
  ----------------------------------------------------------------------
  Ran 381 tests in 34.175s
  FAILED (errors=1, skipped=254)
  ```

---

#### Empirical Finding 3: Full Application Test Suite Execution 2 Fails with 2 Distinct Errors
- **Command Executed**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
  ```
- **Task ID**: `903706c5-a2a1-4656-bef9-0573ef8a068a/task-146`
- **Result**: Exit code 1 (`FAILED (errors=2, skipped=254)`).
- **Verbatim Error 1 (Cross-Test Fixture Contamination)**:
  ```text
  ERROR: test_symmetrical_procedure_import (ism.tests.test_tesla_integration.TestTeslaIntegration.test_symmetrical_procedure_import)
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/test_tesla_integration.py", line 231, in test_symmetrical_procedure_import
      ro = self.create_test_repair_order(customer=cust.name, vehicle=veh.name)
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/base.py", line 126, in create_test_repair_order
      doc = frappe.get_doc({...}).insert(ignore_permissions=True)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 324, in insert
      self.run_before_save_methods()
    ...
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/base_document.py", line 630, in validate_link
      frappe.throw(_("Could not find {0}").format(msg), frappe.LinkValidationError)
  frappe.exceptions.LinkValidationError: Could not find Customer: _TEST_ISM_Cust_b5a2ffb1, Vehicle: 5YJ3E1EBXNFB93E83
  ```
- **Verbatim Error 2 (tabSingles Divergence Deadlock / Error 1020)**:
  ```text
  ERROR: setUpClass (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings)
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 372, in execute_query
      return self._cursor.execute(query, values)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 330, in _query
      db.query(q)
    File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
      _mysql.connection.query(self, query)
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

  The above exception was the direct cause of the following exception:

  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 59, in setUpClass
      frappe.db.set_single_value("ISM Settings", divergent)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 870, in set_single_value
      frappe.db.delete("Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
      raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  ----------------------------------------------------------------------
  Ran 373 tests in 30.864s
  FAILED (errors=2, skipped=254)
  ```

---

#### Empirical Finding 4: Database Leakage of Residual Test Service Bays
- **Query Executed**:
  ```bash
  wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print(frappe.get_all('Service Bay', fields=['name', 'bay_name', 'is_active']))"
  ```
- **Direct Output**:
  ```json
  [
    {"name": "TEMP_BAY", "bay_name": "TEMP_BAY", "is_active": 1},
    {"name": "TEMP_BAY_MULTI", "bay_name": "TEMP_BAY_MULTI", "is_active": 1},
    {"name": "Test Bay 1", "bay_name": "Test Bay 1", "is_active": 1},
    {"name": "Test Bay 2", "bay_name": "Test Bay 2", "is_active": 1},
    {"name": "Test Bay 3", "bay_name": "Test Bay 3", "is_active": 0}
  ]
  ```
- **Violation**: `TEMP_BAY` (created at `2026-10-27 05:47:10`) and `TEMP_BAY_MULTI` (created at `2026-10-27 05:48:02`) persisted uncleaned in `tabService Bay` following full suite execution.

---

## 2. Logic Chain

1. **Falsification of Zero-Deadlock Claim on `tabSeries`**:
   - `m1_worker_5` claimed that MariaDB Error 1020 on `tabSeries` was completely resolved by adding `frappe.db.rollback()` to `test_tesla_integration.py`.
   - In reality, `tabSeries` deadlocks occur on `Schedule Entry` series generation (`SE-`) inside `frappe.model.naming.getseries()`.
   - When `create_test_schedule_entry()` in `ism/tests/test_fixtures.py:338` or `test_provisional_contact_and_vehicle_display` in `test_schedule_entry.py:163` executes `insert()`, no transaction restart retry mechanism exists.
   - Consequently, when concurrent database commits from background workers (`bench worker`) or preceding test fixtures change `tabSeries`, `SELECT ... FOR UPDATE` raises MariaDB error 1020, crashing both the stress loop (Run 7) and the full application suite (Task-130).

2. **Falsification of `tabSingles` Zero-Churn Fix**:
   - `m1_worker_5` implemented a conditional baseline check in `test_ism_settings.py:setUpClass` to prevent updating `tabSingles` when settings match defaults.
   - However, during the full test suite, preceding test classes (e.g. `TestEstimationMath` and `TestSchedulingCapacity` via `test_fixtures.py`) mutate `ISM Settings` (setting `buffer_padding_minutes = 10`, `default_slot_interval = 15`, etc.).
   - When `TestISMSettings.setUpClass` runs, it detects this divergence and calls `frappe.db.set_single_value("ISM Settings", divergent)`.
   - `set_single_value` attempts to `DELETE FROM tabSingles`, immediately triggering MariaDB Error 1020 / `QueryDeadlockError` because `set_single_value` has no retry loop and operates under a stale read view.
   - Thus, the claim that `test_ism_settings.py` is immune to error 1020 is empirically false whenever it follows other test suites.

3. **Persistent Test Contamination (`LinkValidationError`)**:
   - `TestTeslaIntegration.test_symmetrical_procedure_import` failed with `LinkValidationError: Could not find Customer: _TEST_ISM_Cust_b5a2ffb1, Vehicle: 5YJ3E1EBXNFB93E83` during full suite execution.
   - This demonstrates that transaction boundary rollbacks in `base.py` and `cleanup.py` are prematurely rolling back or corrupting records created within test methods.

4. **Conclusion**:
   - Acceptance Criteria state: *"Automated test suite passes all tests via bench run-tests --app ism"*.
   - DISPATCH.md mandates: *"Verify absence of MariaDB error 1020 during stress tests and full suite runs"*.
   - Both criteria failed repeatedly under independent testing.

---

## 3. Caveats

- **Isolated DocType Pass Rates**: In clean, single-run execution isolated from the full suite, `Service Bay` (5/5), `Schedule Entry Type` (8/8), and `ISM Settings` (8/8) pass without failure. The failures manifest specifically under sustained iteration loops (stress testing) and cross-suite execution (`bench run-tests --app ism`).
- **Core Frappe Purity**: All detected bugs are in the test harness (`test_fixtures.py`, `test_ism_settings.py`, `cleanup.py`, `base.py`), not in Frappe Framework core.
- **Development Mode Scope**: The static source code does not contain hardcoded fake outputs or facade mocks; the violation is behavioral instability and unverified completion claims.

---

## 4. Conclusion

**Verdict**: **INTEGRITY VIOLATION**

Milestone 1 work product cannot be approved at this gate. The worker's claim of 100% verified clean execution is refuted by empirical test failures:
1. `Schedule Entry` 10x stress loop: **FAILED** (Run 7, exit code 1, MariaDB error 1020).
2. Full application test suite (`bench run-tests --app ism`): **FAILED** across consecutive independent runs (exit code 1, MariaDB error 1020 on `tabSeries` and `tabSingles`, plus `LinkValidationError`).
3. Database hygiene: **FAILED** (`TEMP_BAY` and `TEMP_BAY_MULTI` leaked in `tabService Bay`).

### Mandatory Remediation Actions Required:
1. **Wrap `create_test_schedule_entry` and `Schedule Entry.insert()` in 3-attempt restart loop**: In `ism/tests/test_fixtures.py` and `ism/ism/doctype/schedule_entry/test_schedule_entry.py`, catch MariaDB 1020 / QueryDeadlockError and restart the transaction with `frappe.db.rollback()` before retrying.
2. **Wrap `test_ism_settings.py:setUpClass` baseline update in restart loop**: In `test_ism_settings.py:setUpClass`, wrap `frappe.db.set_single_value("ISM Settings", divergent)` in a 3-attempt retry loop on error 1020 with `frappe.db.rollback()`.
3. **Synchronize default settings across test fixtures**: Align `test_fixtures.py` default settings (`buffer_padding_minutes = 0`) with `setup.py` and `test_ism_settings.py` so that preceding tests do not leave `ISM Settings` in a divergent state.
4. **Purge `TEMP_BAY` and `TEMP_BAY_MULTI`**: Add `TEMP_BAY` patterns to `_cleanup_service_bays()` in `ism/tests/cleanup.py` and delete existing leaked rows.
5. **Fix Customer/Vehicle link persistence in `test_tesla_integration.py`**: Ensure `cust` and `veh` are committed or transactionally preserved prior to inserting the test Repair Order.

---

## 5. Verification Method

To independently reproduce the failures observed during this audit:

1. **Reproduce `Schedule Entry` 10x Stress Loop Failure**:
   ```powershell
   1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
   ```
   *Observed Failure*: Run 7 throws `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")` on `test_provisional_contact_and_vehicle_display`.

2. **Reproduce Full Application Test Suite Failure**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
   ```
   *Observed Failure*: Exits with code 1, `FAILED (errors=1 or 2, skipped=254)`.

3. **Verify Leaked Service Bays in Database**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print(frappe.get_all('Service Bay', fields=['name', 'bay_name', 'is_active']))"
   ```
   *Observed Output*: Lists `TEMP_BAY` and `TEMP_BAY_MULTI` alongside standard test bays.
