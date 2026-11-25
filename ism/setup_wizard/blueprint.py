# Copyright (c) 2026, patricklai3 and contributors
# For license information, please see license.txt

"""Portable, zero-secret Staging -> Production blueprint export and replay."""

from __future__ import annotations

import copy
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from ism.setup_wizard import runner
from ism.setup_wizard.contracts import (
	PROFILE_PRODUCTION,
	assert_no_secrets_in_payload,
	normalize_profile,
)
from ism.setup_wizard.registry import get_ordered_steps, get_step
from ism.setup_wizard.settings_helper import get_ism_settings_password_fields

BLUEPRINT_VERSION = 1

# Settings keys that are valid for Staging walkthroughs but must NEVER be replayed
# as truthy into a Production environment.
STAGING_ONLY_FLAG_KEYS: frozenset[str] = frozenset({"customer_journey_demo_mode"})


def _normalize_step_filter(steps: list[str] | None) -> set[str] | None:
	if steps is None:
		return None
	cleaned: set[str] = set()
	for sid in steps:
		if isinstance(sid, str) and sid.strip():
			cleaned.add(sid.strip())
	return cleaned


def export_blueprint(steps: list[str] | None = None) -> dict[str, Any]:
	"""Export non-secret setup configuration for selected or all applicable steps."""
	ctx = runner.build_context()
	step_filter = _normalize_step_filter(steps)

	if step_filter is not None:
		for requested_id in sorted(step_filter):
			try:
				get_step(requested_id)
			except KeyError:
				frappe.throw(
					_("Unknown setup wizard step '{0}' in export filter.").format(requested_id),
					frappe.DoesNotExistError,
				)

	exported_steps: dict[str, dict[str, Any]] = {}
	for step in get_ordered_steps():
		if step_filter is not None and step.meta.id not in step_filter:
			continue
		if not step.is_applicable(ctx):
			continue
		data = step.export_blueprint(ctx) or {}
		exported_steps[step.meta.id] = {
			"schema_version": step.meta.schema_version,
			"data": data,
		}

	blueprint: dict[str, Any] = {
		"blueprint_version": BLUEPRINT_VERSION,
		"source_profile": ctx.profile,
		"exported_at": now_datetime().isoformat(),
		"steps": exported_steps,
	}

	assert_no_secrets_in_payload(
		blueprint,
		password_fields=get_ism_settings_password_fields(),
	)
	return blueprint


def _strip_staging_flags_for_production(
	step_id: str,
	data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
	"""Remove or force-disable staging-only flags when replaying into Production."""
	cleaned = copy.deepcopy(data)
	warnings: list[str] = []

	def _scrub_mapping(mapping: dict[str, Any], location: str) -> None:
		for key in STAGING_ONLY_FLAG_KEYS:
			if key not in mapping:
				continue
			val = mapping[key]
			is_truthy = (
				val is True
				or (isinstance(val, (int, float)) and cint(val) != 0)
				or (isinstance(val, str) and val.strip().lower() in ("1", "true", "yes", "on"))
			)
			if is_truthy:
				mapping.pop(key, None)
				warnings.append(
					f"Step '{step_id}': stripped staging-only flag '{location}{key}' for Production profile."
				)

	_scrub_mapping(cleaned, "")
	if isinstance(cleaned.get("settings"), dict):
		_scrub_mapping(cleaned["settings"], "settings.")

	return cleaned, warnings


def apply_blueprint(
	blueprint: dict[str, Any],
	*,
	target_profile: str | None = None,
	steps: list[str] | None = None,
	dry_run: bool = True,
) -> dict[str, Any]:
	"""Replay a setup blueprint in deterministic DAG order via runner.apply_step()."""
	if not isinstance(blueprint, dict):
		frappe.throw(_("Blueprint must be a JSON object."), frappe.ValidationError)

	version = blueprint.get("blueprint_version", BLUEPRINT_VERSION)
	if cint(version) != BLUEPRINT_VERSION:
		frappe.throw(
			_("Unsupported blueprint_version '{0}'. Expected {1}.").format(version, BLUEPRINT_VERSION),
			frappe.ValidationError,
		)

	raw_steps = blueprint.get("steps")
	if not isinstance(raw_steps, dict):
		frappe.throw(_("Blueprint 'steps' must be a JSON object mapping step_id -> entry."), frappe.ValidationError)

	if target_profile is not None and str(target_profile).strip():
		try:
			effective_profile = normalize_profile(target_profile)
		except ValueError as exc:
			frappe.throw(str(exc), frappe.ValidationError)
	else:
		effective_profile = runner.build_context().profile

	step_filter = _normalize_step_filter(steps)
	ordered_steps = get_ordered_steps()
	ordered_ids = {step.meta.id for step in ordered_steps}

	warnings: list[str] = []
	for unknown_id in sorted(set(raw_steps.keys()) - ordered_ids):
		if step_filter is None or unknown_id in step_filter:
			warnings.append(f"Skipped unknown blueprint step '{unknown_id}' (not registered on this site).")

	applied_results: list[dict[str, Any]] = []
	for step in ordered_steps:
		sid = step.meta.id
		if sid not in raw_steps:
			continue
		if step_filter is not None and sid not in step_filter:
			continue

		entry = raw_steps[sid]
		if isinstance(entry, dict) and "data" in entry:
			step_data = entry.get("data")
		elif isinstance(entry, dict):
			step_data = entry
		else:
			warnings.append(f"Skipped malformed blueprint entry for step '{sid}'.")
			continue

		if not isinstance(step_data, dict):
			warnings.append(f"Skipped non-object blueprint data for step '{sid}'.")
			continue

		if effective_profile == PROFILE_PRODUCTION:
			step_data, strip_warnings = _strip_staging_flags_for_production(sid, step_data)
			warnings.extend(strip_warnings)

		step_result = runner.apply_step(
			sid,
			step_data,
			dry_run=bool(dry_run),
			profile_override=effective_profile,
		)
		applied_results.append(step_result)

	final_status = runner.get_setup_status(profile_override=effective_profile)
	return {
		"blueprint_version": BLUEPRINT_VERSION,
		"source_profile": blueprint.get("source_profile"),
		"target_profile": effective_profile,
		"dry_run": bool(dry_run),
		"applied_steps": applied_results,
		"warnings": warnings,
		"status": final_status,
	}
