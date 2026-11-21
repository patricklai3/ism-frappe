# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

import secrets

import frappe

from ism.ism.doctype.schedule_entry_type.schedule_entry_type import STANDARD_SCHEDULE_ENTRY_TYPES

STANDARD_INSPECTION_ITEMS = [
	{"inspection_description": "Manufacturing Certification Label"},
	{"inspection_description": "Front Left Corner"},
	{"inspection_description": "Front Right Corner"},
	{"inspection_description": "Rear Left Corner"},
	{"inspection_description": "Rear Right Corner"},
	{"inspection_description": "Interior (Front Seats)"},
	{"inspection_description": "Interior (Rear Seats)"},
	{"inspection_description": "Dashboard Mileage"},
	{"inspection_description": "Service Mode Alert Page"},
]

DEFAULT_EQUIPMENT_TAGS = [
	{"tag_name": "Lift", "description": "Standard automotive lift (2-post / 4-post)"},
	{"tag_name": "EV High Voltage", "description": "High voltage isolation and battery service equipment"},
	{"tag_name": "Alignment Rack", "description": "3D optical wheel alignment rack and sensor system"},
]

_SCHEDULE_ENTRY_TYPE_METADATA = {
	"Diagnostic": {
		"color": "#1f538d",
		"requires_repair_order": 0,
		"description": "Initial vehicle inspection and diagnostic appointment",
	},
	"Repair": {
		"color": "#2e7d32",
		"requires_repair_order": 1,
		"description": "Authorized vehicle repair service",
	},
	"Maintenance": {
		"color": "#0284c7",
		"requires_repair_order": 1,
		"description": "Scheduled maintenance and inspection service",
	},
	"Meeting": {
		"color": "#7b1fa2",
		"requires_repair_order": 0,
		"description": "Internal staff or shop meeting",
	},
	"Internal Service": {
		"color": "#ef6c00",
		"requires_repair_order": 0,
		"description": "Internal shop vehicle service or bay maintenance",
	},
}

_ISM_SETTINGS_FALSY_DEFAULTS = (
	("authorization_token_expiry_hours", 48),
	("cost_tolerance_percentage", 10.0),
	("cost_tolerance_flat_amount", 20.0),
	(
		"disclaimer_text",
		(
			"I hereby authorize the repair work listed above to be done along with the necessary materials. "
			"You and your employees may operate the vehicle for purpose of testing, inspection, or delivery."
		),
	),
	("operating_hours_start", "08:00:00"),
	("operating_hours_end", "17:00:00"),
	("break_start", "12:00:00"),
	("break_end", "12:30:00"),
	("default_slot_interval", 30),
	("scheduling_horizon_days", 30),
	("technician_designation", "Technician"),
	("estimation_percentile", 80.0),
	("estimation_sigma", 0.30),
	("default_diagnostic_duration", 60),
	("default_labor_rate", 140.0),
	("sms_default_country_code", "+1"),
	("telnyx_webhook_tolerance_seconds", 300),
	("sms_help_reply_text", "Reply STOP to unsubscribe from SMS alerts, or call the shop for assistance."),
	("otp_code_length", 6),
	("otp_ttl_seconds", 300),
	("otp_resend_cooldown_seconds", 60),
	("otp_max_verify_attempts", 5),
	("otp_lockout_seconds", 900),
	("otp_phone_rate_limit_per_hour", 5),
	("otp_ip_rate_limit_per_hour", 20),
	("portal_session_cookie_name", "ism_portal_session"),
	("portal_session_ttl_days", 30),
	("portal_session_cookie_samesite", "Lax"),
	("transactional_email_rate_limit_per_hour", 5),
	("transactional_email_ip_rate_limit_per_hour", 20),
	("square_environment", "Disabled"),
	("square_currency", "CAD"),
)

_ISM_SETTINGS_NONE_DEFAULTS = (
	("enable_technician_capacity", 1),
	("buffer_padding_minutes", 0),
	("sms_enforce_opt_out", 1),
	("portal_session_secure_cookie", 1),
)


def create_standard_inspection_template():
	"""Seed the default Standard Vehicle Walkaround inspection template."""
	template_name = "Standard Vehicle Walkaround"
	if not frappe.db.exists("Inspection Template", template_name):
		doc = frappe.get_doc({
			"doctype": "Inspection Template",
			"template_name": template_name,
			"description": "Standard 9-point intake walkaround inspection.",
			"items": STANDARD_INSPECTION_ITEMS,
		})
		doc.insert(ignore_permissions=True)


def setup_default_ism_settings():
	"""Initialize default ISM Settings using declarative default tables if not populated."""
	settings = frappe.get_single("ISM Settings")
	changed = False

	for fieldname, default_val in _ISM_SETTINGS_FALSY_DEFAULTS:
		if not settings.get(fieldname):
			settings.set(fieldname, default_val)
			changed = True

	persisted_fields = set(
		frappe.db.sql_list("SELECT field FROM `tabSingles` WHERE doctype = %s", ("ISM Settings",))
	)
	for fieldname, default_val in _ISM_SETTINGS_NONE_DEFAULTS:
		if settings.get(fieldname) is None or fieldname not in persisted_fields:
			settings.set(fieldname, default_val)
			changed = True

	if not settings.sms_gateway_provider or settings.sms_gateway_provider == "Twilio":
		settings.sms_gateway_provider = "Disabled"
		changed = True

	if not settings.portal_session_signing_secret and not settings.get_password(
		"portal_session_signing_secret", raise_exception=False
	):
		settings.portal_session_signing_secret = secrets.token_urlsafe(48)
		changed = True

	if changed or settings.is_new():
		settings.save(ignore_permissions=True)

	ensure_square_bank_account_and_mop()


def _find_company_bank_group_account(company_name: str, abbr: str | None) -> str | None:
	if abbr:
		candidate = f"Bank Accounts - {abbr}"
		if frappe.db.get_value("Account", candidate, "is_group") == 1:
			return candidate

	parent = frappe.db.get_value(
		"Account",
		{"company": company_name, "account_name": "Bank Accounts", "is_group": 1},
		"name",
	)
	if parent:
		return parent

	return frappe.db.get_value(
		"Account",
		{"company": company_name, "is_group": 1, "account_type": "Bank"},
		"name",
	)


def ensure_square_bank_account_and_mop() -> dict[str, str]:
	"""Idempotently seed per-company CAD Square Bank Accounts, Square Mode of Payment, and blank ISM Settings links.

	Never overwrites operator-customized Mode of Payment Account rows or non-blank ISM Settings fields.
	"""
	if not frappe.db.exists("DocType", "Company") or not frappe.db.exists("DocType", "Account"):
		return {}

	company_filters = {"is_group": 0} if frappe.db.has_column("Company", "is_group") else {}
	companies = frappe.get_all(
		"Company",
		filters=company_filters,
		fields=["name", "abbr"],
		order_by="creation asc",
	)

	company_square_accounts: dict[str, str] = {}
	for comp in companies:
		comp_name = comp.name
		abbr = comp.get("abbr")
		existing_acc = None
		if abbr:
			expected_name = f"Square - {abbr}"
			if frappe.db.exists("Account", expected_name):
				existing_acc = expected_name
		if not existing_acc:
			existing_acc = frappe.db.get_value(
				"Account",
				{"company": comp_name, "account_name": "Square", "is_group": 0},
				"name",
			)

		if existing_acc:
			company_square_accounts[comp_name] = existing_acc
			continue

		parent_account = _find_company_bank_group_account(comp_name, abbr)
		if not parent_account:
			frappe.logger("ism").warning(
				f"Could not locate Bank Accounts group for Company '{comp_name}'; skipping Square Bank Account seed."
			)
			continue

		try:
			acc_doc = frappe.get_doc({
				"doctype": "Account",
				"account_name": "Square",
				"parent_account": parent_account,
				"company": comp_name,
				"account_type": "Bank",
				"account_currency": "CAD",
				"is_group": 0,
			})
			acc_doc.insert(ignore_permissions=True)
			company_square_accounts[comp_name] = acc_doc.name
		except Exception as exc:
			frappe.logger("ism").warning(f"Could not seed Square Bank Account for {comp_name}: {exc}")

	if frappe.db.exists("DocType", "Mode of Payment"):
		if not frappe.db.exists("Mode of Payment", "Square"):
			mop_doc = frappe.get_doc({
				"doctype": "Mode of Payment",
				"mode_of_payment": "Square",
				"type": "Bank",
				"enabled": 1,
			})
			mop_doc.insert(ignore_permissions=True)

		if frappe.db.exists("Mode of Payment", "Square") and company_square_accounts:
			mop_doc = frappe.get_doc("Mode of Payment", "Square")
			existing_companies = {row.company for row in (mop_doc.accounts or [])}
			mop_changed = False
			for comp_name, acc_name in company_square_accounts.items():
				if comp_name not in existing_companies:
					mop_doc.append("accounts", {"company": comp_name, "default_account": acc_name})
					mop_changed = True
			if mop_changed:
				mop_doc.save(ignore_permissions=True)

	if frappe.db.exists("DocType", "ISM Settings"):
		settings = frappe.get_single("ISM Settings")
		settings_changed = False
		if not settings.square_mode_of_payment and frappe.db.exists("Mode of Payment", "Square"):
			settings.square_mode_of_payment = "Square"
			settings_changed = True

		if not settings.square_bank_account and company_square_accounts:
			default_company = frappe.defaults.get_global_default("company")
			default_acc = (
				company_square_accounts.get(default_company)
				or next(iter(company_square_accounts.values()), None)
			)
			if default_acc:
				settings.square_bank_account = default_acc
				settings_changed = True

		if settings_changed:
			settings.save(ignore_permissions=True)

	return company_square_accounts


def setup_parts_item_group():
	"""Ensure 'Parts' Item Group exists under 'All Item Groups'."""
	if not frappe.db.exists("Item Group", "Parts"):
		try:
			doc = frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": "Parts",
				"parent_item_group": "All Item Groups" if frappe.db.exists("Item Group", "All Item Groups") else "",
				"is_group": 0,
			})
			doc.insert(ignore_permissions=True)
		except Exception as exc:
			frappe.logger("ism").warning(f"Could not seed 'Parts' Item Group: {exc}")


def seed_equipment_tags():
	"""Idempotently seed standard default Equipment Tag records."""
	if not frappe.db.exists("DocType", "Equipment Tag"):
		return
	existing = set(frappe.get_all("Equipment Tag", pluck="name"))
	for tag in DEFAULT_EQUIPMENT_TAGS:
		if tag["tag_name"] not in existing:
			doc = frappe.get_doc({
				"doctype": "Equipment Tag",
				"tag_name": tag["tag_name"],
				"description": tag["description"],
			})
			doc.insert(ignore_permissions=True)


def ensure_ism_roles():
	"""Ensure standard ISM roles exist with appropriate Desk access."""
	roles = [
		{"role_name": "ISM Manager", "desk_access": 1},
		{"role_name": "ISM Service Advisor", "desk_access": 1},
		{"role_name": "ISM Technician", "desk_access": 1},
	]
	for r in roles:
		if not frappe.db.exists("Role", r["role_name"]):
			try:
				doc = frappe.get_doc({
					"doctype": "Role",
					"role_name": r["role_name"],
					"desk_access": r["desk_access"],
				})
				doc.insert(ignore_permissions=True)
			except Exception as exc:
				frappe.logger("ism").warning(f"Could not seed role {r['role_name']}: {exc}")


def seed_schedule_entry_types():
	"""Seed standard Schedule Entry Types idempotently from canonical STANDARD_SCHEDULE_ENTRY_TYPES."""
	if not frappe.db.exists("DocType", "Schedule Entry Type"):
		return

	existing_rows = {
		row.name: row
		for row in frappe.get_all(
			"Schedule Entry Type",
			fields=["name", "requires_repair_order", "color", "description"],
		)
	}

	for type_name in sorted(STANDARD_SCHEDULE_ENTRY_TYPES):
		spec = _SCHEDULE_ENTRY_TYPE_METADATA.get(
			type_name,
			{"color": "#4b5563", "requires_repair_order": 0, "description": type_name},
		)
		row = existing_rows.get(type_name)
		if not row:
			doc = frappe.get_doc({
				"doctype": "Schedule Entry Type",
				"type_name": type_name,
				"requires_repair_order": spec["requires_repair_order"],
				"color": spec["color"],
				"description": spec["description"],
			})
			doc.insert(ignore_permissions=True)
			continue

		needs_update = (
			int(row.get("requires_repair_order") or 0) != spec["requires_repair_order"]
			or (row.get("color") or "") != spec["color"]
			or (row.get("description") or "") != spec["description"]
		)
		if needs_update:
			existing = frappe.get_doc("Schedule Entry Type", type_name)
			existing.requires_repair_order = spec["requires_repair_order"]
			existing.color = spec["color"]
			existing.description = spec["description"]
			existing.save(ignore_permissions=True)
