# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.ism.doctype.vehicle_check_in.vehicle_check_in import (
	check_out_vehicle,
	get_vehicle_customers,
	make_quotation,
)
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestVehicleCheckin(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.vin = "5YJ3E1EBXNF123456"
		self.customer = self.create_test_customer(f"{TEST_PREFIX}Checkin_Cust")
		self.vehicle = self.create_test_vehicle(
			vin=self.vin,
			customer=self.customer.name,
			license_plate="TEST-INTAKE",
		)

	def test_vehicle_check_in_creation_and_defaults(self):
		check_in = self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=25400,
			customer_concern="Squeaking sound from front brakes",
		)

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
		check_in = self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=31200,
			customer_concern="Annual maintenance inspection",
		)

		quotation = make_quotation(check_in.name)
		self.assertEqual(quotation.party_name, self.customer.name)
		self.assertEqual(quotation.custom_vehicle, self.vehicle.name)
		self.assertEqual(quotation.custom_vehicle_check_in, check_in.name)
		self.assertEqual(quotation.custom_odometer_in, 31200)
		self.assertEqual(quotation.custom_customer_concern, "Annual maintenance inspection")
		self.assertEqual(quotation.status, "Draft")

		# Ensure mapped draft quotation saves without select validation error
		quotation.append("items", {
			"item_code": self.get_or_create_test_item(),
			"qty": 1,
			"rate": 100,
		})
		quotation.insert(ignore_permissions=True)
		self.track(quotation)

	def test_check_out_vehicle(self):
		check_in = self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=31500,
		)

		updated = check_out_vehicle(check_in.name)
		self.assertEqual(updated.status, "Checked Out")
		self.assertIsNotNone(updated.checkout_date)

	def test_check_in_links_customer_to_vehicle(self):
		spouse = self.create_test_customer(f"{TEST_PREFIX}Spouse_Dropoff")

		self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=spouse.name,
			intake_mileage=32000,
		)

		linked_customers = get_vehicle_customers(self.vehicle.name)
		customer_names = [c["customer"] for c in linked_customers]
		self.assertIn(self.customer.name, customer_names)
		self.assertIn(spouse.name, customer_names)
		self.assertGreaterEqual(len(customer_names), 2)

	def test_duplicate_active_check_in_rejected(self):
		self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=33000,
		)
		# Attempting second check-in while first is still "Checked In" must fail
		doc2 = frappe.new_doc("Vehicle Check-in")
		doc2.vehicle = self.vehicle.name
		doc2.customer = self.customer.name
		doc2.intake_mileage = 33050
		self.assertRaises(frappe.ValidationError, doc2.insert)

	def test_repeated_checkout_rejected(self):
		check_in = self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=34000,
		)
		check_out_vehicle(check_in.name)
		# Second checkout on already checked out record must fail
		self.assertRaises(frappe.ValidationError, check_out_vehicle, check_in.name)
