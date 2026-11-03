# BRIEFING — 2026-10-26T20:53:00Z

## Mission
Formulate an exact blueprint for eliminating `tabSeries` MariaDB 1020 concurrency issues in `test_tesla_integration.py` and ensuring full application test suite stability under `bench run-tests --app ism`.

## 🔒 My Identity
- Archetype: Explorer
- Roles: Technical Investigation & Architecture Blueprint
- Working directory: d:\ism\.agents\teamwork\m1_it5_explorer_3
- Original parent: 38fb9e27-6ba2-467d-aae9-b867b39fd0fb
- Milestone: Milestone 1 Iteration 5 remediation

## 🔒 Key Constraints
- Read-only investigation — do NOT implement in source code
- Files for content delivery, Messages for coordination
- Self-contained handoff report adhering to the 5-component protocol
- Deliver blueprint in `handoff.md`

## Current Parent
- Conversation ID: 38fb9e27-6ba2-467d-aae9-b867b39fd0fb
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `DEAD_ENDS.md`
  - `m1_it4_auditor_1/handoff.md` (Sections 1.2, 2, 4)
  - `m1_it4_reviewer_2/handoff.md` (Failures 2 & 3, task-96, task-128)
  - `m1_it4_explorer_3/handoff.md`
  - `ism/tests/test_tesla_integration.py`
  - `ism/tests/base.py`
  - `ism/tests/cleanup.py`
  - `ism/tests/test_fixtures.py`
  - `ism/ism/doctype/repair_order/repair_order.py`
  - MariaDB 11.8 variables and processlist
  - `/workspace/development/frappe-bench/logs/worker.log`
- **Key findings**:
  1. MariaDB 11.8 has `innodb_snapshot_isolation = ON` and `REPEATABLE-READ`.
  2. In `base.py:cleanup_tracked_docs()`, calling `frappe.db.exists()` after `frappe.db.rollback()` implicitly starts a new database transaction and establishes a read snapshot. When `committed_docs` is empty, it returns without `rollback()` or `commit()`, leaking this read snapshot to subsequent test methods.
  3. `setUp()` in `base.py` currently does not issue `frappe.db.rollback()`, allowing tests to execute inside lingering stale snapshots.
  4. Concurrent background processes (`bench worker` running `delete_dynamic_links`, scheduler jobs, or mid-test commits) commit database transactions. When a test method inside an outdated snapshot executes `SELECT ... FOR UPDATE` via `frappe.model.naming.getseries()`, MariaDB enforces snapshot isolation and raises `OperationalError: (1020, "Record has changed since last read in table 'tabSeries'; try restarting transaction")`.
  5. In `test_tesla_integration.py`, `test_symmetrical_procedure_import` creates 4 Repair Orders. Its first insert at line 374 fails with 1020 if running within a stale snapshot leaked from `test_import_tesla_procedure_end_to_end`'s teardown.
  6. Empirical reproduction in MariaDB demonstrated that catching 1020, issuing `frappe.db.rollback()`, and retrying `make_autoname()` succeeds immediately and reliably (100% success rate).
  7. In `cleanup.py`, adding `_TEST_Customer`, `Test Customer`, and `5YJSA1E26CF000001` to `PRESERVED_CUSTOMERS` and `PRESERVED_VEHICLES` prevents tombstone generation on `tabCustomer` and eliminates `LinkValidationError`.
- **Unexplored areas**: None. Root causes verified empirically with reproducible test cases.

## Key Decisions Made
- Architecture blueprint formulated across 4 defense layers:
  1. Transaction isolation in `base.py:setUp()` and `base.py:tearDown()` (Phase 3 final rollback).
  2. Transaction restart & retry in `base.py:create_test_repair_order` and creation helpers for MariaDB 1020.
  3. Teardown hygiene in `test_tesla_integration.py`.
  4. Static fixture preservation in `cleanup.py`.

## Artifact Index
- DISPATCH.md — dispatch record
- BRIEFING.md — working memory
- progress.md — liveness heartbeat
- handoff.md — final handoff report
