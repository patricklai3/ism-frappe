# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
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


		self.ensure_service_item()

	def format_package_name(self):
		"""Ensure correction code is not included in package_name / service title."""
		if self.package_name:
			self.package_name = self.package_name.strip()
			if self.correction_code:
				code = str(self.correction_code).strip()
				self.package_name = re.sub(rf"^{re.escape(code)}\s*[-–—:]?\s*", "", self.package_name).strip()

	def ensure_service_item(self):
		"""Ensure a lightweight non-stock Item exists in ERPNext for billing this service."""
		if not self.package_name:
			return

		item_name = self.package_name.strip()
		rate = flt(self.service_rate)
		desc = item_name

		if not frappe.db.exists("Item", item_name):
			item_group = "Services" if frappe.db.exists("Item Group", "Services") else frappe.db.get_value(
				"Item Group", {"is_group": 0}, "name"
			) or "All Item Groups"

			stock_uom = "Hour" if frappe.db.exists("UOM", "Hour") else (
				"Unit" if frappe.db.exists("UOM", "Unit") else "Nos"
			)

			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_name,
				"item_name": item_name,
				"item_group": item_group,
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 0,
				"include_item_in_manufacturing": 0,
				"description": desc,
				"stock_uom": stock_uom,
				"standard_rate": rate,
			})
			item.insert(ignore_permissions=True)
		else:
			frappe.db.set_value(
				"Item",
				item_name,
				{
					"item_name": item_name,
					"description": desc,
					"standard_rate": rate,
				},
				update_modified=False,
			)

	def on_trash(self):
		"""Clean up auto-created Item if no transactions reference it."""
		item_code = (self.package_name or self.name or "").strip()
		if not (item_code and frappe.db.exists("Item", item_code)):
			return
		if self._is_item_referenced(item_code):
			return
		try:
			frappe.delete_doc("Item", item_code, ignore_permissions=True)
		except Exception:
			pass

	def _is_item_referenced(self, item_code):
		if frappe.db.count("Repair Order Service", {"service_package": self.name}):
			return True
		if frappe.db.count("Sales Invoice Item", {"item_code": item_code}):
			return True
		# Narrow candidates via LIKE, then verify exact item_code inside JSON rows to avoid prefix false positives.
		ro_services = frappe.get_all(
			"Repair Order Service",
			filters={"parts": ["like", f"%{item_code}%"]},
			fields=["parts"],
		)
		for row in ro_services:
			raw_parts = row.get("parts")
			if not raw_parts:
				continue
			try:
				parsed = json.loads(raw_parts) if isinstance(raw_parts, str) else raw_parts
			except Exception:
				continue
			if isinstance(parsed, list) and any(
				isinstance(part, dict) and (part.get("item_code") or "").strip() == item_code
				for part in parsed
			):
				return True
		return False
