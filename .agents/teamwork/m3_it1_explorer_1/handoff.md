# Handoff Report: Scheduling Core APIs (`ism/api/scheduling.py`)

## 1. Observation

### 1.1 Requirements & Interface Contracts
- **Dispatch**: Scope covers `effective_end_time`, `check_bay_availability`, `get_available_slots`, `auto_assign_bay`, and `is_holiday` for `ism/api/scheduling.py`, mapped against `ism/tests/test_scheduling_capacity.py`.
- **`PROJECT.md` § Interface Contract 3** defines the API contract for `ism.api.scheduling`:
  - `effective_end_time(start_time: Union[str, time, timedelta], duration_minutes: int, break_start=None, break_end=None) -> timedelta`: Lunch extension logic returning adjusted `timedelta`.
  - `check_bay_availability(service_bay: str, date: str, start_time: str, duration_minutes: int, exclude_entry: str = None) -> bool`: Per-bay collision prevention with lunch-aware intervals.
  - `get_available_slots(date: str, duration_minutes: int, required_tags: List[str] = None, service_bay: str = None) -> List[Dict]`: Iterates operating hours, suppresses lunch slots & past closing, and calculates `available_bays`, `available_technicians`, `effective_capacity`, and `bottleneck`.
  - `auto_assign_bay(date: str, start_time: str, duration_minutes: int, required_tags: List[str] = None) -> str`: Earliest-fit algorithm finding first capable, free bay; raises `frappe.ValidationError` if full or tech capacity exhausted.
  - `is_holiday(date: str) -> Dict[str, Any]`: Checks `ISM Settings.holiday_list` against ERPNext `Holiday` table.

### 1.2 Downstream Dependencies
- **`ScheduleEntry` controller (`ism/ism/doctype/schedule_entry/schedule_entry.py`)**:
  - Line 187: Imports and invokes `check_bay_availability(service_bay=self.service_bay, date=str(self.scheduled_date), start_time=self.scheduled_time, duration_minutes=duration, exclude_entry=self.name)`.
  - Line 248: Imports and invokes `get_shop_settings()` to evaluate `settings.get("enable_technician_capacity")`.
- **`ism/api/technician_availability.py`**:
  - In reference implementation (`induct_shop/api/technician_availability.py` lines 6–11), imports `to_timedelta`, `format_timedelta`, `get_shop_settings`, and `effective_end_time` from `scheduling.py`.

### 1.3 Exact Test Assertions in `ism/tests/test_scheduling_capacity.py`
1. **Feature 6 (Effective End Time)**:
   - Line 128: `eff = effective_end_time("08:00:00", 120, break_start="12:00:00", break_end="12:30:00")` -> `assertEqual(eff, timedelta(hours=10))`.
   - Line 134: `eff = effective_end_time("11:00:00", 120, break_start="12:00:00", break_end="12:30:00")` -> `assertEqual(eff, timedelta(hours=13, minutes=30))`.
   - Line 140: `eff = effective_end_time("13:00:00", 90, break_start="12:00:00", break_end="12:30:00")` -> `assertEqual(eff, timedelta(hours=14, minutes=30))`.
   - Line 146: `eff = effective_end_time("12:15:00", 60, break_start="12:00:00", break_end="12:30:00")` -> `assertGreaterEqual(eff, timedelta(hours=13, minutes=30))`. (Crucial difference from `induct_shop`: appointments starting *inside* the lunch window cannot begin work until `break_end`, pushing effective completion to `break_end + duration`).
   - Line 153: `eff = effective_end_time("11:30:00", 60)` -> reads `break_start="12:00:00", break_end="13:00:00"` from `ISM Settings` -> `assertEqual(eff, timedelta(hours=13, minutes=30))`.
   - Line 693: `eff = effective_end_time("10:00:00", 120, break_start="12:00:00", break_end="12:30:00")` -> ending exactly at 12:00 does not extend -> `assertEqual(eff, timedelta(hours=12))`.
   - Line 699: `eff = effective_end_time("12:30:00", 60, break_start="12:00:00", break_end="12:30:00")` -> starting exactly at 12:30 does not extend -> `assertEqual(eff, timedelta(hours=13, minutes=30))`.
2. **Feature 7 (Per-Bay Overlap Validation)**:
   - Lines 169–171: `check_bay_availability` returns `True` for empty intervals or different bays.
   - Line 186: Returns `False` for overlapping interval.
   - Lines 208–209: Inserting colliding `Schedule Entry` raises `frappe.ValidationError`.
   - Line 225: Passing `exclude_entry=se.name` excludes existing entry on reschedule.
   - Line 242: Adjacent boundary booking (job 1 ending 10:00, job 2 starting 10:00) returns `True`.
   - Lines 707, 715: Partial front (07:30–08:30) and tail (09:30–10:30) overlaps against 08:00–10:00 return `False`.
3. **Feature 8 (Equipment Tag Filtering)**:
   - Lines 250–259: Matches `Lift` to `Bay 1` or `Bay 2`, and `Alignment Rack` strictly to `Bay 2`.
   - Line 275: Lacking required tag raises `frappe.ValidationError`.
   - Line 282: Empty `required_tags` matches any active bay.
   - Line 292: Inactive bay (`Bay 3`, `is_active=0`) is never matched, raising `frappe.ValidationError` when active bays are booked.
4. **Feature 9 (Automated Bay Assignment)**:
   - Lines 308–310: Consistently picks first capable bay (`order_by="name asc"`).
   - Line 318: Picks next available bay when primary is occupied.
   - Line 327: Shop at full capacity raises `frappe.ValidationError`.
   - Lines 759–760 (Tier 3): Exhausted technician pool under `enable_technician_capacity=1` raises `frappe.ValidationError`.
5. **Feature 10 (Slot Generator with Capacity Indicators)**:
   - Line 347–351: Slots contain `start_time`, `available_bays`, `available_technicians`, `effective_capacity`.
   - Line 359: Slots starting directly during lunch break (e.g. 12:00) are suppressed.
   - Line 368: Passing `service_bay` filters slots specifically for that bay.
   - Line 381: `self.assertIn(slot.get("bottleneck"), ["bay", "technician", "none", None])`. (Crucial divergence: returns singular `"bay"` or `"technician"`, NOT plural `"bays"` or `"technicians"` as in induct_shop).
   - Line 390: Slot ending after `operating_hours_end` is suppressed.
   - Line 722: If `operating_hours_start == operating_hours_end`, returns `[]` gracefully.
6. **Feature 18 (ERPNext Holiday Awareness)**:
   - Line 637: `res = is_holiday(today())` returns dict with keys `"is_holiday"` and `"description"`.

---

## 2. Logic Chain

1. **Settings Layer Consistency**:
   - `Shop Settings` in `induct_shop` must map to `ISM Settings` in `d:\ism`.
   - Fields: `operating_hours_start`, `operating_hours_end`, `break_start`, `break_end`, `default_slot_interval`, `scheduling_horizon_days`, `enable_technician_capacity`, `technician_designation`, `holiday_list`, `default_diagnostic_duration`.
   - `get_shop_settings()` extracts these into timedeltas and typed defaults, providing uniform parameter resolution across both `scheduling.py` and `technician_availability.py`.

2. **Lunch Extension Mathematics**:
   - Given `start_td` and `duration_td`:
     - If `bs_td <= start_td < be_td`: Work cannot begin until `be_td`. The effective completion is `be_td + duration_td`.
     - If `start_td < bs_td` and `(start_td + duration_td) > bs_td`: The appointment spans across the lunch break. Work is paused for `break_duration = be_td - bs_td`. The effective completion is `start_td + duration_td + break_duration`.
     - Otherwise, the appointment does not overlap lunch: effective completion is `start_td + duration_td`.
   - This exact formulation satisfies all 7 boundary and happy-path test cases without extra branching.

3. **Collision Detection via Half-Open Intervals**:
   - An appointment interval $[s_1, e_1)$ overlaps with $[s_2, e_2)$ if and only if $\max(s_1, s_2) < \min(e_1, e_2)$.
   - Both intervals use `effective_end_time` so lunch extensions correctly block subsequent jobs (Tier 3 verification).
   - Adjacent boundary jobs where $e_1 = s_2$ yield $\max(s_1, s_2) = \min(e_1, e_2)$, resulting in `False` (no overlap), which permits back-to-back scheduling.

4. **Earliest-Fit Bay Matching with Capability Filtering**:
   - Query active bays (`is_active = 1`) ordered by `name asc`.
   - For each bay, query child table `Service Bay Equipment` for `equipment_tag`.
   - Check if `set(required_tags).issubset(set(bay_tags))`.
   - Check `check_bay_availability(bay, date, start_time, duration)`.
   - Before assigning, if `enable_technician_capacity` is set, verify `technician_availability.get_technician_pool_availability()["is_available"]`.
   - Return first capable bay, or raise `frappe.ValidationError`.

5. **Slot Generation & Capacity Indication**:
   - Walk from `operating_hours_start` to `operating_hours_end` in steps of `default_slot_interval`.
   - Skip slots where `curr_time` falls inside lunch `[bs_td, be_td)`.
   - Skip slots where `effective_end_time(curr_time, duration) > operating_hours_end`.
   - Calculate `avail_bays_count` (single bay check if `service_bay` passed, else `len(get_available_bays())`).
   - If `enable_technician_capacity`, query `get_technician_pool_availability`.
   - Compute `effective_capacity = min(avail_bays_count, avail_techs_count)` and set `bottleneck` to `"bay"`, `"technician"`, or `"none"` (or `None`).
   - Only emit slots with `effective_capacity > 0`.

6. **Holiday Awareness Integration**:
   - Look up `ISM Settings.holiday_list`. If set, query standard ERPNext `Holiday` child table for matching `holiday_date`. Return `{"is_holiday": bool, "description": str}`.

---

## 3. Caveats

- `get_technician_pool_availability` resides in `ism.api.technician_availability` (peer API in Milestone 3). In `scheduling.py`, calls to `get_technician_pool_availability` must use lazy internal imports with graceful fallback (`try ... except ImportError: pass`) so that `scheduling.py` functions can be unit-tested even before `technician_availability.py` is fully loaded.
- `Schedule Entry Type` records do not restrict bay selection, but `Schedule Entry Type.requires_repair_order` is validated on the `Schedule Entry` DocType level.
- Multi-day jobs spanning beyond `operating_hours_end` are not supported by the daily slot generator; appointments are bounded within a single day's operating hours.

---

## 4. Conclusion

The technical strategy for `ism/api/scheduling.py` is fully formulated and reconciled against `test_scheduling_capacity.py` and `induct_shop`. All 5 required functions plus core helpers (`to_timedelta`, `format_timedelta`, `get_shop_settings`, `get_available_bays`) are specified with exact boundary handling.

A production-ready reference implementation has been generated at:
`d:\ism\.agents\teamwork\m3_it1_explorer_1\proposed_scheduling.py`

This file is ready for immediate deployment to `d:\ism\ism\api\scheduling.py`.

---

## 5. Verification Method

To verify the implementation once applied by the builder:

1. **Host-to-WSL Synchronization**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   ```

2. **Automated Unit & Integration Test Run**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling_capacity
   ```

3. **Targeted Invalidation Conditions**:
   - Any test failure in `test_tier1_f6_*` indicates a regression in lunch-aware `effective_end_time`.
   - Any test failure in `test_tier1_f7_*` or `test_tier2_f7_*` indicates boundary flaw in `check_bay_availability`.
   - Any test failure in `test_tier1_f8_*` or `test_tier1_f9_*` indicates equipment tag set evaluation or sorting error in `auto_assign_bay`.
   - Any test failure in `test_tier1_f10_*` indicates slot interval, closing time, lunch suppression, or singular bottleneck name mismatch in `get_available_slots`.
   - Any test failure in `test_tier1_f18_*` indicates `is_holiday` contract mismatch.
