# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class ServicePackage(Document):
	@property
	def frt(self):
		return flt(self.standard_labor_hours)

	@frt.setter
	def frt(self, val):
		self.standard_labor_hours = flt(val)

	def before_insert(self):
		self.format_package_name()

	def validate(self):
		self.format_package_name()

		if not self.is_flat_rate and flt(self.standard_labor_hours) and flt(self.hourly_rate):
			self.service_rate = flt(self.standard_labor_hours) * flt(self.hourly_rate)

		if not getattr(self, "is_mobile_capable", 0) and getattr(self, "tesla_manual_url", None):
			try:
				from ism.utils.tesla_manual import get_mobile_capability_from_manifest
				dep_info = get_mobile_capability_from_manifest(
					self.tesla_manual_url,
					getattr(self, "doc_guid", None),
					getattr(self, "correction_code", None),
				)
				if dep_info:
					if dep_info.get("is_mobile_capable"):
						self.is_mobile_capable = dep_info["is_mobile_capable"]
					if not getattr(self, "rh_correction_code", None) and dep_info.get("rh_correction_code"):
						self.rh_correction_code = dep_info["rh_correction_code"]
					if not getattr(self, "is_symmetrical", 0) and dep_info.get("is_symmetrical"):
						self.is_symmetrical = 1
			except Exception:
				pass

		if not getattr(self, "is_symmetrical", 0) and self.package_name:
			if re.search(r'\bLH\b|\bRH\b|Left Hand|Right Hand', self.package_name, re.IGNORECASE):
				self.is_symmetrical = 1

		# Normalize slot numbers, component_slot labels, and side variants
		from ism.api.parts import parse_slot_number, format_slot_label, has_lhrh_designation
		is_symm = getattr(self, "is_symmetrical", 0)
		for row in (self.items or []):
			slot_num = getattr(row, "slot_number", None)
			if slot_num is None or slot_num == "":
				slot_num = parse_slot_number(getattr(row, "component_slot", None))
			row.slot_number = frappe.utils.cint(slot_num)
			row.component_slot = format_slot_label(row.slot_number)

			# If package has no LH/RH designation and part has no LH/RH designation, side must be Both.
			# Side must never be "None".
			row_side = getattr(row, "side", None)
			if not row_side or str(row_side).strip().lower() in ("none", "null", "") or row_side not in ("Both", "LH", "RH"):
				row.side = "Both"
			elif not is_symm:
				part_ident = f"{row.item_code or ''} {getattr(row, 'item_name', '') or ''}"
				if not has_lhrh_designation(part_ident):
					row.side = "Both"

	def format_package_name(self):
		"""Ensure correction code is not included in package_name / service title."""
		if self.package_name:
			self.package_name = self.package_name.strip()
			if self.correction_code:
				code = str(self.correction_code).strip()
				self.package_name = re.sub(rf"^{re.escape(code)}\s*[-–—:]?\s*", "", self.package_name).strip()
