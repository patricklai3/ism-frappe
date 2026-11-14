# Project: ISM Scheduling and Agent OS Integration

## Architecture

ISM integrates dual-resource shop scheduling, estimation, and repair order management with an external Agent OS adapter layer (`ism-agent-os`) and minimal Frappe Desk views.

```text
+-----------------------------------------------------------------------------------------+
|                         External Agent OS & Minimal Desk UI Layer                       |
|  +---------------------------+   +------------------------------+   +-----------------+ |
|  | ism-agent-os (CLI/REST)   |   | Repair Order & Vehicle Forms |   | Schedule Entry  | |
|  | ism_agent_os.client / cli |   | repair_order.js, list views  |   | Calendar & List | |
|  +-------------|-------------+   +--------------|---------------+   +--------|--------+ |
+----------------|--------------------------------|----------------------------|----------+
                 v                                v                            v
+-----------------------------------------------------------------------------------------+
|                                Server API & Agent Facade Layer                          |
|  +---------------------------+   +------------------------------+   +-----------------+ |
|  | Agent Facade              |   | Scheduling & Tech APIs       |   | Estimation      | |
|  | ism.api.agent             |   | scheduling.py                |   | Service         | |
|  | ism.api.approval / sms    |   | technician_availability.py   |   | (F-W sum, z)    | |
|  +-------------|-------------+   +--------------|---------------+   +--------|--------+ |
+----------------|--------------------------------|----------------------------|----------+
                 v                                v                            v
+-----------------------------------------------------------------------------------------+
|                                Data Model & Settings Layer                              |
|  +------------------------------------------------------------------------------------+ |
|  | DocTypes: Service Bay, Equipment Tag, Service Bay Equipment,                       | |
|  |           Schedule Entry Type, Schedule Entry, Repair Order, Shop Action Item      | |
|  | Settings: ISM Settings (default_labor_rate, sms_gateway_provider, shop hours,      | |
|  |           break window, slots, holiday list, capacity toggle, estimation tuning)   | |
|  +------------------------------------------------------------------------------------+ |
+-----------------------------------------------------------------------------------------+
```

### Core Domain Invariants

1. **One Visit = One Repair Order**: Each customer visit maps to one Repair Order. When the vehicle leaves the shop, staff check out and close the Repair Order. A new visit starts a new Repair Order.
2. **Multiple Appointments per Repair Order**: Additional Schedule Entry records on an active Repair Order cover supplemental work or follow-up labor sessions during the same visit.

---

## Feature Inventory

| # | Feature | Description | Milestone |
|---|---|---|---|
| 1 | Analytic Log-Normal P80 | Computes risk-adjusted job duration from median flat-rate time using a right-skewed log-normal distribution. | M2 |
| 2 | Fenton-Wilkinson Summation | Approximates the sum of independent log-normal service durations as one log-normal distribution. | M2 |
| 3 | Category Sigma Profiles | Applies category variance parameters (for example Brake 0.20, Tire 0.15, Suspension 0.35, Electrical 0.50). | M2 |
| 4 | Rational Z-Score Approximation | Uses the zero-SciPy Abramowitz and Stegun (26.2.23) inverse CDF approximation for continuous percentiles. | M2 |
| 5 | Configurable Buffer Padding | Adds shop buffer minutes from `ISM Settings` to risk-adjusted duration estimates. | M2 |
| 6 | Lunch-Aware Effective End Time | Adds break duration to job end time when a scheduled window crosses the shop break window. | M3 |
| 7 | Per-Bay Overlap Validation | Prevents double-booking on physical bays across lunch-aware time intervals. | M3 |
| 8 | Equipment Tag Filtering | Matches required equipment tags against each Service Bay equipment list. | M3 |
| 9 | Automated Bay Assignment | Picks the earliest free capable Service Bay for a time window. | M3 |
| 10 | Slot Generator with Capacity Indicators | Generates time slots in operating hours, skips breaks, and reports bay and technician bottlenecks. | M3 |
| 11 | Technician Designation Roster | Filters active ERPNext Employee records by the configured technician designation. | M3 |
| 12 | HRMS Leave Application Integration | Excludes technicians on approved leave from the on-duty pool. | M3 |
| 13 | Technician Pool-Level Gating | Caps concurrent appointments at the active on-duty technician count. | M3 |
| 14 | Individual Technician Overlap Check | Detects individual technician overlaps and leave conflicts. | M3 |
| 15 | Master Capacity Toggle | Toggles technician capacity gating via `ISM Settings.enable_technician_capacity`. | M3 |
| 16 | Technician Queue & Utilization | Computes daily technician timelines, open gaps, and utilization percentages. | M3 |
| 17 | Daily Technician Overview | Summarizes scheduled hours, bottlenecks, and unassigned entries for a day. | M3 |
| 18 | ERPNext Holiday Awareness | Detects shop closures from the linked ERPNext Holiday List in `ISM Settings`. | M3 |
| 19 | Service Bay DocType | Stores physical shop bays, active flags, and equipment tables. | M1 |
| 20 | Equipment Tag System | Defines specialized equipment tags for bay capability matching. | M1 |
| 21 | Schedule Entry Type DocType | Categorizes appointments (Diagnostic, Repair, Maintenance, Meeting, Internal Service) with colors. | M1 |
| 22 | Schedule Entry DocType | Stores scheduled time window, bay, technician, and status links. | M1 |
| 23 | Slot & Date Availability Query APIs | Server APIs (`get_available_slots`, `find_available_slots`, `is_holiday`) replace the retired client-side `SchedulingDialog` modal. | M3 / UI Replacement |
| 24 | Agent Estimation & Tag Resolution | Server estimation and service-package resolvers compute P80 durations and equipment tags for callers. | M2 / Agent OS |
| 25 | Bay & Technician Slot Payloads | API payloads return available bays, technician counts, and bottleneck flags per slot. | M3 / Agent OS |
| 26 | Technician Conflict Previews | Scheduling APIs return leave and overlap warnings without relying on modal widgets. | M3 / Agent OS |
| 27 | Calendar View Integration | Frappe Desk Calendar view colors Schedule Entry items by type and status. | M5 |
| 28 | List View Quick Filters | Desk List views (`Schedule Entry`, `Repair Order`, `Shop Action Item`) expose focused presets. | M5 |
| 29 | Early Appointment Stages on RO | Supports `Draft Appointment`, `Scheduled`, `Checked In`, and `No Show` on Repair Order. | M4 |
| 30 | Provisional Intake Formalization | `formalize_provisional_info()` converts provisional intake fields into master `Customer` and `Repair Vehicle` records. | M4 |
| 31 | Supplemental Scheduling | Schedules follow-up work on an open Repair Order without closing the visit. | M4 |

---

## Milestones

| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Resource & Capacity Data Models & Settings | DocTypes (`Service Bay`, `Equipment Tag`, `Service Bay Equipment`, `Schedule Entry Type`, `Schedule Entry`), `ISM Settings`, and idempotent seeding in `ism/setup.py`. | None | DONE |
| M2 | Tunable Estimation Engine | Pure-Python log-normal duration math, Fenton-Wilkinson summation, Abramowitz and Stegun Z-scores, category sigmas, and buffer padding. | M1 | DONE |
| M3 | Dual-Resource Capacity & Scheduling APIs | `scheduling.py` and `technician_availability.py` APIs plus Schedule Entry validations. | M1, M2 | DONE |
| M4 | Repair Order Integration & Early Lifecycle | Repair Order early stages, provisional intake formalization, and appointment linkage. | M1, M3 | DONE |
| M5 | Desk Views & Agent-First Interface | Desk Calendar/List views (`schedule_entry_calendar.js`, `schedule_entry_list.js`, `shop_action_item_list.js`) and `ism.api.agent` facade after retiring `scheduling_modal.js`. | M3, M4 | DONE |
| M6 | Automated Test Suite | Domain, scheduling, estimation, approval, facade, settings, and Agent OS unit tests. | M1–M5 | ACTIVE |

---

## Interface Contracts

### 1. `ism.scheduling.estimation_light` ↔ `ism.api.estimation_service`

- `estimate_duration(flat_rate_minutes: float, sigma: float = 0.30, percentile: float = 0.80) -> int`
  - Accepts `flat_rate_minutes > 0`, `sigma in [0.05, 1.0]`, `percentile in [0.50, 0.99]`.
  - Returns integer ceiling minutes (minimum `1`), or raises `ValueError` on invalid bounds.
- `estimate_total_duration(flat_rate_list: List[Union[float, Dict]], sigma: float = 0.30, percentile: float = 0.80) -> int`
  - Accepts positive flat-rate minutes (or dicts with per-item `sigma`).
  - Returns integer ceiling of the Fenton-Wilkinson sum (`0` if empty).

### 2. `ism.api.estimation_service` ↔ Callers

- `get_estimate(item_code: str, **kwargs) -> Dict[str, Any]`
  - Reads `Item.custom_frt` and `ISM Settings` (`estimation_percentile`, `estimation_sigma`, `buffer_padding_minutes`).
  - Returns `{"duration_minutes": int, "p_pure": int, "buffer_padding": int, "percentile": float, "sigma": float}`.
- `get_total_estimate(item_codes: Union[str, List], **kwargs) -> Dict[str, Any]`
  - Returns `{"total_duration_minutes": int, "p_pure": int, "buffer_padding": int, "operations_count": int}`.

### 3. `ism.api.scheduling` ↔ `Schedule Entry` & Agent Facade

- `effective_end_time(start_time, duration_minutes, break_start=None, break_end=None) -> timedelta`: Adds break duration when work crosses the break window.
- `check_bay_availability(service_bay, date, start_time, duration_minutes, exclude_entry=None) -> bool`: Returns `True` when the bay is free across lunch-aware intervals.
- `get_available_slots(date, duration_minutes, required_tags=None, service_bay=None) -> List[Dict]`: Walks operating hours by `default_slot_interval` and reports bay, technician, and bottleneck status.
- `auto_assign_bay(date, start_time, duration_minutes, required_tags=None) -> str`: Picks the first free capable bay or raises `frappe.ValidationError`.
- `is_holiday(date) -> Dict[str, Any]`: Checks `ISM Settings.holiday_list` and returns `{"is_holiday": bool, "description": str}`.

### 4. `ism.api.technician_availability` ↔ `Schedule Entry` & Agent Facade

- `get_active_technicians(date=None) -> List[Dict]`: Queries active `Employee` records matching `ISM Settings.technician_designation`.
- `get_technician_pool_availability(date, start_time, duration_minutes, exclude_entry=None) -> Dict[str, Any]`: Returns pool capacity counts (`total_technicians`, `on_leave`, `on_duty`, `occupied`, `available`, `is_available`).
- `check_technician_availability(employee, date, start_time, duration_minutes, exclude_entry=None) -> Dict[str, Any]`: Returns `{"is_available": bool, "reason": str, "conflicts": List[Dict]}`.

### 5. `Repair Order` ↔ `Schedule Entry`

- Cardinality: 1 Repair Order to many Schedule Entries (`Schedule Entry.repair_order`).
- `Repair Order.formalize_provisional_info()` resolves provisional customer and vehicle values into master records and updates linked `Schedule Entry` rows.

---

## Code Layout

```text
.
├── ism/
│   ├── api/
│   │   ├── agent.py
│   │   ├── agent_serializers.py
│   │   ├── approval.py
│   │   ├── estimation_service.py
│   │   ├── parts.py
│   │   ├── scheduling.py
│   │   ├── scheduling_views.py
│   │   ├── sms.py
│   │   ├── technician_availability.py
│   │   └── tesla.py
│   ├── ism/doctype/
│   │   ├── equipment_tag/
│   │   ├── inspection_template/
│   │   ├── inspection_template_item/
│   │   ├── ism_settings/
│   │   ├── repair_order/
│   │   ├── repair_vehicle/
│   │   ├── schedule_entry/
│   │   ├── schedule_entry_type/
│   │   ├── service_bay/
│   │   ├── service_bay_equipment/
│   │   ├── service_package/
│   │   └── shop_action_item/
│   ├── patches/
│   ├── scheduling/
│   │   ├── capacity.py
│   │   ├── estimation_light.py
│   │   └── time_utils.py
│   ├── scripts/
│   │   └── verify_mcp_agent.py
│   ├── tests/
│   │   ├── base.py
│   │   ├── cleanup.py
│   │   ├── test_agent_facade.py
│   │   ├── test_estimation_math.py
│   │   ├── test_fixtures.py
│   │   ├── test_mcp_protocol.py
│   │   ├── test_parts_management.py
│   │   ├── test_ro_scheduling_e2e.py
│   │   ├── test_scheduling_capacity.py
│   │   └── test_tesla_integration.py
│   ├── hooks.py
│   ├── patches.txt
│   └── setup.py
├── ism-agent-os/
│   ├── ingestion/
│   ├── rules/
│   ├── scripts/
│   ├── skills/
│   ├── tests/
│   ├── tools/
│   ├── openwebui_tools.py
│   └── pyproject.toml
└── scripts/
    ├── README.md
    ├── sync-from-wsl.ps1
    ├── sync-to-wsl.ps1
    ├── watch-sync.mjs
    ├── watch-sync.ps1
    ├── start-remote-tunnel.ps1
    └── stop-remote-tunnel.ps1
```
