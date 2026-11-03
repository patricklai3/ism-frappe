# Dispatch: M1 Challenger 4 (Migration Deadlock & Settings Concurrency Stress Testing)

## Mission
Empirically stress test `ism/setup.py` migrations and `ISM Settings` concurrency/isolation in live MariaDB in `devcontainer-frappe-1`.

## Mandatory References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Worker Handoff: `d:\ism\.agents\teamwork\m1_worker_2\handoff.md`

## Focus Areas
- Empirically stress test:
  1. Multiple consecutive `bench migrate` executions in WSL without any MariaDB 1020 OperationalError or deadlock.
  2. Verify that seeder execution ordering in `after_migrate()` always ensures roles, settings, equipment tags, and schedule entry types exist before downstream backfills.
  3. Run multi-threaded or rapid sequential calls to `TestISMSettings` (10x runs) to verify zero `tabSingles` lock contention or stale cached data bleed.
  4. Test `ISM Settings` bounds validations (percentile 50-99, sigma 0.05-1.00, operating hours ordering, break hours ordering).
- Output verdict: **APPROVE** or **CHALLENGE_FAILED** in `d:\ism\.agents\teamwork\m1_challenger_4\handoff.md` and message orchestrator.

## 2026-10-26T16:09:22Z
You are m1_challenger_4 (M1 Migration & Concurrency Challenger 4). Your working directory is d:\ism\.agents\teamwork\m1_challenger_4.
Read your dispatch instructions at d:\ism\.agents\teamwork\m1_challenger_4\DISPATCH.md and the authoritative requirements at d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md.
Empirically stress test consecutive bench migrate runs and 10x sequential TestISMSettings executions in live MariaDB in devcontainer-frappe-1. Verify zero MariaDB 1020 OperationalError or deadlock.
Deliver your stress test results with explicit verdict APPROVE or CHALLENGE_FAILED in d:\ism\.agents\teamwork\m1_challenger_4\handoff.md and send_message to orchestrator.
