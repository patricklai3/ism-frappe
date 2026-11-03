# BRIEFING — 2026-10-27T01:10:30Z

## Mission
Investigate holistic test suite stability and test transaction isolation without mid-test commits, and propose an integrated architecture for atomic tests and resilient teardown.

## 🔒 My Identity
- Archetype: explorer
- Roles: teamwork_preview_explorer
- Working directory: d:\ism\.agents\teamwork\m1_it7_explorer_3
- Original parent: 0910fe5c-bb68-4b00-a067-81562d40a7e2
- Milestone: Milestone 1 Iteration 7

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Investigate holistic test suite stability and test transaction isolation without mid-test commits
- Propose an integrated architecture for atomic tests and resilient teardown
- Do NOT write code in the project
- Write handoff report to d:\ism\.agents\teamwork\m1_it7_explorer_3\handoff.md
- Notify parent orchestrator via send_message

## Current Parent
- Conversation ID: 0910fe5c-bb68-4b00-a067-81562d40a7e2
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `m1_it6_auditor_1/handoff.md`, `m1_it6_challenger_2/handoff.md`, `m1_it6_reviewer_1/handoff.md`
  - `ism/tests/base.py`, `ism/tests/cleanup.py`, `ism/tests/test_fixtures.py`
  - `ism/tests/test_scheduling_capacity.py`, `ism/tests/test_ro_scheduling_e2e.py`, `ism/tests/test_estimation_math.py`, `ism/tests/test_parts_management.py`
  - `ism/ism/doctype/schedule_entry/schedule_entry.py`, `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
  - `ism/ism/doctype/repair_order/repair_order.py`, `ism/ism/doctype/repair_order/test_repair_order.py`
  - `ism/ism/doctype/service_bay/test_service_bay.py`, `ism/ism/doctype/ism_settings/test_ism_settings.py`
  - Frappe core: `frappe.model.naming`, `frappe.testing.environment`, `frappe.model.delete_doc`, container processes (`honcho`, `bench worker`, `bench schedule`).
- **Key findings**:
  1. Mid-test `frappe.db.commit()` in `base.py`, `test_fixtures.py`, `test_schedule_entry.py`, and `setUpClass` breaks transaction boundaries, leaves 66 residual Schedule Entries and 10 Repair Vehicles in MariaDB, and fires `enqueue_after_commit` jobs (`delete_dynamic_links`) picked up by the external `bench worker`.
  2. MariaDB 11.8 runs with `REPEATABLE READ` and `innodb_snapshot_isolation = 1`. When mid-test commits or background workers commit updates to `tabSeries` or `tabSingles`, subsequent `SELECT ... FOR UPDATE` or `DELETE` statements in open transactions hit MariaDB Error 1020 (`ER_CHECKREAD`: "Record has changed since last read in table ...; try restarting transaction").
  3. `ScheduleEntry.autoname` calling `frappe.db.rollback()` in inner retry loops wipes out uncommitted parent/preceding test fixtures (e.g. `Service Bay` in `TestServiceBay`), causing `AssertionError: ValidationError not raised`.
  4. Helper retry loops calling `frappe.db.rollback()` wiped out uncommitted `Inspection Template` in `TestRepairOrder`, causing `LinkValidationError: Could not find Inspection Template: _TEST_ISM_Intake Checklist`.
  5. `cleanup.py:434-436` misses leaked test vehicles whose customer was already deleted and whose license plate is NULL.
  6. `teardown_transactional()` was running in `tearDown()` of three major test suites and calling `frappe.db.commit()`, actively re-committing after each test!
- **Unexplored areas**: None — full causal chain from root cause to failure symptoms verified.

## Key Decisions Made
- Formulate an integrated 5-pillar architecture:
  1. True atomic tests without mid-test commits (all test data rolls back in `tearDown()`).
  2. Clean sequence naming without inner `frappe.db.rollback()`.
  3. Safe test boundary handling for `ISM Settings` / `tabSingles` divergence.
  4. Resilient teardown with comprehensive vehicle filter and lock release.
  5. Fixture creation order protection in `TestRepairOrder`.

## Artifact Index
- d:\ism\.agents\teamwork\m1_it7_explorer_3\DISPATCH.md — Dispatch log
- d:\ism\.agents\teamwork\m1_it7_explorer_3\progress.md — Liveness heartbeat
- d:\ism\.agents\teamwork\m1_it7_explorer_3\BRIEFING.md — Situational memory
- d:\ism\.agents\teamwork\m1_it7_explorer_3\handoff.md — Final handoff report
