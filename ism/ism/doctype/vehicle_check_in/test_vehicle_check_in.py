# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import unittest
import frappe
from ism.ism.doctype.vehicle_check_in.vehicle_check_in import (
	check_out_vehicle,
	get_vehicle_customers,
	make_quotation,
)


class TestVehicleCheckin(unittest.TestCase):
	def setUp(self):
		self.vin = "5YJ3E1EBXNF123456"
		if not frappe.db.exists("Customer", "_Test Checkin Customer"):
			self.customer = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": "_Test Checkin Customer",
				"customer_type": "Individual",
			}).insert(ignore_permissions=True)
		else:
			self.customer = frappe.get_doc("Customer", "_Test Checkin Customer")

		if not frappe.db.exists("Repair Vehicle", self.vin):
			self.vehicle = frappe.get_doc({
				"doctype": "Repair Vehicle",
				"vin": self.vin,
				"customer": self.customer.name,
				"license_plate": "TEST-INTAKE",
			}).insert(ignore_permissions=True)
		else:
			self.vehicle = frappe.get_doc("Repair Vehicle", self.vin)

	def tearDown(self):
		frappe.db.rollback()

	def test_vehicle_check_in_creation_and_defaults(self):
		check_in = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 25400,
			"customer_concern": "Squeaking sound from front brakes",
			"check_in_date": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)

		self.assertTrue(check_in.name.startswith("CHK-IN-"))
		self.assertEqual(check_in.status, "Checked In")
		self.assertIsNone(check_in.checkout_date)
		self.assertFalse(hasattr(check_in, "project"))

	def test_negative_mileage_rejected(self):
		check_in = frappe.new_doc("Vehicle Check-in")
		check_in.customer = self.customer.name
		check_in.vehicle = self.vehicle.name
		check_in.intake_mileage = -50
		self.assertRaises(frappe.ValidationError, check_in.insert)

	def test_make_quotation_mapping(self):
		check_in = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 31200,
			"customer_concern": "Annual maintenance inspection",
			"check_in_date": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)

		quotation = make_quotation(check_in.name)
		self.assertEqual(quotation.party_name, self.customer.name)
		self.assertEqual(quotation.custom_vehicle, self.vehicle.name)
		self.assertEqual(quotation.custom_vehicle_check_in, check_in.name)
		self.assertEqual(quotation.custom_odometer_in, 31200)
		self.assertEqual(quotation.custom_customer_concern, "Annual maintenance inspection")

	def test_check_out_vehicle(self):
		check_in = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 31500,
			"check_in_date": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)

		updated = check_out_vehicle(check_in.name)
		self.assertEqual(updated.status, "Checked Out")
		self.assertIsNotNone(updated.checkout_date)

	def test_check_in_links_customer_to_vehicle(self):
		# Create a secondary customer (e.g., spouse)
		spouse = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": "_Spouse Dropoff",
			"customer_type": "Individual",
		}).insert(ignore_permissions=True)

		# Check-in with the spouse as the customer
		frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": spouse.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 32000,
			"check_in_date": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)

		# Verify that the vehicle now has both customers linked
		linked_customers = get_vehicle_customers(self.vehicle.name)
		customer_names = [c["customer"] for c in linked_customers]
		self.assertIn(self.customer.name, customer_names)
		self.assertIn(spouse.name, customer_names)
		self.assertGreaterEqual(len(customer_names), 2)
