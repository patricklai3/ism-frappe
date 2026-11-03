# Dispatch: M1 Iteration 3 Explorer 1 (tabSingles Concurrency & Test Isolation Overhaul)

## Mission
Formulate a fundamentally different, bulletproof strategy to eliminate MariaDB OperationalError (1020) on `tabSingles` in `test_ism_settings.py` without silent exception swallowing or dirty state leakage.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. `d:\ism\DEAD_ENDS.md` (MUST read to avoid repeating failed approaches from Iterations 1 & 2)
4. Full Forensic Auditor Report (DO NOT OMIT OR FILTER):
   `d:\ism\.agents\teamwork\m1_auditor_2\handoff.md`
5. Reviewer & Challenger Reports:
   `d:\ism\.agents\teamwork\m1_reviewer_3\handoff.md`
   `d:\ism\.agents\teamwork\m1_reviewer_4\handoff.md`
   `d:\ism\.agents\teamwork\m1_challenger_4\handoff.md`

## Oscillation Guard & Constraints
- The MariaDB 1020 error (`Record has changed since last read in table 'tabSingles'; try restarting transaction`) has failed the gate for 2 consecutive iterations.
- DO NOT repeat the failed strategies in `DEAD_ENDS.md`:
  - Do NOT leave uncommitted `SELECT ... FOR UPDATE` row locks open across test boundaries.
  - Do NOT call `frappe.db.commit()` inside individual test methods (`test_validate_estimation_percentile_bounds`, `test_setup_idempotency_preserves_customizations`).
  - Do NOT swallow exceptions in `tearDown()` via `try ... except Exception: rollback`. Teardown must fail loudly if it fails.
- Formulate a fundamentally clean test architecture for `TestISMSettings`:
  - Investigate using pure transaction rollbacks (`frappe.db.rollback()`) around every test mutation.
  - Or investigate direct in-memory controller validation without database `save()` where database persistence is not required.
  - Or investigate direct `frappe.db.set_single_value()` or deterministic baseline restoration of every individual setting attribute.
  - Ensure 5 consecutive runs of `bench run-tests --app ism --doctype 'ISM Settings'` succeed cleanly from a fresh shell with exit code 0.

## Output Requirements
Deliver a concrete, line-by-line blueprint, replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it3_explorer_1\handoff.md` and message the orchestrator when complete.

## 2026-10-26T16:27:01Z
You are m1_it3_explorer_1 (M1 It3 Concurrency & Settings Explorer 1). Your working directory is d:\ism\.agents\teamwork\m1_it3_explorer_1.
Read your dispatch instructions at d:\ism\.agents\teamwork\m1_it3_explorer_1\DISPATCH.md and the authoritative requirements at d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md.
Read d:\ism\DEAD_ENDS.md to avoid previously failed approaches.
Read the full forensic auditor report at d:\ism\.agents\teamwork\m1_auditor_2\handoff.md.
Formulate a fundamentally different, bulletproof strategy to eliminate MariaDB 1020 on tabSingles in test_ism_settings.py without silent exception swallowing in tearDown or dirty state leakage.
Deliver your blueprint, proposed replacement file, and unified diff patch in d:\ism\.agents\teamwork\m1_it3_explorer_1\handoff.md and send_message to orchestrator when complete.
