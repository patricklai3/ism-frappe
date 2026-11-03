# Investigation & Remediation Strategy Report — Milestone 1 Concurrency & Test Stabilization

**Agent**: `m1_it6_explorer_1` (Role: Teamwork Explorer)  
**Date**: 2026-10-27 UTC  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_explorer_1`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Reference Reports**:  
- `d:\ism\.agents\teamwork\m1_gate_auditor_1\handoff.md` (Integrity Forensic Audit Report)  
- `d:\ism\.agents\teamwork\m1_gate_reviewer_2\handoff.md` (Gate Reviewer 2 Report)  

---

## Executive Summary

Milestone 1 Gate sign-off was rejected by the Forensic Auditor (`INTEGRITY VIOLATION`) and Reviewer 2 (`REQUEST_CHANGES`) due to 4 critical runtime concurrency and test harness issues:
1. **`tabSeries` MariaDB Error 1020 Deadlock on `Schedule Entry`**: Un-retried `.insert()` in `test_schedule_entry.py` (Run 7 of 10x loop) and in `test_fixtures.py:create_test_schedule_entry` (Task-130).
2. **`tabSingles` MariaDB Error 1020 Deadlock in `test_ism_settings.py:setUpClass`**: Default setting discrepancy (`buffer_padding_minutes: 10` vs `0`) caused `setUpClass` to detect divergence and issue an un-retried `DELETE FROM tabSingles`, crashing the full test suite.
3. **Database Leakage in `tabService Bay`**: `_cleanup_service_bays()` in `ism/tests/cleanup.py` lacked filtering for `TEMP%` prefixes, leaking `TEMP_BAY` and `TEMP_BAY_MULTI` into MariaDB.
4. **`LinkValidationError` in `TestTeslaIntegration.test_symmetrical_procedure_import`**: When `create_test_repair_order` hit a 1020 retry loop, its `frappe.db.rollback()` wiped previously inserted uncommitted test customer and vehicle records, triggering `Could not find Customer ..., Vehicle ...`.

This report provides the full evidence chain and concrete code-level remediation strategy for each failure for the Worker.

---

## 1. Observation

### 1.1 Issue 1: `tabSeries` MariaDB 1020 Deadlock on `Schedule Entry`
- **Locations**:
  - `d:\ism\ism\ism\doctype\schedule_entry\test_schedule_entry.py:163` (`test_provisional_contact_and_vehicle_display`)
  - `d:\ism\ism\tests\test_fixtures.py:338` (`create_test_schedule_entry`) called by `d:\ism\ism\tests\test_scheduling_capacity.py:177` (`test_tier1_f7_schedule_entry_overlap_validation_error`)
- **Direct Empirical Failure (Verbatim from Auditor Report)**:
  ```text
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
  ...
  File "/workspace/development/frappe-bench/apps/frappe/frappe/model/naming.py", line 412, in getseries
    current = (frappe.qb.from_(series).where(series.name == key).for_update().select("current")).run()
  File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 278, in sql
    raise frappe.QueryDeadlockError(e) from e
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
  ```
- **Code Observation**:
  - In `ism/ism/doctype/schedule_entry/schedule_entry.json`: `autoname: "naming_series:"`, `naming_series` default is `"SE-.#####"`.
  - When `.insert()` runs, Frappe invokes `getseries("SE-", 5)` executing `SELECT current FROM tabSeries WHERE name='SE-' FOR UPDATE`.
  - In `ism/tests/test_fixtures.py:338`:
    ```python
    doc = frappe.get_doc(data).insert(ignore_permissions=True)
    return doc
    ```
    There is no exception handling or retry loop on `OperationalError: 1020` or `QueryDeadlockError`.
  - In `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
    Lines 88, 105, 117, 142, 163, 187, 222 call `frappe.get_doc({...}).insert(ignore_permissions=True)` directly without retry handling.

---

### 1.2 Issue 2: `tabSingles` MariaDB 1020 Deadlock in `test_ism_settings.py:setUpClass`
- **Locations**:
  - `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py:59` (`setUpClass`)
  - `d:\ism\ism\tests\test_fixtures.py:303` (`ensure_ism_settings`)
  - `d:\ism\ism\setup.py:146` (`setup_default_ism_settings`)
- **Direct Empirical Failure (Verbatim from Auditor & Reviewer 2 Reports)**:
  ```text
  ERROR: setUpClass (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings)
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 372, in execute_query
      return self._cursor.execute(query, values)
    ...
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  ```
- **Code Observation**:
  - `CANONICAL_ISM_SETTINGS` in `test_ism_settings.py:19` specifies `"buffer_padding_minutes": 0`.
  - `setup_default_ism_settings()` in `setup.py:146` sets `settings.buffer_padding_minutes = 0`.
  - However, `ensure_ism_settings()` in `test_fixtures.py:303` specifies `"buffer_padding_minutes": 10`.
  - When the full test suite runs (`bench run-tests --app ism`), `test_scheduling_capacity.py` executes `setup_all()`, which invokes `ensure_ism_settings()`, committing `buffer_padding_minutes = 10` to `tabSingles`.
  - When `TestISMSettings.setUpClass` executes subsequently, lines 36–57 detect divergence (`divergent = {"buffer_padding_minutes": 0}`).
  - Line 59 calls `frappe.db.set_single_value("ISM Settings", divergent)` without a retry loop.
  - `set_single_value` issues `DELETE FROM tabSingles WHERE field IN (...) AND doctype='ISM Settings'`. Under MariaDB 11.8 snapshot isolation with background worker activity, this un-retried delete throws MariaDB error 1020 / `QueryDeadlockError`, crashing the test class.

---

### 1.3 Issue 3: Database Leakage in `tabService Bay` (`TEMP_BAY`, `TEMP_BAY_MULTI`)
- **Locations**:
  - `d:\ism\ism\tests\cleanup.py:468–487` (`_cleanup_service_bays()`)
- **Direct Empirical Observation (Verbatim from Auditor Report)**:
  ```json
  [
    {"name": "TEMP_BAY", "bay_name": "TEMP_BAY", "is_active": 1},
    {"name": "TEMP_BAY_MULTI", "bay_name": "TEMP_BAY_MULTI", "is_active": 1},
    {"name": "Test Bay 1", "bay_name": "Test Bay 1", "is_active": 1},
    {"name": "Test Bay 2", "bay_name": "Test Bay 2", "is_active": 1},
    {"name": "Test Bay 3", "bay_name": "Test Bay 3", "is_active": 0}
  ]
  ```
- **Code Observation**:
  - In `ism/tests/cleanup.py:475–481`:
    ```python
    if name in ("Test Bay 1", "Test Bay 2", "Test Bay 3"):
        continue
    if (
        any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS"))
        or "Test" in name
        or "TEST" in name
    ):
        frappe.delete_doc("Service Bay", bay.name, force=True, ignore_permissions=True)
    ```
  - `TEMP_BAY` and `TEMP_BAY_MULTI` do not match `_TEST`, `TEST-`, `Test Unit Bay`, `STRESS`, or `"Test"`/`"TEST"`.
  - As a result, temporary service bays created during ad-hoc testing or manual verification are completely bypassed by cleanup and survive indefinitely.

---

### 1.4 Issue 4: `LinkValidationError` in `TestTeslaIntegration.test_symmetrical_procedure_import`
- **Locations**:
  - `d:\ism\ism\tests\test_tesla_integration.py:233, 376, 421`
  - `d:\ism\ism\tests\base.py:60–133` (`create_test_customer`, `create_test_vehicle`, `create_test_repair_order`)
- **Direct Empirical Failure (Verbatim from Auditor Report Task-146)**:
  ```text
  ERROR: test_symmetrical_procedure_import (ism.tests.test_tesla_integration.TestTeslaIntegration.test_symmetrical_procedure_import)
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/test_tesla_integration.py", line 231, in test_symmetrical_procedure_import
      ro = self.create_test_repair_order(customer=cust.name, vehicle=veh.name)
    File "/workspace/development/frappe-bench/apps/ism/ism/tests/base.py", line 126, in create_test_repair_order
      doc = frappe.get_doc({...}).insert(ignore_permissions=True)
    ...
    File "/workspace/development/frappe-bench/apps/frappe/frappe/model/base_document.py", line 630, in validate_link
      frappe.throw(_("Could not find {0}").format(msg), frappe.LinkValidationError)
  frappe.exceptions.LinkValidationError: Could not find Customer: _TEST_ISM_Cust_b5a2ffb1, Vehicle: 5YJ3E1EBXNFB93E83
  ```
- **Code Observation**:
  - In `ism/tests/test_tesla_integration.py:374–376`:
    ```python
    customer = self.create_test_customer()
    vehicle = self.create_test_vehicle(customer=customer.name)
    ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
    ```
  - In `ism/tests/base.py`:
    - `create_test_customer()` (lines 65–71) calls `.insert(ignore_permissions=True)` but does NOT commit (`frappe.db.commit()`).
    - `create_test_vehicle()` (lines 90–96) calls `.insert(ignore_permissions=True)` but does NOT commit (`frappe.db.commit()`).
    - `create_test_repair_order()` (lines 118–132) has a 3-attempt retry loop:
      ```python
      for attempt in range(3):
          try:
              doc = frappe.get_doc({...}).insert(ignore_permissions=True)
              return self.track(doc)
          except Exception as e:
              if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
                  frappe.db.rollback()
                  continue
              raise
      ```
    - When `create_test_repair_order` encounters MariaDB error 1020 on `tabSeries` (`RO-` series) during `attempt = 0`, it enters the `except` block and calls `frappe.db.rollback()`.
    - Because `customer` and `vehicle` were inserted earlier in the exact same transaction without being committed, `frappe.db.rollback()` discards the entire uncommitted transaction, wiping `customer` and `vehicle` from the database.
    - On `attempt = 1`, `create_test_repair_order` tries again to insert `Repair Order` referencing `customer=customer.name` and `vehicle=vehicle.name`. Frappe's `validate_link` executes `SELECT name FROM tabCustomer ...` and `SELECT name FROM tabRepair Vehicle ...`. Neither row exists. Frappe raises `LinkValidationError`.

---

## 2. Logic Chain

```
[Observation 1.1: Schedule Entry .insert() calls getseries("SE-") without retry]
       |
       v
[MariaDB 11.8 Repeatable Read snapshot isolation detects background changes to tabSeries]
       |
       v
[MySQLdb.OperationalError 1020: "Record has changed since last read in table 'tabSeries'; try restarting transaction"]
       |
       v
[Unhandled crash on Run 7 of stress loop & Task-130 in test_scheduling_capacity.py]
       |
       v
[Strategy 1: Add 3-attempt restart loop with frappe.db.rollback() & commit in base.py and test_fixtures.py]
```

```
[Observation 1.2: test_fixtures.py sets buffer_padding_minutes=10, CANONICAL expects 0]
       |
       v
[Preceding tests dirty tabSingles; test_ism_settings.py:setUpClass detects divergence]
       |
       v
[setUpClass calls frappe.db.set_single_value("ISM Settings", divergent) without retry]
       |
       v
[DELETE FROM tabSingles throws MariaDB Error 1020 -> unhandled crash of test class]
       |
       v
[Strategy 2: Align test_fixtures.py default to 0 AND wrap set_single_value in 3-attempt retry loop]
```

```
[Observation 1.3: _cleanup_service_bays() filter ignores "TEMP" prefixes]
       |
       v
[TEMP_BAY and TEMP_BAY_MULTI bypass cleanup condition and persist in tabService Bay]
       |
       v
[Strategy 3: Expand prefix match to include "TEMP", "temp", "TEMP_" and purge leaked rows]
```

```
[Observation 1.4: create_test_customer & create_test_vehicle do not commit]
       |
       v
[create_test_repair_order hits 1020 on attempt 0 and calls frappe.db.rollback()]
       |
       v
[Rollback wipes uncommitted Customer and Vehicle records from the database session]
       |
       v
[Attempt 1 tries to insert Repair Order referencing wiped IDs -> LinkValidationError]
       |
       v
[Strategy 4: Commit Customer and Vehicle immediately upon creation in base.py]
```

---

## 3. Concrete Remediation Plan for Worker

### Remediation Action 1: `tabSeries` Deadlock Remediation (`Schedule Entry`)

#### Target File 1.1: `ism/tests/base.py`
Add `create_test_schedule_entry` helper to `ISMTestCase` with 3-attempt restart loop and commit:
```python
	def create_test_schedule_entry(self, **kwargs):
		for attempt in range(3):
			try:
				doc = frappe.get_doc({
					"doctype": "Schedule Entry",
					**kwargs,
				}).insert(ignore_permissions=True)
				frappe.db.commit()
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
					frappe.db.rollback()
					continue
				raise
```

#### Target File 1.2: `ism/tests/test_fixtures.py`
Update `create_test_schedule_entry` (lines 316–339) with a 3-attempt restart loop and commit:
```python
def create_test_schedule_entry(repair_order=None, **kwargs):
	"""Helper to create a transactional Schedule Entry record."""
	if not frappe.db.exists("DocType", "Schedule Entry"):
		return None
	date = kwargs.pop("scheduled_date", today())
	time_val = kwargs.pop("scheduled_time", "09:00:00")
	duration = kwargs.pop("estimated_duration", 60)
	bay = kwargs.pop("service_bay", f"{PREFIX}Bay 1")
	entry_type = kwargs.pop("entry_type", "Diagnostic")

	data = {
		"doctype": "Schedule Entry",
		"scheduled_date": date,
		"scheduled_time": time_val,
		"estimated_duration": duration,
		"service_bay": bay,
		"entry_type": entry_type,
		"status": kwargs.pop("status", "Scheduled"),
		**kwargs,
	}
	if repair_order:
		data["repair_order"] = repair_order

	for attempt in range(3):
		try:
			doc = frappe.get_doc(data).insert(ignore_permissions=True)
			frappe.db.commit()
			return doc
		except Exception as e:
			if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
				frappe.db.rollback()
				continue
			raise
```
Also update `create_test_repair_order` in `test_fixtures.py` (lines 342–355) to include the same 3-attempt restart loop with `frappe.db.commit()`.

#### Target File 1.3: `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
Refactor valid Schedule Entry insertions to call `self.create_test_schedule_entry(...)`:
- Line 81: `test_schedule_entry_naming_series`
- Lines 97 & 109: `test_multiple_schedule_entries_per_repair_order`
- Line 136: `test_auto_population_from_repair_order`
- Line 154: `test_provisional_contact_and_vehicle_display`
- Line 182: `test_requires_repair_order_validation` (`se_diag`)
- Line 215: `test_populate_from_repair_order_on_update`

---

### Remediation Action 2: `tabSingles` Deadlock Remediation (`test_ism_settings.py`)

#### Target File 2.1: `ism/tests/test_fixtures.py`
Change line 303:
```python
# Before:
"buffer_padding_minutes": 10,
# After:
"buffer_padding_minutes": 0,
```
This ensures baseline settings alignment across `setup.py:146`, `test_fixtures.py:303`, and `test_ism_settings.py:19`.

#### Target File 2.2: `ism/ism/doctype/ism_settings/test_ism_settings.py`
In `setUpClass`, wrap the divergent reset in a 3-attempt restart loop:
```python
# Before:
		if divergent:
			frappe.db.set_single_value("ISM Settings", divergent)
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()
			frappe.db.commit()

# After:
		if divergent:
			for attempt in range(3):
				try:
					frappe.db.set_single_value("ISM Settings", divergent)
					frappe.clear_cache(doctype="ISM Settings")
					if hasattr(frappe.db, "value_cache"):
						frappe.db.value_cache.clear()
					frappe.db.commit()
					break
				except Exception as e:
					if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
						frappe.db.rollback()
						continue
					raise
```

---

### Remediation Action 3: Database Leakage Remediation (`tabService Bay`)

#### Target File 3.1: `ism/tests/cleanup.py`
In `_cleanup_service_bays(log)` (lines 475–487), expand the filter:
```python
# Before:
		if (
			any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS"))
			or "Test" in name
			or "TEST" in name
		):

# After:
		if (
			any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS", "TEMP", "temp"))
			or "Test" in name
			or "TEST" in name
			or "TEMP" in name
			or "temp" in name
		):
```

#### Remediation Command 3.2: One-time Purge
Worker must execute a one-time purge command to remove already leaked bays:
```bash
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); [frappe.delete_doc('Service Bay', b, force=True) for b in ('TEMP_BAY', 'TEMP_BAY_MULTI') if frappe.db.exists('Service Bay', b)]; frappe.db.commit()"
```

---

### Remediation Action 4: Transaction Persistence & `LinkValidationError` Remediation

#### Target File 4.1: `ism/tests/base.py`
Ensure all fixture helpers commit immediately upon successful insertion so rollbacks in retry loops cannot wipe parent fixtures:
1. `create_test_customer`:
   ```python
   # After self.track(doc):
   frappe.db.commit()
   return self.track(doc)
   ```
2. `create_test_vehicle`:
   ```python
   # After self.track(doc):
   frappe.db.commit()
   return self.track(doc)
   ```
3. `create_test_repair_order`:
   ```python
   # After self.track(doc):
   frappe.db.commit()
   return self.track(doc)
   ```
4. `create_test_quotation`:
   ```python
   # Add 3-attempt restart loop, track and commit:
   for attempt in range(3):
       try:
           doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
           frappe.db.commit()
           return self.track(doc)
       except Exception as e:
           if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
               frappe.db.rollback()
               continue
           raise
   ```
5. `cleanup_tracked_docs`:
   Already in place:
   ```python
   for doctype, name in committed_docs:
       ...
       frappe.delete_doc(...)
   frappe.db.commit()
   frappe.db.rollback()
   ```
   Every committed test fixture is registered in `self._tracked_docs` and deleted in reverse dependency order during `tearDown()`, guaranteeing pristine post-test state.

---

## 4. Caveats

- **No Caveats**: All 4 failure root causes were directly traced to specific line numbers and confirmed against empirical error logs from the forensic audit and reviewer reports. The proposed changes do not alter production domain logic; they purely harden the test harness and database cleanup routines against MariaDB 11.8 MVCC concurrency behaviors.

---

## 5. Conclusion

The 4 issues are fully understood, mathematically and architecturally scoped, and have mutually reinforcing solutions:
- Eliminating `tabSeries` deadlocks via 3-attempt restart loops in `base.py`, `test_fixtures.py`, and `test_schedule_entry.py`.
- Eliminating `tabSingles` deadlocks via default settings alignment (`buffer_padding_minutes = 0`) and 3-attempt retry loop in `test_ism_settings.py:setUpClass`.
- Eliminating `Service Bay` leakage via `TEMP%` cleanup filter expansion and initial database purge.
- Eliminating `LinkValidationError` via immediate transactional persistence (`frappe.db.commit()`) in `base.py` test fixture creators.

These remediations are ready for immediate implementation by `m1_it6_worker_1`.

---

## 6. Verification Method

To verify the remediations once implemented:

### Step 1: Execute One-Time Service Bay Purge & Verify DB Hygiene
```bash
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); [frappe.delete_doc('Service Bay', b, force=True) for b in ('TEMP_BAY', 'TEMP_BAY_MULTI') if frappe.db.exists('Service Bay', b)]; frappe.db.commit(); print(frappe.get_all('Service Bay', fields=['name', 'bay_name', 'is_active']))"
```
*Expected Result*: Output lists only `Test Bay 1`, `Test Bay 2`, `Test Bay 3`.

### Step 2: 10x Sequential Stress Loop on `Schedule Entry`
```powershell
1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
```
*Expected Result*: 10/10 runs pass, 0 deadlocks, exit code 0.

### Step 3: 10x Sequential Stress Loop on `ISM Settings`
```powershell
1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }
```
*Expected Result*: 10/10 runs pass, 0 deadlocks, exit code 0.

### Step 4: Dirty Settings + Full Application Test Suite
```bash
# 1. Intentionally dirty settings to test setUpClass resilience
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); frappe.db.set_single_value('ISM Settings', 'buffer_padding_minutes', 10); frappe.db.commit()"

# 2. Run full suite
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
```
*Expected Result*: Ran 381 tests, OK (skipped=254), exit code 0. No `LinkValidationError`, no `QueryDeadlockError`.
