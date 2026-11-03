# Original User Request

## 2026-10-26T14:20:19Z

# Teamwork Project Prompt

Integrate a dual-resource capacity scheduling system into ISM's Repair Order workflow, adapting proven scheduling capabilities from the induct_shop repository while tailoring the user experience to a reusable scheduling modal, early appointment lifecycle stages, tunable estimation algorithms, and Frappe calendar views.

Working directory: d:\ism
Integrity mode: development

Reference repository: https://github.com/patricklai3/induct_shop (and local clone at $env:TEMP\induct_shop)

## Core Domain Rules
- **One Visit = One Repair Order**: Each customer visit corresponds strictly to a single Repair Order. When the vehicle leaves the shop, it must be checked out and the Repair Order closed. A subsequent customer visit is always handled under a new Repair Order.
- **Multiple Appointments per Repair Order**: Multiple appointments on the same Repair Order represent supplemental work or additional labor time required that cannot be completed within the original appointment window while the Repair Order is still active.
- **Codebase Exploration**: When exploring the `d:\ism` codebase, always reach for CodeGraph (`codegraph_explore` tool or `codegraph explore <query>` CLI) before grep/find or manual reading.

## Requirements

### R1. Repair Order Scheduling Integration & Early Appointment Lifecycle
- Add an "Appointment & Scheduling" tab as the initial tab before "Vehicle Check-in & Intake" on `Repair Order`.
- Support early appointment stages on `Repair Order` (e.g., `Draft Appointment`, `Scheduled`, `Checked In`, `No Show`) allowing intake staff to capture provisional customer contact details (name, phone, email) and vehicle notes prior to formal master record registration.
- Provide a smooth transition at vehicle intake to formalize provisional info into master `Customer` and `Repair Vehicle` records.
- Provide a form action button ("Schedule Appointment") on `Repair Order` that opens the reusable scheduling modal to book initial appointments or schedule supplemental work sessions for an active Repair Order.

### R2. Reusable Scheduling Modal (Dialog)
- Implement a modular, reusable client-side scheduling dialog accessible from `Repair Order`, desk actions, or calendar views.
- Provide a date navigator with holiday awareness, showing available time slots generated from shop operating hours and slot intervals.
- Dynamically display dual-resource capacity indicators (available service bays and active technicians) per slot, automatically suppressing lunch break windows.
- Support equipment tag filtering for bays (e.g. EV High Voltage, Alignment Rack, 2-Post Lift).
- Provide real-time technician conflict warnings and leave status checks.
- Support booking and updating `Schedule Entry` records directly from the dialog.

### R3. Resource & Capacity DocTypes
- Create a standard `Schedule Entry` DocType linked to `Repair Order`, storing appointment type, scheduled date/time, estimated duration, assigned service bay, assigned technician, and provisional customer/vehicle notes.
- Implement `Schedule Entry Type` DocType (seeding types: Diagnostic, Repair, Maintenance, Meeting, Internal Service) to support both RO service appointments and standalone non-RO reservations (shop cleaning, bay maintenance, meetings).
- Implement `Service Bay` DocType with active status and a child table for `Equipment Tag`s.

### R4. ISM Settings & Tunable Estimation Engine
- Consolidate scheduling configuration into `ISM Settings`:
  - Shop operating hours (`operating_hours_start`, `operating_hours_end`)
  - Lunch break window (`break_start`, `break_end`)
  - Slot interval (`default_slot_interval`) and scheduling horizon in days
  - ERPNext Holiday List link
  - Technician designation and capacity gating toggle (`enable_technician_capacity`)
  - Tunable duration estimation parameters: percentile target (P80 default, range 50–95%), log-normal variability sigma (default 0.30), standard buffer padding in minutes (e.g. 0–30 min), and default diagnostic duration (e.g. 60 min).
- Implement a duration estimation service utilizing log-normal Fenton-Wilkinson summation that dynamically reads these tuning parameters from `ISM Settings`.

### R5. Dual-Resource Allocation APIs & Calendar View
- Implement server-side availability APIs for bay conflict detection and technician pool-level capacity gating.
- Calculate lunch-aware effective end times (extending job end times when an appointment spans the shop lunch break).
- Prevent double-booking on service bays and enforce technician pool limits on `Schedule Entry` validation.
- Implement Frappe Calendar View for `Schedule Entry` color-coded by entry type or status, rendering bay, technician, and vehicle details.

### R6. Automated Test Suite
- Implement automated unit and integration tests covering:
  - Estimation engine math with custom percentile target, sigma, and buffer padding
  - Bay availability, double-booking prevention, and lunch-aware effective end times
  - Technician leave awareness and pool-level capacity gating
  - Bidirectional synchronization and lifecycle transitions between `Schedule Entry` and `Repair Order`
  - Supplemental scheduling workflows on an active Repair Order

## Acceptance Criteria

### Data Models & Configuration
- [ ] `Service Bay`, `Equipment Tag`, `Schedule Entry`, and `Schedule Entry Type` DocTypes are created as standard DocTypes and successfully migrate via `bench migrate`.
- [ ] `ISM Settings` contains the full scheduling section (operating hours, breaks, slot interval, holiday list, technician designation, capacity toggle, and tunable estimation parameters: percentile target, sigma, buffer padding, default diagnostic duration).
- [ ] `Schedule Entry Type` is seeded with Diagnostic, Repair, Maintenance, Meeting, and Internal Service records.

### Scheduling Engine & Capacity
- [ ] Pure-Python estimation math verifies that single and multi-operation flat-rate durations correctly calculate the configured percentile duration (e.g. P80) plus configured buffer padding.
- [ ] Jobs spanning the configured lunch break window automatically extend their effective end time by the break duration.
- [ ] Overlapping bookings on the same `Service Bay` are strictly blocked with a validation error, while separate bays at the same time are permitted.
- [ ] When `enable_technician_capacity` is active, booking attempts exceeding available on-duty technicians are rejected.
- [ ] Technicians with approved leave for the scheduled date are flagged or excluded from available capacity.

### Repair Order Integration & Modal
- [ ] `Repair Order` displays the "Appointment & Scheduling" tab prior to the Intake tab, displaying linked appointments and provisional fields.
- [ ] Clicking "Schedule Appointment" opens the reusable modal, populating available slots according to shop settings, bay availability, and lunch breaks.
- [ ] Submitting the modal creates or updates a `Schedule Entry` linked to the `Repair Order`.
- [ ] Multiple `Schedule Entry` records can be linked to a single active `Repair Order` when supplemental work or extra duration is needed prior to checkout.
- [ ] At vehicle check-in, provisional contact and vehicle data can be formalized into master `Customer` and `Repair Vehicle` records without data loss.

### Calendar View & Verification
- [ ] Standard Frappe Calendar View renders `Schedule Entry` records color-coded with customer/vehicle, bay, and technician info.
- [ ] Automated test suite passes all tests via `bench run-tests --app ism`.
