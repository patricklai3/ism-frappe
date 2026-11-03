# BRIEFING — 2026-10-26T20:48:00Z

## Mission
Formulate an exact blueprint for a zero-churn conditional baseline check in `test_ism_settings.py:setUpClass()` to eliminate MariaDB 1020 OperationalError on `tabSingles`.

## 🔒 My Identity
- Archetype: explorer
- Roles: Technical Investigation & Architecture Blueprint
- Working directory: d:\ism\.agents\teamwork\m1_it5_explorer_1
- Original parent: 38fb9e27-6ba2-467d-aae9-b867b39fd0fb
- Milestone: Milestone 1 Iteration 5

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Adhere strictly to DEAD_ENDS.md and Section 1.1 of m1_it4_auditor_1/handoff.md
- Produce handoff.md in d:\ism\.agents\teamwork\m1_it5_explorer_1\handoff.md
- Report back to parent via send_message

## Current Parent
- Conversation ID: 38fb9e27-6ba2-467d-aae9-b867b39fd0fb
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `d:\ism\DEAD_ENDS.md` (Iterations 1-3 post-mortems)
  - `d:\ism\.agents\teamwork\m1_it4_auditor_1\handoff.md` (Section 1.1, Section 2.1 MariaDB 1020 trace on tabSingles)
  - `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py` (lines 1-219)
  - `d:\ism\ism\setup.py` (`setup_default_ism_settings`)
  - Frappe framework database engine (`apps/frappe/frappe/database/database.py:get_singles_dict`, `set_single_value`)
- **Key findings**:
  - `tabSingles` is an unkeyed table with secondary index on `(doctype, field)`.
  - Unconditional `set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` executes bulk `DELETE FROM tabSingles` on every test run. Under MariaDB InnoDB REPEATABLE READ, recent undo log state triggers `HA_ERR_RECORD_CHANGED` (error 1020 / `QueryDeadlockError`).
  - Because `CANONICAL_ISM_SETTINGS` are pre-seeded and tests in `TestISMSettings` discard mutations via `rollback()` without committing, `tabSingles` already matches canonical baseline 100% of the time.
  - Formulated zero-churn conditional baseline check comparing `current_settings` against `CANONICAL_ISM_SETTINGS` with type-safe tolerances. When baseline matches, zero DML and zero commits occur.
- **Unexplored areas**: None (investigation complete).

## Key Decisions Made
- Use `frappe.db.get_singles_dict("ISM Settings")` for direct, lightweight, lock-free read from `tabSingles`.
- Compare each of the 12 canonical fields with type-safe handling (float epsilon `1e-4`, int conversion, string normalization).
- Only call `frappe.db.set_single_value` for `divergent` fields if `divergent` is non-empty.
- Preserve explicit rollback and cache clearing before inspection in `setUpClass()`.
- Created `proposed_test_ism_settings.py` and `test_ism_settings.patch`.

## Artifact Index
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\DISPATCH.md` — Dispatch log
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\BRIEFING.md` — Persistent working memory
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\progress.md` — Progress and heartbeat
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\proposed_test_ism_settings.py` — Complete replacement candidate
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\test_ism_settings.patch` — Verified git diff patch
- `d:\ism\.agents\teamwork\m1_it5_explorer_1\handoff.md` — Final handoff report
