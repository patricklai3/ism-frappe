# Project: ISM Dual-Resource Capacity Scheduling Integration

## Architecture
Integrate a dual-resource capacity scheduling system into ISM's Repair Order workflow, adapting proven scheduling capabilities from the `induct_shop` repository while tailoring the user experience to a reusable client-side scheduling modal, early appointment lifecycle stages, tunable estimation algorithms, and Frappe calendar views.

```
+-----------------------------------------------------------------------------------------+
|                                    Client UI Layer                                      |
|  +---------------------------+   +------------------------------+   +-----------------+ |
|  | Repair Order Form Action  |   | Reusable Scheduling Dialog   |   | Schedule Entry  | |
|  | "Schedule Appointment"    |-->| ism.SchedulingDialog         |   | Calendar View   | |
|  | Appointment Tab (#0)      |   | (date nav, slot grid, alerts)|   | & List Filters  | |
|  +---------------------------+   +------------------------------+   +-----------------+ |
+-----------------------------------------------|-----------------------------------------+
                                                |
                                                v
+-----------------------------------------------------------------------------------------+
|                                Server API & Service Layer                               |
|  +--------------------------------+  +------------------------------+  +--------------+ |
|  | Scheduling API                 |  | Technician Availability API  |  | Estimation   | |
|  | (effective_end_time, slots,    |  | (active techs, leave check,  |  | Service      | |
|  |  bay conflict, holiday check)  |  |  pool gating, soft warnings) |  | (F-W sum,    | |
|  +--------------------------------+  +------------------------------+  |  z-approx)   | |
|                                                                        +--------------+ |
+-----------------------------------------------|-----------------------------------------+
                                                |
                                                v
+-----------------------------------------------------------------------------------------+
|                                Data Model & Settings Layer                              |
|  +------------------------------------------------------------------------------------+ |
|  | DocTypes: Service Bay, Equipment Tag, Service Bay Equipment,                       | |
|  |           Schedule Entry Type, Schedule Entry, Repair Order (Early Lifecycle)      | |
|  | Settings: ISM Settings (Shop hours, break, slots, holiday list, capacity toggle,   | |
|  |                        tunable estimation: percentile, sigma, buffer, diagnostic)  | |
|  +------------------------------------------------------------------------------------+ |
+-----------------------------------------------------------------------------------------+
```

### Core Domain Invariants
1. **One Visit = One Repair Order**: Each customer visit corresponds strictly to a single Repair Order. When the vehicle leaves the shop, it must be checked out and the Repair Order closed. A subsequent customer visit is always handled under a new Repair Order.
2. **Multiple Appointments per Repair Order**: Multiple appointments on the same Repair Order represent supplemental work or additional labor time required that cannot be completed within the original appointment window while the Repair Order is still active.

---

## Feature Inventory

| # | Feature | Description | Milestone | Source |
|---|---|---|---|---|
| 1 | Analytic Log-Normal P80 | Extracts risk-adjusted duration from median flat-rate time using right-skewed distribution | M2 | Survey / induct_shop |
| 2 | Fenton-Wilkinson Summation | Fits a single log-normal distribution to the sum of independent log-normal operations | M2 | Survey / induct_shop |
| 3 | Category Sigma Profiles | Category-specific variance parameters (Brake 0.20, Tire 0.15, Suspension 0.35, Electrical 0.50, etc.) | M2 | Survey / induct_shop |
| 4 | Rational Z-Score Approximation | Zero-scipy Abramowitz & Stegun (26.2.23) inverse CDF approximation for continuous percentiles | M2 | Survey / induct_shop |
| 5 | Configurable Buffer Padding | Adds standard shop padding minutes (0–30 min) to risk-adjusted estimate | M2 | Survey / ORIGINAL_REQUEST R4 |
| 6 | Lunch-Aware Effective End Time | Adds shop break duration to job end time if work window spans across lunch break | M3 | Survey / induct_shop |
| 7 | Per-Bay Overlap Validation | Prevents double-booking on physical service bays using lunch-aware time intervals | M3 | Survey / induct_shop |
| 8 | Equipment Tag Filtering | Evaluates if a bay possesses all equipment tags required for scheduled operations | M3 | Survey / induct_shop |
| 9 | Automated Bay Assignment | Automatically picks the first capable, free service bay using earliest-fit strategy | M3 | Survey / induct_shop |
| 10 | Slot Generator with Capacity Indicators | Generates time slots within operating hours, filtering lunch and reporting bay/technician bottleneck | M3 | Survey / induct_shop |
| 11 | Technician Designation Roster | Filters active technicians dynamically from standard Employee DocType by designation | M3 | Survey / induct_shop |
| 12 | HRMS Leave Application Integration | Excludes technicians on approved leave from on-duty pool | M3 | Survey / induct_shop |
| 13 | Technician Pool-Level Gating | Enforces that active appointments at any time do not exceed on-duty technicians | M3 | Survey / induct_shop |
| 14 | Individual Technician Overlap Check | Detects individual technician double-booking and approved leave conflicts with soft warnings | M3 | Survey / induct_shop |
| 15 | Master Capacity Toggle | Master switch to enable/disable technician capacity gating, falling back to bay-only model | M3 | Survey / induct_shop |
| 16 | Technician Queue & Utilization | Calculates personal daily timeline, gap intervals, and shop utilization percentage | M3 | Survey / induct_shop |
| 17 | Daily Technician Overview | Shop-wide dashboard summarizing total scheduled hours, bottleneck, and unassigned entries | M3 | Survey / induct_shop |
| 18 | ERPNext Holiday Awareness | Identifies shop closures based on linked ERPNext Holiday List | M3 | Survey / induct_shop |
| 19 | Service Bay DocType | Represents physical shop bays with active state and equipment table | M1 | Survey / induct_shop |
| 20 | Equipment Tag System | Tag taxonomy for matching bays to specialized vehicle repair capabilities | M1 | Survey / induct_shop |
| 21 | Schedule Entry Type DocType | Categorizes appointments (Diagnostic, Repair, Meeting, Maintenance, Internal) with UI colors | M1 | Survey / induct_shop |
| 22 | Schedule Entry DocType | Core schedule appointment record storing time window, bay, tech, and notes | M1 | Survey / induct_shop |
| 23 | Reusable Modal Date Navigator | Step backward/forward by day with immediate holiday alert and slot refresh | M5 | Survey / induct_shop |
| 24 | Header Badges | Visual pill badges displaying estimated duration (P80 + buffer) and detected equipment tags | M5 | Survey / induct_shop |
| 25 | Slot Selection Grid | Clickable slot buttons showing available bays and technicians | M5 | Survey / induct_shop |
| 26 | Technician Preview & Conflict Warnings | Live preview of technician daily utilization with warning banners for leave and overlap | M5 | Survey / induct_shop |
| 27 | Calendar View Integration | FullCalendar integration color-coded by entry type/status with lunch gap badges | M5 | Survey / induct_shop |
| 28 | List View Quick Filters | Preset buttons on list view for All Entries, Today's Jobs, Needs Review, Unassigned Tech | M5 | Survey / induct_shop |
| 29 | Early Appointment Stages on RO | Supports Draft Appointment, Scheduled, Checked In, No Show on Repair Order | M4 | Survey / ORIGINAL_REQUEST R1 |
| 30 | Provisional Intake Formalization | Formalizes provisional customer/vehicle data into master Customer and Repair Vehicle on check-in | M4 | Survey / ORIGINAL_REQUEST R1 |
| 31 | Supplemental Scheduling | Schedules additional work sessions on an active Repair Order without closing or replacing the RO | M4 | Survey / ORIGINAL_REQUEST R1 |

---

## Milestones

| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Resource & Capacity Data Models & Settings Consolidation | Standard DocTypes (`Service Bay`, `Equipment Tag`, `Service Bay Equipment`, `Schedule Entry Type`, `Schedule Entry`), `ISM Settings` scheduling & estimation fields, and seeding routines in `setup.py` | None | DONE |
| M2 | Tunable Estimation Engine & Pure Math Core | Pure-Python log-normal duration estimation, multi-operation Fenton-Wilkinson summation, Abramowitz & Stegun inverse CDF approximation, category sigmas, dynamic parameters from `ISM Settings`, and buffer padding | M1 | DONE |
| M3 | Dual-Resource Capacity & Scheduling APIs | Server-side APIs in `scheduling.py` and `technician_availability.py`: lunch-aware effective end times, bay conflict detection, equipment filtering, slot generation, technician leave integration, pool gating, and Schedule Entry controller validations | M1, M2 | DONE |
| M4 | Repair Order Integration & Early Appointment Lifecycle | `Repair Order` schema (`tab_appointment` initial tab break, provisional fields, early status options `Draft Appointment`, `Scheduled`, `Checked In`, `No Show`, relaxed mandatory flags with depends_on), intake formalization method `formalize_provisional_info()`, "Schedule Appointment" form button, and client routing | M1, M3 | DONE |
| M5 | Reusable Scheduling Modal & Frappe Calendar View | Modular client-side scheduling dialog `ism.SchedulingDialog` (`scheduling_modal.js`), date navigation, slot grid with capacity indicators, equipment tag filter, tech preview & warnings, Schedule Entry Calendar View and list filters | M3, M4 | DONE |
| M6 | Automated Test Suite & E2E Verification | Execution and verification of complete test suite (Tiers 1–4 requirement-driven E2E tests, followed by Tier 5 adversarial coverage hardening) | M1, M2, M3, M4, M5, TEST_READY | DONE |

---

## Interface Contracts

### 1. `ism.scheduling.estimation_light` ↔ `ism.api.estimation_service`
- **Functions**:
  - `estimate_duration(flat_rate_minutes: float, sigma: float = 0.30, percentile: float = 0.80) -> int`:
    - Inputs: `flat_rate_minutes > 0`, `sigma in [0.05, 1.0]`, `percentile in [0.50, 0.99]`.
    - Returns: Integer ceiling of log-normal duration (min 1).
    - Errors: Raises `ValueError` if `flat_rate_minutes <= 0` or invalid parameters.
  - `estimate_total_duration(flat_rate_list: List[Union[float, Dict]], sigma: float = 0.30, percentile: float = 0.80) -> int`:
    - Inputs: List of items/dicts containing `flat_rate_minutes` (and optional per-item `sigma`).
    - Returns: Integer ceiling of Fenton-Wilkinson sum (min 1, or 0 if empty).
    - Errors: Raises `ValueError` if any FRT $\le 0$.

### 2. `ism.api.estimation_service` ↔ Client / Other Modules
- **Whitelisted Functions**:
  - `get_estimate(item_code: str, **kwargs) -> Dict[str, Any]`:
    - Looks up `Item.custom_frt` (or kwargs), resolves sigma by item group, queries `ISM Settings` for `estimation_percentile`, `estimation_sigma`, and `buffer_padding_minutes`.
    - Returns: `{"duration_minutes": int, "p_pure": int, "buffer_padding": int, "percentile": float, "sigma": float}`.
  - `get_total_estimate(item_codes: Union[str, List], **kwargs) -> Dict[str, Any]`:
    - Accepts list of items, calculates multi-operation Fenton-Wilkinson estimate + buffer padding from `ISM Settings`.
    - Returns: `{"total_duration_minutes": int, "p_pure": int, "buffer_padding": int, "operations_count": int}`.

### 3. `ism.api.scheduling` ↔ `Schedule Entry` & Scheduling Modal
- **Functions**:
  - `effective_end_time(start_time: Union[str, time, timedelta], duration_minutes: int, break_start=None, break_end=None) -> timedelta`:
    - If `start < break_start` and `naive_end > break_start`, adds `(break_end - break_start)`.
    - Returns: Adjusted `timedelta`.
  - `check_bay_availability(service_bay: str, date: str, start_time: str, duration_minutes: int, exclude_entry: str = None) -> bool`:
    - Checks overlap against existing active entries for `service_bay` using lunch-aware intervals.
    - Returns: `True` if bay is free, `False` if collision.
  - `get_available_slots(date: str, duration_minutes: int, required_tags: List[str] = None, service_bay: str = None) -> List[Dict]`:
    - Walks operating hours by `default_slot_interval`, excludes lunch slots and past closing.
    - Computes `available_bays`, `available_technicians`, `effective_capacity`, and `bottleneck`.
    - Returns: List of valid slot objects.
  - `auto_assign_bay(date: str, start_time: str, duration_minutes: int, required_tags: List[str] = None) -> str`:
    - Validates technician pool capacity (if enabled), finds first capable free bay.
    - Returns: `bay_name` string. Raises `frappe.ValidationError` if none available.
  - `is_holiday(date: str) -> Dict[str, Any]`:
    - Returns `{"is_holiday": bool, "description": str}` based on `ISM Settings.holiday_list`.

### 4. `ism.api.technician_availability` ↔ `Schedule Entry` & Modal
- **Functions**:
  - `get_active_technicians(date: str = None) -> List[Dict]`:
    - Queries active `Employee` records with `designation == ISM Settings.technician_designation`.
    - Returns: List of employee dicts with leave status.
  - `get_technician_pool_availability(date: str, start_time: str, duration_minutes: int, exclude_entry: str = None) -> Dict[str, Any]`:
    - Returns: `{"total_technicians": int, "on_leave": int, "on_duty": int, "occupied": int, "available": int, "is_available": bool}`.
  - `check_technician_availability(employee: str, date: str, start_time: str, duration_minutes: int, exclude_entry: str = None) -> Dict[str, Any]`:
    - Returns: `{"is_available": bool, "reason": str, "conflicts": List[Dict]}`.

### 5. `Repair Order` ↔ `Schedule Entry`
- **Relationship**: 1:N cardinality.
- **Fields**:
  - `Schedule Entry.repair_order` (Link: `Repair Order`).
  - `Repair Order.appointments` (Virtual Table or linked query showing all Schedule Entries where `repair_order == doc.name`).
- **Formalization Hook**:
  - `Repair Order.formalize_provisional_info()`: Creates/resolves master `Customer` and `Repair Vehicle` records from provisional fields, updates `customer` and `repair_vehicle` on all linked `Schedule Entry` records.

---

## Code Layout

```
d:\ism\
├── ism\
│   ├── ism\
│   │   ├── doctype\
│   │   │   ├── service_bay\
│   │   │   │   ├── service_bay.json
│   │   │   │   ├── service_bay.py
│   │   │   │   └── test_service_bay.py
│   │   │   ├── equipment_tag\
│   │   │   │   ├── equipment_tag.json
│   │   │   │   └── equipment_tag.py
│   │   │   ├── service_bay_equipment\
│   │   │   │   ├── service_bay_equipment.json
│   │   │   │   └── service_bay_equipment.py
│   │   │   ├── schedule_entry_type\
│   │   │   │   ├── schedule_entry_type.json
│   │   │   │   └── schedule_entry_type.py
│   │   │   ├── schedule_entry\
│   │   │   │   ├── schedule_entry.json
│   │   │   │   ├── schedule_entry.py
│   │   │   │   ├── schedule_entry_calendar.js
│   │   │   │   ├── schedule_entry_list.js
│   │   │   │   └── test_schedule_entry.py
│   │   │   ├── ism_settings\
│   │   │   │   ├── ism_settings.json
│   │   │   │   └── ism_settings.py
│   │   │   └── repair_order\
│   │   │       ├── repair_order.json
│   │   │       ├── repair_order.py
│   │   │       └── repair_order.js
│   ├── api\
│   │   ├── scheduling.py
│   │   ├── technician_availability.py
│   │   ├── estimation_service.py
│   │   └── scheduling_views.py
│   ├── scheduling\
│   │   ├── __init__.py
│   │   └── estimation_light.py
│   ├── public\
│   │   └── js\
│   │       └── scheduling_modal.js
│   ├── setup.py
│   ├── hooks.py
│   └── tests\
│       ├── test_estimation.py
│       ├── test_scheduling.py
│       ├── test_technician_availability.py
│       └── test_repair_order_scheduling.py
```
