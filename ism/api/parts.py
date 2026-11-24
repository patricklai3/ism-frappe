# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe

from ism.api._validation import optional_str as _optional_str, require_str as _require_str, scalar_str as _scalar_str
from ism.api.parts_catalog import (
	LHRH_PATTERN,
	LH_ONLY_PATTERN,
	RH_ONLY_PATTERN,
	_attach_part_to_service_package,
	_backfill_existing_item_fields,
	_coerce_bulk_import_inputs,
	_coerce_item_qty,
	_ensure_item_custom_fields,
	_first_present_flt,
	_import_part_record,
	_link_item_to_packages,
	_normalize_part_condition,
	_normalize_part_source,
	_parse_raw_import_lines,
	_parse_service_package_names,
	_process_structured_import_row,
	_resolve_target_slot,
	bulk_import_parts as _bulk_import_parts_impl,
	detect_part_side,
	format_item_code,
	format_slot_label,
	get_component_alternates as _get_component_alternates_impl,
	has_lhrh_designation,
	link_part_to_service_packages as _link_part_to_service_packages_impl,
	parse_slot_number,
	quick_create_part as _quick_create_part_impl,
	quick_create_service_package as _quick_create_service_package_impl,
	resolve_part_side,
	search_service_packages as _search_service_packages_impl,
)
from ism.api.parts_procurement import (
	_apply_purchase_tax_template,
	_build_purchase_item_row,
	_coerce_bool_param,
	_create_purchase_order,
	_create_rfq,
	_get_items_supplier_map,
	_resolve_active_company,
	_resolve_mode_of_payment,
	_resolve_supplier_payment_account,
	create_purchase_invoice as _create_purchase_invoice_impl,
	create_purchase_payment as _create_purchase_payment_impl,
	ensure_supplier_and_link_to_item as _ensure_supplier_and_link_to_item_impl,
	get_item_procurement_info as _get_item_procurement_info_impl,
	get_repair_order_procurement_data as _get_repair_order_procurement_data_impl,
	submit_procurement_orders as _submit_procurement_orders_impl,
)
from ism.api.parts_stock import (
	_extract_ro_metadata,
	_find_company_warehouse,
	_get_item_stock_qty,
	_get_items_stock_qty_map,
	_get_prior_approved_ros,
	_parse_parts_json,
	_require_any_read_permission,
	_resolve_default_warehouse,
	get_default_warehouse as _get_default_warehouse_impl,
	get_item_available_stock,
	get_items_available_stock_batch,
	get_repair_order_committed_qty,
	get_repair_order_committed_qty_batch,
	get_repair_order_parts_stock as _get_repair_order_parts_stock_impl,
)

__all__ = [
	"LHRH_PATTERN",
	"LH_ONLY_PATTERN",
	"RH_ONLY_PATTERN",
	"_apply_purchase_tax_template",
	"_attach_part_to_service_package",
	"_backfill_existing_item_fields",
	"_build_purchase_item_row",
	"_coerce_bool_param",
	"_coerce_bulk_import_inputs",
	"_coerce_item_qty",
	"_create_purchase_order",
	"_create_rfq",
	"_ensure_item_custom_fields",
	"_extract_ro_metadata",
	"_find_company_warehouse",
	"_first_present_flt",
	"_get_item_stock_qty",
	"_get_items_stock_qty_map",
	"_get_items_supplier_map",
	"_get_prior_approved_ros",
	"_import_part_record",
	"_link_item_to_packages",
	"_normalize_part_condition",
	"_normalize_part_source",
	"_optional_str",
	"_parse_parts_json",
	"_parse_raw_import_lines",
	"_parse_service_package_names",
	"_process_structured_import_row",
	"_require_any_read_permission",
	"_require_str",
	"_resolve_active_company",
	"_resolve_default_warehouse",
	"_resolve_mode_of_payment",
	"_resolve_supplier_payment_account",
	"_resolve_target_slot",
	"_scalar_str",
	"bulk_import_parts",
	"create_purchase_invoice",
	"create_purchase_payment",
	"detect_part_side",
	"ensure_supplier_and_link_to_item",
	"format_item_code",
	"format_slot_label",
	"get_component_alternates",
	"get_default_warehouse",
	"get_item_available_stock",
	"get_item_procurement_info",
	"get_items_available_stock_batch",
	"get_repair_order_committed_qty",
	"get_repair_order_committed_qty_batch",
	"get_repair_order_parts_stock",
	"get_repair_order_procurement_data",
	"has_lhrh_designation",
	"link_part_to_service_packages",
	"parse_slot_number",
	"quick_create_part",
	"quick_create_service_package",
	"resolve_part_side",
	"search_service_packages",
	"submit_procurement_orders",
]


@frappe.whitelist()
def quick_create_part(
	part_number,
	part_name=None,
	part_source="OEM",
	part_condition="New",
	oem_part_number=None,
	revision=None,
	brand=None,
	warranty_period="12 Months / 20,000 km",
	unit_rate=0.0,
	valuation_rate=0.0,
	stock_uom="Nos",
	slot_number=None,
	component_slot=None,
	service_package=None,
	service_packages=None,
	side="Both",
	is_default=0,
	qty=1.0,
):
	"""Fast single part creator that ensures standard ERPNext inventory defaults,
	automotive compliance custom fields, and optional auto-linking to Service Packages.
	"""
	return _quick_create_part_impl(
		part_number=part_number,
		part_name=part_name,
		part_source=part_source,
		part_condition=part_condition,
		oem_part_number=oem_part_number,
		revision=revision,
		brand=brand,
		warranty_period=warranty_period,
		unit_rate=unit_rate,
		valuation_rate=valuation_rate,
		stock_uom=stock_uom,
		slot_number=slot_number,
		component_slot=component_slot,
		service_package=service_package,
		service_packages=service_packages,
		side=side,
		is_default=is_default,
		qty=qty,
	)


@frappe.whitelist()
def bulk_import_parts(
	items=None,
	raw_text=None,
	service_package=None,
	service_packages=None,
	slot_number=None,
	component_slot=None,
	default_source="OEM",
	default_condition="New",
	side="Both",
	supplier=None,
):
	"""Batch-create/update parts from structured JSON items (primary) or delimited text.

	Structured `items` accepts a list[dict], single dict, or JSON string.
	Legacy delimited `raw_text` (or positional string passed as `items`) parses:
	Part# | Description | Source | Condition | Rate | Cost | Qty | Revision | OEM#
	"""
	return _bulk_import_parts_impl(
		items=items,
		raw_text=raw_text,
		service_package=service_package,
		service_packages=service_packages,
		slot_number=slot_number,
		component_slot=component_slot,
		default_source=default_source,
		default_condition=default_condition,
		side=side,
		supplier=supplier,
	)


@frappe.whitelist()
def get_component_alternates(
	service_package=None,
	component_slot=None,
	slot_number=None,
	oem_part_number=None,
	item_code=None,
):
	"""Return all compatible items for a component slot, including condition variants and redesigns.
	Also includes unassigned compatible parts from the Service Package and batches stock/item reads.
	"""
	return _get_component_alternates_impl(
		service_package=service_package,
		component_slot=component_slot,
		slot_number=slot_number,
		oem_part_number=oem_part_number,
		item_code=item_code,
	)


@frappe.whitelist()
def link_part_to_service_packages(
	item_code,
	service_packages=None,
	slot_number=None,
	component_slot=None,
	qty=1.0,
	rate=None,
	side="Both",
	is_default=0,
):
	"""Link an item to multiple Service Packages as a compatible part.
	The primary package receives slot_number (default 1); secondary packages get Unassigned (0).
	"""
	return _link_part_to_service_packages_impl(
		item_code=item_code,
		service_packages=service_packages,
		slot_number=slot_number,
		component_slot=component_slot,
		qty=qty,
		rate=rate,
		side=side,
		is_default=is_default,
	)


@frappe.whitelist()
def search_service_packages(
	query="",
	limit=25,
	applicable_model=None,
	applicable_generation=None,
	vehicle_year=None,
	vehicle=None,
):
	"""Search for Service Packages by title, correction code, model, generation, or vehicle."""
	return _search_service_packages_impl(
		query=query,
		limit=limit,
		applicable_model=applicable_model,
		applicable_generation=applicable_generation,
		vehicle_year=vehicle_year,
		vehicle=vehicle,
	)


@frappe.whitelist()
def quick_create_service_package(
	package_name,
	correction_code=None,
	applicable_model=None,
	applicable_generation=None,
	standard_labor_hours=1.0,
	hourly_rate=120.0,
	is_flat_rate=0,
):
	"""Fast creation of a Service Package directly from parts or service tooling."""
	return _quick_create_service_package_impl(
		package_name=package_name,
		correction_code=correction_code,
		applicable_model=applicable_model,
		applicable_generation=applicable_generation,
		standard_labor_hours=standard_labor_hours,
		hourly_rate=hourly_rate,
		is_flat_rate=is_flat_rate,
	)


@frappe.whitelist()
def ensure_supplier_and_link_to_item(supplier_name, item_code=None, supplier_part_no=None):
	"""Ensure a Supplier exists, and link it to Item's supplier_items child table and default_supplier."""
	return _ensure_supplier_and_link_to_item_impl(
		supplier_name=supplier_name,
		item_code=item_code,
		supplier_part_no=supplier_part_no,
	)


@frappe.whitelist()
def get_repair_order_parts_stock(ro_name=None, items=None, company=None):
	"""Whitelisted API to fetch live FIFO stock and commitment data for repair order parts."""
	return _get_repair_order_parts_stock_impl(ro_name=ro_name, items=items, company=company)


@frappe.whitelist()
def get_item_procurement_info(item_code, company=None, ro_name=None):
	"""Query stock levels, known suppliers from Item Supplier child table, and default supplier for an Item."""
	return _get_item_procurement_info_impl(item_code=item_code, company=company, ro_name=ro_name)


@frappe.whitelist()
def get_repair_order_procurement_data(docname):
	"""Gather all required parts from approved services of a Repair Order with current stock & supplier details."""
	return _get_repair_order_procurement_data_impl(docname=docname)


@frappe.whitelist()
def submit_procurement_orders(repair_order=None, parts_data=None, submit=True, company=None):
	"""Create (and optionally submit) Purchase Orders (grouped by supplier) and RFQs, optionally linked to a Repair Order."""
	return _submit_procurement_orders_impl(
		repair_order=repair_order,
		parts_data=parts_data,
		submit=submit,
		company=company,
	)


@frappe.whitelist()
def get_default_warehouse(company=None):
	"""Dynamically resolve a valid, existing non-group warehouse.
	If company is specified, the warehouse MUST belong to that company.
	"""
	return _get_default_warehouse_impl(company=company)


@frappe.whitelist()
def create_purchase_payment(
	purchase_order=None,
	purchase_invoice=None,
	payment_method=None,
	paid_amount=None,
	reference_no=None,
	reference_date=None,
):
	"""Create and immediately submit a supplier Payment Entry against a Purchase Order or Purchase Invoice."""
	return _create_purchase_payment_impl(
		purchase_order=purchase_order,
		purchase_invoice=purchase_invoice,
		payment_method=payment_method,
		paid_amount=paid_amount,
		reference_no=reference_no,
		reference_date=reference_date,
	)


@frappe.whitelist()
def create_purchase_invoice(
	purchase_order=None,
	supplier=None,
	parts_data=None,
	update_stock=False,
	company=None,
	bill_no=None,
	bill_date=None,
	attach_advances=True,
):
	"""Create and immediately submit a Purchase Invoice (from PO or standalone), optionally updating stock and attaching PO advances."""
	return _create_purchase_invoice_impl(
		purchase_order=purchase_order,
		supplier=supplier,
		parts_data=parts_data,
		update_stock=update_stock,
		company=company,
		bill_no=bill_no,
		bill_date=bill_date,
		attach_advances=attach_advances,
	)
