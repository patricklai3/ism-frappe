# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class InspectionTemplate(Document):
	"""Reusable check-in inspection checklist template."""

	def validate(self):
		if self.template_name:
			self.template_name = self.template_name.strip()
		if not self.template_name:
			frappe.throw(_("Template Name is required."))
		for row in self.items or []:
			if row.inspection_description:
				row.inspection_description = row.inspection_description.strip()
			if not row.inspection_description:
				frappe.throw(_("Row {0}: Inspection Description cannot be blank.").format(row.idx))
