# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import math
import re
from typing import Any, Dict, List

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, nowdate, today

from ism.ism.doctype.repair_order.fulfillment import complete_linked_schedule_entries
from ism.ism.doctype.repair_order.pricing import get_or_create_default_tax_template

_REOPEN_FROM_STATUSES = ("Billed / Paid", "Closed / Released")
_LOCKED_INVOICE_STATUSES = ("Cancelled", "Closed / Released", "Billed / Paid")


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


def _parse_strict_amount(raw_value: Any, field_label: str = "Amount", allow_zero: bool = True) -> float:
	"""Strictly parse a monetary amount, rejecting booleans, non-numeric values, and negatives."""
	if isinstance(raw_value, bool) or raw_value is None or raw_value == "":
		frappe.throw(_("{0} must be a valid number.").format(field_label), frappe.ValidationError)
	try:
		raw_float = float(raw_value)
	except (ValueError, TypeError):
		frappe.throw(_("{0} must be a valid number.").format(field_label), frappe.ValidationError)
	if not math.isfinite(raw_float):
		frappe.throw(_("{0} must be a valid number.").format(field_label), frappe.ValidationError)
	amt = flt(raw_float, 2)
	if amt < 0 or (not allow_zero and amt <= 0):
		msg = _("{0} cannot be negative.") if allow_zero else _("{0} must be greater than zero.")
		frappe.throw(msg.format(field_label), frappe.ValidationError)
	return amt


def _parse_service_parts(raw_parts: Any) -> List[Dict[str, Any]]:
	"""Parse JSON-encoded or list-based service parts into a list of dicts."""
	parts = raw_parts
	if isinstance(parts, str):
		try:
			parts = json.loads(parts) if parts else []
		except Exception:
			parts = []
	if not isinstance(parts, list):
		return []
	return [p for p in parts if isinstance(p, dict)]


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

	existing_uoms = set(frappe.get_all("UOM", filters={"name": ["in", ["Hour", "Unit", "Nos"]]}, pluck="name"))
	part_codes: set[str] = set()
	part_whs: set[str] = set()
	parsed_parts_by_service: Dict[int, List[Dict[str, Any]]] = {}
	for idx, s in enumerate(approved_services):
		valid_parts = _parse_service_parts(s.parts)
		parsed_parts_by_service[idx] = valid_parts
		for p in valid_parts:
			if p.get("item_code"):
				part_codes.add(p["item_code"])
			if p.get("warehouse"):
				part_whs.add(p["warehouse"])

	valid_items: set[str] = set()
	stock_items: set[str] = set()
	if part_codes:
		item_rows = frappe.get_all(
			"Item",
			filters={"name": ["in", list(part_codes)]},
			fields=["name", "is_stock_item"],
		)
		valid_items = {row.name for row in item_rows}
		stock_items = {row.name for row in item_rows if cint(row.is_stock_item) == 1}

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
		"stock_items": stock_items,
		"valid_warehouses": valid_warehouses,
		"parsed_parts_by_service": parsed_parts_by_service,
		"duties_and_taxes": accounts_by_name.get("Duties and Taxes") or f"Duties and Taxes - {abbr}",
		"hst_account": accounts_by_name.get("HST 13%") or accounts_by_name.get("HST"),
	}


def _ensure_non_stock_item(
	item_code: str,
	item_name: str,
	uom: str,
	existing_uoms: set[str],
) -> str:
	"""Ensure a standard non-stock service/fee Item and its UOM exist before building invoice rows."""
	if uom not in existing_uoms and not frappe.db.exists("UOM", uom):
		frappe.get_doc({"doctype": "UOM", "uom_name": uom, "must_be_whole_number": 0}).insert(
			ignore_permissions=True
		)
		existing_uoms.add(uom)

	if not frappe.db.exists("Item", item_code):
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "Services"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_name,
			"item_group": item_group,
			"is_stock_item": 0,
			"stock_uom": uom,
		}).insert(ignore_permissions=True)
	return item_code


def _ensure_standard_labor_item(existing_uoms: set[str]) -> str:
	"""Ensure standard non-stock labor Item exists before building invoice rows."""
	return _ensure_non_stock_item("LABOR-STD", "Standard Labor", "Hour", existing_uoms)


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


def _resolve_paid_to_account(company: str, mop: str, comp_meta: Dict[str, Any] | None = None) -> str:
	"""Resolve the deposit/payment receiving account for a Mode of Payment and Company."""
	if comp_meta is None:
		comp_meta = frappe.db.get_value(
			"Company",
			company,
			["abbr", "default_bank_account", "default_cash_account"],
			as_dict=True,
		) or {}
	abbr = comp_meta.get("abbr") or "I"
	return (
		frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": mop, "company": company},
			"default_account",
		)
		or comp_meta.get("default_bank_account")
		or comp_meta.get("default_cash_account")
		or f"Cash - {abbr}"
	)


def _acquire_locked_ro_state(ro) -> Dict[str, Any]:
	"""Lock the Repair Order row with FOR UPDATE and reject cancelled, invoiced, or released orders."""
	if not ro.name or ro.is_new():
		return {}

	locked_rows = frappe.db.sql(
		"""
		SELECT name, status, sales_invoice, vehicle_released, customer, company, grand_total, paid_amount
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro.name,),
		as_dict=True,
	)
	if not locked_rows:
		return {}

	locked = locked_rows[0]
	if (
		locked.get("sales_invoice")
		or cint(locked.get("vehicle_released")) == 1
		or locked.get("status") in _LOCKED_INVOICE_STATUSES
		or ro.sales_invoice
		or cint(ro.vehicle_released) == 1
		or ro.status in _LOCKED_INVOICE_STATUSES
	):
		frappe.throw(
			_("Repair Order {0} cannot be billed or accept deposits in status '{1}'.").format(
				ro.name, locked.get("status") or ro.status
			),
			frappe.ValidationError,
		)
	return locked


def _is_ro_deposit_remark(remarks: str | None, ro_name: str) -> bool:
	"""Match exact RO deposit remark tags '[RO:<name>]' or 'Repair Order <name>' without substring collisions."""
	if not remarks or not ro_name:
		return False
	if f"[RO:{ro_name}]" in remarks:
		return True
	return bool(re.search(rf"(?:^|\W)Repair Order {re.escape(ro_name)}(?:$|\W)", remarks))


def _extract_ro_names_from_remarks(remarks: str | None) -> List[str]:
	"""Extract candidate Repair Order names from Payment Entry remarks."""
	if not remarks or not isinstance(remarks, str):
		return []
	candidates: List[str] = []
	candidates.extend(re.findall(r"\[RO:([A-Za-z0-9._-]+)\]", remarks))
	candidates.extend(re.findall(r"(?:^|\W)Repair Order ([A-Za-z0-9._-]+)(?:$|\W)", remarks))
	return list(dict.fromkeys(c for c in candidates if c))


def _get_ro_deposit_payment_entries(
	ro_name: str,
	customer: str | None = None,
	only_unallocated: bool = False,
	excluded_pe_name: str | None = None,
) -> List[Dict[str, Any]]:
	"""Fetch submitted advance deposit Payment Entries tagged for the given Repair Order."""
	if not ro_name:
		return []
	filters: Dict[str, Any] = {
		"docstatus": 1,
		"payment_type": "Receive",
		"party_type": "Customer",
		"remarks": ["like", f"%{ro_name}%"],
	}
	if customer:
		filters["party"] = customer
	if only_unallocated:
		filters["unallocated_amount"] = [">", 0]

	rows = frappe.get_all(
		"Payment Entry",
		filters=filters,
		fields=["name", "paid_amount", "received_amount", "unallocated_amount", "remarks"],
		order_by="creation asc, name asc",
	)
	return [
		r
		for r in rows
		if r.get("name") != excluded_pe_name and _is_ro_deposit_remark(r.get("remarks"), ro_name)
	]


def _create_and_submit_payment_entry(
	customer: str,
	company: str,
	mop: str,
	paid_to: str,
	amount: float,
	reference_no: str | None = None,
	reference_date: str | None = None,
	remarks: str | None = None,
	sales_invoice: str | None = None,
	si_grand_total: float | None = None,
	si_outstanding_amount: float | None = None,
	skip_ro_payment_sync: bool = True,
):
	"""Create and submit a Customer Receive Payment Entry, optionally referencing a Sales Invoice."""
	pe = frappe.new_doc("Payment Entry")
	if skip_ro_payment_sync:
		pe.flags.skip_ro_payment_sync = True
	pe.payment_type = "Receive"
	pe.party_type = "Customer"
	pe.party = customer
	pe.company = company
	pe.posting_date = nowdate()
	pe.mode_of_payment = mop
	pe.paid_to = paid_to
	pe.paid_amount = flt(amount, 2)
	pe.received_amount = flt(amount, 2)
	if reference_no:
		pe.reference_no = reference_no
		pe.reference_date = reference_date or nowdate()
	elif reference_date:
		pe.reference_date = reference_date
	if remarks:
		pe.custom_remarks = 1
		pe.remarks = remarks
	if sales_invoice:
		outstanding = flt(si_outstanding_amount, 2)
		pe.append("references", {
			"reference_doctype": "Sales Invoice",
			"reference_name": sales_invoice,
			"total_amount": flt(si_grand_total, 2),
			"outstanding_amount": outstanding,
			"allocated_amount": min(flt(amount, 2), outstanding),
		})
	pe.insert()
	pe.submit()
	return pe


def _persist_ro_payment_fields(
	ro_name: str,
	updates: Dict[str, Any],
	ro_doc=None,
	update_modified: bool = True,
) -> None:
	"""Persist Repair Order billing fields safely across unmigrated Swarm 1 schema columns."""
	db_updates = dict(updates)
	if "deposit_amount" in db_updates and not frappe.db.has_column("Repair Order", "deposit_amount"):
		db_updates.pop("deposit_amount")

	if db_updates and ro_name and frappe.db.exists("Repair Order", ro_name):
		frappe.db.set_value("Repair Order", ro_name, db_updates, update_modified=update_modified)

	if ro_doc is not None:
		for key, value in updates.items():
			setattr(ro_doc, key, value)
		if update_modified and ro_name and frappe.db.exists("Repair Order", ro_name):
			ro_doc.modified = frappe.db.get_value("Repair Order", ro_name, "modified")


def _compute_payment_status(
	paid_amount: float,
	target_total: float,
	has_invoice: bool = False,
) -> str:
	"""Derive consistent Repair Order payment_status from paid and target totals."""
	paid = flt(paid_amount, 2)
	total = flt(target_total, 2)
	if has_invoice:
		outstanding = max(0.0, flt(total - paid, 2))
		if outstanding <= 0:
			return "Paid"
		if paid > 0:
			return "Partially Paid"
		return "Unpaid"
	if paid <= 0:
		return "Unpaid"
	if total > 0 and paid >= total:
		return "Paid"
	return "Partially Paid"


def _recompute_ro_payment_state(
	ro_name: str,
	excluded_pe_name: str | None = None,
	unlinked_si_name: str | None = None,
	reopen_if_cancelled_si: bool = False,
) -> None:
	"""Lock a Repair Order and recompute its deposit, paid, outstanding, and status from ledger docs."""
	locked_rows = frappe.db.sql(
		"""
		SELECT name, customer, status, sales_invoice, grand_total, vehicle_released, vehicle_location
		FROM `tabRepair Order`
		WHERE name = %s
		FOR UPDATE
		""",
		(ro_name,),
		as_dict=True,
	)
	if not locked_rows:
		return

	locked = locked_rows[0]
	si_name = locked.get("sales_invoice")
	updates: Dict[str, Any] = {}

	if unlinked_si_name and si_name == unlinked_si_name:
		si_name = None
		updates["sales_invoice"] = None
		if reopen_if_cancelled_si and locked.get("status") in _REOPEN_FROM_STATUSES:
			updates["status"] = "Work Completed"
			if cint(locked.get("vehicle_released")) == 1:
				updates["vehicle_released"] = 0
				updates["checkout_date"] = None
				if locked.get("vehicle_location") == "Released to Customer":
					updates["vehicle_location"] = "Completed / Parked in Lot"

	deposit_pes = _get_ro_deposit_payment_entries(
		ro_name,
		customer=locked.get("customer"),
		excluded_pe_name=excluded_pe_name,
	)
	total_deposits = flt(sum(flt(pe.get("paid_amount")) for pe in deposit_pes), 2)
	unallocated_deposits = flt(sum(flt(pe.get("unallocated_amount")) for pe in deposit_pes), 2)

	if si_name and frappe.db.exists("Sales Invoice", si_name):
		si_meta = frappe.db.get_value(
			"Sales Invoice",
			si_name,
			["grand_total", "docstatus"],
			as_dict=True,
		) or {}
		if cint(si_meta.get("docstatus")) == 1:
			ref_rows = frappe.db.sql(
				"""
				SELECT per.allocated_amount
				FROM `tabPayment Entry Reference` per
				INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
				WHERE per.reference_doctype = 'Sales Invoice'
				  AND per.reference_name = %s
				  AND pe.docstatus = 1
				  AND pe.name != %s
				""",
				(si_name, excluded_pe_name or ""),
				as_dict=True,
			)
			paid_against_si = flt(sum(flt(r.get("allocated_amount")) for r in ref_rows), 2)
			total_paid = flt(paid_against_si + unallocated_deposits, 2)
			target_total = flt(si_meta.get("grand_total"), 2)
			has_invoice = True
		else:
			total_paid = total_deposits
			target_total = flt(locked.get("grand_total"), 2)
			has_invoice = False
	else:
		total_paid = total_deposits
		target_total = flt(locked.get("grand_total"), 2)
		has_invoice = False

	outstanding = max(0.0, flt(target_total - total_paid, 2))
	payment_status = _compute_payment_status(total_paid, target_total, has_invoice=has_invoice)

	updates.update({
		"deposit_amount": total_deposits,
		"paid_amount": total_paid,
		"outstanding_amount": outstanding,
		"payment_status": payment_status,
	})
	_persist_ro_payment_fields(ro_name, updates)


def _validate_pre_checkout_stock(ro, approved_services: List[Any], ctx: Dict[str, Any]) -> None:
	"""Verify Bin actual_qty covers all Approved stock parts before Sales Invoice creation."""
	required_by_key: Dict[tuple[str, str], float] = {}
	for idx, _service in enumerate(approved_services):
		for p in ctx["parsed_parts_by_service"].get(idx, []):
			item_code = p.get("item_code")
			if not item_code or item_code not in ctx["valid_items"]:
				continue
			if item_code not in ctx["stock_items"]:
				continue
			item_doc = frappe.get_cached_doc("Item", item_code)
			source = p.get("part_source") or getattr(item_doc, "part_source", "OEM") or "OEM"
			if str(source).strip() == "Customer Supplied":
				continue
			part_wh = p.get("warehouse")
			wh = part_wh if (part_wh and part_wh in ctx["valid_warehouses"]) else ctx["default_warehouse"]
			qty = flt(p.get("qty", 1.0))
			if qty > 0:
				key = (item_code, wh)
				required_by_key[key] = flt(required_by_key.get(key, 0.0) + qty, 6)

	if not required_by_key:
		return

	req_items = list({k[0] for k in required_by_key})
	req_whs = list({k[1] for k in required_by_key})
	bin_rows = frappe.get_all(
		"Bin",
		filters={"item_code": ["in", req_items], "warehouse": ["in", req_whs]},
		fields=["item_code", "warehouse", "actual_qty"],
	)
	bin_map = {(r.item_code, r.warehouse): flt(r.actual_qty) for r in bin_rows}

	deficits: List[str] = []
	for (item_code, wh), req_qty in sorted(required_by_key.items()):
		avail_qty = flt(bin_map.get((item_code, wh), 0.0))
		if avail_qty < flt(req_qty) - 1e-9:
			shortfall = flt(req_qty - avail_qty, 3)
			deficits.append(
				_("{0} in {1}: required {2}, available {3}, shortfall {4}").format(
					item_code, wh, flt(req_qty, 3), flt(avail_qty, 3), shortfall
				)
			)

	if deficits:
		frappe.throw(
			_("Insufficient warehouse stock for Repair Order {0}: {1}").format(
				ro.name, "; ".join(deficits)
			),
			frappe.ValidationError,
		)


def _apply_ro_advances_to_invoice(si, ro_name: str, customer: str) -> float:
	"""Attach only unallocated advance Payment Entries belonging to this Repair Order onto the Sales Invoice."""
	advance_pes = _get_ro_deposit_payment_entries(ro_name, customer=customer, only_unallocated=True)
	if not advance_pes:
		return 0.0

	ro_pe_names = {p["name"] for p in advance_pes}
	si.set_advances()
	remaining_to_allocate = flt(si.grand_total, 2)
	kept_advances = []
	total_allocated = 0.0
	for adv in si.get("advances") or []:
		if adv.reference_type == "Payment Entry" and adv.reference_name in ro_pe_names:
			alloc = min(flt(adv.advance_amount, 2), remaining_to_allocate)
			if alloc > 0:
				adv.allocated_amount = alloc
				kept_advances.append(adv)
				total_allocated = flt(total_allocated + alloc, 2)
				remaining_to_allocate = flt(remaining_to_allocate - alloc, 2)
	si.set("advances", kept_advances)
	si.calculate_taxes_and_totals()
	return total_allocated


def _validate_split_tenders(raw_payments: Any, remaining_due: float, explicit_method_supplied: bool) -> List[Dict[str, Any]]:
	"""Validate and normalize split-tender payment entries against the remaining invoice balance."""
	payments = raw_payments
	if isinstance(payments, str):
		stripped = payments.strip()
		if not stripped:
			payments = []
		else:
			try:
				payments = json.loads(stripped)
			except Exception:
				frappe.throw(_("Invalid JSON format for split payments."), frappe.ValidationError)

	if not isinstance(payments, list):
		frappe.throw(_("Split payments must be provided as a list."), frappe.ValidationError)

	tenders: List[Dict[str, Any]] = []
	tenders_total = 0.0
	for idx, entry in enumerate(payments, start=1):
		if not isinstance(entry, dict):
			frappe.throw(_("Payment entry #{0} must be an object.").format(idx), frappe.ValidationError)
		mop = _resolve_mode_of_payment(entry.get("payment_method"), explicit_method_supplied=True)
		amt = _parse_strict_amount(entry.get("amount"), _("Payment #{0} amount").format(idx), allow_zero=False)
		ref_no = entry.get("reference_no")
		if ref_no is not None and not isinstance(ref_no, str):
			ref_no = str(ref_no)
		tenders.append({
			"payment_method": mop,
			"amount": amt,
			"reference_no": ref_no.strip() if isinstance(ref_no, str) and ref_no.strip() else None,
		})
		tenders_total = flt(tenders_total + amt, 2)

	if tenders_total > remaining_due + 0.01:
		frappe.throw(
			_("Total split payments ({0}) cannot exceed remaining invoice balance ({1}).").format(
				tenders_total, remaining_due
			),
			frappe.ValidationError,
		)
	if tenders_total + 0.01 < remaining_due:
		frappe.throw(
			_("Total split payments ({0}) must cover remaining invoice balance ({1}).").format(
				tenders_total, remaining_due
			),
			frappe.ValidationError,
		)
	return tenders


def record_ro_deposit(
	ro,
	amount,
	payment_method="Debit / Interac",
	reference_no=None,
	**kwargs,
) -> Dict[str, Any]:
	"""Record an upfront customer deposit Payment Entry against a Repair Order and sync payment state."""
	ro.check_permission("write")
	frappe.has_permission("Payment Entry", "create", throw=True)
	frappe.has_permission("Payment Entry", "submit", throw=True)

	explicit_method_supplied = "payment_method" in kwargs or payment_method != "Debit / Interac"
	if isinstance(amount, dict):
		payment_method = amount.get("payment_method", payment_method)
		reference_no = amount.get("reference_no", reference_no)
		if amount.get("payment_method"):
			explicit_method_supplied = True
		amount = amount.get("amount")

	deposit_amt = _parse_strict_amount(amount, "Deposit Amount", allow_zero=False)
	locked = _acquire_locked_ro_state(ro)

	mop = _resolve_mode_of_payment(payment_method, explicit_method_supplied=explicit_method_supplied)
	paid_to = _resolve_paid_to_account(ro.company, mop)

	pe = _create_and_submit_payment_entry(
		customer=ro.customer,
		company=ro.company,
		mop=mop,
		paid_to=paid_to,
		amount=deposit_amt,
		reference_no=reference_no or f"DEP-{ro.name}",
		reference_date=nowdate(),
		remarks=f"Advance deposit for Repair Order {ro.name} [RO:{ro.name}]",
		skip_ro_payment_sync=True,
	)

	deposit_pes = _get_ro_deposit_payment_entries(ro.name, customer=ro.customer)
	total_deposits = flt(sum(flt(p.get("paid_amount")) for p in deposit_pes), 2)
	ro_grand_total = flt(locked.get("grand_total") if locked else ro.grand_total, 2)
	paid_amount = total_deposits
	outstanding_amount = max(0.0, flt(ro_grand_total - paid_amount, 2))
	payment_status = _compute_payment_status(paid_amount, ro_grand_total, has_invoice=False)

	_persist_ro_payment_fields(
		ro.name,
		{
			"deposit_amount": total_deposits,
			"paid_amount": paid_amount,
			"outstanding_amount": outstanding_amount,
			"payment_status": payment_status,
		},
		ro_doc=ro,
	)

	return {
		"status": "success",
		"repair_order": ro.name,
		"payment_entry": pe.name,
		"deposit_amount": flt(ro.deposit_amount, 2),
		"paid_amount": flt(ro.paid_amount, 2),
		"outstanding_amount": flt(ro.outstanding_amount, 2),
		"payment_status": ro.payment_status,
	}


def process_ro_payment_and_release(
	ro,
	payment_method="Debit / Interac",
	paid_amount=None,
	odometer_out=None,
	payments=None,
	**kwargs,
) -> Dict[str, Any]:
	"""
	Finalize billing audit:
	1. Acquires FOR UPDATE lock and rejects already-invoiced/released ROs
	2. Strictly validates odometer out and pre-checkout warehouse stock
	3. Creates submitted ERPNext Sales Invoice with update_stock=1, discounts/fees, and RO advance reconciliation
	4. Reconciles single or split-tender payments against remaining invoice balance
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
		payments = payment_method.get("payments", payments)
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
	if payments is None and "payments" in kwargs:
		payments = kwargs.get("payments")

	pay_amt: float | None = None
	if paid_amount is not None and payments is None:
		pay_amt = _parse_strict_amount(paid_amount, "Paid amount", allow_zero=True)

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
	_validate_pre_checkout_stock(ro, approved_services, ctx)

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

	gross_items_total = 0.0
	row_discounts_total = 0.0

	for idx, s in enumerate(approved_services):
		service_gross = 0.0
		if flt(s.labor_hours) > 0 or flt(s.labor_amount) > 0:
			if cint(getattr(s, "is_flat_rate", 0)):
				labor_rate_val = flt(s.labor_amount)
				si.append("items", {
					"item_code": labor_item_code,
					"item_name": f"Labor: {s.service_title}",
					"description": s.advisor_notes or s.service_title,
					"qty": 1.0,
					"uom": unit_uom,
					"rate": labor_rate_val,
					"income_account": ctx["default_income_account"],
					"cost_center": ctx["default_cost_center"],
				})
				service_gross += labor_rate_val
			else:
				labor_qty = flt(s.labor_hours) or 1.0
				labor_rate_val = flt(s.labor_rate)
				si.append("items", {
					"item_code": labor_item_code,
					"item_name": f"Labor: {s.service_title}",
					"description": s.advisor_notes or s.service_title,
					"qty": labor_qty,
					"uom": "Hour",
					"rate": labor_rate_val,
					"income_account": ctx["default_income_account"],
					"cost_center": ctx["default_cost_center"],
				})
				service_gross += flt(labor_qty * labor_rate_val, 2)

		for p in ctx["parsed_parts_by_service"].get(idx, []):
			item_code = p.get("item_code")
			if not item_code or item_code not in ctx["valid_items"]:
				continue
			item_doc = frappe.get_cached_doc("Item", item_code)
			source = p.get("part_source") or getattr(item_doc, "part_source", "OEM") or "OEM"
			part_name = p.get("item_name") or item_doc.item_name
			qty = flt(p.get("qty", 1.0))

			if str(source).strip() == "Customer Supplied":
				cust_item_code = _ensure_non_stock_item(
					"CUST-SUPPLIED-PART",
					"Customer Supplied Part",
					unit_uom,
					ctx["existing_uoms"],
				)
				si.append("items", {
					"item_code": cust_item_code,
					"item_name": f"{part_name} (Customer Supplied)",
					"description": f"{part_name} [Customer Supplied]",
					"qty": qty or 1.0,
					"uom": unit_uom,
					"rate": 0.0,
					"income_account": ctx["default_income_account"],
					"cost_center": ctx["default_cost_center"],
				})
				continue

			condition = p.get("part_condition") or getattr(item_doc, "part_condition", "New") or "New"
			warranty = p.get("warranty_period") or getattr(item_doc, "warranty_period", "") or ""
			compliance_tag = f"[{source} - {condition}"
			if warranty:
				compliance_tag += f" | Warranty: {warranty}"
			compliance_tag += "]"
			part_wh = p.get("warehouse")
			part_rate = flt(p.get("rate", 0.0))
			si.append("items", {
				"item_code": item_code,
				"item_name": part_name,
				"description": f"{part_name} {compliance_tag}",
				"qty": qty,
				"uom": p.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
				"rate": part_rate,
				"warehouse": part_wh if (part_wh and part_wh in ctx["valid_warehouses"]) else ctx["default_warehouse"],
				"income_account": ctx["default_income_account"],
				"cost_center": ctx["default_cost_center"],
			})
			service_gross += flt(qty * part_rate, 2)

		s_discount = max(0.0, flt(getattr(s, "discount_amount", 0), 2))
		row_discounts_total = flt(row_discounts_total + min(s_discount, flt(service_gross, 2)), 2)
		gross_items_total = flt(gross_items_total + service_gross, 2)

	shop_supplies_amt = max(0.0, flt(getattr(ro, "shop_supplies_amount", 0), 2))
	if shop_supplies_amt > 0:
		shop_item = _ensure_non_stock_item("SHOP-SUPPLIES", "Shop Supplies", unit_uom, ctx["existing_uoms"])
		si.append("items", {
			"item_code": shop_item,
			"item_name": "Shop Supplies",
			"description": "Shop Supplies & Consumables",
			"qty": 1.0,
			"uom": unit_uom,
			"rate": shop_supplies_amt,
			"income_account": ctx["default_income_account"],
			"cost_center": ctx["default_cost_center"],
		})
		gross_items_total = flt(gross_items_total + shop_supplies_amt, 2)

	env_fee_amt = max(0.0, flt(getattr(ro, "environmental_fee_amount", 0), 2))
	if env_fee_amt > 0:
		env_item = _ensure_non_stock_item(
			"ENV-DISPOSAL-FEE",
			"Environmental & Disposal Fee",
			unit_uom,
			ctx["existing_uoms"],
		)
		si.append("items", {
			"item_code": env_item,
			"item_name": "Environmental & Disposal Fee",
			"description": "Environmental / Disposal Fee",
			"qty": 1.0,
			"uom": unit_uom,
			"rate": env_fee_amt,
			"income_account": ctx["default_income_account"],
			"cost_center": ctx["default_cost_center"],
		})
		gross_items_total = flt(gross_items_total + env_fee_amt, 2)

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

	ro_discount = max(0.0, flt(getattr(ro, "discount_amount", 0), 2))
	total_discount = min(flt(ro_discount + row_discounts_total, 2), flt(gross_items_total, 2))
	if total_discount > 0:
		si.apply_discount_on = "Net Total"
		si.discount_amount = total_discount

	si.calculate_taxes_and_totals()
	invoice_total = flt(si.grand_total, 2)
	deposit_allocated = _apply_ro_advances_to_invoice(si, ro.name, ro.customer)
	remaining_due = max(0.0, flt(invoice_total - deposit_allocated, 2))

	tenders_to_create: List[Dict[str, Any]] = []
	if payments is not None:
		tenders_to_create = _validate_split_tenders(payments, remaining_due, explicit_method_supplied)
	else:
		if pay_amt is not None:
			if pay_amt > remaining_due + 0.01:
				frappe.throw(
					_("Paid amount ({0}) cannot exceed remaining Sales Invoice balance ({1}).").format(
						pay_amt, remaining_due
					),
					frappe.ValidationError,
				)
		else:
			pay_amt = remaining_due

		if pay_amt > 0:
			mop = _resolve_mode_of_payment(payment_method, explicit_method_supplied=explicit_method_supplied)
			tenders_to_create = [{"payment_method": mop, "amount": pay_amt, "reference_no": None}]

	if tenders_to_create:
		frappe.has_permission("Payment Entry", "create", throw=True)
		frappe.has_permission("Payment Entry", "submit", throw=True)

	created_pe_names: List[str] = []
	created_pe_total = 0.0
	prev_flag = getattr(frappe.flags, "ism_in_billing_operation", False)
	frappe.flags.ism_in_billing_operation = True
	try:
		si.insert()
		if explicit_zero_tax and si.taxes_and_charges:
			si.taxes_and_charges = None
			si.db_set("taxes_and_charges", None, update_modified=False)
		si.submit()
		si.reload()

		deposit_allocated = flt(getattr(si, "total_advance", deposit_allocated), 2)
		comp_meta = {
			"abbr": ctx["abbr"],
			"default_bank_account": ctx["default_bank_account"],
			"default_cash_account": ctx["default_cash_account"],
		}

		for tender in tenders_to_create:
			si_outstanding = flt(si.outstanding_amount, 2)
			if si_outstanding <= 0:
				break
			tender_amt = min(flt(tender["amount"], 2), si_outstanding)
			if tender_amt <= 0:
				continue
			paid_to = _resolve_paid_to_account(ro.company, tender["payment_method"], comp_meta=comp_meta)
			pe = _create_and_submit_payment_entry(
				customer=ro.customer,
				company=ro.company,
				mop=tender["payment_method"],
				paid_to=paid_to,
				amount=tender_amt,
				reference_no=tender.get("reference_no"),
				sales_invoice=si.name,
				si_grand_total=si.grand_total,
				si_outstanding_amount=si_outstanding,
				skip_ro_payment_sync=True,
			)
			created_pe_names.append(pe.name)
			created_pe_total = flt(created_pe_total + tender_amt, 2)
			si.reload()
	finally:
		frappe.flags.ism_in_billing_operation = prev_flag

	deposit_pes = _get_ro_deposit_payment_entries(ro.name, customer=ro.customer)
	total_deposits = flt(sum(flt(p.get("paid_amount")) for p in deposit_pes), 2)
	total_paid = flt(deposit_allocated + created_pe_total, 2)
	computed_outstanding = max(0.0, flt(invoice_total - total_paid, 2))

	ro.sales_invoice = si.name
	ro.deposit_amount = total_deposits
	ro.paid_amount = total_paid
	ro.outstanding_amount = computed_outstanding
	ro.payment_status = _compute_payment_status(total_paid, invoice_total, has_invoice=True)

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

	if abs(flt(ro.outstanding_amount) - computed_outstanding) > 0.005:
		ro.outstanding_amount = computed_outstanding
		frappe.db.set_value("Repair Order", ro.name, "outstanding_amount", computed_outstanding, update_modified=False)

	return {
		"status": "success",
		"sales_invoice": ro.sales_invoice,
		"payment_entry": created_pe_names[0] if created_pe_names else None,
		"payment_entries": created_pe_names,
		"deposit_allocated": flt(deposit_allocated, 2),
		"deposit_amount": flt(ro.deposit_amount, 2),
		"paid_amount": flt(ro.paid_amount, 2),
		"outstanding_amount": flt(ro.outstanding_amount, 2),
		"payment_status": ro.payment_status,
		"vehicle_released": ro.vehicle_released,
		"checkout_date": ro.checkout_date,
	}


def on_sales_invoice_cancel(doc, method=None) -> None:
	"""Unlink Repair Order when its Sales Invoice is cancelled and recompute payment state."""
	if not doc or not getattr(doc, "name", None):
		return
	ro_names = frappe.get_all("Repair Order", filters={"sales_invoice": doc.name}, pluck="name")
	for ro_name in ro_names:
		_recompute_ro_payment_state(
			ro_name,
			unlinked_si_name=doc.name,
			reopen_if_cancelled_si=True,
		)


def _sync_ros_for_payment_entry(doc, excluded_pe_name: str | None = None) -> None:
	"""Resolve all Repair Orders affected by a Payment Entry and recompute their payment state."""
	if (
		not doc
		or not getattr(doc, "name", None)
		or getattr(frappe.flags, "ism_in_billing_operation", False)
		or getattr(doc.flags, "skip_ro_payment_sync", False)
	):
		return

	target_ros: set[str] = set()
	si_names = [
		ref.reference_name
		for ref in (doc.get("references") or [])
		if getattr(ref, "reference_doctype", None) == "Sales Invoice" and getattr(ref, "reference_name", None)
	]
	if si_names:
		for ro_name in frappe.get_all("Repair Order", filters={"sales_invoice": ["in", si_names]}, pluck="name"):
			target_ros.add(ro_name)

	if getattr(doc, "payment_type", None) == "Receive" and getattr(doc, "party_type", None) == "Customer":
		remark_ro_names = _extract_ro_names_from_remarks(getattr(doc, "remarks", None))
		if remark_ro_names:
			ro_filters: Dict[str, Any] = {"name": ["in", remark_ro_names]}
			if getattr(doc, "party", None):
				ro_filters["customer"] = doc.party
			for ro_name in frappe.get_all("Repair Order", filters=ro_filters, pluck="name"):
				target_ros.add(ro_name)

	for ro_name in sorted(target_ros):
		_recompute_ro_payment_state(ro_name, excluded_pe_name=excluded_pe_name)


def on_payment_entry_submit(doc, method=None) -> None:
	"""Sync Repair Order payment totals when an external Payment Entry is submitted."""
	_sync_ros_for_payment_entry(doc, excluded_pe_name=None)


def on_payment_entry_cancel(doc, method=None) -> None:
	"""Recompute Repair Order payment totals when a linked Payment Entry is cancelled."""
	_sync_ros_for_payment_entry(doc, excluded_pe_name=getattr(doc, "name", None))


__all__ = [
	"on_payment_entry_cancel",
	"on_payment_entry_submit",
	"on_sales_invoice_cancel",
	"process_ro_payment_and_release",
	"record_ro_deposit",
]
