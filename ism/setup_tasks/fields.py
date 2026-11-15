# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe

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


def cleanup_legacy_custom_fields():
	"""Safely remove legacy custom fields injected on standard ERPNext sales doctypes in one batch query."""
	target_pairs = {(dt, fn) for dt, fns in LEGACY_FIELDS_TO_REMOVE.items() for fn in fns}
	if not target_pairs:
		return

	existing = frappe.get_all(
		"Custom Field",
		filters={"dt": ["in", list(LEGACY_FIELDS_TO_REMOVE.keys())]},
		fields=["name", "dt", "fieldname"],
	)
	for cf in existing:
		if (cf.dt, cf.fieldname) in target_pairs:
			frappe.delete_doc("Custom Field", cf.name, force=True, ignore_permissions=True)


def ensure_doctype_creation_enabled():
	"""Ensure ISM DocTypes allow document creation by users (in_create must be 0)."""
	if not frappe.db.count("DocType", {"module": "ISM", "in_create": 1}):
		return

	dt = frappe.qb.DocType("DocType")
	(
		frappe.qb.update(dt)
		.set(dt.in_create, 0)
		.where((dt.module == "ISM") & (dt.in_create == 1))
	).run()
	frappe.clear_cache()


def _ensure_custom_fields_present(custom_fields_by_doctype, force_update=False):
	"""Create custom fields only when missing unless force_update is requested."""
	if not force_update and custom_fields_by_doctype:
		existing_rows = frappe.get_all(
			"Custom Field",
			filters={"dt": ["in", list(custom_fields_by_doctype.keys())]},
			fields=["dt", "fieldname"],
		)
		existing_pairs = {(row.dt, row.fieldname) for row in existing_rows}
		missing = any(
			(dt, field["fieldname"]) not in existing_pairs
			for dt, fields in custom_fields_by_doctype.items()
			for field in fields
		)
		if not missing:
			return

	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(custom_fields_by_doctype, update=True)


def setup_item_custom_fields(force_update=False):
	"""Ensure automotive compliance custom fields exist on Item DocType."""
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
	_ensure_custom_fields_present({"Item": fields}, force_update=force_update)


def _repair_order_link_field(insert_after):
	"""Return canonical Repair Order Link custom field definition."""
	return [
		{
			"fieldname": "repair_order",
			"label": "Repair Order",
			"fieldtype": "Link",
			"options": "Repair Order",
			"insert_after": insert_after,
			"in_standard_filter": 1,
		}
	]


def setup_procurement_custom_fields(force_update=False):
	"""Ensure repair_order link custom fields exist on Purchase Order, Request for Quotation, and Sales Invoice."""
	_ensure_custom_fields_present(
		{
			"Purchase Order": _repair_order_link_field("supplier"),
			"Request for Quotation": _repair_order_link_field("company"),
			"Sales Invoice": _repair_order_link_field("customer"),
		},
		force_update=force_update,
	)


def backfill_service_package_models():
	"""Backfill applicable_model and applicable_generation on existing Service Packages and Service rows."""
	from ism.utils.tesla_manual import parse_tesla_manual_url

	if not frappe.db.has_column("Service Package", "applicable_model"):
		return 0

	count = 0
	packages = frappe.get_all(
		"Service Package",
		fields=["name", "tesla_manual_url", "applicable_model", "applicable_generation"],
	)
	for p in packages:
		if not p.applicable_model and p.tesla_manual_url:
			url_meta = parse_tesla_manual_url(p.tesla_manual_url)
			model = url_meta.get("applicable_model")
			gen = url_meta.get("matched_generation")
			if model:
				frappe.db.set_value(
					"Service Package",
					p.name,
					{
						"applicable_model": model,
						"applicable_generation": gen or "",
					},
					update_modified=False,
				)
				count += 1

	if frappe.db.has_column("Repair Order Service", "applicable_model"):
		frappe.db.sql(
			"""
			UPDATE `tabRepair Order Service` ros
			JOIN `tabService Package` sp ON ros.service_package = sp.name
			SET ros.applicable_model = sp.applicable_model
			WHERE (ros.applicable_model IS NULL OR ros.applicable_model = '')
			AND (sp.applicable_model IS NOT NULL AND sp.applicable_model != '')
			"""
		)

	return count
