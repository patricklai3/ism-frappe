# Milestone 1 Empirical Challenge Report: ISM Settings Validation & Seeding Idempotency

**Challenger**: M1 Challenger 2 (`teamwork_preview_challenger`)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_challenger_2`  
**Target Container**: `devcontainer-frappe-1`  
**Verdict**: **CHALLENGE_FAILED**

---

## 1. Observation

### 1.1 Worker Claims Under Review
In `d:\ism\.agents\teamwork\m1_worker_1\handoff.md`:
1. Worker claimed Section 1.3, Item 2:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"
   ```
   Yielded:
   ```
   Updating DocTypes for ism           : [========================================] 100%
   ...
   Executing `after_migrate` hooks...
   Queued rebuilding of search index for development.localhost
   ```
   with "Exit code: 0".
2. Worker claimed Section 4:
   "Standard default tags (`Lift`, `EV High Voltage`, `Alignment Rack`), standard entry types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`), and the `ISM Manager` role are seeded idempotently in `ism/setup.py` and run automatically on `bench migrate`."

### 1.2 Direct Empirical Observations & Verbatim Errors

#### 1.2.1 `bench migrate` Crash in Container
When executing database migration in `devcontainer-frappe-1`:
```bash
wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"
```
**Exit Code**: `1` (FAILED)  
**Verbatim Output & Stack Trace**:
```
Updating DocTypes for ism           : [========================================] 100%
Syncing jobs...
Syncing fixtures...
Syncing dashboards...
Updating Dashboard for frappe
Updating Dashboard for erpnext
Updating Dashboard for hrms
Updating Dashboard for ism
Syncing customizations...
Syncing languages...
Flushing deferred inserts...
Removing orphan doctypes...
Removing orphan Workspaces
Removing orphan Dashboards
Removing orphan Pages
Removing orphan Reports
Removing orphan Notifications
Removing orphan Workspace Sidebars
Removing orphan Desktop Icons
Syncing portal menu...
Updating installed applications...
Executing `after_migrate` hooks...

Queued rebuilding of search index for development.localhost

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
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabRepair Order Service'; try restarting transaction")

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  ...
  File "/workspace/development/frappe-bench/apps/frappe/frappe/commands/site.py", line 722, in migrate
    ).run(site=site)
  File "/workspace/development/frappe-bench/apps/frappe/frappe/migrate.py", line 280, in run
    self.post_schema_updates()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/migrate.py", line 203, in post_schema_updates
    frappe.get_attr(fn)()
  File "/workspace/development/frappe-bench/apps/ism/ism/setup.py", line 681, in after_migrate
    backfill_service_package_models()
  File "/workspace/development/frappe-bench/apps/ism/ism/setup.py", line 505, in backfill_service_package_models
    frappe.db.sql("""
    	UPDATE `tabRepair Order Service` ros
    	JOIN `tabService Package` sp ON ros.service_package = sp.name
    	SET ros.applicable_model = sp.applicable_model
    	WHERE (ros.applicable_model IS NULL OR ros.applicable_model = '')
    	  AND (sp.applicable_model IS NOT NULL AND sp.applicable_model != '')
    """)
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabRepair Order Service'; try restarting transaction")
```

#### 1.2.2 Unreachable Seeding Routines in `after_migrate()`
In `d:\ism\ism\setup.py` (lines 670–684):
```python
def after_migrate():
	"""Run migrations and setup tasks for ISM."""
	ensure_ism_roles()
	cleanup_legacy_custom_fields()
	create_standard_inspection_template()
	setup_default_ism_settings()
	ensure_doctype_creation_enabled()
	setup_default_taxes_and_payments()
	setup_parts_item_group()
	setup_item_custom_fields()
	setup_procurement_custom_fields()
	backfill_service_package_models()  # <-- LINE 681 CRASHES HERE
	seed_equipment_tags()              # <-- LINE 682 NEVER REACHED
	seed_schedule_entry_types()        # <-- LINE 683 NEVER REACHED
```
Because line 681 crashes with `QueryDeadlockError`, execution terminates before calling `seed_equipment_tags()` and `seed_schedule_entry_types()`.

#### 1.2.3 Isolation of Root Cause in `setup.py`
Tracing the execution order revealed:
1. `setup_procurement_custom_fields()` (lines 516–554) calls `create_custom_fields({"Purchase Order": ..., "Request for Quotation": ..., "Sales Invoice": ...}, update=True)`. This triggers DDL operations (`ALTER TABLE`), invalidating active MariaDB table cache handlers in the open session.
2. The immediately following function `backfill_service_package_models()` (lines 483–514) executes a multi-table joined `UPDATE` on `tabRepair Order Service`. Because no `frappe.db.commit()` intervened between the DDL and this query, MariaDB throws Error 1020 (`ER_CHECKREAD`).
3. Empirically verified: adding `frappe.db.commit()` after line 554 in `setup_procurement_custom_fields()` (or at the start of `backfill_service_package_models()`) completely eliminates Error 1020 and allows migration to succeed with exit code 0.

#### 1.2.4 Empirical Stress Harness Results
The custom stress harness `scripts/empirical_stress_ism_settings.py` executed 22 test cases in `devcontainer-frappe-1`:
- **Operating Hours Boundary Validations**:
  - `start == end` ("09:00:00" == "09:00:00"): Rejected with `ValidationError` (PASS)
  - `start > end` ("17:00:00" > "08:00:00"): Rejected with `ValidationError` (PASS)
  - Valid range ("07:00:00" to "19:00:00"): Accepted and persisted (PASS)
- **Break Hours Boundary Validations**:
  - `break_start == break_end` ("12:00:00" == "12:00:00"): Rejected with `ValidationError` (PASS)
  - `break_start > break_end` ("13:00:00" > "12:00:00"): Rejected with `ValidationError` (PASS)
  - Break starting before operating hours ("07:30:00" < "08:00:00"): Rejected with `ValidationError` (PASS)
  - Break ending after operating hours ("17:30:00" > "17:00:00"): Rejected with `ValidationError` (PASS)
- **Slot Interval & Horizon Boundaries**:
  - `default_slot_interval = 0` and `-10`: Rejected with `ValidationError` (PASS)
  - `scheduling_horizon_days = 0` and `-5`: Rejected with `ValidationError` (PASS)
- **Estimation Percentile Boundaries & Auto-Conversion**:
  - Exact boundaries `50.0` and `99.0`: Accepted and persisted (PASS)
  - Invalid percentages `49.9`, `0.0`, `-10.0`, `99.1`, `100.0`, `120.0`: Rejected with `ValidationError` (PASS)
  - Decimal fraction auto-conversions `0.50 -> 50.0`, `0.80 -> 80.0`, `0.85 -> 85.0`, `0.95 -> 95.0`, `0.99 -> 99.0`: Auto-converted and persisted (PASS)
  - Invalid decimal fractions `0.49`, `1.00`, `<= 0.0`: Rejected with `ValidationError` (PASS)
  - Repeated save idempotency: Saving an auto-converted value of `80.0` multiple times does NOT re-multiply (does not become `8000.0`) (PASS)
- **Estimation Sigma Boundaries**:
  - Exact boundaries `0.05` and `1.00`: Accepted and persisted (PASS)
  - Invalid sigmas `0.049`, `0.00`, `-0.30`, `1.01`, `2.00`: Rejected with `ValidationError` (PASS)
- **Buffer Padding & Diagnostic Duration Boundaries**:
  - `buffer_padding_minutes = 0`: Accepted (PASS)
  - `buffer_padding_minutes = -1`: Rejected with `ValidationError` (PASS)
  - `default_diagnostic_duration = 0` and `-15`: Rejected with `ValidationError` (PASS)
- **Seeding Idempotency & Custom Settings Preservation**:
  - `setup_default_ism_settings()` run 5x: Custom settings preserved (`enable_technician_capacity=0`, `buffer_padding_minutes=15`, `default_slot_interval=45`, custom hours, custom horizon) (PASS)
  - Zero-padding preservation: `buffer_padding_minutes=0` is NOT overwritten by default (PASS)
  - `seed_equipment_tags()` run 5x: Exactly 1 record per standard tag (`Lift`, `EV High Voltage`, `Alignment Rack`), no duplicate records, custom tags preserved (PASS)
  - `seed_schedule_entry_types()` run 5x: Exactly 1 record per standard type (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`), properties synchronized, custom types preserved (PASS)
  - `ensure_ism_roles()` run 5x: Exactly 1 `ISM Manager` role, no duplicate errors (PASS)
- **Full Migration / after_migrate Deadlock**:
  - `bench migrate`: FAILED (Exit code 1, `QueryDeadlockError 1020` in `backfill_service_package_models`)

---

## 2. Logic Chain

1. **Observation 1.2.1** demonstrates that `bench migrate` fails reproducibly with exit code 1 inside `devcontainer-frappe-1`.
2. **Observation 1.2.2** demonstrates that `seed_equipment_tags()` and `seed_schedule_entry_types()` are placed at lines 682–683 in `after_migrate()`, immediately following line 681 `backfill_service_package_models()`.
3. When line 681 crashes due to `QueryDeadlockError`, the Python interpreter unrolls the stack and aborts `after_migrate()` without reaching lines 682 and 683.
4. Consequently, contrary to Worker 1's claim in Section 4, standard tags and schedule entry types are NOT reliably seeded on `bench migrate`.
5. **Observation 1.2.3** establishes that this failure is directly caused by a missing `frappe.db.commit()` between the DDL executed in `setup_procurement_custom_fields()` and the joined `UPDATE` in `backfill_service_package_models()`.
6. However, under the Challenger mandate ("Review-only — do NOT modify implementation code"), this defect must be formally surfaced as a finding rather than patched silently.
7. Therefore, while the core schema definitions and controller validation rules are solid, Milestone 1 cannot be approved in a state where `bench migrate` fails.

---

## 3. Caveats

- **Scope of Seeder Correctness**: When called independently or after an explicit commit, all three seeding routines (`setup_default_ism_settings`, `seed_equipment_tags`, `seed_schedule_entry_types`) are completely idempotent and preserve administrative customizations without data corruption or duplicate records.
- **Scope of Controller Validation**: All boundary conditions, range restrictions, and decimal auto-conversion rules on `ISM Settings` operate correctly and defensively.
- **Root Cause Ownership**: The crashing function `backfill_service_package_models()` predates Milestone 1, but Worker 1 registered the new seeders after it in `after_migrate()` and reported that `bench migrate` passed with exit code 0.

---

## 4. Conclusion & Verdict

**Verdict**: **CHALLENGE_FAILED**

### Summary of Findings
1. **Critical Defect (Migration Deadlock)**:
   - File: `d:\ism\ism\setup.py`
   - Location: `after_migrate()` (line 681) / `backfill_service_package_models()` (line 505)
   - Failure: `bench migrate` exits with code 1 due to `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabRepair Order Service'; try restarting transaction")`.
   - Impact: Aborts `bench migrate`, leaving migration incomplete and preventing `seed_equipment_tags()` and `seed_schedule_entry_types()` from executing.
   - Recommended Fix: In `d:\ism\ism\setup.py`, insert `frappe.db.commit()` immediately after `setup_procurement_custom_fields()` (or at the top of `backfill_service_package_models()`). Additionally, move `seed_equipment_tags()` and `seed_schedule_entry_types()` earlier in `after_migrate()` (e.g. alongside `setup_default_ism_settings()`).

2. **Approved Components (Subject to Migration Fix)**:
   - ISM Settings validation rules and boundary rejection: Robust and comprehensive.
   - Decimal fraction auto-conversion (0.80 -> 80.0): Fully functional and idempotent on repeated saves.
   - Seed routine idempotency and custom setting preservation (`enable_technician_capacity=0`, `buffer_padding_minutes=0`, custom hours): Fully verified.

---

## 5. Verification Method

To independently reproduce the deadlock and verify the test harness:

1. **Reproduce `bench migrate` Deadlock**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"
   ```
   *Observed Result*: Exit code 1, `QueryDeadlockError (1020)` at `backfill_service_package_models`.

2. **Run Empirical Stress Harness**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   wsl bash -c "docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python ../apps/ism/scripts/empirical_stress_ism_settings.py"
   ```
   *Observed Result*: All 21 boundary and idempotency tests PASS, demonstrating full coverage of ISM Settings and seed logic.

3. **Run DocType Unit Test Suites**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"
   ```
   *Observed Result*: All 18 DocType tests pass (`OK`).
