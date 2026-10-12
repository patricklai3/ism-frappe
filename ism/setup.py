# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import os
import frappe
from frappe.modules.export_file import export_to_files

LEGACY_FIELDS_TO_REMOVE = {
	"Quotation": [
		"custom_vehicle",
		"custom_vehicle_check_in",
		"custom_odometer_in",
		"custom_customer_concern",
		"custom_sb_jobs",
		"custom_jobs",
		"custom_cb_approval",
		"custom_approval_status",
		"custom_approval_token_expiry",
		"custom_cb_supplement",
		"custom_is_supplement",
		"custom_supplement_for",
		"custom_approval_token",
	],
	"Quotation Item": [
		"custom_job",
		"custom_job_title",
	],
	"Sales Order": [
		"custom_vehicle",
		"custom_vehicle_check_in",
		"custom_odometer_in",
		"custom_sb_jobs",
		"custom_jobs",
	],
	"Sales Order Item": [
		"custom_job",
		"custom_job_title",
	],
	"Sales Invoice": [
		"custom_vehicle",
		"custom_vehicle_check_in",
		"custom_odometer_in",
		"custom_sb_jobs",
		"custom_jobs",
	],
	"Sales Invoice Item": [
		"custom_job",
		"custom_job_title",
	],
}

STANDARD_INSPECTION_ITEMS = [
	{"inspection_description": "Manufacturing Certification Label"},
	{"inspection_description": "Front Left Corner"},
	{"inspection_description": "Front Right Corner"},
	{"inspection_description": "Rear Left Corner"},
	{"inspection_description": "Rear Right Corner"},
	{"inspection_description": "Interior (Front Seats)"},
	{"inspection_description": "Interior (Rear Seats)"},
	{"inspection_description": "Dashboard Mileage"},
	{"inspection_description": "Service Mode Alert Page"},
]


def cleanup_legacy_custom_fields():
	"""Safely remove legacy custom fields injected on standard ERPNext sales doctypes."""
	for dt, fields in LEGACY_FIELDS_TO_REMOVE.items():
		for fn in fields:
			cf = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fn}, "name")
			if cf:
				frappe.delete_doc("Custom Field", cf, force=True, ignore_permissions=True)


def create_standard_inspection_template():
	"""Seed the default Standard Vehicle Walkaround inspection template."""
	template_name = "Standard Vehicle Walkaround"
	if not frappe.db.exists("Inspection Template", template_name):
		doc = frappe.get_doc({
			"doctype": "Inspection Template",
			"template_name": template_name,
			"description": "Standard 9-point intake walkaround inspection.",
			"items": STANDARD_INSPECTION_ITEMS,
		})
		doc.insert(ignore_permissions=True)


def setup_default_ism_settings():
	"""Initialize default ISM Settings if not populated."""
	settings = frappe.get_single("ISM Settings")
	if not settings.authorization_token_expiry_hours:
		settings.authorization_token_expiry_hours = 48
	if not settings.cost_tolerance_percentage:
		settings.cost_tolerance_percentage = 10.0
	if not settings.cost_tolerance_flat_amount:
		settings.cost_tolerance_flat_amount = 50.0
	if not settings.disclaimer_text:
		settings.disclaimer_text = (
			"I hereby authorize the repair work listed above to be done along with the necessary materials. "
			"You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
		)
	settings.save(ignore_permissions=True)


def ensure_doctype_creation_enabled():
	"""Ensure ISM DocTypes allow document creation by users (in_create must be 0).

	Note: In Frappe, DocType.in_create is the database column for 'User Cannot Create'.
	Setting in_create = 1 blocks document creation and hides the '+ Add' button.
	"""
	frappe.db.sql("UPDATE `tabDocType` SET in_create = 0 WHERE module = 'ISM'")
	frappe.db.commit()
	frappe.clear_cache()


def setup_default_taxes_and_payments():
	"""Ensure 13% HST Sales Taxes and Charges Template and Debit / Interac Mode of Payment exist."""
	# 1. Mode of Payment: Debit / Interac
	if not frappe.db.exists("Mode of Payment", "Debit / Interac"):
		mop = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": "Debit / Interac",
			"type": "Bank",
			"enabled": 1,
		})
		mop.insert(ignore_permissions=True)

	companies = frappe.get_all("Company", fields=["name", "abbr", "default_currency"])

	# Ensure Mode of Payment Account for Debit / Interac
	if frappe.db.exists("Mode of Payment", "Debit / Interac"):
		mop_doc = frappe.get_doc("Mode of Payment", "Debit / Interac")
		updated_mop = False
		for comp in companies:
			has_acc = any(r.company == comp.name for r in (mop_doc.accounts or []))
			if not has_acc:
				bank_or_cash = (
					frappe.db.get_value("Company", comp.name, "default_bank_account")
					or frappe.db.get_value("Company", comp.name, "default_cash_account")
					or f"Cash - {comp.abbr or comp.name}"
				)
				if frappe.db.exists("Account", bank_or_cash):
					mop_doc.append("accounts", {
						"company": comp.name,
						"default_account": bank_or_cash,
					})
					updated_mop = True
		if updated_mop:
			mop_doc.save(ignore_permissions=True)

	# 2. For each company, ensure HST 13% account and 13% HST Sales Taxes and Charges Template
	for comp in companies:
		c_name = comp.name
		abbr = comp.abbr or c_name
		duties_and_taxes = (
			frappe.db.get_value("Account", {"account_name": "Duties and Taxes", "company": c_name}, "name")
			or f"Duties and Taxes - {abbr}"
		)

		account_name = f"HST 13% - {abbr}"
		if not frappe.db.exists("Account", account_name):
			if frappe.db.exists("Account", duties_and_taxes):
				acc = frappe.get_doc({
					"doctype": "Account",
					"account_name": "HST 13%",
					"company": c_name,
					"parent_account": duties_and_taxes,
					"account_type": "Tax",
					"currency": comp.default_currency or "CAD",
				})
				acc.insert(ignore_permissions=True)

		template_name = f"13% HST - {abbr}"
		is_primary_company = comp.default_currency in ["CAD", None] or c_name == "I"
		if not frappe.db.exists("Sales Taxes and Charges Template", template_name):
			if frappe.db.exists("Account", account_name):
				tmpl = frappe.get_doc({
					"doctype": "Sales Taxes and Charges Template",
					"title": "13% HST",
					"company": c_name,
					"is_default": 1 if is_primary_company else 0,
					"taxes": [
						{
							"charge_type": "On Net Total",
							"account_head": account_name,
							"rate": 13.0,
							"description": "HST @ 13.0%",
						}
					],
				})
				tmpl.insert(ignore_permissions=True)
		else:
			if is_primary_company:
				frappe.db.set_value("Sales Taxes and Charges Template", template_name, "is_default", 1)

		if is_primary_company:
			for other in frappe.get_all(
				"Sales Taxes and Charges Template",
				filters={"company": c_name, "is_default": 1, "name": ["!=", template_name]},
			):
				frappe.db.set_value("Sales Taxes and Charges Template", other.name, "is_default", 0)


def setup_parts_item_group():
	"""Ensure 'Parts' Item Group exists under 'All Item Groups'."""
	if not frappe.db.exists("Item Group", "Parts"):
		try:
			doc = frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": "Parts",
				"parent_item_group": "All Item Groups" if frappe.db.exists("Item Group", "All Item Groups") else "",
				"is_group": 0,
			})
			doc.insert(ignore_permissions=True)
		except Exception:
			pass


def setup_item_custom_fields():
	"""Ensure automotive compliance custom fields exist on Item DocType."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	meta = frappe.get_meta("Item")
	fields = [
		{
			"fieldname": "is_vehicle_part",
			"label": "Is Vehicle Part",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": "item_group",
		},
		{
			"fieldname": "part_source",
			"label": "Part Source",
			"fieldtype": "Select",
			"options": "OEM\nAftermarket",
			"default": "OEM",
			"insert_after": "is_vehicle_part",
			"in_standard_filter": 1,
		},
		{
			"fieldname": "part_condition",
			"label": "Part Condition",
			"fieldtype": "Select",
			"options": "New\nUsed\nReconditioned",
			"default": "New",
			"insert_after": "part_source",
			"in_standard_filter": 1,
		},
		{
			"fieldname": "oem_part_number",
			"label": "OEM Part Number",
			"fieldtype": "Data",
			"insert_after": "part_condition",
			"in_standard_filter": 1,
		},
		{
			"fieldname": "revision",
			"label": "Revision",
			"fieldtype": "Data",
			"insert_after": "oem_part_number",
		},
	]

	fields_to_add = [f for f in fields if not meta.has_field(f["fieldname"])]
	if fields_to_add:
		create_custom_fields({"Item": fields_to_add}, update=True)



def backfill_service_package_models():
	"""Backfill applicable_model and applicable_generation on existing Service Packages and Service rows."""
	from ism.utils.tesla_manual import parse_tesla_manual_url

	if not frappe.db.has_column("Service Package", "applicable_model"):
		return 0

	count = 0
	packages = frappe.get_all("Service Package", fields=["name", "tesla_manual_url", "applicable_model", "applicable_generation"])
	for p in packages:
		if not p.applicable_model and p.tesla_manual_url:
			url_meta = parse_tesla_manual_url(p.tesla_manual_url)
			model = url_meta.get("applicable_model")
			gen = url_meta.get("matched_generation")
			if model:
				frappe.db.set_value("Service Package", p.name, {
					"applicable_model": model,
					"applicable_generation": gen or "",
				}, update_modified=False)
				count += 1

	if frappe.db.has_column("Repair Order Service", "applicable_model"):
		frappe.db.sql("""
			UPDATE `tabRepair Order Service` ros
			JOIN `tabService Package` sp ON ros.service_package = sp.name
			SET ros.applicable_model = sp.applicable_model
			WHERE (ros.applicable_model IS NULL OR ros.applicable_model = '')
			  AND (sp.applicable_model IS NOT NULL AND sp.applicable_model != '')
		""")

	return count


def after_migrate():
	"""Run migrations and setup tasks for ISM."""
	cleanup_legacy_custom_fields()
	create_standard_inspection_template()
	setup_default_ism_settings()
	ensure_doctype_creation_enabled()
	setup_default_taxes_and_payments()
	setup_parts_item_group()
	setup_item_custom_fields()
	backfill_service_package_models()



