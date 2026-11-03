# Dispatch for M1 Explorer 3 (ISM Settings & Seeding Migration)

You are M1 Explorer 3 (Archetype: teamwork_preview_explorer).
Your working directory is: d:\ism\.agents\teamwork\m1_explorer_3
Project Root: d:\ism
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Reference Survey: d:\ism\.agents\teamwork\survey_ism_codebase_2\handoff.md

## Task: Milestone 1 - ISM Settings Consolidation & Seeding Migration
Investigate and produce the exact schema specification and code implementation plan for:
1. `ISM Settings` DocType (`ism/ism/doctype/ism_settings/ism_settings.json`):
   - Current fields: `sb_approval_settings` and `sb_sms_settings`.
   - Add sections and fields:
     - Section Break: `sb_scheduling` (Label: "Scheduling & Shop Hours")
       - `operating_hours_start` (Time, default `08:00:00`)
       - `operating_hours_end` (Time, default `17:00:00`)
       - `break_start` (Time, default `12:00:00`)
       - `break_end` (Time, default `12:30:00`)
       - `default_slot_interval` (Int, default `30`)
       - `scheduling_horizon_days` (Int, default `30`)
       - `holiday_list` (Link: `Holiday List`)
     - Section Break: `sb_capacity` (Label: "Resource Capacity")
       - `technician_designation` (Data, default `Technician`)
       - `enable_technician_capacity` (Check, default `1`)
     - Section Break: `sb_estimation` (Label: "Duration Estimation Tuning")
       - `estimation_percentile` (Float/Percent, default `80.0`)
       - `estimation_sigma` (Float, default `0.30`)
       - `buffer_padding_minutes` (Int, default `0`)
       - `default_diagnostic_duration` (Int, default `60`)
2. `ism/setup.py`:
   - Inspect existing `setup_default_ism_settings()`.
   - Add initialization for all new scheduling, capacity, and estimation fields.
   - Verify `after_migrate` / `after_install` hooks in `hooks.py`.
3. Verification:
   - Migration safety check (`bench migrate`).
   - Sync script mechanism (`scripts/sync-to-wsl.ps1`).

Write your full findings and concrete file-by-file implementation plan in:
`d:\ism\.agents\teamwork\m1_explorer_3\handoff.md`
and notify the orchestrator via send_message.


## 2026-10-26T14:39:28Z
You are M1 Explorer 3 (Archetype: teamwork_preview_explorer).
Your working directory is: d:\ism\.agents\teamwork\m1_explorer_3
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Dispatch file: d:\ism\.agents\teamwork\m1_explorer_3\DISPATCH.md

Investigate and produce the exact schema specification and code implementation plan for:
1. `ISM Settings` DocType (`ism/ism/doctype/ism_settings/ism_settings.json`): adding sections sb_scheduling, sb_capacity, and sb_estimation with all required parameters.
2. `ism/setup.py`: default initialization of scheduling, capacity, and estimation fields.
3. Migration safety check and sync verification with WSL2 container.
Document your full findings and implementation plan in `d:\ism\.agents\teamwork\m1_explorer_3\handoff.md` and send a completion message to the orchestrator.
