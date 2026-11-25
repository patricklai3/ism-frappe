# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 3 integration tests for all 13 ISM Setup Wizard steps and Staging -> Production blueprints."""

from __future__ import annotations

import json

import frappe
from cryptography.fernet import Fernet

from ism.api import setup_wizard as setup_wizard_api
from ism.setup_wizard import blueprint as wizard_blueprint
from ism.setup_wizard import runner as wizard_runner
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_WARN,
	PROFILE_PRODUCTION,
	PROFILE_STAGING,
	STEP_STATUS_NOT_APPLICABLE,
)
from ism.setup_wizard.presets import CANADIAN_PAYMENT_MODES, STAFF_ROLE_BUNDLES
from ism.tests.base import TEST_PREFIX, ISMTestCase


class TestSetupWizardSteps(ISMTestCase):
	"""Integration tests covering all 13 Setup Wizard steps, gates, verifiers, and blueprint replay."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _ensure_active_technician_employee(self) -> str:
		email = f"{TEST_PREFIX.lower()}sw_tech_{frappe.generate_hash(length=6)}@example.com"
		res = setup_wizard_api.apply_step(
			"users_roles_employees",
			payload={
				"staff": [
					{
						"email": email,
						"first_name": "Setup",
						"last_name": "Technician",
						"bundle": "Floor Technician",
						"create_employee": 1,
					}
				]
			},
			dry_run=0,
		)
		self.assertIn("users_roles_employees", res["step_id"])
		self.track("User", email)
		emp_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
		if emp_name:
			self.track("Employee", emp_name)
		return emp_name

	def test_site_environment_staging_vs_production_guards(self):
		"""customer_journey_demo_mode=1 fails Production readiness; Staging email guard blocks live email."""
		settings = frappe.get_single("ISM Settings")
		settings.customer_journey_demo_mode = 1
		settings.save(ignore_permissions=True)

		step = wizard_runner.get_step("site_environment")
		prod_ctx = wizard_runner.build_context(profile_override=PROFILE_PRODUCTION)
		prod_inspection = step.inspect(prod_ctx)
		prod_check_levels = {c.code: c.level for c in prod_inspection.checks}
		self.assertEqual(prod_check_levels.get("demo_mode"), CHECK_FAIL)

		# Staging profile blocks live test email unless force=True
		staging_ctx = wizard_runner.build_context(profile_override=PROFILE_STAGING)
		verify_res = step.verify(staging_ctx, options={"test_email": "verify@example.com"})
		self.assertFalse(verify_res.passed)
		self.assertTrue(any(c.code == "test_email_guard" and c.level == CHECK_FAIL for c in verify_res.checks))

		# Disabling demo mode and applying Canada / America/Toronto succeeds
		settings.customer_journey_demo_mode = 0
		settings.save(ignore_permissions=True)
		apply_res = setup_wizard_api.apply_step(
			"site_environment",
			payload={
				"environment_profile": PROFILE_STAGING,
				"country": "Canada",
				"time_zone": "America/Toronto",
				"run_bootstrap": 0,
			},
			dry_run=0,
		)
		self.assertEqual(apply_res["profile"], PROFILE_STAGING)

	def test_erpnext_foundation_non_destructive_and_presets(self):
		"""Existing Company accounts/tax templates stay untouched; MOPs & billing items are idempotent."""
		company = wizard_runner.build_context().company
		self.assertTrue(company, "Expected an active Company on test site")
		orig_company_accounts = frappe.db.get_value(
			"Company",
			company,
			["default_bank_account", "default_cash_account", "default_receivable_account"],
			as_dict=True,
		)
		existing_tax_templates = set(frappe.get_all("Sales Taxes and Charges Template", pluck="name"))

		res = setup_wizard_api.apply_step(
			"erpnext_foundation",
			payload={
				"ensure_billing_items": 1,
				"modes_of_payment": CANADIAN_PAYMENT_MODES,
			},
			dry_run=0,
		)
		self.assertEqual(res["step_id"], "erpnext_foundation")

		# Company accounts & existing tax templates are preserved
		after_company_accounts = frappe.db.get_value(
			"Company",
			company,
			["default_bank_account", "default_cash_account", "default_receivable_account"],
			as_dict=True,
		)
		self.assertEqual(orig_company_accounts, after_company_accounts)
		after_tax_templates = set(frappe.get_all("Sales Taxes and Charges Template", pluck="name"))
		self.assertTrue(existing_tax_templates.issubset(after_tax_templates))

		# Canadian MOPs and 4 non-stock billing items exist
		for mop in CANADIAN_PAYMENT_MODES:
			self.assertTrue(frappe.db.exists("Mode of Payment", mop["mode_of_payment"]))

		for item_code in ("LABOR-STD", "SHOP-SUPPLIES", "ENV-DISPOSAL-FEE", "CUST-SUPPLIED-PART"):
			self.assertTrue(frappe.db.exists("Item", item_code), f"Missing billing item {item_code}")
			self.assertEqual(frappe.db.get_value("Item", item_code, "is_stock_item"), 0)

		# Re-running is idempotent
		second_res = setup_wizard_api.apply_step(
			"erpnext_foundation",
			payload={
				"ensure_billing_items": 1,
				"modes_of_payment": CANADIAN_PAYMENT_MODES,
			},
			dry_run=0,
		)
		self.assertEqual(second_res["result"]["created"], [])

	def test_users_roles_employees_additive_bundles_and_tech_isolation(self):
		"""Manager/Advisor bundles are additive; Floor Technician rejects/strips RO-create roles and links Employee."""
		mgr_email = f"{TEST_PREFIX.lower()}sw_mgr_{frappe.generate_hash(length=6)}@example.com"
		pre_existing_role = "Stock User"
		mgr_doc = frappe.get_doc({
			"doctype": "User",
			"email": mgr_email,
			"first_name": "Existing",
			"last_name": "Manager",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": pre_existing_role}],
		})
		mgr_doc.flags.ignore_password_policy = True
		mgr_doc.insert(ignore_permissions=True)
		self.track("User", mgr_email)

		# Additive Service Advisor assignment preserves pre-existing ERPNext role
		setup_wizard_api.apply_step(
			"users_roles_employees",
			payload={
				"seed_role_profiles": 1,
				"staff": [{"email": mgr_email, "bundle": "Service Advisor"}],
			},
			dry_run=0,
		)
		mgr_roles = set(frappe.get_roles(mgr_email))
		self.assertIn(pre_existing_role, mgr_roles)
		for expected_role in STAFF_ROLE_BUNDLES["service_advisor"]["roles"]:
			if frappe.db.exists("Role", expected_role):
				self.assertIn(expected_role, mgr_roles)

		# Role profiles seeded
		for rp_name in ("ISM Manager", "ISM Service Advisor", "ISM Floor Technician"):
			self.assertTrue(frappe.db.exists("Role Profile", rp_name))

		# Floor Technician strict isolation: rejects user with RO-create role unless strip_manager_roles=1
		tech_email = f"{TEST_PREFIX.lower()}sw_iso_{frappe.generate_hash(length=6)}@example.com"
		tech_doc = frappe.get_doc({
			"doctype": "User",
			"email": tech_email,
			"first_name": "Iso",
			"last_name": "Tech",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": "ISM Service Advisor"}],
		})
		tech_doc.flags.ignore_password_policy = True
		tech_doc.insert(ignore_permissions=True)
		self.track("User", tech_email)

		with self.assertRaises(frappe.ValidationError):
			setup_wizard_api.apply_step(
				"users_roles_employees",
				payload={"staff": [{"email": tech_email, "bundle": "Floor Technician"}]},
				dry_run=0,
			)

		# Explicit strip_manager_roles=1 succeeds and creates/links active Employee with Designation
		strip_res = setup_wizard_api.apply_step(
			"users_roles_employees",
			payload={
				"strip_manager_roles": 1,
				"staff": [{"email": tech_email, "bundle": "Floor Technician"}],
			},
			dry_run=0,
		)
		self.assertTrue(strip_res["result"]["warnings"])
		tech_roles = set(frappe.get_roles(tech_email))
		self.assertNotIn("ISM Service Advisor", tech_roles)
		self.assertIn("ISM Technician", tech_roles)

		emp_row = frappe.db.get_value(
			"Employee",
			{"user_id": tech_email},
			["name", "status", "designation"],
			as_dict=True,
		)
		self.assertIsNotNone(emp_row)
		self.track("Employee", emp_row.name)
		self.assertEqual(emp_row.status, "Active")
		tech_desig = frappe.db.get_single_value("ISM Settings", "technician_designation") or "Technician"
		self.assertEqual(emp_row.designation, tech_desig)

	def test_shop_policy_pricing_and_workshop_bays_bins_and_catalog(self):
		"""Policy, bays/bins, catalog presets + CSV/JSON upsert work and workshop_bays_and_bins.verify() passes."""
		self._ensure_active_technician_employee()

		# 1. Shop Policy & Pricing
		policy_res = setup_wizard_api.apply_step(
			"shop_policy_pricing",
			preset="ontario_ev_shop_policy",
			dry_run=0,
		)
		self.assertEqual(policy_res["step_id"], "shop_policy_pricing")
		self.assertEqual(float(frappe.db.get_single_value("ISM Settings", "default_labor_rate")), 150.0)

		# 2. Workshop Bays & Bins
		setup_wizard_api.apply_step(
			"workshop_bays_and_bins",
			preset="starter_bays",
			payload={"settings": {"require_bin_on_receive": 1}},
			dry_run=0,
		)
		setup_wizard_api.apply_step(
			"workshop_bays_and_bins",
			preset="starter_storage_bins",
			dry_run=0,
		)
		self.assertEqual(frappe.db.get_single_value("ISM Settings", "require_bin_on_receive"), 1)
		self.assertGreaterEqual(frappe.db.count("Service Bay", {"is_active": 1}), 1)
		self.assertGreaterEqual(frappe.db.count("Storage Bin", {"is_active": 1}), 1)

		# Verify open scheduling slots via ShopCapacityWindow
		verify_bays = setup_wizard_api.verify_step("workshop_bays_and_bins")
		self.assertTrue(verify_bays["passed"], f"workshop_bays_and_bins verify failed: {verify_bays}")
		self.assertTrue(
			any(c["code"] == "shop_capacity_slot_check" and c["level"] == "pass" for c in verify_bays["result"]["checks"])
		)

		# 3. Catalog, Inspections & Suppliers (preset + CSV/JSON custom records)
		setup_wizard_api.apply_step(
			"catalog_inspections_suppliers",
			preset="ontario_ev_starter_catalog",
			dry_run=0,
		)
		custom_supplier = f"{TEST_PREFIX}CSV_Supplier"
		custom_pkg = f"{TEST_PREFIX}JSON Package"
		csv_suppliers = f"supplier_name,supplier_type\n{custom_supplier},Company"
		json_packages = json.dumps([
			{
				"package_name": custom_pkg,
				"category": "Maintenance",
				"estimated_hours": 1.0,
				"flat_rate_price": 120.0,
				"allow_self_serve_booking": 1,
			}
		])
		setup_wizard_api.apply_step(
			"catalog_inspections_suppliers",
			payload={"suppliers": csv_suppliers, "service_packages": json_packages},
			dry_run=0,
		)
		self.assertTrue(frappe.db.exists("Supplier", custom_supplier))
		self.track("Supplier", custom_supplier)
		self.assertTrue(frappe.db.exists("Service Package", custom_pkg))
		self.track("Service Package", custom_pkg)

	def test_optional_packs_gates_and_mock_verifiers(self):
		"""All 7 optional capability packs: gates, webhooks, mock verifiers, Agent OS handoff, HRMS detection."""
		self._ensure_active_technician_employee()
		setup_wizard_api.apply_step("shop_policy_pricing", preset="ontario_ev_shop_policy", dry_run=0)
		setup_wizard_api.apply_step("workshop_bays_and_bins", preset="starter_bays", dry_run=0)
		setup_wizard_api.apply_step("catalog_inspections_suppliers", preset="starter_service_packages", dry_run=0)

		# 1. comms_telnyx_sms: webhook URL present, Staging warning on Telnyx, Mock verify passes
		sms_schema = setup_wizard_api.get_step_schema("comms_telnyx_sms")["step"]
		self.assertIn("telnyx_webhook", sms_schema.get("webhook_urls", {}))

		setup_wizard_api.apply_step(
			"comms_telnyx_sms",
			payload={"settings": {"sms_gateway_provider": "Telnyx"}},
			secrets={"telnyx_api_key": "KEY_TEST_1234", "portal_session_signing_secret": "SIGN_TEST_1234"},
			dry_run=0,
		)
		sms_staging_status = setup_wizard_api.get_setup_status(profile=PROFILE_STAGING)["steps"]
		sms_entry = next(s for s in sms_staging_status if s["id"] == "comms_telnyx_sms")
		self.assertTrue(any(c["level"] == CHECK_WARN for c in sms_entry["checks"]))

		setup_wizard_api.apply_step("comms_telnyx_sms", preset="mock_staging", dry_run=0)
		sms_verify = setup_wizard_api.verify_step("comms_telnyx_sms")
		self.assertTrue(sms_verify["passed"], f"comms_telnyx_sms verify failed: {sms_verify}")

		# 2. customer_portal: Fernet key + print format verify passes
		fernet_key = Fernet.generate_key().decode()
		setup_wizard_api.apply_step(
			"customer_portal",
			preset="ontario_ev_portal",
			secrets={"portal_pin_encryption_key": fernet_key},
			dry_run=0,
		)
		portal_verify = setup_wizard_api.verify_step("customer_portal")
		self.assertTrue(portal_verify["passed"], f"customer_portal verify failed: {portal_verify}")

		# 3. self_serve_booking: 5-point gate fails when SMS Disabled, passes with Mock SMS + bookable pkg + bay + tech
		settings = frappe.get_single("ISM Settings")
		settings.sms_gateway_provider = "Disabled"
		settings.save(ignore_permissions=True)
		setup_wizard_api.apply_step("self_serve_booking", preset="standard_self_serve", dry_run=0)
		gate_fail = setup_wizard_api.verify_step("self_serve_booking")
		self.assertFalse(gate_fail["passed"])

		setup_wizard_api.apply_step("comms_telnyx_sms", preset="mock_staging", dry_run=0)
		gate_pass = setup_wizard_api.verify_step("self_serve_booking")
		self.assertTrue(gate_pass["passed"], f"self_serve_booking verify failed: {gate_pass}")
		self.assertTrue(
			any(c["code"] == "self_serve_slot_search" and c["level"] == "pass" for c in gate_pass["result"]["checks"])
		)

		# 4. payments_square: webhook URL, CAD Bank account check, Mock verify passes
		sq_schema = setup_wizard_api.get_step_schema("payments_square")["step"]
		self.assertIn("square_webhook", sq_schema.get("webhook_urls", {}))
		setup_wizard_api.apply_step("erpnext_foundation", payload={"ensure_square_account": 1}, dry_run=0)
		setup_wizard_api.apply_step("payments_square", preset="mock_staging", dry_run=0)
		sq_verify = setup_wizard_api.verify_step("payments_square")
		self.assertTrue(sq_verify["passed"], f"payments_square verify failed: {sq_verify}")

		# 5. board_media_and_ai: PWA URLs present, Frappe Private File + Mock AI verify passes
		board_schema = setup_wizard_api.get_step_schema("board_media_and_ai")["step"]
		self.assertIn("shop_board_pwa", board_schema.get("desk_urls", {}))
		setup_wizard_api.apply_step("board_media_and_ai", preset="local_and_mock_ai", dry_run=0)
		board_verify = setup_wizard_api.verify_step("board_media_and_ai")
		self.assertTrue(board_verify["passed"], f"board_media_and_ai verify failed: {board_verify}")

		# 6. agent_os_setup: creates non-Admin service user + one-time credentials in handoff
		agent_email = f"{TEST_PREFIX.lower()}agentos_{frappe.generate_hash(length=6)}@example.com"
		agent_res = setup_wizard_api.apply_step(
			"agent_os_setup",
			payload={"email": agent_email, "generate_api_keys": 1},
			dry_run=0,
		)
		self.track("User", agent_email)
		self.assertNotEqual(agent_email.lower(), "administrator")
		creds = (agent_res.get("handoff") or {}).get("one_time_credentials") or {}
		self.assertEqual(creds.get("user"), agent_email)
		self.assertTrue(creds.get("api_key"))
		self.assertTrue(creds.get("api_secret"))
		agent_verify = setup_wizard_api.verify_step("agent_os_setup", options={"email": agent_email})
		self.assertTrue(agent_verify["passed"], f"agent_os_setup verify failed: {agent_verify}")

		# 7. hrms_workforce_leave: is_applicable matches Leave Application DocType presence
		hrms_step = wizard_runner.get_step("hrms_workforce_leave")
		ctx = wizard_runner.build_context()
		has_hrms = bool(frappe.db.exists("DocType", "Leave Application"))
		self.assertEqual(hrms_step.is_applicable(ctx), has_hrms)
		if has_hrms:
			setup_wizard_api.apply_step("hrms_workforce_leave", preset="ontario_standard_leave", dry_run=0)
			hrms_verify = setup_wizard_api.verify_step("hrms_workforce_leave")
			self.assertTrue(hrms_verify["passed"], f"hrms_workforce_leave verify failed: {hrms_verify}")
		else:
			st = setup_wizard_api.get_setup_status(include_checks=False)["steps"]
			hrms_entry = next(s for s in st if s["id"] == "hrms_workforce_leave")
			self.assertEqual(hrms_entry["status"], STEP_STATUS_NOT_APPLICABLE)

	def test_staging_to_production_blueprint_export_and_replay(self):
		"""Export blueprint with zero secrets, strip customer_journey_demo_mode in Production, and converge idempotently."""
		settings = frappe.get_single("ISM Settings")
		settings.customer_journey_demo_mode = 1
		settings.telnyx_api_key = "SECRET_EXPORT_GUARD_9999"
		settings.save(ignore_permissions=True)

		setup_wizard_api.apply_step("shop_policy_pricing", preset="ontario_ev_shop_policy", dry_run=0)

		bp = setup_wizard_api.export_blueprint(steps=["site_environment", "shop_policy_pricing", "self_serve_booking"])
		bp_json = json.dumps(bp)
		self.assertNotIn("SECRET_EXPORT_GUARD_9999", bp_json)
		self.assertEqual(bp["blueprint_version"], wizard_blueprint.BLUEPRINT_VERSION)

		# Ensure blueprint carries customer_journey_demo_mode=1 before Production replay
		self.assertEqual(
			bp["steps"]["self_serve_booking"]["data"]["settings"].get("customer_journey_demo_mode"),
			1,
		)

		# Dry-run replay targeting Production strips staging-only flag and warns
		dry_replay = setup_wizard_api.apply_blueprint(
			blueprint=bp,
			target_profile=PROFILE_PRODUCTION,
			dry_run=1,
		)
		self.assertTrue(dry_replay["dry_run"])
		self.assertEqual(dry_replay["target_profile"], PROFILE_PRODUCTION)
		self.assertTrue(any("customer_journey_demo_mode" in w for w in dry_replay["warnings"]))
		# Dry-run did not persist demo mode change
		self.assertEqual(frappe.db.get_single_value("ISM Settings", "customer_journey_demo_mode"), 1)

		# Live replay targeting Production strips demo mode; set to 0 explicitly first or verify stripped from replay
		settings.customer_journey_demo_mode = 0
		settings.save(ignore_permissions=True)
		live_replay = setup_wizard_api.apply_blueprint(
			blueprint=bp,
			target_profile=PROFILE_PRODUCTION,
			dry_run=0,
		)
		self.assertFalse(live_replay["dry_run"])
		self.assertEqual(frappe.db.get_single_value("ISM Settings", "customer_journey_demo_mode"), 0)

		# Second replay converges idempotently
		second_replay = setup_wizard_api.apply_blueprint(
			blueprint=bp,
			target_profile=PROFILE_PRODUCTION,
			dry_run=0,
		)
		self.assertEqual(second_replay["target_profile"], PROFILE_PRODUCTION)
		self.assertEqual(frappe.db.get_single_value("ISM Settings", "customer_journey_demo_mode"), 0)
