# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Optional Capability Pack step: ISM Agent OS Service Account & API Token Handoff."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from ism.setup_wizard.base import BaseSetupStep, build_desk_link
from ism.setup_wizard.contracts import (
	CHECK_FAIL,
	CHECK_PASS,
	STEP_STATUS_COMPLETE,
	STEP_STATUS_INCOMPLETE,
	TIER_OPTIONAL_PACK,
	CheckResult,
	SetupContext,
	StepApplyResult,
	StepInspection,
	StepMeta,
	StepVerifyResult,
)
from ism.setup_wizard.presets import STAFF_ROLE_BUNDLES

DEFAULT_AGENT_OS_EMAIL = "ism-agent@shop.local"
AGENT_OS_ROLE_BUNDLE = "Agent OS Service User"
_EXCLUDED_SYSTEM_USERS = frozenset({"Administrator", "Guest"})


def _required_agent_roles() -> list[str]:
	bundle = STAFF_ROLE_BUNDLES[AGENT_OS_ROLE_BUNDLE]
	return list(bundle["roles"])


def _get_user_roles(user_name: str) -> set[str]:
	return set(
		frappe.get_all(
			"Has Role",
			filters={"parent": user_name, "parenttype": "User"},
			pluck="role",
		)
		or []
	)


def _find_agent_os_candidates(preferred_email: str | None = None) -> list[dict[str, Any]]:
	"""Find enabled non-Administrator System Users that have ISM Manager or match preferred_email."""
	required = set(_required_agent_roles())
	candidate_names: list[str] = []

	if preferred_email and frappe.db.exists("User", preferred_email):
		if preferred_email not in _EXCLUDED_SYSTEM_USERS:
			candidate_names.append(preferred_email)

	if DEFAULT_AGENT_OS_EMAIL not in candidate_names and frappe.db.exists("User", DEFAULT_AGENT_OS_EMAIL):
		candidate_names.append(DEFAULT_AGENT_OS_EMAIL)

	# Any non-Administrator System User with ISM Manager role
	manager_users = frappe.get_all(
		"Has Role",
		filters={"role": "ISM Manager", "parenttype": "User"},
		pluck="parent",
	)
	for uname in manager_users or []:
		if uname not in _EXCLUDED_SYSTEM_USERS and uname not in candidate_names:
			candidate_names.append(uname)

	results: list[dict[str, Any]] = []
	for uname in candidate_names:
		row = frappe.db.get_value(
			"User",
			uname,
			["name", "email", "enabled", "user_type", "api_key"],
			as_dict=True,
		)
		if not row:
			continue
		roles = _get_user_roles(row.name)
		missing_roles = sorted(required - roles)
		has_api_key = bool(str(row.get("api_key") or "").strip())
		results.append({
			"user": row.name,
			"email": row.email or row.name,
			"enabled": bool(cint(row.enabled)),
			"user_type": row.user_type,
			"has_all_roles": not missing_roles,
			"missing_roles": missing_roles,
			"has_api_key": has_api_key,
			"is_preferred": bool(preferred_email and row.name.lower() == preferred_email.lower()),
			"is_default_email": row.name.lower() == DEFAULT_AGENT_OS_EMAIL,
		})

	# Sort so fully-ready or explicitly-named service users appear first
	results.sort(
		key=lambda c: (
			not c["is_preferred"],
			not (c["enabled"] and c["has_all_roles"] and c["has_api_key"]),
			not c["is_default_email"],
			not (c["enabled"] and c["has_all_roles"]),
			c["user"],
		)
	)
	return results


class AgentOsSetupStep(BaseSetupStep):
	"""Provision a dedicated non-Administrator service User and API credentials for ISM Agent OS."""

	meta = StepMeta(
		id="agent_os_setup",
		title="ISM Agent OS Service Account",
		phase="agent_os",
		order=160,
		schema_version=1,
		tier=TIER_OPTIONAL_PACK,
		depends_on=("users_roles_employees",),
		owner_domain="agent_os",
		managed_settings_sections=(),
		allow_skip=True,
		description=(
			"Create or audit a dedicated non-Administrator API service user with the "
			"'Agent OS Service User' role bundle and optional one-time API key handoff."
		),
	)

	def get_schema(self, ctx: SetupContext) -> dict[str, Any]:
		return {
			"step_id": self.meta.id,
			"meta": self.meta.to_dict(),
			"fields": [
				{
					"fieldname": "email",
					"label": "Agent OS Service User Email",
					"fieldtype": "Data",
					"default": DEFAULT_AGENT_OS_EMAIL,
					"reqd": True,
					"read_only": False,
					"is_secret": False,
					"description": "Dedicated non-Administrator service account email for ISM Agent OS.",
				},
				{
					"fieldname": "first_name",
					"label": "First Name",
					"fieldtype": "Data",
					"default": "ISM",
					"reqd": False,
					"read_only": False,
					"is_secret": False,
				},
				{
					"fieldname": "last_name",
					"label": "Last Name",
					"fieldtype": "Data",
					"default": "Agent OS",
					"reqd": False,
					"read_only": False,
					"is_secret": False,
				},
				{
					"fieldname": "generate_api_keys",
					"label": "Generate One-Time API Key & Secret",
					"fieldtype": "Check",
					"default": 0,
					"reqd": False,
					"read_only": False,
					"is_secret": False,
					"description": (
						"When enabled on a live apply, generates fresh Frappe API key/secret and "
						"returns them once in handoff.one_time_credentials for --write-env."
					),
				},
			],
			"secret_fields": [],
			"role_bundle": AGENT_OS_ROLE_BUNDLE,
			"required_roles": _required_agent_roles(),
			"presets": self.get_presets(ctx),
			"desk_urls": {"users": build_desk_link("User", site_url=ctx.site_url)},
		}

	def get_presets(self, ctx: SetupContext) -> list[dict[str, Any]]:
		return [
			{
				"id": "default_agent_os",
				"label": "Default ISM Agent OS Service User",
				"description": (
					f"Creates '{DEFAULT_AGENT_OS_EMAIL}' with the '{AGENT_OS_ROLE_BUNDLE}' "
					"role bundle and generates API keys for .env handoff."
				),
				"payload": {
					"email": DEFAULT_AGENT_OS_EMAIL,
					"first_name": "ISM",
					"last_name": "Agent OS",
					"generate_api_keys": 1,
				},
			}
		]

	def inspect(self, ctx: SetupContext) -> StepInspection:
		candidates = _find_agent_os_candidates()
		ready = [
			c for c in candidates if c["enabled"] and c["has_all_roles"] and c["has_api_key"]
		]
		best = ready[0] if ready else (candidates[0] if candidates else None)

		checks: list[CheckResult] = []
		if not best:
			checks.append(
				CheckResult(
					code="agent_os_user_exists",
					level=CHECK_FAIL,
					message="No dedicated non-Administrator Agent OS service User found.",
					remediation=(
						f"Apply this step to create '{DEFAULT_AGENT_OS_EMAIL}' with the "
						f"'{AGENT_OS_ROLE_BUNDLE}' role bundle."
					),
				)
			)
		else:
			if best["enabled"]:
				checks.append(
					CheckResult(
						code="agent_os_user_enabled",
						level=CHECK_PASS,
						message=f"Dedicated service user '{best['user']}' exists and is enabled.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="agent_os_user_enabled",
						level=CHECK_FAIL,
						message=f"Service user '{best['user']}' is disabled.",
						remediation=f"Re-enable '{best['user']}' or apply this step.",
					)
				)

			if best["has_all_roles"]:
				checks.append(
					CheckResult(
						code="agent_os_user_roles",
						level=CHECK_PASS,
						message=(
							f"Service user '{best['user']}' has all required '{AGENT_OS_ROLE_BUNDLE}' roles."
						),
					)
				)
			else:
				checks.append(
					CheckResult(
						code="agent_os_user_roles",
						level=CHECK_FAIL,
						message=(
							f"Service user '{best['user']}' is missing role(s): "
							f"{', '.join(best['missing_roles'])}."
						),
						remediation=f"Apply this step to assign the '{AGENT_OS_ROLE_BUNDLE}' roles.",
					)
				)

			if best["has_api_key"]:
				checks.append(
					CheckResult(
						code="agent_os_api_key",
						level=CHECK_PASS,
						message=f"Service user '{best['user']}' has an API key generated.",
					)
				)
			else:
				checks.append(
					CheckResult(
						code="agent_os_api_key",
						level=CHECK_FAIL,
						message=f"Service user '{best['user']}' does not have an API key generated.",
						remediation="Apply this step with generate_api_keys=true to generate API credentials.",
					)
				)

		complete = bool(ready)
		status = STEP_STATUS_COMPLETE if complete else STEP_STATUS_INCOMPLETE
		summary = (
			f"Agent OS service user '{ready[0]['user']}' is enabled with required roles and API key."
			if complete
			else "Dedicated Agent OS service user with required roles and API key is not yet ready."
		)
		return StepInspection(
			status=status,
			summary=summary,
			checks=tuple(checks),
			counts={
				"ready_users": len(ready),
				"service_user": best["user"] if best else None,
				"has_api_key": bool(best and best["has_api_key"]),
			},
			desk_urls={"users": build_desk_link("User", site_url=ctx.site_url)},
		)

	def apply(
		self,
		payload: dict[str, Any],
		ctx: SetupContext,
		*,
		dry_run: bool = False,
	) -> StepApplyResult:
		email = str(payload.get("email") or DEFAULT_AGENT_OS_EMAIL).strip().lower()
		if not email or "@" not in email:
			frappe.throw("Agent OS service user 'email' must be a valid email address.", frappe.ValidationError)
		if email in {"administrator", "guest"}:
			frappe.throw(
				"Agent OS must use a dedicated non-Administrator service user.",
				frappe.ValidationError,
			)

		first_name = str(payload.get("first_name") or "ISM").strip() or "ISM"
		last_name = str(payload.get("last_name") or "Agent OS").strip() or "Agent OS"
		required_roles = _required_agent_roles()
		generate_api_keys = bool(cint(payload.get("generate_api_keys")))

		created: list[str] = []
		updated: list[str] = []
		unchanged: list[str] = []
		warnings: list[str] = []
		handoff: dict[str, Any] | None = None

		if not frappe.db.exists("User", email):
			user_doc = frappe.get_doc({
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"last_name": last_name,
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in required_roles],
			})
			user_doc.flags.ignore_password_policy = True
			user_doc.insert(ignore_permissions=True)
			created.append(f"User: {user_doc.name}")
		else:
			user_doc = frappe.get_doc("User", email)
			dirty = False
			if cint(user_doc.enabled) != 1:
				user_doc.enabled = 1
				dirty = True
			if user_doc.user_type != "System User":
				user_doc.user_type = "System User"
				dirty = True

			existing_roles = {row.role for row in (user_doc.roles or [])}
			missing_roles = [r for r in required_roles if r not in existing_roles]
			for role in missing_roles:
				user_doc.append("roles", {"role": role})
				dirty = True

			if dirty:
				user_doc.flags.ignore_password_policy = True
				user_doc.save(ignore_permissions=True)
				updated.append(f"User: {user_doc.name}")
			else:
				unchanged.append(f"User: {user_doc.name}")

		if generate_api_keys:
			if dry_run:
				warnings.append("Dry run: skipped API key/secret generation.")
			else:
				from frappe.core.doctype.user.user import generate_keys

				key_response = generate_keys(user_doc.name) or {}
				api_secret = key_response.get("api_secret")
				api_key = frappe.db.get_value("User", user_doc.name, "api_key")
				updated.append(f"API Keys: {user_doc.name} ([GENERATED])")
				handoff = {
					"one_time_credentials": {
						"user": user_doc.name,
						"api_key": api_key,
						"api_secret": api_secret,
						"frappe_url": ctx.site_url,
						"company": ctx.company,
					}
				}

		return StepApplyResult(
			step_id=self.meta.id,
			dry_run=dry_run,
			applied=bool(created or updated),
			created=tuple(created),
			updated=tuple(updated),
			unchanged=tuple(unchanged),
			warnings=tuple(warnings),
			handoff=handoff,
		)

	def verify(
		self,
		ctx: SetupContext,
		options: dict[str, Any] | None = None,
	) -> StepVerifyResult:
		opts = dict(options) if isinstance(options, dict) else {}
		preferred_email = str(opts.get("email") or "").strip() or None
		candidates = _find_agent_os_candidates(preferred_email=preferred_email)

		if preferred_email:
			candidates = [c for c in candidates if c["user"].lower() == preferred_email.lower()]

		ready = [
			c for c in candidates if c["enabled"] and c["has_all_roles"] and c["has_api_key"]
		]
		best = ready[0] if ready else (candidates[0] if candidates else None)

		checks: list[CheckResult] = []
		if not best:
			target_desc = preferred_email or DEFAULT_AGENT_OS_EMAIL
			checks.append(
				CheckResult(
					code="agent_os_user_exists",
					level=CHECK_FAIL,
					message=f"Dedicated Agent OS service user '{target_desc}' not found.",
					remediation="Apply agent_os_setup to provision the service user.",
				)
			)
		else:
			checks.append(
				CheckResult(
					code="agent_os_user_enabled",
					level=CHECK_PASS if best["enabled"] else CHECK_FAIL,
					message=(
						f"Service user '{best['user']}' is enabled."
						if best["enabled"]
						else f"Service user '{best['user']}' is disabled."
					),
				)
			)
			checks.append(
				CheckResult(
					code="agent_os_user_roles",
					level=CHECK_PASS if best["has_all_roles"] else CHECK_FAIL,
					message=(
						f"Service user '{best['user']}' has all '{AGENT_OS_ROLE_BUNDLE}' roles."
						if best["has_all_roles"]
						else f"Service user '{best['user']}' is missing role(s): {', '.join(best['missing_roles'])}."
					),
				)
			)
			checks.append(
				CheckResult(
					code="agent_os_api_key",
					level=CHECK_PASS if best["has_api_key"] else CHECK_FAIL,
					message=(
						f"Service user '{best['user']}' has an API key configured."
						if best["has_api_key"]
						else f"Service user '{best['user']}' does not have an API key configured."
					),
				)
			)

		passed = not any(c.level == CHECK_FAIL for c in checks)
		summary = (
			f"Agent OS service account '{best['user']}' verified."
			if passed and best
			else "Agent OS service account verification failed."
		)
		return StepVerifyResult(
			step_id=self.meta.id,
			passed=passed,
			summary=summary,
			checks=tuple(checks),
		)

	def export_blueprint(self, ctx: SetupContext) -> dict[str, Any]:
		candidates = _find_agent_os_candidates()
		email = candidates[0]["email"] if candidates else DEFAULT_AGENT_OS_EMAIL
		return {
			"email": email,
			"role_bundle": AGENT_OS_ROLE_BUNDLE,
		}
