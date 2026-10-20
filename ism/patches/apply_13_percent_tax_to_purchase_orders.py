import frappe
from frappe.utils import flt
from ism.setup import get_or_create_default_purchase_tax_template


def execute():
	"""Apply default 13% HST purchase taxes to existing Purchase Orders that lack tax."""
	pos = frappe.get_all(
		"Purchase Order",
		filters={"docstatus": ["<", 2]},
		fields=["name", "company", "net_total", "total_taxes_and_charges"],
	)

	for p in pos:
		if flt(p.total_taxes_and_charges) == 0.0 and flt(p.net_total) > 0.0:
			po_name = p.name
			po = frappe.get_doc("Purchase Order", po_name)
			tmpl = get_or_create_default_purchase_tax_template(po.company)
			if not tmpl:
				continue

			po.taxes_and_charges = tmpl
			from erpnext.controllers.accounts_controller import get_taxes_and_charges
			taxes = get_taxes_and_charges("Purchase Taxes and Charges Template", tmpl)
			po.set("taxes", [])
			for t in taxes:
				po.append("taxes", t)

			po.calculate_taxes_and_totals()
			po.db_set("taxes_and_charges", po.taxes_and_charges)
			po.db_set("total_taxes_and_charges", po.total_taxes_and_charges)
			po.db_set("grand_total", po.grand_total)
			po.db_set("rounded_total", po.rounded_total)
			po.db_set("in_words", po.in_words)

			frappe.db.delete("Purchase Taxes and Charges", {"parent": po_name})
			for idx, t in enumerate(po.taxes, 1):
				t.parent = po_name
				t.parenttype = "Purchase Order"
				t.parentfield = "taxes"
				t.idx = idx
				t.db_insert()
