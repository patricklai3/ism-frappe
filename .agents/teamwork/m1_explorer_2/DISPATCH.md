# Dispatch for M1 Explorer 2 (Schedule Entry & Schedule Entry Type Schemas)

You are M1 Explorer 2 (Archetype: teamwork_preview_spec_miner).
Your working directory is: d:\ism\.agents\teamwork\m1_explorer_2
Project Root: d:\ism
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Reference Survey: d:\ism\.agents\teamwork\survey_induct_shop_1\handoff.md

## Task: Milestone 1 - Schedule Entry & Schedule Entry Type Specification
Investigate and produce the exact schema specification and code implementation plan for:
1. `Schedule Entry Type` DocType (`ism/ism/doctype/schedule_entry_type/schedule_entry_type.json`):
   - Primary key: `type_name` (Data, Unique)
   - Fields: `type_name`, `color` (Color), `description` (Small Text), `requires_repair_order` (Check, default 0).
   - Seeding specification: `Diagnostic` (Color: #1f538d, RO=0), `Repair` (Color: #2e7d32, RO=1), `Maintenance` (Color: #0284c7, RO=1), `Meeting` (Color: #7b1fa2, RO=0), `Internal Service` (Color: #ef6c00, RO=0).
2. `Schedule Entry` DocType (`ism/ism/doctype/schedule_entry/schedule_entry.json`):
   - Naming: `autoname: "naming_series:"`, `naming_series: "SE-.#####"`
   - Fields:
     - `entry_type` (Link: `Schedule Entry Type`, reqd: 1)
     - `repair_order` (Link: `Repair Order`) — NOT unique! Must support 1:N cardinality.
     - `customer` (Link: `Customer`)
     - Provisional contact: `provisional_customer_name` (Data), `provisional_phone` (Data), `provisional_email` (Data)
     - `scheduled_date` (Date, reqd: 1)
     - `scheduled_time` (Time, reqd: 1)
     - `estimated_duration` (Int, reqd: 1, default 60)
     - `status` (Select: `Draft\nScheduled\nIn Progress\nCompleted\nCancelled`, default `Draft`)
     - `service_bay` (Link: `Service Bay`)
     - `assigned_technician` (Link: `Employee`)
     - `repair_vehicle` (Link: `Repair Vehicle`)
     - `provisional_vehicle_info` (Data)
     - `notes` (Small Text)
   - Title field / search fields setup for Desk navigation.
3. Seeding logic in `setup.py` and hook registration in `hooks.py`.

Write your full findings and concrete file-by-file implementation plan in:
`d:\ism\.agents\teamwork\m1_explorer_2\handoff.md`
and notify the orchestrator via send_message.


## 2026-10-26T14:39:28Z
You are M1 Explorer 2 (Archetype: teamwork_preview_spec_miner).
Your working directory is: d:\ism\.agents\teamwork\m1_explorer_2
Authoritative requirements: d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
Project plan: d:\ism\PROJECT.md
Dispatch file: d:\ism\.agents\teamwork\m1_explorer_2\DISPATCH.md

Investigate and produce the exact schema specification and code implementation plan for:
1. `Schedule Entry Type` DocType (`ism/ism/doctype/schedule_entry_type/schedule_entry_type.json`) with seeded types: Diagnostic, Repair, Maintenance, Meeting, Internal Service.
2. `Schedule Entry` DocType (`ism/ism/doctype/schedule_entry/schedule_entry.json`) with naming series SE-.#####, 1:N link to Repair Order, provisional contact/vehicle fields, bay and tech links.
3. Seeding logic in setup.py and hooks.py.
Document your full findings and implementation plan in `d:\ism\.agents\teamwork\m1_explorer_2\handoff.md` and send a completion message to the orchestrator.
