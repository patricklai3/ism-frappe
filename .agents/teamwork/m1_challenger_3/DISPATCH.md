# Dispatch: M1 Challenger 3 (Data Models & Collision Empirical Stress Testing)

## Mission
Empirically stress test the Milestone 1 data models and controller logic using python test scripts against live MariaDB in `devcontainer-frappe-1`.

## Mandatory References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Worker Handoff: `d:\ism\.agents\teamwork\m1_worker_2\handoff.md`

## Focus Areas
- Write and execute an empirical test script verifying:
  1. `Schedule Entry` positive duration enforcement (`estimated_duration <= 0`, negative values, strings, None).
  2. Collision detection fallback in `ScheduleEntry._check_bay_availability_fallback` under edge case intervals (exact abutting appointments, 1-minute overlap, nested intervals).
  3. `populate_from_repair_order` on document creation and update (`before_save`).
  4. Multiple appointments per Repair Order (1:N cardinality invariant).
  5. `ServiceBay.on_trash()` blocks deletion when active entries exist, but allows deletion if all entries are `Cancelled` or `Completed`.
  6. `ScheduleEntryType.on_trash()` blocks deletion of standard types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) and types linked to schedule entries.
- Output verdict: **APPROVE** or **CHALLENGE_FAILED** in `d:\ism\.agents\teamwork\m1_challenger_3\handoff.md` and message orchestrator.


## 2026-10-26T16:09:22Z
You are m1_challenger_3 (M1 Data Models Empirical Challenger 3). Your working directory is d:\ism\.agents\teamwork\m1_challenger_3.
Read your dispatch instructions at d:\ism\.agents\teamwork\m1_challenger_3\DISPATCH.md and the authoritative requirements at d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md.
Write and run empirical stress tests in live MariaDB in devcontainer-frappe-1 to verify positive duration validation, collision intervals, update auto-population, 1:N cardinality, and deletion guards on ServiceBay and ScheduleEntryType.
Deliver your stress test results with explicit verdict APPROVE or CHALLENGE_FAILED in d:\ism\.agents\teamwork\m1_challenger_3\handoff.md and send_message to orchestrator.
