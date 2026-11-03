# Dispatch: Milestone 1 Iteration 4 Remediation Worker (m1_worker_4)

## Mission
Apply the unified Milestone 1 Iteration 4 remediations to achieve permanent zero-churn test isolation, eliminate MariaDB 1020 and 1213 deadlocks across `tabSingles`, `tabCustomer`, and `tabSeries`, and achieve 10/10 multi-run stability.

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Explorer Blueprints & Patch Artifacts:
   - Explorer 1 (test_ism_settings.py setUpClass & Zero-Churn):
     `d:\ism\.agents\teamwork\m1_it4_explorer_1\handoff.md`
     `d:\ism\.agents\teamwork\m1_it4_explorer_1\proposed_test_ism_settings.py`
     `d:\ism\.agents\teamwork\m1_it4_explorer_1\test_ism_settings.patch`
   - Explorer 2 (test_schedule_entry.py Entity Uniqueness):
     `d:\ism\.agents\teamwork\m1_it4_explorer_2\handoff.md`
     `d:\ism\.agents\teamwork\m1_it4_explorer_2\proposed_test_schedule_entry.py`
     `d:\ism\.agents\teamwork\m1_it4_explorer_2\entity_uniqueness.patch`
   - Explorer 3 (base.py Two-Phase Zero-Churn Teardown):
     `d:\ism\.agents\teamwork\m1_it4_explorer_3\handoff.md`
     `d:\ism\.agents\teamwork\m1_it4_explorer_3\proposed_base.py`
     `d:\ism\.agents\teamwork\m1_it4_explorer_3\base_teardown_hygiene.patch`

## Owned Files & Scope Boundaries
You have exclusive write ownership of:
1. `d:\ism\ism\ism\doctype\ism_settings\test_ism_settings.py`
2. `d:\ism\ism\ism\doctype\schedule_entry\test_schedule_entry.py`
3. `d:\ism\ism\tests\base.py`

## Implementation Steps
1. Replace `ism/ism/doctype/ism_settings/test_ism_settings.py` with the zero-churn version from Explorer 1 (`proposed_test_ism_settings.py`).
2. Replace `ism/ism/doctype/schedule_entry/test_schedule_entry.py` with the unique-entity version from Explorer 2 (`proposed_test_schedule_entry.py`).
3. Update `ism/tests/base.py`:
   - Integrate Explorer 3's two-phase zero-churn teardown (`proposed_base.py`).
   - Ensure Explorer 2's `create_test_vehicle(self, vin=None, customer=None, **kwargs)` with default unique VIN generation (`vin = vin or f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"`) is included.
4. Sync code to WSL container:
   `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
5. Execute cleanup script in devcontainer:
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute ism.tests.cleanup.cleanup_all_test_data"`
6. Execute all 4 Milestone 1 test suites:
   - `bench --site development.localhost run-tests --app ism --doctype 'Service Bay'`
   - `bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'`
   - `bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'`
   - `bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'`
7. Execute strict 10x sequential stress runs:
   - 10x loop on `ISM Settings`: must pass 10/10 with exit code 0 and 0 deadlocks.
   - 10x loop on `Schedule Entry`: must pass 10/10 with exit code 0 and 0 deadlocks.
8. Verify MariaDB cleanliness: zero residual test records across all tables.

## Verification & Output Requirements
- Document all execution outputs verbatim in `d:\ism\.agents\teamwork\m1_worker_4\handoff.md`.
- Ensure zero errors, zero deadlocks, and zero regressions.
- Send handoff message to orchestrator when complete.


## 2026-10-26T20:10:51Z
You are m1_worker_4 (Milestone 1 Iteration 4 Remediation Worker).
Your working directory is: d:\ism\.agents\teamwork\m1_worker_4
Read your dispatch instructions at: d:\ism\.agents\teamwork\m1_worker_4\DISPATCH.md
Read authoritative requirements at: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Read project plan at: d:\ism\PROJECT.md
Load and follow domain skills:
- d:\ism\.agents\skills\frappe-app-dev\SKILL.md
- d:\ism\.agents\skills\code-style\SKILL.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Your task:
Apply the unified remediation blueprints from the 3 Explorers:
1. Replace ism/ism/doctype/ism_settings/test_ism_settings.py with proposed_test_ism_settings.py from d:\ism\.agents\teamwork\m1_it4_explorer_1
2. Replace ism/ism/doctype/schedule_entry/test_schedule_entry.py with proposed_test_schedule_entry.py from d:\ism\.agents\teamwork\m1_it4_explorer_2
3. Update ism/tests/base.py: integrate Explorer 3's two-phase zero-churn teardown (proposed_base.py from d:\ism\.agents\teamwork\m1_it4_explorer_3) AND ensure Explorer 2's create_test_vehicle unique VIN generator (vin = vin or f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}") is present.
4. Sync to WSL, run cleanup, and verify all 4 M1 test suites (Service Bay, Schedule Entry Type, Schedule Entry, ISM Settings) and strict 10x sequential stress runs on ISM Settings and Schedule Entry.
Deliver your handoff report with verbatim outputs in d:\ism\.agents\teamwork\m1_worker_4\handoff.md and report back via send_message.
