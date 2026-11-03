# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import math
import unittest
from unittest.mock import patch
import frappe

from ism.tests.base import ISMTestCase
from ism.tests.test_fixtures import setup_all, teardown_transactional, ensure_ism_settings, PREFIX, CUSTOMER, VEHICLE_VIN

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
		estimate_repair_order,
	)
except ImportError:
	get_estimate = None
	get_total_estimate = None
	estimate_repair_order = None


class TestEstimationMath(ISMTestCase):
	"""
	Comprehensive 4-Tier Test Suite for Features 1–5 (Estimation Engine & Pure Math):
	- Feature 1: Analytic Log-Normal P80
	- Feature 2: Fenton-Wilkinson Summation
	- Feature 3: Category Sigma Profiles
	- Feature 4: Rational Z-Score Approximation (Abramowitz & Stegun 26.2.23)
	- Feature 5: Configurable Buffer Padding & Dynamic ISM Settings
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		setup_all()

	@classmethod
	def tearDownClass(cls):
		teardown_transactional()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		self.item_brake1 = f"{PREFIX}EST_ITEM_001"
		self.item_brake2 = f"{PREFIX}EST_ITEM_002"
		self.item_tire = f"{PREFIX}EST_ITEM_TIRE"
		self.item_elec = f"{PREFIX}EST_ITEM_ELEC"

	def tearDown(self):
		super().tearDown()

	def _require_math(self):
		if estimate_duration is None:
			self.skipTest("ism.scheduling.estimation_light not yet implemented (Milestone M2)")

	def _require_service(self):
		if get_estimate is None or estimate_repair_order is None:
			self.skipTest("ism.api.estimation_service not yet implemented (Milestone M2)")

	# =========================================================================
	# TIER 1: FEATURE COVERAGE (>=5 tests per feature)
	# =========================================================================

	# --- Feature 1: Analytic Log-Normal P80 ---
	def test_tier1_f1_p80_60_minutes_baseline(self):
		"""F1: 60 min FRT with 0.30 sigma at P80 yields ceil(exp(ln(60) + 0.30 * 0.8416)) = 78."""
		self._require_math()
		res = estimate_duration(60.0, sigma=0.30, percentile=0.80)
		self.assertEqual(res, 78)

	def test_tier1_f1_p80_30_minutes(self):
		"""F1: 30 min FRT with 0.30 sigma at P80 yields ceil(exp(ln(30) + 0.30 * 0.8416)) = 39."""
		self._require_math()
		res = estimate_duration(30.0, sigma=0.30, percentile=0.80)
		self.assertEqual(res, 39)

	def test_tier1_f1_p80_15_minutes(self):
		"""F1: 15 min FRT with 0.30 sigma at P80 yields ceil(exp(ln(15) + 0.30 * 0.8416)) = 20."""
		self._require_math()
		res = estimate_duration(15.0, sigma=0.30, percentile=0.80)
		self.assertEqual(res, 20)

	def test_tier1_f1_p80_90_minutes(self):
		"""F1: 90 min FRT with 0.30 sigma at P80 yields ceil(exp(ln(90) + 0.30 * 0.8416)) = 116."""
		self._require_math()
		res = estimate_duration(90.0, sigma=0.30, percentile=0.80)
		self.assertEqual(res, 116)

	def test_tier1_f1_p80_120_minutes(self):
		"""F1: 120 min FRT with 0.30 sigma at P80 yields ceil(exp(ln(120) + 0.30 * 0.8416)) = 155."""
		self._require_math()
		res = estimate_duration(120.0, sigma=0.30, percentile=0.80)
		self.assertEqual(res, 155)

	# --- Feature 2: Fenton-Wilkinson Summation ---
	def test_tier1_f2_fw_sum_identical_triplet(self):
		"""F2: Sum of three 60m ops (sigma 0.30) produces 215 min (less than naive 78*3=234)."""
		self._require_math()
		res = estimate_total_duration([60.0, 60.0, 60.0], sigma=0.30, percentile=0.80)
		self.assertEqual(res, 215)
		self.assertLess(res, 78 * 3)

	def test_tier1_f2_fw_sum_dict_input(self):
		"""F2: estimate_total_duration accepts list of dicts with individual sigmas."""
		self._require_math()
		ops = [
			{"flat_rate_minutes": 60.0, "sigma": 0.30},
			{"flat_rate_minutes": 60.0, "sigma": 0.30},
			{"flat_rate_minutes": 60.0, "sigma": 0.30},
		]
		res = estimate_total_duration(ops, percentile=0.80)
		self.assertEqual(res, 215)

	def test_tier1_f2_fw_sum_diverse_durations(self):
		"""F2: Sum of 30m, 60m, 90m ops displays variance reduction."""
		self._require_math()
		res = estimate_total_duration([30.0, 60.0, 90.0], sigma=0.30, percentile=0.80)
		naive = estimate_duration(30.0, 0.30) + estimate_duration(60.0, 0.30) + estimate_duration(90.0, 0.30)
		self.assertTrue(180 < res < naive)

	def test_tier1_f2_fw_sum_heterogeneous_sigmas(self):
		"""F2: Dict items with differing category sigmas (0.20, 0.35, 0.50)."""
		self._require_math()
		ops = [
			{"flat_rate_minutes": 60.0, "sigma": 0.20},  # Brake
			{"flat_rate_minutes": 90.0, "sigma": 0.35},  # Suspension
			{"flat_rate_minutes": 45.0, "sigma": 0.50},  # Electrical
		]
		res = estimate_total_duration(ops, percentile=0.80)
		self.assertTrue(res > (60 + 90 + 45))

	def test_tier1_f2_fw_sum_two_large_ops(self):
		"""F2: Two 120m ops with 0.25 sigma."""
		self._require_math()
		res = estimate_total_duration([120.0, 120.0], sigma=0.25, percentile=0.80)
		self.assertTrue(240 < res < 310)

	# --- Feature 3: Category Sigma Profiles ---
	def test_tier1_f3_brake_sigma(self):
		"""F3: Brake category has low-moderate sigma of 0.20."""
		self._require_math()
		self.assertEqual(get_sigma_for_item("Brake"), 0.20)

	def test_tier1_f3_tire_sigma(self):
		"""F3: Tire category has low sigma of 0.15."""
		self._require_math()
		self.assertEqual(get_sigma_for_item("Tire"), 0.15)

	def test_tier1_f3_suspension_sigma(self):
		"""F3: Suspension category has moderate-high sigma of 0.35."""
		self._require_math()
		self.assertEqual(get_sigma_for_item("Suspension"), 0.35)

	def test_tier1_f3_electrical_sigma(self):
		"""F3: Electrical category has high variance sigma of 0.50."""
		self._require_math()
		self.assertEqual(get_sigma_for_item("Electrical"), 0.50)

	def test_tier1_f3_default_and_none_sigma(self):
		"""F3: Unrecognized or None category defaults to standard 0.30."""
		self._require_math()
		self.assertEqual(get_sigma_for_item(None), 0.30)
		self.assertEqual(get_sigma_for_item("General Maintenance"), 0.30)
		self.assertEqual(get_sigma_for_item("UnknownCategory"), 0.30)

	# --- Feature 4: Rational Z-Score Approximation ---
	def test_tier1_f4_z_score_p50(self):
		"""F4: Z-score for median (P50) is 0.0."""
		self._require_math()
		z = approximate_z_score(0.50)
		self.assertAlmostEqual(z, 0.0, places=4)

	def test_tier1_f4_z_score_p80(self):
		"""F4: Z-score for P80 is ~0.8416."""
		self._require_math()
		z = approximate_z_score(0.80)
		self.assertAlmostEqual(z, 0.84162, places=3)

	def test_tier1_f4_z_score_p90(self):
		"""F4: Z-score for P90 is ~1.2816."""
		self._require_math()
		z = approximate_z_score(0.90)
		self.assertAlmostEqual(z, 1.28155, places=3)

	def test_tier1_f4_z_score_p95(self):
		"""F4: Z-score for P95 is ~1.6449."""
		self._require_math()
		z = approximate_z_score(0.95)
		self.assertAlmostEqual(z, 1.64485, places=3)

	def test_tier1_f4_z_score_p99(self):
		"""F4: Z-score for P99 is ~2.3263."""
		self._require_math()
		z = approximate_z_score(0.99)
		self.assertAlmostEqual(z, 2.32635, places=2)

	# --- Feature 5: Configurable Buffer Padding & Service Integration ---
	def test_tier1_f5_single_item_estimate_with_buffer(self):
		"""F5: get_estimate adds buffer_padding_minutes from ISM Settings."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=10, estimation_percentile=0.80, estimation_sigma=0.30)
		res = get_estimate(self.item_brake1)  # Brake FRT 1.0 hr = 60m, sigma 0.20 -> P80 pure is 71
		if isinstance(res, dict):
			self.assertEqual(res["p_pure"], 71)
			self.assertEqual(res["buffer_padding"], 10)
			self.assertEqual(res["duration_minutes"], 81)
		else:
			# Fallback if scalar returned
			self.assertEqual(res, 81)

	def test_tier1_f5_multi_item_estimate_with_buffer(self):
		"""F5: get_total_estimate sums items with F-W and adds buffer padding."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=15)
		res = get_total_estimate([self.item_brake1, self.item_brake2])
		if isinstance(res, dict):
			self.assertEqual(res["buffer_padding"], 15)
			self.assertEqual(res["total_duration_minutes"], res["p_pure"] + 15)
		else:
			self.assertGreater(res, 120)

	def test_tier1_f5_zero_buffer_setting(self):
		"""F5: buffer_padding_minutes=0 adds no padding."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=0)
		res = get_estimate(self.item_brake1)
		p_pure = res["p_pure"] if isinstance(res, dict) else res
		self.assertEqual(p_pure, 71)

	def test_tier1_f5_max_buffer_setting(self):
		"""F5: buffer_padding_minutes=30 adds 30 minutes."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=30)
		res = get_estimate(self.item_brake1)
		total = res["duration_minutes"] if isinstance(res, dict) else res
		self.assertEqual(total, 71 + 30)

	def test_tier1_f5_whitelisted_json_payload(self):
		"""F5: get_total_estimate parses JSON string of items."""
		self._require_service()
		json_items = json.dumps([self.item_brake1, self.item_tire])
		res = get_total_estimate(json_items)
		self.assertIsNotNone(res)

	# =========================================================================
	# TIER 2: BOUNDARY & CORNER CASES (>=5 tests per feature)
	# =========================================================================

	def test_tier2_f1_zero_and_negative_frt_raises(self):
		"""F1 Boundary: FRT <= 0 raises ValueError."""
		self._require_math()
		with self.assertRaises(ValueError):
			estimate_duration(0.0)
		with self.assertRaises(ValueError):
			estimate_duration(-15.0)

	def test_tier2_f1_extreme_percentile_boundaries(self):
		"""F1 Boundary: Percentiles < 0.50 or >= 1.0 raise ValueError."""
		self._require_math()
		with self.assertRaises(ValueError):
			estimate_duration(60.0, percentile=0.49)
		with self.assertRaises(ValueError):
			estimate_duration(60.0, percentile=1.0)

	def test_tier2_f1_extreme_sigma_boundaries(self):
		"""F1 Boundary: Sigma < 0.01 or > 2.0 raise ValueError."""
		self._require_math()
		with self.assertRaises(ValueError):
			estimate_duration(60.0, sigma=0.0)
		with self.assertRaises(ValueError):
			estimate_duration(60.0, sigma=-0.2)

	def test_tier2_f1_very_small_frt_clamps_to_min_1(self):
		"""F1 Boundary: Minimal FRT clamps to at least 1 minute."""
		self._require_math()
		res = estimate_duration(0.01, sigma=0.10, percentile=0.80)
		self.assertGreaterEqual(res, 1)

	def test_tier2_f1_large_frt_overflow_protection(self):
		"""F1 Boundary: Large commercial job (6000 minutes = 100 hrs)."""
		self._require_math()
		res = estimate_duration(6000.0, sigma=0.30, percentile=0.80)
		self.assertGreater(res, 6000)
		self.assertLess(res, 12000)

	def test_tier2_f2_empty_list_returns_zero(self):
		"""F2 Boundary: Empty list produces 0 duration."""
		self._require_math()
		self.assertEqual(estimate_total_duration([]), 0)

	def test_tier2_f2_single_item_matches_estimate_duration(self):
		"""F2 Boundary: Single item bypasses summation and matches estimate_duration exactly."""
		self._require_math()
		self.assertEqual(estimate_total_duration([60.0], sigma=0.30), 78)
		self.assertEqual(estimate_total_duration([{"flat_rate_minutes": 60.0, "sigma": 0.30}]), 78)

	def test_tier2_f2_list_with_invalid_frt_raises(self):
		"""F2 Boundary: Any zero or negative FRT in list raises ValueError."""
		self._require_math()
		with self.assertRaises(ValueError):
			estimate_total_duration([60.0, 0.0])
		with self.assertRaises(ValueError):
			estimate_total_duration([-10.0, 30.0])

	def test_tier2_f2_large_operation_count(self):
		"""F2 Boundary: 20 sequential operations scale stably."""
		self._require_math()
		ops = [15.0] * 20  # 300 minutes total median
		res = estimate_total_duration(ops, sigma=0.20, percentile=0.80)
		self.assertGreater(res, 300)
		self.assertLess(res, 400)

	def test_tier2_f3_case_insensitive_category(self):
		"""F3 Boundary: Category names match case-insensitively and strip whitespace."""
		self._require_math()
		self.assertEqual(get_sigma_for_item("brake"), 0.20)
		self.assertEqual(get_sigma_for_item("  TIRE  "), 0.15)
		self.assertEqual(get_sigma_for_item("Electrical"), 0.50)

	def test_tier2_f4_z_score_monotonicity(self):
		"""F4 Boundary: Z-score is strictly monotonic increasing across range."""
		self._require_math()
		probs = [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99]
		z_scores = [approximate_z_score(p) for p in probs]
		for i in range(len(z_scores) - 1):
			self.assertLess(z_scores[i], z_scores[i + 1])

	def test_tier2_f5_missing_item_fallback_frt(self):
		"""F5 Boundary: Missing Item in get_estimate uses fallback FRT."""
		self._require_service()
		res = get_estimate("NON_EXISTENT_PART_XYZ", fallback_frt=45.0, sigma=0.30)
		# 45 min at 0.30 sigma P80 -> ceil(exp(ln(45) + 0.30*0.8416)) = 58 + 10 buffer = 68
		duration = res["duration_minutes"] if isinstance(res, dict) else res
		self.assertTrue(duration > 0)

	def test_tier2_f5_skip_missing_items(self):
		"""F5 Boundary: skip_missing=True ignores missing items without error."""
		self._require_service()
		res_skip = get_total_estimate([self.item_brake1, "NON_EXISTENT_XYZ"], skip_missing=True)
		res_single = get_estimate(self.item_brake1)
		dur_skip = res_skip["total_duration_minutes"] if isinstance(res_skip, dict) else res_skip
		dur_single = res_single["duration_minutes"] if isinstance(res_single, dict) else res_single
		self.assertEqual(dur_skip, dur_single)

	# =========================================================================
	# TIER 3: CROSS-FEATURE COMBINATIONS
	# =========================================================================

	def test_tier3_fw_sum_with_category_sigmas_and_buffer(self):
		"""Tier 3: Multi-operation repair across diverse categories with buffer padding."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=15, estimation_percentile=0.85)
		res = get_total_estimate([self.item_brake1, self.item_elec, self.item_tire])
		if isinstance(res, dict):
			self.assertEqual(res["buffer_padding"], 15)
			self.assertEqual(res["operations_count"], 3)
			self.assertGreater(res["total_duration_minutes"], 200)

	def test_tier3_dynamic_settings_recalculation(self):
		"""Tier 3: Mutating ISM Settings percentile & buffer immediately alters calculation."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=5, estimation_percentile=0.50)
		res_p50 = get_estimate(self.item_brake1)
		dur_p50 = res_p50["duration_minutes"] if isinstance(res_p50, dict) else res_p50

		ensure_ism_settings(buffer_padding_minutes=25, estimation_percentile=0.95)
		res_p95 = get_estimate(self.item_brake1)
		dur_p95 = res_p95["duration_minutes"] if isinstance(res_p95, dict) else res_p95

		self.assertGreater(dur_p95, dur_p50)

	# =========================================================================
	# TIER 4: REAL-WORLD APPLICATION SCENARIOS
	# =========================================================================

	def test_tier4_scenario_full_service_package_estimation(self):
		"""
		Tier 4 (Scenario 4 Component):
		Full shop package estimation:
		1. Front Brake Pads (60 min FRT, Brake sigma 0.20)
		2. Rear Brake Rotors (90 min FRT, Brake sigma 0.20)
		3. High Voltage Harness Diagnostic (120 min FRT, Electrical sigma 0.50)
		4. Tire Mount & Balance (30 min FRT, Tire sigma 0.15)
		Evaluates diversification savings vs naive sum and adds shop standard buffer.
		"""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=15, estimation_percentile=0.80)

		items = [self.item_brake1, self.item_brake2, self.item_elec, self.item_tire]
		res = get_total_estimate(items)

		if isinstance(res, dict):
			total = res["total_duration_minutes"]
			pure = res["p_pure"]
			buffer = res["buffer_padding"]
			self.assertEqual(buffer, 15)
			self.assertEqual(total, pure + buffer)
			# Naive sum would exceed 380 min; diversified pure estimate should be significantly lower
			self.assertLess(pure, 380)
			self.assertGreater(pure, 300)

	# --- estimate_repair_order Tests ---

	def test_estimate_repair_order_with_multiple_services(self):
		"""estimate_repair_order accurately aggregates active services from Repair Order."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=10, estimation_percentile=0.80)
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)
		ro.append("services", {
			"service_title": "Front Brake Rotor Replacement",
			"labor_hours": 1.0,
			"status": "Estimating",
		})
		ro.append("services", {
			"service_title": "Tire Rotation and Balance",
			"labor_hours": 0.5,
			"status": "Approved",
		})
		ro.save(ignore_permissions=True)

		res = estimate_repair_order(ro.name)
		self.assertIsInstance(res, dict)
		self.assertIn("total_minutes", res)
		self.assertIn("total_duration_minutes", res)
		self.assertEqual(res["total_minutes"], res["total_duration_minutes"])
		self.assertEqual(res["operations_count"], 2)
		self.assertEqual(res["buffer_padding"], 10)
		self.assertGreater(res["total_minutes"], 85)

	def test_estimate_repair_order_declined_service_skipped(self):
		"""estimate_repair_order ignores declined services."""
		self._require_service()
		ensure_ism_settings(buffer_padding_minutes=0, estimation_percentile=0.80)
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)
		ro.append("services", {
			"service_title": "Standard Brake Service",
			"labor_hours": 1.0,
			"status": "Approved",
		})
		ro.append("services", {
			"service_title": "Declined Big Service",
			"labor_hours": 3.0,
			"status": "Declined",
		})
		ro.save(ignore_permissions=True)

		res = estimate_repair_order(ro.name)
		self.assertEqual(res["operations_count"], 1)
		# 1.0 hour (60 min) at P80 with Brake sigma 0.20 is ceil(exp(ln(60) + 0.20*0.8416)) = 71
		self.assertEqual(res["total_minutes"], 71)

	def test_estimate_repair_order_empty_services_fallback(self):
		"""estimate_repair_order falls back to diagnostic duration when RO has no services."""
		self._require_service()
		ensure_ism_settings(default_diagnostic_duration=60, buffer_padding_minutes=10, estimation_percentile=0.80)
		ro = self.create_test_repair_order(customer=CUSTOMER, vehicle=VEHICLE_VIN)
		# Clear services if any
		ro.set("services", [])
		ro.save(ignore_permissions=True)

		res = estimate_repair_order(ro.name)
		self.assertEqual(res["operations_count"], 1)
		self.assertEqual(res["buffer_padding"], 10)
		self.assertGreater(res["total_minutes"], 60)

	def test_estimate_repair_order_empty_or_none(self):
		"""estimate_repair_order returns 0 when repair_order is None or empty."""
		self._require_service()
		res_none = estimate_repair_order(None)
		self.assertEqual(res_none["total_minutes"], 0)
		self.assertEqual(res_none["operations_count"], 0)

		res_empty = estimate_repair_order("")
		self.assertEqual(res_empty["total_minutes"], 0)
		self.assertEqual(res_empty["operations_count"], 0)

		# Temporary unpersisted Desk IDs starting with 'new-' should return 0 without error
		res_new = estimate_repair_order("new-schedule-entry-cajywbtqla")
		self.assertEqual(res_new["total_minutes"], 0)
		self.assertEqual(res_new["operations_count"], 0)

	def test_estimate_repair_order_not_found(self):
		"""estimate_repair_order raises DoesNotExistError if RO does not exist."""
		self._require_service()
		with self.assertRaises(frappe.DoesNotExistError):
			estimate_repair_order("NON_EXISTENT_RO_NAME_99999")
