# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

import re

import frappe
from frappe.utils import cint

BIN_DIMENSION_NAME = "Bin Location"
BIN_REFERENCE_DOCTYPE = "Storage Bin"
DEFAULT_BIN_FIELDNAME = "bin_location"
SLE_BIN_INDEX_NAME = "idx_ism_sle_item_wh_bin"

_SAFE_COL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ensure_sle_bin_index(sle_fieldname: str) -> None:
	"""Ensure composite index (item_code, warehouse, is_cancelled, <sle_fieldname>) exists on SLE."""
	col = (sle_fieldname or "").strip()
	if not col or not _SAFE_COL_RE.fullmatch(col):
		return
	if not frappe.db.has_column("Stock Ledger Entry", col):
		return

	existing = frappe.db.sql(
		"SHOW INDEX FROM `tabStock Ledger Entry` WHERE Key_name = %s",
		(SLE_BIN_INDEX_NAME,),
	)
	if existing:
		return

	frappe.db.sql_ddl(
		f"CREATE INDEX `{SLE_BIN_INDEX_NAME}` ON `tabStock Ledger Entry` "
		f"(`item_code`, `warehouse`, `is_cancelled`, `{col}`)"
	)


def setup_inventory_dimensions(force_update: bool = False) -> str | None:
	"""Idempotently configure ERPNext Inventory Dimension 'Bin Location' -> 'Storage Bin' and SLE index."""
	if not frappe.db.exists("DocType", "Inventory Dimension") or not frappe.db.exists(
		"DocType", BIN_REFERENCE_DOCTYPE
	):
		return None

	existing_name = frappe.db.get_value(
		"Inventory Dimension", {"dimension_name": BIN_DIMENSION_NAME}, "name"
	) or frappe.db.get_value(
		"Inventory Dimension", {"reference_document": BIN_REFERENCE_DOCTYPE}, "name"
	)

	has_disabled_col = frappe.db.has_column("Inventory Dimension", "disabled")
	changed = False
	if not existing_name:
		dim_payload = {
			"doctype": "Inventory Dimension",
			"dimension_name": BIN_DIMENSION_NAME,
			"reference_document": BIN_REFERENCE_DOCTYPE,
			"apply_to_all_doctypes": 1,
			"validate_negative_stock": 1,
		}
		if has_disabled_col:
			dim_payload["disabled"] = 0
		dim = frappe.get_doc(dim_payload)
		dim.insert(ignore_permissions=True)
		changed = True
	else:
		dim = frappe.get_doc("Inventory Dimension", existing_name)
		dirty = False
		if cint(getattr(dim, "validate_negative_stock", 0)) != 1:
			dim.validate_negative_stock = 1
			dirty = True
		if force_update and has_disabled_col and cint(getattr(dim, "disabled", 0)) == 1:
			dim.disabled = 0
			dirty = True
		if force_update and cint(getattr(dim, "apply_to_all_doctypes", 0)) != 1:
			dim.apply_to_all_doctypes = 1
			dirty = True

		sle_col = (getattr(dim, "source_fieldname", None) or DEFAULT_BIN_FIELDNAME).strip()
		if not frappe.db.has_column("Stock Ledger Entry", sle_col):
			dirty = True

		if dirty:
			dim.save(ignore_permissions=True)
			changed = True

	sle_fieldname = (getattr(dim, "source_fieldname", None) or DEFAULT_BIN_FIELDNAME).strip()
	_ensure_sle_bin_index(sle_fieldname)

	if changed:
		from ism.api.parts_stock import clear_bin_dimension_meta_cache

		clear_bin_dimension_meta_cache()

	return dim.name
