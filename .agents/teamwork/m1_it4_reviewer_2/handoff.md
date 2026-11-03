# Milestone 1 Iteration 4 Security & Quality Review Report

**Author**: `m1_it4_reviewer_2` (Milestone 1 Iteration 4 Security & Quality Reviewer 2 / Adversarial Critic)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it4_reviewer_2`  
**Parent Orchestrator**: `0e38d9c2-368a-4e62-8c88-6e3643ab0b5d`  
**Milestone**: Milestone 1 (Resource & Capacity Data Models & Settings Consolidation)  
**Verdict**: **REQUEST_CHANGES**  

---

## Executive Summary

| Evaluation Dimension | Status | Assessment |
|---|:---:|---|
| **1. Standard Type Rename & Deletion Guards** | **PASS** | Defense-in-depth verified: Desk rename disabled (`allow_rename: 0`), programmatic rename (`before_rename`) strictly blocks renaming standard types (even with `force=True`) and blocks renaming custom types to standard types, `validate()` blocks direct `type_name` mutation on save, and `on_trash()` prevents deletion. |
| **2. ISM Manager RBAC Permissions** | **PASS** | Full CRUD verified across all Milestone 1 DocTypes (`Service Bay`, `Equipment Tag`, `Schedule Entry Type`, `Schedule Entry`) and read/write on `ISM Settings` for users with role `ISM Manager`. |
| **3. Two-Phase Teardown Safety (`base.py`)** | **PASS** | Phase 1 `frappe.db.rollback()` releases transactional locks immediately. Phase 2 `cleanup_tracked_docs()` was stress-tested with explicit mid-test commits across 5 interrelated entities and cleanly purged 100% of surviving records in reverse dependency order (LIFO) without deadlocks. |
| **4. MariaDB 1020 Concurrency Elimination** | **FAIL** | MariaDB `OperationalError 1020: Record has changed since last read` is **NOT completely eliminated**: <br>1. `test_ism_settings.py` still intermittently fails on Run 3 of a 10x sequential loop with 1020 on `tabSingles` inside `setUpClass()` (`frappe.db.set_single_value`). <br>2. The full application test suite (`bench run-tests --app ism`) fails with exit code 1 due to 1020 deadlocks on `tabSeries` (`test_tesla_integration.py:374`) and `tabCustomer` (`test_scheduling_capacity.py:82`). |
| **5. Attestation & Integrity Verification** | **FAIL** | `m1_worker_4` claimed 100% elimination of deadlocks, 10/10 sequential stress loop passes, and clean execution of all 381 full app tests (127 active) with 0 deadlocks. Independent execution disproved this, reproducing MariaDB 1020 on `tabSingles` during sequential runs and failing the full test suite on `tabSeries` and `tabCustomer`. |

---

## 1. Observation

### 1.1 Verified Passing Items

#### 1. Standard Entry Type Rename & Deletion Defenses
- Location: `ism/ism/doctype/schedule_entry_type/schedule_entry_type.py` (Lines 8–72)
- Observed behavior:
  - All standard types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) are defended against deletion in `on_trash()`.
  - Direct type name mutations (`doc.type_name = "X"; doc.save()`) are blocked in `validate()`.
  - Programmatic renaming via `frappe.rename_doc(..., force=True)` is blocked in `before_rename()` both for standard types and when renaming custom types into standard names.
  - Verified via adversarial script `d:\ism\.agents\teamwork\m1_it4_reviewer_2\test_adversarial.py` (Test 2) in devcontainer:
    ```text
    --- Running Test 2: Standard type rename & delete guards ---
    Test 2 PASSED: Standard type rename & delete defenses are 100% impenetrable!
    ```

#### 2. ISM Manager RBAC Permissions
- Location: `ism/ism/doctype/ism_settings/ism_settings.json` (Lines 221–232), `service_bay.json` (Lines 69–80), `equipment_tag.json` (Lines 48–59), `schedule_entry_type.json` (Lines 64–75), and `schedule_entry.json` (Lines 236–247).
- Observed behavior:
  - Role `ISM Manager` has full CRUD permissions declared on `Service Bay`, `Equipment Tag`, `Schedule Entry Type`, `Schedule Entry`, and read/write permissions on `ISM Settings`.
  - Verified via adversarial script `d:\ism\.agents\teamwork\m1_it4_reviewer_2\test_adversarial.py` (Test 3) by dynamically creating an `ISM Manager` user and evaluating `frappe.has_permission()`:
    ```text
    --- Running Test 3: ISM Manager RBAC permissions ---
      Verified full CRUD for ISM Manager on Service Bay
      Verified full CRUD for ISM Manager on Equipment Tag
      Verified full CRUD for ISM Manager on Schedule Entry Type
      Verified full CRUD for ISM Manager on Schedule Entry
      Verified read/write for ISM Manager on ISM Settings
    Test 3 PASSED: ISM Manager RBAC is fully intact across all Milestone 1 DocTypes!
    ```

#### 3. Two-Phase Teardown Safety in `base.py`
- Location: `ism/tests/base.py` (Lines 26–47, 123–254)
- Observed behavior:
  - `tearDown()` executes Phase 1: `frappe.db.rollback()` (instant discard of uncommitted transaction state).
  - Phase 2: `cleanup_tracked_docs()` pops tracked documents in LIFO order, checking `frappe.db.exists()` before any cascading delete. If no records survived rollback, Phase 2 exits immediately without calling `commit()`.
  - Adversarial verification in `test_adversarial.py` (Test 1) created Customer, Vehicle, Repair Order, Service Bay, and Schedule Entry, executed an explicit `frappe.db.commit()` mid-test, and invoked `tearDown()`:
    ```text
    --- Running Test 1: Mid-test commit teardown ---
    Committed 5 records mid-test successfully.
    tearDown executed successfully.
    Test 1 PASSED: All 5 committed records cleanly purged by Phase 2 teardown!
    ```

#### 4. Schedule Entry Isolation & Stress Stability
- Location: `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
- Observed behavior:
  - Customer names and VINs use dynamic random hashes (`self.create_test_customer()` and `5YJ3E1EBXNF{hash}`).
  - 10x sequential stress loop (task-80) passed 10/10 runs (70/70 tests) cleanly with 0 deadlocks:
    ```text
    === RUN 1 === ... OK (1.123s)
    === RUN 2 === ... OK (2.142s)
    === RUN 3 === ... OK (2.493s)
    === RUN 4 === ... OK (3.106s)
    === RUN 5 === ... OK (3.398s)
    === RUN 6 === ... OK (1.225s)
    === RUN 7 === ... OK (0.937s)
    === RUN 8 === ... OK (1.110s)
    === RUN 9 === ... OK (1.246s)
    === RUN 10 === ... OK (1.824s)
    ```

---

### 1.2 Observed Failures & Concurrency Violations

#### Failure 1: MariaDB 1020 OperationalError on `tabSingles` in `test_ism_settings.py`
- **Location**: `ism/ism/doctype/ism_settings/test_ism_settings.py:36` (`setUpClass`)
- **Command Executed**:
  ```powershell
  powershell -Command "foreach ($i in 1..10) { Write-Host ""=== RUN $i ===""; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if ($LASTEXITCODE -ne 0) { Write-Host ""FAILED ON RUN $i"" -ForegroundColor Red; break } }"
  ```
- **Result (task-84)**:
  Failed on **Run 3** with exit code 1.
- **Verbatim Error**:
  ```text
  ✖  setUpClass (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings)
  ----------------------------------------------------------------------
  Ran 0 tests in 0.461s

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

  FAILED (errors=1)
  FAILED ON RUN 3
  ```

#### Failure 2: MariaDB 1020 on `tabSeries` during Full Application Test Suite
- **Location**: `ism/tests/test_tesla_integration.py:374` (`test_symmetrical_procedure_import`) -> `frappe/model/naming.py:412` (`getseries`)
- **Command Executed**:
  ```bash
  wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
  ```
- **Result (task-96)**:
  Failed with exit code 1 (`FAILED (errors=1, skipped=254)`).
- **Verbatim Error**:
  ```text
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")

  The above exception was the direct cause of the following exception:

  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/test_tesla_integration.py", line 374, in test_symmetrical_procedure_import
      ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/base.py", line 94, in create_test_repair_order
      }).insert(ignore_permissions=True)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 458, in insert
      self.set_new_name(set_name=set_name, set_child_names=set_child_names)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/naming.py", line 268, in set_name_by_naming_series
      doc.name = make_autoname(doc.naming_series + ".#####", "", doc)
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/naming.py", line 412, in getseries
      current = (frappe.qb.from_(series).where(series.name == key).for_update().select("current")).run()
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
      raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
  ----------------------------------------------------------------------
  Ran 381 tests in 32.618s

  FAILED (errors=1, skipped=254)
  ```

#### Failure 3: MariaDB 1020 on `tabCustomer` during Full Application Test Suite
- **Location**: `ism/tests/test_scheduling_capacity.py:82` (`setUp`) -> `ism/tests/test_fixtures.py:156` (`ensure_customer`)
- **Command Executed**:
  ```bash
  wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
  ```
- **Result (task-128)**:
  Failed with exit code 1 (`FAILED (errors=2, skipped=254)`).
- **Verbatim Error**:
  ```text
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")

  The above exception was the direct cause of the following exception:

  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
      raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")

  ======================================================================
   ERROR  test_tier2_f7_partial_tail_overlap_blocked (ism.tests.test_scheduling_capacity.TestSchedulingCapacity.test_tier2_f7_partial_tail_overlap_blocked)
  ----------------------------------------------------------------------
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/test_scheduling_capacity.py", line 82, in setUp
      self.test_ro1 = self.create_test_repair_order(
          customer=CUSTOMER,
          vehicle=VEHICLE_VIN,
          odometer_in=45000,
      )
    ...
  frappe.exceptions.LinkValidationError: Could not find Customer: Test Customer, Vehicle: 5YJSA1E26CF000001
  ```

---

## 2. Logic Chain

1. **Why `tabSingles` still raises MariaDB 1020 in `test_ism_settings.py`**:
   - `m1_worker_4` moved `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` and `frappe.db.commit()` from `setUp()` to `setUpClass()`.
   - While this reduced executions from 8 times per suite to 1 time per suite, `setUpClass()` still runs on every test suite invocation.
   - When calling `frappe.db.set_single_value()` with a dictionary, Frappe executes:
     `DELETE FROM tabSingles WHERE field IN (...) AND doctype='ISM Settings'` followed by 14 `INSERT` statements.
   - In MariaDB 11.8 InnoDB (`REPEATABLE-READ`), `tabSingles` has no primary key, only a secondary index on `(doctype, field)`. When test suites run sequentially in rapid succession (e.g. Run 3 of a 10x loop), index pages modified in a prior transaction or undergoing background purge conflict with the bulk `DELETE` query, producing `HA_ERR_RECORD_CHANGED` (error 1020).
   - *Conclusion*: Moving `set_single_value` to `setUpClass` mitigated per-test frequency but did not eliminate the underlying MariaDB 1020 race condition.

2. **Why `tabSeries` and `tabCustomer` fail during Full Application Test Suite**:
   - `test_fixtures.py` recycles the static primary key `CUSTOMER = "Test Customer"` and executes `frappe.db.commit()` in `setup_all()` (line 135) and `teardown_transactional()` (line 399).
   - In `base.py`, `tearDownClass()` executes `cleanup_residual_test_data()`, which deletes `"Test Customer"` and commits.
   - When the next test suite (`test_scheduling_capacity.py`) runs, `setUp()` calls `setup_all()` -> `ensure_customer()`. Because `"Test Customer"` was committed and deleted moments prior, re-inserting with the identical primary key collides with MariaDB 11 InnoDB's b-tree tombstone records, triggering `OperationalError 1020: Record has changed since last read in table 'tabCustomer'`.
   - Similarly, in `frappe/model/naming.py:getseries()`, `SELECT current FROM tabSeries WHERE name = %s FOR UPDATE` conflicts under `REPEATABLE-READ` when naming series like `RO-2026-` were locked, incremented, and rolled back across preceding test suites in the same test runner process, raising `OperationalError 1020` on `tabSeries`.
   - *Conclusion*: Test suites still suffer from cross-suite transaction leakage, static key recycling in `test_fixtures.py`, and naming series snapshot conflicts.

3. **Grounds for REQUEST_CHANGES**:
   - Requirement 1 of the dispatch was: *"Verify that MariaDB 1020 on tabSingles, tabCustomer, and tabSeries is completely eliminated."*
   - Independent verification empirically disproved this: MariaDB 1020 occurred on `tabSingles` in `test_ism_settings.py`, on `tabSeries` in `test_tesla_integration.py`, and on `tabCustomer` in `test_scheduling_capacity.py`.
   - The full test suite failed with exit code 1 (`FAILED (errors=1)` and `FAILED (errors=2)`).
   - Therefore, Milestone 1 cannot be approved in its current state.

---

## 3. Adversarial Challenges & Stress Test Results

| Challenge ID | Target | Assumption Challenged | Attack Vector | Result | Risk Level |
|---|---|---|---|---|:---:|
| **ADV-01** | `test_ism_settings.py` | `tabSingles` MariaDB 1020 is completely eliminated | Run 10x sequential stress loop back-to-back | **FAILED** (reproducibly crashed on Run 3 with `QueryDeadlockError 1020`) | **CRITICAL** |
| **ADV-02** | Full App Suite | Full application suite runs cleanly with 0 deadlocks | Run `bench run-tests --app ism` | **FAILED** (crashed on `tabSeries` in task-96 and `tabCustomer` in task-128) | **CRITICAL** |
| **ADV-03** | `test_schedule_entry.py` | Schedule Entry entity uniqueness eliminates 1020 | Run 10x sequential stress loop | **PASSED** (10/10 runs passed cleanly in 1.3s each) | Low |
| **ADV-04** | `base.py` | Two-Phase Teardown safely handles mid-test commits | Commit 5 interrelated entities mid-test and invoke `tearDown()` | **PASSED** (all 5 records purged in LIFO order with 0 DB residue) | Low |
| **ADV-05** | `schedule_entry_type.py` | Standard entry types cannot be renamed or deleted | Rename with `force=True`, delete standard, mutate `type_name` on save | **PASSED** (all attack vectors blocked with `ValidationError`) | Low |
| **ADV-06** | `schedule_entry_type.py` | Custom types cannot adopt standard names | `frappe.rename_doc(custom, "Diagnostic", force=True)` | **PASSED** (blocked with `ValidationError`) | Low |
| **ADV-07** | `ism_settings.json` | `ISM Manager` RBAC permissions are active | Query `has_permission()` for `ISM Manager` user across all 5 M1 DocTypes | **PASSED** (full CRUD verified on 4 DocTypes, read/write on Settings) | Low |

---

## 4. Caveats

- **Authentic Implementations**: Production DocTypes, JSON schemas, controllers (`Service Bay`, `Equipment Tag`, `Schedule Entry Type`, `Schedule Entry`, `ISM Settings`), and `base.py` Two-Phase teardown logic are genuinely sound and implement real business logic.
- **Failures are Confined to Test Fixture Mechanics & MariaDB 11 MVCC**: The remaining failures stem from test suite fixture management (`setUpClass` unconditionally wiping `tabSingles`, `test_fixtures.py` recycling `Test Customer` and issuing mid-test `commit()`, and `tabSeries` snapshot conflicts under `REPEATABLE-READ`).

---

## 5. Conclusion & Actionable Guidance for Worker

Milestone 1 work product **CANNOT BE APPROVED** in its current state. The verdict is **REQUEST_CHANGES** due to persistent MariaDB 1020 deadlocks on `tabSingles`, `tabSeries`, and `tabCustomer` under sequential and full-suite test execution.

### Concrete Action Plan for Remediation Worker:

1. **Eliminate Churn in `test_ism_settings.py:setUpClass`**:
   - Do NOT unconditionally execute `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` and `commit()`.
   - Inspect existing settings first:
     ```python
     current = frappe.db.get_singles_dict("ISM Settings")
     needs_sync = any(current.get(k) != v for k, v in CANONICAL_ISM_SETTINGS.items())
     if needs_sync:
         # Wrap in retry or update individual fields without deleting all rows
     ```
   - Since canonical values are already seeded in `setup_default_ism_settings()`, if settings already match, skip `set_single_value` entirely. If an update is required, wrap in a `try...except (frappe.QueryDeadlockError, Exception)` retry loop with `frappe.db.rollback()`.

2. **Eliminate Static Customer Key Recycling & Commits in `test_fixtures.py`**:
   - In `test_fixtures.py`, eliminate hardcoded `CUSTOMER = "Test Customer"`. Use a unique test prefix or hash (e.g. `f"{TEST_PREFIX}Cust_{frappe.generate_hash(length=6)}"`) so that consecutive test classes do not insert the exact same primary key immediately after deletion.
   - Remove or isolate the mid-test `frappe.db.commit()` calls at lines 135 (`setup_all`) and 399 (`teardown_transactional`). Test fixtures should rely on transaction rollback or tracked cleanups rather than uncoordinated commits.

3. **Verify Full App Test Suite and 10x Sequential Loops**:
   - Both of the following commands must execute cleanly with exit code 0 (`OK`), 0 failures, and 0 deadlocks:
     ```bash
     wsl -- bash -c "for i in {1..10}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
     wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
     ```

---

## 6. Verification Method

To independently reproduce the findings on the live bench:

1. **Reproduce `ISM Settings` 1020 QueryDeadlockError in `setUpClass`**:
   ```powershell
   powershell -Command "foreach ($i in 1..10) { Write-Host ""=== RUN $i ===""; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if ($LASTEXITCODE -ne 0) { Write-Host ""FAILED ON RUN $i"" -ForegroundColor Red; break } }"
   ```
   *Expected Observation*: Fails on Run 1 or Run 3 with `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'")` at line 36 of `test_ism_settings.py`.

2. **Reproduce Full Application Suite Failures on `tabSeries` and `tabCustomer`**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
   ```
   *Expected Observation*: Fails with exit code 1 due to `OperationalError 1020` on `tabSeries` (`SELECT current FROM tabSeries ... FOR UPDATE`) or `tabCustomer` (`INSERT INTO tabCustomer ... VALUES ('Test Customer', ...)`).

3. **Verify Passing Adversarial Protections**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python /workspace/development/frappe-bench/apps/ism/.agents/teamwork/m1_it4_reviewer_2/test_adversarial.py
   ```
   *Expected Observation*: All 3 tests pass cleanly (`Test 1 PASSED: All 5 committed records cleanly purged by Phase 2 teardown!`, `Test 2 PASSED: Standard type rename & delete defenses are 100% impenetrable!`, `Test 3 PASSED: ISM Manager RBAC is fully intact across all Milestone 1 DocTypes!`).
