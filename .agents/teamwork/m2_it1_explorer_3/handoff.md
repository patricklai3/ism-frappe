# Handoff Report — Milestone 2 Test Suite Investigation

**Agent**: Explorer 3 (`m2_it1_explorer_3`)  
**Mission**: Milestone 2 Test Suite Analysis, Skip Conditions, Assertions, and Verification Planning  
**Target Audience**: Project Orchestrator Gen 5 & Milestone 2 Worker  
**Date**: 2026-10-27T01:47:00Z  

---

## 1. Observation

### 1.1 Test File Structure and Architecture
1. **`ism/tests/test_estimation.py`**:
   - Location: `d:\ism\ism\tests\test_estimation.py` (5 lines, 164 bytes).
   - Content:
     ```python
     # Copyright (c) 2026, patricklai3 and contributors
     # For license information, please see license.txt

     from ism.tests.test_estimation_math import TestEstimationMath
     ```
   - Purpose: Backward-compatible test proxy so that calling `--module ism.tests.test_estimation` executes `TestEstimationMath` identically to `--module ism.tests.test_estimation_math`.

2. **`ism/tests/test_estimation_math.py`**:
   - Location: `d:\ism\ism\tests\test_estimation_math.py` (415 lines, 16,078 bytes).
   - Test Class: `class TestEstimationMath(ISMTestCase)` (inherits from `ISMTestCase` in `ism/tests/base.py`).
   - Total test methods: Exactly **41 test cases**.

### 1.2 Import Handlers & Skip Conditions
Lines 13–34 of `ism/tests/test_estimation_math.py`:
```python
try:
	from ism.scheduling.estimation_light import (
		estimate_duration,
		estimate_total_duration,
		get_sigma_for_item,
		approximate_z_score,
	)
except ImportError:
	estimate_duration = None
	estimate_total_duration = None
	get_sigma_for_item = None
	approximate_z_score = None

try:
	from ism.api.estimation_service import (
		get_estimate,
		get_total_estimate,
	)
except ImportError:
	get_estimate = None
	get_total_estimate = None
```

Skip Trigger Guards (Lines 66–73):
```python
	def _require_math(self):
		if estimate_duration is None:
			self.skipTest("ism.scheduling.estimation_light not yet implemented (Milestone M2)")

	def _require_service(self):
		if get_estimate is None:
			self.skipTest("ism.api.estimation_service not yet implemented (Milestone M2)")
```

### 1.3 Baseline Test Runner Execution Output
Running the test suite against the live bench devcontainer via:
```powershell
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math
```
Produced verbatim output:
```
Starting test run with parameters: site=development.localhost, app=ism, module=ism.tests.test_estimation_math, verbose=False, tests=(), force=False, profile=False, failfast=False, skip_before_tests=False, debug=False, selected_categories=[]
View detailed logs (using --verbose): /workspace/development/frappe-bench/logs/frappe.testing.log

Running 41 unspecified-category tests for ism

ism.tests.test_estimation_math.TestEstimationMath
    =  test_tier1_f1_p80_120_minutes
    =  test_tier1_f1_p80_15_minutes
    =  test_tier1_f1_p80_30_minutes
    =  test_tier1_f1_p80_60_minutes_baseline
    =  test_tier1_f1_p80_90_minutes
    =  test_tier1_f2_fw_sum_dict_input
    =  test_tier1_f2_fw_sum_diverse_durations
    =  test_tier1_f2_fw_sum_heterogeneous_sigmas
    =  test_tier1_f2_fw_sum_identical_triplet
    =  test_tier1_f2_fw_sum_two_large_ops
    =  test_tier1_f3_brake_sigma
    =  test_tier1_f3_default_and_none_sigma
    =  test_tier1_f3_electrical_sigma
    =  test_tier1_f3_suspension_sigma
    =  test_tier1_f3_tire_sigma
    =  test_tier1_f4_z_score_p50
    =  test_tier1_f4_z_score_p80
    =  test_tier1_f4_z_score_p90
    =  test_tier1_f4_z_score_p95
    =  test_tier1_f4_z_score_p99
    =  test_tier1_f5_max_buffer_setting
    =  test_tier1_f5_multi_item_estimate_with_buffer
    =  test_tier1_f5_single_item_estimate_with_buffer
    =  test_tier1_f5_whitelisted_json_payload
    =  test_tier1_f5_zero_buffer_setting
    =  test_tier2_f1_extreme_percentile_boundaries
    =  test_tier2_f1_extreme_sigma_boundaries
    =  test_tier2_f1_large_frt_overflow_protection
    =  test_tier2_f1_very_small_frt_clamps_to_min_1
    =  test_tier2_f1_zero_and_negative_frt_raises
    =  test_tier2_f2_empty_list_returns_zero
    =  test_tier2_f2_large_operation_count
    =  test_tier2_f2_list_with_invalid_frt_raises
    =  test_tier2_f2_single_item_matches_estimate_duration
    =  test_tier2_f3_case_insensitive_category
    =  test_tier2_f4_z_score_monotonicity
    =  test_tier2_f5_missing_item_fallback_frt
    =  test_tier2_f5_skip_missing_items
    =  test_tier3_dynamic_settings_recalculation
    =  test_tier3_fw_sum_with_category_sigmas_and_buffer
    =  test_tier4_scenario_full_service_package_estimation

----------------------------------------------------------------------
Ran 41 tests in 0.693s

OK (skipped=41)
```

Running the proxy module `ism.tests.test_estimation` yielded an identical result: `Ran 41 tests in 0.651s. OK (skipped=41)`.

### 1.4 Test Catalog & Exact Assertions

The 41 tests map across features and tiers as follows:

| Test Identifier | Guard | Target Feature | Inputs / Conditions | Expected Assertions |
|:---|:---|:---|:---|:---|
| `test_tier1_f1_p80_60_minutes_baseline` (line 79) | `_require_math` | F1 | `FRT=60.0, sigma=0.30, percentile=0.80` | `res == 78` |
| `test_tier1_f1_p80_30_minutes` (line 85) | `_require_math` | F1 | `FRT=30.0, sigma=0.30, percentile=0.80` | `res == 39` |
| `test_tier1_f1_p80_15_minutes` (line 91) | `_require_math` | F1 | `FRT=15.0, sigma=0.30, percentile=0.80` | `res == 20` |
| `test_tier1_f1_p80_90_minutes` (line 97) | `_require_math` | F1 | `FRT=90.0, sigma=0.30, percentile=0.80` | `res == 116` |
| `test_tier1_f1_p80_120_minutes` (line 103) | `_require_math` | F1 | `FRT=120.0, sigma=0.30, percentile=0.80` | `res == 155` |
| `test_tier1_f2_fw_sum_identical_triplet` (line 110) | `_require_math` | F2 | `[60.0, 60.0, 60.0], sigma=0.30, p=0.80` | `res == 215`, `res < 78 * 3 (234)` |
| `test_tier1_f2_fw_sum_dict_input` (line 117) | `_require_math` | F2 | `[{"flat_rate_minutes": 60.0, "sigma": 0.30}, ...]` | `res == 215` |
| `test_tier1_f2_fw_sum_diverse_durations` (line 128) | `_require_math` | F2 | `[30.0, 60.0, 90.0], sigma=0.30, p=0.80` | `180 < res < 233` (naive sum) |
| `test_tier1_f2_fw_sum_heterogeneous_sigmas` (line 135) | `_require_math` | F2 | `[60m/0.20, 90m/0.35, 45m/0.50]` | `res > (60 + 90 + 45 = 195)` |
| `test_tier1_f2_fw_sum_two_large_ops` (line 146) | `_require_math` | F2 | `[120.0, 120.0], sigma=0.25, p=0.80` | `240 < res < 310` |
| `test_tier1_f3_brake_sigma` (line 153) | `_require_math` | F3 | `item_group="Brake"` | `res == 0.20` |
| `test_tier1_f3_tire_sigma` (line 158) | `_require_math` | F3 | `item_group="Tire"` | `res == 0.15` |
| `test_tier1_f3_suspension_sigma` (line 163) | `_require_math` | F3 | `item_group="Suspension"` | `res == 0.35` |
| `test_tier1_f3_electrical_sigma` (line 168) | `_require_math` | F3 | `item_group="Electrical"` | `res == 0.50` |
| `test_tier1_f3_default_and_none_sigma` (line 173) | `_require_math` | F3 | `None`, `"General Maintenance"`, `"UnknownCategory"` | All equal `0.30` |
| `test_tier1_f4_z_score_p50` (line 181) | `_require_math` | F4 | `p=0.50` | `abs(z - 0.0) < 1e-4` |
| `test_tier1_f4_z_score_p80` (line 187) | `_require_math` | F4 | `p=0.80` | `abs(z - 0.84162) < 1e-3` |
| `test_tier1_f4_z_score_p90` (line 193) | `_require_math` | F4 | `p=0.90` | `abs(z - 1.28155) < 1e-3` |
| `test_tier1_f4_z_score_p95` (line 199) | `_require_math` | F4 | `p=0.95` | `abs(z - 1.64485) < 1e-3` |
| `test_tier1_f4_z_score_p99` (line 205) | `_require_math` | F4 | `p=0.99` | `abs(z - 2.32635) < 1e-2` |
| `test_tier1_f5_single_item_estimate_with_buffer` (line 212) | `_require_service` | F5 | `item=item_brake1 (60m, 0.20), buffer=10, p=0.80` | `p_pure==71, buffer_padding==10, duration_minutes==81` |
| `test_tier1_f5_multi_item_estimate_with_buffer` (line 225) | `_require_service` | F5 | `items=[item_brake1, item_brake2], buffer=15` | `buffer_padding==15, total_duration_minutes == p_pure + 15` |
| `test_tier1_f5_zero_buffer_setting` (line 236) | `_require_service` | F5 | `item=item_brake1, buffer=0` | `p_pure == 71` |
| `test_tier1_f5_max_buffer_setting` (line 244) | `_require_service` | F5 | `item=item_brake1, buffer=30` | `duration_minutes == 71 + 30 == 101` |
| `test_tier1_f5_whitelisted_json_payload` (line 252) | `_require_service` | F5 | `json.dumps([item_brake1, item_tire])` | `res is not None` |
| `test_tier2_f1_zero_and_negative_frt_raises` (line 263) | `_require_math` | F1 Boundary | `FRT=0.0`, `FRT=-15.0` | `raises ValueError` |
| `test_tier2_f1_extreme_percentile_boundaries` (line 271) | `_require_math` | F1 Boundary | `p=0.49`, `p=1.0` | `raises ValueError` |
| `test_tier2_f1_extreme_sigma_boundaries` (line 279) | `_require_math` | F1 Boundary | `sigma=0.0`, `sigma=-0.2` | `raises ValueError` |
| `test_tier2_f1_very_small_frt_clamps_to_min_1` (line 287) | `_require_math` | F1 Boundary | `FRT=0.01, sigma=0.10, p=0.80` | `res >= 1` |
| `test_tier2_f1_large_frt_overflow_protection` (line 293) | `_require_math` | F1 Boundary | `FRT=6000.0 (100 hrs), sigma=0.30, p=0.80` | `6000 < res < 12000` |
| `test_tier2_f2_empty_list_returns_zero` (line 300) | `_require_math` | F2 Boundary | `[]` (empty list) | `res == 0` |
| `test_tier2_f2_single_item_matches_estimate_duration` (line 305) | `_require_math` | F2 Boundary | `[60.0]`, `[{"flat_rate_minutes": 60.0, "sigma": 0.30}]` | `res == 78` (matches `estimate_duration` exactly) |
| `test_tier2_f2_list_with_invalid_frt_raises` (line 311) | `_require_math` | F2 Boundary | `[60.0, 0.0]`, `[-10.0, 30.0]` | `raises ValueError` |
| `test_tier2_f2_large_operation_count` (line 319) | `_require_math` | F2 Boundary | `[15.0] * 20` (20 sequential ops, 300m total) | `300 < res < 400` |
| `test_tier2_f3_case_insensitive_category` (line 327) | `_require_math` | F3 Boundary | `"brake"`, `"  TIRE  "`, `"Electrical"` | `0.20`, `0.15`, `0.50` |
| `test_tier2_f4_z_score_monotonicity` (line 334) | `_require_math` | F4 Boundary | `[0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99]` | `z[i] < z[i+1]` strictly monotonic |
| `test_tier2_f5_missing_item_fallback_frt` (line 342) | `_require_service` | F5 Boundary | `"NON_EXISTENT_PART_XYZ", fallback_frt=45.0` | `duration > 0` |
| `test_tier2_f5_skip_missing_items` (line 350) | `_require_service` | F5 Boundary | `[item_brake1, "NON_EXISTENT_XYZ"], skip_missing=True` | `res_skip == res_single` |
| `test_tier3_fw_sum_with_category_sigmas_and_buffer` (line 363) | `_require_service` | Tier 3 Combo | `[item_brake1, item_elec, item_tire], buffer=15, p=0.85` | `buffer_padding==15, operations_count==3, total > 200` |
| `test_tier3_dynamic_settings_recalculation` (line 373) | `_require_service` | Tier 3 Combo | Mutate `buffer=5, p=0.50` then `buffer=25, p=0.95` | `dur_p95 > dur_p50` |
| `test_tier4_scenario_full_service_package_estimation` (line 390) | `_require_service` | Tier 4 Scenario | Full package: brake1, brake2, elec, tire; buffer=15, p=0.80 | `buffer==15, total == pure + buffer, 300 < pure < 380` |

---

## 2. Logic Chain

1. **Test Suite Independence & Integrity**:
   - `ism/tests/test_estimation_math.py` contains 41 comprehensive tests that exercise pure Python math routines and Frappe whitelisted service wrappers.
   - `ism/tests/test_estimation.py` is a 5-line proxy importing `TestEstimationMath`. It introduces zero new tests, ensuring 100% backward compatibility for legacy bench test commands.

2. **Mechanism of Skip Triggers**:
   - The test module executes `try ... from ism.scheduling.estimation_light import ... except ImportError:` and `try ... from ism.api.estimation_service import ... except ImportError:`.
   - In absence of `ism/scheduling/estimation_light.py`, all symbols are set to `None`.
   - When any test executes `self._require_math()`, `estimate_duration is None` triggers `self.skipTest(...)` for 26 tests.
   - When any test executes `self._require_service()`, `get_estimate is None` triggers `self.skipTest(...)` for 15 tests.
   - Therefore, creating the target implementation files with valid exported symbols will directly unskip all 41 tests without requiring test file modifications.

3. **Pure Math Implementation Requirements (`estimation_light.py`)**:
   - Target path: `d:\ism\ism\scheduling\estimation_light.py` (and package file `d:\ism\ism\scheduling\__init__.py`).
   - Must export 4 symbols:
     - `approximate_z_score(percentile: float) -> float`
     - `get_sigma_for_item(item_group: Optional[str] = None) -> float`
     - `estimate_duration(flat_rate_minutes: float = None, sigma: float = DEFAULT_SIGMA, percentile: float = 0.80, **kwargs) -> int`
     - `estimate_total_duration(flat_rate_list: List[Union[float, int, Dict]], sigma: float = DEFAULT_SIGMA, percentile: float = 0.80) -> int`
   - Abramowitz & Stegun (26.2.23):
     $$t = \sqrt{-2 \ln(1 - p)}$$
     $$z = t - \frac{2.515517 + 0.802853 t + 0.010328 t^2}{1 + 1.432788 t + 0.189269 t^2 + 0.001308 t^3}$$
     For $p = 0.50$, return `0.0` directly.
     For $p < 0.50$ or $p \ge 1.0$, raise `ValueError`.
   - Log-Normal Duration:
     $$\mu = \ln(FRT)$$
     $$duration = \exp(\mu + \sigma \cdot z) = FRT \cdot \exp(\sigma \cdot z)$$
     Return `max(1, math.ceil(duration))`.
     If $FRT \le 0$, raise `ValueError`. If $\sigma \le 0$ or $\sigma > 2.0$, raise `ValueError`.
   - Fenton-Wilkinson Summation:
     For each operation $i$:
     $$E[X_i] = \exp(\mu_i + \sigma_i^2 / 2)$$
     $$Var[X_i] = (\exp(\sigma_i^2) - 1) \exp(2\mu_i + \sigma_i^2)$$
     Fit single log-normal:
     $$\sigma_{sum}^2 = \ln(1 + \sum Var[X_i] / (\sum E[X_i])^2)$$
     $$\mu_{sum} = \ln(\sum E[X_i]) - \sigma_{sum}^2 / 2$$
     Quantile: $\exp(\mu_{sum} + \sigma_{sum} \cdot z)$.
     Return `max(1, math.ceil(duration))`.
     If list is empty, return `0`.
     If list has 1 item, bypass summation and return `estimate_duration(...)`.
     If any item has $FRT \le 0$, raise `ValueError`.
   - Category Profiles (`get_sigma_for_item`):
     Mapping: `{"Brake": 0.20, "Tire": 0.15, "Suspension": 0.35, "Electrical": 0.50, "Body": 0.40, "HVAC": 0.35, "Drivetrain": 0.40}`.
     Default: `0.30`.
     Must strip whitespace and compare case-insensitively (`item_group.strip().lower()`).

4. **Service Layer Requirements (`estimation_service.py`)**:
   - Target path: `d:\ism\ism\api\estimation_service.py`.
   - Must export two whitelisted functions:
     - `@frappe.whitelist() def get_estimate(item_code: str, **kwargs) -> Dict[str, Any]`
     - `@frappe.whitelist() def get_total_estimate(item_codes: Union[str, List[Union[str, Dict[str, Any]]]], **kwargs) -> Dict[str, Any]`
   - Return contracts:
     - `get_estimate` must return dictionary:
       `{"duration_minutes": int, "p_pure": int, "buffer_padding": int, "percentile": float, "sigma": float}`
     - `get_total_estimate` must return dictionary:
       `{"total_duration_minutes": int, "p_pure": int, "buffer_padding": int, "operations_count": int}`
   - Dynamic Settings Reading & Percentage Normalization:
     - `ISM Settings` stores `estimation_percentile` with default `80.0` (as a percentage), but tests also pass `0.80`, `0.85`, `0.50`, `0.95`.
     - Logic must normalize: `p = p / 100.0 if p > 1.0 else p`.
     - Buffer padding is read from `ISM Settings.buffer_padding_minutes` (default 0).
     - Standard diagnostic duration is read from `ISM Settings.default_diagnostic_duration` (default 60).
   - Item FRT Resolution:
     - FRT is stored on `Item.custom_frt` in hours (e.g., `1.0` hr = 60.0 min).
     - Must convert: `frt_minutes = float(custom_frt) * 60.0`.
     - If Item has no `custom_frt` or does not exist, use `kwargs.get("fallback_frt", 60.0)`.
     - If `kwargs.get("skip_missing")` is True, omit items without valid FRT rather than using fallback.
     - `item_codes` input can be a JSON string (e.g. from client REST call) or a Python list. Must parse JSON strings via `frappe.parse_json(item_codes)`.

---

## 3. Caveats

1. **WSL Bench Sync**: Modifying code in `d:\ism` on Windows does not automatically propagate to the WSL devcontainer until `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1` is executed. The Worker must run this sync script before running `bench run-tests`.
2. **Percentile Unit Discrepancy**: Frappe DocType `ISM Settings` defines `estimation_percentile` with fieldtype `Percent` (default `80.0`). In Python code and math calculations, it is used as a decimal `0.80`. Code must support both representation ranges ($0.50 \le p \le 0.99$ and $50.0 \le p \le 99.0$).
3. **Pure vs Total Keys**: `get_estimate` returns `duration_minutes` as the key for total buffered duration, whereas `get_total_estimate` returns `total_duration_minutes`. Both return `p_pure` and `buffer_padding`. The Worker must strictly follow these exact dictionary keys as verified by `test_tier1_f5_*`.

---

## 4. Conclusion

1. The Milestone 2 test suite is 100% complete, fully transactional, and ready for verification against the pure math core and estimation service.
2. Implementing the two required modules:
   - `ism/scheduling/__init__.py` + `ism/scheduling/estimation_light.py`
   - `ism/api/estimation_service.py`
   will unskip all 41 test cases in `ism.tests.test_estimation_math` (and its proxy `ism.tests.test_estimation`).
3. All mathematical constants, formulas, category sigmas, error checks, and dictionary return formats have been identified with exact line numbers and assertions.

---

## 5. Verification Method

### 5.1 Pre-Implementation Verification (Baseline Confirmation)
Execute the current test suite to confirm that 41 tests are discovered and cleanly skipped:
```powershell
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math
```
*Expected*: `Ran 41 tests ... OK (skipped=41)` with 0 failures and 0 errors.

### 5.2 Post-Implementation Verification for Worker
After creating `ism/scheduling/estimation_light.py` and `ism/api/estimation_service.py`:

1. **Synchronize Workspace to WSL**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
   ```

2. **Execute Math Test Suite**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math
   ```
   *Success Criteria*: `Ran 41 tests ... OK` with **0 skipped, 0 failures, 0 errors**.

3. **Execute Backward-Compatible Proxy Suite**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation
   ```
   *Success Criteria*: `Ran 41 tests ... OK` with **0 skipped, 0 failures, 0 errors**.

4. **Verify No Regression on Existing App Suites**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_parts_management
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_tesla_integration
   ```
   *Success Criteria*: All 30 parts management tests and 7 Tesla integration tests continue to pass.
