# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import frappe


def _resolve_default_company(company=None):
	"""Resolve explicit, user default, global default, or first configured Company without hardcoding."""
	return (
		company
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or frappe.db.get_value("Company", {"is_group": 0}, "name")
		or frappe.db.get_value("Company", {}, "name")
	)


def _company_has_default_hst_setup(comp):
	abbr = comp.abbr or comp.name
	template_name = f"13% HST - {abbr}"
	return bool(
		frappe.db.exists("Sales Taxes and Charges Template", template_name)
		and frappe.db.exists("Purchase Taxes and Charges Template", template_name)
		and frappe.db.exists("Item Tax Template", template_name)
	)


def _ensure_company_tax_template(template_doctype, company_name, template_name, tax_row):
	"""Idempotently ensure the 13% HST template exists and is the sole default for the company."""
	if not frappe.db.exists(template_doctype, template_name):
		try:
			tmpl = frappe.get_doc({
				"doctype": template_doctype,
				"title": "13% HST",
				"company": company_name,
				"is_default": 1,
				"taxes": [tax_row],
			})
			tmpl.insert(ignore_permissions=True)
		except Exception as exc:
			frappe.logger("ism").warning(
				f"Could not create {template_doctype} '{template_name}' for {company_name}: {exc}"
			)
	else:
		frappe.db.set_value(template_doctype, template_name, "is_default", 1)

	other_names = frappe.get_all(
		template_doctype,
		filters={"company": company_name, "is_default": 1, "name": ["!=", template_name]},
		pluck="name",
	)
	if other_names:
		dt = frappe.qb.DocType(template_doctype)
		(
			frappe.qb.update(dt)
			.set(dt.is_default, 0)
			.where(dt.name.isin(other_names))
		).run()


def _ensure_default_tax_rule(tax_type, template_name, company):
	"""Ensure a generic default Tax Rule exists for Sales or Purchase in this company."""
	all_rules = frappe.get_all(
		"Tax Rule",
		filters={"company": company, "tax_type": tax_type},
		fields=[
			"name",
			"sales_tax_template",
			"purchase_tax_template",
			"customer",
			"customer_group",
			"supplier",
			"supplier_group",
			"item",
			"item_group",
			"tax_category",
		],
	)

	template_field = "sales_tax_template" if tax_type == "Sales" else "purchase_tax_template"
	generic_rules = [
		r
		for r in all_rules
		if not r.get("customer")
		and not r.get("customer_group")
		and not r.get("supplier")
		and not r.get("supplier_group")
		and not r.get("item")
		and not r.get("item_group")
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
		except Exception as exc:
			frappe.logger("ism").warning(f"Could not create default {tax_type} Tax Rule for {company}: {exc}")


def ensure_company_13_percent_hst_setup(comp):
	"""Ensure 13% HST account, Sales and Purchase Taxes templates, Item Tax Template, and Tax Rules exist."""
	if isinstance(comp, str):
		comp = frappe.get_cached_doc("Company", comp)
	c_name = comp.name
	abbr = comp.abbr or c_name
	currency = comp.default_currency or "CAD"

	duties_and_taxes = (
		frappe.db.get_value("Account", {"account_name": "Duties and Taxes", "company": c_name}, "name")
		or f"Duties and Taxes - {abbr}"
	)
	account_name = f"HST 13% - {abbr}"
	if not frappe.db.exists("Account", account_name) and frappe.db.exists("Account", duties_and_taxes):
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
		except Exception as exc:
			frappe.logger("ism").warning(f"Could not create Account '{account_name}' for {c_name}: {exc}")

	tax_head = account_name if frappe.db.exists("Account", account_name) else duties_and_taxes
	template_name = f"13% HST - {abbr}"

	_ensure_company_tax_template(
		"Sales Taxes and Charges Template",
		c_name,
		template_name,
		{
			"charge_type": "On Net Total",
			"account_head": tax_head,
			"rate": 13.0,
			"description": "HST @ 13.0%",
		},
	)
	_ensure_company_tax_template(
		"Purchase Taxes and Charges Template",
		c_name,
		template_name,
		{
			"category": "Total",
			"add_deduct_tax": "Add",
			"charge_type": "On Net Total",
			"account_head": tax_head,
			"rate": 13.0,
			"description": "HST @ 13.0%",
		},
	)

	if not frappe.db.exists("Item Tax Template", template_name):
		try:
			it_tmpl = frappe.get_doc({
				"doctype": "Item Tax Template",
				"title": "13% HST",
				"company": c_name,
				"taxes": [{"tax_type": tax_head, "tax_rate": 13.0}],
			})
			it_tmpl.insert(ignore_permissions=True)
		except Exception as exc:
			frappe.logger("ism").warning(
				f"Could not create Item Tax Template '{template_name}' for {c_name}: {exc}"
			)

	_ensure_default_tax_rule("Sales", template_name, c_name)
	_ensure_default_tax_rule("Purchase", template_name, c_name)


def setup_default_taxes_and_payments(only_if_missing=False):
	"""Ensure 13% HST tax templates and Debit / Interac Mode of Payment exist."""
	companies = frappe.get_all(
		"Company",
		fields=["name", "abbr", "default_currency", "default_bank_account", "default_cash_account"],
	)

	if only_if_missing and frappe.db.exists("Mode of Payment", "Debit / Interac"):
		if all(_company_has_default_hst_setup(comp) for comp in companies):
			return

	if not frappe.db.exists("Mode of Payment", "Debit / Interac"):
		mop = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": "Debit / Interac",
			"type": "Bank",
			"enabled": 1,
		})
		mop.insert(ignore_permissions=True)

	if frappe.db.exists("Mode of Payment", "Debit / Interac"):
		mop_doc = frappe.get_doc("Mode of Payment", "Debit / Interac")
		existing_companies = {r.company for r in (mop_doc.accounts or [])}
		candidate_accounts = {}
		for comp in companies:
			if comp.name in existing_companies:
				continue
			acc = (
				comp.get("default_bank_account")
				or comp.get("default_cash_account")
				or f"Cash - {comp.abbr or comp.name}"
			)
			candidate_accounts[comp.name] = acc

		if candidate_accounts:
			valid_accounts = set(
				frappe.get_all("Account", filters={"name": ["in", list(candidate_accounts.values())]}, pluck="name")
			)
			updated_mop = False
			for comp_name, acc_name in candidate_accounts.items():
				if acc_name in valid_accounts:
					mop_doc.append("accounts", {"company": comp_name, "default_account": acc_name})
					updated_mop = True
			if updated_mop:
				mop_doc.save(ignore_permissions=True)

	try:
		frappe.db.set_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template", 1)
	except Exception as exc:
		frappe.logger("ism").warning(f"Could not configure Accounts Settings auto-tax setting: {exc}")

	for comp in companies:
		ensure_company_13_percent_hst_setup(comp)


def _get_or_create_default_tax_template(template_doctype, company=None):
	"""Return (or create) the default 13% HST template for Sales or Purchase in the target company."""
	resolved_company = _resolve_default_company(company)
	if not resolved_company:
		return None

	abbr = frappe.db.get_value("Company", resolved_company, "abbr") or resolved_company
	template_name = f"13% HST - {abbr}"

	if not frappe.db.exists(template_doctype, template_name):
		ensure_company_13_percent_hst_setup(resolved_company)

	if frappe.db.exists(template_doctype, template_name):
		return template_name

	return frappe.db.get_value(
		template_doctype,
		{"company": resolved_company, "is_default": 1},
		"name",
	)


def get_or_create_default_purchase_tax_template(company=None):
	"""Return the default 13% HST Purchase Taxes and Charges Template for the company."""
	return _get_or_create_default_tax_template("Purchase Taxes and Charges Template", company)


def get_or_create_default_sales_tax_template(company=None):
	"""Return the default 13% HST Sales Taxes and Charges Template for the company."""
	return _get_or_create_default_tax_template("Sales Taxes and Charges Template", company)
