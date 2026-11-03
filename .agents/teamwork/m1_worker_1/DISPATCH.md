# Dispatch for M1 Worker 1: Resource & Capacity Data Models & Settings Consolidation

You are M1 Worker 1 (Archetype: teamwork_preview_worker).
Your working directory is: d:\ism\.agents\teamwork\m1_worker_1
Project Root: d:\ism
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Detailed Explorer Specifications (Read These First!)
- d:\ism\.agents\teamwork\m1_explorer_1\handoff.md (Service Bay, Equipment Tag, Service Bay Equipment schemas, controller validations, tests, and tag seeding)
- d:\ism\.agents\teamwork\m1_explorer_2\handoff.md (Schedule Entry, Schedule Entry Type schemas, controller validations, tests, and type seeding)
- d:\ism\.agents\teamwork\m1_explorer_3\handoff.md (ISM Settings schema, controller validations, setup_default_ism_settings, and tests)

## Scope & Exclusive File Ownership
You exclusively own and will implement the following files in `d:\ism`:
1. `ism/ism/doctype/equipment_tag/equipment_tag.json`
2. `ism/ism/doctype/equipment_tag/equipment_tag.py`
3. `ism/ism/doctype/equipment_tag/__init__.py`
4. `ism/ism/doctype/service_bay_equipment/service_bay_equipment.json`
5. `ism/ism/doctype/service_bay_equipment/service_bay_equipment.py`
6. `ism/ism/doctype/service_bay_equipment/__init__.py`
7. `ism/ism/doctype/service_bay/service_bay.json`
8. `ism/ism/doctype/service_bay/service_bay.py`
9. `ism/ism/doctype/service_bay/__init__.py`
10. `ism/ism/doctype/service_bay/test_service_bay.py`
11. `ism/ism/doctype/schedule_entry_type/schedule_entry_type.json`
12. `ism/ism/doctype/schedule_entry_type/schedule_entry_type.py`
13. `ism/ism/doctype/schedule_entry_type/__init__.py`
14. `ism/ism/doctype/schedule_entry_type/test_schedule_entry_type.py`
15. `ism/ism/doctype/schedule_entry/schedule_entry.json`
16. `ism/ism/doctype/schedule_entry/schedule_entry.py`
17. `ism/ism/doctype/schedule_entry/__init__.py`
18. `ism/ism/doctype/schedule_entry/test_schedule_entry.py`
19. `ism/ism/doctype/ism_settings/ism_settings.json`
20. `ism/ism/doctype/ism_settings/ism_settings.py`
21. `ism/ism/doctype/ism_settings/test_ism_settings.py`
22. `ism/setup.py` (Add `seed_equipment_tags()`, `ensure_ism_roles()`, `seed_schedule_entry_types()`, update `setup_default_ism_settings()`, and call them in `after_migrate()`)
23. `ism/hooks.py` (Ensure `after_install = "ism.setup.after_install"` and `after_migrate = "ism.setup.after_migrate"` are present)

## Verification Requirements
After writing all files:
1. Sync files to WSL:
   `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
2. Run database migration in Docker container:
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost migrate"`
3. Run the unit tests for the new DocTypes:
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Service Bay'"`
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry Type'"`
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'"`
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'"`
4. Run the full ISM test suite and ensure all 78+ tests pass:
   `wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"`

Document your changes and exact test results in `d:\ism\.agents\teamwork\m1_worker_1\handoff.md` and send a completion message to the Project Orchestrator (Conversation ID: 78c8d884-efd6-4032-aa90-94b4fc167978).


## 2026-10-26T14:49:57Z
You are M1 Worker 1 (Archetype: teamwork_preview_worker).
Your working directory is: d:\ism\.agents\teamwork\m1_worker_1
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Dispatch file: d:\ism\.agents\teamwork\m1_worker_1\DISPATCH.md
Explorer Specifications:
- d:\ism\.agents\teamwork\m1_explorer_1\handoff.md
- d:\ism\.agents\teamwork\m1_explorer_2\handoff.md
- d:\ism\.agents\teamwork\m1_explorer_3\handoff.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Implement the data models, schemas, controllers, unit tests, and seeding for Milestone 1:
- Service Bay, Equipment Tag, Service Bay Equipment
- Schedule Entry Type, Schedule Entry
- ISM Settings scheduling/capacity/estimation parameters
- Seeding routines in setup.py and hooks.py
Follow Frappe conventions and code style.
Sync files to WSL via `scripts\sync-to-wsl.ps1`, execute `bench migrate`, and run the automated test suites inside container `devcontainer-frappe-1`.
Document all changes and test outputs in `d:\ism\.agents\teamwork\m1_worker_1\handoff.md` and send a completion message to the Project Orchestrator.
