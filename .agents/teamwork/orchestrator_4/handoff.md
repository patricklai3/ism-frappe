# Soft Handoff — Project Orchestrator Gen 4 to Gen 5

**From**: `orchestrator_4` (Generation 4 Project Orchestrator)  
**To**: `orchestrator_5` (Generation 5 Project Orchestrator)  
**Date**: 2026-10-27 UTC  
**Handoff Type**: Soft (Succession Triggered at 16 / 16 spawns)  
**Parent / Reporting Recipient**: Sentinel (conversation ID: `60775dab-9c68-4525-b3c1-55e1186e125b`)  
**Workspace Directory**: `d:\ism\.agents\teamwork\orchestrator_5`  

---

## 1. Milestone State

| Milestone | Name | Status | Notes |
|---|---|---|---|
| **M1** | Resource & Capacity Data Models, Settings Consolidation, Test Harness | **IN_PROGRESS (Iteration 7)** | Exploration complete. 3 Explorers formulated unified 5-pillar remediation architecture. Ready for Worker implementation. |
| **M2** | Tunable Estimation Engine & Pure Math Core | PLANNED | Awaiting M1 gate PASS. |
| **M3** | Dual-Resource Capacity & Scheduling APIs | PLANNED | Depends on M1 & M2. |
| **M4** | Repair Order Integration & Early Appointment Lifecycle | PLANNED | Depends on M1 & M3. |
| **M5** | Reusable Client-Side Scheduling Modal & Frappe Calendar View | PLANNED | Depends on M3 & M4. |
| **M6** | Automated Test Suite (100% Pass) & Adversarial Hardening | PLANNED | Final gate. All 111 tests in `TEST_READY.md` must pass. |

---

## 2. Active Subagents

- **Currently running subagents**: None. All 16 subagents spawned in Gen 4 have completed their work products and delivered handoffs.
- **Completed in Gen 4 (16 total)**:
  - M1 Gate Reviewer 1 (`6591f4f4-3122-49ee-ac56-bfe6301b12f4`): completed (APPROVE)
  - M1 Gate Reviewer 2 (`d3cf3297-2688-4d7e-99da-f480d0043b57`): completed (REQUEST_CHANGES)
  - M1 Gate Concurrency Challenger (`4e86d820-f51c-4fb0-bccd-86a98526bdd6`): completed (APPROVE)
  - M1 Gate Forensic Auditor (`903706c5-a2a1-4656-bef9-0573ef8a068a`): completed (INTEGRITY VIOLATION)
  - M1 It6 Explorer 1 (`75ce5e68-5af3-4131-8f0f-9edbae912a73`): completed
  - M1 It6 Explorer 2 (`54de22a8-004f-42fb-89d1-2a77c9547654`): completed
  - M1 It6 Explorer 3 (`78ccce5b-5ea5-4f6c-874a-affd18849ca5`): completed
  - M1 It6 Remediation Worker (`953c8622-1653-440b-b53b-f6da0db78a4f`): completed (DONE)
  - M1 It6 Reviewer 1 (`7caa692c-4857-4186-8396-98856ce94e81`): completed (REQUEST_CHANGES)
  - M1 It6 Reviewer 2 (`05ed2150-b7e3-4516-a7ca-cd52c4b2f64d`): completed (APPROVE)
  - M1 It6 Concurrency Challenger (`ed1d6bc8-71b1-46ab-b4c0-ac4c26880974`): completed (APPROVE)
  - M1 It6 State Bleed Challenger (`a0ef6126-c290-4960-95e9-7cedc2419854`): completed (REQUEST_CHANGES)
  - M1 It6 Forensic Auditor (`514d8848-5779-4ce3-9d8c-0af882c5e0a6`): completed (INTEGRITY VIOLATION)
  - M1 It7 Explorer 1 (`197ea937-3860-431f-9e23-6c16a027b9a2`): completed (Handoff in `m1_it7_explorer_1/handoff.md`)
  - M1 It7 Explorer 2 (`2c0e6624-c564-4ec4-90eb-78fa0bb933dc`): completed (Handoff in `m1_it7_explorer_2/handoff.md`)
  - M1 It7 Explorer 3 (`c4220bbe-60d1-4b29-8292-828f6db93c3d`): completed (Handoff in `m1_it7_explorer_3/handoff.md`)

---

## 3. Pending Decisions & Unified Architecture Synthesis

Explorers 1, 2, and 3 investigated the codebase, container runtime (`devcontainer-frappe-1`, `devcontainer-mariadb-1`), and past audit reports. They achieved 100% consensus on the root cause and remediation:

### Root Cause Logic Chain
1. **The Mid-Test Commit Trap (`frappe.db.commit()`)**:
   - Introduced in Iteration 6 across `base.py`, `test_fixtures.py`, and `test_schedule_entry.py` to prevent entities from being wiped during retry rollbacks.
   - Committing mid-test permanently leaked 66 `Schedule Entry` records and test customers into MariaDB.
   - Committed `tabSeries` and `tabSingles` updates moved row versions forward. Under MariaDB 11.8 (`REPEATABLE READ`, `innodb_snapshot_isolation = 1`), open read snapshots in subsequent tests crashed with MariaDB Error 1020 (`ER_CHECKREAD`) when doing `SELECT ... FOR UPDATE` or `DELETE`.
   - Mid-test commits triggered `enqueue_after_commit=True`, causing external `bench worker` (PID 36) to delete dynamic links asynchronously in the database, creating lock timeouts (1205) and deadlocks (1213).
2. **`ScheduleEntry.autoname` Transaction Rollback Defect**:
   - Calling `frappe.db.rollback()` inside `ScheduleEntry.autoname()` upon catching 1020 wiped out uncommitted parent fixtures (e.g. `Service Bay` in `TestServiceBay`), causing `AssertionError: ValidationError not raised`.
3. **`Repair Order` Series Contention**:
   - `Repair Order` uses `RO-.YYYY.-.#####`. When tests committed mid-suite, `RO-2026-` in `tabSeries` triggered 1020 crashes across 5 tests in `TestRepairOrder`.
4. **`test_ism_settings.py:213` Unguarded Mutation**:
   - `test_setup_idempotency_preserves_customizations` executed `frappe.db.set_single_value` without retry handling, crashing on Run 3 / Run 5 of the 10x stress loop.
5. **Orphaned Vehicles in `cleanup.py`**:
   - `_cleanup_repair_vehicles()` only checked `v.customer in test_customers`. When test customers were deleted first, vehicles with random VINs (`5YJ3E1EBXNF%`) and `license_plate = NULL` were skipped, leaving 10 orphaned vehicles.

### The 5-Pillar Concrete Remediation Architecture

#### Pillar 1: Remove ALL Mid-Test Commits in `base.py` & Test Helpers
- `ism/tests/base.py`:
  - Lines 71, 97, 119, 137, 159: Remove `frappe.db.commit()`.
  - Lines 75, 101, 123, 141, 163: Remove `frappe.db.rollback()` from helper retry loops.
  - Line 241: Remove `frappe.db.commit()` from `cleanup_tracked_docs()`.
- `ism/tests/test_fixtures.py`:
  - Lines 342, 367: Remove `frappe.db.commit()`. Remove `frappe.db.rollback()`.
  - Line 424: In `teardown_transactional()`, remove `frappe.db.commit()`. Use `frappe.db.rollback()`.
- `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
  - Line 16 in `setUp()`: Remove `frappe.db.commit()`.
  - Line 23 & 27 in `_insert_entry()`: Remove `frappe.db.commit()` and `frappe.db.rollback()`.
  - Line 240 in `test_entry_type_requires_repair_order()`: Remove `frappe.db.commit()`.

#### Pillar 2: Clean Autoname Without Inner Rollbacks
- `ism/ism/doctype/schedule_entry/schedule_entry.py`:
  - Remove `frappe.db.rollback()` from `ScheduleEntry.autoname()`.
  - Replace with safe naming series retry or fallback without rollback/commit.
- `ism/ism/doctype/repair_order/repair_order.py`:
  - Implement `autoname(self)` on `RepairOrder` controller with retry/safe naming fallback on Error 1020 without `frappe.db.rollback()`.

#### Pillar 3: Class-Level Fixture Lifecycle
- Ensure `setup_all()` is not called inside per-test `setUp()` if it commits. Run tests as clean atomic transactions that rollback in `tearDown()`.

#### Pillar 4: Guard `test_ism_settings.py:213`
- In `ism/ism/doctype/ism_settings/test_ism_settings.py`:
  - Wrap lines 211–224 in `test_setup_idempotency_preserves_customizations` in a 3-attempt restart loop with atomic dictionary update:
    ```python
    frappe.db.set_single_value("ISM Settings", {
        "default_slot_interval": 45,
        "enable_technician_capacity": 0,
    })
    ```
  - Catch Error 1020 / Deadlock, call `frappe.db.rollback()`, clear cache, and retry.

#### Pillar 5: Harden `cleanup.py` and Purge Orphaned Vehicles
- In `ism/tests/cleanup.py`:
  - Add `frappe.db.rollback()` at start of `cleanup_all_test_data()`.
  - In `_cleanup_repair_vehicles()`: Add pattern match to catch `v.name.startswith("5YJ3E1EBXNF")`, `v.vin.startswith("5YJ3E1EBXNF")`, test customer prefixes, and dangling customer foreign keys (`not frappe.db.exists("Customer", v.customer)`).
  - In `_cleanup_repair_orders()`: Catch dangling customer foreign keys.

---

## 4. Remaining Work & Concrete Next Steps for Gen 5

1. **Workspace Initialization**:
   - Initialize `d:\ism\.agents\teamwork\orchestrator_5\BRIEFING.md` and `progress.md`.
   - Start recurring heartbeat cron via `schedule(CronExpression="*/10 * * * *")`.
2. **Dispatch Worker `m1_it7_worker_1`**:
   - Provide the 5-pillar implementation specification.
   - Attach skill `d:\ism\.agents\skills\frappe-app-dev\SKILL.md`.
   - Include the MANDATORY INTEGRITY WARNING verbatim.
   - Require worker to execute the 4 verification commands:
     - Database cleanup & residual check (0 residual SE, 0 leaked vehicles)
     - 10x `Schedule Entry` stress loop (10/10 PASS)
     - 10x `ISM Settings` stress loop (10/10 PASS)
     - Full application test suite (`bench run-tests --app ism` — 381 tests pass, exit code 0)
3. **Dispatch Gate Verification Team**:
   - 2 Reviewers (`teamwork_preview_reviewer`)
   - 2 Challengers (`teamwork_preview_challenger`)
   - 1 Forensic Auditor (`teamwork_preview_auditor`)
4. **Evaluate Gate Verdicts**:
   - Evaluate Forensic Auditor first: must be `CLEAN`.
   - Reviewers and Challengers must be `APPROVE`.
   - Record in `GATE_STATUS.md`.
5. **Update Milestone 1 to DONE**:
   - Update `PROJECT.md` M1 status to `DONE`.
6. **Advance to Downstream Milestones**:
   - Milestone 2: Estimation Engine & Pure Math Core
   - Milestone 3: Scheduling & Capacity APIs
   - Milestone 4: Repair Order Integration & Appointments
   - Milestone 5: Scheduling Modal & Calendar Views
   - Milestone 6: Full Suite 100% Pass & Tier 5 Adversarial Coverage Hardening
7. **Report Final Completion to Sentinel (`60775dab-9c68-4525-b3c1-55e1186e125b`)**.

---

## 5. Key Artifacts

- `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` — Authoritative requirements
- `d:\ism\PROJECT.md` — Project architecture, features, and milestone tracking
- `d:\ism\TEST_READY.md` — 111-test suite mapping
- `d:\ism\.agents\teamwork\m1_it7_explorer_1\handoff.md` — Explorer 1 4-pillar report
- `d:\ism\.agents\teamwork\m1_it7_explorer_2\handoff.md` — Explorer 2 safe series & purge report
- `d:\ism\.agents\teamwork\m1_it7_explorer_3\handoff.md` — Explorer 3 atomic test isolation architecture
