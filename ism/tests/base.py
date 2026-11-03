# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import time
import unittest
import frappe

TEST_PREFIX = "_TEST_ISM_"
TEST_VINS = ["5YJ3E1EBXNF123456", "1HGCR2F83HA123456"]

STATIC_FIXTURE_CUSTOMERS = {"Test Customer", "Prestiga-Biz", "_TEST_Customer"}
STATIC_FIXTURE_VEHICLES = {"5YJSA1E26CF000001", "LRW3F7FS2NC5xxxxx", "LRW3F7ET2RC1xxxxx"}


class ISMTestCase(unittest.TestCase):
	"""Base test class for ISM that tracks and deletes all generated test records."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Clean residual test data once at the class boundary before running tests
		cls.cleanup_residual_test_data()

	@classmethod
	def tearDownClass(cls):
		# Clean residual test data once at the class boundary after all tests finish
		cls.cleanup_residual_test_data()
		super().tearDownClass()

	def setUp(self):
		# Discard any lingering transaction snapshots from prior tests or background jobs
		frappe.db.rollback()
		super().setUp()
		self._tracked_docs = []

	def tearDown(self):
		# Phase 1: Discard uncommitted transactional changes and release table/row locks
		frappe.db.rollback()

		# Phase 2: If any tracked documents were committed mid-test, explicitly purge them
		self.cleanup_tracked_docs()

		# Phase 3: Guarantee all read locks and transaction snapshots are completely closed
		frappe.db.rollback()

		super().tearDown()

	def track(self, doc, name=None):
		"""Track a document or (doctype, name) tuple for automatic deletion during tearDown."""
		if name and isinstance(doc, str):
			self._tracked_docs.append((doc, name))
		elif isinstance(doc, (list, tuple)) and len(doc) == 2:
			self._tracked_docs.append((doc[0], doc[1]))
		elif hasattr(doc, "doctype") and hasattr(doc, "name"):
			self._tracked_docs.append((doc.doctype, doc.name))
		return doc

	def create_test_customer(self, customer_name=None, **kwargs):
		for attempt in range(3):
			try:
				name = customer_name or f"{TEST_PREFIX}Cust_{frappe.generate_hash(length=8)}"
				if name in STATIC_FIXTURE_CUSTOMERS:
					return frappe.get_doc("Customer", name)
				if frappe.db.exists("Customer", name):
					frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
				doc = frappe.get_doc({
					"doctype": "Customer",
					"customer_name": name,
					"customer_type": "Individual",
					**kwargs,
				}).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise

	def create_test_vehicle(self, vin=None, customer=None, **kwargs):
		for attempt in range(3):
			try:
				if vin in STATIC_FIXTURE_VEHICLES:
					veh = frappe.get_doc("Repair Vehicle", vin)
					if customer and veh.customer != customer:
						veh.customer = customer
						veh.save(ignore_permissions=True)
					return veh
				vin_val = vin or f"5YJ3E1EBXNF{frappe.generate_hash(length=6).upper()}"
				if frappe.db.exists("Repair Vehicle", vin_val):
					frappe.delete_doc("Repair Vehicle", vin_val, force=True, ignore_permissions=True)
				doc = frappe.get_doc({
					"doctype": "Repair Vehicle",
					"vin": vin_val,
					"customer": customer,
					**kwargs,
				}).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise

	def create_test_quotation(self, customer, vehicle=None, items=None, **kwargs):
		item_code = self.get_or_create_test_item()
		doc_dict = {
			"doctype": "Quotation",
			"quotation_to": "Customer",
			"party_name": customer,
			"items": items or [{"item_code": item_code, "qty": 1, "rate": 100.0}],
			**kwargs,
		}
		if vehicle and frappe.db.has_column("Quotation", "custom_vehicle"):
			doc_dict["custom_vehicle"] = vehicle
		for attempt in range(3):
			try:
				doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise

	def create_test_repair_order(self, customer, vehicle, odometer_in=10000, **kwargs):
		for attempt in range(3):
			try:
				doc = frappe.get_doc({
					"doctype": "Repair Order",
					"customer": customer,
					"vehicle": vehicle,
					"odometer_in": odometer_in,
					**kwargs,
				}).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise

	def create_test_schedule_entry(self, **kwargs):
		for attempt in range(3):
			try:
				params = kwargs.copy()
				data = {
					"doctype": "Schedule Entry",
					"scheduled_date": params.pop("scheduled_date", frappe.utils.today()),
					"scheduled_time": params.pop("scheduled_time", "09:00:00"),
					"estimated_duration": params.pop("estimated_duration", 60),
					"entry_type": params.pop("entry_type", "Diagnostic"),
					"status": params.pop("status", "Scheduled"),
					**params,
				}
				doc = frappe.get_doc(data).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise

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
		"""Delete tracked documents in reverse dependency order if they persist in the database."""
		if not hasattr(self, "_tracked_docs") or not self._tracked_docs:
			return

		committed_docs = []
		while self._tracked_docs:
			doctype, name = self._tracked_docs.pop()
			if frappe.db.exists(doctype, name):
				committed_docs.append((doctype, name))

		if not committed_docs:
			frappe.db.rollback()
			return

		# Purge Schedule Entry records before parent docs to prevent link constraints
		committed_docs.sort(key=lambda item: 0 if item[0] == "Schedule Entry" else 1)

		for doctype, name in committed_docs:
			if not frappe.db.exists(doctype, name):
				continue
			if doctype == "Customer" and name in STATIC_FIXTURE_CUSTOMERS:
				continue
			if doctype == "Repair Vehicle" and name in STATIC_FIXTURE_VEHICLES:
				continue
			try:
				doc = frappe.get_doc(doctype, name)
				if doctype == "Repair Order":
					self._teardown_repair_order(doc)
				elif doctype == "Sales Invoice":
					self._teardown_sales_invoice(doc)
				elif doctype == "Payment Entry":
					self._teardown_payment_entry(doc)
				elif doctype == "Customer":
					self._teardown_customer(doc)
				elif doctype == "Service Bay":
					self._teardown_service_bay(doc)
				elif doctype == "Schedule Entry Type":
					self._teardown_schedule_entry_type(doc)
				elif doctype == "Schedule Entry":
					frappe.delete_doc("Schedule Entry", name, force=True, ignore_permissions=True)
				else:
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			except Exception:
				pass

		frappe.db.rollback()

	def _teardown_repair_order(self, doc):
		# Clean up any referencing Schedule Entries before deleting Repair Order
		if frappe.db.exists("DocType", "Schedule Entry"):
			linked_entries = frappe.get_all(
				"Schedule Entry",
				filters={"repair_order": doc.name},
				pluck="name",
			)
			for se_name in linked_entries:
				try:
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
				except Exception:
					pass

		si_name = getattr(doc, "sales_invoice", None)
		if si_name and frappe.db.exists("Sales Invoice", si_name):
			self._teardown_sales_invoice(frappe.get_doc("Sales Invoice", si_name))
			doc.db_set("sales_invoice", None)
			doc.reload()
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Repair Order", doc.name, force=True, ignore_permissions=True)

	def _teardown_sales_invoice(self, doc):
		pe_refs = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Sales Invoice", "reference_name": doc.name},
			pluck="parent",
		)
		for pe_name in set(pe_refs):
			if frappe.db.exists("Payment Entry", pe_name):
				self._teardown_payment_entry(frappe.get_doc("Payment Entry", pe_name))
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Sales Invoice", doc.name, force=True, ignore_permissions=True)

	def _teardown_payment_entry(self, doc):
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Payment Entry", doc.name, force=True, ignore_permissions=True)

	def _teardown_customer(self, doc):
		if doc.name in STATIC_FIXTURE_CUSTOMERS:
			return
		# Clean up any referencing Schedule Entries before deleting Customer
		if frappe.db.exists("DocType", "Schedule Entry"):
			linked_entries = frappe.get_all(
				"Schedule Entry",
				filters={"customer": doc.name},
				pluck="name",
			)
			for se_name in linked_entries:
				try:
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
				except Exception:
					pass

		frappe.db.delete("Repair Vehicle Customer", {"customer": doc.name})
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Customer", doc.name, force=True, ignore_permissions=True)

	def _teardown_service_bay(self, doc):
		# Clean up any referencing Schedule Entries before deleting Service Bay
		if frappe.db.exists("DocType", "Schedule Entry"):
			linked_entries = frappe.get_all(
				"Schedule Entry",
				filters={"service_bay": doc.name},
				pluck="name",
			)
			for se_name in linked_entries:
				try:
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.delete_doc("Service Bay", doc.name, force=True, ignore_permissions=True)

	def _teardown_schedule_entry_type(self, doc):
		# Standard types must never be deleted
		if doc.name in {"Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service"}:
			return
		if frappe.db.exists("DocType", "Schedule Entry"):
			linked_entries = frappe.get_all(
				"Schedule Entry",
				filters={"entry_type": doc.name},
				pluck="name",
			)
			for se_name in linked_entries:
				try:
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.delete_doc("Schedule Entry Type", doc.name, force=True, ignore_permissions=True)

	@classmethod
	def cleanup_residual_test_data(cls):
		"""Purge any leftover records matching test prefixes."""
		from ism.tests.cleanup import cleanup_all_test_data
		cleanup_all_test_data(verbose=False)
