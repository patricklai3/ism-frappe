## 2026-10-27T01:17:43Z

You are Worker m1_it7_worker_1.
Working directory: d:\ism\.agents\teamwork\m1_it7_worker_1
Parent Orchestrator: Project Orchestrator Gen 5 (conversation ID: 0559b1b6-9543-4fd4-bc30-1fd2e15ab34b)

Domain Skill to load and follow:
- d:\ism\.agents\skills\frappe-app-dev\SKILL.md

Mandatory inputs to read:
- d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md
- d:\ism\PROJECT.md
- d:\ism\TEST_READY.md
- d:\ism\.agents\teamwork\orchestrator_4\handoff.md
- d:\ism\.agents\teamwork\m1_it7_explorer_1\handoff.md
- d:\ism\.agents\teamwork\m1_it7_explorer_2\handoff.md
- d:\ism\.agents\teamwork\m1_it7_explorer_3\handoff.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Your task is to implement the concrete 5-pillar Iteration 7 remediation formulated by Explorers 1, 2, and 3:

### Pillar 1: Remove ALL Mid-Test Commits in base.py & Test Helpers
1. `ism/tests/base.py`:
   - Remove `frappe.db.commit()` from `create_test_customer()` (line 71), `create_test_vehicle()` (line 97), `create_test_quotation()` (line 119), `create_test_repair_order()` (line 137), `create_test_schedule_entry()` (line 159), and `cleanup_tracked_docs()` (line 241).
   - Remove `frappe.db.rollback()` from helper retry loops (lines 75, 101, 123, 141, 163).
2. `ism/tests/test_fixtures.py`:
   - Remove `frappe.db.commit()` and `frappe.db.rollback()` from `create_test_schedule_entry()` (lines 342, 346) and `create_test_repair_order()` (lines 367, 371).
   - In `teardown_transactional()`: Remove `frappe.db.commit()` (line 424), use `frappe.db.rollback()` to release locks.
3. `ism/ism/doctype/schedule_entry/test_schedule_entry.py`:
   - Remove `frappe.db.commit()` from `setUp()` (line 16).
   - Remove `frappe.db.commit()` and `frappe.db.rollback()` from `_insert_entry()` (lines 23, 27).
   - Remove `frappe.db.commit()` from `test_requires_repair_order_validation()` / `test_entry_type_requires_repair_order()` (line 240).

### Pillar 2: Clean Sequence Naming Without Inner Rollbacks
1. `ism/ism/doctype/schedule_entry/schedule_entry.py`:
   - In `autoname()`: NEVER call `frappe.db.rollback()`.
   - Implement safe series naming fallback using `_generate_safe_series_name` (or retry make_autoname without rollback) when MariaDB 1020 occurs, ensuring caller test fixtures (e.g. Service Bay) are never rolled back.
2. `ism/ism/doctype/repair_order/repair_order.py`:
   - Implement `autoname(self)` on `RepairOrder` controller with `_generate_safe_series_name` fallback (prefix `RO-.YYYY.-.#####`) without rollback to eliminate tabSeries contention across test suites.

### Pillar 3: Class-Level Fixture Lifecycle (setUpClass vs setUp)
1. `ism/tests/test_ro_scheduling_e2e.py`:
   - Move `setup_all()` from per-test `setUp()` to `@classmethod setUpClass(cls)`.
   - Remove `teardown_transactional()` from per-test `tearDown()`. Move final cleanup to `@classmethod tearDownClass(cls)`.
2. `ism/tests/test_scheduling_capacity.py`:
   - Move `setup_all()` from per-test `setUp()` to `@classmethod setUpClass(cls)`.
   - Remove `teardown_transactional()` from per-test `tearDown()`. Move final cleanup to `@classmethod tearDownClass(cls)`.
3. `ism/tests/test_estimation_math.py`:
   - Move `setup_all()` from per-test `setUp()` to `@classmethod setUpClass(cls)`.
   - Remove `teardown_transactional()` from per-test `tearDown()`.

### Pillar 4: Guard ISM Settings & tabSingles
1. `ism/ism/doctype/ism_settings/test_ism_settings.py`:
   - In `test_setup_idempotency_preserves_customizations`:
     - Wrap setting customization in a 3-attempt restart loop with atomic dictionary assignment:
       `frappe.db.set_single_value("ISM Settings", {"default_slot_interval": 45, "enable_technician_capacity": 0})`
     - Catch 1020 / Deadlock, call `frappe.db.rollback()`, clear caches (`frappe.clear_cache(doctype="ISM Settings")` and `frappe.db.value_cache.clear()`), and retry.

### Pillar 5: Resilient Teardown, Cleanup Hardening & Fixture Order
1. `ism/tests/cleanup.py`:
   - At start of `cleanup_all_test_data()`, call `frappe.db.rollback()`.
   - In `_cleanup_repair_vehicles()`: Expand purge filter to catch `v.name.startswith("5YJ3E1EBXNF")`, `v.customer in test_customers`, `v.customer and (v.customer.startswith("_TEST_") or "Test" in v.customer)`, dangling customers (`not frappe.db.exists("Customer", v.customer)`), and test plates.
   - In `_cleanup_repair_orders()`: Catch dangling customer foreign keys (`not frappe.db.exists("Customer", ro.customer)`).
2. `ism/ism/doctype/repair_order/test_repair_order.py`:
   - In `test_vehicle_checking_incorporated_into_repair_order`: Ensure strict dependency creation order (customer created first, then inspection template, then repair order).

### Host Synchronization & Execution
Push modified files from Windows to WSL2:
`powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`

Run and verify the 4 mandatory test suites:
1. Database cleanup & state hygiene check:
   `wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); from ism.tests.cleanup import cleanup_all_test_data; cleanup_all_test_data(True); se_count = frappe.db.count('Schedule Entry'); non_seed = [v.name for v in frappe.get_all('Repair Vehicle', fields=['name']) if v.name not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Residual SE:', se_count); print('Residual leaked vehicles:', len(non_seed)); assert se_count == 0; assert len(non_seed) == 0; print('CLEANUP CHECK PASSED')"`
2. 10x Sequential Stress Loop on Schedule Entry:
   `powershell -Command "1..10 | ForEach-Object { Write-Host \"=== SE RUN `$_ ===\"; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype \"Schedule Entry\"; if (`$LASTEXITCODE -ne 0) { exit 1 } }"`
3. 10x Sequential Stress Loop on ISM Settings:
   `powershell -Command "1..10 | ForEach-Object { Write-Host \"=== SETTINGS RUN `$_ ===\"; wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --doctype \"ISM Settings\"; if (`$LASTEXITCODE -ne 0) { exit 1 } }"`
4. Full Application Test Suite:
   `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
   (Target: 381 tests ran, 0 errors, 0 failures, exit code 0)

Document all changes, exact commands executed, and output in `d:\ism\.agents\teamwork\m1_it7_worker_1\handoff.md`.
Communicate results back to parent orchestrator via send_message.
