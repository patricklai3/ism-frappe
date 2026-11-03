# Dispatch for M1 Remediation Explorer 1

You are M1 Remediation Explorer 1 (Archetype: teamwork_preview_spec_miner).
Your working directory is: d:\ism\.agents\teamwork\m1_rem_explorer_1
Project Root: d:\ism
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Reviewer 1 Report: d:\ism\.agents\teamwork\m1_reviewer_1\handoff.md

## Task: Milestone 1 Remediation - Schema Permissions & Controller Validations
Formulate exact code fixes for the following defects identified by Reviewer 1:
1. **Missing `ISM Manager` Role Permissions**:
   - In `ism/ism/doctype/schedule_entry/schedule_entry.json`: Add `ISM Manager` role with full CRUD permissions (`create`, `read`, `write`, `delete`, `export`, `print`, `report`, `share`).
   - In `ism/ism/doctype/schedule_entry_type/schedule_entry_type.json`: Add `ISM Manager` role with full CRUD permissions.
2. **Missing `estimated_duration > 0` Validation**:
   - In `ism/ism/doctype/schedule_entry/schedule_entry.py`: In `validate()`, add check: if `cint(self.estimated_duration) <= 0`, raise `frappe.ValidationError(_("Estimated Duration must be greater than 0 minutes."))`.
3. **`populate_from_repair_order` on Update**:
   - In `ism/ism/doctype/schedule_entry/schedule_entry.py`: Call `self.populate_from_repair_order()` in `before_save()` (populating only empty fields so customer edits aren't overwritten).
4. **Invalid Status in `ServiceBay.on_trash`**:
   - In `ism/ism/doctype/service_bay/service_bay.py`: Change status filter from `["Draft", "Scheduled", "Needs Review", "In Progress"]` to `["Draft", "Scheduled", "In Progress"]`.
5. **Deletion Guard on Standard Entry Types**:
   - In `ism/ism/doctype/schedule_entry_type/schedule_entry_type.py`: Add `on_trash()` preventing deletion of standard types (`Diagnostic`, `Repair`, `Maintenance`, `Meeting`, `Internal Service`) or types referenced by existing `Schedule Entry` records.

Provide complete, tested file blueprints in:
`d:\ism\.agents\teamwork\m1_rem_explorer_1\handoff.md`
and notify the orchestrator via send_message.


## 2026-10-26T15:43:03Z
[Message] timestamp=2026-10-26T15:43:03Z sender=78c8d884-efd6-4032-aa90-94b4fc167978 priority=MESSAGE_PRIORITY_HIGH content=You are M1 Remediation Explorer 1 (Archetype: teamwork_preview_spec_miner).
Your working directory is: d:\ism\.agents\teamwork\m1_rem_explorer_1
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Reviewer 1 Report: d:\ism\.agents\teamwork\m1_reviewer_1\handoff.md
Dispatch file: d:\ism\.agents\teamwork\m1_rem_explorer_1\DISPATCH.md

Formulate exact schema and controller fixes for Milestone 1:
1. Add `ISM Manager` role to permissions in `schedule_entry.json` and `schedule_entry_type.json`.
2. Add `cint(self.estimated_duration) > 0` validation in `schedule_entry.py`.
3. Add `self.populate_from_repair_order()` to `before_save()` in `schedule_entry.py`.
4. Fix status options in `ServiceBay.on_trash()`.
5. Add deletion guard on standard seeded entry types in `schedule_entry_type.py`.
Document full blueprints in `d:\ism\.agents\teamwork\m1_rem_explorer_1\handoff.md` and send a completion message to the Project Orchestrator.
