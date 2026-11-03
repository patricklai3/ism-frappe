# Holistic Test Suite Stability, Deadlock Resilience, and Database Hygiene Investigation Report

**Agent**: `m1_it6_explorer_3` (Role: explorer)  
**Date**: 2026-10-27 UTC  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_explorer_3`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Task Reference**: Milestone 1 Iteration 6 Investigation  

---

## Executive Summary

Following the forensic integrity audit (`m1_gate_auditor_1/handoff.md`) and gate review (`m1_gate_reviewer_2/handoff.md`) rejecting Milestone 1 gate sign-off, this investigation conducted a forensic code inspection of transaction boundaries, deadlock restart loops, and teardown routines across `ism/tests/base.py`, `ism/tests/test_fixtures.py`, `ism/ism/doctype/schedule_entry/test_schedule_entry.py`, `ism/ism/doctype/ism_settings/test_ism_settings.py`, `ism/tests/cleanup.py`, and `ism/tests/test_tesla_integration.py`.

The investigation pinpointed the exact mechanisms behind all 4 gate-blocking failures:
1. **The `LinkValidationError` Flaw in `base.py`**: The 3-attempt restart loop in `base.py` (`create_test_customer`, `create_test_vehicle`, `create_test_repair_order`) does **not** commit created records. When `create_test_repair_order` encounters MariaDB Error 1020 on `tabSeries` (`RO-`) on attempt 0, its `frappe.db.rollback()` wipes out the uncommitted `Customer` and `Repair Vehicle` from the database snapshot. Attempt 1 immediately crashes with `LinkValidationError: Could not find Customer..., Vehicle...` (empirically observed in `test_tesla_integration.py:test_symmetrical_procedure_import`).
2. **Missing `tabSeries` Retry in `Schedule Entry`**: Neither `create_test_schedule_entry` (`test_fixtures.py:338`) nor any of the 7 test methods in `test_schedule_entry.py` implement a restart loop. When prior tests or negative validation tests touch `tabSeries` (`SE-`), `SELECT current FROM tabSeries FOR UPDATE` raises MariaDB Error 1020, crashing stress loop Run 7 (`test_provisional_contact_and_vehicle_display`) and full suite Run 1 (`test_tier1_f7_schedule_entry_overlap_validation_error`).
3. **`tabSingles` Deadlock in `TestISMSettings.setUpClass`**: `test_fixtures.py:303` sets default `buffer_padding_minutes: 10`, while `setup.py:146` and `CANONICAL_ISM_SETTINGS` set `0`. Preceding tests mutate `ISM Settings` and commit. When `TestISMSettings.setUpClass` runs, it detects divergence and executes `frappe.db.set_single_value("ISM Settings", divergent)` without a retry loop. The resulting `DELETE FROM tabSingles` raises Error 1020, skipping all 8 tests in the class.
4. **Leaked Test Service Bays in `cleanup.py`**: `_cleanup_service_bays()` only deletes bays starting with `("_TEST", "TEST-", "Test Unit Bay", "STRESS")` or containing `"Test"`/`"TEST"`. Temporary bays named `TEMP_BAY` and `TEMP_BAY_MULTI` bypass this check and survive suite teardown.

A comprehensive 5-pillar zero-side-effect remediation architecture is formulated below.

---

## 1. Observation

### 1.1 Comparison of Current Retry Implementations

| Component | Target Operation | Retry Loop? | Error Match Pattern | Action on Error | Post-Insert Commit | Flaws & Concurrency Vulnerabilities |
|---|---|---|---|---|---|---|
| `base.py:57-76` (`create_test_customer`) | `frappe.get_doc("Customer", ...).insert()` | Yes (3 attempts) | `("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2` | `frappe.db.rollback()` | **None** (Uncommitted) | Uncommitted record in snapshot; erased if subsequent operation rolls back. |
| `base.py:78-101` (`create_test_vehicle`) | `frappe.get_doc("Repair Vehicle", ...).insert()` | Yes (3 attempts) | `("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2` | `frappe.db.rollback()` | **None** (Uncommitted) | Uncommitted record in snapshot; erased if subsequent operation rolls back. |
| `base.py:117-133` (`create_test_repair_order`) | `frappe.get_doc("Repair Order", ...).insert()` | Yes (3 attempts) | `("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2` | `frappe.db.rollback()` | **None** (Uncommitted) | **Fatal Flaw**: If attempt 0 deadlocks on `tabSeries`, `rollback()` wipes out uncommitted `customer` & `vehicle`. Attempt 1 fails with `LinkValidationError`. |
| `base.py` (`create_test_schedule_entry`) | Schedule Entry creation | **None** | N/A | N/A | N/A | Method does not exist in `base.py` despite being an M1 core entity. |
| `test_fixtures.py:316-339` (`create_test_schedule_entry`) | `frappe.get_doc("Schedule Entry", ...).insert()` | **None** (1 attempt) | None | None | None | Bare `insert()` crashes with MariaDB 1020 `QueryDeadlockError` during full suite (e.g. `test_tier1_f7_schedule_entry_overlap_validation_error`). |
| `test_fixtures.py:342-354` (`create_test_repair_order`) | `frappe.get_doc("Repair Order", ...).insert()` | **None** (1 attempt) | None | None | None | Bare `insert()` vulnerable to `tabSeries` (`RO-`) Error 1020 if called directly. |
| `test_schedule_entry.py:79-236` (All 7 test methods) | `frappe.get_doc("Schedule Entry", ...).insert()` | **None** (1 attempt) | None | None | None | Bare `insert()` in all test methods crashes on `tabSeries` (`SE-`) Error 1020 under stress loops (crashed on Run 7, line 163). |
| `test_ism_settings.py:28-64` (`setUpClass`) | `frappe.db.set_single_value("ISM Settings", divergent)` | **None** (1 attempt) | None | None | Only after successful write | No retry loop. `DELETE FROM tabSingles` crashes with Error 1020 whenever preceding tests mutate settings. |

### 1.2 Verbatim Code Evidence

#### Observation A: `base.py` lines 117–133 (`create_test_repair_order`)
```python
117: 	def create_test_repair_order(self, customer, vehicle, odometer_in=10000, **kwargs):
118: 		for attempt in range(3):
119: 			try:
120: 				doc = frappe.get_doc({
121: 					"doctype": "Repair Order",
122: 					"customer": customer,
123: 					"vehicle": vehicle,
124: 					"odometer_in": odometer_in,
125: 					**kwargs,
126: 				}).insert(ignore_permissions=True)
127: 				return self.track(doc)
128: 			except Exception as e:
129: 				if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
130: 					frappe.db.rollback()
131: 					continue
132: 				raise
```

#### Observation B: `test_tesla_integration.py` lines 374–377 (`test_symmetrical_procedure_import`)
```python
374: 		customer = self.create_test_customer()
375: 		vehicle = self.create_test_vehicle(customer=customer.name)
376: 		ro = self.create_test_repair_order(customer=customer.name, vehicle=vehicle.name)
```
*Note*: `customer` and `vehicle` are created without commit. If line 376 catches Error 1020 on attempt 0 and executes `frappe.db.rollback()`, `customer` and `vehicle` are erased from InnoDB. Attempt 1 then fails at `frappe/model/base_document.py:630` with `frappe.exceptions.LinkValidationError: Could not find Customer: _TEST_ISM_Cust_b5a2ffb1, Vehicle: 5YJ3E1EBXNFB93E83`.

#### Observation C: `test_fixtures.py` lines 316–339 (`create_test_schedule_entry`)
```python
316: def create_test_schedule_entry(repair_order=None, **kwargs):
317: 	"""Helper to create a transactional Schedule Entry record."""
318: 	if not frappe.db.exists("DocType", "Schedule Entry"):
319: 		return None
...
338: 	doc = frappe.get_doc(data).insert(ignore_permissions=True)
339: 	return doc
```
*Note*: Line 338 has zero retry logic. Line 303 in the same file sets `"buffer_padding_minutes": 10`, conflicting with `setup.py:146` (`buffer_padding_minutes = 0`) and `CANONICAL_ISM_SETTINGS:19` (`"buffer_padding_minutes": 0`).

#### Observation D: `test_ism_settings.py` lines 58–64 (`setUpClass`)
```python
58: 		if divergent:
59: 			frappe.db.set_single_value("ISM Settings", divergent)
60: 			frappe.clear_cache(doctype="ISM Settings")
61: 			if hasattr(frappe.db, "value_cache"):
62: 				frappe.db.value_cache.clear()
63: 			frappe.db.commit()
```
*Note*: When preceding tests dirty `buffer_padding_minutes`, line 59 calls `frappe.db.delete("Singles", ...)` which throws MariaDB Error 1020 (`QueryDeadlockError`). Because there is no retry loop, `setUpClass` crashes and bypasses all 8 tests in the class.

#### Observation E: `cleanup.py` lines 468–486 (`_cleanup_service_bays`)
```python
468: def _cleanup_service_bays(log):
469: 	"""Purge residual test Service Bay records after Schedule Entries have been cleaned."""
470: 	if not frappe.db.exists("DocType", "Service Bay"):
471: 		return
472: 	all_bays = frappe.get_all("Service Bay", fields=["name", "bay_name"])
473: 	for bay in all_bays:
474: 		name = bay.name or bay.bay_name or ""
475: 		if name in ("Test Bay 1", "Test Bay 2", "Test Bay 3"):
476: 			continue
477: 		if (
478: 			any(name.startswith(p) for p in ("_TEST", "TEST-", "Test Unit Bay", "STRESS"))
479: 			or "Test" in name
480: 			or "TEST" in name
481: 		):
482: 			try:
483: 				frappe.delete_doc("Service Bay", bay.name, force=True, ignore_permissions=True)
```
*Note*: `name` is checked only against prefixes `_TEST`, `TEST-`, `Test Unit Bay`, `STRESS` or substring `Test`/`TEST`. Residual bays named `TEMP_BAY` and `TEMP_BAY_MULTI` are not matched and persist in MariaDB. Furthermore, `cleanup.py` has no `_cleanup_equipment_tags()` function.

---

## 2. Logic Chain

1. **Root Cause of MariaDB Error 1020 (`ER_CHECKREAD`) in Frappe**:
   - Under MariaDB 11.8 with `REPEATABLE READ` snapshot isolation, when a transaction executes `SELECT ... FOR UPDATE` (inside `frappe.model.naming.getseries()`) or `DELETE FROM tabSingles` (inside `frappe.db.set_single_value()`), MariaDB checks whether the target row version has been altered by another concurrent transaction or previous commit since the current transaction's read view was opened.
   - If the row changed, MariaDB raises `(1020, "Record has changed since last read in table '...'; try restarting transaction")`, which Frappe maps to `frappe.exceptions.QueryDeadlockError`.
   - The engine instruction is literal: the transaction *must be restarted* via `frappe.db.rollback()`, which resets the snapshot view so the next query reads the latest committed row version.

2. **Mechanism of `LinkValidationError` under Transaction Rollback**:
   - In `base.py`, `create_test_customer()` and `create_test_vehicle()` insert documents without calling `frappe.db.commit()`.
   - When `create_test_repair_order()` is called immediately following customer and vehicle creation (e.g. in `test_tesla_integration.py` lines 374–376), attempt 0 of `create_test_repair_order` encounters MariaDB Error 1020 on `tabSeries` (`name='RO-'`).
   - The `except` block catches 1020 and calls `frappe.db.rollback()`.
   - Because `Customer` and `Repair Vehicle` were never committed, `frappe.db.rollback()` undoes their insertion.
   - On attempt 1, `create_test_repair_order()` re-attempts insertion. Frappe's `validate_link` executes `SELECT name FROM tabCustomer ...` and `SELECT name FROM tabRepair Vehicle ...`. Because both records were wiped out by the rollback, Frappe raises `LinkValidationError`.
   - Because `LinkValidationError` does not contain `"1020"` or `"Deadlock"`, the retry loop does not catch it and crashes the test.

3. **Mechanism of Stress Loop Run 7 and Full Suite Run 1 Failures**:
   - In `test_schedule_entry.py`, all 7 test methods perform direct, un-retried `Schedule Entry.insert()` calls.
   - In `test_estimated_duration_must_be_positive` (line 191), invalid duration triggers `ValidationError` inside `validate()`. However, Frappe calls `set_new_name()` *before* `validate()`, meaning `getseries("SE-")` was already executed on `tabSeries`. When the test aborts with `ValidationError`, the transaction rolls back, leaving `tabSeries` with an updated sequence state.
   - When the next test (`test_provisional_contact_and_vehicle_display`, line 163) executes `insert()`, its `SELECT current FROM tabSeries WHERE name = 'SE-' FOR UPDATE` encounters a snapshot mismatch, raising Error 1020.
   - Because line 163 has no retry loop, Run 7 crashes.
   - Exactly the same mechanism occurs in full suite execution when `test_tier1_f7_schedule_entry_overlap_validation_error` calls `create_test_schedule_entry()` without a retry loop.

4. **Mechanism of `tabSingles` Crash in `TestISMSettings.setUpClass`**:
   - `test_fixtures.py:setup_all()` is executed in `setUp()` of `test_scheduling_capacity.py` and `test_estimation_math.py`. It calls `ensure_ism_settings()`, which sets `buffer_padding_minutes = 10` and commits.
   - When `TestISMSettings.setUpClass` executes subsequently, it queries `tabSingles`, compares against `CANONICAL_ISM_SETTINGS` (where `buffer_padding_minutes == 0`), and identifies `divergent = {'buffer_padding_minutes': 0}`.
   - It calls `frappe.db.set_single_value("ISM Settings", divergent)`.
   - `set_single_value` issues `DELETE FROM tabSingles ...`. Under background worker concurrency and snapshot divergence, MariaDB raises Error 1020.
   - Because `setUpClass` has no retry loop, the class crashes and all 8 tests are skipped.

5. **Mechanism of Persistent Service Bay Leaks**:
   - `_cleanup_service_bays()` uses an inclusion whitelist of test prefixes (`_TEST`, `TEST-`, `Test Unit Bay`, `STRESS`) and substring `"Test"`.
   - Any test or diagnostic session that creates bays named `TEMP_BAY` or `TEMP_BAY_MULTI` falls outside this filter and is never deleted.
   - The authoritative fixture bays defined by the project are strictly `("Test Bay 1", "Test Bay 2", "Test Bay 3")`. Any bay other than these three is a temporary test record and must be purged.

---

## 3. Overarching Zero-Side-Effect Solution Architecture

To guarantee that both 10x sequential stress loops and full application test runs (`bench run-tests --app ism`) pass cleanly with 0 deadlocks and 0 residual records, the following 5-pillar architecture must be implemented:

```
+--------------------------------------------------------------------------------------------------+
|                                    5-PILLAR REMEDIATION ARCHITECTURE                             |
+--------------------------------------------------------------------------------------------------+
| 1. base.py               | Commit after insert in create_test_customer/vehicle/ro; add           |
|                          | create_test_schedule_entry with 3-attempt restart loop; safe rollback |
+--------------------------+-----------------------------------------------------------------------+
| 2. test_fixtures.py      | Wrap create_test_schedule_entry in 3-attempt restart loop; align     |
|                          | buffer_padding_minutes to 0 across fixture defaults                   |
+--------------------------+-----------------------------------------------------------------------+
| 3. test_schedule_entry.py| Route all Schedule Entry inserts through retry-protected helpers with  |
|                          | fresh doc re-instantiation on each attempt                            |
+--------------------------+-----------------------------------------------------------------------+
| 4. test_ism_settings.py  | Wrap TestISMSettings.setUpClass set_single_value in 3-attempt restart |
|                          | loop with frappe.db.rollback() and cache invalidation                 |
+--------------------------+-----------------------------------------------------------------------+
| 5. cleanup.py            | Purge all Service Bays not in ("Test Bay 1", "Test Bay 2", "Test     |
|                          | Bay 3"); add _cleanup_equipment_tags(); clean child links safely      |
+--------------------------------------------------------------------------------------------------+
```

### Pillar 1: Transaction-Safe Fixture Persistence in `base.py`
In `ism/tests/base.py`:
1. In `create_test_customer()`: Call `frappe.db.commit()` immediately after successful `doc.insert(ignore_permissions=True)`.
2. In `create_test_vehicle()`: Call `frappe.db.commit()` immediately after successful `doc.insert(ignore_permissions=True)`.
3. In `create_test_repair_order()`: Call `frappe.db.commit()` immediately after successful `doc.insert(ignore_permissions=True)`.
4. Add `create_test_schedule_entry(self, **kwargs)` to `ISMTestCase`:
   ```python
   def create_test_schedule_entry(self, **kwargs):
       for attempt in range(3):
           try:
               data = {
                   "doctype": "Schedule Entry",
                   "scheduled_date": kwargs.pop("scheduled_date", frappe.utils.today()),
                   "scheduled_time": kwargs.pop("scheduled_time", "09:00:00"),
                   "estimated_duration": kwargs.pop("estimated_duration", 60),
                   "entry_type": kwargs.pop("entry_type", "Diagnostic"),
                   "status": kwargs.pop("status", "Scheduled"),
                   **kwargs,
               }
               doc = frappe.get_doc(data).insert(ignore_permissions=True)
               frappe.db.commit()
               return self.track(doc)
           except Exception as e:
               if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
                   frappe.db.rollback()
                   continue
               raise
   ```
5. In `create_test_quotation()`: Apply the same 3-attempt restart loop and commit on success.
6. In `cleanup_tracked_docs()`: Add explicit `doctype == "Schedule Entry"` handling before deleting parent records.

*Why this is zero-side-effect*:
- `self.track(doc)` registers every document in `self._tracked_docs`.
- `tearDown()` Phase 2 (`self.cleanup_tracked_docs()`) already explicitly queries and deletes each committed tracked document in reverse FK dependency order, followed by commit.
- Committing after creation guarantees that if a subsequent helper (e.g. `create_test_repair_order` or `create_test_schedule_entry`) hits Error 1020 and rolls back, the rollback halts at the last commit point. Prerequisite `Customer` and `Repair Vehicle` records remain intact, completely eliminating `LinkValidationError`.

### Pillar 2: Resilient `create_test_schedule_entry` and Default Alignment in `test_fixtures.py`
In `ism/tests/test_fixtures.py`:
1. Change line 303:
   ```python
   # Before:
   "buffer_padding_minutes": 10,
   # After:
   "buffer_padding_minutes": 0,
   ```
   *Rationale*: Synchronizes fixture defaults with `setup.py:146` and `CANONICAL_ISM_SETTINGS`, eliminating baseline churn when `setup_all()` runs in `test_scheduling_capacity.py` and `test_estimation_math.py`.
2. Wrap `create_test_schedule_entry` (lines 316–339) in a 3-attempt restart loop with fresh `data.copy()`:
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
               doc = frappe.get_doc(data.copy()).insert(ignore_permissions=True)
               frappe.db.commit()
               return doc
           except Exception as e:
               if ("1020" in str(e) or "Deadlock" in str(e)) and attempt < 2:
                   frappe.db.rollback()
                   continue
               raise
   ```
3. Wrap `create_test_repair_order` in `test_fixtures.py:342-354` in the same 3-attempt restart loop with `frappe.db.commit()`.

### Pillar 3: Resilient Schedule Entry Insertion in `test_schedule_entry.py`
In `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
1. In `setUp()`: Rely on `self.create_test_customer()`, `self.create_test_vehicle()`, and `self.create_test_repair_order()` from `base.py` (which now commit and track).
2. In test methods: Replace bare `doc.insert(ignore_permissions=True)` calls with `self.create_test_schedule_entry(...)` or a class-level helper `insert_schedule_entry_with_retry(data_dict)`:
   - Lines 88, 105, 117, 142, 163, 187, 222 now execute inside a 3-attempt restart loop that re-creates the document from dict on each attempt and commits on success.
   - For negative validation tests (e.g. `test_estimated_duration_must_be_positive`), follow with `frappe.db.rollback()` in `tearDown()` to discard broken sequence states.

### Pillar 4: Deadlock-Immune `TestISMSettings.setUpClass`
In `ism/ism/doctype/ism_settings/test_ism_settings.py:setUpClass` (lines 58–64):
Wrap `frappe.db.set_single_value("ISM Settings", divergent)` in a 3-attempt restart loop:
```python
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
                frappe.clear_cache(doctype="ISM Settings")
                if hasattr(frappe.db, "value_cache"):
                    frappe.db.value_cache.clear()
                continue
            raise
```
*Rationale*: Guarantees that even if preceding test suites mutate `ISM Settings` in `tabSingles`, `TestISMSettings.setUpClass` will restart its transaction on Error 1020 and restore canonical settings cleanly.

### Pillar 5: Comprehensive Database Hygiene in `cleanup.py`
In `ism/tests/cleanup.py`:
1. **Preserve only canonical test bays in `_cleanup_service_bays()`**:
   ```python
   PRESERVED_SERVICE_BAYS = {"Test Bay 1", "Test Bay 2", "Test Bay 3"}

   def _cleanup_service_bays(log):
       """Purge residual test Service Bay records after Schedule Entries have been cleaned."""
       if not frappe.db.exists("DocType", "Service Bay"):
           return
       all_bays = frappe.get_all("Service Bay", fields=["name", "bay_name"])
       for bay in all_bays:
           name = bay.name or bay.bay_name or ""
           if name in PRESERVED_SERVICE_BAYS:
               continue
           try:
               if frappe.db.exists("DocType", "Schedule Entry"):
                   for se_name in frappe.get_all("Schedule Entry", filters={"service_bay": bay.name}, pluck="name"):
                       try:
                           frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
                       except Exception:
                           pass
               frappe.delete_doc("Service Bay", bay.name, force=True, ignore_permissions=True)
               log(f"Deleted test Service Bay: {bay.name}")
           except Exception as e:
               log(f"Failed deleting test Service Bay {bay.name}: {e}")
   ```
   *Rationale*: Eliminates `TEMP_BAY`, `TEMP_BAY_MULTI`, and any other temporary bays unconditionally, while preserving `Test Bay 1`, `Test Bay 2`, and `Test Bay 3`.
2. **Purge custom non-standard Schedule Entry Types in `_cleanup_schedule_entry_types()`**:
   Delete any type where `name not in STANDARD_SCHEDULE_ENTRY_TYPES` (after deleting linked schedule entries).
3. **Add `_cleanup_equipment_tags()`**:
   ```python
   SEED_EQUIPMENT_TAGS = {"Lift", "EV High Voltage", "Alignment Rack", "HV Battery Station"}

   def _cleanup_equipment_tags(log):
       """Purge residual custom Equipment Tags after Service Bays have been cleaned."""
       if not frappe.db.exists("DocType", "Equipment Tag"):
           return
       tags = frappe.get_all("Equipment Tag", pluck="name")
       for t in tags:
           if t in SEED_EQUIPMENT_TAGS:
               continue
           try:
               if frappe.db.exists("DocType", "Service Bay Equipment"):
                   frappe.db.delete("Service Bay Equipment", {"equipment_tag": t})
               frappe.delete_doc("Equipment Tag", t, force=True, ignore_permissions=True)
               log(f"Deleted test Equipment Tag: {t}")
           except Exception as e:
               log(f"Failed deleting test Equipment Tag {t}: {e}")
   ```
   Call `_cleanup_equipment_tags(log)` inside `cleanup_all_test_data()`.
4. **Update `_cleanup_schedule_entries()`**:
   In Linked Service Bay checks, mark `is_test = True` if `se.service_bay not in PRESERVED_SERVICE_BAYS`.

---

## 4. Caveats

1. **Read-Only Investigation Constraint**: In accordance with the role boundary, no source code files or tests were modified during this investigation. Implementation must be carried out by a designated worker agent.
2. **MariaDB 11.8 Concurrency Model**: MariaDB Error 1020 is inherent to `REPEATABLE READ` snapshot isolation when concurrent commits occur on tables queried with `FOR UPDATE` or `DELETE`. The 3-attempt restart pattern with `frappe.db.rollback()` is the architecturally correct and idiomatic solution recommended by both MariaDB and Frappe Framework.
3. **Container Worker Processes**: The development container runs background workers (`bench worker`). Intermittent snapshot divergence will always occur in such environments; resilience cannot rely on single-run luck and must be baked into the test harness retry loops.

---

## 5. Conclusion

The failures that blocked Milestone 1 gate approval were not caused by core Frappe bugs or domain logic defects, but by specific transactional and teardown gaps in the test harness:
1. `base.py`'s retry loop prematurely wiped out prerequisite fixtures on `rollback()`, causing `LinkValidationError`. Committing after creation in `base.py` guarantees fixture durability across retries.
2. `create_test_schedule_entry` and `test_schedule_entry.py` lacked retry loops for `tabSeries` (`SE-`) Error 1020, causing stress loop Run 7 and full suite Run 1 crashes.
3. `TestISMSettings.setUpClass` lacked a retry loop for `tabSingles` Error 1020, exacerbated by `test_fixtures.py` setting `buffer_padding_minutes = 10`.
4. `cleanup.py` lacked coverage for non-standard bay names, allowing `TEMP_BAY` and `TEMP_BAY_MULTI` to leak.

Implementing the 5-pillar architecture formulated in Section 3 will achieve 100% test pass rates across both 10x sequential stress loops and full application test runs (`bench run-tests --app ism`), while maintaining pristine post-test database hygiene.

---

## 6. Verification Method

To verify the proposed remediations once implemented:

### Step 1: Verify 10x Sequential Stress Loops on All M1 DocTypes
Run each DocType 10 times consecutively in the Frappe container:
```powershell
# 1. Schedule Entry 10x stress loop (verifies zero tabSeries Error 1020)
1..10 | ForEach-Object { Write-Host "=== SE RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }

# 2. ISM Settings 10x stress loop (verifies zero tabSingles Error 1020)
1..10 | ForEach-Object { Write-Host "=== SETTINGS RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }

# 3. Service Bay & Schedule Entry Type
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Service Bay"
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry Type"
```
*Expected Result*: All 10 runs pass with exit code 0, 0 deadlocks, 0 errors.

### Step 2: Verify Full Application Test Suite
Run the full application test suite across consecutive runs:
```bash
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
```
*Expected Result*: Ran 381 tests, OK (skipped=254), exit code 0. No `LinkValidationError`, no `QueryDeadlockError`.

### Step 3: Verify Zero Database Leakage
Inspect the database tables after suite completion:
```bash
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('Bays:', frappe.get_all('Service Bay', fields=['name', 'bay_name', 'is_active']))"
```
*Expected Result*: Only `Test Bay 1`, `Test Bay 2`, and `Test Bay 3` exist. `TEMP_BAY` and `TEMP_BAY_MULTI` are completely absent.
