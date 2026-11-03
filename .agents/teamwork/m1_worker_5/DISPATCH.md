# Dispatch Instructions for Worker 5 (Milestone 1 Iteration 5)

Working directory: `d:\ism\.agents\teamwork\m1_worker_5`
Role: Implementation & Test Execution Worker
Task: Milestone 1 Iteration 5 Remediation

Authoritative requirements: `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md`
Global project plan: `d:\ism\PROJECT.md`
Forensic Audit Report: `d:\ism\.agents\teamwork\m1_it4_auditor_1\handoff.md`
Explorer 1 Blueprint: `d:\ism\.agents\teamwork\m1_it5_explorer_1\handoff.md` (and `test_ism_settings.patch`)
Explorer 2 Blueprint: `d:\ism\.agents\teamwork\m1_it5_explorer_2\handoff.md`
Explorer 3 Blueprint: `d:\ism\.agents\teamwork\m1_it5_explorer_3\handoff.md`
Dead ends tracking: `d:\ism\DEAD_ENDS.md`

## MANDATORY INTEGRITY WARNING
> DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Objective
Apply the unified remediation blueprints from Explorers 1, 2, and 3:
1. `ism/ism/doctype/ism_settings/test_ism_settings.py`:
   Apply zero-churn conditional baseline check in `setUpClass()` (from `d:\ism\.agents\teamwork\m1_it5_explorer_1\test_ism_settings.patch` or blueprint).
2. `ism/tests/cleanup.py`:
   Preserve static test fixtures (`Test Customer`, `5YJSA1E26CF000001`, `_TEST_Customer`), separate `SEED_PRESERVED_*` from `STATIC_FIXTURE_*`, remove `5YJSA1E26CF000001` from `TEST_VINS`.
3. `ism/tests/base.py`:
   - Quarantine transaction snapshots: issue `frappe.db.rollback()` at start of `setUp()` and end of `tearDown()`.
   - In `cleanup_tracked_docs()`, issue `frappe.db.rollback()` on early return and after cleanup commit.
   - Protect static fixtures in `create_test_customer()` and `create_test_vehicle()`.
   - Add MariaDB 1020 / QueryDeadlockError 3-attempt restart/retry loops to `create_test_customer()`, `create_test_vehicle()`, `create_test_repair_order()`.
4. `ism/tests/test_fixtures.py`:
   In `ensure_repair_vehicle()`, update customer link if existing without delete/re-insert.
5. `ism/ism/doctype/repair_order/test_repair_order.py`:
   Remove hardcoded `vin="5YJSA1E26CF000001"` in `setUp()` so it uses dynamic test VIN.
6. `ism/tests/test_tesla_integration.py`:
   Add `frappe.db.rollback()` in `setUp()` and `tearDown()`, remove redundant `self.track(ro)`.

## Verification Commands to Execute in Container
1. 10x sequential stress loop on `ISM Settings`:
   `powershell -Command "1..10 | ForEach-Object { Write-Host \"=== RUN `$_ ===\"; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'ISM Settings'; if (`$LASTEXITCODE -ne 0) { exit 1 } }"`
2. 10x sequential stress loop on `Schedule Entry`:
   `powershell -Command "1..10 | ForEach-Object { Write-Host \"=== RUN `$_ ===\"; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype 'Schedule Entry'; if (`$LASTEXITCODE -ne 0) { exit 1 } }"`
3. All 4 Milestone 1 test suites:
   `Service Bay`, `Equipment Tag`, `Schedule Entry Type`, `Schedule Entry`, `ISM Settings`
4. Full application test suite:
   `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
   Must pass 381 tests (127 active) with 0 errors, 0 failures, 0 deadlocks, exit code 0.

Document all modified files, commands run, and exact test output in `d:\ism\.agents\teamwork\m1_worker_5\handoff.md`.
