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
		{
			"fieldname": "default_bin_location",
			"label": "Default Bin Location",
			"fieldtype": "Link",
			"options": "Storage Bin",
			"insert_after": "custom_frt",
			"in_standard_filter": 1,
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
	"""Backfill applicable_model, applicable_generation, and generation_compatibility rows."""
	from ism.utils.tesla_manual import parse_tesla_manual_url, resolve_generation_entries

	if not frappe.db.has_column("Service Package", "applicable_model"):
		return 0

	has_compat_table = frappe.db.exists("DocType", "Service Package Generation") and frappe.db.table_exists(
		"Service Package Generation"
	)
	existing_compat_parents = set()
	if has_compat_table:
		existing_compat_parents = set(
			frappe.get_all(
				"Service Package Generation",
				filters={"parenttype": "Service Package"},
				pluck="parent",
			)
		)

	count = 0
	packages = frappe.get_all(
		"Service Package",
		fields=["name", "tesla_manual_url", "applicable_model", "applicable_generation"],
	)
	for p in packages:
		url_meta = parse_tesla_manual_url(p.tesla_manual_url) if p.tesla_manual_url else {}
		cur_model = (p.applicable_model or "").strip()
		cur_gen = (p.applicable_generation or "").strip()
		new_model = cur_model or (url_meta.get("applicable_model") or "")
		new_gen = cur_gen
		url_full_gen = (url_meta.get("applicable_generation") or url_meta.get("matched_generation") or "").strip()
		url_first_gen = (url_meta.get("matched_generation") or "").strip()

		if not new_gen and url_full_gen:
			new_gen = url_full_gen
		elif new_gen and url_full_gen and new_gen == url_first_gen and url_full_gen != url_first_gen:
			new_gen = url_full_gen

		updates = {}
		if new_model and new_model != cur_model:
			updates["applicable_model"] = new_model
		if new_gen and new_gen != cur_gen:
			updates["applicable_generation"] = new_gen
		if updates:
			frappe.db.set_value("Service Package", p.name, updates, update_modified=False)
			count += 1

		if has_compat_table and p.name not in existing_compat_parents and (new_gen or p.tesla_manual_url):
			entries = resolve_generation_entries(new_gen, model=new_model, manual_url=p.tesla_manual_url)
			for idx, entry in enumerate(entries, start=1):
				frappe.get_doc(
					{
						"doctype": "Service Package Generation",
						"parent": p.name,
						"parenttype": "Service Package",
						"parentfield": "generation_compatibility",
						"idx": idx,
						"applicable_model": entry["applicable_model"],
						"generation_name": entry["generation_name"],
						"date_range": entry.get("date_range") or "",
						"year_from": entry.get("year_from"),
						"month_from": entry.get("month_from"),
						"year_to": entry.get("year_to"),
						"month_to": entry.get("month_to"),
						"manual_base_url": entry.get("manual_base_url") or "",
					}
				).db_insert()
			if entries and not updates:
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

	if frappe.db.has_column("Repair Order Service", "applicable_generation") and frappe.db.has_column(
		"Service Package", "applicable_generation"
	):
		frappe.db.sql(
			"""
			UPDATE `tabRepair Order Service` ros
			JOIN `tabService Package` sp ON ros.service_package = sp.name
			SET ros.applicable_generation = sp.applicable_generation
			WHERE (ros.applicable_generation IS NULL OR ros.applicable_generation = '')
			AND (sp.applicable_generation IS NOT NULL AND sp.applicable_generation != '')
			"""
		)

	return count


def backfill_repair_vehicle_generations():
	"""Backfill vehicle_generation and recommended_manual_url on existing Repair Vehicle records."""
	from ism.utils.tesla_manual import get_tesla_manual_details_for_vehicle

	if not frappe.db.has_column("Repair Vehicle", "vehicle_generation"):
		return 0

	has_url_col = frappe.db.has_column("Repair Vehicle", "recommended_manual_url")
	fields = ["name", "model", "model_year", "manufactured_month", "drive_unit", "vehicle_generation"]
	if has_url_col:
		fields.append("recommended_manual_url")

	legacy_broken_url = "https://service.tesla.com/docs/ModelX/Palladium/ServiceManual/en-us/"
	count = 0
	for veh in frappe.get_all("Repair Vehicle", fields=fields):
		details = get_tesla_manual_details_for_vehicle(veh)
		updates = {}
		if not (veh.get("vehicle_generation") or "").strip() and details.get("vehicle_generation"):
			updates["vehicle_generation"] = details["vehicle_generation"]
		if has_url_col:
			cur_url = (veh.get("recommended_manual_url") or "").strip()
			if (not cur_url or cur_url == legacy_broken_url) and details.get("recommended_manual_url"):
				updates["recommended_manual_url"] = details["recommended_manual_url"]
		if updates:
			frappe.db.set_value("Repair Vehicle", veh["name"], updates, update_modified=False)
			count += 1

	return count
