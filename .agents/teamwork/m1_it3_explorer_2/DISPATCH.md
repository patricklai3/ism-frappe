# Dispatch: M1 Iteration 3 Explorer 2 (Security, RBAC, & Standard Type Protection)

## Mission
Formulate exact remediation blueprints for the security vulnerabilities and RBAC gaps discovered by `m1_reviewer_4`.

## Mandatory Reading & Authoritative References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. Reviewer Report: `d:\ism\.agents\teamwork\m1_reviewer_4\handoff.md`

## Focus Areas & Specific Tasks

### Task 1: Prevent Standard Schedule Entry Type Renaming Bypass
- Finding: `schedule_entry_type.json` specifies `"allow_rename": 1`. An actor can rename `Diagnostic` (e.g. to `Diagnostic_Old`) via `frappe.rename_doc` and then delete it, bypassing `on_trash()` because `self.name` is no longer `"Diagnostic"`.
- Investigate the cleanest fix:
  - Option A: Change `"allow_rename": 0` in `schedule_entry_type.json`.
  - Option B: Add `before_rename(self, old_name, new_name, merge=False)` hook in `ScheduleEntryType` controller throwing `frappe.ValidationError` if `old_name in STANDARD_SCHEDULE_ENTRY_TYPES`.
  - Recommend the optimal approach (or both for defense-in-depth).

### Task 2: Add `ISM Manager` Role Permissions to `ISM Settings`
- Finding: `ism/ism/doctype/ism_settings/ism_settings.json` permissions only include `System Manager`.
- Automotive shop managers (`ISM Manager`) cannot view or configure shop operating hours, lunch breaks, slot intervals, or capacity toggles in Frappe Desk.
- Formulate the exact JSON block granting `read: 1, write: 1` permissions to `ISM Manager` in `ism_settings.json`.

## Output Requirements
Deliver a concrete, line-by-line blueprint, replacement code, and diff patch in `d:\ism\.agents\teamwork\m1_it3_explorer_2\handoff.md` and message the orchestrator when complete.

## 2026-10-26T16:27:01Z
You are m1_it3_explorer_2 (M1 It3 Security & Permissions Spec Miner 2). Your working directory is d:\ism\.agents\teamwork\m1_it3_explorer_2.
Read your dispatch instructions at d:\ism\.agents\teamwork\m1_it3_explorer_2\DISPATCH.md and the authoritative requirements at d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md.
Review m1_reviewer_4's report at d:\ism\.agents\teamwork\m1_reviewer_4\handoff.md.
Formulate exact blueprints and diffs for:
1. Blocking rename circumvention of standard entry types in schedule_entry_type.
2. Adding ISM Manager role permissions to ism_settings.json.
Deliver your blueprint and diff patches in d:\ism\.agents\teamwork\m1_it3_explorer_2\handoff.md and send_message to orchestrator when complete.
