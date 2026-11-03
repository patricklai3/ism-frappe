# Copyright (c) 2026, patricklai3 and contributors
# Adversarial Stress and Verification Suite for Milestone 2 Gate
# Role: Empirical Challenger (m2_gate_challenger_2_gen6)

import json
import math
import sys
import traceback
import frappe

from ism.api.estimation_service import get_estimate, get_total_estimate
from ism.scheduling.estimation_light import (
	DEFAULT_SIGMA,
	approximate_z_score,
	estimate_duration,
	estimate_total_duration,
	get_sigma_for_item,
)


class AdversarialTestRunner:
	def __init__(self):
		self.passed = 0
		self.failed = 0
		self.results = []

	def record(self, test_name: str, passed: bool, details: str = ""):
		status = "PASS" if passed else "FAIL"
		print(f"[{status}] {test_name} {details}")
		self.results.append((test_name, passed, details))
		if passed:
			self.passed += 1
		else:
			self.failed += 1
		return passed


runner = AdversarialTestRunner()


# ==============================================================================
# SECTION 1: Dynamic Settings Mutation Stress Test
# ==============================================================================

def test_dynamic_settings_mutation():
	print("\n" + "=" * 70)
	print("SECTION 1: Dynamic Settings Mutation Stress")
	print("=" * 70)

	orig_settings = frappe.get_single("ISM Settings")
	orig_pct = orig_settings.estimation_percentile
	orig_sig = orig_settings.estimation_sigma
	orig_buf = orig_settings.buffer_padding_minutes
	orig_diag = orig_settings.default_diagnostic_duration

	try:
		# 1.1 Percentile Monotonicity & Live Recalculation
		percentiles = [50.0, 70.0, 80.0, 90.0, 95.0, 99.0]
		durations = []
		for p in percentiles:
			frappe.db.set_single_value("ISM Settings", "estimation_percentile", p)
			frappe.db.commit()
			res = get_estimate("Test EST_ITEM_001")  # 60m FRT, brake sigma 0.20
			durations.append(res["p_pure"])
			runner.record(
				f"1.1 Dynamic Percentile {p}%",
				res["p_pure"] > 0 and abs(res["percentile"] - (p / 100.0)) < 1e-4,
				f"p_pure={res['p_pure']}, reported_percentile={res['percentile']}"
			)

		is_monotonic = all(durations[i] <= durations[i + 1] for i in range(len(durations) - 1))
		runner.record(
			"1.1 Percentile Progression Monotonicity",
			is_monotonic and durations[-1] > durations[0],
			f"Durations across {percentiles}: {durations}"
		)

		# 1.2 Decimal Percentile Handling in Settings
		frappe.db.set_single_value("ISM Settings", "estimation_percentile", 0.85)
		frappe.db.commit()
		res_dec = get_estimate("Test EST_ITEM_001")
		runner.record(
			"1.2 Decimal Percentile Settings Input (0.85)",
			abs(res_dec["percentile"] - 0.85) < 1e-4,
			f"Reported percentile: {res_dec['percentile']}"
		)

		# 1.3 Dynamic Sigma Mutation
		frappe.db.set_single_value("ISM Settings", "estimation_percentile", 80.0)
		sigmas = [0.10, 0.25, 0.50, 0.80]
		sigma_durations = []
		for s in sigmas:
			frappe.db.set_single_value("ISM Settings", "estimation_sigma", s)
			frappe.db.commit()
			# Use an item that falls back to default settings sigma
			res_sig = get_estimate("Consulting", fallback_frt=60.0)
			sigma_durations.append(res_sig["p_pure"])
			runner.record(
				f"1.3 Dynamic Settings Sigma {s}",
				abs(res_sig["sigma"] - s) < 1e-4,
				f"p_pure={res_sig['p_pure']}, reported_sigma={res_sig['sigma']}"
			)

		sigma_monotonic = all(sigma_durations[i] <= sigma_durations[i + 1] for i in range(len(sigma_durations) - 1))
		runner.record(
			"1.3 Sigma Progression Monotonicity at P80",
			sigma_monotonic and sigma_durations[-1] > sigma_durations[0],
			f"Durations across {sigmas}: {sigma_durations}"
		)
		# Reset sigma to 0.30 for subsequent tests
		frappe.db.set_single_value("ISM Settings", "estimation_sigma", 0.30)
		frappe.db.commit()

		# 1.4 Dynamic Buffer Padding Mutation
		buffers = [0, 5, 15, 30, 45]
		for b in buffers:
			frappe.db.set_single_value("ISM Settings", "buffer_padding_minutes", b)
			frappe.db.commit()
			res_buf = get_estimate("Test EST_ITEM_001")
			expected_total = res_buf["p_pure"] + b
			runner.record(
				f"1.4 Dynamic Buffer Padding {b} min",
				res_buf["buffer_padding"] == b and res_buf["duration_minutes"] == expected_total,
				f"duration={res_buf['duration_minutes']}, p_pure={res_buf['p_pure']}, buffer={res_buf['buffer_padding']}"
			)

		# 1.5 Multi-Operation Fenton-Wilkinson with Dynamic Settings
		frappe.db.set_single_value("ISM Settings", "estimation_percentile", 90.0)
		frappe.db.set_single_value("ISM Settings", "buffer_padding_minutes", 20)
		frappe.db.commit()
		res_fw = get_total_estimate(["Test EST_ITEM_001", "Test EST_ITEM_002"])
		runner.record(
			"1.5 Multi-Operation Dynamic Settings Propagation",
			res_fw["buffer_padding"] == 20 and res_fw["total_duration_minutes"] == res_fw["p_pure"] + 20 and res_fw["operations_count"] == 2,
			f"total={res_fw['total_duration_minutes']}, p_pure={res_fw['p_pure']}, buffer={res_fw['buffer_padding']}, ops={res_fw['operations_count']}"
		)

		# 1.6 Dynamic Default Diagnostic Duration
		for diag_val in [45.0, 90.0]:
			frappe.db.set_single_value("ISM Settings", "default_diagnostic_duration", diag_val)
			frappe.db.commit()
			res_diag = get_estimate("NON_EXISTENT_DIAGNOSTIC_PART")
			expected_p = estimate_duration(diag_val, sigma=0.30, percentile=0.90)
			runner.record(
				f"1.6 Default Diagnostic Fallback {diag_val}m",
				res_diag["p_pure"] == expected_p,
				f"p_pure={res_diag['p_pure']}, expected={expected_p}"
			)

	finally:
		# Restore original settings
		frappe.db.set_single_value("ISM Settings", "estimation_percentile", orig_pct)
		frappe.db.set_single_value("ISM Settings", "estimation_sigma", orig_sig)
		frappe.db.set_single_value("ISM Settings", "buffer_padding_minutes", orig_buf)
		frappe.db.set_single_value("ISM Settings", "default_diagnostic_duration", orig_diag)
		frappe.db.commit()
		print("[CLEANUP] ISM Settings restored to original values.")


# ==============================================================================
# SECTION 2: API Parameter Stress & Sanitization
# ==============================================================================

def test_api_parameter_stress():
	print("\n" + "=" * 70)
	print("SECTION 2: API Parameter Stress & Sanitization")
	print("=" * 70)

	# 2.1 Non-existent item handling
	res_missing = get_estimate("COMPLETELY_INVALID_PART_NUMBER")
	runner.record(
		"2.1 Missing Item uses default diagnostic fallback without crash",
		isinstance(res_missing, dict) and res_missing["p_pure"] > 0,
		f"Result: {res_missing}"
	)

	res_missing_custom = get_estimate("COMPLETELY_INVALID_PART_NUMBER", fallback_frt=20.0, sigma=0.20, percentile=0.80)
	expected_pure_20 = estimate_duration(20.0, sigma=0.20, percentile=0.80)
	runner.record(
		"2.1 Missing Item with explicit fallback_frt=20m",
		res_missing_custom["p_pure"] == expected_pure_20,
		f"p_pure={res_missing_custom['p_pure']}, expected={expected_pure_20}"
	)

	# 2.2 Item with custom_frt = 0.0 or explicit 0 FRT
	res_zero_frt = get_estimate("Consulting")  # Consulting has custom_frt = 0.0 -> falls back to diagnostic
	runner.record(
		"2.2 Item with custom_frt=0.0 falls back to diagnostic duration",
		isinstance(res_zero_frt, dict) and res_zero_frt["p_pure"] == 78,
		f"Result: {res_zero_frt}"
	)

	res_explicit_zero = get_estimate(flat_rate_minutes=0.0)
	runner.record(
		"2.2 Explicit flat_rate_minutes=0.0 returns duration 0",
		res_explicit_zero["duration_minutes"] == res_explicit_zero["buffer_padding"] and res_explicit_zero["p_pure"] == 0,
		f"Result: {res_explicit_zero}"
	)

	# 2.3 Non-standard item_code inputs (None, empty, numbers, list)
	for bad_code in [None, "", 99999, [], {}]:
		try:
			res_bad = get_estimate(bad_code, fallback_frt=30.0)
			runner.record(
				f"2.3 Graceful handling for bad item_code ({type(bad_code).__name__}: {bad_code})",
				isinstance(res_bad, dict) and res_bad["p_pure"] >= 0,
				f"duration={res_bad['duration_minutes']}"
			)
		except Exception as e:
			runner.record(
				f"2.3 Graceful handling for bad item_code ({bad_code})",
				False,
				f"Raised exception: {e}"
			)

	# 2.4 Kwarg Overrides & Type Permissiveness
	# Percentile as string float vs int
	res_str_pct = get_estimate("Test EST_ITEM_001", percentile="85")
	res_float_pct = get_estimate("Test EST_ITEM_001", percentile=0.85)
	runner.record(
		"2.4 Percentile as string '85' vs float 0.85 equivalence",
		res_str_pct["p_pure"] == res_float_pct["p_pure"] and abs(res_str_pct["percentile"] - 0.85) < 1e-4,
		f"res_str={res_str_pct['p_pure']}, res_float={res_float_pct['p_pure']}"
	)

	# Out of bounds percentile raises ValueError
	raised_oob = False
	try:
		get_estimate("Test EST_ITEM_001", percentile=150.0)
	except ValueError:
		raised_oob = True
	runner.record(
		"2.4 Out-of-bounds percentile (150%) raises ValueError",
		raised_oob,
		"Properly rejected out-of-bounds percentile"
	)

	# Sigma as string vs float
	res_str_sig = get_estimate("Test EST_ITEM_001", sigma="0.35")
	runner.record(
		"2.4 Sigma as string '0.35'",
		abs(res_str_sig["sigma"] - 0.35) < 1e-4,
		f"Reported sigma: {res_str_sig['sigma']}"
	)

	# Buffer padding as string vs int
	res_str_buf = get_estimate("Test EST_ITEM_001", buffer_padding="12")
	runner.record(
		"2.4 Buffer padding as string '12'",
		res_str_buf["buffer_padding"] == 12 and res_str_buf["duration_minutes"] == res_str_buf["p_pure"] + 12,
		f"Result: {res_str_buf}"
	)

	# Explicit FRT kwargs (minutes vs hours)
	res_kw_min = get_estimate(flat_rate_minutes=45.0, sigma=0.20, percentile=0.80)
	res_kw_hr = get_estimate(flat_rate_hours=0.75, sigma=0.20, percentile=0.80)
	runner.record(
		"2.4 flat_rate_minutes=45 vs flat_rate_hours=0.75 equivalence",
		res_kw_min["p_pure"] == res_kw_hr["p_pure"] and res_kw_min["p_pure"] > 0,
		f"min={res_kw_min['p_pure']}, hr={res_kw_hr['p_pure']}"
	)

	# 2.5 Invariant Check: duration_minutes == p_pure + buffer_padding
	invariant_passed = True
	sample_tests = [
		{"item_code": "Test EST_ITEM_001", "buffer_padding": 5},
		{"item_code": "Test EST_ITEM_002", "buffer_padding": 0},
		{"item_code": "Test EST_ITEM_TIRE", "buffer_padding": 18},
		{"item_code": "Test EST_ITEM_ELEC", "buffer_padding": 30},
		{"flat_rate_minutes": 10.0, "buffer_padding": 7},
		{"flat_rate_minutes": 250.0, "buffer_padding": 12},
	]
	for st in sample_tests:
		res_inv = get_estimate(**st)
		if res_inv["duration_minutes"] != res_inv["p_pure"] + res_inv["buffer_padding"]:
			invariant_passed = False
			break
	runner.record(
		"2.5 Invariant: duration_minutes == p_pure + buffer_padding",
		invariant_passed,
		"Verified across test matrix"
	)

	# 2.6 get_total_estimate Input Varieties
	# Python list
	res_list = get_total_estimate(["Test EST_ITEM_001", "Test EST_ITEM_002"])
	runner.record(
		"2.6 get_total_estimate: Python List of strings",
		res_list["operations_count"] == 2 and res_list["total_duration_minutes"] > 0,
		f"Result: {res_list}"
	)

	# JSON string array
	json_str = json.dumps(["Test EST_ITEM_001", "Test EST_ITEM_002"])
	res_json = get_total_estimate(json_str)
	runner.record(
		"2.6 get_total_estimate: JSON string array",
		res_json["p_pure"] == res_list["p_pure"] and res_json["operations_count"] == 2,
		f"Result: {res_json}"
	)

	# Comma-separated string
	csv_str = "Test EST_ITEM_001, Test EST_ITEM_002"
	res_csv = get_total_estimate(csv_str)
	runner.record(
		"2.6 get_total_estimate: Comma-separated string",
		res_csv["p_pure"] == res_list["p_pure"] and res_csv["operations_count"] == 2,
		f"Result: {res_csv}"
	)

	# Malformed JSON string fallback to CSV
	malformed_json = '["Test EST_ITEM_001", "Test EST_ITEM_002"'  # missing closing bracket
	res_malformed = get_total_estimate(malformed_json)
	runner.record(
		"2.6 get_total_estimate: Malformed JSON string fallback",
		isinstance(res_malformed, dict) and res_malformed["operations_count"] >= 1,
		f"Result: {res_malformed}"
	)

	# List of dicts with mixed minutes and hours
	dict_items = [
		{"item_code": "Test EST_ITEM_001"},
		{"flat_rate_minutes": 45.0, "sigma": 0.25},
		{"custom_frt": 0.5, "sigma": 0.15},  # 30 min
	]
	res_dict = get_total_estimate(dict_items, buffer_padding=10)
	runner.record(
		"2.6 get_total_estimate: List of heterogeneous operation dicts",
		res_dict["operations_count"] == 3 and res_dict["total_duration_minutes"] == res_dict["p_pure"] + 10,
		f"Result: {res_dict}"
	)

	# List of raw numeric values
	res_nums = get_total_estimate([30, 45, 60], sigma=0.20, percentile=0.80)
	runner.record(
		"2.6 get_total_estimate: List of raw numeric FRT values",
		res_nums["operations_count"] == 3 and res_nums["p_pure"] > 135,
		f"Result: {res_nums}"
	)

	# Empty inputs
	for empty_val in [[], "[]", "", None, {}]:
		res_empty = get_total_estimate(empty_val, buffer_padding=15)
		runner.record(
			f"2.6 get_total_estimate: Empty input ({type(empty_val).__name__}) returns 0 duration",
			res_empty["total_duration_minutes"] == 0 and res_empty["p_pure"] == 0 and res_empty["operations_count"] == 0,
			f"Result: {res_empty}"
		)

	# 2.7 skip_missing Behavior
	res_with_missing_skipped = get_total_estimate(
		["Test EST_ITEM_001", "COMPLETELY_FAKE_PART_1234"],
		skip_missing=True,
		buffer_padding=10
	)
	res_single_001 = get_estimate("Test EST_ITEM_001", buffer_padding=10)
	runner.record(
		"2.7 skip_missing=True skips missing items and matches single item exactly",
		res_with_missing_skipped["operations_count"] == 1 and res_with_missing_skipped["p_pure"] == res_single_001["p_pure"],
		f"fw_pure={res_with_missing_skipped['p_pure']}, single_pure={res_single_001['p_pure']}"
	)

	res_with_missing_included = get_total_estimate(
		["Test EST_ITEM_001", "COMPLETELY_FAKE_PART_1234"],
		skip_missing=False,
		buffer_padding=10
	)
	runner.record(
		"2.7 skip_missing=False includes missing item using fallback diagnostic duration",
		res_with_missing_included["operations_count"] == 2 and res_with_missing_included["p_pure"] > res_single_001["p_pure"],
		f"included_pure={res_with_missing_included['p_pure']}, single_pure={res_single_001['p_pure']}"
	)


# ==============================================================================
# SECTION 3: Database Hygiene & State Invariance
# ==============================================================================

def test_database_hygiene():
	print("\n" + "=" * 70)
	print("SECTION 3: Database Hygiene & State Invariance")
	print("=" * 70)

	se_count = frappe.db.count("Schedule Entry")
	runner.record(
		"3.1 Schedule Entry table count is 0",
		se_count == 0,
		f"Schedule Entry count: {se_count}"
	)

	seed_vins = ("5YJSA1E26CF000001", "LRW3E1FA9PC445566")
	non_seed_vehicles = [
		v for v in frappe.get_all("Repair Vehicle", pluck="name")
		if v not in seed_vins
	]
	runner.record(
		"3.2 Non-seed Repair Vehicle count is 0",
		len(non_seed_vehicles) == 0,
		f"Non-seed vehicles found: {non_seed_vehicles}"
	)

	# Verify ISM Settings has no corruption
	settings = frappe.get_single("ISM Settings")
	has_required_fields = all(
		hasattr(settings, f) for f in [
			"estimation_percentile",
			"estimation_sigma",
			"buffer_padding_minutes",
			"default_diagnostic_duration"
		]
	)
	runner.record(
		"3.3 ISM Settings structure and fields intact",
		has_required_fields,
		f"Percentile={settings.estimation_percentile}, Sigma={settings.estimation_sigma}, Buffer={settings.buffer_padding_minutes}"
	)


def main():
	print("Starting Adversarial Stress & Verification Suite for Milestone 2 Gate...")
	frappe.init("development.localhost")
	frappe.connect()
	try:
		test_dynamic_settings_mutation()
		test_api_parameter_stress()
		test_database_hygiene()
	except Exception as e:
		print(f"\nFATAL ERROR DURING TEST EXECUTION: {e}")
		traceback.print_exc()
		sys.exit(1)

	print("\n" + "=" * 70)
	print(f"ADVERSARIAL STRESS SUITE COMPLETED: {runner.passed} PASSED, {runner.failed} FAILED")
	print("=" * 70)

	if runner.failed > 0:
		print("FAILED TESTS:")
		for name, passed, details in runner.results:
			if not passed:
				print(f"  - {name}: {details}")
		sys.exit(1)
	else:
		print("ALL ADVERSARIAL STRESS TESTS PASSED SUCCESSFULLY!")
		sys.exit(0)


if __name__ == "__main__":
	main()
