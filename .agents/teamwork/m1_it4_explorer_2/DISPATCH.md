# Dispatch: M1 Iteration 4 Explorer 2 (test_schedule_entry.py Entity Uniqueness & Key Recycling Elimination)

## Mission
Formulate exact remediation blueprints to eliminate key recycling contention and MariaDB 1020 on `tabCustomer` in `test_schedule_entry.py`.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Reviewer 2 Handoff: `d:\ism\.agents\teamwork\m1_it3_reviewer_2\handoff.md` (§1.2 Failure 2, §2.2)
4. Test files:
   - `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
   - `ism/tests/base.py`

## Problem & Fix Direction
- In `test_schedule_entry.py` line 12:
  `self.customer = self.create_test_customer(f"{TEST_PREFIX}Cust_SE")`
  hardcodes the customer name across all test methods.
- In `tearDown()`, deleting this customer and re-inserting it in `setUp()` of the next test method with the identical primary key causes MariaDB 11 InnoDB undo-log page version collision (OperationalError 1020: `Record has changed since last read in table 'tabCustomer'`).
- Formulate the exact fix:
  1. Generate unique random/hash names for customers and vehicles per test (e.g. `self.create_test_customer()` without hardcoded suffix, using `frappe.generate_hash(length=8)`).
  2. Ensure `test_schedule_entry.py` cleans up its own entities cleanly in `tearDown()`.
  3. Verify that 10 consecutive executions of `bench run-tests --app ism --doctype 'Schedule Entry'` pass 10/10 with 0 errors.

## Deliverable
Deliver your blueprint, proposed replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it4_explorer_2\handoff.md` and message orchestrator when complete.


## 2026-10-26T19:49:56Z
[Message] timestamp=2026-10-26T19:49:56Z sender=0e38d9c2-368a-4e62-8c88-6e3643ab0b5d priority=MESSAGE_PRIORITY_HIGH content=You are m1_it4_explorer_2 (Milestone 1 Iteration 4 Entity Uniqueness Specialist).
Your working directory is: d:\ism\.agents\teamwork\m1_it4_explorer_2
Read your dispatch instructions at: d:\ism\.agents\teamwork\m1_it4_explorer_2\DISPATCH.md
Read authoritative requirements at: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Read project plan at: d:\ism\PROJECT.md
Read Reviewer 2 report at: d:\ism\.agents\teamwork\m1_it3_reviewer_2\handoff.md
Inspect ism/ism/doctype/schedule_entry/test_schedule_entry.py and ism/tests/base.py.

Your mission:
Eliminate hardcoded customer name (_TEST_ISM_Cust_SE) recycling in test_schedule_entry.py by using unique hashes/suffixes per test, eliminating MariaDB 1020 on tabCustomer.
Empirically verify across 10 consecutive test runs in devcontainer.
Deliver blueprint, replacement code, and diff patch in d:\ism\.agents\teamwork\m1_it4_explorer_2\handoff.md and report back via send_message.
