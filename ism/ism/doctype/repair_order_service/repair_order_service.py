# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import flt


class RepairOrderService(Document):
	@property
	def frt(self):
		return flt(self.labor_hours)

	@frt.setter
	def frt(self, val):
		self.labor_hours = flt(val)

	@property
	def category(self):
		if getattr(self, "correction_code", None):
			try:
				from ism.utils.tesla_manual import get_category_for_correction_code
				cat, _ = get_category_for_correction_code(self.correction_code)
				if cat:
					return cat
			except Exception:
				pass
		return getattr(self, "_category", "Maintenance")

	@category.setter
	def category(self, val):
		self._category = val
