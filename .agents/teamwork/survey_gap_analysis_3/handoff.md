# Requirements Extraction & Comprehensive Gap Analysis Report: Dual-Resource Capacity Scheduling Integration

**Author**: Survey Explorer 3 (`teamwork_preview_spec_miner`)  
**Date**: 2026-10-26  
**Reference Repository**: `induct_shop` (`C:\Users\real2\AppData\Local\Temp\induct_shop` / `https://github.com/patricklai3/induct_shop`)  
**Target Repository**: `d:\ism` (Git repository on Windows host, synchronized with WSL2 Docker container `devcontainer-frappe-1`)  
**Authoritative Requirements**: `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (Requirements R1–R6 and Acceptance Criteria)  
**Dispatch Assignment**: `d:\ism\.agents\teamwork\survey_gap_analysis_3\DISPATCH.md`

---

## 1. Observation

A systematic survey of the reference repository `induct_shop`, the target repository `d:\ism`, the authoritative specification `ORIGINAL_REQUEST.md`, and the survey findings from Explorers 1 and 2 directly established the following facts, verbatim code paths, and empirical system behaviors:

### 1.1 Environment & Baseline Verification
- **WSL2 / Docker Runtime**: `d:\ism` code synchronizes to `/workspace/development/frappe-bench/apps/ism/` in container `devcontainer-frappe-1` via `scripts/sync-to-wsl.ps1`.
- **Existing Test Suite Baseline**:
  Command: `docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
  Output: `Ran 78 tests in 37.037s — OK` (30 parts management tests, 7 Tesla integration tests, 6 Repair Vehicle tests, 35 Repair Order tests).
- **Tab Layout Verification**: `ism/ism/doctype/repair_order/test_repair_order.py:1089` (`test_repair_order_super_doctype_tabs_and_status_visibility`) currently verifies:
  `intake_idx < estimate_idx < workshop_idx < billing_idx`.
  Current initial tab break in `repair_order.json:8` is `"tab_intake"` (Label: "Vehicle Check-in & Intake").

### 1.2 Comparison Summary: Reference (`induct_shop`) vs Target (`ism`)

| Component / Subsystem | `induct_shop` Reference Implementation | Target `ism` Requirements (R1–R6) | Gap & Adaptation Status |
|---|---|---|---|
| **Primary Workflow Entity** | ERPNext `Sales Order` (`is_submittable = 1`). Scheduling triggered only on submitted SO (`docstatus == 1`). | ISM `Repair Order` (`is_submittable = 0`). Initial and supplemental scheduling triggered across order lifecycle. | **Major Adaptation**: Decouple from Sales Order; bind to Repair Order. Support non-submittable state progression. |
| **RO / SO Cardinality** | Strict 1:1 relation enforced via `validate_unique_sales_order` (raises `DuplicateEntryError`). | 1:N relation. One Visit = One RO, but **Multiple Appointments per RO** for supplemental work sessions. | **Major Redesign**: Remove uniqueness constraint. Allow multiple `Schedule Entry` records per `Repair Order`. |
| **Early Appointment Lifecycle** | No early appointment stages on Sales Order. Standalone quick entry allowed on `Schedule Entry`. | `Repair Order` introduces early stages: `Draft Appointment`, `Scheduled`, `Checked In`, `No Show`. | **New Feature**: Add early stages to `Repair Order.status`. Update tab routing and list view indicators. |
| **Tab Ordering** | N/A (Standard ERPNext form). | "Appointment & Scheduling" tab (`tab_appointment`) MUST precede "Vehicle Check-in & Intake" (`tab_intake`). | **New Feature**: Insert `tab_appointment` at index 0 of `repair_order.json`. |
| **Mandatory Field Constraints** | Standard ERPNext Sales Order fields. | In `d:\ism`, `customer`, `vehicle`, and `odometer_in` are `reqd: 1` in `repair_order.json:113, 155, 162`. | **Major Adaptation**: Relax unconditional mandatory flags for `Draft Appointment` and `Scheduled` statuses. |
| **Provisional Data & Intake Formalization** | Provisional name and vehicle strings stored on `Schedule Entry` only. No formalization workflow. | Provisional contact (name, phone, email) and vehicle notes captured on RO, formalized into master records at intake. | **New Feature**: Add provisional fields to RO and implement `formalize_provisional_info()` without data loss. |
| **Form Action Button** | "Schedule Service" button added to submitted Sales Order in `induct_shop/public/js/sales_order.js:29`. | "Schedule Appointment" button on `Repair Order` opening reusable modal for initial or supplemental sessions. | **New Feature**: Add custom button on `Repair Order` calling the decoupled scheduling modal. |
| **Client-Side Modal Architecture** | Inline modal logic hardcoded in `sales_order.js` (595 lines). Only books new entries for Sales Orders. | Modular, reusable dialog (`ism/public/js/scheduling_modal.js`) usable from RO form, desk actions, or calendar. | **Refactoring**: Decouple into standalone module; support both booking new and updating existing entries. |
| **Data Models (DocTypes)** | `Service Bay`, `Equipment Tag`, `Service Bay Equipment`, `Schedule Entry`, `Schedule Entry Type` in `induct_shop`. | None of these DocTypes exist in `d:\ism`. | **Port & Adapt**: Recreate DocTypes in `ism` module; retarget links from `sales_order`/`project` to `repair_order`. |
| **Configuration Singleton** | Dedicated `Shop Settings` singleton DocType in `induct_shop/doctype/shop_settings`. | All configuration consolidated into `ISM Settings` singleton (`ism/doctype/ism_settings`). | **Consolidation**: Add scheduling, capacity, and estimation fields to existing `ISM Settings`. |
| **Duration Estimation Math** | Fixed $P80 = 0.80$, fixed $\sigma = 0.30$, no buffer padding in `estimation_light.py`. | **Tunable** in `ISM Settings`: percentile (50–95%), sigma (0.10–0.80), buffer padding (0–30 min), default diagnostic duration. | **Enhancement**: Parameterize Fenton-Wilkinson engine to load dynamic parameters from `ISM Settings` + buffer padding. |
| **Dual-Resource Server APIs** | `api/scheduling.py` and `api/technician_availability.py`: bay conflict, lunch extension, tech pool gating. | None exist in `d:\ism`. | **Port & Adapt**: Adapt APIs to read `ISM Settings` and validate `Repair Order` appointments. |
| **Calendar View** | `schedule_entry_calendar.js` and `api/scheduling_views.py` for FullCalendar. | None exist in `d:\ism`. | **Port & Adapt**: Implement Calendar View for `Schedule Entry` color-coded by entry type/status with rich popovers. |
| **Automated Test Suite** | Unittest suite in `induct_shop/api/test_scheduling.py` testing Sales Order scheduling. | Test infrastructure `ISMTestCase` in `ism/tests/base.py`. | **New Feature**: Implement complete unit & integration tests covering math, capacity, leaves, RO sync, and supplements. |

---

## 2. Logic Chain

### 2.1 Domain Invariants Analysis

1. **Invariant 1: One Visit = One Repair Order**
   - *Requirement statement*: "Each customer visit corresponds strictly to a single Repair Order. When the vehicle leaves the shop, it must be checked out and the Repair Order closed. A subsequent customer visit is always handled under a new Repair Order."
   - *Technical realization in `d:\ism`*:
     - The Repair Order lifecycle controls the physical stay of the vehicle.
     - Early stages (`Draft Appointment`, `Scheduled`) prepare the visit.
     - `Checked In` records vehicle arrival.
     - Workshop stages (`Authorized - In Progress`, `Waiting on Parts`, `Work Completed`) represent the active stay.
     - Vehicle release (`process_payment_and_release`) sets `vehicle_released = 1`, `checkout_date = now_datetime()`, and `status = 'Closed / Released'`.
     - Once closed, no further appointments can be booked on this RO; a new visit requires creating a new `Repair Order`.

2. **Invariant 2: Multiple Appointments per Repair Order**
   - *Requirement statement*: "Multiple appointments on the same Repair Order represent supplemental work or additional labor time required that cannot be completed within the original appointment window while the Repair Order is still active."
   - *Divergence from reference*: `induct_shop` included `validate_unique_sales_order()` which blocked saving more than one `Schedule Entry` for any Sales Order.
   - *Technical realization in `d:\ism`*:
     - `validate_unique_sales_order` MUST be eliminated.
     - `Schedule Entry` links to `Repair Order` (`repair_order`), allowing multiple records to point to the same RO.
     - Typical workflow:
       1. Appointment 1: Initial Diagnostic appointment (e.g. 60 min Diagnostic slot in Bay 1 on Monday).
       2. Findings require parts. RO enters `Waiting on Parts`.
       3. Parts arrive. Advisor schedules supplemental appointment (Appointment 2: Repair slot, 180 min in Bay 2 on Wednesday).
       4. Each appointment consumes its own bay and technician capacity independently, verified by start time and lunch-aware effective end time.
       5. The RO tracks all linked appointments in the "Appointment & Scheduling" tab table.

### 2.2 Tab Ordering & Workflow Precedence (R1)
- In `repair_order.json`, inserting `tab_appointment` ("Appointment & Scheduling") at field index 0 places it before `tab_intake` ("Vehicle Check-in & Intake").
- This satisfies R1 and preserves the test condition in `test_repair_order_super_doctype_tabs_and_status_visibility` (`intake_idx < estimate_idx < workshop_idx < billing_idx`), because shifting all tabs rightward preserves their relative indices:
  `apt_idx (0) < intake_idx (1) < estimate_idx (2) < workshop_idx (3) < billing_idx (4)`.
- Client script `get_status_target_tab()` is updated: when `status in ["Draft Appointment", "Scheduled"]`, the target tab is `tab_appointment`. When `status == "Checked In"`, it targets `tab_intake`.

### 2.3 Early Appointment Lifecycle & Relaxed Mandatory Fields (R1)
- Currently in `repair_order.json`, `customer`, `vehicle`, and `odometer_in` have `"reqd": 1`.
- When an advisor books an appointment via telephone or web, the vehicle has not arrived. The advisor may only have provisional customer contact details (name, phone, email) and provisional vehicle notes (e.g. "2021 Model 3 White, vibration over 60mph"). A master `Customer` record or 17-character Tesla VIN in `Repair Vehicle` might not yet exist, and the odometer reading cannot be known.
- If these fields are unconditionally required, creating a `Draft Appointment` or `Scheduled` Repair Order will fail with Frappe mandatory field validation errors.
- **Solution**:
  - In `repair_order.json`, set `"reqd": 0` on `customer`, `vehicle`, and `odometer_in`.
  - Add `"mandatory_depends_on": "eval:!in_list(['Draft Appointment', 'Scheduled'], doc.status)"`.
  - In `repair_order.py:validate()`, enforce:
    ```python
    if self.status not in ["Draft Appointment", "Scheduled"]:
        if not self.customer:
            frappe.throw(_("Customer is required once checked in or estimating."))
        if not self.vehicle:
            frappe.throw(_("Repair Vehicle is required once checked in or estimating."))
        if self.odometer_in is None:
            frappe.throw(_("Odometer In is required once checked in or estimating."))
    ```
  - In `before_insert()` and `_validate_check_in()`, only set `check_in_date` if `self.status not in ["Draft Appointment", "Scheduled"]` or if explicitly provided.

### 2.4 Provisional Information Capture & Intake Formalization (R1)
- Fields added to `Repair Order`:
  - `provisional_customer_name` (Data)
  - `provisional_phone` (Data)
  - `provisional_email` (Data)
  - `provisional_vehicle_notes` (Small Text)
- Intake Formalization Workflow (`formalize_provisional_info`):
  1. Triggered via "Check In Vehicle" action button or when updating status to `Checked In` / `Draft Estimate`.
  2. If `customer` is not linked:
     - Search for existing `Customer` by phone or email or customer_name.
     - If not found, create new `Customer` record with `customer_name = provisional_customer_name`, create linked `Contact` with `phone` and `email`.
     - Assign `self.customer = new_customer.name`.
  3. If `vehicle` is not linked:
     - If staff provided a 17-character VIN during check-in, check if `Repair Vehicle` exists. If not, create new `Repair Vehicle` with normalized VIN, auto-decoding Tesla specifications.
     - Link customer to vehicle via `Repair Vehicle Customer`.
     - Assign `self.vehicle = new_vehicle.name`.
  4. Link `self.check_in_date = frappe.utils.now_datetime()`.
  5. Update all linked `Schedule Entry` records: set `customer` and `repair_vehicle` foreign keys.
  6. Transition status to `Checked In` (or `Draft Estimate`), preserving provisional text in customer concern notes.

### 2.5 Reusable Scheduling Modal (R2)
- In `induct_shop`, the modal was hardcoded in `sales_order.js` and tightly coupled to the Sales Order DOM.
- For ISM, create a standalone client-side component `ism/public/js/scheduling_modal.js` exposing `ism.open_scheduling_dialog(opts)`:
  - `opts.repair_order`: Optional active Repair Order document.
  - `opts.schedule_entry`: Optional existing Schedule Entry for editing/rescheduling.
  - `opts.on_success`: Callback invoked on save.
- Modal Components:
  1. **Header Badges**: Computed P80 duration pill and detected equipment requirement tags pill.
  2. **Date Navigator**: Prev/Next Day buttons, native date input, holiday awareness banner (queries `ism.api.scheduling.is_holiday`).
  3. **Available Slots Grid**: Interactive button grid generated via `ism.api.scheduling.get_available_slots`, showing time, available bays, and available technicians.
  4. **Lunch Suppression**: Breaks automatically omitted from selectable start slots; spans-lunch duration extension computed and displayed.
  5. **Equipment Tag Filter**: Dropdown allowing user to filter bays by equipment tags (e.g. `Lift`, `Alignment Rack`, `EV High Voltage`).
  6. **Technician Preview & Warning Banners**: Dropdown linked to `Employee` (designation = `technician_designation`), displaying live utilization bar, red alert banner for approved leave, yellow alert banner for double-booking.
  7. **Submission**: Calls `auto_assign_bay` (or selected bay) and creates/updates `Schedule Entry`.

### 2.6 Settings Consolidation & Tunable Estimation Engine (R4)
- **Settings Consolidation**: Add the following sections to `ISM Settings`:
  1. `sb_scheduling`:
     - `operating_hours_start` (Time, default `08:00:00`)
     - `operating_hours_end` (Time, default `17:00:00`)
     - `break_start` (Time, default `12:00:00`)
     - `break_end` (Time, default `12:30:00`)
     - `default_slot_interval` (Int, default `30`)
     - `scheduling_horizon_days` (Int, default `30`)
     - `holiday_list` (Link to ERPNext `Holiday List`)
  2. `sb_capacity`:
     - `technician_designation` (Data, default `Technician`)
     - `enable_technician_capacity` (Check, default `1`)
  3. `sb_estimation`:
     - `estimation_percentile` (Percent/Float, default `80.0`, range 50–95%)
     - `estimation_sigma` (Float, default `0.30`, range 0.10–0.80)
     - `buffer_padding_minutes` (Int, default `0`, range 0–30 min)
     - `default_diagnostic_duration` (Int, default `60` min)
- **Tunable Fenton-Wilkinson Estimation Engine**:
  - `ism/scheduling/estimation_light.py`:
    - Zero external scientific dependencies (no `scipy` or `numpy`).
    - Uses precomputed z-scores `_Z_SCORES` with Abramowitz & Stegun (26.2.23) rational approximation fallback for any arbitrary percentile $p \in [0.50, 0.95]$.
    - Pure-Python multi-operation Fenton-Wilkinson log-normal summation:
      $$E[X_i] = \exp\left(\ln(M_i) + \frac{\sigma_i^2}{2}\right), \quad \text{Var}[X_i] = (\exp(\sigma_i^2) - 1)\exp(2\ln(M_i) + \sigma_i^2)$$
      $$\sigma_{\text{sum}}^2 = \ln\left(1 + \frac{\sum \text{Var}[X_i]}{(\sum E[X_i])^2}\right), \quad \mu_{\text{sum}} = \ln\left(\sum E[X_i]\right) - \frac{\sigma_{\text{sum}}^2}{2}$$
      $$D_{\text{pure}} = \max(1, \lceil \exp(\mu_{\text{sum}} + \sigma_{\text{sum}} \cdot z) \rceil)$$
    - Application of standard buffer padding:
      $$D_{\text{final}} = D_{\text{pure}} + \text{buffer\_padding\_minutes}$$
  - `ism/api/estimation_service.py`:
    - `get_estimate(item_code, **kwargs)`: Evaluates single operation, dynamically reading defaults from `ISM Settings`.
    - `get_total_estimate(item_codes, **kwargs)`: Evaluates multi-operation items, reading tuning parameters (`percentile`, `sigma`, `buffer_padding_minutes`) from `ISM Settings`.

### 2.7 Dual-Resource Allocation APIs & Validation (R5)
- Server-side availability functions in `ism/api/scheduling.py`:
  - `effective_end_time(start_time, duration_minutes, break_start, break_end)`: Inserts lunch break if window spans `break_start`.
  - `check_bay_availability(service_bay, date, start_time, duration_minutes, exclude_entry)`: Strict overlap check `max(start1, start2) < min(end1, end2)` using effective end times.
  - `get_available_bays(date, start_time, duration_minutes, required_tags)`: Bay equipment tag superset match.
  - `get_available_slots(date, duration_minutes, required_tags, service_bay)`: Operating hours walk in slot intervals, excluding lunch slots, computing available bays, available technicians, effective capacity, and bottleneck resource.
  - `is_holiday(date)`: Queries linked `Holiday List`.
- Technician capacity functions in `ism/api/technician_availability.py`:
  - `get_active_technicians(date)`: Filters `Employee` by `designation == technician_designation` and `status == 'Active'`, resolving approved `Leave Application` status.
  - `get_technician_pool_availability(date, start_time, duration_minutes, exclude_entry)`: Counts total, on-leave, on-duty, occupied, and available technicians.
  - `check_technician_availability(employee, date, start_time, duration_minutes, exclude_entry)`: Individual overlap and leave checking with soft warnings (`msgprint(alert=True)`).
- `Schedule Entry` Controller Validation (`ism/ism/doctype/schedule_entry/schedule_entry.py`):
  - `validate_bay_availability()`: Throws `ValidationError` on bay collision.
  - `validate_technician_pool()`: If `enable_technician_capacity` is true, throws `ValidationError` if `available == 0`.
  - `validate_technician_assignment()`: Soft warnings for leave/overlap.

### 2.8 Frappe Calendar View (R5)
- `ism/ism/doctype/schedule_entry/schedule_entry_calendar.js`:
  - Calendar endpoint: `ism.api.scheduling_views.get_calendar_events`.
  - Color-coding by `entry_type` color or `status` color.
  - Lunch badge (`🍱 Spans Lunch`) for jobs spanning break.
  - Rich popover showing vehicle, customer, bay, technician, duration, and notes.

---

## 3. Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|---|---|---|---|---|---|---|
| 1 | Estimation | Analytic Log-Normal P80 | Extracts risk-adjusted duration from median flat-rate time using right-skewed log-normal distribution | `flat_rate_minutes` (float), `sigma` (float), `percentile` (float) | `duration` (ceiling int, min 1) | Raises `ValueError` if `flat_rate_minutes <= 0` | `induct_shop/scheduling/estimation_light.py:38` |
| 2 | Estimation | Fenton-Wilkinson Summation | Fits a single log-normal distribution to the sum of independent log-normal operations, modeling schedule diversification | `flat_rate_list` (list of numbers or dicts with custom `sigma`), `percentile` | `total_duration` (ceiling int, min 1) | Returns 0 for empty list; raises `ValueError` if any FRT $\le 0$ | `induct_shop/scheduling/estimation_light.py:83` |
| 3 | Estimation | Category Sigma Profiles | Category-specific variance parameters (Brake 0.20, Tire 0.15, Suspension 0.35, Electrical 0.50, Body 0.40, HVAC 0.35, Drivetrain 0.40) | `item_group` (str) | `sigma` (float) | Returns `DEFAULT_SIGMA` (0.30) if category unrecognized | `induct_shop/scheduling/estimation_light.py:20` |
| 4 | Estimation | Rational Z-Score Approximation | Zero-scipy Abramowitz & Stegun (26.2.23) inverse CDF approximation for continuous percentiles (50%–95%) | `percentile` (float $0.50 \le p < 1.0$) | `z` (float, error $< 4.5 \times 10^{-4}$) | Returns exact table lookup if $p \in \{0.50, 0.75, 0.80, 0.85, 0.90, 0.95\}$ | `induct_shop/scheduling/estimation_light.py:71` |
| 5 | Estimation | Configurable Buffer Padding | Adds standard shop padding minutes (0–30 min) to risk-adjusted estimate | `buffer_padding_minutes` in `ISM Settings` | `duration + buffer_padding_minutes` | Non-negative integer | `ORIGINAL_REQUEST.md` R4 |
| 6 | Capacity | Lunch-Aware Effective End Time | Adds shop lunch break duration to job end time if work window spans across lunch break | `start_time` (Time/str), `duration_minutes` (int), `break_start`, `break_end` | `effective_end_time` (timedelta) | Returns naive end time if break not configured or window does not span break | `induct_shop/api/scheduling.py:81` |
| 7 | Capacity | Per-Bay Overlap Validation | Prevents double-booking on physical service bays using lunch-aware time intervals | `service_bay` (str), `date` (str), `start_time`, `duration_minutes`, `exclude_entry` | `is_available` (bool) | Schedule Entry controller raises `ValidationError` on collision | `induct_shop/api/scheduling.py:120` |
| 8 | Capacity | Equipment Tag Filtering | Evaluates if a bay possesses all equipment tags required for scheduled operations (superset match) | `date`, `start_time`, `duration_minutes`, `required_tags` (list) | List of matching available bay dicts | Returns empty list if no bay satisfies tag requirements | `induct_shop/api/scheduling.py:168` |
| 9 | Capacity | Automated Bay Assignment | Automatically picks the first capable, free service bay using earliest-fit strategy | `date`, `start_time`, `duration_minutes`, `required_tags` | `bay_name` (str) | Raises `ValidationError` if all bays full or technician pool exhausted | `induct_shop/api/scheduling.py:218` |
| 10 | Capacity | Slot Generator with Capacity Indicators | Generates time slots within operating hours, filtering lunch and reporting bay/technician bottleneck | `date`, `duration_minutes`, `required_tags`, `service_bay` | List of slot dicts with `available_bays`, `available_technicians`, `bottleneck` | Excludes slots with `effective_capacity < 1` or exceeding operating hours | `induct_shop/api/scheduling.py:248` |
| 11 | Capacity | Technician Designation Roster | Filters active technicians dynamically from standard `Employee` DocType by designation | None (reads `technician_designation` from settings) | List of active technician dicts | Empty list if no active employees match designation | `induct_shop/api/technician_availability.py:14` |
| 12 | Capacity | HRMS Leave Application Integration | Excludes technicians on approved leave from on-duty pool | `employee` (str), `date` (str) | Dict `{on_leave: bool, half_day: bool}` | Defensively handles missing `Leave Application` DocType | `induct_shop/api/technician_availability.py:76` |
| 13 | Capacity | Technician Pool-Level Gating | Enforces that active appointments at any time $T$ do not exceed on-duty technicians | `date`, `start_time`, `duration_minutes`, `exclude_entry` | Pool dict `{total, on_leave, on_duty, occupied, available, is_available}` | Schedule Entry controller raises `ValidationError` if `available == 0` | `induct_shop/api/technician_availability.py:119` |
| 14 | Capacity | Individual Technician Overlap Check | Detects individual technician double-booking and approved leave conflicts | `employee`, `date`, `start_time`, `duration_minutes`, `exclude_entry` | Dict `{is_available, reason, conflicts}` | Emits non-blocking soft warnings (`msgprint`) allowing manager override | `induct_shop/api/technician_availability.py:210` |
| 15 | Capacity | Master Capacity Toggle | Master switch to enable/disable technician capacity gating, falling back to bay-only model | `enable_technician_capacity` in settings | Bypasses technician gating when 0 | Returns dummy capacity (999) without error | `induct_shop/api/technician_availability.py:130` |
| 16 | Capacity | Technician Queue & Utilization | Calculates personal daily timeline, gap intervals, and shop utilization percentage | `employee`, `date` | Dict `{entries, total_jobs, total_minutes, utilization_pct, gaps}` | Handles zero appointments gracefully | `induct_shop/api/technician_availability.py:325` |
| 17 | Capacity | Daily Technician Overview | Shop-wide dashboard summarizing total scheduled hours, bottleneck, and unassigned entries | `date` | Overview dict `{technicians, unassigned_entries, shop_summary}` | Automatically calculates shop bottleneck | `induct_shop/api/technician_availability.py:423` |
| 18 | Capacity | ERPNext Holiday Awareness | Identifies shop closures based on linked ERPNext `Holiday List` | `date` (str) | Dict `{is_holiday: bool, description: str}` | Falls back to false if holiday list not configured | `induct_shop/api/scheduling.py:358` |
| 19 | Data Model | Service Bay DocType | Represents physical shop bays with active state and equipment table | `bay_name`, `is_active`, `description`, `equipment` | Service Bay Document | Enforces unique `bay_name` | `induct_shop/doctype/service_bay` |
| 20 | Data Model | Equipment Tag System | Tag taxonomy for matching bays to specialized vehicle repair capabilities | `tag_name`, `description` | Equipment Tag Document | Enforces unique `tag_name` | `induct_shop/doctype/equipment_tag` |
| 21 | Data Model | Schedule Entry Type DocType | Categorizes appointments (Diagnostic, Repair, Meeting, Maintenance, Internal Service) with UI colors | `type_name`, `color`, `description`, `requires_repair_order` | Schedule Entry Type Document | Auto-seeded during app installation/migration | `induct_shop/doctype/schedule_entry_type` |
| 22 | Data Model | Schedule Entry DocType | Core schedule appointment record storing time window, bay, tech, and notes | All scheduling and provisional fields | Schedule Entry Document | Validates bay overlap and tech pool on save | `induct_shop/doctype/schedule_entry` |
| 23 | UI / UX | Reusable Modal Date Navigator | Step backward/forward by day with immediate holiday alert and slot refresh | `scheduled_date` | Updated date display, slot buttons | Disables booking on detected holidays | `induct_shop/public/js/sales_order.js:298` |
| 24 | UI / UX | Header Badges | Visual pill badges displaying estimated duration (P80 + buffer) and detected equipment tags | `estimated_duration`, `required_tags` | HTML badge bar | Displays "No estimation available" if empty | `induct_shop/public/js/sales_order.js:268` |
| 25 | UI / UX | Slot Selection Grid | Clickable slot buttons showing available bays and technicians | List of available slots | Primary active button styling | Displays empty-state banner when no slots available | `induct_shop/public/js/sales_order.js:403` |
| 26 | UI / UX | Technician Preview & Conflict Warnings | Live preview of technician daily utilization with warning banners for leave (red) and overlap (yellow) | `assigned_technician`, `scheduled_date`, `selected_slot` | HTML utilization line and alert banners | Automatically clears warnings when slot changes | `induct_shop/public/js/sales_order.js:483` |
| 27 | UI / UX | Calendar View Integration | FullCalendar integration color-coded by entry type/status with lunch gap badges | Schedule Entry query filters | Calendar event blocks with rich popovers | Shows unassigned bay/technician clearly | `induct_shop/public/js/schedule_entry_calendar.js` |
| 28 | UI / UX | List View Quick Filters | Preset buttons on list view for All Entries, Today's Jobs, Needs Review, Unassigned Tech, In Progress | Filter actions | Filtered Schedule Entry list view | Clear and reset filter functionality | `induct_shop/public/js/schedule_entry_list.js` |
| 29 | Lifecycle | Early Appointment Stages on RO | Supports `Draft Appointment`, `Scheduled`, `Checked In`, `No Show` on Repair Order | `Repair Order.status` | State progression and form UI adaptation | Tab target resolves to `tab_appointment` | `ORIGINAL_REQUEST.md` R1 |
| 30 | Lifecycle | Provisional Intake Formalization | Formalizes provisional customer/vehicle data into master `Customer` and `Repair Vehicle` on check-in | `Repair Order` provisional fields | Master records created and linked | Retains text in concern notes without data loss | `ORIGINAL_REQUEST.md` R1 |
| 31 | Lifecycle | Supplemental Scheduling | Schedules additional work sessions on an active Repair Order without closing or replacing the RO | `Repair Order` form action button | Additional linked `Schedule Entry` created | Each entry validated independently for capacity | `ORIGINAL_REQUEST.md` R1, Core Invariant |

---

## 4. Edge Cases

| # | Feature | Input / Condition | Observed Behavior | Required ISM Handling |
|---|---|---|---|---|
| 1 | `effective_end_time` | Job starts before lunch (11:00) with 120 min duration; lunch is 12:00–12:30 | Effective end time extends from naive 13:00 to 13:30 (+30 min break duration). | Same. Must ensure calendar blocks and overlap algorithms use 13:30. |
| 2 | `effective_end_time` | Job starts at 08:00 with 120 min duration (finishes at 10:00, before lunch) | No break duration is added; effective end is 10:00. | Same. |
| 3 | `effective_end_time` | Job starts after lunch (13:00) with 90 min duration | No break duration is added; effective end is 14:30. | Same. |
| 4 | `effective_end_time` | Break start or break end is null in `ISM Settings` | Effective end equals naive end (start + duration). | Same. Defensively handles null break settings in `ISM Settings`. |
| 5 | `check_bay_availability` | Rescheduling an existing entry to the same slot or saving metadata updates | `exclude_entry=self.name` prevents self-collision; validation succeeds. | Same. Rescheduling or modifying notes must pass `exclude_entry`. |
| 6 | `check_bay_availability` | Schedule Entry has status `"Cancelled"` | Status check bypasses validation; cancelled entries do not occupy bays. | Same. |
| 7 | `get_available_slots` | Candidate slot starts inside lunch break (e.g. 12:00 or 12:15 when break is 12:00–12:30) | Slot is skipped immediately; no appointments can begin during lunch. | Same. |
| 8 | `get_available_slots` | Candidate slot's lunch-aware effective end time extends past `operating_hours_end` (e.g. 16:30 + 60 min job when shop closes at 17:00) | Slot is omitted from returned slot list; jobs cannot exceed shop hours. | Same. |
| 9 | `auto_assign_bay` | Bay pool is empty or all capable bays are booked for requested window | Throws `frappe.ValidationError` with descriptive message. | Same. In the UI modal, catches error and re-enables schedule button. |
| 10 | `auto_assign_bay` | Technician pool is exhausted (`available == 0`) when `enable_technician_capacity` is true | Throws `frappe.ValidationError` stating technician pool capacity exhausted. | Same. |
| 11 | `check_technician_availability` | Assigned technician has approved full-day leave in `Leave Application` | Returns `is_available: False`, `reason: "on_leave"`. Form displays red warning alert. | Same. Does not hard block save to allow emergency supervisor override. |
| 12 | `check_technician_availability` | Assigned technician has overlapping Schedule Entry at requested time | Returns `is_available: False`, `reason: "overlap"`, and conflicts list. Form displays yellow warning alert. | Same. Non-blocking soft warning. |
| 13 | `get_technician_pool_availability` | `enable_technician_capacity` is toggled off (0) in settings | Bypasses employee query; returns `total_technicians: 999, available: 999, is_available: True`. | Same. Enables bay-only scheduling mode without modifying database records. |
| 14 | `is_holiday` | Shop date falls on an ERPNext `Holiday` for configured `holiday_list` | API returns `{is_holiday: True, description: "..."}`. Modal shows alert banner and suppresses slots. | Same. Prevents accidental booking on public holidays. |
| 15 | `estimate_total_duration` | Empty operation list `[]` passed to estimation engine | Returns 0 immediately. | Same. |
| 16 | `estimate_total_duration` | Single operation passed to multi-operation estimator | Bypasses Fenton-Wilkinson summation and calls single `estimate_duration` directly. | Same. |
| 17 | `estimate_total_duration` | Flat rate time $\le 0$ (e.g. 0 or -15) | Raises `ValueError("Flat rate time must be positive (> 0)")`. | Same. Defensively caught in API layer to return 0 or fallback diagnostic time. |
| 18 | `estimate_total_duration` | Multiple operations summed with Fenton-Wilkinson | Sum of variances produces diversification effect; total estimate is strictly less than naive sum of individual P80s. | Same. Verified by unit test math ($180 < 215 < 234$ for $3 \times 60$ min items). |
| 19 | `Schedule Entry` cardinality | Multiple appointments on same `Repair Order` (e.g. supplemental work session) | Threw `DuplicateEntryError` in `induct_shop`. | Must ALLOW multiple `Schedule Entry` records linked to the same `Repair Order` while enforcing individual bay and technician pool constraints per entry. |
| 20 | Provisional Intake Transition | Provisional customer details (name, phone, email) on RO converted to master `Customer` | In `induct_shop`, only quick entry fields existed on `Schedule Entry`. | In `d:\ism`, intake transition formalizes provisional customer & vehicle records on vehicle check-in, populating master links and back-linking `Schedule Entry`. |
| 21 | Early Stage RO Creation | Creating RO in `Draft Appointment` without customer, vehicle, or odometer | Threw `MandatoryError` in current `d:\ism` code. | Must succeed without error when status is `Draft Appointment` or `Scheduled`. |
| 22 | Check-in Date Invariant | RO created as `Draft Appointment` | `before_insert` currently forced `check_in_date = now_datetime()`. | `check_in_date` must remain `None` until physical check-in occurs (`Checked In` status). |

---

## 5. Proposed Milestone Decomposition & Interface Contracts

```
[M1: Resource & Capacity Data Models & Settings Consolidation]
   │
   ├──> [M2: Tunable Estimation Engine & Pure Math Core]
   │       │
   │       v
   └──> [M3: Dual-Resource Capacity & Scheduling APIs]
           │
           ├──> [M4: Repair Order Integration & Early Appointment Lifecycle]
           │       │
           │       v
           └──> [M5: Reusable Scheduling Modal & Frappe Calendar View]
                   │
                   v
                [M6: Automated Test Suite & End-to-End Verification]
```

### Milestone 1: Resource & Capacity Data Models & Settings Consolidation (R3, R4)
- **Scope**:
  - Create standard DocTypes in `ism/ism/doctype/`:
    - `service_bay`: `bay_name` (PK), `is_active` (Check), `description` (Small Text), `equipment` (Table: `Service Bay Equipment`).
    - `equipment_tag`: `tag_name` (PK), `description` (Small Text).
    - `service_bay_equipment`: Child table linking `equipment_tag` (Link: `Equipment Tag`).
    - `schedule_entry_type`: `type_name` (PK), `color` (Color), `description` (Small Text), `requires_repair_order` (Check).
    - `schedule_entry`: `naming_series` (`SE-.#####`), `entry_type` (Link), `repair_order` (Link), `customer` (Link), `provisional_customer_name` (Data), `provisional_phone` (Data), `provisional_email` (Data), `scheduled_date` (Date), `scheduled_time` (Time), `status` (Select: Draft, Scheduled, In Progress, Completed, Cancelled), `repair_vehicle` (Link), `provisional_vehicle_info` (Data), `estimated_duration` (Int), `service_bay` (Link), `assigned_technician` (Link: `Employee`), `notes` (Small Text).
  - Update `ism_settings.json`:
    - Add `sb_scheduling`, `sb_capacity`, and `sb_estimation` sections with all required fields.
  - Seeding in `ism/setup.py`:
    - Implement `seed_schedule_entry_types()` in `after_install` and `after_migrate` hooks. Seed types: `Diagnostic` (#1f538d, RO=0), `Repair` (#2e7d32, RO=1), `Maintenance` (#0284c7, RO=1), `Meeting` (#7b1fa2, RO=0), `Internal Service` (#ef6c00, RO=0).
    - Seed default `Equipment Tag`s (`Lift`, `EV High Voltage`, `Alignment Rack`).
    - Update `setup_default_ism_settings()` to initialize default scheduling, capacity, and estimation values.
- **Verification**: `bench migrate` completes without error; records visible in Desk.

### Milestone 2: Tunable Estimation Engine & Pure Math Core (R4)
- **Scope**:
  - Implement `ism/scheduling/estimation_light.py`:
    - Precomputed z-scores and Abramowitz & Stegun inverse CDF approximation.
    - Single operation log-normal percentile estimation.
    - Multi-operation Fenton-Wilkinson log-normal summation.
  - Implement `ism/api/estimation_service.py`:
    - `get_estimate(item_code, **kwargs) -> int`
    - `get_total_estimate(item_codes, **kwargs) -> int`
    - Reads `estimation_percentile`, `estimation_sigma`, `buffer_padding_minutes`, and `default_diagnostic_duration` from `ISM Settings`.
    - Returns computed duration in minutes ($D_{\text{pure}} + \text{buffer\_padding\_minutes}$).
- **Verification**: Pure-Python unit test suite verifying single/multi item math, buffer padding addition, sigma category lookup, and error handling.

### Milestone 3: Dual-Resource Capacity & Scheduling APIs (R5)
- **Scope**:
  - Implement `ism/api/scheduling.py`:
    - `effective_end_time(start_time, duration_minutes, break_start=None, break_end=None) -> timedelta`
    - `check_bay_availability(service_bay, date, start_time, duration_minutes, exclude_entry=None) -> bool`
    - `get_available_bays(date, start_time, duration_minutes, required_tags=None) -> List[Dict]`
    - `auto_assign_bay(date, start_time, duration_minutes, required_tags=None) -> str`
    - `get_available_slots(date, duration_minutes, required_tags=None, service_bay=None) -> List[Dict]`
    - `is_holiday(date) -> Dict`
  - Implement `ism/api/technician_availability.py`:
    - `get_active_technicians(date=None) -> List[Dict]`
    - `check_employee_leave(employee, date, start_time, duration_minutes) -> Dict`
    - `get_technician_pool_availability(date, start_time, duration_minutes, exclude_entry=None) -> Dict`
    - `check_technician_availability(employee, date, start_time, duration_minutes, exclude_entry=None) -> Dict`
  - Controller validations in `Schedule Entry`:
    - Overlap validation on `service_bay`.
    - Pool limit validation when `enable_technician_capacity` is true.
    - Non-blocking soft warnings for assigned technician conflicts/leave.
- **Verification**: API unit tests verifying lunch extension, bay double-booking prevention, pool exhaustion gating, and leave awareness.

### Milestone 4: Repair Order Integration & Early Appointment Lifecycle (R1)
- **Scope**:
  - Schema update in `repair_order.json`:
    - Prepend `tab_appointment` ("Appointment & Scheduling") at index 0.
    - Add provisional fields: `provisional_customer_name`, `provisional_phone`, `provisional_email`, `provisional_vehicle_notes`.
    - Add appointments section with table displaying linked `Schedule Entry` records.
    - Set `reqd: 0` on `customer`, `vehicle`, and `odometer_in` with `mandatory_depends_on` guarding against early appointment stages.
    - Add status options: `Draft Appointment\nScheduled\nChecked In\nNo Show\nDraft Estimate\n...`.
  - Controller update in `repair_order.py`:
    - Update `before_insert()` and `_validate_check_in()`: suppress forced `check_in_date` during `Draft Appointment` / `Scheduled`.
    - Enforce customer, vehicle, and odometer validations only for `Checked In` or later stages.
    - Implement `formalize_provisional_info()`: formalizes customer and vehicle master records and updates linked Schedule Entries.
  - Client script update in `repair_order.js`:
    - Update `get_status_target_tab()` to target `tab_appointment` for early stages.
    - Update `status_colors` in list view settings.
    - Add "Schedule Appointment" button to form actions.
- **Verification**: Create early stage RO without customer/vehicle; test check-in and formalization into master records; verify tab ordering tests pass.

### Milestone 5: Reusable Scheduling Modal & Frappe Calendar View (R2, R5)
- **Scope**:
  - Implement reusable client-side dialog: `ism/public/js/scheduling_modal.js`:
    - Class `ism.SchedulingDialog` callable from RO button, desk action, or calendar.
    - Header badges for duration and equipment tags.
    - Date navigator with holiday detection.
    - Slot grid with live bay and technician counts.
    - Equipment tag filter.
    - Technician preview with leave/overlap warnings.
    - Direct creation and updating of `Schedule Entry`.
  - Implement Calendar View:
    - `ism/public/js/schedule_entry_calendar.js`.
    - `ism/api/scheduling_views.py`: `get_calendar_events`.
    - Color-coded event blocks by status/entry type with lunch span badge and hover popovers.
- **Verification**: Launch dialog from Desk and RO; book appointment; verify event renders in Frappe Calendar view.

### Milestone 6: Automated Test Suite & End-to-End Verification (R6)
- **Scope**:
  - Implement test modules under `ism/tests/`:
    - `test_estimation.py`: Unit tests for pure-Python Fenton-Wilkinson summation, P50–P95 percentiles, Abramowitz & Stegun approximation, custom sigma, and buffer padding.
    - `test_scheduling.py`: Unit tests for lunch-aware effective end times, bay availability, double-booking prevention, and holiday awareness.
    - `test_technician_availability.py`: Integration tests for technician designation roster, leave application awareness, and pool-level capacity gating.
    - `test_repair_order_scheduling.py`: Integration tests for early appointment lifecycle, tab layout compliance, provisional data formalization, and supplemental appointments on an active RO.
- **Verification**: Run `bench --site development.localhost run-tests --app ism` and confirm 100% pass across all existing and new test suites.

---

## 6. Caveats

1. **Test Environment Execution**:
   - `bench` is not installed directly in the Windows host OS environment. All bench commands (`bench migrate`, `bench run-tests`) execute inside the WSL2 Docker container `devcontainer-frappe-1`.
   - Before executing bench commands in the container, changes made in `d:\ism` must be synced to WSL2 via `scripts/sync-to-wsl.ps1`.
2. **Technician Designation Dependency**:
   - Technician availability queries filter standard ERPNext `Employee` records by `designation == technician_designation` (defaulting to "Technician"). In test fixtures, test employees must either be assigned the "Technician" designation or the test must temporarily override `ISM Settings.technician_designation`.
3. **Non-Submittable Repair Order**:
   - Unlike ERPNext `Sales Order` (which uses `docstatus` 0=Draft, 1=Submitted, 2=Cancelled), `Repair Order` in ISM has `is_submittable = 0`. Its lifecycle is governed purely by `status` and `approval_status`. Scheduling integrations must hook into `status` changes rather than document submission events.

---

## 7. Conclusion

The dual-resource capacity scheduling system from `induct_shop` provides proven, production-grade logic for Fenton-Wilkinson duration estimation, lunch-aware effective end times, service bay collision prevention, and technician pool gating. 

Adapting this system to ISM requires:
1. **Shifting core cardinality**: Moving from a 1:1 binding with Sales Order to a 1:N binding with `Repair Order`, satisfying the invariant **One Visit = One Repair Order, Multiple Appointments per Repair Order**.
2. **Enabling early appointment stages**: Adding `Draft Appointment`, `Scheduled`, `Checked In`, and `No Show` to `Repair Order`, prepending the "Appointment & Scheduling" tab (`tab_appointment`) before intake, and relaxing mandatory field constraints prior to physical vehicle check-in.
3. **Consolidating settings**: Unifying shop hours, lunch breaks, slot intervals, holiday lists, technician capacity toggles, and tunable estimation parameters (P50–P95 percentile, sigma, buffer padding, default diagnostic duration) into `ISM Settings`.
4. **Providing reusable interfaces**: Exposing a decoupled client-side modal dialog for booking initial and supplemental appointments from `Repair Order`, Desk, and Calendar views.

The proposed 6-milestone decomposition provides an incremental, verifiable path to implementing requirements R1 through R6 while maintaining 100% passing status on all 78 existing ISM tests.

---

## 8. Verification Method

To independently verify the observations, gap analysis, and test baseline documented in this report:

1. **Verify Baseline Test Suite Execution**:
   ```powershell
   wsl bash -c "docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism"
   ```
   *Expected outcome*: Ran 78 tests in ~36s, 0 failures, 0 errors.

2. **Verify Codebase Synchronization Mechanism**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   ```
   *Expected outcome*: `[sync-to-wsl] Complete.`

3. **Verify Pure Python Reference Math in `induct_shop`**:
   ```powershell
   python -m unittest "C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop\tests\test_estimation_light.py"
   ```
   *Expected outcome*: Ran 5 tests, OK (verifies analytic P80 = 78 min for 60 min FRT, and Fenton-Wilkinson diversification $180 < 215 < 234$).

4. **Verify DocType JSON Tab Ordering in `d:\ism`**:
   Inspect `d:\ism\ism\ism\doctype\repair_order\repair_order.json` (lines 8–80) to confirm `tab_intake` is currently the initial tab break.
