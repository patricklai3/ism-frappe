# Milestone 1 Empirical Challenge Report: Resource & Capacity Data Models

**Challenger**: M1 Challenger 1 (`teamwork_preview_challenger`)  
**Date**: 2026-10-26  
**Working Directory**: `d:\ism\.agents\teamwork\m1_challenger_1`  
**Milestone**: Milestone 1 — Resource & Capacity Data Models & Settings Consolidation  
**Target Repository**: `d:\ism`  
**Target Container**: `devcontainer-frappe-1`  

---

## 1. Observation

### 1.1 Reviewed Source Files
The following files authored by M1 Worker 1 were investigated:
1. `ism/ism/doctype/equipment_tag/equipment_tag.py`:
   - Line 10: `validate()` strips `tag_name` and throws `Tag Name cannot be empty.`
   - Line 16: `on_trash()` checks active assignments in `Service Bay Equipment` and prevents deletion.
2. `ism/ism/doctype/service_bay/service_bay.py`:
   - Line 10: `validate()` strips `bay_name` and throws `Bay Name cannot be empty.`
   - Line 17: `validate_duplicate_tags()` checks for duplicate `equipment_tag` across child table rows.
   - Line 31: `on_trash()` blocks deletion if `Schedule Entry` exists in active statuses (`Draft`, `Scheduled`, `Needs Review`, `In Progress`).
3. `ism/ism/doctype/schedule_entry/schedule_entry.py`:
   - Line 41: `set_title()` generates dynamic desk headers.
   - Line 58: `validate()` executes Repair Order requirements, bay availability, and technician capacity checks.
   - Line 64: `populate_from_repair_order()` auto-populates Customer, Vehicle, and Services summary.
   - Line 137: `_check_bay_availability_fallback()` ensures interval overlap protection (`max(start1, start2) < min(end1, end2)`).
4. `ism/ism/doctype/ism_settings/ism_settings.py`:
   - Enforces operating hours order, lunch break window bounds, percentile boundaries (50–99%), and sigma boundaries (0.05–1.00).

### 1.2 Empirical Stress-Test Execution
An empirical test harness was executed directly against bench site `development.localhost` in container `devcontainer-frappe-1`.

Command:
```bash
wsl bash -c "docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python /tmp/test_m1_empirical_runner.py"
```

Verbatim Output:
```text
==================================================
STARTING M1 EMPIRICAL STRESS TESTS
==================================================

--- Running Section 1: Empty and Whitespace Validation ---
[PASS] tag_name_empty_string: Blocked with ValidationError: Tag Name is required
[PASS] tag_name_whitespace_spaces: Blocked with ValidationError: Tag Name is required
[PASS] tag_name_whitespace_tabs_newlines: Blocked with ValidationError: Tag Name is required
[PASS] tag_name_none: Blocked with ValidationError: Tag Name is required
[PASS] bay_name_empty_string: Blocked with ValidationError: Bay Name is required
[PASS] bay_name_whitespace_spaces: Blocked with ValidationError: Bay Name is required
[PASS] bay_name_whitespace_tabs_newlines: Blocked with ValidationError: Bay Name is required
[PASS] bay_name_none: Blocked with ValidationError: Bay Name is required
[PASS] tag_name_stripped: tag.name='Stripped Tag 99', tag_name='Stripped Tag 99'
[PASS] bay_name_stripped: bay.name='Stripped Bay 99', bay_name='Stripped Bay 99'

--- Running Section 2: Duplicate Equipment Tags ---
[PASS] service_bay_unique_tags: Successfully created bay with unique tags
[PASS] service_bay_adjacent_duplicate_tags: Blocked with ValidationError: Duplicate equipment tag 'Lift' at row #2.
[PASS] service_bay_nonadjacent_duplicate_tags: Blocked with ValidationError: Duplicate equipment tag 'Lift' at row #3.
[PASS] service_bay_update_duplicate_tags: Blocked update with ValidationError: Duplicate equipment tag 'Lift' at row #2.

--- Running Section 3.1: Equipment Tag Referential Integrity on Deletion ---
[PASS] tag_deletion_blocked_when_assigned_single_bay: Blocked: Cannot delete Equipment Tag 'STRESS-DEL-TAG' because it is assigned to Service Bay(s): STRESS-DEL-BAY-1. Remove the tag from these bays before deleting.
[PASS] tag_deletion_blocked_when_assigned_multiple_bays: Message reported both bays: Cannot delete Equipment Tag 'STRESS-DEL-TAG' because it is assigned to Service Bay(s): STRESS-DEL-BAY-1, STRESS-DEL-BAY-2. Remove the tag from these bays before deleting.
[PASS] tag_deletion_allowed_when_unassigned: Successfully deleted unassigned tag

--- Running Section 3.2: Service Bay Referential Integrity on Deletion ---
[PASS] bay_deletion_blocked_by_scheduled_entry: Blocked: Cannot delete Service Bay 'STRESS-BAY-DEL-GUARD' because it has active schedule entries (SE-00050). Reassign or cancel these entries before deleting.
[PASS] bay_deletion_blocked_by_inprogress_entry: Blocked: Cannot delete Service Bay 'STRESS-BAY-DEL-GUARD' because it has active schedule entries (SE-00050). Reassign or cancel these entries before deleting.
[PASS] bay_deletion_blocked_by_draft_entry: Blocked: Cannot delete Service Bay 'STRESS-BAY-DEL-GUARD' because it has active schedule entries (SE-00050). Reassign or cancel these entries before deleting.
[PASS] bay_active_entries_filter_excludes_cancelled: Active entries count: 0
[PASS] bay_deletion_allowed_with_no_entries: Successfully deleted bay with no entries

--- Running Section 4: Multiple Schedule Entries on Single Repair Order ---
[PASS] single_ro_multiple_entries_cardinality: Found 3 entries linked to RO RO-2026-00115: ['SE-00052', 'SE-00051', 'SE-00050']
[PASS] parallel_entries_different_bays_same_time_allowed: Successfully booked concurrent entry on different bay for same RO
[PASS] overlapping_entries_same_bay_blocked: Properly blocked bay overlap: ValidationError: Service Bay 'STRESS-BAY-RO-A' is already occupied during the requested time window on 2027-01-04.
[PASS] schedule_entries_auto_populate_from_ro: Customer & vehicle properly populated from RO RO-2026-00115
[PASS] schedule_entries_independent_mutation: SE1 updated without altering SE2 (duration=120)

--- Running Section 5: Entry Type Repair Order Requirement Invariant ---
[PASS] requires_repair_order_enforced: Blocked with MandatoryError: Schedule Entry of type 'Repair' requires a linked Repair Order.
[PASS] optional_repair_order_allowed: Diagnostic type without RO created successfully: SE-00054
==================================================
SUMMARY: Total=29, Passed=29, Failed=0
==================================================

ALL EMPIRICAL CHALLENGES EXECUTED AND PASSED.
```

### 1.3 Standard DocType Test Suite Execution
All 4 DocType test suites in Milestone 1 were re-run independently:

1. `Service Bay`:
   Command: `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"`
   Output:
   ```
   ism.ism.doctype.service_bay.test_service_bay.TestServiceBay
       ✔  test_equipment_tag_deletion_protection  (2.85s)
       ✔  test_service_bay_active_filtering
       ✔  test_service_bay_creation_and_equipment_tags
       ✔  test_service_bay_duplicate_tag_rejection
   Ran 4 tests in 3.407s — OK
   ```

2. `Schedule Entry Type`:
   Command: `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"`
   Output:
   ```
   ism.ism.doctype.schedule_entry_type.test_schedule_entry_type.TestScheduleEntryType
       ✔  test_create_custom_schedule_entry_type
       ✔  test_seed_schedule_entry_types_exist
   Ran 2 tests in 1.282s — OK
   ```

3. `Schedule Entry`:
   Command: `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"`
   Output:
   ```
   ism.ism.doctype.schedule_entry.test_schedule_entry.TestScheduleEntry
       ✔  test_auto_population_from_repair_order
       ✔  test_multiple_schedule_entries_per_repair_order
       ✔  test_provisional_contact_and_vehicle_display
       ✔  test_requires_repair_order_validation
       ✔  test_schedule_entry_naming_series
   Ran 5 tests in 1.813s — OK
   ```

4. `ISM Settings`:
   Command: `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"`
   Output:
   ```
   ism.ism.doctype.ism_settings.test_ism_settings.TestISMSettings
       ✔  test_default_ism_settings_initialized
       ✔  test_setup_idempotency_preserves_customizations
       ✔  test_validate_break_hours_order
       ✔  test_validate_break_within_operating_hours
       ✔  test_validate_estimation_percentile_bounds
       ✔  test_validate_estimation_sigma_bounds
       ✔  test_validate_operating_hours_order
   Ran 7 tests in 0.539s — OK
   ```

---

## 2. Logic Chain

1. **Multiple Schedule Entry Records on a Single Repair Order**:
   - `ORIGINAL_REQUEST.md` mandates that a single customer visit corresponds strictly to one `Repair Order`, while multiple appointments can be booked on that same Repair Order for supplemental work or additional labor time.
   - We observed that `Schedule Entry` permits inserting 3 distinct entries linked to `RO-2026-00115` (`SE-00052`, `SE-00051`, `SE-00050`).
   - We verified that booking parallel sessions on different bays for the same Repair Order is permitted.
   - We verified that booking overlapping sessions on the *same* bay for the same Repair Order is strictly blocked (`ValidationError: Service Bay 'STRESS-BAY-RO-A' is already occupied`).
   - We verified that customer and vehicle data correctly auto-populates from the Repair Order, and modifying duration on one entry leaves sibling appointments completely unchanged.

2. **Duplicate Equipment Tags in Service Bay Equipment**:
   - `service_bay.py` implements `validate_duplicate_tags()` over `self.equipment`.
   - In our empirical tests, adjacent duplicate tags (row 1 `Lift`, row 2 `Lift`) and non-adjacent duplicate tags (row 1 `Lift`, row 2 `Alignment Rack`, row 3 `Lift`) were both blocked with `ValidationError: Duplicate equipment tag 'Lift' at row #X`.
   - Modifying an existing valid bay by appending a duplicate row was also caught and rejected on save.

3. **Referential Integrity on Deleting Equipment Tag and Service Bay**:
   - When `Equipment Tag` is assigned to one or more `Service Bay Equipment` child rows, `frappe.delete_doc("Equipment Tag", ...)` was trapped by `EquipmentTag.on_trash()` and rejected with an explicit error naming all referring bays (`STRESS-DEL-BAY-1, STRESS-DEL-BAY-2`), preventing orphaned foreign keys. Deletion succeeded once unlinked.
   - When `Service Bay` has active appointments (`Scheduled`, `In Progress`, `Draft`), `frappe.delete_doc("Service Bay", ...)` was trapped by `ServiceBay.on_trash()` and rejected (`Cannot delete Service Bay ... because it has active schedule entries`). When appointments were cancelled or deleted, bay deletion completed cleanly.

4. **Empty and Whitespace Bay and Tag Names**:
   - Tests with empty strings `""`, pure spaces `"   "`, tabs/newlines `"\t \n"`, and `None` were all rejected by Frappe validation and mandatory field constraints.
   - Inputs with leading/trailing whitespace (e.g. `"   Stripped Tag 99   "` and `"   Stripped Bay 99   "`) were sanitized such that both `doc.name` and the underlying field (`doc.tag_name`, `doc.bay_name`) were trimmed identically to `"Stripped Tag 99"` and `"Stripped Bay 99"`.

5. **Entry Type Repair Order Requirement**:
   - We verified that `Schedule Entry Type.requires_repair_order` is enforced on `Schedule Entry.validate()`: `Repair` requires a linked Repair Order (`MandatoryError`), while `Diagnostic` allows standalone reservations without a Repair Order.

---

## 3. Caveats

- **No Caveats**: All 4 mandated challenge areas plus edge cases were tested empirically inside `devcontainer-frappe-1`. All 29 stress tests passed and all 18 standard DocType unit tests passed with zero failures.

---

## 4. Conclusion

### Explicit Verdict: **APPROVE**

Milestone 1 data models, controllers, and validation rules are robust, conform to all architectural invariants in `ORIGINAL_REQUEST.md`, and survive adversarial boundary testing:
1. 1:N cardinality between `Repair Order` and `Schedule Entry` functions cleanly without artificial 1:1 constraints.
2. Service Bay duplicate tag validation correctly rejects duplicate rows on insert and update.
3. Referential integrity on deletion of `Equipment Tag` and `Service Bay` prevents orphaned child records and blocked bookings.
4. Empty and whitespace input handling correctly rejects blank names and trims padded names.
5. All Milestone 1 unit tests (18 tests) pass with 100% success.

---

## 5. Verification Method

To independently reproduce and verify this empirical challenge:

1. **Run the Empirical Stress-Test Suite**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python /tmp/test_m1_empirical_runner.py"
   ```
   *Expected Result*: Total=29, Passed=29, Failed=0, `ALL EMPIRICAL CHALLENGES EXECUTED AND PASSED.`

2. **Run DocType Unit Tests**:
   ```bash
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"
   ```
   *Expected Result*: All test suites pass with status `OK`.
