# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Whitelisted Frappe API endpoints for the Agent OS-driven ISM Setup Wizard."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ism.api._validation import coerce_bool, optional_str, parse_json_arg, require_str
from ism.setup_wizard import blueprint as _blueprint
from ism.setup_wizard import runner as _runner

SETUP_ADMIN_ROLES = frozenset({"System Manager", "ISM Manager"})


def _assert_setup_admin() -> None:
	"""Allow only Administrator, System Manager, or ISM Manager to access Setup Wizard APIs."""
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		frappe.throw(
			_("Authentication as System Manager or ISM Manager is required."),
			frappe.PermissionError,
		)
	if user == "Administrator":
		return
	roles = set(frappe.get_roles(user))
	if not (roles & SETUP_ADMIN_ROLES):
		frappe.throw(
			_("Only System Manager or ISM Manager users can access ISM Setup Wizard endpoints."),
			frappe.PermissionError,
		)


def _parse_optional_dict(value: str | dict[str, Any] | None, label: str) -> dict[str, Any] | None:
	parsed = parse_json_arg(value, default=None, label=label)
	if parsed is None:
		return None
	if not isinstance(parsed, dict):
		frappe.throw(_("{0} must be a JSON object.").format(label), frappe.ValidationError)
	return parsed


def _parse_steps_arg(steps: str | list[str] | None) -> list[str] | None:
	if steps is None:
		return None
	parsed = parse_json_arg(steps, default=None, label="steps")
	if parsed is None:
		return None
	if isinstance(parsed, str):
		parts = [part.strip() for part in parsed.split(",") if part.strip()]
		return parts or None
	if isinstance(parsed, list):
		result: list[str] = []
		for item in parsed:
			if not isinstance(item, str) or not item.strip():
				frappe.throw(
					_("Each step ID in 'steps' must be a non-empty string."),
					frappe.ValidationError,
				)
			result.append(item.strip())
		return result or None
	frappe.throw(
		_("steps must be a comma-separated string or a list of step IDs."),
		frappe.ValidationError,
	)


@frappe.whitelist(methods=["GET"])
def get_setup_status(
	profile: str | None = None,
	include_checks: int | str = 1,
) -> dict[str, Any]:
	"""Return live setup inspection status across all registered setup wizard steps."""
	_assert_setup_admin()
	return _runner.get_setup_status(
		profile_override=optional_str(profile, "profile"),
		include_checks=coerce_bool(include_checks, default=True),
	)


@frappe.whitelist(methods=["GET"])
def get_step_schema(
	step_id: str | None = None,
	profile: str | None = None,
) -> dict[str, Any]:
	"""Return redacted field schemas, presets, and Desk URLs for one or all setup steps."""
	_assert_setup_admin()
	return _runner.get_step_schema(
		step_id=optional_str(step_id, "step_id"),
		profile_override=optional_str(profile, "profile"),
	)


@frappe.whitelist(methods=["POST"])
def apply_step(
	step_id: str,
	payload: str | dict[str, Any] | None = None,
	preset: str | None = None,
	secrets: str | dict[str, Any] | None = None,
	dry_run: int | str = 0,
	profile: str | None = None,
) -> dict[str, Any]:
	"""Apply configuration for a single setup step (supports dry_run savepoint rollback)."""
	_assert_setup_admin()
	return _runner.apply_step(
		step_id=require_str(step_id, "step_id"),
		payload=_parse_optional_dict(payload, "payload"),
		preset=optional_str(preset, "preset"),
		secrets=_parse_optional_dict(secrets, "secrets"),
		dry_run=coerce_bool(dry_run, default=False),
		profile_override=optional_str(profile, "profile"),
	)


@frappe.whitelist(methods=["POST"])
def skip_step(
	step_id: str,
	reason: str = "",
	unskip: int | str = 0,
) -> dict[str, Any]:
	"""Mark a setup step as skipped (or unskip it back to pending)."""
	_assert_setup_admin()
	return _runner.skip_step(
		step_id=require_str(step_id, "step_id"),
		reason=optional_str(reason, "reason") or "",
		unskip=coerce_bool(unskip, default=False),
	)


@frappe.whitelist(methods=["POST"])
def verify_step(
	step_id: str | None = None,
	options: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Run verification checks for one setup step or all applicable steps."""
	_assert_setup_admin()
	return _runner.verify_step(
		step_id=optional_str(step_id, "step_id"),
		options=_parse_optional_dict(options, "options"),
	)


@frappe.whitelist(methods=["GET"])
def export_blueprint(
	steps: str | list[str] | None = None,
) -> dict[str, Any]:
	"""Export a portable, zero-secret setup blueprint for Staging -> Production carryover."""
	_assert_setup_admin()
	return _blueprint.export_blueprint(steps=_parse_steps_arg(steps))


@frappe.whitelist(methods=["POST"])
def apply_blueprint(
	blueprint: str | dict[str, Any],
	target_profile: str | None = None,
	steps: str | list[str] | None = None,
	dry_run: int | str = 1,
) -> dict[str, Any]:
	"""Replay a setup blueprint in DAG order (defaults to dry_run=1)."""
	_assert_setup_admin()
	parsed_blueprint = _parse_optional_dict(blueprint, "blueprint")
	if parsed_blueprint is None:
		frappe.throw(_("blueprint is required."), frappe.MandatoryError)
	return _blueprint.apply_blueprint(
		blueprint=parsed_blueprint,
		target_profile=optional_str(target_profile, "target_profile"),
		steps=_parse_steps_arg(steps),
		dry_run=coerce_bool(dry_run, default=True),
	)
