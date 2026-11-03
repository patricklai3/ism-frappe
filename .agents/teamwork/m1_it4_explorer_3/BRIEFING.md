# BRIEFING — 2026-10-26T19:50:30Z

## Mission
Analyze ISMTestCase.tearDown() in ism/tests/base.py and cleanup.py to eliminate full 14-table sweeps with mid-suite commits between every single test method while preserving clean tracked-doc cleanup.

## 🔒 My Identity
- Archetype: explorer
- Roles: Milestone 1 Iteration 4 Base Test Hygiene Specialist
- Working directory: d:\ism\.agents\teamwork\m1_it4_explorer_3
- Original parent: 0e38d9c2-368a-4e62-8c88-6e3643ab0b5d
- Milestone: Milestone 1 Iteration 4

## 🔒 Key Constraints
- Read-only investigation — do NOT modify source code directly
- Deliver blueprint, proposed replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it4_explorer_3\handoff.md`
- Write only to own folder (`d:\ism\.agents\teamwork\m1_it4_explorer_3`)

## Current Parent
- Conversation ID: 0e38d9c2-368a-4e62-8c88-6e3643ab0b5d
- Updated: not yet

## Investigation State
- **Explored paths**: `ism/tests/base.py`, `ism/tests/cleanup.py`, `ism/tests/test_fixtures.py`, `ism/ism/doctype/schedule_entry/test_schedule_entry.py`, `ism/ism/doctype/ism_settings/test_ism_settings.py`, `ism/tests/test_scheduling_capacity.py`, `m1_it3_reviewer_1/handoff.md`.
- **Key findings**:
  1. Root cause of MariaDB Error 1020 and 1213: In MariaDB 11.8.8, `innodb_snapshot_isolation = ON` is enabled by default under REPEATABLE READ. `cleanup_all_test_data` running across 14 tables in `setUp` and `tearDown` of every single test method triggers hundreds of mid-suite `frappe.db.commit()` calls. When subsequent tests execute `SELECT current FROM tabSeries WHERE name=%s FOR UPDATE`, MariaDB detects row modification since last read, throwing 1020 or 1213.
  2. Full 14-table sweeps in `setUp()` and `tearDown()` turn an O(1) per-test teardown into O(tables * rows) scans, running 254 sweeps (3,556 table queries) per full test run and wiping out shared master fixtures like `Test Customer`.
  3. Two-phase teardown protocol: Phase 1 issues `frappe.db.rollback()` first, which instantly reverts all uncommitted test inserts and releases `tabSeries` and `tabSingles` locks in <1ms without any SQL writes or commits. Phase 2 checks surviving docs in `_tracked_docs` (only present if explicitly committed mid-test) and purges only those surviving records.
  4. Class-level boundary teardown (`setUpClass` and `tearDownClass`) preserves full residual sweeps before and after the test class while keeping individual test methods completely isolated from table sweeps.
  5. Empirical testing proved that 10x sequential runs of `TestScheduleEntry` (70 tests) completed cleanly in 3.75s with 0 deadlocks, and the full app test suite dropped from 64.39s to 27.95s (56.6% speedup).
- **Unexplored areas**: None. Investigation complete.

## Key Decisions Made
- Designed Two-Phase Teardown protocol in `ISMTestCase.tearDown()` (Phase 1: Rollback uncommitted transactions; Phase 2: Surgical teardown of surviving committed docs).
- Moved `cleanup_residual_test_data()` out of per-test `setUp`/`tearDown` into class boundary hooks `setUpClass()` and `tearDownClass()`.
- Created standalone proposed replacement file `proposed_base.py` and unified git diff patch `base_teardown_hygiene.patch`.

## Artifact Index
- `d:\ism\.agents\teamwork\m1_it4_explorer_3\BRIEFING.md` — Situational awareness working memory
- `d:\ism\.agents\teamwork\m1_it4_explorer_3\progress.md` — Liveness heartbeat and milestone tracking
- `d:\ism\.agents\teamwork\m1_it4_explorer_3\proposed_base.py` — Complete drop-in replacement for `ism/tests/base.py`
- `d:\ism\.agents\teamwork\m1_it4_explorer_3\base_teardown_hygiene.patch` — Unified diff patch for `ism/tests/base.py`
- `d:\ism\.agents\teamwork\m1_it4_explorer_3\handoff.md` — Final deliverable report with 5-component structure

