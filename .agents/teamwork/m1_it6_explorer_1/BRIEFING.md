# BRIEFING — 2026-10-27T00:32:00Z

## Mission
Analyze 4 failure root causes (tabSeries 1020 deadlock, tabSingles 1020 deadlock, leaked TEMP_BAY, LinkValidationError in test_tesla_integration.py) and provide a concrete fix strategy.

## 🔒 My Identity
- Archetype: explorer
- Roles: explorer
- Working directory: d:\ism\.agents\teamwork\m1_it6_explorer_1
- Original parent: 0910fe5c-bb68-4b00-a067-81562d40a7e2
- Milestone: Milestone 1 Iteration 6

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Analyze the 4 failure root causes (tabSeries 1020 deadlock, tabSingles 1020 deadlock, leaked TEMP_BAY, LinkValidationError in test_tesla_integration.py) and provide a concrete fix strategy.
- Do NOT write or modify code. Write handoff report to d:\ism\.agents\teamwork\m1_it6_explorer_1\handoff.md.

## Current Parent
- Conversation ID: 0910fe5c-bb68-4b00-a067-81562d40a7e2
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`
  - `d:\ism\PROJECT.md`
  - `d:\ism\.agents\teamwork\m1_gate_auditor_1\handoff.md`
  - `d:\ism\.agents\teamwork\m1_gate_reviewer_2\handoff.md`
  - `d:\ism\ism\tests\base.py`
  - `d:\ism\ism\tests\test_fixtures.py`
  - `d:\ism\ism\tests\cleanup.py`
  - `d:\ism\ism\tests\test_tesla_integration.py`
  - `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`
  - `d:\ism\ism\ism\doctype\schedule_entry\test_schedule_entry.py`
  - `d:\ism\ism\setup.py`
- **Key findings**:
  - Issue 1 (`tabSeries` 1020): `frappe.model.naming.getseries("SE-", 5)` runs `SELECT current FROM tabSeries WHERE name='SE-' FOR UPDATE`. Under MariaDB 11.8 repeatable read isolation, un-retried inserts in `test_schedule_entry.py` and `test_fixtures.py:create_test_schedule_entry` fail when background workers modify `tabSeries`. A 3-attempt restart loop with `frappe.db.rollback()` is required.
  - Issue 2 (`tabSingles` 1020): `test_fixtures.py:ensure_ism_settings()` defaults `buffer_padding_minutes = 10`, while `setup.py` and `CANONICAL_ISM_SETTINGS` expect `0`. In full suite runs, earlier tests leave settings divergent. `TestISMSettings.setUpClass` detects this divergence and calls `frappe.db.set_single_value()` without a retry loop, failing on MariaDB error 1020. Fix requires aligning `ensure_ism_settings()` default to `0` and wrapping `set_single_value` in `setUpClass` in a 3-attempt retry loop on 1020.
  - Issue 3 (`TEMP_BAY` leakage): `_cleanup_service_bays()` in `ism/tests/cleanup.py` only filters for `_TEST`, `TEST-`, `Test Unit Bay`, `STRESS` or `Test`/`TEST` in name. `TEMP_BAY` and `TEMP_BAY_MULTI` bypass this filter. Fix requires adding `TEMP`, `temp`, `TEMP_` prefixes and `"TEMP" in name` to the filter, plus a one-time purge command.
  - Issue 4 (`LinkValidationError` in `test_tesla_integration.py`): In `base.py`, `create_test_customer()` and `create_test_vehicle()` insert records into the database without committing (`frappe.db.commit()`). When `create_test_repair_order()` encounters a 1020 error on attempt 0 and rolls back, it wipes the uncommitted customer and vehicle. Attempt 1 then fails with `LinkValidationError` because the customer and vehicle no longer exist. Fix requires committing `customer` and `vehicle` immediately upon creation in `base.py`, so rollbacks in subsequent helper calls only roll back the failed child attempt.
- **Unexplored areas**: None. All 4 root causes fully traced and evidenced.

## Key Decisions Made
- Fully documented all 4 issues with exact file paths, line numbers, and before/after code strategies.
- Provided explicit verification commands for the Worker to run after implementation.

## Artifact Index
- `d:\ism\.agents\teamwork\m1_it6_explorer_1\handoff.md` — Final investigation and strategy handoff report
- `d:\ism\.agents\teamwork\m1_it6_explorer_1\DISPATCH.md` — Parent dispatch instructions
- `d:\ism\.agents\teamwork\m1_it6_explorer_1\progress.md` — Liveness progress heartbeat
