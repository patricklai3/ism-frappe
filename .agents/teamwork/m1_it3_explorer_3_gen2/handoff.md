# Handoff Report: Test Data Cleanup & Schedule Entry Isolation Blueprint

**Agent**: `m1_it3_explorer_3_gen2` (M1 It3 Test Cleanup & Isolation Explorer 3)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2`  
**Target Milestone**: M1 Iteration 3 Remediation  
**Status**: COMPLETE (Hard Handoff)

---

## 1. Observation

### 1.1 Forensic Auditor Finding (§1.2 Failure 3)
In `d:\ism\.agents\teamwork\m1_auditor_2\handoff.md` (§1.2 Failure 3), independent replication of the test suite revealed cross-test contamination in `test_schedule_entry.py`:
- **Command**:
  ```bash
  docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'
  ```
- **Verbatim Failure**:
  ```text
  ======================================================================
   FAIL  test_multiple_schedule_entries_per_repair_order (ism.ism.doctype.schedule_entry.test_schedule_entry.TestScheduleEntry.test_multiple_schedule_entries_per_repair_order)
  Crucial Invariant: Multiple Schedule Entries on the same Repair Order must succeed (1:N cardinality).
  ----------------------------------------------------------------------
  Traceback (most recent call last):
    File "/workspace/development/frappe-bench/apps/ism/ism/ism/doctype/schedule_entry/test_schedule_entry.py", line 68, in test_multiple_schedule_entries_per_repair_order
      self.assertEqual(len(linked_entries), 2)
  AssertionError: 7 != 2
  ```

### 1.2 Inspection of `ism/tests/cleanup.py`
Inspecting `d:\ism\ism\tests\cleanup.py` lines 19–66 reveals that `cleanup_all_test_data()` executed 11 cleanup stages:
1. `_cleanup_payment_entries()` (lines 31–32)
2. `_cleanup_sales_invoices()` (lines 34–35)
3. `_cleanup_sales_orders()` (lines 37–38)
4. `_cleanup_quotations()` (lines 40–41)
5. `_cleanup_repair_orders()` (lines 43–44)
6. `_cleanup_vehicle_checkins()` (lines 46–47)
7. `_cleanup_repair_vehicles()` (lines 49–50)
8. `_cleanup_customers()` (lines 52–53)
9. `_cleanup_service_packages()` (lines 55–56)
10. `_cleanup_items()` (lines 58–59)
11. `_cleanup_files()` (lines 61–62)

**Critical Omission**:
- `cleanup_all_test_data()` contained **ZERO** cleanup routines for `Schedule Entry`, `Service Bay`, or custom `Schedule Entry Type`.
- Running `cleanup_all_test_data()` left all transactional and test `Schedule Entry` records intact in MariaDB.

### 1.3 Direct MariaDB Database Inspection
Direct query of table `tabSchedule Entry` in database `_c6b75eebb8edc84d` confirmed **28 orphaned Schedule Entry records**:
```text
name       entry_type             title                    service_bay        repair_order  customer
SE-00079   Diagnostic             Guest (Diagnostic)       NULL               NULL          NULL
SE-00080   Diagnostic             Guest (Diagnostic)       NULL               NULL          NULL
SE-00089   Diagnostic             Guest (Diagnostic)       TEST-BAY-TRACE     NULL          NULL
SE-00091   TEST-Custom-Type-62    Guest (...)              NULL               NULL          NULL
SE-00092   Diagnostic             Guest (Diagnostic)       TEST-BAY-56        NULL          NULL
SE-00093   Diagnostic             Guest (Diagnostic)       TEST-BAY-56        NULL          NULL
SE-00094   Diagnostic             Guest (Diagnostic)       TEST-BAY-56        NULL          NULL
SE-00103   Diagnostic             Guest (Diagnostic)       NULL               NULL          NULL
SE-00104   Diagnostic             Guest (Diagnostic)       NULL               NULL          NULL
SE-00124   Diagnostic             Guest (Diagnostic)       NULL               NULL          NULL
SE-00216   Diagnostic             Guest (Diagnostic)       STRESS-C3-Bay-Del  NULL          NULL
SE-00217   Diagnostic             Guest (Diagnostic)       STRESS-C3-Bay-Del  NULL          NULL
SE-00218   Diagnostic             Guest (Diagnostic)       STRESS-C3-Bay-Del  NULL          NULL
```

### 1.4 Deletion Guard Blockade in `ScheduleEntryType.on_trash`
When attempting to delete the lingering custom test type `TEST-Custom-Type-62`, Frappe's deletion guard in `ScheduleEntryType.on_trash` threw:
```text
frappe.exceptions.ValidationError: Cannot delete Schedule Entry Type 'TEST-Custom-Type-62' because it is referenced by existing Schedule Entry records (SE-00091). Reassign or delete these entries before deleting the type.
```
This empirically proves that orphaned `Schedule Entry` records directly block cleanup of test entry types and bays.

### 1.5 Inspection of `ism/tests/base.py`
In `d:\ism\ism\tests\base.py`:
- `cleanup_tracked_docs()` (line 102) pops documents from `_tracked_docs`.
- `_teardown_repair_order(self, doc)` (lines 125–134) deletes `Sales Invoice` and `Repair Order`, but does **NOT** delete referencing `Schedule Entry` records.
- `_teardown_customer(self, doc)` (lines 153–158) deletes `Repair Vehicle Customer` child rows and `Customer`, but does **NOT** clean referencing `Schedule Entry` records.

### 1.6 Inspection of `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
In `test_schedule_entry.py`:
- `TestScheduleEntry` (lines 8–14) only implemented `setUp()`, creating `self.customer`, `self.vehicle`, and `self.ro`.
- `TestScheduleEntry` had **no `tearDown()` method** at all.
- If a test aborted or if `Repair Order` naming series numbers were recycled (`RO-2026-00001` or similar), entries previously inserted against that RO accumulated, causing `AssertionError: 7 != 2`.

---

## 2. Logic Chain

1. **Relational Dependency Order**:
   - `Schedule Entry` has Link foreign keys to:
     - `Repair Order` (`repair_order`)
     - `Customer` (`customer`)
     - `Repair Vehicle` (`repair_vehicle`)
     - `Service Bay` (`service_bay`)
     - `Schedule Entry Type` (`entry_type`)
     - `Employee` (`assigned_technician`)
   - Furthermore, `ScheduleEntryType.on_trash` actively inspects `tabSchedule Entry` and **blocks** deletion if any `Schedule Entry` references the type.
   - Similarly, `ServiceBay.on_trash` checks for active `Schedule Entry` appointments.
   - Therefore, in strict relational reverse-dependency order:
     $$\text{Schedule Entry} \prec \{\text{Repair Order}, \text{Service Bay}, \text{Schedule Entry Type}, \text{Repair Vehicle}, \text{Customer}\}$$
     `Schedule Entry` records **MUST be deleted before** parent `Repair Orders`, `Service Bays`, and `Schedule Entry Types`.

2. **Causal Chain of the Test Failure**:
   - When unit tests run repeatedly or when stress testing scripts (`scripts/empirical_stress_data_models.py`) commit test entries to MariaDB, `Schedule Entry` records persist in `tabSchedule Entry`.
   - `cleanup_all_test_data()` deleted `Repair Order` records but left `tabSchedule Entry` untouched.
   - When subsequent tests generated a `Repair Order` with the same identifier, or when tests queried entries with un-cleared fixtures, `frappe.get_all("Schedule Entry", filters={"repair_order": self.ro.name})` returned 7 records instead of the 2 created by `test_multiple_schedule_entries_per_repair_order`.

3. **Three-Tier Defense-in-Depth Architecture**:
   To make test isolation permanent and bulletproof, remediation requires three coordinated layers:
   - **Layer 1: Global Teardown Engine (`ism/tests/cleanup.py`)**:
     Add `_cleanup_schedule_entries(test_customers, log)` to `cleanup_all_test_data()` immediately before `_cleanup_repair_orders()`.
     Also add `_cleanup_service_bays(log)` and `_cleanup_schedule_entry_types(log)` immediately following customer cleanup.
   - **Layer 2: Base Test Controller Hooks (`ism/tests/base.py`)**:
     In `_teardown_repair_order(self, doc)` and `_teardown_customer(self, doc)`, query and delete all referencing `Schedule Entry` records prior to invoking `frappe.delete_doc()`.
   - **Layer 3: Test Suite Encapsulation (`test_schedule_entry.py`)**:
     Implement `self.cleanup_schedule_entries()` in both `setUp()` and `tearDown()` of `TestScheduleEntry`. This guarantees that before each test method starts, and immediately after it finishes, all entries linked to `self.ro`, `self.customer`, or provisional test stubs ("Alice Walker", "Provisional Client", "Guest%") are destroyed.

---

## 3. Caveats

1. **Preserved Master Entities**:
   - `cleanup_all_test_data()` must strictly respect `PRESERVED_CUSTOMERS`, `PRESERVED_REPAIR_ORDERS`, and `PRESERVED_VEHICLES`. Any `Schedule Entry` referencing these preserved entities is skipped.
2. **Baseline Development Service Bays**:
   - Bays `Test Bay 1`, `Test Bay 2`, and `Test Bay 3` are baseline shop fixtures present in the development site. `_cleanup_service_bays()` explicitly preserves these three bays while purging transient test bays (`_TEST%`, `TEST-%`, `Test Unit Bay%`, `STRESS%`).
3. **Protected Standard Entry Types**:
   - Standard seeded types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) are system fixtures defined in `STANDARD_SCHEDULE_ENTRY_TYPES` and must never be deleted by `_cleanup_schedule_entry_types()`.

---

## 4. Conclusion & Delivered Artifacts

Complete blueprints, drop-in replacement files, and a unified git diff patch have been formulated and verified:

### 4.1 Delivered Artifact Files in `.agents/teamwork/m1_it3_explorer_3_gen2/`
| File | Purpose | Description |
|---|---|---|
| `proposed_cleanup.py` | Drop-in replacement for `ism/tests/cleanup.py` | Adds `_cleanup_schedule_entries()`, `_cleanup_service_bays()`, `_cleanup_schedule_entry_types()` in strict reverse dependency order. |
| `proposed_base.py` | Drop-in replacement for `ism/tests/base.py` | Updates `_teardown_repair_order()` and `_teardown_customer()` to delete linked `Schedule Entry` records before parent deletion. |
| `proposed_test_schedule_entry.py` | Drop-in replacement for `test_schedule_entry.py` | Adds `setUp()` and `tearDown()` test isolation hooks purging schedule entries before and after each test. |
| `cleanup_isolation.patch` | Unified Git Patch | Machine-applicable patch for the implementer agent covering all 3 target files. |

### 4.2 Blueprint Summary by Target File

#### 1. `ism/tests/cleanup.py`
- Add `STANDARD_SCHEDULE_ENTRY_TYPES`:
  ```python
  STANDARD_SCHEDULE_ENTRY_TYPES = {
      "Diagnostic",
      "Repair",
      "Maintenance",
      "Meeting",
      "Internal Service",
  }
  ```
- Insert `_cleanup_schedule_entries(test_customers, log)` at Step 5 of `cleanup_all_test_data()` (before `_cleanup_repair_orders`).
- Insert `_cleanup_service_bays(log)` at Step 10 and `_cleanup_schedule_entry_types(log)` at Step 11.
- In `_cleanup_repair_orders()`: delete any lingering `Schedule Entry` records referencing `ro.name` before deleting the Repair Order.
- In `_cleanup_customers()`: delete any lingering `Schedule Entry` records referencing `cust` before deleting the Customer.

#### 2. `ism/tests/base.py`
- In `_teardown_repair_order(self, doc)`:
  ```python
  if frappe.db.exists("DocType", "Schedule Entry"):
      linked_entries = frappe.get_all(
          "Schedule Entry",
          filters={"repair_order": doc.name},
          pluck="name",
      )
      for se_name in linked_entries:
          try:
              frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
          except Exception:
              pass
  ```
- In `_teardown_customer(self, doc)`:
  ```python
  if frappe.db.exists("DocType", "Schedule Entry"):
      linked_entries = frappe.get_all(
          "Schedule Entry",
          filters={"customer": doc.name},
          pluck="name",
      )
      for se_name in linked_entries:
          try:
              frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
          except Exception:
              pass
  ```

#### 3. `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
- Update `setUp()` to invoke `self.cleanup_schedule_entries()`.
- Add `tearDown()` invoking `self.cleanup_schedule_entries()`.
- Implement `cleanup_schedule_entries(self)`:
  - Purges entries linked to `self.ro` (if exists).
  - Purges entries linked to `self.customer` (if exists).
  - Purges entries with provisional names in `("Alice Walker", "Provisional Client")`.
  - Purges entries with titles starting with `_TEST_ISM_` or `Guest%`.

---

## 5. Verification Method

### 5.1 Verification Commands for Independent Reproduction & Validation

1. **Verify Unified Patch Application**:
   ```bash
   git apply --check d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\cleanup_isolation.patch
   ```

2. **Run Schedule Entry Test Suite (5x Multi-Run Stress Test)**:
   ```bash
   docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bash -c "for i in {1..5}; do bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry' || exit 1; done"
   ```
   **Pass Condition**: All 5 runs must report 7/7 tests passed with exit code 0.

3. **Verify Database Cleanliness in MariaDB**:
   ```bash
   docker exec devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d -e "SELECT count(*) FROM \`tabSchedule Entry\` WHERE customer LIKE '_TEST%' OR repair_order LIKE 'RO%';"
   ```
   **Pass Condition**: Result must be `0`.

4. **Verify Type and Bay Cleanliness**:
   ```bash
   docker exec devcontainer-mariadb-1 mariadb -u _c6b75eebb8edc84d -pue178FdAd3MD5UbV _c6b75eebb8edc84d -e "SELECT name FROM \`tabSchedule Entry Type\` WHERE name NOT IN ('Diagnostic', 'Repair', 'Maintenance', 'Meeting', 'Internal Service');"
   ```
   **Pass Condition**: Result must be empty (0 rows).
