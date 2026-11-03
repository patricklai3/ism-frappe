# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import math
from typing import Any, Dict, List, Optional, Union

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
	"Brake": 0.20,        # Low variance — well-defined mechanical scope
	"Tire": 0.15,         # Very predictable
	"Suspension": 0.35,   # Moderate variance — corrosion/rust dependent
	"Electrical": 0.50,   # High variance — diagnosis-heavy
	"Diagnostic": 0.40,   # Diagnosis variability
	"Body": 0.30,         # Standard auto body variance
	"HVAC": 0.25,         # Moderate variance
	"Drivetrain": 0.40,   # Complex mechanical
}

_LOWER_SIGMA = {k.lower(): v for k, v in SIGMA_OVERRIDES.items()}


def get_sigma_for_item(
	item_group: Optional[str] = None,
	default_sigma: float = DEFAULT_SIGMA,
) -> float:
	"""
	Resolve the log-normal σ parameter for an item based on its category / group.
	Matches case-insensitively and strips whitespace. Defaults to default_sigma (0.30).
	"""
	if not item_group or not isinstance(item_group, str):
		return default_sigma
	cleaned = item_group.strip().lower()
	return _LOWER_SIGMA.get(cleaned, default_sigma)


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
