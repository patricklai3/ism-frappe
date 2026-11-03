# Dispatch: Project Orchestrator (Generation 6)

## 2026-10-27 UTC

**Agent**: `orchestrator_6` (Generation 6 Project Orchestrator)  
**Parent**: Sentinel (conversation ID: `60775dab-9c68-4525-b3c1-55e1186e125b`)  
**Workspace**: `d:\ism\.agents\teamwork\orchestrator_6`  

### Mission:
Resume leadership from `orchestrator_5`. Authoritative files to read:
- `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`
- `d:\ism\PROJECT.md`
- `d:\ism\TEST_READY.md`
- `d:\ism\.agents\teamwork\orchestrator_5\BRIEFING.md`
- `d:\ism\.agents\teamwork\orchestrator_5\progress.md`
- `d:\ism\.agents\teamwork\m2_it1_worker_1\handoff.md`

### Project Status:
1. **Milestone 1**: **DONE** (Standard DocTypes, ISM Settings consolidation, transaction isolation, clean autoname fallbacks, idempotent seeders).
2. **Milestone 2**: **IMPLEMENTATION COMPLETE** (Tunable Estimation Engine & Pure Math Core).
   - Worker `m2_it1_worker_1` delivered:
     - `ism/scheduling/estimation_light.py`: Pure-Python zero-scipy Abramowitz & Stegun inverse CDF approximation, category sigma profiles (Brake 0.20, Tire 0.15, Suspension 0.35, Electrical 0.50, Diagnostic 0.40, Body 0.30, HVAC 0.25, Drivetrain 0.40, Default 0.30), log-normal P80 duration estimation, and multi-operation Fenton-Wilkinson summation.
     - `ism/api/estimation_service.py`: Whitelisted endpoints `get_estimate` and `get_total_estimate` reading dynamic `ISM Settings` without module caching, converting `Item.custom_frt` hours to minutes, returning pure estimate + buffer padding.
     - `ism/setup.py`: `custom_frt` custom field on `Item`.
     - Verification: 41/41 estimation math tests pass, 381/381 full suite pass.

### Immediate Objectives for Gen 6:
1. **Initialize Workspace**:
   - Create `BRIEFING.md` and `progress.md` in `d:\ism\.agents\teamwork\orchestrator_6`.
   - Start 10-minute heartbeat cron.
2. **Milestone 2 Gate Verification**:
   - Dispatch the 5-agent Gate verification panel:
     - 2 Reviewers (`teamwork_preview_reviewer`): Review math core, algorithms, API service, settings integration, and test suite.
     - 2 Challengers (`teamwork_preview_challenger`): Challenge numerical precision, boundary stress, dynamic settings, and regression.
     - 1 Forensic Auditor (`teamwork_preview_auditor`): Independent forensic integrity audit on Milestone 2 deliverables.
   - Enforce binary veto on Auditor (`CLEAN` required). All reviewers and challengers must `APPROVE`.
   - Upon Gate PASS: Update `PROJECT.md` Milestone 2 status to `DONE`.
3. **Advance to Milestone 3 (Dual-Resource Capacity & Scheduling APIs)**:
   - Server-side APIs in `ism/api/scheduling.py` and `ism/api/technician_availability.py`:
     - Lunch-aware effective end times (extending job duration when spanning lunch break).
     - Bay availability & overlap validation (blocking double-booked bays).
     - Equipment tag filtering & auto bay assignment.
     - Slot generation with bay/tech capacity indicators and bottleneck reporting.
     - Technician designation roster and HRMS Leave Application integration.
     - Schedule Entry controller validations enforcing bay and technician limits.
   - Verify via `ism/tests/test_scheduling_capacity.py` (53 tests).
   - Run Gate verification and mark Milestone 3 `DONE`.
4. **Advance to Milestone 4 (Repair Order Integration & Early Appointment Lifecycle)**:
   - `Repair Order` schema: `tab_appointment` initial tab break, provisional fields (name, phone, email, vehicle notes), early status workflow (`Draft Appointment`, `Scheduled`, `Checked In`, `No Show`), formalization method `formalize_provisional_info()`, "Schedule Appointment" button.
   - Verify via `ism/tests/test_ro_scheduling_e2e.py` (17 tests).
   - Run Gate verification and mark Milestone 4 `DONE`.
5. **Advance to Milestone 5 (Reusable Scheduling Modal & Frappe Calendar View)**:
   - Client-side modal `ism.SchedulingDialog` (`scheduling_modal.js`) with date navigator, holiday awareness, bay/tech capacity bars, equipment filter, conflict warnings.
   - Frappe Calendar View for `Schedule Entry` color-coded by entry type/status.
   - Run Gate verification and mark Milestone 5 `DONE`.
6. **Advance to Milestone 6 (E2E Test Suite 100% Pass & Adversarial Hardening)**:
   - Full repository test execution (`bench --site development.localhost run-tests --app ism` -> 100% pass across all tests with 0 errors/failures).
   - Adversarial stress suite execution.
7. **Final Handoff**:
   - Report final completion to Sentinel. Sentinel will spawn the independent post-victory auditor before user delivery.
