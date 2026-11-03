# Dispatch: Challenger 2 (Milestone 2 Gate)

**Agent**: `m2_gate_challenger_2_gen6`  
**Role**: Challenger (`teamwork_preview_challenger`)  
**Parent**: Project Orchestrator Gen 6 (`60694e2b-2673-4a73-bfb2-434f75ef28e2`)  
**Working Directory**: `d:\ism\.agents\teamwork\m2_gate_challenger_2_gen6`  

## Mandatory Documents to Read
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first before starting work!)
2. `d:\ism\PROJECT.md`
3. `d:\ism\TEST_READY.md`
4. `d:\ism\.agents\teamwork\m2_it1_worker_1\handoff.md`

## Primary Target Code & Test Files
- `d:\ism\ism\api\estimation_service.py`
- `d:\ism\ism\tests\test_estimation_math.py`
- `d:\ism\ism\tests\test_estimation.py`
- Existing test suites (`test_parts_management`, `test_tesla_integration`)

## Mission
Perform empirical adversarial stress testing on API services, settings integration, regression, and database hygiene:
1. Dynamic Settings Mutation Stress:
   - Verify that altering `ISM Settings.estimation_percentile`, `estimation_sigma`, or `buffer_padding_minutes` changes the return values of `get_estimate` and `get_total_estimate` dynamically without restarting Frappe or reloading modules.
2. API Parameter Stress & Sanitization:
   - Test `get_estimate` with missing items, items with null `custom_frt`, JSON payload strings vs Python lists in `item_codes`.
   - Verify that `p_pure` + `buffer_padding` equals `duration_minutes` / `total_duration_minutes`.
3. Regression & Database Hygiene Verification:
   - Run the full suite in devcontainer:
     `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
   - Run regression suites:
     `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_parts_management`
     `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_tesla_integration`
   - Verify database clean state:
     `wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('SE count:', frappe.db.count('Schedule Entry')); non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Vehicles:', len(non_seed)); assert frappe.db.count('Schedule Entry') == 0; assert len(non_seed) == 0; print('DATABASE CLEAN')"`
4. Write your empirical challenge report and verdict (`APPROVE` or `REQUEST_CHANGES`) to `d:\ism\.agents\teamwork\m2_gate_challenger_2_gen6\handoff.md` and send completion notification to parent.

## 2026-10-27T05:11:41Z
You are m2_gate_challenger_2_gen6. Your working directory is d:\ism\.agents\teamwork\m2_gate_challenger_2_gen6. Read your dispatch file at d:\ism\.agents\teamwork\m2_gate_challenger_2_gen6\DISPATCH.md. Also read:
- d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
- d:\ism\PROJECT.md
- d:\ism\TEST_READY.md
- d:\ism\.agents\teamwork\m2_it1_worker_1\handoff.md
Perform adversarial empirical stress-testing on dynamic settings recalculation, API parameters, full regression test execution, and database hygiene, formulate your verdict (APPROVE or REQUEST_CHANGES), write handoff.md, and notify parent orchestrator_6.
