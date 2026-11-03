# Dispatch: Milestone 1 Remediation Worker (m1_worker_2)

## Mission
Apply the verified remediation blueprints for Milestone 1 to eliminate all defects identified during Gate Iteration 1, sync the changes to WSL, execute migration and test suites, and deliver genuine, unadulterated verification reports.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Domain Skills:
   - `d:\ism\.agents\skills\frappe-app-dev\SKILL.md`
   - `d:\ism\.agents\skills\code-style\SKILL.md`
4. Remediation Blueprints:
   - `d:\ism\.agents\teamwork\m1_rem_explorer_1\handoff.md` (Schema permissions, duration validation, update population, on_trash sanitization & deletion guards)
   - `d:\ism\.agents\teamwork\m1_rem_explorer_2\handoff.md` & `d:\ism\.agents\teamwork\m1_rem_explorer_2\test_ism_settings.patch` (Test isolation in `test_ism_settings.py` and MariaDB 1020 elimination)
   - `d:\ism\.agents\teamwork\m1_rem_explorer_3\handoff.md` & `d:\ism\.agents\teamwork\m1_rem_explorer_3\fix_setup_migrate.patch` (Migration deadlock fix and seeder reordering in `ism/setup.py`)

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Detailed Tasks to Apply

### Task 1: Fix Database Migration Deadlock & Reorder Seeders in `ism/setup.py`
- Follow `d:\ism\.agents\teamwork\m1_rem_explorer_3\handoff.md` or apply `d:\ism\.agents\teamwork\m1_rem_explorer_3\fix_setup_migrate.patch` (or copy from `proposed_setup.py`).
- Add `frappe.db.commit()` after DDL in `setup_procurement_custom_fields()` and `setup_item_custom_fields()`.
- Add transaction boundaries around `backfill_service_package_models()`.
- In `after_migrate()`, place core scheduling seeders first:
  1. `ensure_ism_roles()`
  2. `setup_default_ism_settings()`
  3. `seed_equipment_tags()`
  4. `seed_schedule_entry_types()`
  Followed by ERPNext integrations and historical backfills.

### Task 2: Fix Test Isolation & Deadlock in `ism/ism/doctype/ism_settings/test_ism_settings.py`
- Follow `d:\ism\.agents\teamwork\m1_rem_explorer_2\handoff.md` or apply `d:\ism\.agents\teamwork\m1_rem_explorer_2\test_ism_settings.patch` (or copy from `proposed_test_ism_settings.py`).
- Subclass `ISMTestCase` instead of `unittest.TestCase`.
- In `setUp()`: `frappe.db.rollback()`, `frappe.clear_cache(doctype="ISM Settings")`, `setup_default_ism_settings()`, `frappe.db.commit()`, and dictionary snapshot `self._original_settings`.
- In `tearDown()`: rollback, clear cache, restore all non-system fields, `save(ignore_permissions=True)`, `frappe.db.commit()`, and `super().tearDown()`.
- In test methods: Call `frappe.db.rollback()` immediately after validation exception assertions.

### Task 3: Schema Permissions & Controller Validations
- Follow `d:\ism\.agents\teamwork\m1_rem_explorer_1\handoff.md`:
  1. `ism/ism/doctype/schedule_entry_type/schedule_entry_type.json`: Add `ISM Manager` role with full permissions (`create`, `read`, `write`, `delete`, `export`, `print`, `report`, `share`).
  2. `ism/ism/doctype/schedule_entry/schedule_entry.json`: Add `ISM Manager` role with full permissions.
  3. `ism/ism/doctype/schedule_entry/schedule_entry.py`:
     - Add `self.validate_estimated_duration()` in `validate()` throwing `frappe.ValidationError` if `cint(self.estimated_duration) <= 0`.
     - Add `self.populate_from_repair_order()` to `before_save()`.
  4. `ism/ism/doctype/service_bay/service_bay.py`:
     - Clean `on_trash()` filter by restricting status to `["Draft", "Scheduled", "In Progress"]` (removing non-existent `"Needs Review"`).
  5. `ism/ism/doctype/schedule_entry_type/schedule_entry_type.py`:
     - Implement `validate()` checking non-empty `type_name`.
     - Implement `on_trash()` guarding against deletion of standard seeded types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) and types referenced by existing `Schedule Entry` records.

## Synchronization & Execution Requirements
1. **Sync to WSL**:
   Execute from Windows:
   `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
2. **Execute Migration**:
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"`
3. **Execute M1 DocType Unit Test Suites**:
   - `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"`
   - `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"`
   - `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"`
   - `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"`
4. **Execute Settings Isolation Multi-Run (5x)**:
   Verify zero `OperationalError (1020)` across 5 consecutive runs:
   `wsl bash -c "for i in {1..5}; do docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings' || exit 1; done"`

## Strict Reporting Protocol
- **Zero Synthetic Summaries**: Never synthesize fake runner output lines.
- **Verbatim Terminal Output**: Copy and paste the EXACT stdout/stderr generated by the runner tools into code fences with timestamp and exit code.
- **Scope Clarification**: Differentiate Milestone 1 (all 4 DocTypes passing 100%) from any full repository test runs (`bench run-tests --app ism`), which are expected to show failures in future milestone test suites (M2–M5).
- Write completion report to `d:\ism\.agents\teamwork\m1_worker_2\handoff.md` and send message to orchestrator.


## 2026-10-26T15:55:18Z
You are m1_worker_2 (Milestone 1 Remediation Worker). Your working directory is d:\ism\.agents\teamwork\m1_worker_2.
Read your dispatch assignment at d:\ism\.agents\teamwork\m1_worker_2\DISPATCH.md and the authoritative requirements at d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md.
Load domain skills from d:\ism\.agents\skills\frappe-app-dev\SKILL.md and d:\ism\.agents\skills\code-style\SKILL.md.
Apply the exact blueprints from m1_rem_explorer_1, m1_rem_explorer_2, and m1_rem_explorer_3.
Sync changes to WSL using scripts\sync-to-wsl.ps1, run bench migrate, run all 4 M1 DocType test suites, and run the 5x sequential isolation test on ISM Settings.
Follow the strict verification reporting guidelines: document verbatim stdout/stderr terminal blocks with exit codes.
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A forensic auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
Write your handoff report to d:\ism\.agents\teamwork\m1_worker_2\handoff.md and send_message to orchestrator when complete.
