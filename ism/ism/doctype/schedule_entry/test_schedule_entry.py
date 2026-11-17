# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import time
from unittest.mock import patch

import frappe
from ism.ism.doctype.repair_order.repair_order import RepairOrder
from ism.tests.base import ISMTestCase


class TestScheduleEntry(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.manifest = self.get_manifest()
		self.customer = frappe.get_doc("Customer", self.manifest.customers["retail_1"])
		self.vehicle = frappe.get_doc("Repair Vehicle", self.manifest.vehicles["veh_model3_liam"])
		self.ro = self.create_test_repair_order(customer=self.customer.name, vehicle=self.vehicle.name)

	def _insert_entry(self, doc_dict):
		"""Insert Schedule Entry with 3-attempt restart loop on error 1020 without inner rollback."""
		for attempt in range(3):
			try:
				doc = frappe.get_doc(doc_dict.copy()).insert(ignore_permissions=True)
				return self.track(doc)
			except Exception as e:
				if ("1020" in str(e) or "Deadlock" in str(e) or isinstance(e, frappe.QueryDeadlockError)) and attempt < 2:
					time.sleep(0.05 * (attempt + 1))
					continue
				raise

	def test_schedule_entry_naming_series(self):
		"""Verify that Schedule Entry generates SE-.##### series names."""
		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"status": "Draft",
		})

		self.assertTrue(se.name.startswith("SE-"))
		self.assertEqual(se.status, "Draft")
		self.assertEqual(se.estimated_duration, 60)

	def test_multiple_schedule_entries_per_repair_order(self):
		"""Crucial Invariant: Multiple Schedule Entries on the same Repair Order must succeed (1:N cardinality)."""
		se1 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 120,
			"status": "Scheduled",
		})

		# Supplemental work session on the same Repair Order
		se2 = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.add_days(frappe.utils.today(), 1),
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"status": "Scheduled",
		})

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
		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Repair",
			"repair_order": self.ro.name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "10:00:00",
		})

		self.assertEqual(se.customer, self.customer.name)
		self.assertEqual(se.repair_vehicle, self.vehicle.name)

	def test_provisional_contact_and_vehicle_display(self):
		"""Verify display fallbacks for provisional customer and vehicle intake."""
		se_empty = frappe.new_doc("Schedule Entry")
		self.assertEqual(se_empty.get_display_customer(), "Guest")
		self.assertEqual(se_empty.get_display_vehicle(), "")

		se_prov = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "11:00:00",
			"provisional_customer_name": "Alice Walker",
			"provisional_phone": "555-123-4567",
			"provisional_email": "alice@example.com",
			"provisional_vehicle_info": "2024 Model Y Red",
		})

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
		se_diag = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "13:00:00",
		})
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
		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "14:00:00",
			"estimated_duration": 60,
			"provisional_customer_name": "Provisional Client",
		})

		self.assertEqual(se.get_display_customer(), "Provisional Client")
		self.assertIsNone(se.customer)
		self.assertIsNone(se.repair_vehicle)

		# Advisor links existing repair order on update
		se.repair_order = self.ro.name
		se.save(ignore_permissions=True)

		self.assertEqual(se.customer, self.customer.name)
		self.assertEqual(se.repair_vehicle, self.vehicle.name)
		self.assertIn(self.customer.name, se.title)

	def test_sync_does_not_revalidate_parent_ro(self):
		"""Verify Schedule Entry save syncs child rows directly without invoking RepairOrder.validate."""
		with patch.object(RepairOrder, "validate") as mock_ro_validate:
			se = self._insert_entry({
				"doctype": "Schedule Entry",
				"entry_type": "Repair",
				"repair_order": self.ro.name,
				"scheduled_date": frappe.utils.today(),
				"scheduled_time": "15:00:00",
				"estimated_duration": 60,
			})
			se.status = "Confirmed"
			se.save(ignore_permissions=True)

		mock_ro_validate.assert_not_called()
		self.ro.reload()
		self.assertEqual(len(self.ro.schedule_entries), 1)
		self.assertEqual(self.ro.schedule_entries[0].schedule_entry, se.name)
		self.assertEqual(self.ro.schedule_entries[0].status, "Confirmed")

	def test_warnings_and_no_show_release_capacity(self):
		"""Verify assignment warnings use doc flags without msgprint, and No Show releases bay capacity."""
		from ism.api.scheduling import check_bay_availability

		bay_name = self.manifest.bays["main_lift"]

		se = self._insert_entry({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"service_bay": bay_name,
		})
		self.assertFalse(check_bay_availability(bay_name, frappe.utils.today(), "09:00:00", 60))

		with patch("frappe.msgprint") as mock_msgprint, patch(
			"ism.api.technician_availability.check_technician_availability",
			return_value={"is_available": False, "on_leave": True, "conflicts": []},
		):
			se.flags.ignore_links = True
			se.assigned_technician = "TECH-MOCK"
			se.save(ignore_permissions=True)
		mock_msgprint.assert_not_called()
		self.assertEqual(len(se.flags.scheduling_warnings), 1)

		se.status = "No Show"
		se.save(ignore_permissions=True)
		self.assertTrue(check_bay_availability(bay_name, frappe.utils.today(), "09:00:00", 60))
