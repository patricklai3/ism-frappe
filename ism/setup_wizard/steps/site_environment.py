# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Step 10: Site environment profile, System Settings, outgoing email, and Tier-0 ISM bootstrap audit."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import frappe
from frappe.utils import cint

from ism.ism.doctype.schedule_entry_type.schedule_entry_type import STANDARD_SCHEDULE_ENTRY_TYPES
from ism.setup_tasks.inventory_dimensions import BIN_DIMENSION_NAME, BIN_REFERENCE_DOCTYPE
from ism.setup_tasks.seed import _ISM_STANDARD_ROLES
from ism.setup_wizard.base import BaseSetupStep, build_desk_link
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_INFO,
	CHECK_PASS,
	CHECK_WARN,
	PROFILE_PRODUCTION,
	PROFILE_STAGING,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	STEP_STATUS_WARNING,
	TIER_CORE,
	VALID_PROFILES,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
	StepVerifyResult,
	normalize_profile,
)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def _is_local_or_insecure_url(url: str) -> tuple[bool, str]:
	cleaned = (url or "").strip()
	if not cleaned:
		return True, "Site URL is empty."
	parsed = urlparse(cleaned)
	host = (parsed.hostname or "").lower()
	if not host or host in _LOCAL_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
		return True, f"Site URL '{cleaned}' resolves to a local host."
	if parsed.scheme.lower() != "https":
		return True, f"Site URL '{cleaned}' is not using HTTPS."
	return False, ""


def _has_default_bin_location_field() -> bool:
	try:
		if frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "default_bin_location"}):
			return True
		return bool(frappe.get_meta("Item").has_field("default_bin_location"))
	except Exception:
		return False


def _has_bin_location_dimension() -> bool:
	if not frappe.db.exists("DocType", "Inventory Dimension"):
		return False
	return bool(
		frappe.db.exists("Inventory Dimension", {"dimension_name": BIN_DIMENSION_NAME})
		or frappe.db.exists("Inventory Dimension", {"reference_document": BIN_REFERENCE_DOCTYPE})
	)


class SiteEnvironmentStep(BaseSetupStep):
	meta = StepMeta(
		id="site_environment",
		title="Site & Environment Readiness",
		phase="environment",
		order=10,
		schema_version=1,
		tier=TIER_CORE,
		depends_on=(),
		owner_domain="ism",
		managed_settings_sections=(),
		allow_skip=False,
		description=(
			"Audit site URL, environment profile (Staging vs. Production), System Settings "
			"(country & time zone), outgoing email, scheduler safety flags, and Tier-0 ISM bootstrap."
		),
	)

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "ontario_staging",
				"label": "Ontario Staging Site",
				"description": "Configure Canada / America/Toronto timezone in Staging profile and run Tier-0 bootstrap.",
				"payload": {
					"environment_profile": PROFILE_STAGING,
					"country": "Canada",
					"time_zone": "America/Toronto",
					"run_bootstrap": True,
				},
			},
			{
				"id": "ontario_production",
				"label": "Ontario Production Site",
				"description": "Configure Canada / America/Toronto timezone in Production profile and run Tier-0 bootstrap.",
				"payload": {
					"environment_profile": PROFILE_PRODUCTION,
					"country": "Canada",
					"time_zone": "America/Toronto",
					"run_bootstrap": True,
				},
			},
		]

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		sys_settings = frappe.get_single("System Settings")
		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"fields": [
				{
					"fieldname": "environment_profile",
					"label": "Environment Profile",
					"fieldtype": "Select",
					"options": "\n".join(VALID_PROFILES),
					"default": PROFILE_STAGING,
					"reqd": True,
					"read_only": False,
					"is_secret": False,
					"value": ctx.profile,
					"description": "Select 'Staging' for pre-live sandboxes or 'Production' for live customer shops.",
				},
				{
					"fieldname": "country",
					"label": "Country",
					"fieldtype": "Link",
					"options": "Country",
					"default": "Canada",
					"reqd": True,
					"read_only": False,
					"is_secret": False,
					"value": sys_settings.get("country"),
				},
				{
					"fieldname": "time_zone",
					"label": "Time Zone",
					"fieldtype": "Select",
					"options": "America/Toronto",
					"default": "America/Toronto",
					"reqd": True,
					"read_only": False,
					"is_secret": False,
					"value": sys_settings.get("time_zone"),
				},
				{
					"fieldname": "run_bootstrap",
					"label": "Run Tier-0 ISM Bootstrap",
					"fieldtype": "Check",
					"options": None,
					"default": 0,
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"value": 0,
					"description": "Execute idempotent ism.setup._run_setup_tasks(full=False) to seed roles, fields, inventory dimensions, and schedule entry types.",
				},
			],
			"secret_fields": [],
			"presets": self.get_presets(ctx),
			"desk_urls": self._desk_urls(ctx),
		}

	def _desk_urls(self, ctx: SetupContext) -> dict[str, str]:
		return {
			"system_settings": build_desk_link("System Settings", site_url=ctx.site_url),
			"email_account": build_desk_link("Email Account", site_url=ctx.site_url),
			"ism_settings": build_desk_link("ISM Settings", site_url=ctx.site_url),
			"ism_setup_state": build_desk_link("ISM Setup State", site_url=ctx.site_url),
		}

	def inspect(self, ctx: SetupContext) -> StepInspection:
		checks: list[CheckResult] = []
		is_prod = ctx.profile == PROFILE_PRODUCTION

		# 1. Site URL / host_name
		site_url = ctx.site_url or (frappe.utils.get_url() or "").rstrip("/")
		host_name = str(frappe.conf.get("host_name") or "").strip()
		effective_url = host_name or site_url
		insecure, insecure_reason = _is_local_or_insecure_url(effective_url)
		if is_prod and insecure:
			checks.append(
				CheckResult(
					code="site_url",
					level=CHECK_FAIL,
					message=f"Production site URL check failed: {insecure_reason}",
					remediation="Set a public HTTPS host_name in site_config.json (e.g. https://shop.example.com).",
				)
			)
		elif insecure:
			checks.append(
				CheckResult(
					code="site_url",
					level=CHECK_INFO,
					message=f"Staging site URL is '{effective_url or '(unset)'}'.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="site_url",
					level=CHECK_PASS,
					message=f"Site URL '{effective_url}' uses HTTPS with a non-local host.",
				)
			)

		# 2. System Settings (country, time_zone)
		sys_settings = frappe.get_single("System Settings")
		country = (sys_settings.get("country") or "").strip()
		time_zone = (sys_settings.get("time_zone") or "").strip()
		missing_sys = [
			name for name, val in (("country", country), ("time_zone", time_zone)) if not val
		]
		if missing_sys:
			checks.append(
				CheckResult(
					code="system_settings_locale",
					level=CHECK_FAIL,
					message=f"System Settings missing required field(s): {', '.join(missing_sys)}.",
					remediation="Set country (e.g. Canada) and time_zone (e.g. America/Toronto) in System Settings.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="system_settings_locale",
					level=CHECK_PASS,
					message=f"System Settings configured: country='{country}', time_zone='{time_zone}'.",
				)
			)

		# 3. Default outgoing Email Account
		outgoing_accounts = frappe.get_all(
			"Email Account",
			filters={"enable_outgoing": 1},
			fields=["name", "default_outgoing", "email_id"],
		)
		default_outgoing = [a for a in outgoing_accounts if cint(a.get("default_outgoing"))]
		if default_outgoing:
			checks.append(
				CheckResult(
					code="outgoing_email",
					level=CHECK_PASS,
					message=f"Default outgoing Email Account configured: {default_outgoing[0].name}.",
				)
			)
		elif outgoing_accounts:
			checks.append(
				CheckResult(
					code="outgoing_email",
					level=CHECK_WARN if is_prod else CHECK_INFO,
					message=(
						f"Found {len(outgoing_accounts)} outgoing Email Account(s), but none marked as Default Outgoing."
					),
					remediation="Mark one outgoing Email Account as Default Outgoing in Desk.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="outgoing_email",
					level=CHECK_WARN if is_prod else CHECK_INFO,
					message="No outgoing Email Account (enable_outgoing=1) is configured.",
					remediation="Configure an outgoing SMTP Email Account in Desk > Email Account.",
				)
			)

		# 4. pause_scheduler & disable_emails site_config flags
		pause_scheduler = bool(frappe.conf.get("pause_scheduler"))
		disable_emails = bool(frappe.conf.get("disable_emails"))
		if is_prod:
			if pause_scheduler:
				checks.append(
					CheckResult(
						code="scheduler_status",
						level=CHECK_FAIL,
						message="Background scheduler is PAUSED (pause_scheduler=1) in Production profile.",
						remediation="Remove pause_scheduler from site_config.json and run `bench enable-scheduler`.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="scheduler_status",
						level=CHECK_PASS,
						message="Background scheduler is enabled for Production.",
					)
				)

			if disable_emails:
				checks.append(
					CheckResult(
						code="email_delivery_flag",
						level=CHECK_WARN,
						message="Outbound emails are disabled (disable_emails=1) in site_config.json.",
						remediation="Remove disable_emails from site_config.json before live customer communications.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="email_delivery_flag",
						level=CHECK_PASS,
						message="Outbound email delivery is active in site_config.json.",
					)
				)
		else:
			if pause_scheduler or disable_emails:
				checks.append(
					CheckResult(
						code="staging_safety_flags",
						level=CHECK_PASS,
						message=(
							f"Staging safety guard active (pause_scheduler={pause_scheduler}, "
							f"disable_emails={disable_emails})."
						),
					)
				)
			else:
				checks.append(
					CheckResult(
						code="staging_safety_flags",
						level=CHECK_WARN,
						message="Staging site has live scheduler and emails enabled (neither pause_scheduler nor disable_emails is set).",
						remediation="Consider setting pause_scheduler=1 and/or disable_emails=1 in site_config.json for Staging.",
					)
				)

		# 5. ISM Settings.customer_journey_demo_mode
		demo_mode = cint(frappe.db.get_single_value("ISM Settings", "customer_journey_demo_mode") or 0)
		if is_prod and demo_mode == 1:
			checks.append(
				CheckResult(
					code="demo_mode",
					level=CHECK_FAIL,
					message="ISM Settings.customer_journey_demo_mode is enabled in Production profile.",
					remediation="Disable customer_journey_demo_mode in ISM Settings immediately before Production go-live.",
				)
			)
		elif demo_mode == 1:
			checks.append(
				CheckResult(
					code="demo_mode",
					level=CHECK_INFO,
					message="ISM Settings.customer_journey_demo_mode is enabled (allowed in Staging).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="demo_mode",
					level=CHECK_PASS,
					message="ISM Settings.customer_journey_demo_mode is disabled.",
				)
			)

		# 6. Tier-0 bootstrap checks
		existing_roles = set(
			frappe.get_all("Role", filters={"name": ["in", list(_ISM_STANDARD_ROLES)]}, pluck="name")
		)
		missing_roles = [r for r in _ISM_STANDARD_ROLES if r not in existing_roles]
		if missing_roles:
			checks.append(
				CheckResult(
					code="bootstrap_ism_roles",
					level=CHECK_FAIL,
					message=f"Missing standard ISM role(s): {', '.join(missing_roles)}.",
					remediation="Apply this step with run_bootstrap=True (or run bench migrate).",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="bootstrap_ism_roles",
					level=CHECK_PASS,
					message=f"All {len(_ISM_STANDARD_ROLES)} standard ISM roles exist.",
				)
			)

		has_bin_dim = _has_bin_location_dimension()
		checks.append(
			CheckResult(
				code="bootstrap_bin_dimension",
				level=CHECK_PASS if has_bin_dim else CHECK_FAIL,
				message=(
					f"Inventory Dimension '{BIN_DIMENSION_NAME}' is configured."
					if has_bin_dim
					else f"Inventory Dimension '{BIN_DIMENSION_NAME}' is missing."
				),
				remediation=None if has_bin_dim else "Apply this step with run_bootstrap=True.",
			)
		)

		has_item_bin_field = _has_default_bin_location_field()
		checks.append(
			CheckResult(
				code="bootstrap_item_bin_field",
				level=CHECK_PASS if has_item_bin_field else CHECK_FAIL,
				message=(
					"Custom field 'Item.default_bin_location' exists."
					if has_item_bin_field
					else "Custom field 'Item.default_bin_location' is missing."
				),
				remediation=None if has_item_bin_field else "Apply this step with run_bootstrap=True.",
			)
		)

		existing_entry_types = set(
			frappe.get_all(
				"Schedule Entry Type",
				filters={"name": ["in", list(STANDARD_SCHEDULE_ENTRY_TYPES)]},
				pluck="name",
			)
			if frappe.db.exists("DocType", "Schedule Entry Type")
			else []
		)
		missing_entry_types = sorted(set(STANDARD_SCHEDULE_ENTRY_TYPES) - existing_entry_types)
		if missing_entry_types:
			checks.append(
				CheckResult(
					code="bootstrap_schedule_entry_types",
					level=CHECK_FAIL,
					message=f"Missing standard Schedule Entry Type(s): {', '.join(missing_entry_types)}.",
					remediation="Apply this step with run_bootstrap=True.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="bootstrap_schedule_entry_types",
					level=CHECK_PASS,
					message=f"All {len(STANDARD_SCHEDULE_ENTRY_TYPES)} standard Schedule Entry Types exist.",
				)
			)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		has_warn = any(c.level == CHECK_WARN for c in checks)
		if has_fail:
			status = STEP_STATUS_INCOMPLETE
			summary = f"Site environment ({ctx.profile}) has failing readiness checks."
		elif has_warn:
			status = STEP_STATUS_WARNING
			summary = f"Site environment ({ctx.profile}) ready with warnings."
		else:
			status = STEP_STATUS_COMPLETE
			summary = f"Site environment ({ctx.profile}) and Tier-0 ISM bootstrap are ready."

		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"outgoing_email_accounts": len(outgoing_accounts),
				"ism_roles_present": len(existing_roles),
				"schedule_entry_types_present": len(existing_entry_types),
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

		# 1. Environment profile on ISM Setup State
		if "environment_profile" in payload and payload["environment_profile"] is not None:
			try:
				target_profile = normalize_profile(payload["environment_profile"])
			except ValueError as exc:
				frappe.throw(str(exc), frappe.ValidationError)

			if frappe.db.exists("DocType", "ISM Setup State"):
				state_doc = frappe.get_single("ISM Setup State")
				if (state_doc.environment_profile or "") != target_profile:
					state_doc.environment_profile = target_profile
					state_doc.save(ignore_permissions=True)
					updated.append(f"ISM Setup State.environment_profile={target_profile}")
				else:
					unchanged.append(f"ISM Setup State.environment_profile={target_profile}")
				ctx.setup_state_doc = state_doc
			ctx.profile = target_profile

		# 2. System Settings (country, time_zone)
		sys_doc = frappe.get_single("System Settings")
		sys_dirty = False
		for fieldname in ("country", "time_zone"):
			if fieldname not in payload or payload[fieldname] in (None, ""):
				continue
			val = str(payload[fieldname]).strip()
			if fieldname == "country" and not frappe.db.exists("Country", val):
				frappe.throw(f"Country '{val}' does not exist.", frappe.ValidationError)
			if (sys_doc.get(fieldname) or "").strip() != val:
				sys_doc.set(fieldname, val)
				updated.append(f"System Settings.{fieldname}={val}")
				sys_dirty = True
			else:
				unchanged.append(f"System Settings.{fieldname}={val}")
		if sys_dirty:
			sys_doc.save(ignore_permissions=True)

		# 3. Optional Tier-0 bootstrap
		if cint(payload.get("run_bootstrap")):
			if dry_run:
				warnings.append(
					"Skipped executing ism.setup._run_setup_tasks(full=False) during dry_run to avoid DDL auto-commit."
				)
			else:
				from ism.setup import _run_setup_tasks

				_run_setup_tasks(full=False)
				updated.append("ism.setup._run_setup_tasks(full=False)")

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		opts = dict(options) if isinstance(options, dict) else {}
		inspection = self.inspect(ctx)
		checks = list(inspection.checks)

		test_email = str(opts.get("test_email") or "").strip()
		force = bool(opts.get("force"))
		if test_email:
			if ctx.profile == PROFILE_STAGING and not force:
				checks.append(
					CheckResult(
						code="test_email_guard",
						level=CHECK_FAIL,
						message=(
							f"Refused to send live test email to '{test_email}' in Staging profile."
						),
						remediation="Pass options={'test_email': ..., 'force': True} to explicitly override the Staging email guard.",
					)
				)
			else:
				try:
					frappe.sendmail(
						recipients=[test_email],
						subject=f"[ISM Setup Wizard] Outgoing email verification ({ctx.profile})",
						message="This is a test email triggered by ISM Setup Wizard site_environment.verify().",
						now=True,
					)
					checks.append(
						CheckResult(
							code="test_email_sent",
							level=CHECK_PASS,
							message=f"Sent verification email to '{test_email}'.",
						)
					)
				except Exception as exc:
					checks.append(
						CheckResult(
							code="test_email_failed",
							level=CHECK_FAIL,
							message=f"Failed to send verification email to '{test_email}': {exc}",
							remediation="Check Email Account SMTP credentials and site_config.json disable_emails flag.",
						)
					)

		has_fail = any(c.level == CHECK_FAIL for c in checks)
		passed = not has_fail
		summary = (
			f"Site environment ({ctx.profile}) verification passed."
			if passed
			else f"Site environment ({ctx.profile}) verification failed."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		sys_settings = frappe.get_single("System Settings")
		return {
			"country": sys_settings.get("country") or "",
			"time_zone": sys_settings.get("time_zone") or "",
		}
