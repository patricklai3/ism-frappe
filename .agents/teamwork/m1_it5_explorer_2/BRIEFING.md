# BRIEFING — 2026-10-26T20:51:00Z

## Mission
Formulate an exact blueprint for preserving static test fixtures in `cleanup.py` and `test_fixtures.py` (specifically `Test Customer` and `5YJSA1E26CF000001`) to eliminate MariaDB 1020 tombstone collisions and `LinkValidationError`.

## 🔒 My Identity
- Archetype: explorer
- Roles: Technical Investigation & Architecture Blueprint
- Working directory: d:\ism\.agents\teamwork\m1_it5_explorer_2
- Original parent: 38fb9e27-6ba2-467d-aae9-b867b39fd0fb
- Milestone: Milestone 1 Iteration 5 Remediation

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Explore cleanup.py, test_fixtures.py, base.py, and test_scheduling_capacity.py
- Use CodeGraph when navigating code symbols
- Write handoff report in d:\ism\.agents\teamwork\m1_it5_explorer_2\handoff.md
- Check DEAD_ENDS.md and Section 1.2 of m1_it4_auditor_1/handoff.md

## Current Parent
- Conversation ID: 38fb9e27-6ba2-467d-aae9-b867b39fd0fb
- Updated: 2026-10-26T20:51:00Z

## Investigation State
- **Explored paths**: `m1_it4_auditor_1/handoff.md`, `DEAD_ENDS.md`, `cleanup.py`, `test_fixtures.py`, `base.py`, `test_scheduling_capacity.py`, `test_repair_order.py`, `test_repair_vehicle.py`, `test_ro_scheduling_e2e.py`, `test_estimation_math.py`, `test_scheduling.py`, `test_technician_availability.py`, `test_estimation.py`, `test_repair_order_scheduling.py`
- **Key findings**:
  1. `TestSchedulingCapacity`, `TestEstimationMath`, and `TestRepairOrderSchedulingE2E` are discovered and executed multiple times due to alias files (`test_scheduling.py`, `test_technician_availability.py`, `test_estimation.py`, `test_repair_order_scheduling.py`).
  2. Between executions, `ISMTestCase.setUpClass()` and `tearDownClass()` call `cleanup_all_test_data()`, which deletes `"Test Customer"` and `"5YJSA1E26CF000001"`.
  3. Deleting and rapidly re-inserting primary key `"Test Customer"` across test boundaries triggers MariaDB 11 InnoDB index tombstone version conflicts (Error 1020 `HA_ERR_RECORD_CHANGED`).
  4. `TestRepairOrder.setUp()` was also hardcoded with `vin="5YJSA1E26CF000001"`, deleting and mutating the static vehicle fixture on each test method.
  5. Separating `STATIC_FIXTURE_CUSTOMERS` / `STATIC_FIXTURE_VEHICLES` from `SEED_PRESERVED_CUSTOMERS` / `SEED_PRESERVED_VEHICLES` in `cleanup.py` preserves master records while ensuring all test transactions referencing them are cleanly purged.
- **Unexplored areas**: None within the assigned scope.

## Key Decisions Made
- Architected exact diff specifications across `cleanup.py`, `test_fixtures.py`, `base.py`, and `test_repair_order.py`.
- Verified that post-test MariaDB cleanliness (0 residual `tabSchedule Entry`, 0 residual `tabRepair Order`) is fully preserved.

## Artifact Index
- `d:\ism\.agents\teamwork\m1_it5_explorer_2\DISPATCH.md` — Incoming task instructions & dispatch log
- `d:\ism\.agents\teamwork\m1_it5_explorer_2\progress.md` — Liveness heartbeat
- `d:\ism\.agents\teamwork\m1_it5_explorer_2\handoff.md` — Complete investigation, logic chain, and code modification blueprint
