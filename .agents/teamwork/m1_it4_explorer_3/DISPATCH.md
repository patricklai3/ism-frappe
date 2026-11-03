# Dispatch: M1 Iteration 4 Explorer 3 (Cross-Suite Teardown & Base Test Hygiene)

## Mission
Analyze cross-suite teardown interactions in `ism/tests/base.py` and `cleanup.py` to prevent cascading deadlocks during multi-run sequential execution.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Reviewer 1 Handoff: `d:\ism\.agents\teamwork\m1_it3_reviewer_1\handoff.md` (§Critical Failure)
4. Code files:
   - `ism/tests/base.py`
   - `ism/tests/cleanup.py`

## Focus Area
- Reviewer 1 observed:
  "`Schedule Entry` sequential tests intermittently trigger deadlocks (`OperationalError: 1213` and `1020` on `tabSeries`) due to `base.py`'s `tearDown()` running `cleanup_all_test_data` across 14 tables with mid-suite commits between every single test."
- Investigate whether `ISMTestCase.tearDown()` is doing too much global sweeping on every test method instead of only cleaning up `_tracked_docs`.
- Formulate recommendations to streamline `base.py` teardown so individual tests clean up their tracked fixtures without sweeping the entire database and committing between every test.

## Deliverable
Deliver your blueprint, proposed replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it4_explorer_3\handoff.md` and message orchestrator when complete.

## 2026-10-26T19:49:56Z
You are m1_it4_explorer_3 (Milestone 1 Iteration 4 Base Test Hygiene Specialist).
Your working directory is: d:\ism\.agents\teamwork\m1_it4_explorer_3
Read your dispatch instructions at: d:\ism\.agents\teamwork\m1_it4_explorer_3\DISPATCH.md
Read authoritative requirements at: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Read project plan at: d:\ism\PROJECT.md
Read Reviewer 1 report at: d:\ism\.agents\teamwork\m1_it3_reviewer_1\handoff.md
Inspect ism/tests/base.py and ism/tests/cleanup.py.

Your mission:
Analyze ISMTestCase.tearDown() in ism/tests/base.py. Formulate recommendations to avoid full 14-table sweeps with mid-suite commits between every single test method while preserving clean tracked-doc cleanup.
Deliver blueprint, replacement code, and diff patch in d:\ism\.agents\teamwork\m1_it4_explorer_3\handoff.md and report back via send_message.

