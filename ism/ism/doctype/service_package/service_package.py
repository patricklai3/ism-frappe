# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt
from ism.utils.tesla_manual import get_category_for_correction_code


class ServicePackage(Document):
	def before_insert(self):
		self.format_package_name()

	def validate(self):
		self.format_package_name()
		self.sync_frt_and_labor_hours()
		self.populate_tesla_categories()

		if not self.is_flat_rate and flt(self.standard_labor_hours) and flt(self.hourly_rate):
			self.service_rate = flt(self.standard_labor_hours) * flt(self.hourly_rate)

		self.ensure_service_item()

	def format_package_name(self):
		"""Ensure package_name is formatted as '[Correction Code] - [Title]' if code is present."""
		if self.correction_code and self.package_name:
			code = str(self.correction_code).strip()
			if not self.package_name.startswith(code):
				self.package_name = f"{code} - {self.package_name.strip()}"

	def sync_frt_and_labor_hours(self):
		"""Sync FRT with standard_labor_hours."""
		if flt(self.frt) > 0 and (not self.standard_labor_hours or flt(self.standard_labor_hours) == 1.0):
			self.standard_labor_hours = flt(self.frt, 2)
		elif flt(self.standard_labor_hours) > 0 and not flt(self.frt):
			self.frt = flt(self.standard_labor_hours, 2)

	def populate_tesla_categories(self):
		"""Auto-populate category and subcategory from correction code if not set."""
		if self.correction_code:
			cat, subcat = get_category_for_correction_code(self.correction_code)
			if cat and (not self.category or self.category == "Maintenance"):
				self.category = cat
			if subcat and not self.subcategory:
				self.subcategory = subcat

	def ensure_service_item(self):
		"""Ensure a lightweight non-stock Item exists in ERPNext for billing this service."""
		if not self.package_name:
			return

		item_name = self.package_name.strip()
		rate = flt(self.service_rate)
		desc = self.description or item_name

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
		if item_code and frappe.db.exists("Item", item_code):
			has_ro = frappe.db.count("Repair Order Item", {"item_code": item_code}) if frappe.db.table_exists("tabRepair Order Item") else 0
			has_si = frappe.db.count("Sales Invoice Item", {"item_code": item_code})
			if not (has_ro or has_si):
				try:
					frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)
				except Exception:
					pass
