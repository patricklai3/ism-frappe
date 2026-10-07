# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.api.quotation import get_active_checked_in_vehicles
from ism.ism.doctype.vehicle_check_in.vehicle_check_in import check_out_vehicle
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestQuotationWorkflow(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.vin = "5YJ3E1EBXNF123456"
		self.customer = self.create_test_customer(f"{TEST_PREFIX}Quote_Cust")
		self.vehicle = self.create_test_vehicle(
			vin=self.vin,
			customer=self.customer.name,
			license_plate="TEST-QUOTE",
		)

	def test_submission_blocked_without_active_check_in(self):
		q = self.create_test_quotation(
			customer=self.customer.name,
			vehicle=self.vehicle.name,
		)
		self.assertRaises(frappe.ValidationError, q.submit)

	def test_submission_succeeds_with_active_check_in(self):
		check_in = self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=14200,
		)

		q = self.create_test_quotation(
			customer=self.customer.name,
			vehicle=self.vehicle.name,
			check_in=check_in.name,
		)
		q.submit()
		self.assertEqual(q.docstatus, 1)
		self.assertEqual(q.custom_vehicle_check_in, check_in.name)

	def test_submission_blocked_when_checked_out(self):
		check_in = self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=self.customer.name,
			intake_mileage=14500,
		)

		check_out_vehicle(check_in.name)

		q = self.create_test_quotation(
			customer=self.customer.name,
			vehicle=self.vehicle.name,
			check_in=check_in.name,
		)
		self.assertRaises(frappe.ValidationError, q.submit)

	def test_active_vehicle_query_for_secondary_customer(self):
		spouse = self.create_test_customer(f"{TEST_PREFIX}Spouse_Quote")

		self.create_test_check_in(
			vehicle=self.vehicle.name,
			customer=spouse.name,
			intake_mileage=14900,
		)

		results = get_active_checked_in_vehicles("Repair Vehicle", "", "name", 0, 20, {"customer": spouse.name})
		matched_vins = [row[0] for row in results]
		self.assertIn(self.vehicle.name, matched_vins)
