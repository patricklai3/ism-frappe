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

	def test_2024_to_2026_model_sx_hw4_and_model_y_juniper_decoding(self):
		"""Verify 2024-2026 Model S/X return HW4 regardless of sequence and Model Y Juniper drive codes decode."""
		from ism.ism.doctype.repair_vehicle.vin_decoder import get_hardware_generation

		# Direct get_hardware_generation checks for 2024-2026 Model S & X (even with low sequence numbers)
		for model_code in ("S", "X"):
			for year_code in ("R", "S", "T"):  # 2024, 2025, 2026
				self.assertEqual(get_hardware_generation(model_code, 1000, "F", year_code), {"t": "HW4"})

		# End-to-end VIN decoding for 2024-2026 Model S & Model X
		for wmi, model_code, body, drive, expected_model in (
			("5YJ", "S", "A", "5", "Model S"),
			("7SA", "X", "C", "6", "Model X"),
		):
			for year_code, expected_year in (("R", "2024"), ("S", "2025"), ("T", "2026")):
				template = f"{wmi}{model_code}{body}1E{drive}0{year_code}F001234"
				vin = f"{template[:8]}{compute_vin_check_digit(template)}{template[9:]}"
				decoded = decode_tesla_vin(vin)
				self.assertEqual(decoded["is_valid_vin"], 1)
				self.assertEqual(decoded["model"], expected_model)
				self.assertEqual(decoded["model_year"], expected_year)
				self.assertEqual(decoded["autopilot_hardware"], "HW4")

		# 2025-2026 Model Y Juniper drive unit codes (RWD + AWD, including T)
		juniper_cases = (
			("D", "S", "2025", "RWD", "Standard Range"),
			("R", "S", "2025", "RWD", "Standard Range"),
			("S", "T", "2026", "RWD", "Standard Range"),
			("E", "S", "2025", "AWD", "Long Range"),
			("F", "T", "2026", "AWD", "Performance"),
			("K", "S", "2025", "AWD", "Long Range"),
			("L", "T", "2026", "AWD", "Performance"),
			("T", "T", "2026", "AWD", "Performance"),
		)
		for drive_code, year_code, expected_year, expected_dt, expected_trim in juniper_cases:
			vin = f"7SAYGDE{drive_code}0{year_code}A012345"
			vin = f"{vin[:8]}{compute_vin_check_digit(vin)}{vin[9:]}"
			decoded = decode_tesla_vin(vin)
			self.assertEqual(decoded["is_valid_vin"], 1)
			self.assertEqual(decoded["model"], "Model Y")
			self.assertEqual(decoded["model_year"], expected_year)
			self.assertEqual(decoded["drivetrain"], expected_dt)
			self.assertEqual(decoded["trim"], expected_trim)
			self.assertEqual(decoded["autopilot_hardware"], "HW4")

	def test_vehicle_generation_persistence_and_manufactured_month_redecode(self):
		"""Verify RepairVehicle persists vehicle_generation + recommended_manual_url and re-decodes on manufactured_month."""
		from ism.api.tesla import search_vehicles

		# 2021 Model S VIN (5YJSA1E5...) splits Classic (Apr 2016 - Jan 2021) vs Palladium (Feb 2021 - May 2025)
		tpl_ms = "5YJSA1E50MF000123"
		vin_ms = f"{tpl_ms[:8]}{compute_vin_check_digit(tpl_ms)}{tpl_ms[9:]}"

		ambig = decode_tesla_vin(vin_ms)
		self.assertTrue(ambig["generation_ambiguous"])
		self.assertEqual(len(ambig["candidate_generations"]), 2)

		cust = self.create_test_customer(customer_name=f"{TEST_PREFIX}GenCust")
		veh = frappe.get_doc({
			"doctype": "Repair Vehicle",
			"vin": vin_ms,
			"customer": cust.name,
			"manufactured_month": "01/21",
		}).insert(ignore_permissions=True)
		self.track(veh)
		self.assertEqual(veh.vehicle_generation, "Model S Apr 2016 - Jan 2021")
		self.assertEqual(veh.recommended_manual_url, "https://service.tesla.com/docs/ModelS/ServiceManual/en-us/")

		veh.manufactured_month = "03/21"
		veh.save(ignore_permissions=True)
		veh.reload()
		self.assertEqual(veh.vehicle_generation, "Model S Feb 2021 - May 2025")
		self.assertEqual(veh.recommended_manual_url, "https://service.tesla.com/docs/ModelS/ServiceManual/Palladium/en-us/")

		# Search by generation finds the updated vehicle
		hits = search_vehicles(generation="Model S Feb 2021 - May 2025")
		self.assertIn(veh.name, [h["name"] for h in hits])
		miss = search_vehicles(generation="Model S Apr 2016 - Jan 2021")
		self.assertNotIn(veh.name, [h["name"] for h in miss])

		# 2025 Model Y non-Juniper drive code (G) re-decodes between Jan 2025 legacy vs Feb 2025 Juniper
		tpl_my = "7SAYGDEG0SF000456"
		vin_my = f"{tpl_my[:8]}{compute_vin_check_digit(tpl_my)}{tpl_my[9:]}"
		jan_y = decode_tesla_vin(vin_my, manufactured_month="01/25")
		feb_y = decode_tesla_vin(vin_my, manufactured_month="02/25")
		self.assertEqual(jan_y["vehicle_generation"], "Model Y Jan 2020 - Jan 2025")
		self.assertEqual(jan_y["recommended_manual_url"], "https://service.tesla.com/docs/ModelY/ServiceManual/en-us/")
		self.assertEqual(feb_y["vehicle_generation"], "Model Y Feb 2025")
		self.assertEqual(feb_y["recommended_manual_url"], "https://service.tesla.com/docs/ModelY/ServiceManual/2025/en-us/")

