# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.ism.doctype.repair_vehicle.repair_vehicle import compute_vin_check_digit
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
