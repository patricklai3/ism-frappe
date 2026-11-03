# Soft Handoff: Project Orchestrator (Generation 2 -> Generation 3)

**Author**: `orchestrator_2` (Generation 2 Project Orchestrator)  
**Date**: 2026-10-26T20:35:00Z  
**Parent / Sentinel**: Conversation ID `60775dab-9c68-4525-b3c1-55e1186e125b`  
**Working Directory**: `d:\ism\.agents\teamwork\orchestrator_3` (to be used by successor)  
**Reason for Succession**: Cumulative subagent spawn count reached 18 / 16 (threshold met) and all 18 subagents have completed.

---

## 1. Milestone State

| # | Milestone Name | Status | Key Artifacts / Notes |
|---|---|:---:|---|
| M0 | Survey & Scope Mapping | **DONE** | `PROJECT.md` at project root with 31 features, architecture, interface contracts, and code layout. |
| M0B | E2E Testing Infrastructure | **DONE** | `TEST_INFRA.md`, `TEST_READY.md`, and 111 test cases across 4 test suites. |
| M1 | Resource & Capacity Models & Settings Consolidation | **FAIL (Iteration 4)** -> Proceeding to **Iteration 5** | Auditor INTEGRITY VIOLATION veto + Reviewers REQUEST_CHANGES. See Section 2 below. |
| M2 | Tunable Estimation Engine & Pure Math Core | PLANNED | Ready to start immediately once M1 passes gate. |
| M3 | Dual-Resource Capacity & Scheduling APIs | PLANNED | Depends on M1 & M2. |
| M4 | Repair Order Integration & Early Appointment Lifecycle | PLANNED | Depends on M1 & M3. |
| M5 | Reusable Scheduling Modal & Frappe Calendar View | PLANNED | Depends on M3 & M4. |
| M6 | Automated Test Suite (100% Pass) & Adversarial Hardening | PLANNED | Final project milestone. |

---

## 2. Milestone 1 Iteration 4 Gate Review & Root Cause Analysis

### Gate Result: FAIL
- **Auditor (`m1_it4_auditor_1`)**: **INTEGRITY VIOLATION (BINARY VETO)** (`d:\ism\.agents\teamwork\m1_it4_auditor_1\handoff.md`)
- **Reviewer 1 (`m1_it4_reviewer_1`)**: **REQUEST_CHANGES** (`d:\ism\.agents\teamwork\m1_it4_reviewer_1\handoff.md`)
- **Reviewer 2 (`m1_it4_reviewer_2`)**: **REQUEST_CHANGES** (`d:\ism\.agents\teamwork\m1_it4_reviewer_2\handoff.md`)
- **Challenger 1 (`m1_it4_challenger_1`)**: **APPROVE** (`d:\ism\.agents\teamwork\m1_it4_challenger_1\handoff.md`)
- **Challenger 2 (`m1_it4_challenger_2`)**: **APPROVE** (`d:\ism\.agents\teamwork\m1_it4_challenger_2\handoff.md`)

### The 2 Remaining Issues to Remediate in Iteration 5:

#### Issue 1: MariaDB 1020 on `tabSingles` in `test_ism_settings.py:setUpClass`
- **Observed**:
  In rapid sequential stress runs (`bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'`), Run 3 (Reviewer 2), Run 4 (Auditor 1), Run 6 (Reviewer 1), and Run 9 (Reviewer 1) failed with:
  `MySQLdb.OperationalError: (1020, "Record has changed since last read in table 'tabSingles'; try restarting transaction")`
  triggered by `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` in `TestISMSettings.setUpClass()`.
- **Root Cause**:
  `tabSingles` has no primary key, only index `(doctype, field)`. `frappe.db.set_single_value` calls `delete("Singles", filters={"field": ("in", tuple(to_update)), "doctype": doctype})`. When tests run in rapid succession, MariaDB InnoDB `REPEATABLE-READ` MVCC index page scan detects unpurged undo log versions on the unkeyed table, raising error 1020.
- **Required Fix**:
  In `TestISMSettings.setUpClass()`, **DO NOT** unconditionally call `set_single_value()`. Instead, compare current settings:
  ```python
  current = frappe.db.get_singles_dict("ISM Settings")
  to_update = {k: v for k, v in CANONICAL_ISM_SETTINGS.items() if str(current.get(k, "")) != str(v)}
  if to_update:
      frappe.db.set_single_value("ISM Settings", to_update)
      frappe.db.commit()
  ```
  Since `setup_default_ism_settings()` already seeded canonical defaults, `to_update` will be empty in 99.9% of runs, resulting in **TRUE ZERO WRITES** and completely eliminating MariaDB 1020 errors on `tabSingles`.

#### Issue 2: Full Application Test Suite Failures (MariaDB 1020 on `tabCustomer` and `tabSeries`, LinkValidationError)
- **Observed**:
  `bench --site development.localhost run-tests --app ism` failed with exit code 1 (`FAILED (errors=6, skipped=249)`).
  Errors:
  1. `test_ro_scheduling_e2e` & `test_estimation_math`: MariaDB 1020 on `tabCustomer` during `setup_all() -> ensure_customer("Test Customer")`.
  2. `test_scheduling_capacity`: `LinkValidationError: Could not find Customer: Test Customer, Vehicle: 5YJSA1E26CF000001`.
  3. `test_tesla_integration.py:374`: MariaDB 1020 on `tabSeries` during `getseries`.
- **Root Cause**:
  `cleanup.py:_cleanup_customers()` deletes any customer starting with `"Test"`. In `base.py`, class-boundary teardown commits customer deletions. When downstream test suites run and call `ensure_customer("Test Customer")`, MariaDB attempts to insert into `tabCustomer` while tombstoned b-tree index entries exist under active transactions, raising error 1020 or leaving the customer missing.
- **Required Fix**:
  1. In `cleanup.py`: Add `"Test Customer"` to `PRESERVED_CUSTOMERS` (or skip any customer whose name is `"Test Customer"`), and add `"5YJSA1E26CF000001"` to preserved vehicles so standard test fixtures are never deleted by sweeps.
  2. In `test_fixtures.py`: Ensure `ensure_customer()` checks existence and rolls back if an uncommitted conflict occurs.
  3. Verify `test_tesla_integration.py` transaction handling so `tabSeries` does not encounter lock contention.

---

## 3. Prescribed Workflow for Generation 3 (`orchestrator_3`)

1. **Working Directory**: Create and operate out of `d:\ism\.agents\teamwork\orchestrator_3`.
2. **Start Heartbeat Cron**: Schedule 10-minute heartbeat task.
3. **Dispatch Iteration 5 Explorers**:
   - `m1_it5_explorer_1`: Formulate zero-churn conditional baseline check for `test_ism_settings.py:setUpClass`.
   - `m1_it5_explorer_2`: Formulate test fixture preservation in `cleanup.py` and `test_fixtures.py` to prevent `Test Customer` 1020 and `LinkValidationError`.
   - `m1_it5_explorer_3`: Formulate `tabSeries` concurrency resolution in `test_tesla_integration.py` and verify full app suite stability.
   **MANDATORY**: Forward the full auditor report path (`d:\ism\.agents\teamwork\m1_it4_auditor_1\handoff.md`) and verbatim findings to the Explorers.
4. **Dispatch Iteration 5 Worker**:
   Apply blueprints, verify:
   - 10x sequential stress loop on `ISM Settings` passes 10/10 with 0 deadlocks.
   - 10x sequential stress loop on `Schedule Entry` passes 10/10 with 0 deadlocks.
   - All 4 M1 suites pass 28/28.
   - Full application test suite (`bench run-tests --app ism`) passes with 0 errors and exit code 0.
5. **Dispatch Gate Iteration 5 Verification**:
   - Reviewers 1 & 2
   - Challengers 1 & 2
   - Forensic Auditor
   On unanimous approval + clean audit, mark Milestone 1 **DONE** in `PROJECT.md` and proceed directly to **Milestone 2**.

---

## 4. Key Artifacts Index

- `d:\ism\PROJECT.md` — Global architecture, feature inventory (31 features), milestones, contracts
- `d:\ism\TEST_INFRA.md` — E2E test harness specification
- `d:\ism\TEST_READY.md` — E2E test readiness report
- `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` — Immutable user specifications
- `d:\ism\.agents\teamwork\m1_it4_auditor_1\handoff.md` — Full Forensic Audit Report (evidence of failures)
- `d:\ism\.agents\teamwork\m1_it4_reviewer_1\handoff.md` — Code Reviewer Report
- `d:\ism\.agents\teamwork\m1_it4_reviewer_2\handoff.md` — Security Reviewer Report
- `d:\ism\.agents\teamwork\m1_it4_challenger_1\handoff.md` — Concurrency Challenger Report
- `d:\ism\.agents\teamwork\m1_it4_challenger_2\handoff.md` — Entity Challenger Report
