# Copyright (c) 2026, patricklai3 and contributors
# Adversarial Empirical Stress & Verification Suite for Milestone 2 Gate
# Role: Empirical Challenger (m2_gate_challenger_1_gen6)

import math
import sys
import time
from typing import Any, Dict, List

# Imports from pure-math engine
from ism.scheduling.estimation_light import (
    DEFAULT_SIGMA,
    _Z_SCORES,
    approximate_z_score,
    estimate_duration,
    estimate_total_duration,
    get_sigma_for_item,
)

try:
    from scipy.stats import norm
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


class AdversarialTestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.findings = []

    def record(self, test_name: str, passed: bool, details: str = ""):
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {test_name} {details}")
        if passed:
            self.passed += 1
        else:
            self.failed += 1
            self.findings.append((test_name, details))
        return passed


runner = AdversarialTestRunner()


# ==============================================================================
# SECTION 1: approximate_z_score Precision, Monotonicity & Symmetry
# ==============================================================================

def test_section_1_approximate_z_score():
    print("\n" + "=" * 70)
    print("SECTION 1: approximate_z_score Precision, Monotonicity & Symmetry")
    print("=" * 70)

    # 1.1 Median exactness
    z_50 = approximate_z_score(0.50)
    runner.record(
        "1.1 z_score(0.50) == 0.0",
        z_50 == 0.0,
        f"got {z_50}",
    )

    # 1.2 Monotonicity across valid upper domain [0.50, 0.99]
    upper_probs = [0.50, 0.55, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99, 0.999]
    upper_z = [approximate_z_score(p) for p in upper_probs]
    upper_monotonic = all(upper_z[i] < upper_z[i+1] for i in range(len(upper_z)-1))
    runner.record(
        "1.2 Monotonicity across [0.50, 0.999]",
        upper_monotonic,
        f"evaluated {len(upper_probs)} quantiles",
    )

    # 1.3 Challenge: Monotonicity across percentiles 0.01 to 0.99
    # Does approximate_z_score support lower percentiles p in [0.01, 0.49]?
    lower_supported = True
    try:
        z_lower = [approximate_z_score(p) for p in [0.01, 0.10, 0.25, 0.40]]
    except ValueError as e:
        lower_supported = False
        lower_err = str(e)

    runner.record(
        "1.3 Lower tail support p in [0.01, 0.49] in approximate_z_score",
        lower_supported,
        f"Domain truncated to [0.50, 1.0); lower p raises ValueError: {lower_err}" if not lower_supported else "Supported",
    )

    # 1.4 Challenge: Symmetry Z(p) = -Z(1-p) for p in (0, 1)
    symmetry_passed = True
    symmetry_details = ""
    test_pairs = [(0.80, 0.20), (0.90, 0.10), (0.95, 0.05), (0.99, 0.01)]
    for p_hi, p_lo in test_pairs:
        try:
            z_hi = approximate_z_score(p_hi)
            z_lo = approximate_z_score(p_lo)
            if not math.isclose(z_lo, -z_hi, abs_tol=1e-3):
                symmetry_passed = False
                symmetry_details = f"Z({p_lo})={z_lo} != -Z({p_hi})={-z_hi}"
                break
        except Exception as e:
            symmetry_passed = False
            symmetry_details = f"Failed when evaluating Z({p_lo}): {type(e).__name__} {e}"
            break

    runner.record(
        "1.4 Symmetry Z(p) == -Z(1-p) for p in (0, 1)",
        symmetry_passed,
        symmetry_details,
    )

    # 1.5 Precision vs Theoretical Inverse Normal CDF (Abramowitz & Stegun error bound < 4.5e-4)
    if HAS_SCIPY:
        max_err = 0.0
        test_pcts = [0.5001 + 0.005 * i for i in range(99)]  # 0.5001 to 0.9901
        for p in test_pcts:
            approx = approximate_z_score(p)
            exact = norm.ppf(p)
            err = abs(approx - exact)
            if err > max_err:
                max_err = err
        runner.record(
            "1.5 A&S 26.2.23 Max Error Bound (< 4.5e-4)",
            max_err < 4.5e-4,
            f"observed max error: {max_err:0.6f} across {len(test_pcts)} sample points",
        )
    else:
        # Check against known high-precision reference quantiles
        ref_quantiles = {
            0.50: 0.0,
            0.80: 0.84162123,
            0.90: 1.28155157,
            0.95: 1.64485363,
            0.99: 2.32634787,
        }
        all_within_bound = True
        max_err = 0.0
        for p, exact in ref_quantiles.items():
            approx = approximate_z_score(p)
            err = abs(approx - exact)
            max_err = max(max_err, err)
            if err >= 4.5e-4:
                all_within_bound = False
        runner.record(
            "1.5 A&S 26.2.23 Error at standard quantiles (< 4.5e-4)",
            all_within_bound,
            f"max err: {max_err:0.6f}",
        )

    # 1.6 Extreme boundaries handling for approximate_z_score
    p_one_raises = False
    try:
        approximate_z_score(1.0)
    except ValueError:
        p_one_raises = True

    p_zero_raises = False
    try:
        approximate_z_score(0.0)
    except ValueError:
        p_zero_raises = True

    p_neg_raises = False
    try:
        approximate_z_score(-0.5)
    except ValueError:
        p_neg_raises = True

    p_large_raises = False
    try:
        approximate_z_score(1.5)
    except ValueError:
        p_large_raises = True

    runner.record(
        "1.6 Out-of-bounds inputs raise ValueError in approximate_z_score (0.0, 1.0, -0.5, 1.5)",
        p_one_raises and p_zero_raises and p_neg_raises and p_large_raises,
        f"1.0:{p_one_raises}, 0.0:{p_zero_raises}, -0.5:{p_neg_raises}, 1.5:{p_large_raises}",
    )


# ==============================================================================
# SECTION 2: estimate_duration Boundary Handling & Parameter Robustness
# ==============================================================================

def test_section_2_estimate_duration_boundaries():
    print("\n" + "=" * 70)
    print("SECTION 2: estimate_duration Boundary Handling & Parameter Robustness")
    print("=" * 70)

    # 2.1 Non-positive flat-rate times (0, -1, -100) must raise ValueError
    invalid_frts = [0, 0.0, -1, -1.0, -100, -100.0, -1e-6]
    all_raised = True
    for frt in invalid_frts:
        try:
            estimate_duration(frt)
            all_raised = False
            print(f"  Did NOT raise for FRT={frt}")
        except ValueError:
            pass
    runner.record(
        "2.1 Non-positive FRT (0, -1, -100) raises ValueError",
        all_raised,
        f"tested {len(invalid_frts)} non-positive values",
    )

    # 2.2 Very small positive FRT (e.g. 0.001) must clamp to minimum duration of 1 minute
    small_frts = [0.001, 0.0001, 1e-6, 1e-9, 0.5]
    all_clamped = True
    for frt in small_frts:
        val = estimate_duration(frt, sigma=0.30, percentile=0.80)
        if val < 1 or not isinstance(val, int):
            all_clamped = False
            print(f"  FRT={frt} yielded {val} (type {type(val)})")
    runner.record(
        "2.2 Very small positive FRT clamps to min 1 integer",
        all_clamped,
        f"tested {small_frts}",
    )

    # 2.3 Very large FRT values (10^6, 10^9) must not overflow or produce non-integers
    large_frts = [10**6, 10**7, 10**9, 10**12]
    all_large_ok = True
    large_details = []
    for frt in large_frts:
        try:
            val = estimate_duration(float(frt), sigma=0.30, percentile=0.80)
            if not isinstance(val, int) or val <= frt:
                all_large_ok = False
            large_details.append(f"{frt}->{val}")
        except Exception as e:
            all_large_ok = False
            large_details.append(f"{frt}->{type(e).__name__}")
    runner.record(
        "2.3 Large FRT values (10^6 to 10^12) compute without overflow",
        all_large_ok,
        ", ".join(large_details[:2]),
    )

    # 2.4 Challenge: Out-of-bounds sigma per PROJECT.md Contract 1 ([0.05, 1.0])
    # Contract 1 states: sigma in [0.05, 1.0].
    # Implementation checks: sig < 0.01 or sig > 2.0.
    # Let's test sigma < 0.05 (e.g. 0.03) and sigma > 1.0 (e.g. 1.5).
    sigma_contract_enforced = True
    sigma_contract_details = []

    try:
        val_03 = estimate_duration(60.0, sigma=0.03)
        sigma_contract_enforced = False
        sigma_contract_details.append(f"sigma=0.03 (<0.05) accepted -> {val_03}")
    except ValueError:
        sigma_contract_details.append("sigma=0.03 rejected with ValueError")

    try:
        val_15 = estimate_duration(60.0, sigma=1.5)
        sigma_contract_enforced = False
        sigma_contract_details.append(f"sigma=1.5 (>1.0) accepted -> {val_15}")
    except ValueError:
        sigma_contract_details.append("sigma=1.5 rejected with ValueError")

    runner.record(
        "2.4 PROJECT.md Contract 1: Sigma strictly in [0.05, 1.0]",
        sigma_contract_enforced,
        "; ".join(sigma_contract_details),
    )

    # 2.5 Challenge: Out-of-bounds percentile per PROJECT.md Contract 1 ([0.50, 0.99])
    # Contract 1 states: percentile in [0.50, 0.99].
    # Implementation checks: pct < 0.50 or pct >= 1.0.
    # Test pct=0.49 (should raise) and pct=0.999 (should raise if contract strictly enforced).
    pct_49_raised = False
    try:
        estimate_duration(60.0, percentile=0.49)
    except ValueError:
        pct_49_raised = True

    pct_999_raised = False
    try:
        val_999 = estimate_duration(60.0, percentile=0.999)
    except ValueError:
        pct_999_raised = True

    runner.record(
        "2.5 PROJECT.md Contract 1: Percentile boundary [0.50, 0.99]",
        pct_49_raised and pct_999_raised,
        f"p=0.49 raised={pct_49_raised}; p=0.999 raised={pct_999_raised} (accepted val: {val_999 if not pct_999_raised else 'N/A'})",
    )


# ==============================================================================
# SECTION 3: Fenton-Wilkinson Summation estimate_total_duration Stress
# ==============================================================================

def test_section_3_fenton_wilkinson_stress():
    print("\n" + "=" * 70)
    print("SECTION 3: Fenton-Wilkinson Summation estimate_total_duration Stress")
    print("=" * 70)

    # 3.1 Empty list returns 0
    res_empty_list = estimate_total_duration([])
    res_empty_tuple = estimate_total_duration(())
    res_none = estimate_total_duration(None)
    runner.record(
        "3.1 Empty list/tuple/None returns 0",
        res_empty_list == 0 and res_empty_tuple == 0 and res_none == 0,
        f"[]->{res_empty_list}, ()->{res_empty_tuple}, None->{res_none}",
    )

    # 3.2 Single item yields identical result to estimate_duration
    single_match = True
    single_details = []
    test_cases = [
        (15.0, 0.15, 0.80),
        (30.0, 0.20, 0.85),
        (60.0, 0.30, 0.80),
        (90.0, 0.35, 0.90),
        (120.0, 0.50, 0.95),
    ]
    for frt, sig, pct in test_cases:
        e_dur = estimate_duration(frt, sigma=sig, percentile=pct)
        t_dur_num = estimate_total_duration([frt], sigma=sig, percentile=pct)
        t_dur_dict = estimate_total_duration([{"flat_rate_minutes": frt, "sigma": sig}], percentile=pct)
        if not (e_dur == t_dur_num == t_dur_dict):
            single_match = False
            single_details.append(f"FRT {frt}: dur={e_dur}, num={t_dur_num}, dict={t_dur_dict}")
    runner.record(
        "3.2 Single item yields identical result to estimate_duration",
        single_match,
        "All 5 single-item cases matched exactly" if single_match else "; ".join(single_details),
    )

    # 3.3 Large operation count (50 and 200 operations)
    ops_50 = [25.0] * 50  # 1250 total median
    t0 = time.perf_counter()
    res_50 = estimate_total_duration(ops_50, sigma=0.30, percentile=0.80)
    t_50_ms = (time.perf_counter() - t0) * 1000.0

    ops_200 = [15.0] * 200  # 3000 total median
    t0 = time.perf_counter()
    res_200 = estimate_total_duration(ops_200, sigma=0.30, percentile=0.80)
    t_200_ms = (time.perf_counter() - t0) * 1000.0

    runner.record(
        "3.3 Large operation counts (50 and 200 ops) compute stably and fast",
        isinstance(res_50, int) and isinstance(res_200, int) and res_50 > 1250 and res_200 > 3000,
        f"50 ops: {res_50}m ({t_50_ms:0.3f}ms), 200 ops: {res_200}m ({t_200_ms:0.3f}ms)",
    )

    # 3.4 Variance reduction / sub-additivity: FW sum < Naive sum of single estimates
    # For independent log-normal operations, F-W sum quantile must be strictly less than sum of quantiles
    naive_sum_50 = estimate_duration(25.0, sigma=0.30, percentile=0.80) * 50
    runner.record(
        "3.4 Portfolio risk diversification: FW_sum < Naive_sum",
        res_50 < naive_sum_50,
        f"FW sum: {res_50}m < Naive sum: {naive_sum_50}m (savings: {naive_sum_50 - res_50}m / {(naive_sum_50 - res_50)/naive_sum_50*100:0.1f}%)",
    )

    # 3.5 Sub-lists with any invalid FRT (<= 0) raise ValueError
    invalid_sublists = [
        [60.0, 0.0],
        [0.0, 60.0],
        [30.0, -10.0, 45.0],
        [{"flat_rate_minutes": 60.0}, {"flat_rate_minutes": 0.0}],
        [{"flat_rate_minutes": 60.0}, {"flat_rate_minutes": -5.0}],
        [{"flat_rate_minutes": -1.0}],
        [{"frt_minutes": 0}],
    ]
    all_sublists_raised = True
    sub_fail = []
    for sl in invalid_sublists:
        try:
            estimate_total_duration(sl)
            all_sublists_raised = False
            sub_fail.append(str(sl))
        except ValueError:
            pass
    runner.record(
        "3.5 Sub-lists with any invalid FRT (<= 0) raise ValueError",
        all_sublists_raised,
        f"tested {len(invalid_sublists)} invalid variations; failed: {sub_fail if sub_fail else 'none'}",
    )


# ==============================================================================
# SECTION 4: High-Volume Performance Benchmark & Stress
# ==============================================================================

def test_section_4_performance_stress():
    print("\n" + "=" * 70)
    print("SECTION 4: High-Volume Performance Benchmark & Stress")
    print("=" * 70)

    # 10,000 single evaluations
    t0 = time.perf_counter()
    for _ in range(10000):
        estimate_duration(60.0, sigma=0.30, percentile=0.80)
    elapsed_single = time.perf_counter() - t0

    # 1,000 multi-operation FW evaluations (5 operations each)
    t0 = time.perf_counter()
    sample_ops = [30.0, 45.0, 60.0, 90.0, 120.0]
    for _ in range(1000):
        estimate_total_duration(sample_ops, sigma=0.30, percentile=0.80)
    elapsed_multi = time.perf_counter() - t0

    runner.record(
        "4.1 10,000 single duration calculations execute in < 200ms",
        elapsed_single < 0.200,
        f"completed in {elapsed_single * 1000.0:0.2f}ms ({10000 / elapsed_single:0.0f} ops/sec)",
    )

    runner.record(
        "4.2 1,000 5-operation FW summations execute in < 100ms",
        elapsed_multi < 0.100,
        f"completed in {elapsed_multi * 1000.0:0.2f}ms ({1000 / elapsed_multi:0.0f} ops/sec)",
    )


def run_all():
    print("STARTING EMPIRICAL ADVERSARIAL STRESS TEST SUITE (Milestone 2 Gate)")
    test_section_1_approximate_z_score()
    test_section_2_estimate_duration_boundaries()
    test_section_3_fenton_wilkinson_stress()
    test_section_4_performance_stress()

    print("\n" + "=" * 70)
    print(f"ADVERSARIAL SUMMARY: {runner.passed} Passed, {runner.failed} Failed")
    print("=" * 70)
    if runner.findings:
        print("\nADVERSARIAL FINDINGS / CONTRACT CHALLENGES:")
        for idx, (name, details) in enumerate(runner.findings, 1):
            print(f"{idx}. {name}: {details}")
    print("=" * 70)

    return runner.failed


if __name__ == "__main__":
    exit_code = run_all()
    sys.exit(exit_code)
