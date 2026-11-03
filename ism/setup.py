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
	changed = False

	# Customer Approval Settings
	if not settings.authorization_token_expiry_hours:
		settings.authorization_token_expiry_hours = 48
		changed = True
	if not settings.cost_tolerance_percentage:
		settings.cost_tolerance_percentage = 10.0
		changed = True
	if not settings.cost_tolerance_flat_amount:
		settings.cost_tolerance_flat_amount = 50.0
		changed = True
	if not settings.disclaimer_text:
		settings.disclaimer_text = (
			"I hereby authorize the repair work listed above to be done along with the necessary materials. "
			"You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
		)
		changed = True

	# Scheduling & Shop Hours
	if not settings.operating_hours_start:
		settings.operating_hours_start = "08:00:00"
		changed = True
	if not settings.operating_hours_end:
		settings.operating_hours_end = "17:00:00"
		changed = True
	if not settings.break_start:
		settings.break_start = "12:00:00"
		changed = True
	if not settings.break_end:
		settings.break_end = "12:30:00"
		changed = True
	if not settings.default_slot_interval:
		settings.default_slot_interval = 30
		changed = True
	if not settings.scheduling_horizon_days:
		settings.scheduling_horizon_days = 30
		changed = True

	# Resource Capacity
	if not settings.technician_designation:
		settings.technician_designation = "Technician"
		changed = True
	if settings.get("enable_technician_capacity") is None:
		settings.enable_technician_capacity = 1
		changed = True

	# Duration Estimation Tuning
	if not settings.estimation_percentile:
		settings.estimation_percentile = 80.0
		changed = True
	if not settings.estimation_sigma:
		settings.estimation_sigma = 0.30
		changed = True
	if settings.get("buffer_padding_minutes") is None:
		settings.buffer_padding_minutes = 0
		changed = True
	if not settings.default_diagnostic_duration:
		settings.default_diagnostic_duration = 60
		changed = True

	if changed or settings.is_new():
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

	# 2. Configure Accounts Settings to automatically pull taxes from template
	try:
		frappe.db.set_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template", 1)
	except Exception:
		pass

	# 3. For each company, ensure HST 13% account, Sales & Purchase Templates, Item Tax Template, and Tax Rules
	for comp in companies:
		ensure_company_13_percent_hst_setup(comp)


def ensure_company_13_percent_hst_setup(comp):
	"""Ensure 13% HST account, Sales and Purchase Taxes templates, Item Tax Template, and Tax Rules exist and are defaults."""
	if isinstance(comp, str):
		comp = frappe.get_cached_doc("Company", comp)
	c_name = comp.name
	abbr = comp.abbr or c_name
	currency = comp.default_currency or "CAD"

	# A. Account: HST 13% - {abbr} under Duties and Taxes
	duties_and_taxes = (
		frappe.db.get_value("Account", {"account_name": "Duties and Taxes", "company": c_name}, "name")
		or f"Duties and Taxes - {abbr}"
	)
	account_name = f"HST 13% - {abbr}"
	if not frappe.db.exists("Account", account_name):
		if frappe.db.exists("Account", duties_and_taxes):
			try:
				acc = frappe.get_doc({
					"doctype": "Account",
					"account_name": "HST 13%",
					"company": c_name,
					"parent_account": duties_and_taxes,
					"account_type": "Tax",
					"currency": currency,
				})
				acc.insert(ignore_permissions=True)
			except Exception:
				pass

	tax_head = account_name if frappe.db.exists("Account", account_name) else duties_and_taxes
	template_name = f"13% HST - {abbr}"

	# B. Sales Taxes and Charges Template
	if not frappe.db.exists("Sales Taxes and Charges Template", template_name):
		try:
			tmpl = frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template",
				"title": "13% HST",
				"company": c_name,
				"is_default": 1,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": tax_head,
						"rate": 13.0,
						"description": "HST @ 13.0%",
					}
				],
			})
			tmpl.insert(ignore_permissions=True)
		except Exception:
			pass
	else:
		frappe.db.set_value("Sales Taxes and Charges Template", template_name, "is_default", 1)

	for other in frappe.get_all(
		"Sales Taxes and Charges Template",
		filters={"company": c_name, "is_default": 1, "name": ["!=", template_name]},
	):
		frappe.db.set_value("Sales Taxes and Charges Template", other.name, "is_default", 0)

	# C. Purchase Taxes and Charges Template
	if not frappe.db.exists("Purchase Taxes and Charges Template", template_name):
		try:
			ptmpl = frappe.get_doc({
				"doctype": "Purchase Taxes and Charges Template",
				"title": "13% HST",
				"company": c_name,
				"is_default": 1,
				"taxes": [
					{
						"category": "Total",
						"add_deduct_tax": "Add",
						"charge_type": "On Net Total",
						"account_head": tax_head,
						"rate": 13.0,
						"description": "HST @ 13.0%",
					}
				],
			})
			ptmpl.insert(ignore_permissions=True)
		except Exception:
			pass
	else:
		frappe.db.set_value("Purchase Taxes and Charges Template", template_name, "is_default", 1)

	for other in frappe.get_all(
		"Purchase Taxes and Charges Template",
		filters={"company": c_name, "is_default": 1, "name": ["!=", template_name]},
	):
		frappe.db.set_value("Purchase Taxes and Charges Template", other.name, "is_default", 0)

	# D. Item Tax Template
	item_tax_template_name = f"13% HST - {abbr}"
	if not frappe.db.exists("Item Tax Template", item_tax_template_name):
		try:
			it_tmpl = frappe.get_doc({
				"doctype": "Item Tax Template",
				"title": "13% HST",
				"company": c_name,
				"taxes": [
					{
						"tax_type": tax_head,
						"tax_rate": 13.0,
					}
				],
			})
			it_tmpl.insert(ignore_permissions=True)
		except Exception:
			pass

	# E. Global Tax Rules for Sales & Purchase
	_ensure_default_tax_rule("Sales", template_name, c_name)
	_ensure_default_tax_rule("Purchase", template_name, c_name)


def _ensure_default_tax_rule(tax_type, template_name, company):
	"""Ensure a generic default Tax Rule exists for Sales or Purchase in this company."""
	all_rules = frappe.get_all(
		"Tax Rule",
		filters={"company": company, "tax_type": tax_type},
		fields=["name", "sales_tax_template", "purchase_tax_template", "customer", "customer_group", "supplier", "supplier_group", "item", "item_group", "tax_category"],
	)

	template_field = "sales_tax_template" if tax_type == "Sales" else "purchase_tax_template"
	generic_rules = [
		r for r in all_rules
		if not r.get("customer") and not r.get("customer_group")
		and not r.get("supplier") and not r.get("supplier_group")
		and not r.get("item") and not r.get("item_group")
		and not r.get("tax_category")
	]

	if generic_rules:
		rule_name = generic_rules[0].name
		if generic_rules[0].get(template_field) != template_name:
			frappe.db.set_value("Tax Rule", rule_name, template_field, template_name)
		for dup in generic_rules[1:]:
			frappe.delete_doc("Tax Rule", dup.name, ignore_permissions=True)
	else:
		try:
			rule = frappe.get_doc({
				"doctype": "Tax Rule",
				"tax_type": tax_type,
				"company": company,
				template_field: template_name,
				"priority": 1,
				"from_date": "2020-01-01",
				"use_for_shopping_cart": 0,
			})
			rule.insert(ignore_permissions=True)
		except Exception as e:
			frappe.logger("ism").warning(f"Could not create default {tax_type} Tax Rule for {company}: {e}")


def get_or_create_default_purchase_tax_template(company=None):
	"""Return the default 13% HST Purchase Taxes and Charges Template for the company.
	If not present, creates it along with required account and returns it.
	"""
	company = (
		company
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or "Lightspeed"
	)
	abbr = frappe.db.get_value("Company", company, "abbr") or company
	template_name = f"13% HST - {abbr}"

	if not frappe.db.exists("Purchase Taxes and Charges Template", template_name):
		ensure_company_13_percent_hst_setup(company)

	if frappe.db.exists("Purchase Taxes and Charges Template", template_name):
		return template_name

	# Fallback to any default purchase template
	return frappe.db.get_value(
		"Purchase Taxes and Charges Template",
		{"company": company, "is_default": 1},
		"name",
	)


def get_or_create_default_sales_tax_template(company=None):
	"""Return the default 13% HST Sales Taxes and Charges Template for the company."""
	company = (
		company
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or "Lightspeed"
	)
	abbr = frappe.db.get_value("Company", company, "abbr") or company
	template_name = f"13% HST - {abbr}"

	if not frappe.db.exists("Sales Taxes and Charges Template", template_name):
		ensure_company_13_percent_hst_setup(company)

	if frappe.db.exists("Sales Taxes and Charges Template", template_name):
		return template_name

	return frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "is_default": 1},
		"name",
	)


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
		{
			"fieldname": "custom_frt",
			"label": "Flat Rate Time (Hours)",
			"fieldtype": "Float",
			"insert_after": "revision",
		},
	]

	create_custom_fields({"Item": fields}, update=True)
	frappe.db.commit()


def backfill_service_package_models():
	"""Backfill applicable_model and applicable_generation on existing Service Packages and Service rows."""
	from ism.utils.tesla_manual import parse_tesla_manual_url

	if not frappe.db.has_column("Service Package", "applicable_model"):
		return 0

	frappe.db.commit()

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
		frappe.db.commit()

	return count


def setup_procurement_custom_fields():
	"""Ensure repair_order link custom fields exist on Purchase Order, Request for Quotation, and Sales Invoice DocTypes."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	po_fields = [
		{
			"fieldname": "repair_order",
			"label": "Repair Order",
			"fieldtype": "Link",
			"options": "Repair Order",
			"insert_after": "supplier",
			"in_standard_filter": 1,
		}
	]
	rfq_fields = [
		{
			"fieldname": "repair_order",
			"label": "Repair Order",
			"fieldtype": "Link",
			"options": "Repair Order",
			"insert_after": "company",
			"in_standard_filter": 1,
		}
	]
	si_fields = [
		{
			"fieldname": "repair_order",
			"label": "Repair Order",
			"fieldtype": "Link",
			"options": "Repair Order",
			"insert_after": "customer",
			"in_standard_filter": 1,
		}
	]
	create_custom_fields({
		"Purchase Order": po_fields,
		"Request for Quotation": rfq_fields,
		"Sales Invoice": si_fields,
	}, update=True)
	frappe.db.commit()


DEFAULT_EQUIPMENT_TAGS = [
	{
		"tag_name": "Lift",
		"description": "Standard automotive lift (2-post / 4-post)",
	},
	{
		"tag_name": "EV High Voltage",
		"description": "High voltage isolation and battery service equipment",
	},
	{
		"tag_name": "Alignment Rack",
		"description": "3D optical wheel alignment rack and sensor system",
	},
]


def seed_equipment_tags():
	"""Idempotently seed standard default Equipment Tag records."""
	if not frappe.db.exists("DocType", "Equipment Tag"):
		return
	for tag in DEFAULT_EQUIPMENT_TAGS:
		if not frappe.db.exists("Equipment Tag", tag["tag_name"]):
			doc = frappe.get_doc({
				"doctype": "Equipment Tag",
				"tag_name": tag["tag_name"],
				"description": tag["description"],
			})
			doc.insert(ignore_permissions=True)


def ensure_ism_roles():
	"""Ensure standard ISM roles exist with appropriate Desk access."""
	roles = [
		{"role_name": "ISM Manager", "desk_access": 1},
	]
	for r in roles:
		if not frappe.db.exists("Role", r["role_name"]):
			try:
				doc = frappe.get_doc({
					"doctype": "Role",
					"role_name": r["role_name"],
					"desk_access": r["desk_access"],
				})
				doc.insert(ignore_permissions=True)
			except Exception as e:
				frappe.logger("ism").warning(f"Could not seed role {r['role_name']}: {e}")


STANDARD_SCHEDULE_ENTRY_TYPES = [
	{
		"type_name": "Diagnostic",
		"color": "#1f538d",
		"requires_repair_order": 0,
		"description": "Initial vehicle inspection and diagnostic appointment",
	},
	{
		"type_name": "Repair",
		"color": "#2e7d32",
		"requires_repair_order": 1,
		"description": "Authorized vehicle repair service",
	},
	{
		"type_name": "Maintenance",
		"color": "#0284c7",
		"requires_repair_order": 1,
		"description": "Scheduled maintenance and inspection service",
	},
	{
		"type_name": "Meeting",
		"color": "#7b1fa2",
		"requires_repair_order": 0,
		"description": "Internal staff or shop meeting",
	},
	{
		"type_name": "Internal Service",
		"color": "#ef6c00",
		"requires_repair_order": 0,
		"description": "Internal shop vehicle service or bay maintenance",
	},
]


def seed_schedule_entry_types():
	"""Seed standard Schedule Entry Types idempotently."""
	if not frappe.db.exists("DocType", "Schedule Entry Type"):
		return

	for item in STANDARD_SCHEDULE_ENTRY_TYPES:
		if not frappe.db.exists("Schedule Entry Type", item["type_name"]):
			doc = frappe.get_doc({
				"doctype": "Schedule Entry Type",
				"type_name": item["type_name"],
				"requires_repair_order": item["requires_repair_order"],
				"color": item["color"],
				"description": item["description"],
			})
			doc.insert(ignore_permissions=True)
		else:
			existing = frappe.get_doc("Schedule Entry Type", item["type_name"])
			updated = False
			if existing.requires_repair_order != item["requires_repair_order"]:
				existing.requires_repair_order = item["requires_repair_order"]
				updated = True
			if existing.color != item["color"]:
				existing.color = item["color"]
				updated = True
			if existing.description != item["description"]:
				existing.description = item["description"]
				updated = True
			if updated:
				existing.save(ignore_permissions=True)


def after_migrate():
	"""Run migrations and setup tasks for ISM."""
	# 1. Core Scheduling Seeders & ISM Settings (must execute first and idempotently)
	ensure_ism_roles()
	setup_default_ism_settings()
	seed_equipment_tags()
	seed_schedule_entry_types()

	# 2. Master Setup & DocType Permissions
	create_standard_inspection_template()
	ensure_doctype_creation_enabled()

	# 3. ERPNext Integration & Custom Field Definitions
	cleanup_legacy_custom_fields()
	setup_default_taxes_and_payments()
	setup_parts_item_group()
	setup_item_custom_fields()
	setup_procurement_custom_fields()

	# 4. Historical Data Backfills
	backfill_service_package_models()


def after_install():
	"""Run installation tasks for ISM on fresh site installs."""
	after_migrate()





