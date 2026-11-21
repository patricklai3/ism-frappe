# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ism.board.contracts import BOARD_STATUSES, THREAD_TYPES


class ShopBoardThread(Document):
	def validate(self):
		if self.thread_key:
			self.thread_key = self.thread_key.strip()
		if self.thread_type not in THREAD_TYPES:
			frappe.throw(
				_("Invalid Shop Board Thread type: {0}").format(self.thread_type),
				frappe.ValidationError,
			)
		if self.board_status and self.board_status not in BOARD_STATUSES:
			frappe.throw(
				_("Invalid Shop Board Thread status: {0}").format(self.board_status),
				frappe.ValidationError,
			)
		if not self.board_status:
			self.board_status = "ACTIVE"
		if not self.projection_version:
			self.projection_version = 1
