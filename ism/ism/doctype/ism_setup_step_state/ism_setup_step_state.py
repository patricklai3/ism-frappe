# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class ISMSetupStepState(Document):
	def validate(self):
		if self.step_id:
			self.step_id = str(self.step_id).strip()
		if not self.step_id:
			frappe.throw(_("Step ID is required."), frappe.ValidationError)
		if not self.schema_version or int(self.schema_version) < 1:
			self.schema_version = 1
		if not self.status:
			self.status = "Pending"
