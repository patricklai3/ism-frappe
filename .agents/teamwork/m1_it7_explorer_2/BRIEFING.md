# BRIEFING — 2026-10-27T01:13:00Z

## Mission
Investigate transaction isolation, safe naming series mechanics without raw commits/rollbacks, Repair Order series deadlocks, and vehicle purge patterns in cleanup.py for Milestone 1 Iteration 7.

## 🔒 My Identity
- Archetype: explorer
- Roles: teamwork_preview_explorer
- Working directory: d:\ism\.agents\teamwork\m1_it7_explorer_2
- Original parent: 0910fe5c-bb68-4b00-a067-81562d40a7e2
- Milestone: Milestone 1 Iteration 7

## 🔒 Key Constraints
- Read-only investigation — do NOT implement / write code
- Write handoff report to d:\ism\.agents\teamwork\m1_it7_explorer_2\handoff.md
- Focus on transaction isolation, naming series mechanics, MariaDB 1020 deadlock resilience, Repair Order tabSeries contention, test_ism_settings.py:213 retry handling, and cleanup.py vehicle purge patterns.

## Current Parent
- Conversation ID: 0910fe5c-bb68-4b00-a067-81562d40a7e2
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `ORIGINAL_REQUEST.md`, `PROJECT.md`
  - `m1_it6_auditor_1/handoff.md`, `m1_it6_challenger_2/handoff.md`, `m1_it6_reviewer_1/handoff.md`
  - `ism/tests/base.py`, `ism/tests/test_fixtures.py`, `ism/tests/cleanup.py`
  - `ism/ism/doctype/schedule_entry/schedule_entry.py`, `test_schedule_entry.py`
  - `ism/ism/doctype/repair_order/repair_order.py`, `repair_order.json`, `test_repair_order.py`
  - `ism/ism/doctype/ism_settings/test_ism_settings.py`
  - Frappe framework internals (`naming.py`, `document.py`, `database.py`)
- **Key findings**:
  1. `frappe.db.commit()` in test helpers (`base.py`, `test_fixtures.py`, `test_schedule_entry.py`) broke transaction isolation, leaked records, and caused MariaDB 1020 conflicts across tests.
  2. `frappe.db.rollback()` in `ScheduleEntry.autoname()` destroyed uncommitted caller fixtures (`ServiceBay`, `Customer`, `Vehicle`).
  3. Safe series naming without `rollback` or `commit` is achieved via `_generate_safe_series_name` fallback (using clean `frappe.get_all` on the doctype table without `FOR UPDATE`).
  4. Contention on `RO-2026-` in `TestRepairOrder` occurred because `RepairOrder` lacked custom `autoname` handling and earlier tests committed `RO-2026-` updates.
  5. `test_setup_idempotency_preserves_customizations` in `test_ism_settings.py:213` needs a 3-attempt restart loop with cache invalidation on `frappe.db.set_single_value`.
  6. `cleanup.py:_cleanup_repair_vehicles()` skips test vehicles when linked customers are already deleted and plate is NULL; matching `5YJ3E1EBXNF%` and VIN prefixes resolves 100% of leaked vehicles.
- **Unexplored areas**: None. All 4 dispatch questions answered with empirical proof.

## Key Decisions Made
- Formulated complete, drop-in replacement designs and snippets for all 4 focus areas for the implementer worker.

## Artifact Index
- DISPATCH.md — Task assignment and instructions
- BRIEFING.md — Working memory index
- progress.md — Liveness heartbeat
- handoff.md — Final investigation report
