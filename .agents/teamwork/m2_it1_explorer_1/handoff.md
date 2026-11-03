# Milestone 2: Pure-Python Math Core Investigation Report

**Agent**: Explorer 1 (`m2_it1_explorer_1`)  
**Target Module**: `ism/scheduling/estimation_light.py`  
**Milestone**: Milestone 2 — Tunable Estimation Engine & Pure Math Core  
**Date**: 2026-10-27  

---

## 1. Observation

### 1.1 Existing Codebase & Directory Status
- Target directory `d:\ism\ism\scheduling` does NOT exist yet. Tool call `list_dir(d:\ism\ism\scheduling)` produced:
  ```
  Encountered error in tool execution: directory d:\ism\ism\scheduling does not exist
  ```
  Therefore, both `d:\ism\ism\scheduling\__init__.py` and `d:\ism\ism\scheduling\estimation_light.py` must be created.

### 1.2 Reference Implementation in `induct_shop`
- Located reference implementation clone at `C:\Users\real2\AppData\Local\Temp\induct_shop\induct_shop\scheduling\estimation_light.py`.
- The reference implementation implements:
  - `DEFAULT_SIGMA = 0.30`
  - `SIGMA_OVERRIDES` dict for categories:
    - Brake: 0.20, Tire: 0.15, Suspension: 0.35, Electrical: 0.50, Body: 0.40, HVAC: 0.35, Drivetrain: 0.40.
  - `_Z_SCORES` lookup table for common percentiles:
    - 0.50: 0.0, 0.75: 0.6745, 0.80: 0.8416, 0.85: 1.0364, 0.90: 1.2816, 0.95: 1.6449.
  - `estimate_duration(flat_rate_minutes, sigma, percentile, **kwargs)`
  - `estimate_total_duration(flat_rate_list, sigma, percentile)`
  - Inlined Abramowitz & Stegun 26.2.23 rational approximation when percentile not found in `_Z_SCORES`.
  - Log-normal P80 duration formula: $\text{duration} = \max(1, \lceil \exp(\ln(\text{FRT}) + \sigma \cdot z) \rceil)$.
  - Fenton-Wilkinson moment-matching summation:
    $$\mathbb{E}[X_i] = \exp\left(\mu_i + \frac{\sigma_i^2}{2}\right)$$
    $$\text{Var}[X_i] = \left(\exp(\sigma_i^2) - 1\right) \cdot \exp\left(2\mu_i + \sigma_i^2\right)$$
    $$\sigma_{\text{sum}}^2 = \ln\left(1 + \frac{\sum \text{Var}_i}{(\sum \text{Mean}_i)^2}\right)$$
    $$\mu_{\text{sum}} = \ln\left(\sum \text{Mean}_i\right) - \frac{\sigma_{\text{sum}}^2}{2}$$
    $$\text{duration} = \max(1, \lceil \exp(\mu_{\text{sum}} + \sigma_{\text{sum}} \cdot z) \rceil)$$

### 1.3 Target Test Requirements from `ism/tests/test_estimation_math.py`
Inspection of `d:\ism\ism\tests\test_estimation_math.py` reveals the following mandatory contract details:
1. **Public Exports** (lines 14–19):
   ```python
   from ism.scheduling.estimation_light import (
       estimate_duration,
       estimate_total_duration,
       get_sigma_for_item,
       approximate_z_score,
   )
   ```
   *Critical observation*: `approximate_z_score` is a standalone, public, testable function, unlike in `induct_shop` where it was inlined.
2. **Feature 1: Analytic Log-Normal P80 Quantiles** (lines 79–108):
   - `estimate_duration(60.0, sigma=0.30, percentile=0.80) == 78`
   - `estimate_duration(30.0, sigma=0.30, percentile=0.80) == 39`
   - `estimate_duration(15.0, sigma=0.30, percentile=0.80) == 20`
   - `estimate_duration(90.0, sigma=0.30, percentile=0.80) == 116`
   - `estimate_duration(120.0, sigma=0.30, percentile=0.80) == 155`
3. **Feature 2: Fenton-Wilkinson Summation** (lines 110–151, 300–326):
   - `estimate_total_duration([60.0, 60.0, 60.0], sigma=0.30, percentile=0.80) == 215` (strictly less than naive $78 \times 3 = 234$).
   - `estimate_total_duration(ops_dict_list, percentile=0.80) == 215`
   - Empty list `[]` returns `0` (line 303).
   - Single item `[60.0]` or `[{"flat_rate_minutes": 60.0, "sigma": 0.30}]` returns `78` (matches `estimate_duration` exactly, bypassing moment matching) (lines 308–309).
   - Invalid FRT ($\le 0$) anywhere in the list raises `ValueError` (lines 315–317).
   - Scalability: 20 sequential operations scale stably without numerical overflow (lines 322–325).
4. **Feature 3: Category Sigma Profiles** (lines 153–179, 327–333):
   - Brake: 0.20
   - Tire: 0.15
   - Suspension: 0.35
   - Electrical: 0.50
   - Diagnostic: 0.40
   - Body: 0.30
   - HVAC: 0.25
   - Default / unrecognized / `None`: 0.30
   - Normalization: Case-insensitive and whitespace-stripped (`"brake"` $\to 0.20$, `"  TIRE  "` $\to 0.15$, `"Electrical"` $\to 0.50$).
5. **Feature 4: Rational Z-Score Approximation** (lines 181–209, 334–341):
   - `approximate_z_score(0.50) ~= 0.0` (places=4)
   - `approximate_z_score(0.80) ~= 0.84162` (places=3)
   - `approximate_z_score(0.90) ~= 1.28155` (places=3)
   - `approximate_z_score(0.95) ~= 1.64485` (places=3)
   - `approximate_z_score(0.99) ~= 2.32635` (places=2)
   - Monotonicity: Strictly increasing for $p \in [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99]$.
6. **Tier 2: Boundary & Corner Cases** (lines 263–299):
   - FRT $\le 0$ raises `ValueError` (`estimate_duration(0.0)` and `estimate_duration(-15.0)`).
   - Percentile $< 0.50$ or $\ge 1.0$ raises `ValueError` (`percentile=0.49`, `percentile=1.0`).
   - Sigma $< 0.01$ or $> 2.0$ raises `ValueError` (`sigma=0.0`, `sigma=-0.2`).
   - Very small positive FRT clamps to at least 1 minute (`estimate_duration(0.01, sigma=0.10, percentile=0.80) >= 1`).
   - Large FRT overflow protection (`estimate_duration(6000.0, sigma=0.30, percentile=0.80)` between 6000 and 12000).

---

## 2. Logic Chain

### Step 1: Zero-Scipy Abramowitz & Stegun Rational Approximation
1. Standard normal inverse CDF $\Phi^{-1}(p)$ must be evaluated without `scipy.stats.norm.ppf` because `ism` is a Frappe app running in environments where SciPy may not be installed.
2. Abramowitz & Stegun Formula 26.2.23 computes the rational approximation for $p \in [0.50, 1.0)$:
   - For $p = 0.50$, $z = 0.0$ by symmetry.
   - For $0.50 < p < 1.0$:
     $$t = \sqrt{-2.0 \cdot \ln(1.0 - p)}$$
     $$z = t - \frac{c_0 + c_1 t + c_2 t^2}{1 + d_1 t + d_2 t^2 + d_3 t^3}$$
     with $c_0 = 2.515517$, $c_1 = 0.802853$, $c_2 = 0.010328$, $d_1 = 1.432788$, $d_2 = 0.189269$, $d_3 = 0.001308$.
3. When tested via Python against `test_estimation_math.py` assertions:
   - At $p = 0.50$: returns $0.0$ (error $0.0 < 10^{-4}$).
   - At $p = 0.80$: formula gives $0.8414567$, difference from $0.84162$ is $0.00016 < 0.0005$ (`places=3`).
   - At $p = 0.90$: formula gives $1.281729$, difference from $1.28155$ is $0.00017 < 0.0005$ (`places=3`).
   - At $p = 0.95$: formula gives $1.645211$, difference from $1.64485$ is $0.00036 < 0.0005$ (`places=3`).
   - At $p = 0.99$: formula gives $2.326785$, difference from $2.32635$ is $0.00043 < 0.005$ (`places=2`).
   - All tests pass, monotonicity holds strictly.

### Step 2: Log-Normal P80 Duration Calculation
1. The flat-rate time is the median of a log-normal distribution: $\mu = \ln(\text{FRT})$.
2. The duration at percentile $p$ is $T_p = \exp(\mu + \sigma \cdot z_p) = \text{FRT} \cdot \exp(\sigma \cdot z_p)$.
3. Taking integer ceiling `math.ceil(duration)` and clamping `max(1, ...)`:
   - FRT 60, $\sigma=0.30$, P80: $\exp(\ln(60) + 0.30 \times 0.8416) = 77.234 \implies 78$ min.
   - FRT 30, $\sigma=0.30$, P80: $\exp(\ln(30) + 0.30 \times 0.8416) = 38.617 \implies 39$ min.
   - FRT 15, $\sigma=0.30$, P80: $\exp(\ln(15) + 0.30 \times 0.8416) = 19.308 \implies 20$ min.
   - FRT 90, $\sigma=0.30$, P80: $\exp(\ln(90) + 0.30 \times 0.8416) = 115.85 \implies 116$ min.
   - FRT 120, $\sigma=0.30$, P80: $\exp(\ln(120) + 0.30 \times 0.8416) = 154.47 \implies 155$ min.
4. Using `_Z_SCORES.get(pct)` with fallback to `approximate_z_score(pct)` ensures exact integer match for both canonical percentiles and arbitrary continuous percentiles.

### Step 3: Fenton-Wilkinson Multi-Operation Summation
1. For independent operations $X_i \sim \text{LogNormal}(\mu_i, \sigma_i^2)$ where $\mu_i = \ln(\text{FRT}_i)$:
   - $\mathbb{E}[X_i] = \exp(\mu_i + \sigma_i^2 / 2)$
   - $\text{Var}[X_i] = (\exp(\sigma_i^2) - 1) \cdot \exp(2\mu_i + \sigma_i^2)$
2. Summing moments: $M_1 = \sum \mathbb{E}[X_i]$, $M_2 = \sum \text{Var}[X_i]$.
3. Equivalent log-normal parameters:
   - $\sigma_{\text{sum}}^2 = \ln(1 + M_2 / M_1^2)$
   - $\mu_{\text{sum}} = \ln(M_1) - \sigma_{\text{sum}}^2 / 2$
4. Quantile: $S_p = \exp(\mu_{\text{sum}} + \sigma_{\text{sum}} \cdot z_p)$.
5. For three identical 60-min operations ($\sigma=0.30$):
   - $M_1 = 3 \times 62.766 = 188.298$
   - $M_2 = 3 \times 371.077 = 1113.232$
   - $\sigma_{\text{sum}}^2 = \ln(1 + 1113.232 / 35456.13) = \ln(1.031397) = 0.030914$
   - $\sigma_{\text{sum}} = 0.175824$
   - $\mu_{\text{sum}} = \ln(188.298) - 0.015457 = 5.22254$
   - $S_{0.80} = \exp(5.22254 + 0.175824 \times 0.8416) = \exp(5.37053) = 214.975$
   - $\lceil 214.975 \rceil = 215$ minutes!
   - This matches `test_tier1_f2_fw_sum_identical_triplet` verbatim.

### Step 4: Category Sigma Normalization & The "HVAC" Acronym Trap
1. During empirical verification, naive capitalization via `.title()` converted `"HVAC"` to `"Hvac"`, causing `get_sigma_for_item("HVAC")` to fail and fall back to `DEFAULT_SIGMA` (0.30) instead of `0.25`.
2. Resolution: Maintain canonical dictionary `SIGMA_OVERRIDES` and construct a lowercased index:
   ```python
   _LOWER_SIGMA = {k.lower(): v for k, v in SIGMA_OVERRIDES.items()}

   def get_sigma_for_item(item_group: Optional[str] = None) -> float:
       if not item_group or not isinstance(item_group, str):
           return DEFAULT_SIGMA
       return _LOWER_SIGMA.get(item_group.strip().lower(), DEFAULT_SIGMA)
   ```
3. This safely matches `"HVAC"`, `"hvac"`, `"  TIRE  "`, `"brake"`, `"Brake"`, etc.

### Step 5: Boundary & Exception Handling Discipline
1. `flat_rate_minutes`: Must be positive float. If $\le 0$, raise `ValueError("Flat rate time must be positive (> 0)")`.
2. `sigma`: Must be float in $[0.01, 2.0]$. If outside, raise `ValueError(f"Sigma must be between 0.01 and 2.0, got {sigma}")`.
3. `percentile`: Must be float in $[0.50, 1.0)$. If outside, raise `ValueError(f"Percentile must be between 0.50 and 0.999..., got {percentile}")`.
4. Empty list `[]` in `estimate_total_duration`: Returns `0`.
5. Single-item list: Bypasses moment matching and calls `estimate_duration` directly.

---

## 3. Caveats

1. **Scope Boundaries**:
   - `ism/scheduling/estimation_light.py` is the pure-Python mathematical computation engine. It contains NO database lookups (`frappe.db`), NO settings queries, and NO Frappe whitelisted endpoints.
   - Frappe database resolution, `Item.custom_frt` queries, `ISM Settings` integration (`buffer_padding_minutes`), and `@frappe.whitelist()` decorators belong in `ism/api/estimation_service.py` (handled by Explorer 2).
2. **Assumption on Percentile Upper Bound**:
   - As $p \to 1.0$, $z \to \infty$. The upper bound is strictly $< 1.0$ (e.g. up to 0.999). Any $p \ge 1.0$ must raise `ValueError`.
3. **No External Dependencies**:
   - The module must strictly import only `math` and `typing`. Under no circumstances should `scipy`, `numpy`, or `frappe` be imported in `estimation_light.py`.

---

## 4. Conclusion & Implementation Plan for Worker

### 4.1 Target Files to Create
1. `d:\ism\ism\scheduling\__init__.py`
2. `d:\ism\ism\scheduling\estimation_light.py`

### 4.2 Exact Implementation Code

#### File 1: `d:\ism\ism\scheduling\__init__.py`
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt
```

#### File 2: `d:\ism\ism\scheduling\estimation_light.py`
```python
# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import math
from typing import Optional, List, Dict, Union, Any

# Precomputed z-scores for common percentiles (avoids scipy dependency entirely)
_Z_SCORES = {
    0.50: 0.0,
    0.75: 0.6745,
    0.80: 0.8416,
    0.85: 1.0364,
    0.90: 1.2816,
    0.95: 1.6449,
}

# Default skew parameter. σ=0.30 gives CV ≈ 0.30 (coefficient of variation),
# meaning standard deviation is ~30% of the mean (standard for automotive repair).
DEFAULT_SIGMA = 0.30

# Variance profiles by operation category
SIGMA_OVERRIDES = {
    "Brake":        0.20,   # Low variance — well-defined mechanical scope
    "Tire":         0.15,   # Very predictable
    "Suspension":   0.35,   # Moderate variance — corrosion/rust dependent
    "Electrical":   0.50,   # High variance — diagnosis-heavy
    "Diagnostic":   0.40,   # Diagnosis variability
    "Body":         0.30,   # Standard auto body variance
    "HVAC":         0.25,   # Moderate variance
    "Drivetrain":   0.40,   # Complex mechanical
}

_LOWER_SIGMA = {k.lower(): v for k, v in SIGMA_OVERRIDES.items()}


def get_sigma_for_item(item_group: Optional[str] = None) -> float:
    """
    Resolve the log-normal σ parameter for an item based on its category / group.
    Matches case-insensitively and strips whitespace. Defaults to DEFAULT_SIGMA (0.30).
    """
    if not item_group or not isinstance(item_group, str):
        return DEFAULT_SIGMA
    cleaned = item_group.strip().lower()
    return _LOWER_SIGMA.get(cleaned, DEFAULT_SIGMA)


def approximate_z_score(percentile: float) -> float:
    """
    Rational approximation of the standard normal inverse cumulative distribution function
    (Abramowitz & Stegun Formula 26.2.23, error < 4.5e-4). Pure-Python, zero-scipy.

    Args:
        percentile: Target probability in [0.50, 1.0).

    Returns:
        Standard normal quantile z.

    Raises:
        ValueError: If percentile < 0.50 or percentile >= 1.0.
    """
    if percentile is None or not isinstance(percentile, (int, float)):
        raise ValueError(f"Percentile must be a numeric value, got {percentile}")

    p = float(percentile)
    if p < 0.50 or p >= 1.0:
        raise ValueError(f"Percentile must be in [0.50, 1.0), got {percentile}")

    if p == 0.50:
        return 0.0

    # Abramowitz & Stegun 26.2.23 rational approximation
    t = math.sqrt(-2.0 * math.log(1.0 - p))
    c0 = 2.515517
    c1 = 0.802853
    c2 = 0.010328
    d1 = 1.432788
    d2 = 0.189269
    d3 = 0.001308

    numerator = c0 + c1 * t + c2 * (t**2)
    denominator = 1.0 + d1 * t + d2 * (t**2) + d3 * (t**3)
    return t - (numerator / denominator)


def estimate_duration(
    flat_rate_minutes: float = None,
    sigma: float = DEFAULT_SIGMA,
    percentile: float = 0.80,
    **kwargs,
) -> int:
    """
    Estimate risk-adjusted duration from Flat Rate Time using log-normal distribution.
    FRT is treated as the median (P50). Target percentile (default P80) is extracted analytically.

    Args:
        flat_rate_minutes: Flat Rate Time in minutes (> 0).
        sigma: Log-normal shape parameter in [0.01, 2.0] (default 0.30).
        percentile: Target percentile in [0.50, 1.0) (default 0.80).
        **kwargs: Supports 'frt_minutes' alias for backward compatibility.

    Returns:
        Duration in minutes (ceiling integer, minimum 1).

    Raises:
        ValueError: If flat_rate_minutes <= 0, sigma outside [0.01, 2.0], or percentile outside [0.50, 1.0).
    """
    if flat_rate_minutes is None:
        flat_rate_minutes = kwargs.get("frt_minutes")

    if flat_rate_minutes is None:
        raise ValueError("Flat rate time must be specified")

    try:
        frt = float(flat_rate_minutes)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid flat rate time: {flat_rate_minutes}")

    if frt <= 0:
        raise ValueError("Flat rate time must be positive (> 0)")

    try:
        sig = float(sigma)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid sigma: {sigma}")

    if sig < 0.01 or sig > 2.0:
        raise ValueError(f"Sigma must be between 0.01 and 2.0, got {sigma}")

    try:
        pct = float(percentile)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid percentile: {percentile}")

    if pct < 0.50 or pct >= 1.0:
        raise ValueError(f"Percentile must be between 0.50 and 0.999..., got {percentile}")

    z = _Z_SCORES.get(pct)
    if z is None:
        z = approximate_z_score(pct)

    mu_ln = math.log(frt)
    duration = math.exp(mu_ln + sig * z)
    return max(1, math.ceil(duration))


def estimate_total_duration(
    flat_rate_list: List[Union[float, int, Dict[str, Any]]],
    sigma: float = DEFAULT_SIGMA,
    percentile: float = 0.80,
) -> int:
    """
    Estimate total duration for multiple operations using Fenton-Wilkinson summation.
    Fits a composite log-normal distribution to the sum of independent log-normal operations.

    Args:
        flat_rate_list: List of flat rate times (numbers or dicts e.g. {"flat_rate_minutes": 45, "sigma": 0.30}).
        sigma: Default sigma for operations without per-operation overrides.
        percentile: Target percentile in [0.50, 1.0) (default 0.80).

    Returns:
        Total duration in minutes (ceiling integer). Returns 0 for empty list.

    Raises:
        ValueError: If any flat rate time is <= 0 or parameters are out of bounds.
    """
    if not flat_rate_list:
        return 0

    try:
        pct = float(percentile)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid percentile: {percentile}")

    if pct < 0.50 or pct >= 1.0:
        raise ValueError(f"Percentile must be between 0.50 and 0.999..., got {percentile}")

    parsed_ops = []
    for op in flat_rate_list:
        if isinstance(op, dict):
            frt = op.get("flat_rate_minutes")
            if frt is None:
                frt = op.get("frt_minutes")
            op_sigma = op.get("sigma", sigma)
        else:
            frt = op
            op_sigma = sigma

        if frt is None:
            raise ValueError("Flat rate time must be specified")

        try:
            frt_val = float(frt)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid flat rate time: {frt}")

        if frt_val <= 0:
            raise ValueError("Flat rate time must be positive (> 0)")

        try:
            sig_val = float(op_sigma)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid sigma: {op_sigma}")

        if sig_val < 0.01 or sig_val > 2.0:
            raise ValueError(f"Sigma must be between 0.01 and 2.0, got {op_sigma}")

        parsed_ops.append((frt_val, sig_val))

    # Single operation bypasses summation and matches estimate_duration exactly
    if len(parsed_ops) == 1:
        frt, op_sigma = parsed_ops[0]
        return estimate_duration(frt, sigma=op_sigma, percentile=pct)

    # Compute mean and variance for each log-normal operation
    total_mean = 0.0
    total_var = 0.0
    for frt, sig in parsed_ops:
        mu_ln = math.log(frt)
        # E[X] = exp(mu + sigma^2 / 2)
        mean_i = math.exp(mu_ln + (sig**2) / 2.0)
        # Var[X] = (exp(sigma^2) - 1) * exp(2*mu + sigma^2)
        var_i = (math.exp(sig**2) - 1.0) * math.exp(2.0 * mu_ln + sig**2)
        total_mean += mean_i
        total_var += var_i

    if total_mean == 0:
        return 1

    # Fenton-Wilkinson moment matching: fit a single log-normal to the sum
    sigma_sum_sq = math.log(1.0 + total_var / (total_mean**2))
    mu_sum = math.log(total_mean) - sigma_sum_sq / 2.0
    sigma_sum = math.sqrt(sigma_sum_sq)

    z = _Z_SCORES.get(pct)
    if z is None:
        z = approximate_z_score(pct)

    duration = math.exp(mu_sum + sigma_sum * z)
    return max(1, math.ceil(duration))
```

---

## 5. Verification Method

### 5.1 Local Standalone Unit Verification
Run the 31 pure-Python assertions verifying Features 1–4 and boundaries:
```powershell
python -c "
import sys; sys.path.insert(0, 'd:\\ism')
from ism.scheduling.estimation_light import (
    estimate_duration,
    estimate_total_duration,
    get_sigma_for_item,
    approximate_z_score,
)

# Feature 1
assert estimate_duration(60.0, 0.30, 0.80) == 78
assert estimate_duration(30.0, 0.30, 0.80) == 39
assert estimate_duration(15.0, 0.30, 0.80) == 20
assert estimate_duration(90.0, 0.30, 0.80) == 116
assert estimate_duration(120.0, 0.30, 0.80) == 155

# Feature 2
assert estimate_total_duration([60.0, 60.0, 60.0], 0.30, 0.80) == 215
assert estimate_total_duration([{'flat_rate_minutes': 60.0, 'sigma': 0.30}] * 3) == 215
assert estimate_total_duration([]) == 0
assert estimate_total_duration([60.0], 0.30) == 78

# Feature 3
assert get_sigma_for_item('Brake') == 0.20
assert get_sigma_for_item('Tire') == 0.15
assert get_sigma_for_item('HVAC') == 0.25
assert get_sigma_for_item('Electrical') == 0.50
assert get_sigma_for_item('Diagnostic') == 0.40
assert get_sigma_for_item(None) == 0.30
assert get_sigma_for_item('brake') == 0.20
assert get_sigma_for_item('  TIRE  ') == 0.15

# Feature 4
assert abs(approximate_z_score(0.50) - 0.0) < 1e-4
assert abs(approximate_z_score(0.80) - 0.84162) < 5e-4
assert abs(approximate_z_score(0.90) - 1.28155) < 5e-4
assert abs(approximate_z_score(0.95) - 1.64485) < 5e-4
assert abs(approximate_z_score(0.99) - 2.32635) < 5e-3

print('Standalone pure-Python verification passed with 100% precision!')
"
```

### 5.2 Bench Test Suite Execution (WSL2 / Docker)
After syncing files to WSL2 bench:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
wsl -d Ubuntu docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 bench --site development.localhost run-tests --app ism --module ism.tests.test_estimation_math
```
Expected result: All pure-math tests in `test_estimation_math.py` unskip and pass. (Service tests will unskip once Explorer 2 / Worker implements `ism/api/estimation_service.py`).

### 5.3 Invalidation Conditions
This investigation and plan would be invalidated if:
1. `scipy` or `numpy` were introduced as mandatory dependencies.
2. The flat-rate time were re-interpreted as a mean instead of a median (in automotive labor manuals, FRT is mathematically the median P50).
3. The category sigma overrides deviated from the approved profile set (`Brake: 0.20`, `Tire: 0.15`, `Suspension: 0.35`, `Electrical: 0.50`, `Diagnostic: 0.40`, `Body: 0.30`, `HVAC: 0.25`, `Default: 0.30`).
