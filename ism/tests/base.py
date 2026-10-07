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

	def get_or_create_test_item(self, item_code=f"{TEST_PREFIX}Labor_Item"):
		if not frappe.db.exists("Item", item_code):
			doc = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
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
		# 1. Quotations
		test_qtns = frappe.get_all(
			"Quotation",
			filters=[["party_name", "like", f"{TEST_PREFIX}%"]],
			pluck="name",
		)
		for qtn in test_qtns:
			try:
				doc = frappe.get_doc("Quotation", qtn)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Quotation", qtn, force=True, ignore_permissions=True)
			except Exception:
				pass

		# 2. Vehicle Check-ins
		test_vcis = frappe.get_all(
			"Vehicle Check-in",
			filters=[["customer", "like", f"{TEST_PREFIX}%"]],
			pluck="name",
		)
		for vci in test_vcis:
			try:
				frappe.delete_doc("Vehicle Check-in", vci, force=True, ignore_permissions=True)
			except Exception:
				pass

		# 3. Test Repair Vehicles
		for vin in ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456"]:
			if frappe.db.exists("Repair Vehicle", vin):
				try:
					frappe.delete_doc("Repair Vehicle", vin, force=True, ignore_permissions=True)
				except Exception:
					pass

		# 4. Customers with test prefix
		test_customers = frappe.get_all(
			"Customer",
			filters=[["name", "like", f"{TEST_PREFIX}%"]],
			pluck="name",
		)
		for cust in test_customers:
			try:
				frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True)
			except Exception:
				pass

		# Also clean legacy test customers created by earlier test iterations
		legacy_test_names = [
			"_Test Customer Vehicle", "_Husband Customer", "_Wife Customer",
			"_Test Checkin Customer", "_Spouse Dropoff",
			"_Test Quotation Customer", "_Spouse Quotation",
		]
		for name in legacy_test_names:
			if frappe.db.exists("Customer", name):
				try:
					frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
				except Exception:
					pass

		# 5. Test items
		test_items = frappe.get_all(
			"Item",
			filters=[["name", "like", f"{TEST_PREFIX}%"]],
			pluck="name",
		)
		for item in test_items:
			try:
				frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
			except Exception:
				pass
		if frappe.db.exists("Item", "_Test Labor Item"):
			try:
				frappe.delete_doc("Item", "_Test Labor Item", force=True, ignore_permissions=True)
			except Exception:
				pass
