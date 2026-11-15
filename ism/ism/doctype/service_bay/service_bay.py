# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ServiceBay(Document):
	def validate(self):
		if self.bay_name:
			self.bay_name = self.bay_name.strip()
		if not self.bay_name:
			frappe.throw(_("Bay Name cannot be empty."), title=_("Validation Error"))
		self.validate_duplicate_tags()

	def validate_duplicate_tags(self):
		if not self.equipment:
			return
		seen_tags = set()
		for row in self.equipment:
			if not row.equipment_tag:
				continue
			if row.equipment_tag in seen_tags:
				frappe.throw(
					_("Duplicate equipment tag '{0}' at row #{1}.").format(row.equipment_tag, row.idx),
					title=_("Duplicate Tag"),
				)
			seen_tags.add(row.equipment_tag)

	def on_trash(self):
		if frappe.db.exists("DocType", "Schedule Entry"):
			active_entries = frappe.get_all(
				"Schedule Entry",
				filters={
					"service_bay": self.name,
					"status": ["in", ["Draft", "Confirmed", "Scheduled", "In Progress"]],
				},
				pluck="name",
				limit=5,
			)
			if active_entries:
				frappe.throw(
					_(
						"Cannot delete Service Bay '{0}' because it has active schedule entries ({1}). "
						"Reassign or cancel these entries before deleting."
					).format(self.name, ", ".join(active_entries)),
					title=_("Bay in Use"),
				)
