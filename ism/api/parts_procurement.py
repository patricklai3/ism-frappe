# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, today

from ism.api._validation import (
	coerce_bool,
	get_active_company,
	optional_flt,
	optional_str,
	parse_json_arg,
	require_str,
)
from ism.api.parts_stock import (
	_extract_ro_metadata,
	_parse_parts_json,
	_resolve_default_warehouse,
	get_item_available_stock,
	get_items_available_stock_batch,
)

_VALID_ORDER_MODES = ("Purchase Order", "Request for Quotation")


def _resolve_active_company(company=None):
	"""Return explicit company or active shop default Company."""
	return get_active_company(company)


def _coerce_bool_param(value, default=False):
	return coerce_bool(value, default=default)


def _get_items_supplier_map(item_codes):
	"""Batch load known suppliers and default supplier for a list of Item codes."""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	supplier_map = {code: {"known_suppliers": [], "default_supplier": None} for code in codes}
	if not codes:
		return supplier_map

	supplier_rows = frappe.get_all(
		"Item Supplier",
		filters={"parent": ["in", codes], "parenttype": "Item"},
		fields=["parent", "supplier", "idx"],
		order_by="parent asc, idx asc",
	)
	for row in supplier_rows:
		parent = row.get("parent")
		sup = row.get("supplier")
		if parent in supplier_map and sup and sup not in supplier_map[parent]["known_suppliers"]:
			supplier_map[parent]["known_suppliers"].append(sup)

	default_rows = frappe.get_all(
		"Item Default",
		filters={"parent": ["in", codes], "parenttype": "Item"},
		fields=["parent", "default_supplier", "idx"],
		order_by="parent asc, idx asc",
	)
	for row in default_rows:
		parent = row.get("parent")
		sup = row.get("default_supplier")
		if parent in supplier_map and sup and not supplier_map[parent]["default_supplier"]:
			supplier_map[parent]["default_supplier"] = sup

	for info in supplier_map.values():
		if not info["default_supplier"] and info["known_suppliers"]:
			info["default_supplier"] = info["known_suppliers"][0]

	return supplier_map


def _ensure_supplier_exists(supplier_name: str) -> str:
	if frappe.db.exists("Supplier", supplier_name):
		frappe.has_permission("Supplier", "read", doc=supplier_name, throw=True)
		return supplier_name

	frappe.has_permission("Supplier", "create", throw=True)
	sup_group = frappe.db.get_single_value("Buying Settings", "supplier_group") or "All Supplier Groups"
	if not frappe.db.exists("Supplier Group", sup_group):
		sup_group = frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups"

	sup = frappe.new_doc("Supplier")
	sup.supplier_name = supplier_name
	sup.supplier_group = sup_group
	try:
		sup.insert()
	except frappe.DuplicateEntryError:
		frappe.has_permission("Supplier", "read", doc=supplier_name, throw=True)
	return supplier_name


def _apply_supplier_to_item_doc(
	item_doc: Any,
	supplier_name: str,
	supplier_part_no: Optional[str] = None,
	company: Optional[str] = None,
) -> bool:
	changed = False
	existing_suppliers = [getattr(row, "supplier", None) for row in getattr(item_doc, "supplier_items", [])]
	if supplier_name not in existing_suppliers:
		item_doc.append(
			"supplier_items",
			{
				"supplier": supplier_name,
				"supplier_part_no": supplier_part_no or "",
			},
		)
		changed = True

	if hasattr(item_doc, "item_defaults"):
		if item_doc.item_defaults:
			for d in item_doc.item_defaults:
				if not getattr(d, "default_supplier", None):
					d.default_supplier = supplier_name
					changed = True
		else:
			active_co = company or (
				frappe.defaults.get_user_default("Company")
				or frappe.db.get_single_value("Global Defaults", "default_company")
				or frappe.db.get_value("Company", {"is_group": 0}, "name")
			)
			if active_co and frappe.db.exists("Company", active_co):
				item_doc.append(
					"item_defaults",
					{
						"company": active_co,
						"default_supplier": supplier_name,
					},
				)
				changed = True
	return changed


@frappe.whitelist()
def ensure_supplier_and_link_to_item(supplier_name, item_code=None, supplier_part_no=None):
	"""Ensure a Supplier exists, and link it to Item's supplier_items child table and default_supplier."""
	s_name = optional_str(supplier_name, "Supplier")
	if not s_name:
		return None
	item_code = optional_str(item_code, "Item Code")
	supplier_part_no = optional_str(supplier_part_no, "Supplier Part No")

	# Validate Item permissions before creating Supplier so failed Item writes never leave orphan suppliers.
	item_doc = None
	changed = False
	if item_code and frappe.db.exists("Item", item_code):
		item_doc = frappe.get_doc("Item", item_code)
		item_doc.check_permission("read")
		changed = _apply_supplier_to_item_doc(item_doc, s_name, supplier_part_no)
		if changed:
			item_doc.check_permission("write")

	_ensure_supplier_exists(s_name)

	if item_doc and changed:
		item_doc.save()

	return s_name


def _resolve_item_purchase_rate(item_meta: Any, explicit_rate: Any = None) -> float:
	if explicit_rate is not None and str(explicit_rate).strip() != "":
		return optional_flt(explicit_rate, "Rate", non_negative=True, default=0.0) or 0.0
	return flt(
		getattr(item_meta, "last_purchase_rate", 0.0)
		or getattr(item_meta, "valuation_rate", 0.0)
		or getattr(item_meta, "standard_rate", 0.0)
	)


@frappe.whitelist()
def get_item_procurement_info(item_code, company=None, ro_name=None):
	"""Query stock levels, known suppliers from Item Supplier child table, and default supplier for an Item."""
	frappe.has_permission("Item", "read", throw=True)
	item_code = optional_str(item_code, "Item Code")
	company = optional_str(company, "Company")
	ro_name = optional_str(ro_name, "Repair Order")
	if ro_name:
		frappe.has_permission("Repair Order", "read", throw=True)
		if not frappe.db.exists("Repair Order", ro_name):
			frappe.throw(_("Repair Order {0} not found.").format(ro_name), frappe.DoesNotExistError)
		frappe.has_permission("Repair Order", "read", doc=ro_name, throw=True)

	if not item_code or not frappe.db.exists("Item", item_code):
		return {}

	if company and not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist.").format(company), frappe.DoesNotExistError)

	item_doc = frappe.get_cached_doc("Item", item_code)
	item_doc.check_permission("read")
	wh = _resolve_default_warehouse(company)
	avail_info = get_item_available_stock(item_code, wh, ro_name)
	sup_info = _get_items_supplier_map([item_code])[item_code]

	return {
		"item_code": item_code,
		"item_name": item_doc.item_name,
		"stock_qty": flt(avail_info["available_qty"]),
		"actual_qty": flt(avail_info["actual_qty"]),
		"committed_qty": flt(avail_info["committed_qty"]),
		"warehouse": wh,
		"default_supplier": sup_info["default_supplier"],
		"known_suppliers": sup_info["known_suppliers"],
		"rate": _resolve_item_purchase_rate(item_doc),
		"uom": getattr(item_doc, "stock_uom", "Nos"),
	}


@frappe.whitelist()
def get_repair_order_procurement_data(docname):
	"""Gather all required parts from approved services of a Repair Order with current stock & supplier details."""
	frappe.has_permission("Repair Order", "read", throw=True)
	frappe.has_permission("Item", "read", throw=True)
	docname = require_str(docname, "Repair Order")
	if not frappe.db.exists("Repair Order", docname):
		frappe.throw(_("Repair Order {0} not found.").format(docname), frappe.DoesNotExistError)

	ro = frappe.get_doc("Repair Order", docname)
	ro.check_permission("read")
	approved_services = [s for s in (ro.services or []) if s.status == "Approved"]
	if not approved_services:
		return {
			"repair_order": docname,
			"company": ro.company,
			"parts": [],
			"message": _("No approved services found on this Repair Order."),
		}

	raw_entries = []
	candidate_codes = []
	seen_codes = set()
	for svc in approved_services:
		for p in _parse_parts_json(svc.parts):
			if not isinstance(p, dict):
				continue
			code = p.get("item_code")
			if not code or not isinstance(code, str):
				continue
			raw_entries.append((code, p))
			if code not in seen_codes:
				seen_codes.add(code)
				candidate_codes.append(code)

	if not candidate_codes:
		return {
			"repair_order": docname,
			"company": ro.company,
			"warehouse": _resolve_default_warehouse(ro.company),
			"parts": [],
		}

	wh = _resolve_default_warehouse(ro.company)
	item_rows = frappe.get_list(
		"Item",
		filters={"name": ["in", candidate_codes]},
		fields=[
			"name",
			"item_name",
			"is_stock_item",
			"last_purchase_rate",
			"valuation_rate",
			"standard_rate",
			"stock_uom",
		],
	)
	item_meta_map = {row.name: row for row in item_rows if cint(row.is_stock_item) == 1}
	valid_codes = [c for c in candidate_codes if c in item_meta_map]

	stock_map = get_items_available_stock_batch(
		valid_codes,
		warehouse=wh,
		ro_name=docname,
		ro_data=_extract_ro_metadata(ro),
	)
	supplier_map = _get_items_supplier_map(valid_codes)

	items_map = {}
	for code, part in raw_entries:
		if code not in item_meta_map:
			continue
		item_meta = item_meta_map[code]
		req_qty = flt(part.get("qty", 1.0))
		if code not in items_map:
			avail_info = stock_map.get(code, {})
			sup_info = supplier_map.get(code, {})
			items_map[code] = {
				"item_code": code,
				"item_name": part.get("item_name") or item_meta.item_name,
				"qty": 0.0,
				"stock_qty": flt(avail_info.get("available_qty", 0.0)),
				"actual_qty": flt(avail_info.get("actual_qty", 0.0)),
				"committed_qty": flt(avail_info.get("committed_qty", 0.0)),
				"rate": _resolve_item_purchase_rate(item_meta, part.get("rate")),
				"uom": part.get("uom") or getattr(item_meta, "stock_uom", "Nos"),
				"default_supplier": sup_info.get("default_supplier"),
				"known_suppliers": sup_info.get("known_suppliers", []),
				"order_mode": "Purchase Order",
			}
		items_map[code]["qty"] += req_qty

	parts_list = list(items_map.values())
	for p in parts_list:
		p["is_deficit"] = 1 if p["stock_qty"] < p["qty"] else 0

	return {
		"repair_order": docname,
		"company": ro.company,
		"warehouse": wh,
		"parts": parts_list,
	}


def _build_purchase_item_row(item_input, target_wh, include_schedule_date=False):
	item_doc = frappe.get_cached_doc("Item", item_input["item_code"])
	rate = _resolve_item_purchase_rate(item_doc, item_input.get("rate"))
	raw_qty = 1.0 if item_input.get("qty") is None else item_input.get("qty")
	qty = optional_flt(raw_qty, "Quantity", positive=True, default=1.0)
	req_wh = optional_str(item_input.get("warehouse"), "Warehouse")
	resolved_wh = (
		req_wh
		if req_wh and frappe.db.exists("Warehouse", {"name": req_wh, "is_group": 0})
		else target_wh
	)
	row = {
		"item_code": item_input["item_code"],
		"item_name": optional_str(item_input.get("item_name"), "Item Name") or item_doc.item_name,
		"qty": qty,
		"rate": rate,
		"uom": optional_str(item_input.get("uom"), "UOM") or getattr(item_doc, "stock_uom", "Nos"),
		"stock_uom": getattr(item_doc, "stock_uom", "Nos"),
		"conversion_factor": 1.0,
		"warehouse": resolved_wh,
	}
	if include_schedule_date:
		row["schedule_date"] = today()
	return row


def _apply_purchase_tax_template(doc, tax_template):
	if not tax_template:
		return
	doc.taxes_and_charges = tax_template
	from erpnext.controllers.accounts_controller import get_taxes_and_charges

	taxes = get_taxes_and_charges("Purchase Taxes and Charges Template", tax_template)
	doc.set("taxes", [])
	for tax_row in taxes:
		doc.append("taxes", tax_row)


def _create_purchase_order(ro_name, company, supplier, items, target_wh, po_tax_template, should_submit):
	po = frappe.new_doc("Purchase Order")
	po.company = company
	po.supplier = supplier
	po.transaction_date = today()
	po.schedule_date = today()

	if hasattr(po, "repair_order") or frappe.db.has_column("Purchase Order", "repair_order"):
		po.repair_order = ro_name

	for it in items:
		po.append("items", _build_purchase_item_row(it, target_wh, include_schedule_date=True))

	_apply_purchase_tax_template(po, po_tax_template)

	if hasattr(po, "disable_rounded_total"):
		po.disable_rounded_total = 1
	po.set_missing_values()
	po.calculate_taxes_and_totals()
	po.insert()
	if should_submit:
		po.submit()
	return po.name


def _create_rfq(ro_name, company, rfq_suppliers, rfq_parts, target_wh, should_submit):
	rfq = frappe.new_doc("Request for Quotation")
	rfq.company = company
	rfq.transaction_date = today()

	if hasattr(rfq, "repair_order") or frappe.db.has_column("Request for Quotation", "repair_order"):
		rfq.repair_order = ro_name

	for sup in rfq_suppliers:
		rfq.append("suppliers", {"supplier": sup})

	for it in rfq_parts:
		item_doc = frappe.get_cached_doc("Item", it["item_code"])
		raw_qty = 1.0 if it.get("qty") is None else it.get("qty")
		qty = optional_flt(raw_qty, "Quantity", positive=True, default=1.0)
		rfq.append(
			"items",
			{
				"item_code": it["item_code"],
				"item_name": optional_str(it.get("item_name"), "Item Name") or item_doc.item_name,
				"qty": qty,
				"uom": optional_str(it.get("uom"), "UOM") or getattr(item_doc, "stock_uom", "Nos"),
				"stock_uom": getattr(item_doc, "stock_uom", "Nos"),
				"conversion_factor": 1.0,
				"warehouse": target_wh,
				"schedule_date": today(),
			},
		)

	rfq.set_missing_values()
	rfq.insert()
	if should_submit:
		rfq.submit()
	return rfq.name


@frappe.whitelist()
def submit_procurement_orders(repair_order=None, parts_data=None, submit=True, company=None):
	"""Create (and optionally submit) Purchase Orders (grouped by supplier) and RFQs, optionally linked to a Repair Order."""
	should_submit = coerce_bool(submit, default=True)

	repair_order = optional_str(repair_order, "Repair Order")
	ro = None
	if repair_order:
		frappe.has_permission("Repair Order", "write", throw=True)
		if not frappe.db.exists("Repair Order", repair_order):
			frappe.throw(_("Repair Order {0} not found.").format(repair_order), frappe.DoesNotExistError)
		ro = frappe.get_doc("Repair Order", repair_order)
		ro.check_permission("write")

	if isinstance(parts_data, str):
		parts_data = parse_json_arg(parts_data, label="parts_data")
	if not isinstance(parts_data, list) or not parts_data:
		frappe.throw(_("No parts selected for procurement."), frappe.ValidationError)

	explicit_company = optional_str(company, "Company")
	if explicit_company and ro and explicit_company != ro.company:
		frappe.throw(
			_("Company {0} does not match Repair Order {1} company {2}.").format(
				explicit_company, ro.name, ro.company
			),
			frappe.ValidationError,
		)
	resolved_company = get_active_company(ro.company if ro else explicit_company)
	target_wh = _resolve_default_warehouse(resolved_company)

	frappe.has_permission("Item", "read", throw=True)
	po_parts_by_supplier: dict[str, list[dict[str, Any]]] = {}
	rfq_parts: list[dict[str, Any]] = []
	rfq_suppliers: set[str] = set()
	supplier_links: dict[tuple[str, str], Optional[str]] = {}
	candidate_codes: list[str] = []

	for item in parts_data:
		if not isinstance(item, dict):
			frappe.throw(_("Invalid parts data format."), frappe.ValidationError)
		code = require_str(item.get("item_code"), "Item Code")
		mode = optional_str(item.get("order_mode"), "Order Mode") or "Purchase Order"
		if mode not in _VALID_ORDER_MODES:
			frappe.throw(
				_("Invalid order_mode '{0}'. Allowed values: {1}.").format(mode, ", ".join(_VALID_ORDER_MODES)),
				frappe.ValidationError,
			)
		raw_qty = 1.0 if item.get("qty") is None else item.get("qty")
		qty = optional_flt(raw_qty, f"Quantity for {code}", positive=True, default=1.0)
		rate_val = item.get("rate")
		if rate_val is not None and str(rate_val).strip() != "":
			optional_flt(rate_val, f"Rate for {code}", non_negative=True)

		sup = optional_str(item.get("supplier"), "Supplier") or ""
		sup_part_no = optional_str(item.get("supplier_part_no"), "Supplier Part No")
		if mode == "Purchase Order" and not sup:
			frappe.throw(_("Supplier is required for Purchase Order on item {0}.").format(code), frappe.MandatoryError)

		normalized_item = dict(item)
		normalized_item["item_code"] = code
		normalized_item["qty"] = qty
		candidate_codes.append(code)

		if mode == "Request for Quotation":
			rfq_parts.append(normalized_item)
			if sup:
				rfq_suppliers.add(sup)
		else:
			po_parts_by_supplier.setdefault(sup, []).append(normalized_item)

		if sup:
			supplier_links.setdefault((sup, code), sup_part_no)

	# Verify every referenced Item exists and is readable before creating Suppliers/POs/RFQs.
	unique_codes = list(dict.fromkeys(candidate_codes))
	existing_items = set(frappe.get_all("Item", filters={"name": ["in", unique_codes]}, pluck="name"))
	missing_items = [c for c in unique_codes if c not in existing_items]
	if missing_items:
		frappe.throw(
			_("Catalog Item(s) not found: {0}.").format(", ".join(missing_items)),
			frappe.DoesNotExistError,
		)
	for code in unique_codes:
		frappe.get_cached_doc("Item", code).check_permission("read")

	# Enforce all target document and Supplier/Item permissions BEFORE mutating any records.
	if po_parts_by_supplier:
		frappe.has_permission("Purchase Order", "create", throw=True)
		if should_submit:
			frappe.has_permission("Purchase Order", "submit", throw=True)
	if rfq_parts:
		frappe.has_permission("Request for Quotation", "create", throw=True)
		if should_submit:
			frappe.has_permission("Request for Quotation", "submit", throw=True)

	unique_suppliers = list(dict.fromkeys(sup for sup, _code in supplier_links))
	if unique_suppliers:
		existing_sups = set(frappe.get_all("Supplier", filters={"name": ["in", unique_suppliers]}, pluck="name"))
		for sup in unique_suppliers:
			if sup in existing_sups:
				frappe.has_permission("Supplier", "read", doc=sup, throw=True)
			else:
				frappe.has_permission("Supplier", "create", throw=True)

	item_docs_to_save: list[Any] = []
	items_by_code = {code: frappe.get_doc("Item", code) for code in unique_codes}
	for sup in unique_suppliers:
		_ensure_supplier_exists(sup)
	for (sup, code), sup_part_no in supplier_links.items():
		item_doc = items_by_code[code]
		if _apply_supplier_to_item_doc(item_doc, sup, sup_part_no, company=resolved_company):
			item_doc.check_permission("write")
			if item_doc not in item_docs_to_save:
				item_docs_to_save.append(item_doc)
	for item_doc in item_docs_to_save:
		item_doc.save()

	from ism.setup import get_or_create_default_purchase_tax_template

	po_tax_template = get_or_create_default_purchase_tax_template(resolved_company)
	ro_name = ro.name if ro else None

	created_pos = [
		_create_purchase_order(ro_name, resolved_company, sup, items, target_wh, po_tax_template, should_submit)
		for sup, items in po_parts_by_supplier.items()
	]
	created_rfqs = []
	if rfq_parts:
		created_rfqs.append(_create_rfq(ro_name, resolved_company, rfq_suppliers, rfq_parts, target_wh, should_submit))

	return {
		"status": "success",
		"repair_order": ro_name,
		"company": resolved_company,
		"submitted": should_submit,
		"purchase_orders": created_pos,
		"rfqs": created_rfqs,
	}


def _resolve_mode_of_payment(payment_method):
	requested = optional_str(payment_method, "Payment Method")
	if not requested:
		frappe.throw(_("Payment Method is required."), frappe.MandatoryError)

	if frappe.db.exists("Mode of Payment", {"name": requested, "enabled": 1}):
		return requested

	all_modes = frappe.get_all("Mode of Payment", filters={"enabled": 1}, pluck="name")
	for candidate in all_modes:
		if candidate.strip().lower() == requested.lower():
			return candidate

	frappe.throw(
		_("Mode of Payment '{0}' was not found or is disabled. Available modes: {1}.").format(
			requested, ", ".join(sorted(all_modes)) or "None"
		),
		frappe.ValidationError,
	)


def _resolve_supplier_payment_account(company, mop_name):
	account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mop_name, "company": company},
		"default_account",
	)
	if account:
		return account

	mop_type = frappe.db.get_value("Mode of Payment", mop_name, "type")
	if mop_type == "Bank":
		account = (
			frappe.db.get_value("Company", company, "default_bank_account")
			or frappe.db.get_value(
				"Account", {"company": company, "account_type": "Bank", "is_group": 0}, "name"
			)
			or frappe.db.get_value("Company", company, "default_cash_account")
			or frappe.db.get_value(
				"Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name"
			)
		)
	else:
		account = (
			frappe.db.get_value("Company", company, "default_cash_account")
			or frappe.db.get_value(
				"Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name"
			)
			or frappe.db.get_value("Company", company, "default_bank_account")
			or frappe.db.get_value(
				"Account", {"company": company, "account_type": "Bank", "is_group": 0}, "name"
			)
		)

	if not account:
		frappe.throw(
			_("No Bank or Cash account is configured for Company {0}.").format(company),
			frappe.ValidationError,
		)
	return account


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
	purchase_order = optional_str(purchase_order, "Purchase Order")
	purchase_invoice = optional_str(purchase_invoice, "Purchase Invoice")
	if not purchase_order and not purchase_invoice:
		frappe.throw(
			_("Either Purchase Order or Purchase Invoice is required for a supplier payment."),
			frappe.MandatoryError,
		)
	if purchase_order and purchase_invoice:
		frappe.throw(
			_("Specify either Purchase Order or Purchase Invoice, not both."),
			frappe.ValidationError,
		)

	target_dt = "Purchase Order" if purchase_order else "Purchase Invoice"
	target_name = purchase_order or purchase_invoice
	frappe.has_permission("Payment Entry", "create", throw=True)
	frappe.has_permission("Payment Entry", "submit", throw=True)
	frappe.has_permission(target_dt, "read", throw=True)

	mop = _resolve_mode_of_payment(payment_method)
	ref_no = optional_str(reference_no, "Reference No")
	ref_date_str = optional_str(reference_date, "Reference Date")
	explicit_paid = optional_flt(paid_amount, "Paid Amount", positive=True, default=None)

	if not frappe.db.exists(target_dt, target_name):
		frappe.throw(_("{0} {1} not found.").format(target_dt, target_name), frappe.DoesNotExistError)

	target_doc = frappe.get_doc(target_dt, target_name)
	target_doc.check_permission("read")
	if target_doc.docstatus != 1:
		frappe.throw(
			_("{0} {1} must be submitted before recording a payment.").format(target_dt, target_name),
			frappe.ValidationError,
		)

	if target_dt == "Purchase Order":
		po_total = flt(getattr(target_doc, "rounded_total", 0.0) or target_doc.grand_total)
		remaining = flt(po_total - flt(getattr(target_doc, "advance_paid", 0.0)), 2)
		if flt(getattr(target_doc, "per_billed", 0.0), 2) >= 100.0:
			frappe.throw(
				_("Purchase Order {0} is already fully billed; pay against its Purchase Invoice instead.").format(
					target_name
				),
				frappe.ValidationError,
			)
	else:
		remaining = flt(target_doc.outstanding_amount, 2)

	if remaining <= 0:
		frappe.throw(
			_("{0} {1} has no remaining payable balance.").format(target_dt, target_name),
			frappe.ValidationError,
		)

	if explicit_paid is not None:
		if explicit_paid > remaining + 0.01:
			frappe.throw(
				_("Paid amount ({0}) cannot exceed remaining payable balance ({1}) on {2} {3}.").format(
					explicit_paid, remaining, target_dt, target_name
				),
				frappe.ValidationError,
			)
		pay_amt = explicit_paid
	else:
		pay_amt = remaining

	paid_from = _resolve_supplier_payment_account(target_doc.company, mop)
	paid_from_type = frappe.db.get_value("Account", paid_from, "account_type")
	resolved_ref_date = getdate(ref_date_str) if ref_date_str else getdate(today())

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	pe = get_payment_entry(
		target_dt,
		target_name,
		party_amount=pay_amt,
		bank_account=paid_from,
		reference_date=resolved_ref_date,
	)
	pe.mode_of_payment = mop
	pe.paid_from = paid_from
	pe.paid_from_account_type = paid_from_type
	pe.paid_amount = pay_amt
	pe.received_amount = pay_amt
	pe.reference_date = resolved_ref_date

	if ref_no:
		pe.reference_no = ref_no
	elif paid_from_type == "Bank" and not getattr(pe, "reference_no", None):
		pe.reference_no = target_name

	for ref in pe.get("references") or []:
		if ref.reference_doctype == target_dt and ref.reference_name == target_name:
			ref.allocated_amount = min(flt(pay_amt), flt(ref.outstanding_amount or remaining))

	pe.set_amounts()
	pe.check_permission("create")
	pe.insert()
	pe.check_permission("submit")
	pe.submit()
	target_doc.reload()

	return {
		"status": "success",
		"payment_entry": pe.name,
		"docstatus": pe.docstatus,
		"payment_type": pe.payment_type,
		"party_type": pe.party_type,
		"supplier": pe.party,
		"company": pe.company,
		"mode_of_payment": pe.mode_of_payment,
		"paid_from": pe.paid_from,
		"paid_amount": flt(pe.paid_amount),
		"unallocated_amount": flt(getattr(pe, "unallocated_amount", 0.0)),
		"reference_doctype": target_dt,
		"reference_name": target_name,
		"reference_no": getattr(pe, "reference_no", None),
		"reference_date": str(getattr(pe, "reference_date", "")),
		"repair_order": getattr(target_doc, "repair_order", None),
	}


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
	purchase_order = optional_str(purchase_order, "Purchase Order")
	supplier = optional_str(supplier, "Supplier")
	explicit_company = optional_str(company, "Company")
	bill_no = optional_str(bill_no, "Supplier Invoice No")
	bill_date_str = optional_str(bill_date, "Supplier Invoice Date")
	should_update_stock = coerce_bool(update_stock, default=False)
	should_attach_advances = coerce_bool(attach_advances, default=True)

	if isinstance(parts_data, str):
		parts_data = parse_json_arg(parts_data, label="parts_data")

	if not purchase_order and (not supplier or not parts_data):
		frappe.throw(
			_("Provide either Purchase Order or both Supplier and parts_data to create a Purchase Invoice."),
			frappe.MandatoryError,
		)

	if explicit_company and not frappe.db.exists("Company", explicit_company):
		frappe.throw(_("Company {0} does not exist.").format(explicit_company), frappe.DoesNotExistError)

	frappe.has_permission("Purchase Invoice", "create", throw=True)
	frappe.has_permission("Purchase Invoice", "submit", throw=True)

	po = None
	if purchase_order:
		frappe.has_permission("Purchase Order", "read", throw=True)
		if not frappe.db.exists("Purchase Order", purchase_order):
			frappe.throw(_("Purchase Order {0} not found.").format(purchase_order), frappe.DoesNotExistError)
		po = frappe.get_doc("Purchase Order", purchase_order)
		po.check_permission("read")
		if explicit_company and explicit_company != po.company:
			frappe.throw(
				_("Company {0} does not match Purchase Order {1} company {2}.").format(
					explicit_company, po.name, po.company
				),
				frappe.ValidationError,
			)
		if po.docstatus != 1:
			frappe.throw(
				_("Purchase Order {0} must be submitted before creating a Purchase Invoice.").format(
					purchase_order
				),
				frappe.ValidationError,
			)
		if flt(getattr(po, "per_billed", 0.0), 2) >= 100.0:
			frappe.throw(
				_("Purchase Order {0} is already 100% billed.").format(purchase_order),
				frappe.ValidationError,
			)
		if should_update_stock and flt(getattr(po, "per_received", 0.0), 2) >= 100.0:
			frappe.throw(
				_("Purchase Order {0} is already 100% received; create Purchase Invoice with update_stock disabled.").format(
					purchase_order
				),
				frappe.ValidationError,
			)

		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice

		pi = make_purchase_invoice(po.name)
		if parts_data is not None:
			if not isinstance(parts_data, list) or not parts_data:
				frappe.throw(_("Invalid parts data format."), frappe.ValidationError)
			requested: dict[str, dict[str, Any]] = {}
			for row in parts_data:
				if not isinstance(row, dict):
					frappe.throw(_("Invalid parts data format."), frappe.ValidationError)
				code = require_str(row.get("item_code"), "Item Code")
				if code in requested:
					frappe.throw(
						_("Duplicate item_code '{0}' in parts_data override.").format(code),
						frappe.ValidationError,
					)
				if row.get("qty") is not None:
					optional_flt(row["qty"], f"Quantity for {code}", positive=True)
				if row.get("rate") is not None and str(row.get("rate")).strip() != "":
					optional_flt(row["rate"], f"Rate for {code}", non_negative=True)
				requested[code] = row

			filtered_rows = []
			for pi_row in pi.get("items") or []:
				if pi_row.item_code in requested:
					override = requested[pi_row.item_code]
					if override.get("qty") is not None:
						pi_row.qty = optional_flt(override["qty"], f"Quantity for {pi_row.item_code}", positive=True)
					if override.get("rate") is not None and str(override.get("rate")).strip() != "":
						pi_row.rate = optional_flt(override["rate"], f"Rate for {pi_row.item_code}", non_negative=True)
					filtered_rows.append(pi_row)
			missing_codes = [c for c in requested if c not in {r.item_code for r in filtered_rows}]
			if missing_codes:
				frappe.throw(
					_("Item(s) {0} do not exist on Purchase Order {1}.").format(
						", ".join(missing_codes), po.name
					),
					frappe.ValidationError,
				)
			pi.set("items", filtered_rows)

		resolved_company = po.company
		if not pi.get("taxes"):
			from ism.setup import get_or_create_default_purchase_tax_template

			_apply_purchase_tax_template(pi, get_or_create_default_purchase_tax_template(resolved_company))
	else:
		if not isinstance(parts_data, list) or not parts_data:
			frappe.throw(_("No parts selected for Purchase Invoice."), frappe.ValidationError)

		resolved_company = get_active_company(explicit_company)
		target_wh = _resolve_default_warehouse(resolved_company)
		frappe.has_permission("Item", "read", throw=True)
		if frappe.db.exists("Supplier", supplier):
			frappe.has_permission("Supplier", "read", doc=supplier, throw=True)
		else:
			frappe.has_permission("Supplier", "create", throw=True)

		validated_items: list[dict[str, Any]] = []
		candidate_codes: list[str] = []
		for item in parts_data:
			if not isinstance(item, dict):
				frappe.throw(_("Invalid parts data format."), frappe.ValidationError)
			code = require_str(item.get("item_code"), "Item Code")
			raw_qty = 1.0 if item.get("qty") is None else item.get("qty")
			qty = optional_flt(raw_qty, f"Quantity for {code}", positive=True, default=1.0)
			if item.get("rate") is not None and str(item.get("rate")).strip() != "":
				optional_flt(item.get("rate"), f"Rate for {code}", non_negative=True)
			normalized = dict(item)
			normalized["item_code"] = code
			normalized["qty"] = qty
			validated_items.append(normalized)
			candidate_codes.append(code)

		unique_codes = list(dict.fromkeys(candidate_codes))
		existing_items = set(frappe.get_all("Item", filters={"name": ["in", unique_codes]}, pluck="name"))
		missing_items = [c for c in unique_codes if c not in existing_items]
		if missing_items:
			frappe.throw(_("Item {0} not found.").format(missing_items[0]), frappe.DoesNotExistError)

		items_by_code = {code: frappe.get_doc("Item", code) for code in unique_codes}
		for _code, item_doc in items_by_code.items():
			item_doc.check_permission("read")

		_ensure_supplier_exists(supplier)
		sup_parts_by_code: dict[str, Optional[str]] = {}
		for it in validated_items:
			sup_parts_by_code.setdefault(
				it["item_code"], optional_str(it.get("supplier_part_no"), "Supplier Part No")
			)
		for code, sup_part_no in sup_parts_by_code.items():
			item_doc = items_by_code[code]
			if _apply_supplier_to_item_doc(item_doc, supplier, sup_part_no, company=resolved_company):
				item_doc.check_permission("write")
				item_doc.save()

		pi = frappe.new_doc("Purchase Invoice")
		pi.company = resolved_company
		pi.supplier = supplier
		pi.posting_date = today()
		for item in validated_items:
			pi.append("items", _build_purchase_item_row(item, target_wh, include_schedule_date=False))

		from ism.setup import get_or_create_default_purchase_tax_template

		_apply_purchase_tax_template(pi, get_or_create_default_purchase_tax_template(resolved_company))

	pi.update_stock = 1 if should_update_stock else 0
	if hasattr(pi, "disable_rounded_total"):
		pi.disable_rounded_total = 1
	if bill_no:
		pi.bill_no = bill_no
		pi.bill_date = getdate(bill_date_str) if bill_date_str else getdate(today())
	elif bill_date_str:
		pi.bill_date = getdate(bill_date_str)

	pi.set_missing_values()
	pi.calculate_taxes_and_totals()

	if should_attach_advances:
		if po:
			pi.only_include_allocated_payments = 1
		pi.set_advances()
		pi.calculate_taxes_and_totals()

	pi.check_permission("create")
	pi.insert()
	pi.check_permission("submit")
	pi.submit()
	pi.reload()
	if po:
		po.reload()

	return {
		"status": "success",
		"purchase_invoice": pi.name,
		"docstatus": pi.docstatus,
		"update_stock": cint(pi.update_stock),
		"supplier": pi.supplier,
		"company": pi.company,
		"purchase_order": po.name if po else None,
		"repair_order": getattr(po, "repair_order", None) if po else None,
		"bill_no": getattr(pi, "bill_no", None),
		"bill_date": str(getattr(pi, "bill_date", "") or ""),
		"grand_total": flt(pi.grand_total),
		"total_advance": flt(getattr(pi, "total_advance", 0.0)),
		"outstanding_amount": flt(pi.outstanding_amount),
		"per_received": flt(getattr(po, "per_received", getattr(pi, "per_received", 0.0))),
		"per_billed": flt(getattr(po, "per_billed", 100.0)) if po else 100.0,
		"advances": [
			{
				"reference_type": adv.reference_type,
				"reference_name": adv.reference_name,
				"advance_amount": flt(adv.advance_amount),
				"allocated_amount": flt(adv.allocated_amount),
			}
			for adv in (pi.get("advances") or [])
		],
		"items": [
			{
				"item_code": row.item_code,
				"qty": flt(row.qty),
				"rate": flt(row.rate),
				"warehouse": row.warehouse,
			}
			for row in (pi.get("items") or [])
		],
	}
