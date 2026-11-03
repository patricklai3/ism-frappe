# Progress Log — m1_it6_challenger_2

**Agent ID**: `m1_it6_challenger_2`
**Last visited**: 2026-10-27T00:57:00Z

## Status
- [x] Initial setup: BRIEFING.md, DISPATCH.md, skills copy
- [x] Adversarial Plan formulation
- [x] Direct SQL pre-test baseline check on MariaDB:
  - tabService Bay: 3 canonical bays, 0 TEMP_BAYs.
  - tabEquipment Tag: 4 seed tags, 0 custom tags.
  - tabSchedule Entry: 0 rows.
  - Static fixtures: 'Test Customer', '5YJSA1E26CF000001' intact.
- [x] Run 1: Full application test run #1 (`bench run-tests --app ism`) -> FAILED (exit code 1, 19 failures/errors, MariaDB 1020 deadlock cascade on tabSeries, tabRepair Vehicle, tabRepair Vehicle Customer).
- [x] Run 2: Direct SQL check between runs -> Identified 10 leaked Repair Vehicles in tabRepair Vehicle with dangling references to deleted customers.
- [x] Run 3: Full application test run #2 (`bench run-tests --app ism`) -> FAILED (exit code 1, 19 failures/errors including LinkValidationError on Inspection Template and AssertionError on TestServiceBay.test_service_bay_deletion_blocked_with_active_schedule_entry).
- [x] Run 4: High-frequency stress test loops:
  - 10x Schedule Entry stress loop: PASS (70/70 tests passed in isolation).
  - 10x ISM Settings stress loop: PASS (80/80 tests passed in isolation).
  - Dirty baseline ISM Settings test: PASS (8/8 tests passed).
  - Empirical Adversarial Gate suite (`scripts/empirical_adversarial_gate_m1.py`): PASS (46/46 passed).
- [x] Run 5: Direct SQL post-stress check on MariaDB:
  - tabService Bay: 3 canonical bays, 0 TEMP_BAYs (PASS).
  - tabEquipment Tag: 4 seed tags (PASS).
  - Static fixtures: 'Test Customer', '5YJSA1E26CF000001' intact (PASS).
  - tabRepair Vehicle: 10 leaked orphaned test vehicles (`5YJ3E1EBXNF...`) persisting because cleanup.py does not delete vehicles without matching customers or standard plates (FAIL).
- [x] Root Cause Identification:
  - `frappe.db.commit()` in `base.py:71, 97, 119, 137` commits test entities mid-test, corrupting MariaDB 11.8 snapshot isolation (`innodb_snapshot_isolation = ON`) and causing un-retried tests to crash on 1020.
  - `ScheduleEntry.autoname` calls `frappe.db.rollback()` on 1020, wiping out uncommitted parent fixtures (e.g. Service Bay) within the same test transaction.
  - In `base.py:238`, teardown catches deletion failures with `except Exception: pass` and commits, permanently leaking committed vehicles into MariaDB.
- [x] Synthesize findings into handoff.md with explicit verdict: REQUEST_CHANGES
- [ ] Send message to parent orchestrator
