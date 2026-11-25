# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Wave 3 integration tests for the ISM Setup Wizard Framework engine, DAG, RBAC, and secret guards."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import frappe

from ism.api import setup_wizard as setup_wizard_api
from ism.setup_wizard import blueprint as wizard_blueprint
from ism.setup_wizard import runner as wizard_runner
from ism.setup_wizard.base import BaseSetupStep
from ism.setup_wizard.contracts import (
	PROFILE_PRODUCTION,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_SKIPPED,
	TIER_OPTIONAL_PACK,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
)
from ism.setup_wizard.registry import (
	SetupRegistry,
	clear_registry_cache,
	get_claimed_ism_settings_sections,
	get_ordered_steps,
)
from ism.tests.base import TEST_PREFIX, ISMTestCase


class _CycleStepA(BaseSetupStep):
	meta = StepMeta(
		id="test_cycle_a",
		title="Cycle A",
		phase="test",
		order=10,
		depends_on=("test_cycle_b",),
	)

	def inspect(self, ctx: SetupContext) -> StepInspection:
		return StepInspection(status=STEP_STATUS_COMPLETE, summary="ok")

	def apply(self, payload: dict, ctx: SetupContext, *, dry_run: bool = False) -> StepApplyResult:
		return StepApplyResult(step_id=self.meta.id, dry_run=dry_run, applied=False)


class _CycleStepB(BaseSetupStep):
	meta = StepMeta(
		id="test_cycle_b",
		title="Cycle B",
		phase="test",
		order=20,
		depends_on=("test_cycle_a",),
	)

	def inspect(self, ctx: SetupContext) -> StepInspection:
		return StepInspection(status=STEP_STATUS_COMPLETE, summary="ok")

	def apply(self, payload: dict, ctx: SetupContext, *, dry_run: bool = False) -> StepApplyResult:
		return StepApplyResult(step_id=self.meta.id, dry_run=dry_run, applied=False)


class _SelfCycleStep(BaseSetupStep):
	meta = StepMeta(
		id="test_self_cycle",
		title="Self Cycle",
		phase="test",
		order=10,
		depends_on=("test_self_cycle",),
	)

	def inspect(self, ctx: SetupContext) -> StepInspection:
		return StepInspection(status=STEP_STATUS_COMPLETE, summary="ok")

	def apply(self, payload: dict, ctx: SetupContext, *, dry_run: bool = False) -> StepApplyResult:
		return StepApplyResult(step_id=self.meta.id, dry_run=dry_run, applied=False)


class _CustomHookStep(BaseSetupStep):
	meta = StepMeta(
		id="test_custom_hook_step",
		title="Custom Hook Step",
		phase="custom",
		order=999,
		tier=TIER_OPTIONAL_PACK,
		depends_on=("site_environment",),
		allow_skip=True,
	)

	def inspect(self, ctx: SetupContext) -> StepInspection:
		return StepInspection(status=STEP_STATUS_COMPLETE, summary="custom ok")

	def apply(self, payload: dict, ctx: SetupContext, *, dry_run: bool = False) -> StepApplyResult:
		return StepApplyResult(step_id=self.meta.id, dry_run=dry_run, applied=False)


class TestSetupWizardEngine(ISMTestCase):
	"""Integration tests for Setup Wizard registry, runner, state, RBAC, and secret redaction."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		clear_registry_cache()

	def tearDown(self):
		frappe.set_user("Administrator")
		clear_registry_cache()
		super().tearDown()

	def _create_role_user(self, suffix: str, role: str) -> str:
		email = f"{TEST_PREFIX.lower()}sw_{suffix}_{frappe.generate_hash(length=6)}@example.com"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		user_doc = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "SetupTest",
			"last_name": suffix,
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": role}],
		})
		user_doc.flags.ignore_password_policy = True
		user_doc.insert(ignore_permissions=True)
		self.track("User", user_doc.name)
		return user_doc.name

	def test_setup_wizard_coverage(self):
		"""CI drift guard: 100% of ISM Settings Section Breaks must be claimed with zero duplicates."""
		ism_settings_json = (
			Path(frappe.get_app_path("ism"))
			/ "ism"
			/ "doctype"
			/ "ism_settings"
			/ "ism_settings.json"
		)
		data = json.loads(ism_settings_json.read_text(encoding="utf-8"))
		all_sections = [
			str(f["fieldname"])
			for f in (data.get("fields") or [])
			if f.get("fieldtype") == "Section Break" and f.get("fieldname")
		]
		self.assertEqual(len(all_sections), 14, f"Expected 14 ISM Settings Section Breaks, got {all_sections}")
		self.assertEqual(len(set(all_sections)), 14, "Duplicate Section Break fieldnames in ism_settings.json")

		claimed = get_claimed_ism_settings_sections()
		unregistered = set(all_sections) - set(claimed.keys())
		unknown = set(claimed.keys()) - set(all_sections)
		self.assertEqual(unregistered, set(), f"Unregistered ISM Settings sections: {sorted(unregistered)}")
		self.assertEqual(unknown, set(), f"Claimed sections not in ism_settings.json: {sorted(unknown)}")

		# Also verify zero duplicate ownership across step metadata directly
		seen_sections: dict[str, str] = {}
		for step in get_ordered_steps():
			for sec in step.meta.managed_settings_sections:
				self.assertNotIn(
					sec,
					seen_sections,
					f"Duplicate ownership for section '{sec}': {seen_sections.get(sec)} vs {step.meta.id}",
				)
				seen_sections[sec] = step.meta.id

	def test_registry_discovers_all_13_steps_in_valid_dag_order(self):
		"""All 13 built-in steps are discovered in valid topological order respecting depends_on."""
		expected_ids = {
			"site_environment",
			"erpnext_foundation",
			"users_roles_employees",
			"shop_policy_pricing",
			"workshop_bays_and_bins",
			"catalog_inspections_suppliers",
			"comms_telnyx_sms",
			"customer_portal",
			"self_serve_booking",
			"payments_square",
			"board_media_and_ai",
			"agent_os_setup",
			"hrms_workforce_leave",
		}
		steps = get_ordered_steps()
		ordered_ids = [step.meta.id for step in steps]
		self.assertEqual(len(ordered_ids), 13)
		self.assertEqual(set(ordered_ids), expected_ids)

		position = {sid: idx for idx, sid in enumerate(ordered_ids)}
		for step in steps:
			for dep_id in step.meta.depends_on:
				self.assertIn(dep_id, position, f"{step.meta.id} depends on missing step {dep_id}")
				self.assertLess(
					position[dep_id],
					position[step.meta.id],
					f"Dependency {dep_id} must appear before {step.meta.id} in DAG order",
				)

	def test_registry_cycle_detection_and_hook_extension(self):
		"""Cycle detection raises ValueError and ism_setup_wizard_steps hook extends discovery."""
		with self.assertRaises(ValueError):
			SetupRegistry(steps=[_CycleStepA(), _CycleStepB()])

		with self.assertRaises(ValueError):
			SetupRegistry(steps=[_SelfCycleStep()])

		hook_path = f"{_CustomHookStep.__module__}.{_CustomHookStep.__name__}"
		orig_get_hooks = frappe.get_hooks

		def _fake_get_hooks(hook=None, *args, **kwargs):
			if hook == "ism_setup_wizard_steps":
				return [hook_path]
			return orig_get_hooks(hook, *args, **kwargs)

		with patch("ism.setup_wizard.registry.frappe.get_hooks", side_effect=_fake_get_hooks):
			clear_registry_cache()
			registry = SetupRegistry.discover()
			ids = [s.meta.id for s in registry.get_ordered_steps()]
			self.assertIn("test_custom_hook_step", ids)
			self.assertLess(ids.index("site_environment"), ids.index("test_custom_hook_step"))

	def test_upgrade_and_schema_version_drift_detection(self):
		"""Older schema_version or missing step rows in ISM Setup State trigger upgrade_pending=True."""
		state_doc = frappe.get_single("ISM Setup State")
		state_doc.set("step_states", [])
		state_doc.upsert_step_state(
			"site_environment",
			schema_version=0,
			status="Configured",
		)
		state_doc.save(ignore_permissions=True)

		status = setup_wizard_api.get_setup_status(include_checks=False)
		by_id = {entry["id"]: entry for entry in status["steps"]}

		# site_environment has schema_version=0 < 1 -> upgrade_pending=True
		self.assertTrue(by_id["site_environment"]["upgrade_pending"])
		self.assertIn("site_environment", status["new_or_updated_steps"])

		# erpnext_foundation is missing from step_states -> upgrade_pending=True
		self.assertTrue(by_id["erpnext_foundation"]["upgrade_pending"])
		self.assertIn("erpnext_foundation", status["new_or_updated_steps"])
		self.assertGreaterEqual(status["summary"]["upgrade_pending_count"], 2)

		# Applying site_environment updates its recorded schema_version to current -> upgrade_pending=False
		setup_wizard_api.apply_step(
			"site_environment",
			payload={"country": "Canada", "time_zone": "America/Toronto", "run_bootstrap": 0},
			dry_run=0,
		)
		after_status = setup_wizard_api.get_setup_status(include_checks=False)
		after_by_id = {entry["id"]: entry for entry in after_status["steps"]}
		self.assertFalse(after_by_id["site_environment"]["upgrade_pending"])
		self.assertNotIn("site_environment", after_status["new_or_updated_steps"])

	def test_dry_run_savepoint_rollback_and_exception_rollback(self):
		"""dry_run=True rolls back via savepoint with accurate diff; exceptions also roll back cleanly."""
		bay_name = f"{TEST_PREFIX}DryRunBay"
		self.assertFalse(frappe.db.exists("Service Bay", bay_name))

		orig_rate = frappe.db.get_single_value("ISM Settings", "default_labor_rate")
		target_rate = 247.5 if float(orig_rate or 0) != 247.5 else 258.5

		dry_res = setup_wizard_api.apply_step(
			"shop_policy_pricing",
			payload={"settings": {"default_labor_rate": target_rate}},
			dry_run=1,
		)
		self.assertTrue(dry_res["dry_run"])
		self.assertTrue(dry_res["result"]["applied"])
		self.assertIn("default_labor_rate", dry_res["result"]["updated"])
		self.assertEqual(frappe.db.get_single_value("ISM Settings", "default_labor_rate"), orig_rate)

		bay_dry_res = setup_wizard_api.apply_step(
			"workshop_bays_and_bins",
			payload={"service_bays": [{"bay_name": bay_name, "is_active": 1}]},
			dry_run=1,
		)
		self.assertTrue(bay_dry_res["dry_run"])
		self.assertIn(f"Service Bay: {bay_name}", bay_dry_res["result"]["created"])
		self.assertFalse(frappe.db.exists("Service Bay", bay_name))

		# Mid-step exception must roll back DB mutations made before the exception
		tag_name = f"{TEST_PREFIX}RollbackTag"
		step = wizard_runner.get_step("workshop_bays_and_bins")

		def _failing_apply(payload, ctx, *, dry_run=False):
			frappe.get_doc({"doctype": "Equipment Tag", "tag_name": tag_name}).insert(
				ignore_permissions=True
			)
			raise RuntimeError("Simulated mid-step failure")

		with patch.object(step, "apply", side_effect=_failing_apply):
			with self.assertRaises(RuntimeError):
				wizard_runner.apply_step("workshop_bays_and_bins", payload={}, dry_run=False)

		self.assertFalse(frappe.db.exists("Equipment Tag", tag_name))

	def test_skip_step_blocks_required_core_in_production_and_allows_optional(self):
		"""Core steps with allow_skip=False cannot be skipped in Production; optional packs can be skipped/unskipped."""
		state_doc = frappe.get_single("ISM Setup State")
		state_doc.environment_profile = PROFILE_PRODUCTION
		state_doc.save(ignore_permissions=True)

		# Required core step cannot be skipped in Production
		with self.assertRaises(frappe.ValidationError):
			setup_wizard_api.skip_step("site_environment", reason="trying to skip core")

		# Ensure comms_telnyx_sms is Disabled so live_status is incomplete and skip takes effect
		settings = frappe.get_single("ISM Settings")
		settings.sms_gateway_provider = "Disabled"
		settings.save(ignore_permissions=True)

		skip_res = setup_wizard_api.skip_step("comms_telnyx_sms", reason="deferring SMS setup")
		self.assertTrue(skip_res["skipped"])
		self.assertEqual(skip_res["reason"], "deferring SMS setup")

		# Mock site_environment complete so Production dependency blocking does not mask 'skipped' status
		site_step = wizard_runner.get_step("site_environment")
		with patch.object(
			site_step,
			"inspect",
			return_value=StepInspection(status=STEP_STATUS_COMPLETE, summary="mock prod ok"),
		):
			status = setup_wizard_api.get_setup_status(profile=PROFILE_PRODUCTION, include_checks=False)
			by_id = {entry["id"]: entry for entry in status["steps"]}
			self.assertEqual(by_id["comms_telnyx_sms"]["status"], STEP_STATUS_SKIPPED)

			unskip_res = setup_wizard_api.skip_step("comms_telnyx_sms", unskip=1)
			self.assertFalse(unskip_res["skipped"])
			status_after = setup_wizard_api.get_setup_status(profile=PROFILE_PRODUCTION, include_checks=False)
			by_id_after = {entry["id"]: entry for entry in status_after["steps"]}
			self.assertNotEqual(by_id_after["comms_telnyx_sms"]["status"], STEP_STATUS_SKIPPED)

	def test_strict_rbac_and_zero_secret_leakage(self):
		"""Only Administrator, System Manager, and ISM Manager may call Setup Wizard APIs; zero secrets leak."""
		tech_user = self._create_role_user("tech", "ISM Technician")
		advisor_user = self._create_role_user("advisor", "ISM Service Advisor")
		mgr_user = self._create_role_user("mgr", "ISM Manager")

		blueprint_stub = {"blueprint_version": 1, "steps": {}}

		def _call_all_7_endpoints():
			return [
				lambda: setup_wizard_api.get_setup_status(),
				lambda: setup_wizard_api.get_step_schema(step_id="site_environment"),
				lambda: setup_wizard_api.apply_step("site_environment", payload={}, dry_run=1),
				lambda: setup_wizard_api.skip_step("comms_telnyx_sms", reason="rbac"),
				lambda: setup_wizard_api.verify_step(step_id="site_environment"),
				lambda: setup_wizard_api.export_blueprint(steps=["site_environment"]),
				lambda: setup_wizard_api.apply_blueprint(blueprint=blueprint_stub, dry_run=1),
			]

		for unauthorized_user in ("Guest", tech_user, advisor_user):
			frappe.set_user(unauthorized_user)
			for fn in _call_all_7_endpoints():
				with self.assertRaises(
					frappe.PermissionError,
					msg=f"Expected PermissionError for user {unauthorized_user}",
				):
					fn()

		# ISM Manager and Administrator must succeed
		for authorized_user in (mgr_user, "Administrator"):
			frappe.set_user(authorized_user)
			status = setup_wizard_api.get_setup_status(include_checks=False)
			self.assertIn("steps", status)
			schema = setup_wizard_api.get_step_schema(step_id="site_environment")
			self.assertEqual(schema["step"]["step_id"], "site_environment")

		# Secret zero-leakage test across schema, status, apply_step, and export_blueprint
		frappe.set_user("Administrator")
		secrets_map = {
			"telnyx_api_key": "SECRET_TELNYX_KEY_9911_DO_NOT_LEAK",
			"square_access_token": "SECRET_SQUARE_TOK_8822_DO_NOT_LEAK",
			"board_ai_api_key": "SECRET_BOARDAI_KEY_7733_DO_NOT_LEAK",
			"gdrive_refresh_token": "SECRET_GDRIVE_REF_6644_DO_NOT_LEAK",
		}
		settings = frappe.get_single("ISM Settings")
		for fieldname, secret_val in secrets_map.items():
			settings.set(fieldname, secret_val)
		settings.save(ignore_permissions=True)

		schema_all = setup_wizard_api.get_step_schema()
		status_all = setup_wizard_api.get_setup_status(include_checks=True)
		exported = wizard_blueprint.export_blueprint()

		inline_secret = "SECRET_INLINE_APPLY_5500_DO_NOT_LEAK"
		apply_res = setup_wizard_api.apply_step(
			"comms_telnyx_sms",
			payload={"settings": {"sms_gateway_provider": "Telnyx"}},
			secrets={"telnyx_api_key": inline_secret},
			dry_run=1,
		)

		serialized_outputs = [
			json.dumps(schema_all),
			json.dumps(status_all),
			json.dumps(exported),
			json.dumps(apply_res),
		]
		for secret_val in (*secrets_map.values(), inline_secret):
			for blob in serialized_outputs:
				self.assertNotIn(secret_val, blob)

		# Verify is_set is True for the 4 configured secret fields in get_step_schema
		fields_by_name = {
			f["fieldname"]: f
			for step_schema in schema_all["steps"]
			for f in step_schema.get("fields", [])
			if isinstance(f, dict) and f.get("fieldname")
		}
		for fieldname in secrets_map:
			self.assertTrue(
				fields_by_name[fieldname]["is_set"],
				f"Expected is_set=True for secret field '{fieldname}'",
			)
			self.assertNotIn("value", fields_by_name[fieldname])
