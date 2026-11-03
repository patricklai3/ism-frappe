# Root Cause Analysis and Concrete Remediation Plan for Milestone 1 Iteration 6 Audit Failures

**Agent ID**: `m1_it7_explorer_1`  
**Role**: `explorer` (`investigator`, `analyst`, `specialist`)  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it7_explorer_1`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Date**: 2026-10-27 UTC  
**Handoff Type**: Hard  

---

## Executive Summary

During Milestone 1 Iteration 6, the Forensic Auditor (`m1_it6_auditor_1`), Challenger 2 (`m1_it6_challenger_2`), and Reviewer 1 (`m1_it6_reviewer_1`) rejected the work product due to:
1. **Broken Test Isolation & Data Leakage**: In an attempt to prevent caller fixtures from disappearing during inner retry rollbacks, Worker 1 introduced mid-test commits (`frappe.db.commit()`) across `base.py`, `test_fixtures.py`, and `test_schedule_entry.py`. This committed un-isolated test records to MariaDB, permanently leaked 66 `Schedule Entry` records and test customers, held stale row locks, and triggered MariaDB 11.8 snapshot isolation collisions across the test suite.
2. **`ScheduleEntry.autoname` Transaction Rollback Defect**: `ScheduleEntry.autoname` issued `frappe.db.rollback()` upon catching MariaDB Error 1020, wiping out uncommitted caller fixtures (specifically uncommitted `Service Bay` in `TestServiceBay.test_service_bay_deletion_blocked_with_active_schedule_entry`, causing `AssertionError: ValidationError not raised`).
3. **`Repair Order` Series Naming Deadlocks**: Unlike `Schedule Entry`, `Repair Order` had no retry handling around `make_autoname("RO-.YYYY.-.#####")`, causing 5 test crashes in `TestRepairOrder` on `key = 'RO-2026-'`.
4. **Unguarded `set_single_value` in `test_ism_settings.py:213`**: Line 213 in `test_setup_idempotency_preserves_customizations` mutates `tabSingles` without retry or error handling, crashing on Run 5 / Run 3 of the 10x sequential stress loop.
5. **Orphaned Vehicle State Bleed in `cleanup.py`**: 10 test vehicles with random VINs (`5YJ3E1EBXNF...`), `license_plate = NULL`, and dangling links to deleted customers permanently leaked in MariaDB because `cleanup.py:436` only checked `v.customer in test_customers`.

This report provides empirical proof, the complete causal chain, and an exact file-by-file remediation plan for Worker execution.

---

## 1. Observation

### 1.1 Exact Locations of Mid-Test Commits in Test Code
Static analysis and grep verification across the codebase revealed 9 mid-test commits introduced in Iteration 6:

1. **`ism/tests/base.py`**:
   - Line 71: `frappe.db.commit()` inside `create_test_customer()`
   - Line 97: `frappe.db.commit()` inside `create_test_vehicle()`
   - Line 119: `frappe.db.commit()` inside `create_test_quotation()`
   - Line 137: `frappe.db.commit()` inside `create_test_repair_order()`
   - Line 159: `frappe.db.commit()` inside `create_test_schedule_entry()`
   - Line 241: `frappe.db.commit()` inside `cleanup_tracked_docs()` during test `tearDown()`
2. **`ism/tests/test_fixtures.py`**:
   - Line 342: `frappe.db.commit()` inside `create_test_schedule_entry()`
   - Line 368: `frappe.db.commit()` inside `create_test_repair_order()`
3. **`ism/ism/doctype/schedule_entry/test_schedule_entry.py`**:
   - Line 16: `frappe.db.commit()` in `setUp()` after inserting customer, vehicle, and repair order
   - Line 23: `frappe.db.commit()` in `_insert_entry()`
   - Line 240: `frappe.db.commit()` in `test_provisional_contact_and_vehicle_display()`

---

### 1.2 `ScheduleEntry.autoname` Rollback Defect
In `ism/ism/doctype/schedule_entry/schedule_entry.py` (lines 11–25):
```python
10: class ScheduleEntry(Document):
11: 	def autoname(self):
12: 		"""Assign naming series SE-.##### with 3-attempt transaction restart retry on MariaDB 1020."""
13: 		from frappe.model.naming import make_autoname
14: 
15: 		prefix = self.naming_series or "SE-.#####"
16: 		for attempt in range(3):
17: 			try:
18: 				self.name = make_autoname(prefix, doc=self)
19: 				break
20: 			except Exception as e:
21: 				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
22: 					frappe.db.rollback()
23: 					continue
24: 				raise
```
**Consequence observed in `test_service_bay.py:105-134`**:
In `test_service_bay_deletion_blocked_with_active_schedule_entry`:
- Line 111 inserts `bay = ServiceBay(bay_name=self.bay_alpha, is_active=1)` within the caller's test transaction.
- Line 122 calls `se.insert()` (`Schedule Entry`).
- If `se.autoname()` catches MariaDB Error 1020 on `tabSeries`, line 22 issues `frappe.db.rollback()`.
- This wipes out the uncommitted `bay` from the database.
- When the test proceeds to line 126 (`frappe.delete_doc("Service Bay", self.bay_alpha)`), `bay` no longer exists, `on_trash` never triggers, and `self.assertRaises(frappe.ValidationError)` fails with:
  ```text
  FAIL test_service_bay_deletion_blocked_with_active_schedule_entry (ism.ism.doctype.service_bay.test_service_bay.TestServiceBay)
  File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/service_bay/test_service_bay.py", line 125
    with self.assertRaises(frappe.ValidationError):
  AssertionError: ValidationError not raised
  ```

---

### 1.3 `Repair Order` Naming Series 1020 Deadlocks
In `ism/ism/doctype/repair_order/repair_order.json`:
- Line 3: `"autoname": "naming_series:"`
- Lines 92–98: `"naming_series"` default `"RO-.YYYY.-.#####"`
`Repair Order` defines no custom `autoname(self)` in `repair_order.py`.
During full application test suite execution (`bench run-tests --app ism`), when multiple tests or test helpers create Repair Orders while background jobs run:
- MariaDB Error 1020 occurred on `tabSeries` (`key = 'RO-2026-'`):
  `frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")`
- Crashed 5 tests in `ism/ism/doctype/repair_order/test_repair_order.py`:
  1. Line 162 (`test_save_service_as_package`)
  2. Line 593 (`test_unapproved_pickup_and_status_progression_blocked`)
  3. Line 623 (`test_service_package_expansion_and_totals`)
  4. Line 776 (`test_submitted_repair_order_dynamic_lifecycle_and_supplements`)
  5. Line 928 (`test_symmetrical_package_and_opposite_side`)
  Plus additional tests in `test_parts_management.py`.

---

### 1.4 `test_ism_settings.py:213` Unguarded `set_single_value`
In `ism/ism/doctype/ism_settings/test_ism_settings.py`:
- Lines 59–75: `setUpClass()` was wrapped in a 3-attempt retry loop on MariaDB 1020 with `frappe.db.rollback()`.
- Lines 211–215 in `test_setup_idempotency_preserves_customizations`:
  ```python
  209: 	def test_setup_idempotency_preserves_customizations(self):
  210: 		"""Verify setup_default_ism_settings preserves existing customizations without committing state."""
  211: 		try:
  212: 			# Apply custom values via set_single_value in uncommitted transaction
  213: 			frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)
  214: 			frappe.db.set_single_value("ISM Settings", "enable_technician_capacity", 0)
  215: 			frappe.clear_cache(doctype="ISM Settings")
  ```
  Neither line 213 nor line 214 has any retry handling or exception guards.
- **Empirical Failure**: Run 5 (Auditor 1) and Run 3 (Reviewer 1) of the 10x sequential stress loop crashed with:
  ```text
  MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")
  ```

---

### 1.5 Orphaned Vehicles in `cleanup.py` and MariaDB Verification
Empirical SQL query executed directly against `devcontainer-mariadb-1` via Python in `devcontainer-frappe-1`:
```python
frappe.get_all("Repair Vehicle", fields=["name", "customer", "license_plate"])
```
**Direct Output**:
```python
[
    {'name': '5YJ3E1EBXNF44EA46', 'customer': '_TEST_ISM_Cust_89a9940b', 'license_plate': None},
    {'name': '5YJ3E1EBXNF441F26', 'customer': '_TEST_ISM_Cust_6d9f0368', 'license_plate': None},
    {'name': '5YJ3E1EBXNFD325F1', 'customer': '_TEST_ISM_Cust_9998e52f', 'license_plate': None},
    {'name': '5YJ3E1EBXNF44CD7B', 'customer': '_TEST_ISM_Cust_51baf67e', 'license_plate': None},
    {'name': '5YJ3E1EBXNFFF33F9', 'customer': '_TEST_ISM_Cust_bfed407d', 'license_plate': None},
    {'name': '5YJ3E1EBXNF349F49', 'customer': '_TEST_ISM_Cust_5ff111b7', 'license_plate': None},
    {'name': '5YJ3E1EBXNF0DE8E9', 'customer': '_TEST_ISM_Cust_eb16e223', 'license_plate': None},
    {'name': '5YJ3E1EBXNF72FCE6', 'customer': '_TEST_ISM_Cust_cb468898', 'license_plate': None},
    {'name': '5YJ3E1EBXNF502C53', 'customer': '_TEST_ISM_Cust_cfde1852', 'license_plate': None},
    {'name': '5YJ3E1EBXNF16AF39', 'customer': '_TEST_ISM_Cust_11eebec9', 'license_plate': None},
    {'name': '5YJSA1E26CF000001', 'customer': 'Test Customer', 'license_plate': 'TEST-SCHED'},
    {'name': 'LRW3E1FA9PC445566', 'customer': 'Patrick Lai', 'license_plate': 'GVKR035'}
]
```
- Exactly **10 orphaned test vehicles** (`5YJ3E1EBXNF...`) remain in the database.
- None of their referenced customers (`_TEST_ISM_Cust_89a9940b`, etc.) exist in `tabCustomer` (`frappe.db.exists("Customer", ...) -> None`).
- In `ism/tests/cleanup.py:436`:
  ```python
  if v.customer in test_customers or v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]:
  ```
  Because the customer was already deleted, `_find_test_customers()` never finds it. Because `license_plate` is `None`, neither check matches. These 10 vehicles survive every cleanup pass.

---

## 2. Logic Chain

### Step 1: The Root Cause of the Compounding Anti-Pattern
1. In Iteration 5, test helper retries or autoname hooks experienced MariaDB 1020 conflicts and issued `frappe.db.rollback()`.
2. When `frappe.db.rollback()` ran, it erased all uncommitted documents that had been inserted earlier in the test method (such as customer and vehicle fixtures).
3. On the next retry attempt, `doc.insert()` failed with `LinkValidationError: Could not find Customer...` because the uncommitted customer was wiped.
4. In Iteration 6, Worker 1 attempted to fix `LinkValidationError` by adding `frappe.db.commit()` after every entity insertion in `base.py`, `test_fixtures.py`, and `test_schedule_entry.py`.
5. Worker 1 stated: *"Added frappe.db.commit() immediately following .insert() so prerequisite entities survive inner retry rollbacks."*
6. Committing un-isolated entities mid-test broke test isolation, caused partial state commits when tests aborted, and permanently leaked 66 Schedule Entries and test customers into MariaDB.

### Step 2: Mechanics of MariaDB 11.8 Error 1020 (`innodb_snapshot_isolation = ON`)
1. MariaDB 11.8 in the devcontainer has `innodb_snapshot_isolation = ON` and `transaction_isolation = REPEATABLE-READ`.
2. Under this setting, MariaDB tracks row versions read by each transaction's snapshot.
3. If Transaction T1 reads a row from `tabSeries` or `tabSingles`, and another transaction T2 modifies and **commits** that row, any subsequent `SELECT ... FOR UPDATE` or `DELETE` on that row in T1 raises MariaDB Error 1020:
   `Record has changed since last read in table '...'; try restarting transaction`.
4. Because Worker 1 was committing transactions mid-suite (`base.py`, `test_fixtures.py`, `test_schedule_entry.py`), `tabSeries` updates were continuously committed to MariaDB while other test methods had active transactions.
5. Consequently, any other test in the suite that touched `tabSeries` (`RO-2026-` or `SE-`) or `tabSingles` encountered Error 1020.
6. Eliminating mid-test commits completely stops concurrent transactions from committing series updates mid-test, thereby eliminating the root cause of cross-test 1020 collisions.

### Step 3: Why `frappe.db.rollback()` in `ScheduleEntry.autoname` Is an Architectural Violation
1. Per `frappe-code-review` methodology §1.3: *"No partial commits. A stray frappe.db.commit() / db.rollback() mid-transaction ends the transaction and exposes partial state — flag every one."*
2. An `autoname` method on a Document controller runs inside the caller's transaction during `doc.insert()`.
3. Calling `frappe.db.rollback()` inside `autoname` destroys whatever the caller had inserted prior to `insert()` (such as `Service Bay` in `TestServiceBay`).
4. If series naming contention occurs, `autoname` must retry `make_autoname()` with a brief backoff **WITHOUT** calling `frappe.db.rollback()`.
5. Empirical testing proved that retrying `make_autoname` on attempt 1 without rollback succeeds without destroying caller state.

### Step 4: Why `Repair Order` Suffered 5 Series Deadlocks
1. In `repair_order.json`, `Repair Order` is autonamed via `"naming_series:"`.
2. When `ro.insert()` runs, Frappe calls `make_autoname("RO-.YYYY.-.#####")`, which queries `SELECT current FROM tabSeries WHERE name = 'RO-2026-' FOR UPDATE`.
3. Unlike `Schedule Entry` (which had an `autoname` retry loop in `schedule_entry.py`), `Repair Order` had zero retry handling.
4. Under full application test suite execution, concurrent tests or background worker commits triggered 1020 on `RO-2026-`, immediately crashing `TestRepairOrder` at lines 162, 593, 623, 776, and 928.
5. Implementing `autoname(self)` on `RepairOrder` with retry handling will protect `Repair Order` from naming series collisions.

### Step 5: Why `test_ism_settings.py:213` Crashed on Run 3 / Run 5
1. While `TestISMSettings.setUpClass` was guarded with a 3-attempt restart loop around `frappe.db.set_single_value`, `test_setup_idempotency_preserves_customizations` (lines 213–214) called `set_single_value` twice in succession without any retry loop.
2. `frappe.db.set_single_value` executes `DELETE FROM tabSingles WHERE field IN (...) AND doctype = 'ISM Settings'`.
3. If `tabSingles` was touched by preceding tests or background processes, MariaDB threw Error 1020 on `tabSingles`, crashing the test.
4. Wrapping `set_single_value` calls in a retry loop and passing both fields in a single dictionary call eliminates this vulnerability.

### Step 6: Why Orphaned Vehicles Leaked Permanently
1. `base.py:88` and `test_schedule_entry.py:13` generate vehicles with VIN prefix `5YJ3E1EBXNF` and `license_plate = None`.
2. In `cleanup.py:436`, `_cleanup_repair_vehicles()` only checks `v.customer in test_customers` or `v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]`.
3. When test customers were deleted before vehicle cleanup, `test_customers` no longer included those customer IDs.
4. Because `license_plate` was `None`, the vehicles were skipped and remained orphaned with dangling foreign keys in MariaDB.
5. Updating `_cleanup_repair_vehicles()` to check test VIN prefixes (`5YJ3E1EBXNF`), deleted/dangling customer foreign keys, and test customer prefixes (`_TEST`) guarantees complete cleanup.

---

## 3. Caveats

- **Active Background Services**: In `devcontainer-frappe-1`, `bench schedule`, `bench worker`, and `bench serve` are active processes. The background worker continuously processes asynchronous jobs (e.g. `frappe.model.delete_doc.delete_dynamic_links`), which execute SQL and commit. Tests must maintain strict transaction boundaries and must not rely on zero concurrent activity in the database.
- **Single Run vs Stress Loop**: In isolated single-run invocations, timing collisions rarely trigger Error 1020. The 10x sequential stress loop and full test suite are mandatory to empirically expose and verify concurrency stability.
- **Pure Implementation Only**: All identified issues stem strictly from transaction boundary violations, missing retry loops, and filter omissions. No malicious mocks or facade bypasses exist in the codebase.

---

## 4. Conclusion & Concrete Remediation Plan

To resolve the Iteration 6 audit rejection with zero side effects, Worker must implement the following 5-pillar plan:

### Pillar 1: Remove ALL Mid-Test Commits in `base.py` & Test Helpers

#### 1.1 In `ism/tests/base.py`:
- In `create_test_customer()` (lines 70–76): Remove `frappe.db.commit()`. Remove `frappe.db.rollback()` from the retry loop.
- In `create_test_vehicle()` (lines 96–102): Remove `frappe.db.commit()`. Remove `frappe.db.rollback()` from the retry loop.
- In `create_test_quotation()` (lines 118–124): Remove `frappe.db.commit()`. Remove `frappe.db.rollback()` from the retry loop.
- In `create_test_repair_order()` (lines 136–142): Remove `frappe.db.commit()`. Remove `frappe.db.rollback()` from the retry loop.
- In `create_test_schedule_entry()` (lines 158–164): Remove `frappe.db.commit()`. Remove `frappe.db.rollback()` from the retry loop.
- In `cleanup_tracked_docs()` (line 241): Remove `frappe.db.commit()`.

**Proposed Code Pattern for Test Helpers in `base.py`**:
```python
	def create_test_customer(self, customer_name=None, **kwargs):
		for attempt in range(3):
			try:
				name = customer_name or f"{TEST_PREFIX}Cust_{frappe.generate_hash(length=8)}"
				if name in STATIC_FIXTURE_CUSTOMERS:
					return frappe.get_doc("Customer", name)
				if frappe.db.exists("Customer", name):
					frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
				doc = frappe.get_doc({
					"doctype": "Customer",
					"customer_name": name,
					"customer_type": "Individual",
					**kwargs,
				}).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					import time; time.sleep(0.05 * (attempt + 1))
					continue
				raise
```
*(Apply identical pattern to `create_test_vehicle`, `create_test_quotation`, `create_test_repair_order`, and `create_test_schedule_entry`, ensuring NO `frappe.db.commit()` and NO `frappe.db.rollback()` inside the helper).*

#### 1.2 In `ism/tests/test_fixtures.py`:
- In `create_test_schedule_entry()` (lines 341–347): Remove `frappe.db.commit()`. Remove `frappe.db.rollback()`.
- In `create_test_repair_order()` (lines 366–372): Remove `frappe.db.commit()`. Remove `frappe.db.rollback()`.

#### 1.3 In `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
- Line 16 in `setUp()`: Remove `frappe.db.commit()`.
- Line 23 & 27 in `_insert_entry()`: Remove `frappe.db.commit()` and `frappe.db.rollback()`.
- Line 240 in `test_provisional_contact_and_vehicle_display()`: Remove `frappe.db.commit()`.

---

### Pillar 2: Fix `ScheduleEntry.autoname` and Implement `RepairOrder.autoname` Cleanly

#### 2.1 In `ism/ism/doctype/schedule_entry/schedule_entry.py` (lines 11–25):
Remove `frappe.db.rollback()`. Replace with clean backoff retry:
```python
	def autoname(self):
		"""Assign naming series SE-.##### with retry on naming contention without destroying caller transaction."""
		import time
		from frappe.model.naming import make_autoname

		prefix = self.naming_series or "SE-.#####"
		for attempt in range(3):
			try:
				self.name = make_autoname(prefix, doc=self)
				break
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise
```

#### 2.2 In `ism/ism/doctype/repair_order/repair_order.py`:
Implement `autoname(self)` on the `RepairOrder` Document controller:
```python
	def autoname(self):
		"""Assign naming series RO-.YYYY.-.##### with retry on naming contention."""
		import time
		from frappe.model.naming import make_autoname

		prefix = self.naming_series or "RO-.YYYY.-.#####"
		for attempt in range(3):
			try:
				self.name = make_autoname(prefix, doc=self)
				break
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise
```

---

### Pillar 3: Guard `set_single_value` in `test_ism_settings.py:213`
In `ism/ism/doctype/ism_settings/test_ism_settings.py`:
Replace lines 211–224 in `test_setup_idempotency_preserves_customizations` with a 3-attempt restart loop using atomic dictionary assignment:
```python
	def test_setup_idempotency_preserves_customizations(self):
		"""Verify setup_default_ism_settings preserves existing customizations without committing state."""
		for attempt in range(3):
			try:
				# Apply custom values via atomic dictionary set_single_value in uncommitted transaction
				frappe.db.set_single_value("ISM Settings", {
					"default_slot_interval": 45,
					"enable_technician_capacity": 0,
				})
				frappe.clear_cache(doctype="ISM Settings")
				if hasattr(frappe.db, "value_cache"):
					frappe.db.value_cache.clear()
				break
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					frappe.db.rollback()
					frappe.clear_cache(doctype="ISM Settings")
					if hasattr(frappe.db, "value_cache"):
						frappe.db.value_cache.clear()
					continue
				raise

		try:
			# Re-run setup_default_ism_settings
			setup_default_ism_settings()

			# Assert customizations were preserved
			reloaded = frappe.get_single("ISM Settings")
			self.assertEqual(reloaded.default_slot_interval, 45)
			self.assertEqual(reloaded.enable_technician_capacity, 0)
		finally:
			# Discard uncommitted mutations via transaction rollback
			frappe.db.rollback()
			frappe.clear_cache(doctype="ISM Settings")
			if hasattr(frappe.db, "value_cache"):
				frappe.db.value_cache.clear()
```

---

### Pillar 4: Harden `cleanup.py` and Purge Orphaned Vehicles

In `ism/tests/cleanup.py`:
1. In `cleanup_all_test_data()` (line 35):
   Add `frappe.db.rollback()` as the very first operation to discard stale read snapshots and release any lingering row locks.
2. In `_cleanup_repair_vehicles()` (lines 432–442):
   Update the purge filter to catch all test VINs, vehicles with dangling/missing customers, and test customer prefixes:
   ```python
	all_vehs = frappe.get_all("Repair Vehicle", fields=["name", "customer", "license_plate", "vin"])
	for v in all_vehs:
		if v.name in PRESERVED_VEHICLES or v.customer in PRESERVED_CUSTOMERS:
			continue
		is_test_vehicle = (
			v.customer in test_customers
			or (v.customer and any(v.customer.startswith(p) for p in TEST_CUST_PREFIXES))
			or (v.customer and not frappe.db.exists("Customer", v.customer))
			or (v.name and v.name.startswith("5YJ3E1EBXNF"))
			or v.name in TEST_VINS
			or (v.license_plate and v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"])
			or (v.license_plate and any(v.license_plate.startswith(p) for p in ("TEST", "_TEST")))
			or (v.name and any(v.name.startswith(p) for p in ("_TEST", "TEST", "Test")))
		)
		if is_test_vehicle:
			try:
				frappe.delete_doc("Repair Vehicle", v.name, force=True, ignore_permissions=True)
				log(f"Deleted test Repair Vehicle: {v.name}")
			except Exception as e:
				log(f"Failed deleting test Repair Vehicle {v.name}: {e}")
   ```
3. In `_cleanup_repair_orders()` (lines 354–358):
   Ensure `is_test` also matches orphaned orders whose customer no longer exists:
   ```python
		is_test = (
			ro.customer in test_customers
			or ro.vehicle in TEST_VINS
			or (ro.customer and any(ro.customer.startswith(p) for p in TEST_CUST_PREFIXES))
			or (ro.customer and not frappe.db.exists("Customer", ro.customer))
			or (ro.vehicle and ro.vehicle.startswith("5YJ3E1EBXNF"))
		)
   ```

---

## 5. Verification Method

The Worker and Auditor must independently verify this remediation using the following empirical procedure:

### 1. Execute Database Cleanup & Verify Zero Residual Records
```powershell
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); from ism.tests.cleanup import cleanup_all_test_data; cleanup_all_test_data(True); print('Residual SE count:', frappe.db.count('Schedule Entry')); print('Residual RV count:', frappe.db.count('Repair Vehicle')); print('Residual RO count:', frappe.db.count('Repair Order'))"
```
*Expected Output*:
- All 10 orphaned vehicles (`5YJ3E1EBXNF...`) purged.
- Residual non-seed counts: 0 leaked Schedule Entries, 0 leaked Repair Orders, only canonical vehicles remain (`5YJSA1E26CF000001`, `LRW3E1FA9PC445566`).

### 2. 10x Sequential Stress Loop on `Schedule Entry`
```powershell
1..10 | ForEach-Object { Write-Host "=== RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
```
*Expected Output*: 10/10 PASS, 70/70 tests executed, 0 deadlocks, exit code 0.

### 3. 10x Sequential Stress Loop on `ISM Settings`
```powershell
1..10 | ForEach-Object { Write-Host "=== SETTINGS RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }
```
*Expected Output*: 10/10 PASS, 80/80 tests executed, 0 deadlocks, exit code 0.

### 4. Full Application Test Suite Execution
```bash
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
```
*Expected Output*:
`Ran 381 tests in ~35s, OK (skipped=254), exit code 0. 0 errors, 0 failures, 0 LinkValidationError, 0 QueryDeadlockError.`

### 5. Post-Suite MariaDB State Hygiene Check
```bash
wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('SE count:', frappe.db.count('Schedule Entry')); print('Test Customers:', frappe.get_all('Customer', filters=[['name', 'like', '_TEST%']], pluck='name')); print('Orphaned Vehicles:', frappe.get_all('Repair Vehicle', filters=[['name', 'like', '5YJ3E1EBXNF%']], pluck='name'))"
```
*Expected Output*:
`SE count: 0, Test Customers: [], Orphaned Vehicles: []`

### Invalidation Conditions
- Any occurrence of MariaDB Error 1020, 1205, or 1213.
- Any `LinkValidationError` or `AssertionError`.
- Any non-zero exit code on the 10x stress loops or full test suite.
- Residual test records surviving post-suite in MariaDB.
