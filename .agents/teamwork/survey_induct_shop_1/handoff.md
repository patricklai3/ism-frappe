# Survey and Specification Mining Report: Dual-Resource Scheduling Integration

**Author**: Survey Explorer 1 (`teamwork_preview_spec_miner`)  
**Date**: 2026-10-26  
**Reference Repository**: `induct_shop` (`C:\Users\real2\AppData\Local\Temp\induct_shop` / `https://github.com/patricklai3/induct_shop`)  
**Target Repository**: `d:\ism`  
**Authoritative Requirements**: `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`  

---

## 1. Observation

Direct examination of the reference repository `induct_shop` (`C:\Users\real2\AppData\Local\Temp\induct_shop`) and the target repository `d:\ism` revealed the following codebase implementations, schemas, APIs, and algorithms:

### 1.1 Reference Implementation Files & Line Numbers

1. **Lightweight Log-Normal & Fenton-Wilkinson Estimation**:
   - File: `induct_shop/scheduling/estimation_light.py` (Lines 1–159)
     - `_Z_SCORES` (Lines 4–12): Precomputed mapping for `{0.50: 0.0, 0.75: 0.6745, 0.80: 0.8416, 0.85: 1.0364, 0.90: 1.2816, 0.95: 1.6449}`.
     - Rational approximation fallback (Lines 71–76, 151–156): Abramowitz & Stegun formula 26.2.23 approximating the normal inverse CDF to $\sim 4.5 \times 10^{-4}$ precision:
       $$t = \sqrt{-2.0 \cdot \ln(1.0 - p)}$$
       $$z = t - \frac{2.515517 + 0.802853 \cdot t + 0.010328 \cdot t^2}{1.0 + 1.432788 \cdot t + 0.189269 \cdot t^2 + 0.001308 \cdot t^3}$$
     - Single operation estimate (Lines 38–81):
       $$\mu_{\ln} = \ln(\text{flat\_rate\_minutes})$$
       $$\text{duration} = \max(1, \lceil \exp(\mu_{\ln} + \sigma \cdot z) \rceil)$$
     - Fenton-Wilkinson multi-operation summation (Lines 83–158):
       For each operation $i$ with $\text{FRT}_i$ and $\sigma_i$:
       $$\mu_i = \ln(\text{FRT}_i)$$
       $$\text{mean}_i = \exp\left(\mu_i + \frac{\sigma_i^2}{2}\right)$$
       $$\text{var}_i = (\exp(\sigma_i^2) - 1.0) \cdot \exp(2\mu_i + \sigma_i^2)$$
       Sum under independence:
       $$\text{total\_mean} = \sum \text{mean}_i, \quad \text{total\_var} = \sum \text{var}_i$$
       Fit single log-normal:
       $$\sigma_{\text{sum}}^2 = \ln\left(1.0 + \frac{\text{total\_var}}{\text{total\_mean}^2}\right)$$
       $$\mu_{\text{sum}} = \ln(\text{total\_mean}) - \frac{\sigma_{\text{sum}}^2}{2}$$
       $$\text{total\_duration} = \max(1, \lceil \exp(\mu_{\text{sum}} + \sigma_{\text{sum}} \cdot z) \rceil)$$

2. **Frappe Estimation Service Layer**:
   - File: `induct_shop/api/estimation_service.py` (Lines 1–158)
     - `get_estimate(item_code, **kwargs)` (Lines 11–41): Looks up item FRT from `Item.custom_frt` (stored in hours, converted to minutes via $\times 60$), resolves $\sigma$ from `Item.item_group` or default, applies target percentile.
     - `get_total_estimate(item_codes, **kwargs)` (Lines 43–134): Accepts JSON list or list of items/dicts (`flat_rate_minutes` or `flat_rate_hours` / `custom_frt` / `qty` in hours), runs Fenton-Wilkinson summation.
     - Defensive column lookup `_get_frt` (Lines 135–156): Uses `frappe.db.has_column("Item", "custom_frt")` to prevent DB crashes if migrations have not executed.

3. **Dual-Resource Capacity & Scheduling APIs**:
   - File: `induct_shop/api/scheduling.py` (Lines 1–384)
     - Time utilities (Lines 7–40): `to_timedelta` and `format_timedelta`.
     - Settings getter (Lines 42–78): `get_shop_settings()`.
     - Lunch-aware effective end times (Lines 81–117):
       ```python
       def effective_end_time(start_time, duration_minutes, break_start=None, break_end=None):
           naive_end = start_td + timedelta(minutes=float(duration_minutes or 0))
           if bs_td and be_td and be_td > bs_td:
               break_duration = be_td - bs_td
               if start_td < bs_td and naive_end > bs_td:
                   return naive_end + break_duration
           return naive_end
       ```
     - Per-bay availability (Lines 120–166): `check_bay_availability(service_bay, date, start_time, duration_minutes, exclude_entry=None)`:
       Interval overlap test: `max(prop_start, ex_start) < min(prop_end, ex_end)` evaluating against `effective_end_time` of both entries.
     - Equipment-matched bays (Lines 168–216): `get_available_bays(date, start_time, duration_minutes, required_tags=None)` evaluates `required_tags_set.issubset(bay_tags_set)`.
     - Automated bay assignment (Lines 218–246): `auto_assign_bay(date, start_time, duration_minutes, required_tags=None)`. Validates technician pool capacity first if `enable_technician_capacity` is active, then grabs the first available capable bay.
     - Available time slot generator (Lines 248–323): `get_available_slots(date, duration_minutes, required_tags=None, service_bay=None)`. Walks operating hours in `default_slot_interval` steps, excludes lunch break start windows, checks `effective_end_time <= operating_hours_end`, computes both bay availability and technician pool availability, sets bottleneck indicator (`"technicians"` vs `"bays"`).
     - Holiday check (Lines 358–383): `is_holiday(date)` queries ERPNext `Holiday` records linked to `holiday_list`.

4. **Technician Pool Gating & Individual Availability**:
   - File: `induct_shop/api/technician_availability.py` (Lines 1–501)
     - Roster query (Lines 14–73): `get_active_technicians(date=None)` filters `Employee` by `designation == technician_designation` and `status == 'Active'`.
     - Leave Application integration (Lines 76–117): Queries `tabLeave Application` where `status = 'Approved'`, `docstatus = 1`, and `from_date <= date <= to_date`.
     - Pool-level capacity gating (Lines 119–207): `get_technician_pool_availability(date, start_time, duration_minutes, exclude_entry=None)`:
       Counts `total_technicians`, `on_leave`, `on_duty = total - on_leave`, `occupied` (technicians with overlapping entries across the lunch-aware effective window), and `available = on_duty - occupied`.
     - Individual technician availability (Lines 210–323): `check_technician_availability(...)` returns boolean `is_available` and reason (`"on_leave"`, `"overlap"`, `"inactive"`). Soft warnings are rendered as Frappe msgprints (`alert=True`).
     - Technician daily work queue (Lines 325–420): `get_technician_queue(employee, date)` computes timeline, gaps between appointments, and utilization percentage against shop operating minutes (excluding lunch).
     - Daily overview (Lines 423–501): `get_daily_technician_overview(date)` aggregates shop-level metrics, bottleneck resource, and unassigned entries.

5. **Client-Side Reusable Scheduling Dialog**:
   - File: `induct_shop/public/js/sales_order.js` (Lines 45–595)
     - Dialog structure:
       - Header badges: Estimated Duration (P80) and Required Equipment tags.
       - Date navigation: Prev Day / Next Day arrow controls, date picker, holiday alert.
       - Available slots grid: Clickable button cards showing time, available bays, and available technicians.
       - Capacity bottleneck summary pill (`Technicians` vs `Bays`).
       - Lunch notice indicator (`(12:00–12:30 lunch break excluded)`).
       - Technician preview: Selected technician utilization bar, total jobs, scheduled duration.
       - Conflict banners: Dynamic red banner for approved leave, yellow banner for overlapping job assignments.
       - Primary action: Calls `auto_assign_bay()` then creates `Schedule Entry`.

6. **Calendar & List Views**:
   - File: `induct_shop/public/js/schedule_entry_calendar.js` and `induct_shop/induct_shop/doctype/schedule_entry/schedule_entry_calendar.js`
     - Whitelisted calendar endpoint: `get_calendar_events` (`induct_shop/api/scheduling_views.py`)
     - Renders events with color borders mapped to entry types or statuses.
     - Spans-lunch badge (`🍱 Spans Lunch`) for jobs spanning the break window.
     - Hover popovers displaying vehicle, customer, bay, technician, and items summary.
   - File: `induct_shop/public/js/schedule_entry_list.js`: Status indicators, badges for bay and technician (unassigned warning badge), quick filters for Today's Jobs, Needs Review, Unassigned Tech, In Progress.

7. **DocType Schemas**:
   - `Service Bay`: `bay_name` (PK), `is_active` (Check), `description` (Small Text), `equipment` (Child table: `Service Bay Equipment`).
   - `Service Bay Equipment`: child table containing `equipment_tag` (Link: `Equipment Tag`).
   - `Equipment Tag`: `tag_name` (PK), `description` (Small Text).
   - `Schedule Entry Type`: `type_name` (PK), `color` (Color), `description` (Small Text), `requires_sales_order` (Check). Default seeded types: `Diagnostic` (blue), `Repair` (green), `Meeting` (purple), `Maintenance / Shop Cleaning` (red), `Internal Service` (orange).
   - `Schedule Entry`: `naming_series` (`SE-.#####`), `entry_type` (Link), `sales_order` (Link), `customer` (Link), `provisional_customer_name` (Data), `scheduled_date` (Date), `scheduled_time` (Time), `status` (Draft, Scheduled, Needs Review, In Progress, Completed, Cancelled), `project` (Link), `repair_vehicle` (Link), `provisional_vehicle_info` (Data), `estimated_duration` (Int), `service_bay` (Link), `assigned_technician` (Link: `Employee`), `items_summary` (Long Text), `notes` (Small Text).

---

## 2. Logic Chain: Analysis & Adaptation to ISM

### 2.1 Domain & Architectural Divergence: `induct_shop` vs `ism`

| Dimension | `induct_shop` Reference Implementation | `ism` Required Target Implementation | Logic / Architectural Rationale |
|---|---|---|---|
| **Core Entity Association** | Linked 1:1 to `Sales Order` via unique constraint (`validate_unique_sales_order`). | Linked to `Repair Order`. Multiple `Schedule Entry` records per `Repair Order` permitted. | Per `ORIGINAL_REQUEST.md`: "One Visit = One Repair Order" and "Multiple Appointments per Repair Order: Multiple appointments on the same Repair Order represent supplemental work or additional labor time required". Unique constraint on `sales_order` must NOT be ported; instead, link `repair_order` with 1:N cardinality. |
| **Early Appointment Lifecycle** | `Schedule Entry` created only after `Sales Order` is submitted (`docstatus == 1`) or created standalone for quick entry. | `Repair Order` introduces early lifecycle stages (`Draft Appointment`, `Scheduled`, `Checked In`, `No Show`) before formal check-in. | Intake staff book appointments before formal customer/vehicle master record creation. `Repair Order` starts at `Draft Appointment` / `Scheduled` with provisional contact details, then formalizes at vehicle intake. |
| **Provisional Data Capture & Formalization** | Stored on `Schedule Entry` (`provisional_customer_name`, `provisional_vehicle_info`). | Stored on both `Repair Order` ("Appointment & Scheduling" tab) and `Schedule Entry`. | Allows provisional phone, email, name, and vehicle notes prior to master `Customer` and `Repair Vehicle` creation. Formalization workflow creates or links master records at vehicle check-in. |
| **Configuration Singleton** | Dedicated singleton DocType `Shop Settings`. | Consolidated into existing `ISM Settings` singleton (`ism/ism/doctype/ism_settings`). | Avoids creating duplicate settings DocTypes. All scheduling configuration belongs in `ISM Settings`. |
| **Duration Estimation Parameters** | Fixed $\sigma = 0.30$, fixed percentile $P80 = 0.80$, no buffer padding. | **Tunable** in `ISM Settings`: `percentile_target` (50–95%, default 80%), `estimation_sigma` (default 0.30), `buffer_padding_minutes` (default 0–30 min), `default_diagnostic_duration` (default 60 min). | Automotive shops require custom risk tolerances and buffer pads based on shop efficiency and vehicle age profiles. |
| **Client-Side Modal Entrypoint** | Injected on `Sales Order` form ("Schedule Service" button). | Reusable modal dialog launched from `Repair Order` form action ("Schedule Appointment"), Desk actions, or Calendar view. | Decoupled client-side dialog that can accept context from a `Repair Order` (or standalone) and support booking initial visits or supplemental work sessions. |

### 2.2 Mathematical Engine Specification with Tunability & Padding

The target estimation service for `ism` must adapt `estimation_light.py` to incorporate dynamic parameters loaded from `ISM Settings`:

1. **Tunable Inputs from `ISM Settings`**:
   - `percentile_target` ($p \in [0.50, 0.95]$, default $0.80$)
   - `estimation_sigma` ($\sigma \in [0.10, 0.80]$, default $0.30$)
   - `buffer_padding_minutes` ($\Delta_{\text{pad}} \ge 0$, default $0$)
   - `default_diagnostic_duration` ($D_{\text{diag}} > 0$, default $60$ min)

2. **Calculation Pipeline**:
   - Step 1: Resolve $z$-score for $p$ using `_Z_SCORES` or the Abramowitz & Stegun rational approximation.
   - Step 2: For single operation with FRT $M$ (in minutes):
     $$D_{\text{pure}} = \lceil \exp(\ln(M) + \sigma \cdot z) \rceil$$
   - Step 3: For multi-operation with FRTs $\{M_1, \dots, M_k\}$ and category sigmas $\{\sigma_1, \dots, \sigma_k\}$:
     $$E[X_i] = \exp\left(\ln(M_i) + \frac{\sigma_i^2}{2}\right), \quad \text{Var}[X_i] = (\exp(\sigma_i^2) - 1)\exp(2\ln(M_i) + \sigma_i^2)$$
     $$\mu_{\text{sum}} = \ln\left(\sum E[X_i]\right) - \frac{1}{2}\ln\left(1 + \frac{\sum \text{Var}[X_i]}{(\sum E[X_i])^2}\right)$$
     $$\sigma_{\text{sum}} = \sqrt{\ln\left(1 + \frac{\sum \text{Var}[X_i]}{(\sum E[X_i])^2}\right)}$$
     $$D_{\text{pure}} = \lceil \exp(\mu_{\text{sum}} + \sigma_{\text{sum}} \cdot z) \rceil$$
   - Step 4: Apply buffer padding:
     $$D_{\text{final}} = D_{\text{pure}} + \Delta_{\text{pad}}$$

---

## 3. Features Discovered

The following comprehensive feature inventory was mined from the reference implementation and cross-referenced with `ORIGINAL_REQUEST.md`:

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|---|---|---|---|---|---|---|
| 1 | Estimation | Analytic Log-Normal P80 | Extracts risk-adjusted duration from median flat-rate time using right-skewed distribution | `flat_rate_minutes` (float), `sigma` (float), `percentile` (float) | `duration` (ceiling int, min 1) | Raises `ValueError` if `flat_rate_minutes <= 0` | `induct_shop/scheduling/estimation_light.py:38` |
| 2 | Estimation | Fenton-Wilkinson Summation | Fits a single log-normal distribution to the sum of independent log-normal operations, exploiting diversification | `flat_rate_list` (list of numbers or dicts with custom `sigma`), `percentile` | `total_duration` (ceiling int, min 1) | Returns 0 for empty list; raises `ValueError` if any FRT $\le 0$ | `induct_shop/scheduling/estimation_light.py:83` |
| 3 | Estimation | Category Sigma Profiles | Category-specific variance parameters (e.g. Brake 0.20, Tire 0.15, Suspension 0.35, Electrical 0.50) | `item_group` (str) | `sigma` (float) | Returns `DEFAULT_SIGMA` (0.30) if category unrecognized | `induct_shop/scheduling/estimation_light.py:20` |
| 4 | Estimation | Rational Z-Score Approximation | Zero-scipy Abramowitz & Stegun (26.2.23) inverse CDF approximation for continuous percentiles | `percentile` (float $0.50 \le p < 1.0$) | `z` (float, error $< 4.5 \times 10^{-4}$) | Returns exact table lookup if $p \in \{0.50, 0.75, 0.80, 0.85, 0.90, 0.95\}$ | `induct_shop/scheduling/estimation_light.py:71` |
| 5 | Capacity | Lunch-Aware Effective End Time | Adds shop break duration to job end time if work window spans across lunch break | `start_time` (Time/str), `duration_minutes` (int), `break_start`, `break_end` | `effective_end_time` (timedelta) | Returns naive end time if break not configured or window does not span break | `induct_shop/api/scheduling.py:81` |
| 6 | Capacity | Per-Bay Overlap Validation | Prevents double-booking on physical service bays using lunch-aware time intervals | `service_bay` (str), `date` (str), `start_time`, `duration_minutes`, `exclude_entry` | `is_available` (bool) | Schedule Entry controller raises `ValidationError` on overlap | `induct_shop/api/scheduling.py:120` |
| 7 | Capacity | Equipment Tag Filtering | Evaluates if a bay possesses all equipment tags required for scheduled operations (superset match) | `date`, `start_time`, `duration_minutes`, `required_tags` (list) | List of matching available bay dicts | Returns empty list if no bay satisfies tag requirements | `induct_shop/api/scheduling.py:168` |
| 8 | Capacity | Automated Bay Assignment | Automatically picks the first capable, free service bay using earliest-fit strategy | `date`, `start_time`, `duration_minutes`, `required_tags` | `bay_name` (str) | Raises `ValidationError` if all bays full or technician pool exhausted | `induct_shop/api/scheduling.py:218` |
| 9 | Capacity | Slot Generator with Capacity Indicators | Generates time slots within operating hours, filtering lunch and reporting bay/technician bottleneck | `date`, `duration_minutes`, `required_tags`, `service_bay` | List of slot dicts with `available_bays`, `available_technicians`, `bottleneck` | Excludes slots with `effective_capacity < 1` or exceeding operating hours | `induct_shop/api/scheduling.py:248` |
| 10 | Capacity | Technician Designation Roster | Filters active technicians dynamically from standard `Employee` DocType by designation | None (reads `technician_designation` from settings) | List of active technician dicts | Empty list if no active employees match designation | `induct_shop/api/technician_availability.py:14` |
| 11 | Capacity | HRMS Leave Application Integration | Excludes technicians on approved leave from on-duty pool | `employee` (str), `date` (str) | Dict `{on_leave: bool, half_day: bool}` | Defensively handles missing `Leave Application` DocType | `induct_shop/api/technician_availability.py:76` |
| 12 | Capacity | Technician Pool-Level Gating | Enforces that active appointments at any time $T$ do not exceed on-duty technicians | `date`, `start_time`, `duration_minutes`, `exclude_entry` | Pool dict `{total, on_leave, on_duty, occupied, available, is_available}` | Schedule Entry controller raises `ValidationError` if `available == 0` | `induct_shop/api/technician_availability.py:119` |
| 13 | Capacity | Individual Technician Overlap Check | Detects individual technician double-booking and approved leave conflicts | `employee`, `date`, `start_time`, `duration_minutes`, `exclude_entry` | Dict `{is_available, reason, conflicts}` | Emits non-blocking soft warnings (`msgprint`) allowing manager override | `induct_shop/api/technician_availability.py:210` |
| 14 | Capacity | Master Capacity Toggle | Master switch to enable/disable technician capacity gating, falling back to bay-only model | `enable_technician_capacity` in settings | Bypasses technician gating when 0 | Returns dummy capacity (999) without error | `induct_shop/api/technician_availability.py:130` |
| 15 | Capacity | Technician Queue & Utilization | Calculates personal daily timeline, gap intervals, and shop utilization percentage | `employee`, `date` | Dict `{entries, total_jobs, total_minutes, utilization_pct, gaps}` | Handles zero appointments gracefully | `induct_shop/api/technician_availability.py:325` |
| 16 | Capacity | Daily Technician Overview | Shop-wide dashboard summarizing total scheduled hours, bottleneck, and unassigned entries | `date` | Overview dict `{technicians, unassigned_entries, shop_summary}` | Automatically calculates shop bottleneck | `induct_shop/api/technician_availability.py:423` |
| 17 | Capacity | ERPNext Holiday Awareness | Identifies shop closures based on linked ERPNext `Holiday List` | `date` (str) | Dict `{is_holiday: bool, description: str}` | Falls back to false if holiday list not configured | `induct_shop/api/scheduling.py:358` |
| 18 | Data Model | Service Bay DocType | Represents physical shop bays with active state and equipment table | `bay_name`, `is_active`, `description`, `equipment` | Service Bay Document | Enforces unique `bay_name` | `induct_shop/doctype/service_bay` |
| 19 | Data Model | Equipment Tag System | Tag taxonomy for matching bays to specialized vehicle repair capabilities | `tag_name`, `description` | Equipment Tag Document | Enforces unique `tag_name` | `induct_shop/doctype/equipment_tag` |
| 20 | Data Model | Schedule Entry Type DocType | Categorizes appointments (Diagnostic, Repair, Meeting, Maintenance, Internal) with UI colors | `type_name`, `color`, `description`, `requires_sales_order` | Schedule Entry Type Document | Auto-seeded during app installation/migration | `induct_shop/doctype/schedule_entry_type` |
| 21 | Data Model | Schedule Entry DocType | Core schedule appointment record storing time window, bay, tech, and notes | All scheduling and provisional fields | Schedule Entry Document | Validates bay overlap and tech pool on save | `induct_shop/doctype/schedule_entry` |
| 22 | UI / UX | Reusable Modal Date Navigator | Step backward/forward by day with immediate holiday alert and slot refresh | `scheduled_date` | Updated date display, slot buttons | Disables booking on detected holidays | `induct_shop/public/js/sales_order.js:298` |
| 23 | UI / UX | Header Badges | Visual pill badges displaying estimated duration (P80) and detected equipment tags | `estimated_duration`, `required_tags` | HTML badge bar | Displays "No estimation available" if empty | `induct_shop/public/js/sales_order.js:268` |
| 24 | UI / UX | Slot Selection Grid | Clickable slot buttons showing available bays and technicians | List of available slots | Primary active button styling | Displays empty-state banner when no slots available | `induct_shop/public/js/sales_order.js:403` |
| 25 | UI / UX | Technician Preview & Conflict Warnings | Live preview of technician daily utilization with warning banners for leave (red) and overlap (yellow) | `assigned_technician`, `scheduled_date`, `selected_slot` | HTML utilization line and alert banners | Automatically clears warnings when slot changes | `induct_shop/public/js/sales_order.js:483` |
| 26 | UI / UX | Calendar View Integration | FullCalendar integration color-coded by entry type/status with lunch gap badges | Schedule Entry query filters | Calendar event blocks with rich popovers | Shows unassigned bay/technician clearly | `induct_shop/public/js/schedule_entry_calendar.js` |
| 27 | UI / UX | List View Quick Filters | Preset buttons on list view for All Entries, Today's Jobs, Needs Review, Unassigned Tech, In Progress | Filter actions | Filtered Schedule Entry list view | Clear and reset filter functionality | `induct_shop/public/js/schedule_entry_list.js` |
| 28 | Lifecycle | Provisional Info Resolution | Displays provisional customer and vehicle strings in quick entry and calendar when formal links empty | Provisional fields on Schedule Entry | Resolved title string in UI/calendar | Fallback to "Guest" / "No Vehicle" | `induct_shop/doctype/schedule_entry/schedule_entry.py:8` |

---

## 4. Edge Cases

| # | Feature | Input / Condition | Observed Reference Behavior | Required ISM Behavior |
|---|---|---|---|---|
| 1 | `effective_end_time` | Job starts before lunch (11:00) with 120 min duration and lunch is 12:00–12:30 | Effective end time extends from naive 13:00 to 13:30 (+30 min break duration). | Same. Must ensure calendar blocks and overlap algorithms use 13:30. |
| 2 | `effective_end_time` | Job starts at 08:00 with 120 min duration (finishes at 10:00, before lunch) | No break duration is added; effective end is 10:00. | Same. |
| 3 | `effective_end_time` | Job starts after lunch (13:00) with 90 min duration | No break duration is added; effective end is 14:30. | Same. |
| 4 | `effective_end_time` | Break start or break end is null in settings | Effective end equals naive end (start + duration). | Same. Defensively handles null break settings in `ISM Settings`. |
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
| 19 | `Schedule Entry` cardinality | Multiple appointments on same `Repair Order` (e.g. supplemental work session) | N/A in `induct_shop` (threw `DuplicateEntryError` on `Sales Order`). | Must ALLOW multiple `Schedule Entry` records linked to the same `Repair Order` while enforcing individual bay and technician pool constraints per entry. |
| 20 | Provisional Intake Transition | Provisional customer details (name, phone, email) on RO converted to master `Customer` | In `induct_shop`, only quick entry fields existed on `Schedule Entry`. | In `d:\ism`, intake transition formalizes provisional customer & vehicle records on vehicle check-in, populating master links and back-linking `Schedule Entry`. |

---

## 5. Caveats

1. **ERPNext Sales Order vs ISM Repair Order**: In `induct_shop`, scheduling was initiated from submitted ERPNext `Sales Order` documents. In `d:\ism`, the primary transaction is `Repair Order` (which is non-submittable, `is_submittable = 0`). The scheduling modal and controller logic must bind to `Repair Order`.
2. **Cardinality Shift**: `induct_shop` enforced a strict 1:1 relation between `Sales Order` and `Schedule Entry` (`validate_unique_sales_order`). In `d:\ism`, the core business rule dictates that a single `Repair Order` can have multiple `Schedule Entry` appointments to accommodate supplemental work sessions during an active shop visit.
3. **Consolidated Settings**: `induct_shop` used a dedicated singleton DocType `Shop Settings`. `d:\ism` requires consolidating all scheduling parameters (hours, break, slot interval, holiday list, technician designation, capacity toggle, and tunable estimation parameters: percentile, sigma, buffer padding, default diagnostic duration) directly into `ISM Settings`.
4. **Buffer Padding**: In `induct_shop`, buffer padding was explicitly omitted (P80 was used raw). In `d:\ism`, `ORIGINAL_REQUEST.md` mandates configurable `buffer_padding_minutes` (0–30 min) added to the Fenton-Wilkinson percentile duration.

---

## 6. Conclusion

The reference repository `induct_shop` provides a robust, field-tested implementation of:
1. **Lightweight log-normal and Fenton-Wilkinson duration estimation** (`estimation_light.py` and `estimation_service.py`), eliminating external scientific dependencies via analytic z-scores and rational Abramowitz & Stegun approximation.
2. **Dual-resource capacity management** (`scheduling.py` and `technician_availability.py`), coordinating physical service bays (with equipment tag matching) and technician workforce capacity (with HRMS leave exclusion and soft overlap warnings).
3. **Lunch-aware effective scheduling**, ensuring that appointments spanning the shop break window calculate accurate effective end times without double-booking.
4. **Interactive client-side scheduling modal**, offering date navigation with holiday detection, visual slot grid with bottleneck indication, technician load preview, and auto bay assignment.
5. **FullCalendar and list view integration**, rendering color-coded entries, lunch indicators, and hover popovers.

All of these capabilities map directly to requirements R1–R6 in `ORIGINAL_REQUEST.md`, with well-defined adaptations for `d:\ism`:
- Shift from `Sales Order` 1:1 binding to `Repair Order` 1:N binding.
- Introduction of the "Appointment & Scheduling" tab and early lifecycle stages (`Draft Appointment`, `Scheduled`, `Checked In`, `No Show`) on `Repair Order`.
- Capture and formalization of provisional customer and vehicle information.
- Consolidation of shop scheduling and tunable estimation parameters into `ISM Settings`.
- Addition of standard buffer padding ($\Delta_{\text{pad}}$) on top of the Fenton-Wilkinson percentile estimate.

---

## 7. Verification Method

To independently verify the reference findings and subsequent implementation in `d:\ism`:

1. **Verify Reference Math in `induct_shop`**:
   Run the pure-Python unit tests in the temp clone:
   ```powershell
   python -m unittest C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop\tests\test_estimation_light.py
   ```
   *Expected outcome*: 6 passed tests verifying 60 min FRT at P80 = 78 min, diversification effect ($3 \times 60$ min = 215 min vs naive 234 min), and invalid input exceptions.

2. **Verify Target Schemas & Migrations in `d:\ism`**:
   Verify creation of DocTypes in `d:\ism\ism\ism\doctype\`:
   - `service_bay/service_bay.json`
   - `equipment_tag/equipment_tag.json`
   - `service_bay_equipment/service_bay_equipment.json`
   - `schedule_entry_type/schedule_entry_type.json`
   - `schedule_entry/schedule_entry.json`
   - Updated `ism_settings/ism_settings.json`
   - Updated `repair_order/repair_order.json`
   Run:
   ```powershell
   bench --site ism migrate
   ```

3. **Verify Pure Math & Service APIs in `d:\ism`**:
   Execute automated tests:
   ```powershell
   bench run-tests --app ism --doctype "Schedule Entry"
   bench run-tests --app ism --module ism.tests.test_scheduling
   ```
   *Verification conditions*:
   - Single item FRT 60 min, $\sigma=0.30$, P80 + 15 min buffer = $78 + 15 = 93$ min.
   - Job spanning 12:00–12:30 lunch extends effective end by exactly 30 minutes.
   - Double-booking same bay raises `ValidationError`.
   - Exhausting on-duty technician pool raises `ValidationError`.
   - Multiple `Schedule Entry` records link to single `Repair Order` without error.
