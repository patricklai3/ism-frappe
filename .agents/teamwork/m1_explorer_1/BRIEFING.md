# BRIEFING — 2026-10-26T14:49:00Z

## Mission
Investigate and produce the exact schema specification and code implementation plan for Service Bay, Equipment Tag, Service Bay Equipment, and default tag seeding logic.

## 🔒 My Identity
- Archetype: teamwork_preview_spec_miner
- Roles: Specification Miner
- Working directory: d:\ism\.agents\teamwork\m1_explorer_1
- Original parent: 78c8d884-efd6-4032-aa90-94b4fc167978
- Milestone: Milestone 1 (M1)

## 🔒 Key Constraints
- Specification Miner only: do NOT implement anything (read-only).
- Thoroughly document features in standard table format with edge cases.
- Write only to working directory: d:\ism\.agents\teamwork\m1_explorer_1.
- Compare against induct_shop reference implementation and match ISM conventions.

## Current Parent
- Conversation ID: 78c8d884-efd6-4032-aa90-94b4fc167978
- Updated: 2026-10-26T14:39:28Z

## Task Summary
- **What to build**: Exact schema specifications (JSON schemas, Python controllers, permission models) and implementation plan for:
  1. `Service Bay` DocType (`ism/ism/doctype/service_bay/service_bay.json`)
  2. `Equipment Tag` DocType (`ism/ism/doctype/equipment_tag/equipment_tag.json`)
  3. `Service Bay Equipment` Child DocType (`ism/ism/doctype/service_bay_equipment/service_bay_equipment.json`)
  4. Seeding logic for default tags: Lift, EV High Voltage, Alignment Rack.
- **Success criteria**: Complete, validated field definitions, controllers, test plans, and seeding hooks ready for implementation agent.
- **Interface contracts**: PROJECT.md
- **Code layout**: PROJECT.md § Code Layout

## Key Decisions Made
- Module name must be `"ISM"` (matching `modules.txt` and existing DocTypes).
- Primary keys `bay_name` and `tag_name` use `naming_rule: "By fieldname"` and `autoname: "field:<fieldname>"`.
- Permissions: Full CRUD for `System Manager` and `ISM Manager`; Read & Report for Desk users (`Maintenance User`, `Sales User`).
- Role `ISM Manager` seeded in `setup.py` with `desk_access: 1`.
- Tags `Lift`, `EV High Voltage`, `Alignment Rack` seeded idempotently in `setup.py` and called from `after_migrate()` and `after_install()`.
- Wire `after_migrate = "ism.setup.after_migrate"` in `ism/hooks.py`.
- Implement validation in `ServiceBay.validate()` to reject duplicate tags in child table.
- Implement deletion protection in `EquipmentTag.on_trash()` to reject deleting tags in active use by service bays.

## Artifact Index
- `d:\ism\.agents\teamwork\m1_explorer_1\handoff.md` — Final specification & implementation plan
- `d:\ism\.agents\teamwork\m1_explorer_1\progress.md` — Liveness & progress tracker
- `d:\ism\.agents\teamwork\m1_explorer_1\DISPATCH.md` — Dispatch logs

## Loaded Skills
- **Source**: d:\ism\.agents\skills\frappe-app-dev\SKILL.md
- **Local copy**: d:\ism\.agents\teamwork\m1_explorer_1\skills\frappe-app-dev\SKILL.md
- **Core methodology**: Frappe full-stack DocType schemas, controllers, permissions, migrations, and bench patterns.
