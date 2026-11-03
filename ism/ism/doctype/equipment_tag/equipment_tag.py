# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EquipmentTag(Document):
	def validate(self):
		if self.tag_name:
			self.tag_name = self.tag_name.strip()
		if not self.tag_name:
			frappe.throw(_("Tag Name cannot be empty."), title=_("Validation Error"))

	def on_trash(self):
		assigned_bays = frappe.get_all(
			"Service Bay Equipment",
			filters={"equipment_tag": self.name},
			pluck="parent",
		)
		if assigned_bays:
			unique_bays = sorted(set(assigned_bays))
			frappe.throw(
				_(
					"Cannot delete Equipment Tag '{0}' because it is assigned to Service Bay(s): {1}. "
					"Remove the tag from these bays before deleting."
				).format(self.name, ", ".join(unique_bays)),
				title=_("Tag in Use"),
			)
