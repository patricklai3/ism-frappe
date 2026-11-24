# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

VALID_BIN_TYPES = ("Standard", "Overflow", "Receiving", "Bulk", "Hazmat")


class StorageBin(Document):
	def _normalize_fields(self) -> None:
		if self.bin_code is not None:
			self.bin_code = str(self.bin_code).strip().upper()
		for fieldname in ("zone", "aisle", "rack", "shelf"):
			val = getattr(self, fieldname, None)
			if isinstance(val, str):
				setattr(self, fieldname, val.strip())

	def before_naming(self) -> None:
		self._normalize_fields()

	def validate(self) -> None:
		self._normalize_fields()
		if not self.bin_code:
			frappe.throw(_("Bin Code cannot be empty."), frappe.ValidationError)

		if not self.bin_type:
			self.bin_type = "Standard"
		elif self.bin_type not in VALID_BIN_TYPES:
			frappe.throw(
				_("Invalid Bin Type '{0}'. Allowed values: {1}.").format(
					self.bin_type, ", ".join(VALID_BIN_TYPES)
				),
				frappe.ValidationError,
			)

		if not self.warehouse:
			from ism.api.parts_stock import _resolve_default_warehouse

			default_wh = _resolve_default_warehouse()
			if default_wh and frappe.db.exists("Warehouse", default_wh):
				self.warehouse = default_wh

		if self.warehouse:
			wh_meta = frappe.db.get_value(
				"Warehouse",
				self.warehouse,
				["name", "is_group"],
				as_dict=True,
			)
			if not wh_meta:
				frappe.throw(
					_("Warehouse '{0}' does not exist.").format(self.warehouse),
					frappe.DoesNotExistError,
				)
			if cint(wh_meta.is_group) == 1:
				frappe.throw(
					_("Storage Bin warehouse '{0}' cannot be a group warehouse.").format(self.warehouse),
					frappe.ValidationError,
				)
