# Dispatch: M1 Iteration 3 Explorer 3 Gen 2 (Residual Test Data Cleanup & Leak Elimination)

## Mission
Formulate exact remediation blueprints for the test isolation failure in `test_schedule_entry.py` identified by `m1_auditor_2`.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Forensic Auditor Report: `d:\ism\.agents\teamwork\m1_auditor_2\handoff.md` (§1.2 Failure 3)
4. Test files:
   - `ism/tests/cleanup.py`
   - `ism/tests/base.py`
   - `ism/ism/doctype/schedule_entry/test_schedule_entry.py`

## Focus Area
- `test_multiple_schedule_entries_per_repair_order` in `test_schedule_entry.py` failed with:
  `AssertionError: 7 != 2`
- Root Cause:
  `cleanup_all_test_data()` in `ism/tests/cleanup.py` deletes `Repair Order`, `Customer`, `Repair Vehicle`, etc., but does NOT clean up orphaned `Schedule Entry` records.
  When tests run repeatedly or abort midway, leftover `Schedule Entry` records referencing the reused test `Repair Order` accumulate in `tabSchedule Entry`.
- Formulate the exact fix:
  1. Add `Schedule Entry` to `cleanup_all_test_data()` in `ism/tests/cleanup.py` in proper reverse dependency order (delete `Schedule Entry` before deleting `Repair Order` and `Service Bay`).
  2. Ensure `TestScheduleEntry.setUp()` and `tearDown()` clean up any entries created during the test.
  3. Verify that `test_schedule_entry.py` passes reliably across multiple executions.

## Output Requirements
Deliver a concrete, line-by-line blueprint, replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\handoff.md` and message the orchestrator when complete.


## 2026-10-26T19:12:19Z
You are m1_it3_explorer_3_gen2 (M1 It3 Test Cleanup & Isolation Explorer 3).
Your working directory is: d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2
Read your dispatch instructions at: d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\DISPATCH.md
Read authoritative requirements at: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Read project plan at: d:\ism\PROJECT.md
Read Forensic Auditor report at: d:\ism\.agents\teamwork\m1_auditor_2\handoff.md (§1.2 Failure 3)
Inspect ism/tests/cleanup.py, ism/tests/base.py, and test_schedule_entry.py.

Your mission:
Formulate the exact blueprint and diff to add Schedule Entry cleanup to cleanup_all_test_data() in ism/tests/cleanup.py and eliminate residual test leaks in test_schedule_entry.py.
Deliver your blueprint, proposed code, and diff patch in d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\handoff.md and report back to orchestrator via send_message.
