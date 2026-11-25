# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

_VALID_PROFILES = ("Staging", "Production")
_VALID_WIZARD_STATUSES = ("Not Started", "In Progress", "Completed", "Needs Attention")


class ISMSetupState(Document):
	def validate(self):
		if not self.environment_profile:
			self.environment_profile = "Staging"
		if self.environment_profile not in _VALID_PROFILES:
			frappe.throw(
				_("Invalid Environment Profile '{0}'. Expected one of: {1}.").format(
					self.environment_profile, ", ".join(_VALID_PROFILES)
				),
				frappe.ValidationError,
			)

		if not self.wizard_status:
			self.wizard_status = "Not Started"
		if self.wizard_status not in _VALID_WIZARD_STATUSES:
			frappe.throw(
				_("Invalid Wizard Status '{0}'. Expected one of: {1}.").format(
					self.wizard_status, ", ".join(_VALID_WIZARD_STATUSES)
				),
				frappe.ValidationError,
			)

		seen: set[str] = set()
		for row in self.step_states or []:
			step_id = (row.step_id or "").strip()
			row.step_id = step_id
			if not step_id:
				frappe.throw(_("Each Setup Step State row must have a Step ID."), frappe.ValidationError)
			if step_id in seen:
				frappe.throw(
					_("Duplicate Setup Step State row for Step ID '{0}'.").format(step_id),
					frappe.ValidationError,
				)
			seen.add(step_id)

	def get_step_state(self, step_id: str) -> Any | None:
		"""Return the child row for a given step_id, or None if not yet tracked."""
		for row in self.step_states or []:
			if row.step_id == step_id:
				return row
		return None

	def upsert_step_state(self, step_id: str, **fields: Any) -> Any:
		"""Update an existing child row for step_id or append a new one."""
		row = self.get_step_state(step_id)
		if row is None:
			row = self.append("step_states", {"step_id": step_id, **fields})
			return row
		for key, value in fields.items():
			row.set(key, value)
		return row
