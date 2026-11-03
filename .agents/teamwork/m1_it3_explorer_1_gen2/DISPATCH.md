# Dispatch: M1 Iteration 3 Explorer 1 Gen 2 (tabSingles Concurrency & Test Isolation Overhaul)

## Mission
Formulate a fundamentally different, bulletproof strategy to eliminate MariaDB OperationalError (1020) on `tabSingles` in `test_ism_settings.py` without silent exception swallowing in tearDown or dirty state leakage across multi-run test executions.

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
  - Ensure pure transaction rollbacks (`frappe.db.rollback()`) around test mutations or test isolation via `ISMTestCase`.
  - In `setUp()` and `tearDown()`, establish and restore a deterministic dictionary of canonical default values.
  - In-memory testing for validation methods or clean test separation.
  - Ensure 5 consecutive runs of `bench run-tests --app ism --doctype 'ISM Settings'` succeed cleanly from a fresh shell with exit code 0.

## Output Requirements
Deliver a concrete, line-by-line blueprint, replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it3_explorer_1_gen2\handoff.md` and message the orchestrator when complete.
