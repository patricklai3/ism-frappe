# Empirical Adversarial Challenge Report: Milestone 1 Iteration 3

**Agent**: `m1_it3_challenger_2` (Milestone 1 Iteration 3 Security & Data Challenger 2)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it3_challenger_2`  
**Parent Orchestrator**: `0e38d9c2-368a-4e62-8c88-6e3643ab0b5d`  
**Target Bench Site**: `development.localhost`  
**Target Docker Container**: `devcontainer-frappe-1`  
**Empirical Harness**: `d:\ism\scripts\empirical_adversarial_m1_it3.py`  
**Stress Runner**: `d:\ism\scripts\run_adversarial_5x.sh`  
**Final Verdict**: **APPROVE**

---

## Challenge Summary

**Overall risk assessment**: **LOW / MITIGATED**  
**Final Verdict**: **APPROVE**

The adversarial challenges targeted the defense-in-depth mechanisms implemented in Milestone 1 Iteration 3 remediation for:
1. Protection of standard `Schedule Entry Type` records against renaming via `frappe.rename_doc` (with `force=True`, document instance calls, merge calls, and whitespace manipulation).
2. Protection against renaming custom entry types to adopt standard names.
3. Protection against direct mutation of `type_name` via `doc.save()` / `doc.save(ignore_permissions=True)`.
4. Protection against deletion bypass (rename-then-delete sequences, direct deletion, and in-use deletion).
5. Residual test data accumulation and isolation in `Schedule Entry` and `cleanup_all_test_data()`.

Across 5 full sequential adversarial test runs (~200 individual assertions) and all 4 official Milestone 1 unit test suites (28 tests total), **all defenses held with 100% success rate**. Zero bypass vulnerabilities and zero database leaks were found.

---

## 1. Observation

### 1.1 Source Code Under Review

1. **`ism/ism/doctype/schedule_entry_type/schedule_entry_type.py`**:
   - Lines 8–14:
     ```python
     STANDARD_SCHEDULE_ENTRY_TYPES = {
         "Diagnostic",
         "Repair",
         "Maintenance",
         "Meeting",
         "Internal Service",
     }
     ```
   - Lines 24–30 (`validate`):
     ```python
     # Prevent direct renaming via doc.type_name = "new_name"; doc.save()
     if not self.is_new() and self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
         if self.type_name != self.name:
             frappe.throw(
                 _("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name),
                 title=_("Standard Type Protected"),
             )
     ```
   - Lines 32–47 (`before_rename`):
     ```python
     def before_rename(self, old_name, new_name, merge=False):
         target = (old_name or self.name or "").strip()
         if target in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
             frappe.throw(
                 _("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name or target),
                 title=_("Standard Type Protected"),
             )

         new_target = (new_name or "").strip()
         if new_target in STANDARD_SCHEDULE_ENTRY_TYPES:
             frappe.throw(
                 _("Cannot rename to standard Schedule Entry Type '{0}'.").format(new_name),
                 title=_("Standard Type Protected"),
             )

         return new_target
     ```
   - Lines 49–71 (`on_trash`):
     ```python
     def on_trash(self):
         target_name = (self.type_name or self.name or "").strip()
         if target_name in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
             frappe.throw(
                 _("Cannot delete standard Schedule Entry Type '{0}'.").format(self.name or target_name),
                 title=_("Standard Type Protected"),
             )

         if frappe.db.exists("DocType", "Schedule Entry"):
             linked_entries = frappe.get_all(
                 "Schedule Entry",
                 filters={"entry_type": self.name},
                 pluck="name",
                 limit=5,
             )
             if linked_entries:
                 frappe.throw(...)
     ```

2. **`ism/ism/doctype/schedule_entry_type/schedule_entry_type.json`**:
   - Line 3: `"allow_rename": 0`
   - Line 49: `"naming_rule": "By fieldname"`
   - Line 22: `"unique": 1` on `type_name`

3. **`ism/tests/cleanup.py`**:
   - Lines 50–51:
     ```python
     # 5. Clean test Schedule Entries (MUST precede Repair Orders and Service Bays)
     _cleanup_schedule_entries(test_customers, log)
     ```
   - Lines 202–334: `_cleanup_schedule_entries` covers:
     - Preserving `PRESERVED_REPAIR_ORDERS`, `PRESERVED_CUSTOMERS`, `PRESERVED_VEHICLES`
     - Purging entries linked to test Repair Orders (or non-existent Repair Orders)
     - Purging entries linked to test Customers
     - Purging entries linked to test Repair Vehicles
     - Purging entries linked to test Service Bays (`_TEST`, `TEST`, `Test Unit Bay`, `STRESS`)
     - Purging entries with test Entry Types
     - Purging entries with provisional customer/vehicle notes (`Alice Walker`, `Provisional Client`, `TEST-INTAKE`)
     - Purging unlinked standalone entries with no RO and no customer

### 1.2 Verbatim Adversarial Stress Run Results

#### Command: 5x Sequential Stress Loop
```bash
wsl bash /home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/scripts/run_adversarial_5x.sh
```
**Exit Code**: 0  
**Verbatim Output Excerpt**:
```text
=========================================
       ADVERSARIAL STRESS RUN 1 / 5
=========================================
=================================================================
M1 Iteration 3 Adversarial Challenge Suite (Empirical Challenger)
=================================================================

=== Attack 1: Rename Standard Types via frappe.rename_doc with force=True ===
[PASS] Rename 'Diagnostic' with force=True blocked 
[PASS] Rename 'Diagnostic' (doc instance) with force=True blocked 
[PASS] Rename ' Diagnostic ' with force=True blocked 
[PASS] Merge 'Diagnostic' -> 'Repair' with force=True blocked 
[PASS] Rename 'Repair' with force=True blocked 
[PASS] Rename 'Repair' (doc instance) with force=True blocked 
[PASS] Rename ' Repair ' with force=True blocked 
[PASS] Merge 'Repair' -> 'Diagnostic' with force=True blocked 
[PASS] Rename 'Maintenance' with force=True blocked 
[PASS] Rename 'Maintenance' (doc instance) with force=True blocked 
[PASS] Rename ' Maintenance ' with force=True blocked 
[PASS] Merge 'Maintenance' -> 'Repair' with force=True blocked 
[PASS] Rename 'Meeting' with force=True blocked 
[PASS] Rename 'Meeting' (doc instance) with force=True blocked 
[PASS] Rename ' Meeting ' with force=True blocked 
[PASS] Merge 'Meeting' -> 'Repair' with force=True blocked 
[PASS] Rename 'Internal Service' with force=True blocked 
[PASS] Rename 'Internal Service' (doc instance) with force=True blocked 
[PASS] Rename ' Internal Service ' with force=True blocked 
[PASS] Merge 'Internal Service' -> 'Repair' with force=True blocked 

=== Attack 2: Rename Custom Type into Standard Name ===
[PASS] Rename custom to 'Diagnostic' with force=True blocked 
[PASS] Rename custom to '  Diagnostic  ' blocked 
[PASS] Merge custom into standard 'Diagnostic' blocked 
[PASS] Rename custom to 'Repair' with force=True blocked 
[PASS] Rename custom to '  Repair  ' blocked 
[PASS] Merge custom into standard 'Repair' blocked 
[PASS] Rename custom to 'Maintenance' with force=True blocked 
[PASS] Rename custom to '  Maintenance  ' blocked 
[PASS] Merge custom into standard 'Maintenance' blocked 
[PASS] Rename custom to 'Meeting' with force=True blocked 
[PASS] Rename custom to '  Meeting  ' blocked 
[PASS] Merge custom into standard 'Meeting' blocked 
[PASS] Rename custom to 'Internal Service' with force=True blocked 
[PASS] Rename custom to '  Internal Service  ' blocked 
[PASS] Merge custom into standard 'Internal Service' blocked 

=== Attack 3: Direct Mutation of type_name via doc.save() ===
[PASS] doc.save() mutation on 'Diagnostic' blocked 
[PASS] doc.save(ignore_permissions=True) mutation on 'Diagnostic' blocked 
[PASS] doc.save() empty type_name on 'Diagnostic' blocked 
[PASS] doc.save() whitespace type_name on 'Diagnostic' blocked 
[PASS] doc.save() mutation on 'Repair' blocked 
[PASS] doc.save(ignore_permissions=True) mutation on 'Repair' blocked 
[PASS] doc.save() empty type_name on 'Repair' blocked 
[PASS] doc.save() whitespace type_name on 'Repair' blocked 
[PASS] doc.save() mutation on 'Maintenance' blocked 
[PASS] doc.save(ignore_permissions=True) mutation on 'Maintenance' blocked 
[PASS] doc.save() empty type_name on 'Maintenance' blocked 
[PASS] doc.save() whitespace type_name on 'Maintenance' blocked 
[PASS] doc.save() mutation on 'Meeting' blocked 
[PASS] doc.save(ignore_permissions=True) mutation on 'Meeting' blocked 
[PASS] doc.save() empty type_name on 'Meeting' blocked 
[PASS] doc.save() whitespace type_name on 'Meeting' blocked 
[PASS] doc.save() mutation on 'Internal Service' blocked 
[PASS] doc.save(ignore_permissions=True) mutation on 'Internal Service' blocked 
[PASS] doc.save() empty type_name on 'Internal Service' blocked 
[PASS] doc.save() whitespace type_name on 'Internal Service' blocked 

=== Attack 4: Deletion Bypass & Standard Type Deletion Protection ===
[PASS] doc.delete() on standard 'Diagnostic' blocked 
[PASS] frappe.delete_doc(force=True) on standard 'Diagnostic' blocked 
[PASS] Rename-then-delete bypass on 'Diagnostic' blocked 
[PASS] doc.delete() on standard 'Repair' blocked 
[PASS] frappe.delete_doc(force=True) on standard 'Repair' blocked 
[PASS] Rename-then-delete bypass on 'Repair' blocked 
[PASS] doc.delete() on standard 'Maintenance' blocked 
[PASS] frappe.delete_doc(force=True) on standard 'Maintenance' blocked 
[PASS] Rename-then-delete bypass on 'Maintenance' blocked 
[PASS] doc.delete() on standard 'Meeting' blocked 
[PASS] frappe.delete_doc(force=True) on standard 'Meeting' blocked 
[PASS] Rename-then-delete bypass on 'Meeting' blocked 
[PASS] doc.delete() on standard 'Internal Service' blocked 
[PASS] frappe.delete_doc(force=True) on standard 'Internal Service' blocked 
[PASS] Rename-then-delete bypass on 'Internal Service' blocked 
[PASS] In-use custom Schedule Entry Type deletion blocked 

=== Attack 5: Schedule Entry Test Data Isolation & Cleanup Harness ===
Created 7 test Schedule Entries across diverse states: ['SE-00369', 'SE-00370', 'SE-00371', 'SE-00372', 'SE-00373', 'SE-00374', 'SE-00375']
Total Schedule Entry rows in MariaDB before cleanup: 7

Executing cleanup_all_test_data(verbose=False)...
Total Schedule Entry rows in MariaDB after cleanup: count=0, raw_sql=0
[PASS] All test and orphaned Schedule Entry records purged to exactly 0 
[PASS] Associated test entities (bays, custom types, cust, ro) purged 
[PASS] Standard Schedule Entry Types and Shop Bays preserved 
[PASS] cleanup_all_test_data() is idempotent 

=================================================================
Summary of Adversarial Challenge Results:
=================================================================
- Attack 1: Rename Standard Types (force=True): PASSED (Defense Held)
- Attack 2: Rename Custom Type to Standard: PASSED (Defense Held)
- Attack 3: Direct Mutation via doc.save(): PASSED (Defense Held)
- Attack 4: Deletion Bypass: PASSED (Defense Held)
- Attack 5: Schedule Entry Test Data Isolation: PASSED (Defense Held)
=================================================================
FINAL ADVERSARIAL VERDICT: APPROVE
=================================================================

ALL 5 ADVERSARIAL STRESS RUNS COMPLETED SUCCESSFULLY WITH 0 FAILURES!
```

### 1.3 Verbatim Official Unit Test Suite Executions

1. **Schedule Entry Type** (8 tests):
   ```text
   ism.ism.doctype.schedule_entry_type.test_schedule_entry_type.TestScheduleEntryType
       ✔  test_create_custom_schedule_entry_type
       ✔  test_delete_in_use_schedule_entry_type_blocked
       ✔  test_delete_standard_schedule_entry_type_blocked
       ✔  test_modify_standard_type_name_on_save_blocked
       ✔  test_rename_bypass_deletion_prevented
       ✔  test_rename_standard_schedule_entry_type_blocked
       ✔  test_rename_to_standard_schedule_entry_type_blocked
       ✔  test_seed_schedule_entry_types_exist
   Ran 8 tests in 1.963s -> OK
   ```

2. **Schedule Entry** (7 tests):
   ```text
   ism.ism.doctype.schedule_entry.test_schedule_entry.TestScheduleEntry
       ✔  test_auto_population_from_repair_order
       ✔  test_estimated_duration_must_be_positive
       ✔  test_multiple_schedule_entries_per_repair_order
       ✔  test_populate_from_repair_order_on_update
       ✔  test_provisional_contact_and_vehicle_display
       ✔  test_requires_repair_order_validation
       ✔  test_schedule_entry_naming_series
   Ran 7 tests in 2.055s -> OK
   ```

3. **Service Bay** (5 tests):
   ```text
   ism.ism.doctype.service_bay.test_service_bay.TestServiceBay
       ✔  test_equipment_tag_deletion_protection
       ✔  test_service_bay_active_filtering
       ✔  test_service_bay_creation_and_equipment_tags
       ✔  test_service_bay_deletion_blocked_with_active_schedule_entry
       ✔  test_service_bay_duplicate_tag_rejection
   Ran 5 tests in 1.985s -> OK
   ```

4. **ISM Settings** (8 tests):
   ```text
   ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
       ✔  test_default_ism_settings_initialized
       ✔  test_ism_manager_permissions
       ✔  test_setup_idempotency_preserves_customizations
       ✔  test_validate_break_hours_order
       ✔  test_validate_break_within_operating_hours
       ✔  test_validate_estimation_percentile_bounds
       ✔  test_validate_estimation_sigma_bounds
       ✔  test_validate_operating_hours_order
   Ran 8 tests in 2.107s -> OK
   ```

### 1.4 Post-Test Cleanliness Attestation

Verbatim database inspection output:
```text
=== Post-Test Database State Verification ===
Total Schedule Entry count: 0
Schedule Entries: []
Schedule Entry Type count: 5
Schedule Entry Types: ['Diagnostic', 'Repair', 'Maintenance', 'Meeting', 'Internal Service']
Service Bay count: 3
Service Bays: ['Test Bay 1', 'Test Bay 2', 'Test Bay 3']
Test Customers: []
Test Repair Orders: []
```

---

## 2. Logic Chain

1. **Standard Entry Type Rename Protection (Attack 1 & Attack 2)**:
   - In Frappe, `frappe.rename_doc` invokes `before_rename(self, old_name, new_name, merge=False)`.
   - In `ScheduleEntryType`, `target = (old_name or self.name or "").strip()`. If `target in STANDARD_SCHEDULE_ENTRY_TYPES`, `frappe.throw(ValidationError)` is raised unconditionally, even if `force=True` or `merge=True` is supplied.
   - When renaming a custom type to adopt a standard name, `new_target = (new_name or "").strip()` checks `new_target in STANDARD_SCHEDULE_ENTRY_TYPES` and throws `ValidationError`. Additionally, the MariaDB unique constraint on `tabSchedule Entry Type.name` (primary key) prevents collisions regardless of casing.
   - Tested empirically across all 5 standard types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`), doc instance invocations, padded whitespace, and merge operations. All 20 rename attempts were blocked, and database integrity was preserved.

2. **Save-Time Property Mutation Protection (Attack 3)**:
   - In Frappe DocTypes with `autoname = "field:type_name"`, updating `doc.type_name = "NewName"` on an existing document before calling `doc.save()` triggers controller validation.
   - In `ScheduleEntryType.validate()`, lines 25–30 verify `if not self.is_new() and self.name in STANDARD_SCHEDULE_ENTRY_TYPES: if self.type_name != self.name: frappe.throw(...)`.
   - Tested empirically with modified strings, empty strings, and whitespace-only strings, with and without `ignore_permissions=True`. All 20 mutation attempts raised `frappe.ValidationError` and left database fields untouched.

3. **Deletion Protection & Bypass Prevention (Attack 4)**:
   - `on_trash()` in `ScheduleEntryType` blocks deletion if `target_name in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES`.
   - Rename-then-delete bypass is impossible because the renaming step is blocked by `before_rename`. Even if a script bypassed `before_rename` programmatically, `on_trash()` inspects both `self.type_name` and `self.name`.
   - Furthermore, custom types cannot be deleted while referenced by active `Schedule Entry` records (`linked_entries = frappe.get_all("Schedule Entry", filters={"entry_type": self.name})`), preventing referential integrity failure.
   - All deletion attempts on standard types (`doc.delete()`, `delete_doc(force=True)`) and in-use custom types were rejected.

4. **Residual Test Data Isolation & Cleanup (Attack 5)**:
   - `cleanup_all_test_data()` in `ism/tests/cleanup.py` deletes `Schedule Entry` records before parent `Repair Order` and `Service Bay` entities, strictly adhering to reverse FK dependency order.
   - `_cleanup_schedule_entries()` filters comprehensively by test prefixes, orphan references, provisional intake data (`Alice Walker`, `Provisional Client`), and unlinked standalone entries, while explicitly preserving baseline entities (`PRESERVED_REPAIR_ORDERS`, `PRESERVED_CUSTOMERS`, `PRESERVED_VEHICLES`).
   - Stress-tested with 7 distinct uncommitted/aborted fixture states (including orphaned records with dangling FKs). After `cleanup_all_test_data()`, both ORM count (`frappe.db.count`) and raw MariaDB count (`SELECT count(*) FROM \`tabSchedule Entry\``) returned exactly `0`.
   - Repeating `cleanup_all_test_data()` verified complete idempotency without errors.

5. **Deterministic Multi-Run Stability**:
   - Running `run_adversarial_5x.sh` across 5 consecutive iterations produced 0 failures, 0 deadlocks, and 0 database state leaks.
   - All 4 standard Milestone 1 test suites (28 tests total) passed with 100% success rate post-challenge.

---

## 3. Caveats

No caveats. All 5 adversarial attack dimensions specified in the dispatch plan were executed directly against the live MariaDB Docker container. Verification was empirical and exhaustive.

---

## 4. Conclusion

The standard Schedule Entry Type rename defenses, deletion protections, and Schedule Entry residual test data cleanup routines are structurally sound, resilient against adversarial circumvention, and fully production-ready.

**Verdict**: **APPROVE**

---

## 5. Verification Method

To independently reproduce this verification:

1. **Execute 5x Adversarial Stress Harness**:
   ```bash
   wsl bash /home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/scripts/run_adversarial_5x.sh
   ```
   *Expected Output*: `ALL 5 ADVERSARIAL STRESS RUNS COMPLETED SUCCESSFULLY WITH 0 FAILURES!` (Exit code 0).

2. **Execute Full Milestone 1 Test Suites**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"
   ```
   *Expected Output*: All 4 suites report `OK` (28/28 tests passed).

3. **Inspect MariaDB Cleanliness**:
   ```bash
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c "
   import frappe
   frappe.init(site='development.localhost')
   frappe.connect()
   assert frappe.db.count('Schedule Entry') == 0, 'Residual Schedule Entry records found!'
   assert frappe.db.count('Schedule Entry Type') == 5, 'Standard Schedule Entry Types missing!'
   print('Database Cleanliness Verified: 0 residual entries.')
   "
   ```
   *Expected Output*: `Database Cleanliness Verified: 0 residual entries.`
