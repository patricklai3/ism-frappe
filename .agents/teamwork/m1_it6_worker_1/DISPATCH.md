# Dispatch: Milestone 1 Iteration 6 Remediation Worker

**Agent ID**: `m1_it6_worker_1`  
**Working Directory**: `d:\ism\.agents\teamwork\m1_it6_worker_1`  
**Role**: `teamwork_preview_worker`  
**Parent Orchestrator**: `0910fe5c-bb68-4b00-a067-81562d40a7e2`  

### Mandatory Integrity Warning:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

### Context & Task Assignment:
Milestone 1 Gate sign-off was rejected by the Forensic Auditor (`m1_gate_auditor_1/handoff.md`) and Reviewer 2 (`m1_gate_reviewer_2/handoff.md`).
Explorers 1, 2, and 3 have completed the root cause analysis and formulated an exact, comprehensive 5-pillar code modification plan.

Read:
- `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`
- `d:\ism\PROJECT.md`
- `d:\ism\.agents\teamwork\m1_it6_explorer_2\handoff.md` (Detailed before/after code modification plan)
- `d:\ism\.agents\teamwork\m1_it6_explorer_3\handoff.md` (5-pillar architecture)
- `d:\ism\.agents\teamwork\m1_it6_explorer_1\handoff.md` (Line-by-line evidence chains)
- `d:\ism\.agents\teamwork\m1_gate_auditor_1\handoff.md` (Auditor failure traces)

### Exclusive File Ownership:
You own exclusively:
1. `d:\ism\ism\tests\base.py`
2. `d:\ism\ism\tests\test_fixtures.py`
3. `d:\ism\ism\tests\cleanup.py`
4. `d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py`
5. `d:\ism\ism\ism\doctype\schedule_entry\test_schedule_entry.py`
6. `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`

### Implementation Tasks:
1. **Target 1 (`ism/tests/base.py`)**:
   - In `create_test_customer()`, `create_test_vehicle()`, and `create_test_repair_order()`, add `frappe.db.commit()` immediately after successful insertion so prerequisite records survive retries.
   - Add `create_test_schedule_entry(self, **kwargs)` with a 3-attempt restart loop on MariaDB error 1020 / `QueryDeadlockError` with `frappe.db.rollback()` and `frappe.db.commit()`.
   - In `create_test_quotation()`, wrap insertion in a 3-attempt restart loop on 1020 and commit on success.
   - In `cleanup_tracked_docs()`, ensure `Schedule Entry` records are purged before parent docs.

2. **Target 2 (`ism/tests/test_fixtures.py`)**:
   - In `ensure_ism_settings()` (line 301–303): Align default settings to `"buffer_padding_minutes": 0` and `"estimation_percentile": 80.0` (matching `setup.py:146` and `CANONICAL_ISM_SETTINGS`).
   - In `create_test_schedule_entry()` (lines 316–340): Wrap insertion in a 3-attempt restart loop on 1020 / `QueryDeadlockError` with `frappe.db.rollback()` and `frappe.db.commit()`.
   - In `create_test_repair_order()` (lines 342–355): Wrap insertion in a 3-attempt restart loop on 1020 / `QueryDeadlockError` with `frappe.db.rollback()` and `frappe.db.commit()`.
   - In `teardown_transactional()`: Include `TEMP%` and test schedule entries in cleanup.

3. **Target 3 (`ism/ism/doctype/schedule_entry/schedule_entry.py`)**:
   - Add `autoname(self)` with a 3-attempt restart loop on 1020 / `QueryDeadlockError` around `make_autoname(prefix, doc=self)`.

4. **Target 4 (`ism/ism/doctype/schedule_entry/test_schedule_entry.py`)**:
   - In `setUp()`: Call `frappe.db.commit()` after creating customer, vehicle, and repair order.
   - Implement `_insert_entry(self, doc_dict)` with a 3-attempt restart loop on 1020 with `frappe.db.rollback()` and `frappe.db.commit()`, and route test insertions through it.

5. **Target 5 (`ism/ism/doctype/ism_settings/test_ism_settings.py`)**:
   - In `setUpClass()` (lines 58–64): Wrap `frappe.db.set_single_value("ISM Settings", divergent)` in a 3-attempt restart loop on 1020 / `QueryDeadlockError` with `frappe.db.rollback()`, cache clearing, and commit.

6. **Target 6 (`ism/tests/cleanup.py`)**:
   - Define `PRESERVED_SERVICE_BAYS = {"Test Bay 1", "Test Bay 2", "Test Bay 3"}`.
   - In `_cleanup_service_bays()`: Delete any service bay whose name is NOT in `PRESERVED_SERVICE_BAYS` (after cleaning linked schedule entries).
   - Add `_cleanup_equipment_tags()` to delete custom tags not in `{"Lift", "EV High Voltage", "Alignment Rack", "HV Battery Station"}`.
   - In `_cleanup_schedule_entries()`: Mark `is_test = True` if `se.service_bay not in PRESERVED_SERVICE_BAYS` or starts with `TEMP`.

7. **One-Time Database Purge**:
   - Execute one-time purge of `TEMP_BAY` and `TEMP_BAY_MULTI` in `devcontainer-frappe-1`.

8. **Live Container Verification**:
   - 10x sequential stress loop on `Schedule Entry`: 10/10 PASS, 0 deadlocks.
   - 10x sequential stress loop on `ISM Settings`: 10/10 PASS, 0 deadlocks.
   - Dirty baseline test on `ISM Settings`: verify `setUpClass` recovers without deadlock.
   - Full application test suite (`bench run-tests --app ism`): 381 tests pass, 0 errors, 0 failures, exit code 0.
   - Database hygiene check: verify `TEMP_BAY` and `TEMP_BAY_MULTI` are eliminated and static fixtures (`Test Customer`, `5YJSA1E26CF000001`) remain intact.

Write `d:\ism\.agents\teamwork\m1_it6_worker_1\handoff.md` with complete verification outputs.
Notify parent orchestrator (`0910fe5c-bb68-4b00-a067-81562d40a7e2`) via send_message when done.

## 2026-10-27T00:33:36Z
You are m1_it6_worker_1 (Role: worker). Your working directory is d:\ism\.agents\teamwork\m1_it6_worker_1.

DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Implement the 5-pillar remediation described in DISPATCH.md and Explorer 2/3 handoff reports:
1. Transaction-safe persistence (commit after insert) in base.py to eliminate LinkValidationError.
2. 3-attempt restart loops on MariaDB error 1020 / QueryDeadlockError in schedule_entry.py (autoname), test_fixtures.py (create_test_schedule_entry, create_test_repair_order), and test_schedule_entry.py.
3. Default settings alignment in test_fixtures.py:ensure_ism_settings (buffer_padding_minutes = 0, estimation_percentile = 80.0).
4. 3-attempt restart loop on 1020 in test_ism_settings.py:setUpClass.
5. Service Bay & Equipment Tag cleanup hardening in cleanup.py, plus one-time purge of TEMP_BAY and TEMP_BAY_MULTI.

Execute all verification tests inside devcontainer-frappe-1:
- 10x sequential stress loop on Schedule Entry (10/10 PASS)
- 10x sequential stress loop on ISM Settings (10/10 PASS)
- Dirty baseline test on ISM Settings
- Full application test suite (bench run-tests --app ism): 381 tests pass, 0 errors, 0 failures, exit code 0
- Database cleanliness verification

Write your handoff report to d:\ism\.agents\teamwork\m1_it6_worker_1\handoff.md with all command outputs.
When complete, notify parent orchestrator (conversation ID: 0910fe5c-bb68-4b00-a067-81562d40a7e2) via send_message.

