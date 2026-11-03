# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from ism.tests.base import ISMTestCase, TEST_PREFIX


class TestScheduleEntryType(ISMTestCase):
	def test_seed_schedule_entry_types_exist(self):
		"""Verify all 5 standard seed entry types exist with expected attributes."""
		expected_types = {
			"Diagnostic": {"requires_repair_order": 0, "color": "#1f538d"},
			"Repair": {"requires_repair_order": 1, "color": "#2e7d32"},
			"Maintenance": {"requires_repair_order": 1, "color": "#0284c7"},
			"Meeting": {"requires_repair_order": 0, "color": "#7b1fa2"},
			"Internal Service": {"requires_repair_order": 0, "color": "#ef6c00"},
		}

		for type_name, attrs in expected_types.items():
			self.assertTrue(
				frappe.db.exists("Schedule Entry Type", type_name),
				f"Schedule Entry Type '{type_name}' should exist in DB",
			)
			doc = frappe.get_doc("Schedule Entry Type", type_name)
			self.assertEqual(
				doc.requires_repair_order,
				attrs["requires_repair_order"],
				f"'{type_name}' requires_repair_order mismatch",
			)
			self.assertEqual(
				doc.color,
				attrs["color"],
				f"'{type_name}' color mismatch",
			)

	def test_create_custom_schedule_entry_type(self):
		"""Verify custom Schedule Entry Type creation and uniqueness rule."""
		type_name = f"{TEST_PREFIX}Custom_Type"
		if frappe.db.exists("Schedule Entry Type", type_name):
			frappe.delete_doc("Schedule Entry Type", type_name, force=True)

		doc = frappe.get_doc({
			"doctype": "Schedule Entry Type",
			"type_name": type_name,
			"requires_repair_order": 0,
			"color": "#123456",
			"description": "Test custom schedule entry type",
		})
		doc.insert()

		self.assertTrue(frappe.db.exists("Schedule Entry Type", type_name))
		fetched = frappe.get_doc("Schedule Entry Type", type_name)
		self.assertEqual(fetched.color, "#123456")

		# Duplicate type_name should raise DuplicateEntryError
		dup = frappe.get_doc({
			"doctype": "Schedule Entry Type",
			"type_name": type_name,
		})
		self.assertRaises(frappe.DuplicateEntryError, dup.insert)

		frappe.delete_doc("Schedule Entry Type", type_name, force=True)

	def test_delete_standard_schedule_entry_type_blocked(self):
		"""Verify standard entry types cannot be deleted."""
		for std_type in ["Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service"]:
			doc = frappe.get_doc("Schedule Entry Type", std_type)
			self.assertRaises(frappe.ValidationError, doc.delete)

	def test_delete_in_use_schedule_entry_type_blocked(self):
		"""Verify custom entry type cannot be deleted while referenced by Schedule Entry."""
		type_name = f"{TEST_PREFIX}InUseType"
		if frappe.db.exists("Schedule Entry Type", type_name):
			frappe.delete_doc("Schedule Entry Type", type_name, force=True)

		custom_type = frappe.get_doc({
			"doctype": "Schedule Entry Type",
			"type_name": type_name,
			"requires_repair_order": 0,
		}).insert()

		se = frappe.get_doc({
			"doctype": "Schedule Entry",
			"entry_type": type_name,
			"scheduled_date": frappe.utils.today(),
			"scheduled_time": "12:00:00",
			"estimated_duration": 30,
		}).insert(ignore_permissions=True)
		self.track(se)

		self.assertRaises(frappe.ValidationError, custom_type.delete)

		se.delete()
		custom_type.delete()
		self.assertFalse(frappe.db.exists("Schedule Entry Type", type_name))

	def test_rename_standard_schedule_entry_type_blocked(self):
		"""Verify standard entry types cannot be renamed via rename_doc (even with force=True)."""
		for std_type in ["Diagnostic", "Repair", "Maintenance", "Meeting", "Internal Service"]:
			with self.assertRaises(frappe.ValidationError):
				frappe.rename_doc(
					"Schedule Entry Type",
					std_type,
					f"{std_type}_Renamed",
					force=True,
				)
			# Ensure the standard type still exists with its original name
			self.assertTrue(
				frappe.db.exists("Schedule Entry Type", std_type),
				f"Standard Schedule Entry Type '{std_type}' must remain intact in DB",
			)

	def test_rename_to_standard_schedule_entry_type_blocked(self):
		"""Verify custom entry types cannot be renamed to adopt a standard type name."""
		custom_name = f"{TEST_PREFIX}Custom_Rename_Target"
		if frappe.db.exists("Schedule Entry Type", custom_name):
			frappe.delete_doc("Schedule Entry Type", custom_name, force=True)

		custom_doc = frappe.get_doc({
			"doctype": "Schedule Entry Type",
			"type_name": custom_name,
			"requires_repair_order": 0,
		}).insert()
		self.track(custom_doc)

		with self.assertRaises(frappe.ValidationError):
			frappe.rename_doc(
				"Schedule Entry Type",
				custom_name,
				"Diagnostic",
				force=True,
			)

		custom_doc.delete()

	def test_modify_standard_type_name_on_save_blocked(self):
		"""Verify direct mutation of type_name on standard entry types is blocked on save."""
		doc = frappe.get_doc("Schedule Entry Type", "Diagnostic")
		doc.type_name = "Diagnostic_Mutated"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_rename_bypass_deletion_prevented(self):
		"""Adversarial test: verify that attempting to rename and then delete a standard type fails."""
		for std_type in ["Meeting", "Internal Service"]:
			with self.assertRaises(frappe.ValidationError):
				frappe.rename_doc("Schedule Entry Type", std_type, f"{std_type}_Bypass", force=True)
			doc = frappe.get_doc("Schedule Entry Type", std_type)
			with self.assertRaises(frappe.ValidationError):
				doc.delete()
			self.assertTrue(frappe.db.exists("Schedule Entry Type", std_type))
