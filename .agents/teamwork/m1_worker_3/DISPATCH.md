# Dispatch: Milestone 1 Iteration 3 Remediation Worker (m1_worker_3)

## Mission
Implement the unified Milestone 1 Iteration 3 remediations to achieve clean test isolation, permanent elimination of MariaDB 1020 on `tabSingles`, standard Schedule Entry Type rename protection, `ISM Manager` permissions on `ISM Settings`, and residual test data cleanup for `Schedule Entry`.

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Explorer Blueprints & Patch Artifacts:
   - Explorer 1 (tabSingles & test_ism_settings.py):
     `d:\ism\.agents\teamwork\m1_it3_explorer_1_gen2\handoff.md`
     `d:\ism\.agents\teamwork\m1_it3_explorer_1_gen2\proposed_test_ism_settings.py`
   - Explorer 2 (Rename Guard & ISM Manager RBAC):
     `d:\ism\.agents\teamwork\m1_it3_explorer_2_gen2\handoff.md`
     `d:\ism\.agents\teamwork\m1_it3_explorer_2_gen2\proposed_schedule_entry_type.py`
     `d:\ism\.agents\teamwork\m1_it3_explorer_2_gen2\proposed_schedule_entry_type.json`
     `d:\ism\.agents\teamwork\m1_it3_explorer_2_gen2\proposed_ism_settings.json`
     `d:\ism\.agents\teamwork\m1_it3_explorer_2_gen2\proposed_test_schedule_entry_type.py`
   - Explorer 3 (Cleanup & Schedule Entry Isolation):
     `d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\handoff.md`
     `d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\proposed_cleanup.py`
     `d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\proposed_base.py`
     `d:\ism\.agents\teamwork\m1_it3_explorer_3_gen2\proposed_test_schedule_entry.py`

## Owned Files & Scope Boundaries
You have exclusive write ownership of the following target files:
1. `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`
2. `d:\ism\ism\ism\doctype\ism_settings\ism_settings.json`
3. `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.py`
4. `d:\ism\ism\ism\doctype\schedule_entry_type\schedule_entry_type.json`
5. `d:\ism\ism\ism\doctype\schedule_entry_type\test_schedule_entry_type.py`
6. `d:\ism\ism\ism\doctype\schedule_entry\test_schedule_entry.py`
7. `d:\ism\ism\tests\cleanup.py`
8. `d:\ism\ism\tests\base.py`

## Implementation Steps
1. Apply the replacement for `test_ism_settings.py` from Explorer 1 (`proposed_test_ism_settings.py`). Include the `test_ism_manager_permissions` method from Explorer 2 so `ISM Manager` RBAC is explicitly verified.
2. Apply `proposed_schedule_entry_type.py`, `proposed_schedule_entry_type.json`, and `proposed_test_schedule_entry_type.py` from Explorer 2.
3. Apply `proposed_ism_settings.json` from Explorer 2 (`ISM Manager` read/write permissions).
4. Apply `proposed_cleanup.py`, `proposed_base.py`, and `proposed_test_schedule_entry.py` from Explorer 3.
5. Sync code to WSL container:
   `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
6. Run bench migrate to apply schema metadata and permissions:
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"`
7. Execute cleanup script to purge leftover test records:
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute ism.tests.cleanup.cleanup_all_test_data"`
8. Execute all Milestone 1 test suites:
   - `bench --site development.localhost run-tests --app ism --doctype 'Service Bay'`
   - `bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'`
   - `bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'`
   - `bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'`
9. Execute a strict 5x sequential run on `ISM Settings`:
   `wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"`
10. Execute a strict 5x sequential run on `Schedule Entry`:
   `wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry' || exit 1; done"`

## Verification & Output Requirements
- Document all execution outputs verbatim in `d:\ism\.agents\teamwork\m1_worker_3\handoff.md`.
- Ensure zero errors, zero deadlocks, and zero regressions.
- Send handoff message to orchestrator when complete.


## 2026-10-26T19:25:59Z
Received dispatch from parent orchestrator 0e38d9c2-368a-4e62-8c88-6e3643ab0b5d to execute Milestone 1 Iteration 3 Remediation Worker tasks.
