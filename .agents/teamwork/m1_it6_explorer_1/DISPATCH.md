# Dispatch: Milestone 1 Iteration 6 Explorer 1

**Agent ID**: `m1_it6_explorer_1`  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_explorer_1`  
**Role**: `teamwork_preview_explorer`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  

### Context & Failure Report:
Milestone 1 Gate sign-off failed due to a Forensic Audit INTEGRITY VIOLATION (`m1_gate_auditor_1/handoff.md`) and Reviewer 2 REQUEST_CHANGES (`m1_gate_reviewer_2/handoff.md`).

You MUST read the following authoritative files in full:
- `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`
- `d:\ism\PROJECT.md`
- `d:\ism\.agents\teamwork\m1_gate_auditor_1\handoff.md` (FULL audit evidence report)
- `d:\ism\.agents\teamwork\m1_gate_reviewer_2\handoff.md` (Reviewer 2 report)

### Specific Issues Identified:
1. **`tabSeries` MariaDB 1020 Deadlock on `Schedule Entry`**:
   - In `test_schedule_entry.py:test_provisional_contact_and_vehicle_display` (failed on Run 7 of 10x loop) and in `test_scheduling_capacity.py:test_tier1_f7_schedule_entry_overlap_validation_error` (Task-130).
   - Calling `.insert()` on `Schedule Entry` triggers `getseries()` which runs `SELECT current FROM tabSeries WHERE name='SE-' FOR UPDATE`. Under MariaDB 11.8 snapshot isolation with background worker activity, this throws MariaDB error 1020.
   - Needs 3-attempt restart loop on 1020 with `frappe.db.rollback()` in `test_fixtures.py:create_test_schedule_entry` and in `test_schedule_entry.py`.
2. **`tabSingles` MariaDB 1020 Deadlock in `test_ism_settings.py:setUpClass`**:
   - `test_fixtures.py:ensure_ism_settings()` defaults `buffer_padding_minutes = 10`, while `CANONICAL_ISM_SETTINGS` expects `0`. Preceding tests dirty settings; `TestISMSettings.setUpClass` detects divergence and calls `set_single_value` which throws 1020 without retry.
   - Needs: align `test_fixtures.py:ensure_ism_settings` default to `0`, and wrap `set_single_value` in `test_ism_settings.py:setUpClass` with a 3-attempt retry loop on 1020.
3. **Database Leakage in `tabService Bay`**:
   - Leaked `TEMP_BAY` and `TEMP_BAY_MULTI` survive teardown. `_cleanup_service_bays()` in `ism/tests/cleanup.py` must be updated to clean `TEMP%` and all non-standard test bays.
4. **`LinkValidationError` in `TestTeslaIntegration.test_symmetrical_procedure_import`**:
   - `Could not find Customer: _TEST_ISM_Cust_b5a2ffb1, Vehicle: 5YJ3E1EBXNFB93E83`.

### Mission:
Analyze these 4 issues in the codebase and formulate a precise, complete remediation plan for the Worker.
Do NOT write or modify code yourself. Write your complete analysis and recommended fix strategy to `d:\ism\.agents\teamwork\m1_it6_explorer_1\handoff.md`.
Notify parent orchestrator (`0910fe5c-bb68-4b00-a067-81562d40a7e2`) via send_message when done.
