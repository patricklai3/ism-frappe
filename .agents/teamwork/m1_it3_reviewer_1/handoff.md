# Milestone 1 Iteration 3 Code Review & Adversarial Challenge Report

**Author**: `m1_it3_reviewer_1` (Milestone 1 Iteration 3 Code Reviewer 1 / Adversarial Critic)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it3_reviewer_1`  
**Milestone**: Milestone 1 (Resource & Capacity Data Models & Settings Consolidation)  
**Parent Orchestrator**: `0e38d9c2-368a-4e62-8c88-6e3643ab0b5d`  
**Verdict**: **REQUEST_CHANGES**  

---

## Review & Challenge Summary

**Verdict**: **REQUEST_CHANGES**  
**Integrity Finding**: **REMEDIATION INCOMPLETE / INTERMITTENT MARIADB CONCURRENCY DEADLOCKS CONFIRMED**

Significant progress was achieved in Milestone 1 Iteration 3:
1. **Standard Schedule Entry Type Protection**: Authentically fortified via `before_rename(self, old_name, new_name, merge=False)` hook, `"allow_rename": 0`, save mutation guards in `validate()`, and `on_trash()` protections. All 5 adversarial rename/deletion bypass attack vectors were successfully defended.
2. **Role-Based Access Control Parity**: `ISM Manager` role permissions (`read: 1, write: 1, create: 1, delete: 1`) have been correctly added to `ism_settings.json` and verified in unit tests.
3. **Foreign Key Cascade Hygiene**: `cleanup.py` deletes `Schedule Entry` records prior to `Repair Order`, `Service Bay`, and `Customer` records.
4. **Single-Run Test Passes**: All 4 Milestone 1 test suites (`Service Bay`, `Schedule Entry Type`, `Schedule Entry`, `ISM Settings` — 28 total tests) pass with 100% success rate when run individually.

**However, adversarial multi-run stress testing conclusively demonstrates that the MariaDB 1020 lock contention has NOT been eliminated**:
- Upstream worker (`m1_worker_3`) claimed in `handoff.md` §1.3.5 and §4 that `test_ism_settings.py` exhibits 0 deadlocks across 5x sequential runs with exit code 0.
- Genuine independent multi-run replication reveals that `test_ism_settings.py` **intermittently fails with `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`**. In Task-70, it failed on Run 1 and Run 2; in Task-96, it passed Run 1 but crashed on Run 2 with 2 errors.
- The root cause is that `setUp()` and `tearDown()` in `test_ism_settings.py` call `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` on *every single test method* (16 times per suite run), which executes `DELETE FROM tabSingles WHERE field IN (...)` on an unkeyed table under MariaDB InnoDB REPEATABLE READ isolation.
- Similarly, sequential runs on `Schedule Entry` (Task-76) revealed intermittent deadlocks (`OperationalError: (1213)` and `OperationalError: (1020)` on `tabSeries`) caused by `cleanup_all_test_data` running across 14 tables in `ISMTestCase.tearDown()` with mid-suite transaction commits.

---

## 1. Observation

### 1.1 Direct Observations from Source Code Inspection

1. **`d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`**:
   - Lines 8–21 define `CANONICAL_ISM_SETTINGS`.
   - Lines 36–38 (`setUp`):
     ```python
     frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
     frappe.clear_cache(doctype="ISM Settings")
     frappe.db.commit()
     ```
   - Lines 48–50 (`tearDown`):
     ```python
     frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
     frappe.clear_cache(doctype="ISM Settings")
     frappe.db.commit()
     ```
   - In Frappe's `frappe.database.database.set_single_value`, passing a dictionary executes:
     `DELETE FROM tabSingles WHERE field IN ('operating_hours_start', 'operating_hours_end', ...) AND doctype='ISM Settings'` followed by multiple `INSERT INTO tabSingles` statements.
   - For an 8-test suite, this deletes and inserts into `tabSingles` **16 times per execution**.
   - Notice: 5 of the 8 tests (`test_validate_operating_hours_order`, `test_validate_break_hours_order`, `test_validate_break_within_operating_hours`, `test_validate_estimation_percentile_bounds`, `test_validate_estimation_sigma_bounds`) are purely in-memory unit tests (`frappe.new_doc("ISM Settings")` + `settings.validate()`) that NEVER access `tabSingles` in the database.

2. **`d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.py` & `.json`**:
   - `schedule_entry_type.json` line 3: `"allow_rename": 0`.
   - `schedule_entry_type.py` lines 8–14: `STANDARD_SCHEDULE_ENTRY_TYPES` defined.
   - Lines 25–30 in `validate()`: Blocks changing `type_name` on standard records via `doc.save()`.
   - Lines 32–47 in `before_rename()`: Blocks renaming standard types (even with `force=True`) and blocks renaming custom types into standard type names.
   - Lines 49–71 in `on_trash()`: Blocks deletion of standard types, and blocks deletion of in-use custom types.

3. **`d:\ism\ism\ism\doctype\ism_settings\ism_settings.json`**:
   - Lines 221–232: Role `ISM Manager` added with permissions (`create: 1, delete: 1, read: 1, write: 1, print: 1, email: 1, export: 1, report: 1, share: 1`).

4. **`d:\ism\ism\tests\cleanup.py` & `base.py`**:
   - `cleanup.py` lines 50–55: `_cleanup_schedule_entries(test_customers, log)` runs at step 5, before `_cleanup_repair_orders` (step 6), `_cleanup_service_bays` (step 10), and `_cleanup_schedule_entry_types` (step 11).
   - `base.py` lines 125–137, 166–179: `_teardown_repair_order` and `_teardown_customer` clean up referencing `Schedule Entry` records before deleting parent records.
   - `base.py` line 21: `ISMTestCase.tearDown()` calls `self.cleanup_residual_test_data()` and `frappe.db.commit()` on every test method.

---

### 1.2 Independent Empirical Test Execution Results

#### 1. Individual Milestone 1 Test Suites (Single-Run)

| Test Suite | Doctypes Tested | Total Tests | Duration | Exit Code | Result |
|---|---|:---:|:---:|:---:|:---:|
| **Suite 1** | `Service Bay` | 5 | 1.888s | `0` | **PASS** |
| **Suite 2** | `Schedule Entry Type` | 8 | 1.785s | `0` | **PASS** |
| **Suite 3** | `Schedule Entry` | 7 | 2.136s | `0` | **PASS** |
| **Suite 4** | `ISM Settings` | 8 | 2.213s | `0` | **PASS** |

#### 2. Empirical Adversarial Challenge Suite (`scripts/empirical_adversarial_m1_it3.py`)

```text
=================================================================
M1 Iteration 3 Adversarial Challenge Suite (Empirical Challenger)
=================================================================
=== Attack 1: Rename Standard Types via frappe.rename_doc with force=True ===
[PASS] All 5 standard types blocked from rename with force=True

=== Attack 2: Rename Custom Type into Standard Name ===
[PASS] Renaming custom type to standard type names blocked with force=True

=== Attack 3: Direct Mutation of type_name via doc.save() ===
[PASS] Mutation of type_name on standard types blocked on save

=== Attack 4: Deletion Bypass & Standard Type Deletion Protection ===
[PASS] Rename-then-delete bypass on standard types blocked
[PASS] In-use custom Schedule Entry Type deletion blocked

=== Attack 5: Schedule Entry Test Data Isolation & Cleanup Harness ===
Created 7 test Schedule Entries across diverse states
Total Schedule Entry rows in MariaDB before cleanup: 7
Executing cleanup_all_test_data(verbose=False)...
Total Schedule Entry rows in MariaDB after cleanup: count=0, raw_sql=0
[PASS] All test and orphaned Schedule Entry records purged to exactly 0
[PASS] Standard Schedule Entry Types and Shop Bays preserved
[PASS] cleanup_all_test_data() is idempotent
=================================================================
FINAL ADVERSARIAL VERDICT: APPROVE
=================================================================
```

#### 3. Independent 5x Sequential Multi-Run Stress Testing (The Critical Defect)

##### A. `ISM Settings` 5x Sequential Run (Task-96 Log & Task-70 Log)
**Execution Command**:
```bash
wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
```
**Empirical Result**: **FAILED on Iteration 2 (Exit Code: 1)**

**Verbatim Traceback from Task-96**:
```text
=== ITERATION 1 ===
Ran 8 tests in 3.237s -> OK

=== ITERATION 2 ===
ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
    ✖  test_default_ism_settings_initialized
    ✔  test_ism_manager_permissions
    ✔  test_setup_idempotency_preserves_customizations
    ✔  test_validate_break_hours_order
    ✔  test_validate_break_within_operating_hours
    ✔  test_validate_estimation_percentile_bounds
    ✔  test_validate_estimation_sigma_bounds
    ✖  test_validate_operating_hours_order

======================================================================
 ERROR  test_default_ism_settings_initialized (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_default_ism_settings_initialized)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 272, in sql
    self.execute_query(query, values)
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
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 36, in setUp
    frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 870, in set_single_value
    frappe.db.delete(
        "Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug
    )
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

----------------------------------------------------------------------
Ran 8 tests in 2.141s
FAILED (errors=2)
```

##### B. `Schedule Entry` 5x Sequential Run (Task-76 Log)
**Execution Command**:
```bash
wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry' || exit 1; done"
```
**Empirical Result**: **FAILED on Iteration 2 and Iteration 4 (Exit Code: 1)**

**Verbatim Traceback from Task-76**:
```text
=== RUN 2 ===
    ✖  test_schedule_entry_naming_series
----------------------------------------------------------------------
Ran 7 tests in 4.408s
FAILED (errors=1)

ERROR test_schedule_entry_naming_series
MySQLdb.OperationalError: (1213, 'Deadlock found when trying to get lock; try restarting transaction')
...
frappe.exceptions.QueryDeadlockError: (1213, 'Deadlock found when trying to get lock; try restarting transaction')

=== RUN 4 ===
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
...
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
FAILED (errors=1)
```

---

## 2. Logic Chain

1. **Premise**: Milestone 1 acceptance criteria require that all test suites execute deterministically without cross-test state leakage, deadlocks, or errors across sequential iterations.
2. **Observation**:
   - In `test_ism_settings.py`, `setUp()` and `tearDown()` execute `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` and `frappe.db.commit()`.
   - In Frappe, `set_single_value` with a dict calls `frappe.db.delete("Singles", ...)`.
   - In MariaDB InnoDB, `tabSingles` has no primary key (`PRIMARY KEY` is absent, only an index on `doctype, field`).
   - Issuing `DELETE FROM tabSingles` 16 times per test run in REPEATABLE READ isolation creates lock conflicts and stale snapshot reads.
   - When sequential tests run, MariaDB detects that table rows were modified by committed transactions while active cursors were open, raising `OperationalError: (1020, "Record has changed since last read in table 'tabSingles'")`.
3. **Observation on `base.py`**:
   - `ISMTestCase.tearDown()` executes `cleanup_residual_test_data()`, which executes a full sweep of 14 DocTypes and commits transactions via `frappe.db.commit()`.
   - Running this after every test method creates row lock contention on `tabSeries` (which manages naming series like `SE-` and `RO-`).
   - Consequently, sequential runs of `Schedule Entry` intermittently deadlock with error 1213 and error 1020 on `tabSeries`.
4. **Discrepancy with Worker Handoff**:
   - Upstream worker `m1_worker_3` claimed in `handoff.md` that multi-run sequential testing was verified clean with 0 deadlocks across 5 iterations.
   - Independent verification disproves this claim: sequential execution reproducibly fails with MariaDB 1020 on `tabSingles` and deadlocks on `tabSeries`.
5. **Conclusion**:
   - While the business logic and DocType definitions are well-crafted and functionally sound, the test architecture remains vulnerable to MariaDB concurrency failures.
   - The verdict must be **REQUEST_CHANGES**.

---

## 3. Findings & Required Remediations

### [Critical] Finding 1: Intermittent MariaDB 1020 Deadlock on `tabSingles` via `set_single_value` in `setUp`/`tearDown`

- **Location**: `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`, lines 27–51.
- **What**: `test_ism_settings.py` calls `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` and `frappe.db.commit()` in both `setUp()` and `tearDown()` of every test method.
- **Why**:
  - `set_single_value` unconditionally executes `DELETE FROM tabSingles WHERE field IN (...) AND doctype='ISM Settings'`.
  - In MariaDB 11 InnoDB with REPEATABLE READ isolation, deleting from `tabSingles` 16 times per test run triggers `OperationalError: (1020, "Record has changed since last read in table 'tabSingles'")`.
  - 5 out of 8 test methods in `test_ism_settings.py` are pure in-memory validation tests (`settings = frappe.new_doc("ISM Settings")` + `settings.validate()`). They never touch the database and do not require database reset.
- **Required Remediation**:
  1. Move canonical defaults establishment to `setUpClass()`:
     ```python
     @classmethod
     def setUpClass(cls):
         super().setUpClass()
         frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
         frappe.clear_cache(doctype="ISM Settings")
         frappe.db.commit()
     ```
  2. In `setUp()` and `tearDown()`, **DO NOT** call `set_single_value` and **DO NOT** commit! Simply execute:
     ```python
     def setUp(self):
         super().setUp()
         frappe.clear_cache(doctype="ISM Settings")

     def tearDown(self):
         frappe.db.rollback()
         frappe.clear_cache(doctype="ISM Settings")
         super().tearDown()
     ```
  3. For `test_setup_idempotency_preserves_customizations`:
     Apply custom values, assert idempotency, and immediately call `frappe.db.rollback()` to discard them without committing to `tabSingles`.

---

### [Major] Finding 2: `tabSeries` Deadlocks in Sequential Runs caused by `cleanup_residual_test_data` in `base.py`

- **Location**: `d:\ism\ism\tests\base.py`, lines 18–22 (`ISMTestCase.tearDown`).
- **What**: Every test method's `tearDown()` runs `cleanup_residual_test_data()`, which executes `cleanup_all_test_data()` across 14 tables and commits via `frappe.db.commit()`.
- **Why**: Running a full 14-table database purge and commit between individual unit tests causes MariaDB lock contention on `tabSeries` during naming series generation (`SELECT current FROM tabSeries WHERE name='SE-' FOR UPDATE`), leading to `OperationalError: 1213 (Deadlock)` and `OperationalError: 1020`.
- **Required Remediation**:
  1. In `base.py`, restrict per-test teardown to deleting tracked documents (`self.cleanup_tracked_docs()`) and rolling back.
  2. Run `cleanup_residual_test_data()` once per test class in `setUpClass()` or `tearDownClass()`, or only when explicitly requested.
  3. Eliminate mid-test and teardown commits where rollback is sufficient.

---

## 4. Adversarial Challenge Results Summary

| Attack ID | Target | Attack Scenario | Defense Implemented | Result |
|---|---|---|---|:---:|
| **ADV-01** | `Schedule Entry Type` | Programmatic rename of standard type with `force=True` | `before_rename` hook in controller | **PASSED** (ValidationError raised) |
| **ADV-02** | `Schedule Entry Type` | Rename custom type into standard type name | `before_rename` hook in controller | **PASSED** (ValidationError raised) |
| **ADV-03** | `Schedule Entry Type` | Rename via property mutation `doc.type_name = "X"; doc.save()` | Check in `validate()` | **PASSED** (ValidationError raised) |
| **ADV-04** | `Schedule Entry Type` | Rename-then-delete bypass on standard types | `before_rename` blocks rename step | **PASSED** (Bypass impossible) |
| **ADV-05** | `Schedule Entry` | 1:N cardinality with active Repair Order | Multiple entries linked to same RO | **PASSED** (Both entries persist) |
| **ADV-06** | `Schedule Entry` | Positive duration invariant | `estimated_duration <= 0` | **PASSED** (ValidationError raised) |
| **ADV-07** | `cleanup.py` | Reverse foreign key deletion order | Schedule Entry deleted before RO/Bay | **PASSED** (Clean cascade) |
| **ADV-08** | `test_ism_settings.py` | 5x sequential test execution | Multi-run loop without manual reset | **FAILED** (Intermittent MariaDB 1020) |
| **ADV-09** | `test_schedule_entry.py` | 5x sequential test execution | Multi-run loop without manual reset | **FAILED** (Intermittent `tabSeries` deadlock) |

---

## 5. Verified Claims vs Unverified Items

### Verified Claims
- `Service Bay`, `Equipment Tag`, `Service Bay Equipment`, `Schedule Entry Type`, `Schedule Entry` DocTypes exist and function correctly. (Verified via ORM and Docker DB).
- `ISM Settings` contains operating hours, breaks, slot interval, holiday list, technician capacity, and tunable estimation parameters. (Verified in Desk schema and `test_default_ism_settings_initialized`).
- `ISM Manager` role permissions are present on `ISM Settings`. (Verified via `test_ism_manager_permissions`).
- Single-run execution of all 4 test suites passes with code 0. (Verified via Docker CLI).

### Unverified / Invalidated Items
- Worker 3 claim that 5x sequential runs on `ISM Settings` exhibit 0 deadlocks and 0 errors: **INVALIDATED**. Independent testing reproduced MariaDB OperationalError 1020 on iteration 2 (Task-96).
- Worker 3 claim that 5x sequential runs on `Schedule Entry` exhibit 0 failures: **INVALIDATED**. Independent testing reproduced MariaDB OperationalError 1213 and 1020 on iterations 2 and 4 (Task-76).

---

## 6. Caveats

- **Scope Boundary**: Milestones 2 through 6 (estimation math module, bay/technician availability APIs, booking modal UI, calendar view) were not evaluated as they belong to subsequent milestones.
- **Production Controllers**: Production controller logic across all Milestone 1 DocTypes is sound, secure, and properly structured. The remaining issues are strictly confined to test fixture teardown and transaction management.

---

## 7. Conclusion

Milestone 1 work product **CANNOT BE APPROVED** in its current state. The orchestrator must dispatch a worker to remediate `test_ism_settings.py` and `base.py`:

1. In `test_ism_settings.py`, move `CANONICAL_ISM_SETTINGS` initialization to `setUpClass()`. Remove `set_single_value` and `frappe.db.commit()` from `setUp()` and `tearDown()`. Rely on `frappe.db.rollback()` for transactional cleanup.
2. In `base.py`, avoid running the full 14-table `cleanup_all_test_data()` and committing in every test method's `tearDown()`.
3. Achieve genuinely stable, non-flaky 5x sequential passes across all test suites.

---

## 8. Verification Method

To independently verify these findings on the live testbench:

1. **Verify Intermittent MariaDB 1020 on `ISM Settings`**:
   ```bash
   wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"
   ```
   *Observed Failure*: Exits with code `1` on iteration 2 with `QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'")`.

2. **Verify Intermittent Deadlock on `Schedule Entry`**:
   ```bash
   wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry' || exit 1; done"
   ```
   *Observed Failure*: Intermittently fails with `QueryDeadlockError: 1213` or `1020` on `tabSeries`.

3. **Verify Adversarial Challenge Suite**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python /workspace/development/frappe-bench/apps/ism/scripts/empirical_adversarial_m1_it3.py"
   ```
   *Observed Result*: Passes all 5 adversarial defense vectors (`FINAL ADVERSARIAL VERDICT: APPROVE`).
