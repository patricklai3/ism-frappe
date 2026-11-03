# Test Infrastructure & Specification Reference

## 1. Executive Summary & Test Philosophy

This document defines the test architecture, methodology, and verification contracts for the ISM Dual-Resource Capacity Scheduling Integration.

The test suite is built on a **requirement-driven, opaque-box E2E philosophy**:
- **Requirements-First Derivation**: Tests are derived strictly from business requirements in `ORIGINAL_REQUEST.md` and `PROJECT.md § Feature Inventory` (31 features), without assuming or coupling to internal implementation quirks.
- **Authoritative Expected Outputs**: Expected values are mathematically derived from standard log-normal probability theory, Abramowitz & Stegun inverse normal approximations, and the reference implementation in `induct_shop`.
- **Progressive Testability**: Test modules are designed with defensive import and feature gating (`unittest.SkipTest`), ensuring the test suite compiles and runs cleanly at any development stage without breaking existing test suites, while automatically activating as milestones are implemented.
- **Strict Data Hygiene**: Tests inherit from `ISMTestCase` and use centralized fixtures in `ism/tests/test_fixtures.py`, guaranteeing transactional teardown in reverse dependency order, singleton settings restoration, and zero leakage of test records into Desk list views.

---

## 2. 31-Feature Inventory & Tier Mapping Matrix

| # | Feature | Req | Tier 1 (Coverage) | Tier 2 (Boundary/Edge) | Tier 3 (Cross-Feature) | Tier 4 (Scenario) | Test Module |
|---|---|---|---|---|---|---|---|
| 1 | Analytic Log-Normal P80 | R4 | `test_p80_standard_durations` | `test_p80_negative_zero_frt` | Multi-op + Buffer | S4 | `test_estimation_math.py` |
| 2 | Fenton-Wilkinson Summation | R4 | `test_fw_sum_diverse_ops` | `test_fw_sum_empty_single_op` | F-W + Bay Equip | S4 | `test_estimation_math.py` |
| 3 | Category Sigma Profiles | R4 | `test_category_sigmas` | `test_category_unknown_none` | Category + F-W Sum | S4 | `test_estimation_math.py` |
| 4 | Rational Z-Score Approx | R4 | `test_z_score_standard_percentiles` | `test_z_score_monotonicity_bounds` | Custom P80/P90 + Sigma | S4 | `test_estimation_math.py` |
| 5 | Configurable Buffer Padding | R4 | `test_buffer_padding_addition` | `test_buffer_zero_extreme` | Buffer + Estimation Service | S1, S4 | `test_estimation_math.py` |
| 6 | Lunch-Aware Effective End Time | R5 | `test_lunch_aware_spanning` | `test_lunch_boundary_exact_edges` | Lunch + Bay Overlap | S1, S4 | `test_scheduling_capacity.py` |
| 7 | Per-Bay Overlap Validation | R5 | `test_bay_overlap_blocked` | `test_bay_self_exclusion_reschedule` | Overlap + Pool Limit | S1, S4 | `test_scheduling_capacity.py` |
| 8 | Equipment Tag Filtering | R2 | `test_equipment_tag_matching` | `test_bay_missing_tag_exclusion` | Tag + Auto Bay + Slot | S2 | `test_scheduling_capacity.py` |
| 9 | Automated Bay Assignment | R5 | `test_auto_assign_free_bay` | `test_auto_assign_full_capacity_raises` | Auto Bay + Tech Pool | S2 | `test_scheduling_capacity.py` |
| 10 | Slot Generator Capacity Indicators | R2 | `test_slot_generator_hours` | `test_slot_generator_lunch_excluded` | Dual Capacity Bottleneck | S2, S4 | `test_scheduling_capacity.py` |
| 11 | Technician Designation Roster | R5 | `test_technician_roster_active` | `test_technician_roster_inactive_filtered` | Roster + Leave Check | S3 | `test_scheduling_capacity.py` |
| 12 | HRMS Leave Application Integration | R5 | `test_leave_integration_approved` | `test_leave_multi_day_and_pending` | Leave + Slot Capacity | S3 | `test_scheduling_capacity.py` |
| 13 | Technician Pool-Level Gating | R5 | `test_tech_pool_capacity_gating` | `test_tech_pool_exhaustion_blocked` | Pool + Bay Availability | S1, S4 | `test_scheduling_capacity.py` |
| 14 | Individual Tech Overlap Check | R5 | `test_tech_individual_overlap_warning` | `test_tech_overlap_self_exclusion` | Tech + Bay Dual Overlap | S1, S3 | `test_scheduling_capacity.py` |
| 15 | Master Capacity Toggle | R4 | `test_master_capacity_toggle_enabled` | `test_master_capacity_toggle_fallback` | Toggle + Pool Validation | S1 | `test_scheduling_capacity.py` |
| 16 | Technician Queue & Utilization | R5 | `test_tech_queue_timeline` | `test_tech_utilization_zero_full` | Queue + Daily Overview | S3 | `test_scheduling_capacity.py` |
| 17 | Daily Technician Overview | R5 | `test_daily_tech_overview_metrics` | `test_daily_overview_unassigned` | Overview + Bottleneck | S3 | `test_scheduling_capacity.py` |
| 18 | ERPNext Holiday Awareness | R4 | `test_holiday_list_detection` | `test_holiday_missing_list_fallback` | Holiday + Slot Generator | S3 | `test_scheduling_capacity.py` |
| 19 | Service Bay DocType | R3 | `test_service_bay_crud` | `test_service_bay_inactive_filtering` | Bay + Child Equipment | S2 | `test_scheduling_capacity.py` |
| 20 | Equipment Tag System | R3 | `test_equipment_tag_crud` | `test_bay_equipment_child_table` | Tag Match + Auto Assign | S2 | `test_scheduling_capacity.py` |
| 21 | Schedule Entry Type DocType | R3 | `test_schedule_entry_type_seed` | `test_entry_type_color_and_non_ro` | Entry Type + Calendar | S1, S4 | `test_scheduling_capacity.py` |
| 22 | Schedule Entry DocType | R3 | `test_schedule_entry_crud` | `test_schedule_entry_validation_rules` | Schedule Entry + RO Link | S1, S4 | `test_scheduling_capacity.py` |
| 23 | Reusable Modal Date Navigator | R2 | `test_modal_date_navigation` | `test_modal_date_holiday_alert` | Date Nav + Slot Refresh | S1, S3 | `test_ro_scheduling_e2e.py` |
| 24 | Header Badges | R2 | `test_header_badge_duration_calc` | `test_header_badge_equipment_tags` | Badge + Estimation API | S1, S2 | `test_ro_scheduling_e2e.py` |
| 25 | Slot Selection Grid | R2 | `test_slot_grid_capacity_payload` | `test_slot_grid_boundary_slots` | Slot Grid + Dual Resource | S1, S2 | `test_ro_scheduling_e2e.py` |
| 26 | Tech Preview & Conflict Warnings | R2 | `test_tech_preview_payload` | `test_tech_conflict_warning_messages` | Preview + Booking Submit | S1, S3 | `test_ro_scheduling_e2e.py` |
| 27 | Calendar View Integration | R5 | `test_calendar_events_payload` | `test_calendar_lunch_gap_badges` | Calendar + Entry Types | S4 | `test_ro_scheduling_e2e.py` |
| 28 | List View Quick Filters | R5 | `test_list_view_filter_presets` | `test_list_view_unassigned_filter` | Filter + Status Transitions | S1 | `test_ro_scheduling_e2e.py` |
| 29 | Early Appointment Stages on RO | R1 | `test_ro_early_appointment_stages` | `test_ro_early_relaxed_mandatories` | Early RO + Schedule Entry | S1 | `test_ro_scheduling_e2e.py` |
| 30 | Provisional Intake Formalization | R1 | `test_ro_formalize_provisional_info` | `test_ro_formalize_existing_masters` | Formalize + Linked SE Sync | S1, S2 | `test_ro_scheduling_e2e.py` |
| 31 | Supplemental Scheduling | R1 | `test_supplemental_scheduling_active_ro` | `test_multi_se_per_ro_distinct_bays` | Supplemental + Checkout | S1 | `test_ro_scheduling_e2e.py` |

---

## 3. Test Architecture & Directory Layout

```
d:\ism\
├── ism\
│   └── tests\
│       ├── base.py                         # ISMTestCase base class with transactional hygiene
│       ├── cleanup.py                      # Purge routines for test-prefixed records
│       ├── test_fixtures.py                # Centralized scheduling master fixtures & settings helpers
│       ├── test_estimation_math.py         # Features 1–5: Pure log-normal math, F-W, Z-score, buffer padding
│       ├── test_scheduling_capacity.py     # Features 6–22: Dual resource scheduling, bays, technicians, pool gating
│       ├── test_ro_scheduling_e2e.py       # Features 23–31: RO early lifecycle, formalization, supplemental, modal/cal APIs
│       ├── test_estimation.py              # Backward-compatible proxy to test_estimation_math
│       ├── test_scheduling.py              # Backward-compatible proxy to test_scheduling_capacity
│       ├── test_repair_order_scheduling.py # Backward-compatible proxy to test_ro_scheduling_e2e
│       ├── test_parts_management.py        # Existing parts suite (retained)
│       └── test_tesla_integration.py      # Existing Tesla integration suite (retained)
├── TEST_INFRA.md                           # This document
└── TEST_READY.md                           # Suite verification & readiness report
```

---

## 4. Four-Tier Test Design Methodology

### Tier 1 — Feature Coverage (>=5 Tests per Feature)
Every feature is validated across representative positive inputs under standard conditions:
- **Estimation Math**: Standard flat-rate times (15m, 30m, 60m, 90m, 120m) at P80 produce exact integer ceiling values.
- **Scheduling**: Appointments within operating hours, free bays, on-duty technicians, standard slot intervals.
- **Repair Order**: Creating RO in `Draft Appointment`, booking initial `Schedule Entry`, verifying bidirectional linkage.

### Tier 2 — Boundary & Corner Cases (>=5 Tests per Feature)
Robustness under edge conditions and adversarial inputs:
- **Numerical Edge Cases**: Flat-rate time = 0, negative values, extreme percentiles (0.50, 0.99), extreme variance sigmas.
- **Time Boundary Edges**: Appointments starting or ending precisely at shop open, shop close, or lunch break boundaries (`12:00:00`, `12:30:00`).
- **Capacity Exhaustion**: Booking the last available bay, booking the last on-duty technician, and verifying the subsequent booking is strictly rejected.
- **Rescheduling & Self-Exclusion**: Verifying an entry can be rescheduled to its own time slot without self-collision validation errors.
- **Data Integrity**: Missing customer, missing vehicle, unassigned technician, provisional information with special characters and whitespace.

### Tier 3 — Cross-Feature Combinations
Pairwise and multi-variable feature interactions:
- **Multi-Operation Estimation + Buffer + Bay Tag Matching**: Summing multi-operation repairs with category-specific sigmas, applying shop buffer padding, matching required equipment tags to specialized bays (e.g. EV High Voltage + Alignment Rack).
- **Lunch Break Spanning + Dual Resource Gating**: Long repair job spanning shop lunch break, extending effective end time by break duration, and simultaneously occupying both physical bay and technician pool capacity over the entire extended duration.
- **Technician Leave + Pool Capacity Boundary**: Reducing technician pool by approved leave applications, verifying that total concurrent bookings on the floor cannot exceed remaining on-duty staff.

### Tier 4 — Real-World Application Scenarios

#### Scenario 1: Complete Appointment-to-Intake-to-Release Lifecycle
1. Customer calls shop with provisional details (John Doe, 555-0199, john@example.com, Tesla Model 3 squeaking brakes).
2. Service Advisor opens `Repair Order` form in `Draft Appointment` status and schedules initial Diagnostic appointment via scheduling dialog for `Test Bay 1`.
3. Customer arrives at shop. Status advances to `Checked In`.
4. Service Advisor triggers `formalize_provisional_info()`. Master `Customer` and `Repair Vehicle` records are created/resolved, and the linked `Schedule Entry` is updated with formalized references without data loss.
5. Vehicle enters workshop. Diagnostic reveals front brake rotor replacement needed.
6. Technician requires additional work session. Service Advisor creates a **Supplemental** `Schedule Entry` linked to the same active `Repair Order` for the following morning.
7. Work is completed, payment processed, and `Repair Order` closed.

#### Scenario 2: Emergency Walk-In with Auto-Bay & Equipment Tag Match
1. Emergency vehicle arrives requiring specialized EV High Voltage battery inspection and alignment.
2. System requests slot with required equipment tags `["HV Battery Station", "Alignment Rack"]`.
3. Slot generator evaluates available bays and identifies only bays equipped with both tags.
4. Auto-assignment assigns first free capable bay, confirms technician pool capacity, and locks slot.

#### Scenario 3: Technician Leave Conflict & Holiday Reschedule
1. Technician is granted approved leave via HRMS `Leave Application`.
2. Schedule attempt for that technician on that date raises conflict warning.
3. System alerts of ERPNext Holiday List closure on target date.
4. Rescheduling navigates to next available working business day, re-assigning free capable bay and available on-duty technician.

#### Scenario 4: High-Load Shop Floor Capacity Gating
1. Shop floor has 2 active bays and 2 on-duty technicians.
2. Two simultaneous appointments are booked at 09:00:00, exhausting both bay and technician capacity.
3. Third booking attempt at 09:00:00 is blocked with pool-level validation error.
4. Master capacity toggle `enable_technician_capacity` is disabled in `ISM Settings`; system falls back to bay-only validation.

---

## 5. Test Execution Commands & Environment

### Synchronization Pipeline (Windows Host to WSL2)
Before running tests in the Frappe container, push modified files from the Windows workspace to the WSL2 bench:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
```

### Running Tests in Devcontainer
All tests run within the active bench environment:
```powershell
# 1. Run the entire ISM test suite:
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism

# 2. Run Estimation Math module (Features 1–5):
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math

# 3. Run Scheduling & Capacity module (Features 6–22):
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling_capacity

# 4. Run Repair Order Scheduling E2E module (Features 23–31):
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_ro_scheduling_e2e
```

---

## 6. Teardown & Data Hygiene Contract

1. **Transactional Hierarchy**: Downstream documents (`Schedule Entry`, `Sales Invoice`, `Payment Entry`) are cancelled and deleted before parent `Repair Order` records.
2. **Master Data Cleanup**: All test master data (`Customer`, `Repair Vehicle`, `Employee`, `Service Bay`, `Equipment Tag`) uses the `Test ` or `_TEST_ISM_` prefix and is cleaned up in reverse dependency order.
3. **Singleton Restoration**: Tests modifying `ISM Settings` save the original values in `setUp()` and restore them inside `tearDown()` / `finally` blocks.
4. **Preservation of Business Records**: Test fixtures strictly preserve existing shop data (such as `Prestiga-Biz`, `RO-2026-00067`, and live vehicles).
