# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import flt
from ism.ism.doctype.repair_vehicle.repair_vehicle import (
	build_vehicle_intelligence,
	build_vehicle_search_results,
	compute_vin_check_digit,
	decode_tesla_vin,
)
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestRepairVehicle(ISMTestCase):
	def test_vin_check_digit_calculation(self):
		vin = "5YJ3E1EBXNF123456"
		check_digit = compute_vin_check_digit(vin)
		self.assertEqual(check_digit, "X")

	def test_non_tesla_vin_rejected(self):
		vehicle = frappe.new_doc("Repair Vehicle")
		vehicle.vin = "1HGCR2F83HA123456"
		self.assertRaises(frappe.ValidationError, vehicle.insert)

	def test_valid_tesla_vin_decoding(self):
		customer = self.create_test_customer(f"{TEST_PREFIX}Owner_1")
		vehicle = self.create_test_vehicle(
			vin="5YJ3E1EBXNF123456",
			customer=customer.name,
			license_plate="TEST-PLATE",
			color="Midnight Silver",
		)

		self.assertEqual(vehicle.name, "5YJ3E1EBXNF123456")
		self.assertEqual(vehicle.is_valid_vin, 1)
		self.assertEqual(vehicle.model, "Model 3")
		self.assertEqual(vehicle.model_year, "2022")
		self.assertEqual(vehicle.drivetrain, "AWD")
		self.assertTrue(any(row.customer == customer.name and row.is_primary for row in vehicle.customers))

	def test_multiple_customers_linked_to_vehicle(self):
		cust1 = self.create_test_customer(f"{TEST_PREFIX}Husband")
		cust2 = self.create_test_customer(f"{TEST_PREFIX}Wife")

		vehicle = self.create_test_vehicle(
			vin="5YJ3E1EBXNF123456",
			customer=cust1.name,
		)

		vehicle.add_customer(cust2.name, relationship="Spouse")
		vehicle.reload()

		linked_customers = [row.customer for row in vehicle.customers]
		self.assertIn(cust1.name, linked_customers)
		self.assertIn(cust2.name, linked_customers)
		self.assertEqual(len(linked_customers), 2)
		self.assertEqual(vehicle.customer, cust1.name)

	def test_lowercase_vin_normalized_and_named_uppercase(self):
		cust = self.create_test_customer(f"{TEST_PREFIX}Owner_Lower")
		vehicle = self.create_test_vehicle(
			vin="5yj3e1ebxnf123456",
			customer=cust.name,
		)
		self.assertEqual(vehicle.name, "5YJ3E1EBXNF123456")
		self.assertEqual(vehicle.vin, "5YJ3E1EBXNF123456")

	def test_child_table_primary_sync(self):
		cust1 = self.create_test_customer(f"{TEST_PREFIX}Cust_A")
		cust2 = self.create_test_customer(f"{TEST_PREFIX}Cust_B")

		vehicle = self.create_test_vehicle(
			vin="5YJ3E1EBXNF123456",
			customer=cust1.name,
		)
		vehicle.add_customer(cust2.name)
		vehicle.reload()

		# User switches primary in the child table
		for row in vehicle.customers:
			row.is_primary = 1 if row.customer == cust2.name else 0
		vehicle.save(ignore_permissions=True)
		vehicle.reload()

		self.assertEqual(vehicle.customer, cust2.name)
		self.assertTrue(any(r.customer == cust2.name and r.is_primary for r in vehicle.customers))

	def test_offline_decode_tesla_vin_variants_and_errors(self):
		"""Verify pure offline VIN decoder handles valid, invalid check digit, short, and non-Tesla VINs."""
		valid = decode_tesla_vin("5yj3e1ebxnf123456")
		self.assertTrue(valid["is_valid_length"])
		self.assertTrue(valid["is_tesla_wmi"])
		self.assertEqual(valid["is_valid_vin"], 1)
		self.assertEqual(valid["model"], "Model 3")
		self.assertEqual(valid["model_year"], "2022")
		self.assertIn("Model3", valid["recommended_manual_url"])

		bad_check = decode_tesla_vin("5YJ3E1EB0NF123456")
		self.assertTrue(bad_check["is_tesla_wmi"])
		self.assertEqual(bad_check["is_valid_vin"], 0)

		non_tesla = decode_tesla_vin("1HGCR2F83HA123456")
		self.assertFalse(non_tesla["is_tesla_wmi"])
		self.assertIn("not a recognized Tesla manufacturer code", non_tesla["error"])

		short_vin = decode_tesla_vin("5YJ3E1EB")
		self.assertFalse(short_vin["is_valid_length"])
		self.assertIn("17 characters", short_vin["error"])

	def test_vehicle_search_and_intelligence_summary(self):
		"""Verify indexed vehicle search, multi-customer lookup, service history, and deferred summary."""
		cust1 = self.create_test_customer(f"{TEST_PREFIX}IntelPrimary")
		cust2 = self.create_test_customer(f"{TEST_PREFIX}IntelSecondary")
		vehicle = self.create_test_vehicle(
			vin="5YJ3E1EBXNF556677",
			customer=cust1.name,
			license_plate="EV 8821",
		)
		vehicle.add_customer(cust2.name, relationship="Co-Owner")
		vehicle.append("deferred_services", {
			"job_title": "Front Control Arm Bushing",
			"estimated_amount": 650.0,
			"status": "Open Recommendation",
			"safety_hazard": 1,
		})
		vehicle.append("deferred_services", {
			"job_title": "Wiper Blades",
			"estimated_amount": 75.0,
			"status": "Completed Later",
			"safety_hazard": 0,
		})
		vehicle.save(ignore_permissions=True)

		ro = self.create_test_repair_order(customer=cust1.name, vehicle=vehicle.name)
		ro.append("services", {
			"service_title": "Four-Wheel Alignment",
			"status": "Estimating",
			"labor_hours": 1.0,
			"labor_rate": 150.0,
			"labor_amount": 150.0,
			"total_amount": 150.0,
		})
		ro.save(ignore_permissions=True)

		by_vin = build_vehicle_search_results(query="556677")
		self.assertTrue(any(r["name"] == vehicle.name for r in by_vin))

		by_co_owner = build_vehicle_search_results(customer=cust2.name)
		self.assertTrue(any(r["name"] == vehicle.name for r in by_co_owner))

		by_plate = build_vehicle_search_results(query="EV 8821")
		self.assertTrue(any(r["name"] == vehicle.name for r in by_plate))

		intel = build_vehicle_intelligence("EV8821")
		self.assertEqual(intel["vehicle"]["name"], vehicle.name)
		self.assertEqual(len(intel["owners"]), 2)
		self.assertEqual(len(intel["service_history"]), 1)
		self.assertEqual(intel["service_history"][0]["name"], ro.name)
		self.assertEqual(flt(intel["service_history"][0]["labor_total"]), 150.0)
		self.assertEqual(flt(intel["service_history"][0]["parts_total"]), 0.0)
		self.assertEqual(intel["service_history"][0]["services"][0]["service_title"], "Four-Wheel Alignment")
		self.assertEqual(intel["deferred_services"]["open_count"], 1)
		self.assertEqual(intel["deferred_services"]["total_count"], 2)
		self.assertEqual(intel["deferred_services"]["safety_hazard_count"], 1)
		self.assertEqual(flt(intel["deferred_services"]["open_estimated_total"]), 650.0)
