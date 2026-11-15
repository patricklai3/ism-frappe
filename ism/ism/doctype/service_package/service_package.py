# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from ism.ism.doctype.service_package_item.service_package_item import normalize_package_item_slot_and_side
from ism.utils.tesla_manual import get_mobile_capability_from_manifest, strip_correction_code_prefix


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
				dep_info = get_mobile_capability_from_manifest(
					self.tesla_manual_url,
					getattr(self, "doc_guid", None),
					getattr(self, "correction_code", None),
					allow_network=False,
				)
				if dep_info:
					if dep_info.get("is_mobile_capable"):
						self.is_mobile_capable = dep_info["is_mobile_capable"]
					if not getattr(self, "rh_correction_code", None) and dep_info.get("rh_correction_code"):
						self.rh_correction_code = dep_info["rh_correction_code"]
					if not getattr(self, "is_symmetrical", 0) and dep_info.get("is_symmetrical"):
						self.is_symmetrical = 1
			except Exception:
				frappe.log_error(
					message=frappe.get_traceback(),
					title="ServicePackage Manifest Lookup Error",
				)

		if not getattr(self, "is_symmetrical", 0) and self.package_name:
			if re.search(r"\bLH\b|\bRH\b|Left Hand|Right Hand", self.package_name, re.IGNORECASE):
				self.is_symmetrical = 1

		is_symm = getattr(self, "is_symmetrical", 0)
		for row in (self.items or []):
			normalize_package_item_slot_and_side(
				row,
				is_symmetrical=is_symm,
				enforce_package_symmetry=True,
			)

	def format_package_name(self):
		"""Ensure correction code is not included in package_name / service title."""
		if self.package_name:
			self.package_name = strip_correction_code_prefix(self.package_name, self.correction_code)
