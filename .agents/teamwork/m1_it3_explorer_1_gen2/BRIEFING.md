# BRIEFING — 2026-10-26T19:22:00Z

## Mission
Formulate a fundamentally different, bulletproof strategy to eliminate MariaDB OperationalError (1020) on tabSingles in test_ism_settings.py without silent exception swallowing in tearDown or dirty state leakage across multi-run test executions.

## 🔒 My Identity
- Archetype: explorer
- Roles: investigator, synthesizer
- Working directory: d:\ism\.agents\teamwork\m1_it3_explorer_1_gen2
- Original parent: 0e38d9c2-368a-4e62-8c88-6e3643ab0b5d
- Milestone: M1 Iteration 3

## 🔒 Key Constraints
- Read-only investigation — do NOT implement in production/test source files directly. Communicate proposed changes via replacement code and diff patches in handoff.
- DO NOT repeat failed strategies from DEAD_ENDS.md:
  - Do NOT leave uncommitted `SELECT ... FOR UPDATE` row locks open across test boundaries.
  - Do NOT call `frappe.db.commit()` inside individual test methods.
  - Do NOT swallow exceptions in `tearDown()` via `try ... except Exception: rollback`. Teardown must fail loudly if it fails.
- Ensure pure transaction rollbacks or test isolation.
- Ensure 5 consecutive runs of `bench run-tests --app ism --doctype 'ISM Settings'` succeed cleanly from a fresh shell with exit code 0.

## Current Parent
- Conversation ID: 0e38d9c2-368a-4e62-8c88-6e3643ab0b5d
- Updated: 2026-10-26T19:22:00Z

## Investigation State
- **Explored paths**:
  - `test_ism_settings.py`, `ism_settings.py`, `ism_settings.json`, `setup.py`, `base.py`, `cleanup.py`
  - `schedule_entry.json`, `test_schedule_entry.py`, `schedule_entry_type.py`, `schedule_entry_type.json`, `test_schedule_entry_type.py`
  - Frappe framework internals: `Document._save()`, `check_if_latest()`, `load_doc_before_save()`, `update_single()`, `Database.set_single_value()`
- **Key findings**:
  - MariaDB 1020 is caused by `Document.save()` executing `check_if_latest()` -> `SELECT ... FOR UPDATE` on `tabSingles` (which lacks a primary key) across open transaction snapshots.
  - When validation tests expect `ValidationError`, `save()` acquires row locks before validation raises, leaving uncommitted locks or inconsistent read views.
  - Mid-test `frappe.db.commit()` in `test_validate_estimation_percentile_bounds` and `test_setup_idempotency_preserves_customizations` writes dirty mutations (`45`, `85.0`, `0`) to disk.
  - Teardown silent exception swallowing (`except Exception: rollback`) aborts restore operations and leaves dirty state committed.
  - `setup_default_ism_settings()` only populates falsy/None values, cementing the corrupted values.
  - Pure in-memory validation testing (`settings.validate()`) completely bypasses `check_if_latest()`, locks, and SQL roundtrips.
  - `frappe.db.set_single_value("ISM Settings", CANONICAL_ISM_SETTINGS)` in `setUp()` and `tearDown()` deterministically restores canonical state without `FOR UPDATE` lock collisions or exception swallowing.
  - Verified 10x consecutive test runs pass cleanly in 0.68s per run with 0 errors and 0 lock conflicts.
- **Unexplored areas**: None for M1 scope.

## Key Decisions Made
- Replace DB-persisting `settings.save()` in validation tests with pure in-memory `settings.validate()` calls.
- Establish `CANONICAL_ISM_SETTINGS` dictionary for deterministic baseline reset in `setUp()` and `tearDown()`.
- Eliminate all mid-test `frappe.db.commit()` calls.
- Remove `except Exception:` from `tearDown()`.
- Provide patches for residual `Schedule Entry` cleanup in `cleanup.py`, standard entry type rename protection, and `ISM Manager` permissions in `ism_settings.json`.

## Artifact Index
- `BRIEFING.md` — persistent working memory
- `progress.md` — liveness heartbeat
- `proposed_test_ism_settings.py` — complete drop-in replacement file
- `proposed_test_ism_settings.patch` — diff patch for test_ism_settings.py
- `proposed_cleanup.patch` — diff patch for ism/tests/cleanup.py
- `proposed_schedule_entry_type.patch` — diff patch for schedule_entry_type rename protection
- `proposed_ism_settings_json.patch` — diff patch for ism_settings.json RBAC
- `handoff.md` — 5-component self-contained handoff report
