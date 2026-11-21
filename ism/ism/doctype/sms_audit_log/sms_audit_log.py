# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cint

from ism.comms.contracts import redact_otp_body

_MAX_MESSAGE_PREVIEW_LEN = 500


class SMSAuditLog(Document):
	def validate(self):
		if self.message_type == "OTP" or cint(self.is_redacted):
			self.is_redacted = 1
			self.message_preview = redact_otp_body(self.message_preview or "")

		if self.message_preview and len(self.message_preview) > _MAX_MESSAGE_PREVIEW_LEN:
			self.message_preview = self.message_preview[:_MAX_MESSAGE_PREVIEW_LEN]
