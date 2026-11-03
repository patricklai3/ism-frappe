# Milestone 2 Gate Review & Adversarial Challenge Report

**Reviewer**: `m2_gate_reviewer_2_gen6`  
**Role**: Reviewer & Adversarial Critic (`teamwork_preview_reviewer`)  
**Parent Orchestrator**: `60694e2b-2673-4a73-bfb2-434f75ef28e2`  
**Date**: 2026-10-27T05:17:00Z  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW**

---

## 1. Observation

### 1.1 Direct File Observations
1. **`ism/api/estimation_service.py`**:
   - Lines 100–121, 225–247: Whitelisted endpoints `@frappe.whitelist() def get_estimate(item_code=None, **kwargs)` and `@frappe.whitelist() def get_total_estimate(item_codes, **kwargs)`.
   - Lines 17–24: `_get_ism_settings()` calls `frappe.get_single("ISM Settings")` on each invocation without any module-level global caching variable.
   - Lines 27–39: `_get_setting_percentile()` checks $p > 1.0 \implies p / 100.0$ and bounds $0.50 \le p \le 0.999$, defaulting to 0.80.
   - Lines 77–98: `_get_frt(item_code)` fetches `custom_frt` from `Item` DocType and multiplies by 60.0 (`return val * 60.0`).
   - Lines 108–109, 172–181: In `get_estimate`, explicit `custom_frt` or `flat_rate_hours` kwargs are converted from hours to minutes ($frt \times 60.0$).
   - Lines 326–345: In `get_total_estimate`, dictionary items with `flat_rate_hours`, `frt_hours`, `custom_frt`, or `qty` are converted to minutes via $\times 60.0$.
   - Lines 205–222, 384–399: Return schemas strictly adhere to `PROJECT.md § Interface Contract 2`:
     - `get_estimate` returns `duration_minutes`, `p_pure`, `buffer_padding`, `percentile`, `sigma`.
     - `get_total_estimate` returns `total_duration_minutes`, `p_pure`, `buffer_padding`, `operations_count`.

2. **`ism/setup.py`**:
   - Lines 478–482: `custom_frt` defined on `Item` DocType:
     ```python
     {
         "fieldname": "custom_frt",
         "label": "Flat Rate Time (Hours)",
         "fieldtype": "Float",
         "insert_after": "revision",
     }
     ```
   - Lines 680–705: Hooked into `after_migrate()` via `setup_item_custom_fields()`, which is triggered on migration and installation.

3. **`ism/scheduling/estimation_light.py`**:
   - Lines 8–15: Standard normal distribution quantiles table `_Z_SCORES` for common percentiles (0.50, 0.75, 0.80, 0.85, 0.90, 0.95).
   - Lines 50–86: Pure-Python rational approximation `approximate_z_score(percentile)` implementing Abramowitz & Stegun Formula 26.2.23 ($|\text{error}| < 4.5 \times 10^{-4}$), completely free of `scipy` and `numpy`.
   - Lines 22–34, 36–47: Category variance mapping `SIGMA_OVERRIDES` (Brake: 0.20, Tire: 0.15, Suspension: 0.35, Electrical: 0.50, Diagnostic: 0.40, Body: 0.30, HVAC: 0.25, Drivetrain: 0.40, Default: 0.30) with case-insensitive whitespace-stripping matching in `get_sigma_for_item()`.
   - Lines 88–147: `estimate_duration()` implements single-operation log-normal duration: $\exp(\ln(\text{FRT}) + \sigma \cdot z)$, ceiling integer, clamped to minimum 1.
   - Lines 149–243: `estimate_total_duration()` implements multi-operation Fenton-Wilkinson composite log-normal moment matching. Single operation directly bypasses summation to match `estimate_duration` exactly.

4. **`ism/tests/test_estimation.py`**:
   - Lines 4–5: Backward-compatible proxy re-exporting `TestEstimationMath` from `ism.tests.test_estimation_math`.

### 1.2 Independent Test Execution Commands and Results

1. **Host-to-WSL Sync**:
   - Command: `powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1`
   - Exit code: 0 (`[sync-to-wsl] Complete.`)

2. **Estimation Math Test Suite (`test_estimation_math`)**:
   - Command: `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math`
   - Result:
     ```
     Running 41 unspecified-category tests for ism
     ism.tests.test_estimation_math.TestEstimationMath
         ✔ 41 tests passed
     Ran 41 tests in 0.923s
     OK
     ```
   - Exit code: 0 (0 skipped, 0 failures, 0 errors).

3. **Backward-Compatible Proxy Suite (`test_estimation`)**:
   - Command: `wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation`
   - Result:
     ```
     Running 41 unspecified-category tests for ism
     ism.tests.test_estimation_math.TestEstimationMath
         ✔ 41 tests passed
     Ran 41 tests in 0.793s
     OK
     ```
   - Exit code: 0 (0 skipped, 0 failures, 0 errors).

4. **Regression Verification Suites**:
   - `test_parts_management`: `Ran 30 tests in 9.353s. OK` (Exit code: 0)
   - `test_tesla_integration`: `Ran 7 tests in 3.889s. OK` (Exit code: 0)

5. **Database Cleanliness Attestation**:
   - Command:
     ```powershell
     wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); print('SE count:', frappe.db.count('Schedule Entry')); non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; print('Vehicles:', len(non_seed)); assert frappe.db.count('Schedule Entry') == 0; assert len(non_seed) == 0; print('DATABASE CLEAN')"
     ```
   - Result:
     ```
     SE count: 0
     Vehicles: 0
     DATABASE CLEAN
     ```
   - Exit code: 0.

---

## 2. Integrity Verification

As an adversarial reviewer, a rigorous audit was conducted for integrity violations:
1. **No Hardcoded Test Results**: Audited `estimation_light.py` and `estimation_service.py` for conditional checks matching specific test values (e.g. `if frt == 60: return 78`). None exist; all outputs are dynamically computed through continuous log-normal distribution equations.
2. **No Facade or Dummy Implementations**: Verified that Fenton-Wilkinson moment matching executes genuine mathematical operations ($\mu_{\text{sum}}, \sigma_{\text{sum}}^2, z$-scores).
3. **No Shortcuts or Cheating**: Abramowitz & Stegun rational approximation is built from scratch without external dependencies (`scipy`, `numpy`), maintaining a lightweight deployment footprint.
4. **Independent Verification**: All test runs, column existence checks, and database cleanliness asserts were executed directly inside the WSL2 devcontainer, not trusting prior claims.
5. **No Evidence of Integrity Violations**.

---

## 3. Quality Review

### Review Summary
**Verdict**: **APPROVE**  
The Milestone 2 implementation strictly conforms to `PROJECT.md § Interface Contract 2`, features 1–5, Frappe architecture standards, and data model expectations.

### Findings

#### [Minor] Finding 1: Type Validation at Trust Boundary (ORM Type Confusion Defense)
- **What**: In `_get_frt(item_code)` and `get_estimate()`, the parameter `item_code` is not strictly checked for `isinstance(item_code, str)` before passing to `frappe.db.exists("Item", item_code)` and `frappe.db.get_value("Item", item_code, ...)`.
- **Where**: `ism/api/estimation_service.py:87`, `198`, `359`, `370`.
- **Why**: Frappe REST endpoints accept JSON objects. Passing a dictionary payload like `{"item_code": {"custom_frt": [">", 0]}}` causes Frappe's ORM to evaluate the dict as a SQL filter query rather than looking up an exact item key. In testing, this resolved an arbitrary item from the database.
- **Suggestion**: Add defensive type checks:
  ```python
  if not item_code or not isinstance(item_code, str):
      return fallback
  ```

#### [Minor] Finding 2: Out-of-Range Kwargs Percentile Handling
- **What**: While `_get_setting_percentile` validates that $0.50 \le p \le 0.999$, `kwargs.get("percentile")` in `get_estimate` and `get_total_estimate` does not clamp or validate lower/upper bounds before passing to `estimate_duration`.
- **Where**: `ism/api/estimation_service.py:125–135`, `251–261`.
- **Why**: Passing an out-of-range percentile (e.g. `percentile=0.20` or `percentile=1.50`) causes `estimate_duration` to raise an unhandled `ValueError`, which surfaces as an HTTP 500 error in the desk UI.
- **Suggestion**: Fall back to the configured setting percentile or raise `frappe.ValidationError` if $p < 0.50$ or $p \ge 1.0$.

### Verified Claims
- `@frappe.whitelist() def get_estimate` and `@frappe.whitelist() def get_total_estimate` present and callable → verified via devcontainer python execution → PASS.
- Dynamic `ISM Settings` querying without module caching → verified via `test_tier3_dynamic_settings_recalculation` → PASS.
- Unit conversion ($frt \times 60.0$) from `custom_frt` in hours to minutes → verified via `_get_frt` inspection and `test_tier1_f5_single_item_estimate_with_buffer` → PASS.
- Percentile normalization ($p > 1.0 \implies p / 100.0$) → verified via `_get_setting_percentile` and test executions → PASS.
- Schema definition in `setup.py` for `custom_frt` on `Item` (`Float`, `"Flat Rate Time (Hours)"`) → verified in `setup.py:478-482` and database column check `frappe.db.has_column("Item", "custom_frt") == True` → PASS.
- Backward-compatible proxy `ism/tests/test_estimation.py` → verified via `bench run-tests --module ism.tests.test_estimation` (41 passed) → PASS.

### Coverage Gaps
- None for Milestone 2 scope. All 5 features (Features 1–5) have complete 4-tier test coverage.

### Unverified Items
- None.

---

## 4. Adversarial Review & Stress-Testing

### Challenge Summary
**Overall Risk Assessment**: **LOW**

### Challenges

#### [Low] Challenge 1: Extreme Percentile Boundaries
- **Assumption challenged**: Callers supply percentiles within standard shop bounds (0.50 to 0.99).
- **Attack scenario**: An API client or integration passes an out-of-bounds percentile (e.g., $p = 0.20$ or $p = 100.0$).
- **Blast radius**: `estimate_duration` raises `ValueError`, resulting in an unhandled 500 response from the API.
- **Mitigation**: Add bounds validation in `_get_estimate` / `_get_total_estimate` to clamp or fall back gracefully.

#### [Low] Challenge 2: Type Confusion on Item Lookups
- **Assumption challenged**: `item_code` is always a string.
- **Attack scenario**: An adversarial REST caller passes `{"item_code": {"name": ["!=", ""]}}`.
- **Blast radius**: ORM filter evaluation selects the first matching record in `Item` table rather than failing safely.
- **Mitigation**: Add `isinstance(item_code, str)` guard at the service boundary.

### Stress Test Results
1. **Type confusion attack (`item_code={'custom_frt': ['>', 0]}`)**:
   - Result: Matched first Item in DB and fetched its group (`sigma=0.50`). Executed without crash, but returned unintended item's parameters. Flagged as Finding 1.
2. **Out-of-range percentile ($p = 0.20$)**:
   - Result: Raised `ValueError: Percentile must be between 0.50 and 0.999..., got 0.2`. Flagged as Finding 2.
3. **Empty item list in `get_total_estimate([])`**:
   - Result: Returned `total_duration_minutes=0, p_pure=0, operations_count=0` cleanly without error.
4. **Large commercial jobs ($6000$ minutes FRT)**:
   - Result: Computed smoothly without floating-point overflow (`res = 7763`).
5. **Dynamic Settings Mutation**:
   - Result: Changing percentile from 0.50 to 0.95 immediately changed estimate from 60 to 88 without server restart.

### Unchallenged Areas
- Milestones 3–5 features (scheduling availability APIs, calendar UI, Repair Order lifecycle transitions) are planned for subsequent milestones.

---

## 5. Logic Chain

1. **Requirement Conformance**:
   - The primary target files `ism/api/estimation_service.py`, `ism/setup.py`, and `ism/scheduling/estimation_light.py` fulfill every requirement of Milestone 2 and `PROJECT.md § Interface Contract 2`.
   - Dynamic settings querying without caching was verified directly in source code and proven by `test_tier3_dynamic_settings_recalculation`.
   - Unit conversion of `Item.custom_frt` from hours to minutes is implemented consistently across both single and multi-operation workflows.

2. **Test & Execution Evidence**:
   - Both the primary runner command `bench run-tests --module ism.tests.test_estimation_math` and the proxy command `bench run-tests --module ism.tests.test_estimation` execute 41 tests with 100% pass rate and 0 skips.
   - Regression suites for parts management (30 tests) and Tesla integration (7 tests) pass with zero errors.
   - Database hygiene check confirms 0 residual records.

3. **Integrity and Security**:
   - No mock facades or hardcoded values are present. The math is mathematically sound and derived from first principles.
   - The findings identified during adversarial review are minor defense-in-depth suggestions that do not impede Milestone 2 sign-off.

---

## 6. Caveats

- **No Caveats**: All required files, functions, schema definitions, unit conversions, and devcontainer test commands were directly inspected, stress-tested, and verified.

---

## 7. Conclusion

Milestone 2 (Tunable Estimation Engine & Pure Math Core) is of high engineering quality, passes all 41 test cases, introduces zero regressions, and strictly satisfies Interface Contract 2.

**Verdict: APPROVE**

---

## 8. Verification Method

To independently reproduce this verification:

1. **Execute Math Test Suite**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math
   ```
   *Expected: Ran 41 tests ... OK*

2. **Execute Backward-Compatible Proxy Suite**:
   ```powershell
   wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation
   ```
   *Expected: Ran 41 tests ... OK*

3. **Verify Database Hygiene**:
   ```powershell
   wsl docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 ../env/bin/python -c "import frappe; frappe.init('development.localhost'); frappe.connect(); assert frappe.db.count('Schedule Entry') == 0; non_seed = [v for v in frappe.get_all('Repair Vehicle', pluck='name') if v not in ('5YJSA1E26CF000001', 'LRW3E1FA9PC445566')]; assert len(non_seed) == 0; print('DATABASE CLEAN')"
   ```
   *Expected: Output `DATABASE CLEAN` with exit code 0.*
