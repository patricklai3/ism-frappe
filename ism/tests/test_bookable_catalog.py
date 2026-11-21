# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json

import frappe

from ism.scheduling.bookable_catalog import (
	ALLOWED_PRICE_DISPLAY_MODES,
	MAX_PACKAGES_PER_BOOKING,
	compute_packages_booking_requirements,
	format_package_price_display,
	is_package_eligible_for_vehicle,
	list_bookable_packages,
	normalize_vehicle_model,
	parse_generation_year_bounds,
)
from ism.scheduling.estimation_light import estimate_total_duration
from ism.tests.base import ISMTestCase, TEST_PREFIX

PUBLIC_CARD_KEYS = {
	"package_id",
	"title",
	"summary",
	"applicable_model",
	"is_mobile_capable",
	"sort_order",
	"price_display_mode",
	"currency",
	"price_min",
	"price_max",
	"price_display_label",
}

FORBIDDEN_INTERNAL_KEYS = {
	"correction_code",
	"rh_correction_code",
	"hourly_rate",
	"service_rate",
	"tesla_manual_url",
	"doc_guid",
	"items",
	"standard_labor_hours",
	"self_serve_duration_minutes",
	"self_serve_required_tags",
	"supplier_cost",
}


class TestBookableCatalog(ISMTestCase):
	"""Tests for Service Package self-serve curation & bookable_catalog.py (Project 3 Wave 1B)."""

	def setUp(self):
		super().setUp()
		self.manifest = self.get_manifest()

	def _set_ism_settings(self, **kwargs):
		if not frappe.db.exists("DocType", "ISM Settings"):
			return
		settings = frappe.get_single("ISM Settings")
		for key, val in kwargs.items():
			setattr(settings, key, val)
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype="ISM Settings")

	def _create_equipment_tag(self, tag_name: str):
		if frappe.db.exists("Equipment Tag", tag_name):
			return frappe.get_doc("Equipment Tag", tag_name)
		doc = frappe.get_doc(
			{
				"doctype": "Equipment Tag",
				"tag_name": tag_name,
				"description": "Test tag",
			}
		).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def _create_package(self, suffix: str, **kwargs):
		pkg_name = f"{TEST_PREFIX}Pkg_{suffix}"
		payload = {
			"doctype": "Service Package",
			"package_name": pkg_name,
			"applicable_model": "Model 3",
			"standard_labor_hours": 1.0,
			"hourly_rate": 140.0,
			"service_rate": 140.0,
			"allow_self_serve_booking": 1,
			"customer_price_display_mode": "Exact Price",
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def test_non_bookable_packages_excluded_and_rejected(self):
		hidden_pkg = self._create_package(
			"Hidden_Internal",
			allow_self_serve_booking=0,
			customer_title="Internal Only",
		)
		visible_pkg = self._create_package(
			"Visible_Public",
			allow_self_serve_booking=1,
			customer_title="Cabin Air Filter Replacement",
		)

		cards = list_bookable_packages()
		ids = {c["package_id"] for c in cards}
		self.assertNotIn(hidden_pkg.name, ids)
		self.assertIn(visible_pkg.name, ids)

		with self.assertRaises(frappe.ValidationError):
			compute_packages_booking_requirements([hidden_pkg.name])

	def test_model_normalization_and_filtering(self):
		self.assertEqual(normalize_vehicle_model("tesla model 3"), "Model 3")
		self.assertEqual(normalize_vehicle_model("  Model   Y "), "Model Y")
		self.assertEqual(normalize_vehicle_model("m3"), "Model 3")
		self.assertIsNone(normalize_vehicle_model("   "))

		p_m3 = self._create_package("M3_Only", applicable_model="Model 3", customer_title="M3 Brake Fluid")
		p_my = self._create_package("MY_Only", applicable_model="Model Y", customer_title="MY Heat Pump")
		p_all = self._create_package("All_Models", applicable_model="All Models", customer_title="Tire Rotation")

		cards = list_bookable_packages(vehicle_model="tesla model 3")
		ids = {c["package_id"] for c in cards}
		self.assertIn(p_m3.name, ids)
		self.assertIn(p_all.name, ids)
		self.assertNotIn(p_my.name, ids)

		with self.assertRaises(frappe.ValidationError):
			compute_packages_booking_requirements([p_my.name], vehicle_model="Model 3")

	def test_year_filtering_explicit_and_generation_fallback(self):
		self.assertEqual(parse_generation_year_bounds("Model 3 Jun 2017 - Dec 2023"), (2017, 2023))
		self.assertEqual(parse_generation_year_bounds("Model Y Jan 2020 - Present"), (2020, None))
		self.assertEqual(parse_generation_year_bounds("No years here"), (None, None))

		p_explicit = self._create_package(
			"Explicit_Years",
			applicable_model="Model 3",
			applicable_year_from=2021,
			applicable_year_to=2024,
			applicable_generation="Model 3 Jun 2017 - Dec 2023",
		)
		p_gen_bounded = self._create_package(
			"Gen_Bounded",
			applicable_model="Model 3",
			applicable_year_from=0,
			applicable_year_to=0,
			applicable_generation="Model 3 Jun 2017 - Dec 2023",
		)
		p_gen_open = self._create_package(
			"Gen_Open",
			applicable_model="Model Y",
			applicable_year_from=0,
			applicable_year_to=0,
			applicable_generation="Model Y Jan 2020 - Present",
		)

		self.assertFalse(is_package_eligible_for_vehicle(p_explicit.as_dict(), "Model 3", 2020))
		self.assertTrue(is_package_eligible_for_vehicle(p_explicit.as_dict(), "Model 3", 2024))
		self.assertFalse(is_package_eligible_for_vehicle(p_explicit.as_dict(), "Model 3", 2025))

		self.assertTrue(is_package_eligible_for_vehicle(p_gen_bounded.as_dict(), "Model 3", 2019))
		self.assertFalse(is_package_eligible_for_vehicle(p_gen_bounded.as_dict(), "Model 3", 2024))

		self.assertFalse(is_package_eligible_for_vehicle(p_gen_open.as_dict(), "Model Y", 2019))
		self.assertTrue(is_package_eligible_for_vehicle(p_gen_open.as_dict(), "Model Y", 2026))

	def test_year_validation_bounds(self):
		with self.assertRaises(frappe.ValidationError):
			self._create_package("Year_Too_Low", applicable_year_from=2005)
		with self.assertRaises(frappe.ValidationError):
			self._create_package("Year_Too_High", applicable_year_to=2105)
		with self.assertRaises(frappe.ValidationError):
			self._create_package("Year_Inverted", applicable_year_from=2024, applicable_year_to=2020)

	def test_price_display_modes_and_fallback_pricing(self):
		self.assertEqual(
			ALLOWED_PRICE_DISPLAY_MODES,
			("Exact Price", "Starting At", "Estimate Range", "Contact for Quote"),
		)

		exact = format_package_price_display(
			{"customer_price_display_mode": "Exact Price", "customer_estimate_min": 185.0}
		)
		self.assertEqual(exact["price_display_mode"], "Exact Price")
		self.assertEqual(exact["price_min"], 185.0)
		self.assertEqual(exact["price_max"], 185.0)
		self.assertEqual(exact["price_display_label"], "$185.00 CAD")

		starting = format_package_price_display(
			{"customer_price_display_mode": "Starting At", "customer_estimate_min": 99.0}
		)
		self.assertEqual(starting["price_display_mode"], "Starting At")
		self.assertEqual(starting["price_min"], 99.0)
		self.assertIsNone(starting["price_max"])
		self.assertEqual(starting["price_display_label"], "Starting at $99.00 CAD")

		ranged = format_package_price_display(
			{
				"customer_price_display_mode": "Estimate Range",
				"customer_estimate_min": 200.0,
				"customer_estimate_max": 350.0,
			}
		)
		self.assertEqual(ranged["price_display_mode"], "Estimate Range")
		self.assertEqual(ranged["price_min"], 200.0)
		self.assertEqual(ranged["price_max"], 350.0)
		self.assertIn("$200.00 CAD", ranged["price_display_label"])
		self.assertIn("$350.00 CAD", ranged["price_display_label"])

		quote = format_package_price_display(
			{"customer_price_display_mode": "Contact for Quote", "customer_estimate_min": 200.0}
		)
		self.assertEqual(quote["price_display_mode"], "Contact for Quote")
		self.assertIsNone(quote["price_min"])
		self.assertIsNone(quote["price_max"])
		self.assertEqual(quote["price_display_label"], "Contact for Quote")

		# Fallback from service_rate + default parts subtotal
		part_item = self.get_or_create_test_item(f"{TEST_PREFIX}Cabin_Filter")
		fallback_pkg = self._create_package(
			"Fallback_Price",
			service_rate=140.0,
			customer_price_display_mode="Exact Price",
			customer_estimate_min=0,
			items=[{"item_code": part_item, "is_default": 1, "qty": 2, "rate": 25.0}],
		)
		cards = {c["package_id"]: c for c in list_bookable_packages()}
		self.assertEqual(cards[fallback_pkg.name]["price_min"], 190.0)
		self.assertEqual(cards[fallback_pkg.name]["price_display_label"], "$190.00 CAD")

		# Degrades cleanly to Contact for Quote when no price can be computed
		zero_price = format_package_price_display(
			{"customer_price_display_mode": "Exact Price", "customer_estimate_min": 0, "service_rate": 0}
		)
		self.assertEqual(zero_price["price_display_mode"], "Contact for Quote")

		# Validation rejects estimate_max < estimate_min and negative bounds
		with self.assertRaises(frappe.ValidationError):
			self._create_package(
				"Bad_Range",
				customer_price_display_mode="Estimate Range",
				customer_estimate_min=300.0,
				customer_estimate_max=200.0,
			)
		with self.assertRaises(frappe.ValidationError):
			self._create_package("Negative_Min", customer_estimate_min=-10.0)

	def test_duration_calculation_and_required_tags_union(self):
		self._set_ism_settings(estimation_percentile=80.0, buffer_padding_minutes=5)
		tag_lift = self._create_equipment_tag(f"{TEST_PREFIX}Tag_Lift")
		tag_align = self._create_equipment_tag(f"{TEST_PREFIX}Tag_Align")

		# Unknown equipment tag must fail validation
		with self.assertRaises(frappe.ValidationError):
			self._create_package(
				"Unknown_Tag",
				self_serve_required_tags=f"{tag_lift.name}, {TEST_PREFIX}Does_Not_Exist",
			)

		# Valid tags normalize to canonical CSV and spaces are trimmed
		p_override = self._create_package(
			"Override_Dur",
			customer_title="  Four Wheel Alignment  ",
			customer_summary="  Full laser alignment  ",
			self_serve_duration_minutes=90,
			self_serve_required_tags=f" {tag_align.name} , {tag_lift.name} , {tag_align.name} ",
		)
		self.assertEqual(p_override.customer_title, "Four Wheel Alignment")
		self.assertEqual(p_override.customer_summary, "Full laser alignment")
		self.assertEqual(p_override.self_serve_required_tags, f"{tag_align.name}, {tag_lift.name}")

		p_frt_1 = self._create_package(
			"FRT_1",
			standard_labor_hours=1.0,
			self_serve_duration_minutes=0,
			self_serve_required_tags=tag_lift.name,
		)
		p_frt_2 = self._create_package(
			"FRT_2",
			standard_labor_hours=0.5,
			self_serve_duration_minutes=0,
		)

		reqs = compute_packages_booking_requirements(
			[p_override.name, p_frt_1.name, p_frt_2.name],
			vehicle_model="Model 3",
		)
		expected_frt_mins = estimate_total_duration([60.0, 30.0], percentile=0.80)
		self.assertEqual(reqs["duration_minutes"], 90 + expected_frt_mins + 5)
		self.assertEqual(reqs["required_tags"], sorted([tag_align.name, tag_lift.name]))
		self.assertIn("- Four Wheel Alignment", reqs["items_summary"])
		self.assertEqual(len(reqs["booked_packages_snapshot"]), 3)

	def test_zero_internal_field_leakage_and_limits(self):
		pkg = self._create_package(
			"Leak_Check",
			correction_code="10-20-30-40",
			rh_correction_code="10-20-30-41",
			hourly_rate=185.0,
			tesla_manual_url="https://service.tesla.com/docs/secret",
			doc_guid="GUID-1234",
			customer_title="Brake Service",
			customer_summary="Inspect and lubricate calipers.",
			self_serve_sort_order=10,
		)

		cards = list_bookable_packages(search="calipers")
		matching = [c for c in cards if c["package_id"] == pkg.name]
		self.assertEqual(len(matching), 1)
		card = matching[0]

		self.assertEqual(set(card.keys()), PUBLIC_CARD_KEYS)
		for forbidden in FORBIDDEN_INTERNAL_KEYS:
			self.assertNotIn(forbidden, card)

		serialized = json.dumps(card)
		self.assertNotIn("10-20-30-40", serialized)
		self.assertNotIn("GUID-1234", serialized)
		self.assertNotIn("service.tesla.com", serialized)

		# Empty and > MAX_PACKAGES_PER_BOOKING rejected
		with self.assertRaises(frappe.ValidationError):
			compute_packages_booking_requirements([])

		too_many = [f"{TEST_PREFIX}Fake_{i}" for i in range(MAX_PACKAGES_PER_BOOKING + 1)]
		with self.assertRaises(frappe.ValidationError):
			compute_packages_booking_requirements(too_many)

	def test_ism_settings_labor_rate_sigma_and_invalid_package_ids(self):
		# 1. Custom ISM Settings.default_labor_rate affects fallback pricing
		self._set_ism_settings(default_labor_rate=220.0)
		pkg = self._create_package(
			"Settings_Rate_Pkg",
			standard_labor_hours=1.5,
			hourly_rate=0.0,
			service_rate=0.0,
			customer_price_display_mode="Exact Price",
			customer_estimate_min=0.0,
		)
		cards = {c["package_id"]: c for c in list_bookable_packages()}
		self.assertEqual(cards[pkg.name]["price_min"], 330.0)
		self.assertEqual(cards[pkg.name]["price_display_label"], "$330.00 CAD")

		# 2. Custom ISM Settings.estimation_sigma affects FRT-derived duration
		self._set_ism_settings(estimation_sigma=0.60, estimation_percentile=80.0, buffer_padding_minutes=0)
		reqs_high_sigma = compute_packages_booking_requirements([pkg.name])
		expected_high = estimate_total_duration([90.0], sigma=0.60, percentile=0.80)
		self.assertEqual(reqs_high_sigma["duration_minutes"], expected_high)

		self._set_ism_settings(estimation_sigma=0.10, estimation_percentile=80.0, buffer_padding_minutes=0)
		reqs_low_sigma = compute_packages_booking_requirements([pkg.name])
		expected_low = estimate_total_duration([90.0], sigma=0.10, percentile=0.80)
		self.assertEqual(reqs_low_sigma["duration_minutes"], expected_low)
		self.assertGreater(reqs_high_sigma["duration_minutes"], reqs_low_sigma["duration_minutes"])

		# 3. Invalid package_ids types raise frappe.ValidationError
		for bad_input in ({}, False, 0, 1.5, [], None, "", "[]", "{}", [123]):
			with self.assertRaises(frappe.ValidationError):
				compute_packages_booking_requirements(bad_input)

