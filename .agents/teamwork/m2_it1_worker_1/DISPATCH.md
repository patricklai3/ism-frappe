## 2026-10-27T01:51:21Z
You are Worker m2_it1_worker_1 for Milestone 2: Tunable Estimation Engine & Pure Math Core.
Working directory: d:\ism\.agents\teamwork\m2_it1_worker_1
Parent Orchestrator: Project Orchestrator Gen 5 (conversation ID: 0559b1b6-9543-4fd4-bc30-1fd2e15ab34b)

Domain Skill to load and follow:
- d:\ism\.agents\skills\frappe-app-dev\SKILL.md

Mandatory inputs to read:
- d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
- d:\ism\PROJECT.md
- d:\ism\TEST_READY.md
- d:\ism\.agents\teamwork\m2_it1_explorer_1\handoff.md
- d:\ism\.agents\teamwork\m2_it1_explorer_2\handoff.md
- d:\ism\.agents\teamwork\m2_it1_explorer_3\handoff.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Your task is to implement Milestone 2 end-to-end:
1. Update `PROJECT.md`:
   - Set Milestone 1 Status to `DONE`.
   - Set Milestone 2 Status to `IN_PROGRESS`.
2. Create `ism/scheduling/__init__.py` and `ism/scheduling/estimation_light.py`:
   - Implement `approximate_z_score(percentile)` (Abramowitz & Stegun 26.2.23, zero-scipy).
   - Implement `get_sigma_for_item(item_group)` (Brake 0.20, Tire 0.15, Suspension 0.35, Electrical 0.50, Diagnostic 0.40, Body 0.30, HVAC 0.25, Drivetrain 0.40, Default 0.30; case-insensitive, strip whitespace).
   - Implement `estimate_duration(flat_rate_minutes, sigma, percentile, **kwargs)` (log-normal duration integer ceiling, min 1, validates inputs).
   - Implement `estimate_total_duration(flat_rate_list, sigma, percentile)` (Fenton-Wilkinson summation, empty list returns 0, single item bypasses moment matching, validates inputs).
   - Follow the verified code in Explorer 1's handoff report (`m2_it1_explorer_1/handoff.md`).
3. Create `ism/api/estimation_service.py`:
   - Implement `@frappe.whitelist() def get_estimate(item_code=None, **kwargs) -> Dict[str, Any]`
   - Implement `@frappe.whitelist() def get_total_estimate(item_codes, **kwargs) -> Dict[str, Any]`
   - Read dynamic `ISM Settings` without module-level caching. Normalize percentiles (if > 1.0, divide by 100.0).
   - Resolve `Item.custom_frt` (stored in hours) and convert to minutes (* 60.0).
   - Return exact dictionary schemas required by Interface Contract 2 and test assertions (`duration_minutes`, `total_duration_minutes`, `p_pure`, `buffer_padding`, `percentile`, `sigma`, `operations_count`).
   - Follow the verified code in Explorer 2's handoff report (`m2_it1_explorer_2/handoff.md`).
4. Update `ism/setup.py`:
   - In `setup_item_custom_fields()`, add custom field `custom_frt` (Float, label "Flat Rate Time (Hours)", insert_after "revision").
5. Update `ism/tests/test_fixtures.py`:
   - In `ensure_items()`, ensure existing items get updated with `custom_frt` if already in the database.
6. Synchronize changes to WSL bench:
   `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
7. Apply custom field schema in devcontainer:
   `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute ism.setup.setup_item_custom_fields`
8. Execute verification commands:
   - Math test suite:
     `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math`
     (Confirm: 41 tests pass with 0 skipped, 0 failures, 0 errors, exit code 0)
   - Backward-compatible proxy suite:
     `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation`
     (Confirm: 41 tests pass, exit code 0)
   - Full application test suite:
     `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
     (Confirm: 381 tests ran, 0 failures, 0 errors, exit code 0)
   - Database hygiene check:
     `wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('SE count:', frappe.db.count('Schedule Entry')); non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Vehicles:', len(non_seed)); assert frappe.db.count('Schedule Entry') == 0; assert len(non_seed) == 0; print('DATABASE CLEAN')"`
9. Write your complete report in `d:\ism\.agents\teamwork\m2_it1_worker_1\handoff.md`.
10. Send a completion message back via send_message.
