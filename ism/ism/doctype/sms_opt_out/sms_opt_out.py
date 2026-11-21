# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ism.comms.contracts import normalize_phone_e164


class SMSOptOut(Document):
	def before_naming(self):
		self._normalize_phone()

	def before_validate(self):
		self._normalize_phone()

	def _normalize_phone(self):
		try:
			self.phone_e164 = normalize_phone_e164(self.phone_e164 or "")
		except ValueError as exc:
			frappe.throw(str(exc), frappe.ValidationError)
