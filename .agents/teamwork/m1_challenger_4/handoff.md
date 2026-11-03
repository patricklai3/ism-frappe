# Handoff Report: Milestone 1 Migration & Concurrency Stress Challenge

**Author**: `m1_challenger_4` (Milestone 1 Migration & Concurrency Challenger 4)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_challenger_4`  
**Verdict**: **CHALLENGE_FAILED**

---

## 1. Observation

### 1.1 Summary of Focus Areas Tested
1. **Consecutive `bench migrate` executions in WSL**: Tested 3 consecutive migrations against live MariaDB in `devcontainer-frappe-1`.
2. **Seeder execution ordering in `after_migrate()`**: Verified order of `ensure_ism_roles()`, `setup_default_ism_settings()`, `seed_equipment_tags()`, and `seed_schedule_entry_types()` before downstream backfills.
3. **10x Sequential / Multi-Run `TestISMSettings` executions**: Tested 10x sequential executions of `bench run-tests --app ism --doctype 'ISM Settings'` in `devcontainer-frappe-1`.
4. **`ISM Settings` bounds validation**: Verified boundary validation rules for percentile, sigma, operating hours, and break hours.

---

### 1.2 Verbatim Observations & Tool Outputs

#### Observation 1: Consecutive `bench migrate` Executions (3/3 Passed)
Commands executed:
```bash
wsl -e bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"
```
- **Run 1 (Task ID: `task-58`)**: Exited with code `0`.
  ```text
  Updating DocTypes for ism           : [========================================] 100%
  Syncing jobs...
  Syncing fixtures...
  Syncing dashboards...
  Updating Dashboard for ism
  Syncing customizations...
  Syncing languages...
  Flushing deferred inserts...
  Updating installed applications...
  Executing `after_migrate` hooks...
  Queued rebuilding of search index for development.localhost
  ```
- **Run 2 (Task ID: `task-64`)**: Exited with code `0`.
- **Run 3 (Task ID: `task-70`)**: Exited with code `0`.
No deadlocks, lock timeouts, or schema errors were observed across the 3 consecutive migration runs.

#### Observation 2: Seeder Execution Ordering & Database State in MariaDB
Inspected `ism/setup.py` lines 674–695:
```python
def after_migrate():
	"""Run migrations and setup tasks for ISM."""
	# 1. Core Scheduling Seeders & ISM Settings (must execute first and idempotently)
	ensure_ism_roles()
	setup_default_ism_settings()
	seed_equipment_tags()
	seed_schedule_entry_types()

	# 2. Master Setup & DocType Permissions
	create_standard_inspection_template()
	ensure_doctype_creation_enabled()

	# 3. ERPNext Integration & Custom Field Definitions
	cleanup_legacy_custom_fields()
	setup_default_taxes_and_payments()
	setup_parts_item_group()
	setup_item_custom_fields()
	setup_procurement_custom_fields()

	# 4. Historical Data Backfills
	backfill_service_package_models()
```
Seeded records directly inspected in MariaDB (`development.localhost`):
- Role `ISM Manager`: Exists (`desk_access = 1`).
- `ISM Settings`: Default values initialized (`operating_hours_start = 08:00:00`, `operating_hours_end = 17:00:00`, `break_start = 12:00:00`, `break_end = 12:30:00`, `default_slot_interval = 30`, `scheduling_horizon_days = 30`, `estimation_percentile = 80.0`, `estimation_sigma = 0.30`, `default_diagnostic_duration = 60`, `technician_designation = 'Technician'`, `enable_technician_capacity = 1`).
- Equipment Tags: `Lift`, `EV High Voltage`, `Alignment Rack` exist.
- Schedule Entry Types: `Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service` exist with standard colors and RO requirements.

#### Observation 3: Failure in 10x Sequential `TestISMSettings` Execution (MariaDB 1020 Error Reproduced)
Stress harness command executed in `devcontainer-frappe-1`:
```python
import subprocess, sys

total = 10
passed = 0
for i in range(1, total + 1):
    res = subprocess.run(
        ['bench', '--site', 'development.localhost', 'run-tests', '--app', 'ism', '--doctype', 'ISM Settings'],
        cwd='/workspace/development/frappe-bench',
        capture_output=True,
        text=True
    )
    combined = res.stdout + res.stderr
    if res.returncode != 0:
        print(f'Run {i} FAILED (code {res.returncode}):')
        print(combined)
        sys.exit(1)
```
**Execution Result (Task ID: `task-145`)**:
```text
Run 1/10: PASS (OK)
Run 2/10: PASS (OK)
Run 3/10: PASS (OK)
Run 4/10: PASS (OK)
Run 5 FAILED (code 1):
Starting test run with parameters: site=development.localhost, app=ism, doctype=ISM Settings, verbose=False, tests=(), force=False, profile=False, failfast=False, skip_before_tests=False, debug=False, selected_categories=[]
View detailed logs (using --verbose): /workspace/development/frappe-bench/logs/frappe.testing.log

Running 7 unspecified-category tests for ism


======================================================================
 ERROR  test_validate_operating_hours_order (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings.test_validate_operating_hours_order)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 272, in sql
    self.execute_query(query, values)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 372, in execute_query
    return self._cursor.execute(query, values)
    query = 'SELECT `field`,`value` FROM `tabSingles` WHERE `doctype`=%(param1)s FOR UPDATE'
    values = {'param1': 'ISM Settings'}
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 179, in execute
    res = self._query(mogrified_query)
    mogrified_query = b"SELECT `field`,`value` FROM `tabSingles` WHERE `doctype`='ISM Settings' FOR UPDATE"
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 331, in _query
    self._do_get_result(db)
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 136, in _do_get_result
    self._result = result = self._get_result()
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/cursors.py", line 363, in _get_result
    return self._get_db().store_result()
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 77, in test_validate_operating_hours_order
    settings.save()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 534, in save
    return self._save(*args, **kwargs)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 565, in _save
    self.check_if_latest()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 1059, in check_if_latest
    self.load_doc_before_save(raise_exception=True)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 1393, in load_doc_before_save
    self._doc_before_save = frappe.get_doc(self.doctype, self.name, for_update=True)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/utils/__init__.py", line 225, in wrapper
    return dispatch(args[0])(*args, **kw)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 144, in get_doc_str
    doc = controller(doctype, name, **kwargs)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 227, in __init__
    self.load_from_db()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/document.py", line 260, in load_from_db
    single_doc = frappe.db.get_singles_dict(self.doctype, for_update=self.flags.for_update)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 800, in get_singles_dict
    ).run(debug=debug)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/query_builder/utils.py", line 131, in execute_query
    result = frappe.local.db.sql(query, params, *args, **kwargs)  # nosemgrep
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
    raise frappe.QueryDeadlockError(e) from e
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")

ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
    ✔  test_default_ism_settings_initialized
    ✔  test_setup_idempotency_preserves_customizations
    ✔  test_validate_break_hours_order
    ✔  test_validate_break_within_operating_hours
    ✔  test_validate_estimation_percentile_bounds
    ✔  test_validate_estimation_sigma_bounds
    ✖  test_validate_operating_hours_order
----------------------------------------------------------------------
Ran 7 tests in 2.250s

FAILED (errors=1)
```

#### Observation 4: State Bleed and Test Cross-Contamination
When `test_validate_estimation_percentile_bounds` commits `0.85 -> 85.0` or `test_setup_idempotency_preserves_customizations` commits `enable_technician_capacity = 0`, if `tearDown()` is interrupted or catches an error via `except Exception: frappe.db.rollback()`, the mutated values remain committed in MariaDB.
When a subsequent run executes:
```text
AssertionError: 85.0 != 80.0
AssertionError: 0 != 1
```
Because `setup_default_ism_settings()` uses `if not settings.estimation_percentile:` and `if settings.get("enable_technician_capacity") is None:`, it intentionally does NOT overwrite existing values, causing test suite permanent failure until manual database repair.

---

## 2. Logic Chain

1. **Root Cause of MariaDB 1020 Error in `TestISMSettings`**:
   - In Frappe, calling `settings.save()` on any `Document` triggers `Document._save()` which invokes `self.check_if_latest()` before `self.validate()`.
   - `check_if_latest()` executes `frappe.get_doc(self.doctype, self.name, for_update=True)`, which issues `SELECT ... FROM tabSingles WHERE doctype='ISM Settings' FOR UPDATE`.
   - In `TestISMSettings.setUp()`, `setup_default_ism_settings()` is run followed by `frappe.db.commit()`. Then line 25 executes `self._original_settings = frappe.get_doc("ISM Settings").as_dict()`.
   - In MariaDB, this non-locking SELECT after a commit initiates a new transaction and opens a repeatable read snapshot view on `tabSingles`.
   - When the test method subsequently executes `settings.save()`, `check_if_latest()` issues `SELECT ... FOR UPDATE` inside that open transaction.
   - If any background process (e.g., Frappe scheduler, background worker, or web server) modified or touched `tabSingles`, or if previous transaction rollbacks left an open read view, MariaDB InnoDB raises `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`.
   - Frappe wraps this in `frappe.exceptions.QueryDeadlockError`. Because `with self.assertRaises(frappe.ValidationError)` only catches `ValidationError`, the uncaught `QueryDeadlockError` aborts the test with an unhandled ERROR.

2. **Test Design Flaw in `test_ism_settings.py`**:
   - Testing controller validation logic by calling full `settings.save()` unnecessarily hits MariaDB table-level row locks and version checks on `tabSingles`.
   - The validation rules can and should be tested by invoking `settings.validate()` directly, or by setting `settings.flags.ignore_version = True` / rolling back open read views prior to `save()`.
   - Committing mutations (`frappe.db.commit()`) inside test methods (`test_validate_estimation_percentile_bounds`, `test_setup_idempotency_preserves_customizations`) pollutes shared state in `tabSingles` and leaves unisolated records for subsequent runs.

---

## 3. Caveats

1. `bench migrate` executions were completely clean (3/3 runs passed, 0 errors, 0 deadlocks).
2. Seeder execution order in `ism/setup.py` was completely verified.
3. Controller validation boundary logic in `ism_settings.py` is mathematically correct (percentile 50–99%, sigma 0.05–1.00, operating and break hours order all properly enforce constraints).
4. The sole blocking defect is in `test_ism_settings.py` test isolation and locking hygiene, causing reproducible MariaDB 1020 errors during 10x sequential stress runs.

---

## 4. Conclusion

**Verdict**: **CHALLENGE_FAILED**

The mandate required:
> "Empirically stress test consecutive bench migrate runs and 10x sequential TestISMSettings executions in live MariaDB in devcontainer-frappe-1. Verify zero MariaDB 1020 OperationalError or deadlock."

The 10x sequential `TestISMSettings` stress test failed at Run 5 with:
`MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`
wrapped as `frappe.exceptions.QueryDeadlockError`.

### Actionable Remediation Required:
1. In `ism/ism/doctype/ism_settings/test_ism_settings.py`:
   - For pure validation testing (`test_validate_operating_hours_order`, `test_validate_break_hours_order`, `test_validate_break_within_operating_hours`, `test_validate_estimation_percentile_bounds`, `test_validate_estimation_sigma_bounds`), test `settings.validate()` directly instead of `settings.save()`, or pass `ignore_version=True` to avoid `check_if_latest()` and `FOR UPDATE` lock collisions on `tabSingles`.
   - In `setUp()`, ensure `frappe.db.rollback()` is executed after taking the snapshot dict, so the transaction snapshot does not remain open with a stale read view into the test method.
   - Remove permanent mid-test `frappe.db.commit()` calls from `test_validate_estimation_percentile_bounds` and `test_setup_idempotency_preserves_customizations` or explicitly revert baseline state in test cleanup.

---

## 5. Verification Method

To reproduce this failure empirically in the live environment:

```bash
wsl -e docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "import subprocess, sys

total = 10
passed = 0
for i in range(1, total + 1):
    res = subprocess.run(
        ['bench', '--site', 'development.localhost', 'run-tests', '--app', 'ism', '--doctype', 'ISM Settings'],
        cwd='/workspace/development/frappe-bench',
        capture_output=True,
        text=True
    )
    combined = res.stdout + res.stderr
    if res.returncode != 0:
        print(f'Run {i} FAILED (code {res.returncode}):')
        print(combined)
        sys.exit(1)
    passed += 1
    print(f'Run {i}/{total}: PASS (OK)')
"
```
**Invalidation Condition**: The challenge is resolved only when this 10x sequential stress harness completes all 10 runs with 0 errors, 0 failures, and 0 occurrences of MariaDB 1020 `OperationalError`.
