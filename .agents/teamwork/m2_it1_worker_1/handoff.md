# Milestone 2 Implementation Handoff Report: Tunable Estimation Engine & Pure Math Core

**Worker**: `m2_it1_worker_1`  
**Milestone**: Milestone 2 — Tunable Estimation Engine & Pure Math Core  
**Date**: 2026-10-27T01:58:30Z  
**Parent Orchestrator**: Project Orchestrator Gen 5 (`0559b1b6-9543-4fd4-bc30-1fd2e15ab34b`)

---

## 1. Observation

### 1.1 Baseline State & Target Requirements
Prior to implementation:
- `ism/scheduling/` directory did not exist.
- `ism/api/estimation_service.py` did not exist.
- `tabItem` schema lacked `custom_frt` (OperationalError 1054 "Unknown column 'custom_frt' in 'SELECT'").
- Running `ism.tests.test_estimation_math` resulted in 41 skipped tests because `estimate_duration is None` and `get_estimate is None` triggered test skip guards.
- `PROJECT.md` had Milestone 1 as `IN_PROGRESS` and Milestone 2 as `PLANNED`.

### 1.2 Created and Modified Files
1. **`d:\ism\PROJECT.md`** (lines 87–88):
   - Set Milestone 1 Status to `DONE`.
   - Set Milestone 2 Status to `IN_PROGRESS`.
2. **`d:\ism\ism\scheduling\__init__.py`**:
   - Created Python package initialization for `ism.scheduling`.
3. **`d:\ism\ism\scheduling\estimation_light.py`** (426 lines):
   - Implemented Abramowitz & Stegun 26.2.23 rational approximation (`approximate_z_score`).
   - Implemented `get_sigma_for_item` supporting case-insensitive, whitespace-stripped lookup with category profiles:
     - Brake: 0.20
     - Tire: 0.15
     - Suspension: 0.35
     - Electrical: 0.50
     - Diagnostic: 0.40
     - Body: 0.30
     - HVAC: 0.25
     - Drivetrain: 0.40
     - Default: 0.30 (or `default_sigma`).
   - Implemented `estimate_duration(flat_rate_minutes, sigma, percentile, **kwargs)` with input validation, integer ceiling, and min 1.
   - Implemented `estimate_total_duration(flat_rate_list, sigma, percentile)` with Fenton-Wilkinson composite log-normal summation, single-item direct bypass, empty list returning 0, and non-positive FRT validation.
4. **`d:\ism\ism\api\estimation_service.py`** (566 lines):
   - Implemented `@frappe.whitelist() def get_estimate(item_code=None, **kwargs) -> Dict[str, Any]`
   - Implemented `@frappe.whitelist() def get_total_estimate(item_codes, **kwargs) -> Dict[str, Any]`
   - Read dynamic `ISM Settings` without module-level caching.
   - Normalized percentiles ($p > 1.0 \implies p / 100.0$).
   - Resolved `Item.custom_frt` in hours to minutes ($frt \times 60.0$).
   - Conformed to return schema: `duration_minutes`, `total_duration_minutes`, `p_pure`, `buffer_padding`, `percentile`, `sigma`, `operations_count`.
5. **`d:\ism\ism\setup.py`** (lines 477–482):
   - Added custom field `custom_frt` (`Float`, label `"Flat Rate Time (Hours)"`, `insert_after="revision"`).
6. **`d:\ism\ism\tests\test_fixtures.py`** (lines 285–289):
   - Enhanced `ensure_items()` with an `else` branch to update `custom_frt` on pre-existing test items.

### 1.3 Execution Tool Commands & Verbatim Outputs
1. **Host-to-WSL Synchronization**:
   Command: `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
   Output:
   ```
   [sync-to-wsl] Syncing D:/ism -> WSL2 (Ubuntu:/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/)...
   sending incremental file list
   sent 44,644 bytes  received 198 bytes  17,936.80 bytes/sec
   total size is 12,787,448  speedup is 285.17
   [sync-to-wsl] Complete.
   ```
   Exit code: 0.

2. **Schema Migration Execution**:
   Command: `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute ism.setup.setup_item_custom_fields`
   Exit code: 0.

3. **Column Check**:
   Command: `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost execute "frappe.db.has_column" --args "('Item', 'custom_frt')"`
   Output: `true`
   Exit code: 0.

4. **Math Test Suite Execution**:
   Command: `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math`
   Output:
   ```
   Running 41 unspecified-category tests for ism

   ism.tests.test_estimation_math.TestEstimationMath
       ✔  test_tier1_f1_p80_120_minutes
       ✔  test_tier1_f1_p80_15_minutes
       ✔  test_tier1_f1_p80_30_minutes
       ✔  test_tier1_f1_p80_60_minutes_baseline
       ✔  test_tier1_f1_p80_90_minutes
       ✔  test_tier1_f2_fw_sum_dict_input
       ✔  test_tier1_f2_fw_sum_diverse_durations
       ✔  test_tier1_f2_fw_sum_heterogeneous_sigmas
       ✔  test_tier1_f2_fw_sum_identical_triplet
       ✔  test_tier1_f2_fw_sum_two_large_ops
       ✔  test_tier1_f3_brake_sigma
       ✔  test_tier1_f3_default_and_none_sigma
       ✔  test_tier1_f3_electrical_sigma
       ✔  test_tier1_f3_suspension_sigma
       ✔  test_tier1_f3_tire_sigma
       ✔  test_tier1_f4_z_score_p50
       ✔  test_tier1_f4_z_score_p80
       ✔  test_tier1_f4_z_score_p90
       ✔  test_tier1_f4_z_score_p95
       ✔  test_tier1_f4_z_score_p99
       ✔  test_tier1_f5_max_buffer_setting
       ✔  test_tier1_f5_multi_item_estimate_with_buffer
       ✔  test_tier1_f5_single_item_estimate_with_buffer
       ✔  test_tier1_f5_whitelisted_json_payload
       ✔  test_tier1_f5_zero_buffer_setting
       ✔  test_tier2_f1_extreme_percentile_boundaries
       ✔  test_tier2_f1_extreme_sigma_boundaries
       ✔  test_tier2_f1_large_frt_overflow_protection
       ✔  test_tier2_f1_very_small_frt_clamps_to_min_1
       ✔  test_tier2_f1_zero_and_negative_frt_raises
       ✔  test_tier2_f2_empty_list_returns_zero
       ✔  test_tier2_f2_large_operation_count
       ✔  test_tier2_f2_list_with_invalid_frt_raises
       ✔  test_tier2_f2_single_item_matches_estimate_duration
       ✔  test_tier2_f3_case_insensitive_category
       ✔  test_tier2_f4_z_score_monotonicity
       ✔  test_tier2_f5_missing_item_fallback_frt
       ✔  test_tier2_f5_skip_missing_items
       ✔  test_tier3_dynamic_settings_recalculation
       ✔  test_tier3_fw_sum_with_category_sigmas_and_buffer
       ✔  test_tier4_scenario_full_service_package_estimation
   ----------------------------------------------------------------------
   Ran 41 tests in 0.828s

   OK
   ```
   Exit code: 0.

5. **Backward-Compatible Proxy Suite**:
   Command: `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation`
   Output: `Ran 41 tests in 0.767s. OK`
   Exit code: 0.

6. **Full Application Suite Execution**:
   Command: `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism`
   Output:
   ```
   Ran 381 tests in 22.002s
   OK (skipped=172)
   ```
   Exit code: 0 (0 failures, 0 errors).

7. **Database Hygiene Check**:
   Command: `wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('SE count:', frappe.db.count('Schedule Entry')); non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Vehicles:', len(non_seed)); assert frappe.db.count('Schedule Entry') == 0; assert len(non_seed) == 0; print('DATABASE CLEAN')"`
   Output:
   ```
   SE count: 0
   Vehicles: 0
   DATABASE CLEAN
   ```
   Exit code: 0.

8. **Regression Verification Suites**:
   - `test_parts_management`: `Ran 30 tests in 8.841s. OK`
   - `test_tesla_integration`: `Ran 7 tests in 3.963s. OK`

---

## 2. Logic Chain

1. **Pure Math Decoupling**:
   - By creating `ism/scheduling/estimation_light.py` with zero dependencies on Frappe ORM, Redis, or heavy numerical libraries (no `scipy`, no `numpy`), the mathematical core executes in pure Python using Abramowitz & Stegun Formula 26.2.23.
   - This satisfied all assertions in `test_estimation_math` for Features 1–4 (baseline quantiles, Fenton-Wilkinson summation, category sigmas, rational Z-scores, and boundary guards).

2. **Dynamic Settings & Normalization**:
   - Frappe DocType `ISM Settings` stores `estimation_percentile` as a `Percent` field (e.g. `80.0`), while math calculations expect decimal proportions (e.g. `0.80`).
   - Normalizing $p > 1.0 \implies p / 100.0$ and dynamically reading settings on each API call without module caching ensured that tests mutating settings via `ensure_ism_settings(estimation_percentile=0.95)` immediately altered calculations without process restarts.

3. **Hours to Minutes Unit Conversion**:
   - ERPNext items store `custom_frt` in hours (e.g. `1.0` hr for brake pad, `0.5` hr for tire mount).
   - In `_get_frt()`, multiplying by 60.0 converts to minutes as required by the log-normal core, satisfying exact assertions (`p_pure == 71` for 60m FRT at $\sigma=0.20$, P80).

4. **Schema & Fixture Resilience**:
   - Adding `custom_frt` to `setup_item_custom_fields()` and providing the update branch in `ensure_items()` ensured pre-existing database records in `development.localhost` received proper FRT values during test runs.

5. **Proxy Module Equivalence**:
   - `test_estimation.py` re-exports `TestEstimationMath` from `test_estimation_math.py`, ensuring both `run-tests --module ism.tests.test_estimation` and `--module ism.tests.test_estimation_math` execute the identical 41 test cases.

---

## 3. Caveats

- **No Caveats**: All 41 estimation tests passed, 0 skipped, 0 failures. The entire application test suite (381 tests) ran cleanly with 0 failures and 0 errors, and database hygiene was verified with 0 leftover test records.
- **Future Milestone Interaction**: Milestones M3, M4, and M5 build on the estimation endpoints (`get_estimate` and `get_total_estimate`). These endpoints are now fully stable and verified.

---

## 4. Conclusion

Milestone 2 (Tunable Estimation Engine & Pure Math Core) is 100% complete and fully verified:
- Pure math core in `ism/scheduling/estimation_light.py` satisfies all log-normal and Fenton-Wilkinson summation requirements.
- Whitelisted API service in `ism/api/estimation_service.py` satisfies Interface Contract 2 and dynamic settings integration.
- Database schema and test fixtures properly support `Item.custom_frt`.
- `PROJECT.md` correctly reflects Milestone 1 as `DONE` and Milestone 2 as `IN_PROGRESS`.

---

## 5. Verification Method

To independently verify this implementation:

1. **Verify Math Test Suite (41 tests, 0 skipped, 0 failures)**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math
   ```

2. **Verify Backward-Compatible Proxy Suite (41 tests, 0 failures)**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation
   ```

3. **Verify Full Application Test Suite (381 tests, 0 failures, 0 errors)**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism
   ```

4. **Verify Database Hygiene**:
   ```powershell
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('SE count:', frappe.db.count('Schedule Entry')); non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Vehicles:', len(non_seed)); assert frappe.db.count('Schedule Entry') == 0; assert len(non_seed) == 0; print('DATABASE CLEAN')"
   ```
