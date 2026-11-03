# Dispatch: M1 Iteration 4 Explorer 1 (test_ism_settings.py setUpClass & Zero-Churn Isolation)

## Mission
Formulate a fundamentally different, zero-churn test fixture architecture for `test_ism_settings.py` to completely eliminate the 16x per run `set_single_value` bulk `DELETE` + `INSERT` churn on `tabSingles`.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. `d:\ism\DEAD_ENDS.md` (MUST read Iterations 1, 2, and 3 entries to avoid repeated dead ends)
4. Challenger 1 Handoff: `d:\ism\.agents\teamwork\m1_it3_challenger_1\handoff.md`
5. Reviewer 1 & 2 Handoffs:
   `d:\ism\.agents\teamwork\m1_it3_reviewer_1\handoff.md`
   `d:\ism\.agents\teamwork\m1_it3_reviewer_2\handoff.md`

## Problem & Fix Direction
- Iteration 3 failed because `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` was called in both `setUp()` and `tearDown()` of all 8 test methods.
- In Frappe, `set_single_value(dict)` executes bulk `DELETE FROM tabSingles` followed by multi-row `INSERT`. In an 8-test suite, this runs 16 times per test execution! On keyless `tabSingles`, this causes intermittent MariaDB 1020 lock errors in 10x stress loops.
- Formulate a clean solution:
  1. Move canonical settings establishment to `setUpClass()` (runs ONCE per test class, not 16 times per test run).
  2. In `setUp()` and `tearDown()`, do NOT delete/insert rows on `tabSingles`. Simply execute `frappe.db.rollback()` to ensure clean transaction state.
  3. All validation tests (`test_validate_*`) are already pure in-memory tests on `frappe.new_doc("ISM Settings")` and require NO database writes.
  4. For `test_setup_idempotency_preserves_customizations`, mutate settings within the test and restore in a `try...finally` block with `frappe.db.rollback()`.
  5. Ensure 10 consecutive executions of `bench run-tests --app ism --doctype 'ISM Settings'` pass 10/10 with 0 errors.

## Deliverable
Deliver your blueprint, proposed replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it4_explorer_1\handoff.md` and message orchestrator when complete.


## 2026-10-26T19:49:56Z
You are m1_it4_explorer_1 (Milestone 1 Iteration 4 Concurrency & setUpClass Specialist).
Your working directory is: d:\ism\.agents\teamwork\m1_it4_explorer_1
Read your dispatch instructions at: d:\ism\.agents\teamwork\m1_it4_explorer_1\DISPATCH.md
Read authoritative requirements at: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Read project plan at: d:\ism\PROJECT.md
Read DEAD_ENDS.md at: d:\ism\DEAD_ENDS.md
Read reports:
- d:\ism\.agents\teamwork\m1_it3_challenger_1\handoff.md
- d:\ism\.agents\teamwork\m1_it3_reviewer_1\handoff.md
- d:\ism\.agents\teamwork\m1_it3_reviewer_2\handoff.md

Your mission:
Formulate a zero-churn test fixture architecture for test_ism_settings.py:
1. Move canonical settings establishment to setUpClass() so it runs ONCE per test class, not 16 times per run.
2. In setUp() and tearDown(), do NOT delete/insert rows on tabSingles. Only use frappe.db.rollback().
3. In test_setup_idempotency_preserves_customizations, mutate and cleanly restore within a try...finally block.
4. Empirically verify across 10 consecutive test runs in devcontainer.
Deliver blueprint, replacement code, and diff patch in d:\ism\.agents\teamwork\m1_it4_explorer_1\handoff.md and report back via send_message.
