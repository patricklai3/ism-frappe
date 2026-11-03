# Milestone 3 Exploration Report: Schedule Entry Controller Validations & 53-Test Suite Architecture

**Agent**: `m3_it1_explorer_3`  
**Working Directory**: `d:\ism\.agents\teamwork\m3_it1_explorer_3`  
**Parent**: Project Orchestrator Gen 6 (`60694e2b-2673-4a73-bfb2-434f75ef28e2`)  
**Target Milestone**: Milestone 3 — Dual-Resource Capacity & Scheduling APIs  

---

## 1. Observation

### 1.1 Existing Codebase & Environment Inspection
1. **Schedule Entry Controller** (`d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py`):
   - Lines 120–126:
     ```python
     def validate(self):
         self.validate_estimated_duration()
         self.validate_repair_order_requirement()
         self.validate_bay_availability()
         self.validate_technician_pool()
         self.validate_technician_assignment()
     ```
   - Lines 128–134: `validate_estimated_duration()` throws `frappe.ValidationError` if `cint(self.estimated_duration) <= 0`.
   - Lines 163–177: `validate_repair_order_requirement()` queries `Schedule Entry Type.requires_repair_order` and raises `frappe.MandatoryError` if required and `repair_order` is missing.
   - Lines 178–207: `validate_bay_availability()` checks `self.status != "Cancelled"`, invokes `ism.api.scheduling.check_bay_availability` (falling back to `_check_bay_availability_fallback`), and throws `frappe.ValidationError` if occupied.
   - Lines 242–271: `validate_technician_pool()` checks `enable_technician_capacity` in `ISM Settings`, queries `get_technician_pool_availability`, and throws `frappe.ValidationError` if `not pool.get("is_available")`.
   - Lines 272–306: `validate_technician_assignment()` issues non-blocking soft warnings via `frappe.msgprint(..., alert=True)` with indicator `"red"` for approved leave and `"orange"` for overlapping appointments.
   - Lines 11–23: `autoname()` assigns `SE-.#####` naming series with MariaDB error 1020 deadlock protection via `_generate_safe_series_name()`.

2. **Schedule Entry Schema** (`d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.json`):
   - Key fields: `entry_type`, `status` (Options: `Draft`, `Scheduled`, `In Progress`, `Completed`, `Cancelled`), `title`, `scheduled_date`, `scheduled_time`, `estimated_duration`, `repair_order`, `customer`, `provisional_customer_name`, `provisional_phone`, `provisional_email`, `repair_vehicle`, `provisional_vehicle_info`, `service_bay`, `assigned_technician`, `items_summary`, `notes`.
   - No dedicated physical `end_time` database column exists in `schedule_entry.json`. Effective end time is dynamically derived via lunch-aware interval calculation.

3. **Schedule Entry Existing Unit Tests** (`d:\ism\ism\ism\doctype\schedule_entry\test_schedule_entry.py`):
   - Verified via `bench run-tests --app ism --doctype "Schedule Entry"`: 7 passed in 1.383s.
   - Covers: naming series, multiple entries per repair order (1:N), auto-population from repair order, provisional contact fallbacks, `requires_repair_order` check, positive duration validation, and update sync.

4. **Test Suite Execution for `test_scheduling_capacity.py`**:
   - Command:
     ```powershell
     wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling_capacity
     ```
   - Current execution output:
     ```
     Running 53 unspecified-category tests for ism
     Ran 53 tests in 1.699s
     OK (skipped=48)
     ```
   - 5 tests currently passing:
     - `test_tier1_f19_service_bay_creation`
     - `test_tier1_f20_equipment_tag_creation`
     - `test_tier1_f21_schedule_entry_type_seed`
     - `test_tier1_f22_schedule_entry_crud`
     - `test_tier1_f7_schedule_entry_overlap_validation_error` (passes because `_check_bay_availability_fallback` caught naive collision)
   - 48 tests skipped due to missing API implementations:
     - `ism.api.scheduling` (28 tests skipped)
     - `ism.api.technician_availability` (20 tests skipped)

5. **Backward-Compatible Proxies**:
   - `d:\ism\ism\tests\test_scheduling.py`:
     ```python
     from ism.tests.test_scheduling_capacity import TestSchedulingCapacity
     ```
   - `d:\ism\ism\tests\test_technician_availability.py`:
     ```python
     from ism.tests.test_scheduling_capacity import TestSchedulingCapacity
     ```
   - Verified via:
     `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling`
     Executes the identical 53 tests.

6. **Reference Codebase** (`C:\Users\real2\AppData\Local\Temp\induct_shop`):
   - Contains prototype implementations of `scheduling.py`, `technician_availability.py`, `scheduling_views.py`, and `schedule_entry.py`.
   - Identified critical divergences and edge cases between reference code and ISM test suite expectations.

---

## 2. Logic Chain

### 2.1 Complete 53-Test Inventory & Mapping in `test_scheduling_capacity.py`

| # | Test Method Name | Tier | Target Feature | Input Conditions & Assertions | Critical Implementation Contract |
|---|---|---|---|---|---|
| 1 | `test_tier1_f6_end_time_wholly_before_lunch` | Tier 1 | F6 Lunch-Aware End Time | `08:00`, 120m, break `12:00-12:30` -> returns `10:00:00` | Naive end < break_start -> no shift |
| 2 | `test_tier1_f6_end_time_spanning_lunch` | Tier 1 | F6 Lunch-Aware End Time | `11:00`, 120m, break `12:00-12:30` -> returns `13:30:00` | Start < break_start & naive_end > break_start -> +30m break duration |
| 3 | `test_tier1_f6_end_time_wholly_after_lunch` | Tier 1 | F6 Lunch-Aware End Time | `13:00`, 90m, break `12:00-12:30` -> returns `14:30:00` | Start >= break_end -> no shift |
| 4 | `test_tier1_f6_end_time_starts_during_lunch` | Tier 1 | F6 Lunch-Aware End Time | `12:15`, 60m, break `12:00-12:30` -> `eff >= 13:30:00` | Start in lunch window -> effective start delayed to `break_end` (`12:30 + 60m = 13:30`) |
| 5 | `test_tier1_f6_end_time_defaults_from_ism_settings` | Tier 1 | F6 Lunch-Aware End Time | `break_start=None, break_end=None` -> reads `ISM Settings` (`12:00-13:00`); `11:30` + 60m -> `13:30:00` | Fallback to `ISM Settings` singleton |
| 6 | `test_tier1_f7_bay_available_no_conflict` | Tier 1 | F7 Bay Overlap Validation | Bay 1 booked `08:00-10:00`. Check Bay 1 `10:00-12:00` -> `True`. Check Bay 2 `08:00-10:00` -> `True` | Free bay / free slot returns `True` |
| 7 | `test_tier1_f7_bay_overlap_blocked` | Tier 1 | F7 Bay Overlap Validation | Bay 1 booked `08:00-10:00`. Check Bay 1 `09:00-11:00` -> `False` | Overlapping interval returns `False` |
| 8 | `test_tier1_f7_schedule_entry_overlap_validation_error` | Tier 1 | F7 Bay Overlap Validation | Bay 1 booked `08:00-10:00`. Insert second entry on Bay 1 at `09:00` (60m) | Raises `frappe.ValidationError` |
| 9 | `test_tier1_f7_bay_self_exclusion_on_reschedule` | Tier 1 | F7 Bay Overlap Validation | Resave entry `se` with new duration 150m (`exclude_entry=se.name`) | Excludes self from collision query |
| 10 | `test_tier1_f7_adjacent_boundary_booking_allowed` | Tier 1 | F7 Bay Overlap Validation | Bay 1 booked `08:00-10:00`. Check `10:00-11:00` -> `True` | Boundary condition `max(start1, start2) < min(end1, end2)`: `10:00 < 10:00` is False -> No conflict |
| 11 | `test_tier1_f8_equipment_tag_single_match` | Tier 1 | F8 Equipment Tag Filter | `required_tags=["Lift"]` -> matches Bay 1 or Bay 2 | Subset match: `required.issubset(bay_tags)` |
| 12 | `test_tier1_f8_equipment_tag_exclusive_match` | Tier 1 | F8 Equipment Tag Filter | `required_tags=["Alignment Rack"]` -> matches only Bay 2 | Exact capability filtering |
| 13 | `test_tier1_f8_bay_missing_tag_excluded` | Tier 1 | F8 Equipment Tag Filter | Bay 2 booked `08:00-10:00`. Auto-assign for Alignment Rack | Raises `frappe.ValidationError` (Bay 1 lacks tag) |
| 14 | `test_tier1_f8_no_tag_requirement_matches_any_free_bay` | Tier 1 | F8 Equipment Tag Filter | `required_tags=[]` | Matches any active free bay |
| 15 | `test_tier1_f8_inactive_bay_never_matched` | Tier 1 | F8 Equipment Tag Filter | Bay 1 & Bay 2 booked. Bay 3 is `is_active=0` | Raises `frappe.ValidationError`; inactive bay excluded |
| 16 | `test_tier1_f9_auto_assign_free_bay` | Tier 1 | F9 Auto Bay Assignment | Empty morning slot `09:00`, 60m | Returns bay string (Bay 1 or Bay 2) |
| 17 | `test_tier1_f9_earliest_fit_order` | Tier 1 | F9 Auto Bay Assignment | Lowest-index / first available bay selection | Stable deterministic earliest-fit sort |
| 18 | `test_tier1_f9_auto_assign_respects_existing_bookings` | Tier 1 | F9 Auto Bay Assignment | Bay 1 booked `08:00-10:00`. Auto-assign at `08:30` | Returns Bay 2 |
| 19 | `test_tier1_f9_auto_assign_full_shop_raises` | Tier 1 | F9 Auto Bay Assignment | Both Bay 1 and Bay 2 booked `08:00-10:00` | Raises `frappe.ValidationError` |
| 20 | `test_tier1_f9_auto_assign_technician_capacity_gating` | Tier 1 | F9 Auto Bay Assignment | `enable_technician_capacity=1` and tech pool exhausted | Raises `frappe.ValidationError` even if bay is free |
| 21 | `test_tier1_f10_slot_generator_operating_hours` | Tier 1 | F10 Slot Generator | Operating hours `08:00-17:00` | Returns slots with `start_time`, `available_bays`, `available_technicians`, `effective_capacity` |
| 22 | `test_tier1_f10_lunch_break_slot_suppression` | Tier 1 | F10 Slot Generator | Break `12:00-12:30` | `12:00` and `12:00:00` suppressed |
| 23 | `test_tier1_f10_bay_specific_slot_filtering` | Tier 1 | F10 Slot Generator | `service_bay=self.bay1`. Bay 1 booked `08:00-10:00` | Slots `08:00` and `09:00` excluded |
| 24 | `test_tier1_f10_capacity_indicators_bottleneck_bay` | Tier 1 | F10 Slot Generator | Slot indicator when bays < technicians | Bottleneck in `["bay", "technician", "none", None]` (singular!) |
| 25 | `test_tier1_f10_slots_past_closing_suppressed` | Tier 1 | F10 Slot Generator | 120m duration at `16:30` finishes `18:30` (> `17:00`) | Suppressed past shop closing |
| 26 | `test_tier1_f11_technician_roster_active` | Tier 1 | F11 Technician Roster | Active employees with designation "Technician" | Included in roster |
| 27 | `test_tier1_f11_inactive_technicians_excluded` | Tier 1 | F11 Technician Roster | Employee status `Left` | Excluded from roster |
| 28 | `test_tier1_f11_non_technician_designation_excluded` | Tier 1 | F11 Technician Roster | Employee designation `Accountant` | Excluded from roster |
| 29 | `test_tier1_f11_roster_returns_leave_flags` | Tier 1 | F11 Technician Roster | Roster query for `today()` | Dict contains `"on_leave"` boolean |
| 30 | `test_tier1_f11_roster_date_sensitive` | Tier 1 | F11 Technician Roster | Roster query for `add_days(today(), 5)` | Returns list of dicts for given date |
| 31 | `test_tier1_f12_leave_application_marks_on_leave` | Tier 1 | F12 HRMS Leave Integration | Approved Leave Application on target date | `on_leave = True` |
| 32 | `test_tier1_f12_individual_check_reflects_leave` | Tier 1 | F12 HRMS Leave Integration | `check_technician_availability(tech1, ...)` | `is_available=False`, `reason="on_leave"` |
| 33 | `test_tier1_f12_pending_leave_does_not_block` | Tier 1 | F12 HRMS Leave Integration | Open/unapproved leave (`docstatus=0`, `status="Open"`) | `is_available=True` |
| 34 | `test_tier1_f12_leave_on_different_date_free_on_target` | Tier 1 | F12 HRMS Leave Integration | Leave scheduled in 10 days; checked today | `is_available=True` |
| 35 | `test_tier1_f12_multi_day_leave_span` | Tier 1 | F12 HRMS Leave Integration | Leave `+3` to `+5` days; checked at `+4` days | `is_available=False` |
| 36 | `test_tier1_f13_tech_pool_availability_structure` | Tier 1 | F13 Pool-Level Gating | `get_technician_pool_availability` schema check | Keys: `total_technicians`, `on_duty`, `occupied`, `available`, `is_available` |
| 37 | `test_tier1_f13_pool_exhaustion_blocks_insert` | Tier 1 | F13 Pool-Level Gating | Both techs booked at `09:00`. Check availability & insert third entry | `is_available=False`; insert raises `frappe.ValidationError` |
| 38 | `test_tier1_f14_individual_tech_overlap_detected` | Tier 1 | F14 Tech Overlap Check | Tech 1 booked `08:00-10:00`. Check availability at `09:00` | `is_available=False`, `reason="overlap"` |
| 39 | `test_tier1_f15_toggle_disabled_bypasses_pool_limit` | Tier 1 | F15 Master Capacity Toggle | `enable_technician_capacity=0` in `ISM Settings` | `is_available=True` even when all techs are occupied |
| 40 | `test_tier1_f16_technician_queue` | Tier 1 | F16 Technician Queue | `get_technician_queue(tech1, today())` | Dict contains `"timeline"` and `"utilization_percent"` |
| 41 | `test_tier1_f17_daily_technician_overview` | Tier 1 | F17 Daily Tech Overview | `get_daily_technician_overview(today())` | Dict contains `"total_scheduled_hours"` at top-level and `"technicians"` list |
| 42 | `test_tier1_f18_is_holiday_api` | Tier 1 | F18 Holiday Awareness | `is_holiday(today())` | Dict contains `"is_holiday"` and `"description"` |
| 43 | `test_tier1_f19_service_bay_creation` | Tier 1 | F19 Service Bay DocType | Insert `Service Bay` with `is_active=1` and child equipment | Persisted successfully (Passing) |
| 44 | `test_tier1_f20_equipment_tag_creation` | Tier 1 | F20 Equipment Tag DocType | Insert `Equipment Tag` | Persisted successfully (Passing) |
| 45 | `test_tier1_f21_schedule_entry_type_seed` | Tier 1 | F21 Entry Type Seed | Verify seeded types (Diagnostic, Repair, Maintenance, Meeting, Internal Service) | Records exist (Passing) |
| 46 | `test_tier1_f22_schedule_entry_crud` | Tier 1 | F22 Schedule Entry CRUD | Insert & verify Schedule Entry with RO, bay, time | Persisted successfully (Passing) |
| 47 | `test_tier2_f6_lunch_boundary_exact_edges` | Tier 2 | F6 Boundary Conditions | Job `10:00-12:00`, break `12:00-12:30` | Effective end is exactly `12:00:00` (no shift) |
| 48 | `test_tier2_f6_lunch_boundary_exact_start_at_break_end` | Tier 2 | F6 Boundary Conditions | Job `12:30-13:30`, break `12:00-12:30` | Effective end is exactly `13:30:00` (no shift) |
| 49 | `test_tier2_f7_partial_front_overlap_blocked` | Tier 2 | F7 Boundary Conditions | Job 1 `08:00-10:00`. Test `07:30-08:30` | Returns `False` (front overlap blocked) |
| 50 | `test_tier2_f7_partial_tail_overlap_blocked` | Tier 2 | F7 Boundary Conditions | Job 1 `08:00-10:00`. Test `09:30-10:30` | Returns `False` (tail overlap blocked) |
| 51 | `test_tier2_f10_slot_generator_no_operating_hours` | Tier 2 | F10 Boundary Conditions | `operating_hours_start == operating_hours_end == "00:00:00"` | Graceful empty list `[]` |
| 52 | `test_tier3_lunch_extension_plus_bay_overlap_detection` | Tier 3 | Cross-Feature Interaction | Job `11:00-13:00` (120m) spans lunch `12:00-12:30` -> effective end `13:30`. Check `13:00-14:00` -> `False`; check `13:30-14:30` -> `True` | Bay occupation extended by break, blocking overlapping afternoon slot |
| 53 | `test_tier3_tag_filtering_plus_pool_gating` | Tier 3 | Cross-Feature Interaction | Bay 2 is only Alignment bay. Both techs booked at `10:00`. `auto_assign_bay` for Alignment Rack | Raises `frappe.ValidationError` due to tech pool exhaustion |

---

### 2.2 Five Critical Discrepancies Between Reference Code (`induct_shop`) and ISM Specifications

Through comparative analysis between `C:\Users\real2\AppData\Local\Temp\induct_shop` and `d:\ism\ism\tests\test_scheduling_capacity.py`, we identified five high-risk pitfalls that would cause test failures if reference code were copied verbatim:

#### Discrepancy 1: Starting During Lunch in `effective_end_time`
- **Reference Code Behavior** (`induct_shop/api/scheduling.py:114`):
  ```python
  if bs_td and be_td and be_td > bs_td:
      break_duration = be_td - bs_td
      if start_td < bs_td and naive_end > bs_td:
          return naive_end + break_duration
  ```
  If `start_time` is `12:15` (during `12:00-12:30` lunch), `start_td < bs_td` evaluates to `False`. The function returned `13:15`.
- **ISM Test Expectation** (`test_scheduling_capacity.py:146`):
  ```python
  eff = effective_end_time("12:15:00", 60, break_start="12:00:00", break_end="12:30:00")
  self.assertGreaterEqual(eff, timedelta(hours=13, minutes=30))
  ```
- **Required Logic**: If `bs_td <= start_td < be_td`, effective work start cannot commence until `be_td`. The effective end time must be `be_td + duration_td = 12:30 + 60m = 13:30`.

#### Discrepancy 2: Bottleneck String Singularization in `get_available_slots`
- **Reference Code Behavior** (`induct_shop/api/scheduling.py:302`):
  Set `bottleneck = "technicians"` or `"bays"` (plural).
- **ISM Test Expectation** (`test_scheduling_capacity.py:381`):
  ```python
  self.assertIn(slot.get("bottleneck"), ["bay", "technician", "none", None])
  ```
- **Required Logic**: ISM requires `"bay"` (singular) and `"technician"` (singular), or `"none"` / `None`.

#### Discrepancy 3: Technician Queue Key Naming in `get_technician_queue`
- **Reference Code Behavior** (`induct_shop/api/technician_availability.py:412`):
  Returned `{"entries": formatted_entries, "utilization_pct": utilization_pct}`.
- **ISM Test Expectation** (`test_scheduling_capacity.py:623`):
  ```python
  queue = get_technician_queue(self.tech1_id, today())
  self.assertIn("timeline", queue)
  self.assertIn("utilization_percent", queue)
  ```
- **Required Logic**: Return BOTH `"timeline"` (alias to entries) and `"entries"`, and BOTH `"utilization_percent"` (alias to utilization) and `"utilization_pct"`.

#### Discrepancy 4: Top-Level Metrics in `get_daily_technician_overview`
- **Reference Code Behavior** (`induct_shop/api/technician_availability.py:497`):
  `"total_scheduled_hours"` was deeply nested inside `overview["shop_summary"]["total_scheduled_hours"]`.
- **ISM Test Expectation** (`test_scheduling_capacity.py:630`):
  ```python
  overview = get_daily_technician_overview(today())
  self.assertIn("total_scheduled_hours", overview)
  self.assertIn("technicians", overview)
  ```
- **Required Logic**: Return `"total_scheduled_hours"` and `"technicians"` directly at the top level of the returned dictionary.

#### Discrepancy 5: 1:N Cardinality on Repair Order vs 1:1 on Sales Order
- **Reference Code Behavior** (`induct_shop/doctype/schedule_entry/schedule_entry.py:225`):
  Enforced `validate_unique_sales_order()`, throwing `DuplicateEntryError` if a second schedule entry referenced the same Sales Order.
- **ISM Architecture & Domain Rule 2** (`PROJECT.md` & `test_schedule_entry.py:109`):
  "Multiple Appointments per Repair Order: Multiple appointments on the same Repair Order represent supplemental work or additional labor time required... (1:N cardinality)."
- **Required Logic**: Must NOT restrict or validate uniqueness of `repair_order` on `Schedule Entry`.

---

### 2.3 Detailed Architecture of `Schedule Entry` Controller

#### Invariant Ordering in `validate()`
The `validate()` hook on `ScheduleEntry` in `schedule_entry.py` must execute checks in this strict deterministic sequence:
1. `validate_estimated_duration()`:
   Ensures `cint(self.estimated_duration) > 0`. Throws `frappe.ValidationError` if $\le 0$.
2. `validate_repair_order_requirement()`:
   Checks `Schedule Entry Type.requires_repair_order`. If required and `not self.repair_order`, throws `frappe.MandatoryError`.
3. `calculate_effective_end_time()`:
   Dynamically computes lunch-aware effective end time.
4. `validate_bay_availability()`:
   - If `self.status == "Cancelled"`, return immediately (cancelled entries never consume capacity).
   - If `service_bay`, `scheduled_date`, and `scheduled_time` are provided:
     Call `ism.api.scheduling.check_bay_availability(service_bay, date, scheduled_time, duration, exclude_entry=self.name)`.
   - If `False`, throw `frappe.ValidationError` (_"Service Bay '{0}' is already occupied during the requested time window on {1}."_).
5. `validate_technician_pool()`:
   - If `self.status == "Cancelled"`, return immediately.
   - Read `ISM Settings.enable_technician_capacity`. If False / 0, return immediately.
   - If enabled, call `ism.api.technician_availability.get_technician_pool_availability(date, scheduled_time, duration, exclude_entry=self.name)`.
   - If `not pool.get("is_available")`, throw `frappe.ValidationError` (_"Technician pool capacity exhausted for the requested time window on {0}."_).
6. `validate_technician_assignment()`:
   - If `not self.assigned_technician` or `self.status == "Cancelled"`, return.
   - Call `ism.api.technician_availability.check_technician_availability(employee, date, scheduled_time, duration, exclude_entry=self.name)`.
   - If `check.get("on_leave")`:
     `frappe.msgprint(_("Warning: Assigned technician '{0}' is on approved leave on {1}.").format(self.assigned_technician, self.scheduled_date), alert=True, indicator="red")`
   - Elif `check.get("conflicts")`:
     `frappe.msgprint(_("Warning: Assigned technician '{0}' has an overlapping Schedule Entry during this time window.").format(self.assigned_technician), alert=True, indicator="orange")`
   - **Crucial**: Soft warnings must NEVER throw exceptions or block save; they inform the service advisor while permitting intentional overrides.

#### Model Properties & Helper Methods
```python
@property
def end_time(self) -> timedelta:
    """Calculates lunch-aware effective end time."""
    if not self.scheduled_time:
        return timedelta(0)
    try:
        from ism.api.scheduling import effective_end_time
        return effective_end_time(self.scheduled_time, self.estimated_duration or 60)
    except Exception:
        from datetime import timedelta
        from frappe.utils import to_timedelta
        return to_timedelta(self.scheduled_time) + timedelta(minutes=self.estimated_duration or 60)

def get_effective_end_time(self) -> timedelta:
    return self.end_time
```

---

## 3. Caveats

1. **No Source Modification by Explorer**:
   As an explorer, this investigation is strictly read-only. No source files outside the agent's teamwork directory were modified.
2. **Settings Singleton Assumption**:
   `ISM Settings` is assumed to be initialized. In tests, `ensure_ism_settings()` configures hours `08:00-17:00` and lunch break `12:00-12:30`. In production, default fallback values must be provided in `get_shop_settings()` if fields are unpopulated.
3. **Transactional Cleanup in Reverse Order**:
   Because `Schedule Entry` has foreign keys to `Repair Order`, `Customer`, `Repair Vehicle`, `Service Bay`, and `Employee`, teardown in tests and fixture management must always delete `Schedule Entry` records before parent master records.

---

## 4. Conclusion & Implementation Plan

### 4.1 Implementation Architecture Overview

```
                          ISM Scheduling Architecture
                          
       +------------------------------------------------------+
       |                Schedule Entry Model                  |
       |  (d:\ism\ism\ism\doctype\schedule_entry\             |
       |   schedule_entry.py)                                 |
       |                                                      |
       |  - validate_estimated_duration()                    |
       |  - validate_repair_order_requirement()               |
       |  - calculate_effective_end_time() (end_time prop)   |
       |  - validate_bay_availability()    ---(ValidationError)|
       |  - validate_technician_pool()     ---(ValidationError)|
       |  - validate_technician_assignment() --(msgprint alert)|
       +--------------------------+---------------------------+
                                  |
              +-------------------+-------------------+
              v                                       v
+-------------------------------+   +------------------------------------+
|     ism.api.scheduling        |   |   ism.api.technician_availability  |
|                               |   |                                    |
| - to_timedelta                |   | - get_active_technicians           |
| - format_timedelta            |   | - check_employee_leave             |
| - get_shop_settings           |   | - get_technician_pool_availability |
| - effective_end_time          |   | - check_technician_availability    |
| - check_bay_availability      |   | - get_technician_queue             |
| - get_available_bays          |   | - get_daily_technician_overview    |
| - auto_assign_bay             |   +------------------------------------+
| - get_available_slots         |
| - is_holiday                  |
+-------------------------------+
```

### 4.2 Step-by-Step Implementation Strategy & Dependency Order

1. **Phase 1: Scheduling Core Module (`ism/api/scheduling.py`)** [Assigned to Explorer 1 / Implementer]:
   - Implement `to_timedelta`, `format_timedelta`, and `get_shop_settings` reading from `ISM Settings`.
   - Implement `effective_end_time` with lunch span logic AND lunch start delay (`bs_td <= start < be_td -> be_td + duration`).
   - Implement `check_bay_availability` with lunch-aware interval comparison and `exclude_entry`.
   - Implement `get_available_bays` and `auto_assign_bay` (with pool gating check and earliest-fit).
   - Implement `get_available_slots` (with singular bottleneck: `"bay"`, `"technician"`, `"none"`, `None`).
   - Implement `is_holiday` querying `Holiday` linked to `ISM Settings.holiday_list`.

2. **Phase 2: Technician Availability Module (`ism/api/technician_availability.py`)** [Assigned to Explorer 2 / Implementer]:
   - Implement `get_active_technicians` filtering designation and querying approved `Leave Application`.
   - Implement `check_employee_leave` supporting full-day and half-day leaves.
   - Implement `get_technician_pool_availability` with master toggle check (`enable_technician_capacity`).
   - Implement `check_technician_availability` returning `is_available`, `reason` (`"on_leave"`, `"overlap"`), and `conflicts`.
   - Implement `get_technician_queue` returning `"timeline"`, `"entries"`, `"utilization_percent"`, and `"utilization_pct"`.
   - Implement `get_daily_technician_overview` returning top-level `"total_scheduled_hours"` and `"technicians"`.

3. **Phase 3: Schedule Entry Controller Refinement (`ism/ism/doctype/schedule_entry/schedule_entry.py`)**:
   - Add `end_time` property and `get_effective_end_time()` calling `ism.api.scheduling.effective_end_time`.
   - Ensure `validate_bay_availability` imports from `ism.api.scheduling` and calls `check_bay_availability`.
   - Ensure `validate_technician_pool` checks `enable_technician_capacity` and calls `get_technician_pool_availability`.
   - Ensure `validate_technician_assignment` calls `check_technician_availability` and issues soft warnings via `msgprint(..., alert=True)`.
   - Maintain 1:N cardinality (no unique Repair Order check).

---

## 5. Verification Method

### 5.1 Host Synchronization Command
Always sync workspace changes to WSL2 before test execution:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
```

### 5.2 Test Execution Commands
1. **Primary Test Suite (53 Tests)**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling_capacity
   ```
   **Success Criteria**: `Ran 53 tests in ~2s. OK (skipped=0)`. All 53 tests pass.

2. **Backward-Compatible Proxies**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling
   ```
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_technician_availability
   ```
   **Success Criteria**: Both proxy modules run and pass all 53 tests.

3. **Existing DocType Tests**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"
   ```
   **Success Criteria**: All 7 existing unit tests in `test_schedule_entry.py` continue to pass.

### 5.3 Invalidation Conditions
- Any test in `test_scheduling_capacity.py` fails or is skipped due to `ImportError`.
- `effective_end_time("12:15:00", 60, break_start="12:00:00", break_end="12:30:00")` returns `< timedelta(hours=13, minutes=30)`.
- `get_available_slots` returns plural bottleneck strings (`"bays"`, `"technicians"`).
- `get_technician_queue` lacks `"timeline"` or `"utilization_percent"`.
- `get_daily_technician_overview` lacks `"total_scheduled_hours"` at the top level.
- `Schedule Entry` insert blocks on individual technician overlap instead of issuing soft `msgprint` warning.
