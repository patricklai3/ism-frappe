# BRIEFING — 2026-10-26T20:02:00Z

## Mission
Eliminate hardcoded customer name (_TEST_ISM_Cust_SE) recycling in test_schedule_entry.py using unique hashes/suffixes per test to eliminate MariaDB 1020 on tabCustomer.

## 🔒 My Identity
- Archetype: explorer
- Roles: investigation, synthesis, entity uniqueness specialist
- Working directory: d:\ism\.agents\teamwork\m1_it4_explorer_2
- Original parent: 0e38d9c2-368a-4e62-8c88-6e3643ab0b5d
- Milestone: M1 Iteration 4

## 🔒 Key Constraints
- Read-only investigation — do NOT implement directly in source repository, only propose via blueprint/replacement/patch in handoff.md
- Eliminate hardcoded customer name (_TEST_ISM_Cust_SE) recycling in test_schedule_entry.py by using unique hashes/suffixes per test, eliminating MariaDB 1020 on tabCustomer
- Empirically verify across 10 consecutive test runs in devcontainer
- Deliver blueprint, replacement code, and diff patch in d:\ism\.agents\teamwork\m1_it4_explorer_2\handoff.md and report back via send_message

## Current Parent
- Conversation ID: 0e38d9c2-368a-4e62-8c88-6e3643ab0b5d
- Updated: 2026-10-26T20:02:00Z

## Investigation State
- **Explored paths**:
  - `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
  - `ism/tests/base.py`
  - `ism/tests/cleanup.py`
  - `ism/ism/doctype/schedule_entry/schedule_entry.py`
  - `ism/ism/doctype/repair_vehicle/repair_vehicle.py`
  - `.agents/teamwork/m1_it3_reviewer_2/handoff.md`
- **Key findings**:
  - `test_schedule_entry.py` hardcoded customer name `_TEST_ISM_Cust_SE` and vehicle VIN `5YJ3E1EBXNF123456` in `setUp()`.
  - In MariaDB 11.8 with `REPEATABLE-READ` isolation, deleting a row and immediately re-inserting with the identical primary key within milliseconds triggers `OperationalError 1020: Record has changed since last read in table 'tabCustomer'`.
  - Switching to unique per-test customer names (`self.create_test_customer()` with 8-character hash) and unique per-test VINs (`5YJ3E1EBXNF` + 6-character hex hash) completely eliminates key recycling contention.
  - Adding vehicle-linked Schedule Entry cleanup in `cleanup_schedule_entries()` guarantees total entity isolation.
  - Empirically verified across 10 consecutive test runs in devcontainer: 10/10 runs passed (70/70 tests, 0 failures, 0 deadlocks).
- **Unexplored areas**: None within this mission boundary.

## Key Decisions Made
- `test_schedule_entry.py` modified in `setUp()` to use `self.create_test_customer()` without hardcoded suffix and unique VIN per test.
- `create_test_vehicle` in `base.py` updated to default `vin=None` to `f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"`.
- Provided replacement files, diff patch, and comprehensive handoff report.

## Artifact Index
- `d:\ism\.agents\teamwork\m1_it4_explorer_2\DISPATCH.md` — Dispatch instructions
- `d:\ism\.agents\teamwork\m1_it4_explorer_2\BRIEFING.md` — Persistent situational memory
- `d:\ism\.agents\teamwork\m1_it4_explorer_2\progress.md` — Liveness heartbeat and step history
- `d:\ism\.agents\teamwork\m1_it4_explorer_2\proposed_test_schedule_entry.py` — Complete replacement file
- `d:\ism\.agents\teamwork\m1_it4_explorer_2\proposed_base.py` — Complete replacement base class
- `d:\ism\.agents\teamwork\m1_it4_explorer_2\entity_uniqueness.patch` — Unified diff patch
- `d:\ism\.agents\teamwork\m1_it4_explorer_2\handoff.md` — 5-component handoff report
