# Dispatch for Sub-Orchestrator: Milestone 1

You are the Sub-Orchestrator for Milestone 1 (Archetype: teamwork_preview_orchestrator).
Your working directory is: d:\ism\.agents\teamwork\sub_orch_m1
Project Root: d:\ism
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Survey Handoffs:
- d:\ism\.agents\teamwork\survey_induct_shop_1\handoff.md
- d:\ism\.agents\teamwork\survey_ism_codebase_2\handoff.md
- d:\ism\.agents\teamwork\survey_gap_analysis_3\handoff.md

## Scope: Resource & Capacity Data Models & Settings Consolidation (R3, R4)
1. **DocTypes to Create** (Standard DocTypes in `ism/ism/doctype/`):
   - `Service Bay`: `bay_name` (PK), `is_active` (Check), `description` (Small Text), `equipment` (Child table: `Service Bay Equipment`).
   - `Equipment Tag`: `tag_name` (PK), `description` (Small Text).
   - `Service Bay Equipment`: child table containing `equipment_tag` (Link: `Equipment Tag`).
   - `Schedule Entry Type`: `type_name` (PK), `color` (Color), `description` (Small Text), `requires_repair_order` (Check).
   - `Schedule Entry`: `naming_series` (`SE-.#####`), `entry_type` (Link), `repair_order` (Link to `Repair Order`), `customer` (Link to `Customer`), `provisional_customer_name` (Data), `provisional_phone` (Data), `provisional_email` (Data), `scheduled_date` (Date), `scheduled_time` (Time), `status` (Select: Draft, Scheduled, In Progress, Completed, Cancelled), `repair_vehicle` (Link to `Repair Vehicle`), `provisional_vehicle_info` (Data), `estimated_duration` (Int), `service_bay` (Link to `Service Bay`), `assigned_technician` (Link to `Employee`), `notes` (Small Text).
2. **Settings Consolidation**:
   - Update `ism/ism/doctype/ism_settings/ism_settings.json` with sections:
     - `sb_scheduling`: `operating_hours_start` (default 08:00:00), `operating_hours_end` (default 17:00:00), `break_start` (default 12:00:00), `break_end` (default 12:30:00), `default_slot_interval` (default 30), `scheduling_horizon_days` (default 30), `holiday_list` (Link: `Holiday List`).
     - `sb_capacity`: `technician_designation` (default "Technician"), `enable_technician_capacity` (default 1).
     - `sb_estimation`: `estimation_percentile` (default 80.0, range 50–95%), `estimation_sigma` (default 0.30, range 0.10–0.80), `buffer_padding_minutes` (default 0, range 0–30), `default_diagnostic_duration` (default 60).
3. **Data Seeding & Installation**:
   - In `ism/setup.py`:
     - Implement `seed_schedule_entry_types()`: seed `Diagnostic` (#1f538d, RO=0), `Repair` (#2e7d32, RO=1), `Maintenance` (#0284c7, RO=1), `Meeting` (#7b1fa2, RO=0), `Internal Service` (#ef6c00, RO=0).
     - Seed default `Equipment Tag`s: `Lift`, `EV High Voltage`, `Alignment Rack`.
     - Update `setup_default_ism_settings()` to initialize default values.
     - Hook these functions into `after_install` and `after_migrate` in `hooks.py`.
4. **Verification**:
   - Sync code to WSL2 (`powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`).
   - Run `bench --site development.localhost migrate` inside `devcontainer-frappe-1`.
   - Ensure all 78 existing ISM tests continue to pass cleanly.

## Execution Rules
- Follow the iteration loop (2B): Explorer -> Worker -> Reviewer -> Challenger -> Auditor -> Gate.
- Create `SCOPE.md`, `BRIEFING.md`, and `progress.md` in your working directory.
- When gate passes cleanly, write `handoff.md` and send completion message to Project Orchestrator (Conversation ID: 78c8d884-efd6-4032-aa90-94b4fc167978).
