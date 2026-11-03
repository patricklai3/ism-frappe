# Dispatch: Challenger 2 (Milestone 3 Gate)

**Agent**: `m3_gate_challenger_2`  
**Role**: Challenger (`teamwork_preview_challenger`)  
**Parent**: Project Orchestrator Gen 6 (`60694e2b-2673-4a73-bfb2-434f75ef28e2`)  
**Working Directory**: `d:\ism\.agents\teamwork\m3_gate_challenger_2`  

## Mandatory Documents to Read
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first before starting work!)
2. `d:\ism\PROJECT.md`
3. `d:\ism\TEST_READY.md`
4. `d:\ism\.agents\teamwork\m3_it1_worker_1\handoff.md`

## Target Files
- `d:\ism\ism\api\technician_availability.py`
- `d:\ism\ism\ism\doctype\schedule_entry\schedule_entry.py`
- `d:\ism\ism\tests\test_scheduling_capacity.py`

## Mission
Conduct empirical adversarial stress testing on technician pool capacity & concurrency:
1. Stress test technician pool exhaustion: test booking attempts when all on-duty technicians are busy.
2. Stress test toggle `enable_technician_capacity`: verify pool bypass when toggle is 0 and strict gating when 1.
3. Stress test HRMS leave application integration: multi-day leave span, half-day leaves, open/pending leaves (must not block), approved leaves on different dates.
4. Verify regression suites and database hygiene:
   `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
   `wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); assert frappe.db.count('Schedule Entry') == 0; non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; assert len(non_seed) == 0; print('DATABASE CLEAN')"`
5. Formulate your verdict (`APPROVE` or `REQUEST_CHANGES`), write `handoff.md`, and notify parent.


## 2026-10-27T05:43:32Z
You are m3_gate_challenger_2. Your working directory is d:\ism\.agents\teamwork\m3_gate_challenger_2. Read your dispatch file at d:\ism\.agents\teamwork\m3_gate_challenger_2\DISPATCH.md. Also read:
- d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
- d:\ism\PROJECT.md
- d:\ism\TEST_READY.md
- d:\ism\.agents\teamwork\m3_it1_worker_1\handoff.md
Perform adversarial stress-testing on technician pool capacity & concurrency (pool exhaustion, master toggle bypass, HRMS leave scenarios, regression suite, database hygiene), run tests, formulate your verdict (APPROVE or REQUEST_CHANGES), write handoff.md, and notify parent orchestrator_6.
