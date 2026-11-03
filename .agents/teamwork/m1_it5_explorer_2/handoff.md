# Investigation & Architecture Blueprint: Preserving Static Test Fixtures

**Agent**: `m1_it5_explorer_2`  
**Milestone**: Milestone 1 Iteration 5 Remediation  
**Date**: 2026-10-26  
**Scope**: Preserving static test fixtures (`Test Customer` and `5YJSA1E26CF000001`) in `cleanup.py`, `test_fixtures.py`, `base.py`, and test callers to eliminate MariaDB 1020 tombstone collisions and `LinkValidationError`.

---

## 1. Observation

### 1.1 Verbatim Audit Failures (from `m1_it4_auditor_1/handoff.md` Section 1.2)
During full application regression suite execution (`bench --site development.localhost run-tests --app ism`), 6 errors occurred resulting in exit code 1:

```text
======================================================================
 ERROR  test_tier2_f10_slot_generator_no_operating_hours (ism.tests.test_scheduling_capacity.TestSchedulingCapacity.test_tier2_f10_slot_generator_no_operating_hours)
----------------------------------------------------------------------
frappe.exceptions.LinkValidationError: Could not find Customer: Test Customer, Vehicle: 5YJSA1E26CF000001

======================================================================
 ERROR  test_tier1_f27_calendar_events_payload (ism.tests.test_ro_scheduling_e2e.TestRepairOrderSchedulingE2E.test_tier1_f27_calendar_events_payload)
----------------------------------------------------------------------
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")

======================================================================
 ERROR  test_tier1_f1_p80_15_minutes (ism.tests.test_estimation_math.TestEstimationMath.test_tier1_f1_p80_15_minutes)
----------------------------------------------------------------------
MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")
frappe.exceptions.QueryDeadlockError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")
```

### 1.2 Inspection of `ism/tests/cleanup.py`
- **Lines 7–15**:
  ```python
  TEST_PREFIX = "_TEST_ISM_"
  TEST_VINS = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456", "5YJSA1E26CF000001"]

  PRESERVED_CUSTOMERS = {"Prestiga-Biz"}
  PRESERVED_VEHICLES = {"LRW3F7FS2NC5xxxxx", "LRW3F7ET2RC1xxxxx"}
  PRESERVED_REPAIR_ORDERS = {"RO-2026-00067", "RO-2026-00105"}
  ...
  TEST_CUST_PREFIXES = ("_TEST", "_Test", "Test", "_Husband", "_Wife", "_Spouse")
  ```
- **Lines 84–92 (`_find_test_customers`)**:
  ```python
  def _find_test_customers():
      all_custs = frappe.get_all("Customer", pluck="name")
      test_custs = set()
      for c in all_custs:
          if c in PRESERVED_CUSTOMERS:
              continue
          if any(c.startswith(p) for p in TEST_CUST_PREFIXES) or "Test" in c or "TEST" in c:
              test_custs.add(c)
      return test_custs
  ```
  `"Test Customer"` matches `any(c.startswith(p) for p in TEST_CUST_PREFIXES)` and is NOT in `PRESERVED_CUSTOMERS`.
- **Lines 408–418 (`_cleanup_repair_vehicles`)**:
  ```python
  def _cleanup_repair_vehicles(test_customers, log):
      for vin in TEST_VINS:
          if vin in PRESERVED_VEHICLES:
              continue
          if frappe.db.exists("Repair Vehicle", vin):
              try:
                  frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
  ```
  `"5YJSA1E26CF000001"` is in `TEST_VINS` and NOT in `PRESERVED_VEHICLES`. It is deleted on every cleanup run.
- **Lines 431–447 (`_cleanup_customers`)**:
  ```python
  def _cleanup_customers(test_customers, stub_customers_created, log):
      all_to_clean = test_customers | stub_customers_created
      for cust in all_to_clean:
          if cust in PRESERVED_CUSTOMERS:
              continue
          ...
          if frappe.db.exists("Customer", cust):
              frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True)
  ```
  `"Test Customer"` is unconditionally deleted and committed at line 80 (`frappe.db.commit()`).
- **Lines 223–230 (`_cleanup_schedule_entries`)**:
  ```python
  for se in all_ses:
      if se.repair_order and se.repair_order in PRESERVED_REPAIR_ORDERS:
          continue
      if se.customer and se.customer in PRESERVED_CUSTOMERS:
          continue
      if se.repair_vehicle and se.repair_vehicle in PRESERVED_VEHICLES:
          continue
  ```
- **Lines 338–340 (`_cleanup_repair_orders`)**:
  ```python
  for ro in all_ros:
      if ro.name in PRESERVED_REPAIR_ORDERS or ro.customer in PRESERVED_CUSTOMERS:
          continue
  ```

### 1.3 Inspection of `ism/tests/test_fixtures.py`
- **Lines 8–18, 116–117**:
  ```python
  PREFIX = "Test "
  TEST_PREFIX = "_TEST_ISM_"
  ...
  CUSTOMER = f"{PREFIX}Customer"        # "Test Customer"
  VEHICLE_VIN = "5YJSA1E26CF000001"
  ```
- **Lines 124–136 (`setup_all`)**:
  ```python
  def setup_all():
      """Idempotently seeds all standard test master records for capacity scheduling."""
      ensure_company()
      ensure_customer()
      ensure_repair_vehicle()
      ensure_equipment_tags()
      ensure_service_bays()
      ensure_schedule_entry_types()
      emp_map = ensure_employees()
      ensure_items()
      ensure_ism_settings()
      frappe.db.commit()
      return emp_map
  ```
- **Lines 150–167 (`ensure_customer`, `ensure_repair_vehicle`)**:
  ```python
  def ensure_customer():
      if not frappe.db.exists("Customer", CUSTOMER):
          frappe.get_doc({
              "doctype": "Customer",
              "customer_name": CUSTOMER,
              "customer_type": "Individual",
          }).insert(ignore_permissions=True)

  def ensure_repair_vehicle():
      if frappe.db.exists("DocType", "Repair Vehicle") and not frappe.db.exists("Repair Vehicle", VEHICLE_VIN):
          frappe.get_doc({
              "doctype": "Repair Vehicle",
              "vin": VEHICLE_VIN,
              "license_plate": "TEST-SCHED",
              "customer": CUSTOMER,
          }).insert(ignore_permissions=True)
  ```
- **Lines 350–399 (`teardown_transactional`)**:
  Purges transactional `Schedule Entry`, `Leave Application`, and `Repair Order` (lines 386–397 specifically purge ROs for `CUSTOMER` and `TEST_PREFIX`), leaving master fixtures (`Test Customer`, `5YJSA1E26CF000001`) intact.

### 1.4 Inspection of `ism/tests/base.py`
- **Lines 14–24 (`setUpClass`, `tearDownClass`)**:
  ```python
  @classmethod
  def setUpClass(cls):
      super().setUpClass()
      cls.cleanup_residual_test_data()

  @classmethod
  def tearDownClass(cls):
      cls.cleanup_residual_test_data()
      super().tearDownClass()
  ```
- **Lines 49–71 (`create_test_customer`, `create_test_vehicle`)**:
  ```python
  def create_test_customer(self, customer_name=None, **kwargs):
      name = customer_name or f"{TEST_PREFIX}Cust_{frappe.generate_hash(length=8)}"
      if frappe.db.exists("Customer", name):
          frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
      doc = frappe.get_doc({...}).insert(ignore_permissions=True)
      return self.track(doc)

  def create_test_vehicle(self, vin=None, customer=None, **kwargs):
      vin = vin or f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"
      if frappe.db.exists("Repair Vehicle", vin):
          frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
      doc = frappe.get_doc({...}).insert(ignore_permissions=True)
      return self.track(doc)
  ```
- **Lines 123–162 (`cleanup_tracked_docs`)**:
  Deletes tracked documents in reverse dependency order, including `Customer` and `Repair Vehicle`.

### 1.5 Inspection of `ism/ism/doctype/repair_order/test_repair_order.py`
- **Lines 20–26 (`setUp`)**:
  ```python
  def setUp(self):
      super().setUp()
      self.customer = self.create_test_customer(customer_name=f"{TEST_PREFIX}Repair_Cust")
      self.vehicle = self.create_test_vehicle(
          vin="5YJSA1E26CF000001",
          license_plate="REPAIR-01",
          customer=self.customer.name,
      )
  ```
  `TestRepairOrder` explicitly hijacks VIN `"5YJSA1E26CF000001"`, deleting it via `create_test_vehicle()`, re-inserting it under `_TEST_ISM_Repair_Cust`, and tracking it for deletion during `tearDown()`.

### 1.6 Inspection of Test Discovery Aliases
- `ism/tests/test_scheduling.py`: `from ism.tests.test_scheduling_capacity import TestSchedulingCapacity`
- `ism/tests/test_technician_availability.py`: `from ism.tests.test_scheduling_capacity import TestSchedulingCapacity`
- `ism/tests/test_estimation.py`: `from ism.tests.test_estimation_math import TestEstimationMath`
- `ism/tests/test_repair_order_scheduling.py`: `from ism.tests.test_ro_scheduling_e2e import TestRepairOrderSchedulingE2E`
Because of these alias files, the Frappe unittest runner discovers and executes `TestSchedulingCapacity` 3 distinct times and `TestEstimationMath` and `TestRepairOrderSchedulingE2E` 2 distinct times during a single `bench run-tests --app ism` run.

---

## 2. Logic Chain

```
[Observation 1.6: Duplicate class discovery]
                  +
[Observation 1.4: setUpClass/tearDownClass invoke cleanup_all_test_data()]
                  │
                  ▼
[Observation 1.2: cleanup_all_test_data() purges "Test Customer" & "5YJSA1E26CF000001"]
                  │
                  ▼
[MariaDB 11 InnoDB MVCC: delete-marked b-tree tombstones remain in index snapshot]
                  │
                  ▼
[Observation 1.3: Next test class calls setup_all() -> ensure_customer() insert]
                  │
                  ▼
[Observation 1.1: Error 1020 "Record has changed since last read in table 'tabCustomer'"]
                  │
                  ▼
[Transaction rollback or failed ensure_repair_vehicle leaves customer/vehicle missing]
                  │
                  ▼
[Observation 1.1: LinkValidationError: Could not find Customer: Test Customer, Vehicle: 5YJSA1E26CF000001]
```

1. **Test Class Execution Multiplicity**:
   In Frappe's test runner, each `test_*.py` file is discovered independently. Because `test_scheduling.py`, `test_technician_availability.py`, and `test_scheduling_capacity.py` all reference `TestSchedulingCapacity`, `TestSchedulingCapacity` runs three separate times in sequence within the same overall test invocation.
2. **Boundary Cleanup Destroys Static Fixtures**:
   At every class boundary (`setUpClass()` and `tearDownClass()`), `ISMTestCase` runs `cleanup_all_test_data()`. In `cleanup.py`, `_find_test_customers()` identifies `"Test Customer"` because it matches prefix `"Test"`, and `_cleanup_repair_vehicles()` identifies `"5YJSA1E26CF000001"` because it was in `TEST_VINS`. Neither was in `PRESERVED_CUSTOMERS` or `PRESERVED_VEHICLES`. Consequently, both static master records are deleted from MariaDB and committed at the end of every class run.
3. **MariaDB 1020 Index Tombstone Collisions**:
   Under MariaDB 11 InnoDB with `REPEATABLE READ` transaction isolation, deleting a primary key row leaves an internal delete-marked tombstone in the clustered index. When the next test suite begins within milliseconds and invokes `setup_all() -> ensure_customer() -> frappe.get_doc({...}).insert()`, MariaDB encounters an MVCC read-view snapshot version conflict against the recently committed delete-marked row. MariaDB raises `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabCustomer'; try restarting transaction")`, translated by Frappe into `frappe.exceptions.QueryDeadlockError`.
4. **LinkValidationError Cascade**:
   When `ensure_customer()` or `ensure_repair_vehicle()` fails or is aborted due to error 1020, or when `TestRepairOrder` steals and deletes `"5YJSA1E26CF000001"` in its own `setUp()`/`tearDown()`, `TestSchedulingCapacity.setUp()` cannot find the master records when executing `create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)`. Frappe's link validator raises `LinkValidationError: Could not find Customer: Test Customer, Vehicle: 5YJSA1E26CF000001`.
5. **Architectural Separation: Seed Master vs. Fixture Master vs. Fixture Transactions**:
   - Master fixture records (`Test Customer`, `5YJSA1E26CF000001`, `Test Bay 1/2/3`, `Standard Schedule Entry Types`, `Equipment Tags`, `Technician Employees`) are static invariant fixtures. They must NEVER be deleted between test runs.
   - Transactional records (`Schedule Entry`, `Repair Order`, `Sales Invoice`, `Payment Entry`) created during tests referencing `Test Customer` or `5YJSA1E26CF000001` ARE test data and MUST be purged so that bays and schedules remain clear.
   - Therefore, `cleanup.py` must distinguish between `SEED_PRESERVED_CUSTOMERS` (where both master and transactions are preserved) and `STATIC_FIXTURE_CUSTOMERS` (where master records are preserved, but all transactional records are purged).

---

## 3. Caveats

- **Scope Boundary**: Explorer 2 formulations specifically resolve fixture preservation for `Test Customer` and `5YJSA1E26CF000001` across `cleanup.py`, `test_fixtures.py`, `base.py`, and test classes. Remediation of `ISM Settings` / `tabSingles` concurrency in `test_ism_settings.py` is covered by Explorer 1.
- **Database Idempotence**: `setup_all()` in `test_fixtures.py` is idempotent by design (`if not frappe.db.exists(...)`). When static master records are preserved, `setup_all()` executes zero write DML on `tabCustomer` and `tabRepair Vehicle`, entirely bypassing table locks.
- **Post-Test Cleanliness Invariant**: Preserving `Customer: Test Customer` and `Repair Vehicle: 5YJSA1E26CF000001` does not compromise Phase 2.5 of the forensic audit, which inspects for zero residual `Customer ('_TEST%')`, zero `Repair Vehicle ('5YJ3E1EBXNF%')`, zero `Repair Order`, and zero `Schedule Entry`.

---

## 4. Conclusion & Exact Code Modification Blueprint

To permanently resolve MariaDB 1020 deadlocks on `tabCustomer` and `LinkValidationError` across all test suites:

### 4.1 Target 1: `ism/tests/cleanup.py`
Separate master preservation from transactional preservation by defining:
- `STATIC_FIXTURE_CUSTOMERS = {"Test Customer"}`
- `STATIC_FIXTURE_VEHICLES = {"5YJSA1E26CF000001"}`
- `SEED_PRESERVED_CUSTOMERS = {"Prestiga-Biz"}`
- `SEED_PRESERVED_VEHICLES = {"LRW3F7FS2NC5xxxxx", "LRW3F7ET2RC1xxxxx"}`
- `PRESERVED_CUSTOMERS = SEED_PRESERVED_CUSTOMERS | STATIC_FIXTURE_CUSTOMERS`
- `PRESERVED_VEHICLES = SEED_PRESERVED_VEHICLES | STATIC_FIXTURE_VEHICLES`
- Remove `"5YJSA1E26CF000001"` from `TEST_VINS`.
- In `_cleanup_repair_orders()` and `_cleanup_schedule_entries()`, only skip records linked to `SEED_PRESERVED_CUSTOMERS` / `SEED_PRESERVED_VEHICLES`, allowing all test transactions referencing `Test Customer` and `5YJSA1E26CF000001` to be cleanly purged.

#### Diff Specification for `ism/tests/cleanup.py`:
```diff
--- a/ism/tests/cleanup.py
+++ b/ism/tests/cleanup.py
@@ -6,9 +6,15 @@
 TEST_PREFIX = "_TEST_ISM_"
-TEST_VINS = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456", "5YJSA1E26CF000001"]
+TEST_VINS = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456"]
 
-PRESERVED_CUSTOMERS = {"Prestiga-Biz"}
-PRESERVED_VEHICLES = {"LRW3F7FS2NC5xxxxx", "LRW3F7ET2RC1xxxxx"}
+STATIC_FIXTURE_CUSTOMERS = {"Test Customer"}
+STATIC_FIXTURE_VEHICLES = {"5YJSA1E26CF000001"}
+
+SEED_PRESERVED_CUSTOMERS = {"Prestiga-Biz"}
+SEED_PRESERVED_VEHICLES = {"LRW3F7FS2NC5xxxxx", "LRW3F7ET2RC1xxxxx"}
+
+PRESERVED_CUSTOMERS = SEED_PRESERVED_CUSTOMERS | STATIC_FIXTURE_CUSTOMERS
+PRESERVED_VEHICLES = SEED_PRESERVED_VEHICLES | STATIC_FIXTURE_VEHICLES
 PRESERVED_REPAIR_ORDERS = {"RO-2026-00067", "RO-2026-00105"}
 PRESERVED_SALES_INVOICES = {"ACC-SINV-2026-00046", "ACC-SINV-2026-00047"}
 PRESERVED_PAYMENT_ENTRIES = {"ACC-PAY-2026-00043", "ACC-PAY-2026-00044"}
@@ -121,7 +127,7 @@ def _cleanup_payment_entries(test_customers, log):
 	all_pes = frappe.get_all("Payment Entry", fields=["name", "party", "docstatus"])
 	for pe in all_pes:
-		if pe.name in PRESERVED_PAYMENT_ENTRIES or pe.party in PRESERVED_CUSTOMERS:
+		if pe.name in PRESERVED_PAYMENT_ENTRIES or pe.party in SEED_PRESERVED_CUSTOMERS:
 			continue
 		is_test = pe.party in test_customers or any(pe.party.startswith(p) for p in TEST_CUST_PREFIXES) if pe.party else False
@@ -138,7 +144,7 @@ def _cleanup_sales_invoices(test_customers, log):
 	all_sis = frappe.get_all("Sales Invoice", fields=["name", "customer", "docstatus"])
 	for si in all_sis:
-		if si.name in PRESERVED_SALES_INVOICES or si.customer in PRESERVED_CUSTOMERS:
+		if si.name in PRESERVED_SALES_INVOICES or si.customer in SEED_PRESERVED_CUSTOMERS:
 			continue
 		is_test = si.customer in test_customers or any(si.customer.startswith(p) for p in TEST_CUST_PREFIXES) if si.customer else False
@@ -167,7 +173,7 @@ def _cleanup_sales_orders(test_customers, log):
 	all_sos = frappe.get_all("Sales Order", fields=["name", "customer", "docstatus"])
 	for so in all_sos:
-		if so.customer in PRESERVED_CUSTOMERS:
+		if so.customer in SEED_PRESERVED_CUSTOMERS:
 			continue
 		if so.customer in test_customers or (so.customer and any(so.customer.startswith(p) for p in TEST_CUST_PREFIXES)):
@@ -187,7 +193,7 @@ def _cleanup_quotations(test_customers, log):
 	all_qtns = frappe.get_all("Quotation", fields=["name", "party_name", "docstatus"])
 	for qtn in all_qtns:
-		if qtn.party_name in PRESERVED_CUSTOMERS:
+		if qtn.party_name in SEED_PRESERVED_CUSTOMERS:
 			continue
 		if qtn.party_name in test_customers or (qtn.party_name and any(qtn.party_name.startswith(p) for p in TEST_CUST_PREFIXES)):
@@ -225,9 +231,9 @@ def _cleanup_schedule_entries(test_customers, log):
 		if se.repair_order and se.repair_order in PRESERVED_REPAIR_ORDERS:
 			continue
-		if se.customer and se.customer in PRESERVED_CUSTOMERS:
+		if se.customer and se.customer in SEED_PRESERVED_CUSTOMERS:
 			continue
-		if se.repair_vehicle and se.repair_vehicle in PRESERVED_VEHICLES:
+		if se.repair_vehicle and se.repair_vehicle in SEED_PRESERVED_VEHICLES:
 			continue
 
 		is_test = False
@@ -338,7 +344,7 @@ def _cleanup_repair_orders(test_customers, log):
 	all_ros = frappe.get_all("Repair Order", fields=["name", "customer", "vehicle", "sales_invoice", "docstatus"])
 	for ro in all_ros:
-		if ro.name in PRESERVED_REPAIR_ORDERS or ro.customer in PRESERVED_CUSTOMERS:
+		if ro.name in PRESERVED_REPAIR_ORDERS or ro.customer in SEED_PRESERVED_CUSTOMERS:
 			continue
 		is_test = (
 			ro.customer in test_customers
@@ -397,7 +403,7 @@ def _cleanup_vehicle_checkins(test_customers, log):
 	all_vcis = frappe.get_all("Vehicle Check-in", fields=["name", "customer", "vehicle"])
 	for vci in all_vcis:
-		if vci.customer in PRESERVED_CUSTOMERS:
+		if vci.customer in SEED_PRESERVED_CUSTOMERS:
 			continue
 		if vci.customer in test_customers or vci.vehicle in TEST_VINS:
```

---

### 4.2 Target 2: `ism/tests/test_fixtures.py`
Make `ensure_repair_vehicle()` resilient: if the vehicle already exists, ensure its primary customer is still `CUSTOMER` without deleting or re-inserting.

#### Diff Specification for `ism/tests/test_fixtures.py`:
```diff
--- a/ism/tests/test_fixtures.py
+++ b/ism/tests/test_fixtures.py
@@ -160,11 +160,17 @@ def ensure_customer():
 def ensure_repair_vehicle():
-	if frappe.db.exists("DocType", "Repair Vehicle") and not frappe.db.exists("Repair Vehicle", VEHICLE_VIN):
-		frappe.get_doc({
-			"doctype": "Repair Vehicle",
-			"vin": VEHICLE_VIN,
-			"license_plate": "TEST-SCHED",
-			"customer": CUSTOMER,
-		}).insert(ignore_permissions=True)
+	if not frappe.db.exists("DocType", "Repair Vehicle"):
+		return
+	if not frappe.db.exists("Repair Vehicle", VEHICLE_VIN):
+		frappe.get_doc({
+			"doctype": "Repair Vehicle",
+			"vin": VEHICLE_VIN,
+			"license_plate": "TEST-SCHED",
+			"customer": CUSTOMER,
+		}).insert(ignore_permissions=True)
+	else:
+		veh = frappe.get_doc("Repair Vehicle", VEHICLE_VIN)
+		if veh.customer != CUSTOMER:
+			veh.customer = CUSTOMER
+			veh.save(ignore_permissions=True)
```

---

### 4.3 Target 3: `ism/tests/base.py`
Guard `create_test_customer()` and `create_test_vehicle()` in `base.py` against deleting or tracking static fixture entities. Remove `5YJSA1E26CF000001` from `base.py:TEST_VINS`.

#### Diff Specification for `ism/tests/base.py`:
```diff
--- a/ism/tests/base.py
+++ b/ism/tests/base.py
@@ -6,8 +6,11 @@
 import unittest
 import frappe
 
 TEST_PREFIX = "_TEST_ISM_"
-TEST_VINS = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456", "5YJSA1E26CF000001"]
+TEST_VINS = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456"]
+
+STATIC_FIXTURE_CUSTOMERS = {"Test Customer", "Prestiga-Biz"}
+STATIC_FIXTURE_VEHICLES = {"5YJSA1E26CF000001", "LRW3F7FS2NC5xxxxx", "LRW3F7ET2RC1xxxxx"}
 
 
 class ISMTestCase(unittest.TestCase):
@@ -50,6 +53,8 @@ class ISMTestCase(unittest.TestCase):
 	def create_test_customer(self, customer_name=None, **kwargs):
 		name = customer_name or f"{TEST_PREFIX}Cust_{frappe.generate_hash(length=8)}"
+		if name in STATIC_FIXTURE_CUSTOMERS:
+			return frappe.get_doc("Customer", name)
 		if frappe.db.exists("Customer", name):
 			frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
 		doc = frappe.get_doc({
@@ -62,6 +67,12 @@ class ISMTestCase(unittest.TestCase):
 	def create_test_vehicle(self, vin=None, customer=None, **kwargs):
+		if vin in STATIC_FIXTURE_VEHICLES:
+			veh = frappe.get_doc("Repair Vehicle", vin)
+			if customer and veh.customer != customer:
+				veh.customer = customer
+				veh.save(ignore_permissions=True)
+			return veh
 		vin = vin or f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"
 		if frappe.db.exists("Repair Vehicle", vin):
 			frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
@@ -137,6 +148,10 @@ class ISMTestCase(unittest.TestCase):
 		for doctype, name in committed_docs:
 			if not frappe.db.exists(doctype, name):
 				continue
+			if doctype == "Customer" and name in STATIC_FIXTURE_CUSTOMERS:
+				continue
+			if doctype == "Repair Vehicle" and name in STATIC_FIXTURE_VEHICLES:
+				continue
 			try:
 				doc = frappe.get_doc(doctype, name)
@@ -204,6 +219,8 @@ class ISMTestCase(unittest.TestCase):
 	def _teardown_customer(self, doc):
+		if doc.name in STATIC_FIXTURE_CUSTOMERS:
+			return
 		# Clean up any referencing Schedule Entries before deleting Customer
 		if frappe.db.exists("DocType", "Schedule Entry"):
```

---

### 4.4 Target 4: `ism/ism/doctype/repair_order/test_repair_order.py`
In `test_repair_order.py:setUp()`, remove `vin="5YJSA1E26CF000001"` so `TestRepairOrder` uses its own dynamic test VIN and never collides with or claims the static fixture vehicle.

#### Diff Specification for `ism/ism/doctype/repair_order/test_repair_order.py`:
```diff
--- a/ism/ism/doctype/repair_order/test_repair_order.py
+++ b/ism/ism/doctype/repair_order/test_repair_order.py
@@ -21,7 +21,6 @@ class TestRepairOrder(ISMTestCase):
 		self.customer = self.create_test_customer(customer_name=f"{TEST_PREFIX}Repair_Cust")
 		self.vehicle = self.create_test_vehicle(
-			vin="5YJSA1E26CF000001",
 			license_plate="REPAIR-01",
 			customer=self.customer.name,
 		)
```

---

## 5. Verification Method

Once implemented, the remediation can be verified independently with the following commands:

### 5.1 Verification 1: Full Application Test Suite Execution
Execute the entire test suite and verify 0 errors and exit code 0:
```bash
wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
```
*Expected Result*: Exits with code 0. Zero `LinkValidationError`, zero `OperationalError (1020)` on `tabCustomer`.

### 5.2 Verification 2: Sequential 10x Stress Test on `TestSchedulingCapacity`
Run a 10x sequential loop on `TestSchedulingCapacity` to verify zero index tombstones or fixture destruction across runs:
```powershell
powershell -Command "1..10 | ForEach-Object { Write-Host ""=== RUN `$_ ===""; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'; if (`$LASTEXITCODE -ne 0) { Write-Host ""FAILED AT RUN `$_""; exit 1 } }"
```
*Expected Result*: 10/10 runs pass cleanly with exit code 0.

### 5.3 Verification 3: Direct MariaDB Cleanliness & Fixture Integrity Inspection
Verify that static fixtures remain intact while all transient transactions are purged:
```sql
-- Static Fixtures Preserved (Must be 1 each)
SELECT COUNT(*) AS test_customer_count FROM `tabCustomer` WHERE name = 'Test Customer';
SELECT COUNT(*) AS test_vehicle_count FROM `tabRepair Vehicle` WHERE name = '5YJSA1E26CF000001';

-- Residual Test Transactions Cleaned (Must be 0)
SELECT COUNT(*) AS residual_se_count FROM `tabSchedule Entry`;
SELECT COUNT(*) AS residual_ro_count FROM `tabRepair Order` WHERE customer = 'Test Customer' OR customer LIKE '_TEST%';
SELECT COUNT(*) AS residual_temp_custs FROM `tabCustomer` WHERE name LIKE '_TEST%';
```
*Expected Result*:
- `test_customer_count`: 1
- `test_vehicle_count`: 1
- `residual_se_count`: 0
- `residual_ro_count`: 0
- `residual_temp_custs`: 0
