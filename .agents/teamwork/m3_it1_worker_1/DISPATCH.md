# Dispatch: Worker 1 (Milestone 3 Implementation)

**Agent**: `m3_it1_worker_1`  
**Role**: Worker (`teamwork_preview_worker`)  
**Parent**: Project Orchestrator Gen 6 (`60694e2b-2673-4a73-bfb2-434f75ef28e2`)  
**Working Directory**: `d:\ism\.agents\teamwork\m3_it1_worker_1`  

## MANDATORY INTEGRITY WARNING
> DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A forensic auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Mandatory Documents to Read
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first before starting work!)
2. `d:\ism\PROJECT.md` (Interface Contracts 3 & 4, Code Layout)
3. `d:\ism\TEST_READY.md`
4. Milestone 3 Explorer handoffs:
   - `d:\ism\.agents\teamwork\m3_it1_explorer_1\handoff.md`
   - `d:\ism\.agents\teamwork\m3_it1_explorer_1\proposed_scheduling.py`
   - `d:\ism\.agents\teamwork\m3_it1_explorer_2\handoff.md`
   - `d:\ism\.agents\teamwork\m3_it1_explorer_3\handoff.md`

## Write Ownership
You exclusively own and may modify:
- `d:\ism\ism\api\scheduling.py`
- `d:\ism\ism\api\technician_availability.py`
- `d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py`

## Implementation Tasks

### 1. `ism/api/scheduling.py`
Deploy the complete implementation from `d:\ism\.agents\teamwork\m3_it1_explorer_1\proposed_scheduling.py`:
- `to_timedelta`, `format_timedelta`, `get_shop_settings`
- `@frappe.whitelist() def effective_end_time`:
  - When starting before lunch and ending after break start: adds break duration.
  - When starting inside lunch break (`break_start <= start < break_end`): effective start is delayed to `break_end`, returning `break_end + duration`.
  - When ending at or before break start, or starting at or after break end: no extension.
- `@frappe.whitelist() def check_bay_availability`:
  - Uses lunch-aware intervals: `max(prop_start, ex_start) < min(prop_end, ex_end)`.
  - Supports `exclude_entry`.
- `@frappe.whitelist() def get_available_bays` & `@frappe.whitelist() def auto_assign_bay`:
  - Tag subset matching (`required_tags.issubset(bay_tags)`).
  - Earliest-fit sorting (`name asc`).
  - Checks technician pool availability if `enable_technician_capacity` is true.
  - Throws `frappe.ValidationError` if no capable bay free or pool exhausted.
- `@frappe.whitelist() def get_available_slots`:
  - Steps through operating hours by `default_slot_interval`.
  - Suppresses slots starting during lunch or ending after closing.
  - Computes capacity and singular bottleneck string (`"bay"`, `"technician"`, `"none"`, or `None`).
- `@frappe.whitelist() def is_holiday`:
  - Integrates with `ISM Settings.holiday_list` and ERPNext `Holiday` table.

### 2. `ism/api/technician_availability.py`
Deploy the complete implementation from `d:\ism\.agents\teamwork\m3_it1_explorer_2\handoff.md`:
- `@frappe.whitelist() def get_active_technicians(date=None)`:
  - Filters active employees with designation matching `ISM Settings.technician_designation`.
  - Checks approved `Leave Application` records for `date` (full-day and half-day).
- `def check_employee_leave(employee, date, start_time, duration_minutes)`
- `@frappe.whitelist() def get_technician_pool_availability(date, start_time, duration_minutes, exclude_entry=None)`:
  - If `enable_technician_capacity` is false, returns bypass dict with `is_available = True`.
  - Otherwise checks on-duty technicians and counts occupied technicians using lunch-aware intervals.
- `@frappe.whitelist() def check_technician_availability(employee, date, start_time, duration_minutes, exclude_entry=None)`:
  - Checks active status, designation match, leave status (`"on_leave"`), and overlaps (`"overlap"`).
- `@frappe.whitelist() def get_technician_queue(employee, date)`:
  - Returns dict with both `"timeline"` and `"entries"`, and both `"utilization_percent"` and `"utilization_pct"`.
  - Uses `repair_order` (not sales_order).
- `@frappe.whitelist() def get_daily_technician_overview(date)`:
  - Returns dict with `"total_scheduled_hours"`, `"bottleneck"`, `"technicians"`, `"unassigned_entries"`, and `"shop_summary"`.

### 3. `ism/ism/doctype/schedule_entry/schedule_entry.py`
Refine controller validation hooks:
- Add `end_time` property and `get_effective_end_time()` method.
- Update `validate_bay_availability()` to call `check_bay_availability`.
- Update `validate_technician_pool()` to check `enable_technician_capacity` and call `get_technician_pool_availability`.
- Update `validate_technician_assignment()` to call `check_technician_availability` and issue non-blocking soft warnings via `frappe.msgprint(..., alert=True)` (red for leave, orange for overlap).
- Ensure 1:N cardinality on `repair_order` is strictly preserved.

## Verification Protocol
Execute each of the following commands in the devcontainer:
1. Sync files:
   `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
2. Primary Capacity Suite (53 tests):
   `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling_capacity`
3. Backward-Compatible Proxies:
   `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_scheduling`
   `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_technician_availability`
4. Existing DocType Suite:
   `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype "Schedule Entry"`
5. Full Application Suite (All tests pass, 0 failures, 0 errors):
   `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
6. Database Cleanliness Check:
   `wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); assert frappe.db.count('Schedule Entry') == 0; non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; assert len(non_seed) == 0; print('DATABASE CLEAN')"`

## Reporting
Document all steps, modified files, test outputs, and conclusion in `d:\ism\.agents\teamwork\m3_it1_worker_1\handoff.md`, and notify parent.


## 2026-10-27T05:30:32Z
You are m3_it1_worker_1. Your working directory is d:\ism\.agents\teamwork\m3_it1_worker_1. Read your dispatch file at d:\ism\.agents\teamwork\m3_it1_worker_1\DISPATCH.md. Also read:
- d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
- d:\ism\PROJECT.md
- d:\ism\TEST_READY.md
- d:\ism\.agents\teamwork\m3_it1_explorer_1\handoff.md
- d:\ism\.agents\teamwork\m3_it1_explorer_1\proposed_scheduling.py
- d:\ism\.agents\teamwork\m3_it1_explorer_2\handoff.md
- d:\ism\.agents\teamwork\m3_it1_explorer_3\handoff.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A forensic auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

You exclusively own:
- d:\ism\ism\api\scheduling.py
- d:\ism\ism\api\technician_availability.py
- d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py

Implement the Milestone 3 deliverables according to the Explorer specifications, sync to WSL2, run and pass all 53 tests in test_scheduling_capacity.py, run the proxy suites and full application test suite, verify database hygiene, document all results in handoff.md, and notify parent orchestrator_6.
