# Investigation Report & Integrated Architecture: Holistic Test Suite Stability & Test Transaction Isolation

**Work Product**: Milestone 1 Resource & Capacity Data Models, Settings Consolidation, and Test Harness (Iteration 7 Architecture Investigation)  
**Investigator**: `m1_it7_explorer_3` (Role: explorer)  
**Date**: 2026-10-27 UTC  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it7_explorer_3`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Integrity Mode**: Development Mode (authoritative per `ORIGINAL_REQUEST.md`)  
**Handoff Type**: Hard  

---

## Executive Summary

Milestone 1 Gate sign-off was rejected by Forensic Auditor `m1_it6_auditor_1` (INTEGRITY VIOLATION), Challenger `m1_it6_challenger_2` (REQUEST_CHANGES), and Reviewer `m1_it6_reviewer_1` (REQUEST_CHANGES) due to sequential stress crashes, 19 full-suite failures (`bench run-tests --app ism`), MariaDB Error 1020/1205/1213 deadlocks, and permanent leakage of 66 `Schedule Entry` records and 10 `Repair Vehicle` records into the database.

A forensic investigation into the codebase, Frappe test runner, MariaDB 11.8 transaction engine, and container runtime reveals that all observed failures stem from a single architectural anti-pattern and its coping mechanisms: **mid-test commits (`frappe.db.commit()`) and destructive inner rollbacks (`frappe.db.rollback()`) inserted into test helpers and DocType model hooks**.

When tests commit mid-test:
1. Test records survive test teardown and leak permanently into MariaDB.
2. MariaDB 11.8's Snapshot Isolation (`REPEATABLE READ` + `innodb_snapshot_isolation = 1`) invalidates the read snapshots of any open transactions across subsequent tests, triggering MariaDB Error 1020 (`ER_CHECKREAD`: *"Record has changed since last read in table 'tabSeries' / 'tabSingles'; try restarting transaction"*) whenever `SELECT ... FOR UPDATE` or `DELETE` is executed.
3. Frappe's `frappe.delete_doc` enqueues background dynamic link cleanup jobs with `enqueue_after_commit=True`. The external `bench worker` running inside the container consumes these jobs from Redis, connects to MariaDB, and commits asynchronously, colliding directly with active test suites.
4. When workers added retry loops with `frappe.db.rollback()` to "cope" with Error 1020 inside `ScheduleEntry.autoname()` and `base.py` helpers, those rollbacks wiped out uncommitted parent fixtures (e.g. `Service Bay` in `TestServiceBay` and `Inspection Template` in `TestRepairOrder`), causing phantom `LinkValidationError` and `AssertionError` failures.

This report establishes the complete evidence chain and specifies an integrated 5-pillar architecture for true atomic tests and resilient teardown without mid-test commits.

---

## 1. Observation

### 1.1 Direct Observations of Modified Code in Iteration 6

Static code inspection reveals explicit mid-test commits and inner transaction rollbacks scattered across 6 files:

1. **`ism/tests/base.py`**:
   - Lines 71, 97, 119, 137, 159: Worker 1 added `frappe.db.commit()` immediately after `.insert(ignore_permissions=True)` across all transactional helper methods:
     - Line 71: `create_test_customer()`
     - Line 97: `create_test_vehicle()`
     - Line 119: `create_test_quotation()`
     - Line 137: `create_test_repair_order()`
     - Line 159: `create_test_schedule_entry()`
   - Lines 75, 101, 123, 141, 163: Added retry loops that execute `frappe.db.rollback()` upon catching MariaDB Error 1020 or `QueryDeadlockError`.
   - Lines 238–242: Inside `cleanup_tracked_docs()`, all exceptions during teardown deletion are silently swallowed (`except Exception: pass`), followed immediately by `frappe.db.commit()`.

2. **`ism/tests/test_fixtures.py`**:
   - Lines 342, 367: Added `frappe.db.commit()` inside `create_test_schedule_entry()` and `create_test_repair_order()`.
   - Lines 346, 371: Added `frappe.db.rollback()` inside retry loops for `create_test_schedule_entry()` and `create_test_repair_order()`.
   - Line 135: `setup_all()` executes `frappe.db.commit()`.
   - Line 424: `teardown_transactional()` executes `frappe.db.commit()`!

3. **Per-Test Method Fixture Invocations in Test Suites**:
   - `ism/tests/test_ro_scheduling_e2e.py:72`: Calls `self.emp_map = setup_all()` inside `setUp()` (per-test method), committing to MariaDB before every single test!
   - `ism/tests/test_ro_scheduling_e2e.py:80`: Calls `teardown_transactional()` inside `tearDown()` (per-test method), committing to MariaDB after every single test!
   - `ism/tests/test_scheduling_capacity.py:73, 90`: Calls `setup_all()` in `setUp()` and `teardown_transactional()` in `tearDown()`, committing twice per test method across 30+ tests!
   - `ism/tests/test_estimation_math.py:48, 56`: Calls `setup_all()` in `setUp()` and `teardown_transactional()` in `tearDown()`, committing twice per test method!

4. **`ism/ism/doctype/schedule_entry/schedule_entry.py`**:
   - Lines 11–25: Added `autoname(self)` directly to the `ScheduleEntry` Document controller:
     ```python
     def autoname(self):
         """Assign naming series SE-.##### with 3-attempt transaction restart retry on MariaDB 1020."""
         from frappe.model.naming import make_autoname

         prefix = self.naming_series or "SE-.#####"
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
     Calling `frappe.db.rollback()` inside a model Document controller hook is a direct violation of Frappe architecture.

5. **`ism/ism/doctype/schedule_entry/test_schedule_entry.py`**:
   - Line 16: `setUp()` calls `frappe.db.commit()`.
   - Line 23: `_insert_entry()` calls `frappe.db.commit()`.
   - Line 27: `_insert_entry()` catches Error 1020 and calls `frappe.db.rollback()`.
   - Line 240: `test_entry_type_requires_repair_order()` calls `frappe.db.commit()`.

6. **`ism/ism/doctype/ism_settings/test_ism_settings.py`**:
   - Line 65: `setUpClass()` synchronizes divergent settings and calls `frappe.db.commit()`.
   - Line 213: `test_setup_idempotency_preserves_customizations` executes `frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)` without any retry protection or pre-operation snapshot reset.

7. **`ism/tests/cleanup.py`**:
   - Lines 434–436: Vehicle cleanup logic:
     ```python
     if v.customer in test_customers or v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]:
     ```
     If the test customer was deleted in a prior step, `v.customer` is not in `test_customers`. Since test vehicles generated by `base.py:88` have random VINs and `license_plate = NULL`, they match neither condition and are permanently skipped.

---

### 1.2 Container & Database Runtime Environment Observations

Direct command inspection of `devcontainer-frappe-1` and `devcontainer-mariadb-1` revealed critical runtime environmental factors:

1. **Active Concurrent Background Workers**:
   - Command: `wsl docker exec devcontainer-frappe-1 ps aux`
   - Observed Output:
     - PID 7: `honcho start`
     - PID 22, 25300: `frappe serve --port 8000`
     - PID 34: `frappe schedule` (active Frappe scheduler, waking periodically)
     - PID 36: `frappe worker` (`bench worker 1>> logs/worker.log 2>> logs/worker.error.log`)
   - Observation: External background worker and scheduler processes are running continuously against `development.localhost`.

2. **MariaDB Isolation Level & Snapshot Isolation**:
   - Command: `wsl docker exec devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d -e "SELECT @@tx_isolation, @@innodb_snapshot_isolation;"`
   - Output:
     ```text
     @@tx_isolation   @@innodb_snapshot_isolation
     REPEATABLE-READ  1
     ```
   - Observation: MariaDB 11.8 enforces strict Snapshot Isolation (`innodb_snapshot_isolation = 1`). Under this engine setting, MariaDB prohibits any transaction under `REPEATABLE READ` from locking or updating a row that was modified and committed by another transaction after the current transaction's read view was created, raising MariaDB Error 1020 (`ER_CHECKREAD`).

3. **Background Worker Asynchronous Interference Logged in `worker.log`**:
   - Command: `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 tail -n 50 logs/worker.log`
   - Verbatim Log Entries:
     ```text
     2026-10-27 00:56:08,174 workspace-development-frappe-bench:default: frappe.utils.background_jobs.execute_job(event=None, is_async=True, job_name='frappe.model.delete_doc.delete_dynamic_links', kwargs={'doctype': 'Customer', 'name': '_TEST_ADV_GATE_Cust_1toN'}, method='frappe.model.delete_doc.delete_dynamic_links', site='development.localhost', user='Administrator')
     2026-10-27 00:56:08,663 ... kwargs={'doctype': 'Service Bay', 'name': '_TEST_ADV_GATE_BAY_STRESS'} ...
     2026-10-27 00:56:09,152 ... kwargs={'doctype': 'Schedule Entry', 'name': 'SE-01211'} ...
     2026-10-27 00:56:10,735 ... kwargs={'doctype': 'Service Bay', 'name': '_TEST_ADV_GATE_BAY_REF'} ...
     ```
   - Observation: When `delete_doc()` is called, Frappe enqueues `delete_dynamic_links` with `enqueue_after_commit=True`. Whenever mid-test `frappe.db.commit()` occurs, these enqueued jobs are immediately submitted to Redis and executed asynchronously by `bench worker` (PID 36), performing concurrent database mutations on `development.localhost` while tests are executing.

---

### 1.3 Verbatim Empirical Failures Reported in Iteration 6

The following errors were empirically recorded during Iteration 6 audits:

1. **`ISM Settings` Sequential Stress Loop Failure (Run 3 & Run 5)**:
   - Verbatim traceback (`test_ism_settings.py:213`):
     ```text
     MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
     ...
     File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 213, in test_setup_idempotency_preserves_customizations
       frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)
     frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
     ```

2. **Full Application Test Suite Failures (Task-52 & Task-78)**:
   - Command: `bench --site development.localhost run-tests --app ism`
   - Errors:
     - 5 deadlocks on `tabSeries` (`name='RO-2026-'`) during `ro.insert()` in `TestRepairOrder` (`test_save_service_as_package`, `test_service_package_expansion_and_totals`, `test_submitted_repair_order_dynamic_lifecycle_and_supplements`, `test_symmetrical_package_and_opposite_side`, `test_unapproved_pickup_and_status_progression_blocked`).
     - Deadlock (1213) on `tabRepair Order Service` during `INSERT INTO tabRepair Order Service`.
     - Lock wait timeout (1205) on `tabCustomer`:
       `frappe.exceptions.QueryTimeoutError: (1205, 'Lock wait timeout exceeded; try restarting transaction')` during `base.py:64` `frappe.delete_doc("Customer", name)`.

3. **Phantom Fixture Vanishing Failures**:
   - `LinkValidationError` in `TestRepairOrder.test_vehicle_checking_incorporated_into_repair_order`:
     `LinkValidationError: Could not find Inspection Template: _TEST_ISM_Intake Checklist`
   - `AssertionError` in `TestServiceBay.test_service_bay_deletion_blocked_with_active_schedule_entry`:
     `AssertionError: ValidationError not raised` at `test_service_bay.py:125`.

4. **Permanent Residual Database Bleed**:
   - Direct SQL inspection post-test:
     - 66 leaked `Schedule Entry` records (`SE-00925` to `SE-00934`, etc.)
     - 10 orphaned `Repair Vehicle` records (`5YJ3E1EBXNF...`) with dangling foreign keys to deleted customers.
     - Failed cleanup: `Failed deleting test Repair Order RO-2026-00444: This document can not be deleted right now as it's being modified by another user. Please try again after some time.`

---

## 2. Logic Chain

The empirical observations connect through a rigorous, multi-hop causal chain:

```
[Mid-Test Commits in base.py, test_fixtures.py, test_schedule_entry.py]
   │
   ├──> 1. Permanently commits test rows to MariaDB -> Survives tearDown rollback -> Leaked records (66 SEs, 10 Vehicles)
   │
   ├──> 2. Fires enqueue_after_commit -> RQ receives delete_dynamic_links -> External bench worker executes & commits asynchronously
   │
   ├──> 3. Committed tabSeries / tabSingles updates move row versions forward
           │
           └──> Under MariaDB REPEATABLE READ (innodb_snapshot_isolation=1), open test transactions hold stale snapshots
                │
                └──> SELECT ... FOR UPDATE on tabSeries or DELETE on tabSingles raises MariaDB Error 1020 (ER_CHECKREAD)
                     │
                     └──> Coping mechanism: Inner retry loops with frappe.db.rollback()
                          │
                          ├──> ScheduleEntry.autoname() rolls back uncommitted Service Bay -> test_service_bay fails with AssertionError
                          │
                          └──> create_test_customer() rolls back uncommitted Inspection Template -> test_repair_order fails with LinkValidationError
```

### Detailed Causal Steps:

1. **Why Mid-Test Commits Shatter Isolation & Cause MariaDB Error 1020**:
   - Frappe's test harness relies on per-test transaction isolation: a test starts, executes operations within an isolated transaction, and `tearDown()` issues `frappe.db.rollback()`, discarding all inserted records and releasing all locks without issuing costly SQL `DELETE` calls.
   - When Worker 1 introduced `frappe.db.commit()` inside `create_test_customer()`, `create_test_vehicle()`, `create_test_repair_order()`, and `create_test_schedule_entry()`, each helper call committed immediately.
   - In ERPNext, `Customer`, `Repair Order`, and `Material Request` all use naming series that update `tabSeries` (`current = current + 1`).
   - When a helper commits, the transaction ends and `tabSeries` is committed with a new version number.
   - MariaDB 11.8 enforces `innodb_snapshot_isolation = 1`. When another query or subsequent test has an active read snapshot established before that commit, any subsequent attempt to execute `SELECT current FROM tabSeries WHERE name = key FOR UPDATE` detects that the row was modified by a transaction newer than the snapshot.
   - MariaDB immediately aborts the statement with Error 1020: `"Record has changed since last read in table 'tabSeries'; try restarting transaction"`.

2. **Why Mid-Test Commits Trigger External Background Worker Collisions**:
   - Inside `frappe/model/delete_doc.py:193-198`, document deletion triggers:
     ```python
     frappe.enqueue(
         "frappe.model.delete_doc.delete_dynamic_links",
         doctype=doc.doctype,
         name=doc.name,
         now=frappe.in_test,
         enqueue_after_commit=True,
     )
     ```
   - In atomic tests that never commit, `enqueue_after_commit` hooks are never dispatched because `frappe.db.rollback()` in `tearDown()` discards all pending commit hooks.
   - Because Worker 1 committed mid-test, all enqueued `delete_dynamic_links` jobs were immediately flushed to Redis.
   - The external `bench worker` (PID 36) running under honcho picked up these jobs and began modifying MariaDB tables in parallel.
   - As the worker committed deletions, MariaDB snapshot isolation was repeatedly broken for the test runner, producing intermittent 1020 errors, table lock timeouts (1205), and deadlocks (1213).

3. **Why Inner Rollbacks in Retry Loops Cause Phantom Test Failures**:
   - In MariaDB and Python DB-API, there is no nested sub-transaction rollback without explicit `SAVEPOINT`. Calling `frappe.db.rollback()` rolls back the ENTIRE database transaction to the last commit point.
   - In `ScheduleEntry.autoname()`:
     Line 22 called `frappe.db.rollback()` when Error 1020 was caught.
     In `test_service_bay.py:105-125`:
     - Line 111 inserts `bay = ServiceBay(...).insert()` into the active transaction (uncommitted).
     - Line 122 inserts `se = ScheduleEntry(...).insert()`.
     - When `se.autoname()` encountered 1020, it executed `frappe.db.rollback()`.
     - That rollback wiped the uncommitted `ServiceBay` out of MariaDB!
     - When line 126 called `frappe.delete_doc("Service Bay", self.bay_alpha)`, the bay did not exist; `on_trash` never ran, and `with self.assertRaises(frappe.ValidationError)` failed with `AssertionError: ValidationError not raised`.
   - In `TestRepairOrder.test_vehicle_checking_incorporated_into_repair_order`:
     - Line 1155 inserted `template = InspectionTemplate(...).insert()` into the active transaction (uncommitted).
     - Line 1159 called `self.create_test_customer(f"{TEST_PREFIX}Intake_Driver")`.
     - When `create_test_customer()` encountered Error 1020 on attempt 0, its retry loop executed `frappe.db.rollback()`.
     - That rollback erased `template` from MariaDB.
     - `create_test_customer()` retried, succeeded, and committed.
     - When line 1173 called `ro.insert()`, link validation checked for `Inspection Template` `_TEST_ISM_Intake Checklist`. The template was gone, raising `LinkValidationError: Could not find Inspection Template: _TEST_ISM_Intake Checklist`.

4. **Why Swallowed Exceptions and Flawed Cleanup Created Permanent State Bleed**:
   - In `base.py:238-241`, `cleanup_tracked_docs()` wrapped document deletion in `except Exception: pass` and immediately executed `frappe.db.commit()`.
   - When a test crashed due to Error 1020, teardown attempted to delete documents. Any lock conflict or dependency failure caused the deletion to be silently swallowed, followed by a `commit()` of the incomplete state.
   - In `cleanup.py:434-436`, `_cleanup_repair_vehicles()` only deleted vehicles where `v.customer in test_customers`. Because `Customer` was deleted before `Repair Vehicle`, `v.customer` was missing from `test_customers`, and because `license_plate` was `NULL`, the 10 test vehicles were permanently orphaned.

5. **Why `test_setup_idempotency_preserves_customizations` Crashed on Run 3 & 5**:
   - `setUpClass()` in `test_ism_settings.py:65` executed `frappe.db.commit()` when resetting divergent settings.
   - In subsequent runs, `test_setup_idempotency_preserves_customizations` executed `frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)` at line 213.
   - `set_single_value` executed `DELETE FROM tabSingles WHERE field IN (...) AND doctype='ISM Settings'`.
   - Because `tabSingles` was touched by preceding tests, MariaDB raised Error 1020. Because line 213 had no retry logic or pre-operation snapshot reset, it crashed the stress loop.

---

## 3. Caveats

- **No Facade Stubs or Falsified Logic in Implementation**: The core scheduling math, capacity algorithms, and DocType controllers are authentic and functionally sound. The instability is strictly an artifact of transaction boundary violations, mid-test commits, and improper retry loops.
- **Savepoints vs Full Rollback**: While MySQL/MariaDB supports `SAVEPOINT` for sub-transaction rollbacks, Frappe Framework's database abstraction layer does not natively support Python context managers for savepoints across all supported backends (PostgreSQL). Relying on true single-transaction rollback at the `tearDown()` boundary is the idiomatic, robust Frappe standard.
- **Static Seed Fixtures vs Transactional Records**: Static master fixtures (`Test Customer`, `5YJSA1E26CF000001`, canonical bays, seed tags) belong in `setUpClass()` seeded once and committed once. Individual test methods must NEVER run `setup_all()` or `teardown_transactional()` inside per-test `setUp()` or `tearDown()`.

---

## 4. Conclusion & Integrated Architecture Proposal

Milestone 1 work product was rightly rejected. The following integrated 5-pillar architecture eliminates all mid-test commits, restores strict test transaction isolation, eliminates MariaDB Error 1020/1205/1213 deadlocks, and guarantees pristine database cleanliness.

### Pillar 1: Pure Atomic Tests Without Mid-Test Commits
1. In `ism/tests/base.py`:
   - **Remove all `frappe.db.commit()` calls** from `create_test_customer()` (line 71), `create_test_vehicle()` (line 97), `create_test_quotation()` (line 119), `create_test_repair_order()` (line 137), and `create_test_schedule_entry()` (line 159).
   - **Remove `frappe.db.commit()`** from `cleanup_tracked_docs()` (line 241).
   - Test helpers must return tracked documents in the caller's active transaction.
2. In `ism/tests/test_fixtures.py`:
   - **Remove `frappe.db.commit()`** from `create_test_schedule_entry()` (line 342) and `create_test_repair_order()` (line 367).
3. In `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
   - **Remove `frappe.db.commit()`** from `setUp()` (line 16), `_insert_entry()` (line 23), and `test_entry_type_requires_repair_order()` (line 240).

### Pillar 2: Clean Sequence Naming Without Inner Rollbacks
1. In `ism/ism/doctype/schedule_entry/schedule_entry.py`:
   - **Remove `frappe.db.rollback()` from `ScheduleEntry.autoname()`**:
     ```python
     def autoname(self):
         """Assign naming series SE-.#####."""
         from frappe.model.naming import make_autoname

         prefix = self.naming_series or "SE-.#####"
         self.name = make_autoname(prefix, doc=self)
     ```
   - Model Document controller naming hooks must NEVER issue database rollbacks. In a clean transaction isolation architecture without mid-test commits, `make_autoname()` operates within the test's transaction and does not experience snapshot version collisions.
2. In `ism/tests/base.py`:
   - **Remove `frappe.db.rollback()` from helper retry loops** (`create_test_customer`, `create_test_vehicle`, `create_test_quotation`, `create_test_repair_order`, `create_test_schedule_entry`).
   - If an error occurs during document insertion, let it propagate naturally to the test runner so `tearDown()` can cleanly roll back the entire transaction.
3. Do NOT add an autoname retry loop with `frappe.db.rollback()` to `Repair Order` (`repair_order.py`). `Repair Order` already uses standard `"autoname": "naming_series:"`, which runs cleanly under atomic transactions.

### Pillar 3: Class-Level Fixture Lifecycle (`setUpClass` vs `setUp`)
1. In `ism/tests/test_ro_scheduling_e2e.py`:
   - Move `setup_all()` from `setUp()` (line 72) to `@classmethod setUpClass(cls)`.
   - Remove `teardown_transactional()` from per-test `tearDown()` (line 80). Move any final cleanup to `@classmethod tearDownClass(cls)`.
2. In `ism/tests/test_scheduling_capacity.py`:
   - Move `setup_all()` from `setUp()` (line 73) to `@classmethod setUpClass(cls)`.
   - Remove `teardown_transactional()` from per-test `tearDown()` (line 90). Move final cleanup to `@classmethod tearDownClass(cls)`.
3. In `ism/tests/test_estimation_math.py`:
   - Move `setup_all()` from `setUp()` (line 48) to `@classmethod setUpClass(cls)`.
   - Remove `teardown_transactional()` from per-test `tearDown()` (line 56).
4. In `ism/tests/test_fixtures.py`:
   - In `teardown_transactional()`: Remove `frappe.db.commit()` (line 424). Use `frappe.db.rollback()` to release locks.

### Pillar 4: Safe Handling of `ISM Settings` & `tabSingles`
1. In `ism/ism/doctype/ism_settings/test_ism_settings.py`:
   - In `test_setup_idempotency_preserves_customizations`:
     - Issue `frappe.db.rollback()` at the beginning of the test method to guarantee a clean snapshot before reading/writing `tabSingles`.
     - Implement a localized retry helper `_set_single_value_with_retry()` that catches Error 1020, issues `frappe.db.rollback()`, clears caches, and retries up to 3 times:
       ```python
       def _set_single_value_with_retry(doctype, fieldname, value, max_attempts=3):
           for attempt in range(max_attempts):
               try:
                   frappe.db.set_single_value(doctype, fieldname, value)
                   break
               except Exception as e:
                   if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < max_attempts - 1:
                       frappe.db.rollback()
                       frappe.clear_cache(doctype=doctype)
                       if hasattr(frappe.db, "value_cache"):
                           frappe.db.value_cache.clear()
                       continue
                   raise
       ```
       *(Note: `rollback()` is safe here because this is at the test method boundary where no other uncommitted parent fixtures exist.)*

### Pillar 5: Resilient Teardown, Cleanup Hardening & Fixture Order
1. In `ism/tests/cleanup.py`:
   - At the beginning of `cleanup_all_test_data()`, call `frappe.db.rollback()` to release any lingering transaction locks and obtain a fresh snapshot.
   - In `_cleanup_repair_vehicles()` (lines 434–436), update the vehicle matching condition to catch all orphaned test vehicles:
     ```python
     is_test_vehicle = (
         v.name.startswith("5YJ3E1EBXNF")
         or (v.customer and v.customer in test_customers)
         or (v.customer and (v.customer.startswith("_TEST_") or "Test" in v.customer))
         or (v.customer and not frappe.db.exists("Customer", v.customer))
         or v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]
     )
     ```
2. In `ism/ism/doctype/repair_order/test_repair_order.py`:
   - In `test_vehicle_checking_incorporated_into_repair_order` (lines 1147–1160):
     Create prerequisites in strict dependency order: create `new_customer` first, then insert `template`, then insert `ro`:
     ```python
     # 1. Create test customer
     new_customer = self.create_test_customer(f"{TEST_PREFIX}Intake_Driver")

     # 2. Create inspection template
     template = frappe.get_doc({
         "doctype": "Inspection Template",
         "template_name": f"{TEST_PREFIX}Intake Checklist",
         "items": [
             {"inspection_description": "Check windshield for chips or cracks"},
             {"inspection_description": "Inspect tire tread depth and pressure"},
         ],
     })
     template.insert()
     self.track(template)

     # 3. Create Repair Order linked to template
     ro = frappe.get_doc({ ... }).insert()
     ```
   - This eliminates any risk of customer creation invalidating the template.

---

## 5. Verification Method

To independently verify the integrated architecture once implemented:

### 1. Sequential Stress Loop on `Schedule Entry` (10x Consecutive Runs)
```powershell
1..10 | ForEach-Object {
    Write-Host "=== SCHEDULE ENTRY RUN $_ ==="
    wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"
    if ($LASTEXITCODE -ne 0) { Write-Error "FAILED ON RUN $_"; exit 1 }
}
```
*Expected*: 10/10 PASS, exit code 0, 0 deadlocks on `tabSeries`.

### 2. Sequential Stress Loop on `ISM Settings` (10x Consecutive Runs)
```powershell
1..10 | ForEach-Object {
    Write-Host "=== ISM SETTINGS RUN $_ ==="
    wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"
    if ($LASTEXITCODE -ne 0) { Write-Error "FAILED ON RUN $_"; exit 1 }
}
```
*Expected*: 10/10 PASS, exit code 0, zero occurrences of MariaDB Error 1020 on `tabSingles`.

### 3. Full Application Test Suite Back-to-Back Execution
```powershell
1..2 | ForEach-Object {
    Write-Host "=== FULL APPLICATION TEST SUITE RUN $_ ==="
    wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
    if ($LASTEXITCODE -ne 0) { Write-Error "FAILED ON SUITE RUN $_"; exit 1 }
}
```
*Expected*: 381 tests executed (254 skipped), 0 failures, 0 errors, exit code 0.

### 4. Residual Database Cleanliness & Zero Bleed Verification
```powershell
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c @"
import frappe
frappe.init('development.localhost')
frappe.connect()
from ism.tests.cleanup import cleanup_all_test_data
cleanup_all_test_data(False)

se_count = frappe.db.count('Schedule Entry')
ro_count = frappe.db.count('Repair Order', filters={'name': ['like', 'RO-2026-%']})
test_custs = frappe.get_all('Customer', filters=[['name', 'like', '_TEST%']], pluck='name')
test_vehs = frappe.get_all('Repair Vehicle', filters=[['name', 'like', '5YJ3%']], pluck='name')
bays = [b.name for b in frappe.get_all('Service Bay', filters={'name': ['not in', ['Test Bay 1', 'Test Bay 2', 'Test Bay 3']]})]

print(f'Residual Schedule Entries: {se_count}')
print(f'Residual Test Customers: {len(test_custs)}')
print(f'Residual Test Vehicles: {len(test_vehs)}')
print(f'Residual Custom Bays: {len(bays)}')

assert se_count == 0, f'Leaked {se_count} Schedule Entries'
assert len(test_custs) == 0, f'Leaked {len(test_custs)} Customers'
assert len(test_vehs) == 0, f'Leaked {len(test_vehs)} Vehicles'
assert len(bays) == 0, f'Leaked {len(bays)} Bays'
print('DATABASE CLEANLINESS VERIFIED: 100% PRISTINE')
"@
```

### Invalidation Conditions
- Any occurrence of MariaDB Error 1020, 1205, or 1213.
- Any non-zero exit code on the 10x sequential stress loops.
- Any residual non-seed records persisting in MariaDB post-test execution.
