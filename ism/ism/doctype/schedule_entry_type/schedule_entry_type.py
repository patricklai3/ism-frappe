# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

STANDARD_SCHEDULE_ENTRY_TYPES = {
	"Diagnostic",
	"Repair",
	"Maintenance",
	"Meeting",
	"Internal Service",
}


class ScheduleEntryType(Document):
	def validate(self):
		if self.type_name:
			self.type_name = self.type_name.strip()
		if not self.type_name:
			frappe.throw(_("Type Name cannot be empty."), title=_("Validation Error"))

		# Prevent direct renaming via doc.type_name = "new_name"; doc.save()
		if not self.is_new() and self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
			if self.type_name != self.name:
				frappe.throw(
					_("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name),
					title=_("Standard Type Protected"),
				)

	def before_rename(self, old_name, new_name, merge=False):
		target = (old_name or self.name or "").strip()
		if target in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
			frappe.throw(
				_("Cannot rename standard Schedule Entry Type '{0}'.").format(self.name or target),
				title=_("Standard Type Protected"),
			)

		new_target = (new_name or "").strip()
		if new_target in STANDARD_SCHEDULE_ENTRY_TYPES:
			frappe.throw(
				_("Cannot rename to standard Schedule Entry Type '{0}'.").format(new_name),
				title=_("Standard Type Protected"),
			)

		return new_target

	def on_trash(self):
		target_name = (self.type_name or self.name or "").strip()
		if target_name in STANDARD_SCHEDULE_ENTRY_TYPES or self.name in STANDARD_SCHEDULE_ENTRY_TYPES:
			frappe.throw(
				_("Cannot delete standard Schedule Entry Type '{0}'.").format(self.name or target_name),
				title=_("Standard Type Protected"),
			)

		if frappe.db.exists("DocType", "Schedule Entry"):
			linked_entries = frappe.get_all(
				"Schedule Entry",
				filters={"entry_type": self.name},
				pluck="name",
				limit=5,
			)
			if linked_entries:
				frappe.throw(
					_(
						"Cannot delete Schedule Entry Type '{0}' because it is referenced by existing Schedule Entry records ({1}). "
						"Reassign or delete these entries before deleting the type."
					).format(self.name, ", ".join(linked_entries)),
					title=_("Type in Use"),
				)
