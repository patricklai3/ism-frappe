# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Step 20: Non-destructive ERPNext accounting, Ontario 13% HST, payment modes, and ISM billing items."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, getdate, today

from ism.setup_tasks.seed import ensure_square_bank_account_and_mop, setup_parts_item_group
from ism.setup_tasks.taxes import (
	_get_existing_company_tax_template,
	ensure_company_13_percent_hst_setup,
	setup_default_taxes_and_payments,
)
from ism.setup_wizard.base import BaseSetupStep, build_desk_link, normalize_records_list
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	TIER_CORE,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
)
from ism.setup_wizard.presets import CANADIAN_PAYMENT_MODES

CORE_UOMS: tuple[str, ...] = ("Hour", "Unit", "Nos")

CORE_BILLING_ITEMS: tuple[tuple[str, str, str], ...] = (
	("LABOR-STD", "Standard Labor", "Hour"),
	("SHOP-SUPPLIES", "Shop Supplies", "Unit"),
	("ENV-DISPOSAL-FEE", "Environmental Disposal Fee", "Unit"),
	("CUST-SUPPLIED-PART", "Customer Supplied Part", "Nos"),
)


def _resolve_active_fiscal_years(target_date: Any) -> list[str]:
	if not frappe.db.exists("DocType", "Fiscal Year"):
		return []
	return frappe.get_all(
		"Fiscal Year",
		filters={
			"disabled": 0,
			"year_start_date": ["<=", target_date],
			"year_end_date": [">=", target_date],
		},
		pluck="name",
	)


def _ensure_uom(uom_name: str) -> bool:
	if frappe.db.exists("UOM", uom_name):
		return False
	frappe.get_doc({
		"doctype": "UOM",
		"uom_name": uom_name,
		"must_be_whole_number": 0,
	}).insert(ignore_permissions=True)
	return True


def _ensure_non_stock_billing_item(item_code: str, item_name: str, uom: str) -> bool:
	_ensure_uom(uom)
	if frappe.db.exists("Item", item_code):
		return False
	item_group = (
		"Services"
		if frappe.db.exists("Item Group", "Services")
		else (frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups")
	)
	frappe.get_doc({
		"doctype": "Item",
		"item_code": item_code,
		"item_name": item_name,
		"item_group": item_group,
		"is_stock_item": 0,
		"stock_uom": uom,
	}).insert(ignore_permissions=True)
	return True


class ERPNextFoundationStep(BaseSetupStep):
	meta = StepMeta(
		id="erpnext_foundation",
		title="ERPNext Accounting, Tax & Billing Foundation",
		phase="erpnext",
		order=20,
		schema_version=1,
		tier=TIER_CORE,
		depends_on=("site_environment",),
		owner_domain="ism",
		managed_settings_sections=(),
		allow_skip=False,
		description=(
			"Non-destructively audit and additively seed ERPNext Company defaults, Ontario 13% HST "
			"templates, Canadian Modes of Payment, Square CAD bank account, core UOMs, Parts item group, "
			"and ISM non-stock billing items."
		),
	)

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "canadian_payment_modes",
				"label": "Canadian Shop Payment Modes & Ontario 13% HST",
				"description": (
					"Additively seeds standard Canadian payment modes (Cash, Interac Debit, Interac e-Transfer, "
					"Visa / Mastercard, Square, Cheque), Ontario 13% HST templates if missing, Square CAD account, "
					"and core ISM billing items."
				),
				"payload": {
					"ensure_hst": True,
					"ensure_square_account": True,
					"ensure_billing_items": True,
					"modes_of_payment": CANADIAN_PAYMENT_MODES,
				},
			}
		]

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		urls = {
			"global_defaults": build_desk_link("Global Defaults", site_url=ctx.site_url),
			"company": build_desk_link("Company", ctx.company, site_url=ctx.site_url),
			"fiscal_year": build_desk_link("Fiscal Year", site_url=ctx.site_url),
			"selling_settings": build_desk_link("Selling Settings", site_url=ctx.site_url),
			"buying_settings": build_desk_link("Buying Settings", site_url=ctx.site_url),
			"stock_settings": build_desk_link("Stock Settings", site_url=ctx.site_url),
			"accounts_settings": build_desk_link("Accounts Settings", site_url=ctx.site_url),
			"mode_of_payment": build_desk_link("Mode of Payment", site_url=ctx.site_url),
			"item": build_desk_link("Item", site_url=ctx.site_url),
		}
		return urls

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		default_warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
		selling_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"fields": [
				{
					"fieldname": "ensure_hst",
					"label": "Ensure Ontario 13% HST Tax Setup",
					"fieldtype": "Check",
					"default": 1,
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": "Additively seed 13% HST Sales/Purchase Tax Templates & Rules only when missing.",
				},
				{
					"fieldname": "ensure_square_account",
					"label": "Ensure Square CAD Bank Account & Mode of Payment",
					"fieldtype": "Check",
					"default": 1,
					"reqd": False,
					"read_only": False,
					"is_secret": False,
				},
				{
					"fieldname": "ensure_billing_items",
					"label": "Ensure Core UOMs, Parts Group & Billing Items",
					"fieldtype": "Check",
					"default": 1,
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": "Seed Hour/Unit/Nos UOMs, Parts Item Group, and LABOR-STD / SHOP-SUPPLIES / ENV-DISPOSAL-FEE / CUST-SUPPLIED-PART.",
				},
				{
					"fieldname": "modes_of_payment",
					"label": "Additional Modes of Payment",
					"fieldtype": "JSON",
					"default": [],
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": "List/JSON/CSV of {mode_of_payment, type, enabled} to additively create if missing.",
				},
				{
					"fieldname": "default_warehouse",
					"label": "Default Warehouse (only if blank)",
					"fieldtype": "Link",
					"options": "Warehouse",
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"value": default_warehouse,
				},
				{
					"fieldname": "selling_price_list",
					"label": "Default Selling Price List (only if blank)",
					"fieldtype": "Link",
					"options": "Price List",
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"value": selling_price_list,
				},
			],
			"secret_fields": [],
			"presets": self.get_presets(ctx),
			"desk_urls": self._desk_urls(ctx),
		}

	def inspect(self, ctx: SetupContext) -> StepInspection:
		checks: list[CheckResult] = []

		# 1. Global Defaults.default_company
		default_company = (frappe.db.get_single_value("Global Defaults", "default_company") or "").strip()
		company_name = default_company or (ctx.company or "")
		if not default_company:
			checks.append(
				CheckResult(
					code="default_company",
					level=CHECK_FAIL,
					message="Global Defaults.default_company is not set.",
					remediation="Set Default Company in ERPNext Global Defaults.",
				)
			)
		elif not frappe.db.exists("Company", default_company):
			checks.append(
				CheckResult(
					code="default_company",
					level=CHECK_FAIL,
					message=f"Global Defaults.default_company '{default_company}' does not exist.",
					remediation="Select an existing Company in Global Defaults.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="default_company",
					level=CHECK_PASS,
					message=f"Default Company is '{default_company}'.",
				)
			)

		# 2. Company accounting & contact defaults
		comp_meta: dict[str, Any] = {}
		if company_name and frappe.db.exists("Company", company_name):
			comp_meta = (
				frappe.db.get_value(
					"Company",
					company_name,
					[
						"abbr",
						"default_currency",
						"default_bank_account",
						"default_cash_account",
						"default_income_account",
						"cost_center",
						"phone_no",
						"email",
					],
					as_dict=True,
				)
				or {}
			)
			currency = (comp_meta.get("default_currency") or "").strip()
			if currency != "CAD":
				checks.append(
					CheckResult(
						code="company_currency",
						level=CHECK_WARN,
						message=f"Company '{company_name}' default_currency is '{currency or '(unset)'}' (expected 'CAD' for Ontario ISM shops).",
						remediation="Verify Company default currency in ERPNext Company settings.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="company_currency",
						level=CHECK_PASS,
						message=f"Company '{company_name}' currency is CAD.",
					)
				)

			missing_critical_gl = [
				f for f in ("default_income_account", "cost_center") if not comp_meta.get(f)
			]
			missing_optional_gl = [
				f for f in ("default_bank_account", "default_cash_account") if not comp_meta.get(f)
			]
			if missing_critical_gl:
				checks.append(
					CheckResult(
						code="company_gl_defaults",
						level=CHECK_FAIL,
						message=f"Company '{company_name}' missing critical GL default(s): {', '.join(missing_critical_gl)}.",
						remediation="Configure Default Income Account and Cost Center on the Company record.",
					)
				)
			elif missing_optional_gl:
				checks.append(
					CheckResult(
						code="company_gl_defaults",
						level=CHECK_WARN,
						message=f"Company '{company_name}' missing recommended account(s): {', '.join(missing_optional_gl)}.",
						remediation="Set Default Bank Account and Default Cash Account on the Company record.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="company_gl_defaults",
						level=CHECK_PASS,
						message=f"Company '{company_name}' core GL defaults (income, cost center, bank, cash) are configured.",
					)
				)

			missing_contact = [f for f in ("phone_no", "email") if not (comp_meta.get(f) or "").strip()]
			if missing_contact:
				checks.append(
					CheckResult(
						code="company_contact",
						level=CHECK_WARN,
						message=f"Company '{company_name}' missing contact field(s): {', '.join(missing_contact)}.",
						remediation="Add phone_no and email on Company so customer invoices and quotes display shop contact info.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="company_contact",
						level=CHECK_PASS,
						message=f"Company '{company_name}' phone_no and email are configured.",
					)
				)

		# 3. Active Fiscal Year covering today
		current_date = getdate(today())
		active_fys = _resolve_active_fiscal_years(current_date)
		if active_fys:
			checks.append(
				CheckResult(
					code="fiscal_year",
					level=CHECK_PASS,
					message=f"Active Fiscal Year covering {current_date}: {', '.join(active_fys)}.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="fiscal_year",
					level=CHECK_FAIL,
					message=f"No active Fiscal Year covers current date ({current_date}).",
					remediation="Create and enable a Fiscal Year covering the current year in Desk > Fiscal Year.",
				)
			)

		# 4. Selling & Buying Settings
		selling_price_list = (
			frappe.db.get_single_value("Selling Settings", "selling_price_list") or ""
		).strip()
		if selling_price_list:
			checks.append(
				CheckResult(
					code="selling_price_list",
					level=CHECK_PASS,
					message=f"Selling Settings.selling_price_list is '{selling_price_list}'.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="selling_price_list",
					level=CHECK_WARN,
					message="Selling Settings.selling_price_list is blank.",
					remediation="Set a default Selling Price List (e.g. 'Standard Selling') in Selling Settings.",
				)
			)

		buying_price_list = (
			frappe.db.get_single_value("Buying Settings", "buying_price_list") or ""
		).strip()
		checks.append(
			CheckResult(
				code="buying_settings",
				level=CHECK_PASS if buying_price_list else CHECK_INFO,
				message=(
					f"Buying Settings.buying_price_list is '{buying_price_list}'."
					if buying_price_list
					else "Buying Settings.buying_price_list is not set (optional)."
				),
			)
		)

		# 5. Stock Settings.default_warehouse
		default_wh = (frappe.db.get_single_value("Stock Settings", "default_warehouse") or "").strip()
		valid_wh = bool(
			default_wh
			and frappe.db.exists("Warehouse", {"name": default_wh, "is_group": 0, "disabled": 0})
		)
		if valid_wh:
			checks.append(
				CheckResult(
					code="default_warehouse",
					level=CHECK_PASS,
					message=f"Stock Settings.default_warehouse is '{default_wh}'.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="default_warehouse",
					level=CHECK_FAIL,
					message=(
						f"Stock Settings.default_warehouse '{default_wh}' is invalid or disabled."
						if default_wh
						else "Stock Settings.default_warehouse is not configured."
					),
					remediation="Set an active non-group Default Warehouse in Stock Settings.",
				)
			)

		# 6. Core UOMs
		existing_uoms = set(
			frappe.get_all("UOM", filters={"name": ["in", list(CORE_UOMS)]}, pluck="name")
		)
		missing_uoms = [u for u in CORE_UOMS if u not in existing_uoms]
		if missing_uoms:
			checks.append(
				CheckResult(
					code="core_uoms",
					level=CHECK_FAIL,
					message=f"Missing core UOM(s): {', '.join(missing_uoms)}.",
					remediation="Apply this step with ensure_billing_items=True.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="core_uoms",
					level=CHECK_PASS,
					message="All core UOMs (Hour, Unit, Nos) exist.",
				)
			)

		# 7. Parts Item Group
		has_parts_group = bool(frappe.db.exists("Item Group", "Parts"))
		checks.append(
			CheckResult(
				code="parts_item_group",
				level=CHECK_PASS if has_parts_group else CHECK_FAIL,
				message=(
					"'Parts' Item Group exists."
					if has_parts_group
					else "'Parts' Item Group is missing."
				),
				remediation=None if has_parts_group else "Apply this step with ensure_billing_items=True.",
			)
		)

		# 8. Active Sales & Purchase Tax Templates (13% HST)
		sales_tax_tmpl = (
			_get_existing_company_tax_template("Sales Taxes and Charges Template", company_name)
			if company_name
			else None
		)
		purchase_tax_tmpl = (
			_get_existing_company_tax_template("Purchase Taxes and Charges Template", company_name)
			if company_name
			else None
		)
		if sales_tax_tmpl and purchase_tax_tmpl:
			checks.append(
				CheckResult(
					code="hst_tax_templates",
					level=CHECK_PASS,
					message=(
						f"Active tax templates configured: Sales='{sales_tax_tmpl}', "
						f"Purchase='{purchase_tax_tmpl}'."
					),
				)
			)
		else:
			missing_types = []
			if not sales_tax_tmpl:
				missing_types.append("Sales")
			if not purchase_tax_tmpl:
				missing_types.append("Purchase")
			checks.append(
				CheckResult(
					code="hst_tax_templates",
					level=CHECK_FAIL,
					message=f"Missing active {', '.join(missing_types)} Tax Template(s) for '{company_name}'.",
					remediation="Apply this step with ensure_hst=True to seed Ontario 13% HST templates.",
				)
			)

		# 9. Accounts Settings.add_taxes_from_taxes_and_charges_template
		auto_tax = cint(
			frappe.db.get_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template")
			or 0
		)
		checks.append(
			CheckResult(
				code="accounts_auto_tax",
				level=CHECK_PASS if auto_tax == 1 else CHECK_WARN,
				message=(
					"Accounts Settings.add_taxes_from_taxes_and_charges_template is enabled."
					if auto_tax == 1
					else "Accounts Settings.add_taxes_from_taxes_and_charges_template is disabled."
				),
				remediation=None if auto_tax == 1 else "Enable auto-tax in Accounts Settings or apply ensure_hst=True.",
			)
		)

		# 10. Modes of Payment
		enabled_mops = set(
			frappe.get_all("Mode of Payment", filters={"enabled": 1}, pluck="name")
		)
		expected_mops = ("Debit / Interac", "Square")
		missing_mops = [m for m in expected_mops if m not in enabled_mops]
		if missing_mops:
			checks.append(
				CheckResult(
					code="modes_of_payment",
					level=CHECK_WARN,
					message=(
						f"Found {len(enabled_mops)} enabled Mode(s) of Payment; "
						f"recommended ISM mode(s) missing: {', '.join(missing_mops)}."
					),
					remediation="Apply preset 'canadian_payment_modes' or ensure_hst/ensure_square_account.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="modes_of_payment",
					level=CHECK_PASS,
					message=f"{len(enabled_mops)} enabled Mode(s) of Payment including Debit / Interac and Square.",
				)
			)

		# 11. Square - <abbr> CAD Bank Account
		abbr = (comp_meta.get("abbr") or "").strip()
		square_account_name = f"Square - {abbr}" if abbr else None
		has_square_acct = bool(square_account_name and frappe.db.exists("Account", square_account_name))
		checks.append(
			CheckResult(
				code="square_bank_account",
				level=CHECK_PASS if has_square_acct else CHECK_WARN,
				message=(
					f"Square CAD Bank Account '{square_account_name}' exists."
					if has_square_acct
					else f"Square CAD Bank Account '{square_account_name or 'Square - <abbr>'}' is missing."
				),
				remediation=None if has_square_acct else "Apply this step with ensure_square_account=True.",
			)
		)

		# 12. Non-stock billing items
		billing_codes = [code for code, _name, _uom in CORE_BILLING_ITEMS]
		existing_items = set(
			frappe.get_all("Item", filters={"name": ["in", billing_codes]}, pluck="name")
		)
		missing_items = [code for code in billing_codes if code not in existing_items]
		if missing_items:
			checks.append(
				CheckResult(
					code="billing_items",
					level=CHECK_FAIL,
					message=f"Missing core non-stock billing Item(s): {', '.join(missing_items)}.",
					remediation="Apply this step with ensure_billing_items=True.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="billing_items",
					level=CHECK_PASS,
					message=f"All {len(billing_codes)} core non-stock billing items exist.",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = "ERPNext accounting/tax/billing foundation has missing prerequisites."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = "ERPNext foundation is usable with minor recommendations."
		else:
			status = STEP_STATUS_COMPLETE
			summary = f"ERPNext foundation for '{company_name}' (CAD / 13% HST / billing items) is ready."

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"active_fiscal_years": len(active_fys),
				"enabled_modes_of_payment": len(enabled_mops),
				"billing_items_present": len(existing_items),
			},
			desk_urls=self._desk_urls(ctx),
		)

	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		created: list[str] = []
		updated: list[str] = []
		unchanged: list[str] = []
		warnings: list[str] = []

		company_name = (
			frappe.db.get_single_value("Global Defaults", "default_company") or ctx.company or ""
		).strip()

		# 1. Optional Ontario 13% HST setup (strictly non-destructive)
		if cint(payload.get("ensure_hst")):
			if company_name and frappe.db.exists("Company", company_name):
				seeded = ensure_company_13_percent_hst_setup(company_name)
				if seeded:
					created.append(f"13% HST Tax Setup ({company_name})")
				else:
					unchanged.append(f"13% HST Tax Setup ({company_name})")
			else:
				warnings.append("Skipped HST setup: no active Company found.")
			setup_default_taxes_and_payments(only_if_missing=True)

		# 2. Optional Square CAD Bank Account & MOP
		if cint(payload.get("ensure_square_account")):
			before_mop = bool(frappe.db.exists("Mode of Payment", "Square"))
			seeded_accounts = ensure_square_bank_account_and_mop() or {}
			if not before_mop and frappe.db.exists("Mode of Payment", "Square"):
				created.append("Mode of Payment: Square")
			else:
				unchanged.append("Mode of Payment: Square")
			if seeded_accounts:
				updated.append(f"Square Bank Account links ({len(seeded_accounts)} company)")

		# 3. Optional core UOMs, Parts Item Group, and non-stock billing items
		if cint(payload.get("ensure_billing_items")):
			for uom_name in CORE_UOMS:
				if _ensure_uom(uom_name):
					created.append(f"UOM: {uom_name}")
				else:
					unchanged.append(f"UOM: {uom_name}")

			if not frappe.db.exists("Item Group", "Parts"):
				setup_parts_item_group()
				if frappe.db.exists("Item Group", "Parts"):
					created.append("Item Group: Parts")
			else:
				unchanged.append("Item Group: Parts")

			for item_code, item_name, uom in CORE_BILLING_ITEMS:
				if _ensure_non_stock_billing_item(item_code, item_name, uom):
					created.append(f"Item: {item_code}")
				else:
					unchanged.append(f"Item: {item_code}")

		# 4. Additive Modes of Payment (never overwrites existing MOP rows)
		raw_mops = payload.get("modes_of_payment")
		if raw_mops:
			mop_records = normalize_records_list(raw_mops)
			comp_accounts: dict[str, Any] = {}
			if company_name and frappe.db.exists("Company", company_name):
				comp_accounts = (
					frappe.db.get_value(
						"Company",
						company_name,
						["default_bank_account", "default_cash_account"],
						as_dict=True,
					)
					or {}
				)
			for rec in mop_records:
				mop_name = str(rec.get("mode_of_payment") or rec.get("name") or "").strip()
				if not mop_name:
					continue
				if frappe.db.exists("Mode of Payment", mop_name):
					unchanged.append(f"Mode of Payment: {mop_name}")
					continue
				mop_type = str(rec.get("type") or "Bank").strip()
				if mop_type not in ("Cash", "Bank", "General", "Phone"):
					mop_type = "Bank"
				mop_doc = frappe.get_doc({
					"doctype": "Mode of Payment",
					"mode_of_payment": mop_name,
					"type": mop_type,
					"enabled": cint(rec.get("enabled", 1)),
				})
				default_acct = (
					comp_accounts.get("default_cash_account")
					if mop_type == "Cash"
					else comp_accounts.get("default_bank_account")
				)
				if company_name and default_acct and frappe.db.exists("Account", default_acct):
					mop_doc.append("accounts", {"company": company_name, "default_account": default_acct})
				mop_doc.insert(ignore_permissions=True)
				created.append(f"Mode of Payment: {mop_name}")

		# 5. Optional default_warehouse (ONLY if currently blank)
		req_wh = str(payload.get("default_warehouse") or "").strip()
		if req_wh:
			cur_wh = (frappe.db.get_single_value("Stock Settings", "default_warehouse") or "").strip()
			if cur_wh:
				unchanged.append(f"Stock Settings.default_warehouse={cur_wh}")
			elif frappe.db.exists("Warehouse", {"name": req_wh, "is_group": 0, "disabled": 0}):
				stock_settings = frappe.get_single("Stock Settings")
				stock_settings.default_warehouse = req_wh
				stock_settings.save(ignore_permissions=True)
				updated.append(f"Stock Settings.default_warehouse={req_wh}")
			else:
				warnings.append(f"Warehouse '{req_wh}' does not exist or is disabled; skipped.")

		# 6. Optional selling_price_list (ONLY if currently blank)
		req_pl = str(payload.get("selling_price_list") or "").strip()
		if req_pl:
			cur_pl = (
				frappe.db.get_single_value("Selling Settings", "selling_price_list") or ""
			).strip()
			if cur_pl:
				unchanged.append(f"Selling Settings.selling_price_list={cur_pl}")
			elif frappe.db.exists("Price List", {"name": req_pl, "selling": 1, "enabled": 1}):
				selling_settings = frappe.get_single("Selling Settings")
				selling_settings.selling_price_list = req_pl
				selling_settings.save(ignore_permissions=True)
				updated.append(f"Selling Settings.selling_price_list={req_pl}")
			else:
				warnings.append(f"Selling Price List '{req_pl}' does not exist or is disabled; skipped.")

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		mops = frappe.get_all(
			"Mode of Payment",
			filters={"enabled": 1},
			fields=["mode_of_payment", "type", "enabled"],
			order_by="mode_of_payment asc",
		)
		billing_codes = [code for code, _name, _uom in CORE_BILLING_ITEMS]
		existing_items = set(
			frappe.get_all("Item", filters={"name": ["in", billing_codes]}, pluck="name")
		)
		return {
			"ensure_hst": True,
			"ensure_square_account": bool(frappe.db.exists("Mode of Payment", "Square")),
			"ensure_billing_items": len(existing_items) == len(billing_codes),
			"modes_of_payment": [
				{
					"mode_of_payment": row.mode_of_payment,
					"type": row.type,
					"enabled": cint(row.enabled),
				}
				for row in mops
			],
		}
