# Dispatch: Explorer 2 (Milestone 3 — Technician Availability APIs)

**Agent**: `m3_it1_explorer_2`  
**Role**: Explorer (`teamwork_preview_explorer`)  
**Parent**: Project Orchestrator Gen 6 (`60694e2b-2673-4a73-bfb2-434f75ef28e2`)  
**Working Directory**: `d:\ism\.agents\teamwork\m3_it1_explorer_2`  

## Mandatory Documents to Read
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first before starting work!)
2. `d:\ism\PROJECT.md` (Interface Contract 4, Features 11–17)
3. `d:\ism\TEST_READY.md`
4. Reference repository at `C:\Users\real2\AppData\Local\Temp\induct_shop`

## Exploration Scope: Technician Availability APIs (`ism/api/technician_availability.py`)
Investigate requirements and technical design for `ism/api/technician_availability.py`:
1. `get_active_technicians(date=None) -> List[Dict]`:
   - Querying active `Employee` records matching `ISM Settings.technician_designation`.
   - Cross-referencing `Leave Application` in ERPNext/HRMS to flag or exclude technicians on approved leave on `date`.
2. `get_technician_pool_availability(date, start_time, duration_minutes, exclude_entry=None) -> Dict[str, Any]`:
   - Returns counts: `total_technicians`, `on_leave`, `on_duty`, `occupied`, `available`, `is_available`.
   - Calculating concurrent active `Schedule Entry` appointments assigned to technicians during the given window.
   - Master toggle awareness: how `enable_technician_capacity` influences availability logic.
3. `check_technician_availability(employee, date, start_time, duration_minutes, exclude_entry=None) -> Dict[str, Any]`:
   - Evaluating individual technician assignment, leave status, and overlapping appointments.
   - Returning `is_available`, `reason`, and `conflicts`.
4. `get_technician_queue(employee, date) -> Dict[str, Any]`:
   - Individual technician daily timeline, assigned appointments, gap intervals, utilization %.
5. `get_daily_technician_overview(date) -> Dict[str, Any]`:
   - Shop-wide dashboard summarizing total scheduled technician hours, shop utilization, bottleneck, unassigned entries.
6. Examine corresponding test methods in `ism/tests/test_scheduling_capacity.py`.
7. Formulate a detailed, concrete implementation strategy in `handoff.md` and notify parent.


## 2026-10-27T05:21:03Z
[Message] timestamp=2026-10-27T05:21:03Z sender=60694e2b-2673-4a73-bfb2-434f75ef28e2 priority=MESSAGE_PRIORITY_HIGH content=You are m3_it1_explorer_2. Your working directory is d:\ism\.agents\teamwork\m3_it1_explorer_2. Read your dispatch file at d:\ism\.agents\teamwork\m3_it1_explorer_2\DISPATCH.md. Also read:
- d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
- d:\ism\PROJECT.md
- d:\ism\TEST_READY.md
- Reference code at C:\Users\real2\AppData\Local\Temp\induct_shop
Investigate technician availability APIs for ism/api/technician_availability.py (get_active_technicians, get_technician_pool_availability, check_technician_availability, get_technician_queue, get_daily_technician_overview, HRMS Leave Application integration, master capacity toggle), map against test_scheduling_capacity.py, write your detailed implementation strategy to handoff.md, and notify parent orchestrator_6.
