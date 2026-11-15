# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import time
import unittest
import warnings

import frappe

from ism.tests.constants import (
	PRESERVED_CUSTOMERS,
	PRESERVED_PAYMENT_ENTRIES,
	PRESERVED_REPAIR_ORDERS,
	PRESERVED_SALES_INVOICES,
	PRESERVED_VEHICLES,
	STANDARD_SCHEDULE_ENTRY_TYPES,
	STATIC_FIXTURE_BAYS,
	TEST_PREFIX,
	TEST_VINS,
	TEST_VIN_PREFIX,
)

# Backwards-compatible aliases preserved for doctype tests and scripts
STATIC_FIXTURE_CUSTOMERS = PRESERVED_CUSTOMERS
STATIC_FIXTURE_VEHICLES = PRESERVED_VEHICLES

__all__ = [
	"STATIC_FIXTURE_CUSTOMERS",
	"STATIC_FIXTURE_VEHICLES",
	"TEST_PREFIX",
	"TEST_VINS",
	"ISMTestCase",
	"retry_on_deadlock",
]

_TEARDOWN_ORDER = {
	"Schedule Entry": 1,
	"Payment Entry": 2,
	"Purchase Receipt": 3,
	"Purchase Invoice": 4,
	"Sales Invoice": 5,
	"Stock Entry": 6,
	"Purchase Order": 7,
	"Request for Quotation": 8,
	"Sales Order": 9,
	"Quotation": 10,
	"Shop Action Item": 11,
	"Vehicle Check-in": 12,
	"Repair Order": 13,
	"Leave Application": 14,
	"Repair Vehicle": 20,
	"Customer": 21,
	"Supplier": 22,
	"Service Bay": 23,
	"Equipment Tag": 24,
	"Schedule Entry Type": 25,
	"Inspection Template": 26,
	"Service Package": 27,
	"Item": 28,
	"Warehouse": 29,
	"Employee": 30,
	"User": 31,
	"File": 40,
}


def retry_on_deadlock(fn, max_attempts=3):
	"""Execute fn(), retrying briefly on MariaDB/Frappe lock/deadlock errors."""
	for attempt in range(max_attempts):
		try:
			return fn()
		except Exception as e:
			is_deadlock = (
				"1020" in str(e)
				or "Deadlock" in str(e)
				or isinstance(e, frappe.QueryDeadlockError)
			)
			if is_deadlock and attempt < max_attempts - 1:
				time.sleep(0.05 * (attempt + 1))
				continue
			raise


def _cancel_and_delete(doctype, name):
	"""Cancel (if submitted) and forcefully delete an existing document."""
	if not frappe.db.exists(doctype, name):
		return
	try:
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	except frappe.DoesNotExistError:
		pass


class ISMTestCase(unittest.TestCase):
	"""Base test class for ISM that tracks and deletes all generated test records."""

	def setUp(self):
		# Discard any lingering transaction snapshots from prior tests or background jobs
		frappe.db.rollback()
		super().setUp()
		self._tracked_docs = []

	def tearDown(self):
		try:
			# Phase 1: Discard uncommitted transactional changes and release table/row locks
			frappe.db.rollback()

			# Phase 2: Explicitly purge any tracked documents that were committed mid-test
			self.cleanup_tracked_docs()
		finally:
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
		def _create():
			name = customer_name or f"{TEST_PREFIX}Cust_{frappe.generate_hash(length=8)}"
			if name in PRESERVED_CUSTOMERS:
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

		return retry_on_deadlock(_create)

	def create_test_vehicle(self, vin=None, customer=None, **kwargs):
		def _create():
			if vin in PRESERVED_VEHICLES:
				veh = frappe.get_doc("Repair Vehicle", vin)
				if customer and veh.customer != customer:
					veh.customer = customer
					veh.save(ignore_permissions=True)
				return veh
			vin_val = vin or f"{TEST_VIN_PREFIX}{frappe.generate_hash(length=6).upper()}"
			if frappe.db.exists("Repair Vehicle", vin_val):
				frappe.delete_doc("Repair Vehicle", vin_val, force=True, ignore_permissions=True)
			doc = frappe.get_doc({
				"doctype": "Repair Vehicle",
				"vin": vin_val,
				"customer": customer,
				**kwargs,
			}).insert(ignore_permissions=True)
			return self.track(doc)

		return retry_on_deadlock(_create)

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

		def _create():
			doc = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
			return self.track(doc)

		return retry_on_deadlock(_create)

	def create_test_repair_order(self, customer, vehicle, odometer_in=10000, **kwargs):
		def _create():
			doc = frappe.get_doc({
				"doctype": "Repair Order",
				"customer": customer,
				"vehicle": vehicle,
				"odometer_in": odometer_in,
				**kwargs,
			}).insert(ignore_permissions=True)
			return self.track(doc)

		return retry_on_deadlock(_create)

	def create_test_schedule_entry(self, **kwargs):
		def _create():
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

		return retry_on_deadlock(_create)

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
		"""Delete tracked documents in topological reverse-dependency order if persisted."""
		if not hasattr(self, "_tracked_docs") or not self._tracked_docs:
			return

		unique_lifo = list(dict.fromkeys(reversed(self._tracked_docs)))
		self._tracked_docs.clear()

		committed_docs = [
			(doctype, name)
			for doctype, name in unique_lifo
			if doctype and name and frappe.db.exists(doctype, name)
		]
		if not committed_docs:
			frappe.db.rollback()
			return

		# Sort strictly by FK topology (leaves before parents), preserving LIFO order within same rank
		committed_docs.sort(key=lambda item: _TEARDOWN_ORDER.get(item[0], 15))

		errors = []
		try:
			for doctype, name in committed_docs:
				if not frappe.db.exists(doctype, name):
					continue
				if doctype == "Customer" and name in PRESERVED_CUSTOMERS:
					continue
				if doctype == "Repair Vehicle" and name in PRESERVED_VEHICLES:
					continue
				if doctype == "Repair Order" and name in PRESERVED_REPAIR_ORDERS:
					continue
				if doctype == "Sales Invoice" and name in PRESERVED_SALES_INVOICES:
					continue
				if doctype == "Payment Entry" and name in PRESERVED_PAYMENT_ENTRIES:
					continue
				try:
					doc = frappe.get_doc(doctype, name)
					self._dispatch_teardown(doc)
				except frappe.DoesNotExistError:
					continue
				except Exception as err:
					msg = f"{doctype} {name}: {err}"
					errors.append(msg)
					warnings.warn(f"Teardown cleanup error for {msg}", RuntimeWarning, stacklevel=2)
		finally:
			frappe.db.rollback()

		if errors:
			raise AssertionError("Failed to clean up tracked test documents: " + "; ".join(errors))

	def _dispatch_teardown(self, doc):
		handlers = {
			"Repair Order": self._teardown_repair_order,
			"Sales Invoice": self._teardown_sales_invoice,
			"Purchase Invoice": self._teardown_purchase_invoice,
			"Purchase Receipt": self._teardown_purchase_receipt,
			"Purchase Order": self._teardown_purchase_order,
			"Stock Entry": self._teardown_stock_entry,
			"Payment Entry": self._teardown_payment_entry,
			"Repair Vehicle": self._teardown_repair_vehicle,
			"Customer": self._teardown_customer,
			"Supplier": self._teardown_supplier,
			"Service Bay": self._teardown_service_bay,
			"Schedule Entry Type": self._teardown_schedule_entry_type,
		}
		handler = handlers.get(doc.doctype)
		if handler:
			handler(doc)
		else:
			_cancel_and_delete(doc.doctype, doc.name)

	def _delete_referencing_schedule_entries(self, filter_field, filter_value):
		if not frappe.db.exists("DocType", "Schedule Entry"):
			return
		linked_entries = frappe.get_all(
			"Schedule Entry",
			filters={filter_field: filter_value},
			pluck="name",
		)
		for se_name in linked_entries:
			_cancel_and_delete("Schedule Entry", se_name)

	def _cancel_referencing_payments(self, reference_doctype, reference_name):
		pe_refs = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": reference_doctype, "reference_name": reference_name},
			pluck="parent",
		)
		for pe_name in set(pe_refs):
			if pe_name not in PRESERVED_PAYMENT_ENTRIES:
				_cancel_and_delete("Payment Entry", pe_name)

	def _teardown_repair_order(self, doc):
		self._delete_referencing_schedule_entries("repair_order", doc.name)
		if frappe.db.exists("DocType", "Shop Action Item") and frappe.db.has_column("Shop Action Item", "repair_order"):
			for sai in frappe.get_all("Shop Action Item", filters={"repair_order": doc.name}, pluck="name"):
				_cancel_and_delete("Shop Action Item", sai)

		si_name = getattr(doc, "sales_invoice", None)
		if si_name and si_name not in PRESERVED_SALES_INVOICES and frappe.db.exists("Sales Invoice", si_name):
			self._teardown_sales_invoice(frappe.get_doc("Sales Invoice", si_name))
			doc.db_set("sales_invoice", None)
			doc.reload()
		elif si_name and not frappe.db.exists("Sales Invoice", si_name):
			doc.db_set("sales_invoice", None)
			doc.reload()

		_cancel_and_delete("Repair Order", doc.name)

	def _teardown_sales_invoice(self, doc):
		self._cancel_referencing_payments("Sales Invoice", doc.name)
		_cancel_and_delete("Sales Invoice", doc.name)

	def _teardown_purchase_invoice(self, doc):
		self._cancel_referencing_payments("Purchase Invoice", doc.name)
		_cancel_and_delete("Purchase Invoice", doc.name)

	def _teardown_purchase_receipt(self, doc):
		_cancel_and_delete("Purchase Receipt", doc.name)

	def _teardown_purchase_order(self, doc):
		if frappe.db.exists("DocType", "Purchase Receipt Item"):
			pr_names = frappe.get_all(
				"Purchase Receipt Item",
				filters={"purchase_order": doc.name},
				pluck="parent",
			)
			for pr_name in set(pr_names):
				_cancel_and_delete("Purchase Receipt", pr_name)

		if frappe.db.exists("DocType", "Purchase Invoice Item"):
			pi_names = frappe.get_all(
				"Purchase Invoice Item",
				filters={"purchase_order": doc.name},
				pluck="parent",
			)
			for pi_name in set(pi_names):
				if frappe.db.exists("Purchase Invoice", pi_name):
					self._teardown_purchase_invoice(frappe.get_doc("Purchase Invoice", pi_name))

		_cancel_and_delete("Purchase Order", doc.name)

	def _teardown_stock_entry(self, doc):
		_cancel_and_delete("Stock Entry", doc.name)

	def _teardown_payment_entry(self, doc):
		_cancel_and_delete("Payment Entry", doc.name)

	def _teardown_repair_vehicle(self, doc):
		if doc.name in PRESERVED_VEHICLES:
			return
		self._delete_referencing_schedule_entries("repair_vehicle", doc.name)
		if frappe.db.exists("DocType", "Vehicle Check-in"):
			for vci in frappe.get_all("Vehicle Check-in", filters={"vehicle": doc.name}, pluck="name"):
				_cancel_and_delete("Vehicle Check-in", vci)
		if frappe.db.exists("DocType", "Repair Vehicle Customer"):
			frappe.db.delete("Repair Vehicle Customer", {"parent": doc.name})
		_cancel_and_delete("Repair Vehicle", doc.name)

	def _teardown_customer(self, doc):
		if doc.name in PRESERVED_CUSTOMERS:
			return
		self._delete_referencing_schedule_entries("customer", doc.name)
		if frappe.db.exists("DocType", "Repair Vehicle Customer"):
			frappe.db.delete("Repair Vehicle Customer", {"customer": doc.name})
		_cancel_and_delete("Customer", doc.name)

	def _teardown_supplier(self, doc):
		for po_name in frappe.get_all("Purchase Order", filters={"supplier": doc.name}, pluck="name"):
			if frappe.db.exists("Purchase Order", po_name):
				self._teardown_purchase_order(frappe.get_doc("Purchase Order", po_name))
		_cancel_and_delete("Supplier", doc.name)

	def _teardown_service_bay(self, doc):
		if doc.name in STATIC_FIXTURE_BAYS:
			return
		self._delete_referencing_schedule_entries("service_bay", doc.name)
		_cancel_and_delete("Service Bay", doc.name)

	def _teardown_schedule_entry_type(self, doc):
		if doc.name in STANDARD_SCHEDULE_ENTRY_TYPES:
			return
		self._delete_referencing_schedule_entries("entry_type", doc.name)
		_cancel_and_delete("Schedule Entry Type", doc.name)
