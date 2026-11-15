# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import math
from typing import Any, Dict, List

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, today

from ism.ism.doctype.repair_order.fulfillment import complete_linked_schedule_entries
from ism.ism.doctype.repair_order.pricing import get_or_create_default_tax_template


def _parse_strict_odometer(raw_value: Any, field_label: str = "Odometer Out") -> int:
	"""Strictly parse an odometer value as a non-negative integer, rejecting invalid strings or bools."""
	if isinstance(raw_value, bool):
		frappe.throw(_("{0} must be a valid non-negative integer.").format(field_label), frappe.ValidationError)
	if isinstance(raw_value, int):
		val = raw_value
	elif isinstance(raw_value, float):
		if not math.isfinite(raw_value) or not raw_value.is_integer():
			frappe.throw(_("{0} must be a valid non-negative integer.").format(field_label), frappe.ValidationError)
		val = int(raw_value)
	elif isinstance(raw_value, str):
		stripped = raw_value.strip()
		if not stripped:
			frappe.throw(_("{0} is mandatory to check out and release Repair Order.").format(field_label), frappe.MandatoryError)
		if stripped.startswith("-") and stripped[1:].isdigit():
			frappe.throw(_("{0} cannot be negative.").format(field_label), frappe.ValidationError)
		if not stripped.isdigit():
			frappe.throw(_("{0} must be a valid non-negative integer.").format(field_label), frappe.ValidationError)
		val = int(stripped)
	else:
		frappe.throw(_("{0} must be a valid non-negative integer.").format(field_label), frappe.ValidationError)

	if val < 0:
		frappe.throw(_("{0} cannot be negative.").format(field_label), frappe.ValidationError)
	return val


def _resolve_billing_context(ro, approved_services: List[Any]) -> Dict[str, Any]:
	"""Batch Company, UOM, Item, Warehouse, and Account lookups for Sales Invoice creation."""
	comp_meta = frappe.db.get_value(
		"Company",
		ro.company,
		["abbr", "default_income_account", "cost_center", "default_bank_account", "default_cash_account"],
		as_dict=True,
	) or {}
	abbr = comp_meta.get("abbr") or "I"
	default_income_account = comp_meta.get("default_income_account") or f"Sales - {abbr}"
	default_cost_center = comp_meta.get("cost_center") or f"Main - {abbr}"

	default_wh = (
		frappe.db.get_value("Warehouse", {"company": ro.company, "warehouse_name": "Stores"}, "name")
		or frappe.db.get_value("Warehouse", {"company": ro.company, "is_group": 0}, "name")
	)
	if not default_wh:
		stock_default_wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
		if stock_default_wh and frappe.db.get_value("Warehouse", stock_default_wh, "company") == ro.company:
			default_wh = stock_default_wh
	if not default_wh:
		default_wh = f"Stores - {abbr}"

	existing_uoms = set(frappe.get_all("UOM", filters={"name": ["in", ["Hour", "Unit"]]}, pluck="name"))
	part_codes: set[str] = set()
	part_whs: set[str] = set()
	parsed_parts_by_service: Dict[int, List[Dict[str, Any]]] = {}
	for idx, s in enumerate(approved_services):
		parts = s.parts
		if isinstance(parts, str):
			try:
				parts = json.loads(parts) if parts else []
			except Exception:
				parts = []
		if not isinstance(parts, list):
			parts = []
		valid_parts = [p for p in parts if isinstance(p, dict)]
		parsed_parts_by_service[idx] = valid_parts
		for p in valid_parts:
			if p.get("item_code"):
				part_codes.add(p["item_code"])
			if p.get("warehouse"):
				part_whs.add(p["warehouse"])

	valid_items: set[str] = (
		set(frappe.get_all("Item", filters={"name": ["in", list(part_codes)]}, pluck="name"))
		if part_codes
		else set()
	)
	valid_warehouses: set[str] = (
		set(
			frappe.get_all(
				"Warehouse",
				filters={"name": ["in", list(part_whs)], "company": ro.company},
				pluck="name",
			)
		)
		if part_whs
		else set()
	)

	accounts = frappe.get_all(
		"Account",
		filters={"company": ro.company, "account_name": ["in", ["Duties and Taxes", "HST 13%", "HST"]]},
		fields=["name", "account_name"],
	)
	accounts_by_name = {a.account_name: a.name for a in accounts}

	return {
		"abbr": abbr,
		"default_income_account": default_income_account,
		"default_cost_center": default_cost_center,
		"default_bank_account": comp_meta.get("default_bank_account"),
		"default_cash_account": comp_meta.get("default_cash_account"),
		"default_warehouse": default_wh,
		"existing_uoms": existing_uoms,
		"valid_items": valid_items,
		"valid_warehouses": valid_warehouses,
		"parsed_parts_by_service": parsed_parts_by_service,
		"duties_and_taxes": accounts_by_name.get("Duties and Taxes") or f"Duties and Taxes - {abbr}",
		"hst_account": accounts_by_name.get("HST 13%") or accounts_by_name.get("HST"),
	}


def _ensure_standard_labor_item(existing_uoms: set[str]) -> str:
	"""Ensure standard non-stock labor Item exists before building invoice rows."""
	default_labor = "LABOR-STD"
	if "Hour" not in existing_uoms and not frappe.db.exists("UOM", "Hour"):
		frappe.get_doc({"doctype": "UOM", "uom_name": "Hour", "must_be_whole_number": 0}).insert(
			ignore_permissions=True
		)
		existing_uoms.add("Hour")

	if not frappe.db.exists("Item", default_labor):
		labor_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "Services"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": default_labor,
			"item_name": "Standard Labor",
			"item_group": labor_group,
			"is_stock_item": 0,
			"stock_uom": "Hour",
		}).insert(ignore_permissions=True)
	return default_labor


def _resolve_mode_of_payment(payment_method: Any, explicit_method_supplied: bool) -> str:
	"""Resolve an explicit or shop-configured Mode of Payment without arbitrary empty-filter lookups."""
	if not isinstance(payment_method, str) or not payment_method.strip():
		frappe.throw(_("A valid Mode of Payment is required."), frappe.ValidationError)

	requested = payment_method.strip()
	if frappe.db.exists("Mode of Payment", requested):
		return requested
	if not explicit_method_supplied and frappe.db.exists("Mode of Payment", "Cash"):
		return "Cash"

	frappe.throw(
		_("Mode of Payment '{0}' does not exist.").format(requested),
		frappe.ValidationError,
	)


def _acquire_locked_ro_state(ro) -> None:
	"""Lock the Repair Order row with FOR UPDATE and reject already-invoiced or released orders."""
	if not ro.name or ro.is_new():
		return

	locked_rows = frappe.db.sql(
		"""
		SELECT name, status, sales_invoice, vehicle_released
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro.name,),
		as_dict=True,
	)
	if not locked_rows:
		return

	locked = locked_rows[0]
	if (
		locked.get("sales_invoice")
		or cint(locked.get("vehicle_released")) == 1
		or locked.get("status") == "Closed / Released"
		or ro.sales_invoice
		or cint(ro.vehicle_released) == 1
	):
		frappe.throw(
			_("Repair Order {0} has already been invoiced or released.").format(ro.name),
			frappe.ValidationError,
		)


def process_ro_payment_and_release(
	ro,
	payment_method="Debit / Interac",
	paid_amount=None,
	odometer_out=None,
	**kwargs,
) -> Dict[str, Any]:
	"""
	Finalize billing audit:
	1. Acquires FOR UPDATE lock and rejects already-invoiced/released ROs
	2. Strictly validates odometer out
	3. Creates submitted ERPNext Sales Invoice with update_stock=1 using batched lookups
	4. Reconciles payment against invoice grand total and creates Payment Entry
	5. Marks vehicle released, updates vehicle last service specs, and saves RO atomically
	"""
	ro.check_permission("write")
	frappe.has_permission("Sales Invoice", "create", throw=True)
	frappe.has_permission("Sales Invoice", "submit", throw=True)
	if ro.vehicle:
		frappe.has_permission("Repair Vehicle", "write", doc=ro.vehicle, throw=True)

	explicit_method_supplied = "payment_method" in kwargs
	if isinstance(payment_method, dict):
		paid_amount = payment_method.get("paid_amount", paid_amount)
		odometer_out = payment_method.get("odometer_out", odometer_out)
		if payment_method.get("payment_method"):
			explicit_method_supplied = True
		payment_method = payment_method.get("payment_method") or "Debit / Interac"
	else:
		if payment_method != "Debit / Interac":
			explicit_method_supplied = True
		if kwargs.get("payment_method"):
			payment_method = kwargs["payment_method"]
			explicit_method_supplied = True

	if paid_amount is None and "paid_amount" in kwargs:
		paid_amount = kwargs.get("paid_amount")
	if odometer_out is None and "odometer_out" in kwargs:
		odometer_out = kwargs.get("odometer_out")

	pay_amt: float | None = None
	if paid_amount is not None:
		if isinstance(paid_amount, bool):
			frappe.throw(_("Paid amount must be a valid number."), frappe.ValidationError)
		try:
			pay_amt_raw = float(paid_amount)
		except (ValueError, TypeError):
			frappe.throw(_("Paid amount must be a valid number."), frappe.ValidationError)
		if not math.isfinite(pay_amt_raw):
			frappe.throw(_("Paid amount must be a valid number."), frappe.ValidationError)
		pay_amt = flt(pay_amt_raw, 2)
		if pay_amt < 0:
			frappe.throw(_("Paid amount cannot be negative."), frappe.ValidationError)

	_acquire_locked_ro_state(ro)

	if odometer_out is not None:
		ro.odometer_out = _parse_strict_odometer(odometer_out, "Odometer Out")
	else:
		if ro.odometer_out is None or ro.odometer_out == "" or cint(ro.odometer_out) == 0:
			frappe.throw(_("Odometer Out is mandatory to check out and release Repair Order."), frappe.MandatoryError)
		ro.odometer_out = _parse_strict_odometer(ro.odometer_out, "Odometer Out")

	if cint(ro.odometer_out) < cint(ro.odometer_in or 0):
		frappe.throw(
			_("Odometer Out ({0}) cannot be less than Odometer In ({1}).").format(
				ro.odometer_out, ro.odometer_in
			)
		)
	ro._validate_odometer()

	approved_services = [s for s in ro.services if s.status == "Approved"]
	if not approved_services:
		frappe.throw(_("Cannot process payment without approved services."))

	has_pending = any(s.status in ["Draft", "Estimating", "Pending Approval"] for s in ro.services)
	if has_pending:
		frappe.throw(
			_(
				"Cannot close or release Repair Order while services are still in Estimating, Draft, or Pending Approval. "
				"Please obtain customer authorization or decline pending supplements before final billing and release."
			)
		)

	ctx = _resolve_billing_context(ro, approved_services)
	explicit_zero_tax = (
		ro.tax_rate is not None and str(ro.tax_rate).strip() != "" and flt(ro.tax_rate) == 0.0
	)
	if explicit_zero_tax:
		tax_template = None
		ro.taxes_and_charges = None
	else:
		tax_template = ro.taxes_and_charges or get_or_create_default_tax_template(ro.company)
		ro.taxes_and_charges = tax_template

	si = frappe.new_doc("Sales Invoice")
	if explicit_zero_tax:
		si.flags.dont_auto_add_taxes = True
	si.customer = ro.customer
	si.company = ro.company
	si.posting_date = today()
	si.currency = ro.currency or "CAD"
	si.update_stock = 1
	si.disable_rounded_total = 1
	si.taxes_and_charges = tax_template
	if hasattr(si, "repair_order") or frappe.db.has_column("Sales Invoice", "repair_order"):
		si.repair_order = ro.name

	has_labor_lines = any(flt(s.labor_hours) > 0 or flt(s.labor_amount) > 0 for s in approved_services)
	labor_item_code = _ensure_standard_labor_item(ctx["existing_uoms"]) if has_labor_lines else None
	unit_uom = "Unit" if "Unit" in ctx["existing_uoms"] else "Nos"

	for idx, s in enumerate(approved_services):
		if flt(s.labor_hours) > 0 or flt(s.labor_amount) > 0:
			if cint(getattr(s, "is_flat_rate", 0)):
				si.append("items", {
					"item_code": labor_item_code,
					"item_name": f"Labor: {s.service_title}",
					"description": s.advisor_notes or s.service_title,
					"qty": 1.0,
					"uom": unit_uom,
					"rate": flt(s.labor_amount),
					"income_account": ctx["default_income_account"],
					"cost_center": ctx["default_cost_center"],
				})
			else:
				si.append("items", {
					"item_code": labor_item_code,
					"item_name": f"Labor: {s.service_title}",
					"description": s.advisor_notes or s.service_title,
					"qty": flt(s.labor_hours) or 1.0,
					"uom": "Hour",
					"rate": flt(s.labor_rate),
					"income_account": ctx["default_income_account"],
					"cost_center": ctx["default_cost_center"],
				})

		for p in ctx["parsed_parts_by_service"].get(idx, []):
			item_code = p.get("item_code")
			if not item_code or item_code not in ctx["valid_items"]:
				continue
			item_doc = frappe.get_cached_doc("Item", item_code)
			source = p.get("part_source") or getattr(item_doc, "part_source", "OEM") or "OEM"
			condition = p.get("part_condition") or getattr(item_doc, "part_condition", "New") or "New"
			warranty = p.get("warranty_period") or getattr(item_doc, "warranty_period", "") or ""
			compliance_tag = f"[{source} - {condition}"
			if warranty:
				compliance_tag += f" | Warranty: {warranty}"
			compliance_tag += "]"
			part_wh = p.get("warehouse")
			si.append("items", {
				"item_code": item_code,
				"item_name": p.get("item_name") or item_doc.item_name,
				"description": f"{p.get('item_name') or item_doc.item_name} {compliance_tag}",
				"qty": flt(p.get("qty", 1.0)),
				"uom": p.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
				"rate": flt(p.get("rate", 0.0)),
				"warehouse": part_wh if (part_wh and part_wh in ctx["valid_warehouses"]) else ctx["default_warehouse"],
				"income_account": ctx["default_income_account"],
				"cost_center": ctx["default_cost_center"],
			})

	si.set_missing_values()
	if tax_template:
		si.set_taxes()
	elif explicit_zero_tax:
		si.taxes_and_charges = None
		si.set("taxes", [])

	if not si.taxes and not explicit_zero_tax:
		hst_name = f"HST 13% - {ctx['abbr']}"
		account_head = (
			ctx["hst_account"]
			or (hst_name if frappe.db.exists("Account", hst_name) else ctx["duties_and_taxes"])
		)
		rate = flt(ro.tax_rate) if (ro.tax_rate is not None and str(ro.tax_rate).strip() != "") else 13.0
		si.append("taxes", {
			"charge_type": "On Net Total",
			"account_head": account_head,
			"description": f"HST @ {rate}%",
			"rate": rate,
		})

	si.calculate_taxes_and_totals()
	invoice_total = flt(si.grand_total, 2)
	if pay_amt is not None:
		if pay_amt > invoice_total + 0.01:
			frappe.throw(
				_("Paid amount ({0}) cannot exceed Sales Invoice grand total ({1}).").format(pay_amt, invoice_total),
				frappe.ValidationError,
			)
	else:
		pay_amt = invoice_total

	si.insert()
	if explicit_zero_tax and si.taxes_and_charges:
		si.taxes_and_charges = None
		si.db_set("taxes_and_charges", None, update_modified=False)
	si.submit()

	ro.sales_invoice = si.name

	if pay_amt > 0:
		frappe.has_permission("Payment Entry", "create", throw=True)
		frappe.has_permission("Payment Entry", "submit", throw=True)
		mop = _resolve_mode_of_payment(payment_method, explicit_method_supplied=explicit_method_supplied)
		paid_to = (
			frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": mop, "company": ro.company},
				"default_account",
			)
			or ctx["default_bank_account"]
			or ctx["default_cash_account"]
			or f"Cash - {ctx['abbr']}"
		)
		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Receive"
		pe.party_type = "Customer"
		pe.party = ro.customer
		pe.company = ro.company
		pe.paid_amount = pay_amt
		pe.received_amount = pay_amt
		pe.mode_of_payment = mop
		pe.paid_to = paid_to
		pe.append("references", {
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"total_amount": si.grand_total,
			"outstanding_amount": si.outstanding_amount,
			"allocated_amount": min(pay_amt, si.outstanding_amount),
		})
		pe.insert()
		pe.submit()

	ro.paid_amount = pay_amt
	ro.outstanding_amount = max(0.0, flt(invoice_total - pay_amt, 2))
	if ro.outstanding_amount <= 0:
		ro.payment_status = "Paid"
	elif pay_amt > 0:
		ro.payment_status = "Partially Paid"
	else:
		ro.payment_status = "Unpaid"

	ro.vehicle_location = "Released to Customer"
	ro.vehicle_released = 1
	ro.status = "Closed / Released"
	ro.checkout_date = now_datetime()

	if ro.vehicle:
		veh_doc = frappe.get_doc("Repair Vehicle", ro.vehicle)
		veh_doc.last_odometer = ro.odometer_out or ro.odometer_in
		veh_doc.last_service_date = today()
		veh_doc.save()

	complete_linked_schedule_entries(ro.name)
	ro.save()
	return {
		"status": "success",
		"sales_invoice": ro.sales_invoice,
		"payment_status": ro.payment_status,
		"vehicle_released": ro.vehicle_released,
		"checkout_date": ro.checkout_date,
	}
