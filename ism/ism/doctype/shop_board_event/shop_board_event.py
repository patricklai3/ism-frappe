# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from ism.board.contracts import (
	ACTOR_TYPES,
	AI_STATUSES,
	IMMUTABLE_EVENT_FIELDS,
	PAYLOAD_TYPES,
	RESOLUTION_STATES,
	TARGET_ROLES,
)


class ShopBoardEvent(Document):
	def before_insert(self):
		if not self.event_timestamp:
			self.event_timestamp = now_datetime()

		if isinstance(self.idempotency_key, str):
			self.idempotency_key = self.idempotency_key.strip() or None

		if self.thread and (not self.thread_type or not self.repair_order):
			thread_meta = frappe.db.get_value(
				"Shop Board Thread",
				self.thread,
				["thread_type", "repair_order"],
				as_dict=True,
			)
			if thread_meta:
				if not self.thread_type:
					self.thread_type = thread_meta.get("thread_type")
				if not self.repair_order and thread_meta.get("repair_order"):
					self.repair_order = thread_meta.get("repair_order")

	def validate(self):
		if isinstance(self.idempotency_key, str):
			self.idempotency_key = self.idempotency_key.strip() or None

		if not self.event_timestamp:
			self.event_timestamp = now_datetime()

		if self.actor_type and self.actor_type not in ACTOR_TYPES:
			frappe.throw(
				_("Invalid actor_type: {0}").format(self.actor_type),
				frappe.ValidationError,
			)
		if self.payload_type not in PAYLOAD_TYPES:
			frappe.throw(
				_("Invalid payload_type: {0}").format(self.payload_type),
				frappe.ValidationError,
			)
		if self.target_role and self.target_role not in TARGET_ROLES:
			frappe.throw(
				_("Invalid target_role: {0}").format(self.target_role),
				frappe.ValidationError,
			)
		if self.resolution_state and self.resolution_state not in RESOLUTION_STATES:
			frappe.throw(
				_("Invalid resolution_state: {0}").format(self.resolution_state),
				frappe.ValidationError,
			)
		if self.ai_status and self.ai_status not in AI_STATUSES:
			frappe.throw(
				_("Invalid ai_status: {0}").format(self.ai_status),
				frappe.ValidationError,
			)

		if not self.is_new():
			self._enforce_immutability()

	def _enforce_immutability(self):
		before = self.get_doc_before_save()
		if before is not None:
			before_vals = {field: before.get(field) for field in IMMUTABLE_EVENT_FIELDS}
		else:
			before_vals = (
				frappe.db.get_value(
					"Shop Board Event",
					self.name,
					list(IMMUTABLE_EVENT_FIELDS),
					as_dict=True,
				)
				or {}
			)

		for field in IMMUTABLE_EVENT_FIELDS:
			old_val = before_vals.get(field)
			new_val = self.get(field)
			old_norm = None if old_val in (None, "") else old_val
			new_norm = None if new_val in (None, "") else new_val
			if old_norm != new_norm:
				frappe.throw(
					_("Shop Board Event field '{0}' is immutable after creation.").format(field),
					frappe.ValidationError,
				)

	def on_trash(self):
		if not (frappe.flags.in_test and getattr(frappe.flags, "in_board_teardown", False)):
			frappe.throw(
				_("Shop Board Event records are append-only and cannot be deleted."),
				frappe.ValidationError,
			)
