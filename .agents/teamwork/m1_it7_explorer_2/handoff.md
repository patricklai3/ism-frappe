# Investigation & Architectural Remediation Plan — Milestone 1 Iteration 7

**Agent ID**: `m1_it7_explorer_2`  
**Role**: `explorer` (`teamwork_preview_explorer`)  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it7_explorer_2`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  
**Date**: 2026-10-27 UTC  
**Handoff Type**: Hard  

---

## Executive Summary

Milestone 1 Gate sign-off was rejected by Forensic Auditor (`m1_it6_auditor_1`) with an **INTEGRITY VIOLATION**, and by Challenger 2 (`m1_it6_challenger_2`) and Reviewer 1 (`m1_it6_reviewer_1`) with **REQUEST_CHANGES**. The rejection stemmed from four interconnected database transaction and concurrency defects:
1. **Mid-test commits in test helpers (`base.py`, `test_fixtures.py`, `test_schedule_entry.py`)**: Violated Frappe test transaction isolation, caused 66 `Schedule Entry` and 10 `Repair Vehicle` records to leak permanently into MariaDB, and triggered cascading MariaDB 1020 version contention on `tabSeries`.
2. **Raw rollbacks inside `ScheduleEntry.autoname()`**: When MariaDB 1020 occurred on `tabSeries`, `ScheduleEntry.autoname()` executed `frappe.db.rollback()`, which erased uncommitted caller fixtures (`Service Bay`, `Customer`, `Vehicle`) under the caller's feet, breaking tests like `TestServiceBay` and `TestTeslaIntegration`.
3. **Unguarded `tabSingles` updates in `test_ism_settings.py:213`**: `frappe.db.set_single_value` inside `test_setup_idempotency_preserves_customizations` had zero retry handling, crashing on Run 3 or Run 5 during sequential stress loops.
4. **Defective test vehicle purge filter in `cleanup.py`**: `_cleanup_repair_vehicles()` only matched vehicles whose customer was in `test_customers` (which requires the customer to still exist in `tabCustomer`) or whose plate was in a static list. Generated vehicles with random VINs (`5YJ3E1EBXNF%`) and `license_plate = NULL` were permanently bypassed.

This investigation provides the root causes, empirical evidence, and an exact code implementation plan to resolve all four areas safely without mid-test commits and without destructive inner rollbacks.

---

## 1. Observation

### 1.1 Static Code Observations

#### A. Mid-Test Commits in Test Helpers
- **`ism/tests/base.py`**:
  - Line 71: `frappe.db.commit()` in `create_test_customer()`
  - Line 97: `frappe.db.commit()` in `create_test_vehicle()`
  - Line 119: `frappe.db.commit()` in `create_test_quotation()`
  - Line 137: `frappe.db.commit()` in `create_test_repair_order()`
  - Line 159: `frappe.db.commit()` in `create_test_schedule_entry()`
  - Line 241: `frappe.db.commit()` in `cleanup_tracked_docs()`
- **`ism/tests/test_fixtures.py`**:
  - Line 342: `frappe.db.commit()` in `create_test_schedule_entry()`
  - Line 367: `frappe.db.commit()` in `create_test_repair_order()`
- **`ism/ism/doctype/schedule_entry/test_schedule_entry.py`**:
  - Line 16: `frappe.db.commit()` in `setUp()`
  - Line 23: `frappe.db.commit()` in `_insert_entry()`
  - Line 240: `frappe.db.commit()` in `test_requires_repair_order_validation()`

#### B. Destructive Rollbacks in Document Controllers and Test Helpers
- **`ism/ism/doctype/schedule_entry/schedule_entry.py`**:
  - Lines 21–23:
    ```python
    if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
        frappe.db.rollback()
        continue
    ```
- **`ism/tests/base.py`**:
  - Lines 75, 101, 123, 141, 163: `frappe.db.rollback()` inside retry loops in `create_test_customer`, `create_test_vehicle`, `create_test_quotation`, `create_test_repair_order`, `create_test_schedule_entry`.
- **`ism/tests/test_fixtures.py`**:
  - Lines 346, 371: `frappe.db.rollback()` inside `create_test_schedule_entry` and `create_test_repair_order`.
- **`ism/ism/doctype/schedule_entry/test_schedule_entry.py`**:
  - Line 27: `frappe.db.rollback()` in `_insert_entry`.

#### C. Unprotected `set_single_value` in `test_ism_settings.py`
- **`ism/ism/doctype/ism_settings/test_ism_settings.py`**:
  - Lines 213–214:
    ```python
    frappe.db.set_single_value("ISM Settings", "default_slot_interval", 45)
    frappe.db.set_single_value("ISM Settings", "enable_technician_capacity", 0)
    frappe.clear_cache(doctype="ISM Settings")
    ```
    Executed directly without retry logic, while `setUpClass` (lines 58–75) was wrapped in a 3-attempt restart loop.

#### D. Defective Vehicle Purge Filter in `cleanup.py`
- **`ism/tests/cleanup.py`**:
  - Lines 432–436:
    ```python
    all_vehs = frappe.get_all("Repair Vehicle", fields=["name", "customer", "license_plate"])
    for v in all_vehs:
        if v.name in PRESERVED_VEHICLES or v.customer in PRESERVED_CUSTOMERS:
            continue
        if v.customer in test_customers or v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]:
            ...
    ```
    1. Does not fetch `vin` in `fields`.
    2. Only checks `v.customer in test_customers`. `test_customers` is populated from existing `tabCustomer` rows via `_find_test_customers()`. If the customer was already deleted in test teardown, `v.customer in test_customers` is False.
    3. Vehicles generated by `base.py:88` have `vin = 5YJ3E1EBXNF<hash>` and `license_plate = None`, so `v.license_plate in ["REPAIR-01", ...]` is False.

---

### 1.2 Empirical Evidence Direct from Environment

#### Empirical Evidence 1: Current Database State Shows Leaked Vehicles
Direct query execution against `devcontainer-mariadb-1` via Python in container:
```python
frappe.get_all('Repair Vehicle', filters=[['name', 'like', '5YJ3E1EBXNF%']], fields=['name', 'customer', 'vin', 'license_plate'])
```
**Output**:
```json
[
  {"name": "5YJ3E1EBXNF44EA46", "customer": "_TEST_ISM_Cust_89a9940b", "vin": "5YJ3E1EBXNF44EA46", "license_plate": null},
  {"name": "5YJ3E1EBXNF441F26", "customer": "_TEST_ISM_Cust_6d9f0368", "vin": "5YJ3E1EBXNF441F26", "license_plate": null},
  {"name": "5YJ3E1EBXNFD325F1", "customer": "_TEST_ISM_Cust_9998e52f", "vin": "5YJ3E1EBXNFD325F1", "license_plate": null},
  {"name": "5YJ3E1EBXNF44CD7B", "customer": "_TEST_ISM_Cust_51baf67e", "vin": "5YJ3E1EBXNF44CD7B", "license_plate": null},
  {"name": "5YJ3E1EBXNFFF33F9", "customer": "_TEST_ISM_Cust_bfed407d", "vin": "5YJ3E1EBXNFFF33F9", "license_plate": null},
  {"name": "5YJ3E1EBXNF349F49", "customer": "_TEST_ISM_Cust_5ff111b7", "vin": "5YJ3E1EBXNF349F49", "license_plate": null},
  {"name": "5YJ3E1EBXNF0DE8E9", "customer": "_TEST_ISM_Cust_eb16e223", "vin": "5YJ3E1EBXNF0DE8E9", "license_plate": null},
  {"name": "5YJ3E1EBXNF72FCE6", "customer": "_TEST_ISM_Cust_cb468898", "vin": "5YJ3E1EBXNF72FCE6", "license_plate": null},
  {"name": "5YJ3E1EBXNF502C53", "customer": "_TEST_ISM_Cust_cfde1852", "vin": "5YJ3E1EBXNF502C53", "license_plate": null},
  {"name": "5YJ3E1EBXNF16AF39", "customer": "_TEST_ISM_Cust_11eebec9", "vin": "5YJ3E1EBXNF16AF39", "license_plate": null}
]
```
Running `cleanup_all_test_data(False)` left all 10 vehicles uncleaned in `tabRepair Vehicle`. Testing the customer existence for `_TEST_ISM_Cust_89a9940b` returned `None` (customer already deleted), confirming that `v.customer in test_customers` evaluated to `False`.

#### Empirical Evidence 2: MariaDB 11.8 Runtime Configuration
Direct query: `SHOW VARIABLES LIKE '%isolation%'`
**Output**:
```text
Variable_name               Value
innodb_snapshot_isolation   ON
transaction_isolation       REPEATABLE-READ
tx_isolation                REPEATABLE-READ
```

#### Empirical Evidence 3: Running Processes in Devcontainer
Executing `ps aux` in `devcontainer-frappe-1` revealed that background processes are running continuously against `development.localhost`:
- PID 22, 25300: `frappe serve --port 8000`
- PID 34: `frappe schedule`
- PID 36: `frappe worker`

#### Empirical Evidence 4: Frappe `parse_naming_series` Design
Inspecting `/workspace/development/frappe-bench/apps/frappe/frappe/model/naming.py:330-346`:
```python
def parse_naming_series(
    parts: list[str] | str,
    doctype=None,
    doc: "Document" | None = None,
    number_generator: Callable[[str, int], str] | None = None,
) -> str:
    """Parse the naming series and get next name.
    args:
        ...
        number_generator: Use different counter backend other than `tabSeries`. Primarily used for testing.
    """
```
Frappe natively provides `number_generator` in `parse_naming_series` specifically for test environments to avoid `tabSeries` lock contention.

---

## 2. Logic Chain

### 2.1 The Vicious Cycle of Mid-Test Commits and Inner Rollbacks

```
1. Under MariaDB 11.8 REPEATABLE READ with innodb_snapshot_isolation=ON:
   SELECT current FROM tabSeries WHERE name='SE-' FOR UPDATE
   encounters Error 1020 if another transaction modified tabSeries after snapshot creation.
   │
   ▼
2. Worker added retry loop with `frappe.db.rollback()` inside `ScheduleEntry.autoname()`.
   │
   ▼
3. When `autoname()` rolled back, it wiped out caller test fixtures (e.g. uncommitted ServiceBay in TestServiceBay
   or uncommitted Customer in TestTeslaIntegration) created earlier in the same test transaction!
   │
   ▼
4. To prevent fixtures from being wiped out by inner rollbacks, Worker added `frappe.db.commit()`
   after every fixture insertion in `base.py:create_test_*` and `test_fixtures.py`.
   │
   ▼
5. Mid-test commits wrote permanent changes to MariaDB (leaving 66 Schedule Entries and 10 Vehicles leaked)
   AND committed updates to `tabSeries` (e.g. 'RO-2026-') and `tabSingles`.
   │
   ▼
6. These mid-test commits actively created Error 1020 snapshot invalidations across `TestRepairOrder`
   and `test_setup_idempotency_preserves_customizations`!
```

### 2.2 Why `Repair Order` Deadlocked on `RO-2026-`
- `Repair Order` DocType specifies `"autoname": "naming_series:"` with default `"RO-.YYYY.-.#####"`.
- Unlike `Schedule Entry`, `Repair Order` had NO custom `autoname(self)` controller method.
- When `TestRepairOrder` methods executed `ro.insert()`, Frappe called `getseries("RO-2026-", 5)` executing:
  `SELECT current FROM tabSeries WHERE name='RO-2026-' FOR UPDATE`.
- Because preceding test modules (`test_tesla_integration.py`, `test_scheduling_capacity.py`) executed `create_test_repair_order()` which issued `frappe.db.commit()`, the row `'RO-2026-'` in `tabSeries` had committed updates.
- In `TestRepairOrder`, each test method's `setUp()` committed customer/vehicle and subsequent reads established an InnoDB read snapshot.
- When `ro.insert()` tried to execute `SELECT ... FOR UPDATE` on `'RO-2026-'`, MariaDB saw that `'RO-2026-'` had changed since snapshot creation and threw Error 1020.
- With zero retry or fallback logic in `Repair Order`, the test immediately crashed.

### 2.3 Why `ScheduleEntry.autoname()` Must NOT Roll Back
- `doc.insert()` is called within the caller's transaction context.
- A Document lifecycle hook cannot know what other uncommitted records exist in the transaction.
- Calling `frappe.db.rollback()` inside `autoname()` breaks caller transaction atomicity and wipes out parent fixtures.
- To handle MariaDB 1020 resilience safely:
  When `make_autoname()` fails with 1020, instead of rolling back the transaction, the controller must fall back to generating a unique series name directly from the existing records in `tab{doctype}` using a plain `SELECT` (`frappe.get_all`), which reads from the snapshot and never acquires row locks on `tabSeries`.

### 2.4 Why `base.py` Must NOT Issue `frappe.db.commit()`
- When test helpers (`create_test_customer`, `create_test_vehicle`, `create_test_repair_order`, `create_test_schedule_entry`) do not commit:
  1. All test data remains uncommitted in the test transaction.
  2. `tearDown()`'s `frappe.db.rollback()` automatically clears 100% of generated records in microseconds.
  3. No records leak into MariaDB.
  4. No row in `tabSeries` is ever committed during the test suite.
  5. Because no test commits to `tabSeries`, subsequent tests never see committed row version changes, eliminating the root cause of MariaDB 1020 collisions across tests!

---

## 3. Caveats

1. **Class-Boundary Setup**: `setUpClass` and `tearDownClass` boundaries in test classes that initialize shared static state (like `ensure_ism_settings()`) can legitimately commit at the class level if that state is intended to be global across the class, but individual test methods and test helpers must remain strictly uncommitted.
2. **Post-Suite Manual Cleanup**: In `cleanup.py:cleanup_all_test_data()`, issuing `frappe.db.commit()` at the very end (line 91) of an administrative maintenance script is legitimate and necessary to persist deletions.
3. **`test_setup_idempotency_preserves_customizations` Local Rollback**: Because this specific test evaluates whether `setup_default_ism_settings()` preserves customizations without committing, its internal retry loop on `frappe.db.set_single_value` can safely issue `frappe.db.rollback()` because it is executed at the very start of the test method before any other test fixtures or assertions are created.

---

## 4. Conclusion & Actionable Code Remediation Plan

### Remediation Item 1: Implement Safe Fallback Series Naming on `Schedule Entry` and `Repair Order`

Create a shared helper in `ism/scheduling/naming.py` (or within each controller):

```python
# ism/scheduling/naming.py or doctype controllers
import re
import frappe
from frappe.utils import now_datetime

def get_safe_series_name(doctype: str, series_pattern: str, doc=None, digits: int = 5) -> str:
	"""Generate a unique series name safely without tabSeries row locks or transaction rollbacks.
	
	Used as fallback when MariaDB 1020 (ER_RECORD_CHANGED) occurs on tabSeries.
	Uses standard consistent read (frappe.get_all) which never conflicts with snapshot isolation.
	"""
	today = now_datetime()
	parts = series_pattern.split(".")
	prefix_parts = []
	num_digits = digits

	for p in parts:
		if p.startswith("#"):
			num_digits = len(p)
			break
		elif p == "YYYY":
			prefix_parts.append(today.strftime("%Y"))
		elif p == "YY":
			prefix_parts.append(today.strftime("%y"))
		elif p == "MM":
			prefix_parts.append(today.strftime("%m"))
		elif p == "DD":
			prefix_parts.append(today.strftime("%d"))
		elif doc and hasattr(doc, "get") and doc.get(p):
			prefix_parts.append(str(doc.get(p)))
		else:
			prefix_parts.append(p)

	prefix = "".join(prefix_parts)

	# Clean SELECT via index: never throws 1020, never locks tabSeries
	existing = frappe.get_all(
		doctype,
		filters=[["name", "like", f"{prefix}%"]],
		order_by="name desc",
		limit=1,
		pluck="name",
	)

	next_num = 1
	if existing and existing[0].startswith(prefix):
		suffix = existing[0][len(prefix):]
		match = re.search(r"(\d+)", suffix)
		if match:
			try:
				next_num = int(match.group(1)) + 1
			except ValueError:
				next_num = 1

	candidate = f"{prefix}{next_num:0{num_digits}d}"
	while frappe.db.exists(doctype, candidate):
		next_num += 1
		candidate = f"{prefix}{next_num:0{num_digits}d}"

	return candidate
```

#### Update `ScheduleEntry.autoname()` in `ism/ism/doctype/schedule_entry/schedule_entry.py`:
```python
	def autoname(self):
		"""Assign naming series SE-.##### with MariaDB 1020 resilience without transaction rollback."""
		from frappe.model.naming import make_autoname

		prefix = self.naming_series or "SE-.#####"
		try:
			self.name = make_autoname(prefix, doc=self)
		except Exception as e:
			if "1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError):
				# Safely fallback to non-locking series generator without rolling back caller transaction
				self.name = self._generate_safe_series_name(prefix)
			else:
				raise

	def _generate_safe_series_name(self, series_pattern: str) -> str:
		import re
		from frappe.utils import now_datetime
		today = now_datetime()
		parts = series_pattern.split(".")
		prefix_parts = []
		digits = 5
		for p in parts:
			if p.startswith("#"):
				digits = len(p)
				break
			elif p == "YYYY":
				prefix_parts.append(today.strftime("%Y"))
			elif p == "YY":
				prefix_parts.append(today.strftime("%y"))
			elif p == "MM":
				prefix_parts.append(today.strftime("%m"))
			elif p == "DD":
				prefix_parts.append(today.strftime("%d"))
			elif hasattr(self, p) and getattr(self, p):
				prefix_parts.append(str(getattr(self, p)))
			else:
				prefix_parts.append(p)
		prefix = "".join(prefix_parts)
		existing = frappe.get_all(
			self.doctype,
			filters=[["name", "like", f"{prefix}%"]],
			order_by="name desc",
			limit=1,
			pluck="name",
		)
		next_num = 1
		if existing and existing[0].startswith(prefix):
			suffix = existing[0][len(prefix):]
			match = re.search(r"(\d+)", suffix)
			if match:
				try:
					next_num = int(match.group(1)) + 1
				except ValueError:
					next_num = 1
		candidate = f"{prefix}{next_num:0{digits}d}"
		while frappe.db.exists(self.doctype, candidate):
			next_num += 1
			candidate = f"{prefix}{next_num:0{digits}d}"
		return candidate
```

#### Add `RepairOrder.autoname()` in `ism/ism/doctype/repair_order/repair_order.py`:
```python
	def autoname(self):
		"""Assign naming series RO-.YYYY.-.##### with MariaDB 1020 resilience without transaction rollback."""
		from frappe.model.naming import make_autoname

		prefix = self.naming_series or "RO-.YYYY.-.#####"
		try:
			self.name = make_autoname(prefix, doc=self)
		except Exception as e:
			if "1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError):
				self.name = self._generate_safe_series_name(prefix)
			else:
				raise

	def _generate_safe_series_name(self, series_pattern: str) -> str:
		import re
		from frappe.utils import now_datetime
		today = now_datetime()
		parts = series_pattern.split(".")
		prefix_parts = []
		digits = 5
		for p in parts:
			if p.startswith("#"):
				digits = len(p)
				break
			elif p == "YYYY":
				prefix_parts.append(today.strftime("%Y"))
			elif p == "YY":
				prefix_parts.append(today.strftime("%y"))
			elif p == "MM":
				prefix_parts.append(today.strftime("%m"))
			elif p == "DD":
				prefix_parts.append(today.strftime("%d"))
			elif hasattr(self, p) and getattr(self, p):
				prefix_parts.append(str(getattr(self, p)))
			else:
				prefix_parts.append(p)
		prefix = "".join(prefix_parts)
		existing = frappe.get_all(
			self.doctype,
			filters=[["name", "like", f"{prefix}%"]],
			order_by="name desc",
			limit=1,
			pluck="name",
		)
		next_num = 1
		if existing and existing[0].startswith(prefix):
			suffix = existing[0][len(prefix):]
			match = re.search(r"(\d+)", suffix)
			if match:
				try:
					next_num = int(match.group(1)) + 1
				except ValueError:
					next_num = 1
		candidate = f"{prefix}{next_num:0{digits}d}"
		while frappe.db.exists(self.doctype, candidate):
			next_num += 1
			candidate = f"{prefix}{next_num:0{digits}d}"
		return candidate
```

---

### Remediation Item 2: Remove Mid-Test `commit()` and Inner `rollback()` from `base.py` and Test Helpers

1. In `ism/tests/base.py`:
   - Delete line 71: `frappe.db.commit()` in `create_test_customer()`
   - Delete line 75: `frappe.db.rollback()` in `create_test_customer()`
   - Delete line 97: `frappe.db.commit()` in `create_test_vehicle()`
   - Delete line 101: `frappe.db.rollback()` in `create_test_vehicle()`
   - Delete line 119: `frappe.db.commit()` in `create_test_quotation()`
   - Delete line 123: `frappe.db.rollback()` in `create_test_quotation()`
   - Delete line 137: `frappe.db.commit()` in `create_test_repair_order()`
   - Delete line 141: `frappe.db.rollback()` in `create_test_repair_order()`
   - Delete line 159: `frappe.db.commit()` in `create_test_schedule_entry()`
   - Delete line 163: `frappe.db.rollback()` in `create_test_schedule_entry()`
2. In `ism/tests/test_fixtures.py`:
   - Delete lines 342, 367 (`frappe.db.commit()`)
   - Delete lines 346, 371 (`frappe.db.rollback()`)
3. In `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
   - Delete line 16: `frappe.db.commit()` in `setUp()`
   - Delete line 23: `frappe.db.commit()` in `_insert_entry()`
   - Delete line 27: `frappe.db.rollback()` in `_insert_entry()`
   - Delete line 240: `frappe.db.commit()` in `test_requires_repair_order_validation()`

---

### Remediation Item 3: Retry Handling in `test_ism_settings.py:213`

Replace lines 209–224 of `ism/ism/doctype/ism_settings/test_ism_settings.py` with:

```python
	def test_setup_idempotency_preserves_customizations(self):
		"""Verify setup_default_ism_settings preserves existing customizations without committing state."""
		for attempt in range(3):
			try:
				# Apply custom values via set_single_value in uncommitted transaction
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

### Remediation Item 4: Fix `cleanup.py:_cleanup_repair_vehicles()` to Purge Orphaned Vehicles

In `ism/tests/cleanup.py`, replace lines 432–442:

```python
	all_vehs = frappe.get_all("Repair Vehicle", fields=["name", "customer", "license_plate", "vin"])
	for v in all_vehs:
		if v.name in PRESERVED_VEHICLES or v.customer in PRESERVED_CUSTOMERS:
			continue
		is_test = (
			v.customer in test_customers
			or (v.customer and any(v.customer.startswith(p) for p in TEST_CUST_PREFIXES))
			or v.license_plate in ["REPAIR-01", "TEST-PLATE", "TEST-INTAKE"]
			or (v.license_plate and v.license_plate.startswith("TEST"))
			or (v.vin and v.vin.startswith("5YJ3E1EBXNF"))
			or (v.name and v.name.startswith("5YJ3E1EBXNF"))
		)
		if is_test:
			try:
				frappe.delete_doc("Repair Vehicle", v.name, force=True, ignore_permissions=True)
				log(f"Deleted test Repair Vehicle: {v.name}")
			except Exception as e:
				log(f"Failed deleting test Repair Vehicle {v.name}: {e}")
```

---

## 5. Verification Method

To verify the proposed remediations independently once implemented by Worker:

1. **Verify `cleanup_all_test_data()` purges all 10 leaked vehicles**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); from ism.tests.cleanup import cleanup_all_test_data; cleanup_all_test_data(False); non_seed = [v.name for v in frappe.get_all('Repair Vehicle', fields=['name']) if v.name not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Remaining non-seed vehicles:', non_seed); assert len(non_seed) == 0, f'Expected 0 leaked vehicles, found {len(non_seed)}'"
   ```

2. **Verify 10x sequential stress loop on `Schedule Entry`**:
   ```powershell
   1..10 | ForEach-Object { Write-Host "=== SE RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"; if ($LASTEXITCODE -ne 0) { exit 1 } }
   ```

3. **Verify 10x sequential stress loop on `ISM Settings`**:
   ```powershell
   1..10 | ForEach-Object { Write-Host "=== SETTINGS RUN $_ ==="; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "ISM Settings"; if ($LASTEXITCODE -ne 0) { exit 1 } }
   ```

4. **Verify back-to-back full application test suite passes with 0 errors**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
   ```
   *Expected result*: Ran 381 tests, OK (skipped=254), exit code 0.

5. **Verify zero residual records leaked post-suite**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('SE count:', frappe.db.count('Schedule Entry')); print('Test Customers:', frappe.get_all('Customer', filters=[['name', 'like', '_TEST%']], pluck='name')); non_seed = [v.name for v in frappe.get_all('Repair Vehicle', fields=['name']) if v.name not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Leaked vehicles:', len(non_seed)); assert frappe.db.count('Schedule Entry') == 0; assert len(non_seed) == 0"
   ```

### Invalidation Conditions
- Any occurrence of MariaDB Error 1020, 1205, or 1213 in test runs.
- Non-zero count of non-seed `Repair Vehicle` or `Schedule Entry` records leaking into MariaDB.
- Any mid-test commit (`frappe.db.commit()`) in `base.py` or test helpers.
