# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import unittest
import frappe
from ism.api.quotation import get_active_checked_in_vehicles
from ism.ism.doctype.vehicle_check_in.vehicle_check_in import check_out_vehicle


class TestQuotationWorkflow(unittest.TestCase):
	def setUp(self):
		self.vin = "5YJ3E1EBXNF123456"
		if not frappe.db.exists("Customer", "_Test Quotation Customer"):
			self.customer = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": "_Test Quotation Customer",
				"customer_type": "Individual",
			}).insert(ignore_permissions=True)
		else:
			self.customer = frappe.get_doc("Customer", "_Test Quotation Customer")

		if not frappe.db.exists("Repair Vehicle", self.vin):
			self.vehicle = frappe.get_doc({
				"doctype": "Repair Vehicle",
				"vin": self.vin,
				"customer": self.customer.name,
				"license_plate": "TEST-QUOTE",
			}).insert(ignore_permissions=True)
		else:
			self.vehicle = frappe.get_doc("Repair Vehicle", self.vin)

		if not frappe.db.exists("Item", "_Test Labor Item"):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": "_Test Labor Item",
				"item_group": "Services",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _create_quotation(self, check_in_name=None, customer_name=None):
		return frappe.get_doc({
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": customer_name or self.customer.name,
			"custom_vehicle": self.vehicle.name,
			"custom_vehicle_check_in": check_in_name,
			"items": [
				{
					"item_code": "_Test Labor Item",
					"qty": 1,
					"rate": 150.0,
				}
			],
		})

	def test_submission_blocked_without_active_check_in(self):
		existing_check_ins = frappe.get_all("Vehicle Check-in", filters={"vehicle": self.vehicle.name}, pluck="name")
		for ci in existing_check_ins:
			frappe.delete_doc("Vehicle Check-in", ci, force=1)

		q = self._create_quotation().insert(ignore_permissions=True)
		self.assertRaises(frappe.ValidationError, q.submit)

	def test_submission_succeeds_with_active_check_in(self):
		check_in = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 14200,
			"check_in_date": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)

		q = self._create_quotation(check_in.name).insert(ignore_permissions=True)
		q.submit()
		self.assertEqual(q.docstatus, 1)
		self.assertEqual(q.custom_vehicle_check_in, check_in.name)

	def test_submission_blocked_when_checked_out(self):
		check_in = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": self.customer.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 14500,
			"check_in_date": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)

		check_out_vehicle(check_in.name)

		q = self._create_quotation(check_in.name).insert(ignore_permissions=True)
		self.assertRaises(frappe.ValidationError, q.submit)

	def test_active_vehicle_query_for_secondary_customer(self):
		spouse = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": "_Spouse Quotation",
			"customer_type": "Individual",
		}).insert(ignore_permissions=True)

		check_in = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"customer": spouse.name,
			"vehicle": self.vehicle.name,
			"intake_mileage": 14900,
			"check_in_date": frappe.utils.now_datetime(),
		}).insert(ignore_permissions=True)

		# Search active vehicles for spouse
		results = get_active_checked_in_vehicles("Repair Vehicle", "", "name", 0, 20, {"customer": spouse.name})
		matched_vins = [row[0] for row in results]
		self.assertIn(self.vehicle.name, matched_vins)
