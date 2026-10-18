# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ServicePackageItem(Document):
	def validate(self):
		if not self.side or str(self.side).strip().lower() in ("none", "null", "") or self.side not in ("Both", "LH", "RH"):
			self.side = "Both"

