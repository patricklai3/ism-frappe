# Handoff Report — Milestone 1 Iteration 6 Explorer 2

**Agent ID**: `m1_it6_explorer_2`  
**Role**: `teamwork_preview_explorer` (Explorer / Synthesis)  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_explorer_2`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Date**: 2026-10-27 UTC  
**Handoff Type**: Hard (Investigation & Modification Plan Complete)  

---

## Executive Summary

Milestone 1 Gate sign-off was rejected due to an Integrity Violation (`m1_gate_auditor_1/handoff.md`) and Request Changes (`m1_gate_reviewer_2/handoff.md`) arising from 4 specific empirical runtime failures:
1. `tabSeries` deadlock (`QueryDeadlockError: 1020`) on `Schedule Entry.insert()` and `create_test_schedule_entry()` during stress loops (Run 7) and full suite runs.
2. `tabSingles` deadlock (`QueryDeadlockError: 1020`) during `TestISMSettings.setUpClass` caused by test fixture parameter divergence (`buffer_padding_minutes = 10` in `test_fixtures.py` vs `0` in canonical settings) coupled with an un-retried `DELETE FROM tabSingles`.
3. `LinkValidationError: Could not find Customer: ..., Vehicle: ...` in `test_tesla_integration.py` caused by uncommitted fixture records being inadvertently purged by transaction rollbacks (`frappe.db.rollback()`) inside retry loops.
4. Database leakage of residual `Service Bay` records (`TEMP_BAY`, `TEMP_BAY_MULTI`) bypassing `_cleanup_service_bays()` in `cleanup.py`.

This report provides the exhaustive forensic analysis, causal chains, and an exact file-by-file code modification plan with before/after snippets for the implementing worker.

---

## 1. Observation

### 1.1 Direct Observations from Audit & Review Reports

1. **`tabSeries` Deadlock on Run 7 of Schedule Entry 10x Stress Loop**:
   - Source: `m1_gate_auditor_1/handoff.md:58-104`
   - Command:
     ```powershell
     1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
     ```
   - Verbatim traceback:
     ```text
     File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 272, in sql
       self.execute_query(query, values)
       query = 'SELECT `current` FROM `tabSeries` WHERE `name`=%(param1)s FOR UPDATE'
       values = {'param1': 'SE-'}
     MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
     frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
     File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/schedule_entry/test_schedule_entry.py", line 163, in test_provisional_contact_and_vehicle_display
       }).insert(ignore_permissions=True)
     ```

2. **`tabSeries` Deadlock in Full Application Test Suite**:
   - Source: `m1_gate_auditor_1/handoff.md:117-140`
   - Command: `bench --site development.localhost run-tests --app ism` (Task-130)
   - Verbatim traceback:
     ```text
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
     frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")
     ```

3. **`tabSingles` Divergence Deadlock in `TestISMSettings.setUpClass`**:
   - Source: `m1_gate_auditor_1/handoff.md:168-191`, `m1_gate_reviewer_2/handoff.md:104-121`
   - Command: `bench --site development.localhost run-tests --app ism` (Task-92, Task-102, Task-146)
   - Verbatim traceback:
     ```text
     ERROR: setUpClass (ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings)
     File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/ism_settings/test_ism_settings.py", line 59, in setUpClass
       frappe.db.set_single_value("ISM Settings", divergent)
     File "/workspace/development/frappe-bench/apps/frappe/frappe/database/database.py", line 870, in set_single_value
       frappe.db.delete("Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype}, debug=debug)
     MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
     frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
     ```

4. **`LinkValidationError` in `TestTeslaIntegration.test_symmetrical_procedure_import`**:
   - Source: `m1_gate_auditor_1/handoff.md:153-165`
   - Verbatim traceback:
     ```text
     ERROR: test_symmetrical_procedure_import (ism.tests.test_tesla_integration.TestTeslaIntegration.test_symmetrical_procedure_import)
     File "/workspace/development/frappe-bench/apps/ism/ism/tests/test_tesla_integration.py", line 376, in test_symmetrical_procedure_import
       ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
     File "/workspace/development/frappe-bench/apps/ism/ism/tests/base.py", line 126, in create_test_repair_order
       doc = frappe.get_doc({...}).insert(ignore_permissions=True)
     File "/workspace/development/frappe-bench/apps/frappe/frappe/model/base_document.py", line 630, in validate_link
       frappe.throw(_("Could not find {0}").format(msg), frappe.LinkValidationError)
     frappe.exceptions.LinkValidationError: Could not find Customer: _TEST_ISM_Cust_b5a2ffb1, Vehicle: 5YJ3E1EBXNFB93E83
     ```

5. **Leaked Residual Service Bays in Database**:
   - Source: `m1_gate_auditor_1/handoff.md:201-210`
   - Directly queried records:
     `{"name": "TEMP_BAY", "bay_name": "TEMP_BAY", "is_active": 1}`
     `{"name": "TEMP_BAY_MULTI", "bay_name": "TEMP_BAY_MULTI", "is_active": 1}`
   - `ism/tests/cleanup.py:477-481` only filters for:
     `any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS")) or "Test" in name or "TEST" in name`
     Omission: bays starting with or named `TEMP_BAY` or `TEMP` are ignored.

### 1.2 Inspection of Existing Codebase Implementations

1. **`ism/tests/test_fixtures.py` (lines 292-305)**:
   ```python
   fields = {
       "operating_hours_start": "08:00:00",
       "operating_hours_end": "17:00:00",
       "break_start": "12:00:00",
       "break_end": "12:30:00",
       "default_slot_interval": 30,
       "scheduling_horizon_days": 30,
       "enable_technician_capacity": 1,
       "technician_designation": "Technician",
       "estimation_percentile": 0.80,  # Line 301: canonical is 80.0
       "buffer_padding_minutes": 10,   # Line 303: canonical is 0
       "default_diagnostic_duration": 60,
   }
   ```
   Lines 316-339: `create_test_schedule_entry()` calls `.insert(ignore_permissions=True)` directly on line 338 with zero retry logic.

2. **`ism/ism/doctype/ism_settings/test_ism_settings.py` (lines 8-21, 58-64)**:
   Canonical settings expect `buffer_padding_minutes: 0` and `estimation_percentile: 80.0`.
   Lines 58-64:
   ```python
   if divergent:
       frappe.db.set_single_value("ISM Settings", divergent)
       frappe.clear_cache(doctype="ISM Settings")
       if hasattr(frappe.db, "value_cache"):
           frappe.db.value_cache.clear()
       frappe.db.commit()
   ```
   No retry loop exists around `set_single_value`.

3. **`ism/tests/base.py` (lines 57-133)**:
   `create_test_customer()` (lines 57-76) and `create_test_vehicle()` (lines 78-101) insert documents but DO NOT call `frappe.db.commit()`.
   `create_test_repair_order()` (lines 117-132) has a retry loop:
   ```python
   except Exception as e:
       if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
           frappe.db.rollback()
           continue
       raise
   ```
   If attempt 0 encounters error 1020, `frappe.db.rollback()` executes, which rolls back the uncommitted `Customer` and `Repair Vehicle`. Attempt 1 then throws `LinkValidationError`.

4. **`ism/ism/doctype/schedule_entry/schedule_entry.py`**:
   `ScheduleEntry` relies on Frappe's `naming_series` without defining an `autoname(self)` method.
   Inspection of `frappe.model.naming.set_new_name` confirms:
   `doc.run_method("autoname")` is executed BEFORE `set_name_from_naming_options`.

---

## 2. Logic Chain

1. **`tabSeries` Deadlock Mechanics (Focus Area 1)**:
   - When any `Schedule Entry` is inserted, Frappe invokes `getseries("SE-", 5)`.
   - `getseries()` executes `SELECT current FROM tabSeries WHERE name = 'SE-' FOR UPDATE`.
   - Under MariaDB InnoDB `REPEATABLE READ`, if another concurrent connection (such as background `bench worker` or preceding test transactions) has modified `tabSeries`, executing `SELECT ... FOR UPDATE` raises MariaDB Error 1020 (`ER_CHECKREAD`).
   - In MariaDB, Error 1020 marks the transaction read view as permanently stale. Any statement issued on that table will continue to fail with 1020 until the transaction is ended.
   - Therefore, recovery MUST issue `frappe.db.rollback()` before attempting insertion again.
   - Currently, neither `create_test_schedule_entry()` in `test_fixtures.py` nor `test_schedule_entry.py` nor `ScheduleEntry` controller handles 1020, leading directly to the crashes observed on Run 7 and Task-130.

2. **`tabSingles` Divergence Deadlock Mechanics (Focus Area 2)**:
   - In full test suite runs (`bench run-tests --app ism`), `test_scheduling_capacity.py` and `test_estimation_math.py` run before `test_ism_settings.py`.
   - In their `setUp()`, `setup_all()` calls `test_fixtures.py:ensure_ism_settings()`.
   - Because `ensure_ism_settings()` hardcodes `buffer_padding_minutes = 10` and `estimation_percentile = 0.80`, it mutates `tabSingles` away from the canonical values (`0` and `80.0`) and commits.
   - When `TestISMSettings.setUpClass` runs later, it compares `current_settings` against `CANONICAL_ISM_SETTINGS`.
   - It detects that `buffer_padding_minutes` is `10` instead of `0`, populating `divergent = {'buffer_padding_minutes': 0}`.
   - It calls `frappe.db.set_single_value("ISM Settings", divergent)`.
   - `set_single_value` executes `DELETE FROM tabSingles WHERE field IN ('buffer_padding_minutes', 'modified', 'modified_by') AND doctype='ISM Settings'`.
   - Because `tabSingles` was recently updated by preceding tests and background workers, this un-retried delete encounters row version divergence, throwing MariaDB Error 1020.
   - The lack of both baseline convergence (in `test_fixtures.py`) and a transaction restart retry loop (in `setUpClass`) causes this crash.

3. **`LinkValidationError` Mechanics (Focus Area 3)**:
   - In `test_symmetrical_procedure_import`, `create_test_customer()` and `create_test_vehicle()` are called sequentially.
   - In `base.py`, neither helper commits after insertion.
   - When `create_test_repair_order()` runs attempt 0, it hits a 1020 deadlock on `tabSeries` (on series `RO-`).
   - The `except Exception as e:` handler catches 1020 and calls `frappe.db.rollback()`.
   - Because `customer` and `vehicle` were never committed, `rollback()` purges them from the database.
   - On attempt 1, `create_test_repair_order()` calls `Repair Order.insert()`.
   - Frappe runs `validate_link()`, queries `tabCustomer` and `tabRepair Vehicle`, finds neither, and raises `LinkValidationError: Could not find Customer: ..., Vehicle: ...`.
   - This proves that fixture persistence (`frappe.db.commit()`) is required immediately upon creating test prerequisite records.

4. **`TEMP_BAY` Residual Leakage Mechanics (Focus Area 4)**:
   - `_cleanup_service_bays()` in `ism/tests/cleanup.py` specifically deletes bays whose names start with `_TEST`, `TEST-`, `Test Unit Bay`, `STRESS` or contain `Test`/`TEST`.
   - Test bays named `TEMP_BAY` or `TEMP_BAY_MULTI` do not match any of these patterns.
   - `_cleanup_schedule_entries()` also omits `TEMP` from its bay filter.
   - As a result, `TEMP_BAY` and `TEMP_BAY_MULTI` survive full test suite teardown, violating database hygiene invariants.

---

## 3. Caveats

- **Isolated vs Full Suite Behavior**: Isolated test runs (`--doctype 'ISM Settings'`) pass because `divergent` evaluates to empty when no preceding test dirties `tabSingles`. The failures occur exclusively under cross-suite and stress execution.
- **Core Frappe Framework Code**: All modifications are strictly confined to `apps/ism` (`schedule_entry.py`, `test_schedule_entry.py`, `test_fixtures.py`, `test_ism_settings.py`, `base.py`, `cleanup.py`, `test_tesla_integration.py`). Zero modifications to Frappe core or ERPNext core are needed or permitted.
- **No Production Code Mocking**: Retry loops wrap actual database calls and catch genuine MariaDB engine exceptions (`OperationalError: 1020` / `QueryDeadlockError`), strictly preserving production validation rules and domain invariants.

---

## 4. Conclusion & Actionable Remediation Plan

### 4.1 Remediation Summary Table

| Focus Area | Target File | Line(s) | Remediation Action |
|---|---|---|---|
| 1. `tabSeries` Deadlock | `ism/ism/doctype/schedule_entry/schedule_entry.py` | 10–55 | Add `autoname(self)` with 3-attempt restart loop on 1020 deadlock |
| 1. `tabSeries` Deadlock | `ism/tests/test_fixtures.py` | 316–339 | Wrap `create_test_schedule_entry()` in 3-attempt restart loop + commit |
| 1. `tabSeries` Deadlock | `ism/ism/doctype/schedule_entry/test_schedule_entry.py` | 8–180 | Add `_insert_with_retry()` helper; commit prerequisites in `setUp()` |
| 2. `tabSingles` Divergence | `ism/tests/test_fixtures.py` | 301, 303 | Change defaults: `buffer_padding_minutes: 0`, `estimation_percentile: 80.0` |
| 2. `tabSingles` Deadlock | `ism/ism/doctype/ism_settings/test_ism_settings.py` | 58–64 | Wrap `frappe.db.set_single_value` in 3-attempt restart loop |
| 3. `LinkValidationError` | `ism/tests/base.py` | 57–133 | Add `frappe.db.commit()` in `create_test_customer`, `vehicle`, and `repair_order` |
| 3. `LinkValidationError` | `ism/tests/test_tesla_integration.py` | 233, 376 | Add `frappe.db.commit()` before creating Repair Order |
| 4. `TEMP_BAY` Leakage | `ism/tests/cleanup.py` | 282–289, 468–487 | Add `TEMP` pattern to `_cleanup_schedule_entries` and `_cleanup_service_bays` |
| 4. `TEMP_BAY` Leakage | `ism/tests/test_fixtures.py` | 357–373 | Add `TEMP%` and `TEST_PREFIX%` filters to `teardown_transactional()` |

---

### 4.2 Exact Code Modifications (Before vs After)

#### Target File 1: `ism/ism/doctype/schedule_entry/schedule_entry.py`
Add `autoname(self)` directly to `ScheduleEntry` to intercept naming series generation at the controller level:

```python
<<<<
class ScheduleEntry(Document):
	def get_display_customer(self) -> str:
====
class ScheduleEntry(Document):
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

	def get_display_customer(self) -> str:
>>>>
```

---

#### Target File 2: `ism/tests/test_fixtures.py`

**Change 2A: Baseline Settings Convergence (lines 301–303)**:
```python
<<<<
			"estimation_percentile": 0.80,
			"estimation_sigma": 0.30,
			"buffer_padding_minutes": 10,
====
			"estimation_percentile": 80.0,
			"estimation_sigma": 0.30,
			"buffer_padding_minutes": 0,
>>>>
```

**Change 2B: Retry Loop in `create_test_schedule_entry()` (lines 316–340)**:
```python
<<<<
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
	doc = frappe.get_doc(data).insert(ignore_permissions=True)
	return doc
====
def create_test_schedule_entry(repair_order=None, **kwargs):
	"""Helper to create a transactional Schedule Entry record with 3-attempt restart loop on error 1020."""
	if not frappe.db.exists("DocType", "Schedule Entry"):
		return None
	date = kwargs.pop("scheduled_date", today())
	time_val = kwargs.pop("scheduled_time", "09:00:00")
	duration = kwargs.pop("estimated_duration", 60)
	bay = kwargs.pop("service_bay", f"{PREFIX}Bay 1")
	entry_type = kwargs.pop("entry_type", "Diagnostic")
	status = kwargs.pop("status", "Scheduled")

	for attempt in range(3):
		try:
			data = {
				"doctype": "Schedule Entry",
				"scheduled_date": date,
				"scheduled_time": time_val,
				"estimated_duration": duration,
				"service_bay": bay,
				"entry_type": entry_type,
				"status": status,
				**kwargs,
			}
			if repair_order:
				data["repair_order"] = repair_order
			doc = frappe.get_doc(data).insert(ignore_permissions=True)
			frappe.db.commit()
			return doc
		except Exception as e:
			if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
				frappe.db.rollback()
				continue
			raise
>>>>
```

**Change 2C: Comprehensive Teardown for Leaked Schedule Entries (lines 357–373)**:
```python
<<<<
	# 1. Clean Schedule Entries with test bays or test repair orders
	if frappe.db.exists("DocType", "Schedule Entry"):
		entries = frappe.get_all(
			"Schedule Entry",
			filters=[
				["service_bay", "like", f"{PREFIX}%"],
			],
			pluck="name",
		)
		for e in entries:
			try:
				frappe.delete_doc("Schedule Entry", e, force=True, ignore_permissions=True)
			except Exception:
				pass
====
	# 1. Clean Schedule Entries with test bays, temp bays, or test repair orders
	if frappe.db.exists("DocType", "Schedule Entry"):
		entries = set(
			frappe.get_all("Schedule Entry", filters=[["service_bay", "like", f"{PREFIX}%"]], pluck="name")
			+ frappe.get_all("Schedule Entry", filters=[["service_bay", "like", f"{TEST_PREFIX}%"]], pluck="name")
			+ frappe.get_all("Schedule Entry", filters=[["service_bay", "like", "TEMP%"]], pluck="name")
			+ frappe.get_all("Schedule Entry", filters=[["customer", "in", [CUSTOMER, f"{TEST_PREFIX}Cust%"]]], pluck="name")
		)
		for e in entries:
			try:
				frappe.delete_doc("Schedule Entry", e, force=True, ignore_permissions=True)
			except Exception:
				pass
>>>>
```

---

#### Target File 3: `ism/ism/doctype/ism_settings/test_ism_settings.py`
Wrap `frappe.db.set_single_value` in a 3-attempt restart loop with cache invalidation (lines 58–64):

```python
<<<<
		if divergent:
			frappe.db.set_single_value("ISM Settings", divergent)
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()
			frappe.db.commit()
====
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
					if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
						frappe.db.rollback()
						frappe.clear_cache(doctype="ISM Settings")
						if hasattr(frappe.db, "value_cache"):
							frappe.db.value_cache.clear()
						continue
					raise
>>>>
```

---

#### Target File 4: `ism/tests/base.py`
Ensure test fixtures are transactionally committed upon creation so that subsequent retry rollbacks do not destroy prerequisite master entities (lines 57–133):

```python
<<<<
				doc = frappe.get_doc({
					"doctype": "Customer",
					"customer_name": name,
					"customer_type": "Individual",
					**kwargs,
				}).insert(ignore_permissions=True)
				return self.track(doc)
====
				doc = frappe.get_doc({
					"doctype": "Customer",
					"customer_name": name,
					"customer_type": "Individual",
					**kwargs,
				}).insert(ignore_permissions=True)
				frappe.db.commit()
				return self.track(doc)
>>>>
```

```python
<<<<
				doc = frappe.get_doc({
					"doctype": "Repair Vehicle",
					"vin": vin_val,
					"customer": customer,
					**kwargs,
				}).insert(ignore_permissions=True)
				return self.track(doc)
====
				doc = frappe.get_doc({
					"doctype": "Repair Vehicle",
					"vin": vin_val,
					"customer": customer,
					**kwargs,
				}).insert(ignore_permissions=True)
				frappe.db.commit()
				return self.track(doc)
>>>>
```

```python
<<<<
				doc = frappe.get_doc({
					"doctype": "Repair Order",
					"customer": customer,
					"vehicle": vehicle,
					"odometer_in": odometer_in,
					**kwargs,
				}).insert(ignore_permissions=True)
				return self.track(doc)
====
				doc = frappe.get_doc({
					"doctype": "Repair Order",
					"customer": customer,
					"vehicle": vehicle,
					"odometer_in": odometer_in,
					**kwargs,
				}).insert(ignore_permissions=True)
				frappe.db.commit()
				return self.track(doc)
>>>>
```

---

#### Target File 5: `ism/tests/cleanup.py`
Broaden `_cleanup_schedule_entries()` and `_cleanup_service_bays()` to delete `TEMP%` bays:

**Change 5A: Line 283–289 in `_cleanup_schedule_entries`**:
```python
<<<<
		# 4. Linked Service Bay checks
		if se.service_bay:
			if (
				any(se.service_bay.startswith(p) for p in ("_TEST", "TEST", "Test Unit Bay", "STRESS"))
				or not frappe.db.exists("Service Bay", se.service_bay)
			):
				is_test = True
====
		# 4. Linked Service Bay checks
		if se.service_bay:
			if (
				any(se.service_bay.startswith(p) for p in ("_TEST", "TEST", "Test Unit Bay", "STRESS", "TEMP"))
				or "TEMP" in se.service_bay
				or not frappe.db.exists("Service Bay", se.service_bay)
			):
				is_test = True
>>>>
```

**Change 5B: Line 468–487 in `_cleanup_service_bays`**:
```python
<<<<
def _cleanup_service_bays(log):
	"""Purge residual test Service Bay records after Schedule Entries have been cleaned."""
	if not frappe.db.exists("DocType", "Service Bay"):
		return
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
			try:
				frappe.delete_doc("Service Bay", bay.name, force=True, ignore_permissions=True)
				log(f"Deleted test Service Bay: {bay.name}")
			except Exception as e:
				log(f"Failed deleting test Service Bay {bay.name}: {e}")
====
def _cleanup_service_bays(log):
	"""Purge residual test Service Bay records after Schedule Entries have been cleaned."""
	if not frappe.db.exists("DocType", "Service Bay"):
		return
	all_bays = frappe.get_all("Service Bay", fields=["name", "bay_name"])
	for bay in all_bays:
		name = bay.name or bay.bay_name or ""
		if name in ("Test Bay 1", "Test Bay 2", "Test Bay 3"):
			continue
		if (
			any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS", "TEMP"))
			or "Test" in name
			or "TEST" in name
			or "TEMP" in name
		):
			try:
				frappe.delete_doc("Service Bay", bay.name, force=True, ignore_permissions=True)
				log(f"Deleted test Service Bay: {bay.name}")
			except Exception as e:
				log(f"Failed deleting test Service Bay {bay.name}: {e}")
>>>>
```

---

#### Target File 6: `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
Add `_insert_with_retry` and commit prerequisite entities in `setUp()`:

```python
<<<<
	def setUp(self):
		super().setUp()
		self.cleanup_schedule_entries()
		self.customer = self.create_test_customer()
		unique_vin = f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"
		self.vehicle = self.create_test_vehicle(vin=unique_vin, customer=self.customer.name)
		self.ro = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)
====
	def setUp(self):
		super().setUp()
		self.cleanup_schedule_entries()
		self.customer = self.create_test_customer()
		unique_vin = f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"
		self.vehicle = self.create_test_vehicle(vin=unique_vin, customer=self.customer.name)
		self.ro = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)
		frappe.db.commit()

	def _insert_entry(self, doc_dict):
		"""Insert Schedule Entry with 3-attempt restart loop on error 1020."""
		for attempt in range(3):
			try:
				doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
				frappe.db.commit()
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					frappe.db.rollback()
					continue
				raise
>>>>
```

---

## 5. Verification Method

Once the worker implements these modifications, independently verify each failure resolution:

### 5.1 Verification 1: 10x Sequential Stress Loop on `Schedule Entry`
Verify that `Schedule Entry` tests pass 10 consecutive runs without throwing MariaDB error 1020:
```powershell
1..10 | ForEach-Object {
    Write-Host "=== RUN $_ ==="
    wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
```
*Expected Result*: 10/10 runs PASS, 70/70 tests executed, 0 errors, exit code 0.

### 5.2 Verification 2: Dirty Baseline Stability Test on `ISM Settings`
Verify that dirtying `buffer_padding_minutes` does not crash `TestISMSettings.setUpClass`:
```bash
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); frappe.db.set_single_value('ISM Settings', 'buffer_padding_minutes', 15); frappe.db.commit()"
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"
```
*Expected Result*: Ran 8 tests, OK, exit code 0.

### 5.3 Verification 3: Full Application Test Suite
Execute the entire test suite:
```bash
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
```
*Expected Result*: Ran 381 tests in ~30s, OK (skipped=254), exit code 0. Zero errors, zero failures.

### 5.4 Verification 4: Service Bay Hygiene & Leakage Check
Verify that no temporary service bays persist in MariaDB:
```bash
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); from ism.tests.cleanup import cleanup_all_test_data; cleanup_all_test_data(False); print('Remaining Bays:', frappe.get_all('Service Bay', pluck='name'))"
```
*Expected Result*: `Remaining Bays: ['Test Bay 1', 'Test Bay 2', 'Test Bay 3']`. Zero temporary or leaked bays.

### Invalidation Conditions
- If any test in `test_schedule_entry.py` throws `QueryDeadlockError: (1020, ...)` during a 10x loop.
- If `bench run-tests --app ism` exits with code 1 or fails with `LinkValidationError`.
- If `tabService Bay` retains any row with name matching `TEMP%` post-suite.
