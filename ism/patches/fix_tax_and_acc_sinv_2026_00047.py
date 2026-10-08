# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import json
import frappe
from frappe.utils import flt
from ism.setup import setup_default_taxes_and_payments


def execute():
	"""
	1. Setup default 13% HST template and Debit / Interac mode of payment.
	2. Fix ACC-SINV-2026-00047 and RO-2026-00105:
	   Rebuild ACC-SINV-2026-00047 to apply 13% HST ($78.65) on net total ($605.00),
	   producing grand total of $683.65.
	   Re-create Payment Entry to fully allocate $683.65 with 0 unallocated.
	   Ensure RO-2026-00105 reflects 13% HST template, $78.65 tax, $683.65 grand total,
	   and links to ACC-SINV-2026-00047 with Paid status.
	"""
	setup_default_taxes_and_payments()

	ro_name = "RO-2026-00105"
	si_name = "ACC-SINV-2026-00047"
	pe_name = "ACC-PAY-2026-00044"

	if not frappe.db.exists("Repair Order", ro_name):
		return

	ro = frappe.get_doc("Repair Order", ro_name)

	# 1. Cancel and delete old Payment Entry if exists
	if frappe.db.exists("Payment Entry", pe_name):
		pe = frappe.get_doc("Payment Entry", pe_name)
		if pe.docstatus == 1:
			pe.cancel()
		frappe.delete_doc("Payment Entry", pe_name, force=True, ignore_permissions=True)

	# 2. Detach SI from RO so backlink check doesn't block cancellation
	ro.db_set("sales_invoice", None)

	# 3. Cancel and delete old SI if exists
	if frappe.db.exists("Sales Invoice", si_name):
		si = frappe.get_doc("Sales Invoice", si_name)
		if si.docstatus == 1:
			si.cancel()
		frappe.delete_doc("Sales Invoice", si_name, force=True, ignore_permissions=True)

	# 4. Recreate Sales Invoice with 13% HST
	company_abbr = frappe.db.get_value("Company", ro.company, "abbr") or ro.company
	tax_template = f"13% HST - {company_abbr}"
	default_income_account = f"Sales - {company_abbr}"
	default_cost_center = f"Main - {company_abbr}"
	default_warehouse = "Stores - I"

	new_si = frappe.new_doc("Sales Invoice")
	new_si.name = si_name
	new_si.customer = ro.customer
	new_si.company = ro.company
	new_si.posting_date = "2026-10-08"
	new_si.currency = ro.currency or "CAD"
	new_si.update_stock = 1
	new_si.disable_rounded_total = 1
	new_si.taxes_and_charges = tax_template

	for s in [srv for srv in ro.services if srv.status == "Approved"]:
		if flt(s.labor_hours) > 0 or flt(s.labor_amount) > 0:
			new_si.append("items", {
				"item_code": "LABOR-STD",
				"item_name": f"Labor: {s.service_title}",
				"description": s.advisor_notes or s.service_title,
				"qty": flt(s.labor_hours) or 1.0,
				"uom": "Hour",
				"rate": flt(s.labor_rate),
				"income_account": default_income_account,
				"cost_center": default_cost_center,
			})
		parts = json.loads(s.parts) if isinstance(s.parts, str) else (s.parts or [])
		for p in parts:
			if p.get("item_code") and frappe.db.exists("Item", p.get("item_code")):
				item_doc = frappe.get_cached_doc("Item", p.get("item_code"))
				new_si.append("items", {
					"item_code": p.get("item_code"),
					"item_name": p.get("item_name") or item_doc.item_name,
					"description": p.get("item_name") or item_doc.item_name,
					"qty": flt(p.get("qty", 1.0)),
					"uom": p.get("uom") or getattr(item_doc, "stock_uom", "Nos"),
					"rate": flt(p.get("rate", 0.0)),
					"warehouse": p.get("warehouse") or default_warehouse,
					"income_account": default_income_account,
					"cost_center": default_cost_center,
				})

	new_si.set_missing_values()
	new_si.set_taxes()
	if not new_si.taxes:
		new_si.append("taxes", {
			"charge_type": "On Net Total",
			"account_head": f"HST 13% - {company_abbr}",
			"description": "HST @ 13.0%",
			"rate": 13.0,
		})

	new_si.calculate_taxes_and_totals()
	new_si.flags.ignore_permissions = True
	new_si.insert()
	new_si.submit()

	# 5. Recreate Payment Entry
	new_pe = frappe.new_doc("Payment Entry")
	new_pe.name = pe_name
	new_pe.payment_type = "Receive"
	new_pe.party_type = "Customer"
	new_pe.party = ro.customer
	new_pe.company = ro.company
	new_pe.paid_amount = new_si.grand_total
	new_pe.received_amount = new_si.grand_total
	new_pe.mode_of_payment = "Cash"
	new_pe.paid_to = f"Cash - {company_abbr}"
	new_pe.append("references", {
		"reference_doctype": "Sales Invoice",
		"reference_name": new_si.name,
		"total_amount": new_si.grand_total,
		"outstanding_amount": new_si.outstanding_amount,
		"allocated_amount": new_si.grand_total,
	})
	new_pe.flags.ignore_permissions = True
	new_pe.insert()
	new_pe.submit()

	# 6. Update Repair Order
	ro.reload()
	ro.taxes_and_charges = tax_template
	ro.tax_rate = 13.0
	ro.net_total = new_si.net_total
	ro.total_taxes_and_charges = new_si.total_taxes_and_charges
	ro.grand_total = new_si.grand_total
	ro.paid_amount = new_si.grand_total
	ro.outstanding_amount = 0.0
	ro.sales_invoice = new_si.name
	ro.payment_status = "Paid"

	ro.db_set("taxes_and_charges", ro.taxes_and_charges)
	ro.db_set("tax_rate", 13.0)
	ro.db_set("net_total", ro.net_total)
	ro.db_set("total_taxes_and_charges", ro.total_taxes_and_charges)
	ro.db_set("grand_total", ro.grand_total)
	ro.db_set("paid_amount", ro.paid_amount)
	ro.db_set("outstanding_amount", 0.0)
	ro.db_set("sales_invoice", ro.sales_invoice)
	ro.db_set("payment_status", "Paid")
