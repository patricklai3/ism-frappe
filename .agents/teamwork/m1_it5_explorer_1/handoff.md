# Technical Investigation & Blueprint Handoff: Zero-Churn Conditional Baseline Check for TestISMSettings

**Agent**: `m1_it5_explorer_1` (Milestone 1 Iteration 5 Explorer 1)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it5_explorer_1`  
**Mission**: Formulate an exact blueprint for a zero-churn conditional baseline check in `test_ism_settings.py:setUpClass()` to eliminate MariaDB 1020 `OperationalError` on `tabSingles`.  
**Artifacts Produced**:
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\test_ism_settings.patch` (Clean machine-applicable patch)
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\proposed_test_ism_settings.py` (Drop-in replacement file)

---

## 1. Observation

### 1.1 Verbatim Failure Traced from Audit Forensic Evidence
From `d:\ism\.agents\teamwork\m1_it4_auditor_1\handoff.md` lines 58–82:
```text
Traceback (most recent call last):
  File "/workspace/development/frappe-bench/env/lib/python3.14/site-packages/MySQLdb/connections.py", line 280, in query
    _mysql.connection.query(self, query)
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
```

### 1.2 Existing Code in `test_ism_settings.py`
In `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py` lines 27–48:
```python
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()

		# Establish deterministic canonical baseline state once per test class
		frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()
		super().tearDownClass()
```

### 1.3 Frappe Framework `set_single_value` Mechanism
Inspecting `apps/frappe/frappe/database/database.py` lines 862–878:
```python
	def set_single_value(
		self,
		doctype: str,
		fieldname: str | dict,
		value: str | int | None = None,
		*,
		modified=None,
		modified_by=None,
		update_modified=True,
		debug=False,
	):
		to_update = self._get_update_dict(
			fieldname, value, modified=modified, modified_by=modified_by, update_modified=update_modified
		)

		frappe.db.delete(
			"Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug
		)

		singles_data = ((doctype, key, sbool(value)) for key, value in to_update.items())
		frappe.qb.into("Singles").columns("doctype", "field", "value").insert(*singles_data).run(debug=debug)
		frappe.clear_document_cache(doctype, doctype)
```
When `fieldname` is a dictionary (such as `CANONICAL_ISM_SETTINGS`), `to_update` contains all 12 keys plus `modified` and `modified_by`. It executes `DELETE FROM tabSingles WHERE field IN (...) AND doctype = 'ISM Settings'`, followed by multi-row `INSERT`.

### 1.4 MariaDB `tabSingles` Schema and State
Direct SQL inspection via `SELECT field, value FROM tabSingles WHERE doctype = 'ISM Settings'`:
```text
field                            value
operating_hours_start            08:00:00
operating_hours_end              17:00:00
break_start                      12:00:00
break_end                        12:30:00
default_slot_interval            30
scheduling_horizon_days          30
technician_designation           Technician
enable_technician_capacity       1
estimation_percentile            80.0
estimation_sigma                 0.3
buffer_padding_minutes           0
default_diagnostic_duration      60
```
`tabSingles` has no primary key, only a secondary index on `(doctype, field)`.
The database already contains the complete canonical baseline, seeded by `setup_default_ism_settings()` in `ism/setup.py`.

### 1.5 Dead Ends Tracking Context (`d:\ism\DEAD_ENDS.md`)
- **Iteration 1**: Unhandled transaction locks in `setUp`/`tearDown` caused MariaDB 1020 on open `SELECT ... FOR UPDATE` locks.
- **Iteration 2**: Per-test `frappe.db.commit()` and swallowing errors in `tearDown()` corrupted baseline state with mutated values (`45`, `85.0`, `0`).
- **Iteration 3**: Calling `set_single_value(CANONICAL_ISM_SETTINGS)` and committing in every test's `setUp()` and `tearDown()` triggered 16 bulk DELETE/INSERT cycles per run, leading to secondary index scan deadlocks (error 1020 `ER_CHECKREAD`).
- **Iteration 4**: Calling `set_single_value(CANONICAL_ISM_SETTINGS)` in `setUpClass()` reduced churn to once per run, but still triggered MariaDB 1020 on Run 4 during sequential stress testing due to MVCC conflicts on the unkeyed `tabSingles` table.

---

## 2. Logic Chain

1. **Mechanism of MariaDB 1020 (`HA_ERR_RECORD_CHANGED` / `ER_CHECKREAD`)**:
   - In MariaDB InnoDB under `REPEATABLE READ`, `DELETE` and `UPDATE` operations on tables without a primary key perform secondary index scans (`(doctype, field)` on `tabSingles`).
   - When a DELETE statement scans rows that have recent unpurged undo log records from preceding transactions or concurrent read views, InnoDB raises `HA_ERR_RECORD_CHANGED` (error 1020), which Frappe translates to `frappe.exceptions.QueryDeadlockError`.
   - Read operations (`SELECT`), in contrast, operate purely against MVCC snapshots or clustered index reads and **never** produce error 1020.

2. **Causality of Failure in `setUpClass()`**:
   - `setUpClass()` in `test_ism_settings.py` was unconditionally executing `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` followed by `frappe.db.commit()`.
   - In a 10x sequential stress loop, this forced 10 consecutive executions of `DELETE FROM tabSingles WHERE field IN (...)` and 10 bulk insertions on `tabSingles`.
   - On Run 4, MariaDB's transaction coordinator encountered an MVCC version conflict during the `DELETE` scan, terminating the test run before a single test method executed.

3. **Inherent Redundancy of Unconditional Writes**:
   - The canonical baseline values defined in `CANONICAL_ISM_SETTINGS` are identical to the production defaults seeded by `setup_default_ism_settings()` in `ism/setup.py`.
   - Within `TestISMSettings`:
     - Tests 1–6 and 8 perform read-only assertions or in-memory `new_doc().validate()` checks without database writes.
     - Test 7 (`test_setup_idempotency_preserves_customizations`) mutates `default_slot_interval` and `enable_technician_capacity` in an uncommitted transaction and explicitly executes `frappe.db.rollback()` in its `finally:` block, in addition to the rollback in `tearDown()`.
   - Consequently, `tabSingles` remains in the canonical baseline state across consecutive test runs.
   - Re-writing and committing the identical 12 records on every class initialization is 100% redundant.

4. **Zero-Churn Conditional Check Formulation**:
   - By querying `frappe.db.get_singles_dict("ISM Settings")` (a read-only SELECT query), `setUpClass()` can inspect the current state of `tabSingles` with zero lock contention and zero MVCC version changes.
   - The inspection iterates through all 12 keys of `CANONICAL_ISM_SETTINGS`:
     - If the key is absent (`curr_val is None`), it is collected into `divergent`.
     - If the key is float-valued (`estimation_percentile`, `estimation_sigma`), numeric equivalence is checked via `abs(float(curr_val) - expected_val) > 1e-4` to transparently handle MariaDB string representations (e.g. `'0.3'` vs `0.30`, `'80'` vs `80.0`).
     - If the key is integer-valued (`default_slot_interval`, `scheduling_horizon_days`, `enable_technician_capacity`, `buffer_padding_minutes`, `default_diagnostic_duration`), integer equality `int(curr_val) != expected_val` is evaluated.
     - If string-valued, string equality `str(curr_val) != str(expected_val)` is evaluated.
   - Any value parsing exception (`ValueError`, `TypeError`) safely marks the field as divergent for self-healing.
   - `frappe.db.set_single_value("ISM Settings", divergent)` and `frappe.db.commit()` are executed **only if `divergent` is non-empty**.
   - If `divergent` is empty (the standard case across test runs): **ZERO DML, ZERO DELETES, ZERO INSERTS, and ZERO COMMITS occur**.
   - With zero DML operations executed against `tabSingles`, MariaDB error 1020 is eliminated.

---

## 3. Caveats

- **Scope Boundary**: This investigation and blueprint specifically target `test_ism_settings.py:setUpClass()` and MariaDB 1020 errors on `tabSingles`. It does not modify controller code in `ism/ism/doctype/ism_settings/ism_settings.py` or default initialization in `ism/setup.py`, neither of which exhibits defects.
- **Divergence Self-Healing**: If an external test suite outside of `TestISMSettings` leaves committed mutations in `tabSingles`, the conditional check will detect the specific divergent fields and issue a targeted `set_single_value` call for only those fields, restoring the canonical baseline before `TestISMSettings` runs.

---

## 4. Conclusion & Exact Blueprint

### 4.1 Target File & Location
- **File**: `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`
- **Target Function**: `TestISMSettings.setUpClass(cls)`
- **Line Range**: Lines 35–40

### 4.2 Before vs After Code Diff

#### Before (Lines 35–40):
```python
		# Establish deterministic canonical baseline state once per test class
		frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)
		frappe.clear_cache(doctype="ISM Settings")
		if hasattr(frappe.db, "value_cache"):
			frappe.db.value_cache.clear()
		frappe.db.commit()
```

#### After (Zero-Churn Conditional Check):
```python
		# Zero-churn conditional baseline check: only update tabSingles if baseline diverged
		current_settings = frappe.db.get_singles_dict("ISM Settings")
		divergent = {}
		for fieldname, expected_val in CANONICAL_ISM_SETTINGS.items():
			curr_val = current_settings.get(fieldname) if current_settings else None
			if curr_val is None:
				divergent[fieldname] = expected_val
			elif isinstance(expected_val, float):
				try:
					if abs(float(curr_val) - expected_val) > 1e-4:
						divergent[fieldname] = expected_val
				except (ValueError, TypeError):
					divergent[fieldname] = expected_val
			elif isinstance(expected_val, int):
				try:
					if int(curr_val) != expected_val:
						divergent[fieldname] = expected_val
				except (ValueError, TypeError):
					divergent[fieldname] = expected_val
			else:
				if str(curr_val) != str(expected_val):
					divergent[fieldname] = expected_val

		if divergent:
			frappe.db.set_single_value("ISM Settings", divergent)
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()
			frappe.db.commit()
```

### 4.3 Machine-Applicable Artifacts Available
1. Patch file: `d:\ism\.agents\teamwork\m1_it5_explorer_1\test_ism_settings.patch`
   Verified via `git apply --check .agents/teamwork/m1_it5_explorer_1/test_ism_settings.patch` (exit code 0).
2. Proposed full replacement: `d:\ism\.agents\teamwork\m1_it5_explorer_1\proposed_test_ism_settings.py`

---

## 5. Verification Method

To independently verify this remediation:

1. **Verify Patch Cleanliness**:
   ```powershell
   git apply --check .agents/teamwork/m1_it5_explorer_1/test_ism_settings.patch
   ```
   *Expected result*: Exit code 0, no output.

2. **Apply Patch to Target File**:
   ```powershell
   git apply .agents/teamwork/m1_it5_explorer_1/test_ism_settings.patch
   ```

3. **Verify Pure Python Logic Assertions**:
   Execute the verification test inside the devcontainer:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
   import frappe
   frappe.init(site='development.localhost', sites_path='.')
   frappe.connect()
   from ism.ism.doctype.ism_settings.test_ism_settings import CANONICAL_ISM_SETTINGS

   def compute_divergent(current_settings):
       divergent = {}
       for fieldname, expected_val in CANONICAL_ISM_SETTINGS.items():
           curr_val = current_settings.get(fieldname) if current_settings else None
           if curr_val is None:
               divergent[fieldname] = expected_val
           elif isinstance(expected_val, float):
               try:
                   if abs(float(curr_val) - expected_val) > 1e-4:
                       divergent[fieldname] = expected_val
               except (ValueError, TypeError):
                   divergent[fieldname] = expected_val
           elif isinstance(expected_val, int):
               try:
                   if int(curr_val) != expected_val:
                       divergent[fieldname] = expected_val
               except (ValueError, TypeError):
                   divergent[fieldname] = expected_val
           else:
               if str(curr_val) != str(expected_val):
                   divergent[fieldname] = expected_val
       return divergent

   real_settings = frappe.db.get_singles_dict('ISM Settings')
   assert compute_divergent(real_settings) == {}
   print('Verification Passed: Baseline is zero-churn identical.')
   "
   ```
   *Expected result*: `Verification Passed: Baseline is zero-churn identical.`

4. **Execute 10x Sequential Stress Test**:
   ```powershell
   powershell -Command "1..10 | ForEach-Object { Write-Host ""=== RUN `$_ ===""; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if (`$LASTEXITCODE -ne 0) { Write-Host ""RUN `$_ FAILED""; exit 1 } }"
   ```
   *Expected result*: All 10 runs exit with code 0 (8/8 tests passed on each run, total 80/80 tests, 0 errors, 0 deadlocks).

5. **Invalidation Conditions**:
   - Any MariaDB 1020 `OperationalError` during `setUpClass()`.
   - Any test failure in `TestISMSettings`.
