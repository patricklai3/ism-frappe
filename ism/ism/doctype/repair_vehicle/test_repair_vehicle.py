# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import unittest
import frappe
from ism.ism.doctype.repair_vehicle.repair_vehicle import compute_vin_check_digit


class TestRepairVehicle(unittest.TestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_vin_check_digit_calculation(self):
		# For 5YJ3E1EB_NF123456, the calculated 9th check digit is 'X'
		vin = "5YJ3E1EBXNF123456"
		check_digit = compute_vin_check_digit(vin)
		self.assertEqual(check_digit, "X")

	def test_non_tesla_vin_rejected(self):
		# 1HGCR2F83HA123456 is a Honda VIN
		vehicle = frappe.new_doc("Repair Vehicle")
		vehicle.vin = "1HGCR2F83HA123456"
		vehicle.customer = "Test Customer"
		self.assertRaises(frappe.ValidationError, vehicle.insert)

	def test_valid_tesla_vin_decoding(self):
		test_vin = "5YJ3E1EBXNF123456"
		if frappe.db.exists("Repair Vehicle", test_vin):
			frappe.delete_doc("Repair Vehicle", test_vin, force=1)

		if not frappe.db.exists("Customer", "_Test Customer Vehicle"):
			customer = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": "_Test Customer Vehicle",
				"customer_type": "Individual",
			}).insert(ignore_permissions=True)
		else:
			customer = frappe.get_doc("Customer", "_Test Customer Vehicle")

		vehicle = frappe.get_doc({
			"doctype": "Repair Vehicle",
			"vin": test_vin,
			"customer": customer.name,
			"license_plate": "TEST-PLATE",
			"color": "Midnight Silver",
		}).insert(ignore_permissions=True)

		self.assertEqual(vehicle.name, test_vin)
		self.assertEqual(vehicle.is_valid_vin, 1)
		self.assertEqual(vehicle.model, "Model 3")
		self.assertEqual(vehicle.model_year, "2022")
		self.assertEqual(vehicle.drivetrain, "AWD")
		self.assertTrue(any(row.customer == customer.name and row.is_primary for row in vehicle.customers))

	def test_multiple_customers_linked_to_vehicle(self):
		test_vin = "5YJ3E1EBXNF123456"
		if frappe.db.exists("Repair Vehicle", test_vin):
			frappe.delete_doc("Repair Vehicle", test_vin, force=1)

		cust1 = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": "_Husband Customer",
			"customer_type": "Individual",
		}).insert(ignore_permissions=True)

		cust2 = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": "_Wife Customer",
			"customer_type": "Individual",
		}).insert(ignore_permissions=True)

		vehicle = frappe.get_doc({
			"doctype": "Repair Vehicle",
			"vin": test_vin,
			"customer": cust1.name,
		}).insert(ignore_permissions=True)

		# Add second customer (e.g., spouse)
		vehicle.add_customer(cust2.name, relationship="Spouse")
		vehicle.reload()

		linked_customers = [row.customer for row in vehicle.customers]
		self.assertIn(cust1.name, linked_customers)
		self.assertIn(cust2.name, linked_customers)
		self.assertEqual(len(linked_customers), 2)
		self.assertEqual(vehicle.customer, cust1.name)
