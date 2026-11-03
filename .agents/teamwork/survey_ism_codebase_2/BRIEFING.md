# BRIEFING — 2026-10-26T14:34:30Z

## Mission
Investigate Repair Order DocType, intake workflow, provisional data handling, ISM Settings, test setup, and hooks/actions in the ISM codebase.

## 🔒 My Identity
- Archetype: teamwork_preview_explorer
- Roles: explorer, investigator, synthesizer
- Working directory: d:\ism\.agents\teamwork\survey_ism_codebase_2
- Original parent: 78c8d884-efd6-4032-aa90-94b4fc167978
- Milestone: codebase survey

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Rely on CodeGraph if .codegraph/ exists
- Keep metadata in working directory
- Communicate via send_message to parent

## Current Parent
- Conversation ID: 78c8d884-efd6-4032-aa90-94b4fc167978
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `ism/ism/doctype/repair_order/repair_order.json`, `.py`, `.js`, `test_repair_order.py`
  - `ism/ism/doctype/ism_settings/ism_settings.json`, `.py`
  - `ism/setup.py`, `ism/hooks.py`, `ism/tests/base.py`
  - `scripts/sync-to-wsl.ps1`, `scripts/sync-from-wsl.ps1`, `scripts/README.md`
  - `$env:TEMP\induct_shop` (`estimation_light.py`, `scheduling.py`, `technician_availability.py`, `sales_order.js`, doctypes `schedule_entry`, `service_bay`, `schedule_entry_type`, `equipment_tag`)
- **Key findings**:
  - Existing test suite in WSL2 container `devcontainer-frappe-1` (`development.localhost`) runs 78 tests in 35.98s, all passing.
  - Prepending `tab_appointment` before `tab_intake` is compatible with tab structure assertions in `test_repair_order_super_doctype_tabs_and_status_visibility`.
  - Conditional mandatory fields on `customer`, `vehicle`, and `odometer_in` via `mandatory_depends_on` and controller validation allow seamless early appointment capture (`Draft Appointment`, `Scheduled`).
  - Formalization workflow method `formalize_provisional_data` cleanly transitions provisional contact and vehicle data into master `Customer` and `Repair Vehicle` records.
  - Tunable duration estimation (P80 Fenton-Wilkinson log-normal summation) and shop hours/breaks/capacity gating integrate directly into `ISM Settings`.
- **Unexplored areas**: none within survey scope.

## Key Decisions Made
- Survey completed and documented in 5-component `handoff.md`.
- Test commands and verification methods established.

## Artifact Index
- DISPATCH.md — incoming dispatch records
- BRIEFING.md — persistent memory
- progress.md — liveness heartbeat
- handoff.md — 5-component handoff report
