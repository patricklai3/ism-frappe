# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestScheduleEntry(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.cleanup_schedule_entries()
		self.customer = self.create_test_customer(f"{TEST_PREFIX}Cust_SE")
		self.vehicle = self.create_test_vehicle(vin="5YJ3E1EBXNF123456", customer=self.customer.name)
		self.ro = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)

	def tearDown(self):
		self.cleanup_schedule_entries()
		super().tearDown()

	def cleanup_schedule_entries(self):
		"""Clean up test schedule entries created during tests to ensure complete test isolation."""
		if not frappe.db.exists("DocType", "Schedule Entry"):
			return

		# 1. Clean entries linked to self.ro if present
		ro_name = getattr(getattr(self, "ro", None), "name", None)
		if ro_name:
			for se_name in frappe.get_all("Schedule Entry", filters={"repair_order": ro_name}, pluck="name"):
				try:
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
				except Exception:
					pass

		# 2. Clean entries linked to self.customer if present
		cust_name = getattr(getattr(self, "customer", None), "name", None)
		if cust_name:
			for se_name in frappe.get_all("Schedule Entry", filters={"customer": cust_name}, pluck="name"):
				try:
					frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
				except Exception:
					pass

		# 3. Clean test entries with known test provisional names or titles
		test_entries = frappe.get_all(
			"Schedule Entry",
			filters=[
				["provisional_customer_name", "in", ["Alice Walker", "Provisional Client"]],
			],
			pluck="name",
		) + frappe.get_all(
			"Schedule Entry",
			filters=[
				["title", "like", f"{TEST_PREFIX}%"],
			],
			pluck="name",
		) + frappe.get_all(
			"Schedule Entry",
			filters=[
				["title", "like", "Guest%"],
			],
			pluck="name",
		)
		for se_name in set(test_entries):
			try:
				frappe.delete_doc("Schedule Entry", se_name, force=True, ignore_permissions=True)
			except Exception:
				pass

	def test_schedule_entry_naming_series(self):
		"""Verify that Schedule Entry generates SE-.##### series names."""
		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"status": "Draft",
		}).insert(ignore_permissions=True)
		self.track(se)

		self.assertTrue(se.name.startswith("SE-"))
		self.assertEqual(se.status, "Draft")
		self.assertEqual(se.estimated_duration, 60)

	def test_multiple_schedule_entries_per_repair_order(self):
		"""Crucial Invariant: Multiple Schedule Entries on the same Repair Order must succeed (1:N cardinality)."""
		se1 = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 120,
			"status": "Scheduled",
		}).insert(ignore_permissions=True)
		self.track(se1)

		# Supplemental work session on the same Repair Order
		se2 = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.add_days(frappe.utils.today(), 1),
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"status": "Scheduled",
		}).insert(ignore_permissions=True)
		self.track(se2)

		self.assertNotEqual(se1.name, se2.name)
		self.assertEqual(se1.repair_order, self.ro.name)
		self.assertEqual(se2.repair_order, self.ro.name)

		# Both entries linked to same RO
		linked_entries = frappe.get_all(
			"Schedule Entry",
			filters={"repair_order": self.ro.name},
			pluck="name",
		)
		self.assertIn(se1.name, linked_entries)
		self.assertIn(se2.name, linked_entries)
		self.assertEqual(len(linked_entries), 2)

	def test_auto_population_from_repair_order(self):
		"""Verify customer and vehicle auto-population when linked to Repair Order."""
		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
		}).insert(ignore_permissions=True)
		self.track(se)

		self.assertEqual(se.customer, self.customer.name)
		self.assertEqual(se.repair_vehicle, self.vehicle.name)

	def test_provisional_contact_and_vehicle_display(self):
		"""Verify display fallbacks for provisional customer and vehicle intake."""
		se_empty = frappe.new_doc("Schedule Entry")
		self.assertEqual(se_empty.get_display_customer(), "Guest")
		self.assertEqual(se_empty.get_display_vehicle(), "")

		se_prov = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "11:00:00",
			"provisional_customer_name": "Alice Walker",
			"provisional_phone": "555-123-4567",
			"provisional_email": "alice@example.com",
			"provisional_vehicle_info": "2024 Model Y Red",
		}).insert(ignore_permissions=True)
		self.track(se_prov)

		self.assertEqual(se_prov.get_display_customer(), "Alice Walker")
		self.assertEqual(se_prov.get_display_vehicle(), "2024 Model Y Red")
		self.assertEqual(se_prov.title, "Alice Walker | 2024 Model Y Red")

	def test_requires_repair_order_validation(self):
		"""Verify that entry types with requires_repair_order=1 mandate a Repair Order."""
		# Repair requires Repair Order
		se_repair = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "13:00:00",
		})
		self.assertRaises(frappe.MandatoryError, se_repair.insert)

		# Diagnostic does not require Repair Order
		se_diag = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "13:00:00",
		}).insert(ignore_permissions=True)
		self.track(se_diag)
		self.assertTrue(se_diag.name)

	def test_estimated_duration_must_be_positive(self):
		"""Verify that estimated_duration <= 0 raises ValidationError."""
		# Zero duration
		se_zero = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": 0,
		})
		self.assertRaises(frappe.ValidationError, se_zero.insert)

		# Negative duration
		se_neg = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
			"estimated_duration": -30,
		})
		self.assertRaises(frappe.ValidationError, se_neg.insert)

	def test_populate_from_repair_order_on_update(self):
		"""Verify customer/vehicle auto-population when linking Repair Order to existing Schedule Entry."""
		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"provisional_customer_name": "Provisional Client",
		}).insert(ignore_permissions=True)
		self.track(se)

		self.assertEqual(se.get_display_customer(), "Provisional Client")
		self.assertIsNone(se.customer)
		self.assertIsNone(se.repair_vehicle)

		# Advisor links existing repair order on update
		se.repair_order = self.ro.name
		se.save(ignore_permissions=True)

		self.assertEqual(se.customer, self.customer.name)
		self.assertEqual(se.repair_vehicle, self.vehicle.name)
		self.assertIn(self.customer.name, se.title)
