# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import unittest
import frappe

TEST_PREFIX = "_TEST_ISM_"


class ISMTestCase(unittest.TestCase):
	"""Base test class for ISM that tracks and deletes all generated test records."""

	def setUp(self):
		self._tracked_docs = []
		self.cleanup_residual_test_data()

	def tearDown(self):
		self.cleanup_tracked_docs()
		self.cleanup_residual_test_data()
		frappe.db.commit()

	def track(self, doc):
		"""Track a document for automatic deletion during tearDown."""
		if hasattr(doc, "doctype") and hasattr(doc, "name"):
			self._tracked_docs.append((doc.doctype, doc.name))
		return doc

	def create_test_customer(self, customer_name=None, **kwargs):
		name = customer_name or f"{TEST_PREFIX}Cust_{frappe.generate_hash(length=8)}"
		if frappe.db.exists("Customer", name):
			frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Individual",
			**kwargs,
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def create_test_vehicle(self, vin="5YJ3E1EBXNF123456", customer=None, **kwargs):
		if frappe.db.exists("Repair Vehicle", vin):
			frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "Repair Vehicle",
			"vin": vin,
			"customer": customer,
			**kwargs,
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def create_test_check_in(self, vehicle, customer, intake_mileage=10000, **kwargs):
		doc = frappe.get_doc({
			"doctype": "Vehicle Check-in",
			"vehicle": vehicle,
			"customer": customer,
			"intake_mileage": intake_mileage,
			"check_in_date": frappe.utils.now_datetime(),
			**kwargs,
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def create_test_quotation(self, customer, vehicle=None, check_in=None, items=None, **kwargs):
		item_code = self.get_or_create_test_item()
		doc = frappe.get_doc({
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": customer,
			"custom_vehicle": vehicle,
			"custom_vehicle_check_in": check_in,
			"items": items or [{"item_code": item_code, "qty": 1, "rate": 100.0}],
			**kwargs,
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def create_test_repair_order(self, customer, vehicle, odometer_in=10000, **kwargs):
		doc = frappe.get_doc({
			"doctype": "Repair Order",
			"customer": customer,
			"vehicle": vehicle,
			"odometer_in": odometer_in,
			**kwargs,
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def create_test_item(self, item_code, is_stock_item=0, standard_rate=0.0, **kwargs):
		if frappe.db.exists("Item", item_code):
			frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": "Services" if not is_stock_item else "All Item Groups",
			"is_stock_item": is_stock_item,
			"standard_rate": standard_rate,
			**kwargs,
		}).insert(ignore_permissions=True)
		return self.track(doc)

	def get_or_create_test_item(self, item_code=f"{TEST_PREFIX}Labor_Item"):
		if not frappe.db.exists("Item", item_code):
			doc = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": "Services",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)
			self.track(doc)
		return item_code

	def cleanup_tracked_docs(self):
		"""Delete tracked documents in reverse dependency order."""
		while self._tracked_docs:
			doctype, name = self._tracked_docs.pop()
			if frappe.db.exists(doctype, name):
				try:
					doc = frappe.get_doc(doctype, name)
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
				except Exception:
					pass

	@classmethod
	def cleanup_residual_test_data(cls):
		"""Purge any leftover records matching test prefixes."""
		from ism.tests.cleanup import cleanup_all_test_data
		cleanup_all_test_data(verbose=False)
