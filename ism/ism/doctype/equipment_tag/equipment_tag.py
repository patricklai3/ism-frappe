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
			filters={"equipment_tag": self.name, "parenttype": "Service Bay"},
			pluck="parent",
		)
		assigned_packages = (
			frappe.get_all(
				"Service Package Equipment",
				filters={"equipment_tag": self.name, "parenttype": "Service Package"},
				pluck="parent",
			)
			if frappe.db.exists("DocType", "Service Package Equipment")
			else []
		)
		if assigned_bays or assigned_packages:
			details = []
			if assigned_bays:
				details.append(
					_("Service Bay(s): {0}").format(", ".join(sorted(set(assigned_bays))))
				)
			if assigned_packages:
				details.append(
					_("Service Package(s): {0}").format(", ".join(sorted(set(assigned_packages))))
				)
			frappe.throw(
				_(
					"Cannot delete Equipment Tag '{0}' because it is assigned to {1}. "
					"Remove the tag before deleting."
				).format(self.name, "; ".join(details)),
				title=_("Tag in Use"),
			)
