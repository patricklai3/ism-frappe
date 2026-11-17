# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.tests.base import ISMTestCase


class TestServiceBay(ISMTestCase):
	def setUp(self):
		super().setUp()
		self.bay_alpha = "Sim Unit Bay Alpha"
		self.bay_beta = "Sim Unit Bay Beta"
		self.cleanup()

	def tearDown(self):
		self.cleanup()
		super().tearDown()

	def cleanup(self):
		for name in [self.bay_alpha, self.bay_beta]:
			if frappe.db.exists("Service Bay", name):
				frappe.delete_doc("Service Bay", name, force=True, ignore_permissions=True)

	def test_service_bay_creation_and_equipment_tags(self):
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
			"description": "Primary EV and alignment bay",
			"equipment": [
				{"equipment_tag": "Lift"},
				{"equipment_tag": "EV High Voltage"},
			],
		})
		bay.insert(ignore_permissions=True)
		self.track(bay)

		self.assertTrue(frappe.db.exists("Service Bay", self.bay_alpha))
		loaded = frappe.get_doc("Service Bay", self.bay_alpha)
		self.assertEqual(len(loaded.equipment), 2)
		tags = [row.equipment_tag for row in loaded.equipment]
		self.assertIn("Lift", tags)
		self.assertIn("EV High Voltage", tags)

	def test_service_bay_duplicate_tag_rejection(self):
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
			"equipment": [
				{"equipment_tag": "Lift"},
				{"equipment_tag": "Lift"},
			],
		})
		with self.assertRaises(frappe.ValidationError):
			bay.insert(ignore_permissions=True)

	def test_service_bay_active_filtering(self):
		bay1 = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
		}).insert(ignore_permissions=True)
		self.track(bay1)

		bay2 = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_beta,
			"is_active": 0,
		}).insert(ignore_permissions=True)
		self.track(bay2)

		active_bays = frappe.get_all("Service Bay", filters={"is_active": 1}, pluck="name")
		self.assertIn(self.bay_alpha, active_bays)
		self.assertNotIn(self.bay_beta, active_bays)

	def test_equipment_tag_deletion_protection(self):
		test_tag_name = "Sim Special Tool Tag"
		if not frappe.db.exists("Equipment Tag", test_tag_name):
			tag_doc = frappe.get_doc({
				"doctype": "Equipment Tag",
				"tag_name": test_tag_name,
				"description": "Temporary tag for deletion test",
			}).insert(ignore_permissions=True)
			self.track(tag_doc)

		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
			"equipment": [{"equipment_tag": test_tag_name}],
		}).insert(ignore_permissions=True)
		self.track(bay)

		# Attempting to delete tag while assigned to bay should raise ValidationError
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Equipment Tag", test_tag_name, ignore_permissions=True)

		# Remove bay, now deletion succeeds
		frappe.delete_doc("Service Bay", self.bay_alpha, force=True, ignore_permissions=True)
		frappe.delete_doc("Equipment Tag", test_tag_name, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Equipment Tag", test_tag_name))

	def test_service_bay_deletion_blocked_with_active_schedule_entry(self):
		"""Verify Service Bay cannot be deleted when it has active schedule entries."""
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_alpha,
			"is_active": 1,
		}).insert(ignore_permissions=True)
		self.track(bay)

		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": self.bay_alpha,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "09:00:00",
			"estimated_duration": 60,
			"status": "Scheduled",
		}).insert(ignore_permissions=True)
		self.track(se)

		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Service Bay", self.bay_alpha, force=True, ignore_permissions=True)

		# Once cancelled, force deletion succeeds (on_trash allows cancelled entries)
		se.status = "Cancelled"
		se.save(ignore_permissions=True)

		frappe.delete_doc("Service Bay", self.bay_alpha, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Service Bay", self.bay_alpha))

	def test_service_bay_deletion_blocked_with_confirmed_schedule_entry(self):
		"""Verify Service Bay cannot be deleted when it has Confirmed schedule entries."""
		bay = frappe.get_doc({
			"doctype": "Service Bay",
			"bay_name": self.bay_beta,
			"is_active": 1,
		}).insert(ignore_permissions=True)
		self.track(bay)

		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": "Diagnostic",
			"service_bay": self.bay_beta,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "11:00:00",
			"estimated_duration": 60,
			"status": "Confirmed",
		}).insert(ignore_permissions=True)
		self.track(se)

		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Service Bay", self.bay_beta, force=True, ignore_permissions=True)

		se.status = "Cancelled"
		se.save(ignore_permissions=True)

		frappe.delete_doc("Service Bay", self.bay_beta, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Service Bay", self.bay_beta))
