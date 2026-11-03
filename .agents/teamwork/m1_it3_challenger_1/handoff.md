# Adversarial Challenge Handoff Report: Concurrency, Transaction Isolation, and MariaDB 1020 Defenses

**Challenger**: `m1_it3_challenger_1` (Milestone 1 Iteration 3 Concurrency Challenger 1)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it3_challenger_1`  
**Milestone**: M1 (Resource & Capacity Data Models & Settings Consolidation)  
**Parent Orchestrator**: `0e38d9c2-368a-4e62-8c88-6e3643ab0b5d`  
**Verdict**: **CHALLENGE_FAILED**

---

## Challenge Summary

**Overall risk assessment**: **HIGH**

Worker 3 claimed in `m1_worker_3\handoff.md`:
> "1. Elimination of MariaDB 1020 on `tabSingles`: ... By defining `CANONICAL_ISM_SETTINGS` and restoring baseline defaults using `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` in `setUp()` and `tearDown()`, updates bypass the Document lifecycle and version checks. ... Result: 100% elimination of MariaDB 1020 deadlock and state pollution. ... 5x Sequential Run on ISM Settings: 5/5 consecutive passes, 0 deadlocks, 0 failures, 0 errors."

Empirical adversarial challenge disproves this claim:
1. **10x Sequential Stress Loop 1 (Task `task-67`)**: 7 PASS, 3 FAIL (Runs 2, 3, 4 failed).
2. **10x Sequential Stress Loop 2 (Task `task-91`)**: 8 PASS, 2 FAIL (Runs 1 and 4 failed).
3. **Lock Contention Experiment**: 100% reproducible crash with `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`.
4. **Root Cause**: `frappe.db.set_single_value()` is executed in both `setUp()` and `tearDown()` of `TestISMSettings` (16 times per test suite run). `set_single_value()` performs a bulk `DELETE FROM tabSingles WHERE field IN (...) AND doctype='ISM Settings'` followed by `INSERT INTO tabSingles`. Because `tabSingles` has no primary key, MariaDB 11 InnoDB under REPEATABLE READ isolation intermittently fails on secondary index scans during `DELETE` with error 1020 (`ER_CHECKREAD`).

---

## 1. Observation

### 1.1 Empirical Stress Test 1: 10x Sequential Loop (Task `task-67`)
**Command**:
```powershell
$passes = 0; $fails = 0; 1..10 | ForEach-Object { $i = $_; $res = wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' 2>&1; if ($LASTEXITCODE -eq 0) { $passes++; Write-Host "Run $i PASS" } else { $fails++; Write-Host "Run $i FAIL"; Write-Host ($res | Out-String) } }; Write-Host "Total: $passes PASS, $fails FAIL"
```

**Result**: `Total: 7 PASS, 3 FAIL` (30% failure rate)
- Run 1: PASS
- Run 2: FAIL
- Run 3: FAIL
- Run 4: FAIL
- Run 5: PASS
- Run 6: PASS
- Run 7: PASS
- Run 8: PASS
- Run 9: PASS
- Run 10: PASS

**Verbatim Error from Run 2 (`task-67.log` lines 33–75, 119–136)**:
```text
======================================================================
 ERROR  test_default_ism_settings_initialized (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_default_ism_settings_initialized)
Verify canonical default values for all scheduling and estimation fields.
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
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 1446, in delete
    return query.run(**kwargs)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/query_builder/utils.py", line 131, in execute_query
    result = frappe.local.db.sql(query, params, *args, **kwargs)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
    raise frappe.QueryDeadlockError(e) from e
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
```

---

### 1.2 Empirical Stress Test 2: Second Independent 10x Sequential Loop (Task `task-91`)
**Command**:
```powershell
$results = @(); for ($i = 1; $i -le 10; $i++) { $out = wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' 2>&1; $code = $LASTEXITCODE; if ($code -eq 0) { $results += "Run ${i}: PASS"; Write-Host "Run ${i}: PASS" } else { $results += "Run ${i}: FAIL (code ${code})"; Write-Host "Run ${i}: FAIL (code ${code})"; Write-Host ($out | Out-String) } }; Write-Host "SUMMARY:"; $results | ForEach-Object { Write-Host $_ }
```

**Result**: 8 PASS, 2 FAIL (20% failure rate)
- Run 1: FAIL (code 1) — `test_default_ism_settings_initialized` failed in `tearDown()`
- Run 2: PASS
- Run 3: PASS
- Run 4: FAIL (code 1) — `test_validate_operating_hours_order` failed in `tearDown()`
- Run 5: PASS
- Run 6: PASS
- Run 7: PASS
- Run 8: PASS
- Run 9: PASS
- Run 10: PASS

**Verbatim Error from Run 4 (`task-91.log` lines 213–274)**:
```text
======================================================================
 ERROR  test_validate_operating_hours_order (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_validate_operating_hours_order)
Verify validation error when operating_hours_start >= operating_hours_end.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 272, in sql
    self.execute_query(query, values)
  ...
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 48, in tearDown
    frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 870, in set_single_value
    frappe.db.delete(
    	"Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug
    )
  ...
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
```

---

### 1.3 Lock Contention Experiment
**Command**:
```powershell
$proc = Start-Process powershell -ArgumentList "-Command", "wsl docker exec -i devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d -e 'START TRANSACTION; SELECT * FROM tabSingles WHERE doctype = \'ISM Settings\' FOR UPDATE; SELECT SLEEP(15); COMMIT;'" -PassThru; Start-Sleep -Seconds 2; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; $proc.WaitForExit()
```

**Result**: Immediately failed with exit code 1.
```text
======================================================================
 ERROR  test_validate_estimation_sigma_bounds (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_validate_estimation_sigma_bounds)
...
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
...
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
```

---

### 1.4 Post-Run MariaDB Persistence Inspection
**Command**:
```powershell
"SELECT field, value FROM tabSingles WHERE doctype = 'ISM Settings' AND field IN ('enable_technician_capacity', 'default_slot_interval', 'estimation_percentile');" | wsl docker exec -i devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d
```

**Result**:
```text
field                       value
default_slot_interval       30
enable_technician_capacity  1
estimation_percentile       80.0
```
Canonical defaults are preserved after run completion or rollback.

---

## 2. Logic Chain

1. **Schema Structure of `tabSingles`**:
   - `SHOW CREATE TABLE tabSingles;` reveals:
     ```sql
     CREATE TABLE `tabSingles` (
       `doctype` varchar(255) DEFAULT NULL,
       `field` varchar(255) DEFAULT NULL,
       `value` longtext DEFAULT NULL,
       KEY `singles_doctype_field_index` (`doctype`,`field`)
     ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC
     ```
   - `tabSingles` has **no primary key**. It relies on an index `KEY singles_doctype_field_index (doctype, field)`.

2. **Mechanism of `frappe.db.set_single_value`**:
   - In Frappe core (`apps/frappe/frappe/database/database.py`, lines 866–875):
     ```python
     frappe.db.delete(
         "Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug
     )
     singles_data = ((doctype, key, sbool(value)) for key, value in to_update.items())
     frappe.qb.into("Singles").columns("doctype", "field", "value").insert(*singles_data).run(debug=debug)
     ```
   - Whenever `set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` is called, it issues:
     `DELETE FROM tabSingles WHERE field IN ('operating_hours_start', 'operating_hours_end', ...) AND doctype = 'ISM Settings'`
     followed by `INSERT INTO tabSingles ...`.

3. **Defect in `TestISMSettings` (`test_ism_settings.py`)**:
   - In `test_ism_settings.py`:
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
   - In an 8-test suite, this executes 16 discrete `DELETE` + `INSERT` + `COMMIT` cycles against `tabSingles`.
   - In a 10x loop (80 tests), this executes 160 `DELETE` + `INSERT` cycles on the same table rows.
   - Under MariaDB 11 InnoDB with REPEATABLE READ isolation on a table without a primary key, rapid range deletions on secondary index pages cause MariaDB engine error 1020 (`ER_CHECKREAD`: "Record has changed since last read in table 'tabSingles'; try restarting transaction").
   - Furthermore, tests 4–8 (`test_validate_break_hours_order`, `test_validate_break_within_operating_hours`, `test_validate_estimation_percentile_bounds`, `test_validate_estimation_sigma_bounds`, `test_validate_operating_hours_order`) test validation methods purely in-memory on `frappe.new_doc("ISM Settings")` without writing to the database. Running full database deletes and inserts before and after purely in-memory unit tests creates unnecessary database thrashing and induces the 1020 failure.

4. **Flakiness and Intermittency**:
   - Because Worker 3 only tested a 5x loop once, the underlying race condition was hidden. Expanding to 10x sequential runs reveals that the suite fails 20% to 30% of the time, and lock contention triggers it 100% of the time.
   - Worker 3's claim of "100% elimination of MariaDB 1020 deadlock" is empirically invalidated.

---

## 3. Caveats

- **Canonical DB Values**: When the test runner finishes or aborts, MariaDB `tabSingles` retains the correct values (`30`, `1`, `80.0`). The issue is not data corruption of the baseline values, but transaction isolation failure and test runner unreliability due to MariaDB 1020.
- **In-Memory Validation**: The business logic checks in `ISMSettings.validate()` are mathematically and logically correct. The flaw is confined to the test setup/teardown transaction handling.

---

## 4. Conclusion

**Verdict**: **CHALLENGE_FAILED**

Candidate work product fails the adversarial challenge:
1. `test_ism_settings.py` experiences MariaDB OperationalError (1020) / QueryDeadlockError during sequential test execution (3/10 failures in Loop 1, 2/10 failures in Loop 2).
2. Lock contention on `tabSingles` induces 100% failure with MariaDB 1020 error.
3. The root cause is excessive `DELETE` + `INSERT` churn via `frappe.db.set_single_value()` in both `setUp()` and `tearDown()` on every test method on a keyless MariaDB table.

### Recommended Remediation for Worker:
1. **Remove per-method `set_single_value` churn**:
   - In `TestISMSettings`, move the canonical settings setup to `setUpClass()` / `tearDownClass()`, OR only restore canonical settings in `setUp()` if the values differ from canonical.
   - For in-memory validation tests (`test_validate_*`), do not touch `tabSingles` at all.
   - For tests that mutate settings (`test_setup_idempotency_preserves_customizations`), use `frappe.db.rollback()` without calling `set_single_value()` + `commit()` in `tearDown()`.
2. **Alternatively, use direct `UPDATE` rather than `set_single_value` (which does `DELETE` + `INSERT`)**:
   - Updating existing rows (`UPDATE tabSingles SET value = ... WHERE doctype = 'ISM Settings' AND field = ...`) avoids deleting and re-inserting records on keyless InnoDB tables.

---

## 5. Verification Method

To independently reproduce this failure:

1. **Run 10x Sequential Stress Test**:
   ```powershell
   1..10 | ForEach-Object { $i = $_; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if ($LASTEXITCODE -ne 0) { Write-Host "Run $i FAILED with exit code $LASTEXITCODE" } }
   ```
   *Expected Result*: Fails intermittently (e.g. 2–3 out of 10 runs) with `frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`.

2. **Induce Lock Contention**:
   ```powershell
   $proc = Start-Process powershell -ArgumentList "-Command", "wsl docker exec -i devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d -e 'START TRANSACTION; SELECT * FROM tabSingles WHERE doctype = \'ISM Settings\' FOR UPDATE; SELECT SLEEP(15); COMMIT;'" -PassThru; Start-Sleep -Seconds 2; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; $proc.WaitForExit()
   ```
   *Expected Result*: Immediate failure with MariaDB 1020 error.
