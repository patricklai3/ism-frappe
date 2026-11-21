# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from ism.ism.doctype.service_package_item.service_package_item import normalize_package_item_slot_and_side
from ism.scheduling.capacity import parse_required_tags
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

		self._validate_self_serve_catalog()

	def _validate_self_serve_catalog(self):
		"""Normalize and validate self-serve booking catalog fields."""
		for fieldname in ("customer_title", "customer_summary", "self_serve_required_tags"):
			val = getattr(self, fieldname, None)
			if isinstance(val, str):
				setattr(self, fieldname, val.strip())

		if flt(getattr(self, "customer_estimate_min", 0) or 0) < 0:
			frappe.throw(_("Customer Estimate Min cannot be negative."))
		if flt(getattr(self, "customer_estimate_max", 0) or 0) < 0:
			frappe.throw(_("Customer Estimate Max cannot be negative."))
		if cint(getattr(self, "self_serve_duration_minutes", 0) or 0) < 0:
			frappe.throw(_("Self-Serve Duration (Minutes) cannot be negative."))

		if cint(getattr(self, "allow_self_serve_booking", 0)) != 1:
			return

		mode = getattr(self, "customer_price_display_mode", None) or "Exact Price"
		est_min = flt(getattr(self, "customer_estimate_min", 0) or 0)
		est_max = flt(getattr(self, "customer_estimate_max", 0) or 0)
		if mode == "Estimate Range" and est_min > 0 and est_max > 0 and est_max < est_min:
			frappe.throw(_("Customer Estimate Max must be greater than or equal to Customer Estimate Min."))

		year_from = cint(getattr(self, "applicable_year_from", 0) or 0)
		year_to = cint(getattr(self, "applicable_year_to", 0) or 0)
		if year_from != 0 and not (2008 <= year_from <= 2100):
			frappe.throw(_("Applicable Year From must be between 2008 and 2100 (or 0 for no lower bound)."))
		if year_to != 0 and not (2008 <= year_to <= 2100):
			frappe.throw(_("Applicable Year To must be between 2008 and 2100 (or 0 for no upper bound)."))
		if year_from != 0 and year_to != 0 and year_from > year_to:
			frappe.throw(_("Applicable Year From cannot be greater than Applicable Year To."))

		raw_tags = getattr(self, "self_serve_required_tags", None)
		if raw_tags and frappe.db.exists("DocType", "Equipment Tag"):
			tags = list(dict.fromkeys(parse_required_tags(raw_tags)))
			if tags:
				existing = set(
					frappe.get_all("Equipment Tag", filters={"name": ["in", tags]}, pluck="name")
				)
				missing = [tag for tag in tags if tag not in existing]
				if missing:
					frappe.throw(_("Unknown Equipment Tag(s): {0}").format(", ".join(missing)))
				self.self_serve_required_tags = ", ".join(tags)
			else:
				self.self_serve_required_tags = ""

	def format_package_name(self):
		"""Ensure correction code is not included in package_name / service title."""
		if self.package_name:
			self.package_name = strip_correction_code_prefix(self.package_name, self.correction_code)
